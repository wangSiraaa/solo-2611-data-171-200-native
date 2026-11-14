"""Verify exported sites without a browser: HTTP requests against the local
server plus direct parsing of the exported files.

Checks per cutoff: reference closure (HTML/CSS), exported digests, original
payload digests re-derived from the WARC, shared payload bytes with their
provenance supports, fragment targets, gap placeholders and distinct gap
reasons.  Across cutoffs: path stability and shared bytes for shared
captures.  No network access beyond 127.0.0.1, no JavaScript execution.
"""

from __future__ import annotations

import json
import posixpath
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from bs4 import BeautifulSoup

from .rebuild import cutoff_slug
from .refs import extract_css_refs, parse_css
from .resolve import Resolver, Reason
from .urls import canonicalize, origin_of
from .warcfile import full_digest, load_store, parse_warc_date


class Check:
    def __init__(self, name: str, ok: bool, detail: str = ""):
        self.name = name
        self.ok = ok
        self.detail = detail

    def to_dict(self):
        return {"check": self.name, "ok": self.ok, "detail": self.detail}


def _soup(data: bytes) -> BeautifulSoup:
    return BeautifulSoup(data.decode("utf-8"), "html.parser")


def _html_refs(data: bytes):
    soup = _soup(data)
    for tag_name, attr in (("a", "href"), ("link", "href"), ("img", "src")):
        for tag in soup.find_all(tag_name):
            value = tag.get(attr)
            if value is not None:
                yield tag_name, attr, value


def _fragment_targets(data: bytes) -> set:
    soup = _soup(data)
    targets = {t["id"] for t in soup.find_all(attrs={"id": True})}
    targets |= {t["name"] for t in soup.find_all("a", attrs={"name": True})}
    return targets


def verify(plan_path: str, warc_path: str, out_dir: str,
           inventory_path: str | None = None, use_http: bool = True) -> list[Check]:
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    store = load_store(warc_path)
    allowed = {origin_of(o) for o in plan["allowed_origins"]}
    checks: list[Check] = []

    def check(name, ok, detail=""):
        checks.append(Check(name, ok, detail))

    per_cutoff = {}
    for cutoff in plan["cutoffs"]:
        root = Path(out_dir) / cutoff_slug(cutoff)
        site = root / "site"
        captures = json.loads((root / "captures.json").read_text(encoding="utf-8"))
        gaps = json.loads((root / "gaps.json").read_text(encoding="utf-8"))
        references = json.loads((root / "references.json").read_text(encoding="utf-8"))
        per_cutoff[cutoff] = (root, site, captures, gaps, references)
        resolver = Resolver(store, parse_warc_date(cutoff), plan["allowed_origins"])
        resources = {r["canon"]: r for r in captures["resources"]}

        # 1. exported files exist and match their exported digests
        bad = []
        for res in captures["resources"]:
            dest = site / res["local_path"]
            if not dest.is_file():
                bad.append(f"missing {res['local_path']}")
                continue
            if full_digest(dest.read_bytes()) != res["digests"]["exported_sha1_b32"]:
                bad.append(f"digest drift {res['local_path']}")
        check(f"{cutoff}: exported files match exported digests", not bad,
              "; ".join(bad))

        # 2. re-resolve independently from the WARC and compare
        bad = []
        for canon, res in resources.items():
            chain = resolver.resolve_url(canon)
            if chain.gap is not None or chain.resource is None:
                bad.append(f"{canon}: re-resolution failed")
                continue
            fresh = chain.resource
            if fresh.record.record_id != res["selected_record"]["id"]:
                bad.append(f"{canon}: record id mismatch")
            if fresh.payload.digest != res["digests"]["original_sha1_b32"]:
                bad.append(f"{canon}: original digest mismatch")
            data = (site / res["local_path"]).read_bytes()
            if not res["digests"]["rewritten"]:
                if data != fresh.payload.data:
                    bad.append(f"{canon}: exported bytes != captured payload")
                for support_id in fresh.payload.supports:
                    support = store.by_id[support_id]
                    if support.payload != data:
                        bad.append(f"{canon}: bytes differ from support {support_id}")
        check(f"{cutoff}: re-resolution, original digests and shared bytes",
              not bad, "; ".join(bad))

        # 3. HTML closure + no live original-site references
        bad = []
        for res in captures["resources"]:
            if res["kind"] != "html":
                continue
            data = (site / res["local_path"]).read_bytes()
            page_dir = (site / res["local_path"]).parent
            for tag_name, attr, value in _html_refs(data):
                if value.startswith(("http://", "https://")):
                    if origin_of(value) in allowed:
                        bad.append(f"{res['local_path']}: live original-site "
                                   f"reference {value}")
                    continue
                if value.startswith(("#", "javascript:", "mailto:", "data:",
                                     "tel:")):
                    continue
                target, _, frag = value.partition("#")
                if target and not (page_dir / urllib.parse.unquote(target)).resolve().is_file():
                    bad.append(f"{res['local_path']}: unresolved {attr}={value}")
        check(f"{cutoff}: HTML reference closure, no live original-site links",
              not bad, "; ".join(bad))

        # 4. CSS closure
        bad = []
        for res in captures["resources"]:
            if res["kind"] != "css":
                continue
            data = (site / res["local_path"]).read_bytes()
            css_dir = (site / res["local_path"]).parent
            for ref in extract_css_refs(parse_css(data)):
                raw = ref.raw.strip()
                if not raw or raw.startswith(("http://", "https://", "data:")):
                    if raw.startswith(("http://", "https://")) and origin_of(raw) in allowed:
                        bad.append(f"{res['local_path']}: live original-site url {raw}")
                    continue
                if not (css_dir / urllib.parse.unquote(raw)).resolve().is_file():
                    bad.append(f"{res['local_path']}: unresolved css ref {raw}")
        check(f"{cutoff}: CSS reference closure", not bad, "; ".join(bad))

        # 5. fragment targets exist, or are recorded as gaps
        gap_frag_keys = {(g["url"], g["fragment"]) for g in gaps["gaps"]
                         if g["reason"] == Reason.MISSING_FRAGMENT}
        bad = []
        for page in references["pages"]:
            if page["kind"] != "html":
                continue
            for ref in page["refs"]:
                frag = ref.get("fragment")
                if not frag or ref["disposition"] not in ("rewritten", "self-fragment"):
                    continue
                target_path = ref.get("target_path") or page["local_path"]
                target_file = site / target_path
                if not target_file.is_file() or not target_path.endswith(".html"):
                    continue
                if frag in _fragment_targets(target_file.read_bytes()):
                    continue
                canon = ref.get("canon") or page["canon"]
                if (canon, frag) in gap_frag_keys:
                    continue
                bad.append(f"{page['local_path']}: fragment #{frag} of "
                           f"{target_path} missing and not recorded")
        check(f"{cutoff}: fragment targets present or recorded as gaps",
              not bad, "; ".join(bad))

        # 6. gap placeholders exist, carry their reason, keep reasons distinct
        bad = []
        failure_reasons = {}
        for failure in captures["failures"]:
            failure_reasons.setdefault(failure["reason"], []).append(failure["canon"])
            placeholder = site / failure["placeholder_path"]
            if not placeholder.is_file():
                bad.append(f"missing placeholder for {failure['canon']}")
                continue
            body = placeholder.read_bytes().decode("utf-8")
            if failure["reason"] not in body:
                bad.append(f"placeholder {failure['placeholder_path']} lacks reason")
        gap_reasons = {g["reason"] for g in gaps["gaps"]}
        missing_reasons = set(failure_reasons) - gap_reasons
        if missing_reasons:
            bad.append(f"reasons collapsed or missing: {sorted(missing_reasons)}")
        for gap in gaps["gaps"]:
            if not gap["referrers"]:
                bad.append(f"gap {gap['reason']} {gap['url']} has no referrer")
        check(f"{cutoff}: gap placeholders and distinct gap reasons", not bad,
              "; ".join(bad))

        # 7. references.json matches what is actually in the exported HTML
        bad = []
        for page in references["pages"]:
            if page["kind"] != "html":
                continue
            data = (site / page["local_path"]).read_bytes()
            actual = sorted(v for _, _, v in _html_refs(data))
            expected = sorted(
                r["rewritten"] if r.get("rewritten") is not None else r["raw"]
                for r in page["refs"]
            )
            if actual != expected:
                bad.append(f"{page['local_path']}: reference table drift")
        check(f"{cutoff}: reference table matches exported markup", not bad,
              "; ".join(bad))

        if use_http:
            _verify_http(check, cutoff, site, captures, plan)

    # -- cross-cutoff checks ------------------------------------------------
    cutoffs = list(plan["cutoffs"])
    if len(cutoffs) >= 2:
        c1, c2 = cutoffs[0], cutoffs[1]
        _, site1, cap1, _, _ = per_cutoff[c1]
        _, site2, cap2, _, _ = per_cutoff[c2]
        res1 = {r["canon"]: r for r in cap1["resources"]}
        res2 = {r["canon"]: r for r in cap2["resources"]}
        bad = []
        for canon in sorted(set(res1) & set(res2)):
            if res1[canon]["local_path"] != res2[canon]["local_path"]:
                bad.append(f"{canon}: path changed across cutoffs")
        check("cross-cutoff: stable local paths", not bad, "; ".join(bad))
        bad = []
        for canon in sorted(set(res1) & set(res2)):
            r1, r2 = res1[canon], res2[canon]
            b1 = (site1 / r1["local_path"]).read_bytes()
            b2 = (site2 / r2["local_path"]).read_bytes()
            same_record = (r1["selected_record"]["id"] == r2["selected_record"]["id"])
            if same_record and b1 != b2:
                bad.append(f"{canon}: same capture but bytes differ")
            if not same_record and r1["digests"]["original_sha1_b32"] == \
                    r2["digests"]["original_sha1_b32"]:
                bad.append(f"{canon}: different captures share original digest")
        check("cross-cutoff: shared captures share bytes, new captures differ",
              not bad, "; ".join(bad))

    # -- optional inventory cross-check --------------------------------------
    if inventory_path:
        inventory = json.loads(Path(inventory_path).read_text(encoding="utf-8"))
        bad = []
        for entry in inventory:
            rid = entry["record_id"].strip("<>")
            if rid not in store.by_id:
                bad.append(f"inventory record {rid} not in WARC")
        check("inventory: all listed captures present in WARC", not bad,
              "; ".join(bad))

    return checks


def _verify_http(check, cutoff: str, site: Path, captures: dict, plan: dict):
    import functools
    import http.server

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(site)
    )

    class Quiet(handler.func):  # type: ignore[misc]
        def log_message(self, *args):
            pass

    quiet_handler = functools.partial(Quiet, directory=str(site))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), quiet_handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    bad = []
    fetched = 0
    try:
        resources = {r["canon"]: r for r in captures["resources"]}
        queue = []
        seen = set()
        for entry in plan["entry_urls"]:
            canon = canonicalize(entry)
            res = resources.get(canon)
            if res:
                queue.append("/" + res["local_path"])
        while queue:
            path = queue.pop(0)
            if path in seen:
                continue
            seen.add(path)
            try:
                with urllib.request.urlopen(base + path, timeout=10) as resp:
                    body = resp.read()
                    status = resp.status
                    ctype = resp.headers.get("Content-Type", "")
            except Exception as exc:  # noqa: BLE001
                bad.append(f"GET {path}: {exc}")
                continue
            fetched += 1
            if status != 200:
                bad.append(f"GET {path}: status {status}")
                continue
            disk = site / urllib.parse.unquote(path.lstrip("/"))
            if not disk.is_file():
                bad.append(f"GET {path}: no disk file")
                continue
            if disk.read_bytes() != body:
                bad.append(f"GET {path}: HTTP bytes differ from disk")
                continue
            if "text/html" in ctype:
                page_dir = posixpath.dirname(path)
                for _, _, value in _html_refs(body):
                    if value.startswith(("http://", "https://", "#",
                                         "javascript:", "mailto:", "data:")):
                        continue
                    target = urllib.parse.urljoin(page_dir + "/",
                                                  value.partition("#")[0])
                    if target not in seen and target not in queue:
                        queue.append(target)
            elif "text/css" in ctype:
                css_dir = posixpath.dirname(path)
                for ref in extract_css_refs(parse_css(body)):
                    raw = ref.raw.strip()
                    if not raw or raw.startswith(("http://", "https://", "data:")):
                        continue
                    target = urllib.parse.urljoin(css_dir + "/",
                                                  raw.partition("#")[0])
                    if target not in seen and target not in queue:
                        queue.append(target)
        # every gap placeholder must be reachable over HTTP and show its reason
        for failure in captures["failures"]:
            url = base + "/" + failure["placeholder_path"]
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    body = resp.read().decode("utf-8")
                    if resp.status != 200 or failure["reason"] not in body:
                        bad.append(f"placeholder {failure['placeholder_path']} "
                                   f"not served with reason")
            except Exception as exc:  # noqa: BLE001
                bad.append(f"GET placeholder {failure['placeholder_path']}: {exc}")
    finally:
        httpd.shutdown()
        httpd.server_close()
    check(f"{cutoff}: HTTP crawl from entries closes ({fetched} fetches)",
          not bad, "; ".join(bad))


def main_verify(plan_path, warc_path, out_dir, inventory_path=None,
                use_http=True) -> int:
    checks = verify(plan_path, warc_path, out_dir, inventory_path, use_http)
    failed = [c for c in checks if not c.ok]
    for c in checks:
        mark = "PASS" if c.ok else "FAIL"
        line = f"[{mark}] {c.name}"
        if c.detail:
            line += f" -- {c.detail}"
        print(line)
    report = {
        "ok": not failed,
        "checks": [c.to_dict() for c in checks],
    }
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "verify-report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed; "
          f"report at {out / 'verify-report.json'}")
    return 0 if not failed else 1
