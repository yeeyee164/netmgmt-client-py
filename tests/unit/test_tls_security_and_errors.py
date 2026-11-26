# -*- encoding: utf-8 -*-
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from application.dto import AdhocEndpointDTO, ExecutionRequestDTO, OperationStepDTO
from application.operation_mapper import OperationRequestMapper
from application.sessions import DeviceSessionManager
from cli.adapters import session_to_execution_request
from cli.file_builder import FileConfigBuilder
from config.model import ConnectionConfig, OutputConfig, OutputFormat, OutputType, Protocol, SecurityProfile, SessionConfig
from config.operations import CapabilitiesOperation
from modules.formatter import GNMIFormatter, NETCONFFormatter
from modules.output import OutputHandler
from modules.security import SecurityModule
from specs.gnmi_client import GNMIClient

pytestmark = pytest.mark.unit


class TestSecurityModuleSSHAndSkipVerify:
    def test_get_ssh_kwargs_uses_ssh_key_without_tls_client_key(self):
        profile = SecurityProfile(ssh_key="/path/to/id_rsa")
        sec = SecurityModule(profile)
        assert sec.get_ssh_kwargs() == {"key_filename": "/path/to/id_rsa"}

    def test_skip_verify_does_not_force_insecure_in_operation_mapper(self):
        ep = AdhocEndpointDTO(
            target="10.0.0.1:57400",
            protocol="gnmi",
            skip_verify=True,
            insecure=False,
        )
        req = ExecutionRequestDTO(
            adhoc_endpoint=ep,
            operations=[OperationStepDTO(op_type="capabilities")],
        )
        cfg = OperationRequestMapper.build_session_config(req, ep)
        assert cfg.connection.insecure is False
        assert cfg.connection.security.skip_verify is True

    def test_skip_verify_does_not_force_insecure_in_session_manager(self):
        mock_client = MagicMock()
        mock_factory = MagicMock(return_value=mock_client)
        mgr = DeviceSessionManager(client_factory=mock_factory)

        ep = AdhocEndpointDTO(
            target="10.0.0.1:57400",
            protocol="gnmi",
            skip_verify=True,
            insecure=False,
        )
        info = mgr.create_session(owner_id="op-a", adhoc_endpoint=ep)
        try:
            assert mock_factory.call_args.kwargs["insecure"] is False
        finally:
            mgr.close_session(info.session_id, owner_id="op-a")

    def test_insecure_cli_session_does_not_set_skip_verify_true(self):
        sess = SessionConfig(
            connection=ConnectionConfig(target="10.0.0.1:57401", insecure=True),
            protocol=Protocol.GNMI,
            operation=CapabilitiesOperation(),
        )
        req = session_to_execution_request(sess)
        assert req.adhoc_endpoint.insecure is True
        assert req.adhoc_endpoint.skip_verify is False

    @patch.object(SecurityModule, "_fetch_peer_tls_info", return_value=(b"PEER_PEM", "peer.lab"))
    @patch("modules.security.grpc.ssl_channel_credentials")
    def test_skip_verify_uses_peer_cert_and_server_name_override(self, mock_ssl_creds, mock_fetch):
        profile = SecurityProfile(skip_verify=True)
        sec = SecurityModule(profile)
        sec.get_grpc_credentials(target="10.0.0.1:57400")
        opts = sec.get_grpc_options(target="10.0.0.1:57400")

        mock_fetch.assert_called_with("10.0.0.1:57400")
        mock_ssl_creds.assert_called_once_with(
            root_certificates=b"PEER_PEM",
            private_key=None,
            certificate_chain=None,
        )
        assert ("grpc.ssl_target_name_override", "peer.lab") in (opts or [])

    @patch.object(SecurityModule, "_fetch_peer_tls_info", return_value=(b"PEER_PEM", "peer.lab"))
    @patch("modules.security.grpc.ssl_channel_credentials")
    def test_skip_verify_overrides_ca_and_tls_server_name(self, mock_ssl_creds, mock_fetch, tmp_path: Path):
        stale_ca = tmp_path / "stale_ca.crt"
        stale_ca.write_bytes(b"STALE_CA_PEM")
        profile = SecurityProfile(
            tls_ca=str(stale_ca),
            tls_server_name="wrong.host",
            skip_verify=True,
        )
        sec = SecurityModule(profile)
        sec.get_grpc_credentials(target="10.0.0.1:57400")
        opts = sec.get_grpc_options(target="10.0.0.1:57400")

        mock_fetch.assert_called_with("10.0.0.1:57400")
        mock_ssl_creds.assert_called_once_with(
            root_certificates=b"PEER_PEM",
            private_key=None,
            certificate_chain=None,
        )
        assert opts == [("grpc.ssl_target_name_override", "peer.lab")]

    def test_gnmi_client_omits_empty_credentials_metadata(self):
        client = GNMIClient(target="10.0.0.1:57400", username="", password="", insecure=True)
        assert client.metadata == []


class TestFileConfigBuilderSecurity:
    def test_file_builder_preserves_ssh_key_and_per_target_security(self, tmp_path: Path):
        cfg_file = tmp_path / "pipeline.yaml"
        cfg_file.write_text(
            """
protocol: netconf
global:
  username: admin
  security:
    ssh_key: certs/global_key
    tls_ca: certs/ca.crt
targets:
  10.0.0.1:830:
    operation: capability
  10.0.0.2:830:
    operation: capability
    security:
      ssh_key: certs/target_key
      skip_verify: true
outputs:
  out:
    output-type: stdout
    format: json
""",
            encoding="utf-8",
        )
        parsed = FileConfigBuilder(str(cfg_file), protocol="netconf").build()
        assert parsed.sessions[0].connection.security.ssh_key == "certs/global_key"
        assert parsed.sessions[1].connection.security.ssh_key == "certs/target_key"
        assert parsed.sessions[1].connection.security.skip_verify is True
        assert parsed.sessions[1].connection.security.tls_ca == "certs/ca.crt"

    def test_file_builder_inherits_per_device_security_from_devices_block(self, tmp_path: Path):
        cfg_file = tmp_path / "devices_tls.yaml"
        cfg_file.write_text(
            """
protocol: gnmi
devices:
  srl_tls:
    target: 172.20.20.2:57400
    protocol: gnmi
    security:
      tls_ca: certs/ca.crt
      tls_cert: certs/client_srl.crt
      tls_key: certs/client_srl.key
      tls_server_name: srl.nb-lab.io
  ceos_tls:
    target: 172.20.20.3:6031
    protocol: gnmi
    security:
      tls_ca: certs/ca.crt
      tls_cert: certs/client_ceos.crt
      tls_key: certs/client_ceos.key
      tls_server_name: ceos
targets:
  srl_tls:
    operations:
      - get:
          path: [/system/name/host-name]
  ceos_tls:
    operations:
      - get:
          path: [/system/state/hostname]
""",
            encoding="utf-8",
        )
        parsed = FileConfigBuilder(str(cfg_file), protocol="gnmi").build()
        assert parsed.sessions[0].connection.security.tls_cert == "certs/client_srl.crt"
        assert parsed.sessions[0].connection.security.tls_server_name == "srl.nb-lab.io"
        assert parsed.sessions[1].connection.security.tls_cert == "certs/client_ceos.crt"
        assert parsed.sessions[1].connection.security.tls_server_name == "ceos"


class TestOutputAndFormatterErrorHandling:
    def test_gnmi_formatter_handles_error_strings_without_attribute_error(self):
        fmt = GNMIFormatter()
        for rpc in ("capability", "get", "set", "subscribe"):
            res = fmt.format_json("UNAUTHENTICATED: bad cert", rpc=rpc, meta={"source": "10.0.0.1:57400"})
            assert res["error"] == "UNAUTHENTICATED: bad cert"
            assert res["source"] == "10.0.0.1:57400"

    def test_output_handler_writes_structured_error_for_gnmi_and_netconf(self):
        for proto, rpc in (("gnmi", "capability"), ("gnmi", "get"), ("netconf", "capability")):
            out_cfg = OutputConfig(output_type=OutputType.STDOUT, format=OutputFormat.JSON)
            handler = OutputHandler(out_cfg)
            buf = io.StringIO()
            handler.stream = buf

            handler.write({
                "target": "10.0.0.1:57400",
                "protocol": proto,
                "rpc": rpc,
                "data": "UNAUTHENTICATED: failed",
                "error": "UNAUTHENTICATED: failed",
            })
            payload = json.loads(buf.getvalue())
            assert payload["error"] == "UNAUTHENTICATED: failed"
            assert payload["data"] is None
            assert payload["source"] == "10.0.0.1:57400"

    def test_output_handler_text_format_displays_error_message(self):
        out_cfg = OutputConfig(output_type=OutputType.STDOUT, format=OutputFormat.TEXT)
        handler = OutputHandler(out_cfg)
        buf = io.StringIO()
        handler.stream = buf

        handler.write({
            "target": "10.0.0.1:57400",
            "protocol": "gnmi",
            "rpc": "get",
            "data": "UNAUTHENTICATED: failed",
            "error": "UNAUTHENTICATED: failed",
        })
        assert "Error: UNAUTHENTICATED: failed" in buf.getvalue()

    def test_cli_runner_records_stream_errors_as_failed_execution_result(self):
        from cli.runner import CLIApplicationRunner
        from cli.models import ParsedConfig

        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:57400", protocol="gnmi"),
            operations=[OperationStepDTO(op_type="subscribe", paths=["/system"])],
        )
        cfg = ParsedConfig(requests=[req], outputs=[])

        with patch("cli.runner.CLIStreamRunner.run") as mock_run:
            def fake_run(requests, output_handlers=None):
                pass
            mock_run.side_effect = fake_run
            with patch.object(
                __import__("cli.runner", fromlist=["CLIStreamRunner"]).CLIStreamRunner,
                "__init__",
                lambda self, **kw: (
                    setattr(self, "subscription_service", MagicMock())
                    or setattr(self, "data_queue", MagicMock())
                    or setattr(self, "stream_errors", [{"target": "10.0.0.1:57400", "protocol": "gnmi", "error": "UNAUTHENTICATED"}])
                ),
            ):
                results = CLIApplicationRunner.execute(cfg)

        assert len(results) == 1
        assert results[0].success is False
        assert "UNAUTHENTICATED" in (results[0].error_summary or "")
