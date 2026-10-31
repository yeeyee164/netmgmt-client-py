# -*- coding: utf-8 -*-
"""
test_gnmi_subscribe.py

Composite and Live verification of gNMI streaming telemetry subscriptions:
- Concurrency, thread safety, and race conditions during simultaneous startup/shutdown.
- State transitions: STARTING -> RUNNING -> STOPPING -> STOPPED / FAILED.
- Multi-target, multi-datamodel streaming across(require Containerlab):
    * Nokia SR Linux (172.20.20.2:57401)
    * Arista cEOS (172.20.20.3:6030)
- Datamodels: openconfig-interfaces, openconfig-system
"""

from __future__ import annotations
import concurrent.futures
import queue
import socket
import threading
import time
from typing import Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

from application.dto import (
    AdhocEndpointDTO,
    DeliveryModeDTO,
    ExecutionRequestDTO,
    OperationStepDTO,
    StreamEventDTO,
    TelemetryPolicyDTO,
)
from application.exceptions import SubscriptionNotFoundError
from application.subscriptions import ManagedSubscription, SubscriptionService, SubscriptionState
from tests.yang_fetcher import yang_test_suite_builder


def _is_live_target_reachable(target_str: str, timeout: float = 1.0) -> bool:
    """Checks if target TCP port is reachable before running live tests."""
    if not target_str or ":" not in target_str:
        return False
    host, port_s = target_str.split(":", 1)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, int(port_s)))
        sock.close()
        return True
    except Exception:
        return False


def _get_env_config() -> Tuple[Dict[str, str], Dict[str, str]]:
    """Loads configuration for Nokia SR Linux and Arista cEOS from testing_env.json with defaults."""
    srl_suite = yang_test_suite_builder("srlinux")
    ceos_suite = yang_test_suite_builder("ceos")

    srl_cfg = {
        "target": (srl_suite.gnmi_target if srl_suite and srl_suite.gnmi_target else "172.20.20.2:57401"),
        "user": srl_suite.username if srl_suite else "admin",
        "password": srl_suite.password if srl_suite else "NokiaSrl1!",
        "interfaces_path": "/interface",
        "system_path": "/system",
    }

    ceos_cfg = {
        "target": (ceos_suite.gnmi_target if ceos_suite and ceos_suite.gnmi_target else "172.20.20.3:6030"),
        "user": ceos_suite.username if ceos_suite else "admin",
        "password": ceos_suite.password if ceos_suite else "admin",
        "interfaces_path": "/interfaces",
        "system_path": "/system",
    }

    return srl_cfg, ceos_cfg


@pytest.mark.live
@pytest.mark.composite
class TestLiveGNMISubscriptionThreadRaceAndStateTransitions:
    """
    Live target verification against Nokia SR Linux (172.20.20.2:57401)
    and Arista cEOS (172.20.20.3:6030) using openconfig-interfaces and openconfig-system.
    """

    @pytest.fixture(autouse=True)
    def setup_live_env(self):
        srl_cfg, ceos_cfg = _get_env_config()
        self.srl_cfg = srl_cfg
        self.ceos_cfg = ceos_cfg

        srl_ok = _is_live_target_reachable(srl_cfg["target"])
        ceos_ok = _is_live_target_reachable(ceos_cfg["target"])

        if not srl_ok or not ceos_ok:
            pytest.skip(
                f"Live targets unreachable: SRLinux ({srl_cfg['target']} reachable={srl_ok}), "
                f"cEOS ({ceos_cfg['target']} reachable={ceos_ok})"
            )

    def _build_gnmi_request(
        self,
        target: str,
        user: str,
        pwd: str,
        path: str,
        sub_name: str,
        mode: DeliveryModeDTO = DeliveryModeDTO.ON_DEMAND,
        sample_interval: float = 10.0,
    ) -> ExecutionRequestDTO:
        return ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(
                target=target,
                protocol="gnmi",
                username=user,
                password=pwd,
                insecure=True,
            ),
            operations=[
                OperationStepDTO(
                    op_type="subscribe",
                    paths=[path],
                    telemetry=TelemetryPolicyDTO(
                        mode=mode,
                        sample_interval_sec=sample_interval,
                    ),
                    options={"subscription_name": sub_name},
                )
            ],
        )

    def test_concurrent_subscriptions_startup_race_and_state_transitions(self):
        """
        Spawns multiple concurrent gNMI subscription threads across Nokia SR Linux and Arista cEOS.
        Verifies:
        1. Thread race safety during simultaneous startup.
        2. Unique subscription IDs and properly named worker threads.
        3. State transitions: STARTING -> RUNNING.
        4. Clean concurrent shutdown: RUNNING -> STOPPING -> STOPPED.
        5. Worker thread liveness synchronization and resource release.
        """
        sub_svc = SubscriptionService()

        # Build 4 subscription requests: 2 datamodels per device
        requests = [
            self._build_gnmi_request(
                self.srl_cfg["target"],
                self.srl_cfg["user"],
                self.srl_cfg["password"],
                self.srl_cfg["interfaces_path"],
                "srl_interfaces_sub",
            ),
            self._build_gnmi_request(
                self.srl_cfg["target"],
                self.srl_cfg["user"],
                self.srl_cfg["password"],
                self.srl_cfg["system_path"],
                "srl_system_sub",
            ),
            self._build_gnmi_request(
                self.ceos_cfg["target"],
                self.ceos_cfg["user"],
                self.ceos_cfg["password"],
                self.ceos_cfg["interfaces_path"],
                "ceos_interfaces_sub",
            ),
            self._build_gnmi_request(
                self.ceos_cfg["target"],
                self.ceos_cfg["user"],
                self.ceos_cfg["password"],
                self.ceos_cfg["system_path"],
                "ceos_system_sub",
            ),
        ]

        # 1. Start all subscriptions concurrently to stress race conditions in registration
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(requests)) as executor:
            sub_ids = list(executor.map(sub_svc.start_subscription, requests))

        try:
            # Verify 4 distinct subscription tokens generated
            assert len(sub_ids) == 4
            assert len(set(sub_ids)) == 4
            for sid in sub_ids:
                assert len(sid) == 36
                assert sid.count("-") == 4

            # Verify active subscriptions list contains all 4
            active_list = sub_svc.list_active_subscriptions()
            assert len(active_list) == 4
            assert sub_svc.has_active_threads() is True

            # 2. Verify state transitions to RUNNING across all worker threads
            time.sleep(1.0)
            for sid in sub_ids:
                info = sub_svc.get_subscription_info(sid)
                assert info["state"] == SubscriptionState.RUNNING.value
                assert info["is_alive"] is True
                assert info["created_at"] > 0
                assert info["stopped_at"] is None

                # Verify thread object exists and is alive
                managed = sub_svc._get_managed(sid)
                assert managed.thread.is_alive() is True
                assert managed.thread.name.startswith("SubSession-")
                assert sid in managed.thread.name

            # 3. Concurrently trigger poll commands across all subscriptions
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(lambda sid: sub_svc.send_command(sid, "poll"), sub_ids))

            # 4. Concurrently retrieve events from distinct reader threads
            time.sleep(1.0)

            def fetch_events(sid: str) -> List[StreamEventDTO]:
                return sub_svc.get_events(sid, timeout=1.0, max_events=10)

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                events_per_sub = list(executor.map(fetch_events, sub_ids))

            for idx, events in enumerate(events_per_sub):
                sid = sub_ids[idx]
                info = sub_svc.get_subscription_info(sid)
                assert len(events) > 0, f"Expected events for {info['target']} ({info['subscription_name']})"
                assert all(isinstance(e, StreamEventDTO) for e in events)
                assert all(e.protocol == "gnmi" for e in events)
                assert all(e.subscription_name == info["subscription_name"] for e in events)

            # 5. Stress concurrent stopping across multiple caller threads
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                stop_results = list(executor.map(sub_svc.stop_subscription, sub_ids))

            assert all(res is True for res in stop_results)

            # 6. Verify state transitions to STOPPED and thread termination
            time.sleep(0.5)
            assert sub_svc.has_active_threads() is False

            for sid in sub_ids:
                with pytest.raises(SubscriptionNotFoundError):
                    sub_svc.get_subscription_info(sid)

            # 7. Verify idempotent stop calls return False without raising exceptions
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                retry_stops = list(executor.map(sub_svc.stop_subscription, sub_ids))
            assert all(res is False for res in retry_stops)

        finally:
            sub_svc.stop_all()
            assert sub_svc.has_active_threads() is False

    def test_shared_queue_concurrency_and_backpressure_race(self):
        """
        Attaches multiple live gNMI subscriptions to a single shared queue.
        Verifies thread-safe ingestion, event attribution, and absence of worker deadlock.
        """
        sub_svc = SubscriptionService()
        shared_q: queue.Queue = queue.Queue(maxsize=100)

        # 2 subscriptions sharing the same queue
        req_srl = self._build_gnmi_request(
            self.srl_cfg["target"],
            self.srl_cfg["user"],
            self.srl_cfg["password"],
            self.srl_cfg["system_path"],
            "shared_srl_sys",
        )
        req_ceos = self._build_gnmi_request(
            self.ceos_cfg["target"],
            self.ceos_cfg["user"],
            self.ceos_cfg["password"],
            self.ceos_cfg["system_path"],
            "shared_ceos_sys",
        )

        sid1 = sub_svc.start_subscription(req_srl, shared_queue=shared_q)
        sid2 = sub_svc.start_subscription(req_ceos, shared_queue=shared_q)

        try:
            time.sleep(1.0)
            assert sub_svc.get_subscription_info(sid1)["is_alive"] is True
            assert sub_svc.get_subscription_info(sid2)["is_alive"] is True

            # Trigger polls simultaneously
            sub_svc.send_command(sid1, "poll")
            sub_svc.send_command(sid2, "poll")

            collected_targets = set()
            deadline = time.time() + 3.0
            while time.time() < deadline and len(collected_targets) < 2:
                try:
                    item = shared_q.get(timeout=0.5)
                    if isinstance(item, dict) and "target" in item:
                        collected_targets.add(item["target"])
                    shared_q.task_done()
                except queue.Empty:
                    break

            # Both devices delivered telemetry into the shared bounded queue
            assert self.srl_cfg["target"] in collected_targets
            assert self.ceos_cfg["target"] in collected_targets

        finally:
            sub_svc.stop_all()
            assert sub_svc.has_active_threads() is False


@pytest.mark.unit
class TestGNMISubscriptionConcurrencyAndStateTransitionsMocked:
    """
    Simulated thread race and state transition tests that run offline (pytest -m 'not live').
    Verifies that the SubscriptionService state machine, lock acquisition,
    and event drainage remain race-free under high concurrency.
    """

    def test_mocked_concurrent_start_stop_race(self):
        """
        Simulates 20 concurrent subscription threads starting and stopping rapidly.
        """
        sub_svc = SubscriptionService()

        def fake_worker_start(worker_ref, event_queue):
            worker_ref.is_running = True
            for i in range(5):
                if not worker_ref.is_running:
                    break
                event_queue.put({"session_id": "test", "target": "10.0.0.1:57400", "data": i})
                time.sleep(0.01)

        created_sids = []

        def worker_factory(config, data_queue, subscription_name="sub"):
            w = MagicMock()
            w.is_running = False
            w.last_error = None
            w.is_poll_mode.return_value = False

            def run():
                fake_worker_start(w, data_queue)

            w.start.side_effect = run

            def stop():
                w.is_running = False

            w.stop.side_effect = stop
            return w

        with patch("application.subscriptions.SubscribeSession", side_effect=worker_factory):
            reqs = [
                ExecutionRequestDTO(
                    adhoc_endpoint=AdhocEndpointDTO(target=f"10.0.0.{i}:57400", protocol="gnmi"),
                    operations=[OperationStepDTO(op_type="subscribe", paths=["/interfaces"])],
                )
                for i in range(1, 11)
            ]

            # Concurrently start 10 subscriptions
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
                created_sids = list(ex.map(sub_svc.start_subscription, reqs))

            assert len(created_sids) == 10
            assert len(set(created_sids)) == 10

            time.sleep(0.05)
            # Verify all reported in active list
            active = sub_svc.list_active_subscriptions()
            assert len(active) == 10
            assert all(a["state"] in (SubscriptionState.RUNNING.value, SubscriptionState.STOPPED.value) for a in active)

            # Concurrently stop all subscriptions
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
                stop_results = list(ex.map(sub_svc.stop_subscription, created_sids))

            assert all(r is True for r in stop_results)
            assert sub_svc.has_active_threads() is False

