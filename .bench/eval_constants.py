#!/usr/bin/env python3
"""Score candidate memory constants against the mem_recal measurements.

Calls skarabina.memory.plan exactly as the CLI does for each measured run and
compares the plan's largest step estimate with the measured peak.  The
acceptance the handover asks for: plan >= measured, and within ~25 % of it.

    python .bench/eval_constants.py
"""
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from skarabina import memory  # noqa: E402

GB = 2**30
MEERKAT = dict(nrow=143716, nchan=2511, ncorr=2)
STAGE0 = ["save", "autos", "uv-above", "nan", "clip", "spectral-window"]
NVIS = MEERKAT["nrow"] * MEERKAT["nchan"] * MEERKAT["ncorr"]
LIMIT = 58 * GB          # what the CLI saw on schmalzburg
MEASURED = REPO / ".bench" / "scratch" / "mem_recal"


def candidates():
    yield "before (read 1, verbs 4, write 8, no averaging term, flags 1)", dict(
        read=1, verb=4, autos=2, write=8, averaging=0, flags=1, cap=False)
    yield "after  (read 0.5, verbs 4, write 4.5, averaging 4.5, flags 0.5)", dict(
        read=0.5, verb=4, autos=2, write=4.5, averaging=4.5, flags=0.5, cap=False)
    yield "after + chunk-count cap (rejected: under-predicts)", dict(
        read=0.5, verb=4, autos=2, write=4.5, averaging=4.5, flags=0.5, cap=True)


def apply(params):
    memory.CHUNK_COST = {
        "read": (params["read"], 0),
        "autos": (params["autos"], 0),
        "uv-above": (1, 0),
        "nan": (params["verb"], 0),
        "clip": (params["verb"], 0),
        "spectral-window": (params["verb"], 0),
        "restore": (3, 0),
        "tfcrop": (30, 3.5 * GB),
        "rflag": (36, 2.5 * GB),
    }
    memory.CONCURRENT_WRITE_COST = {"write": params["write"],
                                    "write-flags": params["flags"]}
    memory.CONCURRENT_AVERAGING_COST = params["averaging"]
    memory.chunks_in_flight = capped if params["cap"] else \
        (lambda workers, nrow, row_chunk: workers)


def capped(workers, nrow, row_chunk):
    if not row_chunk or not nrow:
        return workers
    return min(workers, max(1, math.ceil(nrow / row_chunk)))


def plan_estimate(run):
    """The plan's largest per-step estimate for one measured run, in MiB."""
    ops = ["save"] if run["case"].startswith("save-") else STAGE0
    write = {"none": None, "flags": "write-flags", "full": "write"}[run["mode"]]
    out = NVIS // run["factor"] if run["factor"] > 1 else NVIS
    result = memory.plan(ops, LIMIT, run["workers"], row_chunk=run["row_chunk"],
                         write=write, out_visibilities=out,
                         concurrent_write=True, streamed_writes=True, **MEERKAT)
    steps = set(memory.CHUNK_COST) | {"save", "write", "write-flags"}
    estimates = []
    for line in result.lines[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[0] in steps:
            try:
                estimates.append(float(parts[1]) * GB)
            except ValueError:
                pass
    return max(estimates) / 2**20 if estimates else 0.0


def measurements():
    runs = {}
    for path in sorted(MEASURED.glob("mem_recal_*.json")):
        data = json.loads(path.read_text())
        for run in data["runs"]:
            key = (run["case"], data["workers"])
            entry = runs.setdefault(key, dict(
                case=run["case"], mode=run["mode"], factor=run["factor"],
                row_chunk=run["row_chunk"], workers=data["workers"], peaks=[]))
            entry["peaks"].append(run["peak_mib"])
    for entry in runs.values():
        entry["peaks"].sort()
        entry["median"] = entry["peaks"][len(entry["peaks"]) // 2]
    return sorted(runs.values(), key=lambda r: (r["case"], r["workers"]))


def main():
    runs = measurements()
    print(f"{len(runs)} measured configurations")
    for name, params in candidates():
        apply(params)
        print(f"\n=== {name} ===")
        worst_over, worst_under = (0.0, None), (9e9, None)
        inside = 0
        for run in runs:
            estimate = plan_estimate(run)
            ratio = estimate / run["median"]
            if ratio > worst_over[0]:
                worst_over = (ratio, run)
            if ratio < worst_under[0]:
                worst_under = (ratio, run)
            if 1.0 <= ratio <= 1.25:
                inside += 1
            flag = "ok" if 1.0 <= ratio <= 1.25 else ("!!" if ratio < 1 else " >")
            print(f"  {flag} {run['case']:<14} {run['workers']:2d}w median"
                  f" {run['median']:8.1f} MiB  plan {estimate:8.1f}"
                  f"  ratio {ratio:5.2f}  ({len(run['peaks'])} reps)")
        print(f"  within 1.0-1.25: {inside}/{len(runs)};"
              f" worst over {worst_over[0]:.2f} ({worst_over[1]['case']}"
              f" {worst_over[1]['workers']}w); worst under"
              f" {worst_under[0]:.2f} ({worst_under[1]['case']}"
              f" {worst_under[1]['workers']}w)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
