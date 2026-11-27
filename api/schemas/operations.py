# -*- coding: utf-8 -*-
"""
Pydantic v2 HTTP schemas for unary device operations and pipelines.
"""

from typing import Any
from pydantic import BaseModel, Field, field_validator, model_validator

from application.dto import PROTOCOL_OPERATION_MATRIX, VALID_OP_TYPES
from api.schemas.common import (
    DatastoreLiteral,
    DeliveryModeLiteral,
    FilterTypeLiteral,
    MutationOperationLiteral,
    ProtocolLiteral,
    StrictRequestModel,
)
from api.schemas.devices import AdhocEndpointSchema
from api.schemas.stats import ExecutionStatsSchema


class MutationChangeSchema(StrictRequestModel):
    """Individual configuration mutation item for Set / Edit-Config operations."""

    path: str = Field(min_length=1, max_length=2048)
    value: Any | None = None
    operation: MutationOperationLiteral = "merge"
    value_type: str | None = Field(default=None, max_length=64)


class TelemetryPolicySchema(StrictRequestModel):
    """Telemetry stream delivery policy parameters."""

    mode: DeliveryModeLiteral = "periodic"
    sample_interval_sec: float = Field(default=10.0, ge=0.0)
    heartbeat_interval_sec: float | None = Field(default=None, ge=0.0)
    suppress_redundant: bool = False


class OperationStepSchema(StrictRequestModel):
    """Single operation step within a unary or streaming execution request."""

    op_type: str = Field(min_length=1, max_length=64)
    op_id: str | None = Field(default=None, max_length=128)
    paths: list[str] = Field(default_factory=list, max_length=256)
    filter_expression: str | None = Field(default=None, max_length=65536)
    filter_type: FilterTypeLiteral | None = None
    prefix: str | None = Field(default=None, max_length=2048)
    datastore: DatastoreLiteral = "all"
    changes: list[MutationChangeSchema] = Field(default_factory=list, max_length=256)
    xml_payload: str | None = Field(default=None, max_length=262144)
    telemetry: TelemetryPolicySchema | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("op_type")
    @classmethod
    def validate_op_type(cls, value: str) -> str:
        norm = value.strip().lower().replace("-", "_")
        if norm not in VALID_OP_TYPES:
            raise ValueError(
                f"Unsupported operation type '{value}'. Valid operations are: {sorted(VALID_OP_TYPES)}"
            )
        return value.strip()

    @field_validator("paths")
    @classmethod
    def validate_paths_non_empty(cls, paths: list[str]) -> list[str]:
        for p in paths:
            if not isinstance(p, str) or not p.strip():
                raise ValueError("All entries in 'paths' must be non-empty strings.")
        return paths


class ExecutionRequestSchema(StrictRequestModel):
    """Top-level HTTP request to execute unary operations or pipelines."""

    device_id: str | None = Field(default=None, min_length=1, max_length=128)
    adhoc_endpoint: AdhocEndpointSchema | None = None
    protocol: ProtocolLiteral | None = None
    operations: list[OperationStepSchema] = Field(min_length=1, max_length=100)
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

    @model_validator(mode="after")
    def validate_target_and_protocol(self) -> "ExecutionRequestSchema":
        if bool(self.device_id) == bool(self.adhoc_endpoint):
            raise ValueError("Provide exactly one of device_id or adhoc_endpoint.")

        if (
            self.adhoc_endpoint is not None
            and self.protocol is not None
            and self.adhoc_endpoint.protocol != self.protocol
        ):
            raise ValueError(
                f"Protocol conflict between request.protocol ('{self.protocol}') "
                f"and adhoc_endpoint.protocol ('{self.adhoc_endpoint.protocol}')."
            )

        effective_proto = self.protocol or (
            self.adhoc_endpoint.protocol if self.adhoc_endpoint is not None else None
        )
        if effective_proto is not None:
            allowed = PROTOCOL_OPERATION_MATRIX.get(effective_proto, frozenset())
            for step in self.operations:
                norm_op = step.op_type.strip().lower().replace("-", "_")
                if norm_op not in allowed:
                    raise ValueError(
                        f"Operation '{step.op_type}' is not supported by protocol '{effective_proto}'."
                    )
        return self


class StepResultSchema(BaseModel):
    """Per-step outcome returned inside `ExecutionResultSchema`."""

    op_type: str
    op_id: str | None = None
    success: bool
    data: Any | None = None
    error: str | None = None
    error_code: str | None = None


class ExecutionResultSchema(BaseModel):
    """Structured unary/pipeline execution result preserving all step outcomes."""

    target: str
    protocol: str
    success: bool
    results: list[StepResultSchema] = Field(default_factory=list)
    error_summary: str | None = None
    error_code: str | None = None
    execution_time_ms: float = 0.0
    stats: ExecutionStatsSchema | None = None
