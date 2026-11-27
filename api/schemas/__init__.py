# -*- coding: utf-8 -*-
"""
FastAPI Pydantic v2 HTTP/WebSocket schemas package.
"""

from api.schemas.common import (
    APIErrorResponseSchema,
    DatastoreLiteral,
    DeliveryModeLiteral,
    FilterTypeLiteral,
    HealthResponseSchema,
    MutationOperationLiteral,
    ProtocolLiteral,
    ReadinessResponseSchema,
    StrictRequestModel,
)
from api.schemas.devices import (
    AdhocEndpointSchema,
    DeviceProfileResponseSchema,
    DeviceRegisterRequestSchema,
)
from api.schemas.operations import (
    ExecutionRequestSchema,
    ExecutionResultSchema,
    MutationChangeSchema,
    OperationStepSchema,
    StepResultSchema,
    TelemetryPolicySchema,
)
from api.schemas.schemas import (
    SchemaModuleSchema,
    SchemaStatisticsResponseSchema,
    SchemaValidateRequestSchema,
    SchemaValidateResponseSchema,
)
from api.schemas.sessions import (
    SessionCreateRequestSchema,
    SessionExecutionRequestSchema,
    SessionInfoResponseSchema,
)
from api.schemas.subscriptions import (
    StreamEventSchema,
    SubscriptionCommandResponseSchema,
    SubscriptionCreateRequestSchema,
    SubscriptionCreateResponseSchema,
    SubscriptionInfoResponseSchema,
)
from api.schemas.stats import (
    ExecutionStatsSchema,
    ExecutionStepStatsSchema,
    LatencySummarySchema,
    OperationStatsSummarySchema,
    RuntimeHealthStatsSchema,
    SchemaStatsSummarySchema,
    SessionStatsSummarySchema,
    StatsResetResponseSchema,
    StatsSnapshotResponseSchema,
    SubscriptionStatsResponseSchema,
)

__all__ = [
    "StrictRequestModel",
    "ProtocolLiteral",
    "DatastoreLiteral",
    "FilterTypeLiteral",
    "MutationOperationLiteral",
    "DeliveryModeLiteral",
    "APIErrorResponseSchema",
    "HealthResponseSchema",
    "ReadinessResponseSchema",
    "AdhocEndpointSchema",
    "DeviceRegisterRequestSchema",
    "DeviceProfileResponseSchema",
    "MutationChangeSchema",
    "TelemetryPolicySchema",
    "OperationStepSchema",
    "ExecutionRequestSchema",
    "StepResultSchema",
    "ExecutionResultSchema",
    "SessionCreateRequestSchema",
    "SessionExecutionRequestSchema",
    "SessionInfoResponseSchema",
    "SubscriptionCreateRequestSchema",
    "SubscriptionCreateResponseSchema",
    "SubscriptionInfoResponseSchema",
    "SubscriptionCommandResponseSchema",
    "StreamEventSchema",
    "SchemaModuleSchema",
    "SchemaStatisticsResponseSchema",
    "SchemaValidateRequestSchema",
    "SchemaValidateResponseSchema",
    "LatencySummarySchema",
    "ExecutionStepStatsSchema",
    "ExecutionStatsSchema",
    "SubscriptionStatsResponseSchema",
    "OperationStatsSummarySchema",
    "SessionStatsSummarySchema",
    "SchemaStatsSummarySchema",
    "RuntimeHealthStatsSchema",
    "StatsSnapshotResponseSchema",
    "StatsResetResponseSchema",
]
