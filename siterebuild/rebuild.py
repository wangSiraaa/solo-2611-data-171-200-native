"""Rebuild pipeline: traverse the plan entries, select captures per cutoff,
allocate stable local paths, rewrite references, export the static site and
write the capture provenance table and gap report."""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .refs import (
    Ref,
    extract_css_refs,
    extract_html_refs,
    parse_css,
    parse_html,
    serialize_css,
    set_token_url,
)
from .resolve import (
    ChainResult,
    Gap,
    Reason,
    Resolver,
    effective_fragment,
)
from .urls import canonicalize, origin_of, resolve_reference
from .warcfile import Store, full_digest, load_store, sha1_b32

EXT_BY_TYPE = {
    "text/html": ".html",
    "text/css": ".css",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "application/json": ".json",
    "text/plain": ".txt",
}

SKIP_SCHEMES = ("javascript:", "mailto:", "data:", "tel:", "urn:", "about:")


def cutoff_slug(cutoff: str) -> str:
    return cutoff.replace(":", "-")


def sanitize_component(text: str) -> str:
    out = []
    for byte in text.encode("utf-8"):
        ch = chr(byte)
        if ch.isascii() and (ch.isalnum() or ch in "._=-~"):
            out.append(ch)
        else:
            out.append("_%02X" % byte)
    return "".join(out)


@dataclass
class ResBuild:
    """A resolved resource plus its export state."""
    resource: object
    path: str = ""
    soup: object = None
    base_tags: list = field(default_factory=list)
    used_base: str | None = None
    base_url: str = ""
    css_rules: object = None
    refs: list = field(default_factory=list)
    exported: bytes = b""
    rewritten: bool = False


@dataclass
class GapEntry:
    reason: str
    url: str
    fragment: str | None
    detail: str
    chain: list
    referrers: list = field(default_factory=list)

    def to_dict(self):
        return {
            "reason": self.reason,
            "url": self.url,
            "fragment": self.fragment,
            "detail": self.detail,
            "chain": self.chain,
            "referrers": self.referrers,
        }


class CutoffBuild:
    def __init__(self, plan: dict, store: Store, cutoff: str):
        self.plan = plan
        self.store = store
        self.cutoff = cutoff
        from .warcfile import parse_warc_date

        self.resolver = Resolver(store, parse_warc_date(cutoff), plan["allowed_origins"])
        self.resources: dict[str, ResBuild] = {}
        self.redirects: dict[str, ChainResult] = {}
        self.failures: dict[str, Gap] = {}
        self.gap_entries: dict[tuple, GapEntry] = {}
        self.paths: dict[str, str] = {}
        self.gap_paths: dict[str, str] = {}

    # -- traversal ---------------------------------------------------------

    def run(self):
        queue: list[str] = []
        seen: set[str] = set()
        entry_canons: set[str] = set()
        for entry in self.plan["entry_urls"]:
            canon = canonicalize(entry)
            entry_canons.add(canon)
            if canon not in seen:
                seen.add(canon)
                queue.append(canon)
        while queue:
            canon = queue.pop(0)
            chain = self.resolver.resolve_url(canon)
            if chain.hops:
                self.redirects[canon] = chain
            if chain.gap is not None:
                self.failures[canon] = chain.gap
                if canon in entry_canons:
                    self._gap(chain.gap, None, None)
                continue
            res = chain.resource
            if res.canon in self.resources:
                continue
            rb = ResBuild(resource=res)
            self.resources[res.canon] = rb
            if res.kind == "html":
                rb.soup, rb.base_tags, rb.used_base, rb.base_url = parse_html(
                    res.payload.data, res.canon
                )
                rb.refs = extract_html_refs(rb.soup)
                for ref in rb.refs:
                    self._classify(ref, rb, queue, seen)
            elif res.kind == "css":
                rb.base_url = res.canon  # CSS relatives resolve against the CSS URL itself
                rb.css_rules = parse_css(res.payload.data)
                rb.refs = extract_css_refs(rb.css_rules)
                for ref in rb.refs:
                    self._classify(ref, rb, queue, seen)
        self._allocate_paths()
        self._rewrite_all()
        self._export()

    def _classify(self, ref: Ref, rb: ResBuild, queue: list, seen: set):
        raw = ref.raw.strip()
        page_canon = rb.resource.canon
        if raw == "":
            ref.disposition = "empty"
            return
        if raw.startswith("#"):
            ref.disposition = "self-fragment"
            ref.canon = page_canon
            ref.fragment = raw[1:] or None
            return
        low = raw.lower()
        if any(low.startswith(s) for s in SKIP_SCHEMES):
            ref.disposition = "skipped-scheme"
            return
        canon, frag = resolve_reference(rb.base_url, raw)
        ref.canon = canon
        ref.fragment = frag
        if origin_of(canon) not in self.resolver.allowed:
            ref.disposition = "external"
            return
        ref.disposition = "pending"
        if canon not in seen:
            seen.add(canon)
            queue.append(canon)

    # -- path allocation ----------------------------------------------------

    def _allocate_paths(self):
        used: dict[str, str] = {}
        for canon in sorted(self.resources):
            rb = self.resources[canon]
            parts = urlsplit(canon)
            segs = [sanitize_component(s) for s in parts.path.split("/") if s]
            if not segs:
                segs = ["index"]
            last = segs[-1]
            if parts.query:
                last = last + "~" + sanitize_component(parts.query)
            if not re.search(r"\.[A-Za-z0-9]{1,8}$", last):
                last += EXT_BY_TYPE.get(rb.resource.main_type, ".bin")
            segs[-1] = last
            rel = "/".join(segs)
            if rel in used and used[rel] != canon:
                stem, dot, ext = rel.rpartition(".")
                rel = f"{stem}__{sha1_b32(canon.encode())[:10]}.{ext}"
            used[rel] = canon
            rb.path = rel
            self.paths[canon] = rel
        gap_used: dict[str, str] = {}
        for canon in sorted(self.failures):
            parts = urlsplit(canon)
            slug = sanitize_component(parts.path.strip("/").replace("/", "_") or "root")
            if parts.query:
                slug += "~" + sanitize_component(parts.query)
            rel = f"_gaps/{slug}.html"
            if rel in gap_used and gap_used[rel] != canon:
                rel = f"_gaps/{slug}__{sha1_b32(canon.encode())[:10]}.html"
            gap_used[rel] = canon
            self.gap_paths[canon] = rel

    # -- rewriting ----------------------------------------------------------

    def _rewrite_all(self):
        for canon in sorted(self.resources):
            rb = self.resources[canon]
            res = rb.resource
            if res.kind == "html":
                for tag in rb.base_tags:
                    tag.decompose()
                for ref in rb.refs:
                    self._finish_ref(ref, rb)
                rb.exported = rb.soup.encode("utf-8")
                rb.rewritten = True
            elif res.kind == "css":
                for ref in rb.refs:
                    self._finish_ref(ref, rb)
                rb.exported = serialize_css(rb.css_rules)
                rb.rewritten = True
            else:
                rb.exported = res.payload.data
                rb.rewritten = False

    def _finish_ref(self, ref: Ref, rb: ResBuild):
        page_path = rb.path
        page_dir = posixpath.dirname(page_path) or "."
        if ref.disposition == "self-fragment":
            frag = ref.fragment
            ref.rewritten = "#" + frag if frag else "#"
            if frag and not self._fragment_exists(rb, frag):
                self._gap(
                    Gap(
                        Reason.MISSING_FRAGMENT,
                        ref.canon,
                        f"fragment #{frag} has no id or a[name] target in "
                        f"{ref.canon}",
                        [],
                    ),
                    rb,
                    ref,
                    fragment=frag,
                )
            return
        if ref.disposition != "pending":
            return
        chain = self.resolver.resolve_url(ref.canon)
        frag = effective_fragment(chain, ref.fragment)
        if chain.gap is not None:
            ref.disposition = "gap"
            ref.gap_reason = chain.gap.reason
            target = self.gap_paths[ref.canon]
            self._gap(chain.gap, rb, ref)
        else:
            final = chain.final_canon
            target = self.paths[final]
            if chain.hops:
                ref.redirect_hops = chain.hops
            final_rb = self.resources[final]
            if frag and final_rb.resource.kind == "html":
                if not self._fragment_exists(final_rb, frag):
                    self._gap(
                        Gap(
                            Reason.MISSING_FRAGMENT,
                            final,
                            f"fragment #{frag} has no id or a[name] target "
                            f"in {final} after redirects",
                            [h["canon"] for h in chain.hops] + [final],
                        ),
                        rb,
                        ref,
                        fragment=frag,
                    )
        rel = posixpath.relpath(target, page_dir)
        ref.target_path = target
        ref.rewritten = rel + ("#" + frag if frag else "")
        if ref.disposition == "pending":
            ref.disposition = "rewritten"
        if ref.tag is not None:
            if ref.disposition == "gap":
                ref.tag["data-gap-reason"] = ref.gap_reason
                ref.tag[f"data-original-{ref.attr}"] = ref.raw
            ref.tag[ref.attr] = ref.rewritten
        elif ref.token is not None:
            set_token_url(ref.token, ref.rewritten)

    def _fragment_exists(self, rb: ResBuild, frag: str) -> bool:
        for candidate in {frag, unquote(frag)}:
            if rb.soup.find(id=candidate):
                return True
            if rb.soup.find("a", attrs={"name": candidate}):
                return True
        return False

    def _gap(self, gap: Gap, rb: ResBuild | None, ref: Ref | None,
             fragment: str | None = None):
        key = (gap.reason, gap.url, fragment)
        entry = self.gap_entries.get(key)
        if entry is None:
            entry = GapEntry(gap.reason, gap.url, fragment, gap.detail, gap.chain)
            self.gap_entries[key] = entry
        referrer = {"page": "(plan entry)", "page_path": None, "kind": "entry",
                    "raw": gap.url}
        if rb is not None and ref is not None:
            referrer = {
                "page": rb.resource.canon,
                "page_path": rb.path,
                "kind": ref.kind,
                "raw": ref.raw,
            }
        if referrer not in entry.referrers:
            entry.referrers.append(referrer)

    # -- export --------------------------------------------------------------

    def _export(self):
        slug = cutoff_slug(self.cutoff)
        root = Path(self.plan["_out_dir"]) / slug
        site = root / "site"
        site.mkdir(parents=True, exist_ok=True)
        for canon in sorted(self.resources):
            rb = self.resources[canon]
            dest = site / rb.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(rb.exported)
        for canon, rel in sorted(self.gap_paths.items()):
            gap = self.failures[canon]
            dest = site / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(self._placeholder(canon, gap, rel))
        (root / "captures.json").write_text(
            json.dumps(self._captures_table(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (root / "gaps.json").write_text(
            json.dumps(self._gaps_report(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (root / "references.json").write_text(
            json.dumps(self._references_report(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        summary = {
            "cutoff": self.cutoff,
            "entry_urls": self.plan["entry_urls"],
            "site_dir": str(site),
            "counts": {
                "resources": len(self.resources),
                "redirects": len(self.redirects),
                "failures": len(self.failures),
                "gaps": len(self.gap_entries),
                "references": sum(len(rb.refs) for rb in self.resources.values()),
            },
        }
        (root / "summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self.out_root = root

    def _placeholder(self, canon: str, gap: Gap, rel: str) -> bytes:
        entries = [e for e in self.gap_entries.values()
                   if e.url == canon and e.reason == gap.reason]
        referrers = []
        for e in entries:
            referrers.extend(e.referrers)
        ref_items = "".join(
            f"<li>{r['page']} &mdash; {r['kind']}: <code>{r['raw']}</code></li>"
            for r in referrers
        ) or "<li>(plan entry)</li>"
        chain_items = "".join(f"<li><code>{c}</code></li>" for c in gap.chain)
        html = (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            f"<title>Unavailable capture: {gap.reason}</title></head><body>"
            "<h1>Offline placeholder &mdash; capture unavailable</h1>"
            f"<p>Reason: <code>{gap.reason}</code></p>"
            f"<p>Original URL: <code>{canon}</code></p>"
            f"<p>{gap.detail}</p>"
            f"<h2>Referenced from</h2><ul>{ref_items}</ul>"
            + (f"<h2>Chain</h2><ul>{chain_items}</ul>" if gap.chain else "")
            + "</body></html>"
        )
        return html.encode("utf-8")

    # -- reports ---------------------------------------------------------------

    def _captures_table(self) -> dict:
        resources = []
        for canon in sorted(self.resources):
            rb = self.resources[canon]
            res = rb.resource
            rec = res.record
            resources.append({
                "canon": canon,
                "local_path": rb.path,
                "kind": res.kind,
                "status": res.status,
                "content_type": res.content_type,
                "selected_record": {
                    "id": rec.record_id,
                    "type": rec.rtype,
                    "warc_date": rec.date,
                    "capture_label": rec.label,
                    "http_headers": [[k, v] for k, v in rec.headers],
                    "stated_digest": rec.digest,
                    "payload_digest_verified": (
                        rec.digest is not None
                        and res.payload.digest == rec.digest
                    ),
                },
                "payload": {
                    "provenance": res.payload.provenance_kind,
                    "supports": res.payload.supports,
                    "revisit_chain": res.payload.revisit_chain,
                },
                "digests": {
                    "original_sha1_b32": res.payload.digest,
                    "exported_sha1_b32": full_digest(rb.exported),
                    "rewritten": rb.rewritten,
                },
            })
        redirects = []
        for canon in sorted(self.redirects):
            chain = self.redirects[canon]
            entry = {"from": canon, "hops": chain.hops}
            if chain.gap is not None:
                entry["error"] = chain.gap.reason
            else:
                entry["final"] = chain.final_canon
            redirects.append(entry)
        failures = []
        for canon in sorted(self.failures):
            gap = self.failures[canon]
            failures.append({
                "canon": canon,
                "reason": gap.reason,
                "detail": gap.detail,
                "chain": gap.chain,
                "placeholder_path": self.gap_paths.get(canon),
            })
        return {
            "cutoff": self.cutoff,
            "resources": resources,
            "redirects": redirects,
            "failures": failures,
        }

    def _gaps_report(self) -> dict:
        return {
            "cutoff": self.cutoff,
            "gaps": [
                entry.to_dict()
                for entry in sorted(
                    self.gap_entries.values(),
                    key=lambda e: (e.reason, e.url, e.fragment or ""),
                )
            ],
        }

    def _references_report(self) -> dict:
        pages = []
        for canon in sorted(self.resources):
            rb = self.resources[canon]
            if rb.resource.kind not in ("html", "css"):
                continue
            page = {
                "canon": canon,
                "local_path": rb.path,
                "kind": rb.resource.kind,
                "refs": [ref.to_dict() for ref in rb.refs],
            }
            if rb.resource.kind == "html":
                page["base"] = {
                    "raw": rb.used_base,
                    "resolved": rb.base_url if rb.used_base else None,
                    "action": "applied-for-resolution-and-removed"
                    if rb.used_base else None,
                    "base_tags_found": len(rb.base_tags),
                }
            pages.append(page)
        return {"cutoff": self.cutoff, "pages": pages}


def rebuild(plan_path: str, warc_path: str, out_dir: str) -> list[dict]:
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    plan["_out_dir"] = out_dir
    store = load_store(warc_path)
    results = []
    for cutoff in plan["cutoffs"]:
        build = CutoffBuild(plan, store, cutoff)
        build.run()
        results.append({
            "cutoff": cutoff,
            "dir": str(build.out_root),
            "resources": len(build.resources),
            "failures": len(build.failures),
            "gaps": len(build.gap_entries),
        })
    summary = {
        "warc": warc_path,
        "plan": plan_path,
        "records": len(store.records),
        "cutoffs": results,
    }
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / "rebuild-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return results
