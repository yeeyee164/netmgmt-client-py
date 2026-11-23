from __future__ import annotations
import json
import base64
import datetime
from abc import ABC, abstractmethod

from typing import Any

try:
    from google.protobuf import text_format
    from specs.gnmi import gnmi_pb2
    from specs.gnmi.gnmi_pb2 import GetResponse, SetResponse, CapabilityResponse, SubscribeResponse
    GNMI_ENCODING_TO_STR = {
        gnmi_pb2.JSON_IETF: "json_ietf",
        gnmi_pb2.JSON: "json",
        gnmi_pb2.PROTO: "proto",
        gnmi_pb2.ASCII: "ascii",
        gnmi_pb2.BYTES: "bytes",
    }
except ImportError:
    text_format = None
    gnmi_pb2 = None
    GetResponse = None
    SetResponse = None
    CapabilityResponse = None
    SubscribeResponse = None
    GNMI_ENCODING_TO_STR = {}

from modules.path import gnmi_path_to_xpath

class ProtocolFormatter(ABC):
    """
    Abstract base class for Protocl formatters

    Ensures that any future northbound protocol can securely
    translate its native payloads into standard representations.
    """

    @abstractmethod
    def format_json(self, raw_data):
        """Translates the native protocol data into JSON/Python dict"""
        pass

    @abstractmethod
    def format_text(self, raw_data):
        """
        Translates the native protocol data into text
        
        For all protocol except gNMI, it will prints exactly same format as
        that protocol intended.
        """
        pass

    @abstractmethod
    def format_xml(self, raw_data):
        """Translates the native protocol data into XML"""
        pass

# =====================
# gNMI output formatter
# =====================

class GNMIFormatter(ProtocolFormatter):
    """
    Handles gNMI-specific Protobuf translations, including RFC 7951 (json_ietf) 
    formatting and deeply-nested Base64 JSON decoding.
    """

    def extract_value(self, tv):
        """Safely extracts and unpacks a gnmi_pb2.TypedValue"""
        if tv.HasField('json_val'):
            try: return json.loads(tv.json_val.decode('utf-8'))
            except: return tv.json_val.decode('utf-8')
        elif tv.HasField('json_ietf_val'):
            try: return json.loads(tv.json_ietf_val.decode('utf-8'))
            except: return tv.json_ietf_val.decode('utf-8')
        elif tv.HasField('string_val'): return tv.string_val
        elif tv.HasField('int_val'): return tv.int_val
        elif tv.HasField('uint_val'): return tv.uint_val
        elif tv.HasField('bool_val'): return tv.bool_val
        elif tv.HasField('bytes_val'): return tv.bytes_val.hex()
        elif tv.HasField('float_val'): return tv.float_val
        elif tv.HasField('decimal_val'): return tv.decimal_val
        elif tv.HasField('leaflist_val'): 
            return [self.extract_value(e) for e in tv.leaflist_val.element]
        elif tv.HasField('any_val'): return str(tv.any_val)
        elif tv.HasField('ascii_val'): return tv.ascii_val
        return None
    
    def _format_get(self, resp: GetResponse, meta):
        """
        Formats a gNMI GetResponse.

        The `resp` object is expected to contain:
        - **Notification**:
            - timestamp
            - prefix
            - update (repeated): path, val (TypedValue), duplicates

        Supported Extensions (gnmi_ext.proto):
        - registered_ext, master_arbitration, history, commit, depth, config_subscription.
        """

        res = []
        # loop Notifications
        for notif in resp.notification:
            n_dict = {}
            if meta: n_dict.update(meta)

            n_dict['timestamp'] = notif.timestamp
            n_dict['time'] = datetime.datetime.fromtimestamp(notif.timestamp / 1e9, tz=datetime.timezone.utc).isoformat()
            if notif.HasField('prefix'):
                n_dict['prefix'] = gnmi_path_to_xpath(notif.prefix)

            n_dict['updates'] = []
            for upt in notif.update:
                p_str = gnmi_path_to_xpath(upt.path)
                p_str_no_key = gnmi_path_to_xpath(upt.path, no_keys=True)
                val = self.extract_value(upt.val)
                n_dict['updates'].append({
                    'Path': p_str,
                    'values': { p_str_no_key: val }
                })
            res.append(n_dict)
        return res

    def _format_set(self, resp: SetResponse, meta):
        """
        Formats a gNMI SetResponse.

        The `resp` object is expected to contain below:
        - **prefix**
        - **response**
            - path
            - op(Operation)
            - message(Error)
        - **timestamp**

        Supported extension(gnmi_ext.proto)
        - None
        """
        res = {}
        if meta:
            res.update(meta)

        res['timestamp'] = resp.timestamp
        if resp.timestamp:
            res['time'] = datetime.datetime.fromtimestamp(resp.timestamp / 1e9, tz=datetime.timezone.utc).isoformat()

        if resp.HasField('prefix'):
            res['prefix'] = gnmi_path_to_xpath(resp.prefix)

        op_map = {
            0: 'INVALID',
            1: 'DELETE',
            2: 'REPLACE',
            3: 'UPDATE',
        }

        res['responses'] = []
        for r in resp.response:
            item = {
                'path': gnmi_path_to_xpath(r.path),
                'op': op_map.get(r.op, str(r.op)),
            }
            if r.HasField('message'):
                item['message'] = {
                    'code': r.message.code,
                    'msg': r.message.msg,
                }
            res['responses'].append(item)
        return res


    def _format_capability(self, resp: CapabilityResponse, meta):
        """
        Formats a gNMI CapabilityResponse.

        The `resp` object is expected to contain below:
        - **supported_models**
        - **supported_encoding**
        - **gNMI_version**

        Supported extension(gnmi_ext.proto)
        - None
        """

        res = {}
        if meta: res.update(meta)
        res['gNMI_version'] = resp.gNMI_version
        res['supported_models'] = []

        for m in resp.supported_models:
            res['supported_models'].append({
                'name': m.name,
                'organization': m.organization,
                'version': m.version
            })

        res['supported_encodings'] = [GNMI_ENCODING_TO_STR.get(e, str(e)).upper() 
                                      for e in resp.supported_encodings]
        return res

    def _format_subscribe(self, resp:SubscribeResponse, meta):
        """
        Formats a gNMI SubscribeResponse.

        The `resp` object is expected to contain one of the following:
        - **Notification**:
            - timestamp
            - prefix
            - update (repeated): path, val (TypedValue), duplicates
        - **sync_response**

        Supported Extensions (gnmi_ext.proto):
        - registered_ext, master_arbitration, history, commit, depth, config_subscription.
        """
        if resp.HasField('update'):
            # get Notification in Response
            notif = resp.update
            res = {}
            if meta:
                res.update(meta)

            res['timestamp'] = notif.timestamp
            res['time'] = datetime.datetime.fromtimestamp(notif.timestamp / 1e9, tz=datetime.timezone.utc).isoformat()

            if notif.HasField('prefix'):
                res['prefix'] = gnmi_path_to_xpath(notif.prefix)

            res['updates'] = []
            for upt in notif.update:
                p_str = gnmi_path_to_xpath(upt.path)
                p_str_no_key = gnmi_path_to_xpath(upt.path, no_keys=True)
                val = self.extract_value(upt.val)
                res['updates'].append({
                    'Path': p_str,
                    'values': { p_str_no_key: val }
                })

            # for ON_CHANGE notification
            res['deletes'] = [gnmi_path_to_xpath(d) for d in notif.delete]

            # cleanup fields 
            if len(res['updates']) == 0: res.pop('updates')
            if len(res['deletes']) == 0: res.pop('deletes')

            return res
        elif resp.HasField('sync_response'):
            res = {'sync_response': resp.sync_response}
            return res

        return {}

    def format_json(self, raw_data, rpc:str, meta=None):
        """
        format_json constructs gNMI 'Response' protobuf message into
        JSON-formatted string.
        
        All formats were depending on `rpc`.
        """
        if isinstance(raw_data, Exception):
            err_dict = {"error": str(raw_data)}
            if meta:
                err_dict.update(meta)
            return err_dict

        rpc = (rpc or "").lower()
        if rpc == 'subscribe':
            return self._format_subscribe(raw_data, meta)
        elif rpc == 'get':
            return self._format_get(raw_data, meta)
        if rpc == 'set':
            return self._format_set(raw_data, meta)
        if rpc == 'capability':
            return self._format_capability(raw_data, meta)

        # If it already forms dict/json, just return itself
        return str(raw_data)

    def format_text(self, raw_data):
        if isinstance(raw_data, Exception):
            return f"Error: {raw_data}"
        if hasattr(raw_data, 'DESCRIPTOR'):
            return text_format.MessageToString(raw_data)
        return str(raw_data)

    def format_xml(self, raw_data):
        raise NotImplementedError("Not supported yet!")

# ========================
# NETCONF output formatter(TODO)
# ========================

try:
    import xmltodict
    from lxml import etree
except ImportError:
    xmltodict = None
    etree = None
import xml.dom.minidom

class NETCONFFormatter(ProtocolFormatter):
    """
    Custom parser that translates raw NETCONF XML responses into clean dictionaries
    using xmltodict, matching the standard output style of the framework.
    """
    def format_json(self, raw_data, rpc: str = "", meta=None, **kwargs):
        res = {}
        if meta:
            res.update(meta)
        if kwargs:
            res.update(kwargs)

        res_rpc = res.get('rpc') or rpc
        res['rpc'] = res_rpc
        res['timestamp'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        res['data'] = None
        res['error'] = None

        # Handle Exception / RPCError
        if isinstance(raw_data, Exception):
            if hasattr(raw_data, 'xml') and raw_data.xml is not None:
                try:
                    if isinstance(raw_data.xml, etree._Element):
                        xml_str = etree.tostring(raw_data.xml, encoding='unicode')
                    else:
                        xml_str = str(raw_data.xml)
                    parsed_err = xmltodict.parse(xml_str)
                    res['error'] = parsed_err.get('rpc-error', parsed_err)
                except Exception as e:
                    res['error'] = str(raw_data)
            else:
                res['error'] = str(raw_data)
            return res

        if res_rpc == 'capability':
            res['data'] = raw_data
            return res

        if isinstance(raw_data, list):
            res['data'] = [
                self.format_json(item.get('data'), rpc=item.get('rpc', ''), meta=None)
                if isinstance(item, dict) and 'data' in item
                else item
                for item in raw_data
            ]
            return res

        if res_rpc == 'get-schema':
            if hasattr(raw_data, 'data'):
                res['data'] = raw_data.data
            else:
                res['data'] = getattr(raw_data, 'xml', str(raw_data))
            return res

        # Handle StreamEvent or raw dictionary payload
        from specs.stream_types import StreamEvent
        if isinstance(raw_data, StreamEvent):
            if raw_data.error is not None:
                res['error'] = str(raw_data.error)
                return res
            res['is_sync_marker'] = raw_data.is_sync_marker
            if raw_data.timestamp:
                res['timestamp'] = raw_data.timestamp
            if raw_data.data:
                res['data'] = raw_data.data
            else:
                raw_xml = raw_data.raw_payload or ""
                try:
                    res['data'] = xmltodict.parse(raw_xml) if xmltodict and raw_xml else raw_xml
                except Exception:
                    res['data'] = raw_xml
            return res

        if isinstance(raw_data, dict):
            res['data'] = raw_data
            return res

        # Handle get, get-config, and other XML RPC replies
        data_xml = getattr(raw_data, 'data_xml', None)
        if isinstance(data_xml, (str, bytes)):
            xml_str = data_xml
        else:
            xml_str = getattr(raw_data, 'xml', str(raw_data))
            if not isinstance(xml_str, (str, bytes)):
                xml_str = str(xml_str)

        try:
            parsed = xmltodict.parse(xml_str)
            # Extract inner data element if present
            if 'data' in parsed:
                res['data'] = parsed['data']
            elif 'rpc-reply' in parsed and 'data' in parsed['rpc-reply']:
                res['data'] = parsed['rpc-reply']['data']
            elif 'rpc-reply' in parsed and 'ok' in parsed['rpc-reply']:
                res['data'] = {'ok': True}
            elif 'ok' in parsed:
                res['data'] = {'ok': True}
            else:
                res['data'] = parsed
        except Exception as e:
            res['data'] = str(xml_str)
            res['error'] = f"XML Parsing failed: {e}"

        return res

    def format_text(self, raw_data, **meta):
        """print the output AS-IS presented"""
        if isinstance(raw_data, Exception):
            return str(raw_data)

        if isinstance(raw_data, list):
            lines = []
            for item in raw_data:
                if isinstance(item, dict) and 'rpc' in item and 'data' in item:
                    item_rpc = item.get('rpc', '')
                    item_text = self.format_text(item.get('data'))
                    lines.append(f"--- [Operation: {item_rpc}] ---\n{item_text}")
                else:
                    lines.append(str(item))
            return "\n".join(lines)

        from specs.stream_types import StreamEvent
        if isinstance(raw_data, StreamEvent):
            if raw_data.error is not None:
                return f"[NETCONF Notification Error] {raw_data.error}"
            return str(raw_data.raw_payload or raw_data.data)

        if isinstance(raw_data, dict):
            return json.dumps(raw_data, indent=2)

        if hasattr(raw_data, 'data') and isinstance(getattr(raw_data, 'data', None), (str, dict, list)):
            return raw_data.data if isinstance(raw_data.data, str) else json.dumps(raw_data.data, indent=2)

        xml_node = getattr(raw_data, 'xml', raw_data)
        if not isinstance(xml_node, str):
            try:
                if isinstance(xml_node, etree._Element):
                    xml_str = etree.tostring(xml_node, encoding='unicode')
                else:
                    xml_str = str(xml_node)
            except Exception:
                xml_str = str(xml_node)
        else:
            xml_str = xml_node

        return xml_str

    def format_xml(self, raw_data, **meta):
        """Uses minidom to return beautify indented XML."""
        rpc = meta.get('rpc', '')

        # in response of <hello> message
        if rpc == 'capability':
            return "\n".join(str(c) for c in raw_data) if isinstance(raw_data, list) else str(raw_data)

        xml_str = getattr(raw_data, 'xml', str(raw_data))

        try:
            dom = xml.dom.minidom.parseString(xml_str)
            return dom.toprettyxml(indent='  ')
        except Exception:
            return xml_str


def normalize_protocol_payload(protocol: str, op_type: str, raw_data: Any) -> Any:
    """
    Normalizes native protocol payloads (gNMI Protobuf messages, NETCONF XML/RPCReply
    objects, bytes, Exceptions) into JSON-serializable Python primitives.
    """
    if raw_data is None or isinstance(raw_data, (bool, int, float, str)):
        return raw_data
    if isinstance(raw_data, bytes):
        try:
            return raw_data.decode("utf-8")
        except UnicodeDecodeError:
            return raw_data.hex()
    if isinstance(raw_data, Exception):
        return str(raw_data)
    if isinstance(raw_data, dict):
        return {
            str(k): normalize_protocol_payload(protocol, op_type, v)
            for k, v in raw_data.items()
        }
    if isinstance(raw_data, (list, tuple)):
        return [
            normalize_protocol_payload(protocol, op_type, item)
            for item in raw_data
        ]

    proto_norm = str(protocol or "").strip().lower()
    if proto_norm == "gnmi" or hasattr(raw_data, "DESCRIPTOR"):
        cls_name = type(raw_data).__name__
        op_norm = str(op_type or "").strip().lower().replace("_", "-")
        if cls_name == "CapabilityResponse" or op_norm in ("capability", "capabilities"):
            rpc_key = "capability"
        elif cls_name == "GetResponse" or op_norm in ("get", "get-config"):
            rpc_key = "get"
        elif cls_name == "SetResponse" or op_norm in ("set", "edit-config", "edit"):
            rpc_key = "set"
        elif cls_name == "SubscribeResponse" or op_norm == "subscribe":
            rpc_key = "subscribe"
        else:
            rpc_key = op_norm

        formatted = GNMIFormatter().format_json(raw_data, rpc=rpc_key, meta=None)
        if isinstance(formatted, (dict, list)):
            return normalize_protocol_payload(protocol, op_type, formatted)
        if hasattr(raw_data, "DESCRIPTOR"):
            try:
                from google.protobuf.json_format import MessageToDict

                return MessageToDict(raw_data, preserving_proto_field_name=True)
            except Exception:
                pass
        return str(formatted)

    if proto_norm == "netconf" or hasattr(raw_data, "xml") or hasattr(raw_data, "data_xml"):
        op_norm = str(op_type or "").strip().lower().replace("_", "-")
        if op_norm == "capabilities":
            op_norm = "capability"
        elif op_norm == "schema":
            op_norm = "get-schema"
        elif op_norm == "edit":
            op_norm = "edit-config"

        formatted = NETCONFFormatter().format_json(raw_data, rpc=op_norm, meta=None)
        if isinstance(formatted, dict):
            inner = formatted.get("data")
            if inner is not None:
                return normalize_protocol_payload(protocol, op_type, inner)
            return normalize_protocol_payload(protocol, op_type, formatted)
        return normalize_protocol_payload(protocol, op_type, formatted)

    return str(raw_data)
