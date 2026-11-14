"""Human-readable rendering of the structured execution record."""

from __future__ import annotations

import json
from typing import Any, Dict, List

_STATUS_ICON = {"ok": "OK ", "error": "ERR", "timeout": "TMO", "not_run": "SKIP", "running": "RUN"}


def render_record(record: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"run_id:            {record['run_id']}")
    lines.append(f"source:            {record['source_notebook']}")
    lines.append(f"output notebook:   {record['output_notebook']}")
    lines.append(f"status:            {record['status']}")
    if record.get("stop_reason"):
        lines.append(f"stop reason:       {json.dumps(record['stop_reason'], ensure_ascii=False)}")
    lines.append(f"working dir:       {record['working_directory']}")
    lines.append(f"duration:          {record.get('duration_seconds')}s")
    startup = record["kernel"]["startup"]
    lines.append(
        f"kernel:            {startup.get('implementation')} {startup.get('language_version')} pid={startup.get('pid')}"
    )
    shutdown = record["kernel"].get("shutdown") or {}
    lines.append(
        "kernel shutdown:   requested={requested} reaped={reaped} waited={waited_seconds}s note={note}".format(
            **shutdown
        )
        if shutdown
        else "kernel shutdown:   (not recorded)"
    )
    if record.get("parameter_changes"):
        lines.append("parameter overrides:")
        for change in record["parameter_changes"]:
            lines.append(
                f"  - {change['name']}: {change['old_value']!r} -> {change['new_value']!r}"
            )

    lines.append("")
    lines.append("cells (document order):")
    for cell in record["cells"]:
        icon = _STATUS_ICON.get(cell["status"], "???")
        count = cell.get("execution_count")
        count_text = "-" if count is None else str(count)
        lines.append(
            f"  [{icon}] index={cell['cell_index']:<2} id={cell.get('cell_id')} "
            f"exec_count={count_text:<3} outputs={cell.get('output_count', 0)} "
            f"request={cell.get('request_msg_id') or '-'}"
        )
        for note in cell.get("notes", []):
            lines.append(f"         note: {note}")

    counts = record["counts"]
    lines.append("")
    lines.append(
        "counts: code={code_cells} executed={executed} ok={succeeded} "
        "errors={errors} timeouts={timeouts} not_run={not_run}".format(**counts)
    )

    messages = record.get("messages", [])
    if messages:
        lines.append("")
        lines.append(f"message log: {len(messages)} entries (inspect --messages to dump)")
    return "\n".join(lines)


def render_messages(record: Dict[str, Any]) -> str:
    lines: List[str] = []
    for entry in record.get("messages", []):
        lines.append(
            "#{seq:>3} [{channel:<6}] {msg_type:<20} cell={cell_index} "
            "accepted={accepted} parent_match={parent_matches_request} "
            "request={request_msg_id}".format(**entry)
        )
        if entry["summary"]:
            lines.append(f"        {json.dumps(entry['summary'], ensure_ascii=False, default=str)}")
    return "\n".join(lines)


def render_notebook_cells(notebook: Dict[str, Any]) -> str:
    """Compact view of an output notebook so final outputs can be checked."""
    lines: List[str] = []
    for index, cell in enumerate(notebook.get("cells", [])):
        ctype = cell["cell_type"]
        if ctype == "markdown":
            lines.append(f"[{index}] markdown id={cell.get('id')}")
            continue
        lines.append(
            f"[{index}] code id={cell.get('id')} execution_count={cell.get('execution_count')} "
            f"outputs={len(cell.get('outputs', []))}"
        )
        for out in cell.get("outputs", []):
            otype = out.get("output_type")
            if otype == "stream":
                lines.append(f"      - stream/{out.get('name')}: {out.get('text')!r}")
            elif otype in ("display_data", "execute_result"):
                lines.append(f"      - {otype}: mimes={sorted(out.get('data', {}).keys())}")
            elif otype == "error":
                lines.append(f"      - error: {out.get('ename')}: {out.get('evalue')}")
            else:
                lines.append(f"      - {otype}")
    return "\n".join(lines)
