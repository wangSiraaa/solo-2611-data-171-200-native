"""URL identity handling per plan policy.

Canonical URL identity: lowercase scheme/host, default port removed, dot
segments resolved, fragment removed.  Percent-encoding (including %2F) and
the exact query string (order included) are preserved verbatim.
"""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit, urlunsplit

DEFAULT_PORTS = {"http": 80, "https": 443}


def remove_dot_segments(path: str) -> str:
    """RFC 3986 section 5.2.4, without decoding any percent-encoding."""
    if not path:
        return path
    leading = path.startswith("/")
    trailing = path.endswith(("/", "/.", "/.."))
    segs: list[str] = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if segs:
                segs.pop()
            continue
        segs.append(seg)
    out = ("/" if leading else "") + "/".join(segs)
    if trailing and out and not out.endswith("/"):
        out += "/"
    if not out and leading:
        out = "/"
    return out


def canonicalize(url: str) -> str:
    """Return the canonical identity of *url* (fragment stripped)."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    netloc = host
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo += ":" + parts.password
        netloc = userinfo + "@" + netloc
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None and port != DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{port}"
    path = remove_dot_segments(parts.path)
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def split_fragment(url: str) -> tuple[str, str | None]:
    parts = urlsplit(url)
    frag = parts.fragment if parts.fragment else None
    base = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    return base, frag


def resolve_reference(base_url: str, ref: str) -> tuple[str, str | None]:
    """Resolve *ref* against *base_url*; return (canonical URL, fragment)."""
    absolute = urljoin(base_url, ref)
    nofrag, frag = split_fragment(absolute)
    return canonicalize(nofrag), frag


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"
