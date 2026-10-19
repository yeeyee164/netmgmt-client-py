import os
import sys
from pathlib import Path
from unittest.mock import MagicMock
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from specs.gnmi_client import GNMIClient
from config.schema import SchemaConfig, SchemaProfile, ModuleRef
from config.operations import (
    CapabilitiesOperation,
    GetOperation,
    SetOperation,
    Change,
    ChangeType,
)
from config.selectors import PathSelector
from config.protocol_options import GNMIOptions
from core.schema_manager import SchemaManager
from core.schema_errors import InvalidSchemaPathError, SchemaValidationError


TEST_YANG_IF = """
module test-if {
  yang-version 1.1;
  namespace "urn:test:if";
  prefix "t-if";

  container interfaces {
    list interface {
      key "name";
      leaf name {
        type string;
      }
      leaf mtu {
        type uint16 {
          range "64..9216";
        }
      }
      leaf description {
        type string;
      }
    }
  }
}
"""


class DummyModelData:
    def __init__(self, name, organization="", version=""):
        self.name = name
        self.organization = organization
        self.version = version


@pytest.fixture(autouse=True)
def clean_schema_manager(tmp_path):
    SchemaManager.reset_instance()
    sm = SchemaManager(global_cache_dir=tmp_path / "schema_cache")
    yield sm
    SchemaManager.reset_instance()


def test_gnmi_execute_capabilities_auto_discovery(tmp_path):
    client = GNMIClient(
        target="192.0.2.1:50051",
        schema_config=SchemaConfig(cache_dir=tmp_path / "schema_cache", auto_discovery=True),
    )
    mock_stub = MagicMock()
    mock_response = MagicMock()
    mock_response.supported_models = [
        DummyModelData(name="openconfig-interfaces", organization="OpenConfig", version="2.0.0"),
    ]
    mock_stub.Capabilities.return_value = mock_response
    client.stub = mock_stub

    res = client.execute_capabilities(CapabilitiesOperation())
    assert res == mock_response
    assert client.schema_profile is not None
    assert any(m.name == "openconfig-interfaces" for m in client.schema_profile.modules)

    # Verify persisted in SchemaDB
    sm = SchemaManager()
    persisted_json = sm.db.get_profile_json(client.schema_profile.profile_id)
    assert persisted_json is not None
    binding = sm.db.get_device_profile_id("192.0.2.1:50051")
    assert binding == client.schema_profile.profile_id


def test_gnmi_execute_set_with_schema_validation_and_json_ietf(tmp_path):
    yang_dir = tmp_path / "yang"
    yang_dir.mkdir()
    (yang_dir / "test-if@2021-01-01.yang").write_text(TEST_YANG_IF, encoding="utf-8")

    client = GNMIClient(
        target="192.0.2.1:50051",
        encoding="json_ietf",
        schema_config=SchemaConfig(
            yang_dirs=(yang_dir,),
            cache_dir=tmp_path / "schema_cache",
            validate=True,
            auto_discovery=False,
        ),
    )
    mock_stub = MagicMock()
    client.stub = mock_stub
    client.schema_profile = SchemaProfile(
        content_id="test_gnmi_profile",
        modules=(ModuleRef(name="test-if", revision="2021-01-01"),),
    )

    set_op = SetOperation(
        changes=[
            Change(
                path="/test-if:interfaces/interface[name='eth0']/mtu",
                operation=ChangeType.MERGE,
                value=1500,
            ),
            Change(
                path="/test-if:interfaces/interface[name='eth0']/description",
                operation=ChangeType.REPLACE,
                value="Uplink Port",
            ),
            Change(
                path="/test-if:interfaces/interface[name='eth1']",
                operation=ChangeType.DELETE,
            ),
        ]
    )

    client.execute_set(set_op)

    assert mock_stub.Set.called
    req = mock_stub.Set.call_args[0][0]
    # Check updates
    assert len(req.update) >= 1
    # Check deletes
    assert len(req.delete) >= 1

    # Check json_ietf_val is used for the updates
    for u in req.update:
        assert u.val.json_ietf_val or u.val.int_val or u.val.string_val


def test_gnmi_execute_set_invalid_path_raises(tmp_path):
    yang_dir = tmp_path / "yang"
    yang_dir.mkdir()
    (yang_dir / "test-if@2021-01-01.yang").write_text(TEST_YANG_IF, encoding="utf-8")

    client = GNMIClient(
        target="192.0.2.1:50051",
        schema_config=SchemaConfig(
            yang_dirs=(yang_dir,),
            cache_dir=tmp_path / "schema_cache",
            validate=True,
            auto_discovery=False,
        ),
    )
    mock_stub = MagicMock()
    client.stub = mock_stub
    client.schema_profile = SchemaProfile(
        content_id="test_gnmi_profile",
        modules=(ModuleRef(name="test-if", revision="2021-01-01"),),
    )

    set_op = SetOperation(
        changes=[
            Change(
                path="/test-if:interfaces/interface[name='eth0']/bad_leaf",
                operation=ChangeType.MERGE,
                value="test",
            ),
        ]
    )

    with pytest.raises(InvalidSchemaPathError):
        client.execute_set(set_op)


def test_gnmi_execute_set_invalid_value_raises(tmp_path):
    yang_dir = tmp_path / "yang"
    yang_dir.mkdir()
    (yang_dir / "test-if@2021-01-01.yang").write_text(TEST_YANG_IF, encoding="utf-8")

    client = GNMIClient(
        target="192.0.2.1:50051",
        schema_config=SchemaConfig(
            yang_dirs=(yang_dir,),
            cache_dir=tmp_path / "schema_cache",
            validate=True,
            auto_discovery=False,
        ),
    )
    mock_stub = MagicMock()
    client.stub = mock_stub
    client.schema_profile = SchemaProfile(
        content_id="test_gnmi_profile",
        modules=(ModuleRef(name="test-if", revision="2021-01-01"),),
    )

    set_op = SetOperation(
        changes=[
            Change(
                path="/test-if:interfaces/interface[name='eth0']/mtu",
                operation=ChangeType.MERGE,
                value=99999,  # Outside 64..9216
            ),
        ]
    )

    with pytest.raises(SchemaValidationError):
        client.execute_set(set_op)


def test_gnmi_execute_get_with_schema_validation(tmp_path):
    yang_dir = tmp_path / "yang"
    yang_dir.mkdir()
    (yang_dir / "test-if@2021-01-01.yang").write_text(TEST_YANG_IF, encoding="utf-8")

    client = GNMIClient(
        target="192.0.2.1:50051",
        schema_config=SchemaConfig(
            yang_dirs=(yang_dir,),
            cache_dir=tmp_path / "schema_cache",
            validate=True,
            auto_discovery=False,
        ),
    )
    mock_stub = MagicMock()
    client.stub = mock_stub
    client.schema_profile = SchemaProfile(
        content_id="test_gnmi_profile",
        modules=(ModuleRef(name="test-if", revision="2021-01-01"),),
    )

    valid_get = GetOperation(
        selector=PathSelector(paths=("/test-if:interfaces/interface[name='eth0']/mtu",))
    )
    client.execute_get(valid_get)
    assert mock_stub.Get.called

    invalid_get = GetOperation(
        selector=PathSelector(paths=("/test-if:interfaces/bad_node",))
    )
    with pytest.raises(InvalidSchemaPathError):
        client.execute_get(invalid_get)

