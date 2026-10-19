import os
import sys
import unittest
from unittest.mock import MagicMock, patch

try:
    import pytest
except ImportError:
    pytest = None

# Ensure project root directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Provide mock fallback for external protocol transport libraries if not installed in the environment
for m in [
    'grpc', 'ncclient', 'ncclient.manager',
    'specs.gnmi.gnmi_pb2', 'specs.gnmi.gnmi_pb2_grpc',
    'google', 'google.protobuf', 'google.protobuf.text_format',
    'xmltodict', 'lxml', 'lxml.etree'
]:
    if m not in sys.modules:
        try:
            __import__(m)
        except ImportError:
            mock_obj = MagicMock()
            if m == 'grpc':
                mock_obj.__version__ = '1.78.0'
                mock_util = MagicMock()
                mock_util.first_version_is_lower = lambda v1, v2: False
                sys.modules['grpc._utilities'] = mock_util
            elif m == 'specs.gnmi.gnmi_pb2':
                mock_obj.JSON_IETF = 4
                mock_obj.JSON = 0
                mock_obj.PROTO = 2
                mock_obj.ASCII = 3
                mock_obj.BYTES = 1
            sys.modules[m] = mock_obj

from ui.cmd import (
    SessionConfig,
    ParsedConfig,
)
from config import (
    Protocol,
    OutputFormat,
    OutputType,
    OutputConfig,
    ConnectionConfig,
    ExecutionConfig,
    PathSelector,
    FilterSelector,
    DeliveryMode,
    DeliveryPolicy,
    CapabilitiesOperation,
    GetOperation,
    GetConfigOperation,
    SetOperation,
    EditConfigOperation,
    SubscribeOperation,
    Change,
    ChangeType,
    GNMIOptions,
    NetconfOptions,
)
from managers.manager import BaseRPCManager, UnaryManager, SubscriptionManager, ManagerFactory
from managers.unary_worker import BaseUnaryWorker, GetWorker, SetWorker, CapabilityWorker, SequentialWorker, UnaryWorker
from managers.subscribe_session import SubscribeSession
from specs.stream_types import StreamEvent, StreamCommandType, StreamContext

def build_sample_sessions():
    get_session_1 = SessionConfig(
        connection=ConnectionConfig(
            target='10.0.0.1:9339'
        ),
        protocol=Protocol.GNMI,
        operation=GetOperation(
            selector=PathSelector(
                paths=['/interfaces/interface[name=eth0]']
            )
        )
    )
    get_session_2 = SessionConfig(
        connection=ConnectionConfig(
            target='10.0.0.2:9339'
        ),
        protocol=Protocol.GNMI,
        operation=GetOperation(
            selector=PathSelector(
                paths=['/system-info/status']
            )
        )
    )
    set_session = SessionConfig(
        connection=ConnectionConfig(
            target='10.0.0.3:9339'
        ),
        protocol=Protocol.GNMI,
        operation=SetOperation(
            changes=([Change(path='/system/config/hostname', operation=ChangeType.MERGE, value="router3")])
        )
    )
    cap_session = SessionConfig(
        connection=ConnectionConfig(
            target='10.0.0.4:9339'
        ),
        protocol=Protocol.GNMI,
        operation=CapabilitiesOperation()
    )
    sub_session_1 = SessionConfig(
        connection=ConnectionConfig(
            target='10.0.0.5:9339'
        ),
        protocol=Protocol.GNMI,
        operation=SubscribeOperation(
            subscription_name='sub_telemetry',
            selector=PathSelector(paths=["/interfaces"]),
            delivery=DeliveryPolicy(mode=DeliveryMode.PERIODIC)
        )
    )
    sub_session_2 = SessionConfig(
        connection=ConnectionConfig(
            target='10.0.0.6:9339'
        ),
        protocol=Protocol.GNMI,
        operation=SubscribeOperation(
            subscription_name='sub_poll',
            selector=PathSelector(paths=["/interfaces"]),
            delivery=DeliveryPolicy(mode=DeliveryMode.ON_DEMAND)
        )
    )

    return {
        'get_1': get_session_1,
        'get_2': get_session_2,
        'set': set_session,
        'cap': cap_session,
        'sub_1': sub_session_1,
        'sub_2': sub_session_2,
        'all': [get_session_1, get_session_2, set_session, cap_session, sub_session_1, sub_session_2],
    }


def build_parsed_config(sample_sessions):
    return ParsedConfig(
        sessions=sample_sessions['all'],
        outputs=[OutputConfig(
            output_type=OutputType.STDOUT,
            format=OutputFormat.JSON,
        )],
        targets=[
            '10.0.0.1:9339', '10.0.0.2:9339', '10.0.0.3:9339',
            '10.0.0.4:9339', '10.0.0.5:9339', '10.0.0.6:9339'
        ],
        debug=False
    )


if pytest:
    @pytest.fixture
    def sample_sessions():
        return build_sample_sessions()

    @pytest.fixture
    def parsed_config(sample_sessions):
        return build_parsed_config(sample_sessions)


class TestManagerSessionScoping(unittest.TestCase):
    def setUp(self):
        self.sample_sessions = build_sample_sessions()
        self.parsed_config = build_parsed_config(self.sample_sessions)

    def test_manager_factory_partitioning(self):
        managers, handlers = ManagerFactory.create_managers(self.parsed_config)

        self.assertEqual(len(managers), 2)
        self.assertIsInstance(managers[0], UnaryManager)
        self.assertIsInstance(managers[1], SubscriptionManager)

        unary_mgr = managers[0]
        self.assertEqual(len(unary_mgr.session_configs), 4)

        sub_mgr = managers[1]
        self.assertEqual(len(sub_mgr.session_configs), 2)

    def test_unary_manager_build_sessions_dispatch(self):
        unary_sessions = [self.sample_sessions['get_1'], self.sample_sessions['set'], self.sample_sessions['cap']]
        mock_handler = MagicMock()
        mgr = UnaryManager(sessions=unary_sessions, output_handlers=[mock_handler])

        mgr.build_sessions()

        self.assertEqual(len(mgr.sessions), 3)
        self.assertIsInstance(mgr.sessions[0], GetWorker)
        self.assertEqual(mgr.sessions[0].config, self.sample_sessions['get_1'])
        self.assertEqual(mgr.sessions[0].target_ip, '10.0.0.1')
        self.assertEqual(mgr.sessions[0].target_port, 9339)

        self.assertIsInstance(mgr.sessions[1], SetWorker)
        self.assertEqual(mgr.sessions[1].config, self.sample_sessions['set'])
        self.assertEqual(mgr.sessions[1].target_ip, '10.0.0.3')
        self.assertEqual(mgr.sessions[1].updates, [('/system/config/hostname', 'router3')])

        self.assertIsInstance(mgr.sessions[2], CapabilityWorker)
        self.assertEqual(mgr.sessions[2].config, self.sample_sessions['cap'])
        self.assertEqual(mgr.sessions[2].target_ip, '10.0.0.4')

    def test_subscription_manager_build_sessions(self):
        sub_sessions = [self.sample_sessions['sub_1'], self.sample_sessions['sub_2']]
        mock_handler = MagicMock()
        mgr = SubscriptionManager(sessions=sub_sessions, output_handlers=[mock_handler])

        mgr.build_sessions()

        self.assertEqual(len(mgr.sessions), 2)
        for s in mgr.sessions:
            self.assertIsInstance(s, SubscribeSession)

        self.assertEqual(mgr.sessions[0].config, self.sample_sessions['sub_1'])
        self.assertEqual(mgr.sessions[0].target_ip, '10.0.0.5')
        self.assertEqual(mgr.sessions[0].subscription_name, 'sub_telemetry')

        self.assertEqual(mgr.sessions[1].config, self.sample_sessions['sub_2'])
        self.assertEqual(mgr.sessions[1].target_ip, '10.0.0.6')
        self.assertEqual(mgr.sessions[1].subscription_name, 'sub_poll')

    def test_output_handlers_centralized_lifecycle(self):
        mock_mgr_unary = MagicMock(spec=UnaryManager)
        mock_mgr_sub = MagicMock(spec=SubscriptionManager)
        mock_handler = MagicMock()

        with patch.object(ManagerFactory, 'create_managers', return_value=([mock_mgr_unary, mock_mgr_sub], [mock_handler])):
            ManagerFactory.execute(self.parsed_config)

            mock_mgr_unary.run_all.assert_called_once()
            mock_mgr_sub.run_all.assert_called_once()
            mock_handler.close.assert_called_once()


class TestPerRPCSessionConfigHierarchy(unittest.TestCase):
    def test_attribute_isolation_gnmi(self):
        cap_cfg = SessionConfig(
            connection=ConnectionConfig(
                target="10.0.0.1:9339"
            ),
            protocol=Protocol.GNMI,
            operation=CapabilitiesOperation()
        )
        self.assertTrue(hasattr(cap_cfg, 'paths'))
        self.assertTrue(hasattr(cap_cfg, 'updates'))
        self.assertTrue(hasattr(cap_cfg, 'sample_interval'))

        get_cfg = SessionConfig(
            connection=ConnectionConfig(
                target="10.0.0.1:9339"
            ),
            protocol=Protocol.GNMI,
            operation=GetOperation(
                selector=PathSelector(
                    paths=["/interface"],
                    prefix="openconfig-interfaces:/interfaces"
                )
            )
        )
        self.assertEqual(get_cfg.paths, ["/interface"])
        self.assertTrue(hasattr(get_cfg, 'updates'))
        self.assertTrue(hasattr(get_cfg, 'sample_interval'))

        set_cfg = SessionConfig(
            connection=ConnectionConfig(
                target="10.0.0.1:9339"
            ),
            protocol=Protocol.GNMI,
            operation=SetOperation(
                operation=GNMIOptions(encoding="json_ietf"),
                changes=tuple([Change(
                    operation=ChangeType.MERGE,
                    path="/path", value="val"
                    )])
            )
        )
        self.assertEqual(set_cfg.operation.changes[0], ("/path", ChangeType.MERGE, "val"))
        self.assertTrue(hasattr(set_cfg, 'paths'))
        self.assertTrue(hasattr(set_cfg, 'sample_interval'))

        sub_cfg = SessionConfig(
            connection=ConnectionConfig(
                target="10.0.0.1:9339"
            ),
            protocol=Protocol.GNMI,
            operation=SubscribeOperation(
                selector=PathSelector(
                    paths=("/interfaces"),
                ),
                delivery=DeliveryPolicy(
                    mode=DeliveryMode.PERIODIC,
                    interval=30,
                    heartbeat=0,
                    suppress_redundant=False
                ),
                protocol_options=GNMIOptions(
                    encoding="json_ietf",
                    updates_only=False
                )
            )
        )
        self.assertEqual(sub_cfg.operation.delivery.interval, 30)
        self.assertTrue(hasattr(sub_cfg, 'updates'))


class TestSemanticManagerSessionScoping(unittest.TestCase):
    def setUp(self):
        conn1 = ConnectionConfig(target='10.0.0.1:9339')
        conn2 = ConnectionConfig(target='10.0.0.2:9339')
        conn3 = ConnectionConfig(target='10.0.0.3:9339')
        conn4 = ConnectionConfig(target='10.0.0.4:9339')
        conn5 = ConnectionConfig(target='10.0.0.5:9339')
        conn6 = ConnectionConfig(target='10.0.0.6:9339')

        self.get_1 = SessionConfig(
            connection=conn1, protocol=Protocol.GNMI,
            operation=GetOperation(selector=PathSelector(paths=['/interfaces/interface[name=eth0]']))
        )
        self.get_2 = SessionConfig(
            connection=conn2, protocol=Protocol.GNMI,
            operation=GetOperation(selector=PathSelector(paths=['/system/config']))
        )
        self.set_1 = SessionConfig(
            connection=conn3, protocol=Protocol.GNMI,
            operation=SetOperation(changes=[Change(path='/system/config/hostname', operation=ChangeType.MERGE, value='router3')])
        )
        self.cap_1 = SessionConfig(
            connection=conn4, protocol=Protocol.GNMI,
            operation=CapabilitiesOperation()
        )
        self.sub_1 = SessionConfig(
            connection=conn5, protocol=Protocol.GNMI,
            operation=SubscribeOperation(
                selector=PathSelector(paths=['/interfaces/...']),
                delivery=DeliveryPolicy(mode=DeliveryMode.PERIODIC),
                subscription_name='sub_telemetry'
            )
        )
        self.sub_2 = SessionConfig(
            connection=conn6, protocol=Protocol.GNMI,
            operation=SubscribeOperation(
                selector=PathSelector(paths=['/components/...']),
                delivery=DeliveryPolicy(mode=DeliveryMode.ON_DEMAND),
                subscription_name='sub_poll'
            )
        )
        self.all_sessions = [self.get_1, self.get_2, self.set_1, self.cap_1, self.sub_1, self.sub_2]
        self.parsed_config = ParsedConfig(
            sessions=self.all_sessions,
            outputs=[OutputConfig(
                output_type=OutputType.STDERR,
                format=OutputFormat.XML,
            )],
            targets=['10.0.0.1:9339', '10.0.0.2:9339', '10.0.0.3:9339', '10.0.0.4:9339', '10.0.0.5:9339', '10.0.0.6:9339']
        )

    def test_semantic_manager_factory_partitioning(self):
        managers, handlers = ManagerFactory.create_managers(self.parsed_config)
        self.assertEqual(len(managers), 2)
        self.assertIsInstance(managers[0], UnaryManager)
        self.assertIsInstance(managers[1], SubscriptionManager)
        self.assertEqual(len(managers[0].session_configs), 4)
        self.assertEqual(len(managers[1].session_configs), 2)

    def test_semantic_unary_manager_dispatch(self):
        unary_sessions = [self.get_1, self.set_1, self.cap_1]
        mgr = UnaryManager(sessions=unary_sessions, output_handlers=[MagicMock()])
        mgr.build_sessions()
        self.assertEqual(len(mgr.sessions), 3)
        self.assertIsInstance(mgr.sessions[0], GetWorker)
        self.assertIsInstance(mgr.sessions[1], SetWorker)
        self.assertIsInstance(mgr.sessions[2], CapabilityWorker)

    def test_semantic_subscription_manager_dispatch(self):
        sub_sessions = [self.sub_1, self.sub_2]
        mgr = SubscriptionManager(sessions=sub_sessions, output_handlers=[MagicMock()])
        mgr.build_sessions()
        self.assertEqual(len(mgr.sessions), 2)
        self.assertIsInstance(mgr.sessions[0], SubscribeSession)
        self.assertIsInstance(mgr.sessions[1], SubscribeSession)
        self.assertFalse(mgr.sessions[0].is_poll_mode())
        self.assertTrue(mgr.sessions[1].is_poll_mode())

    def test_subscribe_session_streaming_execution(self):
        import queue
        data_q = queue.Queue()
        session = SubscribeSession(config=self.sub_1, data_queue=data_q)

        mock_event1 = StreamEvent(
            protocol="gnmi",
            timestamp=123.456,
            raw_payload={"update": "val1"},
            is_sync_marker=False,
        )
        mock_event2 = StreamEvent(
            protocol="gnmi",
            timestamp=123.457,
            raw_payload={"update": "val2"},
            is_sync_marker=True,
        )

        mock_client = MagicMock()
        mock_client.execute_subscribe.return_value = [mock_event1, mock_event2]

        with patch('managers.subscribe_session.ClientFactory.get_client') as mock_factory:
            mock_factory.return_value.__enter__.return_value = mock_client
            session.start()

        mock_client.execute_subscribe.assert_called_once_with(
            operation=self.sub_1.operation,
            context=session.stream_context,
        )

        self.assertEqual(data_q.qsize(), 2)
        msg1 = data_q.get_nowait()
        self.assertEqual(msg1['subscription_name'], 'sub_telemetry')
        self.assertEqual(msg1['data'], {"update": "val1"})
        self.assertFalse(msg1['is_sync_marker'])

        msg2 = data_q.get_nowait()
        self.assertEqual(msg2['data'], {"update": "val2"})
        self.assertTrue(msg2['is_sync_marker'])

    def test_subscribe_session_poll_dispatch(self):
        session = SubscribeSession(config=self.sub_2)
        self.assertTrue(session.is_poll_mode())

        # Trigger poll
        session.trigger_poll()
        cmd = session.stream_context.command_queue.get_nowait()
        self.assertEqual(cmd.command_type, StreamCommandType.POLL)

        # Stop session
        session.stop()
        self.assertTrue(session.stream_context.is_cancelled)

    def test_unary_manager_sequential_worker_resolution(self):
        conn = ConnectionConfig(target='172.20.20.2:830')
        multi_op_session = SessionConfig(
            connection=conn,
            protocol=Protocol.NETCONF,
            operations=[
                CapabilitiesOperation(),
                GetConfigOperation(protocol_options=NetconfOptions(source="running")),
            ]
        )
        mgr = UnaryManager(sessions=[multi_op_session], output_handlers=[MagicMock()])
        mgr.build_sessions()
        self.assertEqual(len(mgr.sessions), 1)
        self.assertIsInstance(mgr.sessions[0], SequentialWorker)

    def test_sequential_worker_single_session_lifecycle(self):
        conn = ConnectionConfig(target='172.20.20.2:830')
        cap_op = CapabilitiesOperation()
        get_op = GetConfigOperation(protocol_options=NetconfOptions(source="running"))
        multi_op_session = SessionConfig(
            connection=conn,
            protocol=Protocol.NETCONF,
            operations=[cap_op, get_op]
        )
        worker = SequentialWorker(config=multi_op_session)

        mock_client = MagicMock()
        mock_client.execute.side_effect = ["CAP_RES", "GET_RES"]

        with patch.object(worker, '_get_client') as mock_get_client:
            mock_get_client.return_value.__enter__.return_value = mock_client
            results = worker.start()

        mock_get_client.return_value.__enter__.assert_called_once()
        self.assertEqual(mock_client.execute.call_count, 2)
        mock_client.execute.assert_any_call(cap_op)
        mock_client.execute.assert_any_call(get_op)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['rpc'], 'capability')
        self.assertEqual(results[0]['data'], 'CAP_RES')
        self.assertEqual(results[1]['rpc'], 'get-config')
        self.assertEqual(results[1]['data'], 'GET_RES')

    def test_output_handler_list_and_text_formatting(self):
        import io
        from modules.output import OutputHandler
        out_cfg = OutputConfig(output_type=OutputType.STDOUT, format=OutputFormat.TEXT)
        handler = OutputHandler(out_cfg)
        buf = io.StringIO()
        handler.stream = buf

        messages = [
            {'target': '172.20.20.2:830', 'rpc': 'capability', 'data': ['cap1', 'cap2'], 'protocol': 'netconf'},
            {'target': '172.20.20.2:830', 'rpc': 'get-schema', 'data': 'module test {}', 'protocol': 'netconf'}
        ]
        handler.write(messages)
        output = buf.getvalue()
        self.assertIn("--- [Operation: capability] ---", output)
        self.assertIn("--- [Operation: get-schema] ---", output)
        self.assertIn("module test {}", output)

    def test_sequential_worker_gnmi_pipeline(self):
        cap_op = CapabilitiesOperation()
        get_op = GetOperation(selector=PathSelector(paths=("/system/state",)), read_scope="all")
        set_op = SetOperation(changes=(Change(path="/interfaces/interface[name=mgmt0]/config/description", operation=ChangeType.MERGE, value="test"),))

        session = SessionConfig(
            connection=ConnectionConfig(target='172.20.20.2:57401'),
            protocol=Protocol.GNMI,
            operations=(cap_op, get_op, set_op)
        )
        worker = SequentialWorker(config=session)

        mock_client = MagicMock()
        mock_client.execute.side_effect = ["GNMI_CAP_RES", "GNMI_GET_RES", "GNMI_SET_RES"]

        with patch.object(worker, '_get_client') as mock_get_client:
            mock_get_client.return_value.__enter__.return_value = mock_client
            results = worker.start()

        mock_get_client.return_value.__enter__.assert_called_once()
        self.assertEqual(mock_client.execute.call_count, 3)
        mock_client.execute.assert_any_call(cap_op)
        mock_client.execute.assert_any_call(get_op)
        mock_client.execute.assert_any_call(set_op)

        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]['rpc'], 'capability')
        self.assertEqual(results[0]['data'], 'GNMI_CAP_RES')
        self.assertEqual(results[1]['rpc'], 'get')
        self.assertEqual(results[1]['data'], 'GNMI_GET_RES')
        self.assertEqual(results[2]['rpc'], 'set')
        self.assertEqual(results[2]['data'], 'GNMI_SET_RES')


if __name__ == '__main__':
    unittest.main()
