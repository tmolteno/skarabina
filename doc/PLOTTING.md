<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# Plotting: skarabina-plotms

`skarabina-plotms` plots a measurement set or a caltable with matplotlib —
a drop-in replacement for CASA's `plotms`.  The `casaplotms` task ships as
an **x86_64-only AppImage**, so a pipeline on arm64 cannot run its `plotms`
steps at all; this is what lets those steps run everywhere, in the same
multi-arch `skarabina` image the other cabs already use.

```sh
# The stage-1 gain-table plot of the meerkat_imaging pipeline, verbatim:
skarabina-plotms --ms out/multi.G0 --plotfile out/cal_G0.pdf --overwrite

# An MS diagnostic, the plotms-style axes:
skarabina-plotms --ms obs.ms --plotfile amp_vs_uvdist.png \
    --xaxis uvdist --yaxis amp --overwrite

# Phase vs channel of a bandpass table, both correlations:
skarabina-plotms --ms out/multi.B0 --plotfile b0_phase.png \
    --xaxis chan --yaxis phase --overwrite
```

## Headless

Nothing needs a display: no X server, no `xvfb-run`, no `DISPLAY`.  (The
`casa.plotms` cab ran casaplotms under `xvfb-run -a python` for its GUI
toolkit; this one has no equivalent requirement.)

- The **Agg** backend is forced at import, *before* pyplot is imported
  anywhere, so an interactive `MPLBACKEND` — or a host with tkinter/Qt
  installed, where matplotlib's own default would be `TkAgg` — cannot pick
  a backend that wants to connect to a display.  A process that imported
  pyplot first is switched over too (`matplotlib.use` runs before the
  first figure; pyplot defers loading its backend until then).
- Matplotlib's cache directory (`MPLCONFIGDIR`, the font cache) is pointed
  at a per-user writable temp path *before* matplotlib is imported when
  the default under `$HOME` is absent or read-only — the normal state in a
  container — so runs neither warn nor rebuild fonts from a random path.
- Only `savefig` is used, never `show()`.

All of it is pinned by tests (`tests/test_plotms.py`): the CLI runs in a
subprocess with `DISPLAY`/`WAYLAND_DISPLAY` stripped, `MPLBACKEND=TkAgg`,
`XDG_CONFIG_HOME`/`MPLCONFIGDIR` unset and a read-only `HOME`, and must
still write the plot with backend `agg`.

## plotms parity

The defaults are `plotms`' own defaults, so a step that only passes
`ms`/`plotfile`/`overwrite` — as every `casa.plotms` step in the
meerkat_imaging pipeline does — produces the same plot:

| plotms | skarabina-plotms |
|---|---|
| x axis default `time` | `--xaxis` blank → time |
| y axis default `amp` | `--yaxis` blank → amplitude |
| flagged data not shown | same; `--show-flagged` shows it |
| `overwrite=false` errors on an existing file | same (`--overwrite` to replace) |
| one panel, all correlations overplotted | same, one series per correlation with a legend |
| output format from the `plotfile` extension | same: `.png`, `.pdf`, `.svg`, `.ps`, `.eps` |

Time is drawn on a real date axis (TIME is MJD seconds), correlations are
named from `POLARIZATION.CORR_TYPE` (XX/YY, RR/LL, …), and antenna/baseline
axes are ticked with antenna names when there are few enough to read.

## Axes

`--xaxis` / `--yaxis` accept plotms' spellings (`chan`, `freq`, `uvdist_l`,
`ant1`, …).  Blank means the plotms default.

| kind | x axes | y axes |
|---|---|---|
| measurement set | time, interval, scan, field, spw, row, channel, frequency, uvdist, uvwave, u, v, w, antenna, antenna1, antenna2, baseline | amp, phase, real, imag, wt |
| caltable | time, interval, field, spw, row, channel, frequency, antenna, antenna1, antenna2, baseline | amp, phase, real, imag, wt, snr |

A caltable has no UVW or scans, an MS has no `SNR` column; asking for one is
an error that lists the choices, never a silently wrong plot.

## Selection and size

| option | meaning |
|---|---|
| `--corr` | correlations by name or 0-based index, comma-separated (default all) |
| `--field` | fields by name or id (default all) |
| `--spw` | spectral windows by id (default all) |
| `--scan` | scans by number, measurement sets only (default all) |
| `--data-column` | MS column: `DATA`, `CORRECTED` or `MODEL` (default `DATA`); caltables always use `CPARAM` |
| `--max-points` | decimate to about this many points per correlation (default 200000, `0` disables) |
| `--title` | title; blank is `<table>: <y> vs <x>` |

Decimation strides the *plot*, not the memory: the column is read in ~64 MB
chunks and sliced, so a multi-GB MS is never materialised.  For a
non-spectral x axis the point budget is spent on time resolution with up to
64 channels sampled across the band; for `channel`/`frequency` every channel
is kept and rows are strided.

## As a stimela cab

`skarabina-cargo` ships the **`skarabina-plotms`** cab, running in the
published multi-arch `skarabina` image (nothing new to build).  Its inputs
are the CLI options under the same names, so the params a `casa.plotms` step
already passes carry over unchanged:

```yaml
    plot-gains:
        cab: skarabina-plotms       # was: casa.plotms
        params:
            ms: "{root.dir-out}/multi.{recipe.caltable}"
            plotfile: "{recipe.dir-out}/cal_{recipe.caltable}.pdf"
            overwrite: true
```

Stimela passes a bool cab param as a bare `--flag` when true and omits it
when false, which is exactly what `--overwrite`/`--show-flagged` are.

## Substituting in the meerkat_imaging pipeline

The pipeline has three `cab: casa.plotms` steps, all in
`white-belt-1-1GC.yml`, all plotting a caltable with default axes:

| step | table | output |
|---|---|---|
| `cal-loop-plot-gains` → `plot-gains` | `multi.{G0,G1,G2,G3,K0,K1,K2,K3,B0,B1,F2,F3}` (12 plots) | `cal_<table>.pdf` |
| `primary-plot-K0` | `multi.K0` | `cal_K0.pdf` |
| `primary-plot-G0` | `multi.G0` | `cal_G0.pdf` |

To substitute, change `cab:` on those three steps and nothing else; drop the
`- plotms.yml` include from `_include` when no step uses `casa.plotms`
anymore (`white-belt-1-1GC.yml` includes it; `white-belt-2-target-flag.yml`
and `white-belt-3-2GC.yml` include it but never use it).

The arm64 wrappers then stop skipping the plots.  In
`yellow-belt-mergA_tim-arm64.yml` and
`yellow-belt-mergA_tim-fast-arm64.yml`, remove these from the skip list:

```yaml
calibration-1gc:
    steps:
        primary-plot-K0:            # delete: now runs on arm64
            skip: true
        primary-plot-G0:            # delete
            skip: true
        plot-gains:                 # delete
            skip: true
        plot-results-1:             # keep: shadems is still amd64-only
            skip: true
```

`plot-results-1` is a `shadems-tim` step (complex-visibility scatter plots),
not plotms; shadems remains amd64-only, so that one stays skipped on arm64.

### Not carried over

`casa.plotms` also offers `averagedata`/`avg*`, `iteraxis`, `expformat`,
`title`/`xlabel`/`ylabel`, `showgui`, ranges and much more.  The pipeline
uses none of them; the substitution covers `ms`, `plotfile`, `overwrite`,
`xaxis`, `yaxis`, `corr`, `field`, `spw`, `scan`, `title` and the data
column.  A recipe that averages or iterates would need those parts
re-expressed — nothing in this pipeline does.
