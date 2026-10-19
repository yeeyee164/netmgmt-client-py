import os
import sys
import unittest
import queue
import time

# Ensure project root directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from specs.stream_types import (
    StreamCommandType,
    StreamCommand,
    StreamContext,
    StreamEvent,
)
from specs.base_client import BaseClient, UnsupportedOperationError
from config.selectors import PathSelector
from config.protocol_options.gnmi import GnmiOptions


class DummyClient(BaseClient):
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class TestStreamTypes(unittest.TestCase):
    def test_stream_command_and_types(self):
        cmd = StreamCommand(command_type=StreamCommandType.POLL, payload={"test": 123})
        self.assertEqual(cmd.command_type, StreamCommandType.POLL)
        self.assertEqual(cmd.payload, {"test": 123})
        self.assertIsInstance(cmd.timestamp, float)

        # Frozen immutability
        with self.assertRaises(Exception):
            cmd.command_type = StreamCommandType.CANCEL

    def test_stream_context_poll_and_cancel(self):
        ctx = StreamContext()
        self.assertFalse(ctx.is_cancelled)

        ctx.request_poll()
        cmd = ctx.command_queue.get_nowait()
        self.assertEqual(cmd.command_type, StreamCommandType.POLL)
        self.assertFalse(ctx.is_cancelled)

        ctx.cancel()
        self.assertTrue(ctx.is_cancelled)
        cmd2 = ctx.command_queue.get_nowait()
        self.assertEqual(cmd2.command_type, StreamCommandType.CANCEL)

    def test_stream_event(self):
        err = RuntimeError("stream failure")
        evt = StreamEvent(
            protocol="gnmi",
            timestamp=time.time(),
            raw_payload={"raw": "val"},
            data={"parsed": "val"},
            is_sync_marker=True,
            error=err,
        )
        self.assertEqual(evt.protocol, "gnmi")
        self.assertEqual(evt.raw_payload, {"raw": "val"})
        self.assertEqual(evt.data, {"parsed": "val"})
        self.assertTrue(evt.is_sync_marker)
        self.assertIs(evt.error, err)

    def test_base_client_unsupported_operations(self):
        client = DummyClient()
        self.assertTrue(issubclass(UnsupportedOperationError, NotImplementedError))

        with self.assertRaises(UnsupportedOperationError) as cm:
            client.execute_subscribe(
                selectors=[PathSelector(paths=["/test"])],
                options=GnmiOptions(),
                context=StreamContext(),
            )
        self.assertIn("DummyClient does not support subscribe operations", str(cm.exception))

    def test_gnmi_client_subscribe_generator(self):
        from specs.gnmi_client import GNMIClient
        from specs.gnmi import gnmi_pb2
        from unittest.mock import MagicMock

        client = GNMIClient(target="10.0.0.1:9339", insecure=True)
        ctx = StreamContext()

        gen = client._build_subscribe_generator(
            selectors=[PathSelector(paths=["/interfaces/interface[name=eth0]"], prefix="/openconfig")],
            options=GnmiOptions(encoding="json_ietf", sub_mode="sample", sample_interval_ns=1000000),
            context=ctx,
        )

        # 1. First item yielded is the initial SubscribeRequest
        initial_req = next(gen)
        self.assertTrue(initial_req.HasField("subscribe"))
        self.assertEqual(len(initial_req.subscribe.subscription), 1)
        self.assertEqual(initial_req.subscribe.mode, gnmi_pb2.SubscriptionList.STREAM)

        # 2. Trigger POLL
        ctx.request_poll()
        poll_req = next(gen)
        self.assertTrue(poll_req.HasField("poll"))

        # 3. Trigger CANCEL
        ctx.cancel()
        with self.assertRaises(StopIteration):
            next(gen)

    def test_gnmi_client_execute_subscribe(self):
        from specs.gnmi_client import GNMIClient
        from unittest.mock import MagicMock

        client = GNMIClient(target="10.0.0.1:9339", insecure=True)
        client.stub = MagicMock()

        mock_resp1 = MagicMock()
        mock_resp1.sync_response = False
        mock_resp2 = MagicMock()
        mock_resp2.sync_response = True

        client.stub.Subscribe.return_value = [mock_resp1, mock_resp2]

        ctx = StreamContext()
        events = list(client.execute_subscribe(
            selectors=[PathSelector(paths=["/interfaces"])],
            context=ctx,
        ))

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].protocol, "gnmi")
        self.assertFalse(events[0].is_sync_marker)
        self.assertIs(events[0].raw_payload, mock_resp1)

        self.assertTrue(events[1].is_sync_marker)
        self.assertIs(events[1].raw_payload, mock_resp2)

    def test_netconf_client_execute_subscribe_raises(self):
        from specs.netconf_client import NetconfClient
        client = NetconfClient(target="10.0.0.1:830", username="admin", password="pwd")
        with self.assertRaises(UnsupportedOperationError) as cm:
            client.execute_subscribe(selectors=[PathSelector(paths=["/interfaces"])])
        self.assertIn("NetconfClient does not support subscribe operations", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
