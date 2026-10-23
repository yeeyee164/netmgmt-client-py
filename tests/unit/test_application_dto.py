# -*- coding: utf-8 -*-
import pytest

from application.dto import (
    SupportedProtocol,
    DeliveryModeDTO,
    TelemetryPolicyDTO,
    MutationChangeDTO,
    AdhocEndpointDTO,
    OperationStepDTO,
    ExecutionRequestDTO,
    StepResultDTO,
    ExecutionResultDTO,
    StreamEventDTO,
)
from application.exceptions import UnsupportedProtocolError, InvalidRequestError


class TestSupportedProtocol:
    def test_valid_protocols(self):
        assert SupportedProtocol.from_string("gnmi") == SupportedProtocol.GNMI
        assert SupportedProtocol.from_string("GNMI") == SupportedProtocol.GNMI
        assert SupportedProtocol.from_string("netconf") == SupportedProtocol.NETCONF
        assert SupportedProtocol.from_string("NETCONF ") == SupportedProtocol.NETCONF

    def test_invalid_protocols(self):
        with pytest.raises(UnsupportedProtocolError, match="must be explicitly declared"):
            SupportedProtocol.from_string("")

        with pytest.raises(UnsupportedProtocolError, match="must be explicitly declared"):
            SupportedProtocol.from_string(None)

        with pytest.raises(UnsupportedProtocolError, match="Unsupported protocol 'snmp'"):
            SupportedProtocol.from_string("snmp")


class TestDeliveryModeDTO:
    def test_valid_delivery_modes(self):
        assert DeliveryModeDTO.from_string("periodic") == DeliveryModeDTO.PERIODIC
        assert DeliveryModeDTO.from_string("event_driven") == DeliveryModeDTO.EVENT_DRIVEN
        assert DeliveryModeDTO.from_string("snapshot") == DeliveryModeDTO.SNAPSHOT
        assert DeliveryModeDTO.from_string("on_demand") == DeliveryModeDTO.ON_DEMAND
        assert DeliveryModeDTO.from_string(None) == DeliveryModeDTO.PERIODIC

    def test_invalid_delivery_mode(self):
        with pytest.raises(InvalidRequestError, match="Invalid delivery mode"):
            DeliveryModeDTO.from_string("stream")


class TestAdhocEndpointDTO:
    def test_valid_endpoint(self):
        ep = AdhocEndpointDTO(
            target="172.20.20.2:830",
            protocol="netconf",
            username="admin",
            password="pwd",
        )
        assert ep.target == "172.20.20.2:830"
        assert ep.protocol == "netconf"
        assert ep.username == "admin"
        assert ep.skip_verify is False

    def test_missing_target_raises(self):
        with pytest.raises(InvalidRequestError, match="requires a non-empty 'target'"):
            AdhocEndpointDTO(target="", protocol="gnmi")

    def test_invalid_protocol_raises(self):
        with pytest.raises(UnsupportedProtocolError):
            AdhocEndpointDTO(target="127.0.0.1:830", protocol="restconf")


class TestExecutionRequestDTO:
    def test_valid_with_adhoc_endpoint(self):
        ep = AdhocEndpointDTO(target="10.0.0.1:57400", protocol="gnmi")
        req = ExecutionRequestDTO(
            adhoc_endpoint=ep,
            protocol="gnmi",
            operations=[OperationStepDTO(op_type="capabilities")],
        )
        assert req.protocol == "gnmi"
        assert len(req.operations) == 1

    def test_valid_with_device_id(self):
        req = ExecutionRequestDTO(
            device_id="spine-01",
            protocol="netconf",
            operations=[OperationStepDTO(op_type="get", paths=["/interfaces"])],
        )
        assert req.device_id == "spine-01"
        assert req.adhoc_endpoint is None

    def test_missing_both_targets_raises(self):
        with pytest.raises(InvalidRequestError, match="requires either 'device_id' or 'adhoc_endpoint'"):
            ExecutionRequestDTO(protocol="gnmi")

    def test_invalid_protocol_raises(self):
        ep = AdhocEndpointDTO(target="10.0.0.1:57400", protocol="gnmi")
        with pytest.raises(UnsupportedProtocolError):
            ExecutionRequestDTO(adhoc_endpoint=ep, protocol="bgp")


class TestExecutionResultDTO:
    def test_result_structure(self):
        step = StepResultDTO(op_type="get", success=True, data={"hostname": "r1"})
        res = ExecutionResultDTO(
            target="10.0.0.1:57400",
            protocol="gnmi",
            success=True,
            results=[step],
            execution_time_ms=12.5,
        )
        assert res.success is True
        assert len(res.results) == 1
        assert res.results[0].data == {"hostname": "r1"}
        assert res.error_summary is None


class TestStreamEventDTO:
    def test_stream_event_structure(self):
        ev = StreamEventDTO(
            session_id="sub-123",
            target="10.0.0.1:830",
            protocol="netconf",
            subscription_name="events",
            data={"notification": "linkUp"},
            is_sync_marker=True,
        )
        assert ev.session_id == "sub-123"
        assert ev.is_sync_marker is True
        assert ev.error is None


class TestExpandRequestRepetitions:
    def test_expand_empty_list(self):
        from application.dto import expand_request_repetitions
        assert expand_request_repetitions([]) == []

    def test_expand_multipliers(self):
        from application.dto import expand_request_repetitions
        req1 = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:57400", protocol="gnmi"),
            times=3,
        )
        req2 = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.2:830", protocol="netconf"),
            times=1,
        )
        req3 = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.3:57400", protocol="gnmi"),
            times=0,  # should default to 1
        )
        expanded = expand_request_repetitions([req1, req2, req3])
        assert len(expanded) == 5
        assert expanded[:3] == [req1, req1, req1]
        assert expanded[3] == req2
        assert expanded[4] == req3


