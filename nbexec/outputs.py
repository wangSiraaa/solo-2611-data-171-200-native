"""Per-cell output state machine implementing Notebook display semantics.

The state machine consumes IOPub messages that already belong to one
execute request (ownership is established from the parent header in the
runner). It is responsible for:

* ordered ``stream`` output with adjacent same-name coalescing, matching
  nbformat conventions;
* preserving full MIME bundles for ``display_data`` / ``execute_result``;
* ``update_display_data``: replace the MIME bundle of *every* output that
  shares the display_id in this kernel run, including outputs produced by
  earlier cells (handled in :class:`OutputLedger`);
* ``clear_output(wait=True)``: the clear is deferred until the next output
  arrives instead of being applied immediately or recorded as output;
* ``error`` outputs, which always flush a pending deferred clear.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import nbformat


def _node(output: Dict[str, Any]) -> Any:
    """Wrap a plain output dict as an nbformat NotebookNode for the writer."""
    return nbformat.from_dict(output)


def _display_id_of(output: Dict[str, Any]) -> Optional[str]:
    # nbformat stores display_id inside output metadata; accept the wire-level
    # top-level transient location as well for robustness.
    metadata = output.get("metadata") or {}
    display_id = (metadata.get("transient") or {}).get("display_id")
    if display_id is not None:
        return display_id
    transient = output.get("transient") or {}
    return transient.get("display_id")


class CellOutputArea:
    """Mutable output list for one code cell."""

    def __init__(self, cell_index: int, cell_id: str) -> None:
        self.cell_index = cell_index
        self.cell_id = cell_id
        self.outputs: List[Dict[str, Any]] = []
        self.pending_clear = False

    def _flush_pending_clear(self) -> None:
        if self.pending_clear:
            self.outputs = []
            self.pending_clear = False

    def clear(self, wait: bool, stdout: bool = True, stderr: bool = True) -> None:
        if wait:
            # Defer: the next real output replaces everything currently shown.
            self.pending_clear = True
            return
        self.pending_clear = False
        if stdout and stderr:
            self.outputs = []
            return
        # Selective clear only removes the requested stream outputs; non-stream
        # outputs and the other stream are left visible.
        cleared_names = set()
        if stdout:
            cleared_names.add("stdout")
        if stderr:
            cleared_names.add("stderr")
        self.outputs = [
            out
            for out in self.outputs
            if not (out.get("output_type") == "stream" and out.get("name") in cleared_names)
        ]

    def add_stream(self, name: str, text: str) -> None:
        self._flush_pending_clear()
        if (
            self.outputs
            and self.outputs[-1].get("output_type") == "stream"
            and self.outputs[-1].get("name") == name
        ):
            self.outputs[-1]["text"] += text
            return
        self.outputs.append(_node({"output_type": "stream", "name": name, "text": text}))

    def add_display(self, content: Dict[str, Any], output_type: str, execution_count: Optional[int]) -> None:
        self._flush_pending_clear()
        metadata = dict(content.get("metadata", {}))
        # The wire protocol carries display_id at content.transient; the on-disk
        # nbformat schema requires it at metadata.transient.display_id.
        transient = content.get("transient")
        if transient:
            metadata["transient"] = transient
        output: Dict[str, Any] = {
            "output_type": output_type,
            "data": content.get("data", {}),
            "metadata": metadata,
        }
        if output_type == "execute_result":
            output["execution_count"] = execution_count
        self.outputs.append(_node(output))

    def add_error(self, content: Dict[str, Any]) -> Dict[str, Any]:
        self._flush_pending_clear()
        output = _node(
            {
                "output_type": "error",
                "ename": content.get("ename", ""),
                "evalue": content.get("evalue", ""),
                "traceback": list(content.get("traceback", [])),
            }
        )
        self.outputs.append(output)
        return output

    def apply_update(self, display_id: str, data: Dict[str, Any], metadata: Dict[str, Any]) -> int:
        """Replace MIME bundles of this cell's outputs matching display_id."""
        self._flush_pending_clear()
        applied = 0
        for output in self.outputs:
            if _display_id_of(output) == display_id:
                output["data"] = data
                merged = dict(output.get("metadata", {}))
                merged.update(metadata)
                output["metadata"] = merged
                applied += 1
        return applied


class OutputLedger:
    """Every output area created during one kernel run, in cell order."""

    def __init__(self) -> None:
        self.areas: List[CellOutputArea] = []

    def open_cell(self, cell_index: int, cell_id: str) -> CellOutputArea:
        area = CellOutputArea(cell_index, cell_id)
        self.areas.append(area)
        return area

    def apply_update_everywhere(
        self, display_id: str, data: Dict[str, Any], metadata: Dict[str, Any]
    ) -> List[int]:
        """Update all matching outputs across the whole run; return cell indexes."""
        touched: List[int] = []
        for area in self.areas:
            if area.apply_update(display_id, data, metadata):
                touched.append(area.cell_index)
        return touched
