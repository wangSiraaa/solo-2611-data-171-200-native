"""End-to-end and structural tests for the template expander."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from docx import Document
from lxml import etree

from template_expand import __version__ as _version  # noqa: F401
from template_expand.api import expand_records
from template_expand.engine import parse_template
from template_expand.errors import (
    MissingLoopValueError,
    StructureError,
    UnpairedMarkerError,
    UnknownFieldError,
)
from template_expand.ooxml import ParagraphIndex, W, WP, R_NS
from template_expand.validate import inspect_package

from conftest import (
    CONTRACT,
    FIXTURES,
    RECORDS,
    TEMPLATE,
    body_of,
    load_template_root,
    make_paragraph,
    replace_text_everywhere,
    write_docx_with_body,
)


def run(tmp_path, records=None, contract=CONTRACT):
    records_path = RECORDS
    if records is not None:
        records_path = tmp_path / "records.json"
        records_path.write_text(json.dumps(records), encoding="utf-8")
    return expand_records(TEMPLATE, records_path, tmp_path / "out",
                          contract_path=contract)


def by_id(result):
    return {s.record_id: s for s in result.statuses}


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------

def test_all_valid_records_render(tmp_path):
    result = run(tmp_path)
    statuses = by_id(result)
    assert statuses["packet-A"].ok
    assert statuses["packet-empty"].ok
    # packet-missing lacks ``note``; it must fail without affecting others.
    assert not statuses["packet-missing"].ok
    assert statuses["packet-missing"].errors[0]["code"] == "missing_value"

    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["records"][0]["ok"] is True


def test_cross_run_placeholder_uses_first_run_format(tmp_path):
    result = run(tmp_path)
    out = by_id(result)["packet-A"].output
    doc = Document(out)
    para = next(p for p in doc.paragraphs if p.text.startswith("Customer:"))
    assert para.text == "Customer: North & East"
    # ``{{cus`` sits in a bold run; the inserted text must stay bold and the
    # untouched trailing italic run must remain italic.
    runs = para.runs
    bold_run = next(r for r in runs if "North & East" in r.text)
    assert bold_run.bold is True
    assert any(r.italic and r.text == "" for r in runs)


def test_surrounding_static_text_preserved(tmp_path):
    result = run(tmp_path)
    out = by_id(result)["packet-A"].output
    doc = Document(out)
    texts = [p.text for p in doc.paragraphs]
    assert "Reference: R-001 — keep this original text." in texts
    assert "Delivery summary" in texts
    assert any("Contact: help" == p.text for p in doc.paragraphs)


def test_newline_becomes_word_break(tmp_path):
    result = run(tmp_path)
    out = by_id(result)["packet-A"].output
    with zipfile.ZipFile(out) as zf:
        root = etree.fromstring(zf.read("word/document.xml"))
    note = next(p for p in root.iter(W("p"))
                if "Line one" in "".join(t.text or "" for t in p.iter(W("t"))))
    assert len(note.findall(f".//{W('br')}")) == 1
    assert "Line oneLine two" in "".join(t.text or "" for t in note.iter(W("t")))


def test_loop_rows_repeat_and_footer_once(tmp_path):
    result = run(tmp_path)
    out = by_id(result)["packet-A"].output
    doc = Document(out)
    rows = [[c.text for c in row.cells] for row in doc.tables[0].rows]
    assert rows == [
        ["Cable <Type A>", "2", "12.50"],
        ["Adapter", "0", "0.00"],
        ["Fixed footer", "do not duplicate", ""],
    ]
    assert by_id(result)["packet-A"].loop_rows == 2


def test_empty_loop_removes_block_keeps_footer(tmp_path):
    result = run(tmp_path)
    out = by_id(result)["packet-empty"].output
    doc = Document(out)
    rows = [[c.text for c in row.cells] for row in doc.tables[0].rows]
    assert rows == [["Fixed footer", "do not duplicate", ""]]
    assert by_id(result)["packet-empty"].loop_rows == 0


# ---------------------------------------------------------------------------
# identity / package invariants
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("record_id", ["packet-A", "packet-empty"])
def test_package_invariants(tmp_path, record_id):
    result = run(tmp_path)
    status = by_id(result)[record_id]
    report = inspect_package(status.output, record_id)
    assert report["bookmark_names"] == report["bookmark_pairs"]
    # packet-A has one bookmark pair per item row; empty has none.
    assert report["bookmark_pairs"] == (2 if record_id == "packet-A" else 0)
    assert report["docpr"] == (2 if record_id == "packet-A" else 0)


def test_bookmarks_paired_and_unique_in_xml(tmp_path):
    result = run(tmp_path)
    out = by_id(result)["packet-A"].output
    with zipfile.ZipFile(out) as zf:
        root = etree.fromstring(zf.read("word/document.xml"))
    starts = {}
    ends = {}
    names = []
    for el in root.iter():
        if el.tag == W("bookmarkStart"):
            starts[el.get(W("id"))] = starts.get(el.get(W("id")), 0) + 1
            names.append(el.get(W("name")))
        elif el.tag == W("bookmarkEnd"):
            ends[el.get(W("id"))] = ends.get(el.get(W("id")), 0) + 1
    assert set(starts) == set(ends)
    assert all(v == 1 for v in starts.values())
    assert all(v == 1 for v in ends.values())
    assert len(names) == len(set(n.casefold() for n in names))


def test_docpr_ids_unique(tmp_path):
    result = run(tmp_path)
    out = by_id(result)["packet-A"].output
    with zipfile.ZipFile(out) as zf:
        root = etree.fromstring(zf.read("word/document.xml"))
    ids = [el.get("id") for el in root.iter(WP("docPr"))]
    assert len(ids) == 2
    assert len(set(ids)) == len(ids)


def test_image_relationship_shared(tmp_path):
    result = run(tmp_path)
    out = by_id(result)["packet-A"].output
    with zipfile.ZipFile(out) as zf:
        root = etree.fromstring(zf.read("word/document.xml"))
    embeds = [el.get(f"{{{R_NS}}}embed") for el in root.iter()
              if el.tag.endswith("}blip")]
    assert embeds == ["rId10", "rId10"]


def test_no_placeholders_remain(tmp_path):
    result = run(tmp_path)
    for status in result.statuses:
        if not status.ok:
            continue
        with zipfile.ZipFile(status.output) as zf:
            xml = zf.read("word/document.xml").decode("utf-8")
        assert "{{" not in xml and "}}" not in xml


def test_template_is_read_only(tmp_path):
    before = hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()
    run(tmp_path)
    after = hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()
    assert before == after


def test_output_reopens_with_plain_reader(tmp_path):
    result = run(tmp_path)
    for status in result.statuses:
        if not status.ok:
            continue
        doc = Document(status.output)
        assert doc.paragraphs
        assert doc.tables


# ---------------------------------------------------------------------------
# failure isolation and error reporting
# ---------------------------------------------------------------------------

def test_failure_does_not_block_others(tmp_path):
    records = [
        {"record_id": "good-1", "customer": "A", "ref": "1", "note": "n",
         "items": [{"name": "X", "qty": 1, "price": "1.00"}]},
        {"record_id": "bad", "customer": "B", "ref": "2", "items": []},
        {"record_id": "good-2", "customer": "C", "ref": "3", "note": "n",
         "items": []},
    ]
    result = run(tmp_path, records=records)
    statuses = by_id(result)
    assert statuses["good-1"].ok and statuses["good-2"].ok
    assert not statuses["bad"].ok
    bad = statuses["bad"].errors[0]
    assert bad["record_id"] == "bad"
    assert bad["field"] == "note"
    assert "location" in bad
    # no output file for the failed record
    assert statuses["bad"].output is None


def test_unknown_field_reported_with_location(tmp_path):
    root = load_template_root()
    body = body_of(root)
    para = make_paragraph(("Unknown: {{mystery}}", None))
    body.insert(1, para)
    tmp_template = tmp_path / "t.docx"
    write_docx_with_body(root, tmp_template)
    records = [{"record_id": "r1", "customer": "A", "ref": "1", "note": "n",
                "mystery": "x", "items": []}]
    records_path = tmp_path / "records.json"
    records_path.write_text(json.dumps(records), encoding="utf-8")
    from template_expand.api import expand_records as expand
    result = expand(tmp_template, records_path, tmp_path / "out",
                    contract_path=CONTRACT)
    status = by_id(result)["r1"]
    assert not status.ok
    err = status.errors[0]
    assert err["code"] == "unknown_field"
    assert err["field"] == "mystery"
    assert err["location"].startswith("word/document.xml")


def test_missing_loop_value_is_reported(tmp_path):
    root = load_template_root()
    from template_expand.engine import Contract
    contract = Contract.from_mapping(json.loads(CONTRACT.read_text(encoding="utf-8")))
    plan = parse_template(root, contract)
    from template_expand.engine import render_record
    with pytest.raises(MissingLoopValueError) as exc:
        render_record(plan, {"customer": "A", "ref": "1", "note": "n"}, "r")
    assert exc.value.record_id == "r"
    assert exc.value.field == "items"
    assert exc.value.location is not None


def test_unpaired_marker_detected(tmp_path):
    root = load_template_root()
    body = body_of(root)
    # Remove the closing marker row.
    table = body.find(W("tbl"))
    rows = [r for r in table if r.tag == W("tr")]
    table.remove(rows[2])
    tmp_template = tmp_path / "t.docx"
    write_docx_with_body(root, tmp_template)
    with pytest.raises(UnpairedMarkerError):
        parse_template(load_from(tmp_template), None)


def load_from(path):
    return Document(str(path)).element


def test_placeholder_crossing_hyperlink_rejected(tmp_path):
    root = load_template_root()
    body = body_of(root)
    p = etree.SubElement(body, W("p"))
    r1 = etree.SubElement(p, W("r"))
    etree.SubElement(r1, W("t")).text = "{{cus"
    hl = etree.SubElement(p, W("hyperlink"))
    r2 = etree.SubElement(hl, W("r"))
    etree.SubElement(r2, W("t")).text = "tomer}}"
    tmp_template = tmp_path / "t.docx"
    write_docx_with_body(root, tmp_template)
    with pytest.raises(StructureError) as exc:
        parse_template(load_from(tmp_template), None)
    assert "hyperlink" in exc.value.message


def test_placeholder_crossing_paragraph_rejected(tmp_path):
    # A placeholder split across two paragraphs must never be treated as one.
    root = load_template_root()
    body = body_of(root)
    body.insert(1, make_paragraph(("{{cus", None)))
    body.insert(2, make_paragraph(("tomer}}", None)))
    tmp_template = tmp_path / "t.docx"
    write_docx_with_body(root, tmp_template)
    with pytest.raises(StructureError) as exc:
        parse_template(load_from(tmp_template), None)
    assert "unclosed" in exc.value.message or "brace" in exc.value.message


def test_unsupported_expression_rejected(tmp_path):
    root = load_template_root()
    body = body_of(root)
    body.insert(1, make_paragraph(("{{a + b}}", None)))
    tmp_template = tmp_path / "t.docx"
    write_docx_with_body(root, tmp_template)
    with pytest.raises(StructureError):
        parse_template(load_from(tmp_template), None)


def test_protocol_keywords(tmp_path):
    result = run(tmp_path)
    for status in result.statuses:
        assert status.record_id
        for use in status.fields:
            assert use.source


def test_xml_special_characters_are_escaped_not_markup(tmp_path):
    records = [{
        "record_id": "special", "customer": "<A & B>", "ref": "R<1>",
        "note": "a & b < c", "items": [{"name": "<x>", "qty": 1, "price": "1"}],
    }]
    result = run(tmp_path, records=records)
    status = by_id(result)["special"]
    assert status.ok, status.errors
    doc = Document(status.output)
    texts = [p.text for p in doc.paragraphs]
    assert "Customer: <A & B>" in texts
    assert "Reference: R<1> — keep this original text." in texts
    assert [c.text for c in doc.tables[0].rows[0].cells][0] == "<x>"


def test_manifest_records_sources(tmp_path):
    result = run(tmp_path)
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    entry = next(r for r in manifest["records"] if r["record_id"] == "packet-A")
    sources = {f["source"] for f in entry["fields"]}
    assert "customer" in sources
    assert "ref" in sources
    assert "items[0].name" in sources
    assert "items[1].price" in sources
    for f in entry["fields"]:
        assert f["location"].startswith("word/document.xml")
        assert f["kind"] in ("scalar", "loop_field")


def test_failed_record_has_no_output_file(tmp_path):
    result = run(tmp_path)
    status = by_id(result)["packet-missing"]
    assert not status.ok
    assert status.output is None
    out_dir = Path(result.manifest_path).parent
    names = sorted(p.name for p in out_dir.iterdir())
    assert names == ["0001_packet-A.docx", "0002_packet-empty.docx", "manifest.json"]
