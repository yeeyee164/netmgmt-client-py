# -*- coding: utf-8 -*-
from unittest.mock import MagicMock, patch
import pytest

from application.subscriptions import SubscriptionService
from application.dto import (
    AdhocEndpointDTO,
    ExecutionRequestDTO,
    OperationStepDTO,
    TelemetryPolicyDTO,
    DeliveryModeDTO,
    StreamEventDTO,
)
from application.exceptions import (
    InvalidRequestError,
    SubscriptionNotFoundError,
    UnsupportedProtocolError,
)


class TestSubscriptionService:
    @patch("application.subscriptions.SubscribeSession")
    def test_start_and_get_events(self, mock_session_cls):
        mock_worker = MagicMock()
        mock_worker.is_running = True
        mock_session_cls.return_value = mock_worker

        sub_svc = SubscriptionService()
        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:57400", protocol="gnmi"),
            operations=[
                OperationStepDTO(
                    op_type="subscribe",
                    paths=["/interfaces/interface/state"],
                    telemetry=TelemetryPolicyDTO(mode=DeliveryModeDTO.PERIODIC, sample_interval_sec=5.0),
                )
            ],
        )

        sub_id = sub_svc.start_subscription(req)
        assert isinstance(sub_id, str)
        assert len(sub_id) > 0

        # Push mock event into worker's data_queue
        managed = sub_svc._get_managed(sub_id)
        managed.data_queue.put({
            "session_id": "sess-1",
            "target": "10.0.0.1:57400",
            "protocol": "gnmi",
            "subscription_name": "sub",
            "data": {"oper_status": "UP"},
            "is_sync_marker": False,
        })

        events = sub_svc.get_events(sub_id, timeout=0.1)
        assert len(events) == 1
        assert isinstance(events[0], StreamEventDTO)
        assert events[0].data == {"oper_status": "UP"}
        assert events[0].protocol == "gnmi"

    def test_start_subscription_without_subscribe_op_raises(self):
        sub_svc = SubscriptionService()
        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:57400", protocol="gnmi"),
            operations=[OperationStepDTO(op_type="get", paths=["/system"])],
        )
        with pytest.raises(InvalidRequestError, match="must contain an operation with op_type='subscribe'"):
            sub_svc.start_subscription(req)

    def test_get_events_unknown_subscription_raises(self):
        sub_svc = SubscriptionService()
        with pytest.raises(SubscriptionNotFoundError):
            sub_svc.get_events("non-existent-sub")

    @patch("application.subscriptions.SubscribeSession")
    def test_send_command_poll_and_stop(self, mock_session_cls):
        mock_worker = MagicMock()
        mock_worker.is_running = True
        mock_session_cls.return_value = mock_worker

        sub_svc = SubscriptionService()
        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:57400", protocol="gnmi"),
            operations=[OperationStepDTO(op_type="subscribe", paths=["/test"])],
        )
        sub_id = sub_svc.start_subscription(req)

        # Send poll command
        sub_svc.send_command(sub_id, "poll")
        mock_worker.trigger_poll.assert_called_once()

        # Send invalid command
        with pytest.raises(InvalidRequestError, match="Unsupported subscription command"):
            sub_svc.send_command(sub_id, "restart")

        # Stop subscription
        assert sub_svc.stop_subscription(sub_id) is True
        mock_worker.stop.assert_called_once()
        assert sub_svc.stop_subscription(sub_id) is False

    @patch("application.subscriptions.SubscribeSession")
    def test_list_and_stop_all(self, mock_session_cls):
        mock_worker = MagicMock()
        mock_worker.is_running = True
        mock_session_cls.return_value = mock_worker

        sub_svc = SubscriptionService()
        req1 = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:57400", protocol="gnmi"),
            operations=[OperationStepDTO(op_type="subscribe", paths=["/sub1"])],
        )
        req2 = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.2:830", protocol="netconf"),
            operations=[OperationStepDTO(op_type="subscribe", paths=["/sub2"])],
        )

        id1 = sub_svc.start_subscription(req1)
        id2 = sub_svc.start_subscription(req2)

        active = sub_svc.list_active_subscriptions()
        assert len(active) == 2

        sub_svc.stop_all()
        assert len(sub_svc.list_active_subscriptions()) == 0

