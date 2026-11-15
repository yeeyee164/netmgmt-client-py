# -*- coding: utf-8 -*-
"""
Pydantic v2 HTTP schemas for managed logical device sessions.
"""

from pydantic import BaseModel, Field

from api.schemas.common import ProtocolLiteral, StrictRequestModel
from api.schemas.operations import OperationStepSchema


class SessionCreateRequestSchema(StrictRequestModel):
    """Request body for opening a managed logical session against a registered device."""

    protocol: ProtocolLiteral | None = None
    ttl_seconds: float | None = Field(default=None, gt=0.0, le=3600.0)


class SessionExecutionRequestSchema(StrictRequestModel):
    """Request body for executing operations inside an existing managed logical session."""

    operations: list[OperationStepSchema] = Field(min_length=1, max_length=100)
    protocol: ProtocolLiteral | None = None
    schema_profile_id: str | None = Field(default=None, max_length=128)
    schema_dirs: list[str] = Field(default_factory=list, max_length=32)
    schema_cache_dir: str | None = Field(default=None, max_length=1024)
    schema_validate: bool = False
    schema_auto_discovery: bool = True
    schema_auto_fetch: bool = True
    validate_only: bool = False
    timeout_seconds: int = Field(default=30, ge=1, le=3600)
    times: int = Field(default=1, ge=1, le=1000)
    continue_on_error: bool = False


class SessionInfoResponseSchema(BaseModel):
    """Safe logical session metadata returned to API callers."""

    session_id: str
    device_id: str | None = None
    target: str
    protocol: str
    owner_id: str
    state: str
    created_at: float
    last_activity: float
    expires_at: float
    in_use: bool = False
    locked_datastores: list[str] = Field(default_factory=list)
    has_uncommitted_changes: bool = False
