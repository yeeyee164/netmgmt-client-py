# -*- coding: utf-8 -*-
"""
Application Layer Exception Hierarchy.
"""

class ApplicationError(Exception):
    """
    Base exception for all Application Service Layer errors.

    Provides a standardized error code attribute along with the message to allow
    presentation adapters (CLI, REST APIs, WebSockets) to return structured and
    predictable error responses.

    Attributes:
        message (str): Human-readable error description.
        error_code (str): Machine-readable error code string (defaults to 'APPLICATION_ERROR').
    """
    default_error_code: str = "APPLICATION_ERROR"

    def __init__(self, message: str = "", error_code: str | None = None):
        super().__init__(message)
        self.error_code: str = error_code or self.default_error_code


class UnsupportedProtocolError(ApplicationError):
    """
    Raised when an unrecognized, unconfigured, or unsupported protocol is requested.

    Typical causes include requesting a protocol other than 'gnmi' or 'netconf', or
    omitting protocol declaration where an explicit choice is mandatory.
    """
    default_error_code: str = "UNSUPPORTED_PROTOCOL"


class DeviceNotFoundError(ApplicationError):
    """
    Raised when a requested device identifier is not found in the device registry.

    Occurs when an ExecutionRequestDTO specifies a 'device_id' that has not been
    registered in DeviceRegistryService.
    """
    default_error_code: str = "DEVICE_NOT_FOUND"


class InvalidRequestError(ApplicationError):
    """
    Raised when an ExecutionRequestDTO violates structural or business validation constraints.

    Examples include out-of-range timeouts or repetition counts, invalid datastore names,
    unsupported mutation operations, malformed selectors, or protocol-operation mismatches
    detected at the application boundary prior to network I/O.
    """
    default_error_code: str = "INVALID_REQUEST"


class SubscriptionNotFoundError(ApplicationError):
    """
    Raised when a subscription identifier is not found in the active subscription registry.

    Occurs when querying status, polling, consuming events from, or stopping a subscription
    using a non-existent or already terminated subscription_id token.
    """
    default_error_code: str = "SUBSCRIPTION_NOT_FOUND"

