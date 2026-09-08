"""OpenAI-shaped error envelope.

The real Batch API returns errors as {"error": {"message", "type",
"param", "code"}}. We mirror that shape everywhere so the official SDKs
raise their normal exception types against this server.
"""

from __future__ import annotations

from fastapi import HTTPException


class ApiError(HTTPException):
    def __init__(
        self, status_code: int, message: str, *, code: str, error_type: str, param: str | None = None
    ):
        super().__init__(
            status_code=status_code,
            detail={
                "error": {
                    "message": message,
                    "type": error_type,
                    "param": param,
                    "code": code,
                }
            },
        )


class InvalidRequestError(ApiError):
    def __init__(self, message: str, *, code: str = "invalid_request", param: str | None = None):
        super().__init__(400, message, code=code, error_type="invalid_request_error", param=param)


class AuthenticationError(ApiError):
    def __init__(self, message: str = "Invalid API key."):
        super().__init__(401, message, code="invalid_api_key", error_type="authentication_error")


class PermissionDeniedError(ApiError):
    def __init__(self, message: str = "You do not have access to this resource."):
        super().__init__(403, message, code="permission_denied", error_type="permission_error")


class NotFoundError(ApiError):
    def __init__(self, message: str = "Resource not found."):
        super().__init__(404, message, code="not_found", error_type="invalid_request_error")


class InsufficientQuotaError(ApiError):
    def __init__(self, message: str):
        super().__init__(429, message, code="insufficient_quota", error_type="insufficient_quota_error")


class ConflictError(ApiError):
    def __init__(self, message: str):
        super().__init__(409, message, code="conflict", error_type="invalid_request_error")
