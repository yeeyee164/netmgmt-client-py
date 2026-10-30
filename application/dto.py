# -*- coding: utf-8 -*-
"""
Pure Data Transfer Objects (DTOs) for the Application Service Layer.
These models isolate external caller intent from internal engine representations.
Zero dependencies on transport libraries (grpc, ncclient) or CLI parsers (argparse).
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Sequence
import time

from application.exceptions import UnsupportedProtocolError, InvalidRequestError


# Configurable Application Boundary Validation Constants
MAX_TIMEOUT_SECONDS: int = 3600
MAX_REPETITIONS: int = 1000

VALID_MUTATION_OPS: frozenset[str] = frozenset({
    "merge", "replace", "delete", "remove", "create"
})
VALID_DATASTORES: frozenset[str] = frozenset({
    "running", "candidate", "startup", "all", "operational", "intended"
})
VALID_FILTER_TYPES: frozenset[str] = frozenset({"xpath", "subtree"})
VALID_GNMI_ENCODINGS: frozenset[str] = frozenset({
    "json", "json_ietf", "bytes", "proto", "ascii"
})

PROTOCOL_OPERATION_MATRIX: dict[str, frozenset[str]] = {
    "gnmi": frozenset({
        "capabilities",
        "capability",
        "get",
        "set",
        "subscribe",
    }),
    "netconf": frozenset({
        "capabilities",
        "capability",
        "get",
        "get_config",
        "get_schema",
        "schema",
        "set",
        "edit_config",
        "edit",
        "subscribe",
        "lock",
        "unlock",
        "commit",
        "cancel_commit",
        "discard_changes",
        "validate",
    }),
}

VALID_OP_TYPES: frozenset[str] = (
    PROTOCOL_OPERATION_MATRIX["gnmi"] | PROTOCOL_OPERATION_MATRIX["netconf"]
)


def validate_protocol_operation(protocol: str, op_type: str) -> str:
    """
    Validates that `op_type` is a recognized operation and is supported by `protocol`
    according to PROTOCOL_OPERATION_MATRIX. Returns the normalized operation name.
    """
    proto = SupportedProtocol.from_string(protocol).value
    if not op_type or not isinstance(op_type, str):
        raise InvalidRequestError(
            "Operation 'op_type' must be a non-empty string.",
            error_code="INVALID_OPERATION",
        )
    norm_op = op_type.strip().lower().replace("-", "_")
    if norm_op not in VALID_OP_TYPES:
        raise InvalidRequestError(
            f"Unsupported operation type '{op_type}'. Valid operations are: {sorted(VALID_OP_TYPES)}",
            error_code="INVALID_OPERATION",
        )
    allowed_ops = PROTOCOL_OPERATION_MATRIX.get(proto, frozenset())
    if norm_op not in allowed_ops:
        raise InvalidRequestError(
            f"Operation '{op_type}' is not supported by protocol '{proto}'. "
            f"Supported operations for '{proto}' are: {sorted(allowed_ops)}",
            error_code="UNSUPPORTED_PROTOCOL_OPERATION",
        )
    return norm_op


class SupportedProtocol(str, Enum):
    """
    Explicitly supported northbound management protocols.

    Attributes:
        GNMI: gRPC Network Management Interface (RFC/Google gNMI specification).
        NETCONF: Network Configuration Protocol (RFC 6241 over SSH/TLS).
    """
    GNMI = "gnmi"
    NETCONF = "netconf"

    @classmethod
    def from_string(cls, val: Optional[str]) -> "SupportedProtocol":
        """
        Normalizes and parses a protocol string into a SupportedProtocol enum instance.

        Args:
            val: String representation of the protocol (case-insensitive, e.g. 'gnmi', 'NETCONF').

        Returns:
            SupportedProtocol enum instance.

        Raises:
            UnsupportedProtocolError: If val is None, empty, non-string, or not in SupportedProtocol.
        """
        if not val or not isinstance(val, str):
            raise UnsupportedProtocolError(
                f"Protocol must be explicitly declared as 'gnmi' or 'netconf'. Received: {val!r}"
            )
        normalized = val.strip().lower()
        try:
            return cls(normalized)
        except ValueError:
            raise UnsupportedProtocolError(
                f"Unsupported protocol '{val}'. Supported protocols are: {[p.value for p in cls]}"
            )


class DeliveryModeDTO(str, Enum):
    """
    Protocol-neutral telemetry delivery semantics conforming to RFC 8641.

    Attributes:
        PERIODIC: Periodic cadence-based streaming at regular sample intervals.
        EVENT_DRIVEN: On-change telemetry sent immediately when underlying state changes.
        SNAPSHOT: One-shot telemetry snapshot returning current state and terminating.
        ON_DEMAND: Subscription stream that delivers updates upon explicit trigger/poll commands.
        SERVER_DETERMINED: Server-driven mode where target device decides delivery cadence.
    """
    PERIODIC = "periodic"
    EVENT_DRIVEN = "event_driven"
    SNAPSHOT = "snapshot"
    ON_DEMAND = "on_demand"
    SERVER_DETERMINED = "server_determined"

    @classmethod
    def from_string(cls, val: Optional[str]) -> "DeliveryModeDTO":
        """
        Normalizes and parses a delivery mode string into a DeliveryModeDTO enum instance.

        Args:
            val: String representation of the delivery mode (defaults to PERIODIC if None or empty).

        Returns:
            DeliveryModeDTO enum instance.

        Raises:
            InvalidRequestError: If val is not a recognized delivery mode.
        """
        if not val:
            return cls.PERIODIC
        normalized = val.strip().lower()
        try:
            return cls(normalized)
        except ValueError:
            raise InvalidRequestError(
                f"Invalid delivery mode '{val}'. Valid options are: {[m.value for m in cls]}"
            )


@dataclass(kw_only=True)
class TelemetryPolicyDTO:
    """
    Telemetry stream delivery and timing configuration.

    Defines cadence, heartbeat intervals, and redundant update suppression for streaming subscriptions.

    Attributes:
        mode: Protocol-neutral delivery semantic (DeliveryModeDTO.PERIODIC by default).
        sample_interval_sec: Interval in seconds between periodic telemetry updates (must be non-negative).
        heartbeat_interval_sec: Maximum time in seconds without an update before sending heartbeat (optional).
        suppress_redundant: When True, suppresses unchanged updates in periodic streams.
    """
    mode: DeliveryModeDTO = DeliveryModeDTO.PERIODIC
    sample_interval_sec: float = 10.0
    heartbeat_interval_sec: Optional[float] = None
    suppress_redundant: bool = False

    def __post_init__(self):
        if isinstance(self.mode, str) and not isinstance(self.mode, DeliveryModeDTO):
            self.mode = DeliveryModeDTO.from_string(self.mode)
        if not isinstance(self.sample_interval_sec, (int, float)) or isinstance(self.sample_interval_sec, bool) or self.sample_interval_sec < 0:
            raise InvalidRequestError(
                "TelemetryPolicyDTO 'sample_interval_sec' must be a non-negative number.",
                error_code="INVALID_TELEMETRY_POLICY",
            )
        if self.heartbeat_interval_sec is not None:
            if not isinstance(self.heartbeat_interval_sec, (int, float)) or isinstance(self.heartbeat_interval_sec, bool) or self.heartbeat_interval_sec < 0:
                raise InvalidRequestError(
                    "TelemetryPolicyDTO 'heartbeat_interval_sec' must be a non-negative number.",
                    error_code="INVALID_TELEMETRY_POLICY",
                )


@dataclass(kw_only=True)
class MutationChangeDTO:
    """
    Individual configuration mutation item for Set / Edit-Config operations.

    Attributes:
        path: Target data tree XPath/path string to mutate (must be non-empty).
        value: New value to apply (scalar, dictionary, or serialized string; None for deletes).
        operation: Mutation verb ('merge', 'replace', 'delete', 'remove', 'create').
        value_type: Optional explicit data type hint for gNMI TypedValue serialization.
    """
    path: str
    value: Optional[Any] = None
    operation: str = "merge"  # "merge" | "replace" | "delete" | "remove" | "create"
    value_type: Optional[str] = None

    def __post_init__(self):
        if not self.path or not isinstance(self.path, str) or not self.path.strip():
            raise InvalidRequestError(
                "MutationChangeDTO requires a non-empty 'path'.",
                error_code="INVALID_MUTATION",
            )
        if not isinstance(self.operation, str) or self.operation.strip().lower() not in VALID_MUTATION_OPS:
            raise InvalidRequestError(
                f"Invalid mutation operation '{self.operation}'. Valid operations are: {sorted(VALID_MUTATION_OPS)}",
                error_code="INVALID_MUTATION",
            )
        self.operation = self.operation.strip().lower()


@dataclass(kw_only=True)
class AdhocEndpointDTO:
    """
    Connection and security parameters specified inline for ad-hoc or dynamic device execution.

    Allows callers to interact with targets without requiring pre-registration in DeviceRegistryService.

    Attributes:
        target: Hostname and port string formatted as '<host>:<port>' (e.g. '192.168.1.1:830').
        protocol: Mandatory Northbound protocol string.
        username: Login credential username.
        password: Login credential password.
        tls_ca: Path to Certificate Authority bundle file.
        tls_cert: Path to client certificate file (for mutual TLS).
        tls_key: Path to client private key file (for mutual TLS).
        skip_verify: When True, skips TLS server certificate validation.
        insecure: When True, uses unencrypted plaintext transport.
        tls_server_name: Server Name Indication (SNI) override.
        tls_version: Explicit TLS protocol version string (e.g. '1.3').
        ssh_key: Path to private key file for SSH authentication (NETCONF).
        gnmi_use_tunnel: When True, establishes connection via gRPC tunnel.
        gnmi_target_name: Optional logical target identifier used inside gRPC tunnel headers.
    """
    target: str  # "<host>:<port>"
    protocol: str  # Mandatory explicit declaration: "gnmi" | "netconf"
    username: Optional[str] = None
    password: Optional[str] = None
    tls_ca: Optional[str] = None
    tls_cert: Optional[str] = None
    tls_key: Optional[str] = None
    skip_verify: bool = False
    insecure: bool = False
    tls_server_name: Optional[str] = None
    tls_version: Optional[str] = None
    ssh_key: Optional[str] = None
    gnmi_use_tunnel: bool = False
    gnmi_target_name: Optional[str] = None

    def __post_init__(self):
        if not self.target or not isinstance(self.target, str) or not self.target.strip():
            raise InvalidRequestError(
                "AdhocEndpointDTO requires a non-empty 'target' (e.g. 'host:port').",
                error_code="INVALID_ENDPOINT",
            )
        # Validate explicit protocol
        self.protocol = SupportedProtocol.from_string(self.protocol).value


@dataclass(kw_only=True)
class OperationStepDTO:
    """
    Represents a single semantic operation step within an execution pipeline.

    Encapsulates selectors, mutation payloads, telemetry policies, and protocol-specific options
    for both unary RPCs (get, set, capabilities, transactions) and streaming subscriptions.

    Attributes:
        op_type: Operation name ('capabilities', 'get', 'get_config', 'set', 'edit_config',
            'subscribe', 'lock', 'unlock', 'commit', 'cancel_commit', 'discard_changes', 'get-schema').
        paths: List of data tree path strings to query or subscribe to.
        filter_expression: Raw filter string (e.g. XML subtree or XPath filter).
        filter_type: Type of filter ('xpath' or 'subtree').
        prefix: Base XPath prefix applied to all relative paths in this step.
        datastore: Target datastore name ('running', 'candidate', 'startup', 'all', 'operational', 'intended').
        changes: List of MutationChangeDTO items for configuration write operations.
        xml_payload: Raw XML configuration payload string (for NETCONF edit-config).
        telemetry: Optional TelemetryPolicyDTO defining cadence and stream behavior (for subscriptions).
        options: Dictionary of protocol-specific options (e.g. encoding, lock, confirmed, confirm_timeout, persist_id).
    """
    op_type: str  # "capabilities" | "get" | "set" | "subscribe" | "lock" | "unlock" | "commit" | "discard_changes" | "get-schema"
    # Selectors
    paths: list[str] = field(default_factory=list)
    filter_expression: Optional[str] = None
    filter_type: Optional[str] = None  # "xpath" | "subtree"
    prefix: Optional[str] = None
    datastore: str = "all"  # "running" | "candidate" | "startup" | "all"

    # Mutations (Set / Edit-Config)
    changes: list[MutationChangeDTO] = field(default_factory=list)
    xml_payload: Optional[str] = None

    # Telemetry
    telemetry: Optional[TelemetryPolicyDTO] = None

    # Protocol-specific options (encoding, lock, confirmed, confirm_timeout, persist_id)
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.op_type or not isinstance(self.op_type, str) or not self.op_type.strip():
            raise InvalidRequestError(
                "OperationStepDTO requires a non-empty 'op_type'.",
                error_code="INVALID_OPERATION",
            )
        norm_op = self.op_type.strip().lower().replace("-", "_")
        if norm_op not in VALID_OP_TYPES:
            raise InvalidRequestError(
                f"Unsupported operation type '{self.op_type}'. Valid operations are: {sorted(VALID_OP_TYPES)}",
                error_code="INVALID_OPERATION",
            )
        if not isinstance(self.datastore, str) or self.datastore.strip().lower() not in VALID_DATASTORES:
            raise InvalidRequestError(
                f"Invalid datastore '{self.datastore}'. Valid datastores are: {sorted(VALID_DATASTORES)}",
                error_code="INVALID_DATASTORE",
            )
        self.datastore = self.datastore.strip().lower()

        if self.filter_type is not None:
            if not isinstance(self.filter_type, str) or self.filter_type.strip().lower() not in VALID_FILTER_TYPES:
                raise InvalidRequestError(
                    f"Invalid filter_type '{self.filter_type}'. Valid filter types are: {sorted(VALID_FILTER_TYPES)}",
                    error_code="INVALID_SELECTOR",
                )
            self.filter_type = self.filter_type.strip().lower()

        if not isinstance(self.paths, list) or any(not isinstance(p, str) or not p.strip() for p in self.paths):
            raise InvalidRequestError(
                "OperationStepDTO 'paths' must be a list of non-empty strings.",
                error_code="INVALID_SELECTOR",
            )

        if not isinstance(self.options, dict):
            raise InvalidRequestError(
                "OperationStepDTO 'options' must be a dictionary.",
                error_code="INVALID_OPTIONS",
            )


@dataclass(kw_only=True)
class ExecutionRequestDTO:
    """
    Top-level external request to execute operations against a network device.

    Can target a registered device by 'device_id' or a dynamic target via 'adhoc_endpoint'.

    Attributes:
        device_id: Identifier of a pre-registered device in DeviceRegistryService (optional).
        adhoc_endpoint: Explicit AdhocEndpointDTO connection parameters (optional).
        protocol: Optional protocol override or explicit declaration ('gnmi' or 'netconf').
        operations: Ordered list of OperationStepDTO items to execute.
        schema_profile_id: Optional schema profile identifier for pre-flight validation.
        schema_dirs: Paths to directories containing YANG schema files.
        schema_cache_dir: Path to directory for SQLite/YANG compilation cache.
        schema_validate: When True, validates requests against compiled schemas before execution.
        schema_auto_discovery: When True, automatically discovers YANG schemas from target device.
        schema_auto_fetch: When True, automatically downloads missing schemas via RFC 6022.
        validate_only: When True, runs only offline schema validation without opening device connection.
        timeout_seconds: Per-request connection and execution timeout in seconds (default: 30, max: 3600).
        times: Repetition count for repeating unary executions (default: 1, max: 1000).
    """
    device_id: Optional[str] = None
    adhoc_endpoint: Optional[AdhocEndpointDTO] = None
    protocol: Optional[str] = None  # May override or be verified against device_id profile

    operations: list[OperationStepDTO] = field(default_factory=list)
    schema_profile_id: Optional[str] = None
    schema_dirs: list[str] = field(default_factory=list)
    schema_cache_dir: Optional[str] = None
    schema_validate: bool = False
    schema_auto_discovery: bool = True
    schema_auto_fetch: bool = True
    validate_only: bool = False
    timeout_seconds: int = 30
    times: int = 1

    def __post_init__(self):
        if not self.device_id and not self.adhoc_endpoint:
            raise InvalidRequestError(
                "ExecutionRequestDTO requires either 'device_id' or 'adhoc_endpoint'.",
                error_code="MISSING_TARGET",
            )
        if self.protocol is not None:
            self.protocol = SupportedProtocol.from_string(self.protocol).value

        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
        ):
            raise InvalidRequestError(
                "ExecutionRequestDTO 'timeout_seconds' must be positive.",
                error_code="INVALID_TIMEOUT",
            )
        if self.timeout_seconds > MAX_TIMEOUT_SECONDS:
            raise InvalidRequestError(
                f"ExecutionRequestDTO 'timeout_seconds' ({self.timeout_seconds}) exceeds maximum allowed ({MAX_TIMEOUT_SECONDS}).",
                error_code="INVALID_TIMEOUT",
            )

        if not isinstance(self.times, int) or isinstance(self.times, bool) or self.times < 0:
            raise InvalidRequestError(
                "ExecutionRequestDTO 'times' cannot be negative.",
                error_code="INVALID_REPETITIONS",
            )
        if self.times == 0:
            self.times = 1
        elif self.times > MAX_REPETITIONS:
            raise InvalidRequestError(
                f"ExecutionRequestDTO 'times' ({self.times}) exceeds maximum allowed ({MAX_REPETITIONS}).",
                error_code="INVALID_REPETITIONS",
            )

        effective_protocol = self.protocol or (
            self.adhoc_endpoint.protocol if self.adhoc_endpoint else None
        )
        if effective_protocol and self.operations:
            for step in self.operations:
                validate_protocol_operation(effective_protocol, step.op_type)


@dataclass(kw_only=True)
class StepResultDTO:
    """
    Result of an individual operation step within an execution pipeline.

    Attributes:
        op_type: Operation name or RPC identifier (e.g. 'get', 'set', 'lock', 'commit').
        success: True if the operation succeeded; False if an error occurred.
        data: Parsed response payload (dictionary, list, or XML/JSON string) on success.
        error: Human-readable error message if the operation failed.
        error_code: Standardized error code identifier (e.g. 'RPC_EXECUTION_ERROR').
    """
    op_type: str
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    error_code: Optional[str] = None


@dataclass(kw_only=True)
class ExecutionResultDTO:
    """
    Structured response returned from unary execution.

    Contains status, timing, and individual step outcomes decoupled from terminal/file presentation sinks.

    Attributes:
        target: Target device address string ('<host>:<port>').
        protocol: Management protocol used ('gnmi' or 'netconf').
        success: Overall execution success (True only if all steps completed without error).
        results: List of StepResultDTO items for each operation in the request.
        error_summary: Consolidated error message if any step failed.
        error_code: Standardized error code string for top-level failure triage.
        execution_time_ms: Total execution duration in milliseconds.
    """
    target: str
    protocol: str
    success: bool
    results: list[StepResultDTO] = field(default_factory=list)
    error_summary: Optional[str] = None
    error_code: Optional[str] = None
    execution_time_ms: float = 0.0


@dataclass(kw_only=True)
class StreamEventDTO:
    """
    Structured telemetry event received from a streaming subscription.

    Suitable for consumption by WebSockets, event busses, and CLI runners.

    Attributes:
        session_id: Unique identifier of the subscription worker session.
        target: Target device address string ('<host>:<port>').
        protocol: Management protocol used ('gnmi' or 'netconf').
        subscription_name: Name of the subscription stream as configured by caller.
        data: Parsed telemetry notification payload (dictionary or list).
        timestamp: Unix epoch timestamp in seconds when the event was received.
        is_sync_marker: When True, indicates initial sync marker (e.g. gNMI sync_response).
        error: Optional error description if the event represents a streaming error.
    """
    session_id: str
    target: str
    protocol: str
    subscription_name: str
    data: Any
    timestamp: float = field(default_factory=time.time)
    is_sync_marker: bool = False
    error: Optional[str] = None


def expand_request_repetitions(
    requests: Sequence[ExecutionRequestDTO],
) -> list[ExecutionRequestDTO]:
    """Expands requests according to their .times repetition multiplier into single-run DTOs."""
    import dataclasses
    expanded: list[ExecutionRequestDTO] = []
    for req in requests:
        repeat = max(getattr(req, "times", 1), 1)
        for _ in range(repeat):
            cloned = dataclasses.replace(req, times=1)
            expanded.append(cloned)
    return expanded


