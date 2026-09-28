#!/usr/bin/env python3
"""Measure the peak RSS of the run shapes skarabina.memory predicts.

Handover §3.1: the per-chunk constants behind the plan were fitted when
casacure still buffered a whole written table, and the write's cost was
charged per *input* visibility even when averaging shrinks the output.  This
harness runs the canonical stage-0 list (or a single verb) over a real MS at
several fixed row chunks, records each run's true peak RSS from ``wait4``'s
rusage, and prints it beside what the plan predicted for the same run, so the
constants can be refitted and the fit checked.

    python bench/mem_recal.py                        # .bench/scan1.ms, 12 workers
    python bench/mem_recal.py --chunks 5000 12000 40000
    python bench/mem_recal.py --cases save-12000 nowrite-12000

Each case is a fresh skarabina process.  Runs that write share the input's
unchanged blocks (``--write-changed-only``), so the input's storage blocks are
left read-only and are repaired before the next run, as ``flag_timing.py``
does.  Output MSs are deleted afterwards; ``save:`` flag versions accumulate
beside the input, as they do in the pipeline.

Printed per case: the measured peak, the plan's largest per-step estimate and
the step that owns it, their ratio, and the *implied* bytes per input
visibility in flight -- ``(peak - BASE) / (chunks in flight x rows per chunk x
nchan x ncorr)`` -- which is the number the constants stand for.  Chunks in
flight is ``min(workers, ceil(rows / row_chunk))``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_MS = REPO / ".bench" / "scan1.ms"
DEFAULT_SKARABINA = REPO / ".venv" / "bin" / "skarabina"
SPECTRAL = HERE / "spectral-flags-L.yml"
SCRATCH = REPO / ".bench" / "scratch" / "mem_recal"
BASE_BYTES = 0.5 * 2**30

#: The stage-0 list of ../meerkat_imaging (bench/meerkat-flags.yml), spelled as
#: the canonical AGENTS.md command spells it.
STAGE0 = ["save:imported", "autos", "uv-above 2500", "nan", "clip 0 100",
          f"spectral-window {SPECTRAL}"]

#: Step names a plan line can start with (everything else indented in the
#: child's output -- the summary's counters -- is not a plan line).
STEPS = {"read", "autos", "uv-above", "nan", "clip", "spectral-window",
         "restore", "tfcrop", "rflag", "save", "write", "write-flags"}


def cases(chunks, ops, averaging):
    """The run matrix: (name, flag list, msout mode, averaging factor)."""
    out = []
    for chunk in chunks:
        out.append((f"nowrite-{chunk}", ops, "none", 1))
        out.append((f"flags-{chunk}", ops, "flags", 1))
        out.append((f"write-{chunk}", ops, "full", 1))
        if averaging > 1:
            out.append((f"avg-{chunk}", ops, "full", averaging))
        out.append((f"save-{chunk}", ["save:imported"], "none", 1))
    return out


def repair_input_perms(ms: Path) -> int:
    """Restore owner-write on blocks a previous shared write left read-only."""
    repaired = 0
    for entry in ms.iterdir():
        if not entry.name.startswith("table.f"):
            continue
        try:
            mode = entry.stat().st_mode
            if not mode & 0o200:
                entry.chmod(mode | 0o200)
                repaired += 1
        except OSError:
            pass
    return repaired


def plan_of(output: str):
    """The plan header and its per-step estimates, as ``(step, bytes)``."""
    header, steps = None, []
    for line in output.splitlines():
        if line.startswith("Memory plan:"):
            header = line
        elif header is not None and line.startswith("  "):
            parts = line.split()
            if parts and parts[0] in STEPS:
                try:
                    steps.append((parts[0], float(parts[1]) * 2**30))
                except (ValueError, IndexError):
                    pass
    return header, steps


def ms_shape(skarabina: Path, ms: Path) -> tuple:
    """``(rows, nchan, ncorr)`` of the input, read by skarabina's own backend."""
    code = (
        "import daskms\n"
        "from casacore.tables import table\n"
        f"t = table({str(ms)!r}, ack=False)\n"
        "d = t.getcell('DATA', 0) if t.nrows() else [[0]]\n"
        "print(t.nrows(), d.shape[0], d.shape[1])\n"
    )
    env = dict(os.environ, DASK_MS_BACKEND="casacure")
    python = skarabina.parent / "python"
    if not python.exists():
        python = REPO / ".venv" / "bin" / "python"
    out = subprocess.run([str(python), "-c", code],
                         capture_output=True, text=True, env=env).stdout.split()
    return tuple(int(v) for v in out[:3]) if len(out) >= 3 else (0, 0, 0)


def run_case(name, ops, mode, factor, ms, skarabina, workers, chunk, timeout):
    """Run one case; return its measurement and what the plan predicted."""
    msout = SCRATCH / f"out_{name}.ms"
    for stale in (msout, Path(str(msout) + ".flagversions")):
        shutil.rmtree(stale, ignore_errors=True)
    repaired = repair_input_perms(ms)

    cmd = [str(skarabina), "--ms", str(ms), "--clobber",
           "--row-chunk", str(chunk), "--workers", str(workers)]
    if mode == "flags":
        cmd += ["--msout", str(msout), "--write-changed-only"]
    elif mode == "full":
        cmd += ["--msout", str(msout)]
    else:
        cmd += ["--summary"]
    if factor > 1:
        cmd += ["--frequency-average-factor", str(factor),
                "--time-average-factor", "1"]
    for op in ops:
        cmd += ["--flag", op]

    env = dict(os.environ, DASK_MS_BACKEND="casacure", PYTHONUNBUFFERED="1")
    load_before = os.getloadavg()[0]
    log_path = SCRATCH / f"{name}.log"
    with open(log_path, "w") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env)
        started = time.perf_counter()
        deadline = started + timeout if timeout else None
        while True:
            pid, status, rusage = os.wait4(proc.pid, os.WNOHANG)
            if pid != 0:
                break
            if deadline and time.perf_counter() > deadline:
                proc.kill()
                _pid, status, rusage = os.wait4(proc.pid, 0)
                break
            time.sleep(0.2)
        wall = time.perf_counter() - started

    output = log_path.read_text(errors="replace")
    header, steps = plan_of(output)
    planned = max((size for _step, size in steps), default=0.0)
    owner = max(steps, key=lambda kv: kv[1])[0] if steps else "-"
    shutil.rmtree(msout, ignore_errors=True)
    shutil.rmtree(Path(str(msout) + ".flagversions"), ignore_errors=True)
    return {
        "case": name,
        "ops": ops,
        "mode": mode,
        "factor": factor,
        "row_chunk": chunk,
        "workers": workers,
        "rc": os.waitstatus_to_exitcode(status),
        "wall_s": round(wall, 2),
        "peak_mib": round((rusage.ru_maxrss if rusage else 0) / 1024, 1),
        "planned_mib": round(planned / 2**20, 1),
        "planned_step": owner,
        "plan_header": header,
        "plan_steps": [[s, round(b / 2**20, 1)] for s, b in steps],
        "repaired_blocks": repaired,
        "load_before": round(load_before, 2),
        "load_after": round(os.getloadavg()[0], 2),
        "log": str(log_path.relative_to(REPO)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ms", type=Path, default=DEFAULT_MS)
    ap.add_argument("--skarabina", type=Path, default=DEFAULT_SKARABINA)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--chunks", type=int, nargs="+", default=[5000, 12000, 40000])
    ap.add_argument("--frequency-average-factor", type=int, default=32)
    ap.add_argument("--cases", nargs="*", default=None,
                    help="run only these case names (default: all)")
    ap.add_argument("--repeat", type=int, default=1,
                    help="run every case this many times (peak RSS varies by"
                         " ~20 %% run to run; use 3 and read the median)")
    ap.add_argument("--timeout", type=float, default=3600)
    ap.add_argument("--tag", default=None)
    opts = ap.parse_args()

    ms = opts.ms.resolve()
    if not ms.exists():
        raise SystemExit(f"no such measurement set: {ms}")
    SCRATCH.mkdir(parents=True, exist_ok=True)

    matrix = cases(opts.chunks, STAGE0, opts.frequency_average_factor)
    if opts.cases:
        matrix = [c for c in matrix if c[0] in opts.cases]
        if not matrix:
            raise SystemExit("no such case: pick from "
                             + ", ".join(c[0] for c in
                                         cases(opts.chunks, STAGE0,
                                               opts.frequency_average_factor)))

    rows, nchan, ncorr = ms_shape(opts.skarabina, ms)
    print(f"[mem_recal] {ms.name}: {rows} rows x {nchan} chan x {ncorr} corr,"
          f" {opts.workers} workers, load {os.getloadavg()[0]:.2f}", flush=True)

    results = []
    for name, ops, mode, factor in matrix:
        chunk = int(name.rsplit("-", 1)[1])
        print(f"[mem_recal] {name}: {' + '.join(ops)} -> {mode}"
              f"{f' x{factor}' if factor > 1 else ''}", flush=True)
        reps = []
        for rep in range(opts.repeat):
            measured = run_case(name, ops, mode, factor, ms, opts.skarabina,
                                opts.workers, chunk, opts.timeout)
            reps.append(measured)
            print(f"    rep {rep + 1}/{opts.repeat}: measured"
                  f" {measured['peak_mib']:8.1f} MiB, planned"
                  f" {measured['planned_mib']:8.1f} MiB"
                  f" ({measured['planned_step']}), rc={measured['rc']},"
                  f" {measured['wall_s']}s, load {measured['load_before']}",
                  flush=True)

        peaks = sorted(r["peak_mib"] for r in reps)
        peak = statistics.median(peaks)
        result = dict(reps[-1])
        result["repeat"] = len(reps)
        result["peak_mib"] = round(peak, 1)
        result["peak_min_mib"] = peaks[0]
        result["peak_max_mib"] = peaks[-1]
        result["wall_s"] = round(statistics.median(r["wall_s"] for r in reps), 2)
        result["rc"] = max(r["rc"] for r in reps)
        in_flight = min(opts.workers, math.ceil(rows / chunk)) if rows else 0
        vis = in_flight * chunk * nchan * ncorr
        result["chunks_in_flight"] = in_flight
        result["input_vis_in_flight"] = vis
        result["implied_per_vis"] = (
            round((peak * 2**20 - BASE_BYTES) / vis, 2) if vis else None)
        result["ratio"] = (round(result["planned_mib"] / peak, 2) if peak else None)
        result["ratio_worst"] = (round(result["planned_mib"] / peaks[0], 2)
                                 if peaks[0] else None)
        results.append(result)
        print(f"    median {peak:8.1f} MiB (min {peaks[0]:.1f}, max {peaks[-1]:.1f}),"
              f" planned {result['planned_mib']:8.1f} MiB"
              f" ({result['planned_step']}), ratio {result['ratio']}"
              f" (worst {result['ratio_worst']}), implied"
              f" {result['implied_per_vis']} B/vis", flush=True)

    header = ["case", "mode", "chunk", "peak MiB", "min", "max", "planned MiB",
              "step", "ratio", "worst", "in flight", "implied B/vis", "wall s", "rc"]
    print()
    print("| " + " | ".join(header) + " |")
    print("|" + "---|" * len(header))
    for r in results:
        print(f"| {r['case']} | {r['mode']}"
              f"{'/' + str(r['factor']) if r['factor'] > 1 else ''}"
              f" | {r['row_chunk']} | {r['peak_mib']} | {r['peak_min_mib']}"
              f" | {r['peak_max_mib']} | {r['planned_mib']}"
              f" | {r['planned_step']} | {r['ratio']} | {r['ratio_worst']}"
              f" | {r['chunks_in_flight']} | {r['implied_per_vis']}"
              f" | {r['wall_s']} | {r['rc']} |")

    tag = opts.tag or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = SCRATCH / f"mem_recal_{tag}.json"
    path.write_text(json.dumps({
        "ms": str(ms), "rows": rows, "nchan": nchan, "ncorr": ncorr,
        "workers": opts.workers, "runs": results}, indent=2) + "\n")
    print(f"\n[mem_recal] wrote {path.relative_to(REPO)}")
    return 1 if any(r["rc"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
