# -*- coding: utf-8 -*-
"""
YAML Operation Item Parser.
Translates individual YAML `operations:` entries into typed `OperationConfig` instances.
"""
from typing import Any, List, Optional

from config.model import Protocol
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
    TransactionType,
)
from config.protocol_options import GNMIOptions, NetconfOptions
from config.selectors import FilterSelector, PathSelector
from cli.models import FileConfigError
from cli.normalizers import extract_filter_expression
from util.utils import read_payload


def parse_operation_item(
    op_item: Any,
    protocol: Protocol = Protocol.NETCONF,
    step_index: Optional[int] = None,
    default_op_id: Optional[str] = None,
) -> OperationConfig:
    """
    Parses a single operation entry from YAML into a typed OperationConfig.

    Args:
        op_item: data of 'operations' key in YAML
        protocol: currently supported protocol defined at `config/model.py`
        step_index: 1-based index of operation within sequential operations list
        default_op_id: optional fallback operation identifier

    Raises:
        FileConfigError: if given operation in `operations` is unsupported or invalid.

    Returns:
        OperationConfig subclass matching parsed operation.
    """
    if isinstance(op_item, OperationConfig):
        return op_item

    if not isinstance(op_item, dict):
        raise FileConfigError(f"Operation entry must be a dictionary, got {type(op_item).__name__}: {op_item}")

    # Extract top-level explicit id/name if present
    explicit_id = op_item.get("id") or op_item.get("op_id") or op_item.get("name")
    cleaned_item = {k: v for k, v in op_item.items() if k not in ("id", "op_id", "name")}

    if "operation" in cleaned_item or "type" in cleaned_item:
        op_name = str(cleaned_item.get("operation") or cleaned_item.get("type"))
        op_params = {k: v for k, v in cleaned_item.items() if k not in ("operation", "type")}
    elif len(cleaned_item) == 1:
        op_name = list(cleaned_item.keys())[0]
        params_val = cleaned_item[op_name]
        op_params = params_val if isinstance(params_val, dict) else {}
    else:
        raise FileConfigError(
            f"Invalid operation structure: {op_item}. "
            "Must be either a single-key mapping (e.g. {'get-schema': {...}}) "
            "or specify 'operation': '<type>'."
        )

    op_norm = op_name.strip().lower().replace("_", "-")

    # Extract nested id/name if top-level was not provided
    nested_id = op_params.get("id") or op_params.get("op_id") or op_params.get("name")
    final_op_id = explicit_id or nested_id
    if not final_op_id:
        proto_str = protocol.value.lower() if hasattr(protocol, "value") else str(protocol).lower()
        if step_index is not None:
            final_op_id = f"{proto_str}-{op_norm}-{step_index:02d}"
        elif default_op_id:
            final_op_id = default_op_id
        else:
            final_op_id = f"{proto_str}-{op_norm}"

    if op_norm in ("capability", "capabilities"):
        return CapabilitiesOperation(op_id=final_op_id)

    elif op_norm == "get-schema":
        ident = op_params.get("identifier")
        if not ident:
            raise FileConfigError("Missing mandatory parameter 'identifier' for get-schema operation")
        version = op_params.get("version")
        format_val = op_params.get("format", "yang") or "yang"
        return GetSchemaOperation(
            identifier=str(ident),
            version=str(version) if version is not None else None,
            format=str(format_val),
            op_id=final_op_id,
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
            protocol_options=nc_opts,
            op_id=final_op_id,
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
            protocol_options=p_opts,
            op_id=final_op_id,
        )

    elif op_norm in ("edit-config", "set"):
        is_gnmi_set = (
            protocol == Protocol.GNMI
            or any(k in op_params for k in ("update", "replace", "delete"))
        ) and not any(k in op_params for k in ("nc_config", "target_datastore", "target"))

        if is_gnmi_set:
            prefix = op_params.get("prefix", "")
            changes_list: List[Change] = []

            def _parse_mutation(item: Any, op_type: ChangeType) -> Change:
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
                protocol_options=gnmi_opts,
                op_id=final_op_id,
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
            protocol_options=nc_opts,
            op_id=final_op_id,
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
            op_id=final_op_id,
        )

    else:
        raise FileConfigError(
            f"Unsupported operation type '{op_name}'. Available: [capability, get, get-config, get-schema, edit-config, set, lock, unlock, commit, cancel-commit, discard-changes, validate]"
        )
