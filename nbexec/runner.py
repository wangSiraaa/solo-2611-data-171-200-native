"""Core execution engine.

One fresh kernel per :func:`run_notebook` call. Cells execute strictly in
document order; every execute request is correlated with its shell reply
and IOPub messages through the request's ``msg_id`` (parent header). A cell
is only considered finished when *both* its shell ``execute_reply`` and its
``status: idle`` IOPub message have arrived — never on just one of them.
"""

from __future__ import annotations

import datetime as _dt
import queue
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import nbformat

from .kernel import ManagedKernel
from .outputs import CellOutputArea, OutputLedger
from .parameters import apply_parameters

POLL_INTERVAL = 0.1


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _truncate(text: Any, limit: int = 500) -> str:
    value = text if isinstance(text, str) else repr(text)
    return value if len(value) <= limit else value[:limit] + f"...<{len(value) - limit} more chars>"


@dataclass
class NotebookRunResult:
    notebook: nbformat.NotebookNode
    record: Dict[str, Any]


class _MessageLog:
    def __init__(self) -> None:
        self.entries: List[Dict[str, Any]] = []

    def add(
        self,
        channel: str,
        msg_type: str,
        request_msg_id: Optional[str],
        parent_msg_id: Optional[str],
        cell_index: Optional[int],
        accepted: bool,
        summary: Dict[str, Any],
    ) -> int:
        seq = len(self.entries)
        self.entries.append(
            {
                "seq": seq,
                "timestamp": _utc_now(),
                "channel": channel,
                "msg_type": msg_type,
                "request_msg_id": request_msg_id,
                "parent_msg_id": parent_msg_id,
                "parent_matches_request": parent_msg_id == request_msg_id
                if parent_msg_id is not None and request_msg_id is not None
                else None,
                "cell_index": cell_index,
                "accepted": accepted,
                "summary": summary,
            }
        )
        return seq

    def event(self, kind: str, cell_index: Optional[int], detail: Dict[str, Any], request_msg_id: Optional[str] = None) -> int:
        return self.add("event", kind, request_msg_id, None, cell_index, True, detail)


def _summarize_content(channel: str, msg_type: str, content: Dict[str, Any]) -> Dict[str, Any]:
    if msg_type == "stream":
        return {"name": content.get("name"), "text": _truncate(content.get("text", ""))}
    if msg_type in ("display_data", "execute_result", "update_display_data"):
        transient = content.get("transient") or {}
        return {
            "mime_types": sorted((content.get("data") or {}).keys()),
            "display_id": transient.get("display_id"),
            "execution_count": content.get("execution_count"),
        }
    if msg_type == "error":
        return {"ename": content.get("ename"), "evalue": _truncate(content.get("evalue", ""))}
    if msg_type == "clear_output":
        return {
            "wait": content.get("wait"),
            "stdout": content.get("stdout", True),
            "stderr": content.get("stderr", True),
        }
    if msg_type == "status":
        return {"execution_state": content.get("execution_state")}
    if msg_type == "execute_input":
        code = content.get("code", "")
        return {"execution_count": content.get("execution_count"), "code_preview": _truncate(code, 120)}
    if channel == "shell":
        return dict(content)
    return {"keys": sorted(content.keys())}


def clear_notebook_outputs(notebook: nbformat.NotebookNode) -> None:
    """Remove every stale code-cell output and execution count (copy only)."""
    for cell in notebook.cells:
        if cell.cell_type != "code":
            continue
        cell["outputs"] = []
        cell["execution_count"] = None


def run_notebook(
    source_path: str,
    output_path: str,
    parameters: Optional[Dict[str, Any]] = None,
    working_directory: str = ".",
    cell_timeout: float = 30.0,
    interrupt_wait: float = 10.0,
    startup_timeout: float = 30.0,
    plan: Optional[Dict[str, Any]] = None,
    parameters_path: Optional[str] = None,
) -> NotebookRunResult:
    """Execute one notebook in a fresh kernel and persist the result copy."""
    run_started = time.monotonic()
    notebook = nbformat.read(source_path, as_version=nbformat.NO_CONVERT)
    nbformat.validate(notebook)
    clear_notebook_outputs(notebook)

    parameter_changes: List[Dict[str, Any]] = []
    if parameters:
        parameter_changes = apply_parameters(notebook, parameters)

    kernel = ManagedKernel(cwd=working_directory, startup_timeout=startup_timeout)
    kernel_info = kernel.start()

    log = _MessageLog()
    ledger = OutputLedger()

    record: Dict[str, Any] = {
        "schema_version": 1,
        "run_id": str(uuid.uuid4()),
        "source_notebook": source_path,
        "output_notebook": output_path,
        "parameters_file": parameters_path,
        "parameter_changes": parameter_changes,
        "plan": plan or {},
        "working_directory": working_directory,
        "started_at": _utc_now(),
        "cell_timeout_seconds": cell_timeout,
        "interrupt_wait_seconds": interrupt_wait,
        "kernel": {"startup": kernel_info, "shutdown": None},
        "status": "completed",
        "stop_reason": None,
        "cells": [],
        "messages": log.entries,
        "counts": {
            "code_cells": 0,
            "executed": 0,
            "succeeded": 0,
            "errors": 0,
            "timeouts": 0,
            "not_run": 0,
        },
    }

    code_cells = [(idx, cell) for idx, cell in enumerate(notebook.cells) if cell.cell_type == "code"]
    record["counts"]["code_cells"] = len(code_cells)
    halted = False

    try:
        for cell_index, cell in code_cells:
            if halted:
                trace = {
                    "cell_index": cell_index,
                    "cell_id": cell.get("id"),
                    "status": "not_run",
                    "request_msg_id": None,
                    "execution_count": None,
                    "output_count": 0,
                    "reason": "execution stopped before this cell",
                }
                record["cells"].append(trace)
                record["counts"]["not_run"] += 1
                continue

            trace, area, halted, terminal_status = _execute_cell(
                kernel=kernel,
                ledger=ledger,
                log=log,
                cell=cell,
                cell_index=cell_index,
                cell_timeout=cell_timeout,
                interrupt_wait=interrupt_wait,
            )
            record["cells"].append(trace)
            record["counts"]["executed"] += 1
            cell["outputs"] = list(area.outputs)
            cell["execution_count"] = trace["execution_count"]

            if terminal_status == "ok":
                record["counts"]["succeeded"] += 1
            elif terminal_status == "error":
                record["counts"]["errors"] += 1
                record["status"] = "error"
                record["stop_reason"] = {
                    "kind": "error",
                    "cell_index": cell_index,
                    "cell_id": cell.get("id"),
                    "request_msg_id": trace["request_msg_id"],
                    "reply_status": trace.get("reply_status"),
                }
            elif terminal_status == "timeout":
                record["counts"]["timeouts"] += 1
                record["status"] = "timeout"
                record["stop_reason"] = {
                    "kind": "timeout",
                    "cell_index": cell_index,
                    "cell_id": cell.get("id"),
                    "request_msg_id": trace["request_msg_id"],
                    "cell_timeout_seconds": cell_timeout,
                }
                # The kernel was force-killed inside _execute_cell; nothing else can run.
                halted = True
    finally:
        # Bounded shutdown/process reclamation happens even if an unexpected
        # exception escapes the cell loop. shutdown() is idempotent: a kernel
        # already force-killed during timeout handling is reported as such.
        shutdown_report = kernel.shutdown(soft_wait=interrupt_wait, hard_wait=interrupt_wait)
        record["kernel"]["shutdown"] = shutdown_report.__dict__
        record["finished_at"] = _utc_now()
        record["duration_seconds"] = round(time.monotonic() - run_started, 6)

    nbformat.validate(notebook)
    nbformat.write(notebook, output_path)
    return NotebookRunResult(notebook=notebook, record=record)


def _execute_cell(
    *,
    kernel: ManagedKernel,
    ledger: OutputLedger,
    log: "_MessageLog",
    cell: Any,
    cell_index: int,
    cell_timeout: float,
    interrupt_wait: float,
) -> Any:
    area = ledger.open_cell(cell_index, cell.get("id"))
    kc = kernel.client
    source = cell.source
    started = time.monotonic()
    request_msg_id = kc.execute(source, allow_stdin=False, stop_on_error=True, reply=False)

    trace: Dict[str, Any] = {
        "cell_index": cell_index,
        "cell_id": cell.get("id"),
        "status": "running",
        "request_msg_id": request_msg_id,
        "reply_msg_id": None,
        "reply_status": None,
        "execution_count": None,
        "output_count": 0,
        "started_at": _utc_now(),
        "finished_at": None,
        "duration_seconds": None,
        "message_seq": [],
        "message_seq_range": None,
        "notes": [],
    }
    first_seq = len(log.entries)
    log.event(
        "execute_request",
        cell_index,
        {"request_msg_id": request_msg_id, "code_preview": _truncate(source, 200)},
        request_msg_id=request_msg_id,
    )

    reply: Optional[Dict[str, Any]] = None
    got_idle = False
    phase = "run"  # "run" -> "grace" (post-interrupt) -> "killed"
    phase_deadline = started + cell_timeout
    terminal_status = "ok"

    def collect_seq() -> None:
        last_seq = len(log.entries) - 1
        trace["message_seq_range"] = [first_seq, max(first_seq, last_seq)]

    while not (reply is not None and got_idle):
        now = time.monotonic()
        phase_remaining = phase_deadline - now

        if phase_remaining <= 0:
            if phase == "run":
                # First deadline miss: interrupt and give the kernel a bounded
                # grace window to produce reply + idle.
                log.event(
                    "cell_timeout",
                    cell_index,
                    {"request_msg_id": request_msg_id, "cell_timeout_seconds": cell_timeout},
                    request_msg_id=request_msg_id,
                )
                kernel.interrupt()
                log.event(
                    "interrupt_request",
                    cell_index,
                    {"request_msg_id": request_msg_id},
                    request_msg_id=request_msg_id,
                )
                trace["notes"].append(f"interrupted after {cell_timeout}s timeout")
                phase = "grace"
                phase_deadline = time.monotonic() + interrupt_wait
                terminal_status = "timeout"
                continue
            # Grace window exhausted: the kernel cannot be recovered in time.
            log.event(
                "forced_shutdown",
                cell_index,
                {"request_msg_id": request_msg_id, "interrupt_wait_seconds": interrupt_wait},
                request_msg_id=request_msg_id,
            )
            report = kernel.shutdown(soft_wait=interrupt_wait, hard_wait=interrupt_wait)
            log.event(
                "kernel_shutdown",
                cell_index,
                report.__dict__,
                request_msg_id=request_msg_id,
            )
            if not any(out.get("output_type") == "error" for out in area.outputs):
                area.add_error(
                    {
                        "ename": "TimeoutError",
                        "evalue": f"cell execution exceeded {cell_timeout} seconds and the kernel did not recover",
                        "traceback": [
                            f"TimeoutError: cell execution exceeded {cell_timeout} seconds"
                        ],
                    }
                )
            terminal_status = "timeout"
            break

        wait_seconds = min(POLL_INTERVAL, max(0.01, phase_remaining))

        # IOPub carries the bulk of the traffic; block briefly on it.
        iopub_msg = None
        try:
            iopub_msg = kc.get_iopub_msg(timeout=wait_seconds)
        except queue.Empty:
            pass
        if iopub_msg is not None:
            parent = iopub_msg["parent_header"].get("msg_id")
            accepted = parent == request_msg_id
            seq = log.add(
                "iopub",
                iopub_msg["msg_type"],
                request_msg_id,
                parent,
                cell_index,
                accepted,
                _summarize_content("iopub", iopub_msg["msg_type"], iopub_msg["content"]),
            )
            if accepted:
                trace["message_seq"].append(seq)
                _handle_iopub(ledger, area, iopub_msg, trace)
                if iopub_msg["msg_type"] == "status" and iopub_msg["content"].get(
                    "execution_state"
                ) == "idle":
                    got_idle = True

        # Shell reply (non-blocking; ownership checked via parent header).
        try:
            shell_msg = kc.get_shell_msg(timeout=0)
        except queue.Empty:
            shell_msg = None
        if shell_msg is not None:
            parent = shell_msg["parent_header"].get("msg_id")
            accepted = parent == request_msg_id
            seq = log.add(
                "shell",
                shell_msg["msg_type"],
                request_msg_id,
                parent,
                cell_index,
                accepted,
                _summarize_content("shell", shell_msg["msg_type"], shell_msg["content"]),
            )
            if accepted and shell_msg["msg_type"] == "execute_reply" and reply is None:
                reply = shell_msg
                trace["reply_msg_id"] = shell_msg["header"]["msg_id"]
                trace["reply_status"] = shell_msg["content"].get("status")
                trace["message_seq"].append(seq)
                # An error reply during normal running fails the cell. After a
                # timeout interrupt the terminal status stays "timeout" (the
                # final reconciliation below handles that case).
                if shell_msg["content"].get("status") == "error" and phase == "run":
                    terminal_status = "error"

        # stdin is unsupported: an input request must fail the cell, never hang.
        try:
            stdin_msg = kc.get_stdin_msg(timeout=0)
        except queue.Empty:
            stdin_msg = None
        if stdin_msg is not None:
            parent = stdin_msg["parent_header"].get("msg_id")
            accepted = parent == request_msg_id
            log.add(
                "stdin",
                stdin_msg["msg_type"],
                request_msg_id,
                parent,
                cell_index,
                accepted,
                _summarize_content("stdin", stdin_msg["msg_type"], stdin_msg["content"]),
            )
            if accepted and stdin_msg["msg_type"] == "input_request":
                area.add_error(
                    {
                        "ename": "StdinNotImplementedError",
                        "evalue": "raw_input was requested, but stdin input is not supported by this executor",
                        "traceback": [
                            "StdinNotImplementedError: interactive stdin input is not supported"
                        ],
                    }
                )
                trace["notes"].append("stdin input_request received and rejected")
                kernel.interrupt()
                log.event(
                    "interrupt_request",
                    cell_index,
                    {"request_msg_id": request_msg_id, "reason": "stdin unsupported"},
                    request_msg_id=request_msg_id,
                )
                phase = "grace"
                phase_deadline = time.monotonic() + interrupt_wait
                terminal_status = "error"

    # Reconcile the final status. A cell that hit its timeout deadline (phase
    # advanced to "grace" or the kernel was force-killed) stays a timeout even
    # when the kernel later reports an interrupted error reply; otherwise the
    # shell reply's status is authoritative.
    if phase == "run":
        if reply is not None:
            reply_status = reply["content"].get("status")
            if reply_status == "error":
                terminal_status = "error"
            elif reply_status == "ok":
                terminal_status = "ok"

    finished = time.monotonic()
    trace["status"] = terminal_status
    trace["finished_at"] = _utc_now()
    trace["duration_seconds"] = round(finished - started, 6)
    trace["execution_count"] = (
        reply["content"].get("execution_count") if reply is not None else None
    )
    trace["output_count"] = len(area.outputs)
    collect_seq()
    halted = terminal_status in ("error", "timeout")
    return trace, area, halted, terminal_status


def _handle_iopub(
    ledger: OutputLedger,
    area: CellOutputArea,
    msg: Dict[str, Any],
    trace: Dict[str, Any],
) -> None:
    msg_type = msg["msg_type"]
    content = msg["content"]
    if msg_type == "stream":
        area.add_stream(content.get("name", "stdout"), content.get("text", ""))
    elif msg_type == "display_data":
        area.add_display(content, "display_data", None)
    elif msg_type == "execute_result":
        area.add_display(content, "execute_result", content.get("execution_count"))
    elif msg_type == "update_display_data":
        display_id = (content.get("transient") or {}).get("display_id")
        if display_id is None:
            trace["notes"].append("update_display_data without display_id ignored")
            return
        touched = ledger.apply_update_everywhere(
            display_id, content.get("data", {}), content.get("metadata", {})
        )
        trace["notes"].append(
            f"update_display_data display_id={display_id!r} applied to cell indexes {touched}"
        )
    elif msg_type == "clear_output":
        area.clear(
            wait=bool(content.get("wait", False)),
            stdout=bool(content.get("stdout", True)),
            stderr=bool(content.get("stderr", True)),
        )
    elif msg_type == "error":
        area.add_error(content)
    # status / execute_input / comm-* / etc. carry no notebook output.
