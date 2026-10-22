# -*- coding: utf-8 -*-
import pytest

from application.devices import DeviceRegistryService
from application.dto import AdhocEndpointDTO, ExecutionRequestDTO
from application.exceptions import DeviceNotFoundError, InvalidRequestError


class TestDeviceRegistryService:
    def test_register_and_get_device(self):
        registry = DeviceRegistryService()
        ep = AdhocEndpointDTO(
            target="172.20.20.2:830",
            protocol="netconf",
            username="admin",
            password="pwd",
        )
        registry.register_device("leaf-01", ep)

        retrieved = registry.get_device("leaf-01")
        assert retrieved.target == "172.20.20.2:830"
        assert retrieved.protocol == "netconf"
        assert retrieved.username == "admin"

    def test_list_and_unregister_device(self):
        registry = DeviceRegistryService()
        ep1 = AdhocEndpointDTO(target="10.0.0.1:830", protocol="netconf")
        ep2 = AdhocEndpointDTO(target="10.0.0.2:57400", protocol="gnmi")

        registry.register_device("spine-02", ep2)
        registry.register_device("spine-01", ep1)

        assert registry.list_devices() == ["spine-01", "spine-02"]

        assert registry.unregister_device("spine-01") is True
        assert registry.unregister_device("spine-01") is False
        assert registry.list_devices() == ["spine-02"]

    def test_get_unregistered_device_raises(self):
        registry = DeviceRegistryService()
        with pytest.raises(DeviceNotFoundError, match="is not registered"):
            registry.get_device("unknown-router")

    def test_invalid_device_registration_raises(self):
        registry = DeviceRegistryService()
        with pytest.raises(InvalidRequestError, match="device_id must be a non-empty string"):
            registry.register_device("", AdhocEndpointDTO(target="10.0.0.1:830", protocol="netconf"))

        with pytest.raises(InvalidRequestError, match="endpoint must be an instance"):
            registry.register_device("dev-01", {"target": "10.0.0.1:830"})  # type: ignore

    def test_resolve_endpoint_from_adhoc(self):
        registry = DeviceRegistryService()
        ep = AdhocEndpointDTO(target="192.168.1.1:830", protocol="netconf")
        req = ExecutionRequestDTO(adhoc_endpoint=ep, protocol="netconf")

        resolved = registry.resolve_endpoint(req)
        assert resolved.target == "192.168.1.1:830"
        assert resolved.protocol == "netconf"

    def test_resolve_endpoint_from_adhoc_protocol_mismatch_raises(self):
        registry = DeviceRegistryService()
        ep = AdhocEndpointDTO(target="192.168.1.1:830", protocol="netconf")
        req = ExecutionRequestDTO(adhoc_endpoint=ep, protocol="gnmi")

        with pytest.raises(InvalidRequestError, match="Protocol conflict"):
            registry.resolve_endpoint(req)

    def test_resolve_endpoint_from_device_id(self):
        registry = DeviceRegistryService()
        ep = AdhocEndpointDTO(target="172.20.20.2:57400", protocol="gnmi", username="admin")
        registry.register_device("srl-01", ep)

        req = ExecutionRequestDTO(device_id="srl-01")
        resolved = registry.resolve_endpoint(req)
        assert resolved.target == "172.20.20.2:57400"
        assert resolved.protocol == "gnmi"
        assert resolved.username == "admin"

    def test_resolve_endpoint_from_device_id_protocol_mismatch_raises(self):
        registry = DeviceRegistryService()
        ep = AdhocEndpointDTO(target="172.20.20.2:57400", protocol="gnmi")
        registry.register_device("srl-01", ep)

        req = ExecutionRequestDTO(device_id="srl-01", protocol="netconf")
        with pytest.raises(InvalidRequestError, match="Protocol conflict"):
            registry.resolve_endpoint(req)

    def test_resolve_endpoint_device_not_found(self):
        registry = DeviceRegistryService()
        req = ExecutionRequestDTO(device_id="missing-dev")
        with pytest.raises(DeviceNotFoundError):
            registry.resolve_endpoint(req)

