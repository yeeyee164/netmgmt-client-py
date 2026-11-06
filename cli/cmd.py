# -*- encoding: utf-8 -*-
"""
cmd.py - Unified Presentation Facade for netmgmt-client-py.

Provides a backwards-compatible interface for CLI and YAML configuration parsing.
Decomposed into modular submodules under `cli/`:
- `cli.models`: ParsedConfig, ConfigBuilder, FileConfigError
- `cli.normalizers`: resolve_delivery_mode, extract_filter_expression, etc.
- `cli.cli_args`: build_args, subparsers, PairedAction
- `cli.cli_builder`: CLIConfigBuilder
- `cli.file_builder`: FileConfigBuilder, parse_operation_item
- `cli.adapters`: session_to_execution_request, execution_request_to_session
"""
import argparse
from typing import Any, Dict, List, Optional, Tuple, Type

# Re-exported domain models for backwards compatibility
from config.delivery import (
    DeliveryMode,
    DeliveryPolicy,
)
from config.model import (
    ConnectionConfig,
    ExecutionConfig,
    OutputConfig,
    OutputFormat,
    OutputType,
    Protocol,
    SessionConfig,
)
from config.operations import (
    CapabilitiesOperation,
    Change,
    ChangeType,
    EditConfigOperation,
    GetConfigOperation,
    GetOperation,
    GetSchemaOperation,
    NetconfTransactionOperation,
    OperationConfig,
    SetOperation,
    SubscribeOperation,
    TransactionType,
)
from config.protocol_options import (
    GNMIOptions,
    NetconfOptions,
)
from config.schema import SchemaConfig
from config.selectors import (
    FilterSelector,
    PathSelector,
    Selector,
)
from modules.security import SecurityProfile
from util.utils import read_payload

# Submodule exports
from cli.models import (
    ConfigBuilder,
    FileConfigError,
    ParsedConfig,
)
from cli.normalizers import (
    _RPC_MODE_TO_DELIVERY,
    _STREAM_SUB_MODE_TO_DELIVERY,
    _normalize_delete_entries,
    _normalize_mode_token,
    _normalize_mutation_entries,
    _parse_schema_config,
    extract_filter_expression,
    resolve_delivery_mode,
)
from cli.cli_args import (
    PairedAction,
    _parse_cli_mutation_flags,
    _parse_paired_options,
    add_schema_args,
    build_args,
    gnmi_args,
    netconf_args,
)
from cli.adapters import (
    execution_request_to_session,
    session_to_execution_request,
)
from cli.cli_builder import CLIConfigBuilder
from cli.file_builder import (
    FileConfigBuilder,
    parse_operation_item,
)


def config_builder(args: Any) -> ParsedConfig:
    """Builds an appropriate `ParsedConfig` class from CLI arguments or YAML config."""
    config_file = getattr(args, 'config', '') or getattr(args, 'global_config', '')
    if config_file and (getattr(args, 'protocol', None) is None or getattr(args, 'operation', None) is None):
        protocol = getattr(args, 'protocol', 'unknown')
        return FileConfigBuilder(config_file, protocol=protocol).build()
    else:
        return CLIConfigBuilder(args).build()


def parse_args(args: Optional[List[str]] = None) -> ParsedConfig:
    """Helper to parse raw argument list and build ParsedConfig directly."""
    return config_builder(build_args(args))
