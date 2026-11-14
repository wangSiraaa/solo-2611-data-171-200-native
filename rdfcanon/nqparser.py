"""N-Quads 解析器 (RDF 1.1 N-Quads 文法的实用子集)。

自行解析而不用 rdflib 的原因:
  1. rdflib 默认 NORMALIZE_LITERALS=True, 会把 "01"^^xsd:integer 归一成 "1",
     违反词项身份要求 (可用全局开关关闭, 但自解析更直接、无全局状态);
  2. rdflib 会把文件里的空白节点标签替换成内部 id, 丢失映射输出所需的原标签。

支持: IRIREF (含 \\u/\\U 转义)、BLANK_NODE_LABEL、STRING_LITERAL_QUOTE
(含 ECHAR/UCHAR 转义)、LANGTAG、可选 datatype、可选图标签 (IRI 或空白节点)、
默认图。逐行解析; 忽略空行; 行内允许 tab/空格。
"""

from __future__ import annotations

from . import model
from .model import Dataset, Quad


class NQuadsError(ValueError):
    pass


_HEX = "0123456789abcdefABCDEF"
_LABEL_START_EXTRA = "_"
_LABEL_CHARS_EXTRA = "_-."


def _is_label_char(ch: str) -> bool:
    if ch.isalnum() or ch in _LABEL_CHARS_EXTRA:
        return True
    return ord(ch) >= 0x80


class _Cursor:
    def __init__(self, text: str, line_no: int):
        self.text = text
        self.pos = 0
        self.line_no = line_no

    def error(self, msg: str) -> NQuadsError:
        return NQuadsError(f"line {self.line_no}: {msg}: {self.text!r}")

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def next(self) -> str:
        ch = self.peek()
        self.pos += 1
        return ch

    def expect(self, ch: str) -> None:
        if self.peek() != ch:
            raise self.error(f"expected {ch!r}")
        self.pos += 1

    def skip_ws(self) -> None:
        while self.peek() in (" ", "\t"):
            self.pos += 1

    def at_end(self) -> bool:
        return self.pos >= len(self.text)

    def read_uchar(self) -> str:
        """已消费反斜杠, 读取 uXXXX 或 UXXXXXXXX。"""
        kind = self.next()
        if kind == "u":
            n = 4
        elif kind == "U":
            n = 8
        else:
            raise self.error("invalid escape")
        digits = self.text[self.pos : self.pos + n]
        if len(digits) != n or any(c not in _HEX for c in digits):
            raise self.error("invalid unicode escape")
        self.pos += n
        return chr(int(digits, 16))


def _parse_iri(cur: _Cursor) -> str:
    cur.expect("<")
    out = []
    while True:
        ch = cur.next()
        if ch == "":
            raise cur.error("unterminated IRI")
        if ch == ">":
            return "".join(out)
        if ch == "\\":
            out.append(cur.read_uchar())
        else:
            out.append(ch)


def _parse_bnode_label(cur: _Cursor) -> str:
    cur.expect("_")
    cur.expect(":")
    start = cur.pos
    while cur.peek() and _is_label_char(cur.peek()):
        cur.pos += 1
    label = cur.text[start : cur.pos]
    # 标签不能以 '.' 结尾; 尾部的 '.' 属于语句终止符
    while label.endswith("."):
        label = label[:-1]
        cur.pos -= 1
    if not label:
        raise cur.error("empty blank node label")
    return label


_ECHAR = {"t": "\t", "b": "\b", "n": "\n", "r": "\r", "f": "\f",
          '"': '"', "'": "'", "\\": "\\"}


def _parse_literal(cur: _Cursor) -> tuple:
    cur.expect('"')
    out = []
    while True:
        ch = cur.next()
        if ch == "":
            raise cur.error("unterminated literal")
        if ch == '"':
            break
        if ch == "\\":
            esc = cur.peek()
            if esc in _ECHAR:
                out.append(_ECHAR[cur.next()])
            elif esc in ("u", "U"):
                out.append(cur.read_uchar())
            else:
                raise cur.error(f"invalid escape \\{esc}")
        else:
            out.append(ch)
    lexical = "".join(out)
    if cur.peek() == "@":
        cur.pos += 1
        start = cur.pos
        while cur.peek() and (cur.peek().isalnum() or cur.peek() == "-"):
            cur.pos += 1
        lang = cur.text[start : cur.pos]
        if not lang:
            raise cur.error("empty language tag")
        return model.literal(lexical, lang=lang)
    if cur.text[cur.pos : cur.pos + 2] == "^^":
        cur.pos += 2
        datatype = _parse_iri(cur)
        return model.literal(lexical, datatype=datatype)
    return model.literal(lexical)  # plain -> xsd:string


def _parse_term(cur: _Cursor, allow_literal: bool) -> tuple:
    ch = cur.peek()
    if ch == "<":
        return model.iri(_parse_iri(cur))
    if ch == "_":
        return model.bnode(_parse_bnode_label(cur))
    if ch == '"' and allow_literal:
        return _parse_literal(cur)
    raise cur.error(f"unexpected term start {ch!r}")


def parse_statement(line: str, line_no: int) -> Quad:
    cur = _Cursor(line, line_no)
    cur.skip_ws()
    s = _parse_term(cur, allow_literal=False)
    cur.skip_ws()
    p = _parse_term(cur, allow_literal=False)
    if p[0] != model.KIND_IRI:
        raise cur.error("predicate must be an IRI")
    cur.skip_ws()
    o = _parse_term(cur, allow_literal=True)
    cur.skip_ws()
    g = model.DEFAULT_GRAPH
    if cur.peek() != ".":
        g = _parse_term(cur, allow_literal=False)
        cur.skip_ws()
    cur.expect(".")
    cur.skip_ws()
    if not cur.at_end():
        raise cur.error("trailing content after '.'")
    return (s, p, o, g)


def parse_nquads(text: str, source: str = "") -> Dataset:
    ds = Dataset(source=source)
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        quad = parse_statement(raw.strip(), line_no)
        ds.add(quad, line_no, raw.strip())
    return ds


def parse_nquads_file(path: str) -> Dataset:
    with open(path, "r", encoding="utf-8") as fh:
        return parse_nquads(fh.read(), source=path)
