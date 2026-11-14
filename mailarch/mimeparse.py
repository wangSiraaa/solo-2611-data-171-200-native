"""MIME tree analysis for one message.

Implements the structural rules from fixtures/rules.json:

- multipart/alternative: prefer a usable text/html, keep the text/plain
  alternative; preference order ["text/html", "text/plain"].
- multipart/related: root = ``start`` Content-ID, else first child.
- CID scope: nearest containing multipart/related; nested scopes do not
  leak outward (a CID in a nested related never binds outward, and an
  outer resource never hijacks a nested reference).
- only the chosen root/alternative is the primary body; other subtrees
  keep their roles and references.

Every part keeps: original byte digest (transfer-decoded payload), part
path, declared type and the actual role assigned by this analysis.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from email import policy
from email.message import Message
from email.parser import BytesParser
from html import unescape
from urllib.parse import unquote

POLICY = policy.default
TEXT_TYPES = {"text/plain", "text/html"}
MAX_DEPTH = 40
CID_RE = re.compile(r"cid:\s*([^\"'\s>)]+)", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]*>")


def parse_bytes(raw: bytes) -> Message:
    return BytesParser(policy=POLICY).parsebytes(raw)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class Part:
    path: str
    declared_type: str
    cte: str = ""
    disposition: str = ""
    role: str = "container"
    sha256: str | None = None
    size: int | None = None
    content_id: str | None = None
    display_name: str | None = None
    charset: str | None = None
    asset: str | None = None
    digest_note: str | None = None
    defects: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "declared_type": self.declared_type,
            "cte": self.cte,
            "disposition": self.disposition,
            "role": self.role,
            "sha256": self.sha256,
            "size": self.size,
            "content_id": self.content_id,
            "display_name": self.display_name,
            "charset": self.charset,
            "asset": self.asset,
            "digest_note": self.digest_note,
            "defects": list(self.defects),
        }


class MimeTree:
    """Analyzed MIME tree of a single message (top-level or attached)."""

    def __init__(self, msg: Message, owner: str = "") -> None:
        self.owner = owner
        self.parts: dict[str, Part] = {}
        self.order: list[str] = []
        self.children: dict[str, list[str]] = {}
        self.related_root: dict[str, str] = {}
        self.scopes: dict[str, dict[str, str]] = {}
        self.part_scope: dict[str, str | None] = {}
        self.cid_refs: list[dict] = []
        self.attached: list[tuple[str, Message, bytes]] = []
        self.text: dict[str, str] = {}
        self.blob: dict[str, bytes] = {}
        self.issues: list[dict] = []
        self._scope_n = 0
        self._walk(msg, "", None, 0)
        self._assign("", "body")
        bodies = [p for p in self.order if self.parts[p].role == "body"]
        if len(bodies) > 1:
            self.issue("", "multiple-body", ",".join(bodies))
        self.body_path = bodies[0] if bodies else None
        self.body_format = None
        if self.body_path:
            self.body_format = (
                "html" if self.parts[self.body_path].declared_type == "text/html" else "plain"
            )
        self.alternatives = [p for p in self.order if self.parts[p].role == "alternative"]
        self._extract_cid_refs()

    # ------------------------------------------------------------------
    def issue(self, path: str, code: str, detail: str) -> None:
        self.issues.append({"part": path, "code": code, "detail": detail})

    # ------------------------------------------------------------------
    # structural walk
    # ------------------------------------------------------------------
    def _walk(self, part: Message, path: str, scope: str | None, depth: int) -> None:
        if depth > MAX_DEPTH:
            self.issue(path, "max-depth-exceeded", f"depth>{MAX_DEPTH}")
            return
        ctype = part.get_content_type()
        if part.get("Content-Type") is None:
            self.issue(path, "content-type-defaulted",
                       "missing Content-Type header, assumed text/plain")
        cte = str(part.get("Content-Transfer-Encoding", "") or "").strip().lower()
        rec = Part(path=path, declared_type=ctype, cte=cte,
                   disposition=part.get_content_disposition() or "")
        rec.defects = sorted({type(d).__name__ for d in getattr(part, "defects", [])})
        for d in getattr(part, "defects", []):
            self.issue(path, "defect", f"{type(d).__name__}: {d}")
        cid = part.get("Content-ID")
        if cid is not None:
            cid = "".join(str(cid).split()).strip("<>")
            if cid:
                rec.content_id = cid
        try:
            fn = part.get_filename()
        except Exception as exc:  # malformed RFC 2231 parameters
            fn = None
            self.issue(path, "filename-decode-failed", str(exc))
        if fn:
            rec.display_name = str(fn)  # display only, never used for paths
        self.parts[path] = rec
        self.order.append(path)

        if ctype == "message/rfc822":
            # note: is_multipart() is payload-based and is also True for
            # message/rfc822, so this branch must come first
            rec.role = "attached-message"
            payload = part.get_payload()
            payloads = payload if isinstance(payload, list) else ([payload] if payload else [])
            msgs = [p for p in payloads if isinstance(p, Message)]
            data = b""
            if not msgs:
                self.issue(path, "empty-rfc822", "message/rfc822 without parseable payload")
            else:
                if len(msgs) > 1:
                    self.issue(path, "rfc822-multi-payload",
                               f"{len(msgs)} payloads, using the first")
                try:
                    data = msgs[0].as_bytes(policy=policy.SMTP)
                except Exception as exc:
                    data = b""
                    self.issue(path, "rfc822-serialize", str(exc))
                self.attached.append((path, msgs[0], data))
            self.blob[path] = data
            rec.sha256 = sha256_hex(data)
            rec.size = len(data)
            rec.digest_note = "canonical-reserialization"
            return

        if part.is_multipart() and ctype.startswith("multipart/"):
            kids = list(part.iter_parts())
            if not kids:
                self.issue(path, "empty-multipart", ctype)
            child_paths = [
                f"{path}.{i}" if path else str(i) for i in range(1, len(kids) + 1)
            ]
            self.children[path] = child_paths
            sub_scope = scope
            sid = None
            if ctype == "multipart/related":
                self._scope_n += 1
                sid = f"s{self._scope_n}"
                self.scopes[sid] = {}
                sub_scope = sid
            for cp, kid in zip(child_paths, kids):
                self._walk(kid, cp, sub_scope, depth + 1)
            if ctype == "multipart/related" and sid is not None:
                self._register_related(part, path, child_paths, sid)
            return

        # leaf part: digest the transfer-decoded payload bytes
        data = None
        try:
            data = part.get_payload(decode=True)
        except Exception as exc:
            self.issue(path, "decode-failed", f"cte={cte or '7bit'}: {exc}")
            data = None
        # decoding registers some defects lazily; pick them up now
        for d in getattr(part, "defects", []):
            name = type(d).__name__
            if name not in rec.defects:
                rec.defects.append(name)
                rec.defects.sort()
                self.issue(path, "defect", f"{name}: {d}")
        if data is None:
            raw = part.get_payload(decode=False)
            data = raw.encode("utf-8", "replace") if isinstance(raw, str) else b""
        rec.sha256 = sha256_hex(data)
        rec.size = len(data)
        self.blob[path] = data
        if ctype in TEXT_TYPES:
            rec.charset = part.get_content_charset() or "utf-8"
            self.text[path] = self._decode_text(rec, data)
            self.part_scope[path] = scope

    def _decode_text(self, rec: Part, data: bytes) -> str:
        charset = rec.charset or "utf-8"
        try:
            return data.decode(charset, "strict")
        except (LookupError, UnicodeDecodeError, ValueError) as exc:
            self.issue(rec.path, "charset-fallback",
                       f"charset={charset}: {exc}; decoded as utf-8/replace")
            return data.decode("utf-8", "replace")

    def _register_related(self, part: Message, path: str,
                          child_paths: list[str], sid: str) -> None:
        start = part.get_param("start")
        root_idx = 0
        if start:
            want = "".join(str(start).split()).strip("<>")
            for i, cp in enumerate(child_paths):
                if self.parts[cp].content_id == want:
                    root_idx = i
                    break
            else:
                self.issue(path, "start-not-found", f"start={want}")
        self.related_root[path] = child_paths[root_idx]
        for i, cp in enumerate(child_paths):
            if i == root_idx:
                continue
            for desc in self._resource_leaves(cp):
                cid = self.parts[desc].content_id
                if not cid:
                    continue
                if cid in self.scopes[sid]:
                    self.issue(desc, "duplicate-cid",
                               f"{cid} in scope {sid}; first binding kept")
                else:
                    self.scopes[sid][cid] = desc

    def _resource_leaves(self, path: str):
        """Descendants of path, never crossing into a nested multipart/related
        (nested scopes do not leak outward)."""
        yield path
        if self.parts[path].declared_type == "multipart/related":
            return
        for c in self.children.get(path, []):
            yield from self._resource_leaves(c)

    # ------------------------------------------------------------------
    # body selection and role assignment
    # ------------------------------------------------------------------
    def _assign(self, path: str, ctx: str) -> None:
        rec = self.parts[path]
        if rec.role == "attached-message":
            return
        t = rec.declared_type
        kids = self.children.get(path, [])
        if t == "multipart/alternative" and ctx in ("body", "alternative"):
            best = None
            cands = {}
            for c in kids:
                cand = self._body_candidate(c)
                cands[c] = cand
                if cand and (best is None or self._rank(cand) > self._rank(best)):
                    best = cand
            for c in kids:
                cand = cands[c]
                if cand is not None and best is not None and cand[0] == best[0]:
                    self._assign(c, ctx)
                elif cand is not None:
                    self._assign(c, "alternative")
                else:
                    self._assign(c, "attachment")
            return
        if t == "multipart/related" and ctx in ("body", "alternative"):
            root = self.related_root.get(path)
            for c in kids:
                self._assign(c, ctx if c == root else "inline")
            return
        if t.startswith("multipart/"):
            # mixed and friends: first child carries the body position
            for i, c in enumerate(kids):
                if ctx in ("body", "alternative"):
                    self._assign(c, ctx if i == 0 else "attachment")
                else:
                    self._assign(c, ctx)
            return
        if t in TEXT_TYPES and ctx in ("body", "alternative"):
            rec.role = "body" if ctx == "body" else "alternative"
        elif ctx == "inline":
            rec.role = "inline"
        else:
            rec.role = "attachment"
            if ctx in ("body", "alternative"):
                self.issue(path, "non-text-in-body-position", t)

    def _body_candidate(self, path: str):
        rec = self.parts[path]
        t = rec.declared_type
        if t in TEXT_TYPES:
            return (path, "html" if t == "text/html" else "plain")
        if t == "multipart/alternative":
            best = None
            for c in self.children.get(path, []):
                cand = self._body_candidate(c)
                if cand and (best is None or self._rank(cand) > self._rank(best)):
                    best = cand
            return best
        if t == "multipart/related":
            root = self.related_root.get(path)
            return self._body_candidate(root) if root else None
        if t.startswith("multipart/"):
            kids = self.children.get(path, [])
            return self._body_candidate(kids[0]) if kids else None
        return None

    def _rank(self, cand) -> int:
        path, fmt = cand
        usable = self._usable(path)
        if fmt == "html":
            return 4 if usable else 2
        return 3 if usable else 1

    def _usable(self, path: str) -> bool:
        text = self.text.get(path, "")
        if self.parts[path].declared_type == "text/html":
            text = unescape(TAG_RE.sub(" ", text))
        return bool(text.strip())

    # ------------------------------------------------------------------
    # cid references (resolved within the nearest related scope only)
    # ------------------------------------------------------------------
    def _extract_cid_refs(self) -> None:
        for path in self.order:
            rec = self.parts[path]
            if rec.declared_type != "text/html":
                continue
            if rec.role not in ("body", "alternative", "inline"):
                continue
            scope = self.part_scope.get(path)
            cmap = self.scopes.get(scope, {}) if scope else {}
            for m in CID_RE.finditer(self.text.get(path, "")):
                cid = unquote(m.group(1)).strip().strip("<>")
                bound = cmap.get(cid)
                self.cid_refs.append({"part": path, "cid": cid, "bound_to": bound})
                if bound is None:
                    self.issue(path, "cid-unbound", cid)
