import os
import sys
import json
import unittest
from unittest.mock import MagicMock, patch

# Ensure project root directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from specs.gnmi import gnmi_pb2
from specs.client import GNMIClient
from managers.unary_worker import SetWorker
from modules.validate import GNMIValidator, PathValidationError
from modules.formatter import GNMIFormatter
from ui.cmd import build_args, CLIConfigBuilder, SessionConfig

from config import (
    SetOperation,
    ConnectionConfig,
    Protocol,
    Change,
    ChangeType,
)


class TestGNMITypedValueAndSetRequest(unittest.TestCase):
    def setUp(self):
        self.client = GNMIClient(target="127.0.0.1:8080", insecure=True)

    def test_build_typed_val_boolean(self):
        tv_true = self.client._build_typed_val(True)
        self.assertTrue(tv_true.bool_val)
        self.assertFalse(tv_true.HasField("int_val"))

        tv_false = self.client._build_typed_val(False)
        self.assertFalse(tv_false.bool_val)

        # String boolean
        tv_str_true = self.client._build_typed_val("true")
        self.assertTrue(tv_str_true.bool_val)

        tv_str_false = self.client._build_typed_val("false")
        self.assertFalse(tv_str_false.bool_val)

    def test_build_typed_val_integer_and_float(self):
        tv_pos = self.client._build_typed_val(100)
        self.assertEqual(tv_pos.uint_val, 100)

        tv_neg = self.client._build_typed_val(-50)
        self.assertEqual(tv_neg.int_val, -50)

        tv_float = self.client._build_typed_val(3.14)
        self.assertAlmostEqual(tv_float.float_val, 3.14, places=2)

    def test_build_typed_val_dict_and_list(self):
        data = {"description": "Uplink", "enabled": True}
        tv = self.client._build_typed_val(data, encoding="json_ietf")
        self.assertTrue(tv.HasField("json_ietf_val"))
        decoded = json.loads(tv.json_ietf_val.decode('utf-8'))
        self.assertEqual(decoded, data)

        # json encoding
        tv_json = self.client._build_typed_val(data, encoding="json")
        self.assertTrue(tv_json.HasField("json_val"))

    def test_build_typed_val_inline_json_string(self):
        json_str = '{"description": "Transit-Link", "enabled": true}'
        tv = self.client._build_typed_val(json_str, encoding="json_ietf")
        self.assertTrue(tv.HasField("json_ietf_val"))
        self.assertEqual(json.loads(tv.json_ietf_val.decode('utf-8'))["description"], "Transit-Link")

    def test_build_typed_val_file_reference(self):
        test_payload = {"name": "Ethernet8", "description": "From-File"}
        tmp_file = "/tmp/test_gnmi_payload.json"
        with open(tmp_file, "w") as f:
            json.dump(test_payload, f)

        try:
            # Test with @ prefix
            tv_at = self.client._build_typed_val(f"@{tmp_file}")
            self.assertTrue(tv_at.HasField("json_ietf_val"))
            self.assertEqual(json.loads(tv_at.json_ietf_val.decode('utf-8')), test_payload)

            # Test direct file path
            tv_direct = self.client._build_typed_val(tmp_file)
            self.assertTrue(tv_direct.HasField("json_ietf_val"))
        finally:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)

    def test_build_typed_val_explicit_types(self):
        # Explicit uint32
        tv_uint = self.client._build_typed_val(42, val_type="uint32")
        self.assertEqual(tv_uint.uint_val, 42)

        # Explicit int64
        tv_int = self.client._build_typed_val(-999, val_type="int64")
        self.assertEqual(tv_int.int_val, -999)

        # Explicit string
        tv_str = self.client._build_typed_val(12345, val_type="string")
        self.assertEqual(tv_str.string_val, "12345")

        # Explicit bool
        tv_bool = self.client._build_typed_val("true", val_type="bool")
        self.assertTrue(tv_bool.bool_val)

        # Explicit json_ietf
        tv_json = self.client._build_typed_val({"mtu": 1500}, val_type="json_ietf")
        self.assertTrue(tv_json.HasField("json_ietf_val"))
        self.assertEqual(json.loads(tv_json.json_ietf_val.decode('utf-8'))["mtu"], 1500)

    def test_execute_set_typed_values(self):
        op = SetOperation(
            changes=(
                Change(path="/interfaces/interface[name=mgmt0]/config/description", operation=ChangeType.MERGE, value="desc", type="string"),
                Change(path="/interfaces/interface[name=mgmt0]/config/mtu", operation=ChangeType.REPLACE, value=1500, type="uint32"),
                Change(path="/interfaces/interface[name=mgmt0]/config/enabled", operation=ChangeType.DELETE),
            )
        )
        with patch.object(self.client, 'set', return_value="SET_RESPONSE_MOCK") as mock_set:
            res = self.client.execute_set(op)
            self.assertEqual(res, "SET_RESPONSE_MOCK")
            mock_set.assert_called_once()
            _, kwargs = mock_set.call_args
            self.assertEqual(len(kwargs["updates"]), 1)
            self.assertEqual(kwargs["updates"][0], ("/interfaces/interface[name=mgmt0]/config/description", "desc", "string"))
            self.assertEqual(len(kwargs["replaces"]), 1)
            self.assertEqual(kwargs["replaces"][0], ("/interfaces/interface[name=mgmt0]/config/mtu", 1500, "uint32"))
            self.assertEqual(kwargs["deletes"], ["/interfaces/interface[name=mgmt0]/config/enabled"])

class TestGNMISetValidation(unittest.TestCase):
    def setUp(self):
        self.validator = GNMIValidator()

    def test_set_wildcards_rejected(self):
        # Wildcard in element name
        with self.assertRaises(PathValidationError):
            self.validator.validate_path("/interfaces/interface/*", operation="set")

        # Ellipsis in element name
        with self.assertRaises(PathValidationError):
            self.validator.validate_path("/interfaces/...", operation="set")

        # Wildcard in key value
        with self.assertRaises(PathValidationError):
            self.validator.validate_path('/interfaces/interface[name="*"]', operation="set")

    def test_set_valid_path_accepted(self):
        # Valid path should pass without exception
        self.validator.validate_path('/interfaces/interface[name="Ethernet4"]/config', operation="set")

    def test_origin_conflict_rejected(self):
        with self.assertRaises(PathValidationError):
            self.validator.validate_path(
                "other-origin:/interfaces/interface[name=\"Ethernet4\"]",
                operation="set",
                prefix="openconfig:/interfaces"
            )

    def test_worker_offline_validation_intercepts(self):
        # SetWorker should fail before opening connection if wildcard is present
        config = SessionConfig(
        connection=ConnectionConfig(
            target='10.0.0.3:9339'
        ),
        protocol=Protocol.GNMI,
        operation=SetOperation(
            changes=tuple([
                Change(path='/interfaces/interface[name=*]/config/description',
                operation=ChangeType.MERGE, value="Configured with asterisk")
                ])
            )
        )
        worker = SetWorker(config=config)
        res = worker.start()
        self.assertIn("data", res)
        self.assertIsInstance(res["data"], PathValidationError)


class TestGNMIFormatterSetResponse(unittest.TestCase):
    def test_format_set_response(self):
        formatter = GNMIFormatter()
        resp = gnmi_pb2.SetResponse()
        resp.timestamp = 1789532984256365745
        resp.prefix.elem.add(name="interfaces")

        # Add Delete result
        r_del = resp.response.add()
        r_del.path.elem.add(name="interface", key={"name": "Ethernet4"})
        r_del.op = gnmi_pb2.UpdateResult.DELETE

        # Add Replace result
        r_rep = resp.response.add()
        r_rep.path.elem.add(name="interface", key={"name": "Loopback1"})
        r_rep.op = gnmi_pb2.UpdateResult.REPLACE

        # Add Update result
        r_upd = resp.response.add()
        r_upd.path.elem.add(name="interface", key={"name": "Ethernet8"})
        r_upd.op = gnmi_pb2.UpdateResult.UPDATE

        meta = {"source": "172.25.78.145:8080"}
        formatted = formatter.format_json(resp, rpc="set", meta=meta)

        self.assertEqual(formatted["source"], "172.25.78.145:8080")
        self.assertEqual(formatted["prefix"], "interfaces")
        self.assertEqual(len(formatted["responses"]), 3)
        self.assertEqual(formatted["responses"][0]["op"], "DELETE")
        self.assertEqual(formatted["responses"][0]["path"], 'interface[name=Ethernet4]')
        self.assertEqual(formatted["responses"][1]["op"], "REPLACE")
        self.assertEqual(formatted["responses"][2]["op"], "UPDATE")
        self.assertIn("time", formatted)


class TestGNMISetCIParsing(unittest.TestCase):
    def test_cli_parsing_delimiter(self):
        args = MagicMock()
        args.times = 1
        args.target = ["172.25.78.145:8080"]
        args.protocol = "gnmi"
        args.operation = "set"
        args.username = "admin"
        args.password = "admin"
        args.insecure = True
        args.debug = False
        args.prefix = "/openconfig-interfaces:interfaces"
        args.encoding = "json_ietf"
        args.update = ['interface[name="Ethernet8"]/config:::{"description": "Uplink", "enabled": true}']
        args.replace = ['interface[name="Loopback1"]/config:::{"description": "Replaced"}']
        args.delete = ['interface[name="Ethernet4"]']
        args.updates = []
        args.replaces = []
        args.tls_ca = ""
        args.tls_cert = ""
        args.tls_key = ""
        args.skip_verify = False
        args.tls_server_name = ""
        args.tls_version = "1.3"
        args.ssh_key = ""
        args.log_level = "ERROR"
        args.syslog_server = ""
        args.log_file = ""

        builder = CLIConfigBuilder(args)
        parsed = builder.build()

        self.assertEqual(len(parsed.sessions), 1)
        session = parsed.sessions[0]
        self.assertIsInstance(session, SessionConfig)
        self.assertIsInstance(session.operation, SetOperation)
        self.assertEqual(session.prefix, "/openconfig-interfaces:interfaces")
        self.assertEqual(len(session.updates), 1)
        self.assertEqual(session.updates[0][0], 'interface[name="Ethernet8"]/config')
        self.assertEqual(session.updates[0][1], '{"description": "Uplink", "enabled": true}')
        self.assertEqual(session.replaces[0][0], 'interface[name="Loopback1"]/config')
        self.assertEqual(session.replaces[0][1], '{"description": "Replaced"}')
        self.assertEqual(session.deletes, ['interface[name="Ethernet4"]'])


class TestGNMISetPairedOptions(unittest.TestCase):
    def test_paired_update_value(self):
        args = build_args([
            "-t", "10.0.0.1:8080", "gnmi", "set",
            "--update-path", '/interfaces/interface[name="lo123"]/config/description',
            "--update-value", "Loopback123123"
        ])
        builder = CLIConfigBuilder(args)
        parsed = builder.build()
        self.assertEqual(len(parsed.sessions), 1)
        session = parsed.sessions[0]
        self.assertEqual(session.updates, [('/interfaces/interface[name="lo123"]/config/description', 'Loopback123123')])

    def test_paired_update_file(self):
        args = build_args([
            "-t", "10.0.0.1:8080", "gnmi", "set",
            "--update-path", '/interfaces/interface[name="lo123"]/config',
            "--update-file", "path/to/payload.json"
        ])
        builder = CLIConfigBuilder(args)
        parsed = builder.build()
        session = parsed.sessions[0]
        self.assertEqual(session.updates, [('/interfaces/interface[name="lo123"]/config', '@path/to/payload.json')])

    def test_paired_replace_value_and_file(self):
        args = build_args([
            "-t", "10.0.0.1:8080", "gnmi", "set",
            "--replace-path", '/interfaces/interface[name="lo123"]/config/description',
            "--replace-value", "ReplacedVal",
            "--replace-path", '/interfaces/interface[name="lo123"]/config',
            "--replace-file", "@/already/prefixed.json"
        ])
        builder = CLIConfigBuilder(args)
        parsed = builder.build()
        session = parsed.sessions[0]
        self.assertEqual(len(session.replaces), 2)
        self.assertEqual(session.replaces[0], ('/interfaces/interface[name="lo123"]/config/description', 'ReplacedVal'))
        self.assertEqual(session.replaces[1], ('/interfaces/interface[name="lo123"]/config', '@/already/prefixed.json'))

    def test_interleaved_mixed_updates(self):
        args = build_args([
            "-t", "10.0.0.1:8080", "gnmi", "set",
            "--update-path", "/path1", "--update-value", "val1",
            "--update-path", "/path2", "--update-file", "file2.json",
            "--update-path", "/path3", "--update-value", "val3"
        ])
        builder = CLIConfigBuilder(args)
        parsed = builder.build()
        session = parsed.sessions[0]
        self.assertEqual(session.updates, [
            ('/path1', 'val1'),
            ('/path2', '@file2.json'),
            ('/path3', 'val3')
        ])

    def test_combined_legacy_and_paired_options(self):
        args = build_args([
            "-t", "10.0.0.1:8080", "gnmi", "set",
            "--update", '/interfaces/interface[name="eth1"]/config:::{"description": "legacy"}',
            "--update-path", '/interfaces/interface[name="lo123"]/config/description',
            "--update-value", "Loopback123123",
            "--delete", '/interfaces/interface[name="eth2"]'
        ])
        builder = CLIConfigBuilder(args)
        parsed = builder.build()
        session = parsed.sessions[0]
        self.assertEqual(len(session.updates), 2)
        self.assertEqual(session.updates[0], ('/interfaces/interface[name="eth1"]/config', '{"description": "legacy"}'))
        self.assertEqual(session.updates[1], ('/interfaces/interface[name="lo123"]/config/description', 'Loopback123123'))
        self.assertEqual(session.deletes, ['/interfaces/interface[name="eth2"]'])

    def test_unpaired_update_path_error(self):
        args = build_args([
            "-t", "10.0.0.1:8080", "gnmi", "set",
            "--update-path", "/path1"
        ])
        builder = CLIConfigBuilder(args)
        with self.assertRaises(ValueError) as ctx:
            builder.build()
        self.assertIn("must be paired", str(ctx.exception))

    def test_consecutive_update_path_error(self):
        args = build_args([
            "-t", "10.0.0.1:8080", "gnmi", "set",
            "--update-path", "/path1",
            "--update-path", "/path2",
            "--update-value", "val2"
        ])
        builder = CLIConfigBuilder(args)
        with self.assertRaises(ValueError) as ctx:
            builder.build()
        self.assertIn("must be paired", str(ctx.exception))

    def test_update_value_without_path_error(self):
        args = build_args([
            "-t", "10.0.0.1:8080", "gnmi", "set",
            "--update-value", "val1"
        ])
        builder = CLIConfigBuilder(args)
        with self.assertRaises(ValueError) as ctx:
            builder.build()
        self.assertIn("must be preceded by --update-path", str(ctx.exception))

    def test_update_file_without_path_error(self):
        args = build_args([
            "-t", "10.0.0.1:8080", "gnmi", "set",
            "--update-file", "file.json"
        ])
        builder = CLIConfigBuilder(args)
        with self.assertRaises(ValueError) as ctx:
            builder.build()
        self.assertIn("must be preceded by --update-path", str(ctx.exception))

    def test_unpaired_replace_path_error(self):
        args = build_args([
            "-t", "10.0.0.1:8080", "gnmi", "set",
            "--replace-path", "/path1"
        ])
        builder = CLIConfigBuilder(args)
        with self.assertRaises(ValueError) as ctx:
            builder.build()
        self.assertIn("must be paired", str(ctx.exception))

    def test_replace_value_without_path_error(self):
        args = build_args([
            "-t", "10.0.0.1:8080", "gnmi", "set",
            "--replace-value", "val1"
        ])
        builder = CLIConfigBuilder(args)
        with self.assertRaises(ValueError) as ctx:
            builder.build()
        self.assertIn("must be preceded by --replace-path", str(ctx.exception))

    def test_empty_path_error(self):
        args = build_args([
            "-t", "10.0.0.1:8080", "gnmi", "set",
            "--update-path", "   ",
            "--update-value", "val"
        ])
        builder = CLIConfigBuilder(args)
        with self.assertRaises(ValueError) as ctx:
            builder.build()
        self.assertIn("cannot be empty", str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
