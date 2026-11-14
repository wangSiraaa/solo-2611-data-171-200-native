# Small MS2 peptide-spectrum identification

Python 3.11, standard library only. Matches the local synthetic MGF spectra to
the supplied target/decoy FASTA and produces target-decoy FDR/q evidence.

## Usage

```sh
python3 identify.py \
  --plan fixtures/plan.json \
  --target fixtures/target.fasta \
  --decoy fixtures/decoy.fasta \
  --mgf fixtures/spectra.mgf \
  --outdir results

python3 verify.py          # independent verification of the fixtures
```

No network access, database download, MS1 quantification, open modification
search, or protein-level inference is performed.

## Method (per `README.md` / `plan.json`)

- **Digestion**: trypsin, cut after K/R unless the next residue is P; protein
  ends are boundaries; 0 or 1 missed cleavages; peptide length 4–20.
- **Modifications**: fixed C carbamidomethylation (+57.021464); at most one
  variable M oxidation (+15.994915); unoxidised forms are also candidates.
- **Candidates**: keyed by sequence + oxidised positions. The same sequence
  from several proteins is a single candidate retaining every source.
  Identical target and decoy candidates are excluded from competition and
  reported separately, so one chemical entity never competes with itself.
- **Precursor filter**: `|observed − theoretical| / theoretical ≤ 20 ppm`.
- **Fragments**: b/y, charge 1+, ±0.02 Da, no isotope correction or neutral
  loss. All in-tolerance peak/ion pairs are assigned deterministically
  one-to-one by absolute error (ties by observed m/z, then ion index). A peak
  and an ion are used at most once, so one strong peak cannot score twice.
  Coincident theoretical ions are reported explicitly.
- **Score**: number of uniquely matched b/y ions. Matched intensity and mean
  absolute error are reported as rationale only. Candidates below
  `min_matched_fragments` are unidentified.
- **Competition**: target and decoy use the same pipeline; equal best scores
  go to decoy. All identities tied at the winning score are retained.
- **FDR**: qualified winners are grouped by complete tied-score groups, sorted
  by score descending. `FDR = min(1, (D+1)/max(T,1))` at each group boundary;
  a spectrum's q value is the smallest FDR over every threshold containing it.
  Groups are never split and decoys are never removed first.

## Outputs (in `--outdir`)

| file | contents |
|---|---|
| `candidates.tsv` | every candidate with all protein sources, neutral mass, oxidation state, shared target/decoy flag |
| `spectra.tsv` | per spectrum: candidates in ppm window, winner, score, q, matched/unused peaks, score basis, diagnostics |
| `ambiguity.tsv` | all identities tied at each spectrum's winning score, with sources |
| `candidate_hits.tsv` | every scored candidate for every spectrum |
| `thresholds.tsv` | complete tied-score groups with cumulative T/D, FDR, monotone q |
| `diagnostics.tsv` | unidentified spectra, unknown charge, illegal peaks, duplicates |
| `details.json` | full per-match audit (observed m/z, theoretical m/z, error, intensity, coincident ions) |
| `config.json` | plan, algorithm description and SHA-256 of all inputs |
| `report.md` | human-readable summary |

## Fixture expectations

9 spectra, 8 identified (6 target wins, 2 decoy wins), 1 unidentified. Notable
cases: `scan003` is the +15.994915 M-oxidised ACDMR at charge 3; `scan002`
ACDMR comes from two target proteins and keeps both sources; `scan007` is the
designed I/L tie AGILK (target) vs AGLLK (decoy), won by decoy and reported as
ambiguous; `scan008` duplicates `scan001` and is counted separately in FDR;
`scan009` has no precursor candidate.

## Limitations

- The database is tiny and synthetic. The FDR/q values demonstrate the
  bookkeeping; they do **not** estimate or guarantee a real experiment's FDR.
- A q value is a target-decoy competition estimate for the identification set
  at a threshold, not the posterior probability that a peptide is true.
- Isotope patterns, neutral losses and intensity-weighted scoring are not
  modelled; isobaric residues (I/L) remain indistinguishable.
