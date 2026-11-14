"""Shared test helpers: build small DOCX templates from explicit OOXML."""

from __future__ import annotations

import copy
import io
import zipfile
from pathlib import Path

from docx import Document
from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = lambda tag: f"{{{W_NS}}}{tag}"  # noqa: E731

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TEMPLATE = FIXTURES / "template.docx"
RECORDS = FIXTURES / "records.json"
CONTRACT = FIXTURES / "template-contract.json"


def load_template_root():
    return Document(str(TEMPLATE)).element


def body_of(doc):
    return doc.find(W("body"))


def make_paragraph(*runs: tuple[str, str | None]) -> etree._Element:
    """Build a ``w:p`` from ``(text, run_property)`` pairs.

    ``run_property`` is ``"b"``/``"i"``/``None`` (plain).
    """
    p = etree.Element(W("p"))
    for text, prop in runs:
        r = etree.SubElement(p, W("r"))
        if prop:
            rpr = etree.SubElement(r, W("rPr"))
            etree.SubElement(rpr, W(prop))
        t = etree.SubElement(r, W("t"))
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t.text = text
    return p


def write_docx_with_body(root: etree._Element, path: Path) -> Path:
    """Write a DOCX whose main body is ``root``, reusing the fixture package."""
    doc = Document(str(TEMPLATE))
    body = body_of(doc.element)
    new_body = body_of(root)
    for child in list(body):
        body.remove(child)
    for child in new_body:
        body.append(copy.deepcopy(child))
    doc.save(str(path))
    return path


def replace_text_everywhere(root: etree._Element, old: str, new: str) -> None:
    for t in root.iter(W("t")):
        if t.text and old in t.text:
            t.text = t.text.replace(old, new)


def docx_bytes(document) -> bytes:
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()
