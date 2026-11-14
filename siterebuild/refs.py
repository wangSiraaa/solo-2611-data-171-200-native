"""Reference extraction and rewriting for HTML and CSS.

HTML: a[href], link[href], img[src] plus the first valid base[href].
CSS: url(...) and @import, resolved against the CSS file's own URL -- never
against a parent HTML document's base.
"""

from __future__ import annotations

import tinycss2
from bs4 import BeautifulSoup

HTML_REF_TAGS = (("a", "href"), ("link", "href"), ("img", "src"))


class Ref:
    """One outgoing reference found in an HTML or CSS resource."""

    __slots__ = (
        "kind", "raw", "tag", "attr", "token",
        "canon", "fragment", "disposition",
        "rewritten", "target_path", "gap_reason", "redirect_hops",
    )

    def __init__(self, kind, raw, tag=None, attr=None, token=None):
        self.kind = kind
        self.raw = raw
        self.tag = tag
        self.attr = attr
        self.token = token
        self.canon = None
        self.fragment = None
        self.disposition = "pending"
        self.rewritten = None
        self.target_path = None
        self.gap_reason = None
        self.redirect_hops = None

    def to_dict(self):
        out = {
            "kind": self.kind,
            "raw": self.raw,
            "disposition": self.disposition,
        }
        if self.canon is not None:
            out["canon"] = self.canon
        if self.fragment is not None:
            out["fragment"] = self.fragment
        if self.rewritten is not None:
            out["rewritten"] = self.rewritten
        if self.target_path is not None:
            out["target_path"] = self.target_path
        if self.gap_reason is not None:
            out["gap_reason"] = self.gap_reason
        if self.redirect_hops:
            out["redirect_hops"] = self.redirect_hops
        return out


# ---------------------------------------------------------------------------
# HTML

def parse_html(payload: bytes, page_canon: str):
    """Parse HTML payload; return (soup, base_tags, used_base_raw, base_url)."""
    from urllib.parse import urljoin

    text = payload.decode("utf-8")
    soup = BeautifulSoup(text, "html.parser")
    base_tags = soup.find_all("base")
    used_base = None
    for tag in base_tags:
        href = (tag.get("href") or "").strip()
        if href:
            used_base = href
            break
    base_url = urljoin(page_canon, used_base) if used_base else page_canon
    return soup, base_tags, used_base, base_url


def extract_html_refs(soup) -> list[Ref]:
    refs: list[Ref] = []
    for tag_name, attr in HTML_REF_TAGS:
        for tag in soup.find_all(tag_name):
            value = tag.get(attr)
            if value is None:
                continue
            refs.append(Ref(f"{tag_name}[{attr}]", value, tag=tag, attr=attr))
    return refs


# ---------------------------------------------------------------------------
# CSS

def parse_css(payload: bytes):
    text = payload.decode("utf-8")
    return tinycss2.parse_stylesheet(text, skip_comments=False, skip_whitespace=False)


def _quote_css_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_token_url(token, new_value: str) -> None:
    token.value = new_value
    if token.type == "url":
        token.representation = "url(" + _quote_css_string(new_value) + ")"
    else:  # string token
        token.representation = _quote_css_string(new_value)


def _walk_url_tokens(tokens, out: list[Ref]):
    for tok in tokens:
        ttype = tok.type
        if ttype == "url":
            out.append(Ref("css url()", tok.value, token=tok))
        elif ttype == "function":
            if tok.lower_name == "url":
                for arg in tok.arguments:
                    if arg.type == "string":
                        out.append(Ref("css url()", arg.value, token=arg))
                        break
            else:
                _walk_url_tokens(tok.arguments, out)
        elif ttype in ("{} block", "[] block", "() block"):
            _walk_url_tokens(tok.content, out)


def extract_css_refs(rules) -> list[Ref]:
    refs: list[Ref] = []
    for rule in rules:
        if rule.type == "at-rule":
            if rule.lower_at_keyword == "import":
                found = False
                for tok in rule.prelude:
                    if tok.type == "string" and not found:
                        refs.append(Ref("css @import", tok.value, token=tok))
                        found = True
                    elif tok.type == "url" and not found:
                        refs.append(Ref("css @import", tok.value, token=tok))
                        found = True
                    elif tok.type == "function" and tok.lower_name == "url" and not found:
                        for arg in tok.arguments:
                            if arg.type == "string":
                                refs.append(Ref("css @import", arg.value, token=arg))
                                found = True
                                break
            if rule.content:
                _walk_url_tokens(rule.content, refs)
        elif rule.type == "qualified-rule":
            _walk_url_tokens(rule.content, refs)
    return refs


def serialize_css(rules) -> bytes:
    return tinycss2.serialize(rules).encode("utf-8")
