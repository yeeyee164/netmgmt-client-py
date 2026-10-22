# -*- coding: utf-8 -*-
"""
Device Registry Service.
Manages network device endpoint profiles and resolves target credentials and protocols.
"""

import threading
from typing import Dict, List, Optional

from application.dto import AdhocEndpointDTO, ExecutionRequestDTO, SupportedProtocol
from application.exceptions import DeviceNotFoundError, InvalidRequestError


class DeviceRegistryService:
    """
    Thread-safe registry for known network device profiles.
    Resolves execution requests into concrete connection endpoints.
    """

    def __init__(self, initial_devices: Optional[Dict[str, AdhocEndpointDTO]] = None):
        self._lock = threading.RLock()
        self._devices: Dict[str, AdhocEndpointDTO] = {}
        if initial_devices:
            for dev_id, ep in initial_devices.items():
                self.register_device(dev_id, ep)

    def register_device(self, device_id: str, endpoint: AdhocEndpointDTO) -> None:
        """Registers a device profile by ID."""
        if not device_id or not isinstance(device_id, str):
            raise InvalidRequestError("device_id must be a non-empty string.")
        if not isinstance(endpoint, AdhocEndpointDTO):
            raise InvalidRequestError("endpoint must be an instance of AdhocEndpointDTO.")

        with self._lock:
            self._devices[device_id] = endpoint

    def unregister_device(self, device_id: str) -> bool:
        """Removes a device profile by ID. Returns True if found and removed."""
        with self._lock:
            return self._devices.pop(device_id, None) is not None

    def get_device(self, device_id: str) -> AdhocEndpointDTO:
        """Retrieves a registered device profile by ID."""
        with self._lock:
            endpoint = self._devices.get(device_id)
            if endpoint is None:
                raise DeviceNotFoundError(f"Device '{device_id}' is not registered in the device catalog.")
            return endpoint

    def has_device(self, device_id: str) -> bool:
        """Returns True if the device_id is registered in the catalog."""
        with self._lock:
            return device_id in self._devices

    def list_devices(self) -> List[str]:
        """Lists all registered device IDs."""
        with self._lock:
            return sorted(list(self._devices.keys()))

    def resolve_endpoint(self, request: ExecutionRequestDTO) -> AdhocEndpointDTO:
        """
        Resolves an execution request into a concrete AdhocEndpointDTO.
        Guarantees that target and protocol are validated and populated.
        """
        if request.adhoc_endpoint is not None:
            ep = request.adhoc_endpoint
            if request.protocol:
                requested_proto = SupportedProtocol.from_string(request.protocol).value
                if ep.protocol != requested_proto:
                    raise InvalidRequestError(
                        f"Protocol conflict: request specifies '{requested_proto}' "
                        f"but adhoc endpoint specifies '{ep.protocol}'."
                    )
            return ep

        if request.device_id is not None:
            ep = self.get_device(request.device_id)
            target_proto = ep.protocol
            if request.protocol:
                target_proto = SupportedProtocol.from_string(request.protocol).value
                if ep.protocol != target_proto:
                    raise InvalidRequestError(
                        f"Protocol conflict: request specifies '{target_proto}' "
                        f"but device '{request.device_id}' is configured with '{ep.protocol}'."
                    )
            return AdhocEndpointDTO(
                target=ep.target,
                protocol=target_proto,
                username=ep.username,
                password=ep.password,
                tls_ca=ep.tls_ca,
                tls_cert=ep.tls_cert,
                tls_key=ep.tls_key,
                skip_verify=ep.skip_verify,
                insecure=ep.insecure,
                tls_server_name=ep.tls_server_name,
                tls_version=ep.tls_version,
                ssh_key=ep.ssh_key,
                gnmi_use_tunnel=ep.gnmi_use_tunnel,
                gnmi_target_name=ep.gnmi_target_name,
            )

        raise InvalidRequestError("Cannot resolve endpoint: neither 'device_id' nor 'adhoc_endpoint' was provided.")

