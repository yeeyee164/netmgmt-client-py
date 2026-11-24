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
    MutationChangeDTO,
    OperationStepDTO,
    SessionInfoDTO,
    StreamEventDTO,
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
