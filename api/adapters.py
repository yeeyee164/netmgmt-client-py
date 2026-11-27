# -*- coding: utf-8 -*-
"""
Explicit adapters converting between FastAPI Pydantic v2 schemas and
framework-independent Application Service Layer (ASL) dataclasses.
"""

from pathlib import Path
from typing import Any, Optional

from application.dto import (
    AdhocEndpointDTO,
    DeliveryModeDTO,
    ExecutionRequestDTO,
    ExecutionResultDTO,
    ExecutionStatsDTO,
    LatencySummaryDTO,
    MutationChangeDTO,
    OperationStepDTO,
    SessionInfoDTO,
    StatsSnapshotDTO,
    StreamEventDTO,
    SubscriptionStatsDTO,
    TelemetryPolicyDTO,
)
from application.exceptions import InvalidRequestError, PermissionDeniedError
from application.operations import normalize_protocol_payload
from api.deps import APISettings
from api.exceptions import redact_sensitive_text
from api.schemas.devices import AdhocEndpointSchema, DeviceProfileResponseSchema
from api.schemas.operations import (
    ExecutionRequestSchema,
    ExecutionResultSchema,
    MutationChangeSchema,
    OperationStepSchema,
    StepResultSchema,
    TelemetryPolicySchema,
)
from api.schemas.schemas import SchemaModuleSchema
from api.schemas.sessions import (
    SessionExecutionRequestSchema,
    SessionInfoResponseSchema,
)
from api.schemas.stats import (
    ExecutionStatsSchema,
    ExecutionStepStatsSchema,
    LatencySummarySchema,
    OperationStatsSummarySchema,
    RuntimeHealthStatsSchema,
    SchemaStatsSummarySchema,
    SessionStatsSummarySchema,
    StatsSnapshotResponseSchema,
    SubscriptionStatsResponseSchema,
)
from api.schemas.subscriptions import (
    StreamEventSchema,
    SubscriptionInfoResponseSchema,
)


def _is_path_allowed(candidate: Path, allowed_roots: tuple[str, ...]) -> bool:
    if not allowed_roots:
        return False
    resolved_candidate = candidate.expanduser().resolve()
    for root in allowed_roots:
        resolved_root = Path(root).expanduser().resolve()
        if resolved_candidate == resolved_root or resolved_candidate.is_relative_to(resolved_root):
            return True
    return False


def enforce_endpoint_security_policy(
    ep: AdhocEndpointSchema,
    settings: APISettings,
    *,
    allow_adhoc_check: bool = True,
) -> None:
    """Enforces target allow-lists, ad-hoc toggles, and filesystem path restrictions."""
    if allow_adhoc_check and not settings.allow_adhoc_endpoints:
        raise PermissionDeniedError(
            "Ad-hoc device endpoints are disabled by server security policy."
        )

    if settings.allowed_targets:
        host = ep.target.split(":", 1)[0]
        if ep.target not in settings.allowed_targets and host not in settings.allowed_targets:
            raise PermissionDeniedError(
                f"Target '{ep.target}' is not permitted by the configured target allow-list."
            )

    if not settings.allow_client_filesystem_paths:
        if any([ep.tls_ca, ep.tls_cert, ep.tls_key, ep.ssh_key]):
            raise InvalidRequestError(
                "Client-supplied certificate or SSH key filesystem paths are not permitted by server security policy.",
                error_code="FORBIDDEN_FILESYSTEM_PATH",
            )


def enforce_schema_dirs_policy(
    schema_dirs: list[str],
    schema_cache_dir: Optional[str],
    settings: APISettings,
) -> None:
    """Rejects untrusted client-supplied YANG directory or cache filesystem paths."""
    if settings.allow_client_filesystem_paths:
        return

    if schema_cache_dir:
        raise InvalidRequestError(
            "Client-supplied 'schema_cache_dir' is not permitted by server security policy.",
            error_code="FORBIDDEN_FILESYSTEM_PATH",
        )

    for raw_dir in schema_dirs:
        if not _is_path_allowed(Path(raw_dir), settings.allowed_schema_dirs):
            raise InvalidRequestError(
                "Client-supplied 'schema_dirs' path is outside the server allow-list.",
                error_code="FORBIDDEN_FILESYSTEM_PATH",
            )


def to_adhoc_endpoint_dto(ep: AdhocEndpointSchema) -> AdhocEndpointDTO:
    """Converts `AdhocEndpointSchema` to `AdhocEndpointDTO`."""
    return AdhocEndpointDTO(
        target=ep.target,
        protocol=ep.protocol,
        username=ep.username,
        password=ep.password,
        tls_ca=ep.tls_ca,
        tls_cert=ep.tls_cert,
        tls_key=ep.tls_key,
        skip_verify=ep.skip_verify,
        insecure=ep.insecure,
        tls_server_name=ep.tls_server_name,
        tls_version=ep.tls_version,
        ssh_key=ep.ssh_key,
        gnmi_use_tunnel=ep.gnmi_use_tunnel,
        gnmi_target_name=ep.gnmi_target_name,
    )


def to_telemetry_policy_dto(
    tel: Optional[TelemetryPolicySchema],
) -> Optional[TelemetryPolicyDTO]:
    """Converts optional `TelemetryPolicySchema` to `TelemetryPolicyDTO`."""
    if tel is None:
        return None
    return TelemetryPolicyDTO(
        mode=DeliveryModeDTO.from_string(tel.mode),
        sample_interval_sec=tel.sample_interval_sec,
        heartbeat_interval_sec=tel.heartbeat_interval_sec,
        suppress_redundant=tel.suppress_redundant,
    )


def to_mutation_change_dto(change: MutationChangeSchema) -> MutationChangeDTO:
    """Converts `MutationChangeSchema` to `MutationChangeDTO`."""
    return MutationChangeDTO(
        path=change.path,
        value=change.value,
        operation=change.operation,
        value_type=change.value_type,
    )


def to_operation_step_dto(step: OperationStepSchema) -> OperationStepDTO:
    """Converts `OperationStepSchema` to `OperationStepDTO`."""
    return OperationStepDTO(
        op_type=step.op_type,
        op_id=step.op_id,
        paths=list(step.paths),
        filter_expression=step.filter_expression,
        filter_type=step.filter_type,
        prefix=step.prefix,
        datastore=step.datastore,
        changes=[to_mutation_change_dto(ch) for ch in step.changes],
        xml_payload=step.xml_payload,
        telemetry=to_telemetry_policy_dto(step.telemetry),
        options=dict(step.options),
    )


def to_execution_request_dto(
    req: ExecutionRequestSchema,
    settings: Optional[APISettings] = None,
) -> ExecutionRequestDTO:
    """
    Validates security policy constraints (when `settings` is provided) and converts
    an `ExecutionRequestSchema` into an ASL `ExecutionRequestDTO`.
    """
    if settings is not None:
        if req.adhoc_endpoint is not None:
            enforce_endpoint_security_policy(
                req.adhoc_endpoint, settings, allow_adhoc_check=True
            )
        enforce_schema_dirs_policy(req.schema_dirs, req.schema_cache_dir, settings)

    endpoint_dto = (
        to_adhoc_endpoint_dto(req.adhoc_endpoint)
        if req.adhoc_endpoint is not None
        else None
    )
    steps = [to_operation_step_dto(step) for step in req.operations]

    return ExecutionRequestDTO(
        device_id=req.device_id,
        adhoc_endpoint=endpoint_dto,
        protocol=req.protocol,
        operations=steps,
        schema_profile_id=req.schema_profile_id,
        schema_dirs=list(req.schema_dirs),
        schema_cache_dir=req.schema_cache_dir,
        schema_validate=req.schema_validate,
        schema_auto_discovery=req.schema_auto_discovery,
        schema_auto_fetch=req.schema_auto_fetch,
        validate_only=req.validate_only,
        timeout_seconds=req.timeout_seconds,
        times=req.times,
        continue_on_error=req.continue_on_error,
    )


def to_session_execution_request_dto(
    req: SessionExecutionRequestSchema,
    session_info: SessionInfoDTO,
    settings: Optional[APISettings] = None,
) -> ExecutionRequestDTO:
    """
    Converts a `SessionExecutionRequestSchema` into an `ExecutionRequestDTO` bound
    to an existing logical session's target and protocol.
    """
    if settings is not None:
        enforce_schema_dirs_policy(req.schema_dirs, req.schema_cache_dir, settings)

    if req.protocol is not None and req.protocol != session_info.protocol:
        raise InvalidRequestError(
            f"Protocol conflict: request specifies '{req.protocol}' "
            f"but session '{session_info.session_id}' uses '{session_info.protocol}'.",
            error_code="PROTOCOL_CONFLICT",
        )

    steps = [to_operation_step_dto(step) for step in req.operations]
    placeholder_ep = (
        AdhocEndpointDTO(target=session_info.target, protocol=session_info.protocol)
        if session_info.device_id is None
        else None
    )
    return ExecutionRequestDTO(
        device_id=session_info.device_id,
        adhoc_endpoint=placeholder_ep,
        protocol=session_info.protocol,
        operations=steps,
        schema_profile_id=req.schema_profile_id,
        schema_dirs=list(req.schema_dirs),
        schema_cache_dir=req.schema_cache_dir,
        schema_validate=req.schema_validate,
        schema_auto_discovery=req.schema_auto_discovery,
        schema_auto_fetch=req.schema_auto_fetch,
        validate_only=req.validate_only,
        timeout_seconds=req.timeout_seconds,
        times=req.times,
        continue_on_error=req.continue_on_error,
    )


def to_execution_stats_schema(
    dto: Optional[ExecutionStatsDTO],
) -> Optional[ExecutionStatsSchema]:
    """Converts `ExecutionStatsDTO` into `ExecutionStatsSchema`."""
    if dto is None:
        return None
    return ExecutionStatsSchema(
        protocol=dto.protocol,
        target=dto.target,
        request_id=dto.request_id,
        total_duration_ms=dto.total_duration_ms,
        connect_duration_ms=dto.connect_duration_ms,
        connection_reused=dto.connection_reused,
        validation_duration_ms=dto.validation_duration_ms,
        validation_passed=dto.validation_passed,
        operations_total=dto.operations_total,
        operations_succeeded=dto.operations_succeeded,
        operations_failed=dto.operations_failed,
        operations_aborted=dto.operations_aborted,
        request_payload_bytes=dto.request_payload_bytes,
        response_payload_bytes=dto.response_payload_bytes,
        rollbacks=dict(dto.rollbacks),
        steps=[
            ExecutionStepStatsSchema(
                step_index=st.step_index,
                op_type=st.op_type,
                success=st.success,
                aborted=st.aborted,
                duration_ms=st.duration_ms,
                wire_rpc_ms=st.wire_rpc_ms,
                request_bytes=st.request_bytes,
                response_bytes=st.response_bytes,
                path_count=st.path_count,
                error_category=st.error_category,
                error_code=st.error_code,
            )
            for st in dto.steps
        ],
    )


def to_latency_summary_schema(dto: LatencySummaryDTO) -> LatencySummarySchema:
    return LatencySummarySchema(
        count=dto.count,
        min_ms=dto.min_ms,
        avg_ms=dto.avg_ms,
        p50_ms=dto.p50_ms,
        p95_ms=dto.p95_ms,
        p99_ms=dto.p99_ms,
        max_ms=dto.max_ms,
    )


def to_subscription_stats_schema(
    dto: SubscriptionStatsDTO,
) -> SubscriptionStatsResponseSchema:
    return SubscriptionStatsResponseSchema(
        subscription_id=dto.subscription_id,
        protocol=dto.protocol,
        delivery_mode=dto.delivery_mode,
        target=dto.target,
        owner_id=dto.owner_id,
        active=dto.active,
        uptime_seconds=dto.uptime_seconds,
        events_by_kind=dict(dto.events_by_kind),
        events_total=dto.events_total,
        event_rate_eps=dto.event_rate_eps,
        time_to_first_event_ms=dto.time_to_first_event_ms,
        time_to_sync_ms=dto.time_to_sync_ms,
        queue_depth_current=dto.queue_depth_current,
        queue_depth_peak=dto.queue_depth_peak,
        events_dropped=dict(dto.events_dropped),
        poll_triggers_total=dto.poll_triggers_total,
        disconnect_reason=dto.disconnect_reason,
    )


def to_stats_snapshot_response(dto: StatsSnapshotDTO) -> StatsSnapshotResponseSchema:
    return StatsSnapshotResponseSchema(
        generated_at_epoch=dto.generated_at_epoch,
        operations=OperationStatsSummarySchema(
            total=dto.operations.total,
            succeeded=dto.operations.succeeded,
            failed=dto.operations.failed,
            aborted=dto.operations.aborted,
            by_protocol=dict(dto.operations.by_protocol),
            by_target=dict(dto.operations.by_target),
            by_op_type=dict(dto.operations.by_op_type),
            errors_by_category=dict(dto.operations.errors_by_category),
            netconf_rollbacks=dict(dto.operations.netconf_rollbacks),
            request_bytes_total=dto.operations.request_bytes_total,
            response_bytes_total=dto.operations.response_bytes_total,
            execution_latency=to_latency_summary_schema(dto.operations.execution_latency),
            connect_latency=to_latency_summary_schema(dto.operations.connect_latency),
            wire_rpc_latency=to_latency_summary_schema(dto.operations.wire_rpc_latency),
        ),
        subscriptions_active=dto.subscriptions_active,
        subscriptions=[to_subscription_stats_schema(s) for s in dto.subscriptions],
        sessions=SessionStatsSummarySchema(
            active=dto.sessions.active,
            active_by_protocol=dict(dto.sessions.active_by_protocol),
            created_total=dto.sessions.created_total,
            closed_by_reason=dict(dto.sessions.closed_by_reason),
            operations_per_session_avg=dto.sessions.operations_per_session_avg,
            datastore_locks_active=dto.sessions.datastore_locks_active,
            datastore_lock_hold_ms=to_latency_summary_schema(
                dto.sessions.datastore_lock_hold_ms
            ),
            idle_evictions_total=dto.sessions.idle_evictions_total,
            orphaned_lock_reclaims_total=dto.sessions.orphaned_lock_reclaims_total,
            reconnects_total=dto.sessions.reconnects_total,
        ),
        schemas=SchemaStatsSummarySchema(
            validations_passed=dto.schemas.validations_passed,
            validations_failed=dto.schemas.validations_failed,
            validation_latency=to_latency_summary_schema(dto.schemas.validation_latency),
            cache_hits=dict(dto.schemas.cache_hits),
            cache_misses=dict(dto.schemas.cache_misses),
            cache_hit_ratio=dto.schemas.cache_hit_ratio,
            profiles_loaded=dto.schemas.profiles_loaded,
            compile_latency=to_latency_summary_schema(dto.schemas.compile_latency),
        ),
        runtime=RuntimeHealthStatsSchema(
            process_uptime_seconds=dto.runtime.process_uptime_seconds,
            worker_threads_active=dict(dto.runtime.worker_threads_active),
            worker_semaphore_wait=to_latency_summary_schema(
                dto.runtime.worker_semaphore_wait
            ),
            websocket_clients_active=dict(dto.runtime.websocket_clients_active),
        ),
    )


def to_execution_result_schema(dto: ExecutionResultDTO) -> ExecutionResultSchema:
    """Converts `ExecutionResultDTO` into `ExecutionResultSchema`, preserving all steps and redacting errors."""
    steps = [
        StepResultSchema(
            op_type=step.op_type,
            op_id=step.op_id,
            success=step.success,
            data=normalize_protocol_payload(dto.protocol, step.op_type, step.data),
            error=redact_sensitive_text(step.error),
            error_code=step.error_code,
        )
        for step in dto.results
    ]
    return ExecutionResultSchema(
        target=dto.target,
        protocol=dto.protocol,
        success=dto.success,
        results=steps,
        error_summary=redact_sensitive_text(dto.error_summary),
        error_code=dto.error_code,
        execution_time_ms=dto.execution_time_ms,
        stats=to_execution_stats_schema(dto.stats),
    )


def to_device_profile_response(
    device_id: str, dto: AdhocEndpointDTO
) -> DeviceProfileResponseSchema:
    """Converts `AdhocEndpointDTO` to a redacted `DeviceProfileResponseSchema`."""
    return DeviceProfileResponseSchema(
        device_id=device_id,
        target=dto.target,
        protocol=dto.protocol,
        username=dto.username,
        has_password=bool(dto.password),
        has_tls_ca=bool(dto.tls_ca),
        has_tls_cert=bool(dto.tls_cert),
        has_tls_key=bool(dto.tls_key),
        has_ssh_key=bool(dto.ssh_key),
        skip_verify=dto.skip_verify,
        insecure=dto.insecure,
        tls_server_name=dto.tls_server_name,
        tls_version=dto.tls_version,
        gnmi_use_tunnel=dto.gnmi_use_tunnel,
        gnmi_target_name=dto.gnmi_target_name,
    )


def to_session_info_response(dto: SessionInfoDTO) -> SessionInfoResponseSchema:
    """Converts `SessionInfoDTO` into `SessionInfoResponseSchema`."""
    return SessionInfoResponseSchema(
        session_id=dto.session_id,
        device_id=dto.device_id,
        target=dto.target,
        protocol=dto.protocol,
        owner_id=dto.owner_id,
        state=dto.state,
        created_at=dto.created_at,
        last_activity=dto.last_activity,
        expires_at=dto.expires_at,
        in_use=dto.in_use,
        locked_datastores=list(dto.locked_datastores),
        has_uncommitted_changes=dto.has_uncommitted_changes,
    )


def to_subscription_info_response(
    info: dict[str, Any],
) -> SubscriptionInfoResponseSchema:
    """Converts `SubscriptionService.get_subscription_info` metadata dict into `SubscriptionInfoResponseSchema`."""
    return SubscriptionInfoResponseSchema(
        subscription_id=info["subscription_id"],
        session_id=info.get("session_id"),
        target=info["target"],
        protocol=info["protocol"],
        subscription_name=info["subscription_name"],
        owner_id=info.get("owner_id"),
        created_at=info["created_at"],
        stopped_at=info.get("stopped_at"),
        is_alive=bool(info["is_alive"]),
        state=info["state"],
        error=redact_sensitive_text(info.get("error")),
        is_poll_mode=bool(info.get("is_poll_mode", False)),
        dropped_count=int(info.get("dropped_count", 0)),
    )


def to_stream_event_schema(dto: StreamEventDTO) -> StreamEventSchema:
    """Converts `StreamEventDTO` into `StreamEventSchema`."""
    return StreamEventSchema(
        session_id=dto.session_id,
        target=dto.target,
        protocol=dto.protocol,
        subscription_name=dto.subscription_name,
        data=normalize_protocol_payload(dto.protocol, "subscribe", dto.data),
        timestamp=dto.timestamp,
        is_sync_marker=dto.is_sync_marker,
        error=redact_sensitive_text(dto.error),
        sequence=dto.sequence,
    )


def to_schema_module_schema(mod: dict[str, Any]) -> SchemaModuleSchema:
    """Converts a cached module metadata dictionary into `SchemaModuleSchema` (omitting `file_path`)."""
    created_raw = mod.get("created_at")
    return SchemaModuleSchema(
        module_id=mod.get("module_id"),
        name=str(mod.get("name", "")),
        revision=mod.get("revision"),
        namespace=mod.get("namespace"),
        kind=mod.get("kind"),
        sha256=mod.get("sha256"),
        source=mod.get("source"),
        source_uri=mod.get("source_uri"),
        created_at=str(created_raw) if created_raw is not None else None,
    )
