# -*- coding: utf-8 -*-
"""
FastAPI routes for device inventory and profile management.
"""

from fastapi import APIRouter, Depends, Response, status

from application.devices import DeviceRegistryService
from application.exceptions import DeviceNotFoundError
from api.adapters import (
    enforce_endpoint_security_policy,
    to_adhoc_endpoint_dto,
    to_device_profile_response,
)
from api.deps import (
    APISettings,
    Principal,
    get_api_settings,
    get_current_principal,
    get_device_registry,
)
from api.schemas.devices import (
    DeviceProfileResponseSchema,
    DeviceRegisterRequestSchema,
)

router = APIRouter(prefix="/api/v1/devices", tags=["Devices"])


@router.get("", response_model=list[DeviceProfileResponseSchema])
async def list_devices(
    principal: Principal = Depends(get_current_principal),
    registry: DeviceRegistryService = Depends(get_device_registry),
) -> list[DeviceProfileResponseSchema]:
    """Lists all registered device profiles with sensitive credentials redacted."""
    _ = principal
    profiles: list[DeviceProfileResponseSchema] = []
    for device_id in registry.list_devices():
        ep = registry.get_device(device_id)
        profiles.append(to_device_profile_response(device_id, ep))
    return profiles


@router.post(
    "",
    response_model=DeviceProfileResponseSchema,
    status_code=status.HTTP_201_CREATED,
)
async def register_device(
    payload: DeviceRegisterRequestSchema,
    principal: Principal = Depends(get_current_principal),
    settings: APISettings = Depends(get_api_settings),
    registry: DeviceRegistryService = Depends(get_device_registry),
) -> DeviceProfileResponseSchema:
    """Registers or updates a device profile in `DeviceRegistryService`."""
    _ = principal
    enforce_endpoint_security_policy(
        payload.endpoint, settings, allow_adhoc_check=False
    )
    endpoint_dto = to_adhoc_endpoint_dto(payload.endpoint)
    registry.register_device(payload.device_id, endpoint_dto)
    return to_device_profile_response(payload.device_id, endpoint_dto)


@router.get("/{device_id}", response_model=DeviceProfileResponseSchema)
async def get_device(
    device_id: str,
    principal: Principal = Depends(get_current_principal),
    registry: DeviceRegistryService = Depends(get_device_registry),
) -> DeviceProfileResponseSchema:
    """Retrieves a single redacted device profile by identifier."""
    _ = principal
    endpoint = registry.get_device(device_id)
    return to_device_profile_response(device_id, endpoint)


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device(
    device_id: str,
    principal: Principal = Depends(get_current_principal),
    registry: DeviceRegistryService = Depends(get_device_registry),
) -> Response:
    """Unregisters a device profile by identifier."""
    _ = principal
    if not registry.unregister_device(device_id):
        raise DeviceNotFoundError(
            f"Device '{device_id}' is not registered in the device catalog."
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
