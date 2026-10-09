# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Release orchestration for skarabina.

Following the example of ../casacure's tasks.py: ``invoke release`` runs the
test gate locally before any tag is pushed to GitHub, so a CI failure on a
release tag becomes a local failure the operator sees first -- and it writes
the release commit for you, so the version bump checklist in AGENTS.md is a
command rather than a list of files to edit by hand.

Usage (from the repo root, in the project venv):

    invoke version                      # show the current + next versions
    invoke test                         # pytest + flake8 (the release gate)
    invoke release                      # bump patch, stamp CHANGES, tag + push
    invoke release --no-bump            # tag the current version as-is
    invoke release --version 1.0.19     # explicit version (implies --no-bump)

``invoke release`` bumps the version in every place AGENTS.md's checklist
names -- ``pyproject.toml``, ``cargo/pyproject.toml``,
``cargo/skarabina_cargo/genesis/skarabina-cargo-base.yml`` (the image tag,
no ``v`` prefix) and the ``skarabina``/``skarabina-cargo`` entries of
``uv.lock`` -- and moves ``doc/CHANGES.md``'s ``[Unreleased]`` entries under
a new ``## [X.Y.Z]`` heading (a fresh, empty ``[Unreleased]`` is left on
top), committed as ``chore(release): X.Y.Z``.  It then pushes ``main`` and
the annotated tag ``vX.Y.Z`` (message ``skarabina X.Y.Z``), which triggers
the tag's three workflows:

  * deploy_module.yaml  -- "Publish skarabina package"   (PyPI skarabina)
  * cargo-publish.yml   -- "Publish skarabina-cargo"     (PyPI skarabina-cargo)
  * docker-publish.yml  -- "Docker"                      (ghcr.io image)

The release waits for all three runs of the tag to go green and then
confirms both packages are live on PyPI (pypi.org/pypi/<name>/json).

Requires: invoke, plumbum, gh (authenticated).  The test gate runs through
``uv run --frozen`` in the repo's venv, so the lockfile is never rewritten
behind your back mid-release.
"""
import json
import re
import time
import urllib.request
from pathlib import Path

from invoke import task
from plumbum import FG, local

REPO = Path(__file__).parent
REPO_SLUG = "tmolteno/skarabina"

#: The files AGENTS.md's version bump checklist names, and the workflows a
#: tag push triggers (workflow *file* names; ``gh run list --workflow`` takes
#: one, so each is polled on its own).
VERSION_FILES = (
    "pyproject.toml",
    "cargo/pyproject.toml",
    "cargo/skarabina_cargo/genesis/skarabina-cargo-base.yml",
    "uv.lock",
)
WORKFLOWS = ("deploy_module.yaml", "cargo-publish.yml", "docker-publish.yml")


# ---------------------------------------------------------------------------
# version + git helpers
# ---------------------------------------------------------------------------

def _read_version(root: Path = REPO) -> str:
    text = (root / "pyproject.toml").read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else "?"


def _bump_patch(version: str) -> str:
    major, minor, patch = version.split(".")
    return f"{major}.{minor}.{int(patch) + 1}"


def _bump_version(new: str, old: str, root: Path = REPO) -> list[str]:
    """Rewrite the version in every file of the release checklist.

    The TOMLs carry ``version = "X.Y.Z"``; the cab's base YAML carries the
    image tag as ``version: X.Y.Z`` (docker/metadata-action strips the ``v``
    from the git tag, so the value must match the tag exactly); ``uv.lock``
    carries it in the ``skarabina`` and ``skarabina-cargo`` package entries,
    exactly as ``uv lock`` would rewrite them.  Returns the changed paths,
    relative to ``root`` (the repo, or a test's copy of it).
    """
    changed = []

    for rel in ("pyproject.toml", "cargo/pyproject.toml"):
        path = root / rel
        text = path.read_text()
        # count=1: only the project's own version, not a dependency's.
        bumped = re.sub(rf'^version\s*=\s*"{re.escape(old)}"',
                        f'version = "{new}"', text, count=1, flags=re.MULTILINE)
        if bumped != text:
            path.write_text(bumped)
            changed.append(rel)

    yaml_rel = "cargo/skarabina_cargo/genesis/skarabina-cargo-base.yml"
    path = root / yaml_rel
    text = path.read_text()
    bumped = re.sub(rf"^(\s+version: ){re.escape(old)}\s*$",
                    rf"\g<1>{new}", text, count=1, flags=re.MULTILINE)
    if bumped != text:
        path.write_text(bumped)
        changed.append(yaml_rel)

    changed.extend(_bump_uv_lock(new, old, root))
    return changed


def _bump_uv_lock(new: str, old: str, root: Path = REPO) -> list[str]:
    """Bump the two local packages' entries in uv.lock.

    The lock holds one ``[[package]]`` block per package; only the blocks
    named ``skarabina`` and ``skarabina-cargo`` carry our version (an
    editable path dependency records its version as metadata), so the edit
    is scoped block by block rather than by a global replace that could
    touch a dependency that happens to share the number.
    """
    path = root / "uv.lock"
    blocks = path.read_text().split("[[package]]")
    names = ("skarabina", "skarabina-cargo")
    for i, block in enumerate(blocks):
        m = re.search(r'^name = "([^"]+)"', block, re.MULTILINE)
        if m and m.group(1) in names:
            blocks[i] = re.sub(rf'^version = "{re.escape(old)}"',
                               f'version = "{new}"', block, count=1,
                               flags=re.MULTILINE)
    path.write_text("[[package]]".join(blocks))
    return ["uv.lock"]


def _changelog_has(version: str, root: Path = REPO) -> bool:
    """True when doc/CHANGES.md already has a ``## [version]`` section."""
    text = (root / "doc" / "CHANGES.md").read_text()
    return bool(re.search(rf"^## \[{re.escape(version)}\]", text, re.MULTILINE))


def _stamp_changelog(new: str, root: Path = REPO) -> bool:
    """Move the ``[Unreleased]`` entries under ``## [new]``.

    doc/CHANGES.md is written newest-first and its release headings carry no
    date, so a release turns the entries accumulated under
    ``## [Unreleased]`` into the released version's own section and leaves a
    fresh, empty ``## [Unreleased]`` on top of it (AGENTS.md's checklist).

    Idempotent and respectful of hand-written notes: if a ``## [new]``
    section is already present (written by hand, or a re-run of the
    release), the file is left untouched and ``False`` is returned; if there
    is no ``## [Unreleased]`` section at all, the section is inserted above
    the first existing one instead.
    """
    path = root / "doc" / "CHANGES.md"
    text = path.read_text()
    if re.search(rf"^## \[{re.escape(new)}\]", text, re.MULTILINE):
        return False
    heading = f"## [{new}]\n"
    unreleased = re.search(r"^## \[Unreleased\][ \t]*\n", text, re.MULTILINE)
    if unreleased:
        text = text[:unreleased.end()] + f"\n{heading}" + text[unreleased.end():]
    else:
        first = re.search(r"^## \[", text, re.MULTILINE)
        if not first:
            return False
        text = text[:first.start()] + f"{heading}\n" + text[first.start():]
    path.write_text(text)
    return True


def _git(*args: str) -> str:
    return local["git"]["-C", str(REPO), *args]()


def _tag_exists(tag: str) -> bool:
    out = _git("ls-remote", "--tags", "origin", f"refs/tags/{tag}")
    return bool(out.strip())


# ---------------------------------------------------------------------------
# CI + PyPI
# ---------------------------------------------------------------------------

def _wait_for_ci(ref: str, timeout_s: int = 3600) -> None:
    """Wait for the tag's three workflow runs to go green.

    Each workflow is polled on its own: ``gh run list``'s ``--workflow`` is
    a single-value flag, so passing it twice silently keeps only the last
    one.  Runs are matched by ``headBranch``, which for a tag push is the
    tag itself; a bare ``--limit 1`` would instead see whatever ran last,
    often the previous tag's still-green run.
    """
    start = time.time()
    run_ids: dict[str, int] = {}
    while len(run_ids) < len(WORKFLOWS):
        for wf in WORKFLOWS:
            listing = local["gh"][
                "run", "list", "-R", REPO_SLUG, "--workflow", wf,
                "--limit", "10", "--json", "databaseId,headBranch,workflowName"]
            runs = json.loads(listing())
            for run in runs:
                if run["headBranch"] == ref:
                    run_ids.setdefault(run["workflowName"], run["databaseId"])
        if len(run_ids) >= len(WORKFLOWS):
            break
        if time.time() - start > 300:
            seen = ", ".join(sorted(run_ids)) or "none"
            raise RuntimeError(
                f"only these workflow runs appeared for {ref} in 300 s:"
                f" {seen} (expected {', '.join(WORKFLOWS)})")
        print("  waiting for the workflow runs of", ref, "to register...")
        time.sleep(20)

    for name, run_id in run_ids.items():
        print(f"  {name} (run {run_id}):")
        while True:
            viewing = local["gh"][
                "run", "view", str(run_id), "-R", REPO_SLUG,
                "--json", "status,conclusion",
                "--jq", '"\\(.status) \\(.conclusion)"']
            line = viewing().strip()
            status, _, conclusion = line.partition(" ")
            if conclusion == "success":
                print(f"    {name}: {line}")
                break
            # Any other completed state (failure, cancelled, timed_out,
            # skipped) is a failed release: do not spin until the timeout.
            if status == "completed":
                raise RuntimeError(
                    f"{name} did not succeed ({line or conclusion}); see"
                    f" gh run view {run_id} -R {REPO_SLUG} --log-failed")
            print(f"    {name}: {line}")
            time.sleep(60)
            if time.time() - start > timeout_s:
                raise RuntimeError(f"{name} CI timed out after {timeout_s}s")


def _check_pypi(version: str, packages=("skarabina", "skarabina-cargo"),
                timeout_s: int = 600) -> None:
    """Confirm the release is visible on PyPI for both packages.

    The publish workflows can be green while an upload raced a mirror; this
    asks pypi.org's JSON API directly (the URL AGENTS.md's checklist names)
    and waits out the propagation window rather than trusting the tag alone.
    """
    start = time.time()
    pending = set(packages)
    while pending:
        for name in sorted(pending):
            url = f"https://pypi.org/pypi/{name}/json"
            try:
                with urllib.request.urlopen(url, timeout=30) as response:
                    releases = json.load(response)["releases"]
                if version in releases and releases[version]:
                    print(f"  {name} {version} is live on PyPI")
                    pending.discard(name)
            except (OSError, ValueError, KeyError):
                pass  # not yet propagated; poll again
        if not pending:
            return
        if time.time() - start > timeout_s:
            raise RuntimeError(
                f"PyPI does not list {version} for {', '.join(sorted(pending))}"
                f" after {timeout_s}s (check {url})")
        print("  waiting for PyPI to list", ", ".join(sorted(pending)), "...")
        time.sleep(30)


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------

@task
def version(c) -> None:
    """Show the versions the release would tag and what it would rewrite."""
    current = _read_version()
    nxt = _bump_patch(current)
    print(f"skarabina: pyproject {current}  ->  would bump to {nxt} and tag v{nxt}")
    print(f"           rewriting: {', '.join(VERSION_FILES)}")
    print(f"           doc/CHANGES.md: [Unreleased] -> [{nxt}]")
    print(f"           commit 'chore(release): {nxt}', tag v{nxt} 'skarabina {nxt}'")
    print(f"           (or tag v{current} as-is with --no-bump;"
          f" --version X.Y.Z tags that exact version)")


@task
def test(c) -> None:
    """Run the pre-release gate: the test suite and flake8, via uv.

    ``--frozen`` so a release never rewrites uv.lock as a side effect: the
    lockfile is part of what the release commits.
    """
    with local.cwd(str(REPO)):
        runner = local["uv"]["run", "--frozen", "python"]
        runner["-m", "pytest", "tests/", "-q"] & FG
        runner["-m", "flake8", "skarabina/", "tasks.py"] & FG
    print("=== test gate PASS (pytest + flake8)")


@task(pre=[test])
def release(c, version: str | None = None, bump: bool = True) -> None:
    """Run the full skarabina release chain.

    1. run the test gate (this task's ``pre=[test]``).
    2. bump the patch version in every file of AGENTS.md's checklist, stamp
       doc/CHANGES.md, and commit as ``chore(release): X.Y.Z`` (the default;
       skip with --no-bump, or override with --version X.Y.Z which implies
       --no-bump since the tag is given explicitly).
    3. tag ``vX.Y.Z`` (annotated, message ``skarabina X.Y.Z``) and push
       ``main`` and the tag, which triggers the PyPI, PyPI-cargo and Docker
       workflows.
    4. wait for those three workflow runs of the tag to go green, and
       confirm both packages are live on PyPI.

    Idempotent: a tag already on origin is verified and skipped, not
    re-pushed.
    """
    # An explicit --version means "tag exactly this"; the tree's version must
    # not be touched or the tag and tree would drift apart.
    if version is not None:
        bump = False

    if bump:
        old = _read_version()
        new = _bump_patch(old)
        print(f"=== bumping version to {new}")
        changed = _bump_version(new, old)
        missing = [rel for rel in VERSION_FILES if rel not in changed]
        if missing:
            raise RuntimeError(
                f"could not rewrite the version in {', '.join(missing)}"
                f" (expected {old} there); fix by hand and re-run"
                f" with --no-bump")
        if _stamp_changelog(new):
            print(f"=== doc/CHANGES.md: [Unreleased] -> [{new}]")
        _git("add", *VERSION_FILES, "doc/CHANGES.md")
        _git("commit", "-m", f"chore(release): {new}")

    v = version or _read_version()
    tag = f"v{v}"
    if not _changelog_has(v):
        print(f"    warning: doc/CHANGES.md has no '## [{v}]' section, so the"
              f" tag will be published without release notes (add one, or"
              f" let the default bump stamp it)")

    # After the bump commit: anything still dirty is not this release's to
    # carry, and the workflows would build a tree nobody has seen.
    status = _git("status", "--porcelain").strip()
    if status:
        raise RuntimeError(
            "tree is dirty; commit the release (the version files and"
            " doc/CHANGES.md) first\n" + status)

    commit = _git("rev-parse", "HEAD").strip()
    if _tag_exists(tag):
        print(f"=== {tag} already on origin -- verified, skipping")
        return

    if _git("tag", "-l", tag):
        # A local tag origin never saw: a previous attempt whose push (or
        # wait) failed.  At this commit it is the release, so push it; at
        # any other it is a leftover nobody has published, and quietly
        # replacing it could hide that.  (^{commit}: an annotated tag's own
        # rev-parse is the tag object, not the commit it names.)
        local_sha = _git("rev-parse", f"{tag}^{{commit}}").strip()
        if local_sha != commit:
            raise RuntimeError(
                f"{tag} exists locally at {local_sha[:12]} but the release"
                f" is at {commit[:12]} and origin has neither; inspect"
                f" 'git show {tag}' and delete it if it is a leftover")
        print(f"=== {tag} already tags this commit; pushing it")
    else:
        print(f"=== tagging {tag} (commit {commit[:12]})")
        _git("tag", "-a", tag, "-m", f"skarabina {v}")
    local["git"]["-C", str(REPO), "push", "origin", "main", tag] & FG

    print("  waiting for the publish workflows...")
    _wait_for_ci(tag)
    _check_pypi(v)
    print(f"\n=== RELEASE COMPLETE: skarabina {tag}"
          f" (PyPI skarabina + skarabina-cargo {v}, Docker image {v})")
