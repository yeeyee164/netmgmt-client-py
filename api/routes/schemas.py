# -*- coding: utf-8 -*-
"""
FastAPI routes for YANG schema catalog queries and offline pre-flight validation.
"""

import anyio
from fastapi import APIRouter, Depends

from application.schemas import SchemaCatalogService
from api.adapters import to_execution_request_dto, to_schema_module_schema
from api.deps import (
    APISettings,
    Principal,
    get_api_settings,
    get_current_principal,
    get_rpc_limiter,
    get_schema_service,
)
from api.schemas.schemas import (
    SchemaModuleSchema,
    SchemaStatisticsResponseSchema,
    SchemaValidateRequestSchema,
    SchemaValidateResponseSchema,
)

router = APIRouter(prefix="/api/v1/schemas", tags=["Schemas"])


@router.get("/modules", response_model=list[SchemaModuleSchema])
async def list_cached_modules(
    principal: Principal = Depends(get_current_principal),
    schema_service: SchemaCatalogService = Depends(get_schema_service),
    limiter: anyio.CapacityLimiter = Depends(get_rpc_limiter),
) -> list[SchemaModuleSchema]:
    """Lists metadata for cached YANG modules in SQLite storage."""
    _ = principal
    modules = await anyio.to_thread.run_sync(
        schema_service.list_cached_modules,
        limiter=limiter,
    )
    return [to_schema_module_schema(m) for m in modules]


@router.get("/statistics", response_model=SchemaStatisticsResponseSchema)
async def get_schema_statistics(
    principal: Principal = Depends(get_current_principal),
    schema_service: SchemaCatalogService = Depends(get_schema_service),
    limiter: anyio.CapacityLimiter = Depends(get_rpc_limiter),
) -> SchemaStatisticsResponseSchema:
    """Returns SQLite schema database cache metrics and table counts."""
    _ = principal
    stats = await anyio.to_thread.run_sync(
        schema_service.get_database_statistics,
        limiter=limiter,
    )
    return SchemaStatisticsResponseSchema(statistics=stats)


@router.get(
    "/profiles/{profile_id}/modules",
    response_model=list[SchemaModuleSchema],
)
async def get_profile_modules(
    profile_id: str,
    principal: Principal = Depends(get_current_principal),
    schema_service: SchemaCatalogService = Depends(get_schema_service),
    limiter: anyio.CapacityLimiter = Depends(get_rpc_limiter),
) -> list[SchemaModuleSchema]:
    """Lists YANG module metadata associated with a specific schema profile."""
    _ = principal
    modules = await anyio.to_thread.run_sync(
        schema_service.get_profile_modules,
        profile_id,
        limiter=limiter,
    )
    return [to_schema_module_schema(m) for m in modules]


@router.post("/validate", response_model=SchemaValidateResponseSchema)
async def validate_request_schema(
    payload: SchemaValidateRequestSchema,
    principal: Principal = Depends(get_current_principal),
    settings: APISettings = Depends(get_api_settings),
    schema_service: SchemaCatalogService = Depends(get_schema_service),
    limiter: anyio.CapacityLimiter = Depends(get_rpc_limiter),
) -> SchemaValidateResponseSchema:
    """Performs offline pre-flight YANG path and payload validation without device I/O."""
    _ = principal
    request_dto = to_execution_request_dto(payload, settings=settings)
    errors = await anyio.to_thread.run_sync(
        schema_service.validate_request_schema,
        request_dto,
        limiter=limiter,
    )
    return SchemaValidateResponseSchema(valid=len(errors) == 0, errors=errors)
