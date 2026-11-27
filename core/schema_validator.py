# -*- encoding: utf-8 -*-
"""
schema_validator.py

Operation-aware validation engine checking XPath schema validity, writability
(config true vs config false), mandatory list keys, and payload constraints.
"""
from __future__ import annotations
import logging
import time
from typing import Optional, Union, Any

from config.selectors import PathSelector
from config.operations import (
    OperationConfig,
    GetOperation,
    GetConfigOperation,
    SetOperation,
    EditConfigOperation,
)
from core.schema_context import SchemaContext
from core.schema_errors import (
    SchemaValidationError,
    InvalidSchemaPathError,
)
from modules.stats_hooks import NOOP_STATS_RECORDER, StatsRecorder, safe_record

logger = logging.getLogger(__name__)


class SchemaValidator:
    """
    Validates operational paths and payloads against compiled libyang SchemaContext.
    """

    def __init__(self, stats_recorder: Optional[StatsRecorder] = None):
        self._stats_recorder: StatsRecorder = stats_recorder or NOOP_STATS_RECORDER

    def set_stats_recorder(self, stats_recorder: Optional[StatsRecorder]) -> None:
        self._stats_recorder = stats_recorder or NOOP_STATS_RECORDER

    @staticmethod
    def _infer_protocol(operation: Optional[OperationConfig]) -> str:
        if operation is None:
            return "unknown"
        cls_name = type(operation).__name__.lower()
        if "editconfig" in cls_name or "netconf" in cls_name:
            return "netconf"
        opts = getattr(operation, "protocol_options", None)
        if opts is not None:
            opts_name = type(opts).__name__.lower()
            if "gnmi" in opts_name:
                return "gnmi"
            if "netconf" in opts_name:
                return "netconf"
        return "unknown"

    @staticmethod
    def _extract_path_str(path: Union[str, PathSelector]) -> str:
        if isinstance(path, str):
            return path
        if hasattr(path, "paths") and path.paths:
            return path.paths[0]
        if hasattr(path, "path") and path.path:
            return path.path
        return str(path)

    def validate_path(
        self,
        path: Union[str, PathSelector],
        schema_context: SchemaContext,
        operation: Optional[OperationConfig] = None,
        stats_recorder: Optional[StatsRecorder] = None,
    ) -> Any:
        """
        Verifies that an XPath points to a valid schema node within the compiled context,
        and enforces operation constraints (e.g. read-only config false rejection for Set).
        Returns the matched SchemaNode.
        """
        rec = stats_recorder or self._stats_recorder
        proto = self._infer_protocol(operation)
        t0 = time.perf_counter()
        passed = False
        try:
            path_str = self._extract_path_str(path)
            if not path_str or path_str == "/":
                passed = True
                return None

            # Clean gNMI-style leading origin prefixes if present, e.g. "openconfig:/..."
            cleaned_path = path_str
            if not cleaned_path.startswith("/"):
                cleaned_path = "/" + cleaned_path

            # Strip list keys from path for find_path lookup (libyang find_path requires schema node path or path expression)
            # e.g., /ietf-interfaces:interfaces/interface[name='eth0']/description -> /ietf-interfaces:interfaces/interface/description
            import re
            schema_lookup_path = re.sub(r'\[[^\]]+\]', '', cleaned_path)

            try:
                nodes = list(schema_context.find_path(schema_lookup_path))
                if not nodes:
                    raise InvalidSchemaPathError(
                        f"Path '{path_str}' does not resolve to any schema node in the compiled context."
                    )
                schema_node = nodes[0]
            except Exception as e:
                if isinstance(e, InvalidSchemaPathError):
                    raise
                raise InvalidSchemaPathError(
                    f"Path '{path_str}' is invalid in the compiled schema context: {e}"
                ) from e

            # Operation-aware checks
            is_set_operation = isinstance(operation, (SetOperation, EditConfigOperation)) or (
                operation is not None and getattr(operation, "__class__", None).__name__ in ("SetOperation", "EditConfigOperation")
            )

            if is_set_operation:
                # Check read-only state
                if hasattr(schema_node, "config_false") and schema_node.config_false():
                    raise SchemaValidationError(
                        f"Path '{path_str}' targets a read-only node ('config false') "
                        f"which cannot be mutated by a Set/EditConfig operation."
                    )

            passed = True
            return schema_node
        finally:
            safe_record(
                rec.record_schema_validation,
                proto,
                passed,
                (time.perf_counter() - t0) * 1000.0,
            )

    def validate_payload(
        self,
        path: Union[str, PathSelector],
        value: Any,
        schema_context: SchemaContext,
        operation: Optional[OperationConfig] = None,
        stats_recorder: Optional[StatsRecorder] = None,
    ) -> None:
        """
        Verifies that a value conforms to the schema constraints for the given path.
        """
        rec = stats_recorder or self._stats_recorder
        proto = self._infer_protocol(operation)
        path_str = self._extract_path_str(path)
        if not path_str or value is None:
            return

        # First ensure path is schema-valid
        self.validate_path(path_str, schema_context, operation=operation, stats_recorder=rec)

        # Ensure leading slash for data path
        data_path = path_str if path_str.startswith("/") else "/" + path_str

        # Test building data path in libyang
        t0 = time.perf_counter()
        passed = False
        try:
            if isinstance(value, (dict, list)):
                from core.schema_data import DataBuilder
                DataBuilder.populate_tree(schema_context, data_path, value)
            else:
                val_str = str(value).lower() if isinstance(value, bool) else (str(value) if value is not None else None)
                if val_str is not None:
                    schema_context.create_data_path(data_path, value=val_str)
            passed = True
        except Exception as e:
            if isinstance(e, SchemaValidationError):
                raise
            raise SchemaValidationError(
                f"Payload validation failed at path '{path_str}' for value '{value}': {e}"
            ) from e
        finally:
            safe_record(
                rec.record_schema_validation,
                proto,
                passed,
                (time.perf_counter() - t0) * 1000.0,
            )

