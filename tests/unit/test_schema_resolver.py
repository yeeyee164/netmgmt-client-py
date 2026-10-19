import os
import sys
from pathlib import Path
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.schema_context import ContextRegistry
from core.schema_db import SchemaDB
from core.schema_resolver import SchemaResolver
from core.schema_errors import SchemaNotFoundError, SchemaDependencyError


BASE_TYPES_YANG = """
module base-types {
  yang-version 1.1;
  namespace "urn:test:base";
  prefix "bt";
  revision 2023-01-01;
  typedef status-code {
    type enumeration {
      enum ok;
      enum fail;
    }
  }
}
"""

SERVICE_A_YANG = """
module service-a {
  yang-version 1.1;
  namespace "urn:test:service";
  prefix "sa";
  import base-types {
    prefix bt;
    revision-date 2023-01-01;
  }
  revision 2023-01-01;
  container service {
    leaf status {
      type bt:status-code;
    }
  }
}
"""


def test_dependency_extraction():
    deps = SchemaResolver.extract_dependencies(SERVICE_A_YANG)
    assert len(deps) == 1
    mod_name, rev, kind = deps[0]
    assert mod_name == "base-types"
    assert rev == "2023-01-01"
    assert kind == "import"


def test_recursive_dependency_resolution(tmp_path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    # Write files to models_dir
    (models_dir / "base-types@2023-01-01.yang").write_text(BASE_TYPES_YANG)
    (models_dir / "service-a@2023-01-01.yang").write_text(SERVICE_A_YANG)

    schema_db = SchemaDB(cache_dir=cache_dir)
    registry = ContextRegistry()
    sctx = registry.get_or_create_context("test-profile", search_dirs=[models_dir])

    resolver = SchemaResolver(schema_db=schema_db)
    # Ensure service-a loads its dependency base-types first
    resolver.ensure_module(context=sctx, module_name="service-a", revision="2023-01-01")

    assert sctx.has_module("base-types", "2023-01-01")
    assert sctx.has_module("service-a", "2023-01-01")
    schema_db.close()


def test_protocol_fetch_callback(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    schema_db = SchemaDB(cache_dir=cache_dir)
    registry = ContextRegistry()
    sctx = registry.get_or_create_context("fetch-profile")

    resolver = SchemaResolver(schema_db=schema_db)

    def mock_fetch(name: str, rev: str = None):
        if name == "base-types":
            return BASE_TYPES_YANG
        return None

    resolver.ensure_module(
        context=sctx,
        module_name="base-types",
        revision="2023-01-01",
        fetch_callback=mock_fetch,
    )

    assert sctx.has_module("base-types")
    # Verify it got saved into the cache
    assert schema_db.find_module("base-types", "2023-01-01") is not None
    schema_db.close()
