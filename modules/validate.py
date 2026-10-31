"""
validate.py defines a validation class that probably raises an exception
if failed to pass.
"""
from abc import ABC, abstractmethod

from typing import TYPE_CHECKING, Any

import ipaddress
import modules.path as mp

if TYPE_CHECKING:
    from ui.cmd import ParsedConfig

class PathValidationError(Exception):
    """Custom exception raised when a path is invalid for the requested protocol or RPC"""
    pass

class BaseValidator(ABC):

    @abstractmethod
    def validate_path(self, path_str:str, operation:str):
        """
        Validate if a path is legal for the given protocol and operation.
        Must raise PathValidationError if the rules are violated.

        Args:
            `path_str`: a path string will be tested
            `operation`: denotes specific protocols or operations related with `path_str`
        """
        pass

class GNMIValidator(BaseValidator):

    def validate_path(
        self,
        path_str: str,
        operation: str = "",
        prefix: str = "",
        schema_context: object = None,
        profile_id: str = None,
        op_config: object = None,
    ):
        """Validate a path for gNMI requests, checking Section 3.4.5 / 2.7 and compiled schema if provided."""

        try:
            # 1. make str to Path -> it also does path validation
            gnmi_path = mp.parse_path(path_str)
            elems = gnmi_path.elem

            # 2. when the operation is 'Set', enforce Section 3.4.5 and Section 2.7
            if operation.lower() == 'set':
                if prefix:
                    prefix_path = mp.parse_path(prefix)
                    if prefix_path.origin and gnmi_path.origin:
                        raise mp.MalformedXPathError(
                            f"Origin cannot be specified in both prefix ('{prefix_path.origin}') and path ('{gnmi_path.origin}') (RFC Section 2.7)"
                        )

                for elem in elems:
                    if elem.name in ['*', '...'] or '*' in elem.name or '...' in elem.name:
                        raise mp.MalformedXPathError("set operation cannot use wildcards in element name")

                    for key_val in elem.key.values():
                        if key_val in ['*', '...'] or '*' in key_val or '...' in key_val:
                            raise mp.MalformedXPathKeyError("set operation cannot use wildcards as a key")

            # 3. Schema validation if context or profile_id is provided
            if schema_context is not None or profile_id is not None:
                from core.schema_validator import SchemaValidator
                from core.schema_manager import SchemaManager
                if schema_context is not None:
                    SchemaValidator().validate_path(path_str, schema_context, operation=op_config)
                elif profile_id is not None:
                    SchemaManager().validate_path(path_str, profile_id, operation=op_config)

        except Exception as e:
            raise PathValidationError(e) from e


class ValidateConfig:
    @staticmethod
    def validate(cfg: Any):
        """
        method validate validates given config  
        """

        try:
            # validate 'targets'
            for tgt in cfg.targets:
                # "IP:PORT" -> IP, ":", PORT
                ip, separator, port = tgt.rpartition(':')

                if not separator:
                    raise ValueError("wrong address format")
                
                # Strip brackets if it's an IPv6 address literal
                ip = ip.strip('[]')

                ipaddress.ip_address(ip)

                port = int(port)
                if not (1 <= port <= 2**16-1):
                    raise ValueError("port should have a value between 1 to 65535")

            # validate for each subscription
            for se in cfg.sessions:
                # validate given path
                paths = getattr(se, 'paths', []) or []
                for path in paths:
                    data = mp.parse_path(path)
                    if data is None:
                        raise mp.EmptyPathElemNameError

        except Exception as e: #propagate exception
            raise e