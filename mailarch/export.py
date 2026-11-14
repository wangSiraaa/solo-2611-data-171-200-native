"""ingest pipeline: a directory of .eml files -> manifest + assets + HTML.

Determinism: node/asset/conversation identities are content-derived and
all emitted collections are canonically sorted, so shuffling the input
file order yields identical conversation evidence.
"""
from __future__ import annotations

import json
import platform
import re
import shutil
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import __version__
from .mimeparse import MimeTree, parse_bytes, sha256_hex
from .render import render_site
from .threads import build_threads

MAX_RFC822_DEPTH = 10

EXT_MAP = {
    "text/plain": ".txt", "text/html": ".html", "text/csv": ".csv",
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "application/pdf": ".pdf",
    "application/octet-stream": ".bin", "message/rfc822": ".eml",
    "text/calendar": ".ics", "application/zip": ".zip",
    "application/json": ".json",
}

_ID_RE = re.compile(r"<([^<>]+)>")

# issue codes that mark a part as not (fully) processable; reported in
# the manifest report section
PROBLEM_CODES = {
    "decode-failed", "charset-fallback", "defect", "empty-rfc822",
    "rfc822-serialize", "rfc822-depth-exceeded", "max-depth-exceeded",
    "non-text-in-body-position", "empty-multipart", "start-not-found",
    "malformed-message-id", "filename-decode-failed",
}

SUPPORTED = [
    "RFC 822/5322 邮件,经 Python 3.11 标准库 email(policy.default)解析",
    "multipart/mixed、multipart/alternative、multipart/related、嵌套 message/rfc822",
    "Content-Transfer-Encoding: 7bit / 8bit / binary / base64 / quoted-printable",
    "Python 编解码器已知的字符集;未知字符集回退 UTF-8/replace 并记录问题",
    "alternative 正文选择:优先可用 text/html,保留 text/plain 替代",
    "related 根:start 参数 Content-ID,缺省取第一个子项",
    "CID 绑定仅限所属最近 multipart/related 作用域,嵌套作用域不外泄",
    "会话:Message-ID + References/In-Reply-To 证据;缺失父节点占位;"
    "同 ID 字节一致才折叠;冲突、环、自引用均显式记录",
]

UNSUPPORTED = [
    "Python 编解码器未知的历史/私有字符集(回退解码并记录,不保证还原)",
    "S/MIME、PGP 解密与签名校验(相关 part 按不透明附件处理)",
    "TNEF(winmail.dat)解包(按不透明附件处理)",
    "远程 URL 抓取、全文检索、摘要生成、账号与权限系统",
]


def safe_ext(ctype: str) -> str:
    ext = EXT_MAP.get(ctype)
    if ext:
        return ext
    try:
        import mimetypes
        guess = mimetypes.guess_extension(ctype or "")
    except Exception:
        guess = None
    if guess and re.fullmatch(r"\.[a-z0-9]{1,8}", guess):
        return guess
    return ".bin"


def _norm_ids(value) -> list[str]:
    """Extract angle-bracket ids; folding whitespace inside brackets is
    normalized away; the id itself is never case-folded."""
    if not value:
        return []
    out = []
    for m in _ID_RE.finditer(str(value)):
        inner = "".join(m.group(1).split())
        if inner:
            out.append(f"<{inner}>")
    return out


def _header_str(msg, name) -> str:
    try:
        v = msg.get(name)
    except Exception:
        return ""
    return str(v) if v is not None else ""


def _date(msg):
    raw = _header_str(msg, "Date")
    if not raw:
        return None, None
    try:
        return raw, parsedate_to_datetime(raw).isoformat()
    except Exception:
        return raw, None


def _build_node(raw: bytes, node_id: str, kind: str, depth: int,
                registry: dict) -> dict:
    msg = parse_bytes(raw)
    tree = MimeTree(msg, owner=node_id)
    ids = _norm_ids(msg.get("Message-ID"))
    synthesized = not ids
    if synthesized:
        message_id = f"<missing-{sha256_hex(raw)[:16]}@mailarch.invalid>"
        if msg.get("Message-ID") is not None:
            tree.issue("", "malformed-message-id", _header_str(msg, "Message-ID"))
    else:
        message_id = ids[0]
    date_raw, date_iso = _date(msg)
    node = {
        "node_id": node_id,
        "kind": kind,
        "message_id": message_id,
        "message_id_synthesized": synthesized,
        "subject": _header_str(msg, "Subject"),
        "from": _header_str(msg, "From"),
        "to": _header_str(msg, "To"),
        "date_raw": date_raw,
        "date_iso": date_iso,
        "references": _norm_ids(msg.get("References")),
        "in_reply_to": _norm_ids(msg.get("In-Reply-To")),
        "raw_sha256": sha256_hex(raw),
        "digest_note": None if kind == "top" else "canonical-reserialization",
        "source_files": [],
        "body": ({"path": tree.body_path, "format": tree.body_format}
                 if tree.body_path is not None else None),
        "alternatives": list(tree.alternatives),
        "parts": [tree.parts[p].to_dict() for p in tree.order],
        "scopes": tree.scopes,
        "part_scope": tree.part_scope,
        "cid_refs": tree.cid_refs,
        "attached": [],
        "attached_to": None,
        "issues": tree.issues,
        "_tree": tree,
    }
    for path, _child, cbytes in tree.attached:
        if depth >= MAX_RFC822_DEPTH:
            tree.issue(path, "rfc822-depth-exceeded", f"depth>{MAX_RFC822_DEPTH}")
            continue
        aid = "a_" + sha256_hex(
            node_id.encode() + b"|" + path.encode() + b"|" + cbytes)[:16]
        child = _build_node(cbytes, aid, "attached", depth + 1, registry)
        child["attached_to"] = {"message": node_id, "part": path}
        registry[aid] = child
        node["attached"].append({"part": path, "node": aid})
    return node


def ingest(src_dir, out_dir) -> dict:
    src = Path(src_dir)
    out = Path(out_dir)
    if not src.is_dir():
        raise FileNotFoundError(f"source directory not found: {src}")
    files = sorted(src.glob("*.eml"), key=lambda p: p.name)

    nodes: dict[str, dict] = {}
    attached_registry: dict[str, dict] = {}
    file_recs = []
    for f in files:
        raw = f.read_bytes()
        digest = sha256_hex(raw)
        nid = "m_" + digest[:16]
        file_recs.append({"name": f.name, "sha256": digest, "node": nid})
        if nid in nodes:  # byte-identical duplicate collapses
            nodes[nid]["source_files"].append(f.name)
            continue
        node = _build_node(raw, nid, "top", 0, attached_registry)
        node["source_files"] = [f.name]
        nodes[nid] = node

    top_nodes = [nodes[k] for k in sorted(nodes)]
    threads = build_threads(top_nodes)

    out.mkdir(parents=True, exist_ok=True)
    for sub in ("assets", "messages", "threads"):
        d = out / sub
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    # export resources under a stable content-derived identity; decoded
    # filenames are display-only and never used on disk
    assets: dict[str, dict] = {}
    attached_nodes = [attached_registry[k] for k in sorted(attached_registry)]
    all_nodes = top_nodes + attached_nodes
    for node in all_nodes:
        tree = node["_tree"]
        for p in node["parts"]:
            if p["role"] not in ("inline", "attachment", "attached-message"):
                continue
            data = tree.blob.get(p["path"])
            if data is None:
                continue
            name = p["sha256"][:16] + safe_ext(p["declared_type"])
            rel = f"assets/{name}"
            (out / rel).write_bytes(data)
            p["asset"] = rel
            entry = assets.setdefault(name, {
                "path": rel, "sha256": p["sha256"], "size": p["size"],
                "declared_type": p["declared_type"], "owners": []})
            owner = {"message": node["node_id"], "part": p["path"],
                     "role": p["role"]}
            if owner not in entry["owners"]:
                entry["owners"].append(owner)

    mime_issues = []
    for node in all_nodes:
        for i in node["issues"]:
            mime_issues.append({"message": node["node_id"], **i})
    mime_issues.sort(key=lambda d: (d["message"], d["part"], d["code"], d["detail"]))

    report = {
        "supported": SUPPORTED,
        "unsupported": UNSUPPORTED,
        "problem_parts": [dict(i) for i in mime_issues
                          if i["code"] in PROBLEM_CODES],
    }

    render_site(out, top_nodes, attached_nodes, threads, report,
                len(mime_issues))

    def public(node):
        return {k: v for k, v in node.items() if not k.startswith("_")}

    manifest = {
        "tool": {"name": "mailarch", "version": __version__,
                 "python": platform.python_version(),
                 "generated_at": datetime.now(timezone.utc).isoformat()},
        "source": {"dir": str(src), "pattern": "*.eml",
                   "file_count": len(file_recs)},
        "files": sorted(file_recs, key=lambda r: r["name"]),
        "messages": [public(nodes[k]) for k in sorted(nodes)],
        "attached_messages": [public(n) for n in attached_nodes],
        "threads": {k: threads[k]
                    for k in ("conversations", "edges", "diagnostics", "placeholders")},
        "assets": [assets[k] for k in sorted(assets)],
        "mime_issues": mime_issues,
        "report": report,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return manifest
