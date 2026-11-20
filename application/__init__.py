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
    SessionNotFoundError,
    SessionConflictError,
    PermissionDeniedError,
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
    SessionInfoDTO,
    expand_request_repetitions,
)
from application.devices import DeviceRegistryService, load_endpoints_from_env_file
from application.operations import DeviceOperationService, normalize_protocol_payload
from application.subscriptions import SubscriptionService, SubscriptionState
from application.schemas import SchemaCatalogService
from application.sessions import (
    DeviceSessionManager,
    LogicalSessionState,
    SessionLease,
)

__all__ = [
    # Exceptions
    "ApplicationError",
    "UnsupportedProtocolError",
    "DeviceNotFoundError",
    "InvalidRequestError",
    "SubscriptionNotFoundError",
    "SessionNotFoundError",
    "SessionConflictError",
    "PermissionDeniedError",
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
    "SessionInfoDTO",
    "expand_request_repetitions",
    # Services
    "DeviceRegistryService",
    "load_endpoints_from_env_file",
    "DeviceOperationService",
    "normalize_protocol_payload",
    "SubscriptionService",
    "SubscriptionState",
    "SchemaCatalogService",
    "DeviceSessionManager",
    "LogicalSessionState",
    "SessionLease",
]

