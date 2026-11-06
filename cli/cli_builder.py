# -*- coding: utf-8 -*-
"""
CLI configuration builder translating command-line arguments into ParsedConfig.
"""
from typing import Any, List, Optional, Sequence, Tuple

from config.delivery import DeliveryMode, DeliveryPolicy
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
    GetOperation,
    GetSchemaOperation,
    NetconfTransactionOperation,
    OperationConfig,
    SetOperation,
    SubscribeOperation,
    TransactionType,
)
from config.protocol_options import GNMIOptions, NetconfOptions
from config.schema import SchemaConfig
from config.selectors import FilterSelector, PathSelector
from modules.security import SecurityProfile
from cli.cli_args import _parse_cli_mutation_flags
from cli.models import ConfigBuilder, ParsedConfig
from cli.normalizers import resolve_delivery_mode
from util.utils import read_payload


class CLIConfigBuilder(ConfigBuilder):
    """
    Parses all command-line arguments via argparse.
    Translates parsed arguments into a canonical ParsedConfig.
    """

    def __init__(self, args: Any):
        self.args = args

    def build(self) -> ParsedConfig:
        sessions: List[SessionConfig] = []

        times = self.args.times
        if times <= 0:
            print("[Config] times option should be positive integer. Ignore given value")
            times = 1

        if not getattr(self.args, 'protocol', None) or not getattr(self.args, 'operation', None):
            print("[CLI] Error: You must specify a protocol and an operation, or use a --config file.")
            return ParsedConfig(sessions=[], debug=self.args.debug)

        targets = self.args.target or []
        protocol = self.args.protocol.lower()
        operation = self.args.operation.lower()

        for target in targets:
            security_profile = self._build_security_profile()
            conn = ConnectionConfig(
                target=target,
                username=self.args.username or "",
                password=self.args.password or "",
                security=security_profile,
                insecure=getattr(self.args, 'insecure', False),
            )
            exec_cfg = ExecutionConfig(
                times=times,
                continue_on_error=getattr(self.args, 'continue_on_error', False),
            )

            raw_paths = getattr(self.args, 'path', []) or []
            paths = [raw_paths] if isinstance(raw_paths, str) else list(raw_paths)
            prefix = getattr(self.args, "prefix", "")

            if protocol == 'gnmi':
                proto_enum = Protocol.GNMI
                op_obj = self._build_gnmi_operation(operation, paths, prefix)
            elif protocol == 'netconf':
                proto_enum = Protocol.NETCONF
                op_obj = self._build_netconf_operation(operation, paths)
            else:
                raise ValueError(f"Unknown protocol: {protocol}")

            schema_cfg = self._build_schema_config()

            session = SessionConfig(
                connection=conn,
                protocol=proto_enum,
                operation=op_obj,
                execution=exec_cfg,
                schema=schema_cfg,
            )
            sessions.append(session)

        outputs = self._build_outputs()

        return ParsedConfig(
            sessions=sessions,
            outputs=outputs,
            targets=targets,
            debug=self.args.debug,
            log_level=self.args.log_level,
            syslog_server=self.args.syslog_server,
            log_file=self.args.log_file,
        )

    def _build_security_profile(self) -> SecurityProfile:
        return SecurityProfile(
            tls_ca=self.args.tls_ca,
            tls_cert=self.args.tls_cert,
            tls_key=self.args.tls_key,
            skip_verify=self.args.skip_verify,
            tls_server_name=self.args.tls_server_name,
            tls_version=self.args.tls_version,
            ssh_key=self.args.ssh_key,
        )

    def _build_schema_config(self) -> SchemaConfig:
        yang_dirs = getattr(self.args, 'yang_dir', None) or []
        if isinstance(yang_dirs, str):
            yang_dirs = [yang_dirs]
        yang_cache_dir = getattr(self.args, 'yang_cache_dir', None) or None
        validate_schema = getattr(self.args, 'validate_schema', False)
        auto_discovery = not getattr(self.args, 'no_auto_discovery', False)
        auto_fetch = not getattr(self.args, 'no_auto_fetch', False)
        schema_profile = getattr(self.args, 'schema_profile', None) or None

        return SchemaConfig(
            yang_dirs=tuple(yang_dirs),
            cache_dir=yang_cache_dir,
            validate=bool(validate_schema),
            auto_discovery=bool(auto_discovery),
            auto_fetch=bool(auto_fetch),
            profile=schema_profile,
        )

    def _build_gnmi_operation(self, operation: str, paths: List[str], prefix: str) -> OperationConfig:
        if operation in ('capability', 'capabilities'):
            return CapabilitiesOperation()

        if operation == 'get':
            encoding = getattr(self.args, "encoding", "json_ietf")
            data_type = getattr(self.args, "type", '') or getattr(self.args, "data_type", '') or 'all'
            return GetOperation(
                selector=PathSelector(paths=tuple(paths), prefix=prefix),
                read_scope=data_type,
                protocol_options=GNMIOptions(encoding=encoding),
            )

        if operation == 'set':
            changes: List[Change] = []
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
            return SetOperation(
                changes=tuple(changes),
                prefix=prefix,
                protocol_options=GNMIOptions(encoding=encoding),
            )

        if operation in ('subscribe', 'stream', 'once', 'poll'):
            mode_str = getattr(self.args, "mode", "stream").lower()
            sub_mode_str = str(getattr(self.args, "sub_mode", getattr(self.args, "stream_mode", "sample"))).lower()
            del_mode = resolve_delivery_mode(mode_str, sub_mode_str)

            sample_interval = getattr(self.args, "sample_interval", getattr(self.args, "interval", 0))
            del_policy = DeliveryPolicy(
                mode=del_mode,
                interval=sample_interval,
                heartbeat=0,
                suppress_redundant=False,
            )
            encoding = getattr(self.args, "encoding", "json_ietf")
            updates_only = getattr(self.args, "update_only", False) or getattr(self.args, "updates_only", False)
            return SubscribeOperation(
                selector=PathSelector(paths=tuple(paths), prefix=prefix),
                delivery=del_policy,
                subscription_name="cli_execution",
                protocol_options=GNMIOptions(encoding=encoding, updates_only=updates_only),
            )

        raise ValueError(f"Unknown gNMI operation: {operation}")

    def _build_netconf_operation(self, operation: str, paths: List[str]) -> OperationConfig:
        if operation in ('capability', 'capabilities'):
            return CapabilitiesOperation()

        if operation in ('get', 'get-config'):
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
            return GetOperation(
                selector=selector,
                read_scope=read_scope,
                protocol_options=NetconfOptions(source=source),
            )

        if operation in ('get-schema', 'get_schema'):
            identifier = getattr(self.args, "identifier", "")
            if not identifier and paths:
                identifier = paths[0]
            version = getattr(self.args, "version", "")
            schema_format = getattr(self.args, "schema_format", "yang")
            return GetSchemaOperation(
                identifier=identifier,
                version=version or None,
                format=schema_format,
            )

        if operation in ('edit-config', 'edit_config', 'set'):
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
            return SetOperation(
                changes=(),
                protocol_options=nc_opts,
            )

        if operation in ('commit', 'cancel-commit', 'cancel_commit', 'discard-changes', 'discard_changes'):
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

            return NetconfTransactionOperation(
                operation=tx_type,
                confirmed=bool(confirmed_val),
                confirm_timeout=confirm_timeout_val,
                persist=persist_val,
                persist_id=persist_id_val,
            )

        if operation == 'subscribe':
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
            return SubscribeOperation(
                selector=selector,
                delivery=DeliveryPolicy(mode=DeliveryMode.EVENT_DRIVEN),
                subscription_name=stream_name,
                protocol_options=nc_opts,
            )

        raise ValueError(f"Unknown NETCONF operation: {operation}")

    def _build_outputs(self) -> List[OutputConfig]:
        output_type = self.args.output_type or OutputType.STDOUT
        output_format = self.args.output_format or OutputFormat.JSON
        output_path = None

        if output_type == OutputType.FILE:
            output_path = self.args.output_path

        return [OutputConfig(
            output_type=output_type,
            format=output_format,
            path=output_path,
        )]

