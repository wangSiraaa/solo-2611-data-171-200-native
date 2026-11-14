"""Unit tests for the output state machine and parameter rewriting (no kernel)."""

import pytest

from nbexec.outputs import CellOutputArea, OutputLedger
from nbexec.parameters import ParameterOverrideError, apply_parameters
import nbformat


def display(content, display_id=None):
    msg = {"data": content, "metadata": {}, "transient": {}}
    if display_id is not None:
        msg["transient"] = {"display_id": display_id}
    return msg


def test_streams_are_ordered_and_coalesced_by_name():
    area = CellOutputArea(0, "c")
    area.add_stream("stdout", "hello ")
    area.add_stream("stdout", "world")
    area.add_stream("stderr", "oops")
    area.add_stream("stderr", "!")
    assert area.outputs == [
        {"output_type": "stream", "name": "stdout", "text": "hello world"},
        {"output_type": "stream", "name": "stderr", "text": "oops!"},
    ]


def test_deferred_clear_waits_for_next_output():
    area = CellOutputArea(0, "c")
    area.add_stream("stdout", "before\n")
    area.clear(wait=True)
    # The clear must not take effect immediately and must not become an output.
    assert len(area.outputs) == 1
    assert area.pending_clear is True
    area.add_stream("stdout", "after\n")
    assert area.outputs == [{"output_type": "stream", "name": "stdout", "text": "after\n"}]


def test_deferred_clear_flushed_by_error_and_discarded_at_end():
    area = CellOutputArea(0, "c")
    area.add_stream("stdout", "visible\n")
    area.clear(wait=True)
    area.add_error({"ename": "E", "evalue": "boom", "traceback": ["E: boom"]})
    assert [o["output_type"] for o in area.outputs] == ["error"]

    area2 = CellOutputArea(1, "d")
    area2.add_stream("stdout", "only output\n")
    area2.clear(wait=True)
    # No further output arrives: the pending clear is simply dropped, so the
    # last visible output remains.
    assert len(area2.outputs) == 1


def test_immediate_clear_removes_everything_now():
    area = CellOutputArea(0, "c")
    area.add_stream("stdout", "x")
    area.add_display(display({"text/plain": "y"}), "display_data", None)
    area.clear(wait=False)
    assert area.outputs == []
    assert area.pending_clear is False


def test_selective_clear_only_removes_the_named_stream():
    area = CellOutputArea(0, "c")
    area.add_stream("stdout", "out")
    area.add_stream("stderr", "err")
    area.add_display(display({"text/plain": "shown"}), "display_data", None)
    area.clear(wait=False, stdout=True, stderr=False)
    assert [o["output_type"] for o in area.outputs] == ["stream", "display_data"]
    assert area.outputs[0]["name"] == "stderr"


def test_interleaved_streams_keep_arrival_order():
    area = CellOutputArea(0, "c")
    area.add_stream("stdout", "a")
    area.add_stream("stderr", "e1")
    area.add_stream("stdout", "b")
    area.add_stream("stderr", "e2")
    assert [(o["name"], o["text"]) for o in area.outputs] == [
        ("stdout", "a"),
        ("stderr", "e1"),
        ("stdout", "b"),
        ("stderr", "e2"),
    ]


def test_update_display_data_rewrites_all_matching_outputs_including_earlier_cells():
    ledger = OutputLedger()
    first = ledger.open_cell(0, "a")
    first.add_display(display({"text/plain": "v1"}, display_id="dup"), "display_data", None)
    other = ledger.open_cell(1, "b")
    other.add_display(display({"text/plain": "other"}, display_id="other-id"), "display_data", None)
    second = ledger.open_cell(2, "c")
    second.add_display(display({"text/plain": "v2"}, display_id="dup"), "display_data", None)

    touched = ledger.apply_update_everywhere("dup", {"text/plain": "new"}, {})
    assert touched == [0, 2]
    assert first.outputs[0]["data"] == {"text/plain": "new"}
    assert second.outputs[0]["data"] == {"text/plain": "new"}
    # The non-matching display is untouched.
    assert other.outputs[0]["data"] == {"text/plain": "other"}


def test_update_unknown_display_id_is_recorded_but_changes_nothing():
    ledger = OutputLedger()
    area = ledger.open_cell(0, "a")
    area.add_display(display({"text/plain": "x"}, display_id="known"), "display_data", None)
    touched = ledger.apply_update_everywhere("missing", {"text/plain": "y"}, {})
    assert touched == []
    assert area.outputs[0]["data"] == {"text/plain": "x"}


def test_display_output_uses_nbformat_metadata_transient_location():
    area = CellOutputArea(0, "c")
    area.add_display(display({"text/plain": "x"}, display_id="id-1"), "display_data", None)
    output = area.outputs[0]
    assert "transient" not in output  # not allowed by the nbformat schema
    assert output["metadata"]["transient"]["display_id"] == "id-1"
    nbformat.validate(nbformat.from_dict({"cells": [
        {"id": "c0", "cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [output], "source": ""}
    ], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}))


def _notebook_with_parameters(source):
    cell = nbformat.v4.new_code_cell(source)
    cell["metadata"]["tags"] = ["parameters"]
    return nbformat.v4.new_notebook(cells=[cell])


def test_parameter_overrides_only_existing_scalar_names():
    nb = _notebook_with_parameters('threshold = 10\nlabel = "original"')
    changes = apply_parameters(nb, {"threshold": 8, "label": "rerun"})
    assert changes == [
        {"name": "threshold", "old_value": 10, "new_value": 8},
        {"name": "label", "old_value": "original", "new_value": "rerun"},
    ]
    assert nb.cells[0].source == "threshold = 8\nlabel = 'rerun'"


def test_unknown_parameter_name_rejected():
    nb = _notebook_with_parameters("threshold = 10")
    with pytest.raises(ParameterOverrideError, match="not present"):
        apply_parameters(nb, {"threshold": 1, "injected": 2})


def test_non_scalar_parameter_rejected():
    nb = _notebook_with_parameters("threshold = 10")
    with pytest.raises(ParameterOverrideError, match="JSON scalar"):
        apply_parameters(nb, {"threshold": [1, 2]})


def test_missing_parameters_cell_rejected():
    nb = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("x = 1")])
    with pytest.raises(ParameterOverrideError, match="tagged 'parameters'"):
        apply_parameters(nb, {"x": 1})


def test_complex_parameters_cell_rejected():
    nb = _notebook_with_parameters("x = 1\ny = x + 1")
    with pytest.raises(ParameterOverrideError, match="JSON scalar literal"):
        apply_parameters(nb, {"x": 2})

    nb2 = _notebook_with_parameters("x += 1")
    with pytest.raises(ParameterOverrideError, match="simple name=literal"):
        apply_parameters(nb2, {"x": 2})
