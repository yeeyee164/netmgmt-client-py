# -*- coding: utf-8 -*-
import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from application.devices import DeviceRegistryService
from application.dto import (
    AdhocEndpointDTO,
    ExecutionRequestDTO,
    OperationStepDTO,
    SubscriptionStatsDTO,
)
from application.exceptions import PermissionDeniedError, SubscriptionNotFoundError
from application.operations import DeviceOperationService
from application.sessions import DeviceSessionManager
from application.stats import (
    ExecutionStatsScope,
    StatsCollector,
    StatsService,
    _LatencyBuffer,
    _SlidingRateCounter,
)
from application.subscription_queue import FanoutQueue
from modules.stats_hooks import NOOP_STATS_RECORDER, NoOpStatsRecorder, StatsRecorder


class _FakeClock:
    def __init__(self, start: float = 100.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class TestLatencyBufferAndSlidingRate:
    def test_latency_buffer_ring_eviction_and_percentiles(self):
        buf = _LatencyBuffer(maxlen=5)
        for v in [10.0, 20.0, 30.0, 40.0, 50.0, 100.0, 200.0]:
            buf.observe(v)

        summary = buf.summary()
        assert summary.count == 7
        assert summary.min_ms == 10.0
        assert summary.max_ms == 200.0
        assert summary.avg_ms == pytest.approx(450.0 / 7.0)
        # Ring buffer holds [30, 40, 50, 100, 200]
        assert summary.p50_ms == 50.0
        assert summary.p95_ms == 200.0
        assert summary.p99_ms == 200.0

    def test_sliding_rate_counter_with_fake_clock(self):
        clk = _FakeClock(start=100.0)
        counter = _SlidingRateCounter(window_seconds=10, clock=clk)
        start_mono = clk()

        for _ in range(10):
            counter.increment()
        clk.advance(5.0)
        for _ in range(5):
            counter.increment()

        # 15 events over 5s uptime (< 10s window) -> 3.0 eps
        assert counter.rate_eps(start_mono) == pytest.approx(3.0)

        # Advance 11 seconds; initial 10 events expire, remaining 5 expire at t=115+
        clk.advance(6.0)
        # At t=111, t=100 bucket expired; t=105 bucket (5 events) is still inside [102..111]
        assert counter.rate_eps(start_mono) == pytest.approx(0.5)


class TestStatsCollectorAndScopes:
    def test_noop_recorder_satisfies_runtime_checkable_protocol(self):
        assert isinstance(NOOP_STATS_RECORDER, StatsRecorder)
        assert isinstance(NoOpStatsRecorder(), StatsRecorder)

    def test_execution_stats_scope_isolates_and_forwards(self):
        clk = _FakeClock(start=10.0)
        collector = StatsCollector(clock=clk)
        scope = ExecutionStatsScope(
            parent=collector,
            protocol="netconf",
            target="172.20.20.4:830",
            request_id="req-123",
            clock=clk,
        )
        scope.record_connect("netconf", "172.20.20.4:830", 12.5, reused=False)
        scope.record_schema_validation("netconf", True, 2.0)
        scope.record_operation_step(
            protocol="netconf",
            target="172.20.20.4:830",
            op_type="get",
            success=True,
            duration_ms=25.0,
            wire_rpc_ms=22.0,
            request_bytes=120,
            response_bytes=840,
            path_count=1,
        )
        scope.record_operation_aborted("netconf", "172.20.20.4:830", "commit")
        clk.advance(0.05)
        exec_dto = scope.finalize()

        assert exec_dto.request_id == "req-123"
        assert exec_dto.total_duration_ms == pytest.approx(50.0)
        assert exec_dto.connect_duration_ms == pytest.approx(12.5)
        assert exec_dto.validation_passed is True
        assert exec_dto.operations_total == 1
        assert exec_dto.operations_succeeded == 1
        assert exec_dto.operations_aborted == 1
        assert exec_dto.request_payload_bytes == 120
        assert exec_dto.response_payload_bytes == 840
        assert len(exec_dto.steps) == 2
        assert exec_dto.steps[1].aborted is True

        snap = StatsService(collector).get_snapshot()
        assert snap.operations.total == 1
        assert snap.operations.aborted == 1
        assert snap.schemas.validations_passed == 1

    def test_thread_safety_under_high_concurrency(self):
        collector = StatsCollector()

        def _worker(tid: int) -> None:
            for i in range(500):
                collector.record_operation_step(
                    protocol="gnmi",
                    target=f"10.0.0.{tid % 4}:57400",
                    op_type="get",
                    success=(i % 10 != 0),
                    duration_ms=1.5,
                    wire_rpc_ms=1.2,
                    request_bytes=10,
                    response_bytes=20,
                )

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(_worker, range(16)))

        snap = StatsService(collector).get_snapshot()
        assert snap.operations.total == 8000
        assert snap.operations.succeeded == 7200
        assert snap.operations.failed == 800
        assert snap.operations.request_bytes_total == 80000
        assert snap.operations.response_bytes_total == 160000

    def test_cardinality_cap_and_protocol_target_filtering(self):
        collector = StatsCollector(max_targets=2)
        collector.record_operation_step(
            protocol="gnmi", target="10.0.0.1:57400", op_type="get", success=True, duration_ms=5.0
        )
        collector.record_operation_step(
            protocol="netconf", target="10.0.0.2:830", op_type="get", success=True, duration_ms=8.0
        )
        collector.record_operation_step(
            protocol="gnmi", target="10.0.0.3:57400", op_type="get", success=True, duration_ms=11.0
        )

        svc = StatsService(collector)
        all_snap = svc.get_snapshot()
        assert all_snap.operations.total == 3
        assert "_overflow" in all_snap.operations.by_target

        gnmi_snap = svc.get_snapshot(protocol="gnmi")
        assert gnmi_snap.operations.total == 2
        assert gnmi_snap.operations.by_protocol == {"gnmi": 2}

        t1_snap = svc.get_snapshot(protocol="gnmi", target="10.0.0.1:57400")
        assert t1_snap.operations.total == 1
        assert t1_snap.operations.by_target == {"10.0.0.1:57400": 1}

    def test_reset_preserves_live_gauges_and_clears_counters(self):
        collector = StatsCollector()
        collector.record_session_opened("netconf", "10.0.0.1:830")
        collector.record_datastore_lock_acquired("10.0.0.1:830", "candidate")
        collector.record_subscription_started("sub-1", "gnmi", "periodic", "10.0.0.1:57400", owner_id="alice")
        collector.record_subscription_started("sub-2", "gnmi", "periodic", "10.0.0.2:57400", owner_id="bob")
        collector.record_subscription_ended("sub-2", "gnmi", "10.0.0.2:57400", "client_stopped")
        collector.record_operation_step(
            protocol="netconf", target="10.0.0.1:830", op_type="get", success=True, duration_ms=4.0
        )
        collector.websocket_connected("stats")

        svc = StatsService(collector)
        svc.reset()
        snap = svc.get_snapshot()

        assert snap.operations.total == 0
        assert snap.sessions.created_total == 0
        assert snap.sessions.active == 1
        assert snap.sessions.datastore_locks_active == 1
        assert snap.subscriptions_active == 1
        assert snap.runtime.websocket_clients_active == {"stats": 1}
        # Closed subscription history cleared on reset
        with pytest.raises(SubscriptionNotFoundError):
            svc.get_subscription_stats("sub-2", is_admin=True)

    def test_subscription_owner_isolation(self):
        collector = StatsCollector()
        collector.record_subscription_started("sub-a", "gnmi", "periodic", "10.0.0.1:57400", owner_id="alice")
        collector.record_subscription_started("sub-b", "gnmi", "periodic", "10.0.0.2:57400", owner_id="bob")
        svc = StatsService(collector)

        alice_snap = svc.get_snapshot(owner_id="alice", include_all_subscriptions=False)
        assert [s.subscription_id for s in alice_snap.subscriptions] == ["sub-a"]

        with pytest.raises(PermissionDeniedError):
            svc.get_subscription_stats("sub-b", owner_id="alice", is_admin=False)

        assert isinstance(
            svc.get_subscription_stats("sub-b", owner_id="alice", is_admin=True),
            SubscriptionStatsDTO,
        )

    def test_times_multiplier_aggregates_execution_stats(self):
        collector = StatsCollector()
        svc = DeviceOperationService(stats_recorder=collector)
        with patch.object(DeviceOperationService, "_resolve_worker") as mock_resolve:
            mock_worker = MagicMock()
            mock_worker.start.return_value = {
                "session_id": "s1",
                "target": "10.0.0.1:57400",
                "rpc": "capability",
                "data": {"ok": True},
                "protocol": "gnmi",
            }
            mock_resolve.return_value = mock_worker

            req = ExecutionRequestDTO(
                adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:57400", protocol="gnmi"),
                operations=[OperationStepDTO(op_type="capabilities")],
                times=3,
            )
            res = svc.execute_unary(req, request_id="batch-req-1")

        assert res.success is True
        assert res.stats is not None
        assert res.stats.request_id == "batch-req-1"
        assert res.stats.total_duration_ms >= 0.0
        snap = StatsService(collector).get_snapshot()
        assert snap.operations.execution_latency.count == 3

    def test_fanout_queue_depth_and_drop_metrics(self):
        collector = StatsCollector()
        collector.record_subscription_started("sub-q", "gnmi", "periodic", "10.0.0.1:57400")
        fq = FanoutQueue(maxsize=2, subscription_id="sub-q", stats_recorder=collector)
        cid = "c1"
        cq = fq.register_consumer(cid)

        fq.put({"data": 1})
        fq.put({"data": 2})
        fq.put({"data": 3})  # triggers DROP_OLDEST -> 1 drop

        snap = StatsService(collector).get_subscription_stats("sub-q", is_admin=True)
        assert snap.queue_depth_current == 2
        assert snap.queue_depth_peak == 2
        assert snap.events_dropped == {"queue_full": 1}

        fq.unregister_consumer(cid)
        assert cq.qsize() == 2
        snap_after = StatsService(collector).get_subscription_stats("sub-q", is_admin=True)
        assert snap_after.events_dropped["consumer_gone"] == 2

    def test_session_manager_lock_hold_and_lazy_eviction_stats(self):
        collector = StatsCollector()
        fake_client = MagicMock()
        fake_client.__enter__.return_value = fake_client
        reg = DeviceRegistryService()
        reg.register_device("dev1", AdhocEndpointDTO(target="10.0.0.5:830", protocol="netconf"))
        mgr = DeviceSessionManager(
            device_registry=reg,
            default_ttl_seconds=5.0,
            client_factory=lambda **kw: fake_client,
            stats_recorder=collector,
        )
        info = mgr.create_session(owner_id="alice", device_id="dev1")
        with mgr.acquire_session(info.session_id, owner_id="alice") as lease:
            lease.record_step_outcome(OperationStepDTO(op_type="lock", datastore="candidate"), True)

        snap_mid = StatsService(collector).get_snapshot()
        assert snap_mid.sessions.active == 1
        assert snap_mid.sessions.datastore_locks_active == 1

        # Trigger lazy idle expiry
        mgr.sweep_expired_sessions(now=info.expires_at + 10.0)
        snap_end = StatsService(collector).get_snapshot()
        assert snap_end.sessions.active == 0
        assert snap_end.sessions.datastore_locks_active == 0
        assert snap_end.sessions.idle_evictions_total == 1
        assert snap_end.sessions.orphaned_lock_reclaims_total == 1
        assert snap_end.sessions.datastore_lock_hold_ms.count == 1
        assert snap_end.operations.netconf_rollbacks.get("forced_unlock") == 1


class TestLayerIsolationBoundaries:
    @staticmethod
    def _imports_in_dir(rel_dir: str) -> set[str]:
        root = Path(__file__).resolve().parents[2] / rel_dir
        imported: set[str] = set()
        for py_file in root.rglob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
        return imported

    def test_presentation_and_lower_layer_import_boundaries(self):
        for upper in ("api", "cli"):
            imports = self._imports_in_dir(upper)
            assert not (imports & {"managers", "specs", "core"}), (
                f"{upper}/ must not import managers/, specs/, or core/: found {imports}"
            )

        for lower in ("managers", "specs", "core", "modules", "config"):
            imports = self._imports_in_dir(lower)
            assert not (imports & {"application", "fastapi", "pydantic", "cli", "api"}), (
                f"{lower}/ must not import upper layers or web frameworks: found {imports}"
            )
