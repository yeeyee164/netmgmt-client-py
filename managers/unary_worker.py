import time
import hashlib
import logging
import dataclasses
from typing import Optional, Any

from util.utils import str_to_bytes
from managers.factory import ClientFactory, ValidatorFactory
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

class BaseUnaryWorker:
    """
    Base class for single request-response workers
    """
    def __init__(self, target_ip=None, target_port=None, username="",
                 password="", protocol="",
                 security=None, config=None, **kwargs):
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
        """Asks the factory for a client based on the requested protocol"""
        client_kwargs = dict(self.kwargs) if hasattr(self, 'kwargs') and self.kwargs else {}
        for k in ['target', 'security', 'username', 'password', 'protocol']:
            client_kwargs.pop(k, None)
        if hasattr(self, 'insecure'):
            client_kwargs.setdefault('insecure', self.insecure)
        if hasattr(self, 'schema') and self.schema:
            client_kwargs.setdefault('schema_config', self.schema)
        return ClientFactory.get_client(
            protocol=self.protocol, target=self.target, 
            username=self.username, password=self.password,
            security=self.security, **client_kwargs
        )

class CapabilityWorker(BaseUnaryWorker):
    def start(self):
        logger.debug(f"[Worker(Capability) {self.target_ip}] Requesting Capability...")

        try:
            with self._get_client() as client:
                if isinstance(self.operation, CapabilitiesOperation):
                    result = client.execute_capabilities(self.operation)
                elif hasattr(client, 'execute_capabilities') and not self.kwargs:
                    result = client.execute_capabilities(CapabilitiesOperation())
                else:
                    result = client.capability(**self.kwargs)
                return self._format_result("capability", result)
                
        except Exception as e:
            logger.error(f'[Worker(Capability) {self.target_ip}] Error: {e}')
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

        try:
            with self._get_client() as client:
                if isinstance(self.operation, GetOperation):
                    result = client.execute_get(self.operation)
                elif isinstance(self.operation, GetSchemaOperation):
                    result = client.execute_schema(self.operation)
                else:
                    result = client.get(**self.kwargs)
                return self._format_result("get", result)
                
        except Exception as e:
            logger.error(f'[Worker(Get) {self.target_ip}] Error: {e}')
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
        try:
            if self.validator is not None:
                self._validate_paths()

            with self._get_client() as client:
                if isinstance(self.operation, SetOperation):
                    result = client.execute_set(self.operation)
                return self._format_result("set", result)
        except Exception as e:
            logger.error(f"[Worker(Set) {self.target_ip}] Error: {e}")
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
        try:
            with self._get_client() as client:
                if isinstance(self.operation, TransactionOperation):
                    result = client.execute_transaction(self.operation)
                elif hasattr(client, 'execute_transaction'):
                    result = client.execute_transaction(self.operation)
                else:
                    raise UnsupportedOperationError(f"Client does not support transaction operations: {client}")
                return self._format_result("transaction", result)
        except Exception as e:
            logger.error(f"[Worker(Transaction) {self.target_ip}] Error: {e}")
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
                for op in self.operations:
                    rpc_name = self._get_rpc_name(op)
                    op_id = getattr(op, 'op_id', None)
                    log_pfx = f"[{op_id}] " if op_id else ""
                    logger.debug(f"[Worker(Sequential) {self.target_ip}] {log_pfx}Executing operation '{rpc_name}'...")
                    is_err = False
                    try:
                        res = client.execute(op)
                        if isinstance(res, Exception):
                            is_err = True
                    except Exception as e:
                        logger.error(f"[Worker(Sequential) {self.target_ip}] {log_pfx}Error in operation '{rpc_name}': {e}")
                        res = e
                        is_err = True
                    results.append(self._format_single_result(rpc_name, res, op=op))
                    if is_err and not continue_on_error:
                        logger.warning(
                            f"[Worker(Sequential) {self.target_ip}] {log_pfx}Aborting subsequent operations after error in '{rpc_name}'."
                        )
                        break
        except Exception as e:
            logger.error(f"[Worker(Sequential) {self.target_ip}] Session connection error: {e}")
            results.append(self._format_single_result("error", e))

        if len(results) == 1:
            return results[0]
        return results

    def __str__(self):
        return f"Sequential({len(self.operations)} ops)"

# Alias for spec and backward-compatibility
UnaryWorker = SequentialWorker