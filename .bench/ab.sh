#!/bin/bash
cd ~/github/skarabina/.bench
FL=(--flag save:imported --flag autos --flag "uv-above 2500" --flag nan --flag "clip 0 100" --flag "spectral-window ../bench/spectral-flags-L.yml")
run() { # name venv env workload
  local name=$1 venv=$2 be=$3 wl=$4 out=ab_$1.out
  if [ $wl = ave ]; then extra=(--time-average-factor 1 --frequency-average-factor 32 --field-of-view 3.3deg --msout ab_out.ms)
  else extra=(--write-changed-only --msout ab_out.ms); fi
  echo "== $name load $(cut -d" " -f1 /proc/loadavg)"
  env $be /usr/bin/time -v ../$venv/bin/skarabina --ms scan1.ms --summary --clobber "${FL[@]}" "${extra[@]}" > $out 2>&1
  echo "rc $? $(grep -E "Elapsed" $out | awk "{print \$NF}") wall, $(grep "Maximum resident" $out | awk "{printf \"%.2f GB\", \$NF/1048576}")"
  rm -rf ab_out.ms
}
for rep in 1 2; do
  run ave-cure-$rep .venv DASK_MS_BACKEND=casacure ave
  run ave-core-$rep .venv-casacore X=1 ave
  run flags-cure-$rep .venv DASK_MS_BACKEND=casacure flags
  run flags-core-$rep .venv-casacore X=1 flags
done
echo ALLDONE
