# -*- coding: utf-8 -*-
"""
Pydantic v2 HTTP schemas for YANG schema catalog and pre-flight validation endpoints.
"""

from typing import Any
from pydantic import BaseModel, Field

from api.schemas.operations import ExecutionRequestSchema


class SchemaModuleSchema(BaseModel):
    """Public YANG module metadata (host filesystem paths omitted)."""

    module_id: int | None = None
    name: str
    revision: str | None = None
    namespace: str | None = None
    kind: str | None = None
    sha256: str | None = None
    source: str | None = None
    source_uri: str | None = None
    created_at: str | None = None


class SchemaStatisticsResponseSchema(BaseModel):
    """Summary metrics from the schema database cache."""

    statistics: dict[str, Any]


class SchemaValidateRequestSchema(ExecutionRequestSchema):
    """Request payload for offline pre-flight YANG schema validation."""


class SchemaValidateResponseSchema(BaseModel):
    """Pre-flight YANG schema validation outcome."""

    valid: bool
    errors: list[str] = Field(default_factory=list)
