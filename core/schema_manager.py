# -*- encoding: utf-8 -*-
"""
schema_manager.py

Thread-safe Singleton facade orchestrating schema contexts, metadata database,
model catalog, dependency resolution, validation, and wire serialization.
"""
from __future__ import annotations
import logging
import threading
from pathlib import Path
from typing import Optional, Union, Sequence, Dict, Any, Callable

logger = logging.getLogger(__name__)

from config.schema import SchemaProfile, ModuleRef
from config.operations import Change, OperationConfig
from config.selectors import PathSelector
from core.schema_context import SchemaContext, ContextRegistry
from core.schema_db import SchemaDB
from core.schema_catalog import ModelCatalog
from core.schema_resolver import SchemaResolver
from core.schema_validator import SchemaValidator
from core.schema_serializer import SchemaSerializer
from core.schema_errors import SchemaError, SchemaNotFoundError
from modules.stats_hooks import NOOP_STATS_RECORDER, StatsRecorder


class SchemaManager:
    """
    Singleton entry point for all YANG schema management and operations.
    Thread-safe initialization via threading.RLock.
    """
    _instance: Optional[SchemaManager] = None
    _lock = threading.RLock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, global_cache_dir: Optional[Union[str, Path]] = None):
        with self._lock:
            if getattr(self, "_initialized", False):
                if global_cache_dir is not None:
                    target_dir = Path(global_cache_dir).expanduser().resolve()
                    # targeted directory has changed: reinitialize SchemaManager class
                    if self.db.cache_dir != target_dir:
                        self.initialize(global_cache_dir)
                return
            self.db = SchemaDB(cache_dir=global_cache_dir)
            self.registry = ContextRegistry()
            self.catalog = ModelCatalog()
            self.resolver = SchemaResolver(schema_db=self.db)
            self.validator = SchemaValidator()
            self.serializer = SchemaSerializer()
            self._stats_recorder: StatsRecorder = NOOP_STATS_RECORDER
            self._initialized = True

    def set_stats_recorder(self, stats_recorder: Optional[StatsRecorder]) -> None:
        """Sets the runtime StatsRecorder across SchemaManager and its child subsystems."""
        with self._lock:
            rec = stats_recorder or NOOP_STATS_RECORDER
            self._stats_recorder = rec
            if hasattr(self, "db") and hasattr(self.db, "set_stats_recorder"):
                self.db.set_stats_recorder(rec)
            if hasattr(self, "registry") and hasattr(self.registry, "set_stats_recorder"):
                self.registry.set_stats_recorder(rec)
            if hasattr(self, "validator") and hasattr(self.validator, "set_stats_recorder"):
                self.validator.set_stats_recorder(rec)

    @classmethod
    def reset_instance(cls) -> None:
        """Resets the singleton for clean test isolation."""
        with cls._lock:
            if cls._instance is not None:
                try:
                    cls._instance.set_stats_recorder(NOOP_STATS_RECORDER)
                except Exception:
                    pass
                try:
                    cls._instance.db.close()
                except Exception:
                    pass
                cls._instance.registry.clear()
            cls._instance = None

    def initialize(self, global_cache_dir: Optional[Union[str, Path]] = None) -> None:
        """Initializes or safely reconfigures global cache directory and database."""
        with self._lock:
            if global_cache_dir is not None:
                new_cache = Path(global_cache_dir).expanduser().resolve()
                if not getattr(self, "_initialized", False) or self.db.cache_dir != new_cache:
                    if hasattr(self, "db"):
                        try:
                            self.db.close()
                        except Exception:
                            pass
                    if hasattr(self, "registry"):
                        self.registry.clear()
                    else:
                        self.registry = ContextRegistry()
                    self.db = SchemaDB(cache_dir=new_cache)
                    self.resolver = SchemaResolver(schema_db=self.db)
                    if not hasattr(self, "catalog"):
                        self.catalog = ModelCatalog()
                    if not hasattr(self, "validator"):
                        self.validator = SchemaValidator()
                    if not hasattr(self, "serializer"):
                        self.serializer = SchemaSerializer()
                    self._initialized = True

    def get_or_create_context(
        self,
        profile: Union[str, SchemaProfile],
        search_dirs: Sequence[Union[str, Path]] = (),
        strict: bool = False,
    ) -> SchemaContext:
        """
        Retrieves or instantiates an isolated SchemaContext for the target profile.
        When strict=True, missing profile modules will raise SchemaNotFoundError.
        """
        profile_id = profile if isinstance(profile, str) else profile.profile_id
        profile_obj = profile if isinstance(profile, SchemaProfile) else None
        with self._lock:
            sctx = self.registry.get_or_create_context(
                profile_id=profile_id,
                search_dirs=search_dirs,
                profile=profile_obj,
            )
            # If profile object passed, persist it in DB and ensure its modules
            if profile_obj is not None:
                self.db.save_profile(profile_obj)
                if profile_obj.modules:
                    try:
                        self.resolver.ensure_profile(
                            context=sctx,
                            profile=profile_obj,
                            search_dirs=search_dirs,
                        )
                    except SchemaNotFoundError as err:
                        if strict:
                            raise
                        logger.warning(
                            f"[SchemaManager] Schema module not found during profile loading: {err}"
                        )
            return sctx

    def load_module_file(
        self,
        file_path: Union[str, Path],
        profile_id: str,
        search_dirs: Sequence[Union[str, Path]] = (),
    ) -> None:
        """
        Loads a single YANG file into the target profile context after resolving dependencies.
        """
        with self._lock:
            sctx = self.registry.get_or_create_context(profile_id=profile_id, search_dirs=search_dirs)
            p = Path(file_path).expanduser().resolve()
            if not p.is_file():
                raise SchemaNotFoundError(f"File not found: '{p}'")

            # Store in DB cache and index
            content = p.read_text(encoding="utf-8")
            mod_name = p.stem.split("@")[0]
            self.db.store_module_content(
                name=mod_name,
                content=content,
                source="local_dir",
                verify_with_libyang=False,
            )

            # Ensure dependencies and parse into context
            self.resolver.ensure_module(
                context=sctx,
                module_name=mod_name,
                search_dirs=list(search_dirs) + [p.parent],
            )

    def load_module_str(
        self,
        module_content: str,
        profile_id: str,
        name: Optional[str] = None,
        revision: Optional[str] = None,
        search_dirs: Sequence[Union[str, Path]] = (),
    ) -> None:
        """
        Parses and registers an in-memory YANG module string into the target profile context.
        """
        with self._lock:
            sctx = self.registry.get_or_create_context(profile_id=profile_id, search_dirs=search_dirs)
            if name:
                self.db.store_module_content(
                    name=name,
                    content=module_content,
                    revision=revision,
                    source="in_memory",
                    verify_with_libyang=False,
                )
                self.resolver.ensure_module(
                    context=sctx,
                    module_name=name,
                    revision=revision,
                    search_dirs=search_dirs,
                )
            else:
                sctx.parse_module_str(module_content)

    def resolve_and_load(
        self,
        module_name: str,
        revision: Optional[str],
        profile_id: str,
        search_dirs: Sequence[Union[str, Path]] = (),
        fetch_callback: Optional[Callable[[str, Optional[str]], Optional[str]]] = None,
    ) -> None:
        """
        Resolves module and all transitive dependencies, downloading via fetch_callback if missing.
        """
        with self._lock:
            sctx = self.registry.get_or_create_context(profile_id=profile_id, search_dirs=search_dirs)
            self.resolver.ensure_module(
                context=sctx,
                module_name=module_name,
                revision=revision,
                search_dirs=search_dirs,
                fetch_callback=fetch_callback,
            )

    def validate_path(
        self,
        path: Union[str, PathSelector],
        profile_id: str,
        operation: Optional[OperationConfig] = None,
        stats_recorder: Optional[StatsRecorder] = None,
    ) -> Any:
        """
        Validates an XPath path against the compiled context for profile_id.
        """
        sctx = self.registry.get_context(profile_id)
        if sctx is None:
            raise SchemaError(f"No SchemaContext initialized for profile '{profile_id}'")
        return self.validator.validate_path(
            path=path,
            schema_context=sctx,
            operation=operation,
            stats_recorder=stats_recorder,
        )

    def validate_payload(
        self,
        path: Union[str, PathSelector],
        value: Any,
        profile_id: str,
        operation: Optional[OperationConfig] = None,
        stats_recorder: Optional[StatsRecorder] = None,
    ) -> None:
        """
        Validates value constraints against compiled schema at path.
        """
        sctx = self.registry.get_context(profile_id)
        if sctx is None:
            raise SchemaError(f"No SchemaContext initialized for profile '{profile_id}'")
        self.validator.validate_payload(
            path=path,
            value=value,
            schema_context=sctx,
            operation=operation,
            stats_recorder=stats_recorder,
        )

    def validate_and_serialize_netconf_edit(
        self,
        changes: Sequence[Change],
        profile_id: str,
        default_operation: str = "merge",
    ) -> str:
        """
        Validates changes against schema and serializes to namespaced XML for NETCONF <edit-config>.
        """
        sctx = self.registry.get_context(profile_id)
        if sctx is None:
            raise SchemaError(f"No SchemaContext initialized for profile '{profile_id}'")

        # Validate each change path and payload
        for ch in changes:
            self.validator.validate_path(path=ch.path, schema_context=sctx)
            if ch.value is not None:
                self.validator.validate_payload(path=ch.path, value=ch.value, schema_context=sctx)

        return self.serializer.serialize_netconf_edit_config(
            schema_context=sctx,
            changes=changes,
            default_operation=default_operation,
        )

    def validate_and_serialize_gnmi_change(
        self,
        change: Change,
        profile_id: str,
    ) -> str:
        """
        Validates a Change against schema and serializes to RFC 7951 JSON for gNMI.
        """
        sctx = self.registry.get_context(profile_id)
        if sctx is None:
            raise SchemaError(f"No SchemaContext initialized for profile '{profile_id}'")

        self.validator.validate_path(path=change.path, schema_context=sctx)
        if change.value is not None:
            self.validator.validate_payload(path=change.path, value=change.value, schema_context=sctx)

        return self.serializer.serialize_gnmi_update(
            schema_context=sctx,
            change=change,
        )