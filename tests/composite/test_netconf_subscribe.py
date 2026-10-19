import os
import sys
import time
import threading
from unittest.mock import MagicMock, patch
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.protocol_options.netconf import NetconfOptions
from config.selectors import PathSelector, FilterSelector
from config.operations import SubscribeOperation
from config.delivery import DeliveryPolicy, DeliveryMode
from config.model import Protocol, SessionConfig
from specs.base_client import UnsupportedOperationError
from specs.stream_types import StreamContext, StreamEvent
from specs.netconf_client import NetconfClient
from specs.netconf_validator import NetconfValidator
from modules.validate import PathValidationError
from ui.cmd import build_args, parse_args


class DummyNotification:
    def __init__(self, xml_content: str, event_time: str = "2026-10-14T12:00:00Z"):
        self.notification_xml = xml_content
        self.event_time = event_time


class TestNetconfOptionsSubscription:
    def test_valid_default_options(self):
        opts = NetconfOptions()
        opts.validate()
        assert opts.stream_name == "NETCONF"
        assert opts.start_time is None
        assert opts.stop_time is None
        assert opts.poll_timeout == 1.0

    def test_valid_replay_options(self):
        opts = NetconfOptions(
            stream_name="ALARMS",
            start_time="2026-10-14T10:00:00Z",
            stop_time="2026-10-14T12:00:00Z",
            poll_timeout=0.5,
        )
        opts.validate()
        assert opts.stream_name == "ALARMS"
        assert opts.start_time == "2026-10-14T10:00:00Z"
        assert opts.stop_time == "2026-10-14T12:00:00Z"
        assert opts.poll_timeout == 0.5

    def test_stop_time_without_start_time_fails(self):
        opts = NetconfOptions(stop_time="2026-10-14T12:00:00Z")
        with pytest.raises(ValueError, match="RFC 5277 constraint violation"):
            opts.validate()

    def test_stop_time_before_start_time_fails(self):
        opts = NetconfOptions(
            start_time="2026-10-14T12:00:00Z",
            stop_time="2026-10-14T10:00:00Z",
        )
        with pytest.raises(ValueError, match="must be strictly later"):
            opts.validate()

    def test_stop_time_equal_start_time_fails(self):
        opts = NetconfOptions(
            start_time="2026-10-14T12:00:00Z",
            stop_time="2026-10-14T12:00:00Z",
        )
        with pytest.raises(ValueError, match="must be strictly later"):
            opts.validate()

    def test_invalid_poll_timeout_fails(self):
        opts = NetconfOptions(poll_timeout=0.0)
        with pytest.raises(ValueError, match="Invalid poll_timeout"):
            opts.validate()

        opts_neg = NetconfOptions(poll_timeout=-1.5)
        with pytest.raises(ValueError, match="Invalid poll_timeout"):
            opts_neg.validate()


class TestNetconfFilterBuilding:
    def test_filter_building(self):
        client = NetconfClient(target="127.0.0.1:830")

        # None selector
        assert client._build_subscription_filter(None) is None

        # FilterSelector xpath
        sel_xpath = FilterSelector(expression="/interfaces/interface", filter_type="xpath")
        assert client._build_subscription_filter(sel_xpath) == (
            '<filter xmlns="urn:ietf:params:xml:ns:netconf:notification:1.0" type="xpath" select="/interfaces/interface"/>'
        )

        # FilterSelector subtree
        sel_sub = FilterSelector(expression="<interfaces/>", filter_type="subtree")
        assert client._build_subscription_filter(sel_sub) == (
            '<filter xmlns="urn:ietf:params:xml:ns:netconf:notification:1.0" type="subtree"><interfaces/></filter>'
        )

        # PathSelector single xpath
        sel_p1 = PathSelector(paths=("/interfaces/interface[name='eth0']",))
        assert client._build_subscription_filter(sel_p1) == (
            '<filter xmlns="urn:ietf:params:xml:ns:netconf:notification:1.0" type="xpath" select="/interfaces/interface[name=\'eth0\']"/>'
        )

        # PathSelector xml string
        sel_pxml = PathSelector(paths=("<interfaces><interface/></interfaces>",))
        assert client._build_subscription_filter(sel_pxml) == (
            '<filter xmlns="urn:ietf:params:xml:ns:netconf:notification:1.0" type="subtree"><interfaces><interface/></interfaces></filter>'
        )

        # String selector
        assert client._build_subscription_filter("<filter/>") == (
            '<filter xmlns="urn:ietf:params:xml:ns:netconf:notification:1.0"/>'
        )
        assert client._build_subscription_filter("/system/state") == (
            '<filter xmlns="urn:ietf:params:xml:ns:netconf:notification:1.0" type="xpath" select="/system/state"/>'
        )


class TestNetconfEventTimeParsing:
    def test_parse_event_time_to_posix(self):
        client = NetconfClient(target="127.0.0.1:830")

        # Standard Z suffix
        ts = client._parse_event_time_to_posix("2026-10-14T12:00:00Z")
        assert isinstance(ts, float)
        assert ts > 0

        # Offset suffix
        ts_offset = client._parse_event_time_to_posix("2026-10-14T12:00:00+00:00")
        assert ts_offset == ts

        # Fallback for None or invalid
        now = time.time()
        fallback_none = client._parse_event_time_to_posix(None)
        assert abs(fallback_none - now) < 5

        fallback_bad = client._parse_event_time_to_posix("invalid-timestamp")
        assert abs(fallback_bad - now) < 5


class TestNetconfClientExecuteSubscribe:
    def test_missing_notification_capability_raises(self):
        client = NetconfClient(target="127.0.0.1:830")
        mock_session = MagicMock()
        mock_session.server_capabilities = [
            "urn:ietf:params:netconf:base:1.0",
            "urn:ietf:params:netconf:base:1.1",
        ]
        client.session = mock_session

        context = StreamContext()
        with pytest.raises(UnsupportedOperationError, match="does not support RFC 5277"):
            client.execute_subscribe(context=context)

    def test_continuous_stream_notifications(self):
        client = NetconfClient(target="127.0.0.1:830")
        mock_session = MagicMock()
        mock_session.server_capabilities = [
            "urn:ietf:params:netconf:base:1.0",
            "urn:ietf:params:netconf:capability:notification:1.0",
        ]
        mock_reply = MagicMock()
        mock_reply.ok = True
        mock_session.create_subscription.return_value = mock_reply

        notif1 = DummyNotification(
            '<notification xmlns="urn:ietf:params:xml:ns:netconf:notification:1.0">'
            '<eventTime>2026-10-14T12:00:00Z</eventTime>'
            '<linkDown xmlns="urn:test"><ifIndex>1</ifIndex></linkDown>'
            '</notification>',
            event_time="2026-10-14T12:00:00Z"
        )
        notif2 = DummyNotification(
            '<notification xmlns="urn:ietf:params:xml:ns:netconf:notification:1.0">'
            '<eventTime>2026-10-14T12:00:05Z</eventTime>'
            '<linkUp xmlns="urn:test"><ifIndex>1</ifIndex></linkUp>'
            '</notification>',
            event_time="2026-10-14T12:00:05Z"
        )

        # Return notif1, notif2, then None
        mock_session.take_notification.side_effect = [notif1, notif2, None, None]
        client.session = mock_session

        context = StreamContext()
        gen = client.execute_subscribe(context=context)

        # First event
        ev1 = next(gen)
        assert isinstance(ev1, StreamEvent)
        assert ev1.protocol == "netconf"
        assert "<linkDown" in ev1.raw_payload
        assert ev1.data is not None
        assert not ev1.is_sync_marker

        # Second event
        ev2 = next(gen)
        assert isinstance(ev2, StreamEvent)
        assert "<linkUp" in ev2.raw_payload

        # Cancel context to terminate loop cleanly
        context.cancel()
        with pytest.raises(StopIteration):
            next(gen)

    def test_bounded_replay_with_completion(self):
        client = NetconfClient(target="127.0.0.1:830")
        mock_session = MagicMock()
        mock_session.server_capabilities = [
            "urn:ietf:params:netconf:base:1.0",
            "urn:ietf:params:netconf:capability:notification:1.0",
        ]
        mock_reply = MagicMock()
        mock_reply.ok = True
        mock_session.create_subscription.return_value = mock_reply

        notif_data = DummyNotification(
            '<notification xmlns="urn:ietf:params:xml:ns:netconf:notification:1.0">'
            '<eventTime>2026-10-14T10:05:00Z</eventTime>'
            '<eventData>Sample</eventData>'
            '</notification>'
        )
        notif_replay_done = DummyNotification(
            '<notification xmlns="urn:ietf:params:xml:ns:netconf:notification:1.0">'
            '<eventTime>2026-10-14T10:10:00Z</eventTime>'
            '<replayComplete/>'
            '</notification>'
        )
        notif_complete = DummyNotification(
            '<notification xmlns="urn:ietf:params:xml:ns:netconf:notification:1.0">'
            '<eventTime>2026-10-14T10:10:00Z</eventTime>'
            '<notificationComplete/>'
            '</notification>'
        )

        mock_session.take_notification.side_effect = [notif_data, notif_replay_done, notif_complete]
        client.session = mock_session

        opts = NetconfOptions(start_time="2026-10-14T10:00:00Z", stop_time="2026-10-14T10:10:00Z")
        op = SubscribeOperation(
            selector=PathSelector(paths=()),
            delivery=DeliveryPolicy(mode=DeliveryMode.EVENT_DRIVEN),
            protocol_options=opts,
        )

        events = list(client.execute_subscribe(operation=op))
        assert len(events) == 3
        assert not events[0].is_sync_marker
        assert events[1].is_sync_marker  # <replayComplete/> sets is_sync_marker=True
        assert "<notificationComplete" in events[2].raw_payload

    def test_create_subscription_rpc_error(self):
        client = NetconfClient(target="127.0.0.1:830")
        mock_session = MagicMock()
        mock_session.server_capabilities = [
            "urn:ietf:params:netconf:capability:notification:1.0",
        ]
        mock_session.create_subscription.side_effect = Exception("Transport error on create-subscription")
        client.session = mock_session

        events = list(client.execute_subscribe())
        assert len(events) == 1
        assert events[0].error is not None
        assert "Transport error" in str(events[0].error)


class TestNetconfValidatorSubscription:
    def test_validate_subscription(self):
        val = NetconfValidator()

        # Valid
        val.validate_subscription(stream_name="NETCONF")
        val.validate_subscription(
            stream_name="NETCONF",
            start_time="2026-10-14T10:00:00Z",
            stop_time="2026-10-14T12:00:00Z",
            filter_str="<interfaces/>",
            filter_type="subtree"
        )

        # Empty stream name
        with pytest.raises(PathValidationError, match="stream_name cannot be empty"):
            val.validate_subscription(stream_name="")

        # Stop without start
        with pytest.raises(PathValidationError, match="stop_time cannot be specified without start_time"):
            val.validate_subscription(stop_time="2026-10-14T12:00:00Z")

        # Stop <= start
        with pytest.raises(PathValidationError, match="must be strictly later than start_time"):
            val.validate_subscription(start_time="2026-10-14T12:00:00Z", stop_time="2026-10-14T10:00:00Z")


class TestNetconfCLISubscribe:
    def test_cli_netconf_subscribe_parsing(self):
        args = [
            "-t", "10.0.0.1:830",
            "-u", "admin",
            "-p", "admin",
            "netconf",
            "subscribe",
            "--stream", "ALARMS",
            "--start-time", "2026-10-14T00:00:00Z",
            "--stop-time", "2026-10-14T01:00:00Z",
            "--filter", "<alarms/>",
            "--poll-timeout", "2.5",
        ]
        parsed = parse_args(args)
        assert len(parsed.sessions) == 1
        sess = parsed.sessions[0]
        assert sess.protocol == Protocol.NETCONF
        assert isinstance(sess.operation, SubscribeOperation)
        assert sess.operation.subscription_name == "ALARMS"
        assert sess.operation.delivery.mode == DeliveryMode.EVENT_DRIVEN

        po = sess.operation.protocol_options
        assert isinstance(po, NetconfOptions)
        assert po.stream_name == "ALARMS"
        assert po.start_time == "2026-10-14T00:00:00Z"
        assert po.stop_time == "2026-10-14T01:00:00Z"
        assert po.poll_timeout == 2.5

    def test_yaml_netconf_subscribe_parsing(self):
        args = ["netconf", "-c", "tests/fixtures/netconf_subscribe_test.yaml"]
        parsed = parse_args(args)
        assert len(parsed.sessions) == 1
        sess = parsed.sessions[0]
        assert sess.protocol == Protocol.NETCONF
        assert sess.connection.target == "172.20.20.2:830"
        assert isinstance(sess.operation, SubscribeOperation)
        assert sess.operation.subscription_name == "srl_alarms"
        assert sess.operation.delivery.mode == DeliveryMode.EVENT_DRIVEN

        po = sess.operation.protocol_options
        assert isinstance(po, NetconfOptions)
        assert po.stream_name == "NETCONF"
        assert po.start_time == "2026-10-14T00:00:00Z"
        assert po.stop_time == "2026-10-14T12:00:00Z"
        assert po.poll_timeout == 1.0

        assert isinstance(sess.operation.selector, FilterSelector)
        assert sess.operation.selector.expression == "/srl_nokia-interfaces:interfaces"
        assert sess.operation.selector.filter_type == "xpath"

    def test_yaml_netopeer2_subscribe_parsing(self):
        args = ["netconf", "-c", "tests/fixtures/netopeer2_subscribe_test.yaml"]
        parsed = parse_args(args)
        assert len(parsed.sessions) == 1
        sess = parsed.sessions[0]
        assert sess.protocol == Protocol.NETCONF
        assert sess.connection.target == "172.20.20.4:830"
        assert isinstance(sess.operation, SubscribeOperation)
        assert sess.operation.subscription_name == "session_events"
        assert sess.operation.delivery.mode == DeliveryMode.EVENT_DRIVEN

        po = sess.operation.protocol_options
        assert isinstance(po, NetconfOptions)
        assert po.stream_name == "NETCONF"
        assert po.poll_timeout == 0.5

        assert isinstance(sess.operation.selector, FilterSelector)
        assert sess.operation.selector.filter_type == "subtree"
        assert "<netconf-session-start" in sess.operation.selector.expression





@pytest.mark.live
class TestLiveNokiaSRLinuxNetconfSubscription:
    """
    Live target verification against Nokia SR Linux (172.20.20.2:830).
    Verifies that NetconfClient connects, examines server capabilities, and
    raises UnsupportedOperationError because Nokia SR Linux does not advertise
    RFC 5277 :notification:1.0.
    """
    def test_live_srl_notification_capability_check(self):
        client = NetconfClient(
            target="172.20.20.2:830",
            username="admin",
            password="NokiaSrl1!",
        )

        # Enter client context manager to connect
        with client:
            assert client.session is not None
            assert client.session.connected

            # Verify that Nokia SR Linux server capabilities do not have :notification:1.0
            server_caps = list(client.session.server_capabilities)
            has_notification_cap = any("urn:ietf:params:netconf:capability:notification:1.0" in c for c in server_caps)
            assert not has_notification_cap, "Nokia SR Linux should not advertise RFC 5277 :notification:1.0"

            # Verify execute_subscribe correctly catches missing capability and raises UnsupportedOperationError
            context = StreamContext()
            with pytest.raises(UnsupportedOperationError, match="does not support RFC 5277"):
                client.execute_subscribe(context=context)


@pytest.mark.live
class TestLiveNetopeer2NetconfSubscription:
    """
    Live target verification against Netopeer2 NETCONF simulator (172.20.20.4:830).
    Verifies that NetconfClient connects, verifies RFC 5277 server capabilities,
    and performs unfiltered and filtered event subscriptions with live event delivery.
    """
    TARGET = "172.20.20.4:830"
    USERNAME = "root"
    PASSWORD = "root"

    def test_live_netopeer2_capabilities(self):
        """
        Verifies :notification:1.0 and :interleave:1.0 capabilities on netopeer2.
        """
        client = NetconfClient(
            target=self.TARGET,
            username=self.USERNAME,
            password=self.PASSWORD,
        )
        with client:
            assert client.session is not None
            assert client.session.connected
            server_caps = list(client.session.server_capabilities)

            # Netopeer2 MUST advertise RFC 5277 notification capability
            assert any("urn:ietf:params:netconf:capability:notification:1.0" in c for c in server_caps), (
                "Netopeer2 must advertise RFC 5277 :notification:1.0"
            )
            # Netopeer2 also supports interleaving
            assert any("urn:ietf:params:netconf:capability:interleave:1.0" in c for c in server_caps), (
                "Netopeer2 must advertise RFC 5277 :interleave:1.0"
            )

    def test_live_netopeer2_unfiltered_subscription_and_reception(self):
        """
        Establishes unfiltered NETCONF subscription, triggers live event via secondary session,
        verifies `StreamEvent` receipt('netconf-session-start').
        """
        from ncclient import manager

        client = NetconfClient(
            target=self.TARGET,
            username=self.USERNAME,
            password=self.PASSWORD,
        )

        received_events = []
        ctx = StreamContext()

        def subscriber_thread():
            with client:
                opts = NetconfOptions(stream_name="NETCONF", poll_timeout=0.5)
                for event in client.execute_subscribe(options=opts, context=ctx):
                    received_events.append(event)
                    if len(received_events) >= 1:
                        ctx.cancel()
                        break

        t = threading.Thread(target=subscriber_thread)
        t.start()

        # Allow subscriber to establish create-subscription
        time.sleep(1.5)

        # Trigger event via secondary session connect/disconnect
        m = manager.connect(
            host="172.20.20.4",
            port=830,
            username=self.USERNAME,
            password=self.PASSWORD,
            hostkey_verify=False,
            timeout=5
        )
        time.sleep(0.5)
        m.close_session()

        t.join(timeout=6)
        ctx.cancel()
        t.join(timeout=2)

        assert len(received_events) >= 1, "Expected at least 1 notification event from Netopeer2"
        event = received_events[0]
        assert event.protocol == "netconf"
        assert isinstance(event.timestamp, float)
        assert event.timestamp > 0
        assert event.raw_payload is not None
        assert "<notification" in event.raw_payload
        assert event.data is not None
        assert "notification" in event.data

    def test_live_netopeer2_subtree_filtered_subscription(self):
        """
        Subscribes with subtree filter for `<netconf-session-start>`,
        triggers event, verifies delivery of matching notification 
        """
        from ncclient import manager

        client = NetconfClient(
            target=self.TARGET,
            username=self.USERNAME,
            password=self.PASSWORD,
        )

        received_events = []
        ctx = StreamContext()

        def subscriber_thread():
            with client:
                filt = FilterSelector(
                    expression='<netconf-session-start xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-notifications"/>',
                    filter_type="subtree"
                )
                opts = NetconfOptions(stream_name="NETCONF", poll_timeout=0.5)
                for event in client.execute_subscribe(selectors=filt, options=opts, context=ctx):
                    received_events.append(event)
                    if len(received_events) >= 1:
                        ctx.cancel()
                        break

        t = threading.Thread(target=subscriber_thread)
        t.start()

        time.sleep(1.5)

        m = manager.connect(
            host="172.20.20.4",
            port=830,
            username=self.USERNAME,
            password=self.PASSWORD,
            hostkey_verify=False,
            timeout=5
        )
        time.sleep(0.5)
        m.close_session()

        t.join(timeout=6)
        ctx.cancel()
        t.join(timeout=2)

        assert len(received_events) >= 1, "Expected filtered netconf-session-start notification"
        event = received_events[0]
        assert "netconf-session-start" in event.raw_payload

    def test_live_netopeer2_xpath_filtered_subscription(self):
        """
        Subscribes with XPath filter `/netconf-session-start` without XML error
        """
        client = NetconfClient(
            target=self.TARGET,
            username=self.USERNAME,
            password=self.PASSWORD,
        )

        ctx = StreamContext()
        with client:
            filt = FilterSelector(
                expression='/netconf-session-start',
                filter_type="xpath"
            )
            opts = NetconfOptions(stream_name="NETCONF", poll_timeout=0.5)
            # Starting subscription with XPath filter must succeed without XML / RPC error
            gen = client.execute_subscribe(selectors=filt, options=opts, context=ctx)
            # Cancel immediately after create-subscription succeeded
            ctx.cancel()
            events = list(gen)
            # Succeeded without raising RPCError or yielding error StreamEvent
            for ev in events:
                assert ev.error is None

