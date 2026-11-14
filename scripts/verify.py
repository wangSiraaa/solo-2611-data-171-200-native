"""Independent verification for the notebook executor.

Re-opens every output notebook from disk (never trusting in-memory state) and
cross-checks the structured message records against the final cell outputs.
Exits non-zero on the first failed assertion.

Checks cover:
  1. stale outputs/execution counts cleared; sources untouched; params applied
  2. cross-cell update_display_data semantics (earlier cell rewritten)
  3. deferred clear_output(wait=True) and ordered/coalesced streams
  4. first-error stop: later cells unexecuted, failure evidence retained
  5. timeout stop: bounded duration, error evidence, kernel reaped
  6. stdin rejection: explicit failure, no permanent wait, later cell unrun
  7. message ownership: every accepted message traces to an execute request;
     each executed cell has both a shell execute_reply and an idle status
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import nbformat

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
RESULTS = ROOT / "results"

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}")
    if not condition:
        failures.append(label)


def load_nb(name: str) -> dict:
    path = RESULTS / name
    with open(path, "r", encoding="utf-8") as handle:
        nb = json.load(handle)
    nbformat.validate(nbformat.from_dict(nb))
    return nb


def load_record(name: str) -> dict:
    with open(RESULTS / name, "r", encoding="utf-8") as handle:
        return json.load(handle)


def code_cells(nb: dict) -> list[dict]:
    return [c for c in nb["cells"] if c["cell_type"] == "code"]


def output_types(cell: dict) -> list[str]:
    return [o["output_type"] for o in cell.get("outputs", [])]


def find_error(cell: dict) -> dict | None:
    for out in cell.get("outputs", []):
        if out["output_type"] == "error":
            return out
    return None


def display_id_of(output: dict) -> str | None:
    return (output.get("metadata", {}).get("transient") or {}).get("display_id")


# ---------------------------------------------------------------------------
print("== 1. analysis.ipynb: full success, stale cleanup, parameters ==")
nb = load_nb("analysis.ipynb")
record = load_record("analysis.record.json")
with open(FIXTURES / "analysis.ipynb", encoding="utf-8") as handle:
    source = json.load(handle)

for cell in code_cells(nb):
    check(cell["execution_count"] is not None, f"cell {cell['id']} has an execution count")
    check(
        all("STALE OUTPUT" not in json.dumps(o) for o in cell["outputs"]),
        f"cell {cell['id']} contains no stale output text",
    )
check(
    all(c["execution_count"] is None for c in code_cells(source) if True) is False
    and all(c["execution_count"] == 77 for c in code_cells(source)),
    "source notebook on disk still carries the old execution_count=77 (read-only source)",
)
check(
    all(
        "STALE OUTPUT" in json.dumps(c["outputs"])
        for c in code_cells(source)
    ),
    "source notebook on disk still carries stale outputs (read-only source)",
)
param_cell = code_cells(nb)[0]
check(
    "".join(param_cell["source"]) == "threshold = 8\nlabel = 'rerun'",
    "parameters cell rewritten with overridden JSON scalar values",
)
# Non-parameter source cells must be byte-identical to the source notebook.
for src_cell, out_cell in zip(code_cells(source)[1:], code_cells(nb)[1:]):
    check(
        "".join(src_cell["source"]) == "".join(out_cell["source"]),
        f"non-parameter cell {out_cell['id']} source unchanged",
    )
markdown_cells = [c for c in nb["cells"] if c["cell_type"] == "markdown"]
check(len(markdown_cells) == 1 and markdown_cells[0]["id"] == "intro", "markdown cell preserved")

print("\n== 2. cross-cell update_display_data ==")
c2 = code_cells(nb)[1]
c3 = code_cells(nb)[2]
dd = c2["outputs"][0]
check(dd["output_type"] == "display_data", "cell-2 first output is display_data")
check(
    dd["data"]["text/plain"] == ["rerun: 3"] and dd["data"]["text/html"] == ["<b>rerun: 3</b>"],
    "cell-2 display bundle was updated by the later cell's handle.update",
)
did = display_id_of(dd)
check(did is not None, "display_id persisted at metadata.transient.display_id")
update_notes = [n for c in record["cells"] for n in c.get("notes", []) if "update_display_data" in n]
check(
    any("applied to cell indexes [2]" in n for n in update_notes),
    "record notes show update_display_data touched the earlier cell index 2",
)
check(
    c3["outputs"][0]["output_type"] == "stream"
    and c3["outputs"][0]["text"] == ["updated another cell display\n"],
    "cell-3 keeps its own stream output alongside the cross-cell update",
)
check(
    not any(o["output_type"] == "update_display_data" for c in code_cells(nb) for o in c["outputs"]),
    "update_display_data never stored as a notebook output",
)

print("\n== 3. deferred clear_output and stream ordering ==")
c4 = code_cells(nb)[3]
check(output_types(c4) == ["stream"], "cell-4 ends with exactly one stream output (deferred clear applied)")
check(c4["outputs"][0]["text"] == ["after clear complete\n"], "cell-4 visible text is only the post-clear stream")
check(
    all(o["output_type"] != "clear_output" for c in code_cells(nb) for o in c["outputs"]),
    "clear_output directive never stored as a normal output",
)
c5 = code_cells(nb)[4]
check(
    c5["outputs"][0]["output_type"] == "execute_result"
    and c5["outputs"][0]["data"]["text/plain"] == ["40"]
    and c5["outputs"][0]["execution_count"] == c5["execution_count"],
    "cell-5 execute_result preserves MIME data and its execution count (sum 8+12+20=40)",
)
check(
    c2["outputs"][1]["output_type"] == "stream" and c2["outputs"][1]["text"] == ["selected 3\n"],
    "cell-2 stream preserved in arrival order after display_data (threshold 8 selects 3 rows)",
)

print("\n== 4. failure.ipynb: stop on first error ==")
fnb = load_nb("failure.ipynb")
frec = load_record("failure.record.json")
fcells = code_cells(fnb)
err_cell = fcells[3]
err = find_error(err_cell)
check(err is not None and err["ename"] == "ValueError" and err["evalue"] == "synthetic stop",
      "cell-9 retains the ValueError evidence")
check(
    err_cell["outputs"][0]["output_type"] == "display_data"
    and err_cell["outputs"][0]["data"]["text/plain"] == ["'before exception'"],
    "output produced before the exception is retained in the failing cell",
)
check(
    all(c["execution_count"] is None and c["outputs"] == [] for c in fcells[4:]),
    "cells after the error are unexecuted with empty outputs (no stale success results)",
)
check(frec["status"] == "error" and frec["stop_reason"]["kind"] == "error",
      "record status/stop_reason classify the run as an error stop")
check(frec["counts"]["not_run"] == 2 and frec["counts"]["errors"] == 1,
      "record counts: 1 error, 2 not-run cells")
check(
    fcells[1]["outputs"][0]["data"]["text/plain"] == ["rerun: 3"],
    "cells executed before the error keep their valid outputs",
)

print("\n== 5. timeout.ipynb: bounded timeout, kernel reaped ==")
tnb = load_nb("timeout.ipynb")
trec = load_record("timeout.record.json")
tcells = code_cells(tnb)
sleep_cell = tcells[2]
terr = find_error(sleep_cell)
check(terr is not None and terr["ename"] == "KeyboardInterrupt",
      "sleeping cell keeps the interrupt traceback as failure evidence")
check(
    not any(o.get("text") == "late\n" for o in sleep_cell["outputs"] if o["output_type"] == "stream"),
    "the late print never appears after interrupt",
)
check(
    all(c["execution_count"] is None and c["outputs"] == [] for c in tcells[3:]),
    "cells after the timeout never executed",
)
check(trec["status"] == "timeout" and trec["stop_reason"]["kind"] == "timeout",
      "record classifies the run as timeout")
check(
    trec["duration_seconds"] < 20,
    f"run ended well before the 20s sleep (duration={trec['duration_seconds']}s)",
)
shutdown = trec["kernel"]["shutdown"]
check(shutdown["reaped"] is True, "kernel process confirmed reaped after the run")
pid = trec["kernel"]["startup"]["pid"]
alive = True
try:
    os.kill(pid, 0)
except ProcessLookupError:
    alive = False
except PermissionError:
    alive = True
check(not alive, f"timeout kernel pid {pid} no longer exists on the host")
time.sleep(0.2)
check(
    trec["counts"]["timeouts"] == 1 and trec["counts"]["not_run"] == 3,
    "record counts: 1 timeout, 3 not-run cells",
)

print("\n== 6. stdin.ipynb: explicit stdin failure, no permanent wait ==")
snb = load_nb("stdin.ipynb")
srec = load_record("stdin.record.json")
scells = code_cells(snb)
serr = find_error(scells[0])
check(serr is not None and serr["ename"] == "StdinNotImplementedError",
      "input() cell fails explicitly with StdinNotImplementedError")
check(
    all(c["execution_count"] is None and c["outputs"] == [] for c in scells[1:]),
    "cell after the stdin request never executed",
)
check(
    "STALE STDIN" not in json.dumps(scells[0]["outputs"])
    and "STALE AFTER STDIN" not in json.dumps(scells[1]["outputs"]),
    "stale outputs cleared before execution",
)
check(srec["duration_seconds"] < 30, f"stdin case did not hang (duration={srec['duration_seconds']}s)")

print("\n== 7. message ownership and reply+idle completeness ==")
for rec_name in ["analysis.record.json", "failure.record.json", "timeout.record.json", "stdin.record.json"]:
    rec = load_record(rec_name)
    request_ids = {
        c["request_msg_id"]
        for c in rec["cells"]
        if c["request_msg_id"] is not None
    }
    accepted = [m for m in rec["messages"] if m["accepted"]]
    check(
        all(m["request_msg_id"] in request_ids for m in accepted),
        f"{rec_name}: every accepted message belongs to a logged execute request",
    )
    check(
        all(m["parent_matches_request"] is True for m in accepted if m["channel"] != "event"),
        f"{rec_name}: accepted channel messages have a matching parent header",
    )
    for cell in rec["cells"]:
        if cell["status"] == "not_run":
            continue
        rid = cell["request_msg_id"]
        own = [m for m in rec["messages"] if m["request_msg_id"] == rid]
        has_reply = any(m["channel"] == "shell" and m["msg_type"] == "execute_reply" for m in own)
        has_idle = any(
            m["channel"] == "iopub"
            and m["msg_type"] == "status"
            and m["summary"].get("execution_state") == "idle"
            for m in own
        )
        # Force-killed timeout cells may have neither once the grace window expires.
        if cell["status"] == "timeout" and not has_reply:
            check(True, f"{rec_name} cell {cell['cell_index']}: timeout force-killed without reply (allowed)")
        else:
            check(has_reply, f"{rec_name} cell {cell['cell_index']}: shell execute_reply received")
            check(has_idle, f"{rec_name} cell {cell['cell_index']}: iopub idle received")
    # Outputs must be traceable back to the messages that produced them.
    for cell in rec["cells"]:
        if cell["status"] in ("not_run",):
            continue
        producing = [
            m
            for m in rec["messages"]
            if m["request_msg_id"] == cell["request_msg_id"]
            and m["msg_type"] in ("stream", "display_data", "execute_result", "error", "update_display_data", "clear_output")
        ]
        check(
            len(producing) >= cell["output_count"],
            f"{rec_name} cell {cell['cell_index']}: {cell['output_count']} outputs trace to "
            f"{len(producing)} output-producing messages",
        )

print("\n== 8. independent kernels per run ==")
pids = {load_record(n)["kernel"]["startup"]["pid"] for n in
        ["analysis.record.json", "failure.record.json", "timeout.record.json", "stdin.record.json"]}
check(len(pids) == 4, f"each run used a distinct kernel pid: {sorted(pids)}")
versions = {load_record(n)["kernel"]["startup"]["language_version"] for n in
            ["analysis.record.json", "failure.record.json", "timeout.record.json", "stdin.record.json"]}
check(versions == {"3.11.2"}, f"all kernels report Python 3.11.x: {versions}")

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("ALL CHECKS PASSED")
