"""源文件装载与半开区间 source span。

行列均从 1 开始；列按 UTF-8 解码后的 Unicode 码点计，span 为半开区间
``[start_offset, end_offset)``，与 fixtures/plan.json 的约定一致。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    """一段源码文本的位置。"""

    file: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int
    start_offset: int
    end_offset: int

    def __str__(self) -> str:  # pragma: no cover - 纯展示
        if self.start_line == self.end_line:
            return f"{self.file}:{self.start_line}:{self.start_col}"
        return f"{self.file}:{self.start_line}:{self.start_col}-{self.end_line}:{self.end_col}"


@dataclass
class SourceText:
    path: str
    text: str

    def line_of(self, offset: int) -> tuple[int, int]:
        """把绝对码点偏移转成 (1-based 行, 1-based 列)。"""
        line = 1
        col = 1
        for i, ch in enumerate(self.text):
            if i == offset:
                return line, col
            if ch == "\n":
                line += 1
                col = 1
            else:
                col += 1
        return line, col

    def span(self, start: int, end: int) -> Span:
        sl, sc = self.line_of(start)
        el, ec = self.line_of(end)
        return Span(self.path, sl, sc, el, ec, start, end)

    def snippet(self, span: Span) -> str:
        """取 span 所在逻辑行，并在下方给出列对齐的 ^^^ 指示。"""
        line_start = self.text.rfind("\n", 0, span.start_offset) + 1
        line_end = self.text.find("\n", span.end_offset)
        if line_end == -1:
            line_end = len(self.text)
        line = self.text[line_start:line_end]
        marker_start = span.start_col - 1
        if span.end_line == span.start_line:
            width = max(1, span.end_col - span.start_col)
        else:
            width = max(1, len(line) - marker_start)
        marker = " " * marker_start + "^" * width
        return f"{line}\n{marker}"


class SourceStore:
    """按路径持有全部源文本，供 span 回溯原片段。"""

    def __init__(self) -> None:
        self._texts: dict[str, SourceText] = {}

    def add(self, path: str, text: str) -> SourceText:
        text = text.decode("utf-8") if isinstance(text, bytes) else text
        source = SourceText(path, text)
        self._texts[path] = source
        return source

    def load(self, path: str) -> SourceText:
        with open(path, "r", encoding="utf-8") as fh:
            return self.add(path, fh.read())

    def get(self, path: str) -> SourceText:
        return self._texts[path]

    def snippet(self, span: Span) -> str:
        return self._texts[span.file].snippet(span)
