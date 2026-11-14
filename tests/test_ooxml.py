"""Unit tests for the logical-text index and in-place replacement."""

from __future__ import annotations

import pytest
from lxml import etree

from template_expand.ooxml import (
    ParagraphIndex,
    Replacement,
    W,
    apply_replacements,
    scan_text,
)


def para(*segments):
    """Build a ``w:p``; each segment is ``(text, bold)``."""
    p = etree.Element(W("p"))
    for text, bold in segments:
        r = etree.SubElement(p, W("r"))
        if bold:
            etree.SubElement(etree.SubElement(r, W("rPr")), W("b"))
        t = etree.SubElement(r, W("t"))
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t.text = text
    return p


def runs_text(p):
    return [(r, "".join(t.text or "" for t in r.iter(W("t")))) for r in p.iter(W("r"))]


def test_index_maps_across_runs():
    p = para(("A {{", False), ("cus", True), ("tomer}} B", False))
    idx = ParagraphIndex(p)
    assert idx.text == "A {{customer}} B"
    tok = idx.tokens()[0][0]
    assert tok.name == "customer"
    assert tok.start == 2
    # The token begins in the plain run that also holds the static prefix.
    start_chunk = idx.first_chunk(tok.start, tok.end)
    assert start_chunk.text == "A {{"


def test_replace_preserves_other_runs_and_format():
    p = para(("A {{", False), ("cus", True), ("tomer}} B", False))
    idx = ParagraphIndex(p)
    tok = idx.tokens()[0][0]
    apply_replacements(idx, [Replacement(tok.start, tok.end, "X")])
    assert "".join(t.text or "" for t in p.iter(W("t"))) == "A X B"
    # Formatting comes from the *starting* run: here that is the plain run,
    # which also holds the untouched "A " prefix.  The middle bold run is
    # emptied, and the trailing run keeps its own text and format.
    texts = [(r.find(W("rPr")) is not None,
              "".join(t.text or "" for t in r.iter(W("t")))) for r in p.iter(W("r"))]
    assert texts == [(False, "A X"), (True, ""), (False, " B")]


def test_replace_uses_bold_start_run_format():
    p = para(("A {{", False), ("cus", True), ("tomer}} B", False))
    idx = ParagraphIndex(p)
    # Replace only the part beginning in the bold run.
    apply_replacements(idx, [Replacement(idx.text.index("cus"), idx.text.index("}}") + 2, "X")])
    texts = [(r.find(W("rPr")) is not None,
              "".join(t.text or "" for t in r.iter(W("t")))) for r in p.iter(W("r"))]
    assert texts == [(False, "A {{"), (True, "X"), (False, " B")]


def test_adjacent_placeholders_in_one_node():
    p = para(("{{a}}{{b}}", False))
    idx = ParagraphIndex(p)
    toks = idx.tokens()[0]
    assert [t.name for t in toks] == ["a", "b"]
    apply_replacements(idx, [Replacement(toks[0].start, toks[0].end, "1"),
                             Replacement(toks[1].start, toks[1].end, "2")])
    assert "".join(t.text or "" for t in p.iter(W("t"))) == "12"


def test_placeholder_with_linebreak_node_is_rejected():
    # A placeholder whose text spans a <w:br/> has a newline inside its name;
    # that is not a valid field name and must be reported, never silently
    # rendered with the break left behind.
    p = etree.Element(W("p"))
    r = etree.SubElement(p, W("r"))
    etree.SubElement(r, W("t")).text = "{{a"
    etree.SubElement(r, W("br"))
    etree.SubElement(r, W("t")).text = "b}}"
    idx = ParagraphIndex(p)
    assert idx.text == "{{a\nb}}"
    tokens, problems = idx.tokens()
    assert tokens == []
    assert problems


def test_scan_text_reports_bad_expressions():
    tokens, problems = scan_text("x {{a + b}} y {{ok}}")
    assert [t.name for t in tokens] == ["ok"]
    assert problems and "unsupported expression" in problems[0][1]


def test_scan_text_reports_unclosed():
    tokens, problems = scan_text("x {{oops")
    assert tokens == []
    assert problems


def test_token_spanning_three_nodes_and_second_token_later():
    p = para(("{{a", False), (".b}} mid {{c}} tail", True))
    idx = ParagraphIndex(p)
    toks = idx.tokens()[0]
    assert [t.name for t in toks] == ["a.b", "c"]
    apply_replacements(idx, [Replacement(toks[0].start, toks[0].end, "1"),
                             Replacement(toks[1].start, toks[1].end, "2")])
    assert "".join(t.text or "" for t in p.iter(W("t"))) == "1 mid 2 tail"
