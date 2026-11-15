# -*- coding: utf-8 -*-
"""
FastAPI routes for unary device operations and sequential pipelines.
"""

import anyio
from fastapi import APIRouter, Depends

from application.devices import DeviceRegistryService
from application.dto import validate_protocol_operation
from application.operations import DeviceOperationService
from api.adapters import to_execution_request_dto, to_execution_result_schema
from api.deps import (
    APISettings,
    Principal,
    get_api_settings,
    get_current_principal,
    get_device_registry,
    get_operation_service,
    get_rpc_limiter,
)
from api.schemas.operations import ExecutionRequestSchema, ExecutionResultSchema

router = APIRouter(prefix="/api/v1/operations", tags=["Operations"])


@router.post("/execute", response_model=ExecutionResultSchema)
async def execute_operation(
    payload: ExecutionRequestSchema,
    principal: Principal = Depends(get_current_principal),
    settings: APISettings = Depends(get_api_settings),
    registry: DeviceRegistryService = Depends(get_device_registry),
    service: DeviceOperationService = Depends(get_operation_service),
    limiter: anyio.CapacityLimiter = Depends(get_rpc_limiter),
) -> ExecutionResultSchema:
    """
    Executes a unary operation or sequential operation pipeline against a target device.
    """
    _ = principal  # Authentication verified via dependency
    request_dto = to_execution_request_dto(payload, settings=settings)

    # Pre-flight target & protocol matrix verification (raises 404/400 on invalid targets/ops)
    endpoint = registry.resolve_endpoint(request_dto)
    for step in request_dto.operations:
        validate_protocol_operation(endpoint.protocol, step.op_type)

    result_dto = await anyio.to_thread.run_sync(
        service.execute_unary,
        request_dto,
        limiter=limiter,
    )
    return to_execution_result_schema(result_dto)
