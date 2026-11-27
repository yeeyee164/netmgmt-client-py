# -*- coding: utf-8 -*-
"""
Application Statistics Collector, Scoped Execution Tracker & Query Service.

Provides thread-safe partitioned metric collection (`StatsCollector`), lock-free
per-execution scoped recording (`ExecutionStatsScope`), and read/filter/reset
access (`StatsService`) across all five observability domains.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
import logging
import math
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from application.dto import (
    ExecutionStatsDTO,
    ExecutionStepStatsDTO,
    LatencySummaryDTO,
    OperationStatsSummaryDTO,
    RuntimeHealthStatsDTO,
    SchemaStatsSummaryDTO,
    SessionStatsSummaryDTO,
    StatsSnapshotDTO,
    SubscriptionStatsDTO,
)
from application.exceptions import PermissionDeniedError, SubscriptionNotFoundError

logger = logging.getLogger(__name__)

_OVERFLOW_KEY = "_overflow"


def _percentile_nearest_rank(sorted_vals: List[float], q: float) -> float:
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    idx = max(0, min(n - 1, math.ceil(q * n) - 1))
    return sorted_vals[idx]


class _LatencyBuffer:
    """Bounded ring buffer storing recent latency observations in O(1) per write."""

    def __init__(self, maxlen: int = 1000):
        self.maxlen = max(1, int(maxlen))
        self.samples: deque[float] = deque(maxlen=self.maxlen)
        self.count: int = 0
        self.total_sum: float = 0.0
        self.min_val: Optional[float] = None
        self.max_val: Optional[float] = None

    def observe(self, value_ms: float) -> None:
        val = max(0.0, float(value_ms))
        self.samples.append(val)
        self.count += 1
        self.total_sum += val
        if self.min_val is None or val < self.min_val:
            self.min_val = val
        if self.max_val is None or val > self.max_val:
            self.max_val = val

    def clear(self) -> None:
        self.samples.clear()
        self.count = 0
        self.total_sum = 0.0
        self.min_val = None
        self.max_val = None

    def summary(self) -> LatencySummaryDTO:
        return summarize_latency_buffers((self,))


def summarize_latency_buffers(buffers: Iterable[_LatencyBuffer]) -> LatencySummaryDTO:
    """Computes a LatencySummaryDTO across one or more _LatencyBuffer instances."""
    total_count = 0
    total_sum = 0.0
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    merged_samples: List[float] = []

    for buf in buffers:
        if buf.count == 0:
            continue
        total_count += buf.count
        total_sum += buf.total_sum
        if buf.min_val is not None and (min_val is None or buf.min_val < min_val):
            min_val = buf.min_val
        if buf.max_val is not None and (max_val is None or buf.max_val > max_val):
            max_val = buf.max_val
        merged_samples.extend(buf.samples)

    if total_count == 0:
        return LatencySummaryDTO()

    sorted_vals = sorted(merged_samples)
    return LatencySummaryDTO(
        count=total_count,
        min_ms=min_val if min_val is not None else 0.0,
        avg_ms=total_sum / total_count,
        p50_ms=_percentile_nearest_rank(sorted_vals, 0.50),
        p95_ms=_percentile_nearest_rank(sorted_vals, 0.95),
        p99_ms=_percentile_nearest_rank(sorted_vals, 0.99),
        max_ms=max_val if max_val is not None else 0.0,
    )


class _SlidingRateCounter:
    """Fixed-size ring of 1-second buckets for O(1) sliding-window event rate."""

    def __init__(self, window_seconds: int = 10, clock: Callable[[], float] = time.perf_counter):
        self._window = max(1, int(window_seconds))
        self._clock = clock
        self._buckets: List[int] = [0] * self._window
        self._bucket_ts: List[int] = [-1] * self._window

    def increment(self, count: int = 1) -> None:
        sec = math.floor(self._clock())
        idx = sec % self._window
        if self._bucket_ts[idx] != sec:
            self._bucket_ts[idx] = sec
            self._buckets[idx] = 0
        self._buckets[idx] += count

    def rate_eps(self, start_mono: float) -> float:
        now = self._clock()
        sec = math.floor(now)
        cutoff = sec - self._window
        total = 0
        for idx in range(self._window):
            ts = self._bucket_ts[idx]
            if ts > cutoff and ts <= sec:
                total += self._buckets[idx]
        uptime = max(0.0, now - start_mono)
        denom = min(max(uptime, 1.0), float(self._window))
        return total / denom


@dataclass
class _TargetOpStats:
    """Per-(protocol, target) operation counters and latency buffers."""

    max_latency_samples: int = 1000
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    aborted: int = 0
    by_op_type: Dict[str, int] = field(default_factory=dict)
    errors_by_category: Dict[str, int] = field(default_factory=dict)
    netconf_rollbacks: Dict[str, int] = field(default_factory=dict)
    request_bytes_total: int = 0
    response_bytes_total: int = 0
    execution_latency: _LatencyBuffer = field(init=False)
    connect_latency: _LatencyBuffer = field(init=False)
    wire_rpc_latency: _LatencyBuffer = field(init=False)

    def __post_init__(self) -> None:
        self.execution_latency = _LatencyBuffer(self.max_latency_samples)
        self.connect_latency = _LatencyBuffer(self.max_latency_samples)
        self.wire_rpc_latency = _LatencyBuffer(self.max_latency_samples)


class _SubscriptionRecord:
    """Per-subscription isolated stats protected by its own fine-grained lock."""

    def __init__(
        self,
        *,
        subscription_id: str,
        protocol: str,
        delivery_mode: str,
        target: str,
        owner_id: Optional[str],
        start_mono: float,
        rate_window_seconds: int,
        clock: Callable[[], float],
    ):
        self.lock = threading.Lock()
        self.subscription_id = subscription_id
        self.protocol = protocol
        self.delivery_mode = delivery_mode
        self.target = target
        self.owner_id = owner_id
        self.start_mono = start_mono
        self.active = True
        self.events_by_kind: Dict[str, int] = {}
        self.events_total: int = 0
        self.rate_counter = _SlidingRateCounter(window_seconds=rate_window_seconds, clock=clock)
        self.time_to_first_event_ms: Optional[float] = None
        self.time_to_sync_ms: Optional[float] = None
        self.queue_depth_current: int = 0
        self.queue_depth_peak: int = 0
        self.events_dropped: Dict[str, int] = {}
        self.poll_triggers_total: int = 0
        self.disconnect_reason: Optional[str] = None

    def to_dto(self, now_mono: float) -> SubscriptionStatsDTO:
        with self.lock:
            uptime = max(0.0, now_mono - self.start_mono)
            return SubscriptionStatsDTO(
                subscription_id=self.subscription_id,
                protocol=self.protocol,
                delivery_mode=self.delivery_mode,
                target=self.target,
                owner_id=self.owner_id,
                active=self.active,
                uptime_seconds=uptime,
                events_by_kind=dict(self.events_by_kind),
                events_total=self.events_total,
                event_rate_eps=self.rate_counter.rate_eps(self.start_mono),
                time_to_first_event_ms=self.time_to_first_event_ms,
                time_to_sync_ms=self.time_to_sync_ms,
                queue_depth_current=self.queue_depth_current,
                queue_depth_peak=self.queue_depth_peak,
                events_dropped=dict(self.events_dropped),
                poll_triggers_total=self.poll_triggers_total,
                disconnect_reason=self.disconnect_reason,
            )


def _cap_target_key(
    store: Dict[Tuple[str, str], _TargetOpStats],
    protocol: str,
    target: str,
    max_targets: int,
) -> Tuple[str, str]:
    proto = (protocol or "unknown").strip().lower()
    tgt = (target or "unknown").strip() or "unknown"
    key = (proto, tgt)
    if key in store:
        return key
    if len(store) < max_targets:
        return key
    return (proto, _OVERFLOW_KEY)


class StatsCollector:
    """
    Thread-safe, partitioned runtime metrics collector implementing `StatsRecorder`.

    Uses domain-partitioned locks and bounded ring buffers / LRU caps to keep
    hot-path writes O(1) and memory strictly bounded over long-running processes.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.perf_counter,
        wall_clock: Callable[[], float] = time.time,
        max_latency_samples: int = 1000,
        max_targets: int = 500,
        max_closed_subscriptions: int = 200,
        rate_window_seconds: int = 10,
    ):
        self._clock = clock
        self._wall_clock = wall_clock
        self._max_latency_samples = max(1, int(max_latency_samples))
        self._max_targets = max(1, int(max_targets))
        self._max_closed_subscriptions = max(1, int(max_closed_subscriptions))
        self._rate_window_seconds = max(1, int(rate_window_seconds))
        self._started_mono = self._clock()

        # Domain A: RPC & pipeline operations
        self._ops_lock = threading.Lock()
        self._op_buckets: Dict[Tuple[str, str], _TargetOpStats] = {}

        # Domain B: Streaming subscriptions
        self._sub_lock = threading.Lock()
        self._active_subs: Dict[str, _SubscriptionRecord] = {}
        self._closed_subs: OrderedDict[str, SubscriptionStatsDTO] = OrderedDict()

        # Domain C: Stateful sessions & datastore locks
        self._session_lock = threading.Lock()
        self._sessions_active_by_protocol: Dict[str, int] = {}
        self._sessions_created_total: int = 0
        self._session_ops_total: int = 0
        self._sessions_closed_by_reason: Dict[str, int] = {}
        self._datastore_locks_active: int = 0
        self._datastore_lock_hold_ms = _LatencyBuffer(self._max_latency_samples)
        self._idle_evictions_total: int = 0
        self._orphaned_lock_reclaims_total: int = 0
        self._reconnects_total: int = 0

        # Domain D: YANG schema engine
        self._schema_lock = threading.Lock()
        self._schema_validations_passed: int = 0
        self._schema_validations_failed: int = 0
        self._schema_validation_latency = _LatencyBuffer(self._max_latency_samples)
        self._schema_cache_hits: Dict[str, int] = {}
        self._schema_cache_misses: Dict[str, int] = {}
        self._schema_profiles_loaded: int = 0
        self._schema_compile_latency = _LatencyBuffer(self._max_latency_samples)

        # Domain E: Backend concurrency & runtime health
        self._runtime_lock = threading.Lock()
        self._worker_threads_active: Dict[str, int] = {}
        self._worker_semaphore_wait = _LatencyBuffer(self._max_latency_samples)
        self._websocket_clients_active: Dict[str, int] = {}

        # Authoritative live gauge providers (prevent drift across errors/resets)
        self._gauge_lock = threading.Lock()
        self._gauge_providers: Dict[str, Callable[[], Any]] = {}

    def register_gauge_provider(self, name: str, provider: Callable[[], Any]) -> None:
        """Registers a read-only callback returning live gauge values on snapshot."""
        try:
            with self._gauge_lock:
                self._gauge_providers[name] = provider
        except Exception as exc:
            logger.debug("[StatsCollector] Failed to register gauge provider '%s': %s", name, exc)

    def _read_gauge(self, name: str) -> Optional[Any]:
        with self._gauge_lock:
            provider = self._gauge_providers.get(name)
        if provider is None:
            return None
        try:
            return provider()
        except Exception as exc:
            logger.debug("[StatsCollector] Gauge provider '%s' failed: %s", name, exc)
            return None

    def _get_op_bucket_locked(self, protocol: str, target: str) -> _TargetOpStats:
        key = _cap_target_key(self._op_buckets, protocol, target, self._max_targets)
        bucket = self._op_buckets.get(key)
        if bucket is None:
            bucket = _TargetOpStats(max_latency_samples=self._max_latency_samples)
            self._op_buckets[key] = bucket
        return bucket

    # ------------------------------------------------------------------
    # Domain A: RPC & Pipeline Execution
    # ------------------------------------------------------------------

    def record_connect(
        self, protocol: str, target: str, duration_ms: float, reused: bool = False
    ) -> None:
        try:
            if reused:
                return
            with self._ops_lock:
                bucket = self._get_op_bucket_locked(protocol, target)
                bucket.connect_latency.observe(duration_ms)
        except Exception as exc:
            logger.debug("[StatsCollector] record_connect error: %s", exc)

    def record_execution_duration(self, protocol: str, target: str, duration_ms: float) -> None:
        """Records total wall-clock execution duration for one request/pipeline."""
        try:
            with self._ops_lock:
                bucket = self._get_op_bucket_locked(protocol, target)
                bucket.execution_latency.observe(duration_ms)
        except Exception as exc:
            logger.debug("[StatsCollector] record_execution_duration error: %s", exc)

    def record_operation_step(
        self,
        *,
        protocol: str,
        target: str,
        op_type: str,
        success: bool,
        duration_ms: float,
        wire_rpc_ms: Optional[float] = None,
        request_bytes: int = 0,
        response_bytes: int = 0,
        path_count: int = 0,
        error_category: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> None:
        try:
            norm_op = (op_type or "unknown").strip().lower()
            with self._ops_lock:
                bucket = self._get_op_bucket_locked(protocol, target)
                bucket.total += 1
                if success:
                    bucket.succeeded += 1
                else:
                    bucket.failed += 1
                bucket.by_op_type[norm_op] = bucket.by_op_type.get(norm_op, 0) + 1
                bucket.request_bytes_total += max(0, int(request_bytes))
                bucket.response_bytes_total += max(0, int(response_bytes))
                if wire_rpc_ms is not None:
                    bucket.wire_rpc_latency.observe(wire_rpc_ms)
                if not success:
                    cat = error_category or error_code or "rpc_error"
                    bucket.errors_by_category[cat] = bucket.errors_by_category.get(cat, 0) + 1
        except Exception as exc:
            logger.debug("[StatsCollector] record_operation_step error: %s", exc)

    def record_operation_aborted(self, protocol: str, target: str, op_type: str) -> None:
        try:
            with self._ops_lock:
                bucket = self._get_op_bucket_locked(protocol, target)
                bucket.aborted += 1
        except Exception as exc:
            logger.debug("[StatsCollector] record_operation_aborted error: %s", exc)

    def record_netconf_rollback(self, target: str, action: str) -> None:
        try:
            act = (action or "unknown").strip().lower()
            with self._ops_lock:
                bucket = self._get_op_bucket_locked("netconf", target)
                bucket.netconf_rollbacks[act] = bucket.netconf_rollbacks.get(act, 0) + 1
        except Exception as exc:
            logger.debug("[StatsCollector] record_netconf_rollback error: %s", exc)

    # ------------------------------------------------------------------
    # Domain B: Streaming Telemetry & Subscriptions
    # ------------------------------------------------------------------

    def record_subscription_started(
        self,
        subscription_id: str,
        protocol: str,
        delivery_mode: str,
        target: str,
        owner_id: Optional[str] = None,
    ) -> None:
        try:
            now_mono = self._clock()
            rec = _SubscriptionRecord(
                subscription_id=subscription_id,
                protocol=(protocol or "unknown").strip().lower(),
                delivery_mode=(delivery_mode or "periodic").strip().lower(),
                target=(target or "unknown").strip() or "unknown",
                owner_id=owner_id,
                start_mono=now_mono,
                rate_window_seconds=self._rate_window_seconds,
                clock=self._clock,
            )
            with self._sub_lock:
                self._active_subs[subscription_id] = rec
        except Exception as exc:
            logger.debug("[StatsCollector] record_subscription_started error: %s", exc)

    def record_subscription_event(
        self,
        subscription_id: str,
        protocol: str,
        target: str,
        event_kind: str,
        payload_bytes: int = 0,
    ) -> None:
        try:
            with self._sub_lock:
                rec = self._active_subs.get(subscription_id)
            if rec is None:
                return
            now_mono = self._clock()
            kind = (event_kind or "update").strip().lower()
            with rec.lock:
                rec.events_total += 1
                rec.events_by_kind[kind] = rec.events_by_kind.get(kind, 0) + 1
                if rec.time_to_first_event_ms is None:
                    rec.time_to_first_event_ms = max(0.0, (now_mono - rec.start_mono) * 1000.0)
                rec.rate_counter.increment()
        except Exception as exc:
            logger.debug("[StatsCollector] record_subscription_event error: %s", exc)

    def record_subscription_sync(self, subscription_id: str, elapsed_ms: float) -> None:
        try:
            with self._sub_lock:
                rec = self._active_subs.get(subscription_id)
            if rec is None:
                return
            with rec.lock:
                if rec.time_to_sync_ms is None:
                    rec.time_to_sync_ms = max(0.0, float(elapsed_ms))
        except Exception as exc:
            logger.debug("[StatsCollector] record_subscription_sync error: %s", exc)

    def record_subscription_poll(self, subscription_id: str) -> None:
        try:
            with self._sub_lock:
                rec = self._active_subs.get(subscription_id)
            if rec is None:
                return
            with rec.lock:
                rec.poll_triggers_total += 1
        except Exception as exc:
            logger.debug("[StatsCollector] record_subscription_poll error: %s", exc)

    def record_subscription_queue_depth(self, subscription_id: str, depth: int) -> None:
        try:
            with self._sub_lock:
                rec = self._active_subs.get(subscription_id)
            if rec is None:
                return
            d = max(0, int(depth))
            with rec.lock:
                rec.queue_depth_current = d
                if d > rec.queue_depth_peak:
                    rec.queue_depth_peak = d
        except Exception as exc:
            logger.debug("[StatsCollector] record_subscription_queue_depth error: %s", exc)

    def record_subscription_drop(self, subscription_id: str, reason: str) -> None:
        try:
            with self._sub_lock:
                rec = self._active_subs.get(subscription_id)
            if rec is None:
                return
            rsn = (reason or "queue_full").strip().lower()
            with rec.lock:
                rec.events_dropped[rsn] = rec.events_dropped.get(rsn, 0) + 1
        except Exception as exc:
            logger.debug("[StatsCollector] record_subscription_drop error: %s", exc)

    def record_subscription_ended(
        self, subscription_id: str, protocol: str, target: str, reason: str
    ) -> None:
        try:
            now_mono = self._clock()
            with self._sub_lock:
                rec = self._active_subs.pop(subscription_id, None)
                if rec is None:
                    return
                with rec.lock:
                    rec.active = False
                    rec.disconnect_reason = (reason or "client_stopped").strip().lower()
                    rec.queue_depth_current = 0
                dto = rec.to_dto(now_mono)
                self._closed_subs[subscription_id] = dto
                self._closed_subs.move_to_end(subscription_id)
                while len(self._closed_subs) > self._max_closed_subscriptions:
                    self._closed_subs.popitem(last=False)
        except Exception as exc:
            logger.debug("[StatsCollector] record_subscription_ended error: %s", exc)

    # ------------------------------------------------------------------
    # Domain C: Stateful Sessions & Connection Lifecycle
    # ------------------------------------------------------------------

    def record_session_opened(self, protocol: str, target: str) -> None:
        try:
            proto = (protocol or "unknown").strip().lower()
            with self._session_lock:
                self._sessions_created_total += 1
                self._sessions_active_by_protocol[proto] = (
                    self._sessions_active_by_protocol.get(proto, 0) + 1
                )
        except Exception as exc:
            logger.debug("[StatsCollector] record_session_opened error: %s", exc)

    def record_session_closed(self, protocol: str, target: str, reason: str) -> None:
        try:
            proto = (protocol or "unknown").strip().lower()
            rsn = (reason or "explicit_close").strip().lower()
            with self._session_lock:
                cur = self._sessions_active_by_protocol.get(proto, 0)
                self._sessions_active_by_protocol[proto] = max(0, cur - 1)
                self._sessions_closed_by_reason[rsn] = (
                    self._sessions_closed_by_reason.get(rsn, 0) + 1
                )
        except Exception as exc:
            logger.debug("[StatsCollector] record_session_closed error: %s", exc)

    def record_session_operation(self, protocol: str, target: str) -> None:
        try:
            with self._session_lock:
                self._session_ops_total += 1
        except Exception as exc:
            logger.debug("[StatsCollector] record_session_operation error: %s", exc)

    def record_session_reconnect(self, protocol: str, target: str) -> None:
        try:
            with self._session_lock:
                self._reconnects_total += 1
        except Exception as exc:
            logger.debug("[StatsCollector] record_session_reconnect error: %s", exc)

    def record_session_idle_eviction(self, protocol: str, target: str) -> None:
        try:
            with self._session_lock:
                self._idle_evictions_total += 1
        except Exception as exc:
            logger.debug("[StatsCollector] record_session_idle_eviction error: %s", exc)

    def record_datastore_lock_acquired(self, target: str, datastore: str) -> None:
        try:
            with self._session_lock:
                self._datastore_locks_active += 1
        except Exception as exc:
            logger.debug("[StatsCollector] record_datastore_lock_acquired error: %s", exc)

    def record_datastore_lock_released(
        self, target: str, datastore: str, hold_ms: float, reclaimed: bool = False
    ) -> None:
        try:
            with self._session_lock:
                self._datastore_locks_active = max(0, self._datastore_locks_active - 1)
                self._datastore_lock_hold_ms.observe(hold_ms)
                if reclaimed:
                    self._orphaned_lock_reclaims_total += 1
        except Exception as exc:
            logger.debug("[StatsCollector] record_datastore_lock_released error: %s", exc)

    # ------------------------------------------------------------------
    # Domain D: YANG Schema Engine & Pre-Flight Validation
    # ------------------------------------------------------------------

    def record_schema_validation(
        self, protocol: str, passed: bool, duration_ms: float
    ) -> None:
        try:
            with self._schema_lock:
                if passed:
                    self._schema_validations_passed += 1
                else:
                    self._schema_validations_failed += 1
                self._schema_validation_latency.observe(duration_ms)
        except Exception as exc:
            logger.debug("[StatsCollector] record_schema_validation error: %s", exc)

    def record_schema_cache_access(self, hit: bool, layer_or_source: str) -> None:
        try:
            key = (layer_or_source or "unknown").strip().lower()
            with self._schema_lock:
                if hit:
                    self._schema_cache_hits[key] = self._schema_cache_hits.get(key, 0) + 1
                else:
                    self._schema_cache_misses[key] = self._schema_cache_misses.get(key, 0) + 1
        except Exception as exc:
            logger.debug("[StatsCollector] record_schema_cache_access error: %s", exc)

    def record_schema_context_compiled(
        self, profile_id: str, duration_ms: float, loaded_profiles_count: int
    ) -> None:
        try:
            with self._schema_lock:
                self._schema_compile_latency.observe(duration_ms)
                self._schema_profiles_loaded = max(0, int(loaded_profiles_count))
        except Exception as exc:
            logger.debug("[StatsCollector] record_schema_context_compiled error: %s", exc)

    # ------------------------------------------------------------------
    # Domain E: Backend Concurrency & Runtime Health
    # ------------------------------------------------------------------

    def record_worker_semaphore_wait(self, worker_type: str, wait_ms: float) -> None:
        try:
            with self._runtime_lock:
                self._worker_semaphore_wait.observe(wait_ms)
        except Exception as exc:
            logger.debug("[StatsCollector] record_worker_semaphore_wait error: %s", exc)

    def worker_thread_enter(self, worker_type: str) -> None:
        try:
            wt = (worker_type or "unary").strip().lower()
            with self._runtime_lock:
                self._worker_threads_active[wt] = self._worker_threads_active.get(wt, 0) + 1
        except Exception as exc:
            logger.debug("[StatsCollector] worker_thread_enter error: %s", exc)

    def worker_thread_exit(self, worker_type: str) -> None:
        try:
            wt = (worker_type or "unary").strip().lower()
            with self._runtime_lock:
                cur = self._worker_threads_active.get(wt, 0)
                if cur <= 1:
                    self._worker_threads_active.pop(wt, None)
                else:
                    self._worker_threads_active[wt] = cur - 1
        except Exception as exc:
            logger.debug("[StatsCollector] worker_thread_exit error: %s", exc)

    def websocket_connected(self, channel: str) -> None:
        try:
            ch = (channel or "subscriptions").strip().lower()
            with self._runtime_lock:
                self._websocket_clients_active[ch] = (
                    self._websocket_clients_active.get(ch, 0) + 1
                )
        except Exception as exc:
            logger.debug("[StatsCollector] websocket_connected error: %s", exc)

    def websocket_disconnected(self, channel: str) -> None:
        try:
            ch = (channel or "subscriptions").strip().lower()
            with self._runtime_lock:
                cur = self._websocket_clients_active.get(ch, 0)
                if cur <= 1:
                    self._websocket_clients_active.pop(ch, None)
                else:
                    self._websocket_clients_active[ch] = cur - 1
        except Exception as exc:
            logger.debug("[StatsCollector] websocket_disconnected error: %s", exc)

    # ------------------------------------------------------------------
    # Reset & Snapshot Queries
    # ------------------------------------------------------------------

    def reset(self) -> float:
        """
        Resets cumulative counters, latency buffers, and closed subscription history
        while preserving live active gauges.
        """
        with self._ops_lock:
            self._op_buckets.clear()
        with self._sub_lock:
            self._closed_subs.clear()
        with self._session_lock:
            self._sessions_created_total = 0
            self._session_ops_total = 0
            self._sessions_closed_by_reason.clear()
            self._datastore_lock_hold_ms.clear()
            self._idle_evictions_total = 0
            self._orphaned_lock_reclaims_total = 0
            self._reconnects_total = 0
        with self._schema_lock:
            self._schema_validations_passed = 0
            self._schema_validations_failed = 0
            self._schema_validation_latency.clear()
            self._schema_cache_hits.clear()
            self._schema_cache_misses.clear()
            self._schema_compile_latency.clear()
        with self._runtime_lock:
            self._worker_semaphore_wait.clear()
        return float(self._wall_clock())

    def snapshot(
        self,
        *,
        protocol: Optional[str] = None,
        target: Optional[str] = None,
        owner_id: Optional[str] = None,
        include_all_subscriptions: bool = True,
    ) -> StatsSnapshotDTO:
        """Builds a read-only StatsSnapshotDTO across all five domains."""
        norm_proto = protocol.strip().lower() if protocol else None
        norm_target = target.strip() if target else None
        now_mono = self._clock()
        now_wall = float(self._wall_clock())

        # Domain A
        with self._ops_lock:
            matching: List[Tuple[Tuple[str, str], _TargetOpStats]] = [
                (k, v)
                for k, v in self._op_buckets.items()
                if (norm_proto is None or k[0] == norm_proto)
                and (norm_target is None or k[1] == norm_target)
            ]
            op_total = 0
            op_succeeded = 0
            op_failed = 0
            op_aborted = 0
            by_protocol: Dict[str, int] = {}
            by_target: Dict[str, int] = {}
            by_op_type: Dict[str, int] = {}
            errors_by_category: Dict[str, int] = {}
            netconf_rollbacks: Dict[str, int] = {}
            req_bytes = 0
            resp_bytes = 0
            exec_bufs: List[_LatencyBuffer] = []
            conn_bufs: List[_LatencyBuffer] = []
            wire_bufs: List[_LatencyBuffer] = []

            for (proto_k, tgt_k), b in matching:
                op_total += b.total
                op_succeeded += b.succeeded
                op_failed += b.failed
                op_aborted += b.aborted
                if b.total > 0:
                    by_protocol[proto_k] = by_protocol.get(proto_k, 0) + b.total
                    by_target[tgt_k] = by_target.get(tgt_k, 0) + b.total
                for op_k, cnt in b.by_op_type.items():
                    by_op_type[op_k] = by_op_type.get(op_k, 0) + cnt
                for cat_k, cnt in b.errors_by_category.items():
                    errors_by_category[cat_k] = errors_by_category.get(cat_k, 0) + cnt
                for rb_k, cnt in b.netconf_rollbacks.items():
                    netconf_rollbacks[rb_k] = netconf_rollbacks.get(rb_k, 0) + cnt
                req_bytes += b.request_bytes_total
                resp_bytes += b.response_bytes_total
                exec_bufs.append(b.execution_latency)
                conn_bufs.append(b.connect_latency)
                wire_bufs.append(b.wire_rpc_latency)

            ops_summary = OperationStatsSummaryDTO(
                total=op_total,
                succeeded=op_succeeded,
                failed=op_failed,
                aborted=op_aborted,
                by_protocol=by_protocol,
                by_target=by_target,
                by_op_type=by_op_type,
                errors_by_category=errors_by_category,
                netconf_rollbacks=netconf_rollbacks,
                request_bytes_total=req_bytes,
                response_bytes_total=resp_bytes,
                execution_latency=summarize_latency_buffers(exec_bufs),
                connect_latency=summarize_latency_buffers(conn_bufs),
                wire_rpc_latency=summarize_latency_buffers(wire_bufs),
            )

        # Domain B
        with self._sub_lock:
            active_records = list(self._active_subs.values())
            closed_dtos = list(self._closed_subs.values())

        sub_dtos: List[SubscriptionStatsDTO] = []
        active_sub_count = 0
        for rec in active_records:
            dto = rec.to_dto(now_mono)
            if (norm_proto is None or dto.protocol == norm_proto) and (
                norm_target is None or dto.target == norm_target
            ):
                if include_all_subscriptions or owner_id is None or dto.owner_id == owner_id:
                    active_sub_count += 1
                    sub_dtos.append(dto)
        for dto in closed_dtos:
            if (norm_proto is None or dto.protocol == norm_proto) and (
                norm_target is None or dto.target == norm_target
            ):
                if include_all_subscriptions or owner_id is None or dto.owner_id == owner_id:
                    sub_dtos.append(dto)

        live_subs_gauge = self._read_gauge("subscriptions_active")
        if (
            live_subs_gauge is not None
            and include_all_subscriptions
            and norm_proto is None
            and norm_target is None
        ):
            subscriptions_active = int(live_subs_gauge)
        else:
            subscriptions_active = active_sub_count

        # Domain C
        with self._session_lock:
            active_by_proto = {
                k: v for k, v in self._sessions_active_by_protocol.items() if v > 0
            }
            created_total = self._sessions_created_total
            ops_avg = (
                self._session_ops_total / created_total if created_total > 0 else 0.0
            )
            locks_active = self._datastore_locks_active
            sess_summary = SessionStatsSummaryDTO(
                active=sum(active_by_proto.values()),
                active_by_protocol=active_by_proto,
                created_total=created_total,
                closed_by_reason=dict(self._sessions_closed_by_reason),
                operations_per_session_avg=ops_avg,
                datastore_locks_active=locks_active,
                datastore_lock_hold_ms=self._datastore_lock_hold_ms.summary(),
                idle_evictions_total=self._idle_evictions_total,
                orphaned_lock_reclaims_total=self._orphaned_lock_reclaims_total,
                reconnects_total=self._reconnects_total,
            )

        live_sess_by_proto = self._read_gauge("sessions_active")
        live_locks = self._read_gauge("datastore_locks_active")
        if live_sess_by_proto is not None or live_locks is not None:
            merged_by_proto = (
                dict(live_sess_by_proto)
                if isinstance(live_sess_by_proto, dict)
                else sess_summary.active_by_protocol
            )
            sess_summary = SessionStatsSummaryDTO(
                active=(
                    sum(merged_by_proto.values())
                    if isinstance(live_sess_by_proto, dict)
                    else (int(live_sess_by_proto) if live_sess_by_proto is not None else sess_summary.active)
                ),
                active_by_protocol=merged_by_proto,
                created_total=sess_summary.created_total,
                closed_by_reason=sess_summary.closed_by_reason,
                operations_per_session_avg=sess_summary.operations_per_session_avg,
                datastore_locks_active=(
                    int(live_locks) if live_locks is not None else sess_summary.datastore_locks_active
                ),
                datastore_lock_hold_ms=sess_summary.datastore_lock_hold_ms,
                idle_evictions_total=sess_summary.idle_evictions_total,
                orphaned_lock_reclaims_total=sess_summary.orphaned_lock_reclaims_total,
                reconnects_total=sess_summary.reconnects_total,
            )

        # Domain D
        with self._schema_lock:
            total_hits = sum(self._schema_cache_hits.values())
            total_misses = sum(self._schema_cache_misses.values())
            denom = total_hits + total_misses
            hit_ratio = (total_hits / denom) if denom > 0 else 0.0
            profiles_loaded = self._schema_profiles_loaded
            live_profiles = self._read_gauge("schema_profiles_loaded")
            if live_profiles is not None:
                profiles_loaded = int(live_profiles)
            schema_summary = SchemaStatsSummaryDTO(
                validations_passed=self._schema_validations_passed,
                validations_failed=self._schema_validations_failed,
                validation_latency=self._schema_validation_latency.summary(),
                cache_hits=dict(self._schema_cache_hits),
                cache_misses=dict(self._schema_cache_misses),
                cache_hit_ratio=hit_ratio,
                profiles_loaded=profiles_loaded,
                compile_latency=self._schema_compile_latency.summary(),
            )

        # Domain E
        with self._runtime_lock:
            runtime_summary = RuntimeHealthStatsDTO(
                process_uptime_seconds=max(0.0, now_mono - self._started_mono),
                worker_threads_active=dict(self._worker_threads_active),
                worker_semaphore_wait=self._worker_semaphore_wait.summary(),
                websocket_clients_active=dict(self._websocket_clients_active),
            )

        return StatsSnapshotDTO(
            generated_at_epoch=now_wall,
            operations=ops_summary,
            subscriptions_active=subscriptions_active,
            subscriptions=sub_dtos,
            sessions=sess_summary,
            schemas=schema_summary,
            runtime=runtime_summary,
        )

    def get_subscription_stats(
        self,
        subscription_id: str,
        *,
        owner_id: Optional[str] = None,
        is_admin: bool = False,
    ) -> SubscriptionStatsDTO:
        """Returns stats for one active or recently closed subscription with ownership check."""
        now_mono = self._clock()
        with self._sub_lock:
            rec = self._active_subs.get(subscription_id)
            closed_dto = self._closed_subs.get(subscription_id) if rec is None else None

        dto = rec.to_dto(now_mono) if rec is not None else closed_dto
        if dto is None:
            raise SubscriptionNotFoundError(
                f"Subscription statistics for '{subscription_id}' not found."
            )
        if not is_admin and owner_id is not None and dto.owner_id is not None and dto.owner_id != owner_id:
            raise PermissionDeniedError(
                f"Caller '{owner_id}' is not authorized to view stats for subscription '{subscription_id}'."
            )
        return dto


class ExecutionStatsScope:
    """
    Per-execution StatsRecorder scope.

    Accumulates per-run step timings, payload sizes, validation status, and rollbacks
    for attachment to `ExecutionResultDTO.stats`, while forwarding all observations
    to the parent `StatsCollector` for runtime aggregates.
    """

    def __init__(
        self,
        *,
        parent: Optional[StatsCollector],
        protocol: str,
        target: str,
        request_id: Optional[str] = None,
        connection_reused: bool = False,
        clock: Callable[[], float] = time.perf_counter,
    ):
        self._parent = parent
        self._protocol = (protocol or "unknown").strip().lower()
        self._target = (target or "unknown").strip() or "unknown"
        self._request_id = request_id
        self._connection_reused = connection_reused
        self._clock = clock
        self._start_mono = self._clock()

        self._connect_duration_ms: float = 0.0
        self._validation_duration_ms: float = 0.0
        self._validation_passed: Optional[bool] = None
        self._operations_total: int = 0
        self._operations_succeeded: int = 0
        self._operations_failed: int = 0
        self._operations_aborted: int = 0
        self._request_payload_bytes: int = 0
        self._response_payload_bytes: int = 0
        self._rollbacks: Dict[str, int] = {}
        self._steps: List[ExecutionStepStatsDTO] = []

    def record_connect(
        self, protocol: str, target: str, duration_ms: float, reused: bool = False
    ) -> None:
        if reused:
            self._connection_reused = True
        else:
            self._connect_duration_ms += max(0.0, float(duration_ms))
        if self._parent is not None:
            self._parent.record_connect(protocol, target, duration_ms, reused=reused)

    def record_operation_step(
        self,
        *,
        protocol: str,
        target: str,
        op_type: str,
        success: bool,
        duration_ms: float,
        wire_rpc_ms: Optional[float] = None,
        request_bytes: int = 0,
        response_bytes: int = 0,
        path_count: int = 0,
        error_category: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> None:
        step_idx = len(self._steps)
        self._operations_total += 1
        if success:
            self._operations_succeeded += 1
        else:
            self._operations_failed += 1
        req_b = max(0, int(request_bytes))
        resp_b = max(0, int(response_bytes))
        self._request_payload_bytes += req_b
        self._response_payload_bytes += resp_b
        self._steps.append(
            ExecutionStepStatsDTO(
                step_index=step_idx,
                op_type=op_type,
                success=success,
                aborted=False,
                duration_ms=max(0.0, float(duration_ms)),
                wire_rpc_ms=max(0.0, float(wire_rpc_ms)) if wire_rpc_ms is not None else None,
                request_bytes=req_b,
                response_bytes=resp_b,
                path_count=max(0, int(path_count)),
                error_category=error_category,
                error_code=error_code,
            )
        )
        if self._parent is not None:
            self._parent.record_operation_step(
                protocol=protocol,
                target=target,
                op_type=op_type,
                success=success,
                duration_ms=duration_ms,
                wire_rpc_ms=wire_rpc_ms,
                request_bytes=req_b,
                response_bytes=resp_b,
                path_count=path_count,
                error_category=error_category,
                error_code=error_code,
            )

    def record_operation_aborted(self, protocol: str, target: str, op_type: str) -> None:
        step_idx = len(self._steps)
        self._operations_aborted += 1
        self._steps.append(
            ExecutionStepStatsDTO(
                step_index=step_idx,
                op_type=op_type,
                success=False,
                aborted=True,
                duration_ms=0.0,
            )
        )
        if self._parent is not None:
            self._parent.record_operation_aborted(protocol, target, op_type)

    def record_netconf_rollback(self, target: str, action: str) -> None:
        act = (action or "unknown").strip().lower()
        self._rollbacks[act] = self._rollbacks.get(act, 0) + 1
        if self._parent is not None:
            self._parent.record_netconf_rollback(target, action)

    def record_schema_validation(
        self, protocol: str, passed: bool, duration_ms: float
    ) -> None:
        self._validation_duration_ms += max(0.0, float(duration_ms))
        if self._validation_passed is None:
            self._validation_passed = passed
        elif not passed:
            self._validation_passed = False
        if self._parent is not None:
            self._parent.record_schema_validation(protocol, passed, duration_ms)

    def record_schema_cache_access(self, hit: bool, layer_or_source: str) -> None:
        if self._parent is not None:
            self._parent.record_schema_cache_access(hit, layer_or_source)

    def record_schema_context_compiled(
        self, profile_id: str, duration_ms: float, loaded_profiles_count: int
    ) -> None:
        if self._parent is not None:
            self._parent.record_schema_context_compiled(
                profile_id, duration_ms, loaded_profiles_count
            )

    # No-op pass-throughs for remaining StatsRecorder methods
    def record_subscription_started(
        self, subscription_id: str, protocol: str, delivery_mode: str, target: str, owner_id: Optional[str] = None
    ) -> None:
        if self._parent is not None:
            self._parent.record_subscription_started(subscription_id, protocol, delivery_mode, target, owner_id=owner_id)

    def record_subscription_event(
        self, subscription_id: str, protocol: str, target: str, event_kind: str, payload_bytes: int = 0
    ) -> None:
        if self._parent is not None:
            self._parent.record_subscription_event(subscription_id, protocol, target, event_kind, payload_bytes=payload_bytes)

    def record_subscription_sync(self, subscription_id: str, elapsed_ms: float) -> None:
        if self._parent is not None:
            self._parent.record_subscription_sync(subscription_id, elapsed_ms)

    def record_subscription_poll(self, subscription_id: str) -> None:
        if self._parent is not None:
            self._parent.record_subscription_poll(subscription_id)

    def record_subscription_queue_depth(self, subscription_id: str, depth: int) -> None:
        if self._parent is not None:
            self._parent.record_subscription_queue_depth(subscription_id, depth)

    def record_subscription_drop(self, subscription_id: str, reason: str) -> None:
        if self._parent is not None:
            self._parent.record_subscription_drop(subscription_id, reason)

    def record_subscription_ended(self, subscription_id: str, protocol: str, target: str, reason: str) -> None:
        if self._parent is not None:
            self._parent.record_subscription_ended(subscription_id, protocol, target, reason)

    def record_session_opened(self, protocol: str, target: str) -> None:
        if self._parent is not None:
            self._parent.record_session_opened(protocol, target)

    def record_session_closed(self, protocol: str, target: str, reason: str) -> None:
        if self._parent is not None:
            self._parent.record_session_closed(protocol, target, reason)

    def record_session_operation(self, protocol: str, target: str) -> None:
        if self._parent is not None:
            self._parent.record_session_operation(protocol, target)

    def record_session_reconnect(self, protocol: str, target: str) -> None:
        if self._parent is not None:
            self._parent.record_session_reconnect(protocol, target)

    def record_session_idle_eviction(self, protocol: str, target: str) -> None:
        if self._parent is not None:
            self._parent.record_session_idle_eviction(protocol, target)

    def record_datastore_lock_acquired(self, target: str, datastore: str) -> None:
        if self._parent is not None:
            self._parent.record_datastore_lock_acquired(target, datastore)

    def record_datastore_lock_released(
        self, target: str, datastore: str, hold_ms: float, reclaimed: bool = False
    ) -> None:
        if self._parent is not None:
            self._parent.record_datastore_lock_released(target, datastore, hold_ms, reclaimed=reclaimed)

    def finalize(self, *, total_duration_ms: Optional[float] = None) -> ExecutionStatsDTO:
        elapsed_ms = (
            max(0.0, float(total_duration_ms))
            if total_duration_ms is not None
            else max(0.0, (self._clock() - self._start_mono) * 1000.0)
        )
        if self._parent is not None:
            self._parent.record_execution_duration(self._protocol, self._target, elapsed_ms)
        return ExecutionStatsDTO(
            protocol=self._protocol,
            target=self._target,
            request_id=self._request_id,
            total_duration_ms=elapsed_ms,
            connect_duration_ms=self._connect_duration_ms,
            connection_reused=self._connection_reused,
            validation_duration_ms=self._validation_duration_ms,
            validation_passed=self._validation_passed,
            operations_total=self._operations_total,
            operations_succeeded=self._operations_succeeded,
            operations_failed=self._operations_failed,
            operations_aborted=self._operations_aborted,
            request_payload_bytes=self._request_payload_bytes,
            response_payload_bytes=self._response_payload_bytes,
            rollbacks=dict(self._rollbacks),
            steps=list(self._steps),
        )


def aggregate_execution_stats(
    stats_list: Sequence[ExecutionStatsDTO],
    *,
    request_id: Optional[str] = None,
) -> Optional[ExecutionStatsDTO]:
    """Combines multiple per-repetition ExecutionStatsDTOs (e.g. `times > 1`) into one."""
    valid = [s for s in stats_list if s is not None]
    if not valid:
        return None
    if len(valid) == 1:
        return valid[0]

    combined_rollbacks: Dict[str, int] = {}
    combined_steps: List[ExecutionStepStatsDTO] = []
    val_passed: Optional[bool] = None
    step_idx = 0

    for item in valid:
        for k, v in item.rollbacks.items():
            combined_rollbacks[k] = combined_rollbacks.get(k, 0) + v
        if item.validation_passed is not None:
            val_passed = item.validation_passed if val_passed is None else (val_passed and item.validation_passed)
        for st in item.steps:
            combined_steps.append(
                ExecutionStepStatsDTO(
                    step_index=step_idx,
                    op_type=st.op_type,
                    success=st.success,
                    aborted=st.aborted,
                    duration_ms=st.duration_ms,
                    wire_rpc_ms=st.wire_rpc_ms,
                    request_bytes=st.request_bytes,
                    response_bytes=st.response_bytes,
                    path_count=st.path_count,
                    error_category=st.error_category,
                    error_code=st.error_code,
                )
            )
            step_idx += 1

    return ExecutionStatsDTO(
        protocol=valid[0].protocol,
        target=valid[0].target,
        request_id=request_id or valid[0].request_id,
        total_duration_ms=sum(s.total_duration_ms for s in valid),
        connect_duration_ms=sum(s.connect_duration_ms for s in valid),
        connection_reused=all(s.connection_reused for s in valid),
        validation_duration_ms=sum(s.validation_duration_ms for s in valid),
        validation_passed=val_passed,
        operations_total=sum(s.operations_total for s in valid),
        operations_succeeded=sum(s.operations_succeeded for s in valid),
        operations_failed=sum(s.operations_failed for s in valid),
        operations_aborted=sum(s.operations_aborted for s in valid),
        request_payload_bytes=sum(s.request_payload_bytes for s in valid),
        response_payload_bytes=sum(s.response_payload_bytes for s in valid),
        rollbacks=combined_rollbacks,
        steps=combined_steps,
    )


class StatsService:
    """Application Service Layer facade for querying and resetting runtime statistics."""

    def __init__(self, collector: Optional[StatsCollector] = None):
        self._collector = collector or StatsCollector()

    @property
    def collector(self) -> StatsCollector:
        return self._collector

    def get_snapshot(
        self,
        *,
        protocol: Optional[str] = None,
        target: Optional[str] = None,
        owner_id: Optional[str] = None,
        include_all_subscriptions: bool = True,
    ) -> StatsSnapshotDTO:
        return self._collector.snapshot(
            protocol=protocol,
            target=target,
            owner_id=owner_id,
            include_all_subscriptions=include_all_subscriptions,
        )

    def get_subscription_stats(
        self,
        subscription_id: str,
        *,
        owner_id: Optional[str] = None,
        is_admin: bool = False,
    ) -> SubscriptionStatsDTO:
        return self._collector.get_subscription_stats(
            subscription_id,
            owner_id=owner_id,
            is_admin=is_admin,
        )

    def reset(self) -> float:
        return self._collector.reset()
