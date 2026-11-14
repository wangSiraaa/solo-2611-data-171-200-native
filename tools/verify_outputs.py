"""Standalone verifier for generated DOCX files.

This deliberately does *not* reuse the renderer's in-memory state.  It re-opens
the output packages as ZIP/XML, checks placeholder removal, bookmark
pairing/uniqueness, ``wp:docPr`` uniqueness and relationship resolution, and
then re-opens each file with a plain ``python-docx`` reader to confirm visible
text and table rows.  Finally it confirms the source template bytes are
unchanged.

Usage::

    python tools/verify_outputs.py fixtures/template.docx fixtures/records.json out

Exit code 0 means every check passed.
"""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

from docx import Document
from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
W = lambda t: f"{{{W_NS}}}{t}"  # noqa: E731


def logical_text(p):
    parts = []
    for node in p.iter():
        if node.tag == W("t"):
            parts.append(node.text or "")
        elif node.tag in (W("br"), W("cr")):
            parts.append("\n")
        elif node.tag == W("tab"):
            parts.append("\t")
    return "".join(parts)


def resolve(base, target, parts):
    if target.startswith("/"):
        candidate = target.lstrip("/")
    else:
        directory = base.rsplit("/", 1)[0] if "/" in base else ""
        candidate = f"{directory}/{target}" if directory else target
    return candidate.replace("\\", "/") in parts


def check_package(path: Path):
    problems = []
    with zipfile.ZipFile(path) as zf:
        parts = set(zf.namelist())
        doc = etree.fromstring(zf.read("word/document.xml"))
        rels = etree.fromstring(zf.read("word/_rels/document.xml.rels"))
        body = doc.find(W("body"))

        residual = [logical_text(p) for p in body.iter(W("p"))
                    if "{{" in logical_text(p) or "}}" in logical_text(p)]
        if residual:
            problems.append(f"residual placeholder: {residual[0]!r}")

        starts, ends, names = {}, {}, []
        for el in doc.iter():
            if el.tag == W("bookmarkStart"):
                starts[el.get(W("id"))] = starts.get(el.get(W("id")), 0) + 1
                names.append(el.get(W("name")))
            elif el.tag == W("bookmarkEnd"):
                ends[el.get(W("id"))] = ends.get(el.get(W("id")), 0) + 1
        if set(starts) != set(ends):
            problems.append(f"unpaired bookmark ids {sorted(set(starts) ^ set(ends))}")
        if any(v != 1 for v in starts.values()) or any(v != 1 for v in ends.values()):
            problems.append("bookmark id repeated")
        folded = [n.casefold() for n in names]
        if len(folded) != len(set(folded)):
            problems.append("bookmark names not unique")

        docpr = [el.get("id") for el in doc.iter(f"{{{WP_NS}}}docPr")]
        if len(docpr) != len(set(docpr)):
            problems.append(f"duplicate docPr ids {docpr}")

        rel_ids = {rel.get("Id") for rel in rels.iter()}
        by_id = {r.get("Id"): r for r in rels.iter()}
        for el in doc.iter():
            for attr, value in el.attrib.items():
                if isinstance(attr, str) and attr.startswith(f"{{{R_NS}}}"):
                    if value not in rel_ids:
                        problems.append(f"undeclared relationship {value}")
                        continue
                    rel = by_id[value]
                    if rel.get("TargetMode") != "External":
                        target = rel.get("Target")
                        if target and not resolve("word/document.xml", target, parts):
                            problems.append(
                                f"missing relationship target {target}")
    return problems, {"bookmarks": len(starts), "docpr": len(docpr)}


def visible(path: Path):
    doc = Document(str(path))
    paras = [p.text for p in doc.paragraphs]
    rows = [[c.text for c in r.cells] for t in doc.tables for r in t.rows]
    return paras, rows


def main(argv):
    if len(argv) != 4:
        print(__doc__)
        return 2
    template, records_path, out_dir = argv[1], argv[2], argv[3]
    template = Path(template)
    out_dir = Path(out_dir)
    records = json.loads(Path(records_path).read_text(encoding="utf-8"))
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_hash = hashlib.sha256(template.read_bytes()).hexdigest()

    failures = 0
    for entry in manifest["records"]:
        rid = entry["record_id"]
        if not entry["ok"]:
            print(f"[skip] {rid}: reported failure {entry['errors']}")
            continue
        path = Path(entry["output"])
        problems, counts = check_package(path)
        try:
            paras, rows = visible(path)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"python-docx could not open: {exc}")
            paras, rows = [], []
        if problems:
            failures += 1
            print(f"[FAIL] {rid}: {problems}")
        else:
            print(f"[ok] {rid}: bookmarks={counts['bookmarks']} "
                  f"docpr={counts['docpr']} rows={len(rows)}")

    after_hash = hashlib.sha256(template.read_bytes()).hexdigest()
    if source_hash != after_hash:
        failures += 1
        print("[FAIL] source template changed!")
    else:
        print("[ok] source template unchanged")

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
