from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any, Union, List
from modules.security import SecurityProfile
from config.operations import OperationConfig
from config.protocol_options.base import BaseProtocolOptions
from config.protocol_options.gnmi import GnmiOptions
from config.protocol_options.netconf import NetconfOptions
from config.selectors import Selector, SelectorConfig
from config.delivery import DeliveryPolicy
from config.schema import SchemaConfig

class Protocol(str, Enum):
    NONE = "none"
    GNMI = "gnmi"
    NETCONF = "netconf"
    RESTCONF = "restconf"

class OutputType(str, Enum):
    """
    Define Output types
    
    Either `STDOUT` or `STDERR` direct to terminal.
    Either `FILE` or `SYSLOG` require additional arguments.
    """
    STDOUT = "stdout"
    STDERR = "stderr"
    FILE = "file"
    SYSLOG = "syslog"

class OutputFormat(str, Enum):
    JSON = "json"
    XML = "xml"
    TEXT = "text"

@dataclass(frozen=True, kw_only=True)
class ConnectionConfig:
    """Pure network transport and authentication parameters."""
    target: str
    username: str = ""
    password: str = ""
    security: SecurityProfile = field(default_factory=SecurityProfile)
    insecure: bool = False

    @property
    def target_ip(self) -> str:
        """Extract IP address or hostname from target, supporting IPv4, bracketed IPv6, and hostnames."""
        if not self.target:
            return ""
        if self.target.startswith('['):
            closing_bracket = self.target.find(']')
            if closing_bracket != -1:
                return self.target[1:closing_bracket]
        if ':' in self.target:
            parts = self.target.split(':')
            if len(parts) == 2 and parts[1].isdigit():
                return parts[0]
        return self.target.strip('[]')

    @property
    def target_port(self) -> int:
        """Extract port number from target, or return 0 if omitted."""
        if not self.target:
            return 0
        if self.target.startswith('['):
            closing_bracket = self.target.find(']')
            if closing_bracket != -1 and closing_bracket < len(self.target) - 1:
                remainder = self.target[closing_bracket + 1:]
                if remainder.startswith(':'):
                    try:
                        return int(remainder[1:])
                    except ValueError:
                        return 0
            return 0
        if ':' in self.target:
            parts = self.target.split(':')
            if len(parts) == 2 and parts[1].isdigit():
                return int(parts[1])
        return 0

@dataclass(frozen=True, kw_only=True)
class ExecutionConfig:
    """Execution policies common to all operations."""
    timeout: int = 30     # timeout value of for each RPC. It may denote TCP timeout.
    retry_count: int = 0  # Retry count
    times: int = 1        # If it has more than 1, it will spawn multiple requests with same information.
    continue_on_error: bool = False  # If True, sequential pipeline continues on RPC error

@dataclass(frozen=True, kw_only=True)
class SessionConfig:
    """Top-level session definition pairing connection, protocol, and intent."""
    connection: ConnectionConfig       # connection part
    protocol: Protocol                 # protocol session
    operation: Optional[OperationConfig] = None   # primary/legacy operation of `protocol`
    operations: tuple[OperationConfig, ...] = ()  # sequential list of operations
    execution: ExecutionConfig = field(default_factory=ExecutionConfig) 
    schema: SchemaConfig = field(default_factory=SchemaConfig)

    def __post_init__(self):
        if self.operation is not None and not self.operations:
            object.__setattr__(self, 'operations', (self.operation,))
        elif self.operations:
            ops_tuple = tuple(self.operations)
            object.__setattr__(self, 'operations', ops_tuple)
            if self.operation is None and ops_tuple:
                object.__setattr__(self, 'operation', ops_tuple[0])

        if self.schema is None:
            object.__setattr__(self, 'schema', SchemaConfig())
        elif isinstance(self.schema, dict):
            object.__setattr__(self, 'schema', SchemaConfig(**self.schema))

    @property
    def target(self) -> str:
        return self.connection.target

    @property
    def target_ip(self) -> str:
        return self.connection.target_ip

    @property
    def target_port(self) -> int:
        return self.connection.target_port

    @property
    def username(self) -> str:
        return self.connection.username

    @property
    def password(self) -> str:
        return self.connection.password

    @property
    def security(self) -> SecurityProfile:
        return self.connection.security

    @property
    def insecure(self) -> bool:
        return self.connection.insecure

    @property
    def times(self) -> int:
        return self.execution.times

    @property
    def prefix(self) -> str:
        if hasattr(self.operation, 'prefix') and self.operation.prefix:
            return self.operation.prefix
        if hasattr(self.operation, 'selector') and hasattr(self.operation.selector, 'prefix'):
            return self.operation.selector.prefix
        return ""

    @property
    def paths(self) -> list[str]:
        if hasattr(self.operation, 'selector') and hasattr(self.operation.selector, 'paths'):
            return list(self.operation.selector.paths)
        return []

    @property
    def updates(self) -> list:
        from config.operations import SetOperation, ChangeType
        if isinstance(self.operation, SetOperation):
            return [(c.path, c.value) for c in self.operation.changes if c.operation == ChangeType.MERGE]
        return []

    @property
    def replaces(self) -> list:
        from config.operations import SetOperation, ChangeType
        if isinstance(self.operation, SetOperation):
            return [(c.path, c.value) for c in self.operation.changes if c.operation == ChangeType.REPLACE]
        return []

    @property
    def deletes(self) -> list:
        from config.operations import SetOperation, ChangeType
        if isinstance(self.operation, SetOperation):
            return [c.path for c in self.operation.changes if c.operation == ChangeType.DELETE]
        return []

    @property
    def target_datastore(self) -> str:
        if hasattr(self.operation, 'protocol_options') and hasattr(self.operation.protocol_options, 'target_datastore'):
            return self.operation.protocol_options.target_datastore
        return "candidate"

    @property
    def config(self) -> Optional[str]:
        if hasattr(self.operation, 'protocol_options') and hasattr(self.operation.protocol_options, 'config'):
            return self.operation.protocol_options.config
        return None

    @property
    def default_operation(self) -> str:
        if hasattr(self.operation, 'protocol_options') and hasattr(self.operation.protocol_options, 'default_operation'):
            return self.operation.protocol_options.default_operation
        return "merge"

    @property
    def error_option(self) -> str:
        if hasattr(self.operation, 'protocol_options') and hasattr(self.operation.protocol_options, 'error_option'):
            return self.operation.protocol_options.error_option
        return "stop-on-error"

    @property
    def sample_interval(self) -> int:
        if hasattr(self.operation, 'delivery') and hasattr(self.operation.delivery, 'interval'):
            return self.operation.delivery.interval
        return 0

    @property
    def subscription_name(self) -> str:
        if hasattr(self.operation, 'subscription_name'):
            return self.operation.subscription_name
        return "default"

    @property
    def yang_dirs(self) -> tuple:
        return self.schema.yang_dirs

    @property
    def yang_cache_dir(self):
        return self.schema.cache_dir

    @property
    def enable_schema_validation(self) -> bool:
        return self.schema.validate

    def validate(self) -> None:
        """Validate session parameters and underlying operation(s)."""
        for op in self.operations:
            if hasattr(op, 'validate') and callable(op.validate):
                op.validate()
            elif hasattr(op, 'protocol_options') and op.protocol_options is not None:
                if hasattr(op.protocol_options, 'validate'):
                    op.protocol_options.validate()

# ================
# Output Classes
# ================

@dataclass(kw_only=True)
class OutputConfig:
    """Output specifier"""
    output_type: OutputType
    format: OutputFormat
    name: str = 'default_output'

    # Variant-specific fields
    # Path is for `OutputType.FILE`
    # syslog_* is for `OutputType.SYSLOG`
    path: str | None = None
    syslog_host: str | None = None
    syslog_port: int | None = None

    def __post_init__(self):
        if self.output_type == OutputType.FILE and not self.path:
            raise ValueError("path must be specified for FILE option")
        if self.output_type == OutputType.SYSLOG and not self.syslog_host:
            raise ValueError("syslog_host must be specified for SYSLOG option")
