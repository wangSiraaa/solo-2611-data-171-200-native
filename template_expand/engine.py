"""Template expansion engine (supported subset only).

Supported template vocabulary, deliberately small:

* ``{{field}}`` and ``{{item.field}}`` scalars in ordinary paragraph text and
  ordinary table cells of ``word/document.xml``;
* a single ``items`` row block delimited by two paragraphs that contain only
  ``{{#items}}`` / ``{{/items}}`` and are rows of the same table, with one or
  more prototype rows between them.

Everything else is rejected with a located error rather than partially
rendered: unknown fields, missing values, unmatched markers, placeholders
crossing run/hyperlink/paragraph boundaries, and expressions outside the
accepted grammar.  Rendering works on the OOXML tree, never on raw XML text.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Optional

from lxml import etree

from .errors import (
    DocLocation,
    MissingLoopValueError,
    MissingValueError,
    StructureError,
    TemplateError,
    UnknownFieldError,
    UnpairedMarkerError,
)
from .identity import clone_element, collect_identities, reidentify_clone
from .ooxml import (
    MARKER_RE,
    ParagraphIndex,
    Replacement,
    W,
    apply_replacements,
    scan_text,
)


@dataclass
class FieldUse:
    field: str
    kind: str            # "scalar" | "loop_field"
    location: DocLocation
    source: str          # dotted path into the record

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "kind": self.kind,
            "location": self.location.describe(),
            "source": self.source,
        }


@dataclass
class _LoopBlock:
    table_index: int
    start_row: int
    end_row: int
    prototype_count: int
    item_prefix: str = "item"

    @property
    def first_prototype(self) -> int:
        return self.start_row + 1


@dataclass(frozen=True)
class Contract:
    """Optional declared vocabulary from ``template-contract.json``."""

    scalar_keys: frozenset[str] = frozenset()
    loop: str = "items"
    loop_fields: frozenset[str] = frozenset()
    declared: bool = False

    @classmethod
    def from_mapping(cls, data: dict) -> "Contract":
        return cls(
            scalar_keys=frozenset(data.get("scalar_keys") or ()),
            loop=str(data.get("loop") or "items"),
            loop_fields=frozenset(data.get("loop_fields") or ()),
            declared=True,
        )


@dataclass
class TemplatePlan:
    """Parsed, validated template reusable for many records."""

    document: etree._Element
    body: etree._Element
    loop: Optional[_LoopBlock]
    contract: Optional[Contract] = None

    @property
    def item_prefix(self) -> str:
        return self.loop.item_prefix if self.loop is not None else "item"

    def copy_document(self) -> etree._Element:
        return etree.fromstring(etree.tostring(self.document))


# ---------------------------------------------------------------------------
# template analysis
# ---------------------------------------------------------------------------

def _body_children(body: etree._Element) -> list[etree._Element]:
    return [c for c in body if isinstance(c.tag, str)]


def _tables(body: etree._Element) -> list[etree._Element]:
    return [c for c in _body_children(body) if c.tag == W("tbl")]


def _rows(table: etree._Element) -> list[etree._Element]:
    return [r for r in table if r.tag == W("tr")]


def _row_text(row: etree._Element) -> str:
    parts = []
    for cell in row.iter(W("tc")):
        parts.append("".join(ParagraphIndex(p).text for p in cell.iter(W("p"))))
    return "".join(parts).strip()


def _classify_row_marker(row: etree._Element) -> Optional[tuple[str, str]]:
    """Return ``(kind, name)`` if the row consists solely of one marker.

    The marker must be whole within a single paragraph; a marker split across
    cells (or paragraphs) is not a valid block delimiter.
    """
    paragraphs = [ParagraphIndex(p).text for p in row.iter(W("p"))]
    non_empty = [text.strip() for text in paragraphs if text.strip()]
    if len(non_empty) != 1:
        return None
    m = MARKER_RE.match(non_empty[0])
    if not m:
        return None
    # Whole-row text must be exactly the marker (no other cell content).
    if _row_text(row) != non_empty[0]:
        return None
    return ("start" if m.group(1) == "#" else "end"), m.group(2)


def _is_ordinary_paragraph(p: etree._Element, body: etree._Element) -> bool:
    """True for a top-level-body or table-cell paragraph.

    Paragraphs nested in text boxes, content controls, drawings or inside
    another paragraph are out of the supported scope.
    """
    excluded = {
        W("txbxContent"), W("sdt"), W("sdtContent"), W("drawing"),
        W("pict"), W("object"), W("p"),
    }
    node = p.getparent()
    while node is not None and node is not body:
        if node.tag in excluded:
            return False
        node = node.getparent()
    return node is body


def _cell_index(el: etree._Element, parent: etree._Element) -> int:
    return [c for c in parent if c.tag == el.tag].index(el)


def locate_paragraph(body: etree._Element, p: etree._Element) -> DocLocation:
    """Best-effort structural location for a paragraph in the main body."""
    tables = _tables(body)
    table_index = row_index = cell_index = None
    node = p
    while node is not None and node is not body:
        parent = node.getparent()
        if node.tag == W("tc") and cell_index is None and parent is not None:
            cell_index = _cell_index(node, parent)
        elif node.tag == W("tr") and row_index is None and parent is not None:
            row_index = _cell_index(node, parent)
        elif node.tag == W("tbl") and table_index is None and node in tables:
            table_index = tables.index(node)
        node = parent
    body_index = None
    top = p
    while top.getparent() is not None and top.getparent() is not body:
        top = top.getparent()
    if top.getparent() is body:
        body_index = _body_children(body).index(top)
    paragraph_index = None
    container = p.getparent()
    while container is not None and container.tag not in (W("tc"), W("body")):
        container = container.getparent()
    if container is not None:
        paras = [c for c in container if c.tag == W("p")]
        if p in paras:
            paragraph_index = paras.index(p)
    return DocLocation(
        body_index=body_index,
        paragraph_index=paragraph_index,
        table_index=table_index,
        row_index=row_index,
        cell_index=cell_index,
    )


def _scan_paragraph(p: etree._Element, body: etree._Element) -> list:
    tokens, problems = ParagraphIndex(p).tokens()
    loc = locate_paragraph(body, p)
    if problems:
        off, msg = problems[0]
        raise StructureError(msg, location=replace(loc, char_offset=off))
    return tokens


def parse_template(document: etree._Element,
                   contract: Optional[Contract] = None) -> TemplatePlan:
    body = document.find(W("body"))
    if body is None:
        raise StructureError("document has no w:body")

    loop: Optional[_LoopBlock] = None
    starts: list[tuple[int, int]] = []
    ends: list[tuple[int, int]] = []
    for ti, table in enumerate(_tables(body)):
        for ri, row in enumerate(_rows(table)):
            marker = _classify_row_marker(row)
            if marker is None:
                continue
            kind, name = marker
            declared_loop = contract.loop if (contract and contract.declared) else "items"
            if name != declared_loop:
                raise StructureError(
                    f"only the {declared_loop!r} row loop is supported, found {name!r}",
                    location=DocLocation(table_index=ti, row_index=ri),
                    field=name,
                )
            (starts if kind == "start" else ends).append((ti, ri))

    if len(starts) == 0 and len(ends) == 0:
        loop = None
    elif len(starts) != 1 or len(ends) != 1:
        loc = DocLocation(table_index=(starts or ends or [(0, 0)])[0][0],
                          row_index=(starts or ends or [(0, 0)])[0][1]) \
            if (starts or ends) else DocLocation()
        raise UnpairedMarkerError(
            f"expected at most one {{{{#items}}}} / {{{{/items}}}} pair, "
            f"found {len(starts)} start / {len(ends)} end",
            location=loc,
            field="items",
        )
    else:
        st_ti, st_ri = starts[0]
        en_ti, en_ri = ends[0]
        if st_ti != en_ti:
            raise StructureError(
                "loop markers must be rows of the same table",
                location=DocLocation(table_index=st_ti, row_index=st_ri),
                field="items",
            )
        if st_ri >= en_ri:
            raise UnpairedMarkerError(
                "{{/items}} appears before {{#items}}",
                location=DocLocation(table_index=en_ti, row_index=en_ri),
                field="items",
            )
        if en_ri - st_ri == 1:
            raise UnpairedMarkerError(
                "loop block has no prototype row between its markers",
                location=DocLocation(table_index=st_ti, row_index=st_ri),
                field="items",
            )
        loop = _LoopBlock(table_index=st_ti, start_row=st_ri, end_row=en_ri,
                          prototype_count=en_ri - st_ri - 1)

    # Validate all paragraphs in the body: no stray/unpaired markers anywhere.
    # Marker-row paragraphs are legitimate loop syntax and are skipped until the
    # row-expansion pass removes them.
    marker_table = _tables(body)[loop.table_index] if loop is not None else None
    marker_row_indices = ({loop.start_row, loop.end_row}
                          if loop is not None else set())
    for p in body.iter(W("p")):
        if not _is_ordinary_paragraph(p, body):
            # A placeholder inside a text box / content control / drawing is a
            # cross-structure use this expander deliberately does not support.
            inside_tokens, _ = ParagraphIndex(p).tokens()
            if inside_tokens:
                raise StructureError(
                    "placeholder inside an unsupported structure "
                    "(text box, content control or drawing)",
                    location=locate_paragraph(body, p))
            continue
        row = p
        while row is not None and row.tag != W("tr"):
            row = row.getparent()
        if (marker_table is not None and row is not None
                and row.getparent() is marker_table):
            rows_here = _rows(row.getparent())
            if rows_here.index(row) in marker_row_indices:
                continue
        tokens = _scan_paragraph(p, body)
        for tok in tokens:
            if tok.kind != "scalar":
                raise UnpairedMarkerError(
                    f"loop marker {tok.raw!r} is not a whole marker row",
                    location=locate_paragraph(body, p),
                )

    if loop is not None:
        prototypes = _rows(marker_table)[loop.first_prototype:loop.end_row]
        loop.item_prefix = _detect_item_prefix(prototypes, loop)

    # Prototype rows must carry balanced bookmarks within each row, otherwise a
    # clone would produce a dangling reference.
    if loop is not None:
        for offset in range(loop.prototype_count):
            ri = loop.first_prototype + offset
            row = _rows(marker_table)[ri]
            _check_row_bookmarks(row, DocLocation(table_index=loop.table_index,
                                                  row_index=ri))

    return TemplatePlan(document=document, body=body, loop=loop,
                        contract=contract)


def _check_row_bookmarks(row: etree._Element, loc: DocLocation) -> None:
    starts, ends = set(), set()
    for el in row.iter():
        if el.tag == W("bookmarkStart"):
            starts.add(el.get(W("id")))
        elif el.tag == W("bookmarkEnd"):
            ends.add(el.get(W("id")))
    if starts != ends:
        raise StructureError(
            "prototype row has bookmark starts/ends that are not paired "
            "within the row: " + repr(sorted(starts ^ ends)),
            location=loc)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _coerce(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _resolve(record: dict, item: Optional[dict], item_prefix: str,
             name: str, contract: Optional[Contract] = None,
             path_prefix: str = "") -> tuple[str, str]:
    """Resolve a placeholder name, returning ``(text, source_path)``.

    ``UnknownFieldError`` means the name is outside the supported vocabulary
    (checked against the contract when one is supplied).  ``MissingValueError``
    means the name is valid but the record does not carry a scalar value for it.
    ``source_path`` is a JSON-style pointer such as ``items[0].name``.
    """
    if "." in name:
        prefix, rest = name.split(".", 1)
        allowed_prefixes = {item_prefix, "item", "items"}
        if contract is not None and contract.declared:
            allowed_prefixes.add(contract.loop)
        if prefix not in allowed_prefixes:
            raise UnknownFieldError(
                f"field {name!r} is outside the supported scope", field=name)
        if contract is not None and contract.declared and rest not in contract.loop_fields:
            raise UnknownFieldError(
                f"unknown loop field {name!r}", field=name)
        if not rest or "." in rest:
            raise UnknownFieldError(f"unsupported field path {name!r}", field=name)
        if item is None:
            raise MissingValueError(
                f"field {name!r} is only available inside {{{{#{item_prefix}}}}}",
                field=name)
        if rest not in item or item[rest] is None:
            raise MissingValueError(
                f"record item has no value for field {name!r}", field=name)
        value = item[rest]
        if isinstance(value, (dict, list)):
            raise MissingValueError(
                f"field {name!r} is not scalar and cannot be inserted", field=name)
        source = f"{path_prefix}.{rest}" if path_prefix else f"{rest}"
        return _coerce(value), source
    if contract is not None and contract.declared and name not in contract.scalar_keys:
        raise UnknownFieldError(f"unknown scalar {name!r}", field=name)
    if name not in record or record[name] is None:
        raise MissingValueError(f"record has no value for field {name!r}",
                                field=name)
    value = record[name]
    if isinstance(value, (dict, list)):
        raise MissingValueError(
            f"field {name!r} is not scalar and cannot be inserted", field=name)
    return _coerce(value), name


def _replace_tokens(p: etree._Element, record: dict, item: Optional[dict],
                    item_prefix: str, location: DocLocation, record_id: str,
                    kind: str, field_uses: list[FieldUse],
                    contract: Optional[Contract] = None,
                    path_prefix: str = "") -> None:
    tokens = _scan_paragraph(p, p.getroottree().getroot().find(W("body")))
    if not tokens:
        return
    repls: list[Replacement] = []
    for tok in tokens:
        if tok.kind != "scalar":
            raise UnpairedMarkerError(
                f"unmatched loop marker {tok.raw!r}",
                location=location, record_id=record_id)
        try:
            value, source = _resolve(record, item, item_prefix, tok.name,
                                     contract, path_prefix)
        except TemplateError as exc:
            exc.location = location
            exc.record_id = record_id
            raise
        repls.append(Replacement(tok.start, tok.end, value))
        field_uses.append(FieldUse(tok.name, kind, location, source))
    apply_replacements(ParagraphIndex(p), repls)


def render_record(plan: TemplatePlan, record: dict,
                  record_id: str) -> tuple[etree._Element, list[FieldUse], int]:
    """Render one record onto a fresh copy of the template.

    Returns ``(root, field_uses, materialised_row_count)``.  Raises a
    :class:`TemplateError` carrying ``record_id`` on any problem.
    """
    doc = plan.copy_document()
    body = doc.find(W("body"))
    assert body is not None

    field_uses: list[FieldUse] = []
    loop_rows = 0

    if plan.loop is not None:
        loop_rows = _expand_loop(body, plan.loop, record, record_id, field_uses,
                                 plan.contract)

    # Fill every remaining scalar.  Marker/prototype rows were removed by the
    # loop pass, so anything left is ordinary content.
    for p in list(body.iter(W("p"))):
        if not _is_ordinary_paragraph(p, body):
            continue
        tokens, problems = ParagraphIndex(p).tokens()
        if problems:
            off, msg = problems[0]
            raise StructureError(
                msg, location=replace(locate_paragraph(body, p), char_offset=off),
                record_id=record_id)
        if not tokens:
            continue
        loc = locate_paragraph(body, p)
        repls: list[Replacement] = []
        for tok in tokens:
            if tok.kind != "scalar":
                raise UnpairedMarkerError(
                    f"stray loop marker {tok.raw!r} survived expansion",
                    location=loc, record_id=record_id)
            try:
                value, source = _resolve(
                    record, None, plan.item_prefix, tok.name, plan.contract)
            except TemplateError as exc:
                exc.location = loc
                exc.record_id = record_id
                raise
            repls.append(Replacement(tok.start, tok.end, value))
            field_uses.append(FieldUse(tok.name, "scalar", loc, source))
        apply_replacements(ParagraphIndex(p), repls)

    _assert_no_residual_placeholders(body, record_id)
    return doc, field_uses, loop_rows


def _expand_loop(body: etree._Element, loop: _LoopBlock, record: dict,
                 record_id: str, field_uses: list[FieldUse],
                 contract: Optional[Contract] = None) -> int:
    tables = _tables(body)
    table = tables[loop.table_index]
    rows = _rows(table)
    # Hold the child list while mutating so lxml keeps stable proxy objects.
    table_children = list(table)
    start_child_index = table_children.index(rows[loop.start_row])

    marker_loc = DocLocation(table_index=loop.table_index, row_index=loop.start_row)
    loop_name = contract.loop if (contract is not None and contract.declared) else "items"
    if loop_name not in record:
        raise MissingLoopValueError(
            f"record has no loop value {loop_name!r}",
            location=marker_loc, record_id=record_id, field=loop_name)
    items = record[loop_name]
    if not isinstance(items, list):
        raise MissingLoopValueError(
            f"loop value {loop_name!r} is not a list",
            location=marker_loc, record_id=record_id, field=loop_name)

    # Accept both the documented ``item.<field>`` prefix and the loop name as a
    # prefix; the prefix was detected from the prototype rows at parse time.
    prototypes = rows[loop.first_prototype:loop.end_row]
    prefix = loop.item_prefix

    # Remove marker and prototype rows; clones replace them at the start
    # marker's original child position (which keeps tblPr/tblGrid first).
    for row in rows[loop.start_row:loop.end_row + 1]:
        table.remove(row)

    if not items:
        return 0

    used_ids, used_names, used_docpr = collect_identities(body)
    insertion_child_index = start_child_index
    # Rows before the loop block are unaffected; the first clone lands there.
    logical_row_index = loop.start_row

    for item_no, item in enumerate(items):
        if not isinstance(item, dict):
            raise MissingLoopValueError(
                f"loop item #{item_no} is not an object",
                location=DocLocation(table_index=loop.table_index,
                                     row_index=loop.start_row + item_no),
                record_id=record_id, field=loop_name)
        for prototype in prototypes:
            clone = clone_element(prototype)
            try:
                reidentify_clone(
                    clone, used_ids, used_names, used_docpr,
                    suffix=f"{record_id}_{item_no + 1}")
            except StructureError as exc:
                exc.record_id = record_id
                exc.location = marker_loc
                raise
            table.insert(insertion_child_index, clone)
            row_loc = DocLocation(table_index=loop.table_index,
                                  row_index=logical_row_index)
            for p in clone.iter(W("p")):
                _replace_tokens(p, record, item, prefix, row_loc, record_id,
                                "loop_field", field_uses, contract,
                                path_prefix=f"{loop_name}[{item_no}]")
            insertion_child_index += 1
            logical_row_index += 1
    return len(items) * len(prototypes)


def _detect_item_prefix(prototypes: list[etree._Element],
                        loop: _LoopBlock) -> str:
    """Infer which ``prefix.field`` spelling the prototype rows use."""
    prefixes: set[str] = set()
    for row in prototypes:
        for p in row.iter(W("p")):
            for tok in ParagraphIndex(p).tokens()[0]:
                if "." in tok.name:
                    prefixes.add(tok.name.split(".", 1)[0])
    if not prefixes:
        return loop.item_prefix
    if len(prefixes) > 1:
        raise StructureError(
            f"prototype row mixes item prefixes: {sorted(prefixes)}",
            location=DocLocation(table_index=loop.table_index,
                                 row_index=loop.first_prototype))
    prefix = prefixes.pop()
    if prefix not in ("item", "items"):
        raise StructureError(
            f"unsupported loop field prefix {prefix!r}",
            location=DocLocation(table_index=loop.table_index,
                                 row_index=loop.first_prototype))
    return prefix


def _assert_no_residual_placeholders(body: etree._Element, record_id: str) -> None:
    """A remaining ``{{`` in logical text means rendering did not fully succeed."""
    for p in body.iter(W("p")):
        text = ParagraphIndex(p).text
        tokens, problems = scan_text(text)
        if tokens or problems:
            msg = (problems[0][1] if problems else "unreplaced placeholder")
            raise StructureError(
                f"unreplaced placeholder remains: {msg}",
                location=locate_paragraph(body, p), record_id=record_id)
