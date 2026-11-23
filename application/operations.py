# -*- coding: utf-8 -*-
"""
Device Operation Service.
Coordinates unary operations, builds internal SessionConfig domain models via
`OperationRequestMapper`, and executes requests returning structured `ExecutionResultDTO`
instances via `ExecutionResultMapper`.
"""

import concurrent.futures
import dataclasses
import logging
import time
from typing import Any, List, Optional, Sequence

from application.devices import DeviceRegistryService
from application.dto import (
    ExecutionRequestDTO,
    ExecutionResultDTO,
    OperationStepDTO,
    StepResultDTO,
    expand_request_repetitions,
)
from application.exceptions import (
    ApplicationError,
    InvalidRequestError,
    PermissionDeniedError,
    SessionConflictError,
    SessionNotFoundError,
)
from application.operation_mapper import ExecutionResultMapper, OperationRequestMapper
from application.schemas import SchemaCatalogService
from application.sessions import DeviceSessionManager

from config.model import Protocol, SessionConfig
from config.operations import (
    CapabilitiesOperation,
    GetOperation,
    GetSchemaOperation,
    OperationConfig,
    SetOperation,
    TransactionOperation,
)
from managers.unary_worker import (
    BaseUnaryWorker,
    CapabilityWorker,
    GetWorker,
    SequentialWorker,
    SetWorker,
    TransactionWorker,
)
from modules.formatter import normalize_protocol_payload

logger = logging.getLogger(__name__)


def normalize_protocol_payload(protocol: str, op_type: str, raw_data: Any) -> Any:
    """
    Normalizes native protocol payloads (gNMI Protobuf messages, NETCONF XML/RPCReply
    objects, bytes, Exceptions) into JSON-serializable Python primitives for API callers.
    """
    if raw_data is None or isinstance(raw_data, (bool, int, float, str)):
        return raw_data
    if isinstance(raw_data, bytes):
        try:
            return raw_data.decode("utf-8")
        except UnicodeDecodeError:
            return raw_data.hex()
    if isinstance(raw_data, Exception):
        return str(raw_data)
    if isinstance(raw_data, dict):
        return {
            str(k): normalize_protocol_payload(protocol, op_type, v)
            for k, v in raw_data.items()
        }
    if isinstance(raw_data, (list, tuple)):
        return [
            normalize_protocol_payload(protocol, op_type, item)
            for item in raw_data
        ]

    proto_norm = str(protocol or "").strip().lower()
    if proto_norm == "gnmi" or hasattr(raw_data, "DESCRIPTOR"):
        from modules.formatter import GNMIFormatter

        cls_name = type(raw_data).__name__
        op_norm = str(op_type or "").strip().lower().replace("_", "-")
        if cls_name == "CapabilityResponse" or op_norm in ("capability", "capabilities"):
            rpc_key = "capability"
        elif cls_name == "GetResponse" or op_norm in ("get", "get-config"):
            rpc_key = "get"
        elif cls_name == "SetResponse" or op_norm in ("set", "edit-config", "edit"):
            rpc_key = "set"
        elif cls_name == "SubscribeResponse" or op_norm == "subscribe":
            rpc_key = "subscribe"
        else:
            rpc_key = op_norm

        formatted = GNMIFormatter().format_json(raw_data, rpc=rpc_key, meta=None)
        if isinstance(formatted, (dict, list)):
            return normalize_protocol_payload(protocol, op_type, formatted)
        if hasattr(raw_data, "DESCRIPTOR"):
            try:
                from google.protobuf.json_format import MessageToDict

                return MessageToDict(raw_data, preserving_proto_field_name=True)
            except Exception:
                pass
        return str(formatted)

    if proto_norm == "netconf" or hasattr(raw_data, "xml") or hasattr(raw_data, "data_xml"):
        from modules.formatter import NETCONFFormatter

        op_norm = str(op_type or "").strip().lower().replace("_", "-")
        if op_norm == "capabilities":
            op_norm = "capability"
        elif op_norm == "schema":
            op_norm = "get-schema"
        elif op_norm == "edit":
            op_norm = "edit-config"

        formatted = NETCONFFormatter().format_json(raw_data, rpc=op_norm, meta=None)
        if isinstance(formatted, dict):
            inner = formatted.get("data")
            if inner is not None:
                return normalize_protocol_payload(protocol, op_type, inner)
            return normalize_protocol_payload(protocol, op_type, formatted)
        return normalize_protocol_payload(protocol, op_type, formatted)

    return str(raw_data)


class DeviceOperationService:
    """
    Application service executing unary requests against network devices.

    Decoupled from CLI argument parsing and terminal/file presentation sinks.
    Translates high-level `ExecutionRequestDTO` instances into internal `SessionConfig` domain
    models, dispatches them to appropriate unary worker engines (`GetWorker`, `SetWorker`,
    `CapabilityWorker`, `TransactionWorker`, `SequentialWorker`), and returns structured
    `ExecutionResultDTO` instances with stable error codes and per-step diagnostics.
    """

    def __init__(
        self,
        device_registry: Optional[DeviceRegistryService] = None,
        schema_service: Optional[SchemaCatalogService] = None,
        session_manager: Optional[DeviceSessionManager] = None,
    ):
        """
        Initializes the device operation service.

        Args:
            device_registry: Optional `DeviceRegistryService` for resolving device_id endpoints.
            schema_service: Optional `SchemaCatalogService` for pre-flight schema validation.
            session_manager: Optional `DeviceSessionManager` for managed logical-session execution.
        """
        self._registry = device_registry or DeviceRegistryService()
        self._schema_service = schema_service or SchemaCatalogService()
        self._session_manager = session_manager

    def build_session_config(self, request: ExecutionRequestDTO) -> SessionConfig:
        """
        Translates an external `ExecutionRequestDTO` into an internal engine `SessionConfig`.
        """
        endpoint = self._registry.resolve_endpoint(request)
        return OperationRequestMapper.build_session_config(request, endpoint)

    def _map_operation_step(self, step: OperationStepDTO, proto: Protocol) -> OperationConfig:
        """Converts an `OperationStepDTO` to an internal `OperationConfig` subclass."""
        return OperationRequestMapper.map_operation_step(step, proto)

    def _build_selector(self, step: OperationStepDTO) -> Any:
        """Constructs a `PathSelector` or `FilterSelector` from DTO fields."""
        return OperationRequestMapper.build_selector(step)

    @classmethod
    def resolve_worker(cls, session_cfg: Any, client: Optional[Any] = None) -> BaseUnaryWorker:
        """
        Instantiates the matching unary worker instance for the session configuration.

        Selects `SequentialWorker` for multi-operation sessions, or specific workers
        (`GetWorker`, `SetWorker`, `CapabilityWorker`, `TransactionWorker`) for single-operation sessions.
        """
        operations = getattr(session_cfg, "operations", ())
        if len(operations) > 1:
            return SequentialWorker(config=session_cfg, client=client)

        op = operations[0] if len(operations) == 1 else getattr(session_cfg, "operation", None)
        if isinstance(op, (GetOperation, GetSchemaOperation)):
            return GetWorker(config=session_cfg, client=client)
        elif isinstance(op, SetOperation):
            return SetWorker(config=session_cfg, client=client)
        elif isinstance(op, CapabilitiesOperation):
            return CapabilityWorker(config=session_cfg, client=client)
        elif isinstance(op, TransactionOperation):
            return TransactionWorker(config=session_cfg, client=client)
        elif len(operations) == 0 and op is None:
            return SequentialWorker(config=session_cfg, client=client)
        raise ValueError(f"Unsupported unary operation: {op}")

    def _resolve_worker(self, session_cfg: Any, client: Optional[Any] = None) -> BaseUnaryWorker:
        """Instance delegation for resolve_worker."""
        return self.resolve_worker(session_cfg, client=client)

    def execute_unary(
        self,
        request: ExecutionRequestDTO,
    ) -> ExecutionResultDTO:
        """
        Executes a unary request pipeline.

        Builds domain configuration, executes workers synchronously or via thread execution,
        and returns a structured `ExecutionResultDTO` without terminal or file presentation side effects.
        If request.times > 1, delegates to execute_batch and aggregates step results.
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
                return self._execute_validate_only(request, target_str, proto_str, start_time)

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
            err_code = getattr(app_err, "error_code", "APPLICATION_ERROR")
            logger.error(f"[DeviceOperationService {target_str}] Application error ({err_code}): {app_err}")
            return self._build_error_result(target_str, proto_str, str(app_err), err_code, start_time)
        except Exception as exc:
            logger.error(f"[DeviceOperationService {target_str}] Unexpected error: {exc}", exc_info=True)
            return self._build_error_result(target_str, proto_str, str(exc), "EXECUTION_ERROR", start_time)

    @staticmethod
    def _build_error_result(
        target: str,
        protocol: str,
        message: str,
        error_code: str,
        start_time: float,
    ) -> ExecutionResultDTO:
        """Builds a structured failed ExecutionResultDTO for top-level service errors."""
        return ExecutionResultMapper.build_error_result(
            target=target,
            protocol=protocol,
            message=message,
            error_code=error_code,
            start_time=start_time,
        )

    def _execute_validate_only(
        self,
        request: ExecutionRequestDTO,
        target_str: str,
        proto_str: str,
        start_time: float,
    ) -> ExecutionResultDTO:
        """Runs offline pre-flight schema validation and formats an ExecutionResultDTO."""
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

    def execute_in_session(
        self,
        session_id: str,
        request: ExecutionRequestDTO,
        *,
        owner_id: str,
    ) -> ExecutionResultDTO:
        """
        Executes operations within an existing managed logical session.

        Acquires an exclusive operation lease from `DeviceSessionManager`, reuses the
        retained protocol client/session without closing it after completion, updates
        transaction/lock metadata on the lease, and returns a structured `ExecutionResultDTO`.
        """
        if self._session_manager is None:
            raise InvalidRequestError(
                "DeviceSessionManager is not configured on this DeviceOperationService.",
                error_code="SESSION_MANAGER_UNAVAILABLE",
            )
        if not request.operations:
            raise InvalidRequestError(
                "ExecutionRequestDTO must contain at least one operation in 'operations'.",
                error_code="INVALID_REQUEST",
            )

        start_time = time.perf_counter()
        with self._session_manager.acquire_session(session_id, owner_id=owner_id) as lease:
            if request.protocol and request.protocol != lease.protocol:
                raise InvalidRequestError(
                    f"Protocol conflict: request specifies '{request.protocol}' "
                    f"but logical session '{session_id}' uses '{lease.protocol}'.",
                    error_code="PROTOCOL_CONFLICT",
                )
            if request.device_id and lease.device_id is not None and request.device_id != lease.device_id:
                raise InvalidRequestError(
                    f"Target conflict: request targets '{request.device_id}' "
                    f"but logical session '{session_id}' is bound to '{lease.device_id}'.",
                    error_code="TARGET_CONFLICT",
                )

            bound_request = dataclasses.replace(
                request,
                device_id=None,
                adhoc_endpoint=lease.endpoint,
                protocol=lease.protocol,
                times=1,
            )

            if bound_request.validate_only:
                return self._execute_validate_only(
                    bound_request, lease.endpoint.target, lease.protocol, start_time
                )

            repeat = max(getattr(request, "times", 1), 1)
            all_raw_items: List[Any] = []
            try:
                session_cfg = self.build_session_config(bound_request)
                for _ in range(repeat):
                    worker = self._resolve_worker(session_cfg, client=lease.client)
                    raw_result = worker.start()
                    items = raw_result if isinstance(raw_result, list) else [raw_result]
                    all_raw_items.extend(items)
            except (SessionNotFoundError, PermissionDeniedError, SessionConflictError, InvalidRequestError):
                raise
            except ApplicationError as app_err:
                err_code = getattr(app_err, "error_code", "APPLICATION_ERROR")
                return self._build_error_result(
                    lease.endpoint.target, lease.protocol, str(app_err), err_code, start_time
                )
            except Exception as exc:
                logger.error(
                    f"[DeviceOperationService {lease.endpoint.target}] Session execution error: {exc}",
                    exc_info=True,
                )
                return self._build_error_result(
                    lease.endpoint.target, lease.protocol, str(exc), "EXECUTION_ERROR", start_time
                )

            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            result_dto = self._format_execution_result(
                raw_result=all_raw_items,
                target=lease.endpoint.target,
                protocol=lease.protocol,
                elapsed_ms=elapsed_ms,
            )

            num_ops = len(bound_request.operations)
            for idx, step_res in enumerate(result_dto.results):
                step_dto = bound_request.operations[idx % num_ops]
                lease.record_step_outcome(step_dto, step_res.success)

            return result_dto

    def execute_batch(
        self,
        requests: Sequence[ExecutionRequestDTO],
        max_workers: Optional[int] = None,
    ) -> List[ExecutionResultDTO]:
        """
        Executes multiple ExecutionRequestDTOs concurrently using a ThreadPoolExecutor.

        Expands requests according to their .times repetition multipliers into single-run DTOs
        (preventing multiplicative execution, conforming to ADR-003) and isolates individual
        request failures so that one failing target does not abort the entire batch.
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
        """Standardizes raw worker dictionaries or lists into a structured `ExecutionResultDTO`."""
        return ExecutionResultMapper.format_execution_result(
            raw_result=raw_result,
            target=target,
            protocol=protocol,
            elapsed_ms=elapsed_ms,
        )
