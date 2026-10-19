import os
import sys
from dataclasses import dataclass
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.schema_catalog import ModelCatalog, ModelCatalogEntry
from core.schema_errors import SchemaDiscoveryError
from config.schema import ModuleRef


@dataclass
class DummyModelData:
    name: str
    organization: str = ""
    version: str = ""


def test_model_catalog_registration_and_lookup():
    catalog = ModelCatalog()
    entry = catalog.register_model(
        name="openconfig-interfaces",
        organization="openconfig",
        version="2.0.0",
        modules=[
            ModuleRef(name="openconfig-interfaces", revision="2021-04-06"),
            ModuleRef(name="openconfig-if-ip", revision="2021-04-06"),
        ],
    )

    # Exact lookup
    resolved = catalog.resolve_model("openconfig-interfaces", "openconfig", "2.0.0")
    assert resolved == entry

    # Wildcard lookup
    resolved_any_ver = catalog.resolve_model("openconfig-interfaces")
    assert resolved_any_ver == entry

    # Search for underlying module
    mod = catalog.find_module("openconfig-if-ip")
    assert mod is not None
    assert mod.revision == "2021-04-06"


def test_model_catalog_gnmi_modeldata_resolution():
    catalog = ModelCatalog()
    catalog.register_model(
        name="ietf-interfaces",
        organization="ietf",
        version="0.1.0",
        modules=[ModuleRef(name="ietf-interfaces", revision="2018-02-20")],
    )

    # Valid model data
    models = [DummyModelData(name="ietf-interfaces", organization="ietf", version="0.1.0")]
    profile = catalog.build_profile_from_gnmi_models(models, strict=True)
    assert len(profile.modules) == 1
    assert profile.modules[0].name == "ietf-interfaces"
    assert profile.modules[0].revision == "2018-02-20"

    # Unmapped model in strict mode raises SchemaDiscoveryError
    unmapped = [DummyModelData(name="unknown-vendor-model", organization="vendor", version="1.0")]
    with pytest.raises(SchemaDiscoveryError) as exc_info:
        catalog.build_profile_from_gnmi_models(unmapped, strict=True)
    assert "cannot be resolved" in str(exc_info.value)

