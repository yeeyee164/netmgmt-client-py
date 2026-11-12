from __future__ import annotations
import os
import json
import time
import queue
import logging
from typing import Iterator, Any, Optional, List, Union

try:
    import grpc
    from specs.gnmi import gnmi_pb2, gnmi_pb2_grpc
except ImportError:
    grpc = None
    gnmi_pb2 = None
    gnmi_pb2_grpc = None

from specs.base_client import BaseClient, UnsupportedOperationError
from specs.stream_types import StreamCommandType, StreamCommand, StreamContext, StreamEvent
from modules.path import parse_path
from modules.security import SecurityModule
from config.schema import SchemaConfig, SchemaProfile, ModuleRef
from config.operations import (
    CapabilitiesOperation,
    GetOperation,
    SetOperation,
    SubscribeOperation,
    ChangeType,
    Change,
)
from config.selectors import PathSelector
from config.delivery import DeliveryMode
from config.protocol_options import BaseProtocolOptions, GnmiOptions, GNMIOptions
from core.schema_manager import SchemaManager
from core.schema_errors import SchemaError, SchemaValidationError, InvalidSchemaPathError

NANOSECOND = 1000000000

logger = logging.getLogger(__name__)

if gnmi_pb2 is not None:
    MODE_TO_GNMI_LIST_MODE = {
        DeliveryMode.PERIODIC: gnmi_pb2.SubscriptionList.STREAM,
        DeliveryMode.EVENT_DRIVEN: gnmi_pb2.SubscriptionList.STREAM,
        DeliveryMode.SERVER_DETERMINED: gnmi_pb2.SubscriptionList.STREAM,
        DeliveryMode.SNAPSHOT: gnmi_pb2.SubscriptionList.ONCE,
        DeliveryMode.ON_DEMAND: gnmi_pb2.SubscriptionList.POLL,
    }
    MODE_TO_GNMI_SUB_MODE = {
        DeliveryMode.PERIODIC: gnmi_pb2.SubscriptionMode.SAMPLE,
        DeliveryMode.EVENT_DRIVEN: gnmi_pb2.SubscriptionMode.ON_CHANGE,
        DeliveryMode.SERVER_DETERMINED: gnmi_pb2.SubscriptionMode.TARGET_DEFINED,
    }
else:
    MODE_TO_GNMI_LIST_MODE = {}
    MODE_TO_GNMI_SUB_MODE = {}

class GNMIClient(BaseClient):
    """
    A gNMI client that support gNMI Services.

    Designed to be used as a context manager (with ... as client:)
    * Part of requests were injected by Worker class
    * It don't need to check validation about path formats - they're already checked by worker.
    """
    def __init__(
            self,
            target: str,
            username: str = "",
            password: str = "",
            security_module=None,
            **kwargs,
        ):
        self.target = target
        self.metadata = [("username", username), ("password", password)]
        if username and password:
            self.metadata = [
                ('username', username),
                ('password', password)
            ]

        # Configure from keyword arguments
        self.insecure = kwargs.get('insecure', False)
        self.security_module = security_module
        self.encoding = kwargs.get('encoding', "json_ietf")
        self.debug = kwargs.get('debug', False)
        schema_cfg = kwargs.get('schema_config') or kwargs.get('schema')
        self.schema_config = schema_cfg if isinstance(schema_cfg, SchemaConfig) else SchemaConfig()
        self.schema_profile: Optional[SchemaProfile] = None

        self.encoding_map = {
            'json': gnmi_pb2.JSON if gnmi_pb2 else 0,
            'json_ietf': gnmi_pb2.JSON_IETF if gnmi_pb2 else 4,
            'bytes': gnmi_pb2.BYTES if gnmi_pb2 else 1,
            'proto': gnmi_pb2.PROTO if gnmi_pb2 else 2,
            'ascii': gnmi_pb2.ASCII if gnmi_pb2 else 3,
        }

        self.channel = None
        self.stub = None

    def __enter__(self):
        """Initializes the gRPC channel and Stub when entering a 'with' block."""
        if self.insecure:
            self.channel = grpc.insecure_channel(self.target)
        elif self.security_module is not None:
            creds = self.security_module.get_grpc_credentials()
            options = self.security_module.get_grpc_options()
            self.channel = grpc.secure_channel(self.target, creds, options=options)
        else:
            raise NotImplementedError("present security information or run insecure mode")

        self.stub = gnmi_pb2_grpc.gNMIStub(self.channel)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Safely tears down the HTTP/2 connection."""
        if self.channel:
            self.channel.close()

    # =========================================================================
    # Semantic Intent Operations
    # =========================================================================

    def execute_capabilities(self, operation: CapabilitiesOperation) -> gnmi_pb2.CapabilityResponse:
        """Executes Capabilities RPC adhering to CapabilitiesOperation."""
        res = self.capability()
        if self.schema_config and self.schema_config.auto_discovery and hasattr(res, 'supported_models') and res.supported_models:
            try:
                sm = SchemaManager(global_cache_dir=self.schema_config.cache_dir)
                profile = sm.catalog.build_profile_from_gnmi_models(res.supported_models, strict=False)
                pid = (
                    getattr(self.schema_config, 'device_profile', None)
                    or getattr(self.schema_config, 'profile', None)
                    or f"gnmi_{self.target}"
                )
                if pid:
                    object.__setattr__(profile, "content_id", pid)
                sm.db.save_profile(profile)
                try:
                    sm.db.bind_device(device_key=self.target, protocol="gnmi", profile_id=profile.profile_id)
                except Exception:
                    pass
                self.schema_profile = profile
            except Exception as e:
                logger.debug(f"[GNMIClient] Auto-discovery from capabilities skipped: {e}")
        return res

    def execute_get(self, operation: GetOperation) -> gnmi_pb2.GetResponse:
        """Executes Get RPC adhering to GetOperation."""
        paths = []
        prefix = ""
        if isinstance(operation.selector, PathSelector):
            paths = list(operation.selector.paths)
            prefix = operation.selector.prefix

        if self.schema_config and self.schema_config.validate and paths:
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

        enc = self.encoding
        if isinstance(operation.protocol_options, GNMIOptions) and operation.protocol_options.encoding:
            enc = operation.protocol_options.encoding

        data_type = operation.read_scope or "all"
        return self.get(paths=paths, prefix=prefix, encoding=enc, data_type=data_type)

    def execute_set(self, operation: SetOperation) -> gnmi_pb2.SetResponse:
        """Executes Set RPC adhering to SetOperation and atomic Change models."""
        enc = self.encoding
        if isinstance(operation.protocol_options, GNMIOptions):
            if operation.protocol_options.encoding:
                enc = operation.protocol_options.encoding

        if self.schema_config and self.schema_config.validate:
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

            validated_changes = []
            for change in operation.changes:
                sm.validate_path(change.path, profile_id=self.schema_profile.profile_id, operation=operation)
                if change.value is not None:
                    sm.validate_payload(change.path, change.value, profile_id=self.schema_profile.profile_id, operation=operation)

                if enc.lower() == 'json_ietf' and change.value is not None and change.operation in (ChangeType.MERGE, ChangeType.REPLACE):
                    try:
                        serialized_json = sm.validate_and_serialize_gnmi_change(change, profile_id=self.schema_profile.profile_id)
                        new_change = Change(
                            path=change.path,
                            operation=change.operation,
                            value=serialized_json,
                            type="json_ietf",
                        )
                        validated_changes.append(new_change)
                    except Exception as e:
                        logger.warning(f"[GNMIClient] Serialization error, falling back to original value: {e}")
                        validated_changes.append(change)
                else:
                    validated_changes.append(change)
            changes_to_process = validated_changes
        else:
            changes_to_process = operation.changes

        updates = []
        replaces = []
        deletes = []

        for change in changes_to_process:
            change_type = getattr(change, 'type', None)
            if change.operation == ChangeType.MERGE:
                updates.append((change.path, change.value, change_type))
            elif change.operation == ChangeType.REPLACE:
                replaces.append((change.path, change.value, change_type))
            elif change.operation == ChangeType.DELETE:
                deletes.append(change.path)

        prefix = getattr(operation, 'prefix', '')
        return self.set(updates=updates, replaces=replaces, deletes=deletes, prefix=prefix, encoding=enc)

    def _build_subscribe_generator(
        self,
        selectors: Any = None,
        options: Optional[BaseProtocolOptions] = None,
        context: Optional[StreamContext] = None,
        operation: Optional[SubscribeOperation] = None,
        **kwargs,
    ) -> Iterator[gnmi_pb2.SubscribeRequest]:
        """Constructs initial SubscribeRequest and yields bi-directional streaming requests."""
        if gnmi_pb2 is None:
            raise RuntimeError("gnmi_pb2 is not loaded")

        if isinstance(selectors, SubscribeOperation):
            operation = selectors
            selectors = operation.selector

        paths = []
        prefix = ""
        list_mode = gnmi_pb2.SubscriptionList.STREAM
        gnmi_sub_mode = gnmi_pb2.SubscriptionMode.SAMPLE
        sample_interval_ns = 0
        heartbeat_interval_ns = 0
        suppress_redundant = False
        updates_only = False
        enc = self.encoding

        if operation is not None:
            if isinstance(operation.selector, PathSelector):
                paths = list(operation.selector.paths)
                prefix = operation.selector.prefix
            elif hasattr(operation.selector, 'paths'):
                paths = list(operation.selector.paths)

            d_mode = operation.delivery.mode
            list_mode = MODE_TO_GNMI_LIST_MODE.get(d_mode, gnmi_pb2.SubscriptionList.STREAM)
            gnmi_sub_mode = MODE_TO_GNMI_SUB_MODE.get(d_mode, gnmi_pb2.SubscriptionMode.TARGET_DEFINED)

            sample_interval_ns = (operation.delivery.interval or 0) * NANOSECOND
            heartbeat_interval_ns = (operation.delivery.heartbeat or 0) * NANOSECOND
            suppress_redundant = operation.delivery.suppress_redundant

            if options is None and operation.protocol_options is not None:
                options = operation.protocol_options
        else:
            if isinstance(selectors, PathSelector):
                paths = list(selectors.paths)
                prefix = selectors.prefix
            elif isinstance(selectors, (list, tuple)):
                for sel in selectors:
                    if isinstance(sel, PathSelector):
                        paths.extend(list(sel.paths))
                        if sel.prefix:
                            prefix = sel.prefix
                    elif isinstance(sel, str):
                        paths.append(sel)
                    elif hasattr(sel, 'paths'):
                        paths.extend(list(sel.paths))
            elif isinstance(selectors, str):
                paths = [selectors]

            legacy_mode = str(kwargs.get('mode', 'stream')).lower()
            if legacy_mode in ('once', 'snapshot'):
                list_mode = gnmi_pb2.SubscriptionList.ONCE
            elif legacy_mode in ('poll', 'on_demand'):
                list_mode = gnmi_pb2.SubscriptionList.POLL
            else:
                list_mode = gnmi_pb2.SubscriptionList.STREAM

            legacy_sub_mode = str(kwargs.get('stream_mode') or kwargs.get('sub_mode') or 'sample').lower()
            if legacy_sub_mode == 'sample':
                gnmi_sub_mode = gnmi_pb2.SubscriptionMode.SAMPLE
            elif legacy_sub_mode == 'on_change':
                gnmi_sub_mode = gnmi_pb2.SubscriptionMode.ON_CHANGE
            else:
                gnmi_sub_mode = gnmi_pb2.SubscriptionMode.TARGET_DEFINED

            if kwargs.get('prefix'):
                prefix = kwargs.get('prefix')
            if kwargs.get('sample_interval'):
                sample_interval_ns = int(kwargs.get('sample_interval')) * NANOSECOND
            if kwargs.get('heartbeat_interval'):
                heartbeat_interval_ns = int(kwargs.get('heartbeat_interval')) * NANOSECOND
            if kwargs.get('suppress_redundant'):
                suppress_redundant = bool(kwargs.get('suppress_redundant'))

        if options is not None:
            if hasattr(options, 'encoding') and options.encoding:
                enc = options.encoding.value if hasattr(options.encoding, 'value') else str(options.encoding)
            if hasattr(options, 'updates_only'):
                updates_only = bool(options.updates_only)
            if hasattr(options, 'sub_mode') and options.sub_mode and (operation is None or list_mode == gnmi_pb2.SubscriptionList.STREAM):
                sm = options.sub_mode.value if hasattr(options.sub_mode, 'value') else str(options.sub_mode).lower()
                if sm == 'sample':
                    gnmi_sub_mode = gnmi_pb2.SubscriptionMode.SAMPLE
                elif sm == 'on_change':
                    gnmi_sub_mode = gnmi_pb2.SubscriptionMode.ON_CHANGE
                elif sm == 'target_defined':
                    gnmi_sub_mode = gnmi_pb2.SubscriptionMode.TARGET_DEFINED
            if hasattr(options, 'sample_interval_ns') and options.sample_interval_ns > 0:
                sample_interval_ns = options.sample_interval_ns
            if hasattr(options, 'heartbeat_interval_ns') and options.heartbeat_interval_ns > 0:
                heartbeat_interval_ns = options.heartbeat_interval_ns
            if hasattr(options, 'suppress_redundant'):
                suppress_redundant = options.suppress_redundant

        sub_list = gnmi_pb2.SubscriptionList()
        sub_list.mode = list_mode
        sub_list.encoding = self.encoding_map.get(str(enc).lower(), gnmi_pb2.JSON_IETF if gnmi_pb2 else 4)
        sub_list.updates_only = bool(updates_only)

        if prefix:
            sub_list.prefix.CopyFrom(parse_path(prefix))

        for path_str in paths:
            sub = sub_list.subscription.add()
            sub.path.CopyFrom(parse_path(path_str))
            if sub_list.mode == gnmi_pb2.SubscriptionList.STREAM:
                sub.mode = gnmi_sub_mode
                if gnmi_sub_mode == gnmi_pb2.SubscriptionMode.SAMPLE:
                    sub.sample_interval = sample_interval_ns
                    if heartbeat_interval_ns:
                        sub.heartbeat_interval = heartbeat_interval_ns
                    sub.suppress_redundant = suppress_redundant
                elif gnmi_sub_mode == gnmi_pb2.SubscriptionMode.ON_CHANGE:
                    if heartbeat_interval_ns:
                        sub.heartbeat_interval = heartbeat_interval_ns
                else:
                    sub.mode = gnmi_pb2.SubscriptionMode.TARGET_DEFINED

        initial_req = gnmi_pb2.SubscribeRequest(subscribe=sub_list)
        yield initial_req

        if context is None:
            return

        while not context.is_cancelled:
            try:
                cmd = context.command_queue.get(timeout=0.2)
                if cmd.command_type == StreamCommandType.POLL:
                    poll_req = gnmi_pb2.SubscribeRequest()
                    poll_req.poll.SetInParent()
                    yield poll_req
                elif cmd.command_type == StreamCommandType.CANCEL:
                    break
            except queue.Empty:
                continue
            except GeneratorExit:
                break
            except Exception:
                break

    def execute_subscribe(
        self,
        selectors: Any = None,
        options: Optional[BaseProtocolOptions] = None,
        context: Optional[StreamContext] = None,
        operation: Optional[SubscribeOperation] = None,
        **kwargs,
    ) -> Iterator[StreamEvent]:
        """Executes a streaming telemetry subscription via gRPC Bidirectional Streaming.

        Yields:
            StreamEvent: Protocol-agnostic telemetry event wrapping SubscribeResponse or error.
        """
        if gnmi_pb2 is None:
            raise RuntimeError("gnmi_pb2 is not loaded")

        if context is None:
            context = StreamContext()

        req_generator = self._build_subscribe_generator(
            selectors=selectors,
            options=options,
            context=context,
            operation=operation,
            **kwargs,
        )

        try:
            response_stream = self.stub.Subscribe(req_generator, metadata=self.metadata)
            for response in response_stream:
                if context.is_cancelled:
                    cancel_func = getattr(response_stream, 'cancel', None)
                    if callable(cancel_func):
                        cancel_func()
                    break

                yield StreamEvent(
                    protocol="gnmi",
                    timestamp=time.time(),
                    raw_payload=response,
                    is_sync_marker=getattr(response, 'sync_response', False),
                )
        except grpc.RpcError as e:
            if context.is_cancelled:
                return
            yield StreamEvent(
                protocol="gnmi",
                timestamp=time.time(),
                raw_payload=None,
                error=e,
            )
        except Exception as e:
            if context.is_cancelled:
                return
            yield StreamEvent(
                protocol="gnmi",
                timestamp=time.time(),
                raw_payload=None,
                error=e,
            )

    # =========================================================================
    # Legacy Methods & Internal Helpers
    # =========================================================================

    def capability(self, **kwargs) -> gnmi_pb2.CapabilityResponse:
        """ Executes an Unary Capabilities RPC """
        request = gnmi_pb2.CapabilityRequest()
        response = self.stub.Capabilities(request, metadata=self.metadata)
        return response

    def get(self, **kwargs) -> gnmi_pb2.GetResponse:
        """ Executes an Unary Get RPC """
        prefix = kwargs.get('prefix', "")
        paths = kwargs.get('paths', [])
        type_arg = kwargs.get('type') or kwargs.get('data_type') or ''
        if isinstance(type_arg, str) and type_arg.lower() == 'all':
            type_arg = ''

        get_type = {
            'state': gnmi_pb2.GetRequest.STATE,
            'config': gnmi_pb2.GetRequest.CONFIG,
            'operational': gnmi_pb2.GetRequest.OPERATIONAL,
            '': gnmi_pb2.GetRequest.ALL,
        }

        enc = kwargs.get('encoding') or self.encoding
        request = gnmi_pb2.GetRequest(
            encoding=self.encoding_map[enc],
            type=get_type.get(type_arg.lower() if isinstance(type_arg, str) else '', gnmi_pb2.GetRequest.ALL)
        )
        
        if prefix:
            request.prefix.CopyFrom(parse_path(prefix))
            
        for p in paths:
            path_elem = request.path.add()
            path_elem.CopyFrom(parse_path(p))

        response = self.stub.Get(request, metadata=self.metadata)
        return response

    def _build_typed_val(self, val: Any, encoding: str = "json_ietf", val_type: Any = None) -> gnmi_pb2.TypedValue:
        """Translates Python inputs into gnmi_pb2.TypedValue adhering to gNMI Section 3.4."""
        if gnmi_pb2 is None:
            raise RuntimeError("gnmi_pb2 is not loaded")

        if isinstance(val, gnmi_pb2.TypedValue):
            return val

        tv = gnmi_pb2.TypedValue()

        # Handle explicit val_type if supplied
        # NOTE: it will be changed to enumeration during YANG Schema support 
        if val_type is not None:
            vt = str(val_type).lower().strip()
            if vt in ("string", "str"):
                tv.string_val = str(val)
                return tv
            elif vt in ("bool", "boolean"):
                if isinstance(val, str):
                    tv.bool_val = (val.lower() == 'true')
                else:
                    tv.bool_val = bool(val)
                return tv
            elif vt in ("int", "int8", "int16", "int32", "int64"):
                tv.int_val = int(val)
                return tv
            elif vt in ("uint", "uint8", "uint16", "uint32", "uint64"):
                tv.uint_val = int(val)
                return tv
            elif vt in ("float", "double"):
                tv.float_val = float(val)
                return tv
            elif vt in ("decimal", "decimal64"):
                if hasattr(val, 'digits') and hasattr(val, 'precision'):
                    tv.decimal_val.CopyFrom(val)
                    return tv
                tv.float_val = float(val)
                return tv
            elif vt in ("json", "json_raw"):
                raw_bytes = val.encode('utf-8') if isinstance(val, str) else json.dumps(val).encode('utf-8')
                tv.json_val = raw_bytes
                return tv
            elif vt in ("json_ietf", "json-ietf"):
                raw_bytes = val.encode('utf-8') if isinstance(val, str) else json.dumps(val).encode('utf-8')
                tv.json_ietf_val = raw_bytes
                return tv
            elif vt in ("bytes",):
                tv.bytes_val = val if isinstance(val, bytes) else str(val).encode('utf-8')
                return tv
            elif vt in ("ascii",):
                tv.ascii_val = str(val)
                return tv

        # 1. Check if string points to a file reference (@file or valid path)
        if isinstance(val, str):
            file_path = val[1:] if val.startswith('@') else val
            if os.path.isfile(file_path):
                try:
                    with open(file_path, 'rb') as f:
                        file_bytes = f.read()
                    try:
                        parsed_json = json.loads(file_bytes.decode('utf-8'))
                        if isinstance(parsed_json, (dict, list)):
                            if encoding == 'json':
                                tv.json_val = file_bytes
                            else:
                                tv.json_ietf_val = file_bytes
                            return tv
                    except Exception:
                        pass
                    tv.bytes_val = file_bytes
                    return tv
                except Exception as e:
                    logger.warning(f"[Client] Failed to read payload file {file_path}: {e}")

            # 2. Check if string is an inline JSON object or list
            try:
                parsed = json.loads(val)
                if isinstance(parsed, (dict, list)):
                    raw_bytes = val.encode('utf-8')
                    if encoding == 'json':
                        tv.json_val = raw_bytes
                    else:
                        tv.json_ietf_val = raw_bytes
                    return tv
            except (json.JSONDecodeError, ValueError):
                pass

            # 3. Check for boolean strings
            if val.lower() == 'true':
                tv.bool_val = True
                return tv
            elif val.lower() == 'false':
                tv.bool_val = False
                return tv

            # 4. Fallback for string
            tv.string_val = str(val)
            return tv

        # 5. Native Python booleans (MUST check before int)
        if isinstance(val, bool):
            tv.bool_val = val
            return tv

        # 6. Native Python integers
        if isinstance(val, int):
            if val >= 0:
                tv.uint_val = val
            else:
                tv.int_val = val
            return tv

        # 7. Native Python floats
        if isinstance(val, float):
            tv.float_val = val
            return tv

        # 8. Native Python dictionaries & lists
        if isinstance(val, (dict, list)):
            raw_bytes = json.dumps(val).encode('utf-8')
            if encoding == 'json':
                tv.json_val = raw_bytes
            else:
                tv.json_ietf_val = raw_bytes
            return tv

        # 9. Bytes
        if isinstance(val, bytes):
            tv.bytes_val = val
            return tv

        # 10. Fallback
        tv.string_val = str(val)
        return tv

    def set(self, updates: list = None, replaces: list = None, deletes: list = None, prefix: str = "", encoding: str = None, **kwargs) -> Any:
        """ Executes a Unary Set RPC adhering to gNMI Section 3.4 """
        if gnmi_pb2 is None:
            raise RuntimeError("gnmi_pb2 is not loaded")

        enc = encoding or self.encoding or "json_ietf"
        request = gnmi_pb2.SetRequest()

        if prefix:
            request.prefix.CopyFrom(parse_path(prefix))

        # Logical execution order per Section 3.4.3:
        # 1. delete
        for path in (deletes or []):
            del_elem = request.delete.add()
            del_elem.CopyFrom(parse_path(path))

        # 2. replace
        for item in (replaces or []):
            val_type = None
            if isinstance(item, (tuple, list)) and len(item) == 3:
                p, v, val_type = item
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                p, v = item
            elif isinstance(item, dict):
                for p, v in item.items():
                    rep_obj = request.replace.add()
                    rep_obj.path.CopyFrom(parse_path(p))
                    rep_obj.val.CopyFrom(self._build_typed_val(v, encoding=enc))
                continue
            elif isinstance(item, str) and ':::' in item:
                p, v = item.split(':::', 1)
            else:
                p, v = item, ""
            rep_obj = request.replace.add()
            rep_obj.path.CopyFrom(parse_path(p))
            rep_obj.val.CopyFrom(self._build_typed_val(v, encoding=enc, val_type=val_type))

        # 3. update
        for item in (updates or []):
            val_type = None
            if isinstance(item, (tuple, list)) and len(item) == 3:
                p, v, val_type = item
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                p, v = item
            elif isinstance(item, dict):
                for p, v in item.items():
                    upd_obj = request.update.add()
                    upd_obj.path.CopyFrom(parse_path(p))
                    upd_obj.val.CopyFrom(self._build_typed_val(v, encoding=enc))
                continue
            elif isinstance(item, str) and ':::' in item:
                p, v = item.split(':::', 1)
            else:
                p, v = item, ""
            upd_obj = request.update.add()
            upd_obj.path.CopyFrom(parse_path(p))
            upd_obj.val.CopyFrom(self._build_typed_val(v, encoding=enc, val_type=val_type))

        response = self.stub.Set(request, metadata=self.metadata)
        return response

    def _build_subscribe_request(self, config: dict) -> gnmi_pb2.SubscribeRequest:
        """ Constructs the SubscribeRequest object """
        sub_list = gnmi_pb2.SubscriptionList()
        mode = config.get('mode', 'stream').lower()

        if mode == 'once':
            sub_list.mode = gnmi_pb2.SubscriptionList.ONCE
        elif mode == 'poll':
            sub_list.mode = gnmi_pb2.SubscriptionList.POLL
        else:
            sub_list.mode = gnmi_pb2.SubscriptionList.STREAM

        sub_list.encoding = self.encoding_map[self.encoding]
        sub_list.updates_only = config.get('updates_only', False)

        prefix = config.get('prefix', '')
        if prefix:
            sub_list.prefix.CopyFrom(parse_path(prefix))

        for path_obj in config.get('paths', []):
            sub = sub_list.subscription.add()
            sub.path.CopyFrom(parse_path(path_obj))

            if mode == 'stream':
                smode = str(config.get('sub_mode') or config.get('stream_mode') or 'sample').lower()
                if smode == 'sample':
                    sub.mode = gnmi_pb2.SubscriptionMode.SAMPLE
                    sub.sample_interval = config.get('sample_interval', 0) * NANOSECOND
                elif smode == 'on_change':
                    sub.mode = gnmi_pb2.SubscriptionMode.ON_CHANGE
                else:
                    sub.mode = gnmi_pb2.SubscriptionMode.TARGET_DEFINED

        request = gnmi_pb2.SubscribeRequest(subscribe=sub_list)
        return request

    def subscribe(self, request_iterator) -> Iterator[gnmi_pb2.SubscribeResponse]:
        """
        Executes a Bidirectional Streaming Subscribe RPC.
        Instead of a single request, it takes an Iterator (or Generator) of SubscribeRequests.
        """
        def pb_generator():
            try:
                for req in request_iterator:
                    if req.get('action') == 'subscribe':
                        yield self._build_subscribe_request(req)
                    elif req.get('action') == 'poll':
                        poll_req = gnmi_pb2.SubscribeRequest()
                        poll_req.poll.SetInParent()
                        yield poll_req
            except Exception:
                if self.debug:
                    import traceback
                    traceback.print_exc()
                raise

        return self.stub.Subscribe(pb_generator(), metadata=self.metadata)
