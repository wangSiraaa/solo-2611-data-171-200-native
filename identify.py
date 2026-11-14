#!/usr/bin/env python3
"""Small MS2 peptide-spectrum matching with target/decoy FDR evidence.

Python 3.11, standard library only.

The program is deliberately a *small synthetic-database* identification demo:

  * trypsin digestion (K/R after, not before P, protein ends are boundaries)
  * 0 or 1 missed cleavages, peptide length from the plan
  * fixed C carbamidomethylation, at most one variable M oxidation
  * precursor filtering by ppm on the theoretical m/z
  * b/y 1+ fragment ions, one-to-one peak/ion assignment, unified tolerance
  * per-spectrum target/decoy competition (score ties go to decoy)
  * complete tied-score groups, (D+1)/max(T,1) FDR, monotone q values

It does NOT do MS1 quantification, database download, open modification
search, or protein-level inference.  See ``report.md`` for limitations.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence


# --------------------------------------------------------------------------
# Plan / configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    proton_mass: float
    water_mass: float
    aa_residue_masses: dict[str, float]
    fixed_modifications: dict[str, float]
    variable_modifications: dict[str, float]
    max_variable_modifications: int
    max_missed_cleavages: int
    peptide_length: tuple[int, int]
    precursor_charges: tuple[int, ...]
    precursor_tolerance_ppm: float
    fragment_tolerance_da: float
    fragment_series: tuple[str, ...]
    fragment_charge: int
    min_matched_fragments: int
    target_decoy_tie: str = "decoy"
    seed: int = 0

    @classmethod
    def load(cls, path: str | Path) -> "Plan":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            proton_mass=float(raw["proton_mass"]),
            water_mass=float(raw["water_mass"]),
            aa_residue_masses={k: float(v) for k, v in raw["aa_residue_masses"].items()},
            fixed_modifications={k: float(v) for k, v in raw.get("fixed_modifications", {}).items()},
            variable_modifications={k: float(v) for k, v in raw.get("variable_modifications", {}).items()},
            max_variable_modifications=int(raw.get("max_variable_modifications", 0)),
            max_missed_cleavages=int(raw.get("max_missed_cleavages", 0)),
            peptide_length=(int(raw["peptide_length"][0]), int(raw["peptide_length"][1])),
            precursor_charges=tuple(int(z) for z in raw["precursor_charges"]),
            precursor_tolerance_ppm=float(raw["precursor_tolerance_ppm"]),
            fragment_tolerance_da=float(raw["fragment_tolerance_da"]),
            fragment_series=tuple(raw.get("fragment_series", ["b", "y"])),
            fragment_charge=int(raw.get("fragment_charge", 1)),
            min_matched_fragments=int(raw.get("min_matched_fragments", 1)),
            target_decoy_tie=str(raw.get("target_decoy_tie", "decoy")),
            seed=int(raw.get("seed", 0)),
        )


# --------------------------------------------------------------------------
# FASTA
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Protein:
    name: str
    sequence: str
    label: str  # "target" or "decoy"


def read_fasta(path: str | Path, label: str) -> list[Protein]:
    proteins: list[Protein] = []
    name: str | None = None
    chunks: list[str] = []

    def flush() -> None:
        if name is not None:
            proteins.append(Protein(name=name, sequence="".join(chunks), label=label))

    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):
            flush()
            name = line[1:].split()[0]
            chunks = []
        else:
            if name is None:
                raise ValueError(f"FASTA {path}: sequence before header")
            chunks.append(line)
    flush()
    return proteins


# --------------------------------------------------------------------------
# Trypsin digestion and candidate generation
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class DigestPeptide:
    start: int  # 1-based inclusive
    end: int    # 1-based inclusive
    sequence: str
    missed_cleavages: int


def trypsin_sites(sequence: str) -> list[int]:
    """Return 0-based cut boundaries, including protein ends.

    Cleavage occurs after K/R unless the following residue is P.
    """
    sites = [0]
    for i in range(len(sequence) - 1):
        if sequence[i] in ("K", "R") and sequence[i + 1] != "P":
            sites.append(i + 1)
    sites.append(len(sequence))
    return sites


def digest(sequence: str, plan: Plan) -> list[DigestPeptide]:
    sites = trypsin_sites(sequence)
    lo, hi = plan.peptide_length
    out: list[DigestPeptide] = []
    # 0 missed cleavages
    for a, b in zip(sites, sites[1:]):
        pep = sequence[a:b]
        if lo <= len(pep) <= hi:
            out.append(DigestPeptide(a + 1, b, pep, 0))
    # 1 missed cleavage: merge two adjacent tryptic segments
    if plan.max_missed_cleavages >= 1:
        for i in range(len(sites) - 2):
            a, b = sites[i], sites[i + 2]
            pep = sequence[a:b]
            if lo <= len(pep) <= hi:
                out.append(DigestPeptide(a + 1, b, pep, 1))
    return out


@dataclass(frozen=True)
class Source:
    label: str
    protein: str
    start: int
    end: int
    missed_cleavages: int


@dataclass
class Candidate:
    sequence: str
    ox_positions: tuple[int, ...]
    neutral_mass: float
    sources: list[Source] = field(default_factory=list)
    labels: set[str] = field(default_factory=set)

    @property
    def key(self) -> tuple[str, tuple[int, ...]]:
        return self.sequence, self.ox_positions

    @property
    def shared_target_decoy(self) -> bool:
        """True when target and decoy produce the *same* chemical candidate."""
        return {"target", "decoy"} <= self.labels

    def modified_sequence(self) -> str:
        # Simple, unambiguous textual notation for reports.
        return "".join(
            f"{aa}[ox]" if i in self.ox_positions else aa
            for i, aa in enumerate(self.sequence)
        )


def _variable_patterns(sequence: str, plan: Plan) -> Iterable[tuple[int, ...]]:
    yield ()
    positions = [i for i, aa in enumerate(sequence) if aa in plan.variable_modifications]
    max_mods = min(plan.max_variable_modifications, len(positions))
    for r in range(1, max_mods + 1):
        for combo in itertools.combinations(positions, r):
            yield combo


def residue_masses(sequence: str, ox_positions: tuple[int, ...], plan: Plan) -> list[float]:
    ox = set(ox_positions)
    return [
        plan.aa_residue_masses[aa]
        + plan.fixed_modifications.get(aa, 0.0)
        + (plan.variable_modifications.get(aa, 0.0) if i in ox else 0.0)
        for i, aa in enumerate(sequence)
    ]


def neutral_mass(sequence: str, ox_positions: tuple[int, ...], plan: Plan) -> float:
    return sum(residue_masses(sequence, ox_positions, plan)) + plan.water_mass


def build_candidates(proteins: Sequence[Protein], plan: Plan) -> dict[tuple[str, tuple[int, ...]], Candidate]:
    candidates: dict[tuple[str, tuple[int, ...]], Candidate] = {}
    for protein in sorted(proteins, key=lambda p: (p.label, p.name)):
        for peptide in digest(protein.sequence, plan):
            for ox in _variable_patterns(peptide.sequence, plan):
                key = (peptide.sequence, ox)
                cand = candidates.get(key)
                if cand is None:
                    cand = Candidate(
                        sequence=peptide.sequence,
                        ox_positions=ox,
                        neutral_mass=neutral_mass(peptide.sequence, ox, plan),
                    )
                    candidates[key] = cand
                src = Source(
                    label=protein.label,
                    protein=protein.name,
                    start=peptide.start,
                    end=peptide.end,
                    missed_cleavages=peptide.missed_cleavages,
                )
                if src not in cand.sources:
                    cand.sources.append(src)
                cand.labels.add(protein.label)
    for cand in candidates.values():
        cand.sources.sort(key=lambda s: (s.label, s.protein, s.start, s.end, s.missed_cleavages))
    return candidates


# --------------------------------------------------------------------------
# MGF parsing
# --------------------------------------------------------------------------

@dataclass
class Peak:
    mz: float
    intensity: float
    index: int  # assigned after deterministic sorting


@dataclass
class Spectrum:
    title: str
    observed_mz: float | None
    charge: int | None
    peaks: list[Peak]
    warnings: list[str] = field(default_factory=list)
    illegal_peaks: list[dict] = field(default_factory=list)
    source_index: int = 0
    content_hash: str = ""
    duplicate_of: str | None = None

    @property
    def total_intensity(self) -> float:
        return sum(p.intensity for p in self.peaks)


def _parse_charge(text: str) -> int | None:
    token = text.strip().split()[0].rstrip(",")
    negative = "-" in token
    cleaned = token.replace("+", "").replace("-", "")
    try:
        z = int(float(cleaned))
    except ValueError:
        return None
    if z == 0:
        return None
    return -z if negative else z


def _normalise_peaks(raw: list[tuple[float, float]], warnings: list[str],
                     illegal: list[dict]) -> list[Peak]:
    valid: list[tuple[float, float]] = []
    for mz, intensity in raw:
        reason = None
        if not math.isfinite(mz) or not math.isfinite(intensity):
            reason = "non_finite"
        elif mz <= 0:
            reason = "non_positive_mz"
        elif intensity < 0:
            reason = "negative_intensity"
        if reason:
            illegal.append({"mz": mz, "intensity": intensity, "reason": reason})
            warnings.append(f"illegal_peak:{reason}")
            continue
        valid.append((mz, intensity))
    # Sorting makes matching independent of peak line order.
    valid.sort(key=lambda t: (t[0], t[1]))
    return [Peak(mz=mz, intensity=inten, index=i) for i, (mz, inten) in enumerate(valid)]


def read_mgf(path: str | Path) -> list[Spectrum]:
    spectra: list[Spectrum] = []
    current: dict | None = None
    raw_peaks: list[tuple[float, float]] = []
    warnings: list[str] = []
    illegal: list[dict] = []
    title: str | None = None
    pepmass: float | None = None
    charge: int | None = None
    block_index = 0

    def finalise() -> None:
        nonlocal current, raw_peaks, warnings, illegal, title, pepmass, charge, block_index
        if current is None:
            return
        block_index += 1
        if title is None:
            title = f"spectrum_{block_index}"
            warnings.append("missing_title")
        peaks = _normalise_peaks(raw_peaks, warnings, illegal)
        content = json.dumps(
            {"pepmass": pepmass, "charge": charge,
             "peaks": [[round(p.mz, 9), round(p.intensity, 6)] for p in peaks]},
            sort_keys=True,
        )
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        spectra.append(Spectrum(
            title=title,
            observed_mz=pepmass,
            charge=charge,
            peaks=peaks,
            warnings=warnings,
            illegal_peaks=illegal,
            source_index=block_index,
            content_hash=digest,
        ))
        current = None
        raw_peaks = []
        warnings = []
        illegal = []
        title = None
        pepmass = None
        charge = None

    for line_no, raw_line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        upper = line.upper()
        if upper == "BEGIN IONS":
            finalise()
            current = {"line": line_no}
            continue
        if upper == "END IONS":
            finalise()
            continue
        if current is None:
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            key = key.strip().upper()
            value = value.strip()
            if key == "TITLE":
                title = value
            elif key == "PEPMASS":
                try:
                    pepmass = float(value.split()[0])
                except (ValueError, IndexError):
                    warnings.append(f"bad_pepmass:{value}")
            elif key == "CHARGE":
                charge = _parse_charge(value)
                if charge is None:
                    warnings.append("unknown_charge")
            continue
        parts = line.split()
        if len(parts) < 2:
            illegal.append({"line": line_no, "text": line, "reason": "missing_intensity"})
            warnings.append("illegal_peak:missing_intensity")
            continue
        try:
            mz = float(parts[0])
            intensity = float(parts[1])
        except ValueError:
            illegal.append({"line": line_no, "text": line, "reason": "not_numeric"})
            warnings.append("illegal_peak:not_numeric")
            continue
        raw_peaks.append((mz, intensity))
    finalise()

    # Duplicate-content census.  All spectra are kept as separate observations;
    # the canonical title is the lexicographically smallest title in the group so
    # that this linkage does not depend on input order.
    hashes: dict[str, list[str]] = {}
    for spec in spectra:
        hashes.setdefault(spec.content_hash, []).append(spec.title)
    canonical = {h: min(titles) for h, titles in hashes.items()}
    for spec in spectra:
        canon = canonical[spec.content_hash]
        spec.duplicate_of = None if spec.title == canon else canon
    return spectra


# --------------------------------------------------------------------------
# Theoretical ions and matching
# --------------------------------------------------------------------------

def theoretical_ions(sequence: str, ox_positions: tuple[int, ...], plan: Plan) -> list[tuple[str, float]]:
    masses = residue_masses(sequence, ox_positions, plan)
    n = len(masses)
    z = plan.fragment_charge
    ions: list[tuple[str, float]] = []
    if "b" in plan.fragment_series:
        prefix = 0.0
        for i in range(n - 1):
            prefix += masses[i]
            ions.append((f"b{i + 1}", (prefix + z * plan.proton_mass) / z))
    if "y" in plan.fragment_series:
        suffix = 0.0
        for i in range(n - 1):
            suffix += masses[n - 1 - i]
            ions.append((f"y{i + 1}", (suffix + plan.water_mass + z * plan.proton_mass) / z))
    return ions


def coincident_ion_groups(ions: Sequence[tuple[str, float]], tolerance: float) -> list[list[str]]:
    """Groups of theoretical ions that could compete for the same peak.

    Two ions are coincident when their m/z are within the fragment tolerance.
    Such ions remain distinct in the one-to-one assignment (each ion token may
    be used once, each peak once), so the group is reported explicitly rather
    than silently collapsed.
    """
    groups: list[list[str]] = []
    used: set[int] = set()
    for i, (name_i, mz_i) in enumerate(ions):
        if i in used:
            continue
        group = [name_i]
        for j in range(i + 1, len(ions)):
            if j in used:
                continue
            name_j, mz_j = ions[j]
            if abs(mz_i - mz_j) <= tolerance:
                group.append(name_j)
                used.add(j)
        if len(group) > 1:
            groups.append(group)
    return groups


@dataclass
class Match:
    ion: str
    observed_mz: float
    theoretical_mz: float
    error: float
    intensity: float


def match_peaks(peaks: Sequence[Peak], ions: Sequence[tuple[str, float]],
                tolerance: float) -> tuple[list[Match], set[int]]:
    """Deterministic one-to-one greedy matching.

    All candidate (peak, ion) pairs within tolerance are sorted by absolute
    mass error, then by observed m/z and ion index.  A peak and an ion may be
    used at most once.  The rule is order-independent and easy to audit.
    """
    edges: list[tuple[float, float, int, int]] = []
    for ion_idx, (_, ion_mz) in enumerate(ions):
        for peak in peaks:
            error = abs(peak.mz - ion_mz)
            if error <= tolerance:
                edges.append((error, peak.mz, ion_idx, peak.index))
    edges.sort(key=lambda e: (e[0], e[1], e[2], e[3]))
    used_peak: set[int] = set()
    used_ion: set[int] = set()
    matches: list[Match] = []
    for error, _, ion_idx, peak_idx in edges:
        if peak_idx in used_peak or ion_idx in used_ion:
            continue
        used_peak.add(peak_idx)
        used_ion.add(ion_idx)
        ion_name, ion_mz = ions[ion_idx]
        peak = peaks[peak_idx]
        matches.append(Match(
            ion=ion_name,
            observed_mz=peak.mz,
            theoretical_mz=ion_mz,
            error=error,
            intensity=peak.intensity,
        ))
    matches.sort(key=lambda m: (m.theoretical_mz, m.ion))
    return matches, used_peak


# --------------------------------------------------------------------------
# Per-spectrum identification
# --------------------------------------------------------------------------

@dataclass
class CandidateHit:
    candidate: Candidate
    precursor_ppm: float
    score: int
    matches: list[Match]
    used_peak_indices: set[int]
    matched_intensity: float
    mean_abs_error: float
    coincident_ions: list[list[str]] = field(default_factory=list)

    @property
    def label(self) -> str:
        # Shared target/decoy candidates are excluded before scoring.
        return next(iter(self.candidate.labels))

    def intensity_fraction(self, total: float) -> float:
        return self.matched_intensity / total if total > 0 else 0.0


@dataclass
class SpectrumResult:
    title: str
    charge: int | None
    observed_mz: float | None
    identified: bool
    qualified: bool
    diagnosis: str
    winner_label: str | None
    winner_score: int | None
    n_total_candidates: int
    n_target_candidates: int
    n_decoy_candidates: int
    n_shared_excluded: int
    hits: list[CandidateHit]
    ambiguity_keys: list[tuple[str, tuple[int, ...]]]
    representative: CandidateHit | None
    unmatched_peaks: list[Peak]
    q_value: float | None = None
    duplicate_of: str | None = None
    warnings: list[str] = field(default_factory=list)
    illegal_peaks: list[dict] = field(default_factory=list)
    content_hash: str = ""


def _first_hit_at(hits: Sequence[CandidateHit], score: int, label: str | None = None) -> CandidateHit | None:
    pool = [h for h in hits if h.score == score and (label is None or h.label == label)]
    if not pool:
        return None
    pool.sort(key=lambda h: (h.label, h.candidate.sequence, h.candidate.ox_positions))
    return pool[0]


def identify_spectrum(spec: Spectrum, candidates: dict[tuple[str, tuple[int, ...]], Candidate],
                      plan: Plan) -> SpectrumResult:
    warnings = list(spec.warnings)

    def result(**kwargs) -> SpectrumResult:
        base = dict(
            title=spec.title,
            charge=spec.charge,
            observed_mz=spec.observed_mz,
            identified=False,
            qualified=False,
            diagnosis="",
            winner_label=None,
            winner_score=None,
            n_total_candidates=0,
            n_target_candidates=0,
            n_decoy_candidates=0,
            n_shared_excluded=0,
            hits=[],
            ambiguity_keys=[],
            representative=None,
            unmatched_peaks=list(spec.peaks),
            duplicate_of=spec.duplicate_of,
            warnings=warnings,
            illegal_peaks=spec.illegal_peaks,
            content_hash=spec.content_hash,
        )
        base.update(kwargs)
        return SpectrumResult(**base)

    if spec.observed_mz is None or not math.isfinite(spec.observed_mz) or spec.observed_mz <= 0:
        return result(diagnosis="invalid_precursor")
    if spec.charge is None or spec.charge <= 0:
        return result(diagnosis="unknown_charge")
    if spec.charge not in plan.precursor_charges:
        return result(diagnosis=f"charge_not_in_plan:{spec.charge}")

    in_window: list[tuple[Candidate, float]] = []
    shared: list[tuple[Candidate, float]] = []
    for cand in candidates.values():
        theo_mz = (cand.neutral_mass + spec.charge * plan.proton_mass) / spec.charge
        ppm = (spec.observed_mz - theo_mz) / theo_mz * 1e6
        if abs(ppm) <= plan.precursor_tolerance_ppm:
            if cand.shared_target_decoy:
                shared.append((cand, ppm))
            else:
                in_window.append((cand, ppm))

    n_target = sum(1 for c, _ in in_window if "target" in c.labels)
    n_decoy = sum(1 for c, _ in in_window if "decoy" in c.labels)

    if not in_window:
        diagnosis = "only_shared_identical_candidates" if shared else "no_precursor_candidate"
        return result(
            diagnosis=diagnosis,
            n_shared_excluded=len(shared),
        )

    hits: list[CandidateHit] = []
    for cand, ppm in in_window:
        ions = theoretical_ions(cand.sequence, cand.ox_positions, plan)
        matches, used = match_peaks(spec.peaks, ions, plan.fragment_tolerance_da)
        matched_intensity = sum(m.intensity for m in matches)
        mean_error = (sum(m.error for m in matches) / len(matches)) if matches else 0.0
        hits.append(CandidateHit(
            candidate=cand,
            precursor_ppm=ppm,
            score=len(matches),
            matches=matches,
            used_peak_indices=used,
            matched_intensity=matched_intensity,
            mean_abs_error=mean_error,
            coincident_ions=coincident_ion_groups(ions, plan.fragment_tolerance_da),
        ))
    hits.sort(key=lambda h: (-h.score, h.label, h.candidate.sequence, h.candidate.ox_positions))

    best_target = max((h.score for h in hits if h.label == "target"), default=-1)
    best_decoy = max((h.score for h in hits if h.label == "decoy"), default=-1)

    # Competition uses the same scoring for both labels.  Equal best scores
    # go to decoy, per the plan.
    if best_target < 0 and best_decoy < 0:
        diagnosis = "no_precursor_candidate"
        winner_label = None
        winner_score = None
    elif best_target < 0:
        winner_label, winner_score = "decoy", best_decoy
    elif best_decoy < 0:
        winner_label, winner_score = "target", best_target
    elif best_target == best_decoy:
        winner_label, winner_score = "decoy", best_decoy
    elif best_target > best_decoy:
        winner_label, winner_score = "target", best_target
    else:
        winner_label, winner_score = "decoy", best_decoy

    assert winner_score is not None and winner_label is not None

    # Every candidate at the best score is retained as an ambiguous identity.
    ambiguity = [h for h in hits if h.score == winner_score]
    ambiguity.sort(key=lambda h: (h.label, h.candidate.sequence, h.candidate.ox_positions))
    ambiguity_keys = [h.candidate.key for h in ambiguity]

    representative = next((h for h in ambiguity if h.label == winner_label), ambiguity[0])
    unmatched = [p for p in spec.peaks if p.index not in representative.used_peak_indices]

    qualified = winner_score >= plan.min_matched_fragments
    if not qualified:
        diagnosis = "insufficient_fragments"
    else:
        diagnosis = "identified"
    if qualified and len(ambiguity) > 1:
        diagnosis = "identified_ambiguous"

    return result(
        identified=qualified,
        qualified=qualified,
        diagnosis=diagnosis,
        winner_label=winner_label,
        winner_score=winner_score,
        n_total_candidates=len(in_window),
        n_target_candidates=n_target,
        n_decoy_candidates=n_decoy,
        n_shared_excluded=len(shared),
        hits=hits,
        ambiguity_keys=ambiguity_keys,
        representative=representative,
        unmatched_peaks=unmatched,
    )


# --------------------------------------------------------------------------
# FDR / q values
# --------------------------------------------------------------------------

def compute_fdr(entries: Sequence[tuple[int, str]]) -> tuple[list[dict], dict[int, float]]:
    """Compute FDR and q values at complete tied-score groups.

    ``entries`` is a sequence of (score, winner_label) for qualified winning
    spectra.  Labels are "target" or "decoy".

    Returns the ordered group table (highest score first) and a mapping from
    score to the group q value.  Identical scores are never split.
    """
    groups: dict[int, list[str]] = {}
    for score, label in entries:
        groups.setdefault(score, []).append(label)

    rows: list[dict] = []
    cum_t = 0
    cum_d = 0
    for score in sorted(groups, reverse=True):
        labels = groups[score]
        group_t = labels.count("target")
        group_d = labels.count("decoy")
        cum_t += group_t
        cum_d += group_d
        fdr = min(1.0, (cum_d + 1) / max(cum_t, 1))
        rows.append({
            "score": score,
            "group_target": group_t,
            "group_decoy": group_d,
            "cum_target": cum_t,
            "cum_decoy": cum_d,
            "fdr": fdr,
        })

    # q is the minimum FDR over all thresholds that include the spectrum,
    # i.e. over this and every wider (lower-score) complete group.
    running = 1.0
    for row in reversed(rows):
        running = min(running, row["fdr"])
        row["q_value"] = running

    q_by_score = {row["score"]: row["q_value"] for row in rows}
    return rows, q_by_score


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

def _fmt(x: float | None, digits: int = 6) -> str:
    if x is None:
        return ""
    if isinstance(x, float) and not math.isfinite(x):
        return ""
    return f"{x:.{digits}f}"


def _tsv(path: Path, header: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write("\t".join(header) + "\n")
        for row in rows:
            handle.write("\t".join(str(v) for v in row) + "\n")


def _source_text(cand: Candidate) -> str:
    return ";".join(
        f"{s.label}:{s.protein}:{s.start}-{s.end}:mc={s.missed_cleavages}"
        for s in cand.sources
    )


def _write_candidates(path: Path, candidates: dict[tuple[str, tuple[int, ...]], Candidate]) -> None:
    rows = []
    for cand in sorted(candidates.values(), key=lambda c: (c.sequence, c.ox_positions)):
        rows.append([
            cand.sequence,
            cand.modified_sequence(),
            ",".join(str(p) for p in cand.ox_positions),
            ",".join(sorted(cand.labels)),
            _source_text(cand),
            _fmt(cand.neutral_mass, 9),
            "yes" if cand.shared_target_decoy else "no",
            "yes" if cand.shared_target_decoy else "no",
        ])
    _tsv(path,
         ["sequence", "modified_sequence", "ox_positions", "labels", "sources",
          "neutral_mass", "shared_target_decoy_identical", "excluded_from_competition"],
         rows)


def _spectrum_rows(results: Sequence[SpectrumResult]) -> list[list[object]]:
    rows = []
    for r in results:
        rep = r.representative
        matched_ions = ",".join(m.ion for m in rep.matches) if rep else ""
        unmatched = ",".join(_fmt(p.mz, 4) for p in r.unmatched_peaks)
        score_basis = ""
        if rep is not None:
            total_int = sum(p.intensity for p in r.unmatched_peaks) + rep.matched_intensity
            score_basis = (
                f"ion_count={rep.score};"
                f"matched_intensity={_fmt(rep.matched_intensity, 3)};"
                f"intensity_fraction={_fmt(rep.intensity_fraction(total_int), 4)};"
                f"mean_abs_error_da={_fmt(rep.mean_abs_error, 6)}"
            )
        ambiguity = ",".join(f"{seq}[ox={ox or '-'}]" for seq, ox in r.ambiguity_keys)
        rows.append([
            r.title, "" if r.charge is None else r.charge, _fmt(r.observed_mz, 6),
            "yes" if r.identified else "no", r.diagnosis,
            r.n_total_candidates, r.n_target_candidates, r.n_decoy_candidates,
            r.n_shared_excluded, "" if r.winner_label is None else r.winner_label,
            "" if r.winner_score is None else r.winner_score,
            len(r.ambiguity_keys), ambiguity,
            "" if rep is None else _fmt(rep.precursor_ppm, 4),
            "" if rep is None else len(rep.matches), matched_ions,
            len(r.unmatched_peaks), unmatched, score_basis,
            "" if r.q_value is None else _fmt(r.q_value, 6),
            "" if r.duplicate_of is None else r.duplicate_of,
            ";".join(sorted(set(r.warnings))),
        ])
    return rows


SPECTRA_HEADER = [
    "title", "charge", "precursor_mz_observed", "identified", "diagnosis",
    "n_candidates_in_ppm", "n_target_candidates", "n_decoy_candidates",
    "n_shared_excluded", "winner_label", "winner_score", "n_best_tied_identities",
    "best_tied_identities", "winner_precursor_ppm", "n_matched_ions",
    "matched_ions_representative", "n_unused_peaks", "unused_peak_mz",
    "score_basis_representative", "q_value", "duplicate_of", "warnings",
]


def write_outputs(results: Sequence[SpectrumResult],
                  candidates: dict[tuple[str, tuple[int, ...]], Candidate],
                  plan: Plan, mgf_path: str | Path,
                  outdir: str | Path,
                  input_paths: dict[str, str | Path] | None = None) -> dict:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)

    results = sorted(results, key=lambda r: (r.title, r.content_hash))
    _write_candidates(out / "candidates.tsv", candidates)

    # --- FDR first so q values are available in the spectra table -----------
    entries = [
        (r.winner_score, r.winner_label)
        for r in results
        if r.qualified and r.winner_score is not None and r.winner_label is not None
    ]
    fdr_rows, q_by_score = compute_fdr(entries)
    for r in results:
        if r.identified and r.winner_label == "target" and r.winner_score is not None:
            r.q_value = q_by_score[r.winner_score]

    # --- per-spectrum table -------------------------------------------------
    _tsv(out / "spectra.tsv", SPECTRA_HEADER, _spectrum_rows(results))

    # --- ambiguity table ----------------------------------------------------
    ambiguity_rows = []
    for r in results:
        best = r.winner_score
        if best is None:
            continue
        for hit in r.hits:
            if hit.score != best:
                continue
            ambiguity_rows.append([
                r.title,
                hit.candidate.sequence,
                hit.candidate.modified_sequence(),
                ",".join(str(p) for p in hit.candidate.ox_positions),
                hit.label,
                _source_text(hit.candidate),
                _fmt(hit.precursor_ppm, 4),
                hit.score,
                ",".join(m.ion for m in hit.matches),
                _fmt(hit.matched_intensity, 3),
                _fmt(hit.mean_abs_error, 6),
                "yes" if r.winner_label == hit.label else "no",
            ])
    _tsv(out / "ambiguity.tsv", [
        "title", "sequence", "modified_sequence", "ox_positions", "label", "sources",
        "precursor_ppm", "score", "matched_ions", "matched_intensity",
        "mean_abs_error_da", "in_winning_label",
    ], ambiguity_rows)

    # --- all scored candidate hits -----------------------------------------
    hit_rows = []
    for r in results:
        for hit in r.hits:
            hit_rows.append([
                r.title,
                hit.candidate.sequence,
                hit.candidate.modified_sequence(),
                ",".join(str(p) for p in hit.candidate.ox_positions),
                hit.label,
                _source_text(hit.candidate),
                _fmt(hit.precursor_ppm, 4),
                hit.score,
                len(hit.matches),
                ",".join(m.ion for m in hit.matches),
                _fmt(hit.matched_intensity, 3),
                _fmt(hit.mean_abs_error, 6),
                "yes" if r.winner_score is not None and hit.score == r.winner_score else "no",
            ])
    _tsv(out / "candidate_hits.tsv", [
        "title", "sequence", "modified_sequence", "ox_positions", "label", "sources",
        "precursor_ppm", "score", "n_matched_ions", "matched_ions",
        "matched_intensity", "mean_abs_error_da", "is_best_score",
    ], hit_rows)

    # --- FDR table (computed above) -----------------------------------------
    table_rows = []
    for rank, row in enumerate(fdr_rows, 1):
        titles = sorted(
            r.title for r in results
            if r.qualified and r.winner_score == row["score"]
        )
        table_rows.append([
            rank, row["score"], row["group_target"], row["group_decoy"],
            row["cum_target"], row["cum_decoy"], _fmt(row["fdr"], 6),
            _fmt(row["q_value"], 6), ",".join(titles),
        ])
    _tsv(out / "thresholds.tsv", [
        "rank", "score", "group_target", "group_decoy", "cum_target", "cum_decoy",
        "fdr", "q_value", "spectra",
    ], table_rows)

    # --- diagnostics --------------------------------------------------------
    diag_rows = []
    for r in results:
        reasons: list[str] = []
        if not r.identified:
            reasons.append(r.diagnosis)
        for w in sorted(set(r.warnings)):
            reasons.append(w)
        if r.n_shared_excluded:
            reasons.append(f"shared_target_decoy_excluded={r.n_shared_excluded}")
        if r.duplicate_of:
            reasons.append(f"duplicate_of={r.duplicate_of}")
        for illegal in r.illegal_peaks:
            reasons.append("illegal_peak:" + json.dumps(illegal, sort_keys=True))
        if reasons:
            diag_rows.append([r.title, "yes" if r.identified else "no", ";".join(reasons)])
    _tsv(out / "diagnostics.tsv", ["title", "identified", "diagnostics"], diag_rows)

    # --- details JSON -------------------------------------------------------
    details = []
    for r in results:
        details.append({
            "title": r.title,
            "charge": r.charge,
            "observed_precursor_mz": r.observed_mz,
            "identified": r.identified,
            "diagnosis": r.diagnosis,
            "winner_label": r.winner_label,
            "winner_score": r.winner_score,
            "q_value": r.q_value,
            "duplicate_of": r.duplicate_of,
            "candidate_counts": {
                "total": r.n_total_candidates,
                "target": r.n_target_candidates,
                "decoy": r.n_decoy_candidates,
                "shared_excluded": r.n_shared_excluded,
            },
            "ambiguity": [
                {
                    "sequence": h.candidate.sequence,
                    "ox_positions": list(h.candidate.ox_positions),
                    "label": h.label,
                    "sources": [s.__dict__ for s in h.candidate.sources],
                    "score": h.score,
                    "precursor_ppm": h.precursor_ppm,
                    "matched_ions": [m.ion for m in h.matches],
                }
                for h in r.hits if h.score == r.winner_score
            ] if r.winner_score is not None else [],
            "candidates": [
                {
                    "sequence": h.candidate.sequence,
                    "ox_positions": list(h.candidate.ox_positions),
                    "label": h.label,
                    "sources": [s.__dict__ for s in h.candidate.sources],
                    "precursor_ppm": h.precursor_ppm,
                    "score": h.score,
                    "matched_ions": [
                        {
                            "ion": m.ion,
                            "observed_mz": m.observed_mz,
                            "theoretical_mz": m.theoretical_mz,
                            "abs_error_da": m.error,
                            "intensity": m.intensity,
                        }
                        for m in h.matches
                    ],
                    "unused_peak_mz": [
                        p.mz for p in r.unmatched_peaks
                        if h is r.representative
                    ],
                    "matched_intensity": h.matched_intensity,
                    "mean_abs_error_da": h.mean_abs_error,
                    "coincident_ion_groups": h.coincident_ions,
                }
                for h in sorted(r.hits, key=lambda h: (-h.score, h.label, h.candidate.sequence))
            ],
            "warnings": sorted(set(r.warnings)),
            "illegal_peaks": r.illegal_peaks,
            "content_hash": r.content_hash,
        })
    (out / "details.json").write_text(json.dumps(details, indent=2, sort_keys=True) + "\n",
                                      encoding="utf-8")

    # --- config -------------------------------------------------------------
    def sha256(path: str | Path) -> str:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    hashed = input_paths or {"mgf": mgf_path}
    config = {
        "program": "identify.py",
        "python": sys.version.split()[0],
        "plan": {
            "proton_mass": plan.proton_mass,
            "water_mass": plan.water_mass,
            "aa_residue_masses": plan.aa_residue_masses,
            "fixed_modifications": plan.fixed_modifications,
            "variable_modifications": plan.variable_modifications,
            "max_variable_modifications": plan.max_variable_modifications,
            "max_missed_cleavages": plan.max_missed_cleavages,
            "peptide_length": list(plan.peptide_length),
            "precursor_charges": list(plan.precursor_charges),
            "precursor_tolerance_ppm": plan.precursor_tolerance_ppm,
            "fragment_tolerance_da": plan.fragment_tolerance_da,
            "fragment_series": list(plan.fragment_series),
            "fragment_charge": plan.fragment_charge,
            "min_matched_fragments": plan.min_matched_fragments,
            "target_decoy_tie": plan.target_decoy_tie,
            "seed": plan.seed,
        },
        "algorithm": {
            "digestion": "trypsin: after K/R unless next is P; protein ends included; 0/1 missed cleavages",
            "modifications": "fixed C carbamidomethyl; up to one variable M oxidation; unoxidised also searched",
            "candidate_key": "sequence + oxidised residue positions; sources merged across proteins",
            "shared_target_decoy": "identical target/decoy candidates excluded from competition and reported separately",
            "precursor_matching": "abs((observed_mz - theoretical_mz) / theoretical_mz * 1e6) <= precursor_tolerance_ppm",
            "fragment_ions": "b/y singly charged, no isotope correction, no neutral loss",
            "fragment_matching": "all pairs within fragment_tolerance_da; deterministic greedy by absolute error, then observed m/z, then ion index; one peak and one ion used at most once",
            "score": "number of uniquely matched b/y ions (primary); matched intensity and mean absolute error reported as rationale only",
            "competition": "best score per label compared; equal best scores won by decoy",
            "ambiguity": "all candidates tied at the winning score retained with full source lists",
            "fdr": "complete tied-score groups sorted by score; FDR=min(1,(D+1)/max(T,1)); q=min FDR over all thresholds containing the spectrum",
        },
        "input_hashes": {name: sha256(path) for name, path in hashed.items()},
    }
    (out / "config.json").write_text(json.dumps(config, indent=2, sort_keys=True) + "\n",
                                     encoding="utf-8")

    # --- summary ------------------------------------------------------------
    diagnostic_counts: dict[str, int] = {}
    for r in results:
        if not r.identified:
            diagnostic_counts[r.diagnosis] = diagnostic_counts.get(r.diagnosis, 0) + 1
        for w in set(r.warnings):
            key = w.split(":")[0]
            diagnostic_counts[key] = diagnostic_counts.get(key, 0) + 1

    summary = {
        "spectra_total": len(results),
        "spectra_identified": sum(1 for r in results if r.identified),
        "spectra_unidentified": sum(1 for r in results if not r.identified),
        "target_wins": sum(1 for r in results if r.identified and r.winner_label == "target"),
        "decoy_wins": sum(1 for r in results if r.identified and r.winner_label == "decoy"),
        "ambiguous_identified": sum(1 for r in results if r.identified and len(r.ambiguity_keys) > 1),
        "duplicate_spectra": sorted(
            [{"title": r.title, "duplicate_of": r.duplicate_of}
             for r in results if r.duplicate_of]
        ),
        "candidates_total": len(candidates),
        "shared_target_decoy_candidates": sorted(
            [
                {
                    "sequence": c.sequence,
                    "ox_positions": list(c.ox_positions),
                    "sources": [s.__dict__ for s in c.sources],
                }
                for c in candidates.values() if c.shared_target_decoy
            ],
            key=lambda d: (d["sequence"], d["ox_positions"]),
        ),
        "diagnostic_counts": dict(sorted(diagnostic_counts.items())),
        "fdr_thresholds": fdr_rows,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n",
                                      encoding="utf-8")

    _write_report(out / "report.md", results, summary, plan)
    return summary


def _write_report(path: Path, results: Sequence[SpectrumResult], summary: dict, plan: Plan) -> None:
    lines: list[str] = []
    lines.append("# Identification report (small synthetic database)\n")
    lines.append("This report was generated by `identify.py`; all numbers below come from "
                 "the supplied local fixtures.\n")
    lines.append("## Method\n")
    lines.append("- Trypsin: cut after K/R unless the next residue is P; protein ends are "
                 "boundaries; 0 or 1 missed cleavages; peptide length "
                 f"{plan.peptide_length[0]}–{plan.peptide_length[1]}.")
    lines.append("- Fixed C carbamidomethylation (+57.021464 Da); at most one variable "
                 "M oxidation (+15.994915 Da); unoxidised forms also searched.")
    lines.append("- Candidates are keyed by sequence + modified positions. The same "
                 "sequence from different proteins is one candidate with all sources retained.")
    lines.append("- Identical target/decoy candidates are excluded from competition and "
                 "reported separately (none in this fixture).")
    lines.append(f"- Precursor filter: |Δm/z| / theoretical m/z ≤ {plan.precursor_tolerance_ppm} ppm.")
    lines.append(f"- Fragments: b/y 1+, tolerance ±{plan.fragment_tolerance_da} Da, "
                 "one-to-one greedy assignment by absolute error. A peak or ion is used at most once.")
    lines.append("- Score: number of uniquely matched b/y ions. Matched intensity and mean "
                 "absolute error are reported as rationale, not used for ranking.")
    lines.append("- Target and decoy compete on the same score; equal best scores go to decoy.")
    lines.append("- FDR uses complete tied-score groups: min(1,(D+1)/max(T,1)); q is the "
                 "minimum FDR over all thresholds containing the spectrum.\n")

    lines.append("## Per-spectrum results\n")
    lines.append("| spectrum | z | winner | score | identified | diagnosis | best tied identities | q |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in results:
        ident = "<br>".join(
            f"{seq}[ox={ox or '-'}]" for seq, ox in r.ambiguity_keys
        ) if r.ambiguity_keys else ""
        lines.append(
            f"| {r.title} | {r.charge if r.charge is not None else '?'} | "
            f"{r.winner_label or ''} | {r.winner_score if r.winner_score is not None else ''} | "
            f"{'yes' if r.identified else 'no'} | {r.diagnosis} | {ident} | "
            f"{'' if r.q_value is None else f'{r.q_value:.6f}'} |"
        )

    lines.append("\n## Threshold table (complete tied-score groups)\n")
    lines.append("| rank | score | group T | group D | cum T | cum D | FDR | q | spectra |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for rank, row in enumerate(summary["fdr_thresholds"], 1):
        titles = sorted(r.title for r in results
                        if r.qualified and r.winner_score == row["score"])
        lines.append(
            f"| {rank} | {row['score']} | {row['group_target']} | {row['group_decoy']} | "
            f"{row['cum_target']} | {row['cum_decoy']} | {row['fdr']:.6f} | "
            f"{row['q_value']:.6f} | {','.join(titles)} |"
        )

    lines.append("\n## Ambiguity and shared sources\n")
    for r in results:
        if len(r.ambiguity_keys) > 1:
            details = []
            for hit in r.hits:
                if hit.score != r.winner_score:
                    continue
                details.append(
                    f"`{hit.candidate.modified_sequence()}` ({hit.label}; "
                    f"{_source_text(hit.candidate)})"
                )
            lines.append(f"- {r.title}: tied at score {r.winner_score}: " + "; ".join(details))
    lines.append("\nAll candidates with their protein sources are in `candidates.tsv`; "
                 "per-spectrum candidate hits are in `candidate_hits.tsv`.\n")

    lines.append("## Diagnostics\n")
    for r in results:
        if not r.identified:
            lines.append(f"- {r.title}: {r.diagnosis} "
                         f"(charge={r.charge}, precursor={r.observed_mz}).")
    for dup in summary["duplicate_spectra"]:
        lines.append(f"- {dup['title']}: duplicate content of {dup['duplicate_of']}; "
                     "kept as a separate spectrum and counted separately in FDR.")
    if not summary["shared_target_decoy_candidates"]:
        lines.append("- No identical target/decoy candidate exists in this fixture; the "
                     "exclusion/reporting path is covered by unit tests.")
    lines.append("")

    lines.append("## Limitations\n")
    lines.append("- This is a tiny synthetic database. Reported FDR and q values demonstrate "
                 "the statistical bookkeeping; they do **not** estimate or guarantee the false "
                 "discovery rate of a real experiment.")
    lines.append("- A q value is a target-decoy competition estimate for the *set* of "
                 "identifications at that threshold. It is not the posterior probability that "
                 "any particular peptide is correct.")
    lines.append("- No MS1 quantification, protein-level inference, open modification search, "
                 "isotope correction, or neutral-loss modelling is performed.")
    lines.append("- Scoring is a simple unique-ion count; intensity is reported but does not "
                 "drive the score. This is transparent but less powerful than a probabilistic "
                 "or intensity-weighted model.")
    lines.append("- I/L (and other isobaric residue pairs) cannot be distinguished by MS2; "
                 "ambiguity is retained in `ambiguity.tsv`.")
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def run(plan_path: str | Path, target_path: str | Path, decoy_path: str | Path,
        mgf_path: str | Path, outdir: str | Path) -> dict:
    plan = Plan.load(plan_path)
    proteins = read_fasta(target_path, "target") + read_fasta(decoy_path, "decoy")
    candidates = build_candidates(proteins, plan)
    spectra = read_mgf(mgf_path)
    results = [identify_spectrum(spec, candidates, plan) for spec in spectra]
    return write_outputs(results, candidates, plan, mgf_path, outdir, input_paths={
        "plan": plan_path,
        "target_fasta": target_path,
        "decoy_fasta": decoy_path,
        "mgf": mgf_path,
    })


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--decoy", required=True)
    parser.add_argument("--mgf", required=True)
    parser.add_argument("--outdir", default="results")
    args = parser.parse_args(argv)
    summary = run(args.plan, args.target, args.decoy, args.mgf, args.outdir)
    print(json.dumps({
        "spectra_total": summary["spectra_total"],
        "spectra_identified": summary["spectra_identified"],
        "target_wins": summary["target_wins"],
        "decoy_wins": summary["decoy_wins"],
        "ambiguous_identified": summary["ambiguous_identified"],
        "outdir": str(args.outdir),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
