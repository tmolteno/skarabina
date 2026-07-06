# Agent Instructions

## Package structure

The repo contains two Python packages:

| Package | Path | Purpose |
|---|---|---|
| `skarabina` | `./` (root) | CLI tool (`skarabina`, `skarabina-analyze`) |
| `skarabina-cargo` | `./skarabina_cargo/` | Stimela cab definitions |

`skarabina-cargo` depends on `stimela` and packages the cab schema
(`skarabina.yml`) and base vars (`genesis/skarabina-base.yml`).
`skarabina` has no dependency on `stimela` — all CLI parameters are
defined as explicit `@click.option` decorators in `main.py`.

## Version bump checklist

When bumping the version for a release, update these files to match:

| File | Field |
|---|---|
| `pyproject.toml` | `project.version` |
| `skarabina_cargo/pyproject.toml` | `project.version` |
| `skarabina_cargo/genesis/skarabina-base.yml` | `vars.skarabina.images.version` |

All three must reference the same version number (e.g. `0.6.2` in both
`pyproject.toml` files and `0.6.2` in the YAML).  Do **not** include a
`v` prefix in the YAML version — CI's `docker/metadata-action` uses
`type=semver` which strips the `v` from the git tag, so the published
Docker image tag is `0.6.2`, not `v0.6.2`.  The YAML value must match
the image tag exactly.

Also add a changelog entry to `doc/CHANGES.md`.  Do not create or use a
`CHANGES.md` at the top level — the canonical changelog lives under `doc/`.

## Docker

- The single `Dockerfile` builds for `linux/amd64` and `linux/arm64`.
- CI (`.github/workflows/docker-publish.yml`) builds and pushes on
  `v*.*.*` tags only.
