"""带位置的词法分析。

输出的每个 Token 都携带半开 source span。词法错误不会抛出，而是以
Diagnostic 收集并跳过坏字符，使一条文件里的多处问题可同时诊断。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from .sources import SourceText, Span


class TokenType(Enum):
    NUMBER = auto()
    IDENT = auto()
    PLUS = auto()
    MINUS = auto()
    STAR = auto()
    SLASH = auto()
    CARET = auto()
    LPAREN = auto()
    RPAREN = auto()
    COMMA = auto()
    EQUAL = auto()
    COLON = auto()
    NEWLINE = auto()
    EOF = auto()


_PUNCT = {
    "+": TokenType.PLUS,
    "-": TokenType.MINUS,
    "*": TokenType.STAR,
    "/": TokenType.SLASH,
    "^": TokenType.CARET,
    "(": TokenType.LPAREN,
    ")": TokenType.RPAREN,
    ",": TokenType.COMMA,
    "=": TokenType.EQUAL,
    ":": TokenType.COLON,
}


@dataclass(frozen=True)
class Token:
    type: TokenType
    text: str
    span: Span


class LexError(Exception):
    def __init__(self, message: str, span: Span) -> None:
        super().__init__(message)
        self.message = message
        self.span = span


def tokenize(source: SourceText) -> tuple[list[Token], list[LexError]]:
    tokens: list[Token] = []
    errors: list[LexError] = []
    text = source.text
    n = len(text)
    i = 0

    def emit(kind: TokenType, start: int, end: int) -> None:
        tokens.append(Token(kind, text[start:end], source.span(start, end)))

    while i < n:
        ch = text[i]
        if ch in " \t\r":
            i += 1
            continue
        if ch == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "\n":
            emit(TokenType.NEWLINE, i, i + 1)
            i += 1
            continue
        if ch.isalpha() and ch.isascii():
            start = i
            i += 1
            while i < n and text[i].isascii() and (text[i].isalnum() or text[i] == "_"):
                i += 1
            emit(TokenType.IDENT, start, i)
            continue
        if ch.isdigit():
            start = i
            i += 1
            while i < n and text[i].isdigit():
                i += 1
            if i < n and text[i] == "." and i + 1 < n and text[i + 1].isdigit():
                i += 1
                while i < n and text[i].isdigit():
                    i += 1
            emit(TokenType.NUMBER, start, i)
            continue
        if ch in _PUNCT:
            emit(_PUNCT[ch], i, i + 1)
            i += 1
            continue
        errors.append(LexError(f"无法识别的字符 {ch!r}", source.span(i, i + 1)))
        i += 1

    emit(TokenType.EOF, n, n)
    return tokens, errors
