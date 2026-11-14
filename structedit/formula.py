"""公式词法分析、校验与引用重写。

只重写真实的 A1 引用 token（单格 / 矩形范围，可带工作表限定与 $ 标记），
其余 token（字符串常量、数字、函数名、名称、运算符、空白）逐字保留。

明确拒绝（定位并抛出 UnsupportedConstruct，从而拒绝本次编辑）：
- 外部工作簿引用：[1]Sheet!A1 或 '[Book.xlsx]Sheet'!A1
- 结构化表引用：Table1[Col]
- 3D 引用：Sheet1:Sheet3!A1（含范围第二段再带工作表限定）
- 动态地址函数：INDIRECT(...) / OFFSET(...)
- 整行/整列引用（A:A、1:1 等非 A1 单格/矩形形式）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from openpyxl.utils import column_index_from_string

from .refs import (
    MAX_COL,
    MAX_ROW,
    CellRef,
    Operation,
    axis_value,
    interval_mapper,
    point_mapper,
    with_axis,
)


class FormulaError(Exception):
    """公式无法解析。"""


class UnsupportedConstruct(Exception):
    """出现题目范围外的构造，须拒绝本次编辑。"""


# ---------------------------------------------------------------- 词法 token

@dataclass
class Token:
    kind: str          # string|sheet|cell|name|func|error|op
    start: int
    end: int
    text: str
    sheet_name: str | None = None      # kind == sheet 时解析出的工作表名


_CELL_RE = re.compile(r"(\$?)([A-Za-z]{1,3})(\$?)([0-9]{1,7})")
_SHEET_RE = re.compile(r"[\w.]+!")                       # 未加引号的工作表名（含 CJK）
_NAME_RE = re.compile(r"[^\W\d_\\][\w.\\]*|[_\\][\w.\\]*")
_ERROR_RE = re.compile(r"#(?:REF!|DIV/0!|N/A|NAME\?|NULL!|NUM!|VALUE!)", re.IGNORECASE)
_DYNAMIC_FUNCS = {"INDIRECT", "OFFSET"}
_CELL_BOUNDARY_STOP = set("_.$")


def _scan_string(text: str, i: int) -> int:
    """从 text[i]=='\"' 起扫描字符串常量，返回结束位置（不含）。"""
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == '"':
            if j + 1 < n and text[j + 1] == '"':
                j += 2
                continue
            return j + 1
        j += 1
    raise FormulaError(f"位置 {i}: 字符串常量未闭合")


def _scan_quoted_sheet(text: str, i: int) -> tuple[int, str]:
    """从 text[i]==\"'\" 起扫描带引号工作表名（'' 为转义），返回 (结束位置, 名称)。"""
    j = i + 1
    n = len(text)
    buf: list[str] = []
    while j < n:
        ch = text[j]
        if ch == "'":
            if j + 1 < n and text[j + 1] == "'":
                buf.append("'")
                j += 2
                continue
            return j + 1, "".join(buf)
        buf.append(ch)
        j += 1
    raise FormulaError(f"位置 {i}: 带引号工作表名未闭合")


def _cell_boundary_ok(text: str, end: int) -> bool:
    """cell 匹配后一个字符不能使 token 继续（函数名、更长的标识符等）。"""
    if end >= len(text):
        return True
    nxt = text[end]
    if nxt == "(" or nxt in _CELL_BOUNDARY_STOP:
        return False
    if nxt.isalnum() or ord(nxt) > 127:
        return False
    return True


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = _scan_string(text, i)
            tokens.append(Token("string", i, j, text[i:j]))
            i = j
            continue
        if ch == "'":
            j, name = _scan_quoted_sheet(text, i)
            if j < n and text[j] == "!":
                if "[" in name or "]" in name:
                    raise UnsupportedConstruct(f"外部工作簿引用: {text[i:j + 1]!r}")
                if ":" in name:
                    raise UnsupportedConstruct(f"3D 引用: {text[i:j + 1]!r}")
                tokens.append(Token("sheet", i, j + 1, text[i:j + 1], sheet_name=name))
                i = j + 1
                continue
            raise FormulaError(f"位置 {i}: 引号文本后缺少 '!'")
        if ch == "[":
            raise UnsupportedConstruct(f"外部工作簿或结构化表引用（'['，位置 {i}）")
        if ch == "#":
            m = _ERROR_RE.match(text, i)
            if not m:
                raise FormulaError(f"位置 {i}: 无法识别的错误字面值")
            tokens.append(Token("error", i, m.end(), m.group()))
            i = m.end()
            continue
        if ch.isalpha() or ch in "_\\$" or ord(ch) > 127:
            if ch != "$":
                m = _SHEET_RE.match(text, i)
                if m:
                    tokens.append(
                        Token("sheet", i, m.end(), m.group(),
                              sheet_name=m.group()[:-1])
                    )
                    i = m.end()
                    continue
            m = _CELL_RE.match(text, i)
            if m:
                col = column_index_from_string(m.group(2).upper())
                row = int(m.group(4))
                if (
                    col <= MAX_COL
                    and 1 <= row <= MAX_ROW
                    and _cell_boundary_ok(text, m.end())
                ):
                    tokens.append(Token("cell", i, m.end(), m.group()))
                    i = m.end()
                    continue
            m = _NAME_RE.match(text, i)
            if m:
                end = m.end()
                nxt = text[end] if end < n else ""
                if nxt == "(":
                    fname = m.group().upper()
                    if fname in _DYNAMIC_FUNCS:
                        raise UnsupportedConstruct(f"动态地址函数 {fname}(...)")
                    tokens.append(Token("func", i, end, m.group()))
                else:
                    tokens.append(Token("name", i, end, m.group()))
                i = m.end()
                continue
            tokens.append(Token("op", i, i + 1, ch))
            i += 1
            continue
        tokens.append(Token("op", i, i + 1, ch))
        i += 1
    return tokens


# ---------------------------------------------------------------- 引用组装

@dataclass
class RefToken:
    """一个引用 token：单格、矩形范围，或已失效的 #REF!（可带工作表限定）。"""

    start: int
    end: int
    text: str
    sheet_raw: str | None            # 原文中的工作表前缀（含引号与 '!'），原样保留
    sheet_name: str | None           # 解析出的工作表名；None 表示未限定
    kind: str                        # cell | range | ref_error
    first: CellRef | None = None
    second: CellRef | None = None

    @property
    def is_ref_error(self) -> bool:
        return self.kind == "ref_error"


def _parse_cell(text: str) -> CellRef:
    m = _CELL_RE.fullmatch(text)
    assert m, text
    return CellRef(
        col=column_index_from_string(m.group(2).upper()),
        row=int(m.group(4)),
        col_abs=bool(m.group(1)),
        row_abs=bool(m.group(3)),
    )


def _blank_strings(text: str) -> str:
    """把字符串常量替换为等长空白，供 3D 引用预扫描使用。"""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        if text[i] == '"':
            j = _scan_string(text, i)
            for k in range(i, j):
                out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


_THREED_RE = re.compile(r"(?:'(?:[^']|'')+'|[\w.]+)\s*:\s*(?:'(?:[^']|'')+'|[\w.]+)!")


def check_unsupported(text: str) -> None:
    """在重写前对整段公式做范围外构造检查。"""
    m = _THREED_RE.search(_blank_strings(text))
    if m:
        raise UnsupportedConstruct(f"3D 引用: {m.group()!r}")


def build_refs(tokens: list[Token]) -> list[RefToken]:
    """把 token 流组装成引用 token 列表（含各 token 在原文中的位置）。"""
    refs: list[RefToken] = []
    i = 0
    while i < len(tokens):
        sheet_tok: Token | None = None
        if tokens[i].kind == "sheet":
            sheet_tok = tokens[i]
            i += 1
            if i >= len(tokens):
                raise FormulaError("工作表限定后缺少引用")
        tok = tokens[i]
        if tok.kind == "error":
            if tok.text.upper() != "#REF!":
                raise FormulaError(f"工作表限定后出现非引用错误: {tok.text}")
            refs.append(
                RefToken(
                    start=sheet_tok.start if sheet_tok else tok.start,
                    end=tok.end,
                    text=(sheet_tok.text if sheet_tok else "") + tok.text,
                    sheet_raw=sheet_tok.text if sheet_tok else None,
                    sheet_name=sheet_tok.sheet_name if sheet_tok else None,
                    kind="ref_error",
                )
            )
            i += 1
            continue
        if tok.kind != "cell":
            if sheet_tok is not None:
                raise UnsupportedConstruct(
                    f"工作表限定后不是 A1 引用: {sheet_tok.text}{tok.text}"
                )
            i += 1
            continue
        first = _parse_cell(tok.text)
        second: CellRef | None = None
        kind = "cell"
        text_end = tok.end
        tail = ""
        if i + 1 < len(tokens) and tokens[i + 1].kind == "op" and tokens[i + 1].text == ":":
            j = i + 2
            if j < len(tokens) and tokens[j].kind == "sheet":
                raise UnsupportedConstruct("3D 引用（范围第二段带工作表限定）")
            if j >= len(tokens) or tokens[j].kind != "cell":
                raise UnsupportedConstruct("整行/整列等非 A1 矩形引用")
            second = _parse_cell(tokens[j].text)
            kind = "range"
            text_end = tokens[j].end
            tail = ":" + tokens[j].text
            i = j
        start = sheet_tok.start if sheet_tok else tok.start
        refs.append(
            RefToken(
                start=start,
                end=text_end,
                text=(sheet_tok.text if sheet_tok else "") + tok.text + tail,
                sheet_raw=sheet_tok.text if sheet_tok else None,
                sheet_name=sheet_tok.sheet_name if sheet_tok else None,
                kind=kind,
                first=first,
                second=second,
            )
        )
        i += 1
    return refs


# ---------------------------------------------------------------- 引用重写

@dataclass
class RefChange:
    ref_before: str
    ref_after: str
    reason: str


@dataclass
class RewriteResult:
    text: str
    changes: list[RefChange] = field(default_factory=list)


def _transform_ref(ref: RefToken, op: Operation) -> tuple[str, str]:
    """对命中目标工作表的引用应用一次结构操作，返回 (新文本, 原因)。"""
    axis_cn = "行" if op.axis == "row" else "列"
    prefix = ref.sheet_raw or ""
    pmap = point_mapper(op)
    imap = interval_mapper(op)

    if ref.kind == "cell":
        c = ref.first
        assert c is not None
        pos = axis_value(c, op.axis)
        new_pos = pmap(pos)
        if new_pos is None:
            return prefix + "#REF!", (
                f"{axis_cn}{pos} 位于删除区间[{op.pos},{op.delete_end}]内，"
                f"引用单元被删除 → #REF!"
            )
        if new_pos == pos:
            return ref.text, f"{axis_cn}{pos} 不受本次操作影响"
        moved = with_axis(c, op.axis, new_pos)
        if op.is_insert:
            reason = f"{axis_cn}{pos} ≥ 插入位置{op.pos} → 移至{axis_cn}{new_pos}"
        else:
            reason = f"{axis_cn}{pos} 在删除区间之后 → 移至{axis_cn}{new_pos}"
        return prefix + moved.render(), reason

    # range：轴向上的两个角点分别映射到新区间的两端
    a, b = ref.first, ref.second
    assert a is not None and b is not None
    va, vb = axis_value(a, op.axis), axis_value(b, op.axis)
    lo, hi = min(va, vb), max(va, vb)
    res = imap(lo, hi)
    if res is None:
        return prefix + "#REF!", f"引用{axis_cn}区间[{lo},{hi}]全部被删除 → #REF!"
    nlo, nhi = res
    if (nlo, nhi) == (lo, hi):
        return ref.text, f"引用{axis_cn}区间[{lo},{hi}]不受本次操作影响"
    if va <= vb:
        na, nb = with_axis(a, op.axis, nlo), with_axis(b, op.axis, nhi)
    else:
        na, nb = with_axis(a, op.axis, nhi), with_axis(b, op.axis, nlo)
    collapsed = (na.col, na.row, na.col_abs, na.row_abs) == (
        nb.col, nb.row, nb.col_abs, nb.row_abs
    )
    if op.is_insert:
        if nlo != lo:
            reason = (
                f"插入位置{op.pos} ≤ 区间起点{lo} → "
                f"引用{axis_cn}区间整体移位为[{nlo},{nhi}]"
            )
        else:
            reason = (
                f"插入位置{op.pos} 落在引用{axis_cn}区间({lo},{hi}]内 → "
                f"区间扩展为[{nlo},{nhi}]"
            )
    else:
        if op.delete_end < lo:
            reason = (
                f"删除区间[{op.pos},{op.delete_end}]在引用{axis_cn}区间[{lo},{hi}]之前 → "
                f"整体前移为[{nlo},{nhi}]"
            )
        else:
            reason = (
                f"删除区间[{op.pos},{op.delete_end}]与引用{axis_cn}区间[{lo},{hi}]相交 → "
                f"收缩为幸存格外接区间[{nlo},{nhi}]"
            )
    if collapsed:
        reason += "，区间收缩为单格"
        return prefix + na.render(), reason
    return prefix + na.render() + ":" + nb.render(), reason


def rewrite_text(
    body: str,
    context_sheet: str | None,
    op: Operation,
    record_all: bool = False,
) -> RewriteResult:
    """重写一段公式/名称定义文本中受 op 影响的引用。

    context_sheet: 未限定引用绑定的工作表（公式所在表；名称为其作用域表）。
    record_all: 为 True 时把未受影响的引用也记入 changes（供报告说明）。
    """
    check_unsupported(body)
    tokens = tokenize(body)
    refs = build_refs(tokens)
    repls: list[tuple[int, int, str]] = []
    changes: list[RefChange] = []
    for ref in refs:
        if ref.is_ref_error:
            continue
        target = ref.sheet_name if ref.sheet_name is not None else context_sheet
        if target is None or target.casefold() != op.sheet.casefold():
            if record_all:
                if ref.sheet_name is not None:
                    why = f"引用目标工作表 {ref.sheet_name}，非本次操作表 {op.sheet}"
                else:
                    why = f"未限定引用绑定 {context_sheet}，非本次操作表 {op.sheet}"
                changes.append(RefChange(ref.text, ref.text, why))
            continue
        new_text, reason = _transform_ref(ref, op)
        if new_text != ref.text:
            repls.append((ref.start, ref.end, new_text))
        changes.append(RefChange(ref.text, new_text, reason))
    if not repls:
        return RewriteResult(body, changes)
    out: list[str] = []
    last = 0
    for start, end, new_text in repls:
        out.append(body[last:start])
        out.append(new_text)
        last = end
    out.append(body[last:])
    return RewriteResult("".join(out), changes)


def rewrite_formula(formula: str, context_sheet: str | None, op: Operation) -> RewriteResult:
    """formula 含前导 '='。"""
    assert formula.startswith("=")
    res = rewrite_text(formula[1:], context_sheet, op)
    return RewriteResult("=" + res.text, res.changes)
