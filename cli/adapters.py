# -*- coding: utf-8 -*-
"""
Adapters bridging domain SessionConfig models and Application Service Layer DTOs.
"""
from typing import Any, Dict, List, Optional

from application.dto import (
    ExecutionRequestDTO,
    AdhocEndpointDTO,
    OperationStepDTO,
    MutationChangeDTO,
    TelemetryPolicyDTO,
    DeliveryModeDTO,
)
from config.model import SessionConfig
from config.operations import (
    CapabilitiesOperation,
    GetOperation,
    GetConfigOperation,
    GetSchemaOperation,
    SetOperation,
    EditConfigOperation,
    TransactionOperation,
    SubscribeOperation,
)
from config.selectors import FilterSelector


def session_to_execution_request(session: SessionConfig, device_id: Optional[str] = None) -> ExecutionRequestDTO:
    """
    Converts an internal SessionConfig to an external ExecutionRequestDTO.
    """
    conn = session.connection
    sec = conn.security
    proto_str = session.protocol.value if hasattr(session.protocol, "value") else str(session.protocol).lower()
    adhoc = AdhocEndpointDTO(
        target=conn.target,
        protocol=proto_str,
        username=conn.username,
        password=conn.password,
        tls_ca=sec.tls_ca if (sec and sec.tls_ca) else None,
        tls_cert=sec.tls_cert if (sec and sec.tls_cert) else None,
        tls_key=sec.tls_key if (sec and sec.tls_key) else None,
        skip_verify=bool(sec.skip_verify) if sec else bool(conn.insecure),
        insecure=bool(conn.insecure),
        tls_server_name=sec.tls_server_name if (sec and getattr(sec, "tls_server_name", None)) else None,
        tls_version=sec.tls_version if (sec and getattr(sec, "tls_version", None)) else None,
        ssh_key=sec.ssh_key if (sec and getattr(sec, "ssh_key", None)) else None,
    )

    step_dtos: List[OperationStepDTO] = []
    for op in session.operations:
        op_id = getattr(op, "op_id", None)
        if isinstance(op, CapabilitiesOperation):
            step_dtos.append(OperationStepDTO(op_type="capabilities", op_id=op_id))
        elif isinstance(op, GetSchemaOperation):
            step_dtos.append(OperationStepDTO(
                op_type="get-schema",
                op_id=op_id,
                paths=[op.identifier] if op.identifier else [],
                options={"version": op.version, "format": op.format},
            ))
        elif isinstance(op, GetOperation):
            is_filter = isinstance(op.selector, FilterSelector)
            po = getattr(op, "protocol_options", None)
            opts: Dict[str, Any] = {"read_scope": getattr(op, "read_scope", "all")}
            if po is not None and hasattr(po, "encoding"):
                opts["encoding"] = po.encoding
            ds = "all"
            if po is not None and hasattr(po, "source_datastore") and po.source_datastore:
                ds = po.source_datastore
            elif po is not None and hasattr(po, "source") and po.source:
                ds = po.source
            step_dtos.append(OperationStepDTO(
                op_type="get_config" if isinstance(op, GetConfigOperation) or (adhoc.protocol == "netconf" and getattr(op, "read_scope", "") == "config") else "get",
                op_id=op_id,
                paths=list(getattr(op.selector, "paths", ())) if not is_filter else [],
                prefix=getattr(op.selector, "prefix", "") or None,
                filter_expression=op.selector.expression if is_filter else None,
                filter_type=op.selector.filter_type if is_filter else None,
                datastore=ds,
                options=opts,
            ))
        elif isinstance(op, SetOperation):
            changes = [
                MutationChangeDTO(
                    path=c.path,
                    value=c.value,
                    operation=c.operation.value if hasattr(c.operation, "value") else str(c.operation).lower(),
                    value_type=getattr(c, "type", None),
                )
                for c in op.changes
            ]
            opts = {}
            xml_payload = None
            ds = "all"
            if hasattr(op, "protocol_options") and op.protocol_options:
                po = op.protocol_options
                if hasattr(po, "config") and po.config is not None:
                    xml_payload = po.config
                if hasattr(po, "target_datastore") and po.target_datastore:
                    ds = po.target_datastore
                for k in [
                    "encoding",
                    "default_operation",
                    "test_option",
                    "error_option",
                    "lock_target",
                    "validate_candidate",
                    "commit",
                    "confirmed",
                    "confirm_timeout",
                    "persist",
                    "persist_id",
                ]:
                    if hasattr(po, k):
                        opts[k] = getattr(po, k)
            step_dtos.append(OperationStepDTO(
                op_type="set",
                op_id=op_id,
                datastore=ds,
                changes=changes,
                xml_payload=xml_payload,
                prefix=op.prefix,
                options=opts,
            ))
        elif isinstance(op, TransactionOperation):
            tx_name = op.operation.value if hasattr(op.operation, "value") else str(op.operation).lower()
            step_dtos.append(OperationStepDTO(
                op_type=tx_name,
                op_id=op_id,
                datastore=getattr(op, "target_datastore", "candidate"),
                options={
                    "source_datastore": getattr(op, "source_datastore", "candidate"),
                    "confirmed": getattr(op, "confirmed", False),
                    "confirm_timeout": getattr(op, "confirm_timeout", None),
                    "persist": getattr(op, "persist", None),
                    "persist_id": getattr(op, "persist_id", None),
                },
            ))
        elif isinstance(op, SubscribeOperation):
            is_filter = isinstance(op.selector, FilterSelector)
            tel_policy = None
            if hasattr(op, "delivery") and op.delivery:
                del_mode_str = op.delivery.mode.value if hasattr(op.delivery.mode, "value") else str(op.delivery.mode).lower()
                tel_policy = TelemetryPolicyDTO(
                    mode=DeliveryModeDTO.from_string(del_mode_str),
                    sample_interval_sec=getattr(op.delivery, "interval", 10.0),
                    heartbeat_interval_sec=getattr(op.delivery, "heartbeat", None),
                    suppress_redundant=getattr(op.delivery, "suppress_redundant", False),
                )
            sub_opts: Dict[str, Any] = {"subscription_name": getattr(op, "subscription_name", "sub")}
            po = getattr(op, "protocol_options", None)
            if po is not None:
                for k in ["encoding", "updates_only", "stream_name", "start_time", "stop_time", "poll_timeout"]:
                    if hasattr(po, k):
                        sub_opts[k] = getattr(po, k)
            step_dtos.append(OperationStepDTO(
                op_type="subscribe",
                op_id=op_id,
                paths=list(getattr(op.selector, "paths", ())) if not is_filter else [],
                prefix=getattr(op.selector, "prefix", "") or None,
                filter_expression=op.selector.expression if is_filter else None,
                filter_type=op.selector.filter_type if is_filter else None,
                telemetry=tel_policy,
                options=sub_opts,
            ))
        else:
            raise ValueError(f"Unsupported operation in SessionConfig: {op}")

    schema_profile_id = None
    schema_dirs = []
    schema_cache_dir = None
    schema_validate = False
    schema_auto_discovery = True
    schema_auto_fetch = True
    if hasattr(session, "schema") and session.schema:
        schema_profile_id = getattr(session.schema, "profile", None)
        schema_dirs = [str(p) for p in getattr(session.schema, "yang_dirs", ())]
        if getattr(session.schema, "cache_dir", None):
            schema_cache_dir = str(session.schema.cache_dir)
        schema_validate = bool(getattr(session.schema, "validate", False))
        schema_auto_discovery = bool(getattr(session.schema, "auto_discovery", True))
        schema_auto_fetch = bool(getattr(session.schema, "auto_fetch", True))

    return ExecutionRequestDTO(
        device_id=device_id,
        adhoc_endpoint=adhoc,
        protocol=adhoc.protocol,
        operations=step_dtos,
        schema_profile_id=schema_profile_id,
        schema_dirs=schema_dirs,
        schema_cache_dir=schema_cache_dir,
        schema_validate=schema_validate,
        schema_auto_discovery=schema_auto_discovery,
        schema_auto_fetch=schema_auto_fetch,
        timeout_seconds=session.execution.timeout if hasattr(session, "execution") and session.execution else 30,
        times=session.execution.times if hasattr(session, "execution") and session.execution else 1,
        continue_on_error=session.execution.continue_on_error if hasattr(session, "execution") and session.execution else False,
    )


def execution_request_to_session(dto: Any, registry: Optional[Any] = None) -> SessionConfig:
    """
    Converts an external ExecutionRequestDTO to an internal SessionConfig.
    Delegates to DeviceOperationService.
    """
    from application.operations import DeviceOperationService
    op_svc = DeviceOperationService(device_registry=registry)
    return op_svc.build_session_config(dto)

