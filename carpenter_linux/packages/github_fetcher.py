"""Concrete :class:`~carpenter.packages.archive_cache.ArchiveFetcher` over
GitHub Releases.

The reconcile system in ``carpenter-core`` reconstructs a package version's
pristine tree from a deterministic ``.tar.gz`` archive, then **verifies it
against the locally-recorded root hash** before trusting a single byte (see
:func:`carpenter.packages.archive_cache.load_pristine_tree`).  The archive's
origin is therefore *untrusted storage*; this fetcher just resolves and
downloads the blob.

Publishing convention (see
``~/notes/carpenter-package-archive-publishing-plan.md``)
------------------------------------------------------------------------
For a package ``<name>`` at version ``<version>``:

* the archive is published as a GitHub **Release** whose tag is
  ``<name>-v<version>``,
* carrying an **asset** named ``<name>-<version>.tar.gz`` whose bytes are the
  package's composed, deterministically-archived install tree (byte-identical
  to what installs + what the root hash measures).

The fetcher computes both from ``(name, version)`` alone, so no extra lookup
state is needed.  The companion *publisher* (a GitHub Actions workflow in
``carpenter-packages``) is built separately; until it exists no releases are
present and ``fetch`` raises :class:`PackageReleaseNotFound`, which the
upgrade CLI treats as "fall back to two-way" — it never blocks an upgrade.

Auth
----
``carpenter-packages`` is **public**, so anonymous download works.  A
read-only GitHub token is used when available (``CARPENTER_GITHUB_TOKEN`` or
``GITHUB_TOKEN`` in the environment) purely to relax the anonymous API
rate-limit and to keep working if the repo is later made private.  The token
is a *platform* secret (never sourced from any package).

Robustness
----------
Every network failure is surfaced as :class:`ArchiveFetchError` (or its
``PackageReleaseNotFound`` subclass) so the caller can cleanly degrade to the
two-way reconcile path rather than crash.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "ArchiveFetchError",
    "PackageReleaseNotFound",
    "GitHubReleaseArchiveFetcher",
    "release_tag",
    "asset_name",
]

# The public repository that publishes per-version package archives.
DEFAULT_REPO = "carpenter-ai/carpenter-packages"
_API_BASE = "https://api.github.com"
# Bound every network call so a hung connection degrades to two-way rather
# than blocking the upgrade indefinitely.
_DEFAULT_TIMEOUT = 30.0


class ArchiveFetchError(Exception):
    """A package archive could not be fetched from GitHub Releases.

    Raised for any failure (network error, HTTP error, missing asset).  The
    upgrade CLI catches this and degrades to a two-way reconcile, so the
    fetcher must never let a low-level exception escape unwrapped.
    """


class PackageReleaseNotFound(ArchiveFetchError):
    """No release/asset exists for the requested ``name@version``.

    A distinct subclass so callers can tell "nothing published yet" (the
    expected state until the publisher ships) from a transient network
    failure — both still degrade to two-way, but only the latter is worth
    retrying.
    """


# ── naming convention (must match the publisher) ─────────────────────


def release_tag(name: str, version: str) -> str:
    """Release tag for a package version: ``<name>-v<version>``."""
    return f"{name}-v{version}"


def asset_name(name: str, version: str) -> str:
    """Release asset filename for a package version: ``<name>-<version>.tar.gz``."""
    return f"{name}-{version}.tar.gz"


class GitHubReleaseArchiveFetcher:
    """Fetch package archives from GitHub Release assets.

    Implements the :class:`carpenter.packages.archive_cache.ArchiveFetcher`
    Protocol (a ``fetch(name, version) -> Path`` method).

    Args:
        repo: ``owner/name`` of the publishing repo (default
            :data:`DEFAULT_REPO`).
        token: Optional read-only GitHub token.  Defaults to
            ``CARPENTER_GITHUB_TOKEN`` / ``GITHUB_TOKEN`` from the
            environment (the platform secret store); ``None`` → anonymous.
        timeout: Per-request timeout in seconds.
        opener: Injection seam for tests — a callable
            ``(request, timeout) -> file-like`` defaulting to
            :func:`urllib.request.urlopen`.  Tests stub this to avoid real
            HTTP.
    """

    def __init__(
        self,
        repo: str = DEFAULT_REPO,
        *,
        token: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        opener=None,
    ) -> None:
        self.repo = repo
        self.token = token if token is not None else _token_from_env()
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    # ── public API ───────────────────────────────────────────────────

    def fetch(self, name: str, version: str) -> Path:
        """Download the archive for ``name@version`` to a temp file.

        Resolves the release by tag ``<name>-v<version>``, finds the asset
        ``<name>-<version>.tar.gz``, downloads it to a temp path (honouring
        ``TMPDIR``, e.g. ``/dev/shm``), and returns that path.  The caller
        (:func:`carpenter.packages.archive_cache.load_pristine_tree`) then
        verifies the expanded tree against the expected root hash before
        trusting it.

        Raises:
            PackageReleaseNotFound: the release or asset does not exist.
            ArchiveFetchError: any network/HTTP failure.
        """
        download_url = self._resolve_asset_url(name, version)
        return self._download(download_url, name, version)

    # ── resolution ───────────────────────────────────────────────────

    def _resolve_asset_url(self, name: str, version: str) -> str:
        """Resolve the browser/asset download URL for the version's archive."""
        tag = release_tag(name, version)
        want_asset = asset_name(name, version)
        url = f"{_API_BASE}/repos/{self.repo}/releases/tags/{tag}"
        try:
            payload = self._get_json(url)
        except PackageReleaseNotFound:
            # Re-raise with a package-oriented message.
            raise PackageReleaseNotFound(
                f"no GitHub release tagged {tag!r} in {self.repo} "
                f"(for {name}@{version})",
            ) from None

        assets = payload.get("assets") or []
        for asset in assets:
            if asset.get("name") == want_asset:
                dl = asset.get("browser_download_url") or asset.get("url")
                if not dl:
                    raise ArchiveFetchError(
                        f"release {tag!r} asset {want_asset!r} has no "
                        f"download URL",
                    )
                return dl

        available = ", ".join(a.get("name", "?") for a in assets) or "(none)"
        raise PackageReleaseNotFound(
            f"release {tag!r} has no asset named {want_asset!r} "
            f"(available: {available})",
        )

    # ── HTTP helpers ─────────────────────────────────────────────────

    def _headers(self, *, accept: str) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": "carpenter-linux-archive-fetcher",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _get_json(self, url: str) -> dict:
        """GET a JSON document, mapping failures to ArchiveFetchError."""
        req = urllib.request.Request(
            url, headers=self._headers(accept="application/vnd.github+json"),
        )
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise PackageReleaseNotFound(
                    f"GitHub returned 404 for {url}",
                ) from exc
            raise ArchiveFetchError(
                f"GitHub returned HTTP {exc.code} for {url}: {exc.reason}",
            ) from exc
        except urllib.error.URLError as exc:
            raise ArchiveFetchError(
                f"network error contacting GitHub at {url}: {exc.reason}",
            ) from exc
        except (OSError, TimeoutError) as exc:  # pragma: no cover - defensive
            raise ArchiveFetchError(
                f"I/O error contacting GitHub at {url}: {exc}",
            ) from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ArchiveFetchError(
                f"GitHub returned non-JSON for {url}: {exc}",
            ) from exc

    def _download(self, url: str, name: str, version: str) -> Path:
        """Stream ``url`` to a temp ``.tar.gz`` file and return its path."""
        # The release asset download endpoint wants octet-stream when hit via
        # the API ``assets/<id>`` URL; browser_download_url ignores Accept.
        req = urllib.request.Request(
            url, headers=self._headers(accept="application/octet-stream"),
        )
        # mkstemp honours $TMPDIR (the repo sets TMPDIR=/dev/shm) so the
        # download lands off the SD card.
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{name}-{version}-", suffix=".tar.gz",
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                data = resp.read()
            tmp_path.write_bytes(data)
        except urllib.error.HTTPError as exc:
            tmp_path.unlink(missing_ok=True)
            if exc.code == 404:
                raise PackageReleaseNotFound(
                    f"asset download 404 for {name}@{version} at {url}",
                ) from exc
            raise ArchiveFetchError(
                f"asset download HTTP {exc.code} for {name}@{version}: "
                f"{exc.reason}",
            ) from exc
        except urllib.error.URLError as exc:
            tmp_path.unlink(missing_ok=True)
            raise ArchiveFetchError(
                f"network error downloading {name}@{version} archive: "
                f"{exc.reason}",
            ) from exc
        except (OSError, TimeoutError) as exc:  # pragma: no cover - defensive
            tmp_path.unlink(missing_ok=True)
            raise ArchiveFetchError(
                f"I/O error downloading {name}@{version} archive: {exc}",
            ) from exc
        logger.info(
            "Fetched archive for %s@%s from %s (%d bytes)",
            name, version, url, tmp_path.stat().st_size,
        )
        return tmp_path


def _token_from_env() -> str | None:
    """Read a read-only GitHub token from the platform environment.

    Prefers ``CARPENTER_GITHUB_TOKEN`` (carpenter-specific) over the generic
    ``GITHUB_TOKEN``.  Returns ``None`` when neither is set — anonymous
    download is fine for the public repo.
    """
    for key in ("CARPENTER_GITHUB_TOKEN", "GITHUB_TOKEN"):
        val = os.environ.get(key)
        if val and val.strip():
            return val.strip()
    return None
