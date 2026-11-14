# Rechunk / transpose tool

`rechunk_tool` reads one local 3-D Zarr v2 array, applies an axis permutation
and new chunking **without materialising the whole array**, and publishes a
normal Zarr v2 directory that any independent Zarr consumer can read.

It targets only the supplied scope: a single local array, C-order little-endian
`<i4`, one zlib compressor, no filters, no remote store, no concurrency. All
other metadata is rejected with a clear error.

## Layout

```
rechunk_tool/
  zmetadata.py   # .zarray parsing and strict validation
  plan.py        # plan validation, overlap planning, budget estimate
  codec.py       # zlib decode/encode with truncation detection
  accounting.py  # explicit live-buffer ledger (no RSS)
  executor.py    # two-pass scatter/assemble executor
  verifier.py    # independent full-read verifier + corruption probe
  cli.py         # plan / rechunk / verify / run commands
scripts/run_fixture.py   # end-to-end record for the supplied fixtures
tests/                   # plan arithmetic, execution, verification, corruption
```

## Environment

Python 3.11 with `numpy==1.26.4`, `zarr==2.18.3`, `numcodecs==0.12.1`
(see `requirements.txt`). Only the **verifier** and the **test corpus builder**
import `zarr`; the executor itself uses `numpy` and the standard-library `zlib`
directly.

A local virtualenv was used for development:

```sh
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt   # or the equivalent pins
```

> Environment note: on the machine used to develop this solution,
> `numcodecs==0.12.1` had no prebuilt wheel for `aarch64`, so it was compiled
> from the pinned sdist (with Python headers supplied from the distro's
> `libpython3.11-dev` package). If you hit the same issue, either use a host
> with a matching wheel or build `numcodecs==0.12.1` from source. The executor
> does not depend on that build detail.

## Coordinate convention

`plan.json` specifies `axis_permutation`; the tool interprets it as

```
target = source.transpose(axis_permutation)
```

so `target.shape[k] == source.shape[axis_permutation[k]]`. All intersection
arithmetic is performed in **target axis order**; a source-grid coordinate `g`
covers target intervals `[g[p[k]]*C[p[k]], (g[p[k]]+1)*C[p[k]])` clipped to the
target domain. The inverse permutation is carried explicitly so that axis
permutation is never confused with a mere reshape.

## Commands

All commands print JSON (or write it with `--report`).

### 1. Plan (no data read)

```sh
.venv/bin/python -m rechunk_tool plan \
  --source fixtures/source.zarr \
  --plan fixtures/plan.json \
  --report .runtime/plan.json
```

The report contains, for every source/target chunk pair intersection in both
**local** and **global** coordinates, the sparse-chunk split, expected decode /
encode counts, and `predicted_data_buffer.predicted_peak_bytes` with a
`within_budget` flag.

### 2. Execute (budgeted)

```sh
.venv/bin/python -m rechunk_tool rechunk \
  --source fixtures/source.zarr \
  --plan fixtures/plan.json \
  --target .runtime/target.zarr \
  --work .runtime/work \
  --report .runtime/rechunk.json
```

`--work` is a temporary spool/staging root on the **same filesystem** as the
target (it is removed on completion; pass `--keep-work` to retain it). If
omitted, a temporary directory next to the target is used.

The target is written to a staging directory, every chunk and both metadata
files are re-read and checked, and only then is the staging directory renamed
to `--target`. On any failure the staging directory is deleted, so a corrupt
input can never leave a complete-looking target. The tool refuses to overwrite
an existing target.

### 3. Verify (independent, separate phase)

```sh
.venv/bin/python -m rechunk_tool verify \
  --source fixtures/source.zarr \
  --target .runtime/target.zarr \
  --plan fixtures/plan.json \
  --report .runtime/verify.json \
  --corruption-probe \
  --corrupt fixtures/corrupt-chunk.bin --replace-key 0.0.1
```

The verifier opens both arrays with the independent `zarr` library, reads them
whole (allowed for this small array, and *not* charged to the executor
budget), and compares `source.transpose(perm)` to the target element-wise,
including `dtype` and fill semantics. It also checks junction coordinates by
on-demand slicing (chunk boundaries, missing-chunk extents, a real zero, a
real negative, a fill cell) and re-checks the permuted attributes.

`--corruption-probe` copies the source to a disposable directory, replaces
`--replace-key` with the truncated payload, runs a conversion, and asserts that
it fails **and** that no published output exists.

### 4. Run both (for a single record)

```sh
.venv/bin/python -m rechunk_tool run \
  --source fixtures/source.zarr --plan fixtures/plan.json \
  --target .runtime/target.zarr --work .runtime/work \
  --report .runtime/run.json \
  --corruption-probe --corrupt fixtures/corrupt-chunk.bin --replace-key 0.0.1
```

`run` records execution and verification under separate top-level keys
(`rechunk` and `verify`), keeping the budgeted phase distinct from the
unbudgeted verifier read.

### Full fixture record

```sh
.venv/bin/python scripts/run_fixture.py --out .runtime/demo
```

Writes `plan.json`, `rechunk.json`, `verify.json` and `summary.json`.

## Execution design

1. **Scatter pass** (source grid order). Each *present* source chunk is read
   and zlib-decoded exactly once into a read-only `<i4` array. A transposed
   view (zero extra bytes) is sliced for every intersecting target chunk and
   the contiguous intersection is appended to a per-target fragment file in the
   spool. A missing chunk performs **no decode** and writes no fragment.
2. **Assemble pass** (target grid order). A full declared-shape target buffer
   is initialised to `fill_value`, all spooled fragments for that target chunk
   are copied in, then the chunk is zlib-encoded and written exactly once.

Missing source chunks and out-of-domain target padding both resolve to the
fill value by construction: the assembly buffer starts filled, and missing
sources contribute nothing. No sparse gap is ever treated as zero.

## Buffer accounting

`data_buffer_budget_bytes` is enforced by an explicit ledger
(`accounting.py`), not by RSS:

* every **unique** live uncompressed numeric buffer is charged once;
* numpy views (transposed chunks, byte-views of decoded chunks, fragment
  arrays over spool bytes) are charged **zero** and registered against their
  owner;
* acquiring a live key twice is an assertion error, so a buffer cannot be
  double-counted;
* compressed input/output byte strings and interpreter overhead are tracked in
  separate counters and excluded from the budget;
* the peak is the maximum live sum, recorded with the stage and the exact live
  blocks.

The executor never holds a decoded source chunk together with a target
assembly, and it never materialises the full array. With the supplied fixture
the measured peak is **10592 bytes** (one 7072-byte source chunk plus one
3520-byte intersection copy) against the 12288-byte budget; the predicted and
measured values match.

## Expected results on the supplied fixture

| item | value |
| --- | --- |
| source | 29×37×41, chunks 8×13×17, fill −999 |
| target | 41×29×37, chunks 11×9×10 |
| decode calls | 32 (one per present chunk; 4 missing chunks skipped) |
| encode/write calls | 64 (one per target chunk, including all-fill edges) |
| intersections | 252, max piece 3520 bytes |
| peak data buffer | 10592 bytes ≤ 12288 budget |
| compressed in / out | 60636 / 80839 bytes |
| fragment spool | 160576 bytes across 64 files |
| verification | 0 mismatches; true zero and true negative −12345 preserved |

## Tests

```sh
.venv/bin/python -m pytest tests/ -q
```

The suite covers plan arithmetic on the fixture, non-divisible shapes in all
axes with permutation, identity-vs-permutation discrimination, metadata and
plan rejection, budget enforcement, truncation and wrong-size corruption,
missing-chunk fill semantics, all-fill and edge-padded target chunks,
attribute permutation, spool truncation, and staged-metadata tampering before
publication.
