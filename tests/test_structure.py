"""Structural / negative-path tests using synthetic templates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from docx import Document
from lxml import etree

from template_expand.api import expand_records
from template_expand.engine import Contract, parse_template
from template_expand.errors import StructureError, UnpairedMarkerError
from template_expand.ooxml import W, WP

from conftest import (
    CONTRACT,
    TEMPLATE,
    body_of,
    load_template_root,
    make_paragraph,
    write_docx_with_body,
)


def _row(body, index=0):
    table = body.find(W("tbl"))
    return [r for r in table if r.tag == W("tr")][index]


def _cell(row, index=0):
    return [c for c in row if c.tag == W("tc")][index]


def save(tmp_path, root):
    path = tmp_path / "tpl.docx"
    write_docx_with_body(root, path)
    return path


def run(tmp_path, template, records, contract=CONTRACT):
    rec = tmp_path / "records.json"
    rec.write_text(json.dumps(records), encoding="utf-8")
    return expand_records(template, rec, tmp_path / "out",
                          contract_path=contract)


def test_prototype_bookmark_imbalance_rejected(tmp_path):
    root = load_template_root()
    body = body_of(root)
    row = _row(body, 1)
    # Add an unmatched bookmarkEnd to the prototype row.
    p = list(row.iter(W("p")))[0]
    etree.SubElement(p, W("bookmarkEnd")).set(W("id"), "99")
    template = save(tmp_path, root)
    with pytest.raises(StructureError) as exc:
        parse_template(Document(str(template)).element, None)
    assert "not paired" in exc.value.message


def test_placeholder_in_textbox_rejected(tmp_path):
    root = load_template_root()
    body = body_of(root)
    outer_p = make_paragraph(("outside", None))
    drawing_run = etree.SubElement(outer_p, W("r"))
    drawing = etree.SubElement(drawing_run, W("drawing"))
    tbox = etree.SubElement(drawing, W("txbxContent"))
    inner_p = etree.SubElement(tbox, W("p"))
    ir = etree.SubElement(inner_p, W("r"))
    it = etree.SubElement(ir, W("t"))
    it.text = "{{customer}}"
    body.insert(1, outer_p)
    template = save(tmp_path, root)
    with pytest.raises(StructureError) as exc:
        parse_template(Document(str(template)).element, None)
    assert "unsupported structure" in exc.value.message or \
           "unsupported" in exc.value.message


def test_marker_not_whole_row_rejected(tmp_path):
    root = load_template_root()
    body = body_of(root)
    # Put extra text beside the start marker in the same row.
    table = body.find(W("tbl"))
    rows = [r for r in table if r.tag == W("tr")]
    first_cell = _cell(rows[0])
    second_cell = [c for c in rows[0] if c.tag == W("tc")][1]
    p = second_cell.find(W("p"))
    r = etree.SubElement(p, W("r"))
    etree.SubElement(r, W("t")).text = "extra"
    template = save(tmp_path, root)
    # With the row no longer a pure marker, no start marker is found.
    with pytest.raises(UnpairedMarkerError):
        parse_template(Document(str(template)).element, None)


def test_extra_marker_outside_table_rejected(tmp_path):
    root = load_template_root()
    body = body_of(root)
    body.insert(1, make_paragraph(("{{#items}}", None)))
    template = save(tmp_path, root)
    with pytest.raises(UnpairedMarkerError):
        parse_template(Document(str(template)).element, None)


def test_docpr_and_bookmarks_unique_across_many_clones(tmp_path):
    root = load_template_root()
    template = save(tmp_path, root)
    records = [{
        "record_id": "many", "customer": "C", "ref": "R", "note": "n",
        "items": [{"name": f"n{i}", "qty": i, "price": "1.00"} for i in range(5)],
    }]
    result = run(tmp_path, template, records)
    status = result.statuses[0]
    assert status.ok, status.errors
    from template_expand.validate import inspect_package
    report = inspect_package(status.output, "many")
    assert report["bookmark_pairs"] == 5
    assert report["bookmark_names"] == 5
    assert report["docpr"] == 5


def test_scalar_item_field_outside_loop_rejected(tmp_path):
    root = load_template_root()
    body = body_of(root)
    body.insert(1, make_paragraph(("{{item.name}}", None)))
    template = save(tmp_path, root)
    records = [{"record_id": "r", "customer": "C", "ref": "R", "note": "n",
                "items": []}]
    result = run(tmp_path, template, records)
    status = result.statuses[0]
    assert not status.ok
    assert status.errors[0]["code"] == "missing_value"


def test_contract_is_enforced(tmp_path):
    root = load_template_root()
    body = body_of(root)
    body.insert(1, make_paragraph(("{{extra}}", None)))
    template = save(tmp_path, root)
    records = [{"record_id": "r", "customer": "C", "ref": "R", "note": "n",
                "extra": "x", "items": []}]
    result = run(tmp_path, template, records)
    status = result.statuses[0]
    assert not status.ok
    assert status.errors[0]["code"] == "unknown_field"


def test_no_contract_unknown_but_recorded(tmp_path):
    root = load_template_root()
    body = body_of(root)
    body.insert(1, make_paragraph(("{{extra}}", None)))
    template = save(tmp_path, root)
    records = [{"record_id": "r", "customer": "C", "ref": "R", "note": "n",
                "items": []}]
    result = run(tmp_path, template, records, contract=None)
    status = result.statuses[0]
    assert not status.ok
    assert status.errors[0]["code"] == "missing_value"
    assert status.errors[0]["field"] == "extra"


def test_loop_value_not_a_list(tmp_path):
    template = save(tmp_path, load_template_root())
    records = [{"record_id": "r", "customer": "C", "ref": "R", "note": "n",
                "items": "not-a-list"}]
    result = run(tmp_path, template, records)
    status = result.statuses[0]
    assert not status.ok
    assert status.errors[0]["code"] == "missing_loop_value"


def test_loop_item_not_object(tmp_path):
    template = save(tmp_path, load_template_root())
    records = [{"record_id": "r", "customer": "C", "ref": "R", "note": "n",
                "items": ["oops"]}]
    result = run(tmp_path, template, records)
    status = result.statuses[0]
    assert not status.ok
    assert status.errors[0]["code"] == "missing_loop_value"


def test_scalar_only_template_renders(tmp_path):
    # A template with no row loop at all must still render scalars.
    doc = Document(str(TEMPLATE))
    body = body_of(doc.element)
    for child in list(body):
        body.remove(child)
    from lxml import etree as _et
    p = _et.SubElement(body, W("p"))
    r = _et.SubElement(p, W("r"))
    _et.SubElement(r, W("t")).text = "Hello {{customer}} / {{ref}}"
    doc.add_paragraph("Note: {{note}}")
    template = save(tmp_path, doc.element)
    records = [{"record_id": "r", "customer": "C", "ref": "R", "note": "n"}]
    result = run(tmp_path, template, records)
    status = result.statuses[0]
    assert status.ok, status.errors
    out = Document(status.output)
    assert "Hello C / R" in [p.text for p in out.paragraphs]
