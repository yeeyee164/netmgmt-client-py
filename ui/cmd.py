# -*- encoding: utf-8 -*-
"""
    cmd.py

    `cmd.py` defines a user interface for the system command lines.
    It has a DataClass named `ParsedConfig` which 'normalizes' information
    given by interfaces. 

    `ParsedConfig` should contain at least one `SessionConfig` which denotes client session.
    `SessionConfig` will be created per each target.
"""
import argparse
import json
import time
from dataclasses import dataclass, field, fields
from typing import List, Optional, Dict, Tuple, Type, Any
from abc import ABC, abstractmethod

from modules.security import SecurityProfile
from util.utils import read_payload
from config.model import (
    Protocol,
    OutputType,
    OutputFormat,
    ConnectionConfig,
    ExecutionConfig,
    SessionConfig,
    OutputConfig,
)
from config.schema import SchemaConfig
from config.selectors import (
    Selector,
    PathSelector,
    FilterSelector,
)
from config.delivery import (
    DeliveryMode,
    DeliveryPolicy,
)
from config.operations import (
    OperationConfig,
    CapabilitiesOperation,
    GetSchemaOperation,
    GetOperation,
    GetConfigOperation,
    ChangeType,
    Change,
    SetOperation,
    EditConfigOperation,
    SubscribeOperation,
    NetconfTransactionOperation,
    TransactionType,
)
from config.protocol_options import (
    GNMIOptions,
    NetconfOptions,
)

class FileConfigError(Exception):
    """Custom exception raised when given config vlolates some criteria"""
    pass

# =====================================================================
# Interface vocabulary -> protocol-neutral DeliveryMode translation
# =====================================================================

# Subscription-level modes (CLI `--mode`, YAML `subscriptions.<name>.mode`)
_RPC_MODE_TO_DELIVERY: Dict[str, DeliveryMode] = {
    "once": DeliveryMode.SNAPSHOT,
    "poll": DeliveryMode.ON_DEMAND,
}
# Per-path stream modes used only when the subscription-level mode is "stream"
# (CLI `--sub-mode`, YAML `subscriptions.<name>.subscription.mode`)
_STREAM_SUB_MODE_TO_DELIVERY: Dict[str, DeliveryMode] = {
    "sample": DeliveryMode.PERIODIC,
    "on_change": DeliveryMode.EVENT_DRIVEN,
    "target_defined": DeliveryMode.SERVER_DETERMINED,
}


def _normalize_mode_token(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_") if value is not None else ""


def extract_filter_expression(raw_filter: Any, default_type: Optional[str] = None) -> tuple[str, str]:
    """Extracts filter expression and filter type from string or dictionary YAML input."""
    if isinstance(raw_filter, dict):
        expr = raw_filter.get("select") or raw_filter.get("expression") or raw_filter.get("filter") or ""
        filter_str = read_payload(expr) if isinstance(expr, str) else str(expr)
        f_type = raw_filter.get("type") or default_type or ("subtree" if filter_str.strip().startswith("<") else "xpath")
        return filter_str, f_type
    filter_str = read_payload(raw_filter) if isinstance(raw_filter, str) else str(raw_filter)
    f_type = default_type or ("subtree" if filter_str.strip().startswith("<") else "xpath")
    return filter_str, f_type



def resolve_delivery_mode(rpc_mode: Optional[str],
                          sub_mode: Optional[str] = None,
                          default_sub_mode: str = "sample") -> DeliveryMode:
    """Translate CLI/YAML subscription modes into a protocol-neutral `DeliveryMode`.

    Args:
        rpc_mode: subscription-level mode ("once" | "poll" | "stream"). Empty means "stream".
            A neutral `DeliveryMode` value (e.g. "on_demand") is also accepted.
        sub_mode: stream mode ("sample" | "on_change" | "target_defined"); only used
            when `rpc_mode` is "stream". A neutral `DeliveryMode` value is also accepted.
        default_sub_mode: stream mode applied when `sub_mode` is empty.

    Raises:
        ValueError: if either mode cannot be translated.
    """
    rpc = _normalize_mode_token(rpc_mode) or "stream"
    if rpc in _RPC_MODE_TO_DELIVERY:
        return _RPC_MODE_TO_DELIVERY[rpc]

    if rpc == "stream":
        sub = _normalize_mode_token(sub_mode) or _normalize_mode_token(default_sub_mode)
        if sub in _STREAM_SUB_MODE_TO_DELIVERY:
            return _STREAM_SUB_MODE_TO_DELIVERY[sub]
        token, valid = sub, sorted(_STREAM_SUB_MODE_TO_DELIVERY)
    else:
        token, valid = rpc, sorted(list(_RPC_MODE_TO_DELIVERY) + ["stream"])

    try:
        return DeliveryMode(token)  # neutral vocabulary passthrough
    except ValueError:
        raise ValueError(
            f"Unsupported subscription mode '{token}'. "
            f"Must be one of {valid} or a delivery mode {[m.value for m in DeliveryMode]}."
        ) from None


def _parse_schema_config(data: Any, fallback_dict: Optional[Dict[str, Any]] = None) -> SchemaConfig:
    """
    Parses a schema mapping from YAML/dict into a SchemaConfig instance.

    Args:
        data: part of parsed YAML data
        fallback_dict: fallback dictionary in case of `data` is incomplete
    
    Returns:
        `SchemaConfig`
    
    Currently supported example of YAML Format
    ------------------------------------------

    ```yaml
    schema:
      enabled: true
      profile_id: "srlinux"
      search_dirs:
        - "./path/to/yang"
      modules:
        - "openconfig-interfaces"
        - name: "openconfig-system"
          revision: "2025-12-12"
          features:
            - "telemetry"
    ```
    """
    schema_map: Dict[str, Any] = {}
    if fallback_dict and isinstance(fallback_dict, dict):
        if 'schema' in fallback_dict and isinstance(fallback_dict['schema'], dict):
            schema_map.update(fallback_dict['schema'])
        else:
            for k in (
                'yang_dirs', 'yang_dir', 'cache_dir', 'yang_cache_dir',
                'validate', 'enable_schema_validation', 'auto_discovery',
                'auto_fetch', 'profile', 'device_profile'
            ):
                if k in fallback_dict:
                    schema_map[k] = fallback_dict[k]

    if isinstance(data, dict):
        if 'schema' in data and isinstance(data['schema'], dict):
            schema_map.update(data['schema'])
        else:
            schema_map.update(data)

    yang_dirs = schema_map.get('yang_dirs', schema_map.get('yang_dir', []))
    if isinstance(yang_dirs, str):
        yang_dirs = [yang_dirs]
    elif not isinstance(yang_dirs, (list, tuple)):
        yang_dirs = []

    cache_dir = schema_map.get('cache_dir', schema_map.get('yang_cache_dir'))
    validate = schema_map.get('validate', schema_map.get('enable_schema_validation', False))
    auto_discovery = schema_map.get('auto_discovery', True)
    auto_fetch = schema_map.get('auto_fetch', True)
    profile = schema_map.get('profile', schema_map.get('device_profile'))

    return SchemaConfig(
        yang_dirs=tuple(yang_dirs),
        cache_dir=cache_dir,
        validate=bool(validate),
        auto_discovery=bool(auto_discovery),
        auto_fetch=bool(auto_fetch),
        profile=profile,
    )

# =====================================================================
# Root config data class per incoming config
# =====================================================================

@dataclass
class ParsedConfig:
    """
        For all classes defined in `cmd.py`, it eventually returns
        this class.

        And our management will use that for configure.
    """
    sessions: List[SessionConfig] = field(default_factory=list)  # List of all individual sessions to spawn
    targets: List[str] = field(default_factory=list)  # List of "IP:PORT" strings
    outputs: List[OutputConfig] = field(default_factory=list)  # Outputs definitions
    debug: bool = False           # Global debug flag

    # for logging this script
    log_level: str = "ERROR"
    syslog_server: str = ""
    log_file: str = ""

    # Application Service Layer DTOs & Device Inventory
    requests: List[Any] = field(default_factory=list)
    device_registry: Optional[Any] = None

    def __post_init__(self):
        if self.sessions and not self.requests:
            for s in self.sessions:
                try:
                    self.requests.append(session_to_execution_request(s))
                except Exception:
                    pass
        elif self.requests and not self.sessions:
            for r in self.requests:
                try:
                    self.sessions.append(execution_request_to_session(r, registry=self.device_registry))
                except Exception:
                    pass

    def __str__(self):
        session_strs = "\n".join([str(s) for s in self.sessions])
        return f"""ParsedConfig(debug={self.debug}, outputs={self.outputs},
sessions=[\n{session_strs}\n]
)
"""

class ConfigBuilder(ABC):
    @abstractmethod
    def build(self) -> ParsedConfig:
        """Parse input and return `ParsedConfig` object"""
        pass

class PairedAction(argparse.Action):
    def __init__(self, option_strings, dest, group_type=None, kind=None, **kwargs):
        self.group_type = group_type
        self.kind = kind
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        items = getattr(namespace, self.dest, None)
        if items is None:
            items = []
            setattr(namespace, self.dest, items)
        items.append(values)

        order_attr = f"_{self.group_type}_order"
        order_list = getattr(namespace, order_attr, None)
        if order_list is None:
            order_list = []
            setattr(namespace, order_attr, order_list)
        order_list.append((self.kind, values))


def _parse_paired_options(args, group_type: str) -> List[Tuple[str, str]]:
    r"""
    Parses paired options (--\<group\>-path with --\<group\>-value or --\<group\>-file).
    - \<group\> is either `update` or `replace`

    Supports both interleaved order and batch lists, validating that every path
    is strictly paired with exactly one value or file.
    """
    order_attr = f"_{group_type}_order"
    order_list = getattr(args, order_attr, None)

    results: List[Tuple[str, str]] = []
    if order_list:
        current_path = None
        for kind, val in order_list:
            if kind == 'path':
                if current_path is not None:
                    raise ValueError(f"Each --{group_type}-path must be paired with either a --{group_type}-value or --{group_type}-file (unpaired path: '{current_path}')")
                val_clean = val.strip()
                if not val_clean:
                    raise ValueError(f"--{group_type}-path cannot be empty")
                current_path = val_clean
            elif kind == 'value':
                if current_path is None:
                    raise ValueError(f"--{group_type}-value must be preceded by --{group_type}-path")
                results.append((current_path, val.strip()))
                current_path = None
            elif kind == 'file':
                if current_path is None:
                    raise ValueError(f"--{group_type}-file must be preceded by --{group_type}-path")
                file_val = val.strip()
                if not file_val:
                    raise ValueError(f"--{group_type}-file cannot be empty")
                if not file_val.startswith('@'):
                    file_val = f"@{file_val}"
                results.append((current_path, file_val))
                current_path = None

        if current_path is not None:
            raise ValueError(f"Each --{group_type}-path must be paired with either a --{group_type}-value or --{group_type}-file (unpaired path: '{current_path}')")
    else:
        paths = getattr(args, f"{group_type}_path", []) or []
        values = getattr(args, f"{group_type}_value", []) or []
        files = getattr(args, f"{group_type}_file", []) or []
        if isinstance(paths, str): paths = [paths]
        if isinstance(values, str): values = [values]
        if isinstance(files, str): files = [files]

        if paths:
            total_val_files = len(values) + len(files)
            if len(paths) != total_val_files:
                raise ValueError(f"Mismatched paired options: {len(paths)} --{group_type}-path options provided, but {total_val_files} values/files given")

            if len(values) == len(paths) and not files:
                for p, v in zip(paths, values):
                    results.append((p.strip(), v.strip()))
            elif len(files) == len(paths) and not values:
                for p, f in zip(paths, files):
                    file_val = f.strip()
                    if not file_val.startswith('@'):
                        file_val = f"@{file_val}"
                    results.append((p.strip(), file_val))
            else:
                val_idx = 0
                file_idx = 0
                for p in paths:
                    if val_idx < len(values):
                        results.append((p.strip(), values[val_idx].strip()))
                        val_idx += 1
                    elif file_idx < len(files):
                        file_val = files[file_idx].strip()
                        if not file_val.startswith('@'):
                            file_val = f"@{file_val}"
                        results.append((p.strip(), file_val))
                        file_idx += 1
        elif values or files:
            raise ValueError(f"--{group_type}-value or --{group_type}-file provided without preceding --{group_type}-path")

    return results


def _parse_cli_mutation_flags(args: Any, group_type: str) -> List[Tuple[str, str]]:
    """Parses CLI mutation options (--update/--replace and paired options)."""
    results: List[Tuple[str, str]] = []
    raw_list = getattr(args, f"{group_type}s", []) or getattr(args, group_type, []) or []
    for item in raw_list:
        if isinstance(item, tuple):
            results.append(item)
        elif isinstance(item, str):
            if ':::' in item:
                p, v = item.split(':::', 1)
                results.append((p.strip(), v.strip()))
            elif '=' in item and not item.endswith(']'):
                p, v = item.split('=', 1)
                results.append((p.strip(), v.strip()))
            else:
                results.append((item.strip(), ""))
    results.extend(_parse_paired_options(args, group_type))
    return results


class CLIConfigBuilder(ConfigBuilder):
    """
    Parsing all command-line arguments via argparse.
    See '--help' strings to inspect this code.
    """

    def __init__(self, args):
        self.args = args
    
    def build(self) -> ParsedConfig:
        sessions = []

        # check general configuration

        times = self.args.times
        if times <= 0:
            print(f"[Config] times option should be positive integer. Ignore given value")
            times = 1

        if not getattr(self.args, 'protocol', None) or not getattr(self.args, 'operation', None):
            print("[CLI] Error: You must specify a protocol and an operation, or use a --config file.")
            return ParsedConfig(sessions=[], debug=self.args.debug)

        targets = self.args.target or []
        protocol = self.args.protocol.lower()
        operation = self.args.operation.lower()

        for target in targets:
            security_profile = SecurityProfile(
                tls_ca=self.args.tls_ca,
                tls_cert=self.args.tls_cert,
                tls_key=self.args.tls_key,
                skip_verify=self.args.skip_verify,
                tls_server_name=self.args.tls_server_name,
                tls_version=self.args.tls_version,
                ssh_key=self.args.ssh_key
            )

            paths = getattr(self.args, 'path', []) or []
            if isinstance(paths, str):
                paths = [paths]

            conn = ConnectionConfig(
                target=target,
                username=self.args.username or "",
                password=self.args.password or "",
                security=security_profile,
                insecure=getattr(self.args, 'insecure', False)
            )

            exec_cfg = ExecutionConfig(
                times=times
            )

            paths = getattr(self.args, 'path', []) or []
            if isinstance(paths, str):
                paths = [paths]

            prefix = getattr(self.args, "prefix", "")

            if protocol == 'gnmi':
                proto_enum = Protocol.GNMI
                if operation in ('capability', 'capabilities'):
                    op_obj = CapabilitiesOperation()
                elif operation == 'get':
                    encoding = getattr(self.args, "encoding", "json_ietf")
                    data_type = getattr(self.args, "type", '') or getattr(self.args, "data_type", '') or 'all'
                    op_obj = GetOperation(
                        selector=PathSelector(paths=tuple(paths), prefix=prefix),
                        read_scope=data_type,
                        protocol_options=GNMIOptions(encoding=encoding)
                    )
                elif operation == 'set':
                    changes = []
                    gnmi_updates = _parse_cli_mutation_flags(self.args, 'update')
                    gnmi_replaces = _parse_cli_mutation_flags(self.args, 'replace')

                    raw_deletes = getattr(self.args, "delete", []) or getattr(self.args, "deletes", []) or []
                    if isinstance(raw_deletes, str):
                        raw_deletes = [raw_deletes]
                    gnmi_deletes = [d.strip() for d in raw_deletes if d]

                    for p, v in gnmi_updates:
                        changes.append(Change(path=p, operation=ChangeType.MERGE, value=v))
                    for p, v in gnmi_replaces:
                        changes.append(Change(path=p, operation=ChangeType.REPLACE, value=v))
                    for d in gnmi_deletes:
                        changes.append(Change(path=d, operation=ChangeType.DELETE, value=None))

                    encoding = getattr(self.args, "encoding", "json_ietf")
                    op_obj = SetOperation(
                        changes=tuple(changes),
                        prefix=prefix,
                        protocol_options=GNMIOptions(encoding=encoding)
                    )
                elif operation in ('subscribe', 'stream', 'once', 'poll'):
                    mode_str = getattr(self.args, "mode", "stream").lower()
                    sub_mode_str = str(getattr(self.args, "sub_mode", getattr(self.args, "stream_mode", "sample"))).lower()
                    del_mode = resolve_delivery_mode(mode_str, sub_mode_str)

                    sample_interval = getattr(self.args, "sample_interval", getattr(self.args, "interval", 0))
                    del_policy = DeliveryPolicy(
                        mode=del_mode,
                        interval=sample_interval,
                        heartbeat=0,
                        suppress_redundant=False
                    )
                    encoding = getattr(self.args, "encoding", "json_ietf")
                    updates_only = getattr(self.args, "update_only", False) or getattr(self.args, "updates_only", False)
                    op_obj = SubscribeOperation(
                        selector=PathSelector(paths=tuple(paths), prefix=prefix),
                        delivery=del_policy,
                        subscription_name="cli_execution",
                        protocol_options=GNMIOptions(encoding=encoding, updates_only=updates_only)
                    )
                else:
                    raise ValueError(f"Unknown gNMI operation: {operation}")

            elif protocol == 'netconf':
                proto_enum = Protocol.NETCONF
                if operation in ('capability', 'capabilities'):
                    op_obj = CapabilitiesOperation()
                elif operation in ('get', 'get-config'):
                    raw_filter = read_payload(getattr(self.args, "filter", ""))
                    nc_xpath = getattr(self.args, "nc_xpath", []) or []
                    if raw_filter:
                        filter_type = "subtree" if raw_filter.strip().startswith('<') else "xpath"
                        selector = FilterSelector(expression=raw_filter, filter_type=filter_type)
                    elif nc_xpath:
                        selector = PathSelector(paths=tuple(nc_xpath))
                    elif paths:
                        selector = PathSelector(paths=tuple(paths))
                    else:
                        selector = PathSelector(paths=())

                    source = getattr(self.args, "source", "") or "running"
                    read_scope = "config" if operation == "get-config" else "all"
                    op_obj = GetOperation(
                        selector=selector,
                        read_scope=read_scope,
                        protocol_options=NetconfOptions(source=source)
                    )
                elif operation in ('get-schema', 'get_schema'):
                    identifier = getattr(self.args, "identifier", "")
                    if not identifier and paths:
                        identifier = paths[0]
                    version = getattr(self.args, "version", "")
                    schema_format = getattr(self.args, "schema_format", "yang")
                    op_obj = GetSchemaOperation(
                        identifier=identifier,
                        version=version or None,
                        format=schema_format
                    )
                elif operation in ('edit-config', 'edit_config', 'set'):
                    raw_cfg = getattr(self.args, "config", "") or getattr(self.args, "nc_config", "")
                    config_payload = read_payload(raw_cfg)
                    target_ds = getattr(self.args, "target_datastore", None) or getattr(self.args, "target", "candidate")
                    default_op = getattr(self.args, "default_operation", "merge")
                    error_opt = getattr(self.args, "error_option", "stop-on-error")
                    test_opt = getattr(self.args, "test_option", None)
                    commit_val = getattr(self.args, "commit", True)
                    lock_val = getattr(self.args, "lock_target", False) or getattr(self.args, "lock", False)
                    val_candidate = getattr(self.args, "validate_candidate", False) or getattr(self.args, "validate", False)
                    confirmed_val = getattr(self.args, "confirmed", False)
                    confirm_timeout_val = getattr(self.args, "confirm_timeout", None)
                    persist_val = getattr(self.args, "persist", "")
                    persist_id_val = getattr(self.args, "persist_id", "")

                    nc_opts = NetconfOptions(
                        target_datastore=target_ds,
                        config=config_payload,
                        default_operation=default_op,
                        error_option=error_opt,
                        test_option=test_opt,
                        commit=commit_val,
                        lock_target=bool(lock_val),
                        validate_candidate=bool(val_candidate),
                        confirmed=bool(confirmed_val),
                        confirm_timeout=confirm_timeout_val,
                        persist=persist_val,
                        persist_id=persist_id_val,
                    )
                    op_obj = SetOperation(
                        changes=(),
                        protocol_options=nc_opts
                    )
                elif operation in ('commit', 'cancel-commit', 'cancel_commit', 'discard-changes', 'discard_changes'):
                    confirmed_val = getattr(self.args, "confirmed", False)
                    confirm_timeout_val = getattr(self.args, "confirm_timeout", None)
                    persist_val = getattr(self.args, "persist", "")
                    persist_id_val = getattr(self.args, "persist_id", "")

                    if operation == 'commit':
                        tx_type = TransactionType.COMMIT
                    elif operation in ('cancel-commit', 'cancel_commit'):
                        tx_type = TransactionType.CANCEL_COMMIT
                    else:
                        tx_type = TransactionType.DISCARD_CHANGES

                    op_obj = NetconfTransactionOperation(
                        operation=tx_type,
                        confirmed=bool(confirmed_val),
                        confirm_timeout=confirm_timeout_val,
                        persist=persist_val,
                        persist_id=persist_id_val,
                    )
                elif operation == 'subscribe':
                    raw_filter = read_payload(getattr(self.args, "filter", ""))
                    nc_xpath = getattr(self.args, "nc_xpath", []) or []
                    if raw_filter:
                        filter_type = "subtree" if raw_filter.strip().startswith('<') else "xpath"
                        selector = FilterSelector(expression=raw_filter, filter_type=filter_type)
                    elif nc_xpath:
                        selector = PathSelector(paths=tuple(nc_xpath))
                    elif paths:
                        selector = PathSelector(paths=tuple(paths))
                    else:
                        selector = PathSelector(paths=())

                    stream_name = getattr(self.args, "stream_name", "NETCONF") or "NETCONF"
                    start_time = getattr(self.args, "start_time", None)
                    stop_time = getattr(self.args, "stop_time", None)
                    poll_timeout = float(getattr(self.args, "poll_timeout", 1.0) or 1.0)

                    nc_opts = NetconfOptions(
                        stream_name=stream_name,
                        start_time=start_time,
                        stop_time=stop_time,
                        poll_timeout=poll_timeout,
                    )
                    op_obj = SubscribeOperation(
                        selector=selector,
                        delivery=DeliveryPolicy(mode=DeliveryMode.EVENT_DRIVEN),
                        subscription_name=stream_name,
                        protocol_options=nc_opts,
                    )
                else:
                    raise ValueError(f"Unknown NETCONF operation: {operation}")
            else:
                raise ValueError(f"Unknown protocol: {protocol}")

            yang_dirs = getattr(self.args, 'yang_dir', None) or []
            if isinstance(yang_dirs, str):
                yang_dirs = [yang_dirs]
            yang_cache_dir = getattr(self.args, 'yang_cache_dir', None) or None
            validate_schema = getattr(self.args, 'validate_schema', False)
            auto_discovery = not getattr(self.args, 'no_auto_discovery', False)
            auto_fetch = not getattr(self.args, 'no_auto_fetch', False)
            schema_profile = getattr(self.args, 'schema_profile', None) or None

            schema_cfg = SchemaConfig(
                yang_dirs=tuple(yang_dirs),
                cache_dir=yang_cache_dir,
                validate=bool(validate_schema),
                auto_discovery=bool(auto_discovery),
                auto_fetch=bool(auto_fetch),
                profile=schema_profile,
            )

            session = SessionConfig(
                connection=conn,
                protocol=proto_enum,
                operation=op_obj,
                execution=exec_cfg,
                schema=schema_cfg,
            )
            sessions.append(session)

        output_type = self.args.output_type or OutputType.STDOUT
        output_format = self.args.output_format or OutputFormat.JSON
        output_path = None

        if output_type == OutputType.FILE:
            output_path = self.args.output_path

        outputs = [OutputConfig(
            output_type = output_type,
            format = output_format,
            path = output_path,
        )]

        return ParsedConfig(
            sessions=sessions,
            outputs=outputs,
            targets=targets,
            debug=self.args.debug,
            log_level=self.args.log_level,
            syslog_server=self.args.syslog_server,
            log_file=self.args.log_file,
        )

def parse_operation_item(op_item: Any, protocol: Protocol = Protocol.NETCONF) -> OperationConfig:
    """
    Parses a single operation entry from YAML into a typed OperationConfig.

    Args:
        op_item: data of 'operations' key in YAML
        protocol: currently supported protocol defined at `config/model.py`
    
    Raises:
        FileConfigError: if given operation in `operations` does not support, or invalid data given

    Returns:
        CapabilitiesOperation: When `operations` contains capability/capabilities operation 
        GetSchemaOperation: When `operations` contains get-schema(NETCONF) operation 
        GetOperation: When `operations` contains get operation 
        GetConfigOperation: When `operations` contains get-config(NETCONF) operation 
        SetOperation: When `operations` contains set(gNMI) operation 
        EditConfigOperation: When `operations` contains edit-config(NETCONF) operation 
        NetconfTransactionOperation: When `operations` contains (un)lock, commit, cancel-commit, discard-changes (NETCONF) opeartion

    Note:
        - Mapping syntax: {"get-schema": {"identifier": "openconfig-interfaces", ...}}
        - Object syntax: {"operation": "get-schema", "identifier": "openconfig-interfaces", ...}
        - Direct OperationConfig instances.
    """
    if isinstance(op_item, OperationConfig):
        return op_item

    if not isinstance(op_item, dict):
        raise FileConfigError(f"Operation entry must be a dictionary, got {type(op_item).__name__}: {op_item}")

    if "operation" in op_item or "type" in op_item:
        op_name = str(op_item.get("operation") or op_item.get("type"))
        op_params = {k: v for k, v in op_item.items() if k not in ("operation", "type")}
    elif len(op_item) == 1:
        op_name = list(op_item.keys())[0]
        params_val = op_item[op_name]
        op_params = params_val if isinstance(params_val, dict) else {}
    else:
        raise FileConfigError(
            f"Invalid operation structure: {op_item}. "
            "Must be either a single-key mapping (e.g. {'get-schema': {...}}) "
            "or specify 'operation': '<type>'."
        )

    op_norm = op_name.strip().lower().replace("_", "-")

    if op_norm in ("capability", "capabilities"):
        return CapabilitiesOperation()

    elif op_norm == "get-schema":
        ident = op_params.get("identifier")
        if not ident:
            raise FileConfigError("Missing mandatory parameter 'identifier' for get-schema operation")
        version = op_params.get("version")
        format_val = op_params.get("format", "yang") or "yang"
        return GetSchemaOperation(
            identifier=str(ident),
            version=str(version) if version is not None else None,
            format=str(format_val)
        )

    elif op_norm == "get-config":
        source = op_params.get("source", "running")
        raw_filter = op_params.get("filter", "")
        filter_type = op_params.get("filter_type")
        paths = op_params.get("paths", op_params.get("path", []))
        if isinstance(paths, str):
            paths = [paths]

        if raw_filter:
            filter_str, f_type = extract_filter_expression(raw_filter, default_type=filter_type)
            selector = FilterSelector(expression=filter_str, filter_type=f_type)
        elif paths:
            selector = PathSelector(paths=tuple(paths), prefix=op_params.get("prefix", ""))
        else:
            selector = PathSelector(paths=())

        nc_opts = NetconfOptions(source=source)
        return GetConfigOperation(
            selector=selector,
            read_scope="config",
            protocol_options=nc_opts
        )

    elif op_norm == "get":
        source = op_params.get("source", "running")
        raw_filter = op_params.get("filter", "")
        filter_type = op_params.get("filter_type")
        paths = op_params.get("paths", op_params.get("path", []))
        if isinstance(paths, str):
            paths = [paths]

        if raw_filter:
            filter_str, f_type = extract_filter_expression(raw_filter, default_type=filter_type)
            selector = FilterSelector(expression=filter_str, filter_type=f_type)
        elif paths:
            selector = PathSelector(paths=tuple(paths), prefix=op_params.get("prefix", ""))
        else:
            selector = PathSelector(paths=())

        read_scope = op_params.get("type", op_params.get("read_scope", "all"))
        if protocol == Protocol.NETCONF:
            p_opts = NetconfOptions(source=source)
        else:
            p_opts = GNMIOptions(encoding=op_params.get("encoding", "json_ietf"))

        return GetOperation(
            selector=selector,
            read_scope=read_scope,
            protocol_options=p_opts
        )

    elif op_norm in ("edit-config", "set"):
        is_gnmi_set = (
            protocol == Protocol.GNMI
            or any(k in op_params for k in ("update", "replace", "delete"))
        ) and not any(k in op_params for k in ("nc_config", "target_datastore", "target"))

        if is_gnmi_set:
            prefix = op_params.get("prefix", "")
            changes_list = []

            def _parse_mutation(item, op_type: ChangeType) -> Change:
                if isinstance(item, str):
                    if ":::" in item:
                        p, v = item.split(":::", 1)
                        return Change(path=p.strip(), operation=op_type, value=v.strip())
                    else:
                        raise FileConfigError(
                            f"Mutation string item '{item}' must follow 'path:::value' notation"
                        )
                elif isinstance(item, dict):
                    path_val = item.get("path")
                    val = item.get("val", item.get("value"))
                    type_val = item.get("type")

                    # If value not provided, check if path contains ":::" delimiter
                    if val is None and path_val and ":::" in str(path_val):
                        p, v = str(path_val).split(":::", 1)
                        return Change(path=p.strip(), operation=op_type, value=v.strip(), type=type_val)

                    if val is None:
                        raise FileConfigError(
                            f"Missing value for mutation path '{path_val}'. "
                            "Provide 'val' / 'value' or use 'path:::value' syntax."
                        )
                    if not path_val:
                        raise FileConfigError(f"Missing path in mutation item: {item}")

                    return Change(path=path_val, operation=op_type, value=val, type=type_val)
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    return Change(path=item[0], operation=op_type, value=item[1])
                else:
                    raise FileConfigError(f"Invalid mutation item structure: {item}")

            # 1. delete
            del_items = op_params.get("delete", [])
            if isinstance(del_items, str):
                del_items = [del_items]
            for d in del_items:
                if isinstance(d, str):
                    p = d.split(":::", 1)[0].strip() if ":::" in d else d.strip()
                    changes_list.append(Change(path=p, operation=ChangeType.DELETE))
                elif isinstance(d, dict) and "path" in d:
                    p = str(d["path"]).split(":::", 1)[0].strip() if ":::" in str(d["path"]) else str(d["path"]).strip()
                    changes_list.append(Change(path=p, operation=ChangeType.DELETE))
                else:
                    raise FileConfigError(f"Invalid delete item structure: {d}")

            # 2. update (merge)
            upd_items = op_params.get("update", [])
            if isinstance(upd_items, (str, dict)) and not isinstance(upd_items, list):
                upd_items = [upd_items]
            for u in upd_items:
                changes_list.append(_parse_mutation(u, ChangeType.MERGE))

            # 3. replace
            rep_items = op_params.get("replace", [])
            if isinstance(rep_items, (str, dict)) and not isinstance(rep_items, list):
                rep_items = [rep_items]
            for r in rep_items:
                changes_list.append(_parse_mutation(r, ChangeType.REPLACE))

            gnmi_opts = GNMIOptions(encoding=op_params.get("encoding", "json_ietf"))
            return SetOperation(
                changes=tuple(changes_list),
                prefix=prefix,
                protocol_options=gnmi_opts
            )

        target_ds = op_params.get("target_datastore") or op_params.get("target", "candidate")
        raw_cfg = op_params.get("config") or op_params.get("nc_config")
        cfg_payload = read_payload(raw_cfg) if isinstance(raw_cfg, str) and raw_cfg else (raw_cfg or "")
        def_op = op_params.get("default_operation", "merge")
        err_opt = op_params.get("error_option", "stop-on-error")
        test_opt = op_params.get("test_option")
        commit_val = op_params.get("commit", True)
        lock_val = op_params.get("lock_target") or op_params.get("lock", False)
        val_candidate = op_params.get("validate_candidate") or op_params.get("validate", False)
        confirmed_val = op_params.get("confirmed", False)
        confirm_timeout_val = op_params.get("confirm_timeout")
        persist_val = op_params.get("persist", "")
        persist_id_val = op_params.get("persist_id", "")

        nc_opts = NetconfOptions(
            target_datastore=target_ds,
            config=cfg_payload if cfg_payload else None,
            default_operation=def_op,
            error_option=err_opt,
            test_option=test_opt,
            commit=commit_val,
            lock_target=bool(lock_val),
            validate_candidate=bool(val_candidate),
            confirmed=bool(confirmed_val),
            confirm_timeout=confirm_timeout_val,
            persist=persist_val,
            persist_id=persist_id_val,
        )
        return EditConfigOperation(
            changes=(),
            protocol_options=nc_opts
        )

    elif op_norm in ("lock", "unlock", "commit", "cancel-commit", "cancel_commit", "discard-changes", "discard_changes", "validate"):
        target_ds = op_params.get("target_datastore") or op_params.get("target", "candidate")
        source_ds = op_params.get("source_datastore") or op_params.get("source", "candidate")
        confirmed_val = op_params.get("confirmed", False)
        confirm_timeout_val = op_params.get("confirm_timeout")
        persist_val = op_params.get("persist", "")
        persist_id_val = op_params.get("persist_id", "")

        if op_norm == "lock":
            tx_type = TransactionType.LOCK
        elif op_norm == "unlock":
            tx_type = TransactionType.UNLOCK
        elif op_norm == "commit":
            tx_type = TransactionType.COMMIT
        elif op_norm in ("cancel-commit", "cancel_commit"):
            tx_type = TransactionType.CANCEL_COMMIT
        elif op_norm in ("discard-changes", "discard_changes"):
            tx_type = TransactionType.DISCARD_CHANGES
        elif op_norm == "validate":
            tx_type = TransactionType.VALIDATE
        else:
            raise FileConfigError(f"Unsupported transaction operation: {op_name}")

        return NetconfTransactionOperation(
            operation=tx_type,
            target_datastore=target_ds,
            source_datastore=source_ds,
            confirmed=bool(confirmed_val),
            confirm_timeout=confirm_timeout_val,
            persist=persist_val,
            persist_id=persist_id_val,
        )

    else:
        raise FileConfigError(
            f"Unsupported operation type '{op_name}'. Available: [capability, get, get-config, get-schema, edit-config, set, lock, unlock, commit, cancel-commit, discard-changes, validate]"
        )


def _normalize_mutation_entries(raw_input: Any) -> List[Tuple[str, str]]:
    """Normalizes dict, list of tuples, list of strings, or scalar mutation entries."""
    if not raw_input:
        return []
    if isinstance(raw_input, dict):
        return [(str(k), str(v)) for k, v in raw_input.items()]
    items = raw_input if isinstance(raw_input, list) else [raw_input]
    results: List[Tuple[str, str]] = []
    for item in items:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            results.append((str(item[0]), str(item[1])))
        elif isinstance(item, dict):
            results.extend((str(k), str(v)) for k, v in item.items())
        elif isinstance(item, str) and ':::' in item:
            p, v = item.split(':::', 1)
            results.append((p.strip(), v.strip()))
        elif item:
            results.append((str(item), ""))
    return results


def _normalize_delete_entries(raw_input: Any) -> List[str]:
    """Normalizes delete entries from list or single string."""
    if not raw_input:
        return []
    items = raw_input if isinstance(raw_input, list) else [raw_input]
    return [str(d).strip() for d in items if d]


class FileConfigBuilder(ConfigBuilder):
    """
    Parse YAML formatted configure file. It may be protocol-specific.
    """
    def __init__(self, path, protocol: str = ""):
        self.protocol = protocol
        self.path = path
        self.data = {}
        try:
            import yaml as _yaml
        except Exception as e:
            raise RuntimeError("PyYAML is required to use --config file parsing. Install with `pip install pyyaml`") from e

        with open(path, 'r') as yfile:
            self.data = _yaml.safe_load(yfile)
        
    def build(self) -> ParsedConfig:
        d = self.data
        sessions = []

        # Global fallbacks
        global_cfg = d.get('global', {})

        # global_operation = global_cfg.get('operation', 'subscribe')
        global_username = global_cfg.get('username', d.get('username', ''))
        global_password = global_cfg.get('password', d.get('password', ''))
        global_times = global_cfg.get('times', d.get('times', 1))
        global_insecure = global_cfg.get('insecure', d.get('insecure', False))
        global_protocol = global_cfg.get('protocol', d.get('protocol', self.protocol))
        global_sec_cfg = global_cfg.get('security', {})
        global_security = SecurityProfile(
            tls_ca=global_sec_cfg.get('tls_ca', ''),
            tls_cert=global_sec_cfg.get('tls_cert', ''),
            tls_key=global_sec_cfg.get('tls_key', ''),
            skip_verify=global_sec_cfg.get('skip_verify', False),
            tls_server_name=global_sec_cfg.get('tls_server_name', ''),
            tls_version=global_sec_cfg.get('tls_version', '')
        )
        debug = global_cfg.get('debug', False)
        fallback_global = dict(d)
        if isinstance(global_cfg, dict):
            fallback_global.update(global_cfg)
        global_schema = _parse_schema_config(
            d.get('schema') or (global_cfg.get('schema') if isinstance(global_cfg, dict) else None),
            fallback_dict=fallback_global
        )

        targets = d.get('targets', {})
        if not isinstance(targets, dict):
            targets = {}

        if global_protocol != self.protocol:
            raise FileConfigError(f"Given protocol is {global_protocol}, "
                                  f"but user runs with protocol {self.protocol}")

        # Optional `devices:` inventory block backed by DeviceRegistryService
        from application.devices import DeviceRegistryService
        from application.dto import AdhocEndpointDTO
        device_registry = DeviceRegistryService()
        raw_devices = d.get('devices', {})
        if isinstance(raw_devices, dict):
            for dev_id, dev_info in raw_devices.items():
                if not isinstance(dev_info, dict):
                    continue
                dev_target = dev_info.get('target') or dev_info.get('host', '')
                dev_proto = dev_info.get('protocol', global_protocol) or self.protocol or 'gnmi'
                dev_sec = dev_info.get('security', {}) if isinstance(dev_info.get('security'), dict) else {}
                ep_dto = AdhocEndpointDTO(
                    target=str(dev_target),
                    protocol=str(dev_proto),
                    username=dev_info.get('username', global_username),
                    password=dev_info.get('password', global_password),
                    tls_ca=dev_sec.get('tls_ca', global_sec_cfg.get('tls_ca', '')) or None,
                    tls_cert=dev_sec.get('tls_cert', global_sec_cfg.get('tls_cert', '')) or None,
                    tls_key=dev_sec.get('tls_key', global_sec_cfg.get('tls_key', '')) or None,
                    skip_verify=bool(dev_sec.get('skip_verify', global_sec_cfg.get('skip_verify', False))),
                    insecure=bool(dev_info.get('insecure', global_insecure)),
                    tls_server_name=dev_sec.get('tls_server_name', global_sec_cfg.get('tls_server_name', '')) or None,
                    tls_version=dev_sec.get('tls_version', global_sec_cfg.get('tls_version', '')) or None,
                    ssh_key=dev_sec.get('ssh_key', global_sec_cfg.get('ssh_key')) or None,
                )
                device_registry.register_device(str(dev_id), ep_dto)

        session_device_ids: List[Optional[str]] = []

        # targets in YAML
        for target_ip_port, tgt_info in targets.items():
            ref_device_id: Optional[str] = tgt_info.get('device_id')
            if not ref_device_id and device_registry.has_device(str(target_ip_port)):
                ref_device_id = str(target_ip_port)

            reg_ep = device_registry.get_device(ref_device_id) if ref_device_id else None
            resolved_target = reg_ep.target if reg_ep else str(target_ip_port)

            t_username = tgt_info.get('username', reg_ep.username if (reg_ep and reg_ep.username is not None) else global_username)
            t_password = tgt_info.get('password', reg_ep.password if (reg_ep and reg_ep.password is not None) else global_password)
            t_times = tgt_info.get('times', global_times)
            t_protocol = tgt_info.get('protocol', reg_ep.protocol if reg_ep else global_protocol) or 'gnmi'
            t_insecure = tgt_info.get('insecure', reg_ep.insecure if reg_ep else global_insecure)
            proto_enum = Protocol.GNMI if t_protocol.lower() == 'gnmi' else Protocol.NETCONF
            t_schema = _parse_schema_config(tgt_info.get('schema'), fallback_dict=tgt_info) if ('schema' in tgt_info or any(k in tgt_info for k in ('yang_dirs', 'yang_dir', 'validate', 'enable_schema_validation'))) else global_schema

            conn = ConnectionConfig(
                target=str(resolved_target),
                username=t_username,
                password=t_password,
                security=global_security,
                insecure=t_insecure
            )
            exec_cfg = ExecutionConfig(
                times=t_times,
                timeout=tgt_info.get('timeout', 30)
            )

            # Check if sequential `operations` pipeline is specified
            if 'operations' in tgt_info:
                ops_raw = tgt_info['operations']
                if not isinstance(ops_raw, list):
                    raise FileConfigError(f"'operations' for target {target_ip_port} must be a list")
                if not ops_raw:
                    raise FileConfigError(f"'operations' for target {target_ip_port} cannot be empty")

                parsed_ops = [parse_operation_item(op_entry, protocol=proto_enum) for op_entry in ops_raw]
                session = SessionConfig(
                    connection=conn,
                    protocol=proto_enum,
                    operations=parsed_ops,
                    execution=exec_cfg,
                    schema=t_schema,
                )
                sessions.append(session)
                session_device_ids.append(ref_device_id)
                continue

            # Deprecate legacy top-level target fields
            for dep_key in ('get-path', 'update-list', 'delete-list', 'replace-list'):
                if dep_key in tgt_info:
                    sugg = "get: { path: [...] }" if dep_key == "get-path" else "set: { ... }"
                    raise FileConfigError(
                        f"Target '{target_ip_port}' uses deprecated field '{dep_key}'. "
                        f"Please use the 'operations:' sequence (e.g. 'operations: [ - {sugg} ]')."
                    )

            tgt_cnt = 0

            # possible Get, Set, Subscribe list
            t_sub_list = tgt_info.get('subscriptions', [])
            t_get_list = tgt_info.get('get-path', [])
            t_update_list = tgt_info.get('update-list', [])
            t_replace_list = tgt_info.get('replace-list', [])
            t_delete_list = tgt_info.get('delete-list', [])
            t_set_block = tgt_info.get('set', {})
            t_op = (tgt_info.get('operation', '') or tgt_info.get('type', '')).lower()

            t_raw_filter = tgt_info.get('filter', '')
            t_raw_config = tgt_info.get('config') or tgt_info.get('nc_config')
            if isinstance(t_set_block, dict) and not t_raw_config:
                t_raw_config = t_set_block.get('config') or t_set_block.get('nc_config')

            is_capability = t_op in ('capability', 'capabilities')
            is_get_schema = t_op in ('get-schema', 'get_schema') or bool(tgt_info.get('identifier'))
            is_subscribe = len(t_sub_list) > 0
            is_get = len(t_get_list) > 0 or t_op in ('get', 'get-config', 'get_config') or bool(t_raw_filter)
            is_set = (
                len(t_update_list) > 0 or len(t_replace_list) > 0 or len(t_delete_list) > 0
                or bool(t_set_block) or t_op in ('set', 'edit-config', 'edit_config')
                or bool(t_raw_config)
            )

            # Avoid collision if is_set was triggered by default but is_capability or is_get_schema or is_subscribe or is_get matches
            if is_capability or is_get_schema or is_subscribe or is_get:
                if not (len(t_update_list) > 0 or len(t_replace_list) > 0 or len(t_delete_list) > 0 or bool(t_set_block) or bool(t_raw_config) or t_op in ('set', 'edit-config', 'edit_config')):
                    is_set = False

            tgt_cnt = sum([1 for flag in [is_capability, is_get_schema, is_subscribe, is_get, is_set] if flag])
            if tgt_cnt > 1:
                raise FileConfigError(f"target {target_ip_port} holds two or more RPC types - only one type of RPC is allowed")

            conn = ConnectionConfig(
                target=str(resolved_target),
                username=t_username,
                password=t_password,
                security=global_security,
                insecure=t_insecure
            )
            proto_enum = Protocol.GNMI if t_protocol.lower() == 'gnmi' else Protocol.NETCONF

            if is_capability:
                op_obj = CapabilitiesOperation()
                session = SessionConfig(
                    connection=conn,
                    protocol=proto_enum,
                    operation=op_obj,
                    execution=ExecutionConfig(times=t_times),
                    schema=t_schema,
                )
                sessions.append(session)
                session_device_ids.append(ref_device_id)
            elif is_get_schema:
                ident = tgt_info.get('identifier') or (t_get_list[0] if t_get_list else '')
                op_obj = GetSchemaOperation(
                    identifier=ident,
                    version=tgt_info.get('version'),
                    format=tgt_info.get('format', 'yang') or 'yang'
                )
                session = SessionConfig(
                    connection=conn,
                    protocol=proto_enum,
                    operation=op_obj,
                    execution=ExecutionConfig(times=t_times),
                    schema=t_schema,
                )
                sessions.append(session)
                session_device_ids.append(ref_device_id)
            elif is_subscribe: # Subscribe
                subs = d.get('subscriptions', {})
                for sub_name in t_sub_list:
                    if sub_name not in subs:
                        raise FileConfigError(f"subscription {sub_name} not found")
                    named_sub = subs[sub_name]

                    update_only = False
                    if 'update_only' in named_sub:
                        update_only = True

                    sub_details = named_sub.get('subscription', {})

                    paths = sub_details.get('path', [])
                    if isinstance(paths, str):
                        paths = [paths]
                    sub_mode = sub_details.get('mode', 'target_defined')
                    sample_interval = sub_details.get('sample_interval', 0)

                    try:
                        d_mode = resolve_delivery_mode(named_sub.get('mode'), sub_mode,
                                                       default_sub_mode='target_defined')
                    except ValueError as e:
                        raise FileConfigError(f"subscription {sub_name}: {e}") from None
                    del_policy = DeliveryPolicy(
                        mode=d_mode,
                        interval=sample_interval,
                        heartbeat=0,
                        suppress_redundant=False
                    )
                    if proto_enum == Protocol.NETCONF:
                        raw_filter = named_sub.get('filter') or sub_details.get('filter')
                        if raw_filter:
                            filter_str, f_type = extract_filter_expression(
                                raw_filter,
                                default_type=named_sub.get('filter_type')
                            )
                            sub_selector = FilterSelector(expression=filter_str, filter_type=f_type)
                        elif paths:
                            sub_selector = PathSelector(paths=tuple(paths), prefix=named_sub.get('prefix', ''))
                        else:
                            sub_selector = PathSelector(paths=())

                        st_name = named_sub.get('stream_name') or named_sub.get('stream') or sub_name or 'NETCONF'
                        nc_opts = NetconfOptions(
                            stream_name=st_name,
                            start_time=named_sub.get('start_time'),
                            stop_time=named_sub.get('stop_time'),
                            poll_timeout=float(named_sub.get('poll_timeout', 1.0)),
                        )
                        op_obj = SubscribeOperation(
                            selector=sub_selector,
                            delivery=DeliveryPolicy(mode=DeliveryMode.EVENT_DRIVEN),
                            subscription_name=sub_name,
                            protocol_options=nc_opts,
                        )
                    else:
                        op_obj = SubscribeOperation(
                            selector=PathSelector(paths=tuple(paths), prefix=named_sub.get('prefix', '')),
                            delivery=del_policy,
                            subscription_name=sub_name,
                            protocol_options=GNMIOptions(
                                encoding=named_sub.get('encoding', 'json_ietf'),
                                updates_only=update_only,
                            )
                        )
                    session = SessionConfig(
                        connection=conn,
                        protocol=proto_enum,
                        operation=op_obj,
                        execution=ExecutionConfig(times=t_times),
                        schema=t_schema,
                    )
                    sessions.append(session)
                    session_device_ids.append(ref_device_id)
            elif is_get: # Get / Get-Config
                paths = t_get_list if t_get_list else tgt_info.get('paths', [])
                if isinstance(paths, str):
                    paths = [paths]

                if t_raw_filter:
                    filter_str, f_type = extract_filter_expression(t_raw_filter)
                    selector = FilterSelector(expression=filter_str, filter_type=f_type)
                else:
                    selector = PathSelector(paths=tuple(paths), prefix=tgt_info.get('prefix', ''))

                read_scope = 'config' if t_op in ('get-config', 'get_config') else 'all'
                if proto_enum == Protocol.NETCONF:
                    p_opts = NetconfOptions(source=tgt_info.get('source', 'running'))
                else:
                    p_opts = GNMIOptions(encoding=tgt_info.get('encoding', 'json_ietf'))

                op_obj = GetOperation(
                    selector=selector,
                    read_scope=read_scope,
                    protocol_options=p_opts
                )
                session = SessionConfig(
                    connection=conn,
                    protocol=proto_enum,
                    operation=op_obj,
                    execution=ExecutionConfig(times=t_times),
                    schema=t_schema,
                )
                sessions.append(session)
                session_device_ids.append(ref_device_id)
            else: # Set / Edit-Config
                if proto_enum == Protocol.NETCONF and t_raw_config:
                    cfg_payload = read_payload(t_raw_config) if isinstance(t_raw_config, str) else str(t_raw_config)
                    nc_opts = NetconfOptions(
                        target_datastore=tgt_info.get('target_datastore', 'candidate'),
                        config=cfg_payload,
                        default_operation=tgt_info.get('default_operation', 'merge'),
                        error_option=tgt_info.get('error_option', 'stop-on-error'),
                        test_option=tgt_info.get('test_option'),
                        commit=tgt_info.get('commit', True)
                    )
                    op_obj = SetOperation(
                        changes=(),
                        protocol_options=nc_opts
                    )
                else:
                    updates = []
                    replaces = []
                    deletes = []
                    set_prefix = tgt_info.get('prefix', '')
                    set_encoding = tgt_info.get('encoding', 'json_ietf')

                    if t_set_block and isinstance(t_set_block, dict):
                        set_prefix = t_set_block.get('prefix', set_prefix)
                        set_encoding = t_set_block.get('encoding', set_encoding)
                        updates.extend(_normalize_mutation_entries(t_set_block.get('update')))
                        replaces.extend(_normalize_mutation_entries(t_set_block.get('replace')))
                        deletes.extend(_normalize_delete_entries(t_set_block.get('delete')))

                    if t_update_list:
                        updates.extend(_normalize_mutation_entries(t_update_list))
                    if t_replace_list:
                        replaces.extend(_normalize_mutation_entries(t_replace_list))
                    if t_delete_list:
                        deletes.extend(_normalize_delete_entries(t_delete_list))

                    changes = []
                    for p, v in updates:
                        changes.append(Change(path=p, operation=ChangeType.MERGE, value=v))
                    for p, v in replaces:
                        changes.append(Change(path=p, operation=ChangeType.REPLACE, value=v))
                    for d_p in deletes:
                        changes.append(Change(path=d_p, operation=ChangeType.DELETE, value=None))

                    p_opts = GNMIOptions(encoding=set_encoding) if proto_enum == Protocol.GNMI else NetconfOptions()
                    op_obj = SetOperation(
                        changes=tuple(changes),
                        prefix=set_prefix,
                        protocol_options=p_opts
                    )

                session = SessionConfig(
                    connection=conn,
                    protocol=proto_enum,
                    operation=op_obj,
                    execution=ExecutionConfig(times=t_times),
                    schema=t_schema,
                )
                sessions.append(session)
                session_device_ids.append(ref_device_id)

        # Parsing outputs
        outputs = []

        output_group = d.get('outputs')
        if output_group is None:
            outputs.append(OutputConfig(
                format = OutputFormat.JSON,
                output_type = OutputType.STDOUT,
            ))
        else:
            for og_name, og_item in output_group.items():
                outputs.append(OutputConfig(
                    name = og_name,
                    format = OutputFormat(og_item['format']),
                    output_type = OutputType(og_item['output-type']),
                    path = og_item.get('path')
                ))

        requests = [
            session_to_execution_request(sess, device_id=dev_id)
            for sess, dev_id in zip(sessions, session_device_ids)
        ]

        return ParsedConfig(
            sessions=sessions, targets=targets,
            outputs=outputs, debug=debug,
            log_level=d.get('log_level', 'ERROR'),
            syslog_server=d.get('syslog_server', ''),
            log_file=d.get('log_file', ''),
            requests=requests,
            device_registry=device_registry if raw_devices else None,
        )

def add_schema_args(p: Any, is_child: bool = False) -> None:
    """Adds YANG schema management CLI options to a parser or subparser."""
    d_list = argparse.SUPPRESS if is_child else []
    d_bool = argparse.SUPPRESS if is_child else False
    d_str = argparse.SUPPRESS if is_child else ""

    p.add_argument('--yang-dir', action='append', default=d_list,
                   help="Directory containing offline YANG models (can be specified multiple times)")
    p.add_argument('--yang-cache-dir', default=d_str,
                   help="Directory for storing cached/downloaded YANG models")
    p.add_argument('--validate-schema', dest='validate_schema', action='store_true', default=d_bool,
                   help="Enable strict YANG schema validation")
    p.add_argument('--no-auto-discovery', dest='no_auto_discovery', action='store_true', default=d_bool,
                   help="Disable dynamic YANG schema/capability discovery")
    p.add_argument('--no-auto-fetch', dest='no_auto_fetch', action='store_true', default=d_bool,
                   help="Disable automatic YANG schema fetching via protocol")
    p.add_argument('--schema-profile', dest='schema_profile', default=d_str,
                   help="Schema profile fingerprint or identifier")

def build_args(args=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="gNMI / NETCONF Client")

    parser.add_argument('-c', '--config', '-g', '--global-config', dest='config', default='', help='Path to YAML configuration file for this client')
    parser.add_argument('-t', '--target', action='append', help="List of targets in IP:PORT format")
    parser.add_argument('-d', '--debug', help="Debugging this script", action='store_true')
    parser.add_argument('--times', default=1, type=int, help="Generate duplicated requests - only use for testing")
    parser.add_argument('-u', '--username', default='', help="Username")
    parser.add_argument('-p', '--password', default='', help="Password")
    parser.add_argument('-i', '--insecure', action='store_true',
                        help="use insecure connection if set True")

    # output specifiers
    parser.add_argument('--output-type', default='stdout', help="direction of output data.",
                        choices=['file', 'stdout', 'stderr', 'syslog'])
    parser.add_argument('--output-file', help="Specify path of file when `--output-type` is `file`")
    parser.add_argument('--output-format', default='json',
                        help="Specify output format.", choices=['json', 'text', 'xml'])

    # security options
    parser.add_argument('--tls-ca', default='', help="Path to CA certificate")
    parser.add_argument('--tls-cert', default='', help="Path to client certificate")
    parser.add_argument('--tls-key', default='', help="Path to client private key")
    parser.add_argument('--skip-verify', action='store_true', help="Path to CA certificate")
    parser.add_argument('--tls-server-name', default='', help="sets the server name to be used when verifying the hostname on the returned certificates. If 'skip-verify' was set, this options is meaningless.")
    parser.add_argument('--tls-version', default='1.3', choices=['1.0','1.1','1.2','1.3'],
                         help="set TLS version. Default version is 1.3")
    parser.add_argument('--ssh-key', help="Path to SSH key")
    
    # logger
    parser.add_argument('--log-level', default='ERROR',
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                        help="Set logging level")
    parser.add_argument('--syslog-server', default='', help="IP:PORT of Syslog server")
    parser.add_argument('--log-file', default='', help="Path to save local logs")

    add_schema_args(parser, is_child=False)

    proto_parser = parser.add_subparsers(dest='protocol', help="select NB protcol")

    # add NETCONF parser
    netconf_args(proto_parser)

    # add gNMI parser
    gnmi_args(proto_parser)

    return parser.parse_args(args)

def netconf_args(parser):
    parser_nc = parser.add_parser('netconf', help='NETwork CONFiguration') 
    add_schema_args(parser_nc, is_child=True)

    parser_nc.add_argument('--device', default='default', help="Name of vendor-specific device")
    # config file for NETCONF
    parser_nc.add_argument('-c', '--config', default='', help='Path to YAML configuration file for NETCONF RPCs')

    subparsers = parser_nc.add_subparsers(dest='operation', help='specify supported NETCONF operation')

    # <hello>
    parser_cap = subparsers.add_parser('capability', help="Fetch NETCONF Server Capabilities")
    add_schema_args(parser_cap, is_child=True)

    # <get>
    parser_get = subparsers.add_parser('get', help="NETCONF <get>")
    add_schema_args(parser_get, is_child=True)
    parser_get.add_argument('--filter', default='',
                            help="XML filter string or path to file. If given path not exists, consider it as a 'XML' formatted request")
    parser_get.add_argument('--nc-xpath', action='append', help="List of selected NETCONF XPaths")

    # <get-config>
    parser_get_config = subparsers.add_parser('get-config', help="NETCONF <get-config>")
    add_schema_args(parser_get_config, is_child=True)
    parser_get_config.add_argument('--source', default='', help="specify one of 'running', 'candidate', 'startup' if you want to request <get-config> or do not present for <get>")
    parser_get_config.add_argument('--filter', default='',
                            help="XML filter string or path to file. If given path not exists, consider it as a 'XML' formatted request")
    parser_get_config.add_argument('--nc-xpath', action='append', help="List of selected NETCONF XPaths")

    # <get-schema>
    parser_get_schema = subparsers.add_parser('get-schema', help="NETCONF <get-schema>")
    add_schema_args(parser_get_schema, is_child=True)
    parser_get_schema.add_argument('--identifier', default='',
                                   help="Identifier for the schema list entry.", required=True)
    parser_get_schema.add_argument('--version', help="Version of the schema requested")
    parser_get_schema.add_argument('--schema-format', default='yang', help="The data modeling language of the schema")

    # <edit-config>
    parser_set = subparsers.add_parser('edit-config', aliases=['set'], help="NETCONF <edit-config>")
    add_schema_args(parser_set, is_child=True)
    parser_set.add_argument('--target-datastore', '--target', dest='target_datastore', default='candidate',
                            choices=['candidate', 'running', 'startup'], help="Target datastore (default: candidate)")
    parser_set.add_argument('-C', '--config', '--nc-config', dest='nc_config', required=True,
                            help="XML string or path to file containing configuration tree")
    parser_set.add_argument('--default-operation', choices=['merge', 'replace', 'none'], default='merge',
                            help="Default operation for <edit-config> (default: merge)")
    parser_set.add_argument('--error-option', choices=['stop-on-error', 'continue-on-error', 'rollback-on-error'],
                            default='stop-on-error', help="Error option behavior (default: stop-on-error)")
    parser_set.add_argument('--test-option', choices=['test-then-set', 'set', 'test-only'], default=None,
                            help="Test option if target supports :validate")
    parser_set.add_argument('--no-commit', dest='commit', action='store_false', default=True,
                            help="Do not commit candidate datastore changes after edit-config")
    parser_set.add_argument('--lock', dest='lock_target', action='store_true', default=False,
                            help="Acquire datastore lock prior to edit-config and unlock in finally")
    parser_set.add_argument('--validate', dest='validate_candidate', action='store_true', default=False,
                            help="Validate candidate configuration before commit")
    parser_set.add_argument('--confirmed', action='store_true', default=False,
                            help="Request confirmed commit")
    parser_set.add_argument('--confirm-timeout', dest='confirm_timeout', type=int, default=None,
                            help="Confirmed commit timeout in seconds")
    parser_set.add_argument('--persist', dest='persist', default="",
                            help="Confirmed commit persist token")
    parser_set.add_argument('--persist-id', dest='persist_id', default="",
                            help="Confirmed commit persist-id token")

    # <commit>
    parser_commit = subparsers.add_parser('commit', help="NETCONF <commit>")
    add_schema_args(parser_commit, is_child=True)
    parser_commit.add_argument('--confirmed', action='store_true', default=False,
                               help="Request confirmed commit")
    parser_commit.add_argument('--confirm-timeout', dest='confirm_timeout', type=int, default=None,
                               help="Confirmed commit timeout in seconds")
    parser_commit.add_argument('--persist', dest='persist', default="",
                               help="Confirmed commit persist token")
    parser_commit.add_argument('--persist-id', dest='persist_id', default="",
                               help="Confirmed commit persist-id token")

    # <cancel-commit>
    parser_cancel_commit = subparsers.add_parser('cancel-commit', aliases=['cancel_commit'], help="NETCONF <cancel-commit>")
    add_schema_args(parser_cancel_commit, is_child=True)
    parser_cancel_commit.add_argument('--persist-id', dest='persist_id', default="",
                                      help="Cancel confirmed commit using persist-id token")

    # <discard-changes>
    parser_discard = subparsers.add_parser('discard-changes', aliases=['discard_changes'], help="NETCONF <discard-changes>")
    add_schema_args(parser_discard, is_child=True)

    # <create-subscription> (RFC 5277)
    parser_sub = subparsers.add_parser('subscribe', help="NETCONF Event Notification (RFC 5277)")
    add_schema_args(parser_sub, is_child=True)
    parser_sub.add_argument('--stream', '--stream-name', dest='stream_name', default='NETCONF',
                            help="Event stream name to subscribe to (default: NETCONF)")
    parser_sub.add_argument('--filter', default='',
                            help="XML filter string or path to file")
    parser_sub.add_argument('--nc-xpath', action='append', help="List of selected NETCONF XPaths")
    parser_sub.add_argument('--start-time', default=None,
                            help="Start time for notification replay (ISO 8601 / RFC 3339)")
    parser_sub.add_argument('--stop-time', default=None,
                            help="Stop time for notification replay (ISO 8601 / RFC 3339)")
    parser_sub.add_argument('--poll-timeout', type=float, default=1.0,
                            help="Poll timeout in seconds for notifications (default: 1.0)")


def gnmi_args(parser):
    parser_gnmi = parser.add_parser('gnmi', help='gRPC Network Management Interfaces') 
    add_schema_args(parser_gnmi, is_child=True)

    # config file for gNMI
    parser_gnmi.add_argument('-c', '--config', default='', help='Path to YAML configuration file for gNMI RPCs')
    parser_gnmi.add_argument('-e', '--encoding', default='json_ietf',
                        help="encoding formats defined at gNMI", 
                        choices=['json', 'json_ietf', 'bytes', 'proto', 'ascii'])

    # Top-Level Operation Parser
    subparsers = parser_gnmi.add_subparsers(dest='operation', help="specify gNMI RPC operation")

    # UNARY: Capabilities
    parser_cap = subparsers.add_parser('capability', help='execute CAPABILITIES RPC')
    add_schema_args(parser_cap, is_child=True)
    
    # UNARY: Get
    parser_get = subparsers.add_parser('get', help='execute GET RPC')
    add_schema_args(parser_get, is_child=True)
    parser_get.add_argument('--type', choices=['config', 'state', 'operational', ''], default='', help="The type of data that is requested from the target. An empty value will grab all kinds of data")
    parser_get.add_argument('--path', action='append', help="List of gNMI Paths")
    parser_get.add_argument('--prefix', default='', help="common prefix for all given paths")

    # UNARY: Set
    parser_set = subparsers.add_parser('set', help='execute SET RPC')
    add_schema_args(parser_set, is_child=True)
    parser_set.add_argument('--prefix', default='', help="common prefix for all given paths")
    parser_set.add_argument('--update', action='append', help='Update path and value (format: PATH:::VALUE or PATH:::@file)')
    parser_set.add_argument('--update-path', action=PairedAction, group_type='update', kind='path',
                            help='Target path for update operation (paired with --update-value or --update-file)')
    parser_set.add_argument('--update-value', action=PairedAction, group_type='update', kind='value',
                            help='Inline value for update operation (paired with --update-path)')
    parser_set.add_argument('--update-file', action=PairedAction, group_type='update', kind='file',
                            help='File path containing payload for update operation (paired with --update-path)')
    parser_set.add_argument('--replace', action='append', help='Replace path and value (format: PATH:::VALUE or PATH:::@file)')
    parser_set.add_argument('--replace-path', action=PairedAction, group_type='replace', kind='path',
                            help='Target path for replace operation (paired with --replace-value or --replace-file)')
    parser_set.add_argument('--replace-value', action=PairedAction, group_type='replace', kind='value',
                            help='Inline value for replace operation (paired with --replace-path)')
    parser_set.add_argument('--replace-file', action=PairedAction, group_type='replace', kind='file',
                            help='File path containing payload for replace operation (paired with --replace-path)')
    parser_set.add_argument('--delete', action='append', help='Delete path')
    parser_set.add_argument('--encoding', default='json_ietf',
                            choices=['json', 'json_ietf', 'bytes', 'proto', 'ascii'],
                            help="encoding format for SET RPC")

    # Subscribe operation
    parser_sub = subparsers.add_parser('subscribe', help="specify mode for STREAM mode of Subscribe RPC")
    add_schema_args(parser_sub, is_child=True)

    parser_sub.add_argument('--path', action='append', help="List of gNMI Paths")
    parser_sub.add_argument('--prefix', default='', help="common prefix for all given paths")
    parser_sub.add_argument('--mode', choices=['once', 'poll', 'stream'], default='stream',
                            help='one of once, poll or stream(default is stream)')

    parser_sub.add_argument('--sub-mode', choices=['sample', 'on_change', 'target_defined'],
                            default='sample', help='choose Subscribe stream mode(default is sample)')
    parser_sub.add_argument('--update-only', help="skip initial responses from server", action='store_true')
    parser_sub.add_argument('--sample-interval', type=int, default=90, help='sample interval in seconds')

def config_builder(args) -> ParsedConfig:
    """Build an appropriate `ParsedConfig` class by the contents of argument"""
    config_file = getattr(args, 'config', '') or getattr(args, 'global_config', '')
    if config_file and (getattr(args, 'protocol', None) is None or getattr(args, 'operation', None) is None):
        protocol = getattr(args, 'protocol', 'unknown')
        return FileConfigBuilder(config_file, protocol=protocol).build()
    else:
        return CLIConfigBuilder(args).build()

def parse_args(args=None) -> ParsedConfig:
    """Helper to parse raw argument list and build ParsedConfig directly."""
    return config_builder(build_args(args))


def session_to_execution_request(session: SessionConfig, device_id: Optional[str] = None) -> Any:
    """
    Converts an internal SessionConfig to an external ExecutionRequestDTO.
    """
    from application.dto import (
        ExecutionRequestDTO,
        AdhocEndpointDTO,
        OperationStepDTO,
        MutationChangeDTO,
        TelemetryPolicyDTO,
        DeliveryModeDTO,
    )
    from config.operations import (
        CapabilitiesOperation,
        GetOperation,
        GetConfigOperation,
        GetSchemaOperation,
        SetOperation,
        EditConfigOperation,
        TransactionOperation,
        SubscribeOperation,
    )
    from config.selectors import FilterSelector

    conn = session.connection
    sec = conn.security
    proto_str = session.protocol.value if hasattr(session.protocol, "value") else str(session.protocol).lower()
    adhoc = AdhocEndpointDTO(
        target=conn.target,
        protocol=proto_str,
        username=conn.username,
        password=conn.password,
        tls_ca=sec.tls_ca if (sec and sec.tls_ca) else None,
        tls_cert=sec.tls_cert if (sec and sec.tls_cert) else None,
        tls_key=sec.tls_key if (sec and sec.tls_key) else None,
        skip_verify=bool(sec.skip_verify) if sec else bool(conn.insecure),
        insecure=bool(conn.insecure),
        tls_server_name=sec.tls_server_name if (sec and getattr(sec, "tls_server_name", None)) else None,
        tls_version=sec.tls_version if (sec and getattr(sec, "tls_version", None)) else None,
        ssh_key=sec.ssh_key if (sec and getattr(sec, "ssh_key", None)) else None,
    )

    step_dtos = []
    for op in session.operations:
        if isinstance(op, CapabilitiesOperation):
            step_dtos.append(OperationStepDTO(op_type="capabilities"))
        elif isinstance(op, GetSchemaOperation):
            step_dtos.append(OperationStepDTO(
                op_type="get-schema",
                paths=[op.identifier] if op.identifier else [],
                options={"version": op.version, "format": op.format},
            ))
        elif isinstance(op, GetOperation):
            is_filter = isinstance(op.selector, FilterSelector)
            po = getattr(op, "protocol_options", None)
            opts: Dict[str, Any] = {"read_scope": getattr(op, "read_scope", "all")}
            if po is not None and hasattr(po, "encoding"):
                opts["encoding"] = po.encoding
            ds = "all"
            if po is not None and hasattr(po, "source_datastore") and po.source_datastore:
                ds = po.source_datastore
            elif po is not None and hasattr(po, "source") and po.source:
                ds = po.source
            step_dtos.append(OperationStepDTO(
                op_type="get_config" if isinstance(op, GetConfigOperation) or getattr(op, "read_scope", "") == "config" else "get",
                paths=list(getattr(op.selector, "paths", ())) if not is_filter else [],
                prefix=getattr(op.selector, "prefix", "") or None,
                filter_expression=op.selector.expression if is_filter else None,
                filter_type=op.selector.filter_type if is_filter else None,
                datastore=ds,
                options=opts,
            ))
        elif isinstance(op, SetOperation):
            changes = [
                MutationChangeDTO(
                    path=c.path,
                    value=c.value,
                    operation=c.operation.value if hasattr(c.operation, "value") else str(c.operation).lower(),
                    value_type=getattr(c, "type", None),
                )
                for c in op.changes
            ]
            opts = {}
            xml_payload = None
            ds = "all"
            if hasattr(op, "protocol_options") and op.protocol_options:
                po = op.protocol_options
                if hasattr(po, "config") and po.config is not None:
                    xml_payload = po.config
                if hasattr(po, "target_datastore") and po.target_datastore:
                    ds = po.target_datastore
                for k in [
                    "encoding",
                    "default_operation",
                    "test_option",
                    "error_option",
                    "lock_target",
                    "validate_candidate",
                    "commit",
                    "confirmed",
                    "confirm_timeout",
                    "persist",
                    "persist_id",
                ]:
                    if hasattr(po, k):
                        opts[k] = getattr(po, k)
            step_dtos.append(OperationStepDTO(
                op_type="set",
                datastore=ds,
                changes=changes,
                xml_payload=xml_payload,
                prefix=op.prefix,
                options=opts,
            ))
        elif isinstance(op, TransactionOperation):
            tx_name = op.operation.value if hasattr(op.operation, "value") else str(op.operation).lower()
            step_dtos.append(OperationStepDTO(
                op_type=tx_name,
                datastore=getattr(op, "target_datastore", "candidate"),
                options={
                    "source_datastore": getattr(op, "source_datastore", "candidate"),
                    "confirmed": getattr(op, "confirmed", False),
                    "confirm_timeout": getattr(op, "confirm_timeout", None),
                    "persist": getattr(op, "persist", None),
                    "persist_id": getattr(op, "persist_id", None),
                },
            ))
        elif isinstance(op, SubscribeOperation):
            is_filter = isinstance(op.selector, FilterSelector)
            tel_policy = None
            if hasattr(op, "delivery") and op.delivery:
                del_mode_str = op.delivery.mode.value if hasattr(op.delivery.mode, "value") else str(op.delivery.mode).lower()
                tel_policy = TelemetryPolicyDTO(
                    mode=DeliveryModeDTO.from_string(del_mode_str),
                    sample_interval_sec=getattr(op.delivery, "interval", 10.0),
                    heartbeat_interval_sec=getattr(op.delivery, "heartbeat", None),
                    suppress_redundant=getattr(op.delivery, "suppress_redundant", False),
                )
            sub_opts: Dict[str, Any] = {"subscription_name": getattr(op, "subscription_name", "sub")}
            po = getattr(op, "protocol_options", None)
            if po is not None:
                for k in ["encoding", "updates_only", "stream_name", "start_time", "stop_time", "poll_timeout"]:
                    if hasattr(po, k):
                        sub_opts[k] = getattr(po, k)
            step_dtos.append(OperationStepDTO(
                op_type="subscribe",
                paths=list(getattr(op.selector, "paths", ())) if not is_filter else [],
                prefix=getattr(op.selector, "prefix", "") or None,
                filter_expression=op.selector.expression if is_filter else None,
                filter_type=op.selector.filter_type if is_filter else None,
                telemetry=tel_policy,
                options=sub_opts,
            ))
        else:
            raise ValueError(f"Unsupported operation in SessionConfig: {op}")

    schema_profile_id = None
    schema_dirs = []
    schema_cache_dir = None
    schema_validate = False
    schema_auto_discovery = True
    schema_auto_fetch = True
    if hasattr(session, "schema") and session.schema:
        schema_profile_id = getattr(session.schema, "profile", None)
        schema_dirs = [str(p) for p in getattr(session.schema, "yang_dirs", ())]
        if getattr(session.schema, "cache_dir", None):
            schema_cache_dir = str(session.schema.cache_dir)
        schema_validate = bool(getattr(session.schema, "validate", False))
        schema_auto_discovery = bool(getattr(session.schema, "auto_discovery", True))
        schema_auto_fetch = bool(getattr(session.schema, "auto_fetch", True))

    return ExecutionRequestDTO(
        device_id=device_id,
        adhoc_endpoint=adhoc,
        protocol=adhoc.protocol,
        operations=step_dtos,
        schema_profile_id=schema_profile_id,
        schema_dirs=schema_dirs,
        schema_cache_dir=schema_cache_dir,
        schema_validate=schema_validate,
        schema_auto_discovery=schema_auto_discovery,
        schema_auto_fetch=schema_auto_fetch,
        timeout_seconds=session.execution.timeout if hasattr(session, "execution") and session.execution else 30,
        times=session.execution.times if hasattr(session, "execution") and session.execution else 1,
    )


def execution_request_to_session(dto: Any, registry: Optional[Any] = None) -> SessionConfig:
    """
    Converts an external ExecutionRequestDTO to an internal SessionConfig.
    Delegates to DeviceOperationService.
    """
    from application.operations import DeviceOperationService
    op_svc = DeviceOperationService(device_registry=registry)
    return op_svc.build_session_config(dto)
