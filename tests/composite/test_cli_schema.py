import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ui.cmd import build_args, parse_args, FileConfigBuilder, CLIConfigBuilder
from config import Protocol
from config.schema import SchemaConfig


class TestCLISchema(unittest.TestCase):

    def test_cli_netconf_root_schema_args(self):
        args = [
            "-t", "10.0.0.1:830",
            "-u", "admin",
            "-p", "admin",
            "--yang-dir", "/tmp/models1",
            "--yang-dir", "/tmp/models2",
            "--yang-cache-dir", "/tmp/yang_cache",
            "--validate-schema",
            "--no-auto-discovery",
            "--no-auto-fetch",
            "--schema-profile", "test-profile-123",
            "netconf",
            "get",
        ]
        parsed = parse_args(args)
        self.assertEqual(len(parsed.sessions), 1)
        sess = parsed.sessions[0]
        self.assertEqual(sess.protocol, Protocol.NETCONF)
        self.assertIsNotNone(sess.schema)
        self.assertEqual(sess.schema.yang_dirs, (Path("/tmp/models1"), Path("/tmp/models2")))
        self.assertEqual(sess.schema.cache_dir, Path("/tmp/yang_cache"))
        self.assertTrue(sess.schema.validate)
        self.assertFalse(sess.schema.auto_discovery)
        self.assertFalse(sess.schema.auto_fetch)
        self.assertEqual(sess.schema.profile, "test-profile-123")

    def test_cli_netconf_subparser_schema_args(self):
        args = [
            "-t", "10.0.0.1:830",
            "netconf",
            "edit-config",
            "-C", "<config/>",
            "--yang-dir", "/tmp/nc_yang",
            "--validate-schema",
        ]
        parsed = parse_args(args)
        sess = parsed.sessions[0]
        self.assertIn(Path("/tmp/nc_yang"), sess.schema.yang_dirs)
        self.assertTrue(sess.schema.validate)
        self.assertTrue(sess.schema.auto_discovery)
        self.assertTrue(sess.schema.auto_fetch)

    def test_cli_gnmi_schema_args(self):
        args = [
            "-t", "10.0.0.2:10161",
            "gnmi",
            "--yang-dir", "/tmp/gnmi_yang",
            "--validate-schema",
            "get",
            "--path", "/interfaces/interface[name=eth0]",
        ]
        parsed = parse_args(args)
        self.assertEqual(len(parsed.sessions), 1)
        sess = parsed.sessions[0]
        self.assertEqual(sess.protocol, Protocol.GNMI)
        self.assertIn(Path("/tmp/gnmi_yang"), sess.schema.yang_dirs)
        self.assertTrue(sess.schema.validate)

    def test_cli_default_schema_config(self):
        args = [
            "-t", "10.0.0.3:10161",
            "gnmi",
            "capability",
        ]
        parsed = parse_args(args)
        sess = parsed.sessions[0]
        self.assertIsNotNone(sess.schema)
        self.assertEqual(sess.schema.yang_dirs, ())
        self.assertIsNone(sess.schema.cache_dir)
        self.assertFalse(sess.schema.validate)
        self.assertTrue(sess.schema.auto_discovery)
        self.assertTrue(sess.schema.auto_fetch)
        self.assertIsNone(sess.schema.profile)

    def test_yaml_global_schema_config(self):
        yaml_content = """
protocol: "netconf"
schema:
  yang_dirs:
    - "/etc/yang/ietf"
    - "/etc/yang/openconfig"
  cache_dir: "/var/cache/yang"
  validate: true
  auto_discovery: false
  auto_fetch: false
  profile: "juniper-profile"
targets:
  192.168.1.1:830:
    username: "admin"
    password: "pwd"
    operations:
      - capability: {}
"""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = f.name

        try:
            builder = FileConfigBuilder(yaml_path, protocol="netconf")
            parsed = builder.build()
            self.assertEqual(len(parsed.sessions), 1)
            sess = parsed.sessions[0]
            self.assertEqual(sess.schema.yang_dirs, (Path("/etc/yang/ietf"), Path("/etc/yang/openconfig")))
            self.assertEqual(sess.schema.cache_dir, Path("/var/cache/yang"))
            self.assertTrue(sess.schema.validate)
            self.assertFalse(sess.schema.auto_discovery)
            self.assertFalse(sess.schema.auto_fetch)
            self.assertEqual(sess.schema.profile, "juniper-profile")
        finally:
            if os.path.exists(yaml_path):
                os.remove(yaml_path)

    def test_yaml_target_schema_override(self):
        yaml_content = """
protocol: "gnmi"
schema:
  validate: false
  yang_dirs:
    - "/global/yang"
targets:
  10.1.1.1:57400:
    operations:
      - capability: {}
  10.2.2.2:57400:
    schema:
      validate: true
      yang_dirs:
        - "/target2/yang"
      profile: "target2-spec"
    operations:
      - capability: {}
"""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = f.name

        try:
            builder = FileConfigBuilder(yaml_path, protocol="gnmi")
            parsed = builder.build()
            self.assertEqual(len(parsed.sessions), 2)
            sess1, sess2 = parsed.sessions[0], parsed.sessions[1]

            # sess1 inherits global schema
            self.assertFalse(sess1.schema.validate)
            self.assertEqual(sess1.schema.yang_dirs, (Path("/global/yang"),))
            self.assertIsNone(sess1.schema.profile)

            # sess2 overrides schema
            self.assertTrue(sess2.schema.validate)
            self.assertEqual(sess2.schema.yang_dirs, (Path("/target2/yang"),))
            self.assertEqual(sess2.schema.profile, "target2-spec")
        finally:
            if os.path.exists(yaml_path):
                os.remove(yaml_path)

    def test_yaml_flat_fallback_schema_fields(self):
        yaml_content = """
protocol: "netconf"
yang_dir: "/flat/yang"
yang_cache_dir: "/flat/cache"
validate: true
targets:
  10.3.3.3:830:
    operations:
      - capability: {}
"""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = f.name

        try:
            builder = FileConfigBuilder(yaml_path, protocol="netconf")
            parsed = builder.build()
            sess = parsed.sessions[0]
            self.assertEqual(sess.schema.yang_dirs, (Path("/flat/yang"),))
            self.assertEqual(sess.schema.cache_dir, Path("/flat/cache"))
            self.assertTrue(sess.schema.validate)
        finally:
            if os.path.exists(yaml_path):
                os.remove(yaml_path)


if __name__ == "__main__":
    unittest.main()

