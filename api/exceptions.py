# -*- coding: utf-8 -*-
"""
Safe API exception mapping, HTTP status resolution, and credential/path redaction.
"""

import logging
import re
from typing import Optional

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from application.exceptions import (
    ApplicationError,
    DeviceNotFoundError,
    InvalidRequestError,
    PermissionDeniedError,
    SessionConflictError,
    SessionNotFoundError,
    SubscriptionNotFoundError,
    UnsupportedProtocolError,
)

logger = logging.getLogger(__name__)

_SECRET_KV_PATTERN = re.compile(
    r"(?i)(\b(?:password|passwd|secret|token|api_key|apikey|ssh_key|tls_key)\b\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)
_PEM_KEY_PATTERN = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.MULTILINE,
)
_SENSITIVE_PATH_PATTERN = re.compile(
    r"(?:(?:/[^\s'\",;]+)+/(?:[^/\s'\",;]+\.(?:key|pem|p12|pfx)|id_rsa|id_ed25519|id_ecdsa)|/etc/ssl/[^\s'\",;]+|/[^\s'\",;]+/\.ssh/[^\s'\",;]+)"
)


class APIAuthenticationError(ApplicationError):
    """Raised when request authentication credentials are missing or invalid."""

    default_error_code: str = "UNAUTHORIZED"


class APIAuthorizationError(ApplicationError):
    """Raised when an authenticated principal lacks permission for the requested action."""

    default_error_code: str = "FORBIDDEN"


def redact_sensitive_text(text: Optional[str]) -> Optional[str]:
    """
    Redacts passwords, tokens, private keys, and sensitive filesystem paths from strings
    before returning API responses or emitting logs.
    """
    if text is None:
        return None
    sanitized = _PEM_KEY_PATTERN.sub("[REDACTED_PRIVATE_KEY]", str(text))
    sanitized = _SECRET_KV_PATTERN.sub(r"\1[REDACTED]", sanitized)
    sanitized = _SENSITIVE_PATH_PATTERN.sub("[REDACTED_PATH]", sanitized)
    return sanitized


def status_code_for_application_error(exc: ApplicationError) -> int:
    """Maps an ASL or API ApplicationError to an appropriate HTTP status code."""
    if isinstance(exc, APIAuthenticationError):
        return status.HTTP_401_UNAUTHORIZED
    if isinstance(exc, (PermissionDeniedError, APIAuthorizationError)):
        return status.HTTP_403_FORBIDDEN
    if isinstance(
        exc, (DeviceNotFoundError, SubscriptionNotFoundError, SessionNotFoundError)
    ):
        return status.HTTP_404_NOT_FOUND
    if isinstance(exc, SessionConflictError):
        return status.HTTP_409_CONFLICT
    if isinstance(exc, (InvalidRequestError, UnsupportedProtocolError)):
        return status.HTTP_400_BAD_REQUEST

    code = (getattr(exc, "error_code", "") or "").upper()
    if "NOT_FOUND" in code:
        return status.HTTP_404_NOT_FOUND
    if "DENIED" in code or "FORBIDDEN" in code:
        return status.HTTP_403_FORBIDDEN
    if "CONFLICT" in code:
        return status.HTTP_409_CONFLICT
    if "INVALID" in code or "UNSUPPORTED" in code or "MISSING" in code:
        return status.HTTP_400_BAD_REQUEST
    if "CONNECT" in code:
        return status.HTTP_502_BAD_GATEWAY
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def register_exception_handlers(app: FastAPI) -> None:
    """Registers centralized, secret-safe exception handlers on the FastAPI app."""

    @app.exception_handler(ApplicationError)
    async def handle_application_error(
        request: Request, exc: ApplicationError
    ) -> JSONResponse:
        http_status = status_code_for_application_error(exc)
        error_code = getattr(exc, "error_code", "APPLICATION_ERROR")
        safe_message = redact_sensitive_text(str(exc)) or error_code
        headers = (
            {"WWW-Authenticate": "Bearer"}
            if http_status == status.HTTP_401_UNAUTHORIZED
            else None
        )
        return JSONResponse(
            status_code=http_status,
            content={
                "error_code": error_code,
                "message": safe_message,
                "target": None,
            },
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.error(
            "Unhandled API exception on %s %s: %s",
            request.method,
            request.url.path,
            redact_sensitive_text(str(exc)),
            exc_info=True,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error_code": "INTERNAL_SERVER_ERROR",
                "message": "An unexpected internal error occurred.",
                "target": None,
            },
        )
