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
    Thread-safe registry for managing network device profiles and credentials.

    Serves as the authoritative application-level device inventory. Presentation adapters
    (CLI, Web APIs) and internal services query the registry to resolve logical device
    identifiers (`device_id`) into concrete, validated connection endpoints (`AdhocEndpointDTO`).

    Thread safety is guaranteed using an internal reentrant lock (`threading.RLock`).
    """

    def __init__(self, initial_devices: Optional[Dict[str, AdhocEndpointDTO]] = None):
        """
        Initializes the device registry with an optional dictionary of pre-configured devices.

        Args:
            initial_devices: Optional dictionary mapping device IDs (str) to `AdhocEndpointDTO` instances.
        """
        self._lock = threading.RLock()
        self._devices: Dict[str, AdhocEndpointDTO] = {}
        if initial_devices:
            for dev_id, ep in initial_devices.items():
                self.register_device(dev_id, ep)

    def register_device(self, device_id: str, endpoint: AdhocEndpointDTO) -> None:
        """
        Registers a device profile in the catalog under the specified identifier.

        Args:
            device_id: Unique string identifier for the device (e.g. 'spine-01', 'leaf-02').
            endpoint: Pre-configured AdhocEndpointDTO containing target address, protocol, and credentials.

        Raises:
            InvalidRequestError: If device_id is empty or endpoint is not an AdhocEndpointDTO.
        """
        if not device_id or not isinstance(device_id, str):
            raise InvalidRequestError("device_id must be a non-empty string.")
        if not isinstance(endpoint, AdhocEndpointDTO):
            raise InvalidRequestError("endpoint must be an instance of AdhocEndpointDTO.")

        with self._lock:
            self._devices[device_id] = endpoint

    def unregister_device(self, device_id: str) -> bool:
        """
        Removes a device profile from the catalog by ID.

        Args:
            device_id: Identifier of the device to remove.

        Returns:
            True if the device was found and removed; False if it was not registered.
        """
        with self._lock:
            return self._devices.pop(device_id, None) is not None

    def get_device(self, device_id: str) -> AdhocEndpointDTO:
        """
        Retrieves a registered device profile by ID.

        Args:
            device_id: Identifier of the device to query.

        Returns:
            AdhocEndpointDTO registered for the given device ID.

        Raises:
            DeviceNotFoundError: If the device_id is not registered in the catalog.
        """
        with self._lock:
            endpoint = self._devices.get(device_id)
            if endpoint is None:
                raise DeviceNotFoundError(f"Device '{device_id}' is not registered in the device catalog.")
            return endpoint

    def has_device(self, device_id: str) -> bool:
        """
        Checks whether a device identifier is currently registered.

        Args:
            device_id: Identifier of the device to check.

        Returns:
            True if registered; False otherwise.
        """
        with self._lock:
            return device_id in self._devices

    def list_devices(self) -> List[str]:
        """
        Lists all registered device identifiers in lexicographical order.

        Returns:
            List of sorted device ID strings.
        """
        with self._lock:
            return sorted(list(self._devices.keys()))

    def resolve_endpoint(self, request: ExecutionRequestDTO) -> AdhocEndpointDTO:
        """
        Resolves an execution request into a concrete, fully validated AdhocEndpointDTO.

        Handles both inline ad-hoc targets (`request.adhoc_endpoint`) and catalog devices
        (`request.device_id`), verifying protocol consistency if an explicit protocol override
        is provided on the request.

        Args:
            request: ExecutionRequestDTO containing either adhoc_endpoint or device_id.

        Returns:
            AdhocEndpointDTO ready for session configuration construction.

        Raises:
            InvalidRequestError: If neither or both targets are specified, or if an explicit
                request protocol conflicts with the endpoint/device protocol.
            DeviceNotFoundError: If request specifies a device_id not found in the catalog.
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

