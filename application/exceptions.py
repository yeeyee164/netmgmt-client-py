# -*- coding: utf-8 -*-
"""
Application Layer Exception Hierarchy.
"""

class ApplicationError(Exception):
    """Base exception for all application service layer errors."""
    default_error_code: str = "APPLICATION_ERROR"

    def __init__(self, message: str = "", error_code: str | None = None):
        super().__init__(message)
        self.error_code: str = error_code or self.default_error_code


class UnsupportedProtocolError(ApplicationError):
    """Raised when an unrecognized, unconfigured, or unsupported protocol is requested."""
    default_error_code: str = "UNSUPPORTED_PROTOCOL"


class DeviceNotFoundError(ApplicationError):
    """Raised when a requested device_id is not found in the device registry."""
    default_error_code: str = "DEVICE_NOT_FOUND"


class InvalidRequestError(ApplicationError):
    """Raised when an ExecutionRequestDTO violates structural or business validation constraints."""
    default_error_code: str = "INVALID_REQUEST"


class SubscriptionNotFoundError(ApplicationError):
    """Raised when a subscription_id is not found in the active subscription registry."""
    default_error_code: str = "SUBSCRIPTION_NOT_FOUND"

