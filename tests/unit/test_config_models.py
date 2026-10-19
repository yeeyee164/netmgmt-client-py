import os
import sys
import unittest
from unittest.mock import MagicMock
from dataclasses import FrozenInstanceError

# Ensure project root directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import (
    Protocol,
    OutputFormat,
    OutputType,
    OutputConfig,
    ConnectionConfig,
    ExecutionConfig,
    SessionConfig,
    Selector,
    PathSelector,
    FilterSelector,
    DeliveryMode,
    DeliveryPolicy,
    OperationConfig,
    CapabilitiesOperation,
    GetSchemaOperation,
    GetOperation,
    GetConfigOperation,
    ChangeType,
    Change,
    SetOperation,
    EditConfigOperation,
    SubscribeOperation,
    GNMIOptions,
    GnmiOptions,
    BaseProtocolOptions,
    NetconfOptions,
    SelectorConfig,
)
from modules.security import SecurityProfile


class TestConfigModels(unittest.TestCase):

    def test_protocol_enum(self):
        self.assertEqual(Protocol.GNMI, "gnmi")
        self.assertEqual(Protocol.NETCONF, "netconf")
        self.assertEqual(Protocol.RESTCONF, "restconf")

    def test_connection_config_and_target_properties(self):
        conn = ConnectionConfig(target="192.168.1.1:9339", username="admin", password="pwd")
        self.assertEqual(conn.target_ip, "192.168.1.1")
        self.assertEqual(conn.target_port, 9339)
        self.assertEqual(conn.username, "admin")
        self.assertFalse(conn.insecure)

        # IPv6 target
        conn_v6 = ConnectionConfig(target="[2001:db8::1]:830")
        self.assertEqual(conn_v6.target_ip, "2001:db8::1")
        self.assertEqual(conn_v6.target_port, 830)

        # Hostname target without port
        conn_host = ConnectionConfig(target="router.local")
        self.assertEqual(conn_host.target_ip, "router.local")
        self.assertEqual(conn_host.target_port, 0)

        # Immutability
        with self.assertRaises(FrozenInstanceError):
            conn.username = "new_admin"

    def test_execution_config(self):
        exec_cfg = ExecutionConfig(timeout=60, times=3)
        self.assertEqual(exec_cfg.timeout, 60)
        self.assertEqual(exec_cfg.times, 3)
        self.assertEqual(exec_cfg.retry_count, 0)

        with self.assertRaises(FrozenInstanceError):
            exec_cfg.timeout = 10

    def test_selectors(self):
        # PathSelector with list
        ps1 = PathSelector(paths=["/interfaces/interface", "/system"])
        self.assertEqual(ps1.paths, ("/interfaces/interface", "/system"))
        self.assertEqual(ps1.prefix, "")

        # PathSelector with string
        ps2 = PathSelector(paths="/interfaces/interface", prefix="/openconfig")
        self.assertEqual(ps2.paths, ("/interfaces/interface",))
        self.assertEqual(ps2.prefix, "/openconfig")

        # FilterSelector
        fs = FilterSelector(expression="<filter/>", filter_type="subtree")
        self.assertEqual(fs.expression, "<filter/>")
        self.assertEqual(fs.filter_type, "subtree")

        with self.assertRaises(FrozenInstanceError):
            ps1.prefix = "change"

    def test_delivery_policy(self):
        dp = DeliveryPolicy(mode=DeliveryMode.EVENT_DRIVEN, interval=10, heartbeat=30, suppress_redundant=True)
        self.assertEqual(dp.mode, DeliveryMode.EVENT_DRIVEN)
        self.assertEqual(dp.interval, 10)
        self.assertEqual(dp.heartbeat, 30)
        self.assertTrue(dp.suppress_redundant)

        with self.assertRaises(FrozenInstanceError):
            dp.interval = 5

    def test_operations_and_changes(self):
        # Capabilities
        cap = CapabilitiesOperation()
        self.assertIsInstance(cap, OperationConfig)

        # GetSchema
        schema_op = GetSchemaOperation(identifier="ietf-interfaces", version="2018-02-20")
        self.assertEqual(schema_op.identifier, "ietf-interfaces")
        self.assertEqual(schema_op.version, "2018-02-20")
        self.assertEqual(schema_op.format, "yang")

        # GetOperation
        get_op = GetOperation(selector=PathSelector(paths=["/interfaces"]), read_scope="config")
        self.assertEqual(get_op.read_scope, "config")
        self.assertEqual(get_op.selector.paths, ("/interfaces",))

        # Change & SetOperation
        c1 = Change(path="/interfaces/interface[name=eth0]/config/enabled", operation=ChangeType.MERGE, value=True)
        c2 = Change(path="/interfaces/interface[name=eth1]", operation=ChangeType.DELETE)
        set_op = SetOperation(changes=[c1, c2])
        self.assertEqual(len(set_op.changes), 2)
        self.assertEqual(set_op.changes[0].operation, ChangeType.MERGE)
        self.assertEqual(set_op.changes[1].operation, ChangeType.DELETE)

        # SubscribeOperation
        sub_op = SubscribeOperation(
            selector=PathSelector(paths=["/interfaces"]),
            delivery=DeliveryPolicy(mode=DeliveryMode.PERIODIC, interval=5),
            subscription_name="test_sub"
        )
        self.assertEqual(sub_op.subscription_name, "test_sub")
        self.assertEqual(sub_op.delivery.mode, DeliveryMode.PERIODIC)

    def test_protocol_options(self):
        gnmi_opts = GNMIOptions(encoding="proto", updates_only=True)
        self.assertEqual(gnmi_opts.encoding, "proto")
        self.assertTrue(gnmi_opts.updates_only)

        nc_opts = NetconfOptions(target_datastore="running", default_operation="replace")
        self.assertEqual(nc_opts.target_datastore, "running")
        self.assertEqual(nc_opts.default_operation, "replace")

    def test_polymorphic_protocol_options_and_subscribe_operation(self):
        # Base class inheritance and to_dict
        gnmi_opts = GnmiOptions(
            encoding="json_ietf",
            sub_mode="sample",
            sample_interval_ns=1000000,
            heartbeat_interval_ns=5000000,
            suppress_redundant=True,
        )
        self.assertIsInstance(gnmi_opts, BaseProtocolOptions)
        gnmi_opts.validate()
        d = gnmi_opts.to_dict()
        self.assertEqual(d["encoding"], "json_ietf")
        self.assertEqual(d["sub_mode"], "sample")
        self.assertEqual(d["sample_interval_ns"], 1000000)

        # GnmiOptions validation errors
        with self.assertRaises(ValueError):
            GnmiOptions(encoding="invalid_enc").validate()
        with self.assertRaises(ValueError):
            GnmiOptions(sub_mode="invalid_mode").validate()
        with self.assertRaises(ValueError):
            GnmiOptions(sample_interval_ns=-1).validate()
        with self.assertRaises(ValueError):
            GnmiOptions(heartbeat_interval_ns=-1).validate()

        # NetconfOptions inheritance, synchronization, and to_dict
        nc_opts = NetconfOptions(
            target_datastore="candidate",
            source="running",
            default_operation="merge",
            error_option="stop-on-error",
            lock_target=True,
        )
        self.assertIsInstance(nc_opts, BaseProtocolOptions)
        self.assertEqual(nc_opts.source_datastore, "running")
        nc_opts.validate()
        nc_dict = nc_opts.to_dict()
        self.assertEqual(nc_dict["target_datastore"], "candidate")
        self.assertTrue(nc_dict["lock_target"])

        # NetconfOptions validation errors
        with self.assertRaises(ValueError):
            NetconfOptions(target_datastore="invalid_ds").validate()
        with self.assertRaises(ValueError):
            NetconfOptions(source="invalid_source").validate()
        with self.assertRaises(ValueError):
            NetconfOptions(default_operation="invalid_op").validate()
        with self.assertRaises(ValueError):
            NetconfOptions(error_option="invalid_err").validate()

        # SubscribeOperation validation with polymorphic protocol_options
        sub_op = SubscribeOperation(
            selector=PathSelector(paths=["/interfaces"]),
            delivery=DeliveryPolicy(mode=DeliveryMode.PERIODIC, interval=10),
            protocol_options=gnmi_opts,
        )
        sub_op.validate()
        self.assertEqual(sub_op.protocol_options.encoding, "json_ietf")

        # SubscribeOperation invalid protocol_options triggers ValueError
        sub_invalid = SubscribeOperation(
            selector=PathSelector(paths=["/interfaces"]),
            delivery=DeliveryPolicy(),
            protocol_options=GnmiOptions(sample_interval_ns=-10),
        )
        with self.assertRaises(ValueError):
            sub_invalid.validate()

    def test_session_config_delegation(self):
        conn = ConnectionConfig(target="10.0.0.1:9339", username="admin", password="pwd", insecure=True)
        op = CapabilitiesOperation()
        exec_cfg = ExecutionConfig(times=2)
        session = SessionConfig(
            connection=conn,
            protocol=Protocol.GNMI,
            operation=op,
            execution=exec_cfg
        )

        self.assertEqual(session.target, "10.0.0.1:9339")
        self.assertEqual(session.target_ip, "10.0.0.1")
        self.assertEqual(session.target_port, 9339)
        self.assertEqual(session.username, "admin")
        self.assertEqual(session.password, "pwd")
        self.assertTrue(session.insecure)
        self.assertEqual(session.times, 2)
        self.assertEqual(session.protocol, Protocol.GNMI)
        self.assertIs(session.operation, op)

        with self.assertRaises(FrozenInstanceError):
            session.protocol = Protocol.NETCONF


class TestClientSemanticExecution(unittest.TestCase):
    def test_gnmi_client_execute_capabilities(self):
        from specs.gnmi_client import GNMIClient
        client = GNMIClient(target="10.0.0.1:9339", insecure=True)
        client.stub = MagicMock()
        cap_op = CapabilitiesOperation()
        client.execute_capabilities(cap_op)
        client.stub.Capabilities.assert_called_once()

    def test_gnmi_client_execute_get(self):
        from specs.gnmi_client import GNMIClient
        client = GNMIClient(target="10.0.0.1:9339", insecure=True)
        client.stub = MagicMock()
        get_op = GetOperation(
            selector=PathSelector(paths=["/interfaces/interface[name=eth0]"], prefix="/openconfig"),
            read_scope="config",
            protocol_options=GNMIOptions(encoding="json")
        )
        client.execute_get(get_op)
        client.stub.Get.assert_called_once()
        req = client.stub.Get.call_args[0][0]
        self.assertEqual(len(req.path), 1)

    def test_gnmi_client_execute_set(self):
        from specs.gnmi_client import GNMIClient
        client = GNMIClient(target="10.0.0.1:9339", insecure=True)
        client.stub = MagicMock()
        set_op = SetOperation(changes=[
            Change(path="/interfaces/interface[name=eth0]/config/description", operation=ChangeType.MERGE, value="Test"),
            Change(path="/interfaces/interface[name=eth1]", operation=ChangeType.DELETE),
            Change(path="/interfaces/interface[name=eth2]/config", operation=ChangeType.REPLACE, value={"enabled": True}),
        ])
        client.execute_set(set_op)
        client.stub.Set.assert_called_once()
        req = client.stub.Set.call_args[0][0]
        self.assertEqual(len(req.update), 1)
        self.assertEqual(len(req.delete), 1)
        self.assertEqual(len(req.replace), 1)

    def test_netconf_client_execute_capabilities(self):
        from specs.netconf_client import NetconfClient
        client = NetconfClient(target="10.0.0.1:830", username="admin", password="pwd")
        mock_session = MagicMock()
        mock_session.server_capabilities = ["urn:ietf:params:netconf:base:1.0"]
        client.session = mock_session
        res = client.execute_capabilities(CapabilitiesOperation())
        self.assertEqual(res, ["urn:ietf:params:netconf:base:1.0"])

    def test_netconf_client_execute_get_config(self):
        from specs.netconf_client import NetconfClient
        client = NetconfClient(target="10.0.0.1:830", username="admin", password="pwd")
        mock_session = MagicMock()
        client.session = mock_session
        get_op = GetOperation(
            selector=FilterSelector(expression="<filter/>"),
            read_scope="config",
            protocol_options=NetconfOptions(source="running")
        )
        client.execute_get(get_op)
        mock_session.get_config.assert_called_once_with(source="running", filter="<filter/>")

    def test_netconf_client_execute_schema(self):
        from specs.netconf_client import NetconfClient
        client = NetconfClient(target="10.0.0.1:830", username="admin", password="pwd")
        mock_session = MagicMock()
        client.session = mock_session
        schema_op = GetSchemaOperation(identifier="ietf-interfaces", version="2018-02-20", format="yang")
        client.execute_schema(schema_op)
        mock_session.get_schema.assert_called_once_with(identifier="ietf-interfaces", version="2018-02-20", format=None)

    def test_netconf_client_execute_set(self):
        from specs.netconf_client import NetconfClient
        client = NetconfClient(target="10.0.0.1:830", username="admin", password="pwd")
        mock_session = MagicMock()
        mock_session.server_capabilities = [
            'urn:ietf:params:netconf:base:1.0',
            'urn:ietf:params:netconf:capability:candidate:1.0'
        ]
        client.session = mock_session
        set_op = SetOperation(
            changes=[Change(path="/test", operation=ChangeType.MERGE, value="<test>val</test>")],
            protocol_options=NetconfOptions(target_datastore="candidate", commit=True)
        )
        client.execute_set(set_op)
        mock_session.edit_config.assert_called_once()
        mock_session.commit.assert_called_once()


class TestWorkersAndManagersSemanticIntegration(unittest.TestCase):
    def test_manager_factory_with_semantic_sessions(self):
        from managers.manager import ManagerFactory, UnaryManager, SubscriptionManager
        from ui.cmd import ParsedConfig

        conn = ConnectionConfig(target="10.0.0.1:9339")
        get_sess = SessionConfig(connection=conn, protocol=Protocol.GNMI, operation=GetOperation(selector=PathSelector(paths=["/interfaces"])))
        sub_sess = SessionConfig(
            connection=conn,
            protocol=Protocol.GNMI,
            operation=SubscribeOperation(
                selector=PathSelector(paths=["/interfaces"]),
                delivery=DeliveryPolicy(mode=DeliveryMode.PERIODIC, interval=10)
            )
        )
        parsed = ParsedConfig(
            sessions=[get_sess, sub_sess],
            outputs=[OutputConfig(
                format = OutputFormat.JSON,
                output_type = OutputType.STDOUT,
            )],
        )
        managers, handlers = ManagerFactory.create_managers(parsed)
        self.assertEqual(len(managers), 2)
        self.assertIsInstance(managers[0], UnaryManager)
        self.assertIsInstance(managers[1], SubscriptionManager)

    def test_unary_manager_worker_resolution_semantic(self):
        from managers.manager import UnaryManager
        from managers.unary_worker import GetWorker, SetWorker, CapabilityWorker

        conn = ConnectionConfig(target="10.0.0.1:9339")
        get_sess = SessionConfig(connection=conn, protocol=Protocol.GNMI, operation=GetOperation(selector=PathSelector(paths=["/interfaces"])))
        set_sess = SessionConfig(connection=conn, protocol=Protocol.GNMI, operation=SetOperation(changes=[Change(path="/test", operation=ChangeType.MERGE, value="val")]))
        cap_sess = SessionConfig(connection=conn, protocol=Protocol.GNMI, operation=CapabilitiesOperation())

        mgr = UnaryManager(sessions=[get_sess, set_sess, cap_sess], output_handlers=[MagicMock()])
        mgr.build_sessions()

        self.assertEqual(len(mgr.sessions), 3)
        self.assertIsInstance(mgr.sessions[0], GetWorker)
        self.assertIsInstance(mgr.sessions[1], SetWorker)
        self.assertIsInstance(mgr.sessions[2], CapabilityWorker)

    def test_set_worker_semantic_execution(self):
        from managers.unary_worker import SetWorker

        conn = ConnectionConfig(target="10.0.0.1:9339", insecure=True)
        set_sess = SessionConfig(
            connection=conn,
            protocol=Protocol.GNMI,
            operation=SetOperation(changes=[Change(path="/interfaces/interface[name=eth0]/config", operation=ChangeType.MERGE, value="active")])
        )
        worker = SetWorker(config=set_sess)
        with unittest.mock.patch.object(worker, '_get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_client.execute_set.return_value = "SET_SUCCESS"
            mock_get_client.return_value.__enter__.return_value = mock_client
            res = worker.start()
            self.assertEqual(res['data'], "SET_SUCCESS")
            mock_client.execute_set.assert_called_once_with(set_sess.operation)

    def test_subscribe_session_poll_mode(self):
        from managers.subscribe_session import SubscribeSession

        conn = ConnectionConfig(target="10.0.0.1:9339")
        sub_sess_periodic = SessionConfig(
            connection=conn,
            protocol=Protocol.GNMI,
            operation=SubscribeOperation(
                selector=PathSelector(paths=["/interfaces"]),
                delivery=DeliveryPolicy(mode=DeliveryMode.PERIODIC)
            )
        )
        sub_sess_poll = SessionConfig(
            connection=conn,
            protocol=Protocol.GNMI,
            operation=SubscribeOperation(
                selector=PathSelector(paths=["/interfaces"]),
                delivery=DeliveryPolicy(mode=DeliveryMode.ON_DEMAND)
            )
        )
        s1 = SubscribeSession(config=sub_sess_periodic)
        s2 = SubscribeSession(config=sub_sess_poll)
        self.assertFalse(s1.is_poll_mode())
        self.assertTrue(s2.is_poll_mode())

    def test_session_config_with_operations_pipeline(self):
        conn = ConnectionConfig(target="172.20.20.2:830")
        cap_op = CapabilitiesOperation()
        schema_op = GetSchemaOperation(identifier="openconfig-interfaces")
        get_cfg_op = GetConfigOperation(
            selector=FilterSelector(expression="<interfaces/>", filter_type="subtree"),
            protocol_options=NetconfOptions(source="running")
        )
        edit_cfg_op = EditConfigOperation(
            protocol_options=NetconfOptions(target_datastore="candidate", default_operation="merge")
        )

        session = SessionConfig(
            connection=conn,
            protocol=Protocol.NETCONF,
            operations=[cap_op, schema_op, get_cfg_op, edit_cfg_op]
        )

        self.assertEqual(len(session.operations), 4)
        self.assertIs(session.operation, cap_op)
        self.assertEqual(session.target, "172.20.20.2:830")
        self.assertIsInstance(session.operations[2], GetConfigOperation)
        self.assertIsInstance(session.operations[2], GetOperation)
        self.assertIsInstance(session.operations[3], EditConfigOperation)
        self.assertIsInstance(session.operations[3], SetOperation)

    def test_base_client_execute_dispatch(self):
        from specs.base_client import BaseClient
        class MockClient(BaseClient):
            def __exit__(self, *args): pass
            def execute_capabilities(self, op): return "CAP_OK"
            def execute_schema(self, op): return "SCHEMA_OK"
            def execute_get(self, op): return "GET_OK"
            def execute_set(self, op): return "SET_OK"
            def execute_subscribe(self, **kwargs): return "SUB_OK"

        client = MockClient()
        self.assertEqual(client.execute(CapabilitiesOperation()), "CAP_OK")
        self.assertEqual(client.execute(GetSchemaOperation(identifier="m")), "SCHEMA_OK")
        self.assertEqual(client.execute(GetConfigOperation()), "GET_OK")
        self.assertEqual(client.execute(GetOperation(selector=PathSelector(paths=()))), "GET_OK")
        self.assertEqual(client.execute(EditConfigOperation()), "SET_OK")
        self.assertEqual(client.execute(SetOperation()), "SET_OK")
        self.assertEqual(client.execute(SubscribeOperation(selector=PathSelector(paths=()), delivery=DeliveryPolicy())), "SUB_OK")

    def test_parse_operation_mapping_syntax(self):
        from ui.cmd import parse_operation_item
        op1 = parse_operation_item({"capability": {}})
        self.assertIsInstance(op1, CapabilitiesOperation)

        op2 = parse_operation_item({"get-schema": {"identifier": "openconfig-interfaces", "version": "2024-04-04"}})
        self.assertIsInstance(op2, GetSchemaOperation)
        self.assertEqual(op2.identifier, "openconfig-interfaces")
        self.assertEqual(op2.version, "2024-04-04")

        op3 = parse_operation_item({"get-config": {"source": "running", "filter": "<interfaces/>", "filter_type": "subtree"}})
        self.assertIsInstance(op3, GetConfigOperation)
        self.assertEqual(op3.read_scope, "config")
        self.assertEqual(op3.selector.expression, "<interfaces/>")
        self.assertEqual(op3.protocol_options.source, "running")

        op4 = parse_operation_item({"get": {"filter": "/interfaces", "filter_type": "xpath"}})
        self.assertIsInstance(op4, GetOperation)
        self.assertEqual(op4.read_scope, "all")
        self.assertEqual(op4.selector.expression, "/interfaces")

        op5 = parse_operation_item({"edit-config": {"target": "candidate", "config": "<data/>", "default_operation": "replace"}})
        self.assertIsInstance(op5, EditConfigOperation)
        self.assertEqual(op5.protocol_options.target_datastore, "candidate")
        self.assertEqual(op5.protocol_options.config, "<data/>")
        self.assertEqual(op5.protocol_options.default_operation, "replace")

    def test_parse_operation_object_syntax(self):
        from ui.cmd import parse_operation_item
        op1 = parse_operation_item({"operation": "get-schema", "identifier": "ietf-interfaces"})
        self.assertIsInstance(op1, GetSchemaOperation)
        self.assertEqual(op1.identifier, "ietf-interfaces")

        op2 = parse_operation_item({"operation": "get-config", "source": "candidate"})
        self.assertIsInstance(op2, GetConfigOperation)
        self.assertEqual(op2.protocol_options.source, "candidate")

    def test_parse_operation_validation_errors(self):
        from ui.cmd import parse_operation_item, FileConfigError
        # Missing mandatory identifier
        with self.assertRaises(FileConfigError):
            parse_operation_item({"get-schema": {}})

        # Unknown operation type
        with self.assertRaises(FileConfigError):
            parse_operation_item({"unsupported-rpc": {}})

        # Non-dictionary input
        with self.assertRaises(FileConfigError):
            parse_operation_item("get-config")

    def test_file_config_builder_operations_list_parsing(self):
        import tempfile
        from ui.cmd import FileConfigBuilder

        yaml_content = """
protocol: "netconf"
targets:
  172.20.20.2:830:
    username: "admin"
    password: "pwd"
    operations:
      - capability: {}
      - get-schema:
          identifier: "openconfig-interfaces"
      - get-config:
          source: "running"
          filter: "<interfaces/>"
"""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            parsed = FileConfigBuilder(temp_path, protocol="netconf").build()
            self.assertEqual(len(parsed.sessions), 1)
            sess = parsed.sessions[0]
            self.assertEqual(sess.target, "172.20.20.2:830")
            self.assertEqual(len(sess.operations), 3)
            self.assertIsInstance(sess.operations[0], CapabilitiesOperation)
            self.assertIsInstance(sess.operations[1], GetSchemaOperation)
            self.assertIsInstance(sess.operations[2], GetConfigOperation)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_change_dataclass_with_type(self):
        c1 = Change(path="/interfaces/interface", operation=ChangeType.MERGE, value=100, type="uint32")
        c2 = Change(path="/interfaces/interface", operation=ChangeType.MERGE, value=100, type="uint32")
        c3 = Change(path="/interfaces/interface", operation=ChangeType.MERGE, value=100, type="int64")

        self.assertEqual(c1.type, "uint32")
        self.assertEqual(c1, c2)
        self.assertNotEqual(c1, c3)
        # Verify tuple comparison backward compatibility
        self.assertEqual(c1, ("/interfaces/interface", ChangeType.MERGE, 100))

    def test_parse_gnmi_operations(self):
        from ui.cmd import parse_operation_item

        # 1. Capability
        op_cap = parse_operation_item({"capability": {}}, protocol=Protocol.GNMI)
        self.assertIsInstance(op_cap, CapabilitiesOperation)

        # 2. Get with type and encoding
        op_get = parse_operation_item({
            "get": {
                "type": "state",
                "encoding": "proto",
                "prefix": "openconfig:",
                "path": ["system/state/hostname"]
            }
        }, protocol=Protocol.GNMI)
        self.assertIsInstance(op_get, GetOperation)
        self.assertEqual(op_get.read_scope, "state")
        self.assertEqual(op_get.protocol_options.encoding, "proto")
        self.assertEqual(op_get.selector.prefix, "openconfig:")
        self.assertEqual(op_get.selector.paths, ("system/state/hostname",))

        # 3. Set with structured mappings, string delimiter, and delete
        op_set = parse_operation_item({
            "set": {
                "prefix": "openconfig:",
                "delete": ["interfaces/interface[name=mgmt0]/config/description"],
                "update": [
                    {"path": "interfaces/interface[name=mgmt0]/config/description", "val": "desc1", "type": "string"},
                    "system/config/hostname:::spine-01",
                ],
                "replace": [
                    {"path": "interfaces/interface[name=mgmt0]/config/mtu:::1500"}
                ]
            }
        }, protocol=Protocol.GNMI)
        self.assertIsInstance(op_set, SetOperation)
        self.assertEqual(op_set.prefix, "openconfig:")
        self.assertEqual(len(op_set.changes), 4)

        # delete change
        self.assertEqual(op_set.changes[0].path, "interfaces/interface[name=mgmt0]/config/description")
        self.assertEqual(op_set.changes[0].operation, ChangeType.DELETE)

        # update 1 (structured)
        self.assertEqual(op_set.changes[1].path, "interfaces/interface[name=mgmt0]/config/description")
        self.assertEqual(op_set.changes[1].operation, ChangeType.MERGE)
        self.assertEqual(op_set.changes[1].value, "desc1")
        self.assertEqual(op_set.changes[1].type, "string")

        # update 2 (string delimiter)
        self.assertEqual(op_set.changes[2].path, "system/config/hostname")
        self.assertEqual(op_set.changes[2].operation, ChangeType.MERGE)
        self.assertEqual(op_set.changes[2].value, "spine-01")

        # replace 1 (path with delimiter and omitted val)
        self.assertEqual(op_set.changes[3].path, "interfaces/interface[name=mgmt0]/config/mtu")
        self.assertEqual(op_set.changes[3].operation, ChangeType.REPLACE)
        self.assertEqual(op_set.changes[3].value, "1500")

    def test_parse_gnmi_set_error_handling(self):
        from ui.cmd import parse_operation_item, FileConfigError

        # String without delimiter
        with self.assertRaises(FileConfigError) as ctx:
            parse_operation_item({"set": {"update": ["invalid-string-no-delimiter"]}}, protocol=Protocol.GNMI)
        self.assertIn("must follow 'path:::value' notation", str(ctx.exception))

        # Dict without val and without delimiter in path
        with self.assertRaises(FileConfigError) as ctx:
            parse_operation_item({"set": {"update": [{"path": "/some/path"}]}}, protocol=Protocol.GNMI)
        self.assertIn("Missing value for mutation path", str(ctx.exception))

    def test_file_config_builder_gnmi_operations_list_and_deprecation(self):
        import tempfile
        from ui.cmd import FileConfigBuilder, FileConfigError

        # Valid operations list for gNMI
        valid_yaml = """
protocol: "gnmi"
targets:
  172.20.20.2:57401:
    username: "admin"
    password: "pwd"
    operations:
      - capability: {}
      - get:
          path:
            - "openconfig:/system/state/hostname"
      - set:
          update:
            - path: "openconfig:/interfaces/interface[name=mgmt0]/config/description"
              val: "test"
"""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(valid_yaml)
            valid_path = f.name

        try:
            parsed = FileConfigBuilder(valid_path, protocol="gnmi").build()
            self.assertEqual(len(parsed.sessions), 1)
            sess = parsed.sessions[0]
            self.assertEqual(len(sess.operations), 3)
            self.assertIsInstance(sess.operations[0], CapabilitiesOperation)
            self.assertIsInstance(sess.operations[1], GetOperation)
            self.assertIsInstance(sess.operations[2], SetOperation)
        finally:
            if os.path.exists(valid_path):
                os.remove(valid_path)

        # Deprecated legacy field should raise FileConfigError
        deprecated_yaml = """
protocol: "gnmi"
targets:
  172.20.20.2:57401:
    username: "admin"
    password: "pwd"
    get-path:
      - "openconfig:/system/state/hostname"
"""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(deprecated_yaml)
            dep_path = f.name

        try:
            with self.assertRaises(FileConfigError) as ctx:
                FileConfigBuilder(dep_path, protocol="gnmi").build()
            self.assertIn("deprecated field 'get-path'", str(ctx.exception))
        finally:
            if os.path.exists(dep_path):
                os.remove(dep_path)


if __name__ == "__main__":
    unittest.main()


