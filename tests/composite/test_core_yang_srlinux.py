# -*- encoding: utf-8 -*-
"""
test_core_yang_srlinux.py

Tests loading, caching, and compiling YANG data models from Nokia SRLinux ("srlinux"):
- Address: 172.20.20.2:830 (NETCONF) or 57401 (gNMI)
- Models: openconfig-interfaces, openconfig-system
- Directory: tests/yang/srlinux / test/yang/srlinux
"""
from __future__ import annotations
import os
import sys
import pytest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.schema_db import SchemaDB
from core.schema_context import ContextRegistry
from core.schema_resolver import SchemaResolver
from core.schema_manager import SchemaManager
from config.schema import SchemaProfile, ModuleRef
from tests.yang_fetcher import YangSchemaFetcher


SRLINUX_TARGET = "172.20.20.2:830"
SRLINUX_GNMI_TARGET = "172.20.20.2:57401"
SRLINUX_USER = "admin"
SRLINUX_PASS = "NokiaSrl1!"
SRLINUX_MODELS = ["openconfig-interfaces", "openconfig-system"]

YANG_DIR = (Path(__file__).resolve().parent / "yang" / "srlinux") if (Path(__file__).resolve().parent / "yang" / "srlinux").exists() else (Path(__file__).resolve().parent.parent / "yang" / "srlinux")
ALT_YANG_DIR = (Path(__file__).resolve().parent.parent / "test" / "yang" / "srlinux") if (Path(__file__).resolve().parent.parent / "test" / "yang" / "srlinux").exists() else (Path(__file__).resolve().parent.parent.parent / "test" / "yang" / "srlinux")


@pytest.fixture(scope="module")
def srlinux_yang_dir():
    """Ensures tests/yang/srlinux directory exists and models are populated."""
    YANG_DIR.mkdir(parents=True, exist_ok=True)

    # Check if models exist, otherwise gather via <get-schema>
    missing = []
    for model in SRLINUX_MODELS:
        candidates = list(YANG_DIR.glob(f"{model}*.yang"))
        if not candidates:
            missing.append(model)

    if missing:
        try:
            fetcher = YangSchemaFetcher(
                target=SRLINUX_TARGET,
                username=SRLINUX_USER,
                password=SRLINUX_PASS,
                output_dir=YANG_DIR,
                timeout=15,
            )
            for m in missing:
                fetcher.fetch_and_save(m, recursive=True)
        except Exception as exc:
            pytest.skip(f"Live target {SRLINUX_TARGET} unreachable or get-schema failed: {exc}")

    return YANG_DIR


def test_srlinux_yang_files_exist(srlinux_yang_dir):
    """Verifies that openconfig-interfaces and openconfig-system exist in the vendor directory."""
    for model in SRLINUX_MODELS:
        matches = list(srlinux_yang_dir.glob(f"{model}*.yang"))
        assert len(matches) > 0, f"Expected {model} in {srlinux_yang_dir}"
        assert matches[0].stat().st_size > 0, f"YANG file {matches[0]} should not be empty"


def test_srlinux_symlink_directory_compatibility(srlinux_yang_dir):
    """Verifies that ./test/yang/srlinux points to the exact same vendor directory."""
    if ALT_YANG_DIR.exists():
        assert ALT_YANG_DIR.resolve() == srlinux_yang_dir.resolve()
        for model in SRLINUX_MODELS:
            matches = list(ALT_YANG_DIR.glob(f"{model}*.yang"))
            assert len(matches) > 0, f"Expected {model} via alt path {ALT_YANG_DIR}"


def test_srlinux_load_and_compile_context(tmp_path, srlinux_yang_dir):
    """Loads openconfig-interfaces and openconfig-system into an isolated SchemaContext."""
    cache_dir = tmp_path / "cache"
    db = SchemaDB(cache_dir=cache_dir)
    registry = ContextRegistry()
    sctx = registry.get_or_create_context(profile_id="srlinux", search_dirs=[srlinux_yang_dir, db.models_dir])

    resolver = SchemaResolver(schema_db=db)
    for model in SRLINUX_MODELS:
        resolver.ensure_module(
            context=sctx,
            module_name=model,
            search_dirs=[srlinux_yang_dir],
        )

    # Verify both modules compiled successfully in libyang
    assert sctx.has_module("openconfig-interfaces")
    assert sctx.has_module("openconfig-system")

    # Verify key imported dependencies are also loaded
    assert sctx.has_module("openconfig-yang-types")
    assert sctx.has_module("openconfig-extensions")
    db.close()


def test_srlinux_schema_db_metadata_indexing(tmp_path, srlinux_yang_dir):
    """Validates that SchemaDB stores modules, computes SHA256, and links them to profile."""
    db_path = tmp_path / "srlinux_test.db"
    db = SchemaDB(db_path=db_path, cache_dir=tmp_path / "cache")

    # Populate SchemaDB with models from srlinux_yang_dir
    for yang_file in srlinux_yang_dir.glob("*.yang"):
        content = yang_file.read_text(encoding="utf-8")
        mod_name = yang_file.name.split("@")[0].replace(".yang", "")
        db.store_module_content(
            name=mod_name,
            content=content,
            source="local_vendor_dir",
            source_uri=str(yang_file),
            verify_with_libyang=False,
        )

    # Verify find_module succeeds
    oc_if_path = db.find_module("openconfig-interfaces")
    assert oc_if_path is not None
    assert oc_if_path.is_file()

    # Save profile and link modules
    profile = SchemaProfile(
        content_id="srlinux",
        modules=(
            ModuleRef(name="openconfig-interfaces"),
            ModuleRef(name="openconfig-system"),
        ),
        datastore_schemas=("running", "candidate"),
    )
    p_id = db.save_profile(profile)
    assert p_id == profile.profile_id

    # Verify query methods
    profile_mods = db.get_profile_modules(p_id)
    mod_names = [m["name"] for m in profile_mods]
    assert "openconfig-interfaces" in mod_names
    assert "openconfig-system" in mod_names

    # Verify database statistics
    stats = db.get_database_statistics()
    assert stats["schema_modules"] >= 2
    assert stats["schema_profiles"] >= 1
    assert stats["profile_modules"] >= 2
    assert stats["profile_datastores"] == 2

    db.close()


def test_srlinux_device_binding_and_persistence(tmp_path):
    """Verifies binding device address to profile and surviving SchemaDB re-instantiation."""
    db_file = tmp_path / "bindings.db"
    db1 = SchemaDB(db_path=db_file, cache_dir=tmp_path / "cache")

    profile = SchemaProfile(
        content_id="srlinux_25_10",
        modules=(ModuleRef(name="openconfig-interfaces"),),
    )
    p_id = db1.save_profile(profile)

    # Bind NETCONF and gNMI endpoints
    db1.bind_device(SRLINUX_TARGET, protocol="netconf", profile_id=p_id)
    db1.bind_device(SRLINUX_GNMI_TARGET, protocol="gnmi", profile_id=p_id)

    info_netconf = db1.get_device_binding_info(SRLINUX_TARGET)
    assert info_netconf is not None
    assert info_netconf["protocol"] == "netconf"
    assert info_netconf["profile_id"] == p_id

    info_gnmi = db1.get_device_binding_info(SRLINUX_GNMI_TARGET)
    assert info_gnmi is not None
    assert info_gnmi["protocol"] == "gnmi"
    assert info_gnmi["profile_id"] == p_id

    db1.close()

    # Re-open with new instance and verify persistence
    db2 = SchemaDB(db_path=db_file, cache_dir=tmp_path / "cache")
    assert db2.get_device_profile_id(SRLINUX_TARGET) == p_id
    assert db2.get_device_profile_id(SRLINUX_GNMI_TARGET) == p_id
    db2.close()


def test_srlinux_validate_openconfig_paths(tmp_path, srlinux_yang_dir):
    """Validates real OpenConfig interface and system paths against the compiled context."""
    registry = ContextRegistry()
    sctx = registry.get_or_create_context(profile_id="srlinux_validate", search_dirs=[srlinux_yang_dir])
    resolver = SchemaResolver(schema_db=SchemaDB(cache_dir=tmp_path / "cache"))

    for model in SRLINUX_MODELS:
        resolver.ensure_module(context=sctx, module_name=model, search_dirs=[srlinux_yang_dir])

    # Validate path lookup in compiled schema
    nodes_if = list(sctx.find_path("/openconfig-interfaces:interfaces/interface/config/description"))
    assert len(nodes_if) > 0, "Failed to resolve /openconfig-interfaces:interfaces/interface/config/description"

    nodes_sys = list(sctx.find_path("/openconfig-system:system/config/hostname"))
    assert len(nodes_sys) > 0, "Failed to resolve /openconfig-system:system/config/hostname"

