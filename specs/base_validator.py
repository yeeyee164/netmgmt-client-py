# -*- encoding: utf-8 -*-
"""
base_validator.py

Abstract base validator contract and shared validation exception for protocol validators.
"""
from abc import ABC, abstractmethod


class PathValidationError(Exception):
    """Custom exception raised when a path is invalid for the requested protocol or RPC."""
    pass


class BaseValidator(ABC):

    @abstractmethod
    def validate_path(self, path_str: str, operation: str):
        """
        Validate if a path is legal for the given protocol and operation.
        Must raise PathValidationError if the rules are violated.

        Args:
            path_str: A path or filter string to validate.
            operation: Protocol operation context for validation.
        """
        pass
