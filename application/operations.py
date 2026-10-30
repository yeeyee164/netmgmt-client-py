# -*- coding: utf-8 -*-
"""
Device Operation Service.
Coordinates unary operations, builds internal SessionConfig domain models,
and executes requests returning structured ExecutionResultDTO instances.
"""

import concurrent.futures
import logging
from pathlib import Path
import time
from typing import Any, List, Optional, Sequence

from application.dto import (
    ExecutionRequestDTO,
    ExecutionResultDTO,
    OperationStepDTO,
    StepResultDTO,
    SupportedProtocol,
    VALID_GNMI_ENCODINGS,
    expand_request_repetitions,
    validate_protocol_operation,
)
from application.devices import DeviceRegistryService
from application.schemas import SchemaCatalogService
from application.exceptions import (
    ApplicationError,
    InvalidRequestError,
    UnsupportedProtocolError,
    DeviceNotFoundError,
)

from config.model import SessionConfig, ConnectionConfig, ExecutionConfig, Protocol
from config.schema import SchemaConfig
from config.operations import (
    OperationConfig,
    CapabilitiesOperation,
    GetOperation,
    GetConfigOperation,
    GetSchemaOperation,
    SetOperation,
    EditConfigOperation,
    TransactionOperation,
    TransactionType,
    SubscribeOperation,
    Change,
    ChangeType,
)
from config.selectors import PathSelector, FilterSelector
from config.protocol_options import GnmiOptions, NetconfOptions
from config.delivery import DeliveryMode, DeliveryPolicy
from modules.security import SecurityProfile
from managers.unary_worker import (
    BaseUnaryWorker,
    SequentialWorker,
    GetWorker,
    SetWorker,
    CapabilityWorker,
    TransactionWorker,
)

logger = logging.getLogger(__name__)


class DeviceOperationService:
    """
    Application service executing unary requests against network devices.
    Decoupled from CLI argument parsing and terminal presentation sinks.
    """

    def __init__(
        self,
        device_registry: Optional[DeviceRegistryService] = None,
        schema_service: Optional[SchemaCatalogService] = None,
    ):
        self._registry = device_registry or DeviceRegistryService()
        self._schema_service = schema_service or SchemaCatalogService()

    def build_session_config(self, request: ExecutionRequestDTO) -> SessionConfig:
        """
        Translates an external ExecutionRequestDTO into an internal SessionConfig.
        """
        endpoint = self._registry.resolve_endpoint(request)
        proto_enum = Protocol.GNMI if endpoint.protocol == "gnmi" else Protocol.NETCONF

        # Security Profile
        security = None
        if any([
            endpoint.tls_ca,
            endpoint.tls_cert,
            endpoint.tls_key,
            endpoint.skip_verify,
            endpoint.tls_server_name,
            endpoint.tls_version,
            endpoint.ssh_key,
        ]):
            security = SecurityProfile(
                tls_ca=endpoint.tls_ca or "",
                tls_cert=endpoint.tls_cert or "",
                tls_key=endpoint.tls_key or "",
                skip_verify=endpoint.skip_verify,
                tls_server_name=endpoint.tls_server_name or "",
                tls_version=endpoint.tls_version or "1.3",
                ssh_key=endpoint.ssh_key,
            )

        connection = ConnectionConfig(
            target=endpoint.target,
            username=endpoint.username or "",
            password=endpoint.password or "",
            security=security,
            insecure=bool(endpoint.insecure or endpoint.skip_verify),
        )

        execution = ExecutionConfig(
            timeout=request.timeout_seconds,
            retry_count=1,
            times=1,  # Repetition multiplier is handled authoritatively by Application Service Layer
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
        operations: List[OperationConfig] = []
        for step in request.operations:
            op = self._map_operation_step(step, proto_enum)
            operations.append(op)

        if not operations:
            raise InvalidRequestError("ExecutionRequestDTO must contain at least one operation in 'operations'.")

        session_cfg = SessionConfig(
            connection=connection,
            protocol=proto_enum,
            operations=tuple(operations),
            execution=execution,
            schema=schema,
        )
        return session_cfg

    def _map_operation_step(self, step: OperationStepDTO, proto: Protocol) -> OperationConfig:
        """Converts an OperationStepDTO to an internal OperationConfig subclass."""
        op_type = validate_protocol_operation(proto.value, step.op_type)

        if proto == Protocol.GNMI and step.filter_expression and step.filter_type == "subtree":
            raise InvalidRequestError(
                "gNMI does not support XML subtree filter expressions.",
                error_code="UNSUPPORTED_PROTOCOL_FILTER",
            )

        # 1. Capabilities
        if op_type in ("capabilities", "capability"):
            return CapabilitiesOperation()

        # 2. Get / Get-Config
        elif op_type in ("get", "get_config"):
            selector = self._build_selector(step)
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
                )
            return GetOperation(
                selector=selector,
                read_scope=read_scope,
                protocol_options=proto_opts,
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
                )
            return SetOperation(
                changes=tuple(changes),
                prefix=step.prefix or "",
                protocol_options=proto_opts,
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
            from config.operations import NetconfTransactionOperation
            return NetconfTransactionOperation(
                operation=tx_type,
                target_datastore=step.datastore if step.datastore != "all" else "candidate",
                source_datastore=step.options.get("source_datastore", "candidate"),
                confirmed=bool(step.options.get("confirmed", False)),
                confirm_timeout=conf_timeout,
                persist=str(step.options.get("persist") or ""),
                persist_id=str(step.options.get("persist_id") or ""),
            )

        # 6. Subscribe
        elif op_type == "subscribe":
            selector = self._build_selector(step)
            delivery = None
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
            )

        else:
            raise InvalidRequestError(
                f"Unsupported operation type: '{step.op_type}'",
                error_code="INVALID_OPERATION",
            )

    def _build_selector(self, step: OperationStepDTO) -> Any:
        """Constructs PathSelector or FilterSelector from DTO fields."""
        if step.filter_expression:
            return FilterSelector(
                expression=step.filter_expression,
                filter_type=step.filter_type or "xpath",
            )
        return PathSelector(paths=tuple(step.paths), prefix=step.prefix or "")

    @classmethod
    def resolve_worker(cls, session_cfg: Any) -> BaseUnaryWorker:
        """Instantiates the matching worker instance for the session."""
        operations = getattr(session_cfg, "operations", ())
        if len(operations) > 1:
            return SequentialWorker(config=session_cfg)

        op = operations[0] if len(operations) == 1 else getattr(session_cfg, "operation", None)
        if isinstance(op, (GetOperation, GetSchemaOperation)):
            return GetWorker(config=session_cfg)
        elif isinstance(op, SetOperation):
            return SetWorker(config=session_cfg)
        elif isinstance(op, CapabilitiesOperation):
            return CapabilityWorker(config=session_cfg)
        elif isinstance(op, TransactionOperation):
            return TransactionWorker(config=session_cfg)
        elif len(operations) == 0 and op is None:
            return SequentialWorker(config=session_cfg)
        raise ValueError(f"Unsupported unary operation: {op}")

    def _resolve_worker(self, session_cfg: Any) -> BaseUnaryWorker:
        """Instance delegation for resolve_worker (maintains compatibility with mocks)."""
        return self.resolve_worker(session_cfg)

    def execute_unary(
        self,
        request: ExecutionRequestDTO,
    ) -> ExecutionResultDTO:
        """
        Executes a unary request pipeline.
        Returns a structured ExecutionResultDTO without terminal/file presentation side effects.
        """
        # If request requested repetition, delegate to execute_batch and aggregate results
        if getattr(request, "times", 1) > 1:
            batch_results = self.execute_batch([request])
            if batch_results:
                combined_steps: List[StepResultDTO] = []
                all_success = True
                errors: List[str] = []
                first_error_code: Optional[str] = None
                total_ms = 0.0
                for r in batch_results:
                    combined_steps.extend(r.results)
                    if not r.success:
                        all_success = False
                        if first_error_code is None and r.error_code:
                            first_error_code = r.error_code
                    if r.error_summary:
                        errors.append(r.error_summary)
                    total_ms += r.execution_time_ms
                return ExecutionResultDTO(
                    target=batch_results[0].target,
                    protocol=batch_results[0].protocol,
                    success=all_success,
                    results=combined_steps,
                    error_summary="; ".join(errors) if errors else None,
                    error_code=first_error_code,
                    execution_time_ms=total_ms,
                )

        start_time = time.perf_counter()
        target_str = request.adhoc_endpoint.target if request.adhoc_endpoint else (request.device_id or "unknown")
        proto_str = request.protocol or (request.adhoc_endpoint.protocol if request.adhoc_endpoint else "unknown")

        try:
            endpoint = self._registry.resolve_endpoint(request)
            target_str = endpoint.target
            proto_str = endpoint.protocol

            # Pre-flight schema validation only mode
            if request.validate_only:
                val_errors = self._schema_service.validate_request_schema(request)
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                if val_errors:
                    return ExecutionResultDTO(
                        target=target_str,
                        protocol=proto_str,
                        success=False,
                        results=[
                            StepResultDTO(
                                op_type="validate",
                                success=False,
                                error=e,
                                error_code="SCHEMA_VALIDATION_ERROR",
                            )
                            for e in val_errors
                        ],
                        error_summary="; ".join(val_errors),
                        error_code="SCHEMA_VALIDATION_ERROR",
                        execution_time_ms=elapsed_ms,
                    )
                return ExecutionResultDTO(
                    target=target_str,
                    protocol=proto_str,
                    success=True,
                    results=[StepResultDTO(op_type="validate", success=True, data="Schema validation succeeded")],
                    execution_time_ms=elapsed_ms,
                )

            # Build domain SessionConfig
            session_cfg = self.build_session_config(request)
            worker = self._resolve_worker(session_cfg)

            raw_result = worker.start()

            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return self._format_execution_result(
                raw_result=raw_result,
                target=target_str,
                protocol=proto_str,
                elapsed_ms=elapsed_ms,
            )
        except ApplicationError as app_err:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            err_code = getattr(app_err, "error_code", "APPLICATION_ERROR")
            logger.error(f"[DeviceOperationService {target_str}] Application error ({err_code}): {app_err}")
            return ExecutionResultDTO(
                target=target_str,
                protocol=proto_str,
                success=False,
                results=[
                    StepResultDTO(
                        op_type="error",
                        success=False,
                        error=str(app_err),
                        error_code=err_code,
                    )
                ],
                error_summary=str(app_err),
                error_code=err_code,
                execution_time_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error(f"[DeviceOperationService {target_str}] Unexpected error: {exc}", exc_info=True)
            return ExecutionResultDTO(
                target=target_str,
                protocol=proto_str,
                success=False,
                results=[
                    StepResultDTO(
                        op_type="error",
                        success=False,
                        error=str(exc),
                        error_code="EXECUTION_ERROR",
                    )
                ],
                error_summary=str(exc),
                error_code="EXECUTION_ERROR",
                execution_time_ms=elapsed_ms,
            )

    def execute_batch(
        self,
        requests: Sequence[ExecutionRequestDTO],
        max_workers: Optional[int] = None,
    ) -> List[ExecutionResultDTO]:
        """
        Executes multiple ExecutionRequestDTOs concurrently using a ThreadPoolExecutor.
        Respects `request.times` repetition multipliers and returns structured results.
        """
        expanded_requests = expand_request_repetitions(requests)

        if not expanded_requests:
            logger.warning("[DeviceOperationService] No unary requests to execute.")
            return []

        workers_count = max_workers if (max_workers is not None and max_workers > 0) else max(len(expanded_requests), 1)
        logger.info(f"[DeviceOperationService] Starting {len(expanded_requests)} concurrent unary tasks...")

        results: List[Optional[ExecutionResultDTO]] = [None] * len(expanded_requests)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers_count) as executor:
            future_to_idx = {
                executor.submit(self.execute_unary, req): idx
                for idx, req in enumerate(expanded_requests)
            }
            for future in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[future]
                req = expanded_requests[idx]
                try:
                    results[idx] = future.result()
                except Exception as exc:
                    target_str = req.adhoc_endpoint.target if req.adhoc_endpoint else (req.device_id or "unknown")
                    proto_str = req.protocol or (req.adhoc_endpoint.protocol if req.adhoc_endpoint else "unknown")
                    logger.error(f"[DeviceOperationService {target_str}] generated an exception: {exc}")
                    results[idx] = ExecutionResultDTO(
                        target=target_str,
                        protocol=proto_str,
                        success=False,
                        results=[
                            StepResultDTO(
                                op_type="error",
                                success=False,
                                error=str(exc),
                                error_code="EXECUTION_ERROR",
                            )
                        ],
                        error_summary=str(exc),
                        error_code="EXECUTION_ERROR",
                    )

        return [r for r in results if r is not None]

    def _format_execution_result(
        self,
        raw_result: Any,
        target: str,
        protocol: str,
        elapsed_ms: float,
    ) -> ExecutionResultDTO:
        """Standardizes raw worker dictionaries into an ExecutionResultDTO."""
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

            step_results.append(
                StepResultDTO(
                    op_type=rpc_name,
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
