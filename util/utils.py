import io
import os

try:
    from specs.gnmi import gnmi_pb2
    # gNMI encoding mapper
    GNMI_ENCODING_TO_STR = {
        gnmi_pb2.JSON_IETF: "json_ietf",
        gnmi_pb2.JSON: "json",
        gnmi_pb2.PROTO: "proto",
        gnmi_pb2.ASCII: "ascii",
        gnmi_pb2.BYTES: "bytes",
    }
except ImportError:
    gnmi_pb2 = None
    GNMI_ENCODING_TO_STR = {}

def str_to_bytes(text: str, encoding: str = "utf-8") -> bytes:
    """
    Convert a string into a byte stream.

    Args:
        text (str): The input string to convert.
        encoding (str): Character encoding to use (default: UTF-8).

    Returns:
        io.BytesIO: A byte stream containing the encoded string.
    """

    if not isinstance(text, str):
        raise TypeError("input must be a string")
    
    try:
        byte_data = text.encode(encoding)
    except LookupError:
        raise ValueError(f'Unknown encoding: {encoding}')
    
    return byte_data

def read_payload(value: str) -> str:
    """
    Checks if the provided string is a valid file path.
    If it is, returns the file contents (great for XML configs/filters).
    Otherwise, returns the string as-is.
    """
    if value and os.path.isfile(value):
        try:
            with open(value, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception as e:
            print(f"[CLI Warning] Failed to read payload file '{value}': {e}")
    return value


def format_error_summary(error: object) -> str:
    """
    Extracts a concise, single-line error summary from an Exception or error object.

    Handles gRPC RpcError (status code + details), NETCONF RPCError (tag + message),
    and standard Python Exceptions without multi-line traceback dumps.
    """
    if error is None:
        return ""

    # 1. gRPC RpcError handling
    code = getattr(error, 'code', None)
    details = getattr(error, 'details', None)
    if callable(code):
        try:
            code_val = code()
            code_name = getattr(code_val, 'name', str(code_val))
            det_val = details() if callable(details) else ""
            if code_name and det_val:
                return f"{code_name}: {det_val.strip()}"
            if det_val:
                return det_val.strip()
            if code_name:
                return code_name
        except Exception:
            pass

    # 2. NETCONF RPCError handling
    tag = getattr(error, 'tag', None)
    msg = getattr(error, 'message', None)
    if tag or msg:
        tag_str = f"[{tag}] " if tag else ""
        return f"NETCONF RPCError {tag_str}{msg or str(error)}".strip()

    # 3. Fallback: single-line sanitized string
    err_str = str(error).strip()
    lines = [line.strip() for line in err_str.splitlines() if line.strip()]
    return " - ".join(lines) if lines else type(error).__name__
