from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Iterator, Any, Optional, Union, List

from config.operations import (
    CapabilitiesOperation,
    GetOperation,
    SetOperation,
    SubscribeOperation,
    GetSchemaOperation,
    TransactionOperation,
)
from config.protocol_options.base import BaseProtocolOptions
from config.selectors import Selector
from specs.stream_types import StreamContext, StreamEvent


class UnsupportedOperationError(NotImplementedError):
    """Raised when an operation is not supported by a protocol client."""
    pass


class BaseClient(ABC):
    """
    Abstract Base Class for all Northbound Protocol Clients such as NETCONF, gNMI, RESTCONF...
    Establishes a unified interface so managers and workers can operate protocol-agnostically.
    """

    def __enter__(self):
        """Initializes connection of session context manager."""
        return self

    @abstractmethod
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Safely tears down connection or session context manager"""
        pass

    # =========================================================================
    # Semantic Intent Operations
    # =========================================================================

    def execute_capabilities(self, operation: CapabilitiesOperation) -> Any:
        """Executes capabilities discovery based on CapabilitiesOperation."""
        raise UnsupportedOperationError(
            f"{self.__class__.__name__} does not support capabilities operations."
        )

    def execute_get(self, operation: GetOperation) -> Any:
        """Executes a get/read operation based on GetOperation."""
        raise UnsupportedOperationError(
            f"{self.__class__.__name__} does not support get operations."
        )

    def execute_set(self, operation: SetOperation) -> Any:
        """Executes a set/edit operation based on SetOperation."""
        raise UnsupportedOperationError(
            f"{self.__class__.__name__} does not support set operations."
        )

    def execute_subscribe(
        self,
        selectors: Any = None,
        options: Optional[BaseProtocolOptions] = None,
        context: Optional[StreamContext] = None,
        operation: Optional[SubscribeOperation] = None,
        **kwargs,
    ) -> Iterator[StreamEvent]:
        """Execute a telemetry streaming session.

        Raises:
            UnsupportedOperationError: If the protocol handler does not support streaming.
        """
        raise UnsupportedOperationError(
            f"{self.__class__.__name__} does not support subscribe operations."
        )

    def execute_schema(self, operation: GetSchemaOperation) -> Any:
        """Executes schema retrieval based on GetSchemaOperation."""
        raise UnsupportedOperationError(
            f"{self.__class__.__name__} does not support schema operations."
        )

    def execute_transaction(self, operation: TransactionOperation) -> Any:
        """Executes transaction lifecycle operations based on TransactionOperation."""
        raise UnsupportedOperationError(
            f"{self.__class__.__name__} does not support transaction operations."
        )

    def execute(self, operation: Any) -> Any:
        """Executes a semantic operation by dispatching to the appropriate execute_* method."""
        if isinstance(operation, CapabilitiesOperation):
            return self.execute_capabilities(operation)
        elif isinstance(operation, GetSchemaOperation):
            return self.execute_schema(operation)
        elif isinstance(operation, GetOperation):
            return self.execute_get(operation)
        elif isinstance(operation, SetOperation):
            return self.execute_set(operation)
        elif isinstance(operation, SubscribeOperation):
            return self.execute_subscribe(operation=operation)
        elif isinstance(operation, TransactionOperation):
            return self.execute_transaction(operation)
        else:
            raise UnsupportedOperationError(
                f"{self.__class__.__name__} does not support operation {type(operation).__name__}."
            )

    def _prepare_schema_context(self, allow_auto_discovery: bool = False) -> tuple[Any, Any]:
        """
        Ensures `self.schema_profile` is initialized (optionally running `discover_schema_profile()`
        when `allow_auto_discovery=True`) and compiles/retrieves the isolated `SchemaContext`
        from `SchemaManager`.

        Returns:
            Tuple of `(SchemaManager, SchemaContext)`.
        """
        from config.schema import SchemaProfile
        from core.schema_manager import SchemaManager

        if getattr(self, "schema_profile", None) is None:
            if (
                allow_auto_discovery
                and getattr(self.schema_config, "auto_discovery", False)
                and getattr(self, "session", None)
                and hasattr(self, "discover_schema_profile")
            ):
                try:
                    self.discover_schema_profile()
                except Exception:
                    pass
            if getattr(self, "schema_profile", None) is None:
                pid = (
                    getattr(self.schema_config, "device_profile", None)
                    or getattr(self.schema_config, "profile", None)
                )
                self.schema_profile = SchemaProfile(content_id=pid)

        sm = SchemaManager(global_cache_dir=self.schema_config.cache_dir)
        sctx = sm.get_or_create_context(
            profile=self.schema_profile,
            search_dirs=self.schema_config.yang_dirs,
            strict=True,
        )
        return sm, sctx
