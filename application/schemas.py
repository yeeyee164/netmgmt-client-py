# -*- coding: utf-8 -*-
"""
Schema Catalog Service.
Provides offline YANG schema validation, metadata queries, and SQLite cache introspection.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from core.schema_manager import SchemaManager
from core.schema_db import SchemaDB
from application.dto import ExecutionRequestDTO


class SchemaCatalogService:
    """
    Application-level facade bridging to the core YANG schema engine and SQLite database.
    """

    def __init__(
        self,
        schema_manager: Optional[SchemaManager] = None,
        schema_db: Optional[SchemaDB] = None,
    ):
        self._schema_manager = schema_manager or SchemaManager()
        self._schema_db = schema_db or SchemaDB()

    def validate_request_schema(self, request: ExecutionRequestDTO) -> List[str]:
        """
        Validates operations in an ExecutionRequestDTO against compiled YANG schema nodes.
        Returns a list of validation error messages (empty if completely valid).
        """
        profile_id = request.schema_profile_id or "default"
        search_dirs = [Path(p) for p in request.schema_dirs]

        errors: List[str] = []
        try:
            # Ensure context exists
            self._schema_manager.get_or_create_context(profile=profile_id, search_dirs=search_dirs)
        except Exception as e:
            return [f"Failed to initialize schema context for profile '{profile_id}': {e}"]

        for step_idx, step in enumerate(request.operations):
            # Validate simple paths
            for p in step.paths:
                try:
                    self._schema_manager.validate_path(path=p, profile_id=profile_id)
                except Exception as e:
                    errors.append(f"Step {step_idx} ({step.op_type}) invalid path '{p}': {e}")

            # Validate mutation changes
            for change in step.changes:
                try:
                    self._schema_manager.validate_path(path=change.path, profile_id=profile_id)
                    if change.value is not None:
                        self._schema_manager.validate_payload(
                            path=change.path,
                            value=change.value,
                            profile_id=profile_id,
                        )
                except Exception as e:
                    errors.append(f"Step {step_idx} ({step.op_type}) mutation error on '{change.path}': {e}")

        return errors

    def list_cached_modules(self) -> List[Dict[str, Any]]:
        """Returns metadata for all YANG modules stored in the local SQLite cache."""
        return self._schema_db.list_modules()

    def get_database_statistics(self) -> Dict[str, Any]:
        """Returns SQLite schema database cache metrics."""
        return self._schema_db.get_database_statistics()

    def get_profile_modules(self, profile_id: str) -> List[Dict[str, Any]]:
        """Returns the list of modules associated with a schema profile."""
        return self._schema_db.get_profile_modules(profile_id)

