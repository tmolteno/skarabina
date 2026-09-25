#!/bin/bash
# Compare MS column reads, wall time and peak RSS of two skarabina versions.
#
#   bench/compare_versions.sh <ms> <old-ref> "<flag list>" [skarabina args...]
#
# Runs the same skarabina command through bench/io_probe.py twice: once from a
# temporary worktree of <old-ref> (e.g. v1.0.7), once from this checkout.  Any
# output MS goes to .bench/cmp_out.ms and is deleted after each run; pass
# --msout OUT to write there ("OUT" is replaced).  Use a COPY of the data for
# anything that writes beside the input (save:, --apply).  Prints the reads per
# compute and a RESULT line (wall, peak RSS) per version.  Check `uptime` first:
# timings on a loaded machine mean little.
#
#   e.g. bench/compare_versions.sh .bench/scan1.ms v1.0.7 \
#          "autos, uv-above 8000, nan, clip 0 100, rflag" \
#          --summary --msout OUT --clobber --write-changed-only
# If it is killed (SIGKILL) the cleanup trap cannot run: remove the temporary
# worktree with `git worktree prune` and delete .bench/cmp_out.ms by hand.
set -euo pipefail
if [ $# -lt 3 ]; then
  echo "usage: $0 <ms> <old-ref> \"<flag list>\" [skarabina args, OUT = output MS]" >&2
  exit 2
fi
MS=$(realpath "$1"); OLD=$2; FLAGS=$3; shift 3
REPO=$(cd "$(dirname "$0")/.." && pwd)
OUT=$REPO/.bench/cmp_out.ms
WT=$(mktemp -d /tmp/skarabina-cmp-XXXX)
export DASK_MS_BACKEND=${DASK_MS_BACKEND:-casacure}
trap 'cd "$REPO"; git worktree remove --force "$WT" 2>/dev/null || true; rm -rf "$OUT"' EXIT
rmdir "$WT"; git -C "$REPO" worktree add -q "$WT" "$OLD"
# Only an argument that is exactly OUT is replaced.
ARGS=(); for a in "$@"; do if [ "$a" = OUT ]; then ARGS+=("$OUT"); else ARGS+=("$a"); fi; done
for tree in "$WT" "$REPO"; do
  label=$([ "$tree" = "$WT" ] && echo "$OLD" || echo "$(git -C "$REPO" rev-parse --short HEAD)")
  echo "== $label"
  (cd "$tree" && PYTHONPATH="$tree" /usr/bin/time -f "RESULT $label wall=%e s maxrss=%M kB" \
     "$REPO/.venv/bin/python" "$REPO/bench/io_probe.py" --ms "$MS" --flag "$FLAGS" "${ARGS[@]}" 2>&1 \
     | grep -aE "RESULT|IOPROBE|Traceback|Error" | cut -c1-200) || true
  rm -rf "$OUT"
done
