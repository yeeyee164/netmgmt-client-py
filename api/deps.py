# -*- coding: utf-8 -*-
"""
FastAPI dependency providers, application settings, service wiring, and authentication.
"""

from dataclasses import dataclass, field
import inspect
import os
import secrets
import time
from typing import Any, Optional
import uuid

import anyio
from fastapi import Depends, Request, WebSocket

from application.devices import DeviceRegistryService
from application.operations import DeviceOperationService
from application.schemas import SchemaCatalogService
from application.sessions import DeviceSessionManager
from application.stats import StatsCollector, StatsService
from application.subscriptions import SubscriptionService
from api.exceptions import APIAuthenticationError, APIAuthorizationError


def _parse_bool(val: Optional[str], default: bool) -> bool:
    if val is None:
        return default
    norm = val.strip().lower()
    if norm in ("1", "true", "yes", "on"):
        return True
    if norm in ("0", "false", "no", "off"):
        return False
    return default


def _parse_csv(val: Optional[str]) -> tuple[str, ...]:
    if not val:
        return ()
    return tuple(item.strip() for item in val.split(",") if item.strip())


def _parse_api_keys(val: Optional[str]) -> dict[str, str]:
    """
    Parses `NETMGMT_API_KEYS` formatted as `owner1:token1,owner2:token2`
    or plain `token1,token2` (defaulting owner to 'api-client') into `{token: owner_id}`.
    Returns an empty dict when unset, empty, or containing only malformed entries.
    """
    if not val or not val.strip():
        return {}
    mapping: dict[str, str] = {}
    for entry in val.split(","):
        item = entry.strip()
        if not item:
            continue
        if ":" in item:
            owner_id, token = item.split(":", 1)
            owner_id = owner_id.strip()
            token = token.strip()
            if not owner_id or not token:
                continue
        else:
            owner_id = "api-client"
            token = item
        if token:
            mapping[token] = owner_id
    return mapping


def _parse_workers() -> int:
    for env_name in ("NETMGMT_WORKERS", "WEB_CONCURRENCY", "UVICORN_WORKERS"):
        raw = os.getenv(env_name)
        if raw is not None and raw.strip():
            try:
                return int(raw.strip())
            except ValueError as exc:
                raise ValueError(f"Invalid integer in {env_name}={raw!r}") from exc
    return 1


@dataclass(frozen=True)
class Principal:
    """Authenticated API caller identity."""

    owner_id: str
    scopes: frozenset[str] = frozenset({"read", "write", "admin"})


@dataclass
class APISettings:
    """Configuration for the FastAPI presentation tier."""

    auth_enabled: bool = True
    api_keys: dict[str, str] = field(default_factory=dict)
    default_owner_id: str = "anonymous"
    rpc_concurrency_limit: int = 32
    session_ttl_seconds: float = 300.0
    workers: int = 1
    allow_adhoc_endpoints: bool = True
    allowed_targets: tuple[str, ...] = ()
    allow_client_filesystem_paths: bool = False
    allowed_schema_dirs: tuple[str, ...] = ()
    device_seed_file: Optional[str] = None
    cors_origins: tuple[str, ...] = (
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    )
    admin_owners: tuple[str, ...] = ()

    def validate_startup(self) -> None:
        """Validates production security and single-process deployment invariants at startup."""
        if self.workers > 1:
            raise RuntimeError(
                f"Multi-process deployment (workers={self.workers}) is not supported because "
                "device registry, logical sessions, and telemetry subscriptions are stored in "
                "process memory. Run with a single worker (--workers 1)."
            )
        if self.auth_enabled and not self.api_keys:
            raise RuntimeError(
                "API authentication is enabled (NETMGMT_AUTH_ENABLED=true), but no valid API keys "
                "are configured. Set NETMGMT_API_KEYS (e.g. 'owner:secret-token')."
            )

    @classmethod
    def from_env(cls) -> "APISettings":
        """Loads API configuration from `NETMGMT_*` environment variables."""
        limit_raw = os.getenv("NETMGMT_RPC_CONCURRENCY_LIMIT", "32")
        ttl_raw = os.getenv("NETMGMT_SESSION_TTL_SECONDS", "300")
        cors_raw = os.getenv("NETMGMT_CORS_ORIGINS")
        return cls(
            auth_enabled=_parse_bool(os.getenv("NETMGMT_AUTH_ENABLED"), True),
            api_keys=_parse_api_keys(os.getenv("NETMGMT_API_KEYS")),
            default_owner_id=os.getenv("NETMGMT_DEFAULT_OWNER_ID", "anonymous").strip()
            or "anonymous",
            rpc_concurrency_limit=max(1, int(limit_raw)),
            session_ttl_seconds=max(1.0, float(ttl_raw)),
            workers=_parse_workers(),
            allow_adhoc_endpoints=_parse_bool(
                os.getenv("NETMGMT_ALLOW_ADHOC_ENDPOINTS"), True
            ),
            allowed_targets=_parse_csv(os.getenv("NETMGMT_ALLOWED_TARGETS")),
            allow_client_filesystem_paths=_parse_bool(
                os.getenv("NETMGMT_ALLOW_CLIENT_PATHS"), False
            ),
            allowed_schema_dirs=_parse_csv(os.getenv("NETMGMT_ALLOWED_SCHEMA_DIRS")),
            device_seed_file=os.getenv("NETMGMT_DEVICE_SEED_FILE") or None,
            cors_origins=_parse_csv(cors_raw)
            if cors_raw is not None
            else ("http://localhost:3000", "http://127.0.0.1:3000"),
            admin_owners=_parse_csv(os.getenv("NETMGMT_ADMIN_OWNERS")),
        )


def build_services(settings: Optional[APISettings] = None) -> dict[str, Any]:
    """
    Constructs and wires the Application Service Layer instances for one FastAPI app.
    """
    effective = settings or APISettings()
    stats_collector = StatsCollector()
    stats_service = StatsService(collector=stats_collector)

    device_registry = DeviceRegistryService()
    if effective.device_seed_file:
        device_registry.seed_from_env_file(effective.device_seed_file)

    schema_service = SchemaCatalogService(stats_recorder=stats_collector)
    session_manager = DeviceSessionManager(
        device_registry=device_registry,
        default_ttl_seconds=effective.session_ttl_seconds,
        stats_recorder=stats_collector,
    )
    operation_service = DeviceOperationService(
        device_registry=device_registry,
        schema_service=schema_service,
        session_manager=session_manager,
        stats_recorder=stats_collector,
    )
    subscription_service = SubscriptionService(
        device_registry=device_registry,
        operation_service=operation_service,
        stats_recorder=stats_collector,
    )
    return {
        "device_registry": device_registry,
        "schema_service": schema_service,
        "session_manager": session_manager,
        "operation_service": operation_service,
        "subscription_service": subscription_service,
        "stats_collector": stats_collector,
        "stats_service": stats_service,
    }


def _extract_token(
    authorization: Optional[str],
    x_api_key: Optional[str],
    query_token: Optional[str] = None,
) -> Optional[str]:
    if authorization:
        parts = authorization.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
            return parts[1].strip()
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    if query_token and query_token.strip():
        return query_token.strip()
    return None


def _resolve_principal(
    *,
    settings: APISettings,
    authorization: Optional[str],
    x_api_key: Optional[str],
    query_token: Optional[str] = None,
    x_owner_id: Optional[str] = None,
) -> Principal:
    if settings.auth_enabled and not settings.api_keys:
        raise APIAuthenticationError(
            "API authentication is enabled, but no valid API keys are configured."
        )

    token = _extract_token(authorization, x_api_key, query_token)

    if token is not None:
        for configured_token, owner_id in settings.api_keys.items():
            if secrets.compare_digest(token, configured_token):
                scopes = (
                    frozenset({"read", "write", "admin"})
                    if owner_id in settings.admin_owners
                    else frozenset({"read", "write"})
                )
                return Principal(owner_id=owner_id, scopes=scopes)
        if settings.auth_enabled:
            raise APIAuthenticationError("Invalid API credentials.")

    if settings.auth_enabled:
        raise APIAuthenticationError("Missing authentication credentials.")

    owner = x_owner_id.strip() if x_owner_id and x_owner_id.strip() else settings.default_owner_id
    scopes = (
        frozenset({"read", "write", "admin"})
        if owner in settings.admin_owners
        else frozenset({"read", "write"})
    )
    return Principal(owner_id=owner, scopes=scopes)


def get_api_settings(request: Request) -> APISettings:
    return request.app.state.settings


def get_device_registry(request: Request) -> DeviceRegistryService:
    return request.app.state.device_registry


def get_schema_service(request: Request) -> SchemaCatalogService:
    return request.app.state.schema_service


def get_operation_service(request: Request) -> DeviceOperationService:
    return request.app.state.operation_service


def get_subscription_service(request: Request) -> SubscriptionService:
    return request.app.state.subscription_service


def get_session_manager(request: Request) -> DeviceSessionManager:
    return request.app.state.session_manager


def get_rpc_limiter(request: Request) -> anyio.CapacityLimiter:
    return request.app.state.rpc_limiter


def get_stats_service(request: Request) -> StatsService:
    return request.app.state.stats_service


def get_stats_collector(request: Request) -> Optional[StatsCollector]:
    return getattr(request.app.state, "stats_collector", None)


def get_request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None) or request.headers.get("x-request-id")
    if rid and str(rid).strip():
        return str(rid).strip()
    return str(uuid.uuid4())


def call_with_optional_request_id(
    fn: Any,
    *args: Any,
    request_id: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    """Invokes fn, passing request_id only if fn accepts it."""
    if request_id is not None:
        try:
            sig = inspect.signature(fn)
            params = sig.parameters
            if "request_id" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            ):
                kwargs["request_id"] = request_id
        except (TypeError, ValueError):
            pass
    return fn(*args, **kwargs)


async def run_sync_with_stats(
    func: Any,
    *,
    limiter: anyio.CapacityLimiter,
    stats_collector: Optional[StatsCollector] = None,
    worker_type: str = "unary",
) -> Any:
    """Acquires concurrency limiter, records wait time and active worker threads, and runs func."""
    t0 = time.perf_counter()
    async with limiter:
        wait_ms = (time.perf_counter() - t0) * 1000.0
        if stats_collector is not None:
            stats_collector.record_worker_semaphore_wait(worker_type, wait_ms)
            stats_collector.worker_thread_enter(worker_type)
        try:
            return await anyio.to_thread.run_sync(func)
        finally:
            if stats_collector is not None:
                stats_collector.worker_thread_exit(worker_type)


def get_current_principal(
    request: Request,
    settings: APISettings = Depends(get_api_settings),
) -> Principal:
    """FastAPI HTTP authentication dependency returning the resolved `Principal`."""
    return _resolve_principal(
        settings=settings,
        authorization=request.headers.get("authorization"),
        x_api_key=request.headers.get("x-api-key"),
        x_owner_id=request.headers.get("x-owner-id"),
    )


def require_admin_principal(
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    """Requires `'admin'` scope on the authenticated principal."""
    if "admin" not in principal.scopes:
        raise APIAuthorizationError(
            f"Principal '{principal.owner_id}' lacks required 'admin' scope."
        )
    return principal


def authenticate_websocket(websocket: WebSocket) -> Principal:
    """Authenticates a WebSocket handshake via headers or `?token=` query parameter."""
    settings: APISettings = websocket.app.state.settings
    return _resolve_principal(
        settings=settings,
        authorization=websocket.headers.get("authorization"),
        x_api_key=websocket.headers.get("x-api-key"),
        query_token=websocket.query_params.get("token"),
        x_owner_id=websocket.headers.get("x-owner-id"),
    )
