# -*- coding: utf-8 -*-
"""
FastAPI routes for managed logical device sessions and cross-request session execution.
"""

from functools import partial

import anyio
from fastapi import APIRouter, Depends, Response, status

from application.dto import validate_protocol_operation
from application.exceptions import SessionNotFoundError
from application.operations import DeviceOperationService
from application.sessions import DeviceSessionManager
from api.adapters import (
    to_execution_result_schema,
    to_session_execution_request_dto,
    to_session_info_response,
)
from api.deps import (
    APISettings,
    Principal,
    get_api_settings,
    get_current_principal,
    get_operation_service,
    get_rpc_limiter,
    get_session_manager,
)
from api.schemas.operations import ExecutionResultSchema
from api.schemas.sessions import (
    SessionCreateRequestSchema,
    SessionExecutionRequestSchema,
    SessionInfoResponseSchema,
)

router = APIRouter(prefix="/api/v1", tags=["Sessions"])


@router.post(
    "/devices/{device_id}/sessions",
    response_model=SessionInfoResponseSchema,
    status_code=status.HTTP_201_CREATED,
)
async def create_device_session(
    device_id: str,
    payload: SessionCreateRequestSchema | None = None,
    principal: Principal = Depends(get_current_principal),
    session_manager: DeviceSessionManager = Depends(get_session_manager),
    limiter: anyio.CapacityLimiter = Depends(get_rpc_limiter),
) -> SessionInfoResponseSchema:
    """Opens a managed logical session bound to the authenticated caller and device."""
    req = payload or SessionCreateRequestSchema()
    info = await anyio.to_thread.run_sync(
        partial(
            session_manager.create_session,
            owner_id=principal.owner_id,
            device_id=device_id,
            protocol=req.protocol,
            ttl_seconds=req.ttl_seconds,
        ),
        limiter=limiter,
    )
    return to_session_info_response(info)


@router.get("/sessions", response_model=list[SessionInfoResponseSchema])
async def list_sessions(
    principal: Principal = Depends(get_current_principal),
    session_manager: DeviceSessionManager = Depends(get_session_manager),
    limiter: anyio.CapacityLimiter = Depends(get_rpc_limiter),
) -> list[SessionInfoResponseSchema]:
    """Lists active logical sessions owned by the authenticated caller."""
    sessions = await anyio.to_thread.run_sync(
        partial(session_manager.list_sessions, owner_id=principal.owner_id),
        limiter=limiter,
    )
    return [to_session_info_response(s) for s in sessions]


@router.get("/sessions/{session_id}", response_model=SessionInfoResponseSchema)
async def get_session(
    session_id: str,
    principal: Principal = Depends(get_current_principal),
    session_manager: DeviceSessionManager = Depends(get_session_manager),
    limiter: anyio.CapacityLimiter = Depends(get_rpc_limiter),
) -> SessionInfoResponseSchema:
    """Inspects metadata for a caller-owned managed logical session."""
    info = await anyio.to_thread.run_sync(
        partial(session_manager.get_session_info, session_id, owner_id=principal.owner_id),
        limiter=limiter,
    )
    return to_session_info_response(info)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def close_session(
    session_id: str,
    principal: Principal = Depends(get_current_principal),
    session_manager: DeviceSessionManager = Depends(get_session_manager),
    limiter: anyio.CapacityLimiter = Depends(get_rpc_limiter),
) -> Response:
    """Closes a caller-owned managed logical session and releases its transport."""
    closed = await anyio.to_thread.run_sync(
        partial(
            session_manager.close_session,
            session_id,
            owner_id=principal.owner_id,
        ),
        limiter=limiter,
    )
    if not closed:
        raise SessionNotFoundError(f"Logical session '{session_id}' not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/sessions/{session_id}/operations",
    response_model=ExecutionResultSchema,
)
async def execute_session_operations(
    session_id: str,
    payload: SessionExecutionRequestSchema,
    principal: Principal = Depends(get_current_principal),
    settings: APISettings = Depends(get_api_settings),
    session_manager: DeviceSessionManager = Depends(get_session_manager),
    operation_service: DeviceOperationService = Depends(get_operation_service),
    limiter: anyio.CapacityLimiter = Depends(get_rpc_limiter),
) -> ExecutionResultSchema:
    """Executes operations within an existing managed logical session."""
    session_info = await anyio.to_thread.run_sync(
        partial(
            session_manager.get_session_info,
            session_id,
            owner_id=principal.owner_id,
        ),
        limiter=limiter,
    )
    request_dto = to_session_execution_request_dto(
        payload, session_info, settings=settings
    )
    for step in request_dto.operations:
        validate_protocol_operation(session_info.protocol, step.op_type)

    result_dto = await anyio.to_thread.run_sync(
        partial(
            operation_service.execute_in_session,
            session_id,
            request_dto,
            owner_id=principal.owner_id,
        ),
        limiter=limiter,
    )
    return to_execution_result_schema(result_dto)
