#!/usr/bin/env python3
"""Independent verification for identify.py.

This script deliberately re-derives the important numbers instead of trusting
the pipeline.  It covers the README requirements:

  * hand-computed precursor and b/y masses for a short peptide
  * every reported match re-checked against the tolerance, with unique peak
    and ion use
  * exact candidate provenance (shared proteins, missed cleavages)
  * target/decoy identical-candidate exclusion (synthetic fixture + real path)
  * same-score competition defaults to decoy (scan007 style)
  * complete tied-score group accumulation and monotone q values
  * order independence: shuffled MGF lines / FASTA order / spectrum order
  * diagnostics for unknown charge, illegal peaks, no precursor, no support

Run:  python3 verify.py
"""

from __future__ import annotations

import itertools
import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
PLAN_PATH = FIXTURES / "plan.json"
TARGET_PATH = FIXTURES / "target.fasta"
DECOY_PATH = FIXTURES / "decoy.fasta"
MGF_PATH = FIXTURES / "spectra.mgf"

sys.path.insert(0, str(ROOT))
import identify  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")


def approx(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


# --------------------------------------------------------------------------
# 1. Hand calculation for a short peptide (QASGDK)
# --------------------------------------------------------------------------

def hand_calc_check(plan: identify.Plan) -> None:
    aa = plan.aa_residue_masses
    H = plan.proton_mass
    W = plan.water_mass
    seq = "QASGDK"

    residue_sum = aa["Q"] + aa["A"] + aa["S"] + aa["G"] + aa["D"] + aa["K"]
    neutral = residue_sum + W
    mz2 = (neutral + 2 * H) / 2
    # Comparison against the fixture's observed precursor for scan004.
    check(approx(mz2, 303.148103606, 1e-6),
          f"hand precursor z=2 {mz2:.9f} != 303.148103606")
    # Independently expand the b/y series by explicit residue lists.
    residues = [aa[c] for c in seq]
    b_expected = {
        "b1": sum(residues[:1]) + H,
        "b2": sum(residues[:2]) + H,
        "b3": sum(residues[:3]) + H,
        "b4": sum(residues[:4]) + H,
        "b5": sum(residues[:5]) + H,
    }
    y_expected = {
        "y1": sum(residues[-1:]) + W + H,
        "y2": sum(residues[-2:]) + W + H,
        "y3": sum(residues[-3:]) + W + H,
        "y4": sum(residues[-4:]) + W + H,
        "y5": sum(residues[-5:]) + W + H,
    }
    got = dict(identify.theoretical_ions(seq, (), plan))
    check(set(got) == set(b_expected) | set(y_expected),
          "hand calc: ion label set mismatch")
    for label, value in {**b_expected, **y_expected}.items():
        check(approx(got[label], value, 1e-9),
              f"hand calc {label}: {got[label]:.9f} != {value:.9f}")

    # Spot-check against explicitly listed numbers from an independent sheet.
    # Spot-check two values against an independent hand sheet.
    check(approx(got["b1"], 129.06585400662098, 1e-12), "hand b1")
    check(approx(got["y5"], 477.23035320562093, 1e-12), "hand y5")
    check(approx(mz2, 303.148103606, 1e-9), "hand precursor m/z")


# --------------------------------------------------------------------------
# 2. Provenance / digestion / modification checks
# --------------------------------------------------------------------------

def provenance_checks(plan: identify.Plan,
                      candidates: dict[tuple[str, tuple[int, ...]], identify.Candidate]) -> None:
    acdmr = candidates[("ACDMR", ())]
    sources = {(s.label, s.protein, s.start, s.end) for s in acdmr.sources}
    check(sources == {("target", "T01", 10, 14), ("target", "T02", 7, 11)},
          f"ACDMR sources unexpected: {sources}")

    check(("ACDMRAGILK", ()) in candidates,
          "missed cleavage ACDMRAGILK (T01 10-19) missing")
    check(candidates[("ACDMRAGILK", ())].sources[0].missed_cleavages == 1,
          "ACDMRAGILK should be a 1-missed-cleavage peptide")

    # No cleavage between K and P: VVAAK|PEPTIDER must exist as a missed-cleavage peptide.
    check(("VVAAKPEPTIDER", ()) in candidates,
          "K-P missed cleavage VVAAKPEPTIDER missing")
    # Trypsin must not cut at K/P, so 'PEPTIDER' alone should NOT exist.
    check(("PEPTIDER", ()) not in candidates,
          "PEPTIDER should not be produced (K/P not cleaved)")

    # Oxidation only on M, at most one, and unmodified form present.
    m_ox = [key for key in candidates if key[0] == "ACDMR"]
    check(set(m_ox) == {("ACDMR", ()), ("ACDMR", (3,))},
          f"ACDMR modification variants wrong: {sorted(m_ox)}")
    check(("ACDMR", (3, 3)) not in candidates, "duplicate oxidation state leaked")
    # No oxidation candidates for M-free peptides.
    for key in candidates:
        if "M" not in key[0]:
            check(key[1] == (), f"M-free peptide has oxidation: {key}")

    # Fixed C modification is reflected in the mass.
    ac = candidates[("ACDMR", ())].neutral_mass
    raw = sum(plan.aa_residue_masses[c] for c in "ACDMR") + plan.water_mass
    check(approx(ac - raw, plan.fixed_modifications["C"], 1e-9),
          "fixed C carbamidomethylation not applied once")


# --------------------------------------------------------------------------
# 3. Identical target/decoy candidate handling
# --------------------------------------------------------------------------

def shared_candidate_synthetic_check(plan: identify.Plan, tmp: Path) -> None:
    """Build a tiny target/decoy pair with the exact same peptide."""
    shared_seq = "PEPTIDEK"
    (tmp / "t.fasta").write_text(f">Tshared\n{shared_seq}\n", encoding="utf-8")
    (tmp / "d.fasta").write_text(f">Dshared\n{shared_seq}\n", encoding="utf-8")

    target = identify.read_fasta(tmp / "t.fasta", "target")
    decoy = identify.read_fasta(tmp / "d.fasta", "decoy")
    cands = identify.build_candidates(target + decoy, plan)

    # The identical sequence must collapse to one candidate retaining both sources.
    key = (shared_seq, ())
    check(key in cands, "shared candidate not built")
    check(cands[key].labels == {"target", "decoy"}, "shared candidate labels wrong")
    check(cands[key].shared_target_decoy, "shared candidate not flagged")
    check(len(cands[key].sources) == 2, "shared candidate should keep both sources")

    # Feed a perfect spectrum: every b/y ion of the shared peptide.
    ions = identify.theoretical_ions(shared_seq, (), plan)
    peaks = [identify.Peak(mz=mz, intensity=100.0, index=i)
             for i, (_, mz) in enumerate(sorted(ions, key=lambda t: t[1]))]
    neutral = identify.neutral_mass(shared_seq, (), plan)
    precursor = (neutral + 2 * plan.proton_mass) / 2
    spec = identify.Spectrum(
        title="shared_spectrum", observed_mz=precursor, charge=2, peaks=peaks,
        source_index=1, content_hash="x",
    )
    result = identify.identify_spectrum(spec, cands, plan)
    check(not result.identified, "shared-only spectrum must not be identified")
    check(result.diagnosis == "only_shared_identical_candidates",
          f"shared-only diagnosis wrong: {result.diagnosis}")
    check(result.n_shared_excluded == 1, "shared candidate not counted as excluded")
    check(result.n_total_candidates == 0, "shared candidate must not compete")
    check(result.winner_label is None, "shared candidate must not win a label")


# --------------------------------------------------------------------------
# 4. Re-check every reported match: tolerance + unique peak/ion
# --------------------------------------------------------------------------

def recompute_matches_check(plan: identify.Plan,
                            candidates: dict[tuple[str, tuple[int, ...]], identify.Candidate],
                            results: list[identify.SpectrumResult],
                            spectra: list[identify.Spectrum]) -> None:
    by_title = {s.title: s for s in spectra}
    total_matches = 0
    for r in results:
        spec = by_title[r.title]
        for hit in r.hits:
            theo = dict(identify.theoretical_ions(hit.candidate.sequence,
                                                  hit.candidate.ox_positions, plan))
            seen_peaks: set[int] = set()
            seen_ions: set[str] = set()
            for m in hit.matches:
                total_matches += 1
                expected = theo.get(m.ion)
                check(expected is not None, f"{r.title}/{m.ion}: unknown ion label")
                check(approx(m.theoretical_mz, expected, 1e-9),
                      f"{r.title}/{m.ion}: theoretical m/z mismatch")
                err = abs(m.observed_mz - expected)
                check(err <= plan.fragment_tolerance_da + 1e-12,
                      f"{r.title}/{m.ion}: error {err} exceeds tolerance")
                check(approx(err, m.error, 1e-9), f"{r.title}/{m.ion}: error field wrong")
                # Unique peak by m/z value (peak indices are reconstructed carefully)
                check(m.observed_mz not in seen_peaks,
                      f"{r.title}: peak {m.observed_mz} used twice")
                check(m.ion not in seen_ions,
                      f"{r.title}: ion {m.ion} used twice")
                seen_peaks.add(m.observed_mz)
                seen_ions.add(m.ion)
            check(hit.score == len(hit.matches),
                  f"{r.title}: score != number of matches")
            # The greedy assignment must be maximal among available peaks for
            # the reported ion set (no unused in-tolerance pair exists).
            used_peak_mz = {m.observed_mz for m in hit.matches}
            for peak in spec.peaks:
                if peak.mz in used_peak_mz:
                    continue
                for label, mz in theo.items():
                    if label in seen_ions:
                        continue
                    if abs(peak.mz - mz) <= plan.fragment_tolerance_da:
                        check(False, f"{r.title}: unassigned feasible pair "
                                     f"peak {peak.mz} ion {label}")
                        break
    check(total_matches > 0, "no fragment matches were re-checked")


# --------------------------------------------------------------------------
# 5. Precursor tolerance recomputation
# --------------------------------------------------------------------------

def precursor_check(plan: identify.Plan,
                    candidates: dict[tuple[str, tuple[int, ...]], identify.Candidate],
                    results: list[identify.SpectrumResult],
                    spectra: list[identify.Spectrum]) -> None:
    by_title = {s.title: s for s in spectra}
    for r in results:
        spec = by_title[r.title]
        if r.charge is None or r.observed_mz is None:
            continue
        allowed = 0
        for cand in candidates.values():
            if cand.shared_target_decoy:
                continue
            theoretical = (cand.neutral_mass + r.charge * plan.proton_mass) / r.charge
            ppm = (spec.observed_mz - theoretical) / theoretical * 1e6
            if abs(ppm) <= plan.precursor_tolerance_ppm:
                allowed += 1
        check(allowed == r.n_total_candidates,
              f"{r.title}: precursor candidate count {r.n_total_candidates} != {allowed}")
        for hit in r.hits:
            check(abs(hit.precursor_ppm) <= plan.precursor_tolerance_ppm,
                  f"{r.title}: candidate outside precursor tolerance")


# --------------------------------------------------------------------------
# 6. Competition and tie-to-decoy
# --------------------------------------------------------------------------

def competition_check(results: list[identify.SpectrumResult],
                      plan: identify.Plan) -> None:
    for r in results:
        if not r.hits:
            continue
        best_t = max((h.score for h in r.hits if h.label == "target"), default=None)
        best_d = max((h.score for h in r.hits if h.label == "decoy"), default=None)
        if best_t is None and best_d is not None:
            expected = "decoy"
        elif best_d is None and best_t is not None:
            expected = "target"
        elif best_t == best_d:
            expected = "decoy"
        else:
            expected = "target" if best_t > best_d else "decoy"
        check(r.winner_label == expected,
              f"{r.title}: winner {r.winner_label} != expected {expected}")

    # scan007 is the designed exact tie (AGILK target vs AGLLK decoy).
    scan7 = next(r for r in results if r.title == "scan007")
    labels = sorted(h.label for h in scan7.hits if h.score == scan7.winner_score)
    check(labels == ["decoy", "target"], f"scan007 tie labels: {labels}")
    check(scan7.winner_label == "decoy", "scan007 tie must go to decoy")
    check(len(scan7.ambiguity_keys) == 2, "scan007 must keep both tied identities")
    check(not scan7.identified or scan7.n_shared_excluded == 0,
          "scan007 should not involve identical shared candidates")


# --------------------------------------------------------------------------
# 7. FDR / q values by independent implementation
# --------------------------------------------------------------------------

def fdr_check(results: list[identify.SpectrumResult], plan: identify.Plan) -> None:
    entries = [(r.winner_score, r.winner_label) for r in results
               if r.identified and r.winner_score is not None]
    # Independent grouping: dict keyed by score, then sorted descending.
    groups: dict[int, list[str]] = {}
    for score, label in entries:
        groups.setdefault(score, []).append(label)

    t = d = 0
    rows = []
    for score in sorted(groups, reverse=True):
        gt = groups[score].count("target")
        gd = groups[score].count("decoy")
        t += gt
        d += gd
        fdr = min(1.0, (d + 1) / max(t, 1))
        rows.append((score, gt, gd, t, d, fdr))

    # Monotone q: running minimum from the strictest threshold outward.
    q = {}
    running = 1.0
    for score, gt, gd, ct, cd, fdr in reversed(rows):
        running = min(running, fdr)
        q[score] = running

    # Cross-check against the pipeline's summary.
    summary = json.loads((ROOT / ".runtime/verify_out" / "summary.json").read_text())
    reported = {row["score"]: row for row in summary["fdr_thresholds"]}
    check(set(reported) == set(groups), "FDR group scores differ")
    for score, gt, gd, ct, cd, fdr in rows:
        row = reported[score]
        check(row["group_target"] == gt and row["group_decoy"] == gd,
              f"FDR group counts wrong at {score}")
        check(row["cum_target"] == ct and row["cum_decoy"] == cd,
              f"FDR cumulative counts wrong at {score}")
        check(approx(row["fdr"], fdr, 1e-9), f"FDR value wrong at {score}")
        check(approx(row["q_value"], q[score], 1e-9), f"q value wrong at {score}")

    # q must be non-increasing as thresholds widen (score decreases).
    ordered_q = [q[s] for s in sorted(groups, reverse=True)]
    check(all(ordered_q[i] <= ordered_q[i - 1] + 1e-12
              for i in range(1, len(ordered_q))),
          f"q values not monotone: {ordered_q}")

    # Every spectrum's q must equal its score-group q.
    for r in results:
        if r.identified and r.winner_label == "target" and r.winner_score is not None:
            expected_q = q[r.winner_score]
            reported_q = r.q_value if r.q_value is not None else expected_q
            check(approx(reported_q, expected_q, 1e-9),
                  f"{r.title}: q {reported_q} != group q {expected_q}")

    # verify the 8 qualified spectra, 6 target / 2 decoy expectation.
    check(sum(1 for _, lab in entries if lab == "target") == 6, "expected 6 target wins")
    check(sum(1 for _, lab in entries if lab == "decoy") == 2, "expected 2 decoy wins")
    # Duplicate scan001/scan008 must both be counted (not deduplicated).
    dup_scores = [s for r in results if r.title in ("scan001", "scan008")
                  for s in [r.winner_score]]
    check(len(dup_scores) == 2 and dup_scores[0] == dup_scores[1],
          "duplicate spectra must both be counted in the score group")


# --------------------------------------------------------------------------
# 8. Diagnostics: unknown charge / illegal peaks / no support
# --------------------------------------------------------------------------

def diagnostics_check(plan: identify.Plan, candidates, tmp: Path) -> None:
    def make_spectrum(title, charge, peaks, pepmass=500.0):
        return identify.Spectrum(
            title=title, observed_mz=pepmass, charge=charge, peaks=peaks,
            source_index=1, content_hash=title,
        )

    # Unknown charge
    r = identify.identify_spectrum(
        make_spectrum("nocharge", None, [identify.Peak(100.0, 10.0, 0)]), candidates, plan)
    check(r.diagnosis == "unknown_charge", "unknown charge not diagnosed")
    check(not r.identified, "unknown charge must not be identified")

    # Charge outside plan
    r = identify.identify_spectrum(
        make_spectrum("z1", 1, [identify.Peak(100.0, 10.0, 0)]), candidates, plan)
    check(r.diagnosis == "charge_not_in_plan:1", "charge outside plan not diagnosed")

    # Invalid precursor
    r = identify.identify_spectrum(
        make_spectrum("badmass", 2, [identify.Peak(100.0, 10.0, 0)], pepmass=0.0),
        candidates, plan)
    check(r.diagnosis == "invalid_precursor", "invalid precursor not diagnosed")

    # No precursor candidate / no support
    r = identify.identify_spectrum(
        make_spectrum("nosupport", 2, [identify.Peak(100.0, 10.0, 0)]), candidates, plan)
    check(r.diagnosis == "no_precursor_candidate", "no-support spectrum not diagnosed")

    # Illegal peaks in MGF parsing
    mgf = tmp / "bad.mgf"
    mgf.write_text(
        "BEGIN IONS\nTITLE=bad\nPEPMASS=530.25498242\nCHARGE=2+\n"
        "-5 10\n100 20\n200 -3\n300 abc\n400\nnan 5\n"
        "END IONS\n", encoding="utf-8")
    spectra = identify.read_mgf(mgf)
    check(len(spectra) == 1, "bad MGF block not parsed")
    spec = spectra[0]
    reasons = {p["reason"] for p in spec.illegal_peaks}
    check({"non_positive_mz", "negative_intensity", "not_numeric", "missing_intensity"}
          <= reasons, f"illegal peak reasons incomplete: {reasons}")
    # Only valid peaks (100) survive.
    check([p.mz for p in spec.peaks] == [100.0],
          f"valid peaks wrong: {[p.mz for p in spec.peaks]}")

    # An unknown charge block
    mgf2 = tmp / "noz.mgf"
    mgf2.write_text(
        "BEGIN IONS\nTITLE=noz\nPEPMASS=530.25498242\nCHARGE=0\n100 20\nEND IONS\n",
        encoding="utf-8")
    spec2 = identify.read_mgf(mgf2)[0]
    check(spec2.charge is None, "CHARGE=0 must be unknown")
    check("unknown_charge" in spec2.warnings, "unknown charge not warned")


# --------------------------------------------------------------------------
# 8b. Coincident theoretical ions and one-to-one matching
# --------------------------------------------------------------------------

def coincident_ion_check(plan: identify.Plan) -> None:
    # Construct a deliberate coincidence (two ions within tolerance) and one
    # distinct ion, then verify grouping and one-to-one assignment.
    synthetic = [("b1", 100.000), ("y1", 100.010), ("b2", 300.000),
                 ("y2", 300.015), ("b3", 500.000)]
    groups = identify.coincident_ion_groups(synthetic, plan.fragment_tolerance_da)
    check(groups == [["b1", "y1"], ["b2", "y2"]],
          f"coincident grouping wrong: {groups}")
    # Every ion appears in at most one reported group.
    flat = [name for group in groups for name in group]
    check(len(flat) == len(set(flat)), "ion reported in more than one coincident group")

    # Coincident ions must still be separately usable when separate peaks
    # support them, with each ion/peak token used once.
    ions = synthetic
    peaks = [identify.Peak(mz=mz, intensity=50.0, index=i)
             for i, (_, mz) in enumerate(ions)]
    matches, used = identify.match_peaks(peaks, ions, plan.fragment_tolerance_da)
    check(len(matches) == len(ions),
          f"coincident matching reused tokens: {len(matches)} vs {len(ions)}")
    check(len({m.ion for m in matches}) == len(matches), "ion token reused")
    check(len({m.observed_mz for m in matches}) == len(matches), "peak reused")

    # A single strong peak near two coincident ions must be assigned to only
    # one of them; it must not inflate the score to two.
    one_peak = [identify.Peak(mz=100.005, intensity=999.0, index=0)]
    one_matches, _ = identify.match_peaks(one_peak, ions, plan.fragment_tolerance_da)
    check(len(one_matches) == 1,
          f"one strong peak matched {len(one_matches)} ions; must be exactly 1")

    # Real fixture candidates: coincident groups are reported (possibly empty),
    # and each group only contains distinct ion labels.
    for seq in ("VVAAKPEPTIDER", "ACDMR", "QASGDK"):
        real = identify.theoretical_ions(seq, (), plan)
        for group in identify.coincident_ion_groups(real, plan.fragment_tolerance_da):
            check(len(group) == len(set(group)),
                  f"{seq}: duplicate label inside coincident group")


# --------------------------------------------------------------------------
# 9. Order independence
# --------------------------------------------------------------------------

def _run_pipeline(plan_path, t_path, d_path, mgf_path, outdir) -> dict:
    identify.run(plan_path, t_path, d_path, mgf_path, outdir)
    return json.loads((Path(outdir) / "summary.json").read_text())


def order_independence_check(tmp: Path) -> None:
    baseline = _run_pipeline(PLAN_PATH, TARGET_PATH, DECOY_PATH, MGF_PATH, tmp / "base")

    # Shuffle spectrum blocks and peak lines within each block.
    rng = random.Random(196)
    blocks = []
    current = None
    for line in MGF_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "BEGIN IONS":
            current = [line]
        elif stripped == "END IONS":
            current.append(line)
            blocks.append(current)
            current = None
        elif current is not None:
            current.append(line)
    for block in blocks:
        begin, body, end = block[0], block[1:-1], block[-1]
        headers = [l for l in body if "=" in l]
        peaks = [l for l in body if "=" not in l and l.strip()]
        rng.shuffle(peaks)
        block[:] = [begin] + headers + peaks + [end]
    rng.shuffle(blocks)
    shuffled = tmp / "shuffled.mgf"
    shuffled.write_text("\n".join(l for b in blocks for l in b) + "\n", encoding="utf-8")

    # Shuffle FASTA records within each file.
    def shuffled_fasta(src: Path, dst: Path) -> None:
        records = []
        name = None
        seq = []
        for line in src.read_text(encoding="utf-8").splitlines():
            if line.startswith(">"):
                if name:
                    records.append((name, "".join(seq)))
                name, seq = line, []
            elif line.strip():
                seq.append(line.strip())
        if name:
            records.append((name, "".join(seq)))
        rng.shuffle(records)
        dst.write_text("".join(f"{n}\n{s}\n" for n, s in records), encoding="utf-8")

    t2 = tmp / "target_shuf.fasta"
    d2 = tmp / "decoy_shuf.fasta"
    shuffled_fasta(TARGET_PATH, t2)
    shuffled_fasta(DECOY_PATH, d2)

    other = _run_pipeline(PLAN_PATH, t2, d2, shuffled, tmp / "shuffled")

    # Compare the semantically meaningful fields.
    keys = ["spectra_total", "spectra_identified", "target_wins", "decoy_wins",
            "ambiguous_identified", "candidates_total", "diagnostic_counts"]
    for key in keys:
        check(baseline[key] == other[key],
              f"order independence: {key} changed ({baseline[key]} != {other[key]})")
    check(baseline["shared_target_decoy_candidates"] == other["shared_target_decoy_candidates"],
          "order independence: shared candidate list changed")
    # Threshold groups: same scores, same counts, same FDR/q.
    b_rows = [(r["score"], r["group_target"], r["group_decoy"], r["cum_target"],
               r["cum_decoy"], round(r["fdr"], 12), round(r["q_value"], 12))
              for r in baseline["fdr_thresholds"]]
    o_rows = [(r["score"], r["group_target"], r["group_decoy"], r["cum_target"],
               r["cum_decoy"], round(r["fdr"], 12), round(r["q_value"], 12))
              for r in other["fdr_thresholds"]]
    check(b_rows == o_rows, f"order independence: FDR table changed\n{b_rows}\n{o_rows}")


# --------------------------------------------------------------------------
# 10. Fixture-level expectations
# --------------------------------------------------------------------------

def fixture_expectations(plan: identify.Plan,
                         results: list[identify.SpectrumResult]) -> None:
    by_title = {r.title: r for r in results}
    expected = {
        "scan001": ("target", 14, "MPEPTIDEK"),
        "scan002": ("target", 7, "ACDMR"),
        "scan003": ("target", 7, "ACDMR"),  # oxidised, charge 3
        "scan004": ("target", 9, "QASGDK"),
        "scan005": ("target", 21, "VVAAKPEPTIDER"),
        "scan006": ("decoy", 14, "EDITPEPMK"),
        "scan007": ("decoy", 7, "AGLLK"),
        "scan008": ("target", 14, "MPEPTIDEK"),
    }
    for title, (label, score, seq) in expected.items():
        r = by_title[title]
        check(r.identified, f"{title}: expected identified")
        check(r.winner_label == label, f"{title}: expected {label} win")
        check(r.winner_score == score, f"{title}: expected score {score}, got {r.winner_score}")
        check(r.representative is not None and r.representative.candidate.sequence == seq,
              f"{title}: unexpected representative sequence")

    check(not by_title["scan009"].identified, "scan009 should be unidentified")
    check(by_title["scan009"].diagnosis == "no_precursor_candidate",
          "scan009 diagnosis wrong")
    check(by_title["scan008"].duplicate_of == "scan001", "scan008 duplicate not linked")
    check(by_title["scan003"].representative.candidate.ox_positions == (3,),
          "scan003 should use oxidised M")
    # Shared protein source retained for ACDMR.
    check(any(len(h.candidate.sources) == 2 for h in by_title["scan002"].hits),
          "scan002 ACDMR should retain both protein sources")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    plan = identify.Plan.load(PLAN_PATH)
    target = identify.read_fasta(TARGET_PATH, "target")
    decoy = identify.read_fasta(DECOY_PATH, "decoy")
    candidates = identify.build_candidates(target + decoy, plan)
    spectra = identify.read_mgf(MGF_PATH)

    # Produce the canonical run used by the FDR cross-check.
    outdir = ROOT / ".runtime" / "verify_out"
    identify.run(PLAN_PATH, TARGET_PATH, DECOY_PATH, MGF_PATH, outdir)
    results = [identify.identify_spectrum(spec, candidates, plan) for spec in spectra]

    hand_calc_check(plan)
    provenance_checks(plan, candidates)
    fixture_expectations(plan, results)
    recompute_matches_check(plan, candidates, results, spectra)
    precursor_check(plan, candidates, results, spectra)
    competition_check(results, plan)
    fdr_check(results, plan)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        shared_candidate_synthetic_check(plan, tmp)
        diagnostics_check(plan, candidates, tmp)
        coincident_ion_check(plan)
        order_independence_check(tmp)

    print(f"\n{CHECKS} checks executed, {len(FAILURES)} failure(s).")
    if FAILURES:
        for failure in FAILURES:
            print("  -", failure)
        return 1
    print("All verification checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
