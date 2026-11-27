# -*- encoding: utf-8 -*-
"""
schema_context.py

Encapsulates libyang Context lifecycle, isolating compiled YANG contexts
per schema profile to avoid vendor revision collisions.
"""
from __future__ import annotations
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence, Union, Dict, Any, List, Iterator, Tuple

try:
    import libyang
except ImportError:
    libyang = None

from core.schema_errors import SchemaError, SchemaProfileConflictError
from modules.stats_hooks import NOOP_STATS_RECORDER, StatsRecorder, safe_record


@dataclass
class SchemaContext:
    """
    Wrapper holding an isolated libyang.Context compiled for a specific SchemaProfile.
    """
    profile_id: str
    yang: Any  # libyang.Context
    search_dirs: Tuple[Path, ...] = ()
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def parse_module_file(self, file_path: Union[str, Path]) -> Any:
        with self._lock:
            p = Path(file_path).expanduser().resolve()
            with open(p, "r", encoding="utf-8") as f:
                return self.yang.parse_module_file(f)

    def parse_module_str(self, module_content: str) -> Any:
        with self._lock:
            return self.yang.parse_module_str(module_content)

    def get_module(self, name: str, revision: Optional[str] = None) -> Optional[Any]:
        with self._lock:
            try:
                mod = self.yang.get_module(name)
                if mod is None:
                    return None
                if revision is not None:
                    rev_dates = [r.date() for r in mod.revisions()] if hasattr(mod, "revisions") else []
                    if revision not in rev_dates:
                        return None
                return mod
            except Exception:
                return None

    def has_module(self, name: str, revision: Optional[str] = None) -> bool:
        return self.get_module(name, revision=revision) is not None

    def find_path(self, xpath: str) -> Iterator[Any]:
        with self._lock:
            return self.yang.find_path(xpath)

    def create_data_path(self, xpath: str, value: Any = None) -> Any:
        with self._lock:
            return self.yang.create_data_path(xpath, value=value)

    def parse_data_mem(self, data: Union[str, bytes], fmt: str = "xml") -> Any:
        with self._lock:
            return self.yang.parse_data_mem(data, fmt=fmt)

    def add_search_dir(self, directory: Union[str, Path]) -> None:
        with self._lock:
            d_path = Path(directory).expanduser().resolve()
            if d_path not in self.search_dirs:
                self.search_dirs = self.search_dirs + (d_path,)
                if hasattr(self.yang, 'cdata') and self.yang.cdata is not None and libyang is not None:
                    try:
                        from _libyang import lib
                        from libyang.util import str2c
                        lib.ly_ctx_set_searchdir(self.yang.cdata, str2c(str(d_path)))
                    except Exception:
                        pass


class ContextRegistry:
    """
    Thread-safe registry managing isolated SchemaContext instances keyed by profile_id.
    """

    def __init__(self, stats_recorder: Optional[StatsRecorder] = None):
        self._lock = threading.RLock()
        self._contexts: Dict[str, SchemaContext] = {}
        self._profiles: Dict[str, Any] = {}
        self._stats_recorder: StatsRecorder = stats_recorder or NOOP_STATS_RECORDER

    def set_stats_recorder(self, stats_recorder: Optional[StatsRecorder]) -> None:
        with self._lock:
            self._stats_recorder = stats_recorder or NOOP_STATS_RECORDER

    def profile_count(self) -> int:
        with self._lock:
            return len(self._contexts)

    def get_or_create_context(
        self,
        profile_id: str,
        search_dirs: Sequence[Union[str, Path]] = (),
        profile: Optional[Any] = None,
    ) -> SchemaContext:
        """
        Retrieves an existing SchemaContext for profile_id, or initializes a new one.
        Rejects conflicting schema profile definitions for the same profile_id.
        """
        if libyang is None:
            raise SchemaError("libyang package is required for schema management but is not installed.")

        with self._lock:
            norm_dirs = tuple(Path(d).expanduser().resolve() for d in search_dirs)

            if profile is not None and hasattr(profile, "to_canonical_json"):
                if profile_id in self._profiles:
                    existing_p = self._profiles[profile_id]
                    if hasattr(existing_p, "to_canonical_json"):
                        if profile.to_canonical_json() != existing_p.to_canonical_json():
                            raise SchemaProfileConflictError(
                                f"Conflicting SchemaProfile definitions encountered for profile_id '{profile_id}'."
                            )
                else:
                    self._profiles[profile_id] = profile

            if profile_id in self._contexts:
                sctx = self._contexts[profile_id]
                for d in norm_dirs:
                    sctx.add_search_dir(d)
                safe_record(self._stats_recorder.record_schema_cache_access, True, "memory_context")
                return sctx

            # Instantiate a new isolated libyang.Context
            t0 = time.perf_counter()
            search_path_str = ":".join(str(d) for d in norm_dirs) if norm_dirs else None
            ctx = libyang.Context(search_path=search_path_str, disable_searchdir_cwd=True)

            sctx = SchemaContext(
                profile_id=profile_id,
                yang=ctx,
                search_dirs=norm_dirs,
            )
            self._contexts[profile_id] = sctx
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            safe_record(
                self._stats_recorder.record_schema_context_compiled,
                profile_id,
                elapsed_ms,
                len(self._contexts),
            )
            return sctx

    def get_context(self, profile_id: str) -> Optional[SchemaContext]:
        with self._lock:
            return self._contexts.get(profile_id)

    def has_context(self, profile_id: str) -> bool:
        with self._lock:
            return profile_id in self._contexts

    def clear(self) -> None:
        with self._lock:
            self._contexts.clear()
            self._profiles.clear()