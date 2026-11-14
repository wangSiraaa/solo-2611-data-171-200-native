"""Independent ZIP/XML validation of rendered output.

These checks deliberately re-open the output package instead of trusting the
in-memory tree: they confirm placeholders are gone, bookmark pairs are
matched and unique, ``wp:docPr`` ids are unique, and every relationship
reference (hyperlinks, images) still resolves.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from lxml import etree

from .errors import TemplateError
from .ooxml import R_NS, REL_NS, W, WP


@dataclass
class RecordStatus:
    record_id: str
    output: Optional[str]
    ok: bool
    fields: list = field(default_factory=list)
    loop_rows: int = 0
    checks: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "ok": self.ok,
            "output": self.output,
            "loop_rows": self.loop_rows,
            "fields": [f.to_dict() if hasattr(f, "to_dict") else f
                       for f in self.fields],
            "checks": self.checks,
            "errors": self.errors,
        }


# A leftover is a syntactically valid placeholder or a stray marker, not any
# pair of braces inside an inserted value (e.g. a customer note "use {{x}}").
_LEFTOVER_RE = re.compile(r"\{\{\s*(?:[#/]\s*)?[A-Za-z_][A-Za-z0-9_.]*\s*\}\}")


def _text_of(p: etree._Element) -> str:
    # Import here to avoid a cycle; logical text rules are shared.
    from .ooxml import ParagraphIndex

    return ParagraphIndex(p).text


def _has_leftover(text: str) -> bool:
    """True if text still looks like an unrendered placeholder.

    Only the placeholder grammar counts, so inserted values that happen to
    contain literal braces are not misreported as leftovers.
    """
    return bool(_LEFTOVER_RE.search(text))


def validate_tree(root: etree._Element, record_id: str) -> dict:
    """Checks that can be done on an in-memory document tree."""
    body = root.find(W("body"))
    if body is None:
        raise TemplateError("output has no w:body", record_id=record_id)

    residual = []
    for p in body.iter(W("p")):
        text = _text_of(p)
        if _has_leftover(text):
            residual.append(text.strip())
    if residual:
        raise TemplateError(
            "unreplaced placeholder text remains: " + repr(residual[0]),
            record_id=record_id)

    return {"residual_placeholders": 0}


def _resolve_target(base_part: str, target: str, parts: set[str]) -> bool:
    target = unquote(target)
    if target.startswith("/"):
        candidate = target.lstrip("/")
    else:
        base_dir = base_part.rsplit("/", 1)[0] if "/" in base_part else ""
        candidate = f"{base_dir}/{target}" if base_dir else target
    candidate = candidate.replace("\\", "/")
    return candidate in parts


def inspect_package(path: str | Path, record_id: str = "?") -> dict:
    """ZIP/XML-level checks for a written DOCX file."""
    path = Path(path)
    with zipfile.ZipFile(path) as zf:
        package_parts = set(zf.namelist())
        if "word/document.xml" not in package_parts:
            raise TemplateError("output is not a Word document",
                                record_id=record_id)
        doc = etree.fromstring(zf.read("word/document.xml"))
        rels = etree.fromstring(zf.read("word/_rels/document.xml.rels"))

    body = doc.find(W("body"))
    if body is None:
        raise TemplateError("output has no w:body", record_id=record_id)

    # --- placeholders -----------------------------------------------------
    residual = [t for t in (_text_of(p) for p in body.iter(W("p")))
                if _has_leftover(t)]
    if residual:
        raise TemplateError("unreplaced placeholder remains in package: "
                            + repr(residual[0].strip()), record_id=record_id)

    # --- bookmarks -----------------------------------------------------
    starts: dict[str, int] = {}
    ends: dict[str, int] = {}
    names: list[str] = []
    for el in doc.iter():
        if el.tag == W("bookmarkStart"):
            ident = el.get(W("id"))
            name = el.get(W("name"))
            if ident is None or name is None:
                raise TemplateError("bookmarkStart missing id or name",
                                    record_id=record_id)
            starts[ident] = starts.get(ident, 0) + 1
            names.append(name)
        elif el.tag == W("bookmarkEnd"):
            ident = el.get(W("id"))
            if ident is None:
                raise TemplateError("bookmarkEnd missing id", record_id=record_id)
            ends[ident] = ends.get(ident, 0) + 1
    unbalanced = [i for i in set(starts) | set(ends) if starts.get(i, 0) != ends.get(i, 0)]
    if unbalanced:
        raise TemplateError(
            f"bookmark ids not paired 1:1: {sorted(unbalanced)}",
            record_id=record_id)
    if len(names) != len({n.casefold() for n in names}):
        raise TemplateError("bookmark names are not unique", record_id=record_id)
    for ident, count in starts.items():
        if count > 1:
            raise TemplateError(f"bookmark id {ident} used by multiple starts",
                                record_id=record_id)

    # --- drawing ids ----------------------------------------------------
    docpr_ids = [el.get("id") for el in doc.iter(WP("docPr"))]
    if len(docpr_ids) != len(set(docpr_ids)):
        raise TemplateError("duplicate wp:docPr ids", record_id=record_id)

    # --- relationships --------------------------------------------------
    relationship_ids = {rel.get("Id") for rel in rels.iter()}
    missing_internal = []
    used_rel_ids = set()
    for el in doc.iter():
        for attr, value in el.attrib.items():
            if not isinstance(attr, str) or not attr.startswith(f"{{{R_NS}}}"):
                continue
            used_rel_ids.add(value)
            if value not in relationship_ids:
                raise TemplateError(
                    f"relationship {value!r} referenced but not declared",
                    record_id=record_id)
            rel = rels.find(
                f"./{{{REL_NS}}}Relationship[@Id='{value}']")
            assert rel is not None
            if rel.get("TargetMode") != "External":
                target = rel.get("Target")
                if target is not None and not _resolve_target(
                        "word/document.xml", target, package_parts):
                    missing_internal.append(target)
    if missing_internal:
        raise TemplateError(
            f"relationship targets not found: {sorted(set(missing_internal))}",
            record_id=record_id)

    return {
        "bookmark_pairs": len(starts),
        "bookmark_names": len(names),
        "docpr": len(docpr_ids),
        "relationships_used": len(used_rel_ids),
        "rows": len(list(body.iter(W("tr")))) - _nested_row_double_count(body),
    }


def _nested_row_double_count(body: etree._Element) -> int:
    # Nested tables would be counted twice by iter(); subtract one per nesting
    # level.  Supported scope has no nested tables, so this is a no-op guard.
    total = 0
    for tbl in body.iter(W("tbl")):
        depth = 0
        node = tbl.getparent()
        while node is not None and node is not body:
            if node.tag == W("tbl"):
                depth += 1
            node = node.getparent()
        total += depth
    return total


def validate_output(root: etree._Element, record_id: str) -> dict:
    return validate_tree(root, record_id)
