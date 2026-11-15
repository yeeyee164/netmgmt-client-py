# -*- coding: utf-8 -*-
"""
FastAPI router package.
"""

from api.routes.devices import router as devices_router
from api.routes.operations import router as operations_router
from api.routes.schemas import router as schemas_router
from api.routes.sessions import router as sessions_router
from api.routes.subscriptions import router as subscriptions_router

__all__ = [
    "devices_router",
    "operations_router",
    "schemas_router",
    "sessions_router",
    "subscriptions_router",
]
