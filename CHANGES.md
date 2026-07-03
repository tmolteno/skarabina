# Changelog

## [0.6.1] — 2026-07-03

### Fixed

- **CI: add QEMU for multi-arch builds.**  `docker/setup-qemu-action` is required to build `linux/arm64` on x86_64 runners.  Without it, the `:latest` tag contained only an amd64 manifest, causing `no matching manifest for linux/arm64/v8` on aarch64.
- **CI: add `:latest` tag.**  The metadata action now emits `type=raw,value=latest` so `ghcr.io/tmolteno/skarabina:latest` always points to the most recent release.

## [0.6.0] — 2026-07-03

### Docker

- **Single multi-arch Dockerfile.**  One `Dockerfile` now builds on x86_64 and aarch64 (DGX Spark, AWS Graviton, Raspberry Pi).  On x86_64 `python-casacore` installs from a pre-built wheel; on aarch64 it builds from source via scikit-build-core with `CMAKE_CXX_STANDARD=17` to avoid C++20 `std::allocator` incompatibilities.
- **Entrypoint requires explicit command.**  `docker run ...` now requires `run` or `analyze` as the first argument.  Example: `docker run ... run --ms /data/obs.ms --summary`.  The old bare-arg dispatch (no `run` prefix) is no longer supported.
- **Removed conda-based Dockerfile.**  `Dockerfile.conda` and `Dockerfile.source` are deleted; the main `Dockerfile` handles all architectures.
- **CI overhaul.**  Docker builds are tested on every push and PR.  Tagged releases publish a single multi-arch image (`linux/amd64`, `linux/arm64`) to `ghcr.io/tmolteno/skarabina` with a unified `:latest` tag (no `-conda` suffix).

### Documentation

- `INSTALL.md` rewritten: Docker pull/run/workflow examples, `CMAKE_ARGS` workaround for bare-metal aarch64, C++ allocator error explanation.

## [0.5.1] — earlier

- Initial Docker support (x86_64 only, conda Dockerfile for aarch64).

## [0.5.0] — earlier

- First release with conda Dockerfile.
