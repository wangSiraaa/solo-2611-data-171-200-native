"""Low-level OOXML helpers.

The important abstraction here is :class:`ParagraphIndex`.  Word splits
placeholder text arbitrarily across ``<w:r>`` / ``<w:t>`` nodes, so we build the
*logical* text of a paragraph together with a map from every logical character
range back to the concrete OOXML text node it came from.  Replacement edits
those nodes in place, which keeps every untouched run, its ``w:rPr`` and all
non-text children (bookmarks, drawings, tabs, ...) intact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator, Optional

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
XML_NS = "http://www.w3.org/XML/1998/namespace"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

W = lambda tag: f"{{{W_NS}}}{tag}"  # noqa: E731
WP = lambda tag: f"{{{WP_NS}}}{tag}"  # noqa: E731


# Elements whose inner text must never be treated as part of the surrounding
# paragraph's logical text (matching across them is unsupported by design).
BARRIER_TAGS = {
    W("txbxContent"),
    W("del"),
    W("moveFrom"),
    W("fldSimple"),
    W("instrText"),
    W("delText"),
    W("drawing"),
    W("pict"),
    W("object"),
    W("sdt"),
    W("footnoteReference"),
    W("endnoteReference"),
    W("commentReference"),
    W("p"),
}

# Structural children that carry no inserted text; skip without recording a
# barrier (a property block must not look like a placeholder boundary).
SKIP_TAGS = {W("rPr"), W("pPr"), W("tblPr"), W("tcPr"), W("trPr")}

IDENT_SIMPLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
IDENT_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
PLACEHOLDER_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
MARKER_RE = re.compile(r"^\{\{\s*([#/])\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")


@dataclass(frozen=True)
class Token:
    start: int          # logical-text offset
    end: int            # exclusive
    raw: str
    name: str
    kind: str           # "scalar" | "start" | "end"


@dataclass
class Chunk:
    kind: str           # "t" | "tab" | "br" | "cr" | "hyphen"
    node: etree._Element
    run: Optional[etree._Element]
    start: int
    end: int

    @property
    def text(self) -> str:
        if self.kind == "t":
            return self.node.text or ""
        if self.kind == "tab":
            return "\t"
        if self.kind == "br":
            return "\n"
        if self.kind == "cr":
            return "\n"
        if self.kind == "noBreakHyphen":
            return "‑"
        if self.kind == "softHyphen":
            return "­"
        return ""


def nearest_run(node: etree._Element) -> Optional[etree._Element]:
    parent = node.getparent()
    while parent is not None:
        if parent.tag == W("r"):
            return parent
        if parent.tag == W("p"):
            return None
        parent = parent.getparent()
    return None


class ParagraphIndex:
    """Logical text of one ``w:p`` plus node mapping for every character."""

    def __init__(self, paragraph: etree._Element):
        self.paragraph = paragraph
        self.chunks: list[Chunk] = []
        self.barriers: list[tuple[int, str]] = []
        self._buf: list[str] = []
        self._len = 0
        self._walk(paragraph)
        self.text = "".join(self._buf)

    # -- indexing ---------------------------------------------------------
    def _append(self, kind: str, node: etree._Element, text: str,
                run: Optional[etree._Element]) -> None:
        if not text:
            return
        start = self._len
        self._buf.append(text)
        self._len += len(text)
        self.chunks.append(Chunk(kind, node, run if run is not None else nearest_run(node),
                                 start, self._len))

    def _walk(self, el: etree._Element, run: Optional[etree._Element] = None) -> None:
        for child in el:
            tag = child.tag
            if not isinstance(tag, str):
                continue
            if tag in SKIP_TAGS:
                continue
            if tag in BARRIER_TAGS:
                self.barriers.append((self._len, tag))
                continue
            if tag == W("r"):
                self._walk(child, child)
                continue
            if tag == W("t"):
                self._append("t", child, child.text or "", run)
            elif tag == W("tab"):
                self._append("tab", child, "\t", run)
            elif tag == W("cr"):
                self._append("cr", child, "\n", run)
            elif tag == W("br"):
                # Page/column breaks are not logical line breaks.
                if child.get(W("type")) in (None, "textWrapping"):
                    self._append("br", child, "\n", run)
            elif tag == W("noBreakHyphen"):
                self._append("noBreakHyphen", child, "‑", run)
            elif tag == W("softHyphen"):
                self._append("softHyphen", child, "­", run)
            else:
                # Runs may sit inside smartTag/hyperlink/ins-like wrappers.
                self._walk(child, run)

    # -- lookup -----------------------------------------------------------
    def chunks_for(self, start: int, end: int) -> list[tuple[Chunk, int, int]]:
        """Return ``(chunk, local_start, local_end)`` overlapping ``[start,end)``."""
        out = []
        for chunk in self.chunks:
            a = max(start, chunk.start)
            b = min(end, chunk.end)
            if a < b:
                out.append((chunk, a - chunk.start, b - chunk.start))
        return out

    def first_chunk(self, start: int, end: int) -> Optional[Chunk]:
        hits = self.chunks_for(start, end)
        return hits[0][0] if hits else None

    def tokens(self) -> tuple[list[Token], list[tuple[int, str]]]:
        """Split logical text into placeholders and report malformed leftovers.

        Tokens whose character range crosses a non-text child (drawing, nested
        content control, ...) or a hyperlink boundary are *not* returned; they
        are reported as unsupported structure instead.
        """
        tokens, problems = scan_text(self.text)
        kept: list[Token] = []
        barrier_offsets = self.barriers
        for tok in tokens:
            crossing = [tag for off, tag in barrier_offsets if tok.start < off < tok.end]
            if crossing:
                problems.append((tok.start, "placeholder crosses a "
                                 f"{_tag_name(crossing[0])} boundary"))
                continue
            if self._crosses_hyperlink(tok):
                problems.append((tok.start, "placeholder crosses a hyperlink boundary"))
                continue
            kept.append(tok)
        # Text inside barred structures (content controls, text boxes, drawings)
        # is not part of the logical paragraph text, so a placeholder hiding
        # there would otherwise be silently missed.
        for node in self._barrier_nodes():
            inner = "".join(t.text or "" for t in node.iter(W("t")))
            if "{{" in inner or "}}" in inner:
                problems.append((0, "placeholder inside unsupported "
                                    f"{_tag_name(node.tag)} content"))
                break
        return kept, problems

    def _barrier_nodes(self) -> list[etree._Element]:
        out = []
        for node in self.paragraph.iter():
            if node is self.paragraph:
                continue
            if node.tag in BARRIER_TAGS:
                out.append(node)
        return out

    def _crosses_hyperlink(self, tok: Token) -> bool:
        owners = set()
        for chunk, _, _ in self.chunks_for(tok.start, tok.end):
            owners.add(_hyperlink_path(chunk.node))
        return len(owners) > 1


def _hyperlink_path(node: etree._Element) -> tuple[int, ...] | None:
    """Child-index path to the enclosing hyperlink, relative to the paragraph."""
    path: list[int] = []
    current = node
    while current is not None and current.tag != W("p"):
        parent = current.getparent()
        if parent is None:
            return None
        if current.tag == W("hyperlink"):
            path.append(list(parent).index(current))
            return tuple(reversed(path))
        path.append(list(parent).index(current))
        current = parent
    return None


def _tag_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def scan_text(text: str) -> tuple[list[Token], list[tuple[int, str]]]:
    tokens: list[Token] = []
    problems: list[tuple[int, str]] = []
    pos = 0
    for m in PLACEHOLDER_RE.finditer(text):
        gap = text[pos:m.start()]
        if "{{" in gap or "}}" in gap:
            problems.append((pos + gap.find("{{") if "{{" in gap else pos + gap.find("}}"),
                             "stray brace sequence outside a placeholder"))
        inner = m.group(1).strip()
        if not inner:
            problems.append((m.start(), "empty placeholder"))
        elif inner[0] in "#/":
            name = inner[1:].strip()
            if not IDENT_SIMPLE.match(name):
                problems.append((m.start(), f"unsupported loop marker {m.group(0)!r}"))
            else:
                tokens.append(Token(m.start(), m.end(), m.group(0), name,
                                    "start" if inner[0] == "#" else "end"))
        elif IDENT_DOTTED.match(inner):
            tokens.append(Token(m.start(), m.end(), m.group(0), inner, "scalar"))
        else:
            problems.append((m.start(), f"unsupported expression {m.group(0)!r}"))
        pos = m.end()
    tail = text[pos:]
    if "{{" in tail or "}}" in tail:
        at = pos + (tail.find("{{") if "{{" in tail else tail.find("}}"))
        problems.append((at, "unclosed or stray brace sequence"))
    return tokens, problems


@dataclass
class Replacement:
    start: int
    end: int
    value: str


def _split_line_breaks(text: str) -> list[Optional[str]]:
    """``None`` markers represent a Word line break between text parts."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    parts: list[Optional[str]] = []
    for i, part in enumerate(text.split("\n")):
        if i:
            parts.append(None)
        parts.append(part)
    return parts


def set_text_node(node: etree._Element, text: str) -> None:
    """Set a ``w:t`` value, translating ``\\n`` into ``<w:br/>`` siblings."""
    run = node.getparent()
    if run is None:
        node.text = text
        return
    idx = run.index(node)
    run.remove(node)
    new_nodes: list[etree._Element] = []
    for part in _split_line_breaks(text):
        if part is None:
            new_nodes.append(etree.Element(W("br")))
        else:
            t = etree.Element(W("t"))
            t.set(f"{{{XML_NS}}}space", "preserve")
            t.text = part
            new_nodes.append(t)
    for offset, new in enumerate(new_nodes):
        run.insert(idx + offset, new)


def apply_replacements(index: ParagraphIndex, replacements: list[Replacement]) -> None:
    """Apply non-overlapping replacements, editing as few nodes as possible."""
    replacements = sorted(replacements, key=lambda r: r.start)
    for prev, nxt in zip(replacements, replacements[1:]):
        if prev.end > nxt.start:
            raise ValueError("overlapping replacements")

    # Edits are grouped per node; each text node is rewritten exactly once.
    # Non-text chunks (tab/br/hyphen) that fall entirely inside a placeholder
    # are removed; a partial overlap is a structural impossibility we reject.
    per_node: dict[int, list[tuple[int, int, str]]] = {}
    nodes: dict[int, etree._Element] = {}
    removals: dict[int, etree._Element] = {}
    for repl in replacements:
        start_chunk = index.first_chunk(repl.start, repl.end)
        if start_chunk is None:
            raise ValueError("replacement outside paragraph text")
        for hit, local_start, local_end in index.chunks_for(repl.start, repl.end):
            if hit.kind != "t":
                if local_start != 0 or local_end != len(hit.text):
                    raise ValueError("replacement partially covers a non-text node")
                removals[id(hit.node)] = hit.node
                continue
            key = id(hit.node)
            nodes[key] = hit.node
            insert_here = repl.value if hit is start_chunk else ""
            per_node.setdefault(key, []).append((local_start, local_end, insert_here))

    for key, edits in per_node.items():
        node = nodes[key]
        current = node.text or ""
        for local_start, local_end, insert_here in sorted(edits, reverse=True):
            current = current[:local_start] + insert_here + current[local_end:]
        set_text_node(node, current)

    for node in removals.values():
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)


def iter_paragraphs(root: etree._Element) -> Iterator[etree._Element]:
    for p in root.iter(W("p")):
        yield p


def text_of_paragraph(p: etree._Element) -> str:
    return ParagraphIndex(p).text
