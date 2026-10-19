import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure project root directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ui.cmd import parse_args
from config.model import SessionConfig
from config.operations import SetOperation
from specs.client import NetconfClient
from modules.formatter import NETCONFFormatter


class TestNetconfEditConfig(unittest.TestCase):

    def setUp(self):
        self.mock_session = MagicMock()
        self.mock_session.server_capabilities = [
            'urn:ietf:params:netconf:base:1.0',
            'urn:ietf:params:netconf:capability:writable-running:1.0',
        ]
        self.client = NetconfClient(
            target="10.1.11.101:830",
            username="admin",
            password="pwd"
        )
        self.client.session = self.mock_session

    def test_edit_config_candidate_fallback(self):
        """Verify fallback from candidate to running when server lacks :candidate capability."""
        # Server only has base 1.0, no :candidate
        self.mock_session.server_capabilities = ['urn:ietf:params:netconf:base:1.0']
        self.client.set(config="<test/>", target_datastore="candidate")

        call_kwargs = self.mock_session.edit_config.call_args[1]
        self.assertEqual(call_kwargs['target'], 'running')

    def test_edit_config_candidate_retained_when_supported(self):
        """Verify candidate target is retained when server supports :candidate."""
        self.mock_session.server_capabilities = [
            'urn:ietf:params:netconf:base:1.0',
            'urn:ietf:params:netconf:capability:candidate:1.0'
        ]
        self.client.set(config="<test/>", target_datastore="candidate")

        call_kwargs = self.mock_session.edit_config.call_args[1]
        self.assertEqual(call_kwargs['target'], 'candidate')

    def test_formatter_ok_response(self):
        """Verify NETCONFFormatter formats <ok/> reply into JSON {'ok': True}."""
        formatter = NETCONFFormatter()
        mock_reply = MagicMock()
        mock_reply.data_xml = None
        mock_reply.xml = '<?xml version="1.0" encoding="UTF-8"?><rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><ok/></rpc-reply>'
        
        with patch('modules.formatter.xmltodict') as mock_xmltodict:
            mock_xmltodict.parse.return_value = {'rpc-reply': {'ok': None}}
            result = formatter.format_json(mock_reply, rpc="edit-config")
            self.assertEqual(result['data'], {"ok": True})
            self.assertIsNone(result['error'])

    def test_cli_parser_netconf_edit_config(self):
        """Verify CLI parsing produces SessionConfig with SetOperation."""
        parsed = parse_args([
            "-t", "10.1.11.101:830",
            "--username", "admin",
            "--password", "pwd",
            "netconf", "edit-config",
            "--target", "running",
            "--config", "<test/>",
            "--default-operation", "replace",
            "--error-option", "rollback-on-error"
        ])
        self.assertEqual(len(parsed.sessions), 1)
        cfg = parsed.sessions[0]
        self.assertIsInstance(cfg, SessionConfig)
        self.assertIsInstance(cfg.operation, SetOperation)
        self.assertEqual(cfg.target, "10.1.11.101:830")
        self.assertEqual(cfg.target_datastore, "running")
        self.assertEqual(cfg.config, "<test/>")
        self.assertEqual(cfg.default_operation, "replace")
        self.assertEqual(cfg.error_option, "rollback-on-error")


if __name__ == "__main__":
    unittest.main()
