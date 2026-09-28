#!/bin/bash
cd ~/github/skarabina && git pull -q --ff-only
git worktree prune; rm -rf /tmp/sk_old; git worktree add -q /tmp/sk_old v1.0.7
export DASK_MS_BACKEND=casacure
SW=$HOME/github/skarabina/bench/spectral-flags-L.yml
MS=$HOME/github/skarabina/.bench/scan1.ms
for list in "autos, uv-above 8000, nan, clip 0 100, spectral-window $SW" "autos, uv-above 8000, nan, clip 0 100, spectral-window $SW, rflag"; do
  for tree in /tmp/sk_old $HOME/github/skarabina; do
    cd $tree
    PYTHONPATH=$tree /usr/bin/time -f "RESULT $(basename $tree) wall=%e maxrss_kb=%M" $HOME/github/skarabina/.venv/bin/python /tmp/ioprobe.py \
      --ms $MS --workers 12 --flag "$list" --summary --msout $HOME/github/skarabina/.bench/io_out.ms --clobber --write-changed-only 2>&1 \
      | grep -aE "RESULT|IOPROBE TOTAL|IOPROBE   dask|Traceback|Error" | cut -c1-160
    rm -rf $HOME/github/skarabina/.bench/io_out.ms
  done
done
cd ~/github/skarabina; git worktree remove --force /tmp/sk_old
echo ALLDONE
