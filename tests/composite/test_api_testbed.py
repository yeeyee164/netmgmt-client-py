# -*- coding: utf-8 -*-
"""
Live testbed integration suite for the FastAPI presentation tier.

Exercises health probes, inventory seeding, gNMI/NETCONF unary pipelines,
gNMI streaming subscriptions + WebSocket detach/reconnect, managed persistent
logical NETCONF sessions with owner isolation, and schema catalog metrics
against Nokia SRLinux (172.20.20.2), Arista cEOS (172.20.20.3), and
Netopeer2 (172.20.20.4).
"""

from pathlib import Path
import socket
import time

from fastapi.testclient import TestClient
import pytest

from api.deps import APISettings
from api.main import create_app


_ENV_FILE = str(Path(__file__).resolve().parents[1] / "testing_env.json")
_OP_A_HEADERS = {"Authorization": "Bearer token-op-a"}
_OP_B_HEADERS = {"Authorization": "Bearer token-op-b"}


def _require_reachable(host: str, port: int, timeout: float = 2.0) -> None:
    """Skips the test only when the live testbed TCP endpoint is unreachable."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return
    except OSError as exc:
        pytest.skip(f"Live testbed target {host}:{port} unreachable: {exc}")


@pytest.fixture(scope="module")
def api_client():
    """Creates a FastAPI TestClient seeded from `tests/testing_env.json`."""
    settings = APISettings(
        auth_enabled=True,
        api_keys={"token-op-a": "operator-a", "token-op-b": "operator-b"},
        device_seed_file=_ENV_FILE,
        allow_client_filesystem_paths=False,
        session_ttl_seconds=120.0,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        yield client


def test_seeded_inventory_and_health(api_client: TestClient):
    """Verifies `/healthz`, `/readyz`, and redacted seeded inventory endpoints."""
    assert api_client.get("/healthz").json() == {"status": "ok"}

    ready_resp = api_client.get("/readyz")
    assert ready_resp.status_code == 200
    assert ready_resp.json()["ready"] is True

    devices_resp = api_client.get("/api/v1/devices", headers=_OP_A_HEADERS)
    assert devices_resp.status_code == 200
    items = {d["device_id"]: d for d in devices_resp.json()}

    for expected_id in ("srlinux_gnmi", "srlinux_netconf", "ceos", "netopeer2"):
        assert expected_id in items
        assert items[expected_id]["has_password"] is True
        assert "password" not in items[expected_id]


def test_srlinux_gnmi_capabilities_and_get(api_client: TestClient):
    """Executes gNMI capabilities + get against live Nokia SRLinux."""
    _require_reachable("172.20.20.2", 57401)

    resp = api_client.post(
        "/api/v1/operations/execute",
        headers=_OP_A_HEADERS,
        json={
            "device_id": "srlinux_gnmi",
            "operations": [
                {"op_type": "capabilities"},
                {
                    "op_type": "get",
                    "paths": ["/system/name/host-name"],
                    "options": {"encoding": "json_ietf"},
                },
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert len(body["results"]) == 2
    assert body["results"][0]["data"]["gNMI_version"]
    assert len(body["results"][0]["data"]["supported_models"]) > 0
    assert len(body["results"][1]["data"]) > 0
    assert len(body["results"][1]["data"][0]["updates"]) > 0


def test_srlinux_gnmi_subscription_and_websocket_detach(api_client: TestClient):
    """Starts gNMI subscription on Nokia SRLinux, streams via WS, detaches, and deletes."""
    _require_reachable("172.20.20.2", 57401)

    create_resp = api_client.post(
        "/api/v1/subscriptions",
        headers=_OP_A_HEADERS,
        json={
            "device_id": "srlinux_gnmi",
            "operations": [
                {
                    "op_type": "subscribe",
                    "paths": ["/system/name/host-name"],
                    "telemetry": {"mode": "periodic", "sample_interval_sec": 1.0},
                    "options": {"encoding": "json_ietf", "subscription_name": "srl-sub"},
                }
            ],
        },
    )
    assert create_resp.status_code == 201
    sub_id = create_resp.json()["subscription_id"]

    try:
        received_events = []
        with api_client.websocket_connect(
            f"/api/v1/subscriptions/{sub_id}/ws?token=token-op-a"
        ) as ws:
            for _ in range(3):
                event = ws.receive_json()
                assert event["error"] is None
                assert isinstance(event["data"], dict)
                received_events.append(event)
                if "updates" in event["data"]:
                    break

        assert len(received_events) >= 1
        assert any("updates" in ev["data"] for ev in received_events)

        # Disconnecting the WebSocket must leave the underlying subscription running
        time.sleep(0.1)
        info_resp = api_client.get(
            f"/api/v1/subscriptions/{sub_id}",
            headers=_OP_A_HEADERS,
        )
        assert info_resp.status_code == 200
        assert info_resp.json()["is_alive"] is True
    finally:
        del_resp = api_client.delete(
            f"/api/v1/subscriptions/{sub_id}",
            headers=_OP_A_HEADERS,
        )
        assert del_resp.status_code == 204


def test_srlinux_netconf_capabilities(api_client: TestClient):
    """Executes NETCONF capabilities against live Nokia SRLinux."""
    _require_reachable("172.20.20.2", 830)

    resp = api_client.post(
        "/api/v1/operations/execute",
        headers=_OP_A_HEADERS,
        json={
            "device_id": "srlinux_netconf",
            "operations": [{"op_type": "capabilities"}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert isinstance(body["results"][0]["data"], list)
    assert len(body["results"][0]["data"]) > 0


def test_ceos_gnmi_capabilities_and_get(api_client: TestClient):
    """Executes gNMI capabilities + get against live Arista cEOS."""
    _require_reachable("172.20.20.3", 6030)

    resp = api_client.post(
        "/api/v1/operations/execute",
        headers=_OP_A_HEADERS,
        json={
            "device_id": "ceos",
            "operations": [
                {"op_type": "capabilities"},
                {
                    "op_type": "get",
                    "paths": ["/system/config/hostname"],
                    "options": {"encoding": "json_ietf"},
                },
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert len(body["results"]) == 2
    assert body["results"][0]["data"]["gNMI_version"]
    assert len(body["results"][1]["data"]) > 0
    assert len(body["results"][1]["data"][0]["updates"]) > 0


def test_ceos_gnmi_subscription_and_websocket_detach(api_client: TestClient):
    """Starts gNMI subscription on Arista cEOS, streams via WS, detaches, and deletes."""
    _require_reachable("172.20.20.3", 6030)

    create_resp = api_client.post(
        "/api/v1/subscriptions",
        headers=_OP_A_HEADERS,
        json={
            "device_id": "ceos",
            "operations": [
                {
                    "op_type": "subscribe",
                    "paths": ["/system/config/hostname"],
                    "telemetry": {"mode": "periodic", "sample_interval_sec": 1.0},
                    "options": {"encoding": "json_ietf", "subscription_name": "ceos-sub"},
                }
            ],
        },
    )
    assert create_resp.status_code == 201
    sub_id = create_resp.json()["subscription_id"]

    try:
        received_events = []
        with api_client.websocket_connect(
            f"/api/v1/subscriptions/{sub_id}/ws?token=token-op-a"
        ) as ws:
            for _ in range(3):
                event = ws.receive_json()
                assert event["error"] is None
                assert isinstance(event["data"], dict)
                received_events.append(event)
                if "updates" in event["data"]:
                    break

        assert len(received_events) >= 1

        time.sleep(0.1)
        info_resp = api_client.get(
            f"/api/v1/subscriptions/{sub_id}",
            headers=_OP_A_HEADERS,
        )
        assert info_resp.status_code == 200
        assert info_resp.json()["is_alive"] is True
    finally:
        del_resp = api_client.delete(
            f"/api/v1/subscriptions/{sub_id}",
            headers=_OP_A_HEADERS,
        )
        assert del_resp.status_code == 204


def test_netopeer2_unary_capabilities_get_config_and_get_schema(api_client: TestClient):
    """Executes NETCONF capabilities, get-config, and get-schema against live Netopeer2."""
    _require_reachable("172.20.20.4", 830)

    resp = api_client.post(
        "/api/v1/operations/execute",
        headers=_OP_A_HEADERS,
        json={
            "device_id": "netopeer2",
            "operations": [
                {"op_type": "capabilities"},
                {"op_type": "get_config", "datastore": "running"},
                {"op_type": "get_schema", "paths": ["ietf-netconf-notifications"]},
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert len(body["results"]) == 3
    assert isinstance(body["results"][0]["data"], list)
    assert isinstance(body["results"][1]["data"], dict)
    assert "module ietf-netconf-notifications" in body["results"][2]["data"]


def test_netopeer2_managed_logical_session_lifecycle_and_owner_isolation(
    api_client: TestClient,
):
    """Tests cross-request NETCONF lock -> validate/discard/unlock reuse and owner isolation."""
    _require_reachable("172.20.20.4", 830)

    create_resp = api_client.post(
        "/api/v1/devices/netopeer2/sessions",
        headers=_OP_A_HEADERS,
        json={"ttl_seconds": 120.0},
    )
    assert create_resp.status_code == 201
    session_id = create_resp.json()["session_id"]

    try:
        lock_resp = api_client.post(
            f"/api/v1/sessions/{session_id}/operations",
            headers=_OP_A_HEADERS,
            json={"operations": [{"op_type": "lock", "datastore": "candidate"}]},
        )
        assert lock_resp.status_code == 200
        assert lock_resp.json()["success"] is True

        inspect_resp = api_client.get(
            f"/api/v1/sessions/{session_id}",
            headers=_OP_A_HEADERS,
        )
        assert inspect_resp.status_code == 200
        assert inspect_resp.json()["locked_datastores"] == ["candidate"]

        # Cross-caller isolation: operator-b must be rejected with 403
        b_get = api_client.get(
            f"/api/v1/sessions/{session_id}",
            headers=_OP_B_HEADERS,
        )
        assert b_get.status_code == 403
        assert b_get.json()["error_code"] == "PERMISSION_DENIED"

        b_exec = api_client.post(
            f"/api/v1/sessions/{session_id}/operations",
            headers=_OP_B_HEADERS,
            json={"operations": [{"op_type": "validate", "datastore": "candidate"}]},
        )
        assert b_exec.status_code == 403
        assert b_exec.json()["error_code"] == "PERMISSION_DENIED"

        # Step 2 reusing the exact same NETCONF SSH session holding candidate lock
        step2_resp = api_client.post(
            f"/api/v1/sessions/{session_id}/operations",
            headers=_OP_A_HEADERS,
            json={
                "operations": [
                    {"op_type": "validate", "datastore": "candidate"},
                    {"op_type": "discard_changes", "datastore": "candidate"},
                    {"op_type": "unlock", "datastore": "candidate"},
                ]
            },
        )
        assert step2_resp.status_code == 200
        assert step2_resp.json()["success"] is True

        inspect_after = api_client.get(
            f"/api/v1/sessions/{session_id}",
            headers=_OP_A_HEADERS,
        )
        assert inspect_after.status_code == 200
        assert inspect_after.json()["locked_datastores"] == []
    finally:
        del_resp = api_client.delete(
            f"/api/v1/sessions/{session_id}",
            headers=_OP_A_HEADERS,
        )
        assert del_resp.status_code == 204


def test_schema_catalog_statistics_after_discovery(api_client: TestClient):
    """Verifies `/api/v1/schemas/statistics` returns database cache metrics."""
    resp = api_client.get("/api/v1/schemas/statistics", headers=_OP_A_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["statistics"], dict)
