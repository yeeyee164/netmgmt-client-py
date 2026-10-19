import os
import sys
import json
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.schema_context import ContextRegistry
from core.schema_data import DataBuilder
from core.schema_serializer import SchemaSerializer
from config.operations import Change, ChangeType


DEVICE_YANG = """
module device-config {
  yang-version 1.1;
  namespace "urn:device:cfg";
  prefix "dev";

  container system {
    leaf hostname {
      type string;
    }
    leaf timezone {
      type string;
    }
  }
}
"""


@pytest.fixture
def schema_context():
    registry = ContextRegistry()
    sctx = registry.get_or_create_context("serializer-profile")
    sctx.parse_module_str(DEVICE_YANG)
    return sctx


def test_serializer_xml_and_json(schema_context):
    serializer = SchemaSerializer()
    changes = [
        Change(path="/device-config:system/hostname", value="router-01", operation=ChangeType.MERGE),
        Change(path="/device-config:system/timezone", value="UTC", operation=ChangeType.MERGE),
    ]

    dnode = DataBuilder.build_from_changes(schema_context, changes)
    xml_str = serializer.to_xml(dnode)
    json_str = serializer.to_json_ietf(dnode)

    assert "<hostname>router-01</hostname>" in xml_str
    assert "xmlns=\"urn:device:cfg\"" in xml_str

    parsed_json = json.loads(json_str)
    assert parsed_json["device-config:system"]["hostname"] == "router-01"
    assert parsed_json["device-config:system"]["timezone"] == "UTC"


def test_serializer_netconf_edit_config_metadata(schema_context):
    serializer = SchemaSerializer()
    changes = [
        Change(path="/device-config:system/hostname", value="router-02", operation=ChangeType.REPLACE),
    ]

    xml_edit = serializer.serialize_netconf_edit_config(schema_context, changes)
    assert "nc:operation=\"replace\"" in xml_edit
    assert 'hostname nc:operation="replace">router-02</hostname>' in xml_edit


def test_serializer_gnmi_update(schema_context):
    serializer = SchemaSerializer()
    ch = Change(path="/device-config:system/hostname", value="switch-01", operation=ChangeType.MERGE)
    json_out = serializer.serialize_gnmi_update(schema_context, ch)

    parsed = json.loads(json_out)
    assert parsed["device-config:system"]["hostname"] == "switch-01"

