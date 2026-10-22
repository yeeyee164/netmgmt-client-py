# -*- coding: utf-8 -*-
"""
Application Layer Exception Hierarchy.
"""

class ApplicationError(Exception):
    """Base exception for all application service layer errors."""


class UnsupportedProtocolError(ApplicationError):
    """Raised when an unrecognized, unconfigured, or unsupported protocol is requested."""


class DeviceNotFoundError(ApplicationError):
    """Raised when a requested device_id is not found in the device registry."""


class InvalidRequestError(ApplicationError):
    """Raised when an ExecutionRequestDTO violates structural or business validation constraints."""


class SubscriptionNotFoundError(ApplicationError):
    """Raised when a subscription_id is not found in the active subscription registry."""

