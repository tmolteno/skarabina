<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# Skarabina Documentation

Skarabina is a 1GC radio astronomy RFI flagger for efficient, low-memory
flagging of measurement sets.

- [Usage & CLI reference](usage.md)
- [Time & frequency averaging](averaging.md)
- [Measurement set analyzer](analyze.md)
- [Changelog](CHANGES.md)

## Quick start

```sh
pip install skarabina

# Flag and write a cleaned MS
skarabina --ms raw.ms --flag-nan --flag-uv-above 4000 \
    --flag-spectral-window spectral-flags.yml \
    --time-average-factor 3 --optimize --msout clean.ms --clobber

# Analyze a measurement set
skarabina-analyze --ms raw.ms --image-fov 2.5
```
