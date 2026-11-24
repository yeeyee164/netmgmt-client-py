# -*- coding: utf-8 -*-
"""
FastAPI dependency providers, application settings, service wiring, and authentication.
"""

from dataclasses import dataclass, field
import os
import secrets
from typing import Any, Optional

import anyio
from fastapi import Depends, Request, WebSocket

from application.devices import DeviceRegistryService
from application.operations import DeviceOperationService
from application.schemas import SchemaCatalogService
from application.sessions import DeviceSessionManager
from application.subscriptions import SubscriptionService
from api.exceptions import APIAuthenticationError


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
        )


def build_services(settings: Optional[APISettings] = None) -> dict[str, Any]:
    """
    Constructs and wires the Application Service Layer instances for one FastAPI app.
    """
    effective = settings or APISettings()
    device_registry = DeviceRegistryService()
    if effective.device_seed_file:
        device_registry.seed_from_env_file(effective.device_seed_file)

    schema_service = SchemaCatalogService()
    session_manager = DeviceSessionManager(
        device_registry=device_registry,
        default_ttl_seconds=effective.session_ttl_seconds,
    )
    operation_service = DeviceOperationService(
        device_registry=device_registry,
        schema_service=schema_service,
        session_manager=session_manager,
    )
    subscription_service = SubscriptionService(
        device_registry=device_registry,
        operation_service=operation_service,
    )
    return {
        "device_registry": device_registry,
        "schema_service": schema_service,
        "session_manager": session_manager,
        "operation_service": operation_service,
        "subscription_service": subscription_service,
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
                return Principal(owner_id=owner_id)
        if settings.auth_enabled:
            raise APIAuthenticationError("Invalid API credentials.")

    if settings.auth_enabled:
        raise APIAuthenticationError("Missing authentication credentials.")

    owner = x_owner_id.strip() if x_owner_id and x_owner_id.strip() else settings.default_owner_id
    return Principal(owner_id=owner)


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
