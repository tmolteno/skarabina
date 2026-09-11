#!/bin/sh
# Demo: pass skarabina-analyze results into a larger imaging pipeline.
#
# Requirements:
#   - skarabina-cargo installed (pip install skarabina-cargo), or run from
#     cargo/ with the cab package importable
#   - stimela >= 2.1.2 on PATH (uv run stimela ... also works)
#   - a measurement set; edit the default below or pass one as $1
#
# The recipe flags the MS, analyzes it, and shows the recommended image size,
# resolution, and imager arguments that follow from them.

set -eu

MS="${1:-$HOME/astro/cyg2052.ms}"

stimela run skarabina-demo-pipeline.yml demo-imaging-pipeline ms="$MS"
