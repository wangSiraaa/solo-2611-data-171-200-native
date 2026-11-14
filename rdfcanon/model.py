"""词项身份模型与规范词项序列化。

词项身份(按 README / plan.json):
  * IRI     : 原始字符串, 不做任何归一。
  * literal : (解码后词法值, datatype, 小写语言标签)。
              plain literal 的 datatype 视为 xsd:string;
              带语言标签的 literal datatype 记为 None。
              不做数值归一 ("01"^^xsd:integer 与 "1"^^xsd:integer 不同)。
  * blank node : 标签仅在本文件内有意义, 跨文件不可比较。
  * 默认图  : 与任何命名图都不同。

内部表示全部为可哈希 tuple:
  IRI      -> ("i", iri_string)
  literal  -> ("l", lexical, datatype_or_None, lang_lower_or_None)
  blank    -> ("b", label)
  默认图    -> ("d",)
四元组 Quad = (s, p, o, g), g 为默认图时是 DEFAULT_GRAPH。
"""

from __future__ import annotations

XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"

KIND_IRI = "i"
KIND_LIT = "l"
KIND_BNODE = "b"
KIND_DEFAULT = "d"

DEFAULT_GRAPH = (KIND_DEFAULT,)

Quad = tuple  # (s, p, o, g), 每个元素是上面的词项 tuple


def iri(value: str) -> tuple:
    return (KIND_IRI, value)


def literal(lexical: str, datatype: str | None = None, lang: str | None = None) -> tuple:
    if lang is not None:
        return (KIND_LIT, lexical, None, lang.lower())
    return (KIND_LIT, lexical, datatype or XSD_STRING, None)


def bnode(label: str) -> tuple:
    return (KIND_BNODE, label)


def is_bnode(term: tuple) -> bool:
    return term[0] == KIND_BNODE


class Dataset:
    """一份 N-Quads 解析结果: 四元组集合 + 逐语句来源。"""

    def __init__(self, source: str = ""):
        self.source = source
        self.quads: set[Quad] = set()
        # statements[i] = (line_no, raw_text, quad); 重复语句也各自记录
        self.statements: list[tuple[int, str, Quad]] = []
        self.duplicate_count = 0

    def add(self, quad: Quad, line_no: int, raw: str) -> None:
        if quad in self.quads:
            self.duplicate_count += 1
        else:
            self.quads.add(quad)
        self.statements.append((line_no, raw, quad))

    @property
    def bnode_labels(self) -> list[str]:
        labels = set()
        for quad in self.quads:
            for term in quad:
                if is_bnode(term):
                    labels.add(term[1])
        return sorted(labels)

    def __len__(self) -> int:
        return len(self.quads)


# ---------------------------------------------------------------------------
# 规范词项序列化 (公开格式, 见 REPORT.md)
# ---------------------------------------------------------------------------

def escape_iri(value: str) -> str:
    """IRI 转义: 控制字符、DEL、空格及 "<>\\^`{|} 用 \\uXXXX / \\UXXXXXXXX。"""
    out = []
    for ch in value:
        o = ord(ch)
        if o < 0x20 or o == 0x7F or ch in '<>"{}|^` ' or ch == "\\":
            if o <= 0xFFFF:
                out.append("\\u%04X" % o)
            else:
                out.append("\\U%08X" % o)
        else:
            out.append(ch)
    return "".join(out)


def escape_literal(value: str) -> str:
    """词法值转义: \\ " 用反斜杠; \\t \\n \\r 用短形式; 其余控制字符用 \\uXXXX。"""
    out = []
    for ch in value:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        else:
            o = ord(ch)
            if o < 0x20 or o == 0x7F:
                out.append("\\u%04X" % o)
            elif o > 0xFFFF:
                # 非 BMP 字符原样输出 (UTF-8 编码后仍按码点有序)
                out.append(ch)
            else:
                out.append(ch)
    return "".join(out)


def ground_term_string(term: tuple) -> str:
    """非空白词项(含默认图标记)的规范字符串, 也用作精化时的结构键。"""
    kind = term[0]
    if kind == KIND_IRI:
        return "<" + escape_iri(term[1]) + ">"
    if kind == KIND_LIT:
        lex, datatype, lang = term[1], term[2], term[3]
        text = '"' + escape_literal(lex) + '"'
        if lang is not None:
            return text + "@" + lang
        return text + "^^<" + escape_iri(datatype) + ">"
    if kind == KIND_DEFAULT:
        return ""  # 默认图在四元组行里不输出
    raise ValueError(f"not a ground term: {term!r}")


def term_string(term: tuple, bnode_name) -> str:
    """完整词项的规范字符串; bnode_name(label) -> 规范空白标签字符串。"""
    if term[0] == KIND_BNODE:
        return "_:" + bnode_name(term[1])
    return ground_term_string(term)


def quad_line(quad: Quad, bnode_name) -> str:
    """单条四元组的规范行 (不含换行)。默认图不输出第四项。"""
    s, p, o, g = quad
    parts = [term_string(s, bnode_name), term_string(p, bnode_name), term_string(o, bnode_name)]
    if g != DEFAULT_GRAPH:
        parts.append(term_string(g, bnode_name))
    return " ".join(parts) + " ."


def dataset_to_nquads(ds: "Dataset", bnode_name=None) -> str:
    """把数据集写回 N-Quads 文本 (用于生成重命名/换序变体)。"""
    name = bnode_name or (lambda label: label)
    return "".join(quad_line(q, name) + "\n" for q in ds.quads)
