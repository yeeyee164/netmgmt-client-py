# -*- coding: utf-8 -*-
"""
Pydantic v2 HTTP schemas for device inventory and endpoint configuration.
"""

from pydantic import BaseModel, Field

from api.schemas.common import ProtocolLiteral, StrictRequestModel


class AdhocEndpointSchema(StrictRequestModel):
    """Inline connection and security parameters for a target device."""

    target: str = Field(min_length=1, max_length=255, examples=["172.20.20.2:830"])
    protocol: ProtocolLiteral
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=512)
    tls_ca: str | None = Field(default=None, max_length=1024)
    tls_cert: str | None = Field(default=None, max_length=1024)
    tls_key: str | None = Field(default=None, max_length=1024)
    skip_verify: bool = False
    insecure: bool = False
    tls_server_name: str | None = Field(default=None, max_length=255)
    tls_version: str | None = Field(default=None, max_length=32)
    ssh_key: str | None = Field(default=None, max_length=1024)
    gnmi_use_tunnel: bool = False
    gnmi_target_name: str | None = Field(default=None, max_length=255)


class DeviceRegisterRequestSchema(StrictRequestModel):
    """Request payload for registering a logical device profile."""

    device_id: str = Field(min_length=1, max_length=128)
    endpoint: AdhocEndpointSchema


class DeviceProfileResponseSchema(BaseModel):
    """Redacted device profile metadata returned by the API (secrets omitted)."""

    device_id: str
    target: str
    protocol: str
    username: str | None = None
    has_password: bool = False
    has_tls_ca: bool = False
    has_tls_cert: bool = False
    has_tls_key: bool = False
    has_ssh_key: bool = False
    skip_verify: bool = False
    insecure: bool = False
    tls_server_name: str | None = None
    tls_version: str | None = None
    gnmi_use_tunnel: bool = False
    gnmi_target_name: str | None = None
