# -*- coding: utf-8 -*-
"""
Application Service Layer Package.
Provides protocol-agnostic, presentation-decoupled services for network management.
"""

from application.exceptions import (
    ApplicationError,
    UnsupportedProtocolError,
    DeviceNotFoundError,
    InvalidRequestError,
    SubscriptionNotFoundError,
)
from application.dto import (
    SupportedProtocol,
    DeliveryModeDTO,
    TelemetryPolicyDTO,
    MutationChangeDTO,
    AdhocEndpointDTO,
    OperationStepDTO,
    ExecutionRequestDTO,
    StepResultDTO,
    ExecutionResultDTO,
    StreamEventDTO,
)
from application.devices import DeviceRegistryService
from application.operations import DeviceOperationService
from application.subscriptions import SubscriptionService
from application.schemas import SchemaCatalogService

__all__ = [
    # Exceptions
    "ApplicationError",
    "UnsupportedProtocolError",
    "DeviceNotFoundError",
    "InvalidRequestError",
    "SubscriptionNotFoundError",
    # DTOs
    "SupportedProtocol",
    "DeliveryModeDTO",
    "TelemetryPolicyDTO",
    "MutationChangeDTO",
    "AdhocEndpointDTO",
    "OperationStepDTO",
    "ExecutionRequestDTO",
    "StepResultDTO",
    "ExecutionResultDTO",
    "StreamEventDTO",
    # Services
    "DeviceRegistryService",
    "DeviceOperationService",
    "SubscriptionService",
    "SchemaCatalogService",
]

