# -*- coding: utf-8 -*-
"""
CLI argument parsers and action handlers for gNMI and NETCONF.
"""
import argparse
from typing import Any, List, Optional, Sequence, Tuple


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


def _parse_paired_options(args: Any, group_type: str) -> List[Tuple[str, str]]:
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


def netconf_args(parser: Any) -> None:
    """Constructs the NETCONF subcommand hierarchy and operations."""
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


def gnmi_args(parser: Any) -> None:
    """Constructs the gNMI subcommand hierarchy and operations."""
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


def build_args(args: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Builds and parses top-level CLI command line arguments."""
    parser = argparse.ArgumentParser(description="gNMI / NETCONF Client")

    parser.add_argument('-c', '--config', '-g', '--global-config', dest='config', default='', help='Path to YAML configuration file for this client')
    parser.add_argument('-t', '--target', action='append', help="List of targets in IP:PORT format")
    parser.add_argument('-d', '--debug', help="Debugging this script", action='store_true')
    parser.add_argument('--times', default=1, type=int, help="Generate duplicated requests - only use for testing")
    parser.add_argument('--continue-on-error', action='store_true', default=False,
                        help="Continue sequential execution despite operation errors")
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
