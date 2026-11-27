# -*- coding: utf-8 -*-
"""
CLI Statistics Presentation Formatter.

Renders `ExecutionStatsDTO` and `SubscriptionStatsDTO` summaries as concise ASCII
tables or JSON to `sys.stderr` (keeping `sys.stdout` clean) and optionally exports
full JSON snapshots to `--stats-file`.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import sys
from typing import Any, List, Optional, Sequence, TextIO, Union

from application.dto import ExecutionStatsDTO, SubscriptionStatsDTO

StatsItem = Union[ExecutionStatsDTO, SubscriptionStatsDTO]


def format_execution_stats_table(stats: ExecutionStatsDTO) -> str:
    """Formats an ExecutionStatsDTO into a concise human-readable ASCII summary."""
    val_str = "N/A"
    if stats.validation_passed is not None:
        val_state = "PASS" if stats.validation_passed else "FAIL"
        val_str = f"{stats.validation_duration_ms:.1f} ms [{val_state}]"
    conn_str = "reused (0.0 ms)" if stats.connection_reused else f"{stats.connect_duration_ms:.1f} ms"
    rb_total = sum(stats.rollbacks.values())

    lines: List[str] = [
        f"--- Execution Statistics ({stats.protocol} @ {stats.target}) ---",
        f"Total Duration : {stats.total_duration_ms:.1f} ms (Connect: {conn_str} | Validation: {val_str})",
        (
            f"Operations     : {stats.operations_total} total | "
            f"{stats.operations_succeeded} succeeded | "
            f"{stats.operations_failed} failed | "
            f"{stats.operations_aborted} aborted"
        ),
        (
            f"Payload        : {stats.request_payload_bytes:,} B sent | "
            f"{stats.response_payload_bytes:,} B received | Rollbacks: {rb_total}"
        ),
    ]
    if stats.steps:
        lines.append("Steps          :")
        for st in stats.steps:
            idx_num = st.step_index + 1
            if st.aborted:
                lines.append(f"  #{idx_num} {st.op_type:<14} 0.0 ms               ABORTED")
            else:
                wire = f"{st.wire_rpc_ms:.1f} ms" if st.wire_rpc_ms is not None else "n/a"
                status_txt = "OK" if st.success else f"FAIL ({st.error_code or st.error_category or 'error'})"
                lines.append(
                    f"  #{idx_num} {st.op_type:<14} {st.duration_ms:6.1f} ms (wire: {wire})  {status_txt}"
                )
    return "\n".join(lines)


def format_subscription_stats_table(stats: SubscriptionStatsDTO) -> str:
    """Formats a SubscriptionStatsDTO into a concise human-readable ASCII summary."""
    sync_str = f"{stats.time_to_sync_ms:.1f} ms" if stats.time_to_sync_ms is not None else "n/a"
    first_str = (
        f"{stats.time_to_first_event_ms:.1f} ms"
        if stats.time_to_first_event_ms is not None
        else "n/a"
    )
    drops_total = sum(stats.events_dropped.values())
    kinds = ", ".join(f"{k}={v}" for k, v in sorted(stats.events_by_kind.items())) or "none"
    lines: List[str] = [
        f"--- Subscription Statistics ({stats.protocol} @ {stats.target} | {stats.subscription_id}) ---",
        f"Mode / Status  : {stats.delivery_mode} | active={stats.active} | disconnect={stats.disconnect_reason or 'n/a'}",
        f"Uptime / Rate  : {stats.uptime_seconds:.2f} s | {stats.events_total} events ({stats.event_rate_eps:.2f} events/s)",
        f"Events by Kind : {kinds}",
        f"Latency        : first_event={first_str} | sync={sync_str} | polls={stats.poll_triggers_total}",
        f"Queue / Drops  : depth_current={stats.queue_depth_current} | depth_peak={stats.queue_depth_peak} | dropped={drops_total}",
    ]
    return "\n".join(lines)


def serialize_stats_items(items: Sequence[StatsItem]) -> Any:
    """Serializes one or more statistics DTOs to JSON-ready primitives."""
    serialized = [dataclasses.asdict(item) for item in items]
    if len(serialized) == 1:
        return serialized[0]
    return serialized


def write_cli_stats(
    items: Sequence[StatsItem],
    *,
    print_stderr: bool = False,
    fmt: str = "table",
    file_path: Optional[str] = None,
    stream: Optional[TextIO] = None,
) -> None:
    """
    Renders CLI statistics to `stderr` (never `stdout`) and/or writes JSON to `file_path`.
    """
    if not items:
        return

    target_stream = stream if stream is not None else sys.stderr
    norm_fmt = (fmt or "table").strip().lower()

    if print_stderr:
        if norm_fmt == "json":
            target_stream.write(json.dumps(serialize_stats_items(items), indent=2) + "\n")
        else:
            blocks = [
                format_execution_stats_table(it)
                if isinstance(it, ExecutionStatsDTO)
                else format_subscription_stats_table(it)
                for it in items
            ]
            target_stream.write("\n".join(blocks) + "\n")
        target_stream.flush()

    if file_path:
        out_path = Path(file_path).expanduser()
        if out_path.parent:
            out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(serialize_stats_items(items), indent=2) + "\n",
            encoding="utf-8",
        )
