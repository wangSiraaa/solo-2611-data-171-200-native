#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
release_audit.py — 离线发布审计工具

命令:
  audit   解析 Git bundle 与发布计划, 产出:
            - netdiff.json   净差量包(自包含, 可独立重放)
            - netdiff.patch  净差量的 unified diff 形式(供人工复核)
            - audit.json     全量审计数据(历史、来源证据、判定理由)
            - report.html    可复核 HTML 报告
  verify  从 base 树在独立临时目录重放 netdiff.json, 逐文件核对 target 树身份,
          产出 verify.json 并把验证结果并入 report.html

约束: 只读取输入 bundle(克隆到临时目录), 不修改输入仓库, 不推送。
仅依赖 Python 3.11 标准库与 git 命令行(读取对象/diff)。
"""
from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import html
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from pathlib import Path

RENAME_THRESHOLD = 0.55   # 内容相似度阈值(difflib ratio), 确定性策略, 同等候选保留
SIM_EPS = 1e-9            # “同等候选”的判定容差

# 计划 source_statuses 词汇映射(moved_base_content 为对枚举的扩展:
# 内容来自 base 的移动/复制, 非新交付, 给定仓库不出现)
STATUS_MAP = {"single": "single_branch",
              "independent": "multiple_candidate_origins",
              "merge_resolution": "merge_resolution",
              "unattributed": "unresolved",
              "moved": "moved_base_content"}


# ---------------------------------------------------------------- git 读取层
def run_git(repo, *args, text=True):
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=text)
    if r.returncode != 0:
        err = r.stderr if text else r.stderr.decode("utf-8", "replace")
        raise RuntimeError(f"git {' '.join(args)} 失败: {err.strip()}")
    return r.stdout


def clone_bundle(bundle: str, dest: str):
    """克隆 bundle 到 dest。优先 git clone, 失败则 init+fetch(不依赖 HEAD)。"""
    bundle = str(Path(bundle).resolve())
    r = subprocess.run(["git", "clone", "-q", "-n", bundle, dest],
                       capture_output=True, text=True)
    if r.returncode != 0:
        os.makedirs(dest, exist_ok=True)
        run_git(dest, "init", "-q")
        run_git(dest, "fetch", "-q", bundle, "refs/*:refs/*")


class Repo:
    def __init__(self, path):
        self.path = str(path)

    def rev_parse(self, ref):
        return run_git(self.path, "rev-parse", f"{ref}^{{commit}}").strip()

    def meta(self, c):
        out = run_git(self.path, "log", "-1",
                      "--format=%H%x00%P%x00%an%x00%ad%x00%s",
                      "--date=short", c)
        h, p, a, d, s = out.rstrip("\n").split("\x00", 4)
        return {"sha": h, "parents": p.split() if p else [],
                "author": a, "date": d, "subject": s}

    def rev_list(self, a, b):
        """b 可达而 a 不可达的提交, topo 序, 最旧在前。"""
        out = run_git(self.path, "rev-list", "--reverse", "--topo-order",
                      f"{a}..{b}")
        return out.split()

    def merge_base(self, a, b):
        return run_git(self.path, "merge-base", a, b).strip()

    def ls_tree(self, c):
        out = run_git(self.path, "ls-tree", "-r", c)
        tree = {}
        for line in out.splitlines():
            meta, path = line.split("\t", 1)
            mode, _typ, sha = meta.split()
            tree[path] = (mode, sha)
        return tree

    def cat(self, sha) -> bytes:
        return run_git(self.path, "cat-file", "blob", sha, text=False)

    def archive(self, sha) -> bytes:
        return run_git(self.path, "archive", sha, text=False)


# ---------------------------------------------------------------- 文本工具
def is_binary(data: bytes) -> bool:
    return b"\0" in data[:8000]


def is_binary_path(path, data: bytes, plan) -> bool:
    """优先使用发布计划给出的文件分类, 未列出的回退到 NUL 启发式。"""
    if path in set(plan.get("binary_files", [])):
        return True
    if path in set(plan.get("text_files", [])):
        return False
    return is_binary(data)


def split_text(data: bytes, encoding="utf-8"):
    """返回 (行列表, 是否以换行结尾)。"""
    text = data.decode(encoding, errors="replace")
    lines = text.split("\n")
    final_nl = bool(lines) and lines[-1] == ""
    if final_nl:
        lines.pop()
    return lines, final_nl


def join_text(lines, final_nl):
    if not lines:
        return ""
    return "\n".join(lines) + ("\n" if final_nl else "")


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def short(s):
    return s[:10] if s else s


def line_fragments(a_lines, b_lines):
    """自己的行对齐: difflib opcodes -> 片段列表。
    base_start 为 1 起始行号; base_count 为被替换/删除的行数(0 表示纯插入)。"""
    sm = difflib.SequenceMatcher(a=a_lines, b=b_lines, autojunk=False)
    frags = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        frags.append({
            "base_start": i1 + 1, "base_count": i2 - i1,
            "target_start": j1 + 1,
            "deleted": a_lines[i1:i2], "added": b_lines[j1:j2],
        })
    return frags


def find_block(lines, block):
    """block 作为连续行序列出现的所有 1 起始行号。"""
    n = len(block)
    if n == 0:
        return []
    first = block[0]
    return [i + 1 for i, ln in enumerate(lines)
            if ln == first and lines[i:i + n] == block]


def similarity(a: bytes, b: bytes) -> float:
    ta = a.decode("utf-8", errors="replace")
    tb = b.decode("utf-8", errors="replace")
    return difflib.SequenceMatcher(None, ta, tb).ratio()


# ---------------------------------------------------------------- 存在性搜索
class Presence:
    """在整个提交树上做内容块存在性搜索(不限于同一路径), 结果带路径证据。"""

    def __init__(self, repo, plan):
        self.repo = repo
        self.plan = plan
        self.cache = {}

    def tree_lines(self, c):
        if c not in self.cache:
            out = {}
            enc = self.plan.get("text_encoding", "utf-8")
            for path, (_mode, sha) in self.repo.ls_tree(c).items():
                data = self.repo.cat(sha)
                if is_binary_path(path, data, self.plan):
                    continue
                out[path] = split_text(data, enc)[0]
            self.cache[c] = out
        return self.cache[c]

    def find(self, c, block):
        hits = []
        for path, lines in self.tree_lines(c).items():
            for start in find_block(lines, block):
                hits.append((path, start))
        return sorted(hits)


# ---------------------------------------------------------------- 净差量
def detect_renames(repo, plan, bt, tt, deleted, added):
    """重命名: 对每个被删文本文件, 在新增文件中找相似度 >= 阈值的候选。
    唯一最佳 -> 重命名; 多个同等最佳 -> 保留歧义(仍按删除+新增处理)。"""
    renames, ambiguous = [], []
    claimed = set()
    for d in deleted:
        ddata = repo.cat(bt[d][1])
        if is_binary_path(d, ddata, plan):
            continue
        best, cands = None, []
        for a in added:
            adata = repo.cat(tt[a][1])
            if is_binary_path(a, adata, plan):
                continue
            r = similarity(ddata, adata)
            if r < RENAME_THRESHOLD:
                continue
            if best is None or r > best + SIM_EPS:
                best, cands = r, [a]
            elif abs(r - best) <= SIM_EPS:
                cands.append(a)
        if not cands:
            continue
        if len(cands) == 1 and cands[0] not in claimed:
            renames.append({"old_path": d, "new_path": cands[0],
                            "similarity": round(best, 4)})
            claimed.add(cands[0])
        else:
            ambiguous.append({"deleted": d,
                              "candidates": [{"path": c,
                                              "similarity": round(best, 4)}
                                             for c in cands]})
    return renames, ambiguous


def make_text_entry(repo, plan, bt, tt, old_path, new_path, change):
    omode, osha = bt[old_path]
    nmode, nsha = tt[new_path]
    odata, ndata = repo.cat(osha), repo.cat(nsha)
    entry = {"change": change, "path": new_path, "old_path": old_path,
             "base_blob": osha, "target_blob": nsha,
             "base_mode": omode, "target_mode": nmode, "binary": False}
    if is_binary_path(old_path, odata, plan) or \
            is_binary_path(new_path, ndata, plan):
        entry["binary"] = True
        entry["fragments"] = []
        # 二进制无行级片段, 差量包携带目标内容以便独立重放
        entry["content_b64"] = base64.b64encode(ndata).decode("ascii")
        entry["note"] = "二进制文件: 仅报告 blob 身份"
        return entry
    enc = plan.get("text_encoding", "utf-8")
    ol, _ = split_text(odata, enc)
    nl, _ = split_text(ndata, enc)
    entry["fragments"] = line_fragments(ol, nl)
    return entry


def make_add_entry(repo, plan, tt, path):
    nmode, nsha = tt[path]
    data = repo.cat(nsha)
    entry = {"change": "add", "path": path, "old_path": None,
             "base_blob": None, "target_blob": nsha,
             "base_mode": None, "target_mode": nmode,
             "content_b64": base64.b64encode(data).decode("ascii"),
             "size": len(data)}
    if is_binary_path(path, data, plan):
        entry["binary"] = True
        entry["fragments"] = []
        entry["note"] = "二进制文件: 仅报告 blob 身份"
    else:
        entry["binary"] = False
        lines, _ = split_text(data, plan.get("text_encoding", "utf-8"))
        entry["fragments"] = [{"base_start": 1, "base_count": 0,
                               "target_start": 1,
                               "deleted": [], "added": lines}]
    return entry


def make_delete_entry(repo, plan, bt, path):
    omode, osha = bt[path]
    data = repo.cat(osha)
    entry = {"change": "delete", "path": path, "old_path": None,
             "base_blob": osha, "target_blob": None,
             "base_mode": omode, "target_mode": None}
    if is_binary_path(path, data, plan):
        entry["binary"] = True
        entry["fragments"] = []
        entry["note"] = "二进制文件: 仅报告 blob 身份"
    else:
        entry["binary"] = False
        lines, _ = split_text(data, plan.get("text_encoding", "utf-8"))
        entry["fragments"] = [{"base_start": 1, "base_count": len(lines),
                               "target_start": 1,
                               "deleted": lines, "added": []}]
    return entry


# ---------------------------------------------------------------- 来源归因
class Ctx:
    def __init__(self, repo, plan, commits, metas, base, target):
        self.repo = repo
        self.plan = plan
        self.commits = commits
        self.metas = metas
        self.base = base
        self.target = target
        self.presence = Presence(repo, plan)


def _merge_rows(ctx, P):
    rows = []
    for c in ctx.commits:
        pars = ctx.metas[c]["parents"]
        if len(pars) < 2:
            continue
        prow = [{"sha": p, "present": bool(P(p))} for p in pars]
        if not (P(c) or any(r["present"] for r in prow)):
            continue
        mb = ctx.repo.merge_base(pars[0], pars[1]) if len(pars) == 2 else None
        rows.append({"merge": c, "in_result": bool(P(c)), "parents": prow,
                     "merge_base": mb,
                     "in_merge_base": (bool(P(mb)) if mb else None)})
    return rows


def attribute_added(block, ctx):
    """新增片段来源: 在发布区间全部提交上搜索引入边(父无->子有), 不取首个命中。"""
    cache = {}

    def P(c):
        if c not in cache:
            cache[c] = ctx.presence.find(c, block)
        return cache[c]

    intros = []
    for c in ctx.commits:
        if not P(c):
            continue
        pars = ctx.metas[c]["parents"]
        if all(not P(p) for p in pars):
            intros.append(c)

    merge_rows = _merge_rows(ctx, P)
    base_loc, target_loc = P(ctx.base), P(ctx.target)
    metas = ctx.metas

    if len(intros) == 1:
        c = intros[0]
        if len(metas[c]["parents"]) > 1:
            kind = "merge_resolution"
            rationale = (f"该文本在合并提交 {short(c)} 的全部父版本中均不存在, "
                         f"首次出现于合并结果 ⇒ 手工合并解决阶段引入。")
        else:
            kind = "single"
            p = metas[c]["parents"][0] if metas[c]["parents"] else None
            rationale = (f"父边对照: {short(p)} 中不存在 → {short(c)} 中出现; "
                         f"全区间仅此一条引入边 ⇒ 由提交 {short(c)}"
                         f"（{metas[c]['subject']}）引入。")
    elif len(intros) > 1:
        kind = "independent"
        lst = "、".join(f"{short(c)}（{metas[c]['subject']}）" for c in intros)
        rationale = (f"相同文本在 {len(intros)} 个提交中各自首次出现"
                     f"（其父版本均不含）: {lst}。"
                     f" ⇒ 多分支独立产生相同文本, 不能凭首次命中归因, 保留候选集。")
    elif base_loc:
        kind = "moved"
        locs = "、".join(f"{p}:{l}" for p, l in base_loc)
        rationale = (f"相同文本在 base 树已存在（{locs}）"
                     f" ⇒ 属于移动/复制, 而非新引入内容。")
    else:
        kind = "unattributed"
        rationale = "未在发布区间找到引入边, base 中亦不存在（启发式限制, 需人工核查）。"

    if intros and base_loc:
        locs = "、".join(f"{p}:{l}" for p, l in base_loc)
        rationale += f" 注意: 相同文本在 base 亦存在（{locs}）, 属重复文本, 以上按引入边判定。"

    return {"kind": kind, "status": STATUS_MAP[kind],
            "introduction_commits": intros,
            "merge_rows": merge_rows,
            "base_locations": base_loc, "target_locations": target_loc,
            "rationale": rationale}


def attribute_deleted(block, ctx):
    """删除片段来源: 搜索消失边(父有->子无); 始终保留 base 中的路径与范围。
    合并边的消失若可由区间内更早的非合并删除解释, 记为"沿用"而非合并解决。"""
    cache = {}

    def P(c):
        if c not in cache:
            cache[c] = ctx.presence.find(c, block)
        return cache[c]

    events = []        # 非合并提交上的消失事件
    merge_edges = []   # 合并提交上的消失边
    for c in ctx.commits:
        pc = bool(P(c))
        pars = ctx.metas[c]["parents"]
        if len(pars) <= 1:
            for p in pars:
                if P(p) and not pc:
                    events.append(c)
        elif not pc:
            for p in pars:
                if P(p):
                    merge_edges.append({"commit": c, "parent": p})

    merge_rows = _merge_rows(ctx, P)
    still = P(ctx.target)
    metas = ctx.metas
    events = list(dict.fromkeys(events))

    if events:
        uniq = events
        if len(uniq) == 1:
            kind = "single"
            c = uniq[0]
            p = metas[c]["parents"][0]
            rationale = (f"父边对照: {short(p)} 中存在 → {short(c)} 中消失; "
                         f"全区间仅此一条非合并消失边 ⇒ 由提交 {short(c)}"
                         f"（{metas[c]['subject']}）删除。")
        else:
            kind = "independent"
            lst = "、".join(f"{short(c)}（{metas[c]['subject']}）" for c in uniq)
            rationale = (f"同一段内容被 {len(uniq)} 个提交分别删除: {lst}。"
                         f" ⇒ 不能凭单一命中归因, 保留候选集。")
        if merge_edges:
            inh = "、".join(f"{short(e['commit'])}(父 {short(e['parent'])})"
                            for e in merge_edges)
            rationale += (f" 合并边 {inh} 上的消失沿用上述删除"
                          f"（内容在该合并的另一父分支已被删除）, 非合并解决。")
        commits = uniq
    elif merge_edges:
        kind = "merge_resolution"
        commits = list(dict.fromkeys(e["commit"] for e in merge_edges))
        lst = "、".join(f"{short(e['commit'])}(父 {short(e['parent'])})"
                        for e in merge_edges)
        rationale = (f"区间内不存在其他删除事件; 内容在合并父版本中存在、"
                     f"合并结果中消失（边 {lst}）"
                     f" ⇒ 在合并解决阶段被删除。")
    elif still:
        kind = "moved"
        commits = []
        locs = "、".join(f"{p}:{l}" for p, l in still)
        rationale = (f"内容在 target 树仍存在于 {locs}"
                     f" ⇒ 文件虽被删除, 内容并未真正消失（移动/复制）。")
    else:
        kind = "unattributed"
        commits = []
        rationale = "未找到消失边（启发式限制, 需人工核查）。"

    return {"kind": kind, "status": STATUS_MAP[kind],
            "deletion_commits": commits, "merge_edges": merge_edges,
            "merge_rows": merge_rows, "still_present": still,
            "rationale": rationale}


# ---------------------------------------------------------------- 历史分析
def blob_lines(repo, plan, sha):
    data = repo.cat(sha)
    if is_binary(data):
        return None
    return split_text(data, plan.get("text_encoding", "utf-8"))[0]


def tree_line_changes(repo, plan, old, new):
    """两棵树路径级对比(不做重命名跟踪), 返回 (新增行列表, 删除行列表)。"""
    ot, nt = repo.ls_tree(old), repo.ls_tree(new)
    added, removed = [], []
    for p in sorted(set(ot) | set(nt)):
        o, n = ot.get(p), nt.get(p)
        if o == n:
            continue
        ol = blob_lines(repo, plan, o[1]) if o else []
        nl = blob_lines(repo, plan, n[1]) if n else []
        if ol is None or nl is None:
            continue  # 二进制: 不做行级统计
        for f in line_fragments(ol, nl):
            removed += f["deleted"]
            added += f["added"]
    return added, removed


def analyze_history(repo, plan, commits, metas, target):
    """历史新增提交集合: 逐提交统计与第一父的差异, 标注撤销/疑似还原。"""
    target_counter = Counter()
    for lines in Presence(repo, plan).tree_lines(target).values():
        target_counter.update(lines)

    rows, earlier_added = [], []
    for c in commits:
        m = metas[c]
        row = {"sha": c, "subject": m["subject"], "author": m["author"],
               "date": m["date"], "parents": m["parents"],
               "is_merge": len(m["parents"]) > 1,
               "adds": None, "dels": None, "annotations": []}
        if row["is_merge"]:
            rows.append(row)
            continue
        p = m["parents"][0]
        added, removed = tree_line_changes(repo, plan, p, c)
        row["adds"], row["dels"] = len(added), len(removed)
        nb_added = [l for l in added if l.strip()]
        nb_removed = [l for l in removed if l.strip()]
        if nb_added and not any(target_counter.get(l) for l in nb_added):
            row["annotations"].append(
                "本提交引入的行在 target 中均不存在（被后续修改覆盖或撤销）, "
                "仅作历史说明, 不计入净变更")
        cr = Counter(nb_removed)
        if cr:
            for esha, ecounter in earlier_added:
                if all(ecounter[k] >= v for k, v in cr.items()):
                    row["annotations"].append(
                        f"移除的行与 {short(esha)} 引入的行一致"
                        f" ⇒ 疑似撤销该提交的修改")
                    break
        # 纯移动/重命名提交(新增与删除行多重集相同)不参与撤销判定
        if Counter(nb_added) != Counter(nb_removed):
            earlier_added.append((c, Counter(nb_added)))
        rows.append(row)
    return rows


def analyze_merges(repo, plan, commits, metas):
    out = []
    for c in commits:
        m = metas[c]
        if len(m["parents"]) < 2:
            continue
        ps = []
        for p in m["parents"]:
            added, removed = tree_line_changes(repo, plan, p, c)
            ps.append({"sha": p, "adds": len(added), "dels": len(removed)})
        mb = repo.merge_base(m["parents"][0], m["parents"][1]) \
            if len(m["parents"]) == 2 else None
        out.append({"sha": c, "subject": m["subject"], "parents": ps,
                    "merge_base": mb})
    return out


# ---------------------------------------------------------------- 报告渲染
CSS = """
body{font-family:-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei",sans-serif;margin:2rem;color:#24292f;line-height:1.5}
h1{font-size:1.5rem} h2{font-size:1.2rem;border-bottom:1px solid #d0d7de;padding-bottom:.3rem;margin-top:2.2rem}
h3{font-size:1.02rem;margin-bottom:.3rem}
table{border-collapse:collapse;margin:.6rem 0;font-size:.9rem}
th,td{border:1px solid #d0d7de;padding:.3rem .6rem;text-align:left;vertical-align:top}
th{background:#f6f8fa}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.85em}
.file{border:1px solid #d0d7de;border-radius:6px;margin:1rem 0;padding:.8rem 1rem}
.frag{background:#f6f8fa;border-radius:4px;padding:.4rem .6rem;margin:.5rem 0;overflow-x:auto;white-space:pre;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.82rem}
.add{background:#e6ffec;color:#116329}.del{background:#ffebe9;color:#82071e}
.hdr{color:#57606a}
.attr{border-left:3px solid #0969da;background:#f6f8fa;padding:.4rem .8rem;margin:.4rem 0;font-size:.88rem}
.attr.delattr{border-left-color:#cf222e}
.badge{display:inline-block;border-radius:10px;padding:0 .55rem;font-size:.78rem;margin-left:.4rem}
.b-single{background:#ddf4e4;color:#116329}.b-merge_resolution{background:#fff8c5;color:#7d4e00}
.b-independent{background:#ffebe9;color:#82071e}.b-moved{background:#ddf4ff;color:#0550ae}
.b-unattributed{background:#eaeef2;color:#57606a}
.ok{color:#116329}.fail{color:#82071e;font-weight:600}
.note{color:#57606a;font-size:.85rem}
"""


def esc(s):
    return html.escape(str(s), quote=True)


def badge(att):
    kind = att["kind"]
    status = att.get("status", kind)
    return (f'<span class="badge b-{esc(kind)}">{esc(status)}</span>'
            f'<span class="note">（{esc(kind)}）</span>')


def render_merge_rows(rows, metas):
    if not rows:
        return ""
    h = ['<table><tr><th>合并提交</th><th>父版本</th><th>父中是否存在</th>'
         '<th>merge-base</th><th>合并结果</th></tr>']
    for r in rows:
        mb = f"{short(r['merge_base'])}:{'有' if r['in_merge_base'] else '无'}" \
             if r["merge_base"] else "—"
        for pr in r["parents"]:
            h.append(f"<tr><td class='mono'>{esc(short(r['merge']))} "
                     f"{esc(metas[r['merge']]['subject'])}</td>"
                     f"<td class='mono'>{esc(short(pr['sha']))}</td>"
                     f"<td>{'有' if pr['present'] else '无'}</td>"
                     f"<td class='mono'>{mb}</td>"
                     f"<td>{'有' if r['in_result'] else '无'}</td></tr>")
    h.append("</table>")
    return "".join(h)


def render_attr(title, att, metas, cls="attr"):
    h = [f"<div class='{cls}'><b>{esc(title)}</b>{badge(att)}<br>",
         esc(att["rationale"])]
    commits = att.get("introduction_commits") or att.get("deletion_commits")
    if commits:
        h.append("<br>涉及提交: " + ", ".join(
            f"<code>{esc(short(c))}</code> {esc(metas[c]['subject'])}"
            for c in commits))
    if att.get("base_path"):
        s, e = att["base_range"]
        h.append(f"<br>base 位置: <code>{esc(att['base_path'])}</code> "
                 f"第 {s}–{e} 行")
    h.append(render_merge_rows(att.get("merge_rows"), metas))
    h.append("</div>")
    return "".join(h)


def render_fragment(fr, metas):
    h = [f"<div class='frag'><span class='hdr'>@@ base 第{fr['base_start']}行"
         f"起 -{fr['base_count']} → target 第{fr['target_start']}行起 "
         f"+{len(fr['added'])} @@</span>"]
    for l in fr["deleted"]:
        h.append(f"<span class='del'>- {esc(l)}</span>")
    for l in fr["added"]:
        h.append(f"<span class='add'>+ {esc(l)}</span>")
    h.append("</div>")
    att = fr.get("attribution") or {}
    if att.get("added"):
        h.append(render_attr("新增行来源判定", att["added"], metas))
    if att.get("deleted"):
        h.append(render_attr("删除行来源判定", att["deleted"], metas,
                             cls="attr delattr"))
    return "".join(h)


def render_netdiff(entries, ambiguous, metas):
    h = []
    for amb in ambiguous:
        cands = "、".join(f"<code>{esc(c['path'])}</code>"
                          f"(相似度 {c['similarity']})" for c in amb["candidates"])
        h.append(f"<div class='attr'>⚠ 歧义重命名: <code>{esc(amb['deleted'])}"
                 f"</code> 被删除, 存在多个同等相似度候选 {cands}"
                 f" ⇒ 保留歧义, 按删除+新增处理。</div>")
    for e in entries:
        if e["change"] == "rename":
            head = (f"重命名 <code>{esc(e['old_path'])}</code> → "
                    f"<code>{esc(e['path'])}</code> "
                    f"(相似度 {e.get('similarity')})")
        elif e["change"] == "add":
            head = f"新增 <code>{esc(e['path'])}</code>"
        elif e["change"] == "delete":
            head = f"删除 <code>{esc(e['path'])}</code>"
        else:
            head = f"修改 <code>{esc(e['path'])}</code>"
        blobs = (f"base blob <code>{esc(short(e.get('base_blob')))}</code> → "
                 f"target blob <code>{esc(short(e.get('target_blob')))}</code>")
        h.append(f"<div class='file'><h3>{head}</h3><div class='note'>{blobs}"
                 f"　模式 {esc(e.get('base_mode') or '—')} → "
                 f"{esc(e.get('target_mode') or '—')}</div>")
        if e.get("binary"):
            size = e.get("size")
            extra = f", {size} 字节" if size is not None else ""
            h.append(f"<div class='note'>二进制文件: 仅报告 blob 身份"
                     f"{extra}, 不做行级片段。</div>")
        for fr in e.get("fragments", []):
            h.append(render_fragment(fr, metas))
        h.append("</div>")
    return "".join(h)


def render_history(rows):
    h = ["<table><tr><th>提交</th><th>主题</th><th>作者</th><th>日期</th>"
         "<th>父</th><th>+/-</th><th>历史说明</th></tr>"]
    for r in rows:
        parents = ", ".join(short(p) for p in r["parents"]) or "—"
        pm = "（合并）" if r["is_merge"] else ""
        stat = "—" if r["adds"] is None else f"+{r['adds']}/−{r['dels']}"
        ann = "<br>".join(esc(a) for a in r["annotations"]) or ""
        h.append(f"<tr><td class='mono'>{esc(short(r['sha']))}{pm}</td>"
                 f"<td>{esc(r['subject'])}</td><td>{esc(r['author'])}</td>"
                 f"<td>{esc(r['date'])}</td><td class='mono'>{esc(parents)}</td>"
                 f"<td>{esc(stat)}</td><td>{ann}</td></tr>")
    h.append("</table>")
    return "".join(h)


def render_merges(merges):
    if not merges:
        return "<p class='note'>区间内无合并提交。</p>"
    h = []
    for m in merges:
        h.append(f"<h3>合并 <code>{esc(short(m['sha']))}</code> "
                 f"{esc(m['subject'])}</h3><table><tr><th>父版本</th>"
                 "<th>相对该父的差异(合并引入)</th></tr>")
        for p in m["parents"]:
            h.append(f"<tr><td class='mono'>{esc(short(p['sha']))}</td>"
                     f"<td>+{p['adds']}/−{p['dels']}</td></tr>")
        h.append(f"<tr><td>merge-base</td><td class='mono'>"
                 f"{esc(short(m['merge_base']) if m['merge_base'] else '—')}"
                 f"</td></tr></table>")
    return "".join(h)


def render_verify(verify):
    if verify is None:
        return ("<p class='note'>尚未运行 verify。执行 "
                "<code>release_audit.py verify --bundle … --plan … --out …</code>"
                " 后, 本节将显示逐文件重放核对结果。</p>")
    status = ("<span class='ok'>✔ 全部一致</span>" if verify["ok"]
              else "<span class='fail'>✘ 存在不一致</span>")
    h = [f"<p>重放结果: {status}　(从 base 树 + netdiff.json 在独立临时目录"
         f"重建, 与 target 树逐文件比对 blob 身份)</p>",
         "<table><tr><th>文件</th><th>期望 blob(target)</th>"
         "<th>实际 blob(重放)</th><th>结论</th></tr>"]
    for f in verify["files"]:
        ok = f["status"] == "ok"
        h.append(f"<tr><td class='mono'>{esc(f['path'])}</td>"
                 f"<td class='mono'>{esc(short(f.get('expected')))}</td>"
                 f"<td class='mono'>{esc(short(f.get('actual')))}</td>"
                 f"<td class='{ 'ok' if ok else 'fail'}'>"
                 f"{'一致' if ok else esc(f['status'])}</td></tr>")
    h.append("</table>")
    return "".join(h)


METHODOLOGY = """
<ol>
<li>净变更仅来自 base→target 最终树差异; 历史新增提交集合单独列出, 发布前被撤销的修改只在历史说明中可见, 不计入净变更数量。</li>
<li>片段来源在发布区间<b>全部</b>提交上搜索引入边(父版本不存在而本提交存在), 不凭第一个命中定来源。</li>
<li>合并提交按所有父版本对照: 仅一方父版本含有 ⇒ 来自该分支; 双方父版本均含而 merge-base 不含 ⇒ 两分支独立引入; 所有父版本均不含 ⇒ 合并解决阶段引入。</li>
<li>删除的对称规则: 非合并提交上的消失边是删除事件; 合并边上的消失若可由区间内更早的非合并删除解释, 记为"沿用", 否则记为合并解决阶段删除。</li>
<li>重复文本: 发现多个独立引入/删除边时保留候选集(multiple_candidate_origins), 不做唯一归因。</li>
<li>删除以 base 中的路径与行范围记录, 并在全区间搜索消失边; 不依赖 target 的 blame(已删除内容在 target 中无从标注)。</li>
<li>重命名: 内容相似度(difflib ratio, 确定性阈值 0.55); 多个同等候选时保留歧义(retain ties), 按删除+新增处理并标注。</li>
<li>二进制文件(按计划 binary_files 清单及 NUL 启发式判定)仅报告 blob 身份(SHA-1 与大小), 不做行级片段。</li>
<li>来源状态使用发布计划词汇: single_branch / multiple_candidate_origins / merge_resolution / unresolved; moved_base_content 为对枚举的扩展(内容来自 base 的移动/复制)。</li>
<li>以上为启发式证据规则, 不试图证明任意代码的语义等价; 输入仓库只读, 不修改、不推送。</li>
</ol>
"""


def render_report(audit, verify):
    metas = audit["metas"]
    s = audit["summary"]
    plan = audit["plan"]
    h = [f"<html><head><meta charset='utf-8'><title>发布审计报告</title>"
         f"<style>{CSS}</style></head><body>",
         f"<h1>发布审计报告 — {esc(plan.get('name', 'release'))}</h1>",
         "<h2>概览</h2><table>",
         f"<tr><th>base</th><td><code>{esc(audit['base']['ref'])}</code> = "
         f"<code>{esc(audit['base']['sha'])}</code></td></tr>",
         f"<tr><th>target</th><td><code>{esc(audit['target']['ref'])}</code> = "
         f"<code>{esc(audit['target']['sha'])}</code></td></tr>",
         f"<tr><th>历史新增提交</th><td>{s['commits']} 个"
         f"(其中合并 {s['merges']} 个)"
         + (f"; ⚠ base 中有 {len(audit['not_in_target'])} 个提交不在 target 中"
            if audit["not_in_target"] else "") + "</td></tr>",
         f"<tr><th>净变更</th><td>{s['files_changed']} 个文件, "
         f"<span class='add'>+{s['net_adds']}</span> / "
         f"<span class='del'>−{s['net_dels']}</span> 行; "
         f"重命名 {s['renames']} 个, 歧义重命名 {s['ambiguous']} 个, "
         f"二进制 {s['binary']} 个</td></tr>",
         f"<tr><th>计划约束</th><td>文本文件 "
         f"{len(plan.get('text_files', []))} 个, 二进制文件 "
         f"{len(plan.get('binary_files', []))} 个; 编码 "
         f"{esc(plan.get('text_encoding', 'UTF-8'))}; 重命名策略: "
         f"{esc(plan.get('rename_policy', '—'))}; 输入只读: "
         f"{'是' if plan.get('read_only_input') else '否'}</td></tr>",
         f"<tr><th>生成时间</th><td>{esc(audit['generated'])}</td></tr>",
         "</table>",
         "<h2>一、净变更清单(最终树差异, 含来源证据)</h2>",
         render_netdiff(audit["entries"], audit["ambiguous_renames"], metas),
         "<h2>二、历史说明(target 可达而 base 不可达的提交集合)</h2>",
         "<p class='note'>以下为历史产物, 与净变更分开呈现: "
         "被撤销的修改在此可见, 但不计入上方净变更数量。</p>",
         render_history(audit["history"]),
         "<h3>合并提交父边对照</h3>",
         render_merges(audit["merges"]),
         "<h2>三、重放验证</h2>",
         render_verify(verify),
         "<h2>判定方法(证据充分性)</h2>",
         METHODOLOGY,
         "</body></html>"]
    return "".join(h)


# ---------------------------------------------------------------- audit
def build_audit(repo, plan):
    base_ref, target_ref = plan["base"], plan["target"]
    base = repo.rev_parse(base_ref)
    target = repo.rev_parse(target_ref)
    commits = repo.rev_list(base, target)
    if not commits:
        raise SystemExit("base..target 为空, 无发布区间。")
    not_in_target = repo.rev_list(target, base)
    metas = {c: repo.meta(c) for c in commits}
    ctx = Ctx(repo, plan, commits, metas, base, target)

    bt, tt = repo.ls_tree(base), repo.ls_tree(target)
    deleted = sorted(set(bt) - set(tt))
    added = sorted(set(tt) - set(bt))
    changed = [p for p in sorted(set(bt) & set(tt)) if bt[p] != tt[p]]

    renames, ambiguous = detect_renames(repo, plan, bt, tt, deleted, added)
    renamed_old = {r["old_path"] for r in renames}
    renamed_new = {r["new_path"] for r in renames}

    entries = []
    for r in renames:
        e = make_text_entry(repo, plan, bt, tt, r["old_path"], r["new_path"],
                            "rename")
        e["similarity"] = r["similarity"]
        entries.append(e)
    for p in changed:
        entries.append(make_text_entry(repo, plan, bt, tt, p, p, "modify"))
    for p in deleted:
        if p not in renamed_old:
            entries.append(make_delete_entry(repo, plan, bt, p))
    for p in added:
        if p not in renamed_new:
            entries.append(make_add_entry(repo, plan, tt, p))

    # 来源归因: 逐片段, 新增侧找引入边, 删除侧找消失边
    for e in entries:
        if e.get("binary"):
            continue
        for fr in e["fragments"]:
            att = {}
            if fr["added"]:
                att["added"] = attribute_added(fr["added"], ctx)
            if fr["deleted"]:
                att["deleted"] = attribute_deleted(fr["deleted"], ctx)
                s = fr["base_start"]
                att["deleted"]["base_path"] = e["old_path"] or e["path"]
                att["deleted"]["base_range"] = [s, s + fr["base_count"] - 1]
            fr["attribution"] = att

    history = analyze_history(repo, plan, commits, metas, target)
    merges = analyze_merges(repo, plan, commits, metas)

    net_adds = sum(len(f["added"]) for e in entries for f in e["fragments"])
    net_dels = sum(len(f["deleted"]) for e in entries for f in e["fragments"])
    summary = {
        "commits": len(commits),
        "merges": sum(1 for c in commits if len(metas[c]["parents"]) > 1),
        "files_changed": len(entries),
        "net_adds": net_adds, "net_dels": net_dels,
        "renames": len(renames), "ambiguous": len(ambiguous),
        "binary": sum(1 for e in entries if e.get("binary")),
    }

    import datetime
    return {
        "plan": plan,
        "base": {"ref": base_ref, "sha": base},
        "target": {"ref": target_ref, "sha": target},
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "commits": commits, "metas": metas, "not_in_target": not_in_target,
        "entries": entries, "renames": renames,
        "ambiguous_renames": ambiguous,
        "history": history, "merges": merges, "summary": summary,
    }


def write_patch(audit, path):
    """净差量的 unified diff 形式(文本文件), 供人工复核。"""
    out = []
    for e in audit["entries"]:
        if e.get("binary"):
            out.append(f"Binary files differ: {e['path']} "
                       f"({short(e.get('base_blob'))} -> "
                       f"{short(e.get('target_blob'))})")
            continue
        old = e.get("old_path") or e["path"]
        if e["change"] == "delete":
            a_lines = e["fragments"][0]["deleted"] if e["fragments"] else []
            diff = difflib.unified_diff(a_lines, [], fromfile=f"a/{old}",
                                        tofile=f"b/{e['path']}", lineterm="")
            out.extend(diff)
        elif e["change"] == "add":
            b_lines = e["fragments"][0]["added"] if e["fragments"] else []
            diff = difflib.unified_diff([], b_lines, fromfile=f"a/{old}",
                                        tofile=f"b/{e['path']}", lineterm="")
            out.extend(diff)
        else:
            # 修改/重命名: 按片段输出(不含未变上下文)
            for fr in e["fragments"]:
                out.append(f"--- a/{old}\n+++ b/{e['path']}")
                out.append(f"@@ -{fr['base_start']},{fr['base_count']} "
                           f"+{fr['target_start']},{len(fr['added'])} @@")
                out += ["-" + l for l in fr["deleted"]]
                out += ["+" + l for l in fr["added"]]
    Path(path).write_text("\n".join(out) + "\n", encoding="utf-8")


def cmd_audit(args):
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="relaudit-") as tmp:
        clone_bundle(args.bundle, os.path.join(tmp, "repo"))
        repo = Repo(os.path.join(tmp, "repo"))
        audit = build_audit(repo, plan)

    netdiff = {"base": audit["base"]["sha"], "target": audit["target"]["sha"],
               "base_ref": audit["base"]["ref"],
               "target_ref": audit["target"]["ref"],
               "files": audit["entries"]}
    (out / "netdiff.json").write_text(
        json.dumps(netdiff, ensure_ascii=False, indent=1), encoding="utf-8")
    write_patch(audit, out / "netdiff.patch")
    (out / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "report.html").write_text(render_report(audit, None),
                                     encoding="utf-8")

    s = audit["summary"]
    print(f"base   : {audit['base']['ref']} ({short(audit['base']['sha'])})")
    print(f"target : {audit['target']['ref']} ({short(audit['target']['sha'])})")
    print(f"历史新增提交 {s['commits']} 个(合并 {s['merges']} 个); "
          f"净变更 {s['files_changed']} 文件 +{s['net_adds']}/−{s['net_dels']}; "
          f"重命名 {s['renames']}, 歧义 {s['ambiguous']}, 二进制 {s['binary']}")
    for e in audit["entries"]:
        for fr in e.get("fragments", []):
            att = fr.get("attribution") or {}
            for side, key in (("新增", "added"), ("删除", "deleted")):
                a = att.get(key)
                if a and a["kind"] in ("independent", "merge_resolution",
                                       "moved", "unattributed"):
                    print(f"  [{a['kind']}] {e['path']}: {side}片段 — "
                          f"{a['rationale'][:80]}…")
    print(f"输出: {out}/netdiff.json, netdiff.patch, audit.json, report.html")


# ---------------------------------------------------------------- verify
def splice(lines, frags):
    for f in sorted(frags, key=lambda x: x["base_start"], reverse=True):
        i = f["base_start"] - 1
        lines[i:i + f["base_count"]] = f["added"]
    return lines


def apply_netdiff(netdiff, workdir):
    """仅依据 netdiff.json 在 base 树上重放净差量。"""
    for e in netdiff["files"]:
        path = e["path"]
        dst = os.path.join(workdir, path)
        if e["change"] == "delete":
            os.unlink(dst)
            continue
        os.makedirs(os.path.dirname(dst) or workdir, exist_ok=True)
        if e["change"] == "add":
            with open(dst, "wb") as fh:
                fh.write(base64.b64decode(e["content_b64"]))
        elif e["change"] in ("modify", "rename"):
            src_path = e["old_path"] if e["change"] == "rename" else path
            src = os.path.join(workdir, src_path)
            if e.get("binary"):
                # 二进制: 差量包直接携带目标内容
                result = base64.b64decode(e["content_b64"])
            else:
                with open(src, "rb") as fh:
                    data = fh.read()
                lines, final_nl = split_text(data)
                result = join_text(splice(lines, e["fragments"]),
                                   final_nl).encode("utf-8")
            if e["change"] == "rename":
                os.unlink(src)
            with open(dst, "wb") as fh:
                fh.write(result)
        if e.get("target_mode") == "100755":
            os.chmod(dst, 0o755)


def cmd_verify(args):
    out = Path(args.out)
    audit = json.loads((out / "audit.json").read_text(encoding="utf-8"))
    netdiff = json.loads((out / "netdiff.json").read_text(encoding="utf-8"))
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="relverify-") as tmp:
        clone_bundle(args.bundle, os.path.join(tmp, "repo"))
        repo = Repo(os.path.join(tmp, "repo"))
        base = repo.rev_parse(plan["base"])
        target = repo.rev_parse(plan["target"])
        if base != netdiff["base"] or target != netdiff["target"]:
            raise SystemExit("计划中的引用与 netdiff.json 记录的提交不一致。")

        # 独立目录解出 base 树, 重放净差量
        workdir = os.path.join(tmp, "work")
        os.makedirs(workdir)
        tar = tarfile.open(fileobj=io.BytesIO(repo.archive(base)), mode="r:")
        tar.extractall(workdir)
        apply_netdiff(netdiff, workdir)

        expected = repo.ls_tree(target)
        actual = {}
        for root, _dirs, files in os.walk(workdir):
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, workdir)
                with open(full, "rb") as fh:
                    data = fh.read()
                mode = "100755" if os.access(full, os.X_OK) else "100644"
                actual[rel] = (mode, blob_sha(data))

    files, ok = [], True
    for p in sorted(set(expected) | set(actual)):
        e, a = expected.get(p), actual.get(p)
        if e is None:
            files.append({"path": p, "status": "extra", "expected": None,
                          "actual": a[1]})
            ok = False
        elif a is None:
            files.append({"path": p, "status": "missing", "expected": e[1],
                          "actual": None})
            ok = False
        elif e[1] != a[1]:
            files.append({"path": p, "status": "content-mismatch",
                          "expected": e[1], "actual": a[1]})
            ok = False
        elif e[0] != a[0]:
            files.append({"path": p, "status": "mode-mismatch",
                          "expected": e[1], "actual": a[1]})
            ok = False
        else:
            files.append({"path": p, "status": "ok", "expected": e[1],
                          "actual": a[1]})

    verify = {"ok": ok, "files": files,
              "base": base, "target": target}
    (out / "verify.json").write_text(
        json.dumps(verify, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "report.html").write_text(render_report(audit, verify),
                                     encoding="utf-8")

    n_ok = sum(1 for f in files if f["status"] == "ok")
    print(f"重放核对: {n_ok}/{len(files)} 个文件与 target 树一致")
    for f in files:
        if f["status"] != "ok":
            print(f"  ✘ {f['path']}: {f['status']}")
    print("验证结论:", "✔ 净差量包可完整复现 target 树" if ok else "✘ 存在不一致")
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description="离线发布审计工具")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("audit", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--bundle", required=True, help="输入 Git bundle 路径")
        p.add_argument("--plan", required=True, help="发布计划 JSON")
        p.add_argument("--out", required=True, help="输出目录")
    args = ap.parse_args()
    if args.cmd == "audit":
        cmd_audit(args)
    else:
        cmd_verify(args)


if __name__ == "__main__":
    main()
