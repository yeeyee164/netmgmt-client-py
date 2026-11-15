# -*- coding: utf-8 -*-
"""
FastAPI HTTP and WebSocket routes for telemetry subscriptions.
"""

from functools import partial

import anyio
from fastapi import APIRouter, Depends, Response, WebSocket, WebSocketDisconnect, status

from application.devices import DeviceRegistryService
from application.dto import validate_protocol_operation
from application.exceptions import PermissionDeniedError, SubscriptionNotFoundError
from application.subscriptions import SubscriptionService
from api.adapters import (
    to_execution_request_dto,
    to_stream_event_schema,
    to_subscription_info_response,
)
from api.deps import (
    APISettings,
    Principal,
    authenticate_websocket,
    get_api_settings,
    get_current_principal,
    get_device_registry,
    get_rpc_limiter,
    get_subscription_service,
)
from api.exceptions import APIAuthenticationError
from api.schemas.subscriptions import (
    SubscriptionCommandResponseSchema,
    SubscriptionCreateRequestSchema,
    SubscriptionCreateResponseSchema,
    SubscriptionInfoResponseSchema,
)

router = APIRouter(prefix="/api/v1/subscriptions", tags=["Subscriptions"])


@router.post(
    "",
    response_model=SubscriptionCreateResponseSchema,
    status_code=status.HTTP_201_CREATED,
)
async def create_subscription(
    payload: SubscriptionCreateRequestSchema,
    principal: Principal = Depends(get_current_principal),
    settings: APISettings = Depends(get_api_settings),
    registry: DeviceRegistryService = Depends(get_device_registry),
    subscription_service: SubscriptionService = Depends(get_subscription_service),
    limiter: anyio.CapacityLimiter = Depends(get_rpc_limiter),
) -> SubscriptionCreateResponseSchema:
    """Starts a background telemetry subscription owned by the authenticated caller."""
    request_dto = to_execution_request_dto(payload, settings=settings)
    endpoint = registry.resolve_endpoint(request_dto)
    for step in request_dto.operations:
        validate_protocol_operation(endpoint.protocol, step.op_type)

    sub_id = await anyio.to_thread.run_sync(
        partial(
            subscription_service.start_subscription,
            request_dto,
            queue_size=payload.queue_size,
            owner_id=principal.owner_id,
        ),
        limiter=limiter,
    )
    info = subscription_service.get_subscription_info(
        sub_id, owner_id=principal.owner_id
    )
    return SubscriptionCreateResponseSchema(
        subscription_id=info["subscription_id"],
        target=info["target"],
        protocol=info["protocol"],
        subscription_name=info["subscription_name"],
        state=info["state"],
        owner_id=info.get("owner_id"),
    )


@router.get("", response_model=list[SubscriptionInfoResponseSchema])
async def list_subscriptions(
    principal: Principal = Depends(get_current_principal),
    subscription_service: SubscriptionService = Depends(get_subscription_service),
) -> list[SubscriptionInfoResponseSchema]:
    """Lists active subscriptions owned by the authenticated caller."""
    entries = subscription_service.list_active_subscriptions(owner_id=principal.owner_id)
    results: list[SubscriptionInfoResponseSchema] = []
    for entry in entries:
        sid = entry["subscription_id"]
        try:
            info = subscription_service.get_subscription_info(
                sid, owner_id=principal.owner_id
            )
            results.append(to_subscription_info_response(info))
        except SubscriptionNotFoundError:
            continue
    return results


@router.get("/{subscription_id}", response_model=SubscriptionInfoResponseSchema)
async def get_subscription(
    subscription_id: str,
    principal: Principal = Depends(get_current_principal),
    subscription_service: SubscriptionService = Depends(get_subscription_service),
) -> SubscriptionInfoResponseSchema:
    """Returns metadata and liveness status for a caller-owned subscription."""
    info = subscription_service.get_subscription_info(
        subscription_id, owner_id=principal.owner_id
    )
    return to_subscription_info_response(info)


@router.post("/{subscription_id}/poll", response_model=SubscriptionCommandResponseSchema)
async def poll_subscription(
    subscription_id: str,
    principal: Principal = Depends(get_current_principal),
    subscription_service: SubscriptionService = Depends(get_subscription_service),
    limiter: anyio.CapacityLimiter = Depends(get_rpc_limiter),
) -> SubscriptionCommandResponseSchema:
    """Triggers an on-demand poll update for an active POLL subscription."""
    await anyio.to_thread.run_sync(
        partial(
            subscription_service.send_command,
            subscription_id,
            "poll",
            owner_id=principal.owner_id,
        ),
        limiter=limiter,
    )
    return SubscriptionCommandResponseSchema(
        subscription_id=subscription_id,
        command="poll",
        status="sent",
    )


@router.delete("/{subscription_id}", status_code=status.HTTP_204_NO_CONTENT)
async def stop_subscription(
    subscription_id: str,
    principal: Principal = Depends(get_current_principal),
    subscription_service: SubscriptionService = Depends(get_subscription_service),
    limiter: anyio.CapacityLimiter = Depends(get_rpc_limiter),
) -> Response:
    """Explicitly stops and cleans up a caller-owned telemetry subscription."""
    stopped = await anyio.to_thread.run_sync(
        partial(
            subscription_service.stop_subscription,
            subscription_id,
            owner_id=principal.owner_id,
        ),
        limiter=limiter,
    )
    if not stopped:
        raise SubscriptionNotFoundError(
            f"Active subscription '{subscription_id}' not found."
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.websocket("/{subscription_id}/ws")
async def stream_subscription_websocket(
    websocket: WebSocket,
    subscription_id: str,
) -> None:
    """
    Attaches a WebSocket consumer to an existing telemetry subscription.

    Disconnecting the WebSocket detaches only this consumer and never stops the
    underlying `SubscriptionService` stream.
    """
    subscription_service: SubscriptionService = websocket.app.state.subscription_service
    limiter: anyio.CapacityLimiter = websocket.app.state.rpc_limiter

    try:
        principal = authenticate_websocket(websocket)
        subscription_service.get_subscription_info(
            subscription_id, owner_id=principal.owner_id
        )
    except (APIAuthenticationError, PermissionDeniedError):
        await websocket.close(code=1008)
        return
    except SubscriptionNotFoundError:
        await websocket.close(code=4404)
        return

    await websocket.accept()

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

    async def _forward_subscription_events(task_group: anyio.abc.TaskGroup) -> None:
        try:
            while True:
                try:
                    events = await anyio.to_thread.run_sync(
                        partial(
                            subscription_service.get_events,
                            subscription_id,
                            timeout=0.1,
                            max_events=25,
                            owner_id=principal.owner_id,
                        ),
                        limiter=limiter,
                    )
                except SubscriptionNotFoundError:
                    await websocket.close(code=1000)
                    break

                for event in events:
                    await websocket.send_json(to_stream_event_schema(event).model_dump())

                if not events:
                    try:
                        info = subscription_service.get_subscription_info(
                            subscription_id, owner_id=principal.owner_id
                        )
                        if not info["is_alive"] and info["state"] in ("stopped", "failed"):
                            # Drain any last buffered events before closing
                            tail = await anyio.to_thread.run_sync(
                                partial(
                                    subscription_service.get_events,
                                    subscription_id,
                                    timeout=0.05,
                                    max_events=50,
                                    owner_id=principal.owner_id,
                                ),
                                limiter=limiter,
                            )
                            for event in tail:
                                await websocket.send_json(
                                    to_stream_event_schema(event).model_dump()
                                )
                            await websocket.close(code=1000)
                            break
                    except SubscriptionNotFoundError:
                        await websocket.close(code=1000)
                        break
        except WebSocketDisconnect:
            pass
        finally:
            # Never call subscription_service.stop_subscription() on consumer detach.
            task_group.cancel_scope.cancel()

    async with anyio.create_task_group() as tg:
        tg.start_soon(_watch_client_disconnect, tg)
        tg.start_soon(_forward_subscription_events, tg)
