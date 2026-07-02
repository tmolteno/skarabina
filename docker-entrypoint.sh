#!/bin/sh
# Docker entrypoint: dispatches to skarabina or skarabina-analyze
# First argument must be 'run' (skarabina) or 'analyze' (skarabina-analyze).
case "${1:-}" in
    run)       shift; exec skarabina "$@" ;;
    analyze)   shift; exec skarabina-analyze "$@" ;;
    "")        echo "Usage: docker run ... [run|analyze] [args...]" >&2
                echo "  run      Run skarabina (flag, summarize, etc.)" >&2
                echo "  analyze  Run skarabina-analyze (image analysis)" >&2
                exit 1 ;;
    *)         echo "Unknown command: $1. Use 'run' or 'analyze'." >&2; exit 1 ;;
esac
