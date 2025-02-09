"""JWT Backend.

Navigator Authentication using JSON Web Tokens.
"""
import logging
import hashlib
import base64
import secrets
from aiohttp import web
from navigator_session import AUTH_SESSION_OBJECT
from datamodel.exceptions import ValidationError
from navigator_auth.exceptions import (
    AuthException,
    FailedAuth,
    UserNotFound,
    InvalidAuth,
)
from navigator_auth.conf import (
    AUTH_PWD_DIGEST,
    AUTH_PWD_ALGORITHM,
    AUTH_PWD_LENGTH,
    AUTH_PWD_SALT_LENGTH,
    AUTH_USER_MODEL
)

# Authenticated Entity
from navigator_auth.identities import AuthUser
from .abstract import BaseAuthBackend


class BasicUser(AuthUser):
    """BasicAuth.

    Basic authenticated user.
    """


# "%s$%d$%s$%s" % (algorithm, iterations, salt, hash)
class BasicAuth(BaseAuthBackend):
    """Basic User/password Authentication."""

    user_attribute: str = "user"
    pwd_atrribute: str = "password"
    _ident: AuthUser = BasicUser
    _description: str = "Basic User/Password authentication"
    _service_name: str = "basic"

    async def on_startup(self, app: web.Application):
        """Used to initialize Backend requirements."""
        ## Using Startup for detecting and loading functions.
        if self._success_callbacks:
            self._user_model = self.get_authmodel(AUTH_USER_MODEL)
            self.get_successful_callbacks()

    async def on_cleanup(self, app: web.Application):
        """Used to cleanup and shutdown any db connection."""

    async def validate_user(self, login: str = None, password: str = None):
        # get the user based on Model
        try:
            search = {self.username_attribute: login}
            user = await self.get_user(**search)
        except ValidationError as ex:
            raise InvalidAuth(
                f"Invalid User Information: {ex.payload}"
            ) from ex
        except UserNotFound as err:
            raise UserNotFound(
                f"User {login} doesn't exists: {err}"
            ) from err
        except Exception as err:
            raise InvalidAuth(
                f"Unknown Exception: {err}"
            ) from err
        try:
            # later, check the password
            pwd = user[self.pwd_atrribute]
        except KeyError as ex:
            raise InvalidAuth(
                "Missing Password attr on User Account"
            ) from ex
        except (ValidationError, TypeError, ValueError) as ex:
            raise InvalidAuth(
                "Invalid credentials on User Account"
            ) from ex
        try:
            if self.check_password(pwd, password):
                # return the user Object
                return user
            else:
                raise FailedAuth(
                    "Basic Auth: Invalid Credentials"
                )
        except FailedAuth:
            raise
        except Exception as err:
            raise InvalidAuth(
                f"Unknown Password Error: {err}"
            ) from err

    def set_password(
        self,
        password: str,
        token_num: int = 6,
        iterations: int = 80000,
        salt: str = None,
    ):
        if not salt:
            salt = secrets.token_hex(token_num)
        key = hashlib.pbkdf2_hmac(
            AUTH_PWD_DIGEST,
            password.encode("utf-8"),
            salt.encode("utf-8"),
            iterations,
            dklen=AUTH_PWD_LENGTH,
        )
        hst = base64.b64encode(key).decode("utf-8").strip()
        return f"{AUTH_PWD_ALGORITHM}${iterations}${salt}${hst}"

    def check_password(self, current_password, password):
        try:
            algorithm, iterations, salt, _ = current_password.split("$", 3)
        except ValueError as ex:
            raise InvalidAuth(
                f"Basic Auth: Invalid Password: {ex}"
            ) from ex
        assert algorithm == AUTH_PWD_ALGORITHM
        compare_hash = self.set_password(
            password,
            iterations=int(iterations),
            salt=salt,
            token_num=AUTH_PWD_SALT_LENGTH,
        )
        try:
            return secrets.compare_digest(current_password, compare_hash)
        except (TypeError, ValueError)  as ex:
            raise InvalidAuth(
                f"Basic Auth: Invalid Credentials: {ex}"
            ) from ex

    async def get_payload(self, request):
        ctype = request.content_type
        if request.method == "GET":
            try:
                user = request.query.get(self.username_attribute, None)
                password = request.query.get(self.pwd_atrribute, None)
                return [user, password]
            except Exception:  # pylint: disable=W0703
                return [None, None]
        elif ctype in (
            "multipart/mixed",
            "multipart/form-data",
            "application/x-www-form-urlencoded",
        ):
            data = await request.post()
            if len(data) > 0:
                user = data.get(self.username_attribute, None)
                password = data.get(self.pwd_atrribute, None)
                return [user, password]
            else:
                return [None, None]
        elif ctype == "application/json":
            try:
                data = await request.json()
                user = data[self.username_attribute]
                password = data[self.pwd_atrribute]
                return [user, password]
            except Exception:  # pylint: disable=W0703
                return [None, None]
        else:
            return [None, None]

    async def authenticate(self, request):
        """Authenticate, refresh or return the user credentials."""
        try:
            user, pwd = await self.get_payload(request)
        except Exception as err:
            raise AuthException(
                str(err),
                status=400
            ) from err
        if not pwd and not user:
            raise InvalidAuth(
                "Basic Auth: Invalid Credentials",
                status=401
            )
        else:
            # making validations
            try:
                user = await self.validate_user(login=user, password=pwd)
            except (FailedAuth, UserNotFound):  # pylint: disable=W0706
                raise
            except (ValidationError, InvalidAuth) as err:
                raise InvalidAuth(str(err), status=401) from err
            except Exception as err:
                raise AuthException(str(err), status=500) from err
            try:
                userdata = self.get_userdata(user)
                username = user[self.username_attribute]
                uid = user[self.userid_attribute]
                userdata[self.username_attribute] = username
                userdata[self.session_key_property] = username
                usr = await self.create_user(userdata[AUTH_SESSION_OBJECT])
                usr.id = uid
                usr.set(self.username_attribute, username)
                payload = {
                    self.user_property: user[self.userid_attribute],
                    self.username_attribute: username,
                    "user_id": uid,
                    self.session_key_property: username,
                }
                # Create the User session and returned.
                token = self.create_jwt(data=payload)
                usr.access_token = token
                ### saving User data into session:
                await self.remember(request, username, userdata, usr)
                ### check if any callbacks exists:
                if user and self._callbacks:
                    # construir e invocar callbacks para actualizar data de usuario
                    args = {
                        "username_attribute": self.username_attribute,
                        "userid_attribute": self.userid_attribute,
                        "userdata": userdata
                    }
                    await self.auth_successful_callback(request, user, **args)
                return {"token": token, **userdata}
            except Exception as err:  # pylint: disable=W0703
                logging.exception(f"BasicAuth: Authentication Error: {err}")
                return False

    async def check_credentials(self, request):
        """Using for check the user credentials to the backend."""
