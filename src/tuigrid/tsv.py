"""TSV 解析器（标准库实现，无第三方依赖）。

规则（与题目/schema 约定一致）：
- 记录分隔符为 ``\\n``；记录外的 ``\\r\\n`` 按 ``\\r`` 跟随 ``\\n`` 处理（``\\r`` 被吞掉）。
- 字段分隔符为制表符 ``\\t``。
- 双引号字段内可以原样包含制表符、换行、双引号；字段内双引号写作两个双引号 ``""``。
- 仅当双引号出现在字段起始位置时才是引用字段；字段起始引号之后再出现的裸引号为错误。
- 末尾记录后的单个换行不产生额外空记录；但两个连续换行产生一条空记录。
- 未闭合的引号在输入结束时报告错误，并尽量还原已解析的逻辑矩形。

解析结果为 :class:`ParseResult`，其中每个字段都带有逻辑源行/列（从 1 开始），
错误定位基于“逻辑记录/字段”，引号字段内的换行不会切分记录。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Field:
    """单个 TSV 字段。"""

    value: str
    source_row: int  # 逻辑记录序号（1 起），不是物理行号
    source_col: int  # 逻辑字段序号（1 起）
    raw_quoted: bool = False  # 是否来自双引号字段（用于区分 0 与 "0" 之外的场景留痕）
    start_line: int = 1  # 字段起始的物理行号（1 起），供错误定位展示


@dataclass
class ParseError:
    """解析期错误，定位到逻辑源行/列与物理行。"""

    source_row: int
    source_col: int
    line: int
    reason: str


@dataclass
class ParseResult:
    records: list[list[Field]] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)
    # 输入结束时仍处于引用状态 => 未闭合引号
    unterminated: bool = False


# 解析器状态
_FIELD_START = 0   # 字段起点，可能遇到引号
_UNQUOTED = 1      # 普通字段中
_QUOTED = 2        # 引号字段中
_QUOTE_IN_QUOTED = 3  # 引号字段内刚遇到一个引号（可能是转义或结束）


def parse_tsv(data: str) -> ParseResult:
    """把完整 TSV 文本解析为逻辑记录矩形（可能带错误）。"""

    result = ParseResult()
    records = result.records

    record: list[Field] = []
    buf: list[str] = []
    state = _FIELD_START
    field_index = 0
    record_index = 0
    physical_line = 1
    field_start_line = 1
    raw_quoted = False
    field_errored = False  # 一个字段最多报一次结构错误，避免级联误报

    def end_field() -> None:
        nonlocal buf, field_errored
        record.append(
            Field(
                value="".join(buf),
                source_row=record_index + 1,
                source_col=field_index + 1,
                raw_quoted=raw_quoted,
                start_line=field_start_line,
            )
        )
        buf = []
        field_errored = False

    def end_record() -> None:
        nonlocal record, field_index
        end_field()
        records.append(record)
        record = []
        field_index = 0

    i = 0
    n = len(data)
    while i < n:
        ch = data[i]

        if ch == "\n":
            physical_line += 1
            if state == _QUOTED:
                # 引号字段内的换行是数据
                buf.append("\n")
                i += 1
                continue
            if state == _QUOTE_IN_QUOTED:
                state = _FIELD_START
            # 记录外的 \r\n：前一个字符是 \r 时，\r 已被忽略，这里正常结束记录
            end_record()
            record_index += 1
            state = _FIELD_START
            i += 1
            continue

        if ch == "\r":
            if state == _QUOTED:
                # 引号字段内任何字符都是数据，包括 \r
                buf.append("\r")
                i += 1
                continue
            if i + 1 < n and data[i + 1] == "\n":
                # CRLF：交给下一个 \n 分支统一结束记录，这里吞掉 \r
                i += 1
                continue
            # 裸 \r（后面不是 \n）：按普通数据字符处理，不擅自当作行分隔
            if state == _FIELD_START:
                state = _UNQUOTED
                field_start_line = physical_line
            elif state == _QUOTE_IN_QUOTED:
                if not field_errored:
                    result.errors.append(
                        ParseError(
                            source_row=record_index + 1,
                            source_col=field_index + 1,
                            line=physical_line,
                            reason="闭合双引号后只允许制表符或换行，多余字符须放在引号内",
                        )
                    )
                    field_errored = True
                state = _UNQUOTED
            buf.append("\r")
            i += 1
            continue

        if ch == "\t":
            if state == _QUOTED:
                buf.append("\t")
                i += 1
                continue
            if state == _QUOTE_IN_QUOTED:
                state = _FIELD_START
            end_field()
            field_index += 1
            state = _FIELD_START
            i += 1
            continue

        if ch == '"':
            if state == _FIELD_START:
                state = _QUOTED
                raw_quoted = True
                field_start_line = physical_line
                i += 1
                continue
            if state == _QUOTED:
                state = _QUOTE_IN_QUOTED
                i += 1
                continue
            if state == _QUOTE_IN_QUOTED:
                # "" 转义：字面双引号
                buf.append('"')
                state = _QUOTED
                i += 1
                continue
            # 普通字段中出现引号 => 结构错误
            if not field_errored:
                result.errors.append(
                    ParseError(
                        source_row=record_index + 1,
                        source_col=field_index + 1,
                        line=physical_line,
                        reason="裸双引号不能出现在非引用字段中；含特殊字符的字段需整体加双引号",
                    )
                )
                field_errored = True
            buf.append('"')
            i += 1
            continue

        # 普通字符
        if state == _FIELD_START:
            state = _UNQUOTED
            field_start_line = physical_line
        elif state == _QUOTE_IN_QUOTED:
            # 闭合引号之后又出现普通字符（而非分隔符）=> 结构错误
            if not field_errored:
                result.errors.append(
                    ParseError(
                        source_row=record_index + 1,
                        source_col=field_index + 1,
                        line=physical_line,
                        reason="闭合双引号后只允许制表符或换行，多余字符须放在引号内",
                    )
                )
                field_errored = True
            state = _UNQUOTED
        buf.append(ch)
        i += 1

    # 输入结束
    if state == _QUOTED:
        result.unterminated = True
        result.errors.append(
            ParseError(
                source_row=record_index + 1,
                source_col=field_index + 1,
                line=physical_line,
                reason="双引号字段未闭合（缺少结束双引号）",
            )
        )
        end_field()
        records.append(record)
    elif state in (_UNQUOTED, _FIELD_START, _QUOTE_IN_QUOTED):
        # 有未提交内容，或最后一个字段为空（如输入以 \t 结尾）时都要落盘；
        # 完全空输入不产生任何记录（末尾换行已在循环内结束记录）。
        if record or buf or field_index > 0:
            if state == _FIELD_START and not buf and not record:
                # 理论上不会走到：空输入在上方 n==0 时直接跳过
                pass
            end_field()
            records.append(record)

    return result
