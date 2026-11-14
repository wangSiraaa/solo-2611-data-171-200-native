"""Brute-force audit of the tiny fixture.

The tiny case exists to be checked by hand.  This script enumerates *every*
haplotype assignment for the single tiny component and writes:

  * tiny_enumeration.csv : one row per candidate haplotype (cost, optimal flag)
  * tiny_probe_enumeration.csv : every forced relative-phase assignment and its
    cost, so the reported probe increment can be read off directly.

It deliberately imports no solver code from phase_small.py.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import os

import pysam

MIN_MAPQ = 20
MIN_BASEQ = 20
GAP_MIN = 10
CIG_NAMES = "MIDNSHP=X"


def read_rows(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bam", required=True)
    ap.add_argument("--variants", required=True)
    ap.add_argument("--reference", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--evidence", required=True,
                    help="evidence.csv produced by phase_small.py")
    args = ap.parse_args()

    variants = read_rows(args.variants)
    for v in variants:
        v["position"] = int(v["position"])
        v["allele0"] = v["allele0"].upper()
        v["allele1"] = v["allele1"].upper()
    order = [v["variant_id"] for v in variants]

    # Re-derive usable evidence from the shipped evidence table, then assert it
    # equals a direct re-extraction from the BAM.
    shipped = read_rows(args.evidence)
    usable = {}
    for row in shipped:
        if row["allele"] != "" and row["weight"] != "":
            usable[(row["fragment"], row["variant_id"])] = (
                int(row["allele"]), int(row["weight"])
            )

    # Re-extract directly from BAM as an independent cross-check.
    recs = {}
    with pysam.AlignmentFile(args.bam, "rb") as bam:
        for r in bam:
            if (r.is_unmapped or r.is_secondary or r.is_supplementary
                    or not r.cigartuples or r.mapping_quality < MIN_MAPQ):
                continue
            recs.setdefault(r.query_name, {})["read1" if r.is_read1 else "read2"] = r

    def locate(rec, pos1):
        ref0 = pos1 - 1
        if not (rec.reference_start <= ref0 < rec.reference_end):
            return None
        target = ref0 - rec.reference_start
        q = r = 0
        for op, ln in rec.cigartuples:
            nm = CIG_NAMES[op]
            if nm in "M=X":
                if r <= target < r + ln:
                    return q + target - r
                q += ln
                r += ln
            elif nm in "IS":
                q += ln
            elif nm in "DN":
                r += ln
        return None

    direct = {}
    for frag, mates in recs.items():
        out = []
        for v in variants:
            ends = []
            for mate in ("read1", "read2"):
                rec = mates.get(mate)
                if rec is None:
                    continue
                qi = locate(rec, v["position"])
                if qi is None:
                    continue
                base = rec.query_sequence[qi].upper()
                qual = int(rec.query_qualities[qi])
                if base in (v["allele0"], v["allele1"]) and qual >= MIN_BASEQ:
                    ends.append((0 if base == v["allele0"] else 1, qual))
            if not ends:
                out.append(None)
            elif len(ends) == 1:
                out.append(ends[0])
            else:
                (a1, q1), (a2, q2) = ends
                if a1 == a2:
                    out.append((a1, max(q1, q2)))
                elif abs(q1 - q2) >= GAP_MIN:
                    out.append((a1, abs(q1 - q2)) if q1 > q2 else (a2, abs(q1 - q2)))
                else:
                    out.append(None)
        direct[frag] = out

    for frag, vals in direct.items():
        for idx, vid in enumerate(order):
            key = (frag, vid)
            got = usable.get(key)
            exp = vals[idx]
            if got != exp:
                raise SystemExit(f"tiny extract mismatch {key}: {got} vs {exp}")

    fragments = sorted(recs)
    fragments = [f for f in fragments if len(recs[f]) == 2]

    def cost_of(hap):
        total = 0
        per_frag = {}
        for frag in fragments:
            mh = mc = 0
            for idx, vid in enumerate(order):
                pair = direct[frag][idx]
                if pair is None:
                    continue
                a, w = pair
                if hap[idx] != a:
                    mh += w
                if (1 - hap[idx]) != a:
                    mc += w
            total += min(mh, mc)
            per_frag[frag] = (min(mh, mc), "h" if mh < mc else ("c" if mc < mh else "u"), mh, mc)
        return total, per_frag

    all_haps = [(0,) + tail for tail in itertools.product((0, 1), repeat=len(order) - 1)]
    table = []
    costs = []
    for hap in all_haps:
        c, per_frag = cost_of(hap)
        costs.append(c)
        table.append((hap, c))
    best = min(costs)

    os.makedirs(args.outdir, exist_ok=True)
    with open(os.path.join(args.outdir, "tiny_enumeration.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["haplotype", "mec_cost", "optimal"])
        for hap, c in table:
            w.writerow(["".join(map(str, hap)), c, int(c == best)])

    # Forced relative-phase enumeration for each adjacent pair.
    probe_rows = []
    main_hap = next(h for h, c in table if c == best)
    for i in range(len(order) - 1):
        req = 1 - (main_hap[i] ^ main_hap[i + 1])
        rows = []
        for hap in all_haps:
            if (hap[i] ^ hap[i + 1]) == req:
                rows.append((hap, cost_of(hap)[0]))
        alt = min(c for _, c in rows)
        probe_rows.append((order[i], order[i + 1], main_hap[i] ^ main_hap[i + 1], req,
                           best, alt, alt - best, "".join(map(str, main_hap))))
        for hap, c in rows:
            probe_rows.append((order[i], order[i + 1], "", "", "", "", "",
                               "".join(map(str, hap))))
    with open(os.path.join(args.outdir, "tiny_probe_enumeration.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["left", "right", "main_rel", "forced_rel", "main_cost",
                    "forced_min_cost", "increment", "candidate_haplotype"])
        for r in probe_rows:
            w.writerow(r)

    summary = {
        "component_variants": order,
        "fragments": fragments,
        "n_candidate_haplotypes": len(all_haps),
        "best_cost": best,
        "optimal_haplotypes": ["".join(map(str, h)) for h, c in table if c == best],
        "probes": [
            {"left": p[0], "right": p[1], "main_relative_phase": p[2],
             "forced_relative_phase": p[3], "main_cost": p[4],
             "forced_min_cost": p[5], "increment": p[6]}
            for p in probe_rows if p[2] != ""
        ],
    }
    with open(os.path.join(args.outdir, "tiny_enumeration_summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
