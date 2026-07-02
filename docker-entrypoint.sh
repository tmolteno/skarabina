#!/bin/sh
# Docker entrypoint: dispatches to skarabina or skarabina-analyze
case "${1:-}" in
    analyze|skarabina-analyze) shift; exec skarabina-analyze "$@" ;;
    *) exec skarabina "$@" ;;
esac
