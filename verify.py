#!/usr/bin/env python3
"""对 fixtures 运行规范化与判等, 生成全部交付物到 out/, 并做独立复核。

交付物:
  out/<name>.canonical.nq      规范 N-Quads (UTF-8)
  out/<name>.canonical.json    sha256 + 规范标号 + 诊断
  out/compare-<a>-<b>.json     判等结果: 映射 + 语句来源映射, 或规范差异
  out/verification-summary.json  微型图独立枚举核对与随机重命名核对结果
"""

from __future__ import annotations

import itertools
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rdfcanon import model, parse_nquads_file, canonicalize, compare
from rdfcanon.compare import _apply_mapping

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")

NAMES = ["base", "renamed", "changed", "symmetric", "symmetric-renamed", "nonisomorphic"]
PAIRS = [("base", "renamed"), ("base", "changed"),
         ("symmetric", "symmetric-renamed"), ("symmetric", "nonisomorphic")]


def write(name, text):
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def brute_force_isomorphic(quads_a, quads_b):
    nodes_a = sorted({t[1] for q in quads_a for t in q if model.is_bnode(t)})
    nodes_b = sorted({t[1] for q in quads_b for t in q if model.is_bnode(t)})
    if len(nodes_a) != len(nodes_b):
        return False
    for perm in itertools.permutations(nodes_b):
        if _apply_mapping(quads_a, dict(zip(nodes_a, perm))) == quads_b:
            return True
    return False


def relabeled_text(ds, rng):
    labels = ds.bnode_labels
    fresh = rng.sample([f"n{i}{c}" for i in range(len(labels)) for c in "xyz"], len(labels))
    rename = dict(zip(labels, fresh))
    lines = [model.quad_line(q, rename.get) for q in ds.quads]
    rng.shuffle(lines)
    return "".join(line + "\n" for line in lines)


def cycle_dataset(n, prefix="v"):
    ds = model.Dataset()
    for i in range(n):
        ds.add((model.bnode(f"{prefix}{i}"), model.iri("https://ex.invalid/next"),
                model.bnode(f"{prefix}{(i + 1) % n}"), model.DEFAULT_GRAPH), i + 1, "")
    return ds


def main():
    os.makedirs(OUT, exist_ok=True)
    from rdfcanon import parse_nquads

    datasets = {name: parse_nquads_file(os.path.join(FIXTURES, name + ".nq")) for name in NAMES}

    print("== canonicalize ==")
    canons = {}
    for name, ds in datasets.items():
        t0 = time.time()
        result = canonicalize(ds)
        canons[name] = result
        write(f"{name}.canonical.nq", result.canonical_nquads)
        diag = {
            "source": f"fixtures/{name}.nq",
            "sha256": result.sha256,
            "labeling": result.labeling,
            "stats": result.stats,
            "quads": len(ds),
            "duplicates_folded": ds.duplicate_count,
            "elapsed_ms": round((time.time() - t0) * 1000, 2),
        }
        write(f"{name}.canonical.json", json.dumps(diag, ensure_ascii=False, indent=2) + "\n")
        print(f"  {name:20s} sha256={result.sha256[:16]}... stats={result.stats}")

    print("== compare ==")
    for a, b in PAIRS:
        result = compare(datasets[a], datasets[b])
        report = result.to_dict()
        report["file_a"] = f"fixtures/{a}.nq"
        report["file_b"] = f"fixtures/{b}.nq"
        write(f"compare-{a}-{b}.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        verdict = "EQUIVALENT" if result.equivalent else "NOT equivalent"
        extra = f" replay_ok={result.mapping_replay_ok}" if result.equivalent else \
                f" only_in_a={len(result.only_in_a)} only_in_b={len(result.only_in_b)}"
        print(f"  {a} vs {b}: {verdict}{extra}")

    print("== independent cross-check ==")
    summary = {"fixture_pairs": {}, "micro_bruteforce": {}, "random_relabel": {}}
    ok = True
    for a, b in PAIRS:
        r = compare(datasets[a], datasets[b])
        expect = brute_force_isomorphic(datasets[a].quads, datasets[b].quads)
        match = r.equivalent == expect
        ok &= match
        summary["fixture_pairs"][f"{a}-{b}"] = {"verdict": r.equivalent, "brute_force": expect, "match": match}
        if r.equivalent:
            replay = _apply_mapping(datasets[a].quads, r.bnode_mapping) == datasets[b].quads
            ok &= replay
            summary["fixture_pairs"][f"{a}-{b}"]["mapping_replay_ok"] = replay

    # 微型图 (<=6 节点): 环族两两对照暴力枚举
    micro = {f"C{n}": cycle_dataset(n) for n in (3, 4, 5, 6)}
    two = model.Dataset()
    for off, n in ((0, 3), (3, 3)):
        for i in range(n):
            two.add((model.bnode(f"w{off + i}"), model.iri("https://ex.invalid/next"),
                     model.bnode(f"w{off + (i + 1) % n}"), model.DEFAULT_GRAPH), i, "")
    micro["C3+C3"] = two
    micro_ok = micro_total = 0
    for (na, da), (nb, db) in itertools.combinations(micro.items(), 2):
        micro_total += 1
        r = compare(da, db)
        expect = brute_force_isomorphic(da.quads, db.quads)
        good = r.equivalent == expect
        if r.equivalent:
            good &= _apply_mapping(da.quads, r.bnode_mapping) == db.quads
        micro_ok += good
        ok &= good
    summary["micro_bruteforce"] = {"pairs": micro_total, "matched": micro_ok}

    # 随机换标签/换序: 规范字节必须一致
    rng = random.Random(20261114)
    trials = fails = 0
    for name, ds in datasets.items():
        want = canons[name].canonical_bytes
        for _ in range(25):
            trials += 1
            variant = parse_nquads(relabeled_text(ds, rng))
            if canonicalize(variant).canonical_bytes != want:
                fails += 1
    ok &= fails == 0
    summary["random_relabel"] = {"trials": trials, "mismatches": fails}
    summary["all_ok"] = ok
    write("verification-summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(f"  fixture pairs vs brute force: {sum(1 for v in summary['fixture_pairs'].values() if v['match'])}/{len(PAIRS)}")
    print(f"  micro cycles vs brute force:  {micro_ok}/{micro_total}")
    print(f"  random relabel trials:        {trials - fails}/{trials} consistent")
    print(f"  ALL OK: {ok}")
    print(f"deliverables written to {OUT}/")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
