# -*- coding: utf-8 -*-
import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import anyio
from fastapi.testclient import TestClient
import httpx
import pytest

from api.deps import (
    APISettings,
    get_operation_service,
    get_schema_service,
)
from api.main import create_app
from application.dto import (
    AdhocEndpointDTO,
    DeliveryModeDTO,
    ExecutionRequestDTO,
    ExecutionResultDTO,
    MutationChangeDTO,
    OperationStepDTO,
    StepResultDTO,
    TelemetryPolicyDTO,
)
from application.sessions import DeviceSessionManager


def _make_test_app(**setting_overrides):
    defaults = dict(
        auth_enabled=True,
        api_keys={
            "token-alice": "alice",
            "token-bob": "bob",
        },
        rpc_concurrency_limit=4,
        allow_adhoc_endpoints=True,
        allow_client_filesystem_paths=False,
    )
    defaults.update(setting_overrides)
    settings = APISettings(**defaults)
    return create_app(settings=settings)


ALICE_HEADERS = {"Authorization": "Bearer token-alice"}
BOB_HEADERS = {"Authorization": "Bearer token-bob"}


class TestHealthAndLifespan:
    def test_liveness_readiness_and_lifespan_shutdown(self):
        app = _make_test_app()
        sub_spy = MagicMock(wraps=app.state.subscription_service)
        sess_spy = MagicMock(wraps=app.state.session_manager)
        app.state.subscription_service = sub_spy
        app.state.session_manager = sess_spy

        with TestClient(app) as client:
            r_live = client.get("/healthz")
            assert r_live.status_code == 200
            assert r_live.json() == {"status": "ok"}

            r_ready = client.get("/readyz")
            assert r_ready.status_code == 200
            body = r_ready.json()
            assert body["ready"] is True
            assert body["status"] == "ready"
            assert all(body["services"].values())

            app.state.ready = False
            r_not_ready = client.get("/readyz")
            assert r_not_ready.status_code == 503
            assert r_not_ready.json()["ready"] is False
            app.state.ready = True

        sub_spy.stop_all.assert_called_once()
        sess_spy.close_all.assert_called_once()


class TestAuthSecurityAndRedaction:
    def test_requires_valid_authentication(self):
        app = _make_test_app()
        client = TestClient(app)

        assert client.get("/api/v1/devices").status_code == 401
        assert client.get("/api/v1/devices", headers={"X-API-Key": "wrong"}).status_code == 401
        assert client.get("/api/v1/devices", headers=ALICE_HEADERS).status_code == 200

    def test_rejects_client_filesystem_paths_and_disallowed_targets(self):
        app = _make_test_app(
            allowed_targets=("172.20.20.2:57401", "172.20.20.4"),
            allow_client_filesystem_paths=False,
        )
        client = TestClient(app)

        # Client-supplied ssh_key / tls_key rejected
        resp_key = client.post(
            "/api/v1/operations/execute",
            headers=ALICE_HEADERS,
            json={
                "adhoc_endpoint": {
                    "target": "172.20.20.4:830",
                    "protocol": "netconf",
                    "ssh_key": "/home/user/.ssh/id_rsa",
                },
                "operations": [{"op_type": "capabilities"}],
            },
        )
        assert resp_key.status_code == 400
        assert resp_key.json()["error_code"] == "FORBIDDEN_FILESYSTEM_PATH"

        # Client-supplied schema_dirs outside allow-list rejected
        resp_schema_dir = client.post(
            "/api/v1/operations/execute",
            headers=ALICE_HEADERS,
            json={
                "adhoc_endpoint": {
                    "target": "172.20.20.4:830",
                    "protocol": "netconf",
                },
                "operations": [{"op_type": "capabilities"}],
                "schema_dirs": ["/etc/passwd"],
            },
        )
        assert resp_schema_dir.status_code == 400
        assert resp_schema_dir.json()["error_code"] == "FORBIDDEN_FILESYSTEM_PATH"

        # Target outside allow-list rejected
        resp_target = client.post(
            "/api/v1/operations/execute",
            headers=ALICE_HEADERS,
            json={
                "adhoc_endpoint": {
                    "target": "192.0.2.99:830",
                    "protocol": "netconf",
                },
                "operations": [{"op_type": "capabilities"}],
            },
        )
        assert resp_target.status_code == 403
        assert resp_target.json()["error_code"] == "PERMISSION_DENIED"

    def test_device_crud_redacts_secrets(self):
        app = _make_test_app(allow_client_filesystem_paths=True)
        client = TestClient(app)

        create_resp = client.post(
            "/api/v1/devices",
            headers=ALICE_HEADERS,
            json={
                "device_id": "srl-01",
                "endpoint": {
                    "target": "172.20.20.2:57401",
                    "protocol": "gnmi",
                    "username": "admin",
                    "password": "super-secret-password",
                    "tls_key": "/etc/ssl/private/client.key",
                    "insecure": True,
                },
            },
        )
        assert create_resp.status_code == 201
        created = create_resp.json()
        assert created["device_id"] == "srl-01"
        assert created["has_password"] is True
        assert created["has_tls_key"] is True
        assert "password" not in created
        assert "tls_key" not in created
        assert "super-secret-password" not in create_resp.text

        get_resp = client.get("/api/v1/devices/srl-01", headers=ALICE_HEADERS)
        assert get_resp.status_code == 200
        assert get_resp.json()["has_password"] is True
        assert "super-secret-password" not in get_resp.text

        list_resp = client.get("/api/v1/devices", headers=ALICE_HEADERS)
        assert list_resp.status_code == 200
        assert [d["device_id"] for d in list_resp.json()] == ["srl-01"]

        del_resp = client.delete("/api/v1/devices/srl-01", headers=ALICE_HEADERS)
        assert del_resp.status_code == 204
        assert client.get("/api/v1/devices/srl-01", headers=ALICE_HEADERS).status_code == 404

    def test_unexpected_exception_and_error_redaction(self):
        app = _make_test_app()
        fake_op_svc = MagicMock()
        fake_op_svc.execute_unary.side_effect = RuntimeError(
            "Traceback leak password=TopSecret123 key=/home/admin/.ssh/id_rsa"
        )
        app.dependency_overrides[get_operation_service] = lambda: fake_op_svc
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/api/v1/operations/execute",
            headers=ALICE_HEADERS,
            json={
                "adhoc_endpoint": {"target": "172.20.20.2:57401", "protocol": "gnmi", "insecure": True},
                "operations": [{"op_type": "capabilities"}],
            },
        )
        assert resp.status_code == 500
        body = resp.json()
        assert body["error_code"] == "INTERNAL_SERVER_ERROR"
        assert "TopSecret123" not in resp.text
        assert "id_rsa" not in resp.text
        assert "Traceback" not in resp.text


class TestOperationSchemasAndAdapters:
    def test_pydantic_rejects_invalid_requests(self):
        app = _make_test_app()
        client = TestClient(app)

        # Unknown field (extra="forbid")
        r_extra = client.post(
            "/api/v1/operations/execute",
            headers=ALICE_HEADERS,
            json={
                "adhoc_endpoint": {"target": "172.20.20.2:57401", "protocol": "gnmi"},
                "operations": [{"op_type": "capabilities"}],
                "unexpected_field": True,
            },
        )
        assert r_extra.status_code == 422

        # Both device_id and adhoc_endpoint provided
        r_both = client.post(
            "/api/v1/operations/execute",
            headers=ALICE_HEADERS,
            json={
                "device_id": "srl-01",
                "adhoc_endpoint": {"target": "172.20.20.2:57401", "protocol": "gnmi"},
                "operations": [{"op_type": "capabilities"}],
            },
        )
        assert r_both.status_code == 422

        # Invalid datastore
        r_ds = client.post(
            "/api/v1/operations/execute",
            headers=ALICE_HEADERS,
            json={
                "adhoc_endpoint": {"target": "172.20.20.4:830", "protocol": "netconf"},
                "operations": [{"op_type": "get_config", "datastore": "bogus"}],
            },
        )
        assert r_ds.status_code == 422

        # Protocol-operation mismatch (NETCONF lock on gNMI)
        r_mismatch = client.post(
            "/api/v1/operations/execute",
            headers=ALICE_HEADERS,
            json={
                "adhoc_endpoint": {"target": "172.20.20.2:57401", "protocol": "gnmi"},
                "operations": [{"op_type": "lock", "datastore": "candidate"}],
            },
        )
        assert r_mismatch.status_code == 422

    def test_execute_maps_to_real_dtos_and_preserves_partial_pipeline_results(self):
        app = _make_test_app()
        captured_requests = []

        class FakeOpService:
            def execute_unary(self, req: ExecutionRequestDTO) -> ExecutionResultDTO:
                captured_requests.append(req)
                return ExecutionResultDTO(
                    target="172.20.20.4:830",
                    protocol="netconf",
                    success=False,
                    results=[
                        StepResultDTO(op_type="lock", op_id="s1", success=True, data="<ok/>"),
                        StepResultDTO(
                            op_type="edit-config",
                            op_id="s2",
                            success=False,
                            error="RPC failed with password=SecretValue99",
                            error_code="RPC_EXECUTION_ERROR",
                        ),
                    ],
                    error_summary="RPC failed with password=SecretValue99",
                    error_code="RPC_EXECUTION_ERROR",
                    execution_time_ms=12.5,
                )

        app.dependency_overrides[get_operation_service] = lambda: FakeOpService()
        client = TestClient(app)

        resp = client.post(
            "/api/v1/operations/execute",
            headers=ALICE_HEADERS,
            json={
                "adhoc_endpoint": {
                    "target": "172.20.20.4:830",
                    "protocol": "netconf",
                    "username": "root",
                    "password": "pw",
                },
                "continue_on_error": True,
                "operations": [
                    {"op_type": "lock", "op_id": "s1", "datastore": "candidate"},
                    {
                        "op_type": "edit_config",
                        "op_id": "s2",
                        "datastore": "candidate",
                        "changes": [
                            {
                                "path": "/system/hostname",
                                "value": "netopeer-node",
                                "operation": "merge",
                            }
                        ],
                        "telemetry": {"mode": "event_driven", "sample_interval_sec": 0},
                    },
                ],
            },
        )
        assert resp.status_code == 200
        assert len(captured_requests) == 1
        dto = captured_requests[0]
        assert isinstance(dto, ExecutionRequestDTO)
        assert isinstance(dto.adhoc_endpoint, AdhocEndpointDTO)
        assert isinstance(dto.operations[1], OperationStepDTO)
        assert isinstance(dto.operations[1].changes[0], MutationChangeDTO)
        assert isinstance(dto.operations[1].telemetry, TelemetryPolicyDTO)
        assert dto.operations[1].telemetry.mode == DeliveryModeDTO.EVENT_DRIVEN

        body = resp.json()
        assert body["success"] is False
        assert len(body["results"]) == 2
        assert body["results"][0]["op_id"] == "s1"
        assert body["results"][0]["success"] is True
        assert body["results"][1]["op_id"] == "s2"
        assert body["results"][1]["success"] is False
        assert "SecretValue99" not in resp.text
        assert "[REDACTED]" in body["results"][1]["error"]

    def test_execute_unregistered_device_returns_404(self):
        app = _make_test_app()
        client = TestClient(app)
        resp = client.post(
            "/api/v1/operations/execute",
            headers=ALICE_HEADERS,
            json={
                "device_id": "missing-dev",
                "operations": [{"op_type": "capabilities"}],
            },
        )
        assert resp.status_code == 404
        assert resp.json()["error_code"] == "DEVICE_NOT_FOUND"


class TestConcurrencyAndBlockingIO:
    def test_capacity_limiter_bounds_concurrent_blocking_rpcs(self):
        app = _make_test_app(rpc_concurrency_limit=2)
        lock = threading.Lock()
        state = {"active": 0, "max_active": 0, "threads": set()}

        class SlowFakeOpService:
            def execute_unary(self, req: ExecutionRequestDTO) -> ExecutionResultDTO:
                with lock:
                    state["active"] += 1
                    state["max_active"] = max(state["max_active"], state["active"])
                    state["threads"].add(threading.current_thread().ident)
                try:
                    time.sleep(0.08)
                finally:
                    with lock:
                        state["active"] -= 1
                return ExecutionResultDTO(
                    target="172.20.20.2:57401",
                    protocol="gnmi",
                    success=True,
                    results=[StepResultDTO(op_type="capability", success=True, data={"ok": True})],
                )

        app.dependency_overrides[get_operation_service] = lambda: SlowFakeOpService()

        async def _run_concurrent():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                payload = {
                    "adhoc_endpoint": {
                        "target": "172.20.20.2:57401",
                        "protocol": "gnmi",
                        "insecure": True,
                    },
                    "operations": [{"op_type": "capabilities"}],
                }
                tasks = [
                    ac.post("/api/v1/operations/execute", headers=ALICE_HEADERS, json=payload)
                    for _ in range(5)
                ]
                tasks.append(ac.get("/healthz"))
                responses = await asyncio.gather(*tasks)
                assert all(r.status_code == 200 for r in responses)

        anyio.run(_run_concurrent)
        assert state["max_active"] <= 2


class TestManagedSessionRoutes:
    def test_session_lifecycle_and_owner_isolation_over_http(self):
        created_clients = []

        class FakeClient:
            def __init__(self, target="172.20.20.4:830", **kwargs):
                self.target = target
                self.entered = 0
                self.exited = 0
                self.calls = []
                created_clients.append(self)

            def __enter__(self):
                self.entered += 1
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                self.exited += 1
                return False

            def execute(self, op):
                self.calls.append(type(op).__name__)
                return "<ok/>"

            def execute_transaction(self, op):
                self.calls.append(f"tx:{op.operation.value}")
                return "<ok/>"

        app = _make_test_app()
        reg = app.state.device_registry
        reg.register_device(
            "netopeer2",
            AdhocEndpointDTO(
                target="172.20.20.4:830",
                protocol="netconf",
                username="root",
                password="root",
            ),
        )
        sess_mgr = DeviceSessionManager(
            device_registry=reg,
            client_factory=lambda **kw: FakeClient(target=kw["target"]),
        )
        app.state.session_manager = sess_mgr
        app.state.operation_service._session_manager = sess_mgr

        with TestClient(app) as client:
            create_resp = client.post(
                "/api/v1/devices/netopeer2/sessions",
                headers=ALICE_HEADERS,
                json={"ttl_seconds": 120},
            )
            assert create_resp.status_code == 201
            session_id = create_resp.json()["session_id"]
            assert len(created_clients) == 1
            assert created_clients[0].entered == 1

            # Bob cannot inspect, execute on, or delete Alice's session
            assert client.get(f"/api/v1/sessions/{session_id}", headers=BOB_HEADERS).status_code == 403
            assert (
                client.post(
                    f"/api/v1/sessions/{session_id}/operations",
                    headers=BOB_HEADERS,
                    json={"operations": [{"op_type": "get", "paths": ["/system"]}]},
                ).status_code
                == 403
            )
            assert client.delete(f"/api/v1/sessions/{session_id}", headers=BOB_HEADERS).status_code == 403

            # Alice runs two separate operations reusing the same NetconfClient
            r_lock = client.post(
                f"/api/v1/sessions/{session_id}/operations",
                headers=ALICE_HEADERS,
                json={"operations": [{"op_type": "lock", "datastore": "candidate"}]},
            )
            assert r_lock.status_code == 200
            assert r_lock.json()["success"] is True

            r_commit = client.post(
                f"/api/v1/sessions/{session_id}/operations",
                headers=ALICE_HEADERS,
                json={
                    "operations": [
                        {"op_type": "commit"},
                        {"op_type": "unlock", "datastore": "candidate"},
                    ]
                },
            )
            assert r_commit.status_code == 200
            assert r_commit.json()["success"] is True
            assert len(created_clients) == 1
            assert created_clients[0].exited == 0

            list_alice = client.get("/api/v1/sessions", headers=ALICE_HEADERS).json()
            list_bob = client.get("/api/v1/sessions", headers=BOB_HEADERS).json()
            assert [s["session_id"] for s in list_alice] == [session_id]
            assert list_bob == []

            del_resp = client.delete(f"/api/v1/sessions/{session_id}", headers=ALICE_HEADERS)
            assert del_resp.status_code == 204
            assert created_clients[0].exited == 1


class TestSubscriptionAndWebSocketRoutes:
    @patch("application.subscriptions.SubscribeSession")
    def test_subscription_lifecycle_and_websocket_detach_reconnect(self, mock_session_cls):
        mock_worker = MagicMock()
        mock_worker.session_id = "sub-sess-1"
        mock_worker.is_running = True
        mock_worker.is_poll_mode.return_value = True
        mock_session_cls.return_value = mock_worker

        app = _make_test_app()
        with TestClient(app) as client:
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
                            "options": {"subscription_name": "intf-sub"},
                        }
                    ],
                },
            )
            assert create_resp.status_code == 201
            sub_id = create_resp.json()["subscription_id"]

            # Bob cannot inspect, poll, attach WS, or stop Alice's subscription
            assert client.get(f"/api/v1/subscriptions/{sub_id}", headers=BOB_HEADERS).status_code == 403
            assert client.post(f"/api/v1/subscriptions/{sub_id}/poll", headers=BOB_HEADERS).status_code == 403
            assert client.delete(f"/api/v1/subscriptions/{sub_id}", headers=BOB_HEADERS).status_code == 403

            # Push first telemetry event and attach first WebSocket consumer
            managed = app.state.subscription_service._get_managed(sub_id)
            managed.data_queue.put(
                {
                    "session_id": "s1",
                    "target": "172.20.20.2:57401",
                    "protocol": "gnmi",
                    "subscription_name": "intf-sub",
                    "data": {"seq": 1},
                    "is_sync_marker": False,
                }
            )

            with client.websocket_connect(
                f"/api/v1/subscriptions/{sub_id}/ws",
                headers=ALICE_HEADERS,
            ) as ws1:
                ev1 = ws1.receive_json()
                assert ev1["data"] == {"seq": 1}

            # Crucial invariant: WebSocket disconnect must NOT stop the subscription!
            info_after_detach = client.get(
                f"/api/v1/subscriptions/{sub_id}", headers=ALICE_HEADERS
            )
            assert info_after_detach.status_code == 200
            assert info_after_detach.json()["is_alive"] is True

            # Poll and reconnect with a second WebSocket consumer
            poll_resp = client.post(
                f"/api/v1/subscriptions/{sub_id}/poll", headers=ALICE_HEADERS
            )
            assert poll_resp.status_code == 200
            mock_worker.trigger_poll.assert_called_once()

            managed.data_queue.put(
                {
                    "session_id": "s1",
                    "target": "172.20.20.2:57401",
                    "protocol": "gnmi",
                    "subscription_name": "intf-sub",
                    "data": {"seq": 2},
                    "is_sync_marker": False,
                }
            )
            with client.websocket_connect(
                f"/api/v1/subscriptions/{sub_id}/ws?token=token-alice"
            ) as ws2:
                ev2 = ws2.receive_json()
                assert ev2["data"] == {"seq": 2}

            # Stop explicitly
            del_resp = client.delete(
                f"/api/v1/subscriptions/{sub_id}", headers=ALICE_HEADERS
            )
            assert del_resp.status_code == 204
            mock_worker.stop.assert_called_once()


class TestSchemaRoutes:
    def test_schema_catalog_routes_omit_host_paths_and_validate(self):
        app = _make_test_app()
        fake_schema = MagicMock()
        fake_schema.list_cached_modules.return_value = [
            {
                "module_id": 1,
                "name": "openconfig-interfaces",
                "revision": "2021-04-06",
                "namespace": "http://openconfig.net/yang/interfaces",
                "kind": "module",
                "sha256": "abc123",
                "file_path": "/secret/host/cache/openconfig-interfaces.yang",
                "source": "netconf_get_schema",
            }
        ]
        fake_schema.get_database_statistics.return_value = {"modules": 1, "profiles": 1}
        fake_schema.get_profile_modules.return_value = [
            {"module_id": 1, "name": "openconfig-interfaces", "file_path": "/secret/path"}
        ]
        fake_schema.validate_request_schema.return_value = []

        app.dependency_overrides[get_schema_service] = lambda: fake_schema
        client = TestClient(app)

        mods_resp = client.get("/api/v1/schemas/modules", headers=ALICE_HEADERS)
        assert mods_resp.status_code == 200
        mods = mods_resp.json()
        assert len(mods) == 1
        assert mods[0]["name"] == "openconfig-interfaces"
        assert "file_path" not in mods[0]
        assert "/secret/host" not in mods_resp.text

        stats_resp = client.get("/api/v1/schemas/statistics", headers=ALICE_HEADERS)
        assert stats_resp.status_code == 200
        assert stats_resp.json() == {"statistics": {"modules": 1, "profiles": 1}}

        prof_resp = client.get(
            "/api/v1/schemas/profiles/srlinux/modules", headers=ALICE_HEADERS
        )
        assert prof_resp.status_code == 200
        assert prof_resp.json()[0]["name"] == "openconfig-interfaces"
        assert "file_path" not in prof_resp.json()[0]

        val_resp = client.post(
            "/api/v1/schemas/validate",
            headers=ALICE_HEADERS,
            json={
                "adhoc_endpoint": {
                    "target": "172.20.20.2:57401",
                    "protocol": "gnmi",
                    "insecure": True,
                },
                "schema_profile_id": "srlinux",
                "operations": [{"op_type": "get", "paths": ["/interfaces"]}],
            },
        )
        assert val_resp.status_code == 200
        assert val_resp.json() == {"valid": True, "errors": []}
