"""Tests for the interactive package-upgrade CLI.

Exercises the gather-all-then-apply reconcile flow with synthetic old / new /
current trees, using the real core APIs (install, archive cache, classify,
apply) and an injected connection + fetcher.  Covers:

* a no-conflict upgrade applies non-interactively,
* a conflict with no tty fails nonzero and applies nothing,
* a conflict with scripted input gathers the decision, applies the resolved
  tree, and the install is updated.
"""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path
from textwrap import dedent

import pytest

from carpenter.packages import archive_cache
from carpenter.packages.installer import (
    compute_package_hash,
    ensure_installer_tables,
    get_install_record,
    install_package,
)
from carpenter_linux.packages.upgrade_cli import _cmd_upgrade


# ── fixtures / helpers ───────────────────────────────────────────────


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_installer_tables(conn)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _base_dir(tmp_path, monkeypatch):
    # Archive cache writes under config base_dir; isolate per test.
    from carpenter import config
    monkeypatch.setitem(config.CONFIG, "base_dir", str(tmp_path / "base"))


def _manifest(name: str, version: str) -> str:
    return dedent(f"""\
        name: {name}
        version: "{version}"
        description: fixture package.
    """)


def _make_src(root: Path, name: str, version: str, files: dict[str, str]) -> Path:
    pkg = root / f"src-{version}" / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.yaml").write_text(_manifest(name, version))
    for rel, content in files.items():
        t = pkg / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text(content)
    return pkg


def _install_old(db_conn, tmp_path, name, version, files):
    """Install an OLD version and cache its pristine archive."""
    src = _make_src(tmp_path, name, version, files)
    dest = tmp_path / "installed" / name
    install_package(src, dest, conn=db_conn)
    # Cache the pristine tree so load_pristine_tree hits locally (this is what
    # the install flow would do; we do it explicitly here).
    archive_cache.store_archive(name, version, src)
    return dest


def _no_fetch():
    class _F:
        def fetch(self, name, version):
            raise archive_cache.ArchiveCacheError("no network in test")
    return _F()


# ── no-conflict upgrade applies non-interactively ────────────────────


def test_no_conflict_upgrade_applies_non_interactively(db_conn, tmp_path):
    name = "demo"
    _install_old(db_conn, tmp_path, name, "1.0.0", {"a.txt": "alpha\n"})

    # New version changes a.txt; user has NOT modified it locally → clean
    # upstream-only adoption, no conflict.
    new_src = _make_src(tmp_path, name, "2.0.0", {"a.txt": "alpha v2\n"})

    out = io.StringIO()
    rc = _cmd_upgrade(
        [name, "--to", str(new_src)],
        out=out,
        interactive=False,
        fetcher=_no_fetch(),
        conn=db_conn,
    )
    assert rc == 0, out.getvalue()

    rec = get_install_record(db_conn, name)
    assert rec["version"] == "2.0.0"
    installed = Path(rec["install_path"])
    assert (installed / "a.txt").read_text() == "alpha v2\n"


def test_user_only_edit_kept_non_interactively(db_conn, tmp_path):
    name = "demo"
    dest = _install_old(db_conn, tmp_path, name, "1.0.0", {"a.txt": "alpha\n"})
    # User edits a.txt locally.
    (dest / "a.txt").write_text("alpha MINE\n")
    # New version leaves a.txt unchanged (== old) → USER_ONLY, kept, no conflict.
    new_src = _make_src(tmp_path, name, "2.0.0", {"a.txt": "alpha\n"})

    out = io.StringIO()
    rc = _cmd_upgrade(
        [name, "--to", str(new_src)],
        out=out, interactive=False, fetcher=_no_fetch(), conn=db_conn,
    )
    assert rc == 0, out.getvalue()
    rec = get_install_record(db_conn, name)
    assert rec["version"] == "2.0.0"
    assert (Path(rec["install_path"]) / "a.txt").read_text() == "alpha MINE\n"


# ── conflict, no tty → nonzero, nothing applied ──────────────────────


def test_conflict_no_tty_fails_and_applies_nothing(db_conn, tmp_path):
    name = "demo"
    dest = _install_old(db_conn, tmp_path, name, "1.0.0", {"a.txt": "alpha\n"})
    # User edited a.txt AND upstream changed it differently → CONFLICT.
    (dest / "a.txt").write_text("alpha MINE\n")
    new_src = _make_src(tmp_path, name, "2.0.0", {"a.txt": "alpha UPSTREAM\n"})

    out = io.StringIO()
    rc = _cmd_upgrade(
        [name, "--to", str(new_src)],
        out=out, interactive=False, fetcher=_no_fetch(), conn=db_conn,
    )
    assert rc == 1, out.getvalue()
    assert "no interactive terminal" in out.getvalue()

    # Nothing applied: version + on-disk content unchanged.
    rec = get_install_record(db_conn, name)
    assert rec["version"] == "1.0.0"
    assert (Path(rec["install_path"]) / "a.txt").read_text() == "alpha MINE\n"


# ── conflict, scripted input → gather then apply ─────────────────────


def _scripted_input(answers):
    it = iter(answers)

    def _inp(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError()
    return _inp


def test_conflict_take_new_applies_resolved_tree(db_conn, tmp_path):
    name = "demo"
    dest = _install_old(db_conn, tmp_path, name, "1.0.0", {"a.txt": "alpha\n"})
    (dest / "a.txt").write_text("alpha MINE\n")
    new_src = _make_src(tmp_path, name, "2.0.0", {"a.txt": "alpha UPSTREAM\n"})

    out = io.StringIO()
    rc = _cmd_upgrade(
        [name, "--to", str(new_src)],
        out=out,
        interactive=True,
        input_fn=_scripted_input(["n"]),  # take-new
        fetcher=_no_fetch(),
        conn=db_conn,
    )
    assert rc == 0, out.getvalue()
    rec = get_install_record(db_conn, name)
    assert rec["version"] == "2.0.0"
    assert (Path(rec["install_path"]) / "a.txt").read_text() == "alpha UPSTREAM\n"


def test_conflict_keep_current_applies_resolved_tree(db_conn, tmp_path):
    name = "demo"
    dest = _install_old(db_conn, tmp_path, name, "1.0.0", {"a.txt": "alpha\n"})
    (dest / "a.txt").write_text("alpha MINE\n")
    new_src = _make_src(tmp_path, name, "2.0.0", {"a.txt": "alpha UPSTREAM\n"})

    out = io.StringIO()
    rc = _cmd_upgrade(
        [name, "--to", str(new_src)],
        out=out,
        interactive=True,
        input_fn=_scripted_input(["k"]),  # keep-current
        fetcher=_no_fetch(),
        conn=db_conn,
    )
    assert rc == 0, out.getvalue()
    rec = get_install_record(db_conn, name)
    assert rec["version"] == "2.0.0"  # version bumps even keeping content
    assert (Path(rec["install_path"]) / "a.txt").read_text() == "alpha MINE\n"


def test_mixed_conflict_and_autoapply(db_conn, tmp_path):
    name = "demo"
    dest = _install_old(
        db_conn, tmp_path, name, "1.0.0",
        {"a.txt": "alpha\n", "b.txt": "beta\n", "c.txt": "gamma\n"},
    )
    # a.txt: conflict (user + upstream both edit)
    (dest / "a.txt").write_text("a MINE\n")
    # c.txt: user-only edit (upstream unchanged) → kept
    (dest / "c.txt").write_text("c MINE\n")
    new_src = _make_src(
        tmp_path, name, "2.0.0",
        {
            "a.txt": "a UPSTREAM\n",   # conflict
            "b.txt": "beta v2\n",       # upstream-only adopt
            "c.txt": "gamma\n",         # unchanged upstream
            "d.txt": "new file\n",      # added upstream
        },
    )

    out = io.StringIO()
    rc = _cmd_upgrade(
        [name, "--to", str(new_src)],
        out=out, interactive=True,
        input_fn=_scripted_input(["n"]),  # take new for the one conflict
        fetcher=_no_fetch(), conn=db_conn,
    )
    assert rc == 0, out.getvalue()
    installed = Path(get_install_record(db_conn, name)["install_path"])
    assert (installed / "a.txt").read_text() == "a UPSTREAM\n"  # took new
    assert (installed / "b.txt").read_text() == "beta v2\n"     # adopted
    assert (installed / "c.txt").read_text() == "c MINE\n"      # kept
    assert (installed / "d.txt").read_text() == "new file\n"    # added


# ── two-way fallback when shipped_old unrecoverable ──────────────────


def test_two_way_fallback_when_old_archive_missing(db_conn, tmp_path):
    name = "demo"
    src = _make_src(tmp_path, name, "1.0.0", {"a.txt": "alpha\n"})
    dest = tmp_path / "installed" / name
    install_package(src, dest, conn=db_conn)
    # install_package now (PR #45) auto-caches the pristine archive; to
    # simulate the "shipped_old unrecoverable" scenario (no cache, offline)
    # we delete that cached archive.  Combined with the raising fetcher
    # below, load_pristine_tree fails → two-way (current vs new) fallback.
    cached = archive_cache._archive_path(name, "1.0.0")
    cached.unlink(missing_ok=True)
    new_src = _make_src(tmp_path, name, "2.0.0", {"a.txt": "alpha v2\n"})

    out = io.StringIO()
    rc = _cmd_upgrade(
        [name, "--to", str(new_src)],
        out=out, interactive=True,
        input_fn=_scripted_input(["n"]),  # two-way: current vs new is a conflict
        fetcher=_no_fetch(), conn=db_conn,
    )
    assert rc == 0, out.getvalue()
    assert "TWO-WAY" in out.getvalue()
    rec = get_install_record(db_conn, name)
    assert rec["version"] == "2.0.0"
    assert (Path(rec["install_path"]) / "a.txt").read_text() == "alpha v2\n"


def test_not_installed_errors(db_conn, tmp_path):
    new_src = _make_src(tmp_path, "ghost", "2.0.0", {"a.txt": "x\n"})
    out = io.StringIO()
    rc = _cmd_upgrade(
        ["ghost", "--to", str(new_src)],
        out=out, interactive=False, fetcher=_no_fetch(), conn=db_conn,
    )
    assert rc == 1
    assert "not installed" in out.getvalue()
