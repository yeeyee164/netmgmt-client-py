import os
import sys
from pathlib import Path
from unittest.mock import MagicMock
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from specs.netconf_client import NetconfClient
from config.schema import SchemaConfig, SchemaProfile, ModuleRef
from config.operations import (
    GetOperation,
    SetOperation,
    GetSchemaOperation,
    Change,
    ChangeType,
)
from config.selectors import PathSelector
from config.protocol_options import NetconfOptions
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


@pytest.fixture(autouse=True)
def clean_schema_manager(tmp_path):
    SchemaManager.reset_instance()
    sm = SchemaManager(global_cache_dir=tmp_path / "schema_cache")
    yield sm
    SchemaManager.reset_instance()


def test_netconf_discover_schema_profile(tmp_path):
    client = NetconfClient(
        target="192.0.2.1:830",
        schema_config=SchemaConfig(cache_dir=tmp_path / "schema_cache")
    )
    mock_session = MagicMock()
    mock_session.server_capabilities = [
        "urn:ietf:params:netconf:base:1.0",
        "urn:ietf:params:netconf:capability:candidate:1.0",
        "urn:ietf:params:netconf:capability:startup:1.0",
        "urn:test:if?module=test-if&revision=2021-01-01&features=feat1",
    ]
    client.session = mock_session

    profile = client.discover_schema_profile()

    assert "running" in profile.datastore_schemas
    assert "candidate" in profile.datastore_schemas
    assert "startup" in profile.datastore_schemas
    assert len(profile.modules) == 1
    assert profile.modules[0].name == "test-if"
    assert profile.modules[0].revision == "2021-01-01"

    # Verify persisted in DB
    sm = SchemaManager()
    persisted_json = sm.db.get_profile_json(profile.profile_id)
    assert persisted_json is not None
    binding = sm.db.get_device_profile_id("192.0.2.1:830")
    assert binding == profile.profile_id


def test_netconf_get_schema_and_cache(tmp_path):
    client = NetconfClient(
        target="192.0.2.1:830",
        schema_config=SchemaConfig(cache_dir=tmp_path / "schema_cache")
    )
    mock_session = MagicMock()
    mock_reply = MagicMock()
    mock_reply.data = TEST_YANG_IF
    mock_session.get_schema.return_value = mock_reply
    client.session = mock_session

    res = client.get_schema(identifier="test-if", version="2021-01-01")
    assert res == mock_reply

    # Verify cached in SchemaDB
    sm = SchemaManager()
    cached_path = sm.db.find_module(name="test-if", revision="2021-01-01")
    assert cached_path is not None
    assert cached_path.exists()
    assert "module test-if" in cached_path.read_text()


def test_netconf_execute_set_with_schema_validation_and_serialization(tmp_path):
    yang_dir = tmp_path / "yang"
    yang_dir.mkdir()
    (yang_dir / "test-if@2021-01-01.yang").write_text(TEST_YANG_IF, encoding="utf-8")

    client = NetconfClient(
        target="192.0.2.1:830",
        schema_config=SchemaConfig(
            yang_dirs=(yang_dir,),
            cache_dir=tmp_path / "schema_cache",
            validate=True,
            auto_discovery=False,
        ),
    )
    mock_session = MagicMock()
    mock_session.server_capabilities = ["urn:ietf:params:netconf:base:1.0", "urn:ietf:params:netconf:capability:candidate:1.0"]
    client.session = mock_session
    client.schema_profile = SchemaProfile(
        content_id="test_profile",
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
                operation=ChangeType.MERGE,
                value="Uplink Port",
            ),
        ],
        protocol_options=NetconfOptions(target_datastore="candidate", commit=False),
    )

    client.execute_set(set_op)

    # Verify edit_config was invoked with valid XML config subtree
    assert mock_session.edit_config.called
    kwargs = mock_session.edit_config.call_args[1]
    assert kwargs["target"] == "candidate"
    config_xml = kwargs["config"]
    assert "<interfaces xmlns=\"urn:test:if\">" in config_xml
    assert "<name>eth0</name>" in config_xml
    assert "<mtu>1500</mtu>" in config_xml
    assert "<description>Uplink Port</description>" in config_xml


def test_netconf_execute_set_invalid_path_raises(tmp_path):
    yang_dir = tmp_path / "yang"
    yang_dir.mkdir()
    (yang_dir / "test-if@2021-01-01.yang").write_text(TEST_YANG_IF, encoding="utf-8")

    client = NetconfClient(
        target="192.0.2.1:830",
        schema_config=SchemaConfig(
            yang_dirs=(yang_dir,),
            cache_dir=tmp_path / "schema_cache",
            validate=True,
            auto_discovery=False,
        ),
    )
    mock_session = MagicMock()
    mock_session.server_capabilities = ["urn:ietf:params:netconf:base:1.0"]
    client.session = mock_session
    client.schema_profile = SchemaProfile(
        content_id="test_profile",
        modules=(ModuleRef(name="test-if", revision="2021-01-01"),),
    )

    set_op = SetOperation(
        changes=[
            Change(
                path="/test-if:interfaces/interface[name='eth0']/nonexistent",
                operation=ChangeType.MERGE,
                value="test",
            )
        ]
    )

    with pytest.raises(InvalidSchemaPathError):
        client.execute_set(set_op)


def test_netconf_execute_set_invalid_value_raises(tmp_path):
    yang_dir = tmp_path / "yang"
    yang_dir.mkdir()
    (yang_dir / "test-if@2021-01-01.yang").write_text(TEST_YANG_IF, encoding="utf-8")

    client = NetconfClient(
        target="192.0.2.1:830",
        schema_config=SchemaConfig(
            yang_dirs=(yang_dir,),
            cache_dir=tmp_path / "schema_cache",
            validate=True,
            auto_discovery=False,
        ),
    )
    mock_session = MagicMock()
    mock_session.server_capabilities = ["urn:ietf:params:netconf:base:1.0"]
    client.session = mock_session
    client.schema_profile = SchemaProfile(
        content_id="test_profile",
        modules=(ModuleRef(name="test-if", revision="2021-01-01"),),
    )

    set_op = SetOperation(
        changes=[
            Change(
                path="/test-if:interfaces/interface[name='eth0']/mtu",
                operation=ChangeType.MERGE,
                value=99999,  # Outside 64..9216
            )
        ]
    )

    with pytest.raises(SchemaValidationError):
        client.execute_set(set_op)


def test_netconf_execute_get_with_schema_validation(tmp_path):
    yang_dir = tmp_path / "yang"
    yang_dir.mkdir()
    (yang_dir / "test-if@2021-01-01.yang").write_text(TEST_YANG_IF, encoding="utf-8")

    client = NetconfClient(
        target="192.0.2.1:830",
        schema_config=SchemaConfig(
            yang_dirs=(yang_dir,),
            cache_dir=tmp_path / "schema_cache",
            validate=True,
            auto_discovery=False,
        ),
    )
    mock_session = MagicMock()
    mock_session.server_capabilities = ["urn:ietf:params:netconf:base:1.0"]
    client.session = mock_session
    client.schema_profile = SchemaProfile(
        content_id="test_profile",
        modules=(ModuleRef(name="test-if", revision="2021-01-01"),),
    )

    valid_get = GetOperation(
        selector=PathSelector(paths=("/test-if:interfaces/interface[name='eth0']/mtu",))
    )
    client.execute_get(valid_get)
    assert mock_session.get.called

    invalid_get = GetOperation(
        selector=PathSelector(paths=("/test-if:interfaces/invalid_node",))
    )
    with pytest.raises(InvalidSchemaPathError):
        client.execute_get(invalid_get)
