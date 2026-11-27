# -*- coding: utf-8 -*-
"""
Zero-dependency Statistics & Observability hook protocol.

Lower engine layers (`managers/`, `specs/`, `core/`) depend exclusively on this
module to emit instrumentation events without importing `application/`, `fastapi`,
`pydantic`, or `cli/`.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class StatsRecorder(Protocol):
    """Lightweight instrumentation protocol injected into managers/, specs/, and core/."""

    def record_connect(
        self, protocol: str, target: str, duration_ms: float, reused: bool = False
    ) -> None: ...

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
    ) -> None: ...

    def record_operation_aborted(self, protocol: str, target: str, op_type: str) -> None: ...

    def record_netconf_rollback(self, target: str, action: str) -> None: ...

    def record_subscription_started(
        self,
        subscription_id: str,
        protocol: str,
        delivery_mode: str,
        target: str,
        owner_id: Optional[str] = None,
    ) -> None: ...

    def record_subscription_event(
        self,
        subscription_id: str,
        protocol: str,
        target: str,
        event_kind: str,
        payload_bytes: int = 0,
    ) -> None: ...

    def record_subscription_sync(self, subscription_id: str, elapsed_ms: float) -> None: ...

    def record_subscription_poll(self, subscription_id: str) -> None: ...

    def record_subscription_queue_depth(self, subscription_id: str, depth: int) -> None: ...

    def record_subscription_drop(self, subscription_id: str, reason: str) -> None: ...

    def record_subscription_ended(
        self, subscription_id: str, protocol: str, target: str, reason: str
    ) -> None: ...

    def record_session_opened(self, protocol: str, target: str) -> None: ...

    def record_session_closed(self, protocol: str, target: str, reason: str) -> None: ...

    def record_session_operation(self, protocol: str, target: str) -> None: ...

    def record_session_reconnect(self, protocol: str, target: str) -> None: ...

    def record_session_idle_eviction(self, protocol: str, target: str) -> None: ...

    def record_datastore_lock_acquired(self, target: str, datastore: str) -> None: ...

    def record_datastore_lock_released(
        self, target: str, datastore: str, hold_ms: float, reclaimed: bool = False
    ) -> None: ...

    def record_schema_validation(
        self, protocol: str, passed: bool, duration_ms: float
    ) -> None: ...

    def record_schema_cache_access(
        self, hit: bool, layer_or_source: str
    ) -> None: ...

    def record_schema_context_compiled(
        self, profile_id: str, duration_ms: float, loaded_profiles_count: int
    ) -> None: ...


class NoOpStatsRecorder:
    """Zero-overhead default recorder used when statistics collection is disabled."""

    def record_connect(
        self, protocol: str, target: str, duration_ms: float, reused: bool = False
    ) -> None:
        return None

    def record_operation_step(self, **kwargs: Any) -> None:
        return None

    def record_operation_aborted(self, protocol: str, target: str, op_type: str) -> None:
        return None

    def record_netconf_rollback(self, target: str, action: str) -> None:
        return None

    def record_subscription_started(
        self,
        subscription_id: str,
        protocol: str,
        delivery_mode: str,
        target: str,
        owner_id: Optional[str] = None,
    ) -> None:
        return None

    def record_subscription_event(
        self,
        subscription_id: str,
        protocol: str,
        target: str,
        event_kind: str,
        payload_bytes: int = 0,
    ) -> None:
        return None

    def record_subscription_sync(self, subscription_id: str, elapsed_ms: float) -> None:
        return None

    def record_subscription_poll(self, subscription_id: str) -> None:
        return None

    def record_subscription_queue_depth(self, subscription_id: str, depth: int) -> None:
        return None

    def record_subscription_drop(self, subscription_id: str, reason: str) -> None:
        return None

    def record_subscription_ended(
        self, subscription_id: str, protocol: str, target: str, reason: str
    ) -> None:
        return None

    def record_session_opened(self, protocol: str, target: str) -> None:
        return None

    def record_session_closed(self, protocol: str, target: str, reason: str) -> None:
        return None

    def record_session_operation(self, protocol: str, target: str) -> None:
        return None

    def record_session_reconnect(self, protocol: str, target: str) -> None:
        return None

    def record_session_idle_eviction(self, protocol: str, target: str) -> None:
        return None

    def record_datastore_lock_acquired(self, target: str, datastore: str) -> None:
        return None

    def record_datastore_lock_released(
        self, target: str, datastore: str, hold_ms: float, reclaimed: bool = False
    ) -> None:
        return None

    def record_schema_validation(
        self, protocol: str, passed: bool, duration_ms: float
    ) -> None:
        return None

    def record_schema_cache_access(self, hit: bool, layer_or_source: str) -> None:
        return None

    def record_schema_context_compiled(
        self, profile_id: str, duration_ms: float, loaded_profiles_count: int
    ) -> None:
        return None


NOOP_STATS_RECORDER: StatsRecorder = NoOpStatsRecorder()


def safe_record(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Invokes a recorder callback while guaranteeing no exception escapes into caller logic."""
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        logger.debug("[Stats] Suppressed instrumentation error in %s: %s", getattr(fn, "__name__", fn), exc)
