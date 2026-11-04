from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Union
from config.selectors import Selector, PathSelector
from config.delivery import DeliveryPolicy
from config.protocol_options.base import BaseProtocolOptions

@dataclass(frozen=True, kw_only=True)
class OperationConfig:
    """Base marker for all semantic operational intents."""
    op_id: Optional[str] = None

    def validate(self) -> None:
        """Validate operational intent and encapsulated options."""
        if hasattr(self, 'protocol_options') and self.protocol_options is not None:
            if hasattr(self.protocol_options, 'validate'):
                self.protocol_options.validate()

@dataclass(frozen=True, kw_only=True)
class CapabilitiesOperation(OperationConfig):
    """Discovery of supported models, encodings, and extensions."""
    pass

@dataclass(frozen=True, kw_only=True)
class GetSchemaOperation(OperationConfig):
    """Retrieval of schema definitions (RFC 6022 / YANG files)."""
    identifier: str
    version: Optional[str] = None
    format: str = "yang"

@dataclass(frozen=True, kw_only=True)
class GetOperation(OperationConfig):
    """Retrieval of operational or configuration state."""
    selector: Selector
    read_scope: str = "all"  # all, config, state, operational
    protocol_options: Optional[BaseProtocolOptions] = None

    def validate(self) -> None:
        if hasattr(self.selector, 'validate'):
            self.selector.validate()
        if self.protocol_options is not None and hasattr(self.protocol_options, 'validate'):
            self.protocol_options.validate()

class ChangeType(str, Enum):
    MERGE = "merge"
    REPLACE = "replace"
    DELETE = "delete"
    CREATE = "create"
    REMOVE = "remove"

@dataclass(frozen=True, kw_only=True)
class GetConfigOperation(GetOperation):
    r"""
    Retrieval of configuration datastores

    Probably Affected protocols:
        - gNMI Get(type: config)
        - NETCONF \<get-config\>
    """
    read_scope: str = "config"

    def __init__(
        self,
        selector: Optional[Selector] = None,
        read_scope: str = "config",
        protocol_options: Optional[BaseProtocolOptions] = None,
        **kwargs,
    ):
        sel = kwargs.get('selector', selector)
        if sel is None:
            sel = PathSelector(paths=())
        object.__setattr__(self, 'selector', sel)
        object.__setattr__(self, 'read_scope', read_scope)
        object.__setattr__(self, 'protocol_options', kwargs.get('protocol_options', protocol_options))
        object.__setattr__(self, 'op_id', kwargs.get('op_id', None))

@dataclass(frozen=True, kw_only=True)
class Change:
    """Atomic mutation intent."""
    path: str
    operation: ChangeType = ChangeType.MERGE
    value: Any = None
    type: Optional[str] = None

    def __eq__(self, other):
        if isinstance(other, tuple):
            return (self.path, self.operation, self.value) == other[:3]
        if isinstance(other, Change):
            return (self.path, self.operation, self.value, self.type) == (other.path, other.operation, other.value, other.type)
        return False

@dataclass(frozen=True, kw_only=True)
class SetOperation(OperationConfig):
    """Mutation of configuration datastores."""
    changes: tuple[Change, ...] = ()
    prefix: str = ""
    protocol_options: Optional[BaseProtocolOptions] = None

    def __init__(self, changes: Any = (), prefix: str = "", protocol_options: Optional[BaseProtocolOptions] = None, **kwargs):
        c = kwargs.get('changes', changes)
        pr = kwargs.get('prefix', prefix)
        po = kwargs.get('protocol_options', protocol_options)
        if isinstance(c, tuple):
            object.__setattr__(self, 'changes', c)
        elif isinstance(c, (list, set)):
            object.__setattr__(self, 'changes', tuple(c))
        else:
            object.__setattr__(self, 'changes', tuple(c))
        object.__setattr__(self, 'prefix', pr)
        object.__setattr__(self, 'protocol_options', po)
        object.__setattr__(self, 'op_id', kwargs.get('op_id', None))

    def validate(self) -> None:
        if self.protocol_options is not None and hasattr(self.protocol_options, 'validate'):
            self.protocol_options.validate()

@dataclass(frozen=True, kw_only=True)
class EditConfigOperation(SetOperation):
    r"""
    Mutation of configuration datastores

    Probably Affected protocols:
        - gNMI Set(maybe)
        - NETCONF \<edit-config\>
    """
    pass

@dataclass(frozen=True, kw_only=True)
class SubscribeOperation(OperationConfig):
    """Continuous or snapshot data subscription."""
    selector: Selector
    delivery: DeliveryPolicy
    subscription_name: str = "default"
    protocol_options: Optional[BaseProtocolOptions] = None

    def validate(self) -> None:
        if hasattr(self.selector, 'validate'):
            self.selector.validate()
        if hasattr(self.delivery, 'validate'):
            self.delivery.validate()
        if self.protocol_options is not None and hasattr(self.protocol_options, 'validate'):
            self.protocol_options.validate()

class TransactionType(str, Enum):
    """Define possible transactional operations among NB clients"""
    LOCK = "lock"
    UNLOCK = "unlock"
    COMMIT = "commit"
    CANCEL_COMMIT = "cancel-commit"
    DISCARD_CHANGES = "discard-changes"
    VALIDATE = "validate"

@dataclass(frozen=True, kw_only=True)
class TransactionOperation(OperationConfig):
    """Define common transaction operations such as commit"""
    operation: TransactionType
    confirmed: bool = False
    confirm_timeout: Optional[int] = None
    protocol_options: Optional[BaseProtocolOptions] = None

    def validate(self) -> None:
        super().validate()
        if self.confirm_timeout is not None and self.confirm_timeout <= 0:
            raise ValueError(
                f"Invalid confirm_timeout '{self.confirm_timeout}'. Must be a positive integer."
            )

@dataclass(frozen=True, kw_only=True)
class NetconfTransactionOperation(TransactionOperation):
    """Explicit NETCONF transaction lifecycle operations (RFC 6241)."""
    target_datastore: str = "candidate"
    source_datastore: str = "candidate"
    persist: str = ""
    persist_id: str = ""
