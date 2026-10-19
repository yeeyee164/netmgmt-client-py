import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from managers.factory import ValidatorFactory
from specs.netconf_validator import NetconfValidator
from specs.gnmi_validator import GNMIValidator
from modules.validate import PathValidationError
from core.schema_context import ContextRegistry
from core.schema_manager import SchemaManager
from config.operations import SetOperation, GetOperation
from config.schema import SchemaProfile, ModuleRef


SAMPLE_INTERFACES_YANG = """
module netconf-test-if {
  yang-version 1.1;
  namespace "urn:netconf:test:if";
  prefix "ntif";

  container interfaces {
    list interface {
      key "name";
      leaf name {
        type string;
      }
      leaf mtu {
        type uint16;
      }
      leaf status {
        type string;
        config false;
      }
    }
  }
}
"""


@pytest.fixture
def schema_context():
    registry = ContextRegistry()
    sctx = registry.get_or_create_context("validator-test-profile")
    sctx.parse_module_str(SAMPLE_INTERFACES_YANG)
    return sctx


def test_validator_factory():
    gnmi_val = ValidatorFactory.get_validator("gnmi")
    assert isinstance(gnmi_val, GNMIValidator)

    netconf_val = ValidatorFactory.get_validator("netconf")
    assert isinstance(netconf_val, NetconfValidator)

    restconf_val = ValidatorFactory.get_validator("restconf")
    assert restconf_val is None

    with pytest.raises(ValueError):
        ValidatorFactory.get_validator("unknown_protocol")


def test_netconf_validator_syntax():
    val = NetconfValidator()

    # Valid XPath
    val.validate_path("/interfaces/interface[name='eth0']/mtu")

    # Invalid empty path
    with pytest.raises(PathValidationError):
        val.validate_path("")

    # Invalid XPath syntax
    with pytest.raises(PathValidationError):
        val.validate_path("///invalid///xpath")

    # Valid Subtree filter
    val.validate_path("<interfaces><interface><name>eth0</name></interface></interfaces>", filter_type="subtree")

    # Invalid Subtree filter
    with pytest.raises(PathValidationError):
        val.validate_path("<interfaces><unclosed></interfaces>", filter_type="subtree")


def test_netconf_validator_schema_validation(schema_context):
    val = NetconfValidator()

    # Valid schema node
    val.validate_path("/netconf-test-if:interfaces/interface/mtu", schema_context=schema_context)

    # Unknown schema node
    with pytest.raises(PathValidationError):
        val.validate_path("/netconf-test-if:interfaces/interface/unknown-leaf", schema_context=schema_context)

    # Read-only 'config false' node allowed for Get
    val.validate_path(
        "/netconf-test-if:interfaces/interface/status",
        operation="get",
        schema_context=schema_context,
        op_config=GetOperation(selector=None),
    )

    # Read-only 'config false' node rejected for Set
    with pytest.raises(PathValidationError) as exc_info:
        val.validate_path(
            "/netconf-test-if:interfaces/interface/status",
            operation="set",
            schema_context=schema_context,
            op_config=SetOperation(),
        )
    assert "read-only" in str(exc_info.value)


def test_gnmi_validator_syntax_and_schema(schema_context):
    val = GNMIValidator()

    # Section 3.4.5: Wildcard in Set is rejected
    with pytest.raises(PathValidationError):
        val.validate_path("/interfaces/interface[name=*]/mtu", operation="set")

    # Valid syntactic path
    val.validate_path("/interfaces/interface[name=eth0]/mtu", operation="get")

    # Schema validation with schema_context
    val.validate_path("/netconf-test-if:interfaces/interface/mtu", schema_context=schema_context)

    # Schema validation failure on unknown node
    with pytest.raises(PathValidationError):
        val.validate_path("/netconf-test-if:interfaces/interface/non-existent", schema_context=schema_context)

    # Schema validation failure on read-only for Set
    with pytest.raises(PathValidationError) as exc_info:
        val.validate_path(
            "/netconf-test-if:interfaces/interface/status",
            operation="set",
            schema_context=schema_context,
            op_config=SetOperation(),
        )
    assert "read-only" in str(exc_info.value)

