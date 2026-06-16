"""Linux-platform package-management surfaces.

Concrete implementations of the package-reconciliation seams that
``carpenter-core`` leaves to the platform layer:

* :mod:`carpenter_linux.packages.github_fetcher` — a concrete
  :class:`carpenter.packages.archive_cache.ArchiveFetcher` that resolves
  and downloads per-version package archives published as GitHub Release
  assets.
* :mod:`carpenter_linux.packages.upgrade_cli` — the interactive
  ``python3 -m carpenter_linux packages upgrade`` command that drives the
  three-way reconcile flow (gather all resolutions, then apply atomically).
"""
