# -*- coding: utf-8 -*-
"""
Deterministic concurrency test suite for the FastAPI presentation tier and
underlying Application Service Layer concurrency controls.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import socket
import threading
import time
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import httpx
import pytest
import uvicorn
import websockets

from api.deps import (
    APISettings,
    get_operation_service,
)
from api.main import create_app
from application.dto import (
    AdhocEndpointDTO,
    ExecutionRequestDTO,
    ExecutionResultDTO,
    StepResultDTO,
)
from application.sessions import DeviceSessionManager


ALICE_HEADERS = {"Authorization": "Bearer token-alice"}
BOB_HEADERS = {"Authorization": "Bearer token-bob"}


class AsyncConcurrencyHarness:
    """Reusable deterministic barrier/latch helper for concurrency unit tests."""

    def __init__(self, **setting_overrides):
        defaults = dict(
            auth_enabled=True,
            api_keys={
                "token-alice": "alice",
                "token-bob": "bob",
            },
            rpc_concurrency_limit=2,
            allow_adhoc_endpoints=True,
            allow_client_filesystem_paths=False,
        )
        defaults.update(setting_overrides)
        self.settings = APISettings(**defaults)
        self.app = create_app(self.settings)

    def make_client(self) -> TestClient:
        return TestClient(self.app)


class _LatchedOpService:
    """Fake DeviceOperationService that pauses callers on a Barrier/Event latch."""

    def __init__(self, latch_count: int):
        self.entered_barrier = threading.Barrier(latch_count, timeout=5.0)
        self.release_event = threading.Event()
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.total_calls = 0

    def execute_unary(self, req: ExecutionRequestDTO) -> ExecutionResultDTO:
        with self._lock:
            self.active += 1
            self.total_calls += 1
            if self.active > self.max_active:
                self.max_active = self.active
        try:
            # Wait for the initial batch to hit the limiter boundary before holding.
            try:
                self.entered_barrier.wait(timeout=0.5)
            except threading.BrokenBarrierError:
                pass
            assert self.release_event.wait(timeout=5.0), "Timed out waiting for release_event"
        finally:
            with self._lock:
                self.active -= 1
        return ExecutionResultDTO(
            target="172.20.20.2:57401",
            protocol="gnmi",
            success=True,
            results=[StepResultDTO(op_type="capability", success=True, data={"ok": True})],
        )


class _LatchedExitNetconfClient:
    """Fake NetconfClient whose RPCs or __exit__ can block deterministically."""

    def __init__(
        self,
        target="172.20.20.4:830",
        *,
        block_on_exit: bool = False,
        **kwargs,
    ):
        self.target = target
        self.entered = 0
        self.exited = 0
        self.calls = []
        self.active_calls = 0
        self.max_concurrent_calls = 0
        self._counter_lock = threading.Lock()
        self.block_on_exit = block_on_exit
        self.exit_started = threading.Event()
        self.allow_exit = threading.Event()

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.block_on_exit:
            self.exit_started.set()
            assert self.allow_exit.wait(timeout=5.0), "Timed out waiting for allow_exit"
        self.exited += 1
        return False

    def execute(self, operation):
        with self._counter_lock:
            self.active_calls += 1
            if self.active_calls > self.max_concurrent_calls:
                self.max_concurrent_calls = self.active_calls
        try:
            self.calls.append(type(operation).__name__)
            return "<ok/>"
        finally:
            with self._counter_lock:
                self.active_calls -= 1

    def execute_transaction(self, operation):
        with self._counter_lock:
            self.active_calls += 1
            if self.active_calls > self.max_concurrent_calls:
                self.max_concurrent_calls = self.active_calls
        try:
            self.calls.append(f"tx:{operation.operation.value}")
            return "<ok/>"
        finally:
            with self._counter_lock:
                self.active_calls -= 1


class TestScenario1BarrierGatekeeper:
    def test_capacity_limiter_latched_allows_non_rpc_probes(self):
        harness = AsyncConcurrencyHarness(rpc_concurrency_limit=2)
        op_svc = _LatchedOpService(latch_count=2)
        harness.app.dependency_overrides[get_operation_service] = lambda: op_svc

        payload = {
            "adhoc_endpoint": {
                "target": "172.20.20.2:57401",
                "protocol": "gnmi",
                "insecure": True,
            },
            "operations": [{"op_type": "capabilities"}],
        }

        with harness.make_client() as client, ThreadPoolExecutor(max_workers=6) as pool:
            rpc_futures = [
                pool.submit(
                    client.post,
                    "/api/v1/operations/execute",
                    headers=ALICE_HEADERS,
                    json=payload,
                )
                for _ in range(4)
            ]

            # Wait until 2 RPCs occupy the CapacityLimiter tokens.
            deadline = time.monotonic() + 3.0
            while op_svc.active < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert op_svc.active == 2

            try:
                # Non-RPC async endpoints must complete immediately while limiter is saturated.
                r_health = client.get("/healthz")
                r_ready = client.get("/readyz")
                r_devs = client.get("/api/v1/devices", headers=ALICE_HEADERS)
                assert r_health.status_code == 200
                assert r_ready.status_code == 200
                assert r_devs.status_code == 200
                assert op_svc.max_active == 2
            finally:
                op_svc.release_event.set()

            responses = [f.result(timeout=5.0) for f in rpc_futures]
            assert all(r.status_code == 200 for r in responses)
            assert op_svc.total_calls == 4
            assert op_svc.max_active <= 2


class TestScenario2LimiterStarvationAndEventLoopSafety:
    @patch("application.subscriptions.SubscribeSession")
    def test_websocket_streaming_not_starved_when_rpc_limiter_saturated(
        self, mock_session_cls
    ):
        from unittest.mock import MagicMock

        mock_worker = MagicMock()
        mock_worker.session_id = "sub-starve-1"
        mock_worker.is_running = True
        mock_worker.is_poll_mode.return_value = False
        mock_session_cls.return_value = mock_worker

        harness = AsyncConcurrencyHarness(rpc_concurrency_limit=2)
        op_svc = _LatchedOpService(latch_count=2)
        harness.app.dependency_overrides[get_operation_service] = lambda: op_svc

        rpc_payload = {
            "adhoc_endpoint": {
                "target": "172.20.20.2:57401",
                "protocol": "gnmi",
                "insecure": True,
            },
            "operations": [{"op_type": "capabilities"}],
        }

        with harness.make_client() as client, ThreadPoolExecutor(max_workers=4) as pool:
            sub_resp = client.post(
                "/api/v1/subscriptions",
                headers=ALICE_HEADERS,
                json={
                    "adhoc_endpoint": {
                        "target": "172.20.20.2:57401",
                        "protocol": "gnmi",
                        "insecure": True,
                    },
                    "operations": [
                        {
                            "op_type": "subscribe",
                            "paths": ["/interfaces"],
                            "telemetry": {"mode": "periodic", "sample_interval_sec": 5},
                        }
                    ],
                },
            )
            assert sub_resp.status_code == 201
            sub_id = sub_resp.json()["subscription_id"]
            managed = harness.app.state.subscription_service._get_managed(sub_id)

            # Saturate both rpc_limiter slots with blocked unary RPCs.
            rpc_futures = [
                pool.submit(
                    client.post,
                    "/api/v1/operations/execute",
                    headers=ALICE_HEADERS,
                    json=rpc_payload,
                )
                for _ in range(2)
            ]
            deadline = time.monotonic() + 3.0
            while op_svc.active < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert op_svc.active == 2

            try:
                def _consume_ws():
                    with client.websocket_connect(
                        f"/api/v1/subscriptions/{sub_id}/ws",
                        headers=ALICE_HEADERS,
                    ) as ws:
                        managed.data_queue.put(
                            {
                                "session_id": "sub-starve-1",
                                "target": "172.20.20.2:57401",
                                "protocol": "gnmi",
                                "subscription_name": "sub",
                                "data": {"seq": 99},
                                "is_sync_marker": False,
                            }
                        )
                        return ws.receive_json()

                ws_future = pool.submit(_consume_ws)
                # WebSocket event delivery must NOT block behind saturated unary RPCs!
                ev = ws_future.result(timeout=2.0)
                assert ev["data"] == {"seq": 99}
            finally:
                op_svc.release_event.set()
                client.delete(f"/api/v1/subscriptions/{sub_id}", headers=ALICE_HEADERS)

            assert all(f.result(timeout=5.0).status_code == 200 for f in rpc_futures)

    def test_session_list_and_get_offload_expired_cleanup_from_event_loop(self):
        harness = AsyncConcurrencyHarness(rpc_concurrency_limit=4)
        reg = harness.app.state.device_registry
        reg.register_device(
            "netopeer2",
            AdhocEndpointDTO(
                target="172.20.20.4:830",
                protocol="netconf",
                username="root",
                password="root",
            ),
        )
        blocking_client = _LatchedExitNetconfClient(block_on_exit=True)
        sess_mgr = DeviceSessionManager(
            device_registry=reg,
            default_ttl_seconds=0.05,
            client_factory=lambda **kw: blocking_client,
        )
        harness.app.state.session_manager = sess_mgr
        harness.app.state.operation_service._session_manager = sess_mgr

        with harness.make_client() as client, ThreadPoolExecutor(max_workers=4) as pool:
            create_resp = client.post(
                "/api/v1/devices/netopeer2/sessions",
                headers=ALICE_HEADERS,
                json={"ttl_seconds": 0.05},
            )
            assert create_resp.status_code == 201

            # Wait until TTL expires so GET /api/v1/sessions triggers _cleanup_record.__exit__.
            time.sleep(0.08)

            list_future = pool.submit(
                client.get,
                "/api/v1/sessions",
                headers=ALICE_HEADERS,
            )
            assert blocking_client.exit_started.wait(timeout=3.0)

            try:
                # While blocking SSH/gRPC cleanup runs, /healthz on the event loop must stay fast.
                health_future = pool.submit(client.get, "/healthz")
                r_health = health_future.result(timeout=1.5)
                assert r_health.status_code == 200
            finally:
                blocking_client.allow_exit.set()

            r_list = list_future.result(timeout=5.0)
            assert r_list.status_code == 200
            assert r_list.json() == []


class TestScenario3TwoBrowserWebSocketArena:
    @patch("application.subscriptions.SubscribeSession")
    def test_two_concurrent_websockets_receive_broadcast_and_delete_closes_cleanly(
        self, mock_session_cls
    ):
        from unittest.mock import MagicMock

        mock_worker = MagicMock()
        mock_worker.session_id = "sub-fanout-1"
        mock_worker.is_running = True
        mock_worker.is_poll_mode.return_value = True
        mock_session_cls.return_value = mock_worker

        harness = AsyncConcurrencyHarness(rpc_concurrency_limit=4)
        with harness.make_client() as client:
            create_resp = client.post(
                "/api/v1/subscriptions",
                headers=ALICE_HEADERS,
                json={
                    "adhoc_endpoint": {
                        "target": "172.20.20.2:57401",
                        "protocol": "gnmi",
                        "insecure": True,
                    },
                    "operations": [
                        {
                            "op_type": "subscribe",
                            "paths": ["/interfaces"],
                            "telemetry": {"mode": "on_demand", "sample_interval_sec": 5},
                            "options": {"subscription_name": "fanout-sub"},
                        }
                    ],
                },
            )
            assert create_resp.status_code == 201
            sub_id = create_resp.json()["subscription_id"]
            managed = harness.app.state.subscription_service._get_managed(sub_id)

            both_connected = threading.Barrier(3, timeout=5.0)
            ready_for_delete = threading.Barrier(3, timeout=5.0)

            def _ws_reader():
                events = []
                with client.websocket_connect(
                    f"/api/v1/subscriptions/{sub_id}/ws",
                    headers=ALICE_HEADERS,
                ) as ws:
                    both_connected.wait()
                    for _ in range(3):
                        events.append(ws.receive_json()["data"])
                    ready_for_delete.wait()
                    # After DELETE /subscriptions/{id}, receive should end cleanly on WS close.
                    try:
                        ws.receive_json()
                    except Exception:
                        pass
                return events

            pool = ThreadPoolExecutor(max_workers=4)
            f1 = pool.submit(_ws_reader)
            f2 = pool.submit(_ws_reader)
            try:
                both_connected.wait()

                poll_resp = client.post(
                    f"/api/v1/subscriptions/{sub_id}/poll",
                    headers=ALICE_HEADERS,
                )
                assert poll_resp.status_code == 200

                for seq in (1, 2, 3):
                    managed.data_queue.put(
                        {
                            "session_id": "sub-fanout-1",
                            "target": "172.20.20.2:57401",
                            "protocol": "gnmi",
                            "subscription_name": "fanout-sub",
                            "data": {"seq": seq},
                            "is_sync_marker": False,
                        }
                    )

                ready_for_delete.wait()
            finally:
                mock_worker.is_running = False
                del_resp = client.delete(
                    f"/api/v1/subscriptions/{sub_id}",
                    headers=ALICE_HEADERS,
                )
                pool.shutdown(wait=True)

            assert del_resp.status_code == 204
            ev1 = f1.result(timeout=5.0)
            ev2 = f2.result(timeout=5.0)

            assert ev1 == [{"seq": 1}, {"seq": 2}, {"seq": 3}]
            assert ev2 == [{"seq": 1}, {"seq": 2}, {"seq": 3}]

    @patch("application.subscriptions.SubscribeSession")
    def test_consumer_queue_drop_oldest_keeps_newest_events_under_burst(
        self, mock_session_cls
    ):
        from unittest.mock import MagicMock

        mock_worker = MagicMock()
        mock_worker.session_id = "sub-drop-1"
        mock_worker.is_running = True
        mock_worker.is_poll_mode.return_value = False
        mock_session_cls.return_value = mock_worker

        harness = AsyncConcurrencyHarness(rpc_concurrency_limit=4)
        sub_svc = harness.app.state.subscription_service
        with harness.make_client() as client:
            create_resp = client.post(
                "/api/v1/subscriptions",
                headers=ALICE_HEADERS,
                json={
                    "adhoc_endpoint": {
                        "target": "172.20.20.2:57401",
                        "protocol": "gnmi",
                        "insecure": True,
                    },
                    "queue_size": 2,
                    "operations": [
                        {
                            "op_type": "subscribe",
                            "paths": ["/interfaces"],
                            "telemetry": {"mode": "periodic", "sample_interval_sec": 5},
                        }
                    ],
                },
            )
            assert create_resp.status_code == 201
            sub_id = create_resp.json()["subscription_id"]
            managed = sub_svc._get_managed(sub_id)

            try:
                # 1. Pre-attach burst of 5 events into queue_size=2 retains newest 2.
                for seq in range(1, 6):
                    managed.data_queue.put_nowait(
                        {
                            "session_id": "sub-drop-1",
                            "target": "172.20.20.2:57401",
                            "protocol": "gnmi",
                            "subscription_name": "sub",
                            "data": {"seq": seq},
                            "is_sync_marker": False,
                        }
                    )

                with client.websocket_connect(
                    f"/api/v1/subscriptions/{sub_id}/ws",
                    headers=ALICE_HEADERS,
                ) as ws:
                    assert [ws.receive_json()["data"], ws.receive_json()["data"]] == [
                        {"seq": 4},
                        {"seq": 5},
                    ]

                # 2. Slow consumer paused alongside active fast WS consumer keeps newest 2
                # without blocking fast consumer delivery.
                slow_cid = sub_svc.open_consumer(sub_id, owner_id="alice")
                try:
                    fast_received = []
                    with client.websocket_connect(
                        f"/api/v1/subscriptions/{sub_id}/ws",
                        headers=ALICE_HEADERS,
                    ) as fast_ws:
                        for seq in range(10, 15):
                            managed.data_queue.put_nowait(
                                {
                                    "session_id": "sub-drop-1",
                                    "target": "172.20.20.2:57401",
                                    "protocol": "gnmi",
                                    "subscription_name": "sub",
                                    "data": {"seq": seq},
                                    "is_sync_marker": False,
                                }
                            )
                            fast_received.append(fast_ws.receive_json()["data"])

                    slow_events = sub_svc.get_consumer_events(
                        sub_id, slow_cid, timeout=0.05, max_events=10, owner_id="alice"
                    )
                    assert fast_received == [{"seq": s} for s in range(10, 15)]
                    assert [ev.data for ev in slow_events] == [{"seq": 13}, {"seq": 14}]
                finally:
                    sub_svc.close_consumer(slow_cid)
            finally:
                client.delete(f"/api/v1/subscriptions/{sub_id}", headers=ALICE_HEADERS)

    @patch("application.subscriptions.SubscribeSession")
    def test_early_consumer_detach_leaves_remaining_consumer_and_stream_alive(
        self, mock_session_cls
    ):
        from unittest.mock import MagicMock

        mock_worker = MagicMock()
        mock_worker.session_id = "sub-detach-1"
        mock_worker.is_running = True
        mock_worker.is_poll_mode.return_value = False
        mock_session_cls.return_value = mock_worker

        harness = AsyncConcurrencyHarness(rpc_concurrency_limit=4)
        with harness.make_client() as client:
            create_resp = client.post(
                "/api/v1/subscriptions",
                headers=ALICE_HEADERS,
                json={
                    "adhoc_endpoint": {
                        "target": "172.20.20.2:57401",
                        "protocol": "gnmi",
                        "insecure": True,
                    },
                    "operations": [
                        {
                            "op_type": "subscribe",
                            "paths": ["/interfaces"],
                            "telemetry": {"mode": "periodic", "sample_interval_sec": 5},
                        }
                    ],
                },
            )
            assert create_resp.status_code == 201
            sub_id = create_resp.json()["subscription_id"]
            managed = harness.app.state.subscription_service._get_managed(sub_id)

            ws2_ready = threading.Event()
            ws1_done = threading.Event()

            def _ws2_loop():
                received = []
                with client.websocket_connect(
                    f"/api/v1/subscriptions/{sub_id}/ws",
                    headers=ALICE_HEADERS,
                ) as ws2:
                    ws2_ready.set()
                    received.append(ws2.receive_json()["data"])
                    assert ws1_done.wait(timeout=5.0)
                    received.append(ws2.receive_json()["data"])
                return received

            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    f2 = pool.submit(_ws2_loop)
                    assert ws2_ready.wait(timeout=3.0)

                    with client.websocket_connect(
                        f"/api/v1/subscriptions/{sub_id}/ws",
                        headers=ALICE_HEADERS,
                    ) as ws1:
                        time.sleep(0.05)
                        managed.data_queue.put(
                            {
                                "session_id": "sub-detach-1",
                                "target": "172.20.20.2:57401",
                                "protocol": "gnmi",
                                "subscription_name": "sub",
                                "data": {"seq": 1},
                                "is_sync_marker": False,
                            }
                        )
                        assert ws1.receive_json()["data"] == {"seq": 1}

                    # ws1 detached; stream and ws2 must remain alive and receive seq=2.
                    ws1_done.set()
                    time.sleep(0.05)
                    assert (
                        client.get(
                            f"/api/v1/subscriptions/{sub_id}", headers=ALICE_HEADERS
                        ).json()["is_alive"]
                        is True
                    )
                    managed.data_queue.put(
                        {
                            "session_id": "sub-detach-1",
                            "target": "172.20.20.2:57401",
                            "protocol": "gnmi",
                            "subscription_name": "sub",
                            "data": {"seq": 2},
                            "is_sync_marker": False,
                        }
                    )
                    assert f2.result(timeout=5.0) == [{"seq": 1}, {"seq": 2}]
            finally:
                client.delete(f"/api/v1/subscriptions/{sub_id}", headers=ALICE_HEADERS)


class _LatchedOpNetconfClient(_LatchedExitNetconfClient):
    """Fake NetconfClient that optionally latches inside its first RPC call."""

    def __init__(self, target="172.20.20.4:830", *, latch_first_exec: bool = False, **kwargs):
        super().__init__(target=target, **kwargs)
        self.latch_first_exec = latch_first_exec
        self.exec_started = threading.Event()
        self.allow_exec = threading.Event()
        self._latched_once = False

    def _maybe_latch(self):
        if self.latch_first_exec and not self._latched_once:
            self._latched_once = True
            self.exec_started.set()
            assert self.allow_exec.wait(timeout=5.0), "Timed out waiting for allow_exec"

    def execute(self, operation):
        with self._counter_lock:
            self.active_calls += 1
            if self.active_calls > self.max_concurrent_calls:
                self.max_concurrent_calls = self.active_calls
        try:
            self._maybe_latch()
            self.calls.append(type(operation).__name__)
            return "<ok/>"
        finally:
            with self._counter_lock:
                self.active_calls -= 1

    def execute_transaction(self, operation):
        with self._counter_lock:
            self.active_calls += 1
            if self.active_calls > self.max_concurrent_calls:
                self.max_concurrent_calls = self.active_calls
        try:
            self._maybe_latch()
            self.calls.append(f"tx:{operation.operation.value}")
            return "<ok/>"
        finally:
            with self._counter_lock:
                self.active_calls -= 1


class TestScenario4SessionCollisionMatrix:
    def _setup_session_app(self, *, default_ttl_seconds=60.0, factory=None):
        harness = AsyncConcurrencyHarness(
            rpc_concurrency_limit=6, session_ttl_seconds=default_ttl_seconds
        )
        reg = harness.app.state.device_registry
        reg.register_device(
            "netopeer2",
            AdhocEndpointDTO(
                target="172.20.20.4:830",
                protocol="netconf",
                username="root",
                password="root",
            ),
        )
        created = []

        def _factory(**kw):
            c = factory(**kw) if factory else _LatchedOpNetconfClient(target=kw["target"])
            created.append(c)
            return c

        sess_mgr = DeviceSessionManager(
            device_registry=reg,
            default_ttl_seconds=default_ttl_seconds,
            client_factory=_factory,
        )
        harness.app.state.session_manager = sess_mgr
        harness.app.state.operation_service._session_manager = sess_mgr
        return harness, sess_mgr, created

    def test_concurrent_same_session_ops_serialize_and_delete_returns_409_conflict(self):
        harness, _, created = self._setup_session_app(
            factory=lambda **kw: _LatchedOpNetconfClient(
                target=kw["target"], latch_first_exec=True
            )
        )

        with harness.make_client() as client, ThreadPoolExecutor(max_workers=6) as pool:
            create_resp = client.post(
                "/api/v1/devices/netopeer2/sessions",
                headers=ALICE_HEADERS,
                json={"ttl_seconds": 60.0},
            )
            assert create_resp.status_code == 201
            sid = create_resp.json()["session_id"]
            fake_client = created[0]

            # Start first session operation and pause inside fake_client.execute_transaction.
            f_lock = pool.submit(
                client.post,
                f"/api/v1/sessions/{sid}/operations",
                headers=ALICE_HEADERS,
                json={"operations": [{"op_type": "lock", "datastore": "candidate"}]},
            )
            assert fake_client.exec_started.wait(timeout=3.0)

            try:
                # Queue concurrent operations on the same session while locked in lease.
                f_val = pool.submit(
                    client.post,
                    f"/api/v1/sessions/{sid}/operations",
                    headers=ALICE_HEADERS,
                    json={"operations": [{"op_type": "validate", "datastore": "candidate"}]},
                )
                f_unlock = pool.submit(
                    client.post,
                    f"/api/v1/sessions/{sid}/operations",
                    headers=ALICE_HEADERS,
                    json={"operations": [{"op_type": "unlock", "datastore": "candidate"}]},
                )

                # DELETE while an operation lease is active must fail with 409 SESSION_CONFLICT.
                r_del_busy = client.delete(
                    f"/api/v1/sessions/{sid}", headers=ALICE_HEADERS
                )
                assert r_del_busy.status_code == 409
                assert r_del_busy.json()["error_code"] == "SESSION_CONFLICT"
                assert fake_client.exited == 0
            finally:
                fake_client.allow_exec.set()

            for fut in (f_lock, f_val, f_unlock):
                r = fut.result(timeout=5.0)
                assert r.status_code == 200
                assert r.json()["success"] is True

            assert fake_client.max_concurrent_calls == 1
            assert fake_client.entered == 1
            assert fake_client.exited == 0

            r_del_ok = client.delete(f"/api/v1/sessions/{sid}", headers=ALICE_HEADERS)
            assert r_del_ok.status_code == 204
            assert fake_client.exited == 1

    def test_ttl_sweep_preserves_leased_session_and_evicts_idle_sibling(self):
        clients_by_order = []

        def _factory(**kw):
            idx = len(clients_by_order)
            # First session created will be latched during its op; second will stay idle.
            c = _LatchedOpNetconfClient(
                target=kw["target"], latch_first_exec=(idx == 0)
            )
            clients_by_order.append(c)
            return c

        harness, _, _ = self._setup_session_app(
            default_ttl_seconds=0.10, factory=_factory
        )

        with harness.make_client() as client, ThreadPoolExecutor(max_workers=4) as pool:
            sid_busy = client.post(
                "/api/v1/devices/netopeer2/sessions",
                headers=ALICE_HEADERS,
                json={"ttl_seconds": 0.10},
            ).json()["session_id"]
            sid_idle = client.post(
                "/api/v1/devices/netopeer2/sessions",
                headers=ALICE_HEADERS,
                json={"ttl_seconds": 0.10},
            ).json()["session_id"]
            busy_client, idle_client = clients_by_order[0], clients_by_order[1]

            # Lock candidate on idle session, and start latched op on busy session.
            assert (
                client.post(
                    f"/api/v1/sessions/{sid_idle}/operations",
                    headers=ALICE_HEADERS,
                    json={"operations": [{"op_type": "lock", "datastore": "candidate"}]},
                ).status_code
                == 200
            )
            f_busy = pool.submit(
                client.post,
                f"/api/v1/sessions/{sid_busy}/operations",
                headers=ALICE_HEADERS,
                json={"operations": [{"op_type": "lock", "datastore": "candidate"}]},
            )
            assert busy_client.exec_started.wait(timeout=3.0)

            try:
                # Let TTL pass while sid_busy is actively leased and sid_idle is idle.
                time.sleep(0.14)
                r_list = client.get("/api/v1/sessions", headers=ALICE_HEADERS)
                assert r_list.status_code == 200
                remaining_ids = [s["session_id"] for s in r_list.json()]
                assert remaining_ids == [sid_busy]
                assert "tx:unlock" in idle_client.calls
                assert idle_client.exited == 1
                assert busy_client.exited == 0
            finally:
                busy_client.allow_exec.set()

            assert f_busy.result(timeout=5.0).status_code == 200
            # sid_busy must still accept operations cleanly after lease release.
            r_unlock = client.post(
                f"/api/v1/sessions/{sid_busy}/operations",
                headers=ALICE_HEADERS,
                json={"operations": [{"op_type": "unlock", "datastore": "candidate"}]},
            )
            assert r_unlock.status_code == 200
            assert (
                client.delete(
                    f"/api/v1/sessions/{sid_busy}", headers=ALICE_HEADERS
                ).status_code
                == 204
            )

    def test_shutdown_close_all_waits_for_active_lease_before_unlock_and_exit(self):
        harness, sess_mgr, created = self._setup_session_app(
            factory=lambda **kw: _LatchedOpNetconfClient(
                target=kw["target"], latch_first_exec=True
            )
        )

        with harness.make_client() as client, ThreadPoolExecutor(max_workers=3) as pool:
            sid = client.post(
                "/api/v1/devices/netopeer2/sessions",
                headers=ALICE_HEADERS,
                json={"ttl_seconds": 60.0},
            ).json()["session_id"]
            fake_client = created[0]

            f_op = pool.submit(
                client.post,
                f"/api/v1/sessions/{sid}/operations",
                headers=ALICE_HEADERS,
                json={"operations": [{"op_type": "lock", "datastore": "candidate"}]},
            )
            assert fake_client.exec_started.wait(timeout=3.0)

            f_close_all = pool.submit(sess_mgr.close_all)
            time.sleep(0.05)
            # close_all must wait for the in-flight operation rather than tearing down underneath it.
            assert fake_client.exited == 0
            assert not f_close_all.done()

            fake_client.allow_exec.set()
            assert f_op.result(timeout=5.0).status_code == 200
            f_close_all.result(timeout=5.0)
            assert fake_client.calls == ["tx:lock", "tx:unlock"]
            assert fake_client.exited == 1
            assert fake_client.max_concurrent_calls == 1


class TestScenario7RealUvicornSocketBoundary:
    @patch("application.subscriptions.SubscribeSession")
    def test_live_uvicorn_tcp_concurrency_abrupt_ws_drop_and_clean_shutdown(
        self, mock_session_cls
    ):
        mock_worker = MagicMock()
        mock_worker.session_id = "sub-uvicorn-1"
        mock_worker.is_running = True
        mock_worker.is_poll_mode.return_value = False
        mock_session_cls.return_value = mock_worker

        harness = AsyncConcurrencyHarness(rpc_concurrency_limit=4)

        class _FastFakeOpService:
            def execute_unary(self, req: ExecutionRequestDTO) -> ExecutionResultDTO:
                time.sleep(0.02)
                return ExecutionResultDTO(
                    target="172.20.20.2:57401",
                    protocol="gnmi",
                    success=True,
                    results=[StepResultDTO(op_type="capability", success=True, data={"ok": True})],
                )

        harness.app.dependency_overrides[get_operation_service] = lambda: _FastFakeOpService()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

        config = uvicorn.Config(
            harness.app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None
        srv_thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": [sock]},
            daemon=True,
        )
        srv_thread.start()

        deadline = time.monotonic() + 5.0
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "Uvicorn server failed to start in time"

        base_http = f"http://127.0.0.1:{port}"
        base_ws = f"ws://127.0.0.1:{port}"

        async def _exercise_tcp_sockets():
            async with httpx.AsyncClient(base_url=base_http, timeout=10.0) as ac:
                op_payload = {
                    "adhoc_endpoint": {
                        "target": "172.20.20.2:57401",
                        "protocol": "gnmi",
                        "insecure": True,
                    },
                    "operations": [{"op_type": "capabilities"}],
                }
                tasks = [
                    ac.post("/api/v1/operations/execute", headers=ALICE_HEADERS, json=op_payload)
                    for _ in range(10)
                ]
                tasks.extend(
                    [
                        ac.get("/healthz"),
                        ac.get("/readyz"),
                        ac.get("/api/v1/devices", headers=ALICE_HEADERS),
                    ]
                )
                results = await asyncio.gather(*tasks)
                assert all(r.status_code == 200 for r in results)

                sub_resp = await ac.post(
                    "/api/v1/subscriptions",
                    headers=ALICE_HEADERS,
                    json={
                        "adhoc_endpoint": {
                            "target": "172.20.20.2:57401",
                            "protocol": "gnmi",
                            "insecure": True,
                        },
                        "operations": [
                            {
                                "op_type": "subscribe",
                                "paths": ["/interfaces"],
                                "telemetry": {"mode": "periodic", "sample_interval_sec": 5},
                            }
                        ],
                    },
                )
                assert sub_resp.status_code == 201
                sub_id = sub_resp.json()["subscription_id"]
                managed = harness.app.state.subscription_service._get_managed(sub_id)

                ws = await websockets.connect(
                    f"{base_ws}/api/v1/subscriptions/{sub_id}/ws?token=token-alice"
                )
                try:
                    managed.data_queue.put(
                        {
                            "session_id": "sub-uvicorn-1",
                            "target": "172.20.20.2:57401",
                            "protocol": "gnmi",
                            "subscription_name": "sub",
                            "data": {"seq": 77},
                            "is_sync_marker": False,
                        }
                    )
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
                    assert msg["data"] == {"seq": 77}
                finally:
                    # Simulate abrupt TCP socket drop without clean WS close handshake.
                    ws.transport.abort()

                await asyncio.sleep(0.05)
                info_resp = await ac.get(
                    f"/api/v1/subscriptions/{sub_id}",
                    headers=ALICE_HEADERS,
                )
                assert info_resp.status_code == 200
                assert info_resp.json()["is_alive"] is True

        try:
            asyncio.run(_exercise_tcp_sockets())
        finally:
            server.should_exit = True
            srv_thread.join(timeout=5.0)
            sock.close()

        assert not srv_thread.is_alive(), "Uvicorn server hung during lifespan shutdown"
        mock_worker.stop.assert_called_once()

    def test_execute_session_operations_does_not_block_event_loop_during_expired_cleanup(
        self,
    ):
        class _SlowCleanupClient:
            def __init__(self, target: str):
                self.target = target

            def __enter__(self):
                return self

            def __exit__(self, *args):
                time.sleep(0.30)

            def execute_transaction(self, op):
                time.sleep(0.10)
                return {"ok": True}

            def execute(self, op):
                return {"ok": True}

        harness = AsyncConcurrencyHarness()
        reg = harness.app.state.device_registry
        reg.register_device(
            "netopeer2",
            AdhocEndpointDTO(
                target="10.0.0.1:830",
                protocol="netconf",
                username="root",
                password="pw",
            ),
        )
        sess_mgr = DeviceSessionManager(
            device_registry=reg,
            default_ttl_seconds=30.0,
            client_factory=lambda **kw: _SlowCleanupClient(target=kw["target"]),
        )
        harness.app.state.session_manager = sess_mgr
        harness.app.state.operation_service._session_manager = sess_mgr

        s_exp = sess_mgr.create_session(
            owner_id="alice", device_id="netopeer2", ttl_seconds=0.05
        )
        s_act = sess_mgr.create_session(
            owner_id="alice", device_id="netopeer2", ttl_seconds=30.0
        )
        with sess_mgr.acquire_session(s_exp.session_id, owner_id="alice") as lease:
            lease.locked_datastores.add("candidate")
        time.sleep(0.08)

        async def _verify_non_blocking():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=harness.app),
                base_url="http://test",
            ) as ac:
                async def _run_ops():
                    return await ac.post(
                        f"/api/v1/sessions/{s_act.session_id}/operations",
                        headers=ALICE_HEADERS,
                        json={"operations": [{"op_type": "capabilities"}]},
                    )

                t_start = time.perf_counter()

                async def _run_health():
                    await asyncio.sleep(0.02)
                    resp = await ac.get("/healthz")
                    return resp.status_code, time.perf_counter() - t_start

                ops_resp, (health_status, health_elapsed) = await asyncio.gather(
                    _run_ops(), _run_health()
                )
                assert ops_resp.status_code == 200
                assert health_status == 200
                assert health_elapsed < 0.20, (
                    f"Event loop blocked by expired session cleanup: /healthz finished at {health_elapsed:.3f}s"
                )

        asyncio.run(_verify_non_blocking())
