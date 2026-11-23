# -*- coding: utf-8 -*-
"""
YAML file configuration builder and operation pipeline parser.
"""
from typing import Any, Dict, List, Optional, Tuple

from application.devices import DeviceRegistryService
from application.dto import AdhocEndpointDTO
from config.delivery import DeliveryMode, DeliveryPolicy
from config.model import (
    ConnectionConfig,
    ExecutionConfig,
    OutputConfig,
    OutputFormat,
    OutputType,
    Protocol,
    SecurityProfile,
    SessionConfig,
)
from config.operations import (
    CapabilitiesOperation,
    Change,
    ChangeType,
    GetOperation,
    GetSchemaOperation,
    OperationConfig,
    SetOperation,
    SubscribeOperation,
)
from config.protocol_options import GNMIOptions, NetconfOptions
from config.schema import SchemaConfig
from config.selectors import FilterSelector, PathSelector
from cli.adapters import session_to_execution_request
from cli.models import ConfigBuilder, FileConfigError, ParsedConfig
from cli.normalizers import (
    _normalize_delete_entries,
    _normalize_mutation_entries,
    _parse_schema_config,
    extract_filter_expression,
    resolve_delivery_mode,
)
from cli.operation_parser import parse_operation_item
from util.utils import read_payload


class FileConfigBuilder(ConfigBuilder):
    """
    Parse YAML formatted configure file. It may be protocol-specific.
    """

    def __init__(self, path: str, protocol: str = ""):
        self.protocol = protocol
        self.path = path
        self.data: Dict[str, Any] = {}
        try:
            import yaml as _yaml
        except Exception as e:
            raise RuntimeError("PyYAML is required to use --config file parsing. Install with `pip install pyyaml`") from e

        with open(path, 'r') as yfile:
            self.data = _yaml.safe_load(yfile)

    def build(self) -> ParsedConfig:
        d = self.data
        sessions: List[SessionConfig] = []

        # Global fallbacks
        global_cfg = d.get('global', {})
        global_username = global_cfg.get('username', d.get('username', ''))
        global_password = global_cfg.get('password', d.get('password', ''))
        global_times = global_cfg.get('times', d.get('times', 1))
        global_insecure = global_cfg.get('insecure', d.get('insecure', False))
        global_continue_on_error = bool(
            global_cfg.get(
                'continue_on_error',
                d.get(
                    'continue_on_error',
                    global_cfg.get('continue-on-error', d.get('continue-on-error', False)),
                ),
            )
        )
        global_protocol = global_cfg.get('protocol', d.get('protocol', self.protocol))
        global_sec_cfg = global_cfg.get('security', {})
        global_security = SecurityProfile(
            tls_ca=global_sec_cfg.get('tls_ca', ''),
            tls_cert=global_sec_cfg.get('tls_cert', ''),
            tls_key=global_sec_cfg.get('tls_key', ''),
            skip_verify=global_sec_cfg.get('skip_verify', False),
            tls_server_name=global_sec_cfg.get('tls_server_name', ''),
            tls_version=global_sec_cfg.get('tls_version', ''),
        )
        debug = global_cfg.get('debug', False)
        fallback_global = dict(d)
        if isinstance(global_cfg, dict):
            fallback_global.update(global_cfg)
        global_schema = _parse_schema_config(
            d.get('schema') or (global_cfg.get('schema') if isinstance(global_cfg, dict) else None),
            fallback_dict=fallback_global,
        )

        targets = d.get('targets', {})
        if not isinstance(targets, dict):
            targets = {}

        if global_protocol != self.protocol:
            raise FileConfigError(
                f"Given protocol is {global_protocol}, but user runs with protocol {self.protocol}"
            )

        # Optional `devices:` inventory block backed by DeviceRegistryService
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
            t_continue_on_error = bool(
                tgt_info.get(
                    'continue_on_error',
                    tgt_info.get('continue-on-error', global_continue_on_error),
                )
            )
            t_protocol = tgt_info.get('protocol', reg_ep.protocol if reg_ep else global_protocol) or 'gnmi'
            t_insecure = tgt_info.get('insecure', reg_ep.insecure if reg_ep else global_insecure)
            proto_enum = Protocol.GNMI if t_protocol.lower() == 'gnmi' else Protocol.NETCONF
            t_schema = _parse_schema_config(tgt_info.get('schema'), fallback_dict=tgt_info) if ('schema' in tgt_info or any(k in tgt_info for k in ('yang_dirs', 'yang_dir', 'validate', 'enable_schema_validation'))) else global_schema

            conn = ConnectionConfig(
                target=str(resolved_target),
                username=t_username,
                password=t_password,
                security=global_security,
                insecure=t_insecure,
            )
            exec_cfg = ExecutionConfig(
                times=t_times,
                timeout=tgt_info.get('timeout', 30),
                continue_on_error=t_continue_on_error,
            )

            # Check if sequential `operations` pipeline is specified
            if 'operations' in tgt_info:
                ops_raw = tgt_info['operations']
                if not isinstance(ops_raw, list):
                    raise FileConfigError(f"'operations' for target {target_ip_port} must be a list")
                if not ops_raw:
                    raise FileConfigError(f"'operations' for target {target_ip_port} cannot be empty")

                parsed_ops = [
                    parse_operation_item(op_entry, protocol=proto_enum, step_index=idx)
                    for idx, op_entry in enumerate(ops_raw, 1)
                ]
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

            # possible Get, Set, Subscribe list
            t_sub_list = tgt_info.get('subscriptions', [])
            t_set_block = tgt_info.get('set', {})
            t_op = (tgt_info.get('operation', '') or tgt_info.get('type', '')).lower()

            t_raw_filter = tgt_info.get('filter', '')
            t_raw_config = tgt_info.get('config') or tgt_info.get('nc_config')
            if isinstance(t_set_block, dict) and not t_raw_config:
                t_raw_config = t_set_block.get('config') or t_set_block.get('nc_config')

            is_capability = t_op in ('capability', 'capabilities')
            is_get_schema = t_op in ('get-schema', 'get_schema') or bool(tgt_info.get('identifier'))
            is_subscribe = len(t_sub_list) > 0
            is_get = t_op in ('get', 'get-config', 'get_config') or bool(t_raw_filter)
            is_set = (
                bool(t_set_block) or t_op in ('set', 'edit-config', 'edit_config')
                or bool(t_raw_config)
            )

            # Avoid collision if is_set was triggered by default but is_capability or is_get_schema or is_subscribe or is_get matches
            if is_capability or is_get_schema or is_subscribe or is_get:
                if not (bool(t_set_block) or bool(t_raw_config) or t_op in ('set', 'edit-config', 'edit_config')):
                    is_set = False

            tgt_cnt = sum([1 for flag in [is_capability, is_get_schema, is_subscribe, is_get, is_set] if flag])
            if tgt_cnt > 1:
                raise FileConfigError(f"target {target_ip_port} holds two or more RPC types - only one type of RPC is allowed")

            conn = ConnectionConfig(
                target=str(resolved_target),
                username=t_username,
                password=t_password,
                security=global_security,
                insecure=t_insecure,
            )
            proto_enum = Protocol.GNMI if t_protocol.lower() == 'gnmi' else Protocol.NETCONF

            if is_capability:
                op_obj = CapabilitiesOperation()
            elif is_get_schema:
                ident = tgt_info.get('identifier') or (t_get_list[0] if t_get_list else '')
                op_obj = GetSchemaOperation(
                    identifier=ident,
                    version=tgt_info.get('version'),
                    format=tgt_info.get('format', 'yang') or 'yang',
                )
            elif is_subscribe:
                subs = d.get('subscriptions', {})
                for sub_name in t_sub_list:
                    if sub_name not in subs:
                        raise FileConfigError(f"subscription {sub_name} not found")
                    named_sub = subs[sub_name]

                    update_only = bool('update_only' in named_sub)
                    sub_details = named_sub.get('subscription', {})
                    paths = sub_details.get('path', [])
                    if isinstance(paths, str):
                        paths = [paths]
                    sub_mode = sub_details.get('mode', 'target_defined')
                    sample_interval = sub_details.get('sample_interval', 0)

                    try:
                        d_mode = resolve_delivery_mode(named_sub.get('mode'), sub_mode, default_sub_mode='target_defined')
                    except ValueError as e:
                        raise FileConfigError(f"subscription {sub_name}: {e}") from None

                    del_policy = DeliveryPolicy(
                        mode=d_mode,
                        interval=sample_interval,
                        heartbeat=0,
                        suppress_redundant=False,
                    )
                    if proto_enum == Protocol.NETCONF:
                        raw_filter = named_sub.get('filter') or sub_details.get('filter')
                        if raw_filter:
                            filter_str, f_type = extract_filter_expression(
                                raw_filter,
                                default_type=named_sub.get('filter_type'),
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
                            ),
                        )
                    session = SessionConfig(
                        connection=conn,
                        protocol=proto_enum,
                        operation=op_obj,
                        execution=ExecutionConfig(times=t_times, continue_on_error=t_continue_on_error),
                        schema=t_schema,
                    )
                    sessions.append(session)
                    session_device_ids.append(ref_device_id)
                continue

            elif is_get:
                paths = tgt_info.get('paths', [])
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
                    protocol_options=p_opts,
                )
            else: # Set / Edit-Config
                if proto_enum == Protocol.NETCONF and t_raw_config:
                    cfg_payload = read_payload(t_raw_config) if isinstance(t_raw_config, str) else str(t_raw_config)
                    nc_opts = NetconfOptions(
                        target_datastore=tgt_info.get('target_datastore', 'candidate'),
                        config=cfg_payload,
                        default_operation=tgt_info.get('default_operation', 'merge'),
                        error_option=tgt_info.get('error_option', 'stop-on-error'),
                        test_option=tgt_info.get('test_option'),
                        commit=tgt_info.get('commit', True),
                    )
                    op_obj = SetOperation(
                        changes=(),
                        protocol_options=nc_opts,
                    )
                else:
                    updates: List[Tuple[str, str]] = []
                    replaces: List[Tuple[str, str]] = []
                    deletes: List[str] = []
                    set_prefix = tgt_info.get('prefix', '')
                    set_encoding = tgt_info.get('encoding', 'json_ietf')

                    if t_set_block and isinstance(t_set_block, dict):
                        set_prefix = t_set_block.get('prefix', set_prefix)
                        set_encoding = t_set_block.get('encoding', set_encoding)
                        updates.extend(_normalize_mutation_entries(t_set_block.get('update')))
                        replaces.extend(_normalize_mutation_entries(t_set_block.get('replace')))
                        deletes.extend(_normalize_delete_entries(t_set_block.get('delete')))

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
                        protocol_options=p_opts,
                    )

            session = SessionConfig(
                connection=conn,
                protocol=proto_enum,
                operation=op_obj,
                execution=ExecutionConfig(times=t_times, continue_on_error=t_continue_on_error),
                schema=t_schema,
            )
            sessions.append(session)
            session_device_ids.append(ref_device_id)

        # Parsing outputs
        outputs: List[OutputConfig] = []
        output_group = d.get('outputs')
        if output_group is None:
            outputs.append(OutputConfig(
                format=OutputFormat.JSON,
                output_type=OutputType.STDOUT,
            ))
        else:
            for og_name, og_item in output_group.items():
                outputs.append(OutputConfig(
                    name=og_name,
                    format=OutputFormat(og_item['format']),
                    output_type=OutputType(og_item['output-type']),
                    path=og_item.get('path'),
                ))

        requests = [
            session_to_execution_request(sess, device_id=dev_id)
            for sess, dev_id in zip(sessions, session_device_ids)
        ]

        return ParsedConfig(
            sessions=sessions,
            targets=targets,
            outputs=outputs,
            debug=debug,
            log_level=d.get('log_level', 'ERROR'),
            syslog_server=d.get('syslog_server', ''),
            log_file=d.get('log_file', ''),
            requests=requests,
            device_registry=device_registry if raw_devices else None,
        )

