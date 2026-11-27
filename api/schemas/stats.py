# -*- coding: utf-8 -*-
"""
Pydantic v2 HTTP/WebSocket schemas for Statistics & Observability endpoints.
"""

from pydantic import BaseModel, Field


class LatencySummarySchema(BaseModel):
    """Latency distribution summary in milliseconds."""

    count: int = 0
    min_ms: float = 0.0
    avg_ms: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: float = 0.0


class ExecutionStepStatsSchema(BaseModel):
    """Per-step execution timing and payload telemetry."""

    step_index: int
    op_type: str
    success: bool
    aborted: bool = False
    duration_ms: float = 0.0
    wire_rpc_ms: float | None = None
    request_bytes: int = 0
    response_bytes: int = 0
    path_count: int = 0
    error_category: str | None = None
    error_code: str | None = None


class ExecutionStatsSchema(BaseModel):
    """Per-execution summary embedded on operation responses."""

    protocol: str
    target: str
    request_id: str | None = None
    total_duration_ms: float = 0.0
    connect_duration_ms: float = 0.0
    connection_reused: bool = False
    validation_duration_ms: float = 0.0
    validation_passed: bool | None = None
    operations_total: int = 0
    operations_succeeded: int = 0
    operations_failed: int = 0
    operations_aborted: int = 0
    request_payload_bytes: int = 0
    response_payload_bytes: int = 0
    rollbacks: dict[str, int] = Field(default_factory=dict)
    steps: list[ExecutionStepStatsSchema] = Field(default_factory=list)


class SubscriptionStatsResponseSchema(BaseModel):
    """Per-subscription telemetry statistics."""

    subscription_id: str
    protocol: str
    delivery_mode: str
    target: str
    owner_id: str | None = None
    active: bool = True
    uptime_seconds: float = 0.0
    events_by_kind: dict[str, int] = Field(default_factory=dict)
    events_total: int = 0
    event_rate_eps: float = 0.0
    time_to_first_event_ms: float | None = None
    time_to_sync_ms: float | None = None
    queue_depth_current: int = 0
    queue_depth_peak: int = 0
    events_dropped: dict[str, int] = Field(default_factory=dict)
    poll_triggers_total: int = 0
    disconnect_reason: str | None = None


class OperationStatsSummarySchema(BaseModel):
    """Aggregate RPC and pipeline execution statistics."""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    aborted: int = 0
    by_protocol: dict[str, int] = Field(default_factory=dict)
    by_target: dict[str, int] = Field(default_factory=dict)
    by_op_type: dict[str, int] = Field(default_factory=dict)
    errors_by_category: dict[str, int] = Field(default_factory=dict)
    netconf_rollbacks: dict[str, int] = Field(default_factory=dict)
    request_bytes_total: int = 0
    response_bytes_total: int = 0
    execution_latency: LatencySummarySchema = Field(default_factory=LatencySummarySchema)
    connect_latency: LatencySummarySchema = Field(default_factory=LatencySummarySchema)
    wire_rpc_latency: LatencySummarySchema = Field(default_factory=LatencySummarySchema)


class SessionStatsSummarySchema(BaseModel):
    """Aggregate managed session and datastore lock statistics."""

    active: int = 0
    active_by_protocol: dict[str, int] = Field(default_factory=dict)
    created_total: int = 0
    closed_by_reason: dict[str, int] = Field(default_factory=dict)
    operations_per_session_avg: float = 0.0
    datastore_locks_active: int = 0
    datastore_lock_hold_ms: LatencySummarySchema = Field(default_factory=LatencySummarySchema)
    idle_evictions_total: int = 0
    orphaned_lock_reclaims_total: int = 0
    reconnects_total: int = 0


class SchemaStatsSummarySchema(BaseModel):
    """Aggregate YANG schema cache and validation statistics."""

    validations_passed: int = 0
    validations_failed: int = 0
    validation_latency: LatencySummarySchema = Field(default_factory=LatencySummarySchema)
    cache_hits: dict[str, int] = Field(default_factory=dict)
    cache_misses: dict[str, int] = Field(default_factory=dict)
    cache_hit_ratio: float = 0.0
    profiles_loaded: int = 0
    compile_latency: LatencySummarySchema = Field(default_factory=LatencySummarySchema)


class RuntimeHealthStatsSchema(BaseModel):
    """Backend concurrency and WebSocket client health metrics."""

    process_uptime_seconds: float = 0.0
    worker_threads_active: dict[str, int] = Field(default_factory=dict)
    worker_semaphore_wait: LatencySummarySchema = Field(default_factory=LatencySummarySchema)
    websocket_clients_active: dict[str, int] = Field(default_factory=dict)


class StatsSnapshotResponseSchema(BaseModel):
    """Complete runtime statistics snapshot across all five domains."""

    generated_at_epoch: float
    operations: OperationStatsSummarySchema
    subscriptions_active: int
    subscriptions: list[SubscriptionStatsResponseSchema] = Field(default_factory=list)
    sessions: SessionStatsSummarySchema
    schemas: SchemaStatsSummarySchema
    runtime: RuntimeHealthStatsSchema


class StatsResetResponseSchema(BaseModel):
    """Administrative reset confirmation payload."""

    status: str = "reset"
    reset_at_epoch: float
