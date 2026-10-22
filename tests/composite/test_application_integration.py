# -*- coding: utf-8 -*-
from unittest.mock import MagicMock, patch
import pytest

from ui.cmd import (
    FileConfigBuilder,
    ParsedConfig,
    session_to_execution_request,
    execution_request_to_session,
)
from ui.runner import CLIApplicationRunner, CLIStreamRunner
from managers.manager import ManagerFactory, UnaryManager, SubscriptionManager
from application.operations import DeviceOperationService
from application.devices import DeviceRegistryService
from application.dto import (
    AdhocEndpointDTO,
    ExecutionRequestDTO,
    ExecutionResultDTO,
    OperationStepDTO,
    TelemetryPolicyDTO,
    DeliveryModeDTO,
)
from config.model import Protocol, OutputConfig, OutputType, OutputFormat


class TestApplicationLayerIntegration:
    def test_gnmi_yaml_to_dto_roundtrip(self):
        builder = FileConfigBuilder(
            "tests/fixtures/gnmi_get_test.yaml",
            protocol="gnmi",
        )
        parsed = builder.build()
        assert len(parsed.sessions) == 1

        session = parsed.sessions[0]
        assert session.protocol == Protocol.GNMI

        # Convert to DTO
        dto = session_to_execution_request(session)
        assert isinstance(dto, ExecutionRequestDTO)
        assert dto.protocol == "gnmi"
        assert dto.adhoc_endpoint.target == "172.20.20.2:57401"
        assert dto.adhoc_endpoint.username == "admin"
        assert len(dto.operations) == 1
        assert dto.operations[0].op_type == "get"
        assert len(dto.operations[0].paths) == 2

        # Convert back to SessionConfig
        reconstructed = execution_request_to_session(dto)
        assert reconstructed.protocol == Protocol.GNMI
        assert reconstructed.target == "172.20.20.2:57401"
        assert len(reconstructed.operations) == 1
        assert list(reconstructed.operations[0].selector.paths) == list(dto.operations[0].paths)

    def test_netconf_transaction_yaml_to_dto_roundtrip(self):
        builder = FileConfigBuilder(
            "tests/fixtures/netconf_transaction_pipeline_test.yaml",
            protocol="netconf",
        )
        parsed = builder.build()
        assert len(parsed.sessions) == 1

        session = parsed.sessions[0]
        assert session.protocol == Protocol.NETCONF
        assert len(session.operations) == 5

        dto = session_to_execution_request(session)
        assert isinstance(dto, ExecutionRequestDTO)
        assert dto.protocol == "netconf"
        assert dto.adhoc_endpoint.target == "172.20.20.2:830"
        assert len(dto.operations) == 5

        # Verify step types
        assert dto.operations[0].op_type == "set"
        assert dto.operations[1].op_type == "validate"
        assert dto.operations[2].op_type == "commit"
        assert dto.operations[3].op_type == "get_config"
        assert dto.operations[4].op_type == "set"

        # Reconstructed
        reconstructed = execution_request_to_session(dto)
        assert reconstructed.protocol == Protocol.NETCONF
        assert len(reconstructed.operations) == 5

    @patch("application.operations.SequentialWorker.start")
    def test_end_to_end_pipeline_execution(self, mock_start):
        mock_start.return_value = [
            {"rpc": "lock", "data": "<ok/>", "session_id": "s1"},
            {"rpc": "edit-config", "data": "<ok/>", "session_id": "s1"},
            {"rpc": "validate", "data": "<ok/>", "session_id": "s1"},
            {"rpc": "commit", "data": "<ok/>", "session_id": "s1"},
            {"rpc": "unlock", "data": "<ok/>", "session_id": "s1"},
        ]

        builder = FileConfigBuilder(
            "tests/fixtures/netconf_transaction_pipeline_test.yaml",
            protocol="netconf",
        )
        parsed = builder.build()
        dto = session_to_execution_request(parsed.sessions[0])

        svc = DeviceOperationService()
        result = svc.execute_unary(dto)

        assert isinstance(result, ExecutionResultDTO)
        assert result.success is True
        assert result.protocol == "netconf"
        assert len(result.results) == 5
        assert all(r.success for r in result.results)
        assert result.results[3].op_type == "commit"

    def test_parsed_config_dual_property_synchronization(self):
        # 1. Constructing ParsedConfig with requests automatically populates sessions
        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="192.168.1.10:57400", protocol="gnmi"),
            operations=[OperationStepDTO(op_type="get", paths=["/system/state/hostname"])],
        )
        cfg_from_req = ParsedConfig(requests=[req])
        assert len(cfg_from_req.sessions) == 1
        assert cfg_from_req.sessions[0].target == "192.168.1.10:57400"
        assert cfg_from_req.sessions[0].protocol == Protocol.GNMI

        # 2. Constructing ParsedConfig with sessions automatically populates requests
        cfg_from_sess = ParsedConfig(sessions=cfg_from_req.sessions)
        assert len(cfg_from_sess.requests) == 1
        assert cfg_from_sess.requests[0].adhoc_endpoint.target == "192.168.1.10:57400"
        assert cfg_from_sess.requests[0].protocol == "gnmi"

    def test_yaml_devices_inventory_with_device_registry(self, tmp_path):
        yaml_content = """
global:
  protocol: gnmi

devices:
  spine1:
    target: "10.10.10.1:57400"
    protocol: gnmi
    username: admin
    password: secret
    insecure: true

targets:
  spine1:
    operations:
      - get:
          paths:
            - /system/state/hostname
"""
        cfg_file = tmp_path / "devices_test.yaml"
        cfg_file.write_text(yaml_content)

        builder = FileConfigBuilder(str(cfg_file), protocol="gnmi")
        parsed = builder.build()

        assert parsed.device_registry is not None
        assert parsed.device_registry.has_device("spine1")
        assert len(parsed.requests) == 1
        assert parsed.requests[0].device_id == "spine1"
        assert parsed.sessions[0].target == "10.10.10.1:57400"
        assert parsed.sessions[0].username == "admin"

    @patch("ui.runner.OutputHandler")
    @patch("managers.manager.OutputHandler")
    @patch.object(DeviceOperationService, "_resolve_worker")
    def test_cli_application_runner_and_manager_factory_delegation(
        self, mock_resolve, mock_mgr_oh, mock_runner_oh
    ):
        mock_worker = MagicMock()
        mock_worker.start.return_value = {
            "session_id": "s1",
            "target": "172.20.20.2:57401",
            "rpc": "get",
            "data": {"hostname": "srl1"},
            "protocol": "gnmi",
        }
        mock_resolve.return_value = mock_worker

        mock_handler_instance = MagicMock()
        mock_runner_oh.return_value = mock_handler_instance
        mock_mgr_oh.return_value = mock_handler_instance

        builder = FileConfigBuilder(
            "tests/fixtures/gnmi_get_test.yaml",
            protocol="gnmi",
        )
        cfg = builder.build()

        # Execute via CLIApplicationRunner (used by client_main.py)
        results = CLIApplicationRunner.execute(cfg)
        assert len(results) == 1
        assert results[0].success is True
        assert results[0].target == "172.20.20.2:57401"
        mock_handler_instance.write.assert_called_once()
        mock_handler_instance.close.assert_called_once()

        # Execute via legacy ManagerFactory.execute (delegates to DeviceOperationService.execute_batch)
        ManagerFactory.execute(cfg)
        assert mock_worker.start.call_count == 2
        assert mock_handler_instance.write.call_count == 2
        assert mock_handler_instance.close.call_count == 2

    @patch("application.subscriptions.SubscribeSession")
    def test_cli_stream_runner_and_subscription_manager_delegation(self, mock_sub_cls):
        def make_worker(config, data_queue, subscription_name="sub"):
            w = MagicMock()
            w.is_running = False
            w.is_poll_mode.return_value = False

            def fake_start():
                data_queue.put({
                    "session_id": "sub-1",
                    "target": config.target,
                    "protocol": config.protocol.value,
                    "subscription_name": subscription_name,
                    "data": {"interfaces": "up"},
                    "timestamp": 100.0,
                    "is_sync_marker": False,
                })

            w.start.side_effect = fake_start
            return w

        mock_sub_cls.side_effect = make_worker

        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.5:57400", protocol="gnmi"),
            operations=[
                OperationStepDTO(
                    op_type="subscribe",
                    paths=["/interfaces"],
                    telemetry=TelemetryPolicyDTO(mode=DeliveryModeDTO.PERIODIC, sample_interval_sec=5.0),
                    options={"subscription_name": "if_sub"},
                )
            ],
        )

        mock_handler = MagicMock()
        runner = CLIStreamRunner()
        sub_ids = runner.run([req], output_handlers=[mock_handler])

        assert len(sub_ids) == 1
        mock_handler.write.assert_called_once()
        written_msg = mock_handler.write.call_args[0][0]
        assert written_msg["target"] == "10.0.0.5:57400"
        assert written_msg["subscription_name"] == "if_sub"

