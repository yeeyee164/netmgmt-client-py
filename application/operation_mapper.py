# -*- coding: utf-8 -*-
"""
Operation Request & Result Mappers.
Translates Application Service Layer DTOs into internal `SessionConfig`/`OperationConfig`
domain models (`OperationRequestMapper`) and normalizes worker outputs into structured
`ExecutionResultDTO` instances (`ExecutionResultMapper`).
"""

from pathlib import Path
import time
from typing import Any, List

from application.dto import (
    AdhocEndpointDTO,
    ExecutionRequestDTO,
    ExecutionResultDTO,
    OperationStepDTO,
    StepResultDTO,
    VALID_GNMI_ENCODINGS,
    validate_protocol_operation,
)
from application.exceptions import InvalidRequestError
from application.sessions import build_security_profile

from config.delivery import DeliveryMode, DeliveryPolicy
from config.model import ConnectionConfig, ExecutionConfig, Protocol, SessionConfig
from config.operations import (
    CapabilitiesOperation,
    Change,
    ChangeType,
    EditConfigOperation,
    GetConfigOperation,
    GetOperation,
    GetSchemaOperation,
    NetconfTransactionOperation,
    OperationConfig,
    SetOperation,
    SubscribeOperation,
    TransactionType,
)
from config.protocol_options import GnmiOptions, NetconfOptions
from config.schema import SchemaConfig
from config.selectors import FilterSelector, PathSelector


class OperationRequestMapper:
    """Translates `ExecutionRequestDTO` and `OperationStepDTO` into domain configs."""

    @classmethod
    def build_selector(cls, step: OperationStepDTO) -> Any:
        """
        Constructs a `PathSelector` or `FilterSelector` from DTO fields.

        Args:
            step: `OperationStepDTO` containing paths or filter_expression.

        Returns:
            `FilterSelector` if filter_expression is present; `PathSelector` otherwise.
        """
        if step.filter_expression:
            return FilterSelector(
                expression=step.filter_expression,
                filter_type=step.filter_type or "xpath",
            )
        return PathSelector(paths=tuple(step.paths), prefix=step.prefix or "")

    @classmethod
    def map_operation_step(cls, step: OperationStepDTO, proto: Protocol) -> OperationConfig:
        """
        Converts an `OperationStepDTO` to an internal `OperationConfig` subclass.

        Performs protocol-matrix validation and enforces protocol-specific constraints
        (e.g. gNMI does not support NETCONF candidate datastore locks/commits or XML subtree filters).

        Args:
            step: `OperationStepDTO` describing the operation step.
            proto: Protocol enum (`Protocol.GNMI` or `Protocol.NETCONF`).

        Returns:
            `OperationConfig` subclass instance matching the step and protocol.

        Raises:
            InvalidRequestError: If the step is invalid, unsupported for the protocol,
                or missing mandatory parameters.
        """
        op_type = validate_protocol_operation(proto.value, step.op_type)

        if proto == Protocol.GNMI and step.filter_expression and step.filter_type == "subtree":
            raise InvalidRequestError(
                "gNMI does not support XML subtree filter expressions.",
                error_code="UNSUPPORTED_PROTOCOL_FILTER",
            )

        # 1. Capabilities
        if op_type in ("capabilities", "capability"):
            return CapabilitiesOperation(op_id=step.op_id)

        # 2. Get / Get-Config
        elif op_type in ("get", "get_config"):
            selector = cls.build_selector(step)
            if proto == Protocol.GNMI:
                encoding = str(step.options.get("encoding", "json_ietf")).strip().lower()
                if encoding not in VALID_GNMI_ENCODINGS:
                    raise InvalidRequestError(
                        f"Unsupported gNMI encoding '{encoding}'. Valid encodings: {sorted(VALID_GNMI_ENCODINGS)}",
                        error_code="INVALID_OPTIONS",
                    )
                proto_opts = GnmiOptions(
                    encoding=encoding,
                )
            else:
                proto_opts = NetconfOptions(
                    source_datastore=step.datastore if step.datastore != "all" else "running",
                )
            read_scope = step.options.get(
                "read_scope",
                "config" if op_type == "get_config" or step.datastore != "all" else "all",
            )
            if op_type == "get_config" and proto == Protocol.NETCONF:
                return GetConfigOperation(
                    selector=selector,
                    read_scope=read_scope,
                    protocol_options=proto_opts,
                    op_id=step.op_id,
                )
            return GetOperation(
                selector=selector,
                read_scope=read_scope,
                protocol_options=proto_opts,
                op_id=step.op_id,
            )

        # 3. Get-Schema (NETCONF only, enforced by PROTOCOL_OPERATION_MATRIX)
        elif op_type in ("get_schema", "schema"):
            identifier = step.paths[0] if step.paths else step.options.get("identifier", "")
            if not identifier or not str(identifier).strip():
                raise InvalidRequestError(
                    "get-schema operation requires a non-empty 'identifier'.",
                    error_code="INVALID_SELECTOR",
                )
            return GetSchemaOperation(
                identifier=str(identifier).strip(),
                version=step.options.get("version"),
                format=step.options.get("format", "yang"),
                op_id=step.op_id,
            )

        # 4. Set / Edit-Config
        elif op_type in ("set", "edit_config", "edit"):
            has_xml = step.xml_payload is not None or step.options.get("config") is not None
            if not step.changes and not has_xml:
                raise InvalidRequestError(
                    "Set / edit-config operation requires at least one mutation change or an XML config payload.",
                    error_code="INVALID_MUTATION",
                )
            if proto == Protocol.GNMI and not step.changes and has_xml:
                raise InvalidRequestError(
                    "gNMI 'set' does not support raw NETCONF XML payloads; provide structured 'changes'.",
                    error_code="UNSUPPORTED_PROTOCOL_PAYLOAD",
                )

            changes: List[Change] = []
            for ch in step.changes:
                ch_op = ChangeType(ch.operation.lower())
                changes.append(Change(path=ch.path, value=ch.value, operation=ch_op, type=ch.value_type))

            if proto == Protocol.GNMI:
                encoding = str(step.options.get("encoding", "json_ietf")).strip().lower()
                if encoding not in VALID_GNMI_ENCODINGS:
                    raise InvalidRequestError(
                        f"Unsupported gNMI encoding '{encoding}'. Valid encodings: {sorted(VALID_GNMI_ENCODINGS)}",
                        error_code="INVALID_OPTIONS",
                    )
                proto_opts = GnmiOptions(
                    encoding=encoding,
                )
            else:
                lock_val = step.options.get("lock_target", step.options.get("lock", False))
                val_cand = step.options.get("validate_candidate", step.options.get("validate", False))
                default_commit = True if step.xml_payload is not None else False
                conf_timeout = step.options.get("confirm_timeout")
                if conf_timeout is not None:
                    if not isinstance(conf_timeout, int) or isinstance(conf_timeout, bool) or conf_timeout <= 0:
                        raise InvalidRequestError(
                            "Netconf 'confirm_timeout' must be a positive integer.",
                            error_code="INVALID_OPTIONS",
                        )
                proto_opts = NetconfOptions(
                    target_datastore=step.datastore if step.datastore != "all" else step.options.get("target_datastore", "candidate"),
                    config=step.xml_payload if step.xml_payload is not None else step.options.get("config"),
                    default_operation=step.options.get("default_operation", "merge"),
                    test_option=step.options.get("test_option"),
                    error_option=step.options.get("error_option", "stop-on-error"),
                    lock_target=bool(lock_val),
                    validate_candidate=bool(val_cand),
                    commit=bool(step.options.get("commit", default_commit)),
                    confirmed=bool(step.options.get("confirmed", False)),
                    confirm_timeout=conf_timeout,
                    persist=str(step.options.get("persist", "") or ""),
                    persist_id=str(step.options.get("persist_id", "") or ""),
                )
            if op_type in ("edit_config", "edit") and proto == Protocol.NETCONF:
                return EditConfigOperation(
                    changes=tuple(changes),
                    prefix=step.prefix or "",
                    protocol_options=proto_opts,
                    op_id=step.op_id,
                )
            return SetOperation(
                changes=tuple(changes),
                prefix=step.prefix or "",
                protocol_options=proto_opts,
                op_id=step.op_id,
            )

        # 5. Candidate Transactions (NETCONF only, enforced by PROTOCOL_OPERATION_MATRIX)
        elif op_type in ("lock", "unlock", "commit", "discard_changes", "cancel_commit", "validate"):
            tx_map = {
                "lock": TransactionType.LOCK,
                "unlock": TransactionType.UNLOCK,
                "commit": TransactionType.COMMIT,
                "cancel_commit": TransactionType.CANCEL_COMMIT,
                "discard_changes": TransactionType.DISCARD_CHANGES,
                "validate": TransactionType.VALIDATE,
            }
            tx_type = tx_map.get(op_type, TransactionType.COMMIT)
            conf_timeout = step.options.get("confirm_timeout")
            if conf_timeout is not None:
                if (not isinstance(conf_timeout, int) and not (isinstance(conf_timeout, str) and conf_timeout.isdigit())) or isinstance(conf_timeout, bool):
                    raise InvalidRequestError(
                        "Transaction 'confirm_timeout' must be a positive integer.",
                        error_code="INVALID_OPTIONS",
                    )
                conf_timeout = int(conf_timeout)
                if conf_timeout <= 0:
                    raise InvalidRequestError(
                        "Transaction 'confirm_timeout' must be a positive integer.",
                        error_code="INVALID_OPTIONS",
                    )
            return NetconfTransactionOperation(
                operation=tx_type,
                target_datastore=step.datastore if step.datastore != "all" else "candidate",
                source_datastore=step.options.get("source_datastore", "candidate"),
                confirmed=bool(step.options.get("confirmed", False)),
                confirm_timeout=conf_timeout,
                persist=str(step.options.get("persist") or ""),
                persist_id=str(step.options.get("persist_id") or ""),
                op_id=step.op_id,
            )

        # 6. Subscribe
        elif op_type == "subscribe":
            selector = cls.build_selector(step)
            if step.telemetry:
                d_mode = DeliveryMode(step.telemetry.mode.value)
                hb = int(step.telemetry.heartbeat_interval_sec) if step.telemetry.heartbeat_interval_sec is not None else 0
                delivery = DeliveryPolicy(
                    mode=d_mode,
                    interval=int(round(step.telemetry.sample_interval_sec)),
                    heartbeat=hb,
                    suppress_redundant=step.telemetry.suppress_redundant,
                )
            else:
                delivery = DeliveryPolicy(mode=DeliveryMode.PERIODIC, interval=10)

            sub_name = step.options.get("subscription_name", "sub")
            if proto == Protocol.GNMI:
                encoding = str(step.options.get("encoding", "json_ietf")).strip().lower()
                if encoding not in VALID_GNMI_ENCODINGS:
                    raise InvalidRequestError(
                        f"Unsupported gNMI encoding '{encoding}'. Valid encodings: {sorted(VALID_GNMI_ENCODINGS)}",
                        error_code="INVALID_OPTIONS",
                    )
                proto_opts = GnmiOptions(
                    encoding=encoding,
                    updates_only=bool(step.options.get("updates_only", False)),
                )
            else:
                proto_opts = NetconfOptions(
                    stream_name=step.options.get("stream_name", "NETCONF"),
                    start_time=step.options.get("start_time"),
                    stop_time=step.options.get("stop_time"),
                    poll_timeout=step.options.get("poll_timeout", 1.0),
                )
            return SubscribeOperation(
                selector=selector,
                delivery=delivery,
                subscription_name=sub_name,
                protocol_options=proto_opts,
                op_id=step.op_id,
            )

        else:
            raise InvalidRequestError(
                f"Unsupported operation type: '{step.op_type}'",
                error_code="INVALID_OPERATION",
            )

    @classmethod
    def build_session_config(
        cls,
        request: ExecutionRequestDTO,
        endpoint: AdhocEndpointDTO,
    ) -> SessionConfig:
        """
        Translates an external `ExecutionRequestDTO` and resolved `AdhocEndpointDTO`
        into an internal engine `SessionConfig`.
        """
        proto_enum = Protocol.GNMI if endpoint.protocol == "gnmi" else Protocol.NETCONF

        # Security Profile
        security = build_security_profile(endpoint)

        connection = ConnectionConfig(
            target=endpoint.target,
            username=endpoint.username or "",
            password=endpoint.password or "",
            security=security,
            insecure=bool(endpoint.insecure),
        )

        execution = ExecutionConfig(
            timeout=request.timeout_seconds,
            retry_count=1,
            times=1,  # Repetition multiplier is handled authoritatively by Application Service Layer
            continue_on_error=getattr(request, "continue_on_error", False),
        )

        # Schema Config
        schema = SchemaConfig(
            yang_dirs=tuple(Path(p) for p in request.schema_dirs),
            cache_dir=Path(request.schema_cache_dir) if request.schema_cache_dir else None,
            profile=request.schema_profile_id,
            validate=bool(request.schema_validate or request.schema_profile_id or request.schema_dirs),
            auto_discovery=request.schema_auto_discovery,
            auto_fetch=request.schema_auto_fetch,
        )

        # Map operations
        operations: List[OperationConfig] = [
            cls.map_operation_step(step, proto_enum)
            for step in request.operations
        ]

        if not operations:
            raise InvalidRequestError("ExecutionRequestDTO must contain at least one operation in 'operations'.")

        return SessionConfig(
            connection=connection,
            protocol=proto_enum,
            operations=tuple(operations),
            execution=execution,
            schema=schema,
        )


class ExecutionResultMapper:
    """Converts worker execution outputs and service errors into structured `ExecutionResultDTO`s."""

    @staticmethod
    def build_error_result(
        target: str,
        protocol: str,
        message: str,
        error_code: str,
        start_time: float,
    ) -> ExecutionResultDTO:
        """Builds a structured failed `ExecutionResultDTO` for top-level service errors."""
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        return ExecutionResultDTO(
            target=target,
            protocol=protocol,
            success=False,
            results=[
                StepResultDTO(
                    op_type="error",
                    success=False,
                    error=message,
                    error_code=error_code,
                )
            ],
            error_summary=message,
            error_code=error_code,
            execution_time_ms=elapsed_ms,
        )

    @staticmethod
    def format_execution_result(
        raw_result: Any,
        target: str,
        protocol: str,
        elapsed_ms: float,
    ) -> ExecutionResultDTO:
        """
        Standardizes raw worker dictionaries or lists into a structured `ExecutionResultDTO`.
        """
        items = raw_result if isinstance(raw_result, list) else [raw_result]
        step_results: List[StepResultDTO] = []
        errors: List[str] = []

        for item in items:
            if not isinstance(item, dict):
                continue
            rpc_name = item.get("rpc", "unknown")
            payload = item.get("data")
            is_err = isinstance(payload, Exception) or item.get("error") is not None
            err_msg = str(item.get("error") or payload) if is_err else None
            data = None if is_err else payload

            if is_err and err_msg:
                errors.append(err_msg)

            op_id = item.get("op_id")
            step_results.append(
                StepResultDTO(
                    op_type=rpc_name,
                    op_id=op_id,
                    success=not is_err,
                    data=data,
                    error=err_msg,
                    error_code="RPC_EXECUTION_ERROR" if is_err else None,
                )
            )

        all_success = (len(errors) == 0) and len(step_results) > 0
        error_summary = "; ".join(errors) if errors else None

        return ExecutionResultDTO(
            target=target,
            protocol=protocol,
            success=all_success,
            results=step_results,
            error_summary=error_summary,
            error_code="RPC_EXECUTION_ERROR" if errors else None,
            execution_time_ms=elapsed_ms,
        )
