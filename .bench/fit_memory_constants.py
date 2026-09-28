#!/usr/bin/env python3
"""Turn mem_recal's measurements into what the plan's constants must be.

For every measured run, the plan's estimate for its most expensive step is

    BASE + fixed + in_flight x row_chunk x nchan x ncorr x (per_vis + extra)

(plus a whole-table reserve when the backend buffers), so a measured peak
implies the per-visibility cost that step must carry:

    required = (peak - BASE - fixed) / (in_flight x row_chunk x nchan x ncorr)

`in_flight` is the number of chunks that can be resident: the workers, capped
by how many chunks the table has.  Reads .bench/scratch/mem_recal/*.json.
"""
import glob
import json
import math
import statistics
import sys
from pathlib import Path

BASE = 0.5 * 2**30
FIXED = {"tfcrop": 3.5 * 2**30, "rflag": 2.5 * 2**30}


def load():
    runs = {}
    for path in sorted(glob.glob(str(Path(__file__).parent / "scratch" /
                                     "mem_recal" / "mem_recal_*.json"))):
        data = json.loads(Path(path).read_text())
        for run in data["runs"]:
            key = (run["case"], data["workers"], run["row_chunk"])
            entry = runs.setdefault(key, {
                "case": run["case"], "mode": run["mode"], "factor": run["factor"],
                "chunk": run["row_chunk"], "workers": data["workers"],
                "rows": data["rows"], "nchan": data["nchan"],
                "ncorr": data["ncorr"], "peaks": [], "planned": run["planned_mib"],
                "step": run["planned_step"], "files": []})
            entry["peaks"].append(run["peak_mib"])
            entry["files"].append(Path(path).name)
    return runs


def main():
    runs = load()
    rows_out = []
    for key, run in sorted(runs.items()):
        nchunks = math.ceil(run["rows"] / run["chunk"])
        in_flight = min(run["workers"], nchunks)
        vis = in_flight * run["chunk"] * run["nchan"] * run["ncorr"]
        peaks = sorted(run["peaks"])
        median = statistics.median(peaks)
        # The dominant step for these lists is a verb (nan/clip/spectral-window);
        # `save` is charged its own chunk; `read` dominates the save-only runs.
        step = "read" if run["case"].startswith("save-") else "nan"
        fixed = FIXED.get(step, 0.0)
        required = (median * 2**20 - BASE - fixed) / vis if vis else float("nan")
        required_max = (peaks[-1] * 2**20 - BASE - fixed) / vis if vis else float("nan")
        rows_out.append(dict(run, nchunks=nchunks, in_flight=in_flight,
                             median=median, min=peaks[0], max=peaks[-1],
                             reps=len(peaks), step=step,
                             required=round(required, 2),
                             required_max=round(required_max, 2)))

    header = ["case", "mode", "chunk", "workers", "chunks", "in flight", "reps",
              "median MiB", "max MiB", "planned", "required B/vis",
              "required (max) B/vis"]
    print("| " + " | ".join(header) + " |")
    print("|" + "---|" * len(header))
    for r in rows_out:
        print(f"| {r['case']} | {r['mode']}"
              f"{'/' + str(r['factor']) if r['factor'] > 1 else ''}"
              f" | {r['chunk']} | {r['workers']} | {r['nchunks']}"
              f" | {r['in_flight']} | {r['reps']} | {r['median']} | {r['max']}"
              f" | {r['planned']} | {r['required']} | {r['required_max']} |")

    print("\nRequired per-vis by family (median peak; max over chunk sizes):")
    families = {}
    for r in rows_out:
        if r["step"] == "read":
            family = "save-alone (read step)"
        elif r["mode"] == "none":
            family = "verbs, no write"
        elif r["mode"] == "flags":
            family = "verbs + flags write"
        elif r["factor"] > 1:
            family = "verbs + averaged write"
        else:
            family = "verbs + full write"
        families.setdefault(family, []).append((r["chunk"], r["workers"],
                                                r["required"]))
    for family, values in sorted(families.items()):
        worst = max(values, key=lambda v: v[2])
        print(f"  {family:<26} max {worst[2]:6.2f} B/vis"
              f"  (at chunk {worst[0]}, {worst[1]} workers)"
              f"   all: {', '.join(f'{v[2]:.2f}@{v[0]}x{v[1]}w' for v in sorted(values))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
