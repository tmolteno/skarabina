# Agent Instructions

## Package structure

The repo contains two Python packages:

| Package | Path | Purpose |
|---|---|---|
| `skarabina` | `./` (root) | CLI tool (`skarabina`, `skarabina-analyze`) |
| `skarabina-cargo` | `./cargo/` | Stimela cab definitions |

`skarabina-cargo` depends on `stimela` and packages the cab schema
(`skarabina.yml`), base vars (`genesis/skarabina-cargo-base.yml`),
and container config (`stimela.conf`).
`skarabina` has no dependency on `stimela` — all CLI parameters are
defined as explicit `@click.option` decorators in `main.py`.

## Version bump checklist

When bumping the version for a release, update these files to match:

| File | Field |
|---|---|
| `pyproject.toml` | `project.version` |
| `cargo/pyproject.toml` | `project.version` |
| `cargo/genesis/skarabina-cargo-base.yml` | `vars.skarabina-cargo.images.version` |

All three must reference the same version number (e.g. `0.6.2` in both
`pyproject.toml` files and `0.6.2` in the YAML).  Do **not** include a
`v` prefix in the YAML version — CI's `docker/metadata-action` uses
`type=semver` which strips the `v` from the git tag, so the published
Docker image tag is `0.6.2`, not `v0.6.2`.  The YAML value must match
the image tag exactly.

Also add a changelog entry to `doc/CHANGES.md`.  Do not create or use a
`CHANGES.md` at the top level — the canonical changelog lives under `doc/`.

## Docker

- The single `Dockerfile` builds for `linux/amd64` and `linux/arm64`.
- CI (`.github/workflows/docker-publish.yml`) builds and pushes on
  `v*.*.*` tags only.

## Benchmarks (flagging timing)

`bench/` is the local test-bench for skarabina's flagging performance.  It is
tracked; the measurement set it runs on is not.

| File | Purpose |
|---|---|
| `bench/flag_timing.py` | the driver: times skarabina flag runs, writes JSON + Markdown |
| `bench/meerkat-flags.yml` | the flag list it runs, with the `../meerkat_imaging` provenance of every entry |
| `bench/spectral-flags-L.yml` | copy of `../meerkat_imaging/spectral-flags-L.yml`, used by the `spectral-window` entry |
| `bench/run_stage.py` | child process: fixes the casacure/casacore import order, then runs the CLI |

The workload is the stage-0 flag sequence of `../meerkat_imaging`
(`white-belt-0-flagging.yml`, step `flag-average`) — `save:imported`, `autos`,
`uv-above 8000`, `nan`, `clip 0 100`, `spectral-window` — plus the `rflag` verb
that replaces the recipe's CASA autoflag loop over the calibrator fields.  It
runs against `.bench/data/bpcal.ms`, the local (gitignored, ~1.6 GB) copy of a
real MeerKAT bandpass-calibrator MS:

```
python bench/flag_timing.py                     # seq, casacure, bpcal.ms
python bench/flag_timing.py --mode both         # + one process per operation
python bench/flag_timing.py --ms .bench/ms_cure_2k.ms --drop-save-imported
python bench/flag_timing.py --backend casacore --rflag-args "winsize=5"
```

- `--mode seq` (default) runs the whole list in one process — the number the
  pipeline sees.  `--mode per-op` runs each operation in its own process from
  the same input flags, so the cost is attributable; `save:imported` there is
  the I/O floor of the table.
- Each run writes only the flag columns (`--write-changed-only` hard-links the
  unchanged blocks) and deletes its output MS afterwards.  A list containing
  `save:imported` rotates the input's saved flag version and leaves the old one
  in `<ms>.flagversions` (~18 MB for bpcal.ms); `--drop-save-imported` skips it.
- `--write-changed-only` had a sharp edge, fixed after 1.0.5 (issue #3,
  `_ensure_writable` in `dask_ms.py`): hard-linking a block and chmod'ing it
  read-only also made the *input's* `table.fN` read-only (same inode), and the
  next run copied such a block with `shutil.copy2` — which preserves the mode —
  so it died with `storage error: Permission denied` on the first column it
  wrote.  The bench still restores owner-write on the input's blocks before
  every run (`--repair-input-perms`, the default), which is what an MS left
  read-only by 1.0.5 or earlier needs; `--no-repair-input-perms` leaves the
  modes alone, and is how the failure was — and can still be — reproduced.
- Results land in `bench/results/` (gitignored) as JSON + Markdown.  The
  numbers worth quoting by hand go into `BENCHMARKS.md` at the repo root, with
  the host, date, backend and workload they were measured on.
- Re-run the bench and refresh `BENCHMARKS.md` whenever a change claims to make
  flagging faster, and keep `bench/meerkat-flags.yml` in step with the
  `flag-average` step of `../meerkat_imaging/white-belt-0-flagging.yml`.
