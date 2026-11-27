# -*- coding: utf-8 -*-
from unittest.mock import MagicMock, patch

import anyio
from fastapi.testclient import TestClient
import pytest

from api.deps import APISettings, get_stats_service, run_sync_with_stats
from api.main import create_app
from application.stats import StatsCollector, StatsService


def _make_stats_test_app(**overrides):
    cfg = dict(
        auth_enabled=True,
        api_keys={
            "token-admin": "admin-user",
            "token-alice": "alice",
            "token-bob": "bob",
        },
        admin_owners=("admin-user",),
        rpc_concurrency_limit=2,
        allow_adhoc_endpoints=True,
    )
    cfg.update(overrides)
    return create_app(APISettings(**cfg))


ADMIN_HEADERS = {"Authorization": "Bearer token-admin"}
ALICE_HEADERS = {"Authorization": "Bearer token-alice"}
BOB_HEADERS = {"Authorization": "Bearer token-bob"}


class TestAPIStatsEndpoints:
    def test_stats_requires_auth_and_enforces_admin_reset_and_owner_isolation(self):
        app = _make_stats_test_app()
        collector = StatsCollector()
        app.dependency_overrides[get_stats_service] = lambda: StatsService(collector)
        collector.record_operation_step(
            protocol="gnmi", target="10.0.0.1:57400", op_type="get", success=True, duration_ms=5.0
        )
        collector.record_subscription_started(
            "sub-alice", "gnmi", "periodic", "10.0.0.1:57400", owner_id="alice"
        )
        collector.record_subscription_started(
            "sub-bob", "netconf", "periodic", "10.0.0.2:830", owner_id="bob"
        )

        with TestClient(app) as client:
            assert client.get("/api/v1/stats").status_code == 401

            # Non-admin sees only own subscriptions
            r_alice = client.get("/api/v1/stats", headers=ALICE_HEADERS)
            assert r_alice.status_code == 200
            body_alice = r_alice.json()
            assert body_alice["operations"]["total"] == 1
            assert [s["subscription_id"] for s in body_alice["subscriptions"]] == ["sub-alice"]

            # Cross-tenant subscription stats denied
            assert (
                client.get("/api/v1/stats/subscriptions/sub-bob", headers=ALICE_HEADERS).status_code
                == 403
            )
            assert (
                client.get("/api/v1/stats/subscriptions/sub-alice", headers=ALICE_HEADERS).status_code
                == 200
            )
            assert (
                client.get("/api/v1/stats/subscriptions/unknown-id", headers=ADMIN_HEADERS).status_code
                == 404
            )

            # Admin sees all and filters by protocol/target
            r_admin = client.get("/api/v1/stats?protocol=gnmi", headers=ADMIN_HEADERS)
            assert r_admin.status_code == 200
            assert len(r_admin.json()["subscriptions"]) == 1

            # Non-admin cannot reset
            assert client.post("/api/v1/stats/reset", headers=ALICE_HEADERS).status_code == 403

            # Admin reset clears counters but keeps active subscriptions gauge
            r_reset = client.post("/api/v1/stats/reset", headers=ADMIN_HEADERS)
            assert r_reset.status_code == 200
            assert r_reset.json()["status"] == "reset"

            r_after = client.get("/api/v1/stats", headers=ADMIN_HEADERS)
            assert r_after.json()["operations"]["total"] == 0
            assert r_after.json()["subscriptions_active"] == 2

    def test_execute_operation_embeds_stats_and_request_id(self):
        app = _make_stats_test_app()
        with patch("application.operations.DeviceOperationService._resolve_worker") as mock_resolve:
            mock_worker = MagicMock()
            mock_worker.start.return_value = {
                "session_id": "s1",
                "target": "10.0.0.1:57400",
                "rpc": "get",
                "data": {"ok": True},
                "protocol": "gnmi",
            }
            mock_resolve.return_value = mock_worker

            with TestClient(app) as client:
                resp = client.post(
                    "/api/v1/operations/execute",
                    headers={**ALICE_HEADERS, "X-Request-ID": "trace-abc-99"},
                    json={
                        "adhoc_endpoint": {
                            "target": "10.0.0.1:57400",
                            "protocol": "gnmi",
                            "insecure": True,
                        },
                        "operations": [{"op_type": "get", "paths": ["/interfaces"]}],
                    },
                )

        assert resp.status_code == 200
        assert resp.headers["X-Request-ID"] == "trace-abc-99"
        body = resp.json()
        assert body["stats"] is not None
        assert body["stats"]["request_id"] == "trace-abc-99"
        assert body["stats"]["protocol"] == "gnmi"
        assert body["stats"]["total_duration_ms"] >= 0.0

    def test_websocket_stats_stream_and_auth(self):
        app = _make_stats_test_app()
        collector: StatsCollector = app.state.stats_collector
        collector.record_subscription_started(
            "sub-ws-alice", "gnmi", "periodic", "10.0.0.1:57400", owner_id="alice"
        )

        with TestClient(app) as client:
            with client.websocket_connect(
                "/api/v1/stats/ws?token=token-alice&interval_ms=50"
            ) as ws:
                frame = ws.receive_json()
                assert "operations" in frame
                assert [s["subscription_id"] for s in frame["subscriptions"]] == ["sub-ws-alice"]
                assert frame["runtime"]["websocket_clients_active"].get("stats") == 1

            snap = app.state.stats_service.get_snapshot()
            assert snap.runtime.websocket_clients_active.get("stats", 0) == 0

    def test_run_sync_with_stats_tracks_semaphore_and_restores_on_error(self):
        collector = StatsCollector()
        limiter = anyio.CapacityLimiter(1)

        async def _exercise():
            res = await run_sync_with_stats(
                lambda: 42,
                limiter=limiter,
                stats_collector=collector,
                worker_type="unary",
            )
            assert res == 42

            def _boom():
                raise RuntimeError("boom")

            with pytest.raises(RuntimeError, match="boom"):
                await run_sync_with_stats(
                    _boom,
                    limiter=limiter,
                    stats_collector=collector,
                    worker_type="unary",
                )

        anyio.run(_exercise)
        snap = collector.snapshot()
        assert snap.runtime.worker_threads_active.get("unary", 0) == 0
        assert snap.runtime.worker_semaphore_wait.count == 2
