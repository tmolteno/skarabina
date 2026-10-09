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


# ---------------------------------------------------------------------------
# the release task itself, against a scratch git clone with a bare origin
# ---------------------------------------------------------------------------

def _scratch_repo(tmp_path, version="1.0.19"):
    """A git repo shaped like the real one, with a bare origin pushed."""
    import subprocess

    root = _release_tree(tmp_path, version=version)
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "release@example.com")
    git("config", "user.name", "Release Test")
    git("add", "-A")
    git("commit", "-q", "-m", "the state the operator committed")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    git("remote", "add", "origin", str(origin))
    git("push", "-q", "origin", "main")
    return root, origin


@pytest.fixture
def release_env(tmp_path, monkeypatch):
    """The release task pointed at a scratch repo, gate and waits stubbed."""
    import subprocess

    root, origin = _scratch_repo(tmp_path)
    waited = []
    monkeypatch.setattr(tasks, "REPO", root)
    monkeypatch.setattr(tasks, "test", lambda c: print("=== gate stubbed"))
    monkeypatch.setattr(tasks, "_wait_for_ci",
                        lambda ref: waited.append(("ci", ref)))
    monkeypatch.setattr(tasks, "_check_pypi",
                        lambda v: waited.append(("pypi", v)))

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    from invoke import Context

    def run(**kwargs):
        body = getattr(tasks.release, "body", tasks.release)
        body(Context(), **kwargs)

    return root, git, run, waited


def test_a_dirty_tree_is_refused_before_anything_runs(release_env):
    """The 1.0.19 incident: the bump commit landed on a half-finished tree
    and only then did the release notice.  Now it must refuse first, and
    leave the tree exactly as it was."""
    root, git, run, waited = release_env
    # Uncommitted work, exactly the shape of the incident: untracked new
    # code plus a modified tracked file.
    (root / "new_module.py").write_text("work in progress\n")
    changelog = (root / "doc" / "CHANGES.md").read_text()
    (root / "doc" / "CHANGES.md").write_text(changelog + "\n- half an entry.\n")

    with pytest.raises(RuntimeError, match="tree is dirty"):
        run(bump=True)

    # Nothing happened: no bump commit, no tag, no rewrite, no gate.
    assert git("log", "--format=%s", "-2").splitlines() == [
        "the state the operator committed"]
    assert git("tag") == ""
    assert tasks._read_version(root) == "1.0.19"
    assert "half an entry" in (root / "doc" / "CHANGES.md").read_text()
    assert waited == []


def test_a_clean_tree_runs_the_whole_chain(release_env):
    root, git, run, waited = release_env
    run(bump=True)

    assert tasks._read_version(root) == "1.0.20"
    assert git("log", "--format=%s", "-1") == "chore(release): 1.0.20"
    assert git("tag") == "v1.0.20"
    # The tag is on origin and names HEAD, annotated with the release
    # message; the waits ran for it.
    origin_tags = git("ls-remote", "--tags", "origin").splitlines()
    assert any("refs/tags/v1.0.20" in line for line in origin_tags)
    assert git("rev-parse", "v1.0.20^{commit}") == git("rev-parse", "HEAD")
    assert git("for-each-ref", "refs/tags/v1.0.20",
               "--format=%(contents:subject)") == "skarabina 1.0.20"
    assert waited == [("ci", "v1.0.20"), ("pypi", "1.0.20")]


def test_a_repositioned_local_tag_is_refused(release_env):
    """A local tag origin never saw, at a commit that is not HEAD: a
    leftover from something else -- refuse and say what to inspect."""
    root, git, run, waited = release_env
    # The release tags the version's tag (v1.0.19 here); park it at the
    # initial commit while HEAD moves on.
    git("commit", "-q", "--allow-empty", "-m", "moved on")
    git("tag", "-a", "v1.0.19", "-m", "leftover", "HEAD~1")
    with pytest.raises(RuntimeError, match="leftover"):
        run(bump=False)
    assert waited == []
