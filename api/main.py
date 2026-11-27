# -*- coding: utf-8 -*-
"""
FastAPI application factory, lifespan management, health probes, and router wiring.
"""

from contextlib import asynccontextmanager
from typing import Optional
import uuid

import anyio
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.deps import APISettings, build_services
from api.exceptions import register_exception_handlers
from api.routes import (
    devices_router,
    operations_router,
    schemas_router,
    sessions_router,
    stats_router,
    subscriptions_router,
)
from api.schemas.common import HealthResponseSchema, ReadinessResponseSchema

_SERVICE_KEYS = (
    "device_registry",
    "schema_service",
    "session_manager",
    "operation_service",
    "subscription_service",
    "stats_service",
)


def _initialize_app_state(app: FastAPI, settings: APISettings) -> None:
    """Initializes missing application services and concurrency limiter on `app.state`."""
    app.state.settings = settings
    missing = [key for key in _SERVICE_KEYS if not hasattr(app.state, key)]
    if missing:
        services = build_services(settings)
        for key, svc in services.items():
            if not hasattr(app.state, key):
                setattr(app.state, key, svc)
    if not hasattr(app.state, "rpc_limiter"):
        app.state.rpc_limiter = anyio.CapacityLimiter(settings.rpc_concurrency_limit)
    app.state.ready = True


def create_app(settings: Optional[APISettings] = None) -> FastAPI:
    """
    Creates and configures the FastAPI presentation application.
    """
    effective_settings = settings or APISettings.from_env()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        effective_settings.validate_startup()
        _initialize_app_state(application, effective_settings)
        try:
            yield
        finally:
            application.state.ready = False
            sub_svc = getattr(application.state, "subscription_service", None)
            if sub_svc is not None:
                sub_svc.stop_all()
            sess_mgr = getattr(application.state, "session_manager", None)
            if sess_mgr is not None:
                sess_mgr.close_all()

    application = FastAPI(
        title="netmgmt-client-py API",
        version="0.1.0",
        description="Northbound gNMI and NETCONF orchestration API built over the Application Service Layer.",
        lifespan=lifespan,
    )

    _initialize_app_state(application, effective_settings)

    if effective_settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(effective_settings.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Owner-Id"],
        )

    register_exception_handlers(application)

    @application.middleware("http")
    async def attach_request_id(request: Request, call_next):
        rid = request.headers.get("x-request-id")
        request_id = rid.strip() if rid and rid.strip() else str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @application.get("/healthz", response_model=HealthResponseSchema, tags=["Health"])
    async def liveness() -> HealthResponseSchema:
        """Liveness probe endpoint."""
        return HealthResponseSchema(status="ok")

    @application.get("/readyz", response_model=ReadinessResponseSchema, tags=["Health"])
    async def readiness(request: Request):
        """Readiness probe verifying initialized application services."""
        svc_status = {
            key: getattr(request.app.state, key, None) is not None
            for key in _SERVICE_KEYS
        }
        is_ready = bool(getattr(request.app.state, "ready", False)) and all(
            svc_status.values()
        )
        payload = ReadinessResponseSchema(
            status="ready" if is_ready else "not_ready",
            ready=is_ready,
            services=svc_status,
        )
        if not is_ready:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content=payload.model_dump(),
            )
        return payload

    application.include_router(devices_router)
    application.include_router(operations_router)
    application.include_router(sessions_router)
    application.include_router(subscriptions_router)
    application.include_router(schemas_router)
    application.include_router(stats_router)

    return application


app = create_app()
