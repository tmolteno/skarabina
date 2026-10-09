# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""The release orchestration's version bump and changelog stamp (tasks.py).

`invoke release` edits five files and pushes a tag that three CI workflows
build from; a wrong rewrite there ships a broken release.  These tests run
the rewriting against copies of the files in tmp_path, shaped like the real
ones -- including the traps: a dependency that happens to share the version
number, the lockfile's per-package blocks, and the changelog's
newest-first-with-Unreleased-on-top layout.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tasks  # noqa: E402

YAML_REL = "cargo/skarabina_cargo/genesis/skarabina-cargo-base.yml"


def _release_tree(tmp_path, version="1.0.18"):
    """A tmp copy of the five files the release rewrites, with traps set."""
    root = tmp_path / "repo"
    (root / "cargo" / "skarabina_cargo" / "genesis").mkdir(parents=True)
    (root / "doc").mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "skarabina"\n'
        f'version = "{version}"\n'
        'dependencies = ["casacure>=3.8.17"]\n'
    )
    (root / "cargo" / "pyproject.toml").write_text(
        '[project]\nname = "skarabina-cargo"\n'
        f'version = "{version}"\n'
    )
    (root / YAML_REL).write_text(
        "# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)\n"
        "vars:\n"
        "  skarabina-cargo:\n"
        "    images:\n"
        "      registry: ghcr.io/tmolteno\n"
        f"      version: {version}\n"
    )
    # A dependency block that shares our version number: only the two local
    # packages' entries may be rewritten.
    (root / "uv.lock").write_text(
        f"[[package]]\nname = \"casacure\"\nversion = \"{version}\"\n\n"
        f"[[package]]\nname = \"skarabina\"\nversion = \"{version}\"\n"
        'source = {{ editable = "." }}\n\n'
        f"[[package]]\nname = \"skarabina-cargo\"\nversion = \"{version}\"\n"
        'source = {{ editable = "cargo" }}\n'
    )
    (root / "doc" / "CHANGES.md").write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "### Added\n\n"
        "- something unreleased.\n\n"
        f"## [{tasks._bump_patch(version)}]\n\n"
        "### Fixed\n\n"
        "- the previous release.\n"
    )
    return root


def test_the_bump_rewrites_every_checklist_file_and_only_ours(tmp_path):
    root = _release_tree(tmp_path)
    changed = tasks._bump_version("1.1.0", "1.0.18", root)
    assert set(changed) == set(tasks.VERSION_FILES)

    assert tasks._read_version(root) == "1.1.0"
    cargo = (root / "cargo" / "pyproject.toml").read_text()
    assert 'version = "1.1.0"' in cargo
    # The dependency keeps its own (coincidentally equal) version.
    assert 'casacure>=3.8.17' in (root / "pyproject.toml").read_text()

    # The image tag carries no v prefix, exactly the tag docker metadata
    # will produce from v1.1.0.
    assert "version: 1.1.0" in (root / YAML_REL).read_text()

    lock = (root / "uv.lock").read_text()
    ours = lock.split("[[package]]")
    names = {}
    for block in ours[1:]:
        first = next(line for line in block.splitlines() if line.startswith("name = "))
        names[first] = block
    assert 'version = "1.1.0"' in names['name = "skarabina"']
    assert 'version = "1.1.0"' in names['name = "skarabina-cargo"']
    assert 'version = "1.0.18"' in names['name = "casacure"']


def test_the_bump_fails_loudly_when_a_file_does_not_carry_the_version(tmp_path):
    root = _release_tree(tmp_path)
    (root / "cargo" / "pyproject.toml").write_text(
        '[project]\nname = "skarabina-cargo"\nversion = "0.0.1"\n')
    changed = tasks._bump_version("1.1.0", "1.0.18", root)
    assert "cargo/pyproject.toml" not in changed
    assert len(changed) == len(tasks.VERSION_FILES) - 1


def test_the_changelog_stamp_moves_unreleased_under_the_new_heading(tmp_path):
    root = _release_tree(tmp_path)
    assert tasks._stamp_changelog("1.1.0", root)
    text = (root / "doc" / "CHANGES.md").read_text()
    # Newest first: a fresh, empty Unreleased sits above the new section,
    # which owns the entries that were unreleased, above the previous
    # release's own heading.
    order = [text.index(s) for s in (
        "## [Unreleased]", "## [1.1.0]", "- something unreleased.",
        "## [1.0.19]", "- the previous release.")]
    assert order == sorted(order)
    assert tasks._changelog_has("1.1.0", root)


def test_the_changelog_stamp_is_idempotent(tmp_path):
    root = _release_tree(tmp_path)
    tasks._stamp_changelog("1.1.0", root)
    once = (root / "doc" / "CHANGES.md").read_text()
    assert not tasks._stamp_changelog("1.1.0", root)
    assert (root / "doc" / "CHANGES.md").read_text() == once


def test_the_changelog_stamp_inserts_above_the_first_section_without_unreleased(tmp_path):
    root = _release_tree(tmp_path)
    text = (root / "doc" / "CHANGES.md").read_text()
    (root / "doc" / "CHANGES.md").write_text(
        text.replace("## [Unreleased]\n\n### Added\n\n- something unreleased.\n\n",
                     ""))
    assert tasks._stamp_changelog("1.1.0", root)
    text = (root / "doc" / "CHANGES.md").read_text()
    assert text.index("## [1.1.0]") < text.index("## [1.0.19]")


def test_bump_patch():
    assert tasks._bump_patch("1.0.18") == "1.0.19"
    assert tasks._bump_patch("2.9.9") == "2.9.10"
    with pytest.raises(ValueError):
        tasks._bump_patch("1.0")


def test_the_real_tree_is_consistent_with_the_checklist():
    """The repo's own files carry one version everywhere the checklist
    names -- the invariant the bump rewrites from and the release tags."""
    version = tasks._read_version()
    cargo = (tasks.REPO / "cargo" / "pyproject.toml").read_text()
    assert f'version = "{version}"' in cargo
    assert f"version: {version}" in (tasks.REPO / YAML_REL).read_text()
    assert tasks._changelog_has(version)
