import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.schema_manager import SchemaManager
from config.schema import SchemaProfile, ModuleRef
from config.operations import Change, ChangeType


SAMPLE_YANG = """
module simple-net {
  yang-version 1.1;
  namespace "urn:simple:net";
  prefix "sn";

  container network {
    leaf domain {
      type string;
    }
  }
}
"""


def test_schema_manager_singleton(tmp_path):
    SchemaManager.reset_instance()
    mgr1 = SchemaManager(global_cache_dir=tmp_path / "cache1")
    mgr2 = SchemaManager(global_cache_dir=tmp_path / "cache2")

    assert mgr1 is mgr2
    SchemaManager.reset_instance()


def test_schema_manager_end_to_end_flow(tmp_path):
    SchemaManager.reset_instance()
    mgr = SchemaManager(global_cache_dir=tmp_path / "cache")

    profile = SchemaProfile(modules=(ModuleRef(name="simple-net"),))
    sctx = mgr.get_or_create_context(profile)
    assert sctx.profile_id == profile.profile_id

    # Load module directly
    mgr.load_module_str(SAMPLE_YANG, profile_id=profile.profile_id, name="simple-net")

    # Validate path
    node = mgr.validate_path("/simple-net:network/domain", profile_id=profile.profile_id)
    assert node is not None

    # Validate and serialize NETCONF edit
    changes = [Change(path="/simple-net:network/domain", value="example.com", operation=ChangeType.MERGE)]
    xml_output = mgr.validate_and_serialize_netconf_edit(changes, profile_id=profile.profile_id)
    assert "<domain>example.com</domain>" in xml_output

    # Validate and serialize gNMI change
    json_output = mgr.validate_and_serialize_gnmi_change(changes[0], profile_id=profile.profile_id)
    assert "example.com" in json_output

    SchemaManager.reset_instance()

