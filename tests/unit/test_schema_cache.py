import os
import sys
from pathlib import Path
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.schema_db import SchemaDB
from core.schema_errors import SchemaCacheError
from config.schema import SchemaProfile, ModuleRef


SAMPLE_YANG = """
module test-mod {
  yang-version 1.1;
  namespace "urn:test:mod";
  prefix "tm";
  revision 2023-01-01 {
    description "Initial version";
  }
  container config {
    leaf enabled {
      type boolean;
      default true;
    }
  }
}
"""


def test_schema_db_init_and_store(tmp_path):
    db_path = tmp_path / "test.db"
    cache_dir = tmp_path / "cache"

    db = SchemaDB(db_path=db_path, cache_dir=cache_dir)
    assert db.models_dir.exists()
    assert db_path.exists()

    # Store module
    path = db.store_module_content(
        name="test-mod",
        content=SAMPLE_YANG,
        revision="2023-01-01",
        namespace="urn:test:mod",
        source="unit_test",
        features=["feat-a"],
        verify_with_libyang=True,
    )
    assert path.is_file()
    assert path.name == "test-mod@2023-01-01.yang"

    # Verify lookup by exact name and revision
    found = db.find_module(name="test-mod", revision="2023-01-01")
    assert found == path

    # Verify lookup by name only
    found_name_only = db.find_module(name="test-mod")
    assert found_name_only == path

    # Non-existent revision
    assert db.find_module(name="test-mod", revision="1999-01-01") is None
    db.close()


def test_schema_db_atomic_write_rollback_on_bad_content(tmp_path):
    db = SchemaDB(cache_dir=tmp_path / "cache")
    bad_yang = "this is not valid yang { syntax error !!"

    with pytest.raises(SchemaCacheError):
        db.store_module_content(
            name="bad-mod",
            content=bad_yang,
            revision="2023-01-01",
            verify_with_libyang=True,
        )

    # Ensure no file leaked into models directory
    assert list(db.models_dir.glob("bad-mod*")) == []
    db.close()


def test_schema_db_corruption_recovery(tmp_path):
    db = SchemaDB(cache_dir=tmp_path / "cache")
    path = db.store_module_content(
        name="test-mod",
        content=SAMPLE_YANG,
        revision="2023-01-01",
        verify_with_libyang=False,
    )
    assert path.exists()

    # Tamper with file content
    with open(path, "w", encoding="utf-8") as f:
        f.write("corrupted content")

    # Lookup should detect checksum mismatch and invalidate file
    found = db.find_module(name="test-mod", revision="2023-01-01")
    assert found is None
    assert not path.exists()  # corrupt file was unlinked
    db.close()


def test_schema_db_profile_and_device_binding(tmp_path):
    db = SchemaDB(cache_dir=tmp_path / "cache")
    profile = SchemaProfile(modules=(ModuleRef(name="test-mod", revision="2023-01-01"),))

    p_id = db.save_profile(profile)
    assert p_id == profile.profile_id
    assert db.get_profile_json(p_id) is not None

    db.bind_device(device_key="10.0.0.1:830", protocol="netconf", profile_id=p_id)
    assert db.get_device_profile_id("10.0.0.1:830") == p_id
    assert db.get_device_profile_id("10.0.0.2:830") is None
    db.close()

