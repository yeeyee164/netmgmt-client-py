from __future__ import annotations
import os
import time
import logging
import urllib.parse
from datetime import datetime, timezone
from typing import Iterator, Any, Optional

try:
    from ncclient import manager, operations
except ImportError:
    manager = None
    operations = None

from specs.base_client import BaseClient, UnsupportedOperationError
from specs.stream_types import StreamContext, StreamEvent
from modules.security import SecurityModule
from config.schema import SchemaConfig, SchemaProfile, ModuleRef
from config.operations import (
    CapabilitiesOperation,
    GetOperation,
    SetOperation,
    SubscribeOperation,
    GetSchemaOperation,
    ChangeType,
    NetconfTransactionOperation,
    TransactionType,
)
from config.selectors import PathSelector, FilterSelector
from config.protocol_options import BaseProtocolOptions, NetconfOptions
from core.schema_manager import SchemaManager
from core.schema_errors import SchemaError, SchemaValidationError, InvalidSchemaPathError

RPCError = getattr(operations, 'RPCError', Exception) if operations else Exception

RPCError = getattr(operations, 'RPCError', Exception) if operations else Exception

NETCONF_BASE_NS = "urn:ietf:params:xml:ns:netconf:base:1.0"
NETCONF_NS = {
    'nc': NETCONF_BASE_NS
}

logger = logging.getLogger(__name__)

class NetconfClient(BaseClient):
    """
    A NETCONF Client utilizing ncclient.
    Currently accepts raw XPath strings or XML Subtree for its paths/filters.
    Future versions will utilize libyang to translate standard paths to XML Subtrees.
    """
    def __init__(self, target: str, username: str = "", password: str = "",
                 security_module=None, **kwargs):
        self.target = target
        self.username = username
        self.password = password
        self.security_module = security_module
        self.session = None
        self.device = kwargs.get('device', 'default')
        schema_cfg = kwargs.get('schema_config') or kwargs.get('schema')
        self.schema_config = schema_cfg if isinstance(schema_cfg, SchemaConfig) else SchemaConfig()
        self.schema_profile: Optional[SchemaProfile] = None

    def __enter__(self):
        host, port = self.target.split(':')
        
        netconf_info = {
            'host': host,
            'port': int(port),
            'username': self.username,
            'password': self.password,
            'device_params': {"name": self.device},
            'hostkey_verify': False
        }

        if self.security_module:
            ssh_kwargs = self.security_module.get_ssh_kwargs()
            if ssh_kwargs:
                netconf_info.update(ssh_kwargs)

        self.session = manager.connect(**netconf_info)
        if self.schema_config and self.schema_config.auto_discovery:
            try:
                self.discover_schema_profile()
            except Exception as e:
                logger.debug(f"[NetconfClient] Discovery during connect skipped: {e}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            try:
                if self.session.connected:
                    self.session.close_session()
            except Exception:
                pass

    # =========================================================================
    # Semantic Intent Operations
    # =========================================================================

    def discover_schema_profile(self) -> SchemaProfile:
        """
        Discovers server capabilities and datastores from the active NETCONF session,
        extracts YANG module references and revisions from capability URIs,
        constructs a deterministic SchemaProfile, and persists it in SchemaDB.
        """
        capabilities = []
        if self.session and hasattr(self.session, 'server_capabilities'):
            capabilities = list(self.session.server_capabilities)

        modules = []
        for cap in capabilities:
            try:
                parsed = urllib.parse.urlsplit(cap)
                if not parsed.query:
                    continue
                qs = urllib.parse.parse_qs(parsed.query)
                mod_name = qs.get('module', [None])[0]
                if mod_name:
                    rev = qs.get('revision', [None])[0]
                    features = tuple(f.strip() for f in qs.get('features', [''])[0].split(',') if f.strip())
                    deviations = tuple(d.strip() for d in qs.get('deviations', [''])[0].split(',') if d.strip())
                    ns = (parsed.scheme + '://' + parsed.netloc + parsed.path) if parsed.scheme else parsed.path
                    modules.append(ModuleRef(name=mod_name, revision=rev, namespace=ns))
            except Exception as e:
                logger.debug(f"[NetconfClient] Error parsing capability '{cap}': {e}")

        datastores = ["running"]
        if any(':candidate' in c for c in capabilities):
            datastores.append("candidate")
        if any(':startup' in c for c in capabilities):
            datastores.append("startup")

        profile = SchemaProfile(
            modules=tuple(modules),
            datastore_schemas=tuple(datastores),
            content_id=(
                getattr(self.schema_config, 'device_profile', None)
                or getattr(self.schema_config, 'profile', None)
            ),
        )

        try:
            sm = SchemaManager(global_cache_dir=self.schema_config.cache_dir)
            sm.db.save_profile(profile)
            sm.db.bind_device(device_key=self.target, protocol="netconf", profile_id=profile.profile_id)
        except Exception as e:
            logger.debug(f"[NetconfClient] Error saving discovered profile to SchemaDB: {e}")

        self.schema_profile = profile
        return profile

    def _extract_schema_content(self, reply: Any) -> Optional[str]:
        """Extracts raw YANG schema text from an ncclient get_schema reply or XML string."""
        if reply is None:
            return None
        if hasattr(reply, 'data') and reply.data:
            return reply.data
        if hasattr(reply, '_data') and reply._data:
            return reply._data
        if isinstance(reply, str):
            if "<data" in reply:
                try:
                    from xml.etree import ElementTree as ET
                    root = ET.fromstring(reply)
                    for elem in root.iter():
                        if elem.tag.endswith('data') and elem.text:
                            return elem.text
                except Exception:
                    pass
            return reply
        if hasattr(reply, 'xml'):
            try:
                from xml.etree import ElementTree as ET
                root = ET.fromstring(reply.xml)
                for elem in root.iter():
                    if elem.tag.endswith('data') and elem.text:
                        return elem.text
            except Exception:
                pass
        return None

    def get_schema(self, identifier: str, version: Optional[str] = None, format: Optional[str] = None) -> Any:
        """
        Executes RFC 6022 <get-schema> RPC, caches the resulting YANG module in SchemaDB,
        and returns the result.
        """
        fmt = format
        if fmt and fmt.lower() == 'yang':
            fmt = None
        res = self.session.get_schema(
            identifier=identifier,
            version=version or None,
            format=fmt
        )
        content = self._extract_schema_content(res)
        if content:
            try:
                sm = SchemaManager(global_cache_dir=self.schema_config.cache_dir)
                sm.db.store_module_content(
                    name=identifier,
                    revision=version,
                    content=content,
                    source="netconf_get_schema",
                    verify_with_libyang=False,
                )
            except Exception as e:
                logger.debug(f"[NetconfClient] Error storing downloaded schema in SchemaDB: {e}")
        return res

    def execute_capabilities(self, operation: CapabilitiesOperation) -> Any:
        """Executes Capabilities discovery based on CapabilitiesOperation."""
        return self.capability()

    def execute_schema(self, operation: GetSchemaOperation) -> Any:
        """Executes schema retrieval based on GetSchemaOperation."""
        try:
            return self.get_schema(
                identifier=operation.identifier,
                version=operation.version or None,
                format=operation.format
            )
        except Exception as e:
            return e

    def execute_get(self, operation: GetOperation) -> Any:
        """Executes <get> or <get-config> based on GetOperation."""
        filter_arg = None
        paths = []
        if isinstance(operation.selector, FilterSelector):
            filter_arg = operation.selector.expression
        elif isinstance(operation.selector, PathSelector):
            paths = list(operation.selector.paths)

        if self.schema_config and self.schema_config.validate and paths:
            if self.schema_profile is None:
                if self.schema_config.auto_discovery and self.session:
                    try:
                        self.discover_schema_profile()
                    except Exception:
                        pass
                if self.schema_profile is None:
                    pid = (
                        getattr(self.schema_config, 'device_profile', None)
                        or getattr(self.schema_config, 'profile', None)
                    )
                    self.schema_profile = SchemaProfile(content_id=pid)
            sm = SchemaManager(global_cache_dir=self.schema_config.cache_dir)
            sm.get_or_create_context(
                profile=self.schema_profile,
                search_dirs=self.schema_config.yang_dirs,
                strict=True,
            )
            for p in paths:
                sm.validate_path(path=p, profile_id=self.schema_profile.profile_id, operation=operation)

        source = "running"
        if isinstance(operation.protocol_options, NetconfOptions):
            source = operation.protocol_options.source or "running"

        op_name = "get-config" if operation.read_scope == "config" else "get"
        return self.get(
            operation=op_name,
            source=source,
            filter=filter_arg,
            paths=paths
        )

    def execute_set(self, operation: SetOperation) -> Any:
        """Executes <edit-config> within an RFC 6241 candidate transaction workflow."""
        target_ds = "candidate"
        default_op = "merge"
        error_opt = "stop-on-error"
        test_opt = None
        commit = True
        raw_config = None
        lock_target = False
        validate_candidate = False
        confirmed = False
        confirm_timeout = None
        persist = ""
        persist_id = ""

        if isinstance(operation.protocol_options, NetconfOptions):
            target_ds = operation.protocol_options.target_datastore
            default_op = operation.protocol_options.default_operation
            error_opt = operation.protocol_options.error_option
            test_opt = operation.protocol_options.test_option
            commit = operation.protocol_options.commit
            raw_config = operation.protocol_options.config
            lock_target = operation.protocol_options.lock_target
            validate_candidate = operation.protocol_options.validate_candidate
            confirmed = operation.protocol_options.confirmed
            confirm_timeout = operation.protocol_options.confirm_timeout
            persist = operation.protocol_options.persist
            persist_id = operation.protocol_options.persist_id

        updates = []
        replaces = []
        deletes = []
        creates = []
        removes = []
        for change in operation.changes:
            val = change.value if change.value is not None else change.path
            if change.operation == ChangeType.MERGE:
                updates.append(val)
            elif change.operation == ChangeType.REPLACE:
                replaces.append(val)
            elif change.operation == ChangeType.DELETE:
                deletes.append(val)
            elif change.operation == ChangeType.CREATE:
                creates.append(val)
            elif change.operation == ChangeType.REMOVE:
                removes.append(val)

        if self.schema_config and self.schema_config.validate:
            if self.schema_profile is None:
                if self.schema_config.auto_discovery and self.session:
                    try:
                        self.discover_schema_profile()
                    except Exception:
                        pass
                if self.schema_profile is None:
                    pid = (
                        getattr(self.schema_config, 'device_profile', None)
                        or getattr(self.schema_config, 'profile', None)
                    )
                    self.schema_profile = SchemaProfile(content_id=pid)

            sm = SchemaManager(global_cache_dir=self.schema_config.cache_dir)
            sctx = sm.get_or_create_context(
                profile=self.schema_profile,
                search_dirs=self.schema_config.yang_dirs,
                strict=True,
            )

            fetch_cb = (
                (lambda name, rev: self._extract_schema_content(self.get_schema(name, rev)))
                if self.schema_config.auto_fetch
                else None
            )

            if not raw_config and operation.changes:
                if fetch_cb and self.schema_profile.modules:
                    for m in self.schema_profile.modules:
                        try:
                            sm.resolver.ensure_module(
                                context=sctx,
                                module_name=m.name,
                                revision=m.revision,
                                search_dirs=self.schema_config.yang_dirs,
                                fetch_callback=fetch_cb,
                            )
                        except Exception as exc:
                            logger.warning(
                                f"Failed to ensure module '{m.name}' via fetch callback: {exc}"
                            )

                raw_config = sm.validate_and_serialize_netconf_edit(
                    changes=operation.changes,
                    profile_id=self.schema_profile.profile_id,
                    default_operation=default_op,
                )
                updates = []
                replaces = []
                deletes = []
                creates = []
                removes = []
            elif operation.changes:
                for ch in operation.changes:
                    sm.validate_path(path=ch.path, profile_id=self.schema_profile.profile_id, operation=operation)
                    if ch.value is not None:
                        sm.validate_payload(path=ch.path, value=ch.value, profile_id=self.schema_profile.profile_id, operation=operation)

        # Datastore capability fallback
        if target_ds == 'candidate' and not self._has_capability(':candidate'):
            logger.warning("[NetconfClient] Server does not advertise :candidate capability. Falling back to 'running'.")
            target_ds = 'running'

        locked = False
        try:
            if lock_target:
                self.lock(target=target_ds)
                locked = True

            res = self.set(
                config=raw_config,
                target_datastore=target_ds,
                default_operation=default_op,
                error_option=error_opt,
                test_option=test_opt,
                commit=False,
                updates=updates or None,
                replaces=replaces or None,
                deletes=deletes or None,
                creates=creates or None,
                removes=removes or None,
            )
            if isinstance(res, Exception):
                raise res

            if target_ds == 'candidate':
                if validate_candidate:
                    self.validate(source='candidate')
                if commit:
                    self.commit(
                        confirmed=confirmed,
                        confirm_timeout=confirm_timeout,
                        persist=persist or None,
                        persist_id=persist_id or None,
                    )

            return res

        except Exception as e:
            if target_ds == 'candidate' and self._has_capability(':candidate'):
                try:
                    self.discard_changes()
                except Exception as discard_err:
                    logger.warning(f"[NetconfClient] Failed to discard candidate changes during cleanup: {discard_err}")
            raise
        finally:
            if locked:
                try:
                    self.unlock(target=target_ds)
                except Exception as unlock_err:
                    logger.warning(f"[NetconfClient] Failed to unlock {target_ds} in finally: {unlock_err}")

    def execute_transaction(self, operation: NetconfTransactionOperation) -> Any:
        """Executes explicit transaction lifecycle operations based on NetconfTransactionOperation."""
        op_type = operation.operation
        if op_type == TransactionType.LOCK:
            return self.lock(target=operation.target_datastore)
        elif op_type == TransactionType.UNLOCK:
            return self.unlock(target=operation.target_datastore)
        elif op_type == TransactionType.COMMIT:
            return self.commit(
                confirmed=operation.confirmed,
                confirm_timeout=operation.confirm_timeout,
                persist=operation.persist or None,
                persist_id=operation.persist_id or None,
            )
        elif op_type == TransactionType.CANCEL_COMMIT:
            return self.cancel_commit(persist_id=operation.persist_id or None)
        elif op_type == TransactionType.DISCARD_CHANGES:
            return self.discard_changes()
        elif op_type == TransactionType.VALIDATE:
            return self.validate(source=operation.source_datastore)
        else:
            raise UnsupportedOperationError(
                f"[NetconfClient] Unknown transaction operation: {op_type}"
            )

    def execute_subscribe(
        self,
        selectors: Any = None,
        options: Optional[BaseProtocolOptions] = None,
        context: Optional[StreamContext] = None,
        operation: Optional[SubscribeOperation] = None,
        **kwargs,
    ) -> Iterator[StreamEvent]:
        """Executes NETCONF Event Notifications (RFC 5277 / RFC 8639) based on SubscribeOperation.
        Planned for a future milestone.
        """
        raise UnsupportedOperationError(
            f"{self.__class__.__name__} does not support subscribe operations."
        )

    # =========================================================================
    # Legacy Methods & Internal Helpers
    # =========================================================================

    def capability(self, **kwargs) -> Any:
        """NETCONF exchanges <hello> when the session has established"""
        return list(self.session.server_capabilities)

    def get(self, **kwargs) -> Any:
        """
        Executes a NETCONF <get>, <get-config>, <get-schema>.
        """
        try:
            paths = kwargs.get('nc_xpath') or kwargs.get('path') or kwargs.get('paths') or []
            if isinstance(paths, str):
                paths = [paths]

            op = kwargs.get('operation', 'get').lower()

            identifier = kwargs.get('identifier')
            if op == 'get-schema' or identifier:
                ident = identifier or (paths[0] if paths else None)
                if not ident:
                    raise ValueError("An Identifier (YANG module name) is required for <get-schema>")

                version = kwargs.get('version') or None
                fmt = kwargs.get('schema_format') or kwargs.get('format')
                if fmt and fmt.lower() == 'yang':
                    fmt = None

                return self.session.get_schema(identifier=ident, version=version, format=fmt)

            source = kwargs.get('source', '')
            raw_filter = kwargs.get('filter', '')
            filter_xml = ""

            if raw_filter and os.path.isfile(raw_filter):
                try:
                    with open(raw_filter, 'r', encoding='utf-8') as f:
                        raw_filter = f.read().strip()
                except Exception as e:
                    logger.warning(f"[NetconfClient] Failed to read filter file '{raw_filter}': {e}")

            if raw_filter:
                stripped = raw_filter.strip()
                if stripped.startswith('<'):
                    if stripped.startswith('<filter'):
                        filter_xml = stripped
                    else:
                        filter_xml = f'<filter xmlns="{NETCONF_BASE_NS}" type="subtree">{stripped}</filter>'
                else:
                    filter_xml = f'<filter type="xpath" select="{stripped}"/>'
            elif paths:
                xpath_filter = " | ".join(paths)
                filter_xml = f"""<filter type="xpath" select="{xpath_filter}"/>"""

            if 'type="xpath"' in filter_xml:
                if not any(':xpath' in cap for cap in self.session.server_capabilities):
                    logger.warning("[NetconfClient] Server does not advertise :xpath capability. Request may fail.")

            logger.debug(f"[NetconfClient] Operation: {op}, source: {source}")

            if op == 'get-config':
                src = source if source else 'running'
                if filter_xml:
                    return self.session.get_config(source=src, filter=filter_xml)
                else:
                    return self.session.get_config(source=src)
            else:
                if filter_xml:
                    return self.session.get(filter=filter_xml)
                else:
                    return self.session.get()
        except RPCError as e:
            return e

    def _has_capability(self, name: str) -> bool:
        if not self.session or not hasattr(self.session, 'server_capabilities'):
            return False
        return any(name in cap for cap in self.session.server_capabilities)

    def _check_capability(self, name: str) -> None:
        if not self._has_capability(name):
            raise UnsupportedOperationError(
                f"[NetconfClient] Server does not advertise required capability '{name}'."
            )

    def lock(self, target: str = "candidate") -> Any:
        return self.session.lock(target=target)

    def unlock(self, target: str = "candidate") -> Any:
        return self.session.unlock(target=target)

    def commit(
        self,
        confirmed: bool = False,
        confirm_timeout: Optional[int] = None,
        persist: Optional[str] = None,
        persist_id: Optional[str] = None,
    ) -> Any:
        self._check_capability(":candidate")
        kwargs: dict[str, Any] = {}
        if confirmed:
            self._check_capability(":confirmed-commit")
            kwargs["confirmed"] = True
            if confirm_timeout is not None:
                kwargs["timeout"] = str(confirm_timeout)
            if persist:
                kwargs["persist"] = persist
        if persist_id:
            kwargs["persist_id"] = persist_id

        return self.session.commit(**kwargs)

    def cancel_commit(self, persist_id: Optional[str] = None) -> Any:
        self._check_capability(":candidate")
        self._check_capability(":confirmed-commit")
        kwargs = {"persist_id": persist_id} if persist_id else {}
        return self.session.cancel_commit(**kwargs)

    def discard_changes(self) -> Any:
        self._check_capability(":candidate")
        return self.session.discard_changes()

    def validate(self, source: str = "candidate") -> Any:
        self._check_capability(":validate")
        return self.session.validate(source=source)

    def set(self,
            updates: Optional[list] = None,
            replaces: Optional[list] = None,
            deletes: Optional[list] = None,
            creates: Optional[list] = None,
            removes: Optional[list] = None,
            **kwargs) -> Any:
        """
        Executes a NETCONF <edit-config>.
        Handles raw XML strings, file paths, and structured config elements.
        """
        try:
            raw_config = kwargs.get('config') or kwargs.get('nc_config', '')
            if raw_config and os.path.isfile(raw_config):
                try:
                    with open(raw_config, 'r', encoding='utf-8') as f:
                        raw_config = f.read().strip()
                except Exception as e:
                    logger.warning(f"[NetconfClient] Failed to read config file '{raw_config}': {e}")

            extracted_target = None
            extracted_default_op = None
            extracted_test_op = None
            extracted_error_op = None
            config_xml = ""

            if raw_config:
                stripped = raw_config.strip()
                if stripped.startswith('<'):
                    try:
                        from lxml import etree
                        root = etree.fromstring(stripped.encode('utf-8'))
                        if root.tag.endswith('rpc'):
                            edit_cfg = root.find('nc:edit-config', namespaces=NETCONF_NS)
                            if edit_cfg is None:
                                edit_cfg = root.find('edit-config')
                            if edit_cfg is not None:
                                root = edit_cfg
                        if root.tag.endswith('edit-config'):
                            target_el = root.find('nc:target', namespaces=NETCONF_NS)
                            if target_el is None:
                                target_el = root.find('target')
                            if target_el is not None and len(target_el) > 0:
                                extracted_target = etree.QName(target_el[0]).localname
                            def_op_el = root.find('nc:default-operation', namespaces=NETCONF_NS)
                            if def_op_el is None:
                                def_op_el = root.find('default-operation')
                            if def_op_el is not None:
                                extracted_default_op = def_op_el.text
                            test_op_el = root.find('nc:test-option', namespaces=NETCONF_NS)
                            if test_op_el is None:
                                test_op_el = root.find('test-option')
                            if test_op_el is not None and test_op_el.text:
                                extracted_test_op = test_op_el.text.strip()
                            err_op_el = root.find('nc:error-option', namespaces=NETCONF_NS)
                            if err_op_el is None:
                                err_op_el = root.find('error-option')
                            if err_op_el is not None and err_op_el.text:
                                extracted_error_op = err_op_el.text.strip()
                            config_elem = root.find('nc:config', namespaces=NETCONF_NS)
                            if config_elem is None:
                                config_elem = root.find('config')
                            if config_elem is not None:
                                config_xml = etree.tostring(config_elem, encoding='unicode')
                        elif root.tag.endswith('config'):
                            config_xml = etree.tostring(root, encoding='unicode')
                        else:
                            config_xml = f'<config xmlns="{NETCONF_BASE_NS}">\n{stripped}\n</config>'
                    except Exception:
                        if stripped.startswith('<config'):
                            config_xml = stripped
                        else:
                            config_xml = f'<config xmlns="{NETCONF_BASE_NS}">\n{stripped}\n</config>'
                else:
                    config_xml = f'<config xmlns="{NETCONF_BASE_NS}">\n{stripped}\n</config>'
            else:
                def _tag_snippet(snippet: Any, op_name: str) -> str:
                    snip_str = str(snippet).strip()
                    if not snip_str:
                        return ""
                    if snip_str.startswith('<'):
                        try:
                            from lxml import etree
                            node = etree.fromstring(snip_str.encode('utf-8'))
                            if op_name:
                                node.set('{urn:ietf:params:xml:ns:netconf:base:1.0}operation', op_name)
                            return etree.tostring(node, encoding='unicode')
                        except Exception:
                            return snip_str
                    return snip_str

                xml_snippets = []
                for c in (creates or []):
                    xml_snippets.append(_tag_snippet(c, 'create'))
                for rm in (removes or []):
                    xml_snippets.append(_tag_snippet(rm, 'remove'))
                for u in (updates or []):
                    xml_snippets.append(_tag_snippet(u, 'merge'))
                for r in (replaces or []):
                    xml_snippets.append(_tag_snippet(r, 'replace'))
                for d in (deletes or []):
                    xml_snippets.append(_tag_snippet(d, 'delete'))

                inner = "\n".join(s for s in xml_snippets if s)
                config_xml = f'<config xmlns="{NETCONF_BASE_NS}" xmlns:nc="{NETCONF_BASE_NS}">\n{inner}\n</config>'

            target_ds = kwargs.get('target_datastore') or kwargs.get('target') or extracted_target or 'candidate'

            if target_ds == 'candidate':
                if not any(':candidate' in cap for cap in self.session.server_capabilities):
                    logger.warning("[NetconfClient] Server does not advertise :candidate capability. Falling back to 'running'.")
                    target_ds = 'running'

            default_op = kwargs.get('default_operation') or kwargs.get('default_op') or extracted_default_op or 'merge'
            default_op = default_op.lower() if default_op else 'merge'
            if default_op not in ['merge', 'replace', 'none']:
                default_op = 'merge'

            error_option = kwargs.get('error_option') or extracted_error_op or 'stop-on-error'
            test_option = kwargs.get('test_option') or extracted_test_op

            logger.debug(f"[NetconfClient] edit-config target={target_ds}, default_op={default_op}, error_option={error_option}")
            edit_kwargs = {
                'target': target_ds,
                'config': config_xml,
                'default_operation': default_op,
                'error_option': error_option
            }
            if test_option:
                edit_kwargs['test_option'] = test_option

            res = self.session.edit_config(**edit_kwargs)
            return res
        except RPCError as e:
            return e

    def _parse_event_time_to_posix(self, event_time_str: Optional[str]) -> float:
        """Converts an RFC 3339 / ISO 8601 eventTime string into a POSIX timestamp float."""
        if not event_time_str:
            return time.time()
        try:
            clean_str = event_time_str.strip().replace('Z', '+00:00')
            dt = datetime.fromisoformat(clean_str)
            return dt.timestamp()
        except Exception:
            return time.time()

    def _build_subscription_filter(self, selector: Any) -> Optional[str]:
        """
        Constructs an RFC 5277 compliant filter XML string suitable for
        ncclient.create_subscription. Ensures the <filter> element resides
        in 'urn:ietf:params:xml:ns:netconf:notification:1.0'.
        """
        if selector is None:
            return None

        notif_ns = 'xmlns="urn:ietf:params:xml:ns:netconf:notification:1.0"'

        def _wrap_filter(content: str, filter_type: str = "subtree") -> str:
            content = content.strip()
            if not content:
                return ""
            if content.startswith("<filter"):
                if "xmlns=" not in content:
                    return content.replace("<filter", f"<filter {notif_ns}", 1)
                elif "urn:ietf:params:xml:ns:netconf:base:1.0" in content:
                    return content.replace(
                        "urn:ietf:params:xml:ns:netconf:base:1.0",
                        "urn:ietf:params:xml:ns:netconf:notification:1.0"
                    )
                return content
            if filter_type == "xpath":
                return f'<filter {notif_ns} type="xpath" select="{content}"/>'
            return f'<filter {notif_ns} type="subtree">{content}</filter>'

        if isinstance(selector, FilterSelector):
            content = (selector.expression or "").strip()
            if not content:
                return None
            f_type = getattr(selector, "filter_type", "subtree")
            return _wrap_filter(content, filter_type=f_type)

        if isinstance(selector, PathSelector):
            paths = list(selector.paths) if hasattr(selector, "paths") else []
            if not paths:
                return None
            first = paths[0].strip()
            if first.startswith("<"):
                return _wrap_filter(first, filter_type="subtree")
            return _wrap_filter(first, filter_type="xpath")

        if isinstance(selector, (list, tuple)):
            if not selector:
                return None
            first = selector[0]
            if isinstance(first, str):
                s = first.strip()
                if not s:
                    return None
                if s.startswith("<"):
                    return _wrap_filter(s, filter_type="subtree")
                return _wrap_filter(s, filter_type="xpath")

        if isinstance(selector, str):
            s = selector.strip()
            if not s:
                return None
            if s.startswith("<"):
                return _wrap_filter(s, filter_type="subtree")
            return _wrap_filter(s, filter_type="xpath")

        return None


    def execute_subscribe(
        self,
        selectors: Any = None,
        options: Optional[BaseProtocolOptions] = None,
        context: Optional[StreamContext] = None,
        operation: Optional[SubscribeOperation] = None,
        **kwargs,
    ) -> Iterator[StreamEvent]:
        """
        Executes an RFC 5277 NETCONF event notification subscription.
        Performs synchronous capability verification before returning generator.
        """
        server_caps = list(getattr(self.session, 'server_capabilities', [])) if self.session else []
        if not self.session or not any("urn:ietf:params:netconf:capability:notification:1.0" in c for c in server_caps):
            raise UnsupportedOperationError(
                "NetconfClient does not support subscribe operations: Target NETCONF server does not support RFC 5277 (:notification:1.0)."
            )

        return self._run_subscribe_generator(
            selectors=selectors,
            options=options,
            context=context,
            operation=operation,
            **kwargs,
        )

    def _run_subscribe_generator(
        self,
        selectors: Any = None,
        options: Optional[BaseProtocolOptions] = None,
        context: Optional[StreamContext] = None,
        operation: Optional[SubscribeOperation] = None,
        **kwargs,
    ) -> Iterator[StreamEvent]:
        if context is None:
            context = StreamContext()

        # 2. Resolve parameters from operation, options, or kwargs
        opts = options or (operation.protocol_options if operation else None)
        stream_name = getattr(opts, 'stream_name', 'NETCONF') if opts else 'NETCONF'
        start_time = getattr(opts, 'start_time', None) if opts else None
        stop_time = getattr(opts, 'stop_time', None) if opts else None
        poll_timeout = float(getattr(opts, 'poll_timeout', 1.0) if opts else 1.0)

        if 'stream_name' in kwargs:
            stream_name = kwargs['stream_name']
        if 'start_time' in kwargs:
            start_time = kwargs['start_time']
        if 'stop_time' in kwargs:
            stop_time = kwargs['stop_time']
        if 'poll_timeout' in kwargs:
            poll_timeout = float(kwargs['poll_timeout'])

        sel = operation.selector if operation else selectors
        nc_filter = self._build_subscription_filter(sel)

        logger.info(
            f"[NetconfClient {self.target}] Creating RFC 5277 subscription on stream '{stream_name}'..."
        )

        # 3. Dispatch <create-subscription> RPC
        try:
            sub_kwargs = {
                'stream_name': stream_name,
            }
            if nc_filter is not None:
                sub_kwargs['filter'] = nc_filter
            if start_time is not None:
                sub_kwargs['start_time'] = start_time
            if stop_time is not None:
                sub_kwargs['stop_time'] = stop_time

            reply = self.session.create_subscription(**sub_kwargs)
            if not getattr(reply, 'ok', True):
                err = getattr(reply, 'error', 'Unknown error')
                raise RPCError(f"create-subscription failed: {err}")
        except Exception as e:
            if context.is_cancelled:
                return
            yield StreamEvent(protocol="netconf", timestamp=time.time(), raw_payload=None, error=e)
            return

        # 4. Continuous Notification Consumer Loop
        while not context.is_cancelled:
            try:
                notif = self.session.take_notification(block=True, timeout=poll_timeout)
                if notif is None:
                    continue

                raw_xml = getattr(notif, 'notification_xml', '')
                event_time_str = getattr(notif, 'event_time', None)
                ts = self._parse_event_time_to_posix(event_time_str)

                # Parse XML to dictionary for structured handlers
                event_dict = None
                if raw_xml:
                    try:
                        import xmltodict
                        event_dict = xmltodict.parse(raw_xml)
                    except Exception as parse_err:
                        logger.debug(f"[NetconfClient {self.target}] xmltodict parse warning: {parse_err}")

                is_replay_complete = "<replayComplete" in raw_xml
                is_notif_complete = "<notificationComplete" in raw_xml

                yield StreamEvent(
                    protocol="netconf",
                    timestamp=ts,
                    raw_payload=raw_xml,
                    data=event_dict,
                    is_sync_marker=is_replay_complete,
                )

                # Termination on bounded replay complete
                if is_notif_complete:
                    logger.info(
                        f"[NetconfClient {self.target}] Reached <notificationComplete/>. Stream finished."
                    )
                    break

            except Exception as e:
                if context.is_cancelled:
                    break
                yield StreamEvent(protocol="netconf", timestamp=time.time(), raw_payload=None, error=e)
                break

    def subscribe(self, *args, **kwargs) -> Iterator[Any]:
        """Backward-compatible delegation to execute_subscribe."""
        return self.execute_subscribe(*args, **kwargs)


