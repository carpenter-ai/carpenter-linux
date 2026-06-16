"""Tests for the GitHub-Release archive fetcher.

The fetcher is exercised with a stubbed HTTP opener (no real network) and
checked end-to-end through ``archive_cache.load_pristine_tree``: on a cache
miss the fetcher downloads, the core layer verifies the expanded tree against
the expected root hash, and a hash mismatch is rejected.
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from textwrap import dedent

import pytest

from carpenter.packages import archive_cache
from carpenter.packages.archive_cache import (
    ArchiveVerificationError,
    archive_tree,
)
from carpenter_linux.packages.github_fetcher import (
    ArchiveFetchError,
    GitHubReleaseArchiveFetcher,
    PackageReleaseNotFound,
    asset_name,
    release_tag,
)


# ── helpers ──────────────────────────────────────────────────────────


def _make_pkg(root: Path, name: str, version: str, files: dict[str, str]) -> Path:
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.yaml").write_text(
        dedent(f"""\
            name: {name}
            version: "{version}"
            description: fixture package.
        """)
    )
    for rel, content in files.items():
        target = pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return pkg


class _Resp:
    """Minimal context-manager file-like wrapping fixed bytes."""

    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._data


def _make_opener(release_json: dict, asset_bytes: bytes, asset_url: str):
    """Build a stub opener that serves the release JSON then the asset bytes."""

    def opener(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        if url.endswith(f"/releases/tags/{release_json['__tag']}"):
            body = {k: v for k, v in release_json.items() if k != "__tag"}
            return _Resp(json.dumps(body).encode("utf-8"))
        if url == asset_url:
            return _Resp(asset_bytes)
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    return opener


# ── direct fetch ─────────────────────────────────────────────────────


def test_fetch_resolves_tag_and_asset_then_downloads(tmp_path):
    pkg = _make_pkg(tmp_path, "demo", "1.0.0", {"a.txt": "hello\n"})
    archive_path = tmp_path / "demo-1.0.0.tar.gz"
    archive_tree(pkg, archive_path)
    asset_bytes = archive_path.read_bytes()

    tag = release_tag("demo", "1.0.0")
    aname = asset_name("demo", "1.0.0")
    asset_url = "https://example.test/download/demo-1.0.0.tar.gz"
    release_json = {
        "__tag": tag,
        "tag_name": tag,
        "assets": [{"name": aname, "browser_download_url": asset_url}],
    }
    opener = _make_opener(release_json, asset_bytes, asset_url)

    fetcher = GitHubReleaseArchiveFetcher(repo="o/r", opener=opener)
    out = fetcher.fetch("demo", "1.0.0")
    try:
        assert out.is_file()
        assert out.read_bytes() == asset_bytes
    finally:
        out.unlink(missing_ok=True)


def test_fetch_missing_release_raises_not_found(tmp_path):
    def opener(req, timeout=None):
        url = req.full_url
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    fetcher = GitHubReleaseArchiveFetcher(repo="o/r", opener=opener)
    with pytest.raises(PackageReleaseNotFound):
        fetcher.fetch("demo", "9.9.9")


def test_fetch_release_without_asset_raises_not_found():
    tag = release_tag("demo", "1.0.0")
    release_json = {
        "__tag": tag,
        "tag_name": tag,
        "assets": [{"name": "something-else.tar.gz",
                    "browser_download_url": "x"}],
    }
    opener = _make_opener(release_json, b"", "x")
    fetcher = GitHubReleaseArchiveFetcher(repo="o/r", opener=opener)
    with pytest.raises(PackageReleaseNotFound):
        fetcher.fetch("demo", "1.0.0")


def test_fetch_network_error_raises_archive_fetch_error():
    def opener(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    fetcher = GitHubReleaseArchiveFetcher(repo="o/r", opener=opener)
    with pytest.raises(ArchiveFetchError) as ei:
        fetcher.fetch("demo", "1.0.0")
    assert not isinstance(ei.value, PackageReleaseNotFound)


def test_token_from_env(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("CARPENTER_GITHUB_TOKEN", "tok-abc")
    fetcher = GitHubReleaseArchiveFetcher(repo="o/r", opener=lambda *a, **k: None)
    assert fetcher.token == "tok-abc"
    headers = fetcher._headers(accept="application/json")
    assert headers["Authorization"] == "Bearer tok-abc"


# ── integration with load_pristine_tree ──────────────────────────────


def test_load_pristine_tree_fetches_and_verifies(tmp_path, monkeypatch):
    # Point the archive cache at a temp base_dir so the cache miss is real.
    from carpenter import config
    monkeypatch.setitem(config.CONFIG, "base_dir", str(tmp_path / "base"))

    pkg = _make_pkg(tmp_path, "demo", "2.0.0", {"x.py": "print('hi')\n"})
    archive_path = tmp_path / "src.tar.gz"
    root_hash = archive_tree(pkg, archive_path)
    asset_bytes = archive_path.read_bytes()

    tag = release_tag("demo", "2.0.0")
    aname = asset_name("demo", "2.0.0")
    asset_url = "https://example.test/d/demo-2.0.0.tar.gz"
    release_json = {
        "__tag": tag,
        "tag_name": tag,
        "assets": [{"name": aname, "browser_download_url": asset_url}],
    }
    opener = _make_opener(release_json, asset_bytes, asset_url)
    fetcher = GitHubReleaseArchiveFetcher(repo="o/r", opener=opener)

    tree = archive_cache.load_pristine_tree(
        "demo", "2.0.0", root_hash, fetcher=fetcher,
    )
    assert tree["x.py"] == b"print('hi')\n"
    assert tree["manifest.yaml"]

    # And the fetched archive was cached: a second load (no fetcher) hits it.
    again = archive_cache.load_pristine_tree("demo", "2.0.0", root_hash)
    assert again == tree


def test_load_pristine_tree_hash_mismatch_rejected(tmp_path, monkeypatch):
    from carpenter import config
    monkeypatch.setitem(config.CONFIG, "base_dir", str(tmp_path / "base"))

    pkg = _make_pkg(tmp_path, "demo", "2.0.0", {"x.py": "print('hi')\n"})
    archive_path = tmp_path / "src.tar.gz"
    archive_tree(pkg, archive_path)
    asset_bytes = archive_path.read_bytes()

    tag = release_tag("demo", "2.0.0")
    aname = asset_name("demo", "2.0.0")
    asset_url = "https://example.test/d/demo-2.0.0.tar.gz"
    release_json = {
        "__tag": tag, "tag_name": tag,
        "assets": [{"name": aname, "browser_download_url": asset_url}],
    }
    opener = _make_opener(release_json, asset_bytes, asset_url)
    fetcher = GitHubReleaseArchiveFetcher(repo="o/r", opener=opener)

    wrong_hash = "0" * 64
    with pytest.raises(ArchiveVerificationError):
        archive_cache.load_pristine_tree(
            "demo", "2.0.0", wrong_hash, fetcher=fetcher,
        )
