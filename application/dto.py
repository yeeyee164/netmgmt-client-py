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
    """Explicitly supported northbound management protocols."""
    GNMI = "gnmi"
    NETCONF = "netconf"

    @classmethod
    def from_string(cls, val: Optional[str]) -> "SupportedProtocol":
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
    """Protocol-neutral telemetry delivery semantics conforming to RFC 8641."""
    PERIODIC = "periodic"
    EVENT_DRIVEN = "event_driven"
    SNAPSHOT = "snapshot"
    ON_DEMAND = "on_demand"
    SERVER_DETERMINED = "server_determined"

    @classmethod
    def from_string(cls, val: Optional[str]) -> "DeliveryModeDTO":
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
    """Telemetry stream delivery and timing configuration."""
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
    """Individual configuration mutation item for Set / Edit-Config."""
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
    """Connection parameters specified inline for ad-hoc or CLI execution."""
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
    """Represents a single semantic operation step within a pipeline."""
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
    """Top-level external request to execute operations against a device."""
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
    """Result of an individual operation step."""
    op_type: str
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    error_code: Optional[str] = None


@dataclass(kw_only=True)
class ExecutionResultDTO:
    """Structured response returned from unary execution."""
    target: str
    protocol: str
    success: bool
    results: list[StepResultDTO] = field(default_factory=list)
    error_summary: Optional[str] = None
    error_code: Optional[str] = None
    execution_time_ms: float = 0.0


@dataclass(kw_only=True)
class StreamEventDTO:
    """Structured telemetry event received from a streaming subscription."""
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


