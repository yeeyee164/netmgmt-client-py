# -*- encoding: utf-8 -*-
"""
factory.py

It supports common interface for each northbound protocols.

Currently supported common interfaces:
* client picker: `ClientFactory`
* simple validator: `ValidatorFactory`
"""
from specs.gnmi_client import GNMIClient
from specs.netconf_client import NetconfClient
from specs.gnmi_validator import GNMIValidator
from specs.netconf_validator import NetconfValidator
from modules.security import SecurityModule

class ClientFactory:
    """
    Instantiates and returns the appropriate client driver based on the requested protocol.
    """
    @staticmethod
    def get_client(protocol: str, target: str, username: str = "", password: str = "",
                   security=None, **kwargs):
        """
        By calling `get_client` class method, you can instantiate and create a client session
        with `protocol`.

        Args:
            `protocol`: Client for Northbound Protocol. Currently only gNMI is supported.
            `target`: a string formatted of "IP addr:PORT"
            `username`, `password`: authentication via ID/PW
            `security`: `SecurityProfile` from session config dataclass
            `kwargs`: for any parameters that want to pass. NOTE: Each client class only uses it partially.
        """
        security_module = SecurityModule(security) if security else None
        protocol = protocol.lower()

        kwargs = dict(kwargs)
        schema_cfg = kwargs.pop('schema_config', None) or kwargs.pop('schema', None)
        insecure = kwargs.pop('insecure', False)
        device = kwargs.pop('device', 'default')

        if protocol == "gnmi":
            return GNMIClient(target, username, password,
                              security_module,
                              insecure=insecure,
                              schema_config=schema_cfg,
                              **kwargs)
        elif protocol == "netconf":
            return NetconfClient(target, username, password,
                                 security_module,
                                 device=device,
                                 schema_config=schema_cfg,
                                 **kwargs)
        elif protocol == "restconf":
            raise NotImplementedError("RESTCONF client support is coming soon!")
        else:
            raise ValueError(f"Unsupported protocol: '{protocol}'. Available: [gnmi]")

class ValidatorFactory:
    """
    Instantiates and returns the appropriate offline validator based on the
    requested protocol.
    """
    @staticmethod
    def get_validator(protocol: str):
        protocol = protocol.lower()

        if protocol == 'gnmi':
            return GNMIValidator()
        elif protocol == 'netconf':
            return NetconfValidator()
        elif protocol == 'restconf':
            # Offline validation for RESTCONF will be introduced later
            return None
        else:
            raise ValueError(f"Not supported protocol: {protocol}")