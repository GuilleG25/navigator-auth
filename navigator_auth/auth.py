"""Navigator Auth.

Navigator Authentication/Authorization system.

AuthHandler is the Authentication/Authorization system for NAV,
Supporting:
 * multiple authentication backends
 * authorization exceptions via middlewares
 * Session Support (on top of navigator-session)
"""
import importlib
import inspect
from typing import Union
from collections.abc import Awaitable, Callable, Iterable
import orjson
from orjson import JSONDecodeError
import aiohttp_cors
from aiohttp import hdrs, web
from aiohttp.abc import AbstractView
from aiohttp.web_urldispatcher import SystemRoute
from navigator_session import SESSION_KEY, SessionHandler, get_session

from .authorizations import authz_allow_hosts, authz_hosts
from .backends.abstract import decode_token
from .conf import (
    AUTH_CREDENTIALS_REQUIRED,
    AUTH_USER_VIEW,
    AUTHENTICATION_BACKENDS,
    AUTHORIZATION_BACKENDS,
    AUTHORIZATION_MIDDLEWARES,
    default_dsn,
    REDIS_AUTH_URL,
    logging,
    exclude_list,
)
from .exceptions import (
    AuthException,
    AuthExpired,
    ConfigError,
    FailedAuth,
    Forbidden,
    InvalidAuth,
    UserNotFound,
)
from .handlers import handler_routes

## Responses
from .libs.json import JSONContent
from .responses import JSONResponse
from .storages.postgres import PostgresStorage
from .storages.redis import RedisStorage


url = logging.getLogger("urllib3.connectionpool")
url.setLevel(logging.WARNING)


class AuthHandler:
    """Authentication Backend for Navigator."""

    name: str = "auth"
    app: web.Application = None
    secure_cookies: bool = True

    def __init__(
        self, app_name: str = "auth", secure_cookies: bool = True, **kwargs
    ) -> None:
        self.name: str = app_name
        self.backends: dict = {}
        self._session = None
        self.secure_cookies = secure_cookies
        if "scheme" in kwargs:
            self.auth_scheme = kwargs["scheme"]
        else:
            self.auth_scheme = "Bearer"
        # Get User Model:
        try:
            user_model = self.get_usermodel(AUTH_USER_VIEW)
        except Exception as ex:
            raise ConfigError(f"Error Getting Auth User Model: {ex}") from ex
        args = {
            "scheme": self.auth_scheme,
            "user_model": user_model,
            **kwargs,
        }
        # get the authentication backends (all of the list)
        self.backends = self.get_backends(**args)
        self._middlewares = self.get_authorization_middlewares(
            AUTHORIZATION_MIDDLEWARES
        )
        self._authz_backends: list = self.get_authorization_backends(
            AUTHORIZATION_BACKENDS
        )
        # TODO: Session Support with parametrization (other backends):
        self._session = SessionHandler(
            storage="redis", use_cookie=True
        )  # pylint: disable=E1123
        ### JSON encoder
        self._json = JSONContent()

    @property
    def session(self):
        return self._session

    async def auth_startup(self, app):
        """
        Some Authentication backends need to call an Startup.
        """
        for name, backend in self.backends.items():
            try:
                await backend.on_startup(app)
            except Exception as err:
                logging.exception(
                    f"Error on Startup Auth Backend {name} init: {err.message}"
                )
                raise AuthException(
                    f"Error on Startup Auth Backend {name} init: {err.message}"
                ) from err

    async def on_cleanup(self, app):
        """
        Cleanup the processes
        """
        for name, backend in self.backends.items():
            try:
                await backend.on_cleanup(app)
            except Exception as err:
                print(err)
                logging.exception(
                    f"Error on Cleanup Auth Backend {name} init: {err.message}"
                )
                raise AuthException(
                    f"Error on Cleanup Auth Backend {name} init: {err.message}"
                ) from err

    def get_backends(self, **kwargs):
        backends = {}
        for backend in AUTHENTICATION_BACKENDS:
            try:
                parts = backend.split(".")
                bkname = parts[-1]
                classpath = ".".join(parts[:-1])
                module = importlib.import_module(classpath, package=bkname)
                obj = getattr(module, bkname)
                logging.debug(f"Auth: Loading Backend {bkname}")
                backends[bkname] = obj(**kwargs)
            except ImportError as ex:
                raise ConfigError(f"Error loading Auth Backend {backend}: {ex}") from ex
        return backends

    def get_usermodel(self, model: str):
        try:
            parts = model.split(".")
            name = parts[-1]
            classpath = ".".join(parts[:-1])
            module = importlib.import_module(classpath, package=name)
            obj = getattr(module, name)
            return obj
        except ImportError as ex:
            raise ConfigError(
                f"Auth: Error loading Auth User Model {model}: {ex}"
            ) from ex

    def get_authorization_backends(self, backends: Iterable) -> tuple:
        b = []
        for backend in backends:
            # TODO: more automagic logic
            if backend == "hosts":
                b.append(authz_hosts())
            elif backend == "allow_hosts":
                b.append(authz_allow_hosts())
        return b

    def get_authorization_middlewares(self, backends: Iterable) -> tuple:
        b = tuple()
        for backend in backends:
            try:
                parts = backend.split(".")
                bkname = parts[-1]
                classpath = ".".join(parts[:-1])
                module = importlib.import_module(classpath, package=bkname)
                obj = getattr(module, bkname)
                b.append(obj)
            except ImportError as ex:
                raise RuntimeError(
                    f"Error loading Authz Middleware {backend}: {ex}"
                ) from ex
        return b

    async def api_logout(self, request: web.Request) -> web.Response:
        """Logout.
        API-based Logout.
        """
        try:
            response = web.json_response(
                {"message": "Logout successful", "state": 202}, status=202
            )
            await self._session.storage.forgot(request, response)
            return response
        except Exception as err:
            print(err)
            raise web.HTTPUnauthorized(reason=f"Logout Error {err.message}")

    async def api_login(self, request: web.Request) -> web.Response:
        """Login.

        API based login.
        """
        # first: getting header for an existing backend
        method = request.headers.get("X-Auth-Method")
        userdata = None
        if method:
            try:
                backend = self.backends[method]
            except (TypeError, KeyError) as ex:
                raise self.Unauthorized(
                    reason=f"Unacceptable Auth Method: {method}"
                ) from ex
            try:
                userdata = await backend.authenticate(request)
                if not userdata:
                    raise self.ForbiddenAccess(
                        reason="User was not authenticated"
                    )
            except UserNotFound as err:
                raise self.Unauthorized(
                    reason=f"User Doesn't exists: {err.message}",
                    exception=err
                )
            except Forbidden as err:
                raise self.ForbiddenAccess(
                    reason=f"{err.message}"
                )
            except FailedAuth as err:
                raise self.ForbiddenAccess(
                    reason="Failed Authentication",
                    exception=err
                )
            except InvalidAuth as err:
                logging.exception(err)
                raise self.ForbiddenAccess(
                    reason=f"{err.message}",
                    exception=err
                )
            except Exception as err:
                raise self.auth_error(
                    reason=f"Auth Exception: {err}",
                    exception=err
                )
        else:
            # second: if no backend declared, will iterate over all backends
            userdata = None
            for _, backend in self.backends.items():
                try:
                    # check credentials for all backends
                    userdata = await backend.authenticate(request)
                    if userdata:
                        break
                except (AuthException, UserNotFound, InvalidAuth, FailedAuth) as err:
                    continue
                except Exception as err:
                    raise self.auth_error(
                        reason=f"Auth Exception: {err}",
                        exception=err
                    )
        # if not userdata, then raise an not Authorized
        if not userdata:
            raise self.ForbiddenAccess(
                reason="Login Failure in all Auth Methods."
            )
        else:
            # at now: create the user-session
            try:
                response = JSONResponse(userdata, status=200)
                await self._session.storage.load_session(
                    request, userdata, response=response
                )
            except Exception as err:
                raise self.Unauthorized(
                    reason=f"Error Creating User Session: {err.message}",
                    exception=err
                ) from err
            return response

    ### Auth Methods:
    async def auth_methods(self, request: web.Request) -> web.Response:
        """auth_methods.

        Return information about enabled auth backends.
        Args:
            request (web.Request): _description_

        Returns:
            web.Response: _description_
        """
        response = {}
        if request.method == "GET":
            # get info about all enable backends
            for name, backend in self.backends.items():
                response[name] = backend.get_backend_info()
        else:
            try:
                backends = await request.json(loads=orjson.loads)
                for name, backend in self.backends.items():
                    bk = backend.get_backend_info()
                    if bk.name in backends:
                        response[name] = backend.get_backend_info()
            except JSONDecodeError as err:
                raise web.HTTPClientError(reason=f"Invalid POST DATA: {err!s}") from err
        return JSONResponse(response, status=200)

    # Session Methods:
    async def forgot_session(self, request: web.Request):
        await self._session.storage.forgot(request)

    async def create_session(self, request: web.Request, data: Iterable):
        return await self._session.storage.new_session(request, data)

    async def get_session(self, request: web.Request) -> web.Response:
        """Get user data from session."""
        session = None
        try:
            session = await self._session.storage.get_session(request)
        except AuthException as err:
            response = {
                "message": "Session Error",
                "error": err.message,
                "status": err.state,
            }
            return JSONResponse(response, status=err.state)
        except Exception as err:
            raise web.HTTPClientError(reason=err) from err
        if not session:
            try:
                session = await self._session.storage.get_session(request)
            except Exception:  # pylint: disable=W0703
                # always return a null session for user:
                session = await self._session.storage.new_session(request, {})
        if isinstance(session, bool):
            # missing User Data:
            userdata = {}
        else:
            userdata = dict(session)
        try:
            del userdata["user"]
        except KeyError:
            pass
        return JSONResponse(userdata, status=200)

    async def get_auth(self, request: web.Request) -> str:
        """
        Get the current User ID from Request
        """
        return request.get(SESSION_KEY, None)

    async def get_userdata(self, request: web.Request) -> str:
        """
        Get the current User ID from Request
        """
        data = request.get(self.user_property, None)
        if data:
            return data
        else:
            raise web.HTTPForbidden(reason="Auth: User Data is missing on Request.")

    def setup_cors(self, cors):
        for route in list(self.app.router.routes()):
            try:
                if inspect.isclass(route.handler) and issubclass(
                    route.handler, AbstractView
                ):
                    cors.add(route, webview=True)
                else:
                    cors.add(route)
            except (TypeError, ValueError):
                pass

    def setup(self, app: web.Application) -> web.Application:
        if isinstance(app, web.Application):
            self.app = app  # register the app into the Extension
        else:
            self.app = app.get_app()  # Nav Application
        ## load the Session System
        # configuring Session Object
        self._session.setup(self.app)
        ## Manager for Auth Storage and Policy Storage
        ## adding a Redis Connection:
        try:
            redis = RedisStorage(driver="redis", dsn=REDIS_AUTH_URL)
            redis.configure(self.app)  # pylint: disable=E1123
        except RuntimeError as ex:
            raise web.HTTPServerError(reason=f"Error creating Redis connection: {ex}")
        ## getting Database Connection:
        try:
            pool = PostgresStorage(driver="pg", dsn=default_dsn)
            pool.configure(self.app)  # pylint: disable=E1123
        except RuntimeError as ex:
            raise web.HTTPServerError(
                reason=f"Error creating Database connection: {ex}"
            )
        # startup operations over extension backend
        self.app.on_startup.append(self.auth_startup)
        # cleanup operations over Auth backend
        self.app.on_cleanup.append(self.on_cleanup)
        logging.debug(":::: Auth Handler Loaded ::::")
        # register the Auth extension into the app
        self.app[self.name] = self
        ## Configure Routes
        router = self.app.router
        router.add_route("GET", "/api/v1/login", self.api_login, name="api_login")
        router.add_route("POST", "/api/v1/login", self.api_login, name="api_login_post")
        router.add_route("GET", "/api/v1/logout", self.api_logout, name="api_logout")
        # get the session information for a program (only)
        router.add_route(
            "GET",
            "/api/v1/session/{program}",
            self.get_session,
            name="api_session_tenant",
        )
        # get all user information
        router.add_route(
            "GET", "/api/v1/user/session", self.get_session, name="api_session"
        )
        ### get info about auth methods
        router.add_route(
            "GET",
            "/api/v1/auth/methods",
            self.auth_methods,
            name="api_get_auth_methods",
        )
        router.add_route(
            "POST",
            "/api/v1/auth/methods",
            self.auth_methods,
            name="api_get_auth_methods",
        )
        ### Handler for Auth Objects:
        handler_routes(router)
        # the backend add a middleware to the app
        mdl = self.app.middlewares
        # if authentication backend needs initialization
        for name, backend in self.backends.items():
            try:
                # backend.configure(app, router, handler=app)
                backend.configure(self.app, router)
                if hasattr(backend, "auth_middleware"):
                    # add the middleware for this backend Authentication
                    mdl.append(backend.auth_middleware)
            except Exception as err:
                logging.exception(f"Auth: Error on Backend {name} init: {err!s}")
                raise ConfigError(
                    f"Auth: Error on Backend {name} init: {err!s}"
                ) from err
        # last: add the basic jwt middleware (used by basic auth and others)
        mdl.append(self.auth_middleware)

        # at the End: configure CORS for routes:
        cors = aiohttp_cors.setup(
            self.app,
            defaults={
                "*": aiohttp_cors.ResourceOptions(
                    allow_credentials=True,
                    expose_headers="*",
                    allow_methods="*",
                    allow_headers="*",
                    max_age=3600,
                )
            },
        )
        self.setup_cors(cors)
        return self.app

    async def get_session_user(self, session: Iterable, name: str = "user") -> Iterable:
        try:
            if session:
                user = session.decode(name)
                if user:
                    user.is_authenticated = True
                return user
        except (AttributeError, RuntimeError) as ex:
            logging.warning(f"NAV: Unable to decode User session: {ex}")

    def default_headers(self, message: str, exception: BaseException = None) -> dict:
        headers = {
            "X-AUTH": message,
        }
        if exception:
            headers['X-ERROR'] = str(exception)
        return headers

    def auth_error(
        self,
        reason: dict = None,
        exception: Exception = None,
        status: int = 400,
        headers: dict = None,
        content_type: str = 'application/json',
        **kwargs,
    ) -> web.HTTPError:
        if headers:
            headers = {**self.default_headers(message=str(reason), exception=exception), **headers}
        else:
            headers = self.default_headers(message=str(reason), exception=exception)
        # TODO: process the exception object
        response_obj = {
            "status": status
        }
        if exception:
            response_obj["error"] = str(exception)
        args = {
            "content_type": content_type,
            "headers": headers,
            **kwargs
        }
        if isinstance(reason, dict):
            response_obj = {**response_obj, **reason}
            # args["content_type"] = "application/json"
            args["body"] = self._json.dumps(response_obj)
        else:
            response_obj['reason'] = reason
            args["body"] = self._json.dumps(response_obj)
        # defining the error
        if status == 400:  # bad request
            obj = web.HTTPBadRequest(**args)
        elif status == 401:  # unauthorized
            obj = web.HTTPUnauthorized(**args)
        elif status == 403:  # forbidden
            obj = web.HTTPForbidden(**args)
        elif status == 404:  # not found
            obj = web.HTTPNotFound(**args)
        elif status == 406: # Not acceptable
            obj = web.HTTPNotAcceptable(**args)
        elif status == 412:
            obj = web.HTTPPreconditionFailed(**args)
        elif status == 428:
            obj = web.HTTPPreconditionRequired(**args)
        else:
            obj = web.HTTPBadRequest(**args)
        return obj

    def ForbiddenAccess(self, reason: Union[str, dict], **kwargs) -> web.HTTPError:
        return self.auth_error(
            reason=reason, **kwargs, status=403
        )

    def Unauthorized(self, reason: Union[str, dict], **kwargs) -> web.HTTPError:
        return self.auth_error(
            reason=reason, **kwargs, status=401
        )

    @web.middleware
    async def auth_middleware(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        """
        Basic Auth Middleware.
        Description: Basic Authentication for NoAuth, Basic, Token and Django.
        """
        # avoid authorization backend on excluded methods:
        if request.method == hdrs.METH_OPTIONS:
            return await handler(request)
        # avoid authorization on exclude list
        if request.path in exclude_list:
            return await handler(request)
        # avoid check system routes
        try:
            if isinstance(request.match_info.route, SystemRoute):  # eg. 404
                return await handler(request)
        except Exception:  # pylint: disable=W0703
            pass
        ### Authorization backends:
        for backend in self._authz_backends:
            if await backend.check_authorization(request):
                return await handler(request)
        ## Already Authenticated
        try:
            if request.get("authenticated", False) is True:
                return await handler(request)
        except KeyError:
            pass
        logging.debug(":: AUTH MIDDLEWARE ::")
        try:
            _, payload = decode_token(request)
            if payload:
                ## check if user has a session:
                # load session information
                session = await get_session(request, payload, new=False)
                if not session:
                    if AUTH_CREDENTIALS_REQUIRED is True:
                        raise self.Unauthorized(
                            reason="There is no Session for User or Authentication is missing"
                        )
                try:
                    request.user = await self.get_session_user(session)
                    request["authenticated"] = True
                except Exception as ex:  # pylint: disable=W0703
                    logging.error(f"Missing User Object from Session: {ex}")
            elif self.secure_cookies is True:
                session = await get_session(request, None, new=False)
                if not session:
                    if AUTH_CREDENTIALS_REQUIRED is True:
                        raise self.Unauthorized(
                            reason="There is no Session for User or Authentication is missing"
                        )
                request.user = await self.get_session_user(session)
                request["authenticated"] = True
        except Forbidden as err:
            logging.error(
                "Auth Middleware: Access Denied"
            )
            raise self.Unauthorized(reason=err.message)
        except AuthExpired as err:
            logging.error("Auth Middleware: Auth Credentials were expired")
            raise self.Unauthorized(reason=err.message, exception=err)
        except FailedAuth as err:
            raise self.ForbiddenAccess(reason=err.message, exception=err)
        except AuthException as err:
            logging.error("Auth Middleware: Invalid Signature, secret or authentication failed.")
            raise self.Unauthorized(
                reason="Auth Middleware: Invalid Signature, secret or authentication failed.",
                exception=err
            )
        return await handler(request)
