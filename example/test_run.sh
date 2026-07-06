#!/bin/sh
# Example: flag, average, optimize, and summarize a measurement set.
#
# Requirements:
#   - skarabina installed (pip install skarabina or uv sync)
#   - A measurement set at the path below (edit to match your data)
#
# The spectral-flags file is in this directory; run from example/ or use
# an absolute path.

set -eu

MS="${1:-$HOME/astro/merghers/mergA_tim.ms}"

uv run skarabina \
    --ms "$MS" \
    --flag-nan \
    --flag-clip-lo 0 \
    --flag-clip-hi 100 \
    --flag-uv-above 1000 \
    --flag-spectral-window spectral-flags.example.yml \
    --time-average-factor 3 \
    --frequency-average-factor 20 \
    --optimize \
    --msout test_spw.ms \
    --clobber \
    --barber \
    --summary
