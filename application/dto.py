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


@dataclass(kw_only=True)
class MutationChangeDTO:
    """Individual configuration mutation item for Set / Edit-Config."""
    path: str
    value: Optional[Any] = None
    operation: str = "merge"  # "merge" | "replace" | "delete" | "remove"
    value_type: Optional[str] = None


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
        if not self.target or not isinstance(self.target, str):
            raise InvalidRequestError("AdhocEndpointDTO requires a non-empty 'target' (e.g. 'host:port').")
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
            raise InvalidRequestError("ExecutionRequestDTO requires either 'device_id' or 'adhoc_endpoint'.")
        if self.protocol is not None:
            self.protocol = SupportedProtocol.from_string(self.protocol).value
        if self.timeout_seconds <= 0:
            raise InvalidRequestError("ExecutionRequestDTO 'timeout_seconds' must be positive.")
        if self.times <= 0:
            self.times = 1


@dataclass(kw_only=True)
class StepResultDTO:
    """Result of an individual operation step."""
    op_type: str
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None


@dataclass(kw_only=True)
class ExecutionResultDTO:
    """Structured response returned from unary execution."""
    target: str
    protocol: str
    success: bool
    results: list[StepResultDTO] = field(default_factory=list)
    error_summary: Optional[str] = None
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


