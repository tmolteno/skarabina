#!/usr/bin/env python3
"""Time skarabina's flagging on a real measurement set (local test-bench).

The workload is the flag sequence ../meerkat_imaging runs at stage 0 -- the
`flag-average` step of `white-belt-0-flagging.yml` -- plus the `rflag` verb that
stands in for the CASA autoflag loop over the calibrator fields.  The list and
its provenance are in `meerkat-flags.yml` next to this file.  By default it
runs against `.bench/data/bpcal.ms`, the local copy of a real MeerKAT
bandpass-calibrator MS.

Two measurement modes:

  seq      one skarabina process running the whole list, which is what the
           pipeline does; the headline number.
  per-op   one process per flag operation, each starting from the same input
           flags, so the cost of an operation can be attributed.  The
           `save:imported` marker is the I/O floor of that table: read the MS,
           write the flags, write nothing else.

Each run is a fresh subprocess; wall time is measured around it and peak RSS
comes from ``wait4``'s rusage, so it is the child's true maximum rather than
the Python peak.  Runs go through `run_stage.py`, which fixes the backend
import order before skarabina loads.

Runs write only the flag columns (``--write-changed-only`` hard-links the
unchanged storage blocks), and the output MS is deleted afterwards unless
``--keep-ms-out`` is given.  Note that a run whose list contains
``save:imported`` rotates the input's saved flag version and leaves the old one
behind in ``<ms>.flagversions`` (about 18 MB for bpcal.ms); pass
``--drop-save-imported`` to skip it.

There is a sharp edge in that write path.  Hard-linking a block and then
chmod'ing it read-only also makes the *input's* own ``table.fN`` read-only
(same inode, that is the point of the hard link), and a later run copies such a
block with ``shutil.copy2`` -- which preserves the mode -- so it dies with
``storage error: Permission denied`` on the first column it has to write.  The
bench restores owner-write on the input's blocks before every run
(``--repair-input-perms``, the default); pass ``--no-repair-input-perms`` to
watch it fail instead.  BENCHMARKS.md has the reproduction.

Results are printed as a Markdown table and saved under ``bench/results/`` as
JSON + Markdown.  The numbers worth quoting are transcribed into BENCHMARKS.md.

Usage:
    python bench/flag_timing.py                       # seq on bpcal.ms
    python bench/flag_timing.py --mode both
    python bench/flag_timing.py --ms .bench/ms_cure_2k.ms --drop-save-imported
    python bench/flag_timing.py --mode per-op --rflag-args "winsize=5"
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CHILD = HERE / "run_stage.py"
DEFAULT_MS = REPO / ".bench" / "data" / "bpcal.ms"
DEFAULT_OPS = HERE / "meerkat-flags.yml"
DEFAULT_SPECTRAL = HERE / "spectral-flags-L.yml"
SCRATCH = REPO / ".bench" / "scratch" / "flag_timing"
RESULTS = HERE / "results"


# --------------------------------------------------------------------------
# environment and workload description
# --------------------------------------------------------------------------

def find_python(explicit: str | None) -> str:
    """The interpreter that runs the child: it must see the local skarabina."""
    for candidate in (
        explicit,
        os.environ.get("BENCH_PY"),
        REPO / ".venv-bench" / "bin" / "python",
        REPO / ".venv" / "bin" / "python",
        sys.executable,
    ):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return sys.executable


def host_info() -> dict:
    cpu = platform.processor()
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    mem_kib = 0
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal"):
                    mem_kib = int(line.split()[1])
                    break
    except OSError:
        pass
    return {
        "hostname": socket.gethostname(),
        "cpu": cpu,
        "cores": os.cpu_count(),
        "mem_gib": round(mem_kib / 1024 / 1024, 1),
        "platform": platform.platform(),
    }


def project_info() -> dict:
    import re

    version = "unknown"
    try:
        text = (REPO / "pyproject.toml").read_text()
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
        if match:
            version = match.group(1)
    except OSError:
        pass
    rev = "unknown"
    try:
        rev = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        pass
    return {"skarabina": version, "git_rev": rev}


def describe_ms(python: str, env: dict, ms: Path) -> dict:
    """Workload stats, read through the same interpreter and backend."""
    try:
        proc = subprocess.run(
            [python, str(CHILD), "describe", str(ms)],
            capture_output=True, text=True, env=env, timeout=600,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("DESCRIBE="):
                return json.loads(line[len("DESCRIBE="):])
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return {"ms": str(ms)}


# --------------------------------------------------------------------------
# the flag list
# --------------------------------------------------------------------------

def load_ops(path: Path, uv_max: int, spectral: Path, rflag_args: str | None,
             extra: list, drop_save: bool) -> list:
    """Read `meerkat-flags.yml` and apply the command-line substitutions."""
    try:
        import yaml
    except ImportError:  # pragma: no cover - the bench env always has yaml
        raise SystemExit("PyYAML is needed to read the flag list")

    spec = yaml.safe_load(path.read_text())
    subs = {"uv-max": str(uv_max), "spectral-flags": str(spectral)}
    ops = [entry.format(**subs) for entry in spec["ops"]]

    if rflag_args is not None:
        ops = [
            f"rflag [{rflag_args}]" if op.split()[0] == "rflag" else op
            for op in ops
        ]
    if drop_save:
        ops = [op for op in ops if not op.startswith("save:")]
    ops.extend(extra)
    return ops


# --------------------------------------------------------------------------
# one timed run
# --------------------------------------------------------------------------

def run_once(python: str, ms: Path, msout: Path, ops: list, opts, env: dict,
             log_dir: Path, label: str) -> dict:
    """Run one skarabina process over the whole op list; time it and reap it."""
    cmd = [
        python, str(CHILD), "flag",
        "--ms", str(ms),
        "--msout", str(msout),
        "--clobber", "--summary",
        "--frequency-average-factor", str(opts.frequency_average_factor),
        "--time-average-factor", str(opts.time_average_factor),
        "--field-of-view", opts.field_of_view,
    ]
    if opts.write_changed_only:
        cmd.append("--write-changed-only")
    if opts.scan:
        cmd += ["--scan", opts.scan]
    for op in ops:
        cmd += ["--flag", op]

    if not opts.keep_ms_out:
        for stale in (msout, Path(str(msout) + ".flagversions")):
            shutil.rmtree(stale, ignore_errors=True)
    if opts.write_changed_only and opts.repair_input_perms:
        repaired = repair_input_perms(ms)
        if repaired:
            print(f"[flag_timing] {label}: restored write permission on"
                  f" {repaired} read-only block(s) of the input MS", flush=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{label}.log"

    with open(log_path, "w") as log:
        log.write("CMD: " + " ".join(cmd) + "\n")
        log.flush()
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                env=env)
        t0 = time.perf_counter()
        deadline = t0 + opts.timeout if opts.timeout else None
        timed_out = False
        while True:
            pid, status, rusage = os.wait4(proc.pid, os.WNOHANG)
            if pid != 0:
                break
            if deadline and time.perf_counter() > deadline:
                timed_out = True
                proc.kill()
                _pid, status, rusage = os.wait4(proc.pid, 0)
                break
            time.sleep(0.2)
        wall = time.perf_counter() - t0

    output = log_path.read_text(errors="replace")
    result = {
        "label": label,
        "ops": ops,
        "rc": os.waitstatus_to_exitcode(status),
        "timed_out": timed_out,
        "wall_s": round(wall, 2),
        "peak_rss_mib": round((rusage.ru_maxrss if rusage else 0) / 1024, 1),
        "flagged_percent": _flagged_percent(output),
        "log": str(log_path.relative_to(REPO)),
    }
    note = _failure_note(result["rc"], output)
    if note:
        result["note"] = note
    if not opts.keep_ms_out:
        for stale in (msout, Path(str(msout) + ".flagversions")):
            shutil.rmtree(stale, ignore_errors=True)
    return result


def repair_input_perms(ms: Path) -> int:
    """Make the input's storage blocks writable again; return how many changed.

    `--write-changed-only` hard-links the blocks it does not rewrite into the
    output and then chmods them read-only so that a later write to the output
    cannot reach the input.  Hard links share the inode, so that chmod also
    lands on the *input's* own file, and the MS is left with read-only
    `table.fN` blocks.  The next run then copies such a block with
    ``shutil.copy2`` -- which preserves the mode -- and fails with
    ``storage error: Permission denied`` on the first column it has to write.

    The bench therefore restores the owner-write bit before every run, which is
    the state a measurement set normally arrives in.  It never touches data or
    any other permission bit.  See BENCHMARKS.md for the reproduction.
    """
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


def _failure_note(rc: int, output: str) -> str | None:
    """Say what went wrong in one line, for the result table."""
    if rc == 0:
        return None
    if "Permission denied" in output:
        return (
            "write failed with EACCES: a shared table.fN block of the input is"
            " read-only, so the copy of it could not be written"
            " (--repair-input-perms fixes the input's modes)"
        )
    return "run failed; see the log"


def _flagged_percent(output: str) -> float | None:
    """The 'Flagging Summary ... NN.NN %' figure, when the run printed one."""
    match = re.search(r"^Flagging Summary.*?:\s*([\d.]+)\s*%", output, re.M)
    return float(match.group(1)) if match else None


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def render_markdown(meta: dict, runs: list) -> str:
    host, work = meta["host"], meta["workload"]
    lines = [
        f"# skarabina flag timing -- {meta['backend']} backend",
        "",
        f"- date: {meta['generated']}",
        f"- skarabina {meta['skarabina']} (git {meta['git_rev']}), "
        f"python {meta['python_version']}",
        f"- host: {host['hostname']}, {host['cpu']}, {host['cores']} cores, "
        f"{host['mem_gib']} GiB RAM",
        f"- workload: {meta['ms']} -- {work.get('rows', '?')} rows, "
        f"{work.get('channels', '?')} channels, "
        f"{work.get('correlations', '?')} correlations, "
        f"{work.get('integrations', '?')} integrations, "
        f"{work.get('fields', '?')} fields, "
        f"{_gib(work.get('size_bytes'))}",
        f"- averaging: channel x{meta['options']['frequency_average_factor']}, "
        f"time x{meta['options']['time_average_factor']}; "
        f"write-changed-only={meta['options']['write_changed_only']}; "
        f"repeat={meta['options']['repeat']}",
        f"- operations: {', '.join('`' + op + '`' for op in meta['ops'])}",
        "",
        "| mode | operation(s) | wall (s) | peak RSS (MiB) | flags reported"
        " | rc |",
        "|---|---|---|---|---|---|",
    ]
    for run in runs:
        shown = (
            f"all {len(run['ops'])}, one process"
            if run.get("mode") == "seq" else ", ".join(run["ops"])
        )
        lines.append(
            f"| {run['mode']} | {shown} | {run['wall_s']:.1f} | "
            f"{run['peak_rss_mib']:.0f} | {_pct(run.get('flagged_percent'))} | "
            f"{run['rc']}{' (timeout)' if run['timed_out'] else ''} |"
        )
        if run.get("note"):
            lines.append(f"| | ^ {run['note']} | | | | |")
    lines.append("")
    return "\n".join(lines)


def _gib(size_bytes) -> str:
    if not size_bytes:
        return "unknown size"
    return f"{size_bytes / 1024 ** 3:.2f} GiB"


def _pct(value) -> str:
    return "n/a" if value is None else f"{value:.2f}%"


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--ms", type=Path, default=DEFAULT_MS,
                    help=f"input measurement set (default: {DEFAULT_MS})")
    ap.add_argument("--mode", choices=["seq", "per-op", "both"], default="seq")
    ap.add_argument("--ops-file", type=Path, default=DEFAULT_OPS,
                    help="YAML list of flag operations to time")
    ap.add_argument("--backend", choices=["casacure", "casacore"],
                    default="casacure")
    ap.add_argument("--python", default=None,
                    help="interpreter for the child (default: .venv-bench)")
    ap.add_argument("--repeat", type=int, default=1,
                    help="run the whole mode this many times")
    ap.add_argument("--scan", default=None,
                    help="pass through to skarabina --scan (e.g. '1,12' or '0~5')")
    ap.add_argument("--uv-max", type=int, default=8000,
                    help="metres for the uv-above operation (meerkat ms-uvmax)")
    ap.add_argument("--spectral-flags", type=Path, default=DEFAULT_SPECTRAL,
                    help="rules file for the spectral-window operation")
    ap.add_argument("--frequency-average-factor", type=int, default=1)
    ap.add_argument("--time-average-factor", type=int, default=1)
    ap.add_argument("--field-of-view", default="3.3deg",
                    help="meerkat image-fov (used by --summary)")
    ap.add_argument("--rflag-args", default=None,
                    help="replace rflag's parameters, e.g. 'winsize=5,"
                         " timedevscale=4' (default: CASA's, as the recipe uses)")
    ap.add_argument("--extra-op", action="append", default=[],
                    help="append an operation after the meerkat list (repeatable)")
    ap.add_argument("--drop-save-imported", action="store_true",
                    help="skip save:imported, so no flag version is rotated")
    ap.add_argument("--no-write-changed-only", dest="write_changed_only",
                    action="store_false",
                    help="copy every column instead of hard-linking the "
                         "unchanged ones (the full-write cost)")
    ap.add_argument("--repair-input-perms", dest="repair_input_perms",
                    action="store_true", default=True,
                    help="restore owner-write on the input's read-only table.fN"
                         " blocks before each run (default; a previous"
                         " --write-changed-only run leaves them read-only, see"
                         " BENCHMARKS.md)")
    ap.add_argument("--no-repair-input-perms", dest="repair_input_perms",
                    action="store_false",
                    help="leave the input's block permissions alone, so a"
                         " read-only block fails the run the way it would fail"
                         " outside the bench")
    ap.add_argument("--keep-ms-out", action="store_true",
                    help="do not delete the output MS after each run")
    ap.add_argument("--timeout", type=float, default=None,
                    help="kill a run after this many seconds")
    ap.add_argument("--out-dir", type=Path, default=RESULTS)
    ap.add_argument("--tag", default=None, help="name for this measurement")
    ap.add_argument("--no-save", action="store_true",
                    help="print the table but write no result files")
    opts = ap.parse_args()

    ms = opts.ms.resolve()
    if not ms.exists():
        raise SystemExit(f"no such measurement set: {ms}")

    python = find_python(opts.python)
    python_version = subprocess.run(
        [python, "-c", "import platform; print(platform.python_version())"],
        capture_output=True, text=True,
    ).stdout.strip() or "unknown"

    env = dict(os.environ)
    env.pop("DASK_MS_BACKEND", None)
    if opts.backend == "casacure":
        env["DASK_MS_BACKEND"] = "casacure"
    env["PYTHONUNBUFFERED"] = "1"

    ops = load_ops(opts.ops_file.resolve(), opts.uv_max,
                   opts.spectral_flags.resolve(), opts.rflag_args,
                   opts.extra_op, opts.drop_save_imported)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = opts.tag or f"{ms.name}-{opts.backend}-{stamp}"
    log_dir = SCRATCH / "logs" / tag
    msout = SCRATCH / f"out_{tag}.ms"

    meta = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "host": host_info(),
        "python": python,
        "python_version": python_version,
        "backend": opts.backend,
        "ms": str(ms),
        "workload": describe_ms(python, env, ms),
        "ops": ops,
        "options": {
            "mode": opts.mode,
            "repeat": opts.repeat,
            "frequency_average_factor": opts.frequency_average_factor,
            "time_average_factor": opts.time_average_factor,
            "field_of_view": opts.field_of_view,
            "write_changed_only": opts.write_changed_only,
            "uv_max": opts.uv_max,
            "spectral_flags": str(opts.spectral_flags.resolve()),
            "rflag_args": opts.rflag_args,
        },
        **project_info(),
    }

    runs = []
    for index in range(opts.repeat):
        suffix = "" if opts.repeat == 1 else f"_r{index + 1}"
        if opts.mode in ("seq", "both"):
            label = f"seq{suffix}"
            print(f"[flag_timing] {label}: {len(ops)} operations", flush=True)
            result = run_once(python, ms, msout, ops, opts, env, log_dir, label)
            result["mode"] = "seq"
            runs.append(result)
            print(f"[flag_timing] {label}: {result['wall_s']}s, "
                  f"{result['peak_rss_mib']} MiB, rc={result['rc']}", flush=True)
        if opts.mode in ("per-op", "both"):
            for op_index, op in enumerate(ops):
                label = f"op{op_index + 1}{suffix}"
                print(f"[flag_timing] {label}: {op}", flush=True)
                result = run_once(python, ms, msout, [op], opts, env, log_dir,
                                  label)
                result["mode"] = "per-op"
                runs.append(result)
                print(f"[flag_timing] {label}: {result['wall_s']}s, "
                      f"{result['peak_rss_mib']} MiB, rc={result['rc']}",
                      flush=True)

    markdown = render_markdown(meta, runs)
    print()
    print(markdown)

    if not opts.no_save:
        opts.out_dir.mkdir(parents=True, exist_ok=True)
        payload = dict(meta, runs=runs)
        json_path = opts.out_dir / f"{tag}.json"
        md_path = opts.out_dir / f"{tag}.md"
        json_path.write_text(json.dumps(payload, indent=2) + "\n")
        md_path.write_text(markdown)
        print(f"[flag_timing] wrote {json_path.relative_to(REPO)} and "
              f"{md_path.relative_to(REPO)}")

    failed = [r for r in runs if r["rc"] != 0 or r["timed_out"]]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
