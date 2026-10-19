import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.schema_context import ContextRegistry
from core.schema_validator import SchemaValidator
from core.schema_errors import SchemaValidationError, InvalidSchemaPathError
from config.operations import GetOperation, SetOperation
from config.selectors import PathSelector


INTERFACES_YANG = """
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
      leaf oper-status {
        type enumeration {
          enum up;
          enum down;
        }
        config false;
      }
    }
  }
}
"""


@pytest.fixture
def schema_context():
    registry = ContextRegistry()
    sctx = registry.get_or_create_context("validator-profile")
    sctx.parse_module_str(INTERFACES_YANG)
    return sctx


def test_validator_valid_and_invalid_path(schema_context):
    validator = SchemaValidator()

    # Valid schema path
    node = validator.validate_path("/test-if:interfaces/interface/name", schema_context)
    assert node is not None

    # Valid schema path with list key predicate
    node_with_key = validator.validate_path("/test-if:interfaces/interface[name='eth0']/mtu", schema_context)
    assert node_with_key is not None

    # Invalid path
    with pytest.raises(InvalidSchemaPathError):
        validator.validate_path("/test-if:interfaces/interface/non-existent-leaf", schema_context)


def test_validator_read_only_config_false_enforcement(schema_context):
    validator = SchemaValidator()
    oper_status_path = "/test-if:interfaces/interface/oper-status"

    # Permitted for Get
    get_op = GetOperation(selector=PathSelector(paths=[oper_status_path]))
    node = validator.validate_path(oper_status_path, schema_context, operation=get_op)
    assert node is not None

    # Rejected for Set
    set_op = SetOperation()
    with pytest.raises(SchemaValidationError) as exc_info:
        validator.validate_path(oper_status_path, schema_context, operation=set_op)
    assert "read-only" in str(exc_info.value)


def test_validator_payload_validation(schema_context):
    validator = SchemaValidator()
    mtu_path = "/test-if:interfaces/interface[name='eth0']/mtu"

    # Valid MTU
    validator.validate_payload(mtu_path, 1500, schema_context)

    # Invalid MTU (out of range 64..9216)
    with pytest.raises(SchemaValidationError) as exc_info:
        validator.validate_payload(mtu_path, 99999, schema_context)
    assert "Payload validation failed" in str(exc_info.value)

