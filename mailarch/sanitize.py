"""HTML sanitizer for mail previews.

Policy (fixtures/rules.json html_policy): no scripts, no event
attributes, no external loading, no active attachments; unsupported
markup is escaped (shown as text) rather than silently dropped.

- allowlist of tags/attributes; everything else is escaped as text
- script/style/iframe/object/embed/svg/form/... content is dropped
- <img src> only for cid: references bound inside the owning related
  scope and only for safe raster image types; other bound resources are
  rendered as download links; remote/data URLs become blocked
  placeholders; unbound CIDs become visible gap markers
- <a href> limited to http/https/mailto, marked nofollow/noreferrer
"""
from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import unquote

ALLOWED_TAGS = {
    "a", "p", "br", "div", "span", "b", "strong", "i", "em", "u", "s",
    "blockquote", "pre", "code", "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "img", "figure",
    "figcaption", "sub", "sup", "small", "q", "cite", "abbr",
}
VOID_TAGS = {"br", "hr", "img"}
SKIP_CONTENT = {
    "script", "style", "iframe", "object", "embed", "applet", "svg",
    "math", "template", "form", "link", "meta", "base", "noscript",
}
# document-structure tags: dropped silently, content kept
DROP_TAGS = {"html", "body", "head"}
SAFE_INLINE_IMG = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_ALLOWED_SCHEMES = {"http", "https", "mailto"}
_SCHEME_RE = re.compile(r"^([a-z][a-z0-9+.-]*):", re.IGNORECASE)


def _sanitize_href(href):
    href = (href or "").strip()
    if not href:
        return None
    if href.startswith("#"):
        return href
    m = _SCHEME_RE.match(href)
    if m and m.group(1).lower() in _ALLOWED_SCHEMES:
        return href
    return None


class Sanitizer(HTMLParser):
    def __init__(self, resolve_cid=None, note=None):
        super().__init__(convert_charrefs=True)
        self.resolve_cid = resolve_cid or (lambda cid: None)
        self.note = note or (lambda kind, detail: None)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.skip = 0

    def _close_to(self, tag):
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_starttag(self, tag, attrs):
        self._start(tag, dict(attrs))

    def handle_startendtag(self, tag, attrs):
        self._start(tag, dict(attrs))
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def _start(self, tag, attrs):
        tag = tag.lower()
        if self.skip:
            if tag in SKIP_CONTENT:
                self.skip += 1
            return
        if tag in SKIP_CONTENT:
            self.skip = 1
            self.note("dropped-element", tag)
            return
        if tag in DROP_TAGS:
            return
        if tag not in ALLOWED_TAGS:
            raw = self.get_starttag_text() or f"<{tag}>"
            self.out.append(escape(raw))
            self.note("escaped-markup", tag)
            return
        if tag == "a":
            href = _sanitize_href(attrs.get("href"))
            if href:
                self._open("a", f' href="{escape(href, quote=True)}"'
                                ' rel="nofollow noopener noreferrer"')
            else:
                raw_href = (attrs.get("href") or "").strip()
                if raw_href:
                    self.note("dropped-url", raw_href[:120])
                self._open("a", "")
            return
        if tag == "img":
            self._img(attrs)
            return
        extra = ""
        if tag in ("td", "th"):
            for a in ("colspan", "rowspan"):
                v = (attrs.get(a) or "").strip()
                if v.isdigit() and 0 < int(v) <= 100:
                    extra += f' {a}="{v}"'
        self._open(tag, extra)

    def _open(self, tag, extra):
        self.out.append(f"<{tag}{extra}>")
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def _img(self, attrs):
        src = (attrs.get("src") or "").strip()
        alt = escape((attrs.get("alt") or "")[:200], quote=True)
        dim = ""
        for a in ("width", "height"):
            v = (attrs.get(a) or "").strip()
            if v.isdigit() and int(v) <= 9999:
                dim += f' {a}="{v}"'
        if not src:
            self.note("dropped-img", "empty src")
            return
        if src.lower().startswith("cid:"):
            cid = unquote(src[4:]).strip().strip("<>")
            hit = self.resolve_cid(cid)
            if hit:
                url, declared = hit
                if declared in SAFE_INLINE_IMG:
                    self.out.append(
                        f'<img src="{escape(url, quote=True)}" alt="{alt}"{dim}>')
                else:
                    self.out.append(
                        f'<a class="inline-res" href="{escape(url, quote=True)}" download>'
                        f'[inline resource: {escape(cid)} ({escape(declared)})]</a>')
            else:
                self.out.append(
                    f'<span class="cid-gap">[missing inline resource: {escape(cid)}]</span>')
                self.note("cid-gap", cid)
            return
        if re.match(r"https?://", src, re.IGNORECASE):
            self.out.append(
                f'<span class="blocked-remote">[remote image blocked: {escape(src)}]</span>')
            self.note("blocked-remote", src)
            return
        self.out.append(
            f'<span class="blocked-remote">[image source blocked: {escape(src[:80])}]</span>')
        self.note("blocked-src", src[:120])

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip:
            if tag in SKIP_CONTENT:
                self.skip -= 1
            return
        if tag in SKIP_CONTENT:
            return
        if tag in DROP_TAGS:
            return
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            self._close_to(tag)
        elif tag not in ALLOWED_TAGS:
            self.out.append(escape(f"</{tag}>"))

    def handle_data(self, data):
        if not self.skip:
            self.out.append(escape(data))

    def result(self):
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize_html(text, resolve_cid=None, note=None):
    s = Sanitizer(resolve_cid=resolve_cid, note=note)
    s.feed(text)
    s.close()
    return s.result()
