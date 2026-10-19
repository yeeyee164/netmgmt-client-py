# -*- encoding: utf-8 -*-
"""
test_schema_db_setup.py

Validates that SchemaDB provides meaningful, structured, and resilient metadata
indexing, profile isolation, and offline caching for YANG data models.
"""
from __future__ import annotations
import os
import sys
import threading
import sqlite3
import pytest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.schema_db import SchemaDB
from config.schema import SchemaProfile, ModuleRef, DeviationRef


SAMPLE_SRLINUX_YANG = """
module srl-interfaces {
  yang-version 1.1;
  namespace "urn:nokia:srl:interfaces";
  prefix srl-if;
  revision 2024-01-01 {
    description "SRLinux interfaces";
  }
  feature subinterfaces {
    description "Subinterface support";
  }
  container interface {
    leaf name { type string; }
  }
}
"""

SAMPLE_OCNOS_YANG = """
module ipi-telemetry {
  yang-version 1.1;
  namespace "http://www.ipinfusion.com/yang/ocnos/telemetry";
  prefix ipi-tel;
  revision 2025-08-26 {
    description "OcNOS streaming telemetry";
  }
  feature grpc-transport {
    description "gRPC transport support";
  }
  container telemetry {
    leaf enabled { type boolean; }
  }
}
"""


def test_schema_db_table_architecture(tmp_path):
    """Verifies that all 7 required schema tables are initialized with foreign key constraints."""
    db_file = tmp_path / "architecture.db"
    db = SchemaDB(db_path=db_file, cache_dir=tmp_path / "cache")

    cur = db._conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row["name"] for row in cur.fetchall()]

    expected_tables = {
        "device_bindings",
        "profile_datastores",
        "profile_modules",
        "schema_deviations",
        "schema_features",
        "schema_modules",
        "schema_profiles",
    }
    for expected in expected_tables:
        assert expected in tables, f"Missing expected table {expected} in SchemaDB"

    cur.execute("PRAGMA foreign_keys")
    fk_enabled = cur.fetchone()[0]
    assert fk_enabled == 1, "Foreign key enforcement must be active"

    db.close()


def test_schema_db_module_deduplication_and_hashing(tmp_path):
    """Verifies SHA256 computation and deduplication when storing identical or updated content."""
    db = SchemaDB(cache_dir=tmp_path / "cache")

    # Store first time
    path1 = db.store_module_content(
        name="srl-interfaces",
        content=SAMPLE_SRLINUX_YANG,
        revision="2024-01-01",
        namespace="urn:nokia:srl:interfaces",
        features=["subinterfaces"],
        verify_with_libyang=True,
    )
    assert path1.is_file()

    # Store exact same content again (idempotent)
    path2 = db.store_module_content(
        name="srl-interfaces",
        content=SAMPLE_SRLINUX_YANG,
        revision="2024-01-01",
        namespace="urn:nokia:srl:interfaces",
        features=["subinterfaces"],
        verify_with_libyang=True,
    )
    assert path1 == path2

    # Verify single row in schema_modules
    cur = db._conn.cursor()
    cur.execute("SELECT COUNT(*) FROM schema_modules WHERE name='srl-interfaces'")
    assert cur.fetchone()[0] == 1

    # Verify features recorded
    features = db.get_module_features("srl-interfaces", "2024-01-01")
    assert "subinterfaces" in features

    db.close()


def test_schema_db_vendor_profile_isolation(tmp_path):
    """Verifies that SRLinux and OcNOS profiles maintain strict module isolation without cross-contamination."""
    db = SchemaDB(cache_dir=tmp_path / "cache")

    # Store both vendor modules
    db.store_module_content(
        name="srl-interfaces",
        content=SAMPLE_SRLINUX_YANG,
        revision="2024-01-01",
        verify_with_libyang=True,
    )
    db.store_module_content(
        name="ipi-telemetry",
        content=SAMPLE_OCNOS_YANG,
        revision="2025-08-26",
        verify_with_libyang=True,
    )

    # Create distinct profiles
    p_srl = SchemaProfile(
        content_id="srlinux",
        modules=(ModuleRef(name="srl-interfaces", revision="2024-01-01"),),
        datastore_schemas=("running", "candidate"),
    )
    p_ocnos = SchemaProfile(
        content_id="ocnos_derived",
        modules=(ModuleRef(name="ipi-telemetry", revision="2025-08-26"),),
        datastore_schemas=("running",),
    )

    srl_id = db.save_profile(p_srl)
    ocnos_id = db.save_profile(p_ocnos)

    # Verify SRLinux profile contains ONLY srl-interfaces
    srl_mods = db.get_profile_modules(srl_id)
    assert len(srl_mods) == 1
    assert srl_mods[0]["name"] == "srl-interfaces"

    # Verify OcNOS profile contains ONLY ipi-telemetry
    ocnos_mods = db.get_profile_modules(ocnos_id)
    assert len(ocnos_mods) == 1
    assert ocnos_mods[0]["name"] == "ipi-telemetry"

    # Verify datastores isolated
    cur = db._conn.cursor()
    cur.execute("SELECT datastore_name FROM profile_datastores WHERE profile_id=? ORDER BY datastore_name", (srl_id,))
    assert [r[0] for r in cur.fetchall()] == ["candidate", "running"]

    cur.execute("SELECT datastore_name FROM profile_datastores WHERE profile_id=?", (ocnos_id,))
    assert [r[0] for r in cur.fetchall()] == ["running"]

    db.close()


def test_schema_db_device_fingerprint_bindings(tmp_path):
    """Verifies binding targets to profiles and fast offline fingerprint lookup."""
    db = SchemaDB(cache_dir=tmp_path / "cache")

    p1 = SchemaProfile(content_id="srlinux_profile")
    p2 = SchemaProfile(content_id="ocnos_profile")
    id1 = db.save_profile(p1)
    id2 = db.save_profile(p2)

    db.bind_device("172.20.20.2:830", protocol="netconf", profile_id=id1)
    db.bind_device("172.20.20.2:57401", protocol="gnmi", profile_id=id1)
    db.bind_device("10.1.11.101:830", protocol="netconf", profile_id=id2)
    db.bind_device("10.1.11.101:9339", protocol="gnmi", profile_id=id2)

    # Instant lookup verification
    assert db.get_device_profile_id("172.20.20.2:830") == id1
    assert db.get_device_profile_id("172.20.20.2:57401") == id1
    assert db.get_device_profile_id("10.1.11.101:830") == id2
    assert db.get_device_profile_id("10.1.11.101:9339") == id2
    assert db.get_device_profile_id("unknown:830") is None

    stats = db.get_database_statistics()
    assert stats["device_bindings"] == 4
    assert stats["schema_profiles"] == 2

    db.close()


def test_schema_db_corruption_self_healing(tmp_path):
    """Verifies that tampered or corrupted files are caught and cleaned up on lookup."""
    db = SchemaDB(cache_dir=tmp_path / "cache")

    path = db.store_module_content(
        name="srl-interfaces",
        content=SAMPLE_SRLINUX_YANG,
        revision="2024-01-01",
        verify_with_libyang=True,
    )
    assert path.is_file()

    # Tamper with file on disk
    path.write_text("corrupted content without valid sha", encoding="utf-8")

    # Lookup should detect corruption, remove file, delete row, and return None
    found = db.find_module("srl-interfaces", revision="2024-01-01")
    assert found is None
    assert not path.exists()

    cur = db._conn.cursor()
    cur.execute("SELECT COUNT(*) FROM schema_modules WHERE name='srl-interfaces'")
    assert cur.fetchone()[0] == 0

    db.close()


def test_schema_db_persistence_across_process_restarts(tmp_path):
    """Simulates closing and reopening SchemaDB to prove true database persistence."""
    db_file = tmp_path / "persistent.db"
    cache_dir = tmp_path / "cache"

    db1 = SchemaDB(db_path=db_file, cache_dir=cache_dir)
    p = db1.store_module_content(
        name="srl-interfaces",
        content=SAMPLE_SRLINUX_YANG,
        revision="2024-01-01",
        features=["subinterfaces"],
        verify_with_libyang=True,
    )
    profile = SchemaProfile(content_id="srl_prod", modules=(ModuleRef(name="srl-interfaces"),))
    p_id = db1.save_profile(profile)
    db1.bind_device("172.20.20.2:830", protocol="netconf", profile_id=p_id)
    db1.close()

    # Re-open with separate instance
    db2 = SchemaDB(db_path=db_file, cache_dir=cache_dir)
    assert db2.find_module("srl-interfaces", revision="2024-01-01") == p
    assert db2.get_device_profile_id("172.20.20.2:830") == p_id
    features = db2.get_module_features("srl-interfaces")
    assert "subinterfaces" in features

    mods = db2.list_modules()
    assert len(mods) == 1
    assert mods[0]["name"] == "srl-interfaces"

    db2.close()


def test_schema_db_multithreaded_concurrency(tmp_path):
    """Verifies that thread-safe locking prevents SQLite race conditions or lock contention."""
    db = SchemaDB(cache_dir=tmp_path / "cache")
    errors = []

    def worker(worker_id: int):
        try:
            content = f"""
            module worker-mod-{worker_id} {{
              yang-version 1.1;
              namespace "urn:worker:{worker_id}";
              prefix "wm{worker_id}";
              container data {{ leaf id {{ type int32; }} }}
            }}
            """
            p = db.store_module_content(
                name=f"worker-mod-{worker_id}",
                content=content,
                verify_with_libyang=False,
            )
            assert p.is_file()
            prof = SchemaProfile(
                content_id=f"prof_{worker_id}",
                modules=(ModuleRef(name=f"worker-mod-{worker_id}"),),
            )
            p_id = db.save_profile(prof)
            db.bind_device(f"192.168.1.{worker_id}:830", protocol="netconf", profile_id=p_id)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Thread errors encountered: {errors}"
    stats = db.get_database_statistics()
    assert stats["schema_modules"] == 10
    assert stats["schema_profiles"] == 10
    assert stats["device_bindings"] == 10

    db.close()