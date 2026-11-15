# -*- coding: utf-8 -*-
import threading
import time
from unittest.mock import MagicMock, patch
import pytest

from application.devices import DeviceRegistryService
from application.dto import (
    AdhocEndpointDTO,
    ExecutionRequestDTO,
    MutationChangeDTO,
    OperationStepDTO,
    SessionInfoDTO,
)
from application.exceptions import (
    InvalidRequestError,
    PermissionDeniedError,
    SessionConflictError,
    SessionNotFoundError,
)
from application.operations import DeviceOperationService
from application.sessions import DeviceSessionManager, LogicalSessionState
from application.subscriptions import SubscriptionService
from config.operations import TransactionType


class _FakeNetconfClient:
    """In-memory fake NetconfClient tracking lifecycle and serialized usage."""

    def __init__(self, target="172.20.20.4:830", **kwargs):
        self.target = target
        self.entered = 0
        self.exited = 0
        self.calls = []
        self.tx_ops = []
        self.active_calls = 0
        self.max_concurrent_calls = 0
        self._counter_lock = threading.Lock()
        self.delay_sec = 0.0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.exited += 1
        return False

    def _track_call(self, name):
        with self._counter_lock:
            self.active_calls += 1
            if self.active_calls > self.max_concurrent_calls:
                self.max_concurrent_calls = self.active_calls
        try:
            if self.delay_sec > 0:
                time.sleep(self.delay_sec)
            self.calls.append(name)
        finally:
            with self._counter_lock:
                self.active_calls -= 1

    def execute_capabilities(self, operation):
        self._track_call("capabilities")
        return ["urn:ietf:params:netconf:base:1.1"]

    def execute_get(self, operation):
        self._track_call("get")
        return "<data><system/></data>"

    def execute_set(self, operation):
        self._track_call("set")
        return "<ok/>"

    def execute_transaction(self, operation):
        self._track_call(f"tx:{operation.operation.value}")
        self.tx_ops.append(operation)
        return "<ok/>"

    def execute(self, operation):
        from config.operations import (
            CapabilitiesOperation,
            GetOperation,
            SetOperation,
            TransactionOperation,
        )

        if isinstance(operation, CapabilitiesOperation):
            return self.execute_capabilities(operation)
        if isinstance(operation, GetOperation):
            return self.execute_get(operation)
        if isinstance(operation, SetOperation):
            return self.execute_set(operation)
        if isinstance(operation, TransactionOperation):
            return self.execute_transaction(operation)
        raise AssertionError(f"Unexpected operation: {operation}")


class _FakeGnmiClient:
    """In-memory fake GNMIClient verifying gNMI session reuse without NETCONF state."""

    def __init__(self, target="172.20.20.2:57401", **kwargs):
        self.target = target
        self.entered = 0
        self.exited = 0
        self.calls = []

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.exited += 1
        return False

    def execute_capabilities(self, operation):
        self.calls.append("capabilities")
        return {"gnmi_version": "0.8.0"}

    def execute_get(self, operation):
        self.calls.append("get")
        return {"system": {"hostname": "srl-01"}}

    def execute_set(self, operation):
        self.calls.append("set")
        return {"response": "ok"}


def _build_registry():
    reg = DeviceRegistryService()
    reg.register_device(
        "netopeer2",
        AdhocEndpointDTO(
            target="172.20.20.4:830",
            protocol="netconf",
            username="root",
            password="root",
        ),
    )
    reg.register_device(
        "srlinux_gnmi",
        AdhocEndpointDTO(
            target="172.20.20.2:57401",
            protocol="gnmi",
            username="admin",
            password="NokiaSrl1!",
            insecure=True,
        ),
    )
    return reg


class TestManagedLogicalSessions:
    def test_single_request_pipeline_uses_one_netconf_session(self):
        fake_client = _FakeNetconfClient()
        reg = _build_registry()
        op_svc = DeviceOperationService(device_registry=reg)

        req = ExecutionRequestDTO(
            device_id="netopeer2",
            operations=[
                OperationStepDTO(op_type="lock", datastore="candidate", op_id="step-lock"),
                OperationStepDTO(
                    op_type="edit_config",
                    datastore="candidate",
                    changes=[MutationChangeDTO(path="/system/hostname", value="r1", operation="merge")],
                    op_id="step-edit",
                ),
                OperationStepDTO(op_type="validate", datastore="candidate", op_id="step-val"),
                OperationStepDTO(op_type="commit", op_id="step-commit"),
                OperationStepDTO(op_type="unlock", datastore="candidate", op_id="step-unlock"),
            ],
        )

        with patch("managers.unary_worker.ClientFactory.get_client", return_value=fake_client) as mock_factory:
            res = op_svc.execute_unary(req)

        assert res.success is True
        assert [r.op_id for r in res.results] == [
            "step-lock",
            "step-edit",
            "step-val",
            "step-commit",
            "step-unlock",
        ]
        mock_factory.assert_called_once()
        assert fake_client.entered == 1
        assert fake_client.exited == 1
        assert fake_client.calls == ["tx:lock", "set", "tx:validate", "tx:commit", "tx:unlock"]

    def test_cross_request_netconf_session_reuses_same_client(self):
        created_clients = []

        def factory(**kwargs):
            c = _FakeNetconfClient(target=kwargs["target"])
            created_clients.append(c)
            return c

        reg = _build_registry()
        sess_mgr = DeviceSessionManager(device_registry=reg, client_factory=factory)
        op_svc = DeviceOperationService(device_registry=reg, session_manager=sess_mgr)

        info = sess_mgr.create_session(owner_id="alice", device_id="netopeer2")
        assert isinstance(info, SessionInfoDTO)
        assert info.state == LogicalSessionState.ACTIVE.value
        assert len(created_clients) == 1
        client = created_clients[0]
        assert client.entered == 1
        assert client.exited == 0

        # Request 1: lock candidate
        r1 = op_svc.execute_in_session(
            info.session_id,
            ExecutionRequestDTO(
                device_id="netopeer2",
                operations=[OperationStepDTO(op_type="lock", datastore="candidate")],
            ),
            owner_id="alice",
        )
        assert r1.success is True
        assert client.exited == 0

        # Request 2: edit candidate (uncommitted)
        r2 = op_svc.execute_in_session(
            info.session_id,
            ExecutionRequestDTO(
                device_id="netopeer2",
                operations=[
                    OperationStepDTO(
                        op_type="edit_config",
                        datastore="candidate",
                        xml_payload="<config><system/></config>",
                        options={"commit": False},
                    )
                ],
            ),
            owner_id="alice",
        )
        assert r2.success is True
        mid_info = sess_mgr.get_session_info(info.session_id, owner_id="alice")
        assert mid_info.locked_datastores == ["candidate"]
        assert mid_info.has_uncommitted_changes is True
        assert client.exited == 0

        # Request 3: commit and unlock
        r3 = op_svc.execute_in_session(
            info.session_id,
            ExecutionRequestDTO(
                device_id="netopeer2",
                operations=[
                    OperationStepDTO(op_type="commit"),
                    OperationStepDTO(op_type="unlock", datastore="candidate"),
                ],
            ),
            owner_id="alice",
        )
        assert r3.success is True
        assert len(created_clients) == 1
        assert client.entered == 1
        assert client.exited == 0
        assert client.calls == ["tx:lock", "set", "tx:commit", "tx:unlock"]

        end_info = sess_mgr.get_session_info(info.session_id, owner_id="alice")
        assert end_info.locked_datastores == []
        assert end_info.has_uncommitted_changes is False

        assert sess_mgr.close_session(info.session_id, owner_id="alice") is True
        assert client.exited == 1

    def test_session_ownership_isolation(self):
        reg = _build_registry()
        sess_mgr = DeviceSessionManager(
            device_registry=reg,
            client_factory=lambda **kw: _FakeNetconfClient(target=kw["target"]),
        )
        op_svc = DeviceOperationService(device_registry=reg, session_manager=sess_mgr)

        info = sess_mgr.create_session(owner_id="owner-a", device_id="netopeer2")

        with pytest.raises(PermissionDeniedError):
            sess_mgr.get_session_info(info.session_id, owner_id="owner-b")

        with pytest.raises(PermissionDeniedError):
            with sess_mgr.acquire_session(info.session_id, owner_id="owner-b"):
                pass

        with pytest.raises(PermissionDeniedError):
            op_svc.execute_in_session(
                info.session_id,
                ExecutionRequestDTO(
                    device_id="netopeer2",
                    operations=[OperationStepDTO(op_type="get", paths=["/system"])],
                ),
                owner_id="owner-b",
            )

        with pytest.raises(PermissionDeniedError):
            sess_mgr.close_session(info.session_id, owner_id="owner-b")

        # Owner A still owns and can close cleanly
        assert sess_mgr.close_session(info.session_id, owner_id="owner-a") is True

    def test_concurrent_operations_on_same_netconf_session_are_serialized(self):
        fake_client = _FakeNetconfClient()
        fake_client.delay_sec = 0.08
        reg = _build_registry()
        sess_mgr = DeviceSessionManager(
            device_registry=reg,
            client_factory=lambda **kw: fake_client,
        )
        op_svc = DeviceOperationService(device_registry=reg, session_manager=sess_mgr)
        info = sess_mgr.create_session(owner_id="alice", device_id="netopeer2")

        results = []
        errors = []

        def run_get():
            try:
                res = op_svc.execute_in_session(
                    info.session_id,
                    ExecutionRequestDTO(
                        device_id="netopeer2",
                        operations=[OperationStepDTO(op_type="get", paths=["/system"])],
                    ),
                    owner_id="alice",
                )
                results.append(res)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=run_get)
        t2 = threading.Thread(target=run_get)
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        assert not errors
        assert len(results) == 2
        assert all(r.success for r in results)
        assert fake_client.max_concurrent_calls == 1
        sess_mgr.close_all()

    def test_active_lease_prevents_sweep_and_conflict_close(self):
        fake_client = _FakeNetconfClient()
        reg = _build_registry()
        sess_mgr = DeviceSessionManager(
            device_registry=reg,
            default_ttl_seconds=0.05,
            client_factory=lambda **kw: fake_client,
        )
        info = sess_mgr.create_session(owner_id="alice", device_id="netopeer2")

        with sess_mgr.acquire_session(info.session_id, owner_id="alice") as lease:
            assert lease.client is fake_client
            time.sleep(0.08)  # Past initial expires_at while leased
            closed = sess_mgr.sweep_expired_sessions()
            assert closed == []
            assert fake_client.exited == 0

            with pytest.raises(SessionConflictError):
                sess_mgr.close_session(info.session_id, owner_id="alice")

            lease_info = sess_mgr.get_session_info(info.session_id, owner_id="alice")
            assert lease_info.in_use is True
            assert lease_info.state == LogicalSessionState.IN_USE.value

        # Lease released: now closes cleanly
        assert sess_mgr.close_session(info.session_id, owner_id="alice") is True
        assert fake_client.exited == 1

    def test_idle_expiration_unlocks_owned_lock_and_never_discards_changes(self):
        fake_client = _FakeNetconfClient()
        reg = _build_registry()
        sess_mgr = DeviceSessionManager(
            device_registry=reg,
            default_ttl_seconds=0.05,
            client_factory=lambda **kw: fake_client,
        )
        op_svc = DeviceOperationService(device_registry=reg, session_manager=sess_mgr)
        info = sess_mgr.create_session(owner_id="alice", device_id="netopeer2")

        # Lock candidate and make uncommitted edit
        op_svc.execute_in_session(
            info.session_id,
            ExecutionRequestDTO(
                device_id="netopeer2",
                operations=[
                    OperationStepDTO(op_type="lock", datastore="candidate"),
                    OperationStepDTO(
                        op_type="edit_config",
                        datastore="candidate",
                        xml_payload="<config><system/></config>",
                        options={"commit": False},
                    ),
                ],
            ),
            owner_id="alice",
        )

        # Force idle sweep beyond expiration
        closed_ids = sess_mgr.sweep_expired_sessions(now=time.time() + 10.0)
        assert closed_ids == [info.session_id]
        assert fake_client.exited == 1

        tx_types = [op.operation for op in fake_client.tx_ops]
        assert TransactionType.UNLOCK in tx_types
        assert TransactionType.DISCARD_CHANGES not in tx_types

        with pytest.raises(SessionNotFoundError):
            sess_mgr.get_session_info(info.session_id, owner_id="alice")

    def test_gnmi_logical_session_does_not_apply_netconf_transaction_state(self):
        fake_gnmi = _FakeGnmiClient()
        reg = _build_registry()
        sess_mgr = DeviceSessionManager(
            device_registry=reg,
            client_factory=lambda **kw: fake_gnmi,
        )
        op_svc = DeviceOperationService(device_registry=reg, session_manager=sess_mgr)

        info = sess_mgr.create_session(owner_id="alice", device_id="srlinux_gnmi")
        assert info.protocol == "gnmi"

        r1 = op_svc.execute_in_session(
            info.session_id,
            ExecutionRequestDTO(
                device_id="srlinux_gnmi",
                operations=[OperationStepDTO(op_type="capabilities")],
            ),
            owner_id="alice",
        )
        r2 = op_svc.execute_in_session(
            info.session_id,
            ExecutionRequestDTO(
                device_id="srlinux_gnmi",
                operations=[
                    OperationStepDTO(
                        op_type="set",
                        changes=[MutationChangeDTO(path="/system/config/hostname", value="srl")],
                    )
                ],
            ),
            owner_id="alice",
        )

        assert r1.success is True
        assert r2.success is True
        assert fake_gnmi.entered == 1
        assert fake_gnmi.exited == 0
        assert fake_gnmi.calls == ["capabilities", "set"]

        updated = sess_mgr.get_session_info(info.session_id, owner_id="alice")
        assert updated.locked_datastores == []
        assert updated.has_uncommitted_changes is False

        # NETCONF-only transaction operation is rejected for gNMI session
        with pytest.raises(InvalidRequestError):
            op_svc.execute_in_session(
                info.session_id,
                ExecutionRequestDTO(
                    device_id="srlinux_gnmi",
                    operations=[OperationStepDTO(op_type="lock", datastore="candidate")],
                ),
                owner_id="alice",
            )

        sess_mgr.close_all()
        assert fake_gnmi.exited == 1


class TestSubscriptionOwnershipIsolation:
    @patch("application.subscriptions.SubscribeSession")
    def test_subscription_owner_isolation_and_cli_compat(self, mock_session_cls):
        mock_worker = MagicMock()
        mock_worker.is_running = True
        mock_session_cls.return_value = mock_worker

        sub_svc = SubscriptionService()
        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="172.20.20.2:57401", protocol="gnmi", insecure=True),
            operations=[OperationStepDTO(op_type="subscribe", paths=["/interfaces"])],
        )

        sub_a = sub_svc.start_subscription(req, owner_id="alice")
        sub_b = sub_svc.start_subscription(req, owner_id="bob")
        sub_cli = sub_svc.start_subscription(req)  # owner_id=None (CLI mode)

        # Filtered listing per owner
        alice_subs = sub_svc.list_active_subscriptions(owner_id="alice")
        assert [s["subscription_id"] for s in alice_subs] == [sub_a]
        assert len(sub_svc.list_active_subscriptions()) == 3

        # Bob cannot inspect, poll, read events, or stop Alice's subscription
        with pytest.raises(PermissionDeniedError):
            sub_svc.get_subscription_info(sub_a, owner_id="bob")
        with pytest.raises(PermissionDeniedError):
            sub_svc.get_events(sub_a, timeout=0.05, owner_id="bob")
        with pytest.raises(PermissionDeniedError):
            sub_svc.send_command(sub_a, "poll", owner_id="bob")
        with pytest.raises(PermissionDeniedError):
            sub_svc.stop_subscription(sub_a, owner_id="bob")

        # Alice and CLI (owner_id=None) continue to work
        info_a = sub_svc.get_subscription_info(sub_a, owner_id="alice")
        assert info_a["owner_id"] == "alice"
        assert sub_svc.get_subscription_info(sub_cli)["owner_id"] is None
        assert sub_svc.stop_subscription(sub_a, owner_id="alice") is True
        assert sub_svc.stop_subscription(sub_b, owner_id="bob") is True
        assert sub_svc.stop_subscription(sub_cli) is True
