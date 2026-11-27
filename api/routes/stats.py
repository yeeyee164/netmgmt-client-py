# -*- coding: utf-8 -*-
"""
FastAPI HTTP and WebSocket routes for runtime Statistics & Observability.
"""

from typing import Optional

import anyio
from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from application.exceptions import PermissionDeniedError
from application.stats import StatsCollector, StatsService
from api.adapters import to_stats_snapshot_response, to_subscription_stats_schema
from api.deps import (
    Principal,
    authenticate_websocket,
    get_current_principal,
    get_stats_service,
    require_admin_principal,
)
from api.exceptions import APIAuthenticationError
from api.schemas.stats import (
    StatsResetResponseSchema,
    StatsSnapshotResponseSchema,
    SubscriptionStatsResponseSchema,
)

router = APIRouter(prefix="/api/v1/stats", tags=["Statistics"])


@router.get("", response_model=StatsSnapshotResponseSchema)
async def get_runtime_stats(
    protocol: Optional[str] = Query(default=None, pattern="^(gnmi|netconf)$"),
    target: Optional[str] = Query(default=None, min_length=1, max_length=256),
    principal: Principal = Depends(get_current_principal),
    stats_service: StatsService = Depends(get_stats_service),
) -> StatsSnapshotResponseSchema:
    """Returns a runtime statistics snapshot across all five observability domains."""
    is_admin = "admin" in principal.scopes
    snapshot = stats_service.get_snapshot(
        protocol=protocol,
        target=target,
        owner_id=principal.owner_id,
        include_all_subscriptions=is_admin,
    )
    return to_stats_snapshot_response(snapshot)


@router.get(
    "/subscriptions/{subscription_id}",
    response_model=SubscriptionStatsResponseSchema,
)
async def get_subscription_stats(
    subscription_id: str,
    principal: Principal = Depends(get_current_principal),
    stats_service: StatsService = Depends(get_stats_service),
) -> SubscriptionStatsResponseSchema:
    """Returns live or recently closed statistics for one subscription."""
    is_admin = "admin" in principal.scopes
    dto = stats_service.get_subscription_stats(
        subscription_id,
        owner_id=principal.owner_id,
        is_admin=is_admin,
    )
    return to_subscription_stats_schema(dto)


@router.post("/reset", response_model=StatsResetResponseSchema)
async def reset_runtime_stats(
    principal: Principal = Depends(require_admin_principal),
    stats_service: StatsService = Depends(get_stats_service),
) -> StatsResetResponseSchema:
    """Resets cumulative counters and latency buffers while preserving live gauges."""
    _ = principal
    reset_ts = stats_service.reset()
    return StatsResetResponseSchema(status="reset", reset_at_epoch=reset_ts)


def _parse_interval_ms(raw: Optional[str]) -> int:
    try:
        val = int(raw) if raw is not None else 1000
    except (TypeError, ValueError):
        val = 1000
    return max(250, min(60000, val))


@router.websocket("/ws")
async def stream_runtime_stats_websocket(websocket: WebSocket) -> None:
    """Pushes periodic runtime statistics snapshots over WebSocket for live dashboards."""
    stats_service: StatsService = websocket.app.state.stats_service
    stats_collector: Optional[StatsCollector] = getattr(
        websocket.app.state, "stats_collector", None
    )

    try:
        principal = authenticate_websocket(websocket)
    except (APIAuthenticationError, PermissionDeniedError):
        await websocket.close(code=1008)
        return

    is_admin = "admin" in principal.scopes
    interval_ms = _parse_interval_ms(websocket.query_params.get("interval_ms"))
    protocol = websocket.query_params.get("protocol") or None
    target = websocket.query_params.get("target") or None

    await websocket.accept()
    if stats_collector is not None:
        stats_collector.websocket_connected("stats")

    async def _watch_client_disconnect(task_group: anyio.abc.TaskGroup) -> None:
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            task_group.cancel_scope.cancel()

    async def _push_stats_snapshots(task_group: anyio.abc.TaskGroup) -> None:
        try:
            while True:
                snapshot = stats_service.get_snapshot(
                    protocol=protocol,
                    target=target,
                    owner_id=principal.owner_id,
                    include_all_subscriptions=is_admin,
                )
                await websocket.send_json(
                    to_stats_snapshot_response(snapshot).model_dump()
                )
                await anyio.sleep(interval_ms / 1000.0)
        except WebSocketDisconnect:
            pass
        finally:
            task_group.cancel_scope.cancel()

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_watch_client_disconnect, tg)
            tg.start_soon(_push_stats_snapshots, tg)
    finally:
        if stats_collector is not None:
            stats_collector.websocket_disconnected("stats")
