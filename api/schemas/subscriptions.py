# -*- coding: utf-8 -*-
"""
Pydantic v2 HTTP and WebSocket schemas for telemetry subscriptions.
"""

from typing import Any
from pydantic import BaseModel, Field, model_validator

from api.schemas.operations import ExecutionRequestSchema


class SubscriptionCreateRequestSchema(ExecutionRequestSchema):
    """Request payload for starting a telemetry subscription."""

    queue_size: int = Field(default=1000, ge=1, le=10000)

    @model_validator(mode="after")
    def validate_contains_subscribe_step(self) -> "SubscriptionCreateRequestSchema":
        has_subscribe = any(
            step.op_type.strip().lower().replace("-", "_") == "subscribe"
            for step in self.operations
        )
        if not has_subscribe:
            raise ValueError(
                "Subscription request must contain at least one operation with op_type='subscribe'."
            )
        return self


class SubscriptionCreateResponseSchema(BaseModel):
    """Response returned upon creating a telemetry subscription."""

    subscription_id: str
    target: str
    protocol: str
    subscription_name: str
    state: str
    owner_id: str | None = None


class SubscriptionInfoResponseSchema(BaseModel):
    """Detailed status metadata for an active telemetry subscription."""

    subscription_id: str
    session_id: str | None = None
    target: str
    protocol: str
    subscription_name: str
    owner_id: str | None = None
    created_at: float
    stopped_at: float | None = None
    is_alive: bool
    state: str
    error: str | None = None
    is_poll_mode: bool = False
    dropped_count: int = 0


class SubscriptionCommandResponseSchema(BaseModel):
    """Response returned when triggering a subscription control command (e.g. poll)."""

    subscription_id: str
    command: str
    status: str


class StreamEventSchema(BaseModel):
    """Telemetry update payload emitted over WebSocket connections."""

    session_id: str
    target: str
    protocol: str
    subscription_name: str
    data: Any
    timestamp: float
    is_sync_marker: bool = False
    error: str | None = None
    sequence: int | None = None
