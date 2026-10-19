# -*- encoding: utf-8 -*-
"""
test_core_yang_ocnos.py

Tests loading, caching, and compiling YANG data models from OcNOS device ("ocnos_derived"):
- Address: 10.1.11.101:830 (NETCONF) or 9339 (gNMI)
- Models: ipi-streaming-telemetry, ipi-interface
- Directory: tests/yang/ocnos_derived / test/yang/ocnos_derived
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
from config.schema import SchemaProfile, ModuleRef
from tests.yang_fetcher import YangSchemaFetcher


OCNOS_TARGET = "10.1.11.101:830"
OCNOS_GNMI_TARGET = "10.1.11.101:9339"
OCNOS_USER = "admin"
OCNOS_PASS = "Frontier12#"
OCNOS_MODELS = ["ipi-streaming-telemetry", "ipi-interface"]

YANG_DIR = (Path(__file__).resolve().parent / "yang" / "ocnos_derived") if (Path(__file__).resolve().parent / "yang" / "ocnos_derived").exists() else (Path(__file__).resolve().parent.parent / "yang" / "ocnos_derived")
ALT_YANG_DIR = (Path(__file__).resolve().parent.parent / "test" / "yang" / "ocnos_derived") if (Path(__file__).resolve().parent.parent / "test" / "yang" / "ocnos_derived").exists() else (Path(__file__).resolve().parent.parent.parent / "test" / "yang" / "ocnos_derived")


@pytest.fixture(scope="module")
def ocnos_yang_dir():
    """Ensures tests/yang/ocnos_derived directory exists and models are populated."""
    YANG_DIR.mkdir(parents=True, exist_ok=True)

    missing = []
    for model in OCNOS_MODELS:
        candidates = list(YANG_DIR.glob(f"{model}*.yang"))
        if not candidates:
            missing.append(model)

    if missing:
        try:
            fetcher = YangSchemaFetcher(
                target=OCNOS_TARGET,
                username=OCNOS_USER,
                password=OCNOS_PASS,
                output_dir=YANG_DIR,
                timeout=15,
            )
            for m in missing:
                fetcher.fetch_and_save(m, recursive=True)
        except Exception as exc:
            pytest.skip(f"Live target {OCNOS_TARGET} unreachable or get-schema failed: {exc}")

    return YANG_DIR


def test_ocnos_yang_files_exist(ocnos_yang_dir):
    """Verifies that ipi-streaming-telemetry and ipi-interface exist in vendor directory."""
    for model in OCNOS_MODELS:
        matches = list(ocnos_yang_dir.glob(f"{model}*.yang"))
        assert len(matches) > 0, f"Expected {model} in {ocnos_yang_dir}"
        assert matches[0].stat().st_size > 0, f"YANG file {matches[0]} should not be empty"


def test_ocnos_symlink_directory_compatibility(ocnos_yang_dir):
    """Verifies that ./test/yang/ocnos_derived points to the exact same vendor directory."""
    if ALT_YANG_DIR.exists():
        assert ALT_YANG_DIR.resolve() == ocnos_yang_dir.resolve()
        for model in OCNOS_MODELS:
            matches = list(ALT_YANG_DIR.glob(f"{model}*.yang"))
            assert len(matches) > 0, f"Expected {model} via alt path {ALT_YANG_DIR}"


def test_ocnos_load_and_compile_context(tmp_path, ocnos_yang_dir):
    """Loads ipi-streaming-telemetry and ipi-interface into an isolated SchemaContext."""
    cache_dir = tmp_path / "cache"
    db = SchemaDB(cache_dir=cache_dir)
    registry = ContextRegistry()
    sctx = registry.get_or_create_context(profile_id="ocnos_derived", search_dirs=[ocnos_yang_dir, db.models_dir])

    resolver = SchemaResolver(schema_db=db)
    for model in OCNOS_MODELS:
        resolver.ensure_module(
            context=sctx,
            module_name=model,
            search_dirs=[ocnos_yang_dir],
        )

    # Verify both modules compiled successfully in libyang
    assert sctx.has_module("ipi-streaming-telemetry")
    assert sctx.has_module("ipi-interface")

    # Verify key imported dependencies are also loaded
    assert sctx.has_module("cml-data-types")
    assert sctx.has_module("ipi-network-instance")
    assert sctx.has_module("ipi-vrf")
    assert sctx.has_module("feature-list")
    db.close()


def test_ocnos_schema_db_metadata_indexing(tmp_path, ocnos_yang_dir):
    """Validates that SchemaDB stores OcNOS modules, SHA256 hashes, and links them to profile."""
    db_path = tmp_path / "ocnos_test.db"
    db = SchemaDB(db_path=db_path, cache_dir=tmp_path / "cache")

    for yang_file in ocnos_yang_dir.glob("*.yang"):
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
    telemetry_path = db.find_module("ipi-streaming-telemetry")
    assert telemetry_path is not None
    assert telemetry_path.is_file()

    if_path = db.find_module("ipi-interface")
    assert if_path is not None
    assert if_path.is_file()

    # Save profile and link modules
    profile = SchemaProfile(
        content_id="ocnos_derived",
        modules=(
            ModuleRef(name="ipi-streaming-telemetry"),
            ModuleRef(name="ipi-interface"),
        ),
        datastore_schemas=("running", "candidate"),
    )
    p_id = db.save_profile(profile)
    assert p_id == profile.profile_id

    # Verify query methods
    profile_mods = db.get_profile_modules(p_id)
    mod_names = [m["name"] for m in profile_mods]
    assert "ipi-streaming-telemetry" in mod_names
    assert "ipi-interface" in mod_names

    # Verify database statistics
    stats = db.get_database_statistics()
    assert stats["schema_modules"] >= 2
    assert stats["schema_profiles"] >= 1
    assert stats["profile_modules"] >= 2

    db.close()


def test_ocnos_device_binding_and_persistence(tmp_path):
    """Verifies binding OcNOS endpoint addresses to profile and surviving restart."""
    db_file = tmp_path / "ocnos_bindings.db"
    db1 = SchemaDB(db_path=db_file, cache_dir=tmp_path / "cache")

    profile = SchemaProfile(
        content_id="ocnos_6_5",
        modules=(ModuleRef(name="ipi-interface"),),
    )
    p_id = db1.save_profile(profile)

    # Bind NETCONF and gNMI endpoints
    db1.bind_device(OCNOS_TARGET, protocol="netconf", profile_id=p_id)
    db1.bind_device(OCNOS_GNMI_TARGET, protocol="gnmi", profile_id=p_id)

    info_netconf = db1.get_device_binding_info(OCNOS_TARGET)
    assert info_netconf is not None
    assert info_netconf["protocol"] == "netconf"
    assert info_netconf["profile_id"] == p_id

    info_gnmi = db1.get_device_binding_info(OCNOS_GNMI_TARGET)
    assert info_gnmi is not None
    assert info_gnmi["protocol"] == "gnmi"
    assert info_gnmi["profile_id"] == p_id

    db1.close()

    # Re-open with new instance and verify persistence
    db2 = SchemaDB(db_path=db_file, cache_dir=tmp_path / "cache")
    assert db2.get_device_profile_id(OCNOS_TARGET) == p_id
    assert db2.get_device_profile_id(OCNOS_GNMI_TARGET) == p_id
    db2.close()


def test_ocnos_validate_telemetry_and_interface_paths(tmp_path, ocnos_yang_dir):
    """Validates real OcNOS telemetry and interface paths against the compiled context."""
    registry = ContextRegistry()
    sctx = registry.get_or_create_context(profile_id="ocnos_validate", search_dirs=[ocnos_yang_dir])
    resolver = SchemaResolver(schema_db=SchemaDB(cache_dir=tmp_path / "cache"))

    for model in OCNOS_MODELS:
        resolver.ensure_module(context=sctx, module_name=model, search_dirs=[ocnos_yang_dir])

    # Validate path lookup in compiled schema
    nodes_telem = list(sctx.find_path("/ipi-streaming-telemetry:telemetry-system/global/config"))
    assert len(nodes_telem) > 0, "Failed to resolve /ipi-streaming-telemetry:telemetry-system/global/config"

    nodes_if = list(sctx.find_path("/ipi-interface:interfaces/interface/name"))
    assert len(nodes_if) > 0, "Failed to resolve /ipi-interface:interfaces/interface/name"

