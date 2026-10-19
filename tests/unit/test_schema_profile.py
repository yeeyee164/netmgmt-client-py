import os
import sys
from pathlib import Path
import pytest

# Ensure project root directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config.model import ConnectionConfig, SessionConfig, Protocol
from config.schema import (
    SchemaConfig,
    SchemaProfile,
    ModuleRef,
    SubmoduleRef,
    FeatureRef,
    DeviationRef,
)


def test_schema_config_defaults():
    cfg = SchemaConfig()
    assert cfg.yang_dirs == ()
    assert cfg.cache_dir is None
    assert cfg.profile is None
    assert cfg.validate is False
    assert cfg.auto_discovery is True
    assert cfg.auto_fetch is True


def test_schema_config_normalization(tmp_path):
    yang_dir1 = tmp_path / "yang1"
    yang_dir1.mkdir()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    cfg = SchemaConfig(
        yang_dirs=[str(yang_dir1)],
        cache_dir=str(cache_dir),
        validate=True,
    )
    assert len(cfg.yang_dirs) == 1
    assert isinstance(cfg.yang_dirs[0], Path)
    assert cfg.yang_dirs[0] == yang_dir1.resolve()
    assert isinstance(cfg.cache_dir, Path)
    assert cfg.cache_dir == cache_dir.resolve()
    assert cfg.validate is True


def test_schema_profile_order_independence():
    m1 = ModuleRef(name="ietf-interfaces", revision="2018-02-20", namespace="urn:ietf:params:xml:ns:yang:ietf-interfaces")
    m2 = ModuleRef(name="ietf-ip", revision="2014-06-16", namespace="urn:ietf:params:xml:ns:yang:ietf-ip")
    m3 = ModuleRef(name="openconfig-interfaces", revision="2021-04-06")

    sub1 = SubmoduleRef(name="sub-a", revision="2020-01-01")
    sub2 = SubmoduleRef(name="sub-b", revision="2020-02-01")

    feat1 = FeatureRef(module="ietf-interfaces", name="if-mib")
    feat2 = FeatureRef(module="ietf-ip", name="ipv6-privacy-autoconf")

    dev1 = DeviationRef(name="cisco-xr-ietf-interfaces-deviations", revision="2018-12-01")
    dev2 = DeviationRef(name="juniper-interfaces-deviations", revision="2019-01-01")

    # Order 1
    p1 = SchemaProfile(
        modules=(m1, m2, m3),
        submodules=(sub1, sub2),
        features=(feat1, feat2),
        deviations=(dev1, dev2),
        datastore_schemas=("running", "candidate"),
        content_id="content-1234",
    )

    # Order 2 (reversed collections)
    p2 = SchemaProfile(
        modules=(m3, m2, m1),
        submodules=(sub2, sub1),
        features=(feat2, feat1),
        deviations=(dev2, dev1),
        datastore_schemas=("candidate", "running"),
        content_id="content-1234",
    )

    assert p1.profile_id == p2.profile_id
    assert p1.to_canonical_json() == p2.to_canonical_json()


def test_schema_profile_hash_sensitivity():
    base_m = ModuleRef(name="ietf-interfaces", revision="2018-02-20")
    base_profile = SchemaProfile(modules=(base_m,))
    base_id = base_profile.profile_id

    # 1. Module revision change
    rev_profile = SchemaProfile(modules=(ModuleRef(name="ietf-interfaces", revision="2014-05-08"),))
    assert rev_profile.profile_id != base_id

    # 2. Namespace change
    ns_profile = SchemaProfile(modules=(ModuleRef(name="ietf-interfaces", revision="2018-02-20", namespace="urn:custom"),))
    assert ns_profile.profile_id != base_id

    # 3. Extra module
    extra_m_profile = SchemaProfile(modules=(base_m, ModuleRef(name="ietf-ip", revision="2014-06-16")))
    assert extra_m_profile.profile_id != base_id

    # 4. Feature added
    feat_profile = SchemaProfile(modules=(base_m,), features=(FeatureRef(module="ietf-interfaces", name="arbitrary"),))
    assert feat_profile.profile_id != base_id

    # 5. Deviation added
    dev_profile = SchemaProfile(modules=(base_m,), deviations=(DeviationRef(name="custom-dev", revision="2021-01-01"),))
    assert dev_profile.profile_id != base_id

    # 6. Datastore schema added
    ds_profile = SchemaProfile(modules=(base_m,), datastore_schemas=("operational",))
    assert ds_profile.profile_id != base_id

    # 7. Content ID change
    cid_profile = SchemaProfile(modules=(base_m,), content_id="diff-id")
    assert cid_profile.profile_id != base_id


def test_schema_profile_host_independence():
    profile = SchemaProfile(modules=(ModuleRef(name="ietf-interfaces", revision="2018-02-20"),))

    conn1 = ConnectionConfig(target="192.0.2.1:830", username="admin", password="pwd")
    conn2 = ConnectionConfig(target="198.51.100.2:830", username="netops", password="pwd")

    sess1 = SessionConfig(connection=conn1, protocol=Protocol.NETCONF, schema=SchemaConfig(profile=profile.profile_id))
    sess2 = SessionConfig(connection=conn2, protocol=Protocol.NETCONF, schema=SchemaConfig(profile=profile.profile_id))

    # Profiles and their IDs are strictly decoupled from connection endpoints
    assert sess1.schema.profile == sess2.schema.profile == profile.profile_id


def test_session_config_schema_integration(tmp_path):
    conn = ConnectionConfig(target="127.0.0.1:830")
    
    # Default schema configuration
    sess_default = SessionConfig(connection=conn, protocol=Protocol.NETCONF)
    assert isinstance(sess_default.schema, SchemaConfig)
    assert sess_default.yang_dirs == ()
    assert sess_default.yang_cache_dir is None
    assert sess_default.enable_schema_validation is False

    # Explicit schema configuration
    ydir = tmp_path / "models"
    ydir.mkdir()
    s_cfg = SchemaConfig(yang_dirs=(ydir,), validate=True)
    sess_custom = SessionConfig(connection=conn, protocol=Protocol.NETCONF, schema=s_cfg)
    assert sess_custom.yang_dirs == (ydir.resolve(),)
    assert sess_custom.enable_schema_validation is True

    # Dict schema input in SessionConfig
    sess_dict = SessionConfig(connection=conn, protocol=Protocol.NETCONF, schema={"yang_dirs": [str(ydir)], "validate": True})
    assert sess_dict.yang_dirs == (ydir.resolve(),)
    assert sess_dict.enable_schema_validation is True
