import time
import hashlib
import logging
import dataclasses
from typing import Optional, Any

from util.utils import str_to_bytes, format_error_summary
from managers.factory import ClientFactory, ValidatorFactory
from modules.stats_hooks import NOOP_STATS_RECORDER, StatsRecorder, safe_record
from config.model import SessionConfig, Protocol
from config.operations import (
    OperationConfig,
    CapabilitiesOperation,
    GetOperation,
    SetOperation,
    GetSchemaOperation,
    ChangeType,
    TransactionOperation,
)

logger = logging.getLogger(__name__)


class _BorrowedClientContext:
    """
    Context manager wrapper that yields an already-entered BaseClient
    without closing it when the worker exits its `with` block.
    """

    def __init__(self, client: Any):
        self._client = client

    def __enter__(self):
        return self._client

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


class _TimedClientContext:
    """Wraps a client context manager to measure transport connect latency cleanly."""

    def __init__(
        self,
        inner_ctx: Any,
        *,
        stats_recorder: StatsRecorder,
        protocol: str,
        target: str,
        reused: bool,
    ):
        self._inner = inner_ctx
        self._stats_recorder = stats_recorder
        self._protocol = protocol
        self._target = target
        self._reused = reused
        self._borrowed_client: Any = None
        self._prev_client_recorder: Any = None

    def __enter__(self):
        t0 = time.perf_counter()
        client = self._inner.__enter__()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if self._reused and hasattr(client, "stats_recorder"):
            self._borrowed_client = client
            self._prev_client_recorder = getattr(client, "stats_recorder")
            try:
                client.stats_recorder = self._stats_recorder
            except Exception:
                self._borrowed_client = None
        safe_record(
            self._stats_recorder.record_connect,
            self._protocol,
            self._target,
            elapsed_ms,
            reused=self._reused,
        )
        return client

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._borrowed_client is not None:
            try:
                self._borrowed_client.stats_recorder = self._prev_client_recorder
            except Exception:
                pass
            self._borrowed_client = None
        return self._inner.__exit__(exc_type, exc_val, exc_tb)


class BaseUnaryWorker:
    """
    Base class for single request-response workers
    """
    def __init__(self, target_ip=None, target_port=None, username="",
                 password="", protocol="",
                 security=None, config=None, client=None, stats_recorder: Optional[StatsRecorder] = None, **kwargs):
        self._external_client = client
        self.stats_recorder: StatsRecorder = stats_recorder or NOOP_STATS_RECORDER
        self.config = config
        self.session = config
        self.target_ip = target_ip
        self.target_port = target_port
        self.username = username
        self.password = password
        self.protocol = protocol.lower() if protocol else 'gnmi'
        self.security = security
        self.insecure = kwargs.get('insecure', False)
        self.execution = kwargs.get('execution', None)
        self.operation = kwargs.get('operation', '')
        self.operations = tuple(kwargs.get('operations', ()))
        if not self.operations and self.operation:
            self.operations = (self.operation,) if isinstance(self.operation, OperationConfig) else ()
        elif self.operations and not self.operation:
            self.operation = self.operations[0]
        self.kwargs = dict(kwargs)

        if config is not None:
            if isinstance(config, SessionConfig) or hasattr(config, 'connection'):
                self.target_ip = config.target_ip
                self.target_port = config.target_port
                self.username = config.username
                self.password = config.password
                proto_val = config.protocol
                self.protocol = proto_val.value if hasattr(proto_val, 'value') else str(proto_val).lower()
                self.security = config.security
                self.insecure = config.insecure
                self.execution = getattr(config, 'execution', None)
                self.operation = getattr(config, 'operation', None)
                self.operations = tuple(config.operations) if hasattr(config, 'operations') and config.operations else ()
                if not self.operations and self.operation:
                    self.operations = (self.operation,)
                elif self.operations and not self.operation:
                    self.operation = self.operations[0]
                self.schema = getattr(config, 'schema', None)
                self.kwargs = dict(kwargs)


        self.target = f'{self.target_ip}:{self.target_port}'

        raw_id_str = f"{self.target_ip}:{self.target_port}:{time.time()}"
        self.session_id = hashlib.md5(str_to_bytes(raw_id_str)).hexdigest()[:10]

    def _get_rpc_name(self, op: Any) -> str:
        """Determines the standard RPC name for an operation."""
        if isinstance(op, CapabilitiesOperation):
            return "capability"
        elif isinstance(op, GetSchemaOperation):
            return "get-schema"
        elif isinstance(op, GetOperation):
            return "get-config" if getattr(op, 'read_scope', '') == "config" else "get"
        elif isinstance(op, SetOperation):
            return "edit-config" if self.protocol == "netconf" else "set"
        elif isinstance(op, TransactionOperation):
            return op.operation.value if hasattr(op.operation, 'value') else str(op.operation)
        elif isinstance(op, str) and op:
            return op
        return self.kwargs.get('operation', 'operation')

    def _format_single_result(self, rpc_name: str, data: Any, op: Any = None) -> dict:
        """Standardizes a single operation output dictionary for handlers."""
        res = {
            'session_id': self.session_id,
            'target': self.target,
            'rpc': rpc_name,
            'data': data,
            'protocol': self.protocol,
        }
        op_id = getattr(op, 'op_id', None) if op is not None else None
        if op_id:
            res['op_id'] = op_id
        return res

    def _format_result(self, default_rpc_name, data):
        """Standardizes the output dictionary for the handlers"""
        rpc_name = self._get_rpc_name(self.operation) if self.operation else default_rpc_name
        return self._format_single_result(rpc_name, data, op=self.operation)

    def _get_client(self):
        """Asks the factory for a client based on the requested protocol, or yields a borrowed client."""
        if self._external_client is not None:
            return _TimedClientContext(
                _BorrowedClientContext(self._external_client),
                stats_recorder=self.stats_recorder,
                protocol=self.protocol,
                target=self.target,
                reused=True,
            )
        client_kwargs = dict(self.kwargs) if hasattr(self, 'kwargs') and self.kwargs else {}
        for k in ['target', 'security', 'username', 'password', 'protocol']:
            client_kwargs.pop(k, None)
        if hasattr(self, 'insecure'):
            client_kwargs.setdefault('insecure', self.insecure)
        if hasattr(self, 'schema') and self.schema:
            client_kwargs.setdefault('schema_config', self.schema)
        client_kwargs.setdefault('stats_recorder', self.stats_recorder)
        raw_ctx = ClientFactory.get_client(
            protocol=self.protocol, target=self.target, 
            username=self.username, password=self.password,
            security=self.security, **client_kwargs
        )
        return _TimedClientContext(
            raw_ctx,
            stats_recorder=self.stats_recorder,
            protocol=self.protocol,
            target=self.target,
            reused=False,
        )

    @staticmethod
    def _consume_client_metrics(client: Any) -> dict:
        if client is None:
            return {}
        consume_fn = getattr(client, "consume_last_rpc_metrics", None)
        if callable(consume_fn):
            try:
                raw = consume_fn()
                if isinstance(raw, dict):
                    return raw
            except Exception:
                pass
        return {}

    @staticmethod
    def _classify_exception(exc: Exception) -> tuple[str, str]:
        name = type(exc).__name__
        if "Validation" in name or "SchemaPath" in name or "PathValidation" in name:
            return ("validation_error", name)
        if isinstance(exc, TimeoutError) or "Timeout" in name:
            return ("timeout_error", name)
        if "TLS" in name or "Auth" in name or "Permission" in name:
            return ("auth_tls_error", name)
        if "RPCError" in name:
            return ("netconf_rpc_error", str(getattr(exc, "tag", None) or name))
        return ("transport_error", name)

    def _record_step_stats(
        self,
        *,
        op_type: str,
        duration_ms: float,
        result: Any = None,
        client: Any = None,
        exc: Optional[Exception] = None,
    ) -> None:
        is_err = exc is not None or isinstance(result, Exception)
        metrics = self._consume_client_metrics(client)
        err_obj = exc if exc is not None else (result if isinstance(result, Exception) else None)
        err_cat = metrics.get("error_category")
        err_code = metrics.get("error_code")
        if is_err and not err_cat and err_obj is not None:
            err_cat, err_code = self._classify_exception(err_obj)
        safe_record(
            self.stats_recorder.record_operation_step,
            protocol=self.protocol,
            target=self.target,
            op_type=op_type,
            success=not is_err,
            duration_ms=duration_ms,
            wire_rpc_ms=metrics.get("wire_rpc_ms"),
            request_bytes=int(metrics.get("request_bytes", 0)),
            response_bytes=int(metrics.get("response_bytes", 0)),
            path_count=int(metrics.get("path_count", 0)),
            error_category=err_cat if is_err else None,
            error_code=err_code if is_err else None,
        )

class CapabilityWorker(BaseUnaryWorker):
    def start(self):
        logger.debug(f"[Worker(Capability) {self.target_ip}] Requesting Capability...")
        rpc_name = self._get_rpc_name(self.operation) if self.operation else "capability"
        t0 = time.perf_counter()
        active_client = None
        try:
            with self._get_client() as client:
                active_client = client
                t_step = time.perf_counter()
                if isinstance(self.operation, CapabilitiesOperation):
                    result = client.execute_capabilities(self.operation)
                elif hasattr(client, 'execute_capabilities') and not self.kwargs:
                    result = client.execute_capabilities(CapabilitiesOperation())
                else:
                    result = client.capability(**self.kwargs)
                self._record_step_stats(
                    op_type=rpc_name,
                    duration_ms=(time.perf_counter() - t_step) * 1000.0,
                    result=result,
                    client=client,
                )
                return self._format_result("capability", result)
                
        except Exception as e:
            self._record_step_stats(
                op_type=rpc_name,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
                client=active_client,
                exc=e,
            )
            logger.error(f'[Worker(Capability) {self.target_ip}] Error: {format_error_summary(e)}')
            return self._format_result("capability", e)

    def __str__(self):
        return "Capabilities"

class GetWorker(BaseUnaryWorker):
    def __init__(self, target_ip=None, target_port=None, config=None, **kwargs):
        super().__init__(target_ip=target_ip, target_port=target_port, config=config, **kwargs)
        if isinstance(self.operation, GetOperation) and hasattr(self.operation.selector, 'paths'):
            self.paths = list(self.operation.selector.paths)
        else:
            paths = kwargs.get('paths', [])
            self.paths = paths

    def start(self):
        logger.debug(f"[Worker(Get) {self.target_ip}] Requesting Get...")
        rpc_name = self._get_rpc_name(self.operation) if self.operation else "get"
        t0 = time.perf_counter()
        active_client = None
        try:
            with self._get_client() as client:
                active_client = client
                t_step = time.perf_counter()
                if isinstance(self.operation, GetOperation):
                    result = client.execute_get(self.operation)
                elif isinstance(self.operation, GetSchemaOperation):
                    result = client.execute_schema(self.operation)
                else:
                    result = client.get(**self.kwargs)
                self._record_step_stats(
                    op_type=rpc_name,
                    duration_ms=(time.perf_counter() - t_step) * 1000.0,
                    result=result,
                    client=client,
                )
                return self._format_result("get", result)
                
        except Exception as e:
            self._record_step_stats(
                op_type=rpc_name,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
                client=active_client,
                exc=e,
            )
            logger.error(f'[Worker(Get) {self.target_ip}] Error: {format_error_summary(e)}')
            return self._format_result("get", e)

    def __str__(self):
        return "Get"

class SetWorker(BaseUnaryWorker):
    def __init__(self, target_ip=None, target_port=None, updates=None, replaces=None, deletes=None, config=None, **kwargs):
        super().__init__(target_ip=target_ip, target_port=target_port, config=config, **kwargs)
        if isinstance(self.operation, SetOperation):
            self.updates = []
            self.replaces = []
            self.deletes = []
            for change in self.operation.changes:
                if change.operation == ChangeType.MERGE:
                    self.updates.append((change.path, change.value))
                elif change.operation == ChangeType.REPLACE:
                    self.replaces.append((change.path, change.value))
                elif change.operation == ChangeType.DELETE:
                    self.deletes.append(change.path)
            self.prefix = getattr(self.operation.protocol_options, 'prefix', '') if hasattr(self.operation.protocol_options, 'prefix') else ''
        else:
            self.updates = updates or []
            self.replaces = replaces or []
            self.deletes = deletes or []
            self.prefix = kwargs.get('prefix', '')

        try:
            self.validator = ValidatorFactory.get_validator(self.protocol)
        except (NotImplementedError, ValueError):
            self.validator = None

    def _validate_paths(self):
        """Performs offline validation of all paths in SetRequest adhering to gNMI Section 3.4.5 & 2.7."""
        if self.prefix:
            self.validator.validate_path(self.prefix, 'set')

        all_paths = []
        for item in self.updates:
            if isinstance(item, (tuple, list)) and len(item) >= 1:
                all_paths.append(item[0])
            elif isinstance(item, dict):
                all_paths.extend(list(item.keys()))
            elif isinstance(item, str):
                all_paths.append(item)

        for item in self.replaces:
            if isinstance(item, (tuple, list)) and len(item) >= 1:
                all_paths.append(item[0])
            elif isinstance(item, dict):
                all_paths.extend(list(item.keys()))
            elif isinstance(item, str):
                all_paths.append(item)

        for item in self.deletes:
            all_paths.append(item)

        for path in all_paths:
            self.validator.validate_path(path, 'set', prefix=self.prefix)

    def start(self):
        logger.debug(f"[Worker(Set) {self.target_ip}] Requesting Set...")
        rpc_name = self._get_rpc_name(self.operation) if self.operation else "set"
        t0 = time.perf_counter()
        active_client = None
        try:
            if self.validator is not None:
                self._validate_paths()

            with self._get_client() as client:
                active_client = client
                t_step = time.perf_counter()
                if isinstance(self.operation, SetOperation):
                    result = client.execute_set(self.operation)
                self._record_step_stats(
                    op_type=rpc_name,
                    duration_ms=(time.perf_counter() - t_step) * 1000.0,
                    result=result,
                    client=client,
                )
                return self._format_result("set", result)
        except Exception as e:
            self._record_step_stats(
                op_type=rpc_name,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
                client=active_client,
                exc=e,
            )
            logger.error(f"[Worker(Set) {self.target_ip}] Error: {format_error_summary(e)}")
            return self._format_result("set", e)

    def __str__(self):
        return "Set"

class TransactionWorker(BaseUnaryWorker):
    """
    Executes a standalone transaction lifecycle operation (e.g. commit, discard-changes).
    """
    def start(self):
        rpc_name = self._get_rpc_name(self.operation)
        logger.debug(f"[Worker(Transaction) {self.target_ip}] Requesting Transaction ({rpc_name})...")
        t0 = time.perf_counter()
        active_client = None
        try:
            with self._get_client() as client:
                active_client = client
                t_step = time.perf_counter()
                if isinstance(self.operation, TransactionOperation):
                    result = client.execute_transaction(self.operation)
                elif hasattr(client, 'execute_transaction'):
                    result = client.execute_transaction(self.operation)
                else:
                    raise UnsupportedOperationError(f"Client does not support transaction operations: {client}")
                self._record_step_stats(
                    op_type=rpc_name,
                    duration_ms=(time.perf_counter() - t_step) * 1000.0,
                    result=result,
                    client=client,
                )
                return self._format_result("transaction", result)
        except Exception as e:
            self._record_step_stats(
                op_type=rpc_name,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
                client=active_client,
                exc=e,
            )
            logger.error(f"[Worker(Transaction) {self.target_ip}] Error: {format_error_summary(e)}")
            return self._format_result("transaction", e)

    def __str__(self):
        return f"Transaction({self._get_rpc_name(self.operation)})"

class SequentialWorker(BaseUnaryWorker):
    """
    Executes an ordered sequence of operations within a single client connection lifecycle.
    """
    def start(self):
        logger.debug(f"[Worker(Sequential) {self.target_ip}] Executing {len(self.operations)} sequential operations...")
        results = []
        continue_on_error = getattr(self.execution, 'continue_on_error', False) if self.execution else False
        try:
            with self._get_client() as client:
                # operations holds each protocol RPC 'specifically'
                for idx, op in enumerate(self.operations):
                    rpc_name = self._get_rpc_name(op)
                    op_id = getattr(op, 'op_id', None)
                    log_pfx = f"[{op_id}] " if op_id else ""
                    logger.debug(f"[Worker(Sequential) {self.target_ip}] {log_pfx}Executing operation '{rpc_name}'...")
                    is_err = False
                    step_exc: Optional[Exception] = None
                    t_step = time.perf_counter()
                    try:
                        res = client.execute(op)
                        if isinstance(res, Exception):
                            is_err = True
                    except Exception as e:
                        logger.error(f"[Worker(Sequential) {self.target_ip}] {log_pfx}Error in operation '{rpc_name}': {format_error_summary(e)}")
                        res = e
                        step_exc = e
                        is_err = True
                    self._record_step_stats(
                        op_type=rpc_name,
                        duration_ms=(time.perf_counter() - t_step) * 1000.0,
                        result=res,
                        client=client,
                        exc=step_exc,
                    )
                    results.append(self._format_single_result(rpc_name, res, op=op))
                    if is_err and not continue_on_error:
                        logger.warning(
                            f"[Worker(Sequential) {self.target_ip}] {log_pfx}Aborting subsequent operations after error in '{rpc_name}'."
                        )
                        for skipped_op in self.operations[idx + 1:]:
                            safe_record(
                                self.stats_recorder.record_operation_aborted,
                                self.protocol,
                                self.target,
                                self._get_rpc_name(skipped_op),
                            )
                        break
        except Exception as e:
            logger.error(f"[Worker(Sequential) {self.target_ip}] Session connection error: {format_error_summary(e)}")
            for op in self.operations:
                safe_record(
                    self.stats_recorder.record_operation_aborted,
                    self.protocol,
                    self.target,
                    self._get_rpc_name(op),
                )
            results.append(self._format_single_result("error", e))

        if len(results) == 1:
            return results[0]
        return results

    def __str__(self):
        return f"Sequential({len(self.operations)} ops)"

# Alias for spec and backward-compatibility
UnaryWorker = SequentialWorker