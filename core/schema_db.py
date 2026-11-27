# -*- encoding: utf-8 -*-
"""
schema_db.py

SQLite-backed metadata index and atomic filesystem cache for compiled and downloaded
YANG modules, submodules, features, deviations, and schema profiles.
"""
from __future__ import annotations
import os
import re
import sqlite3
import hashlib
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Union, Sequence

try:
    import libyang
except ImportError:
    libyang = None

from config.schema import SchemaProfile, ModuleRef, SubmoduleRef, FeatureRef, DeviationRef
from core.schema_errors import SchemaCacheError
from modules.stats_hooks import NOOP_STATS_RECORDER, StatsRecorder, safe_record


class SchemaDB:
    """
    Manages SQLite metadata storage and atomic filesystem writes for YANG models.
    Thread-safe connection handling using threading.RLock.
    """

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        cache_dir: Optional[Union[str, Path]] = None,
        stats_recorder: Optional[StatsRecorder] = None,
    ):
        self._stats_recorder: StatsRecorder = stats_recorder or NOOP_STATS_RECORDER
        if cache_dir is None:
            self.cache_dir = Path.home() / ".cache" / "gnmi_client_py"
        else:
            self.cache_dir = Path(cache_dir).expanduser().resolve()

        self.models_dir = self.cache_dir / "models"
        self.models_dir.mkdir(parents=True, exist_ok=True)

        if db_path is None:
            self.db_path = self.cache_dir / "schema_cache.db"
        else:
            self.db_path = Path(db_path).expanduser().resolve()
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self) -> None:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("PRAGMA busy_timeout = 5000;")
            cursor.execute("PRAGMA journal_mode = WAL;")
            cursor.execute("PRAGMA foreign_keys = ON;")
            cursor.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_modules (
                    module_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    revision TEXT,
                    namespace TEXT,
                    kind TEXT NOT NULL,              -- module | submodule
                    sha256 TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    source TEXT NOT NULL,            -- bundled | local_dir | netconf_get_schema | other
                    source_uri TEXT,
                    created_at TIMESTAMP NOT NULL,
                    UNIQUE(name, revision, sha256)
                );

                CREATE TABLE IF NOT EXISTS schema_features (
                    module_id INTEGER NOT NULL,
                    feature_name TEXT NOT NULL,
                    PRIMARY KEY(module_id, feature_name),
                    FOREIGN KEY(module_id) REFERENCES schema_modules(module_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS schema_deviations (
                    profile_id TEXT NOT NULL,
                    module_name TEXT NOT NULL,
                    revision TEXT,
                    PRIMARY KEY(profile_id, module_name, revision)
                );

                CREATE TABLE IF NOT EXISTS schema_profiles (
                    profile_id TEXT PRIMARY KEY,
                    content_id TEXT,
                    canonical_json TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL
                );

                CREATE TABLE IF NOT EXISTS profile_modules (
                    profile_id TEXT NOT NULL,
                    module_id INTEGER NOT NULL,
                    PRIMARY KEY(profile_id, module_id),
                    FOREIGN KEY(profile_id) REFERENCES schema_profiles(profile_id) ON DELETE CASCADE,
                    FOREIGN KEY(module_id) REFERENCES schema_modules(module_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS profile_datastores (
                    profile_id TEXT NOT NULL,
                    datastore_name TEXT NOT NULL,
                    schema_name TEXT,
                    PRIMARY KEY(profile_id, datastore_name),
                    FOREIGN KEY(profile_id) REFERENCES schema_profiles(profile_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS device_bindings (
                    device_key TEXT PRIMARY KEY,
                    protocol TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    last_seen TIMESTAMP NOT NULL,
                    FOREIGN KEY(profile_id) REFERENCES schema_profiles(profile_id) ON DELETE CASCADE
                );
                """
            )
            self._conn.commit()

    @staticmethod
    def calculate_sha256(content: Union[str, bytes]) -> str:
        data = content.encode("utf-8") if isinstance(content, str) else content
        return hashlib.sha256(data).hexdigest()

    def store_module_content(
        self,
        name: str,
        content: str,
        revision: Optional[str] = None,
        namespace: Optional[str] = None,
        kind: str = "module",
        source: str = "netconf_get_schema",
        source_uri: Optional[str] = None,
        features: Sequence[str] = (),
        verify_with_libyang: bool = True,
    ) -> Path:
        """
        Atomically saves module content to the cache directory and indexes it in SQLite.
        Follows Section 12.2 atomic caching rules described in `yang_schema_architecture.md`.
        """
        sha = self.calculate_sha256(content)
        rev_str = revision.strip() if revision else ""
        file_suffix = f"@{rev_str}.yang" if rev_str else ".yang"
        standard_filename = f"{name}{file_suffix}"
        standard_path = self.models_dir / standard_filename

        # If standard file already exists on disk with a DIFFERENT sha, disambiguate with sha prefix
        if standard_path.exists():
            try:
                with open(standard_path, "rb") as f:
                    existing_sha = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                existing_sha = None
            if existing_sha and existing_sha != sha:
                # Content collision for same module name/revision: use content-addressed filename
                target_filename = f"{name}@{rev_str}_{sha[:8]}.yang" if rev_str else f"{name}_{sha[:8]}.yang"
                target_path = self.models_dir / target_filename
            else:
                target_filename = standard_filename
                target_path = standard_path
        else:
            target_filename = standard_filename
            target_path = standard_path

        is_submodule = bool(re.search(r'^\s*submodule\s+', content, re.MULTILINE))
        if is_submodule:
            kind = "submodule"
            verify_with_libyang = False

        # If file already exists with exact same checksum, avoid redundant re-write
        if target_path.exists():
            try:
                with open(target_path, "rb") as f:
                    if hashlib.sha256(f.read()).hexdigest() == sha:
                        # Existing file is intact; update DB record and return
                        with self._lock:
                            now_str = datetime.now(timezone.utc).isoformat()
                            cursor = self._conn.cursor()
                            cursor.execute(
                                """
                                INSERT INTO schema_modules (
                                    name, revision, namespace, kind, sha256, file_path, source, source_uri, created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(name, revision, sha256) DO UPDATE SET
                                    file_path = excluded.file_path,
                                    namespace = coalesce(excluded.namespace, schema_modules.namespace),
                                    source = excluded.source,
                                    source_uri = excluded.source_uri
                                """,
                                (
                                    name,
                                    rev_str or None,
                                    namespace,
                                    kind,
                                    sha,
                                    str(target_path),
                                    source,
                                    source_uri,
                                    now_str,
                                ),
                            )
                            self._conn.commit()
                            return target_path
            except OSError:
                pass

        # Step 1: Write to temporary file
        temp_file = None
        try:
            fd, temp_path = tempfile.mkstemp(
                prefix=f"{name}_",
                suffix=".tmp",
                dir=str(self.models_dir),
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)

            # Step 2 & 3: Validate module parsing via libyang if requested
            if verify_with_libyang and libyang is not None:
                try:
                    ctx = libyang.Context(search_path=str(self.models_dir))
                    with open(temp_path, "r", encoding="utf-8") as f:
                        ctx.parse_module_file(f)
                except Exception as exc:
                    raise SchemaCacheError(
                        f"Failed libyang verification for module '{name}' (rev: {revision}): {exc}"
                    ) from exc

            # Step 4: Atomic file rename
            os.replace(temp_path, str(target_path))
            temp_path = None

            # Step 5: Transactional SQLite metadata update
            with self._lock:
                now_str = datetime.now(timezone.utc).isoformat()
                cursor = self._conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO schema_modules (
                        name, revision, namespace, kind, sha256, file_path, source, source_uri, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name, revision, sha256) DO UPDATE SET
                        file_path = excluded.file_path,
                        namespace = coalesce(excluded.namespace, schema_modules.namespace),
                        source = excluded.source,
                        source_uri = excluded.source_uri
                    """,
                    (
                        name,
                        rev_str or None,
                        namespace,
                        kind,
                        sha,
                        str(target_path),
                        source,
                        source_uri,
                        now_str,
                    ),
                )
                mod_id = cursor.lastrowid
                if not mod_id:
                    cursor.execute(
                        "SELECT module_id FROM schema_modules WHERE name=? AND revision IS ? AND sha256=?",
                        (name, rev_str or None, sha),
                    )
                    row = cursor.fetchone()
                    mod_id = row["module_id"] if row else None

                if mod_id and features:
                    for feat in features:
                        cursor.execute(
                            """
                            INSERT OR IGNORE INTO schema_features (module_id, feature_name)
                            VALUES (?, ?)
                            """,
                            (mod_id, feat),
                        )

                self._conn.commit()
                return target_path

        except Exception as e:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            if not isinstance(e, SchemaCacheError):
                raise SchemaCacheError(f"Error caching schema module '{name}': {e}") from e
            raise

    def set_stats_recorder(self, stats_recorder: Optional[StatsRecorder]) -> None:
        with self._lock:
            self._stats_recorder = stats_recorder or NOOP_STATS_RECORDER

    def find_module(
        self,
        name: str,
        revision: Optional[str] = None,
        sha256: Optional[str] = None,
    ) -> Optional[Path]:
        """
        Locates a module in SQLite and ensures the file exists on disk and is non-corrupt.
        Adheres to Section 12.3 priority:
          1. exact name + exact revision + exact checksum
          2. exact name + exact revision
          3. name-only lookup (strictly when revision is None)
        """
        with self._lock:
            cursor = self._conn.cursor()

            # Priority 1: Exact name + revision + checksum
            if revision is not None and sha256 is not None:
                cursor.execute(
                    "SELECT file_path, sha256 FROM schema_modules WHERE name=? AND revision=? AND sha256=?",
                    (name, revision, sha256),
                )
                row = cursor.fetchone()
                if row and self._verify_and_return_path(row["file_path"], row["sha256"]):
                    safe_record(self._stats_recorder.record_schema_cache_access, True, "disk_db")
                    return Path(row["file_path"])

            # Priority 2: Exact name + exact revision
            if revision is not None:
                cursor.execute(
                    "SELECT file_path, sha256 FROM schema_modules WHERE name=? AND revision=?",
                    (name, revision),
                )
                row = cursor.fetchone()
                if row and self._verify_and_return_path(row["file_path"], row["sha256"]):
                    safe_record(self._stats_recorder.record_schema_cache_access, True, "disk_db")
                    return Path(row["file_path"])
                safe_record(self._stats_recorder.record_schema_cache_access, False, "local_dir")
                return None

            # Priority 3: Name-only lookup (when revision is None)
            cursor.execute(
                "SELECT file_path, sha256 FROM schema_modules WHERE name=? ORDER BY revision DESC",
                (name,),
            )
            rows = cursor.fetchall()
            for r in rows:
                p = self._verify_and_return_path(r["file_path"], r["sha256"])
                if p:
                    safe_record(self._stats_recorder.record_schema_cache_access, True, "disk_db")
                    return Path(p)

            # Fallback: Check models directory for RFC naming conventions
            candidates = list(self.models_dir.glob(f"{name}@*.yang"))
            if revision:
                expected_p = self.models_dir / f"{name}@{revision}.yang"
                if expected_p.exists():
                    safe_record(self._stats_recorder.record_schema_cache_access, True, "disk_db")
                    return expected_p
            elif candidates:
                safe_record(self._stats_recorder.record_schema_cache_access, True, "disk_db")
                return sorted(candidates, reverse=True)[0]
            name_p = self.models_dir / f"{name}.yang"
            if name_p.exists() and revision is None:
                safe_record(self._stats_recorder.record_schema_cache_access, True, "disk_db")
                return name_p

            safe_record(self._stats_recorder.record_schema_cache_access, False, "local_dir")
            return None

    def _verify_and_return_path(self, file_path_str: str, expected_sha: str) -> Optional[str]:
        p = Path(file_path_str)
        if not p.is_file():
            return None
        try:
            with open(p, "rb") as f:
                actual_sha = hashlib.sha256(f.read()).hexdigest()
            if actual_sha == expected_sha:
                return file_path_str
            else:
                # File corrupted on disk; delete corrupt file and drop row
                p.unlink(missing_ok=True)
                cursor = self._conn.cursor()
                cursor.execute("DELETE FROM schema_modules WHERE file_path=?", (file_path_str,))
                self._conn.commit()
                return None
        except OSError:
            return None

    def save_profile(self, profile: SchemaProfile) -> str:
        """Persists a SchemaProfile and establishes associations with known modules."""
        with self._lock:
            now_str = datetime.now(timezone.utc).isoformat()
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO schema_profiles (profile_id, content_id, canonical_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    content_id = coalesce(excluded.content_id, schema_profiles.content_id),
                    updated_at = excluded.updated_at
                """,
                (
                    profile.profile_id,
                    profile.content_id,
                    profile.to_canonical_json(),
                    now_str,
                    now_str,
                ),
            )

            # Record datastores
            for ds in profile.datastore_schemas:
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO profile_datastores (profile_id, datastore_name)
                    VALUES (?, ?)
                    """,
                    (profile.profile_id, ds),
                )

            # Record deviations
            for dev in profile.deviations:
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO schema_deviations (profile_id, module_name, revision)
                    VALUES (?, ?, ?)
                    """,
                    (profile.profile_id, dev.name, dev.revision),
                )

            # Link modules if already in database
            for m in profile.modules:
                cursor.execute(
                    "SELECT module_id FROM schema_modules WHERE name=? AND revision IS ?",
                    (m.name, m.revision or None),
                )
                row = cursor.fetchone()
                if row:
                    cursor.execute(
                        """
                        INSERT OR IGNORE INTO profile_modules (profile_id, module_id)
                        VALUES (?, ?)
                        """,
                        (profile.profile_id, row["module_id"]),
                    )

            self._conn.commit()
            return profile.profile_id

    def get_profile_json(self, profile_id: str) -> Optional[str]:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT canonical_json FROM schema_profiles WHERE profile_id=?",
                (profile_id,),
            )
            row = cursor.fetchone()
            return row["canonical_json"] if row else None

    def bind_device(self, device_key: str, protocol: str, profile_id: str) -> None:
        """Binds a device target string (e.g. host:port) to an effective profile_id."""
        with self._lock:
            now_str = datetime.now(timezone.utc).isoformat()
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO device_bindings (device_key, protocol, profile_id, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(device_key) DO UPDATE SET
                    protocol = excluded.protocol,
                    profile_id = excluded.profile_id,
                    last_seen = excluded.last_seen
                """,
                (device_key, protocol, profile_id, now_str),
            )
            self._conn.commit()

    def get_device_profile_id(self, device_key: str) -> Optional[str]:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT profile_id FROM device_bindings WHERE device_key=?",
                (device_key,),
            )
            row = cursor.fetchone()
            return row["profile_id"] if row else None

    def get_profile_modules(self, profile_id: str) -> List[Dict[str, Any]]:
        """Returns metadata for all modules linked to a specific profile_id."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT sm.module_id, sm.name, sm.revision, sm.namespace, sm.kind,
                       sm.sha256, sm.file_path, sm.source, sm.created_at
                FROM profile_modules pm
                JOIN schema_modules sm ON pm.module_id = sm.module_id
                WHERE pm.profile_id = ?
                ORDER BY sm.name ASC
                """,
                (profile_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_module_features(self, module_name: str, revision: Optional[str] = None) -> List[str]:
        """Returns all features associated with a module name and optional revision."""
        with self._lock:
            cursor = self._conn.cursor()
            if revision is not None:
                cursor.execute(
                    """
                    SELECT sf.feature_name
                    FROM schema_features sf
                    JOIN schema_modules sm ON sf.module_id = sm.module_id
                    WHERE sm.name = ? AND sm.revision = ?
                    ORDER BY sf.feature_name ASC
                    """,
                    (module_name, revision),
                )
            else:
                cursor.execute(
                    """
                    SELECT sf.feature_name
                    FROM schema_features sf
                    JOIN schema_modules sm ON sf.module_id = sm.module_id
                    WHERE sm.name = ?
                    ORDER BY sf.feature_name ASC
                    """,
                    (module_name,),
                )
            return [row["feature_name"] for row in cursor.fetchall()]

    def get_device_binding_info(self, device_key: str) -> Optional[Dict[str, Any]]:
        """Returns full device binding information for a device key."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT device_key, protocol, profile_id, last_seen FROM device_bindings WHERE device_key=?",
                (device_key,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_database_statistics(self) -> Dict[str, int]:
        """Returns aggregate row counts across all SchemaDB SQLite tables."""
        stats = {}
        tables = [
            "schema_modules",
            "schema_features",
            "schema_deviations",
            "schema_profiles",
            "profile_modules",
            "profile_datastores",
            "device_bindings",
        ]
        with self._lock:
            cursor = self._conn.cursor()
            for tbl in tables:
                cursor.execute(f"SELECT COUNT(*) FROM {tbl}")
                stats[tbl] = cursor.fetchone()[0]
        return stats

    def list_modules(self) -> List[Dict[str, Any]]:
        """Returns metadata for all cached modules in SQLite."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT module_id, name, revision, namespace, kind, sha256, file_path, source, created_at
                FROM schema_modules
                ORDER BY name ASC, revision DESC
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
