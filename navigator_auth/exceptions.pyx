# cython: language_level=3, embedsignature=True, boundscheck=False, wraparound=True, initializedcheck=False
# Copyright (C) 2018-present Jesus Lara
#
"""Navigator Auth Exceptions."""
cdef class AuthException(Exception):
    """Base class for other exceptions"""

    status: int = 400

    def __init__(self, str message, int status = 400, **kwargs):
        super().__init__(message)
        self.stacktrace = None
        if 'stacktrace' in kwargs:
            self.stacktrace = kwargs['stacktrace']
        self.message = message
        self.status = int(status)

    def __str__(self):
        return f"{__name__}: {self.message}"

    def get(self):
        return self.message

#### Exceptions:
cdef class ConfigError(AuthException):

    def __init__(self, str message = None):
        super().__init__(message or f"Auth Configuration Error.", status=500)

#### Authentication / Authorization
cdef class UserNotFound(AuthException):

    def __init__(self, str message = None):
        super().__init__(message or "User doesn't exists.", status=404)

cdef class Unauthorized(AuthException):

    def __init__(self, str message = None):
        super().__init__(message or "Unauthorized", status=401)

cdef class InvalidAuth(AuthException):

    def __init__(self, str message = None):
        super().__init__(message or "Invalid Authentication", status=401)

cdef class FailedAuth(AuthException):

    def __init__(self, str message = None):
        super().__init__(message or "Failed Authorization", status=403)

cdef class Forbidden(AuthException):

    def __init__(self, str message = None):
        super().__init__(message or "Forbidden", status=403)

cdef class AuthExpired(AuthException):

    def __init__(self, str message = None):
        super().__init__(message or "Gone: Authentication Expired.", status=410)
