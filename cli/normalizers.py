# -*- coding: utf-8 -*-
"""
Normalizers and translation helpers for configuration tokens, filters,
telemetry delivery modes, and YANG schemas.
"""
from typing import Any, Dict, List, Optional, Tuple

from config.delivery import DeliveryMode
from config.schema import SchemaConfig
from util.utils import read_payload

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
    """Normalizes mode string by lowercasing and replacing hyphens with underscores."""
    return str(value).strip().lower().replace("-", "_") if value is not None else ""


def extract_filter_expression(raw_filter: Any, default_type: Optional[str] = None) -> Tuple[str, str]:
    """Extracts filter expression and filter type from string or dictionary YAML input."""
    if isinstance(raw_filter, dict):
        expr = raw_filter.get("select") or raw_filter.get("expression") or raw_filter.get("filter") or ""
        filter_str = read_payload(expr) if isinstance(expr, str) else str(expr)
        f_type = raw_filter.get("type") or default_type or ("subtree" if filter_str.strip().startswith("<") else "xpath")
        return filter_str, f_type
    filter_str = read_payload(raw_filter) if isinstance(raw_filter, str) else str(raw_filter)
    f_type = default_type or ("subtree" if filter_str.strip().startswith("<") else "xpath")
    return filter_str, f_type


def resolve_delivery_mode(
    rpc_mode: Optional[str],
    sub_mode: Optional[str] = None,
    default_sub_mode: str = "sample",
) -> DeliveryMode:
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

