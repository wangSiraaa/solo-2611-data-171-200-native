"""Phase a small diploid BAM into two haplotypes with exact weighted MEC.

Inputs (see README.md / fixtures/plan.json):
  * a coordinate-sorted BAM with primary read1/read2 pairs for a single contig,
  * a table of biallelic SNVs in 1-based reference coordinates,
  * the reference FASTA used only to verify allele0 == REF.

The program performs *no* read alignment, *no* variant calling, and *no* indel
locus handling.  It only extracts the already-known SNV genotypes.

Pipeline:
  1. Drop unmapped/secondary/supplementary/low-MAPQ records.
  2. Group survivors strictly by query_name into (read1, read2) fragments.
  3. For every fragment x locus, locate the SNV in each mate via the CIGAR and
     read the stored SEQ base plus its quality.  SEQ is never reverse-
     complemented based on the reverse flag.
  4. Merge the two mates into at most one evidence record per fragment/locus
     with the fixed quality rules.
  5. Build connected components from *effective co-coverage*.
  6. Exactly minimise total weighted MEC over every haplotype assignment with
     the first locus of each component pinned to 0.
  7. For each adjacent locus pair, force the relative phase opposite to the
     main solution, re-minimise, and report the cost increment (no probability
     interpretation).
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

import pysam

# ---------------------------------------------------------------------------
# CIGAR op codes (SAM spec)
# ---------------------------------------------------------------------------
CIGAR_M = 0
CIGAR_I = 1
CIGAR_D = 2
CIGAR_N = 3
CIGAR_S = 4
CIGAR_H = 5
CIGAR_P = 6
CIGAR_EQ = 7
CIGAR_X = 8

QUERY_REF_OPS = {CIGAR_M, CIGAR_EQ, CIGAR_X}
QUERY_ONLY_OPS = {CIGAR_I, CIGAR_S}
REF_ONLY_OPS = {CIGAR_D, CIGAR_N}

CIGAR_NAMES = {
    CIGAR_M: "M", CIGAR_I: "I", CIGAR_D: "D", CIGAR_N: "N",
    CIGAR_S: "S", CIGAR_H: "H", CIGAR_P: "P", CIGAR_EQ: "=", CIGAR_X: "X",
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class Variant:
    variant_id: str
    chrom: str
    position: int
    allele0: str
    allele1: str


@dataclass
class ReadEnd:
    """One mate's raw observation at one locus, before mate merging."""

    frag: str
    variant_id: str
    mate: str                  # read1 / read2
    read_name: str
    flag: int
    is_reverse: bool
    mapq: int
    cigar: str
    reference_start_1based: int
    query_index: int | None    # 0-based index into stored BAM SEQ
    base: str
    base_qual: int
    status: str                # valid | nonref_alt | low_base_qual | deletion | ...
    detail: str = ""


@dataclass
class Evidence:
    """Merged per-fragment, per-locus evidence after mate reconciliation."""

    frag: str
    variant_id: str
    allele: int | None         # 0/1 for allele0/allele1, None if unassigned
    weight: int | None         # fixed integer weight (quality or quality gap)
    decision: str              # single_read1 | single_read2 | mates_agree |
                               # adjudicated | undecided | no_call
    reason: str
    read1: ReadEnd | None = None
    read2: ReadEnd | None = None


@dataclass
class FragmentGroup:
    frag: str
    read1: pysam.AlignedSegment | None = None
    read2: pysam.AlignedSegment | None = None
    status: str = "complete"
    note: str = ""


@dataclass
class ComponentResult:
    component_id: str
    variant_ids: list[str]
    fragments: list[str]
    mec_cost: int
    haplotype: list[int]
    fragment_assignments: list[dict[str, Any]]
    probes: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Loading inputs
# ---------------------------------------------------------------------------
def load_variants(path: str) -> list[Variant]:
    out: list[Variant] = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            out.append(
                Variant(
                    variant_id=row["variant_id"].strip(),
                    chrom=row["chrom"].strip(),
                    position=int(row["position"]),
                    allele0=row["allele0"].strip().upper(),
                    allele1=row["allele1"].strip().upper(),
                )
            )
    return out


def load_plan(path: str | None) -> dict[str, Any]:
    """Load plan.json, falling back to documented defaults."""
    plan = {
        "max_variants_per_block": 12,
        "max_fragments_per_block": 40,
        "min_mapping_quality": 20,
        "min_base_quality": 20,
        "mate_disagreement_min_quality_gap": 10,
    }
    if path and os.path.exists(path):
        with open(path) as fh:
            plan.update(json.load(fh))
    return plan


# ---------------------------------------------------------------------------
# BAM grouping
# ---------------------------------------------------------------------------
def filter_reason(rec: pysam.AlignedSegment, min_mapq: int) -> str:
    reasons: list[str] = []
    if rec.is_unmapped:
        reasons.append("unmapped")
    if rec.is_secondary:
        reasons.append("secondary")
    if rec.is_supplementary:
        reasons.append("supplementary")
    if not rec.cigartuples:
        reasons.append("no_cigar")
    if rec.mapping_quality < min_mapq:
        reasons.append(f"mapq<{min_mapq}")
    return ";".join(reasons)


def group_fragments(
    bam_path: str, min_mapq: int, diagnostics: list[dict[str, Any]]
) -> tuple[dict[str, FragmentGroup], int]:
    """Group surviving primary records strictly by query_name.

    Row order is irrelevant: pairing is by name + read1/read2 flag.  Exactly one
    primary read1 and one primary read2 are required for a usable fragment.
    Returns (groups, number_of_BAM_records_seen).
    """
    groups: dict[str, FragmentGroup] = {}
    records_seen = 0

    with pysam.AlignmentFile(bam_path, "rb") as bam:
        for rec in bam:
            records_seen += 1
            reason = filter_reason(rec, min_mapq)
            if reason:
                diagnostics.append(
                    {
                        "type": "filtered_record",
                        "query_name": rec.query_name,
                        "flag": rec.flag,
                        "mapq": rec.mapping_quality,
                        "cigar": rec.cigarstring,
                        "reason": reason,
                    }
                )
                continue

            grp = groups.setdefault(rec.query_name, FragmentGroup(frag=rec.query_name))
            if rec.is_read1 and not rec.is_read2:
                if grp.read1 is not None:
                    grp.status = "duplicate_read1"
                    diagnostics.append(
                        {
                            "type": "duplicate_mate",
                            "fragment": rec.query_name,
                            "mate": "read1",
                            "detail": "more than one primary read1",
                        }
                    )
                else:
                    grp.read1 = rec
            elif rec.is_read2 and not rec.is_read1:
                if grp.read2 is not None:
                    grp.status = "duplicate_read2"
                    diagnostics.append(
                        {
                            "type": "duplicate_mate",
                            "fragment": rec.query_name,
                            "mate": "read2",
                            "detail": "more than one primary read2",
                        }
                    )
                else:
                    grp.read2 = rec
            else:
                grp.status = "unpaired_flag"
                diagnostics.append(
                    {
                        "type": "unpaired_flag",
                        "fragment": rec.query_name,
                        "flag": rec.flag,
                        "detail": "primary record is neither read1 nor read2",
                    }
                )

    for frag, grp in groups.items():
        if grp.status != "complete":
            continue
        if grp.read1 is None:
            grp.status = "missing_read1"
        elif grp.read2 is None:
            grp.status = "missing_read2"
        if grp.status != "complete":
            diagnostics.append(
                {"type": "incomplete_fragment", "fragment": frag, "status": grp.status}
            )
    return groups, records_seen


# ---------------------------------------------------------------------------
# CIGAR-aware lookup of a reference SNV in a read
# ---------------------------------------------------------------------------
def locate_query_base(rec: pysam.AlignedSegment, ref_pos1: int) -> dict[str, Any]:
    """Map a 1-based reference position to a query index through the CIGAR.

    M/=/X consume reference and query; I/S consume query only; D/N consume
    reference only and therefore yield no base.  The stored SEQ is used as-is;
    the reverse flag is *not* applied again.
    """
    ref0 = ref_pos1 - 1
    rec_start = rec.reference_start
    rec_end = rec.reference_end
    if rec_start is None or rec_end is None:
        return {"kind": "outside", "query_index": None, "cigar_op": None,
                "detail": "no reference span"}
    if not (rec_start <= ref0 < rec_end):
        return {
            "kind": "outside",
            "query_index": None,
            "cigar_op": None,
            "detail": f"pos {ref_pos1} outside [{rec_start + 1},{rec_end}]",
        }

    local = ref0 - rec_start
    q = 0
    r = 0
    for op, length in rec.cigartuples:
        if op in QUERY_REF_OPS:
            if r <= local < r + length:
                return {
                    "kind": "base",
                    "query_index": q + (local - r),
                    "cigar_op": CIGAR_NAMES[op],
                    "detail": f"{CIGAR_NAMES[op]} block [{r},{r + length}) local={local}",
                }
            q += length
            r += length
        elif op in QUERY_ONLY_OPS:
            q += length
        elif op in REF_ONLY_OPS:
            if r <= local < r + length:
                return {
                    "kind": "deletion" if op == CIGAR_D else "refskip",
                    "query_index": None,
                    "cigar_op": CIGAR_NAMES[op],
                    "detail": f"{CIGAR_NAMES[op]} block [{r},{r + length}) covers "
                    f"local={local}; no query base",
                }
            r += length
        elif op in (CIGAR_H, CIGAR_P):
            continue
        else:  # pragma: no cover - defensive
            raise ValueError(f"unsupported CIGAR op {op}")
    return {"kind": "outside", "query_index": None, "cigar_op": None,
            "detail": "CIGAR exhausted before target"}


def reference_base(fasta: pysam.FastaFile, chrom: str, pos1: int) -> str:
    return fasta.fetch(chrom, pos1 - 1, pos1).upper()


def extract_end_observation(
    frag: str,
    mate: str,
    rec: pysam.AlignedSegment,
    var: Variant,
    fasta: pysam.FastaFile,
    min_baseq: int,
) -> ReadEnd:
    """Extract one mate's raw observation at one locus with full provenance."""
    ref_base = reference_base(fasta, var.chrom, var.position)
    loc = locate_query_base(rec, var.position)
    status = "valid"
    detail = ""
    qi: int | None = None
    base = ""
    qual = -1

    if rec.reference_name != var.chrom:
        # A mate on another contig must never be silently mapped by coordinate.
        return ReadEnd(
            frag=frag, variant_id=var.variant_id, mate=mate,
            read_name=rec.query_name, flag=rec.flag, is_reverse=rec.is_reverse,
            mapq=rec.mapping_quality, cigar=rec.cigarstring or "",
            reference_start_1based=rec.reference_start + 1,
            query_index=None, base="", base_qual=-1, status="wrong_chrom",
            detail=f"read contig {rec.reference_name} != variant {var.chrom}",
        )

    if loc["kind"] != "base":
        # No aligned query base: preserve why (outside span / deletion / N).
        status = loc["kind"]
        detail = loc["detail"]
    else:
        # Record the raw query base honestly even if a downstream rule then
        # rejects it; diagnostics must never be rewritten into allele 0.
        qi = loc["query_index"]
        seq = rec.query_sequence
        quals = rec.query_qualities
        if seq is None or qi is None or qi >= len(seq):
            status = "no_query_base"
            detail = f"query index {qi} outside SEQ"
        else:
            base = seq[qi].upper()
            qual = int(quals[qi]) if quals is not None else 0
            if ref_base != var.allele0:
                status = "ref_mismatch"
                detail = f"reference {ref_base} != allele0 {var.allele0}"
            elif base not in (var.allele0, var.allele1):
                status = "nonref_alt"
                detail = f"base {base} not in {{{var.allele0},{var.allele1}}}"
            elif qual < min_baseq:
                status = "low_base_qual"
                detail = f"Q{qual} < {min_baseq}"

    return ReadEnd(
        frag=frag,
        variant_id=var.variant_id,
        mate=mate,
        read_name=rec.query_name,
        flag=rec.flag,
        is_reverse=rec.is_reverse,
        mapq=rec.mapping_quality,
        cigar=rec.cigarstring or "",
        reference_start_1based=rec.reference_start + 1,
        query_index=qi,
        base=base,
        base_qual=qual,
        status=status,
        detail=detail,
    )


def allele_index(base: str, var: Variant) -> int | None:
    if base == var.allele0:
        return 0
    if base == var.allele1:
        return 1
    return None


# ---------------------------------------------------------------------------
# Mate merge (fixed quality rules)
# ---------------------------------------------------------------------------
def merge_mates(
    e1: ReadEnd | None, e2: ReadEnd | None, var: Variant, gap_min: int
) -> Evidence:
    """Merge the two mates of one fragment at one locus into <=1 evidence."""
    valid1 = e1 is not None and e1.status == "valid"
    valid2 = e2 is not None and e2.status == "valid"

    def summary() -> str:
        parts = []
        for e in (e1, e2):
            if e is None:
                parts.append("(absent)")
            else:
                parts.append(
                    f"{e.mate}:{e.status}:base={e.base or '-'}:Q{e.base_qual}"
                )
        return " | ".join(parts)

    frag = (e1.frag if e1 else e2.frag)  # type: ignore[union-attr]

    if not valid1 and not valid2:
        return Evidence(frag=frag, variant_id=var.variant_id, allele=None,
                        weight=None, decision="no_call", reason=summary(),
                        read1=e1, read2=e2)

    if valid1 and not valid2:
        return Evidence(frag=frag, variant_id=var.variant_id,
                        allele=allele_index(e1.base, var), weight=e1.base_qual,
                        decision="single_read1", reason=summary(),
                        read1=e1, read2=e2)  # type: ignore[union-attr]

    if valid2 and not valid1:
        return Evidence(frag=frag, variant_id=var.variant_id,
                        allele=allele_index(e2.base, var), weight=e2.base_qual,
                        decision="single_read2", reason=summary(),
                        read1=e1, read2=e2)  # type: ignore[union-attr]

    a1 = allele_index(e1.base, var)  # type: ignore[union-attr]
    a2 = allele_index(e2.base, var)  # type: ignore[union-attr]
    assert a1 is not None and a2 is not None

    if a1 == a2:
        return Evidence(frag=frag, variant_id=var.variant_id, allele=a1,
                        weight=max(e1.base_qual, e2.base_qual),  # type: ignore[union-attr]
                        decision="mates_agree", reason=summary(),
                        read1=e1, read2=e2)

    gap = abs(e1.base_qual - e2.base_qual)  # type: ignore[union-attr]
    if gap >= gap_min:
        if e1.base_qual > e2.base_qual:  # type: ignore[union-attr]
            allele, winner = a1, e1
        else:
            allele, winner = a2, e2
        return Evidence(
            frag=frag, variant_id=var.variant_id, allele=allele, weight=gap,
            decision="adjudicated",
            reason=summary() + f" | gap={gap}>= {gap_min}; winner "
            f"{winner.mate} Q{winner.base_qual}",  # type: ignore[union-attr]
            read1=e1, read2=e2)

    return Evidence(frag=frag, variant_id=var.variant_id, allele=None,
                    weight=None, decision="undecided",
                    reason=summary() + f" | gap={gap} < {gap_min}",
                    read1=e1, read2=e2)


# ---------------------------------------------------------------------------
# Evidence extraction over all fragments and loci
# ---------------------------------------------------------------------------
def build_evidence(
    groups: dict[str, FragmentGroup],
    variants: list[Variant],
    fasta: pysam.FastaFile,
    plan: dict[str, Any],
) -> tuple[list[ReadEnd], list[Evidence]]:
    """Return all raw end observations and all merged evidences.

    Ends are emitted for every locus where the mate had alignment/coverage,
    including diagnostic statuses.  Merged evidence is emitted for every
    fragment/locus pair with at least one end (even no_call / undecided) so that
    diagnostics are preserved.
    """
    min_baseq = int(plan["min_base_quality"])
    gap_min = int(plan["mate_disagreement_min_quality_gap"])
    ends: list[ReadEnd] = []
    evidences: list[Evidence] = []

    for frag in sorted(groups):
        grp = groups[frag]
        if grp.status != "complete":
            continue
        for var in variants:
            e1 = extract_end_observation(frag, "read1", grp.read1, var, fasta, min_baseq)
            e2 = extract_end_observation(frag, "read2", grp.read2, var, fasta, min_baseq)
            # Every mate is emitted (including outside/deletion/low-Q rows) so
            # that no-coverage and rejection diagnostics survive; merged evidence
            # is one row per fragment/locus regardless of both, one, or no ends.
            ends.append(e1)
            ends.append(e2)
            evidences.append(merge_mates(e1, e2, var, gap_min))

    return ends, evidences


# ---------------------------------------------------------------------------
# Component construction
# ---------------------------------------------------------------------------
def build_components(
    variants: list[Variant],
    evidences: list[Evidence],
    usable: dict[tuple[str, str], Evidence],
) -> tuple[list[list[str]], dict[str, list[str]]]:
    """Connected components from effective co-coverage.

    Two loci are adjacent only if some fragment contributes usable evidence
    (allele not None) at both of them.  Loci without any usable evidence are
    excluded from components entirely (they remain isolated).
    """
    order = {v.variant_id: i for i, v in enumerate(variants)}
    cov_frags: dict[str, set[str]] = {}
    for (frag, vid) in usable:
        cov_frags.setdefault(vid, set()).add(frag)

    adj: dict[str, set[str]] = {v.variant_id: set() for v in variants}
    for frag in sorted({fr for fr, _ in usable}):
        vids = sorted(
            [vid for (f, vid) in usable if f == frag],
            key=lambda x: order[x],
        )
        for i in range(len(vids)):
            for j in range(i + 1, len(vids)):
                adj[vids[i]].add(vids[j])
                adj[vids[j]].add(vids[i])

    components: list[list[str]] = []
    seen: set[str] = set()
    for v in variants:
        vid = v.variant_id
        if vid in seen or not cov_frags.get(vid):
            continue
        stack = [vid]
        comp: list[str] = []
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            comp.append(x)
            stack.extend(sorted(adj[x], key=lambda y: order[y], reverse=True))
        comp.sort(key=lambda y: order[y])
        components.append(comp)

    frags_by_comp: dict[str, list[str]] = {}
    for comp in components:
        cid = "block_" + "+".join(comp)
        frags = sorted(
            {f for f in {fr for fr, vid in usable} if any((f, vid) in usable for vid in comp)}
        )
        frags_by_comp[cid] = frags
    return components, frags_by_comp


# ---------------------------------------------------------------------------
# Exact weighted-MEC solver
# ---------------------------------------------------------------------------
def fragment_cost(
    frag: str,
    comp: list[str],
    hap: list[int],
    usable: dict[tuple[str, str], Evidence],
) -> tuple[int, int, str]:
    """Return (mismatch_to_h, mismatch_to_complement_h, assignment).

    assignment is 'h', 'c' (complement), or 'u' (tie -> unassigned).
    """
    mismatch_h = 0
    mismatch_c = 0
    for idx, vid in enumerate(comp):
        ev = usable.get((frag, vid))
        if ev is None:
            continue
        if hap[idx] != ev.allele:
            mismatch_h += ev.weight
        if (1 - hap[idx]) != ev.allele:
            mismatch_c += ev.weight
    if mismatch_h < mismatch_c:
        assign = "h"
    elif mismatch_c < mismatch_h:
        assign = "c"
    else:
        assign = "u"
    return mismatch_h, mismatch_c, assign


def total_cost(
    comp: list[str],
    hap: list[int],
    frags: list[str],
    usable: dict[tuple[str, str], Evidence],
) -> int:
    total = 0
    for frag in frags:
        mh, mc, _ = fragment_cost(frag, comp, hap, usable)
        total += min(mh, mc)
    return total


def best_haplotype(
    comp: list[str],
    frags: list[str],
    usable: dict[tuple[str, str], Evidence],
    force: tuple[int, int, int] | None = None,
) -> tuple[int, list[int]]:
    """Exhaustively minimise weighted MEC with position 0 pinned to 0.

    force = (i, j, required_xor) restricts h[i] XOR h[j] to the given value.
    Returns (cost, haplotype).  Ties keep the first explored solution, and the
    enumeration order is lexicographic on (h[1], h[2], ...), so the reported
    optimum is the lexicographically smallest one.  This is a *stability* rule
    only: it does not rank alternatives or assign probabilities.
    """
    n = len(comp)
    if n == 1:
        # A single locus has no internal relative phase.
        if force is not None and force[0] != force[1] and force[2] != 0:
            return (10 ** 9, [0])
        return (total_cost(comp, [0], frags, usable), [0])

    best_cost: int | None = None
    best_hap: list[int] | None = None
    for tail in itertools.product((0, 1), repeat=n - 1):
        hap = [0, *tail]
        if force is not None:
            i, j, required = force
            if (hap[i] ^ hap[j]) != required:
                continue
        cost = total_cost(comp, hap, frags, usable)
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_hap = hap
    assert best_cost is not None and best_hap is not None
    return best_cost, best_hap


def solve_component(
    comp: list[str],
    frags: list[str],
    usable: dict[tuple[str, str], Evidence],
    cid: str,
) -> ComponentResult:
    cost, hap = best_haplotype(comp, frags, usable)

    assignments: list[dict[str, Any]] = []
    for frag in frags:
        mh, mc, assign = fragment_cost(frag, comp, hap, usable)
        entry = {
            "fragment": frag,
            "loss": min(mh, mc),
            "assignment": assign,
            "mismatch_to_h": mh,
            "mismatch_to_complement": mc,
            "status": "assigned" if assign != "u" else "unassigned_tie",
            "used_loci": [],
        }
        for idx, vid in enumerate(comp):
            ev = usable.get((frag, vid))
            if ev is None:
                continue
            entry["used_loci"].append(
                {
                    "variant_id": vid,
                    "allele": ev.allele,
                    "weight": ev.weight,
                    "decision": ev.decision,
                }
            )
        assignments.append(entry)

    probes: list[dict[str, Any]] = []
    for i in range(len(comp) - 1):
        required = 1 - (hap[i] ^ hap[i + 1])
        alt_cost, alt_hap = best_haplotype(
            comp, frags, usable, force=(i, i + 1, required)
        )
        probes.append(
            {
                "left_variant": comp[i],
                "right_variant": comp[i + 1],
                "main_relative_phase": hap[i] ^ hap[i + 1],
                "forced_relative_phase": required,
                "main_cost": cost,
                "forced_cost": alt_cost,
                "increment": alt_cost - cost,
                "alternative_haplotype": alt_hap,
                "same_cost": alt_cost == cost,
            }
        )

    return ComponentResult(
        component_id=cid,
        variant_ids=list(comp),
        fragments=list(frags),
        mec_cost=cost,
        haplotype=hap,
        fragment_assignments=assignments,
        probes=probes,
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def write_csv(path: str, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def evidence_row(ev: Evidence) -> dict[str, Any]:
    def end_fields(e: ReadEnd | None, prefix: str) -> dict[str, Any]:
        if e is None:
            return {
                f"{prefix}_flag": "", f"{prefix}_reverse": "", f"{prefix}_mapq": "",
                f"{prefix}_cigar": "", f"{prefix}_ref_start": "",
                f"{prefix}_query_index": "", f"{prefix}_base": "",
                f"{prefix}_qual": "", f"{prefix}_status": "", f"{prefix}_detail": "",
            }
        return {
            f"{prefix}_flag": e.flag,
            f"{prefix}_reverse": int(e.is_reverse),
            f"{prefix}_mapq": e.mapq,
            f"{prefix}_cigar": e.cigar,
            f"{prefix}_ref_start": e.reference_start_1based,
            f"{prefix}_query_index": "" if e.query_index is None else e.query_index,
            f"{prefix}_base": e.base,
            f"{prefix}_qual": "" if e.base_qual < 0 else e.base_qual,
            f"{prefix}_status": e.status,
            f"{prefix}_detail": e.detail,
        }
    row = {
        "fragment": ev.frag,
        "variant_id": ev.variant_id,
        "allele": "" if ev.allele is None else ev.allele,
        "weight": "" if ev.weight is None else ev.weight,
        "decision": ev.decision,
        "reason": ev.reason,
    }
    row.update(end_fields(ev.read1, "r1"))
    row.update(end_fields(ev.read2, "r2"))
    return row


def end_row(e: ReadEnd) -> dict[str, Any]:
    return {
        "fragment": e.frag,
        "variant_id": e.variant_id,
        "mate": e.mate,
        "flag": e.flag,
        "reverse": int(e.is_reverse),
        "mapq": e.mapq,
        "cigar": e.cigar,
        "ref_start_1based": e.reference_start_1based,
        "query_index": "" if e.query_index is None else e.query_index,
        "base": e.base,
        "qual": "" if e.base_qual < 0 else e.base_qual,
        "status": e.status,
        "detail": e.detail,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bam", required=True)
    ap.add_argument("--variants", required=True)
    ap.add_argument("--reference", required=True)
    ap.add_argument("--plan", default=None)
    ap.add_argument("--outdir", required=True)
    args = ap.parse_args(argv)

    plan = load_plan(args.plan)
    min_mapq = int(plan["min_mapping_quality"])
    variants = load_variants(args.variants)
    fasta = pysam.FastaFile(args.reference)

    diagnostics: list[dict[str, Any]] = []
    groups, records_seen = group_fragments(args.bam, min_mapq, diagnostics)
    ends, evidences = build_evidence(groups, variants, fasta, plan)

    # Usable evidence = has an assigned allele with a positive weight.
    usable = {
        (ev.frag, ev.variant_id): ev
        for ev in evidences
        if ev.allele is not None and ev.weight is not None
    }

    components, frags_by_comp = build_components(variants, evidences, usable)

    # Enforce the per-block limits from plan.json.  Exceeding them is reported
    # as a hard diagnostic rather than silently splitting, because splitting
    # would change the optimisation problem.
    max_vars = int(plan["max_variants_per_block"])
    max_frags = int(plan["max_fragments_per_block"])
    for comp in components:
        cid = "block_" + "+".join(comp)
        nf = len(frags_by_comp[cid])
        if len(comp) > max_vars or nf > max_frags:
            diagnostics.append(
                {
                    "type": "block_limit_exceeded",
                    "component": cid,
                    "n_variants": len(comp),
                    "n_fragments": nf,
                    "max_variants": max_vars,
                    "max_fragments": max_frags,
                }
            )

    results: list[ComponentResult] = []
    for comp in components:
        cid = "block_" + "+".join(comp)
        results.append(solve_component(comp, frags_by_comp[cid], usable, cid))

    # Per-locus diagnostics.  Loci with no usable evidence are reported here;
    # they never enter a component and are never represented as allele 0.
    locus_diag: list[dict[str, Any]] = []
    for var in variants:
        n_usable = sum(1 for (_, vid) in usable if vid == var.variant_id)
        statuses: dict[str, int] = {}
        end_statuses: dict[str, int] = {}
        for ev in evidences:
            if ev.variant_id != var.variant_id:
                continue
            statuses[ev.decision] = statuses.get(ev.decision, 0) + 1
        for e in ends:
            if e.variant_id != var.variant_id:
                continue
            end_statuses[e.status] = end_statuses.get(e.status, 0) + 1
        locus_diag.append(
            {
                "variant_id": var.variant_id,
                "position": var.position,
                "usable_evidence": n_usable,
                "decision_counts": statuses,
                "end_status_counts": end_statuses,
                "in_component": any(var.variant_id in res.variant_ids for res in results),
                "note": (
                    "no usable coverage; excluded from components"
                    if n_usable == 0
                    else ("component member" if any(var.variant_id in res.variant_ids for res in results)
                          else "no co-coverage; isolated locus")
                ),
            }
        )

    os.makedirs(args.outdir, exist_ok=True)

    # ---- per-end extraction table
    write_csv(
        os.path.join(args.outdir, "end_observations.csv"),
        [end_row(e) for e in ends],
        list(end_row(ends[0]).keys()) if ends else [
            "fragment", "variant_id", "mate", "flag", "reverse", "mapq", "cigar",
            "ref_start_1based", "query_index", "base", "qual", "status", "detail",
        ],
    )

    # ---- merged evidence table
    ev_rows = [evidence_row(ev) for ev in evidences]
    ev_fields = list(ev_rows[0].keys()) if ev_rows else [
        "fragment", "variant_id", "allele", "weight", "decision", "reason",
    ]
    write_csv(os.path.join(args.outdir, "evidence.csv"), ev_rows, ev_fields)

    # ---- blocks
    block_rows = []
    for res in results:
        block_rows.append(
            {
                "component_id": res.component_id,
                "n_variants": len(res.variant_ids),
                "n_fragments": len(res.fragments),
                "variant_ids": ";".join(res.variant_ids),
                "fragments": ";".join(res.fragments),
                "mec_cost": res.mec_cost,
                "haplotype": "".join(str(x) for x in res.haplotype),
            }
        )
    write_csv(
        os.path.join(args.outdir, "blocks.csv"),
        block_rows,
        ["component_id", "n_variants", "n_fragments", "variant_ids", "fragments",
         "mec_cost", "haplotype"],
    )

    # ---- phase calls
    phase_rows = []
    for res in results:
        for vid, h in zip(res.variant_ids, res.haplotype):
            phase_rows.append(
                {
                    "component_id": res.component_id,
                    "variant_id": vid,
                    "haplotype_value": h,
                }
            )
    write_csv(
        os.path.join(args.outdir, "phase_calls.csv"),
        phase_rows,
        ["component_id", "variant_id", "haplotype_value"],
    )

    # ---- fragment losses
    loss_rows = []
    for res in results:
        for a in res.fragment_assignments:
            loss_rows.append(
                {
                    "component_id": res.component_id,
                    "fragment": a["fragment"],
                    "loss": a["loss"],
                    "assignment": a["assignment"],
                    "status": a["status"],
                    "mismatch_to_h": a["mismatch_to_h"],
                    "mismatch_to_complement": a["mismatch_to_complement"],
                }
            )
    write_csv(
        os.path.join(args.outdir, "fragment_losses.csv"),
        loss_rows,
        ["component_id", "fragment", "loss", "assignment", "status",
         "mismatch_to_h", "mismatch_to_complement"],
    )

    # ---- local probes
    probe_rows = []
    for res in results:
        for p in res.probes:
            probe_rows.append(
                {
                    "component_id": res.component_id,
                    "left_variant": p["left_variant"],
                    "right_variant": p["right_variant"],
                    "main_relative_phase": p["main_relative_phase"],
                    "forced_relative_phase": p["forced_relative_phase"],
                    "main_cost": p["main_cost"],
                    "forced_cost": p["forced_cost"],
                    "increment": p["increment"],
                    "same_cost": p["same_cost"],
                    "alternative_haplotype": "".join(
                        str(x) for x in p["alternative_haplotype"]
                    ),
                }
            )
    write_csv(
        os.path.join(args.outdir, "local_probes.csv"),
        probe_rows,
        ["component_id", "left_variant", "right_variant",
         "main_relative_phase", "forced_relative_phase", "main_cost",
         "forced_cost", "increment", "same_cost", "alternative_haplotype"],
    )

    # ---- locus diagnostics
    write_csv(
        os.path.join(args.outdir, "locus_diagnostics.csv"),
        [
            {
                "variant_id": d["variant_id"],
                "position": d["position"],
                "usable_evidence": d["usable_evidence"],
                "decision_counts": json.dumps(d["decision_counts"], sort_keys=True),
                "end_status_counts": json.dumps(d["end_status_counts"], sort_keys=True),
                "in_component": int(d["in_component"]),
                "note": d["note"],
            }
            for d in locus_diag
        ],
        ["variant_id", "position", "usable_evidence", "decision_counts",
         "end_status_counts", "in_component", "note"],
    )

    # ---- corrected observations (any non-agreeing merged evidence or
    #      adjudicated/undecided call; the raw ends remain in evidence.csv)
    corrected = [
        evidence_row(ev)
        for ev in evidences
        if ev.decision in ("adjudicated", "undecided", "single_read1", "single_read2")
    ]
    write_csv(
        os.path.join(args.outdir, "corrected_observations.csv"),
        corrected,
        ev_fields,
    )

    # ---- JSON bundle
    bundle = {
        "plan": plan,
        "fragments": [
            {
                "fragment": g.frag,
                "status": g.status,
                "read1_cigar": g.read1.cigarstring if g.read1 else None,
                "read2_cigar": g.read2.cigarstring if g.read2 else None,
            }
            for g in sorted(groups.values(), key=lambda x: x.frag)
        ],
        "components": [
            {
                "component_id": res.component_id,
                "variant_ids": res.variant_ids,
                "fragments": res.fragments,
                "mec_cost": res.mec_cost,
                "haplotype": res.haplotype,
                "fragment_assignments": res.fragment_assignments,
                "probes": res.probes,
            }
            for res in results
        ],
        "locus_diagnostics": locus_diag,
        "diagnostics": diagnostics,
        "counts": {
            "bam_records": records_seen,
            "fragments_grouped": len(groups),
            "fragments_complete": sum(1 for g in groups.values() if g.status == "complete"),
            "end_observations": len(ends),
            "evidence_records": len(evidences),
            "usable_evidence": len(usable),
            "components": len(results),
        },
    }
    with open(os.path.join(args.outdir, "phase_result.json"), "w") as fh:
        json.dump(bundle, fh, indent=2)

    return 0


if __name__ == "__main__":
    sys.exit(main())
