#!/bin/bash
cd ~/github/skarabina/.bench
OUT=${OUT:-run_grow.out}
echo "START $(date) $(uptime)"
DASK_MS_BACKEND=casacure /usr/bin/time -v ../.venv/bin/skarabina --ms scan1.ms --summary --time-average-factor 1 --frequency-average-factor 32 --clobber --flag save:imported --flag autos --flag "uv-above 2500" --flag nan --flag "clip 0 100" --flag "spectral-window ../bench/spectral-flags-L.yml" --field-of-view 3.3deg --msout bench_ave.ms > $OUT 2>&1
echo "RC $?"
grep -E "Elapsed|Maximum resident|User time|System time" $OUT
grep -iE "memory plan|whole table|save |write " $OUT | head -12
du -sh bench_ave.ms
DASK_MS_BACKEND=casacure ../.venv/bin/python -c "
import daskms
from casacore.tables import table
t=table(\"bench_ave.ms\",ack=False); print(\"out rows\", t.nrows(), t.getcol(\"DATA\",0,2).shape, t.getcol(\"SCAN_NUMBER\")[[0,-1]], t.getcol(\"FIELD_ID\")[[0,-1]])
v=table(\"scan1.ms.flagversions/flags.imported\",ack=False); print(\"version rows\", v.nrows(), v.getcol(\"FLAG\",0,1).shape)
"
echo "END $(date) $(uptime)"
echo ALLDONE
