# -*- coding: utf-8 -*-
"""
Common Pydantic v2 schema primitives, type literals, and standard API responses.
"""

from typing import Literal
from pydantic import BaseModel, ConfigDict


class StrictRequestModel(BaseModel):
    """Base request model rejecting unrecognized fields (`extra='forbid'`)."""

    model_config = ConfigDict(extra="forbid")


ProtocolLiteral = Literal["gnmi", "netconf"]
DatastoreLiteral = Literal[
    "running", "candidate", "startup", "all", "operational", "intended"
]
FilterTypeLiteral = Literal["xpath", "subtree"]
MutationOperationLiteral = Literal["merge", "replace", "delete", "remove", "create"]
DeliveryModeLiteral = Literal[
    "periodic", "event_driven", "snapshot", "on_demand", "server_determined"
]


class APIErrorResponseSchema(BaseModel):
    """Standardized API error payload."""

    error_code: str
    message: str
    target: str | None = None


class HealthResponseSchema(BaseModel):
    """Liveness probe response."""

    status: str = "ok"


class ReadinessResponseSchema(BaseModel):
    """Readiness probe response indicating whether core services are initialized."""

    status: str
    ready: bool
    services: dict[str, bool]
