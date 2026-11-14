"""Independent verification of an exported archive against its manifest.

Works from the export manifest plus the files on disk (it does not
re-parse the mailboxes): every referenced resource is checked against
its source part and byte digest, pages and assets are accounted for,
conversation evidence is recomputed, and the generated HTML is scanned
for active content.  With --src the original .eml files are also
re-digested and matched.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from .mimeparse import sha256_hex

HTML_FORBIDDEN = ("<script", 'src="http://', 'src="https://', "srcset=")


def verify(out_dir, src_dir=None):
    problems: list[str] = []
    out = Path(out_dir)
    mpath = out / "manifest.json"
    if not mpath.exists():
        return False, [f"manifest not found: {mpath}"]
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, [f"manifest unreadable: {exc}"]

    # -- assets: presence, byte digest, size; no stray files --------------
    assets = {a["path"]: a for a in manifest.get("assets", [])}
    for a in manifest.get("assets", []):
        fp = out / a["path"]
        if not fp.exists():
            problems.append(f"asset missing on disk: {a['path']}")
            continue
        data = fp.read_bytes()
        if sha256_hex(data) != a["sha256"]:
            problems.append(f"asset digest mismatch: {a['path']}")
        if len(data) != a["size"]:
            problems.append(f"asset size mismatch: {a['path']}")
    assets_dir = out / "assets"
    if assets_dir.is_dir():
        for fp in sorted(assets_dir.iterdir()):
            if f"assets/{fp.name}" not in assets:
                problems.append(f"unexpected file in assets/: {fp.name}")

    # -- pages --------------------------------------------------------------
    msgs = manifest.get("messages", []) + manifest.get("attached_messages", [])
    threads = manifest.get("threads", {})
    expected = ["index.html"]
    expected += [f"threads/{c['conv_id']}.html"
                 for c in threads.get("conversations", [])]
    expected += [f"messages/{m['node_id']}.html" for m in msgs]
    for rel in expected:
        if not (out / rel).exists():
            problems.append(f"page missing: {rel}")

    # -- every referenced resource: source part and byte digest -------------
    for m in msgs:
        parts = {p["path"]: p for p in m.get("parts", [])}
        for p in m.get("parts", []):
            if not p.get("asset"):
                continue
            a = assets.get(p["asset"])
            if a is None:
                problems.append(f"{m['node_id']} part {p['path']}: "
                                f"asset {p['asset']} not in manifest")
            elif a["sha256"] != p["sha256"]:
                problems.append(f"{m['node_id']} part {p['path']}: "
                                f"part digest != asset digest")
        for ref in m.get("cid_refs", []):
            bound = ref.get("bound_to")
            if bound is None:
                continue  # visible gap in the HTML, nothing to verify
            p = parts.get(bound)
            if p is None:
                problems.append(f"{m['node_id']}: cid {ref['cid']} bound to "
                                f"unknown part {bound}")
            elif not p.get("asset"):
                problems.append(f"{m['node_id']}: cid {ref['cid']} bound part "
                                f"{bound} has no exported asset")
            elif assets.get(p["asset"], {}).get("sha256") != p["sha256"]:
                problems.append(f"{m['node_id']}: cid {ref['cid']} bound part "
                                f"{bound} digest mismatch")

    # -- attached messages trace back to their source part -------------------
    by_id = {m["node_id"]: m for m in msgs}
    for m in manifest.get("attached_messages", []):
        at = m.get("attached_to") or {}
        parent = by_id.get(at.get("message"))
        if parent is None:
            problems.append(f"{m['node_id']}: owner message missing")
            continue
        pparts = {p["path"]: p for p in parent["parts"]}
        src_part = pparts.get(at.get("part"))
        if src_part is None or src_part.get("sha256") != m["raw_sha256"]:
            problems.append(f"{m['node_id']}: digest does not match source "
                            f"part {at.get('part')} of {at.get('message')}")

    # -- conversation evidence ------------------------------------------------
    node_ids = {m["node_id"] for m in manifest.get("messages", [])}
    ghosts = {g["node_id"] for g in threads.get("placeholders", [])}
    for c in threads.get("conversations", []):
        expect = "c_" + hashlib.sha256(
            "\n".join(sorted(c["members"])).encode("utf-8")).hexdigest()[:12]
        if c["conv_id"] != expect:
            problems.append(f"conversation id mismatch: {c['conv_id']}")
        for mem in c["members"]:
            if mem not in node_ids and mem not in ghosts:
                problems.append(f"conversation {c['conv_id']}: unknown member {mem}")
    for e in threads.get("edges", []):
        if e["child"] not in node_ids:
            problems.append(f"edge from unknown node {e['child']}")
        if e.get("parent") and e["parent"] not in node_ids | ghosts:
            problems.append(f"edge to unknown parent {e['parent']}")
        for cand in e.get("ambiguous", []):
            if cand not in node_ids:
                problems.append(f"ambiguous candidate {cand} not a node")
    seen_raw = {}
    for m in manifest.get("messages", []):
        if m["raw_sha256"] in seen_raw:
            problems.append(f"collapse invariant violated: {m['node_id']} and "
                            f"{seen_raw[m['raw_sha256']]} share raw bytes")
        seen_raw[m["raw_sha256"]] = m["node_id"]
    counts = Counter(m["message_id"] for m in manifest.get("messages", []))
    conflict_ids = {k for k, v in counts.items() if v > 1}
    diag_conflicts = {d["message_id"] for d in threads.get("diagnostics", [])
                      if d["type"] == "id-conflict"}
    if conflict_ids != diag_conflicts:
        problems.append(f"id-conflict diagnostics mismatch: "
                        f"{sorted(conflict_ids)} vs {sorted(diag_conflicts)}")
    for g in threads.get("placeholders", []):
        if g.get("source_files"):
            problems.append(f"placeholder {g['node_id']} must not have sources")

    # -- generated HTML must stay inert ----------------------------------------
    for fp in sorted(out.rglob("*.html")):
        text = fp.read_text(encoding="utf-8").lower()
        for bad in HTML_FORBIDDEN:
            if bad in text:
                problems.append(f"unsafe content in {fp.relative_to(out)}: {bad}")

    # -- optional source re-check ------------------------------------------------
    if src_dir is not None:
        src = Path(src_dir)
        names = set()
        for fr in manifest.get("files", []):
            names.add(fr["name"])
            fp = src / fr["name"]
            if not fp.exists():
                problems.append(f"source file missing: {fr['name']}")
            elif sha256_hex(fp.read_bytes()) != fr["sha256"]:
                problems.append(f"source digest mismatch: {fr['name']}")
        if src.is_dir():
            for fp in sorted(src.glob("*.eml")):
                if fp.name not in names:
                    problems.append(f"source file not ingested: {fp.name}")

    return (not problems), problems
