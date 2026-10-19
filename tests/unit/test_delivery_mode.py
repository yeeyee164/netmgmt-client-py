import unittest
import glob
from dataclasses import FrozenInstanceError

from config.delivery import DeliveryMode, DeliveryPolicy
from config.operations import SubscribeOperation
from config.selectors import PathSelector
from config.model import SessionConfig, ConnectionConfig, Protocol
from ui.cmd import resolve_delivery_mode, FileConfigBuilder, CLIConfigBuilder, build_args, config_builder
from managers.subscribe_session import SubscribeSession

try:
    from specs.gnmi import gnmi_pb2
    from specs.gnmi_client import GNMIClient, MODE_TO_GNMI_LIST_MODE, MODE_TO_GNMI_SUB_MODE
except ImportError:
    gnmi_pb2 = None
    GNMIClient = None
    MODE_TO_GNMI_LIST_MODE = {}
    MODE_TO_GNMI_SUB_MODE = {}


class TestDeliveryModeEnumAndPolicy(unittest.TestCase):
    """Verifies DeliveryMode enum semantics, case/hyphen normalization, and DeliveryPolicy validation."""

    def test_enum_members_and_values(self):
        self.assertEqual(DeliveryMode.PERIODIC.value, "periodic")
        self.assertEqual(DeliveryMode.EVENT_DRIVEN.value, "event_driven")
        self.assertEqual(DeliveryMode.SNAPSHOT.value, "snapshot")
        self.assertEqual(DeliveryMode.ON_DEMAND.value, "on_demand")
        self.assertEqual(DeliveryMode.SERVER_DETERMINED.value, "server_determined")
        self.assertEqual(len(DeliveryMode), 5)

    def test_legacy_attribute_names_removed(self):
        self.assertFalse(hasattr(DeliveryMode, "POLL"))
        self.assertFalse(hasattr(DeliveryMode, "ON_CHANGE"))
        self.assertFalse(hasattr(DeliveryMode, "TARGET_DEFINED"))
        self.assertFalse(hasattr(DeliveryMode, "SAMPLE"))
        self.assertFalse(hasattr(DeliveryMode, "ONCE"))

    def test_enum_normalization(self):
        self.assertIs(DeliveryMode("periodic"), DeliveryMode.PERIODIC)
        self.assertIs(DeliveryMode("PERIODIC"), DeliveryMode.PERIODIC)
        self.assertIs(DeliveryMode("event_driven"), DeliveryMode.EVENT_DRIVEN)
        self.assertIs(DeliveryMode("event-driven"), DeliveryMode.EVENT_DRIVEN)
        self.assertIs(DeliveryMode("EVENT-DRIVEN"), DeliveryMode.EVENT_DRIVEN)
        self.assertIs(DeliveryMode("snapshot"), DeliveryMode.SNAPSHOT)
        self.assertIs(DeliveryMode("SNAPSHOT"), DeliveryMode.SNAPSHOT)
        self.assertIs(DeliveryMode("on_demand"), DeliveryMode.ON_DEMAND)
        self.assertIs(DeliveryMode("on-demand"), DeliveryMode.ON_DEMAND)
        self.assertIs(DeliveryMode("ON-DEMAND"), DeliveryMode.ON_DEMAND)
        self.assertIs(DeliveryMode("server_determined"), DeliveryMode.SERVER_DETERMINED)
        self.assertIs(DeliveryMode("server-determined"), DeliveryMode.SERVER_DETERMINED)
        self.assertIs(DeliveryMode("SERVER-DETERMINED"), DeliveryMode.SERVER_DETERMINED)

        with self.assertRaises(ValueError):
            DeliveryMode("invalid_mode")

    def test_delivery_policy_instantiation_and_normalization(self):
        dp1 = DeliveryPolicy(mode=DeliveryMode.PERIODIC, interval=10, heartbeat=30, suppress_redundant=True)
        self.assertIs(dp1.mode, DeliveryMode.PERIODIC)
        self.assertEqual(dp1.interval, 10)
        self.assertEqual(dp1.heartbeat, 30)
        self.assertTrue(dp1.suppress_redundant)

        # String normalization in __post_init__
        dp2 = DeliveryPolicy(mode="event-driven")
        self.assertIs(dp2.mode, DeliveryMode.EVENT_DRIVEN)

        dp3 = DeliveryPolicy(mode="ON_DEMAND")
        self.assertIs(dp3.mode, DeliveryMode.ON_DEMAND)

        # Invalid mode string raises clear ValueError
        with self.assertRaises(ValueError) as cm:
            DeliveryPolicy(mode="poll")
        self.assertIn("Invalid delivery mode 'poll'", str(cm.exception))

    def test_delivery_policy_validation(self):
        valid_dp = DeliveryPolicy(mode=DeliveryMode.PERIODIC, interval=5, heartbeat=10)
        valid_dp.validate()

        with self.assertRaises(ValueError):
            DeliveryPolicy(interval=-1).validate()

        with self.assertRaises(ValueError):
            DeliveryPolicy(heartbeat=-1).validate()


class TestResolveDeliveryMode(unittest.TestCase):
    """Verifies ui/cmd.py resolve_delivery_mode helper."""

    def test_legacy_cli_and_yaml_modes(self):
        self.assertIs(resolve_delivery_mode("once", None), DeliveryMode.SNAPSHOT)
        self.assertIs(resolve_delivery_mode("poll", None), DeliveryMode.ON_DEMAND)
        self.assertIs(resolve_delivery_mode("stream", "sample"), DeliveryMode.PERIODIC)
        self.assertIs(resolve_delivery_mode("stream", "on_change"), DeliveryMode.EVENT_DRIVEN)
        self.assertIs(resolve_delivery_mode("stream", "target_defined"), DeliveryMode.SERVER_DETERMINED)

    def test_defaults_and_normalization(self):
        # Default sub_mode in CLI is sample -> PERIODIC
        self.assertIs(resolve_delivery_mode("stream", None, default_sub_mode="sample"), DeliveryMode.PERIODIC)
        # Default sub_mode in YAML is target_defined -> SERVER_DETERMINED
        self.assertIs(resolve_delivery_mode("stream", None, default_sub_mode="target_defined"), DeliveryMode.SERVER_DETERMINED)
        # Empty rpc_mode defaults to stream
        self.assertIs(resolve_delivery_mode(None, "on-change"), DeliveryMode.EVENT_DRIVEN)
        self.assertIs(resolve_delivery_mode("", "sample"), DeliveryMode.PERIODIC)

    def test_protocol_neutral_passthrough(self):
        self.assertIs(resolve_delivery_mode("snapshot", None), DeliveryMode.SNAPSHOT)
        self.assertIs(resolve_delivery_mode("on_demand", None), DeliveryMode.ON_DEMAND)
        self.assertIs(resolve_delivery_mode("on-demand", None), DeliveryMode.ON_DEMAND)
        self.assertIs(resolve_delivery_mode("stream", "event-driven"), DeliveryMode.EVENT_DRIVEN)
        self.assertIs(resolve_delivery_mode("stream", "server_determined"), DeliveryMode.SERVER_DETERMINED)

    def test_invalid_modes(self):
        with self.assertRaises(ValueError):
            resolve_delivery_mode("unsupported_rpc_mode")

        with self.assertRaises(ValueError):
            resolve_delivery_mode("stream", "unknown_sub_mode")


@unittest.skipIf(gnmi_pb2 is None, "specs.gnmi is not loaded")
class TestGnmiProtocolTranslation(unittest.TestCase):
    """Verifies translation of neutral DeliveryMode to wire-level gNMI SubscriptionList and Subscription."""

    def test_mode_translation_tables(self):
        self.assertEqual(MODE_TO_GNMI_LIST_MODE[DeliveryMode.PERIODIC], gnmi_pb2.SubscriptionList.STREAM)
        self.assertEqual(MODE_TO_GNMI_LIST_MODE[DeliveryMode.EVENT_DRIVEN], gnmi_pb2.SubscriptionList.STREAM)
        self.assertEqual(MODE_TO_GNMI_LIST_MODE[DeliveryMode.SERVER_DETERMINED], gnmi_pb2.SubscriptionList.STREAM)
        self.assertEqual(MODE_TO_GNMI_LIST_MODE[DeliveryMode.SNAPSHOT], gnmi_pb2.SubscriptionList.ONCE)
        self.assertEqual(MODE_TO_GNMI_LIST_MODE[DeliveryMode.ON_DEMAND], gnmi_pb2.SubscriptionList.POLL)

        self.assertEqual(MODE_TO_GNMI_SUB_MODE[DeliveryMode.PERIODIC], gnmi_pb2.SubscriptionMode.SAMPLE)
        self.assertEqual(MODE_TO_GNMI_SUB_MODE[DeliveryMode.EVENT_DRIVEN], gnmi_pb2.SubscriptionMode.ON_CHANGE)
        self.assertEqual(MODE_TO_GNMI_SUB_MODE[DeliveryMode.SERVER_DETERMINED], gnmi_pb2.SubscriptionMode.TARGET_DEFINED)

    def test_subscribe_generator_modes(self):
        client = GNMIClient(target="127.0.0.1:9339", insecure=True)

        mode_expectations = [
            (DeliveryMode.PERIODIC, gnmi_pb2.SubscriptionList.STREAM, gnmi_pb2.SubscriptionMode.SAMPLE),
            (DeliveryMode.EVENT_DRIVEN, gnmi_pb2.SubscriptionList.STREAM, gnmi_pb2.SubscriptionMode.ON_CHANGE),
            (DeliveryMode.SERVER_DETERMINED, gnmi_pb2.SubscriptionList.STREAM, gnmi_pb2.SubscriptionMode.TARGET_DEFINED),
            (DeliveryMode.SNAPSHOT, gnmi_pb2.SubscriptionList.ONCE, None),
            (DeliveryMode.ON_DEMAND, gnmi_pb2.SubscriptionList.POLL, None),
        ]

        for d_mode, exp_list_mode, exp_sub_mode in mode_expectations:
            op = SubscribeOperation(
                selector=PathSelector(paths=["/interfaces"]),
                delivery=DeliveryPolicy(mode=d_mode, interval=15, heartbeat=30, suppress_redundant=True),
            )
            gen = client._build_subscribe_generator(operation=op)
            initial_req = next(gen)

            self.assertEqual(initial_req.subscribe.mode, exp_list_mode)
            self.assertEqual(len(initial_req.subscribe.subscription), 1)
            if exp_sub_mode is not None:
                sub = initial_req.subscribe.subscription[0]
                self.assertEqual(sub.mode, exp_sub_mode)
                if exp_sub_mode == gnmi_pb2.SubscriptionMode.SAMPLE:
                    self.assertEqual(sub.sample_interval, 15 * 10**9)
                    self.assertEqual(sub.heartbeat_interval, 30 * 10**9)
                    self.assertTrue(sub.suppress_redundant)


class TestSubscribeSessionPollMode(unittest.TestCase):
    """Verifies SubscribeSession.is_poll_mode with DeliveryMode.ON_DEMAND."""

    def test_is_poll_mode_with_operation(self):
        conn = ConnectionConfig(target="10.0.0.1:9339")

        sess_on_demand = SessionConfig(
            connection=conn,
            protocol=Protocol.GNMI,
            operation=SubscribeOperation(
                selector=PathSelector(paths=["/interfaces"]),
                delivery=DeliveryPolicy(mode=DeliveryMode.ON_DEMAND),
            ),
        )
        sub_session_poll = SubscribeSession(config=sess_on_demand)
        self.assertTrue(sub_session_poll.is_poll_mode())

        for mode in [DeliveryMode.PERIODIC, DeliveryMode.EVENT_DRIVEN, DeliveryMode.SNAPSHOT, DeliveryMode.SERVER_DETERMINED]:
            sess = SessionConfig(
                connection=conn,
                protocol=Protocol.GNMI,
                operation=SubscribeOperation(
                    selector=PathSelector(paths=["/interfaces"]),
                    delivery=DeliveryPolicy(mode=mode),
                ),
            )
            sub_sess = SubscribeSession(config=sess)
            self.assertFalse(sub_sess.is_poll_mode())

    def test_is_poll_mode_legacy_kwargs(self):
        sub_legacy_poll = SubscribeSession(mode="poll")
        self.assertTrue(sub_legacy_poll.is_poll_mode())

        sub_legacy_stream = SubscribeSession(mode="stream")
        self.assertFalse(sub_legacy_stream.is_poll_mode())


class TestConfigBuildersIntegration(unittest.TestCase):
    """Verifies CLI and YAML builders output correct DeliveryMode and don't leak sub_mode into GNMIOptions."""

    def test_cli_builder_delivery_modes(self):
        cases = [
            (["-t", "10.0.0.1:9339", "gnmi", "subscribe", "--path", "/interfaces", "--mode", "once"], DeliveryMode.SNAPSHOT),
            (["-t", "10.0.0.1:9339", "gnmi", "subscribe", "--path", "/interfaces", "--mode", "poll"], DeliveryMode.ON_DEMAND),
            (["-t", "10.0.0.1:9339", "gnmi", "subscribe", "--path", "/interfaces", "--mode", "stream", "--sub-mode", "sample"], DeliveryMode.PERIODIC),
            (["-t", "10.0.0.1:9339", "gnmi", "subscribe", "--path", "/interfaces", "--mode", "stream", "--sub-mode", "on_change"], DeliveryMode.EVENT_DRIVEN),
            (["-t", "10.0.0.1:9339", "gnmi", "subscribe", "--path", "/interfaces", "--mode", "stream", "--sub-mode", "target_defined"], DeliveryMode.SERVER_DETERMINED),
        ]

        for argv, expected_mode in cases:
            args = build_args(argv)
            parsed = config_builder(args)
            op = parsed.sessions[0].operation
            self.assertIs(op.delivery.mode, expected_mode)
            self.assertIsNone(op.protocol_options.sub_mode)

    def test_yaml_files_parsing(self):
        expected_modes = {
            "tests/fixtures/gnmi_subscribe_once_test.yaml": DeliveryMode.SNAPSHOT,
            "tests/fixtures/gnmi_subscribe_poll_test.yaml": DeliveryMode.ON_DEMAND,
            "tests/fixtures/gnmi_subscribe_stream_sample_test.yaml": DeliveryMode.PERIODIC,
            "tests/fixtures/gnmi_subscribe_stream_on_change_test.yaml": DeliveryMode.EVENT_DRIVEN,
            "tests/fixtures/gnmi_subscribe_stream_target_defined_test.yaml": DeliveryMode.SERVER_DETERMINED,
        }

        for path, exp_mode in expected_modes.items():
            parsed = FileConfigBuilder(path, protocol="gnmi").build()
            self.assertGreater(len(parsed.sessions), 0)
            for sess in parsed.sessions:
                self.assertIs(sess.operation.delivery.mode, exp_mode)
                self.assertIsNone(sess.operation.protocol_options.sub_mode)


if __name__ == "__main__":
    unittest.main()
