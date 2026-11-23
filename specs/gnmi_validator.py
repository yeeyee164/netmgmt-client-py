# -*- encoding: utf-8 -*-
"""
gnmi_validator.py

Validator for gNMI paths, RFC Section 3.4.5 / 2.7 wildcard & origin rules,
and compiled YANG schema nodes.
"""
from __future__ import annotations

import modules.path as mp
from specs.base_validator import BaseValidator, PathValidationError


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
            # 1. Parse string into gNMI Path (enforces structural XPath rules)
            gnmi_path = mp.parse_path(path_str)
            elems = gnmi_path.elem

            # 2. Enforce gNMI spec Section 3.4.5 and Section 2.7 on Set operations
            if operation.lower() == 'set':
                if prefix:
                    prefix_path = mp.parse_path(prefix)
                    if prefix_path.origin and gnmi_path.origin:
                        raise mp.MalformedXPathError(
                            f"Origin cannot be specified in both prefix ('{prefix_path.origin}') "
                            f"and path ('{gnmi_path.origin}') (RFC Section 2.7)"
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
