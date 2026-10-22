# -*- coding: utf-8 -*-
from unittest.mock import MagicMock, patch
import pytest

from application.operations import DeviceOperationService
from application.devices import DeviceRegistryService
from application.dto import (
    AdhocEndpointDTO,
    ExecutionRequestDTO,
    OperationStepDTO,
    MutationChangeDTO,
    TelemetryPolicyDTO,
    DeliveryModeDTO,
)
from application.exceptions import InvalidRequestError
from config.model import Protocol
from config.operations import (
    CapabilitiesOperation,
    GetOperation,
    SetOperation,
    TransactionOperation,
    SubscribeOperation,
    ChangeType,
)
from config.selectors import PathSelector, FilterSelector
from config.delivery import DeliveryMode


class TestDeviceOperationServiceBuildSessionConfig:
    def test_build_capabilities_session(self):
        svc = DeviceOperationService()
        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:57400", protocol="gnmi"),
            protocol="gnmi",
            operations=[OperationStepDTO(op_type="capabilities")],
        )
        session = svc.build_session_config(req)
        assert session.protocol == Protocol.GNMI
        assert session.target == "10.0.0.1:57400"
        assert len(session.operations) == 1
        assert isinstance(session.operations[0], CapabilitiesOperation)

    def test_build_get_session_with_path_selector(self):
        svc = DeviceOperationService()
        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:57400", protocol="gnmi"),
            operations=[
                OperationStepDTO(
                    op_type="get",
                    paths=["/system/state", "/interfaces/interface[name=mgmt0]"],
                    options={"encoding": "json_ietf"},
                )
            ],
        )
        session = svc.build_session_config(req)
        op = session.operations[0]
        assert isinstance(op, GetOperation)
        assert isinstance(op.selector, PathSelector)
        assert len(op.selector.paths) == 2
        assert op.protocol_options.encoding == "json_ietf"

    def test_build_get_config_with_filter_selector(self):
        svc = DeviceOperationService()
        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:830", protocol="netconf"),
            operations=[
                OperationStepDTO(
                    op_type="get_config",
                    filter_expression="<interfaces xmlns='urn:ietf:params:xml:ns:yang:ietf-interfaces'/>",
                    filter_type="subtree",
                    datastore="running",
                )
            ],
        )
        session = svc.build_session_config(req)
        op = session.operations[0]
        assert isinstance(op, GetOperation)
        assert isinstance(op.selector, FilterSelector)
        assert op.selector.filter_type == "subtree"
        assert op.protocol_options.source_datastore == "running"

    def test_build_set_session_with_mutations(self):
        svc = DeviceOperationService()
        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:830", protocol="netconf"),
            operations=[
                OperationStepDTO(
                    op_type="set",
                    datastore="candidate",
                    changes=[
                        MutationChangeDTO(path="/interfaces/interface[name=eth0]/config/enabled", value=True, operation="merge"),
                        MutationChangeDTO(path="/interfaces/interface[name=eth1]", operation="delete"),
                    ],
                    options={"lock": True, "commit": True},
                )
            ],
        )
        session = svc.build_session_config(req)
        op = session.operations[0]
        assert isinstance(op, SetOperation)
        assert len(op.changes) == 2
        assert op.changes[0].operation == ChangeType.MERGE
        assert op.changes[1].operation == ChangeType.DELETE
        assert op.protocol_options.lock_target is True
        assert op.protocol_options.commit is True

    def test_build_transaction_session(self):
        svc = DeviceOperationService()
        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:830", protocol="netconf"),
            operations=[
                OperationStepDTO(op_type="lock", datastore="candidate"),
                OperationStepDTO(op_type="commit", options={"confirmed": True, "confirm_timeout": 30}),
                OperationStepDTO(op_type="unlock", datastore="candidate"),
            ],
        )
        session = svc.build_session_config(req)
        assert len(session.operations) == 3
        assert isinstance(session.operations[0], TransactionOperation)
        assert session.operations[1].confirmed is True
        assert session.operations[1].confirm_timeout == 30

    def test_build_subscribe_session(self):
        svc = DeviceOperationService()
        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:57400", protocol="gnmi"),
            operations=[
                OperationStepDTO(
                    op_type="subscribe",
                    paths=["/interfaces/interface/state/counters"],
                    telemetry=TelemetryPolicyDTO(
                        mode=DeliveryModeDTO.PERIODIC,
                        sample_interval_sec=5.0,
                    ),
                    options={"subscription_name": "counters_sub"},
                )
            ],
        )
        session = svc.build_session_config(req)
        op = session.operations[0]
        assert isinstance(op, SubscribeOperation)
        assert op.delivery.mode == DeliveryMode.PERIODIC
        assert op.delivery.interval == 5.0
        assert op.subscription_name == "counters_sub"

    def test_empty_operations_raises(self):
        svc = DeviceOperationService()
        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:830", protocol="netconf"),
            operations=[],
        )
        with pytest.raises(InvalidRequestError, match="must contain at least one operation"):
            svc.build_session_config(req)


class TestDeviceOperationServiceExecuteUnary:
    @patch.object(DeviceOperationService, "_resolve_worker")
    def test_execute_unary_success(self, mock_resolve):
        mock_worker = MagicMock()
        mock_worker.start.return_value = {
            "session_id": "abc12345",
            "target": "10.0.0.1:57400",
            "rpc": "capability",
            "data": {"gnmi_version": "0.8.0", "supported_encodings": ["JSON_IETF"]},
            "protocol": "gnmi",
        }
        mock_resolve.return_value = mock_worker

        svc = DeviceOperationService()
        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:57400", protocol="gnmi"),
            operations=[OperationStepDTO(op_type="capabilities")],
        )

        res = svc.execute_unary(req)
        assert res.success is True
        assert res.target == "10.0.0.1:57400"
        assert res.protocol == "gnmi"
        assert len(res.results) == 1
        assert res.results[0].op_type == "capability"
        assert res.results[0].success is True
        assert res.results[0].data["gnmi_version"] == "0.8.0"
        assert res.execution_time_ms >= 0.0

    @patch.object(DeviceOperationService, "_resolve_worker")
    def test_execute_unary_with_output_handler(self, mock_resolve):
        mock_result = {
            "session_id": "abc12345",
            "target": "10.0.0.1:57400",
            "rpc": "get",
            "data": {"state": "up"},
            "protocol": "gnmi",
        }
        mock_worker = MagicMock()
        mock_worker.start.return_value = mock_result
        mock_resolve.return_value = mock_worker

        mock_handler = MagicMock()
        svc = DeviceOperationService()
        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:57400", protocol="gnmi"),
            operations=[OperationStepDTO(op_type="get", paths=["/interfaces"])],
        )

        res = svc.execute_unary(req, output_handler=mock_handler)
        assert res.success is True
        mock_handler.write.assert_called_once_with(mock_result)

    @patch.object(DeviceOperationService, "_resolve_worker")
    def test_execute_unary_with_error_step(self, mock_resolve):
        mock_worker = MagicMock()
        mock_worker.start.return_value = [
            {
                "session_id": "s1",
                "target": "10.0.0.1:830",
                "rpc": "lock",
                "data": "<ok/>",
                "protocol": "netconf",
            },
            {
                "session_id": "s1",
                "target": "10.0.0.1:830",
                "rpc": "edit-config",
                "data": Exception("RPC error: syntax error in XML"),
                "protocol": "netconf",
            },
        ]
        mock_resolve.return_value = mock_worker

        svc = DeviceOperationService()
        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:830", protocol="netconf"),
            operations=[
                OperationStepDTO(op_type="lock"),
                OperationStepDTO(op_type="set", changes=[MutationChangeDTO(path="/test", value=1)]),
            ],
        )

        res = svc.execute_unary(req)
        assert res.success is False
        assert len(res.results) == 2
        assert res.results[0].success is True
        assert res.results[1].success is False
        assert "syntax error" in res.error_summary

    def test_execute_validate_only_mode(self):
        mock_schema_svc = MagicMock()
        mock_schema_svc.validate_request_schema.return_value = []

        svc = DeviceOperationService(schema_service=mock_schema_svc)
        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:57400", protocol="gnmi"),
            operations=[OperationStepDTO(op_type="get", paths=["/interfaces"])],
            validate_only=True,
        )

        res = svc.execute_unary(req)
        assert res.success is True
        assert len(res.results) == 1
        assert res.results[0].op_type == "validate"
        mock_schema_svc.validate_request_schema.assert_called_once_with(req)


class TestDeviceOperationServiceExecuteBatch:
    @patch.object(DeviceOperationService, "_resolve_worker")
    def test_execute_batch_multiple_targets_concurrently(self, mock_resolve):
        def side_effect(session_cfg):
            worker = MagicMock()
            worker.start.return_value = {
                "session_id": f"sess-{session_cfg.target}",
                "target": session_cfg.target,
                "rpc": "get",
                "data": {"hostname": session_cfg.target},
                "protocol": session_cfg.protocol.value,
            }
            return worker

        mock_resolve.side_effect = side_effect

        svc = DeviceOperationService()
        requests = [
            ExecutionRequestDTO(
                adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:57400", protocol="gnmi"),
                operations=[OperationStepDTO(op_type="get", paths=["/system/config/hostname"])],
            ),
            ExecutionRequestDTO(
                adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.2:830", protocol="netconf"),
                operations=[OperationStepDTO(op_type="get", paths=["/system/config/hostname"])],
            ),
        ]

        mock_handler = MagicMock()
        results = svc.execute_batch(requests, output_handlers=[mock_handler])

        assert len(results) == 2
        assert results[0].target == "10.0.0.1:57400"
        assert results[0].protocol == "gnmi"
        assert results[0].success is True
        assert results[1].target == "10.0.0.2:830"
        assert results[1].protocol == "netconf"
        assert results[1].success is True
        assert mock_handler.write.call_count == 2

    @patch.object(DeviceOperationService, "_resolve_worker")
    def test_execute_batch_respects_times_multiplier(self, mock_resolve):
        mock_worker = MagicMock()
        mock_worker.start.return_value = {
            "session_id": "s1",
            "target": "10.0.0.1:57400",
            "rpc": "capability",
            "data": {"gnmi_version": "0.8.0"},
            "protocol": "gnmi",
        }
        mock_resolve.return_value = mock_worker

        svc = DeviceOperationService()
        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:57400", protocol="gnmi"),
            operations=[OperationStepDTO(op_type="capabilities")],
            times=3,
        )

        results = svc.execute_batch([req])
        assert len(results) == 3
        assert mock_worker.start.call_count == 3
        assert all(r.success for r in results)

    @patch.object(DeviceOperationService, "_resolve_worker")
    def test_execute_batch_partial_failure_isolation(self, mock_resolve):
        def side_effect(session_cfg):
            worker = MagicMock()
            if session_cfg.target == "10.0.0.2:57400":
                worker.start.side_effect = RuntimeError("Connection refused")
            else:
                worker.start.return_value = {
                    "session_id": "ok-1",
                    "target": session_cfg.target,
                    "rpc": "get",
                    "data": {"ok": True},
                    "protocol": "gnmi",
                }
            return worker

        mock_resolve.side_effect = side_effect

        svc = DeviceOperationService()
        requests = [
            ExecutionRequestDTO(
                adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:57400", protocol="gnmi"),
                operations=[OperationStepDTO(op_type="get", paths=["/system"])],
            ),
            ExecutionRequestDTO(
                adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.2:57400", protocol="gnmi"),
                operations=[OperationStepDTO(op_type="get", paths=["/system"])],
            ),
        ]

        results = svc.execute_batch(requests)
        assert len(results) == 2
        assert results[0].success is True
        assert results[1].success is False
        assert "Connection refused" in results[1].error_summary

