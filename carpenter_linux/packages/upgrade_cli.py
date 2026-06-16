"""Interactive package-upgrade CLI (the user-facing reconcile surface).

``python3 -m carpenter_linux packages upgrade <name> --to <version|source>``

Drives the three-way reconciliation that ``carpenter-core`` provides as pure
machinery (classify → resolve → apply), turning it into an operator
conversation:

1. **Resolve the three trees.**
   * ``shipped_old`` — the pristine tree the *installed* version shipped,
     via :func:`carpenter.packages.archive_cache.load_pristine_tree`
     (local cache hit, else the GitHub-Release fetcher).  If unrecoverable
     (no cache, no release, offline) we **degrade to two-way** (current vs
     new) — never block the upgrade.
   * ``shipped_new`` — the new version's tree, read from the ``--to`` source
     directory (or, when ``--to`` is a bare version, fetched + verified from
     the release for that version).
   * ``current`` — the installed package's actual on-disk tree.
2. **Classify** via :func:`carpenter.packages.reconcile.classify`.
3. **Gather ALL resolutions, THEN apply** (transactional, per the design
   note).  Auto-applicable deltas (upstream-only adopt, user-only keep,
   additions, clean removals) need no prompt.  For each *conflict* we print
   the unified diff and prompt keep-current / take-new.  Only once every
   conflict has a decision do we build the final resolved tree and call
   :func:`carpenter.packages.reconcile_apply.apply_reconciled_install`
   atomically.
4. **Interactive-only conflicts.**  A conflict with no controlling tty fails
   with a nonzero exit and applies *nothing* — conflicts are never
   auto-resolved.  A no-conflict upgrade proceeds non-interactively.

The command function takes an ``argv`` list and returns an int exit code
(mirroring ``carpenter.cli_packages``) so it is directly unit-testable
without a subprocess.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


# ── tree helpers ─────────────────────────────────────────────────────


def _read_tree_from_dir(root: Path) -> dict[str, bytes]:
    """Read a package directory into a ``{posix_rel_path: bytes}`` mapping.

    Uses the installer's ``_iter_files`` so the same cruft the install hash
    ignores (``__pycache__``, ``.pyc``, ``.git``, editor temp files) is
    excluded here too — the tree matches what ``compute_package_hash``
    measured, so reconcile comparisons line up with recorded root hashes.
    """
    from carpenter.packages.installer import _iter_files

    tree: dict[str, bytes] = {}
    for path in _iter_files(root):
        rel = path.relative_to(root).as_posix()
        tree[rel] = path.read_bytes()
    return tree


def _looks_like_source_dir(value: str) -> bool:
    """True if ``--to`` points at an existing package source directory."""
    p = Path(value).expanduser()
    return p.is_dir() and (p / "manifest.yaml").is_file()


# ── resolution prompting (gather-all-then-apply) ─────────────────────


# Per-conflict decision values.
KEEP_CURRENT = "keep-current"
TAKE_NEW = "take-new"


def _prompt_conflict(
    delta,
    *,
    old_bytes: bytes | None,
    new_bytes: bytes | None,
    current_bytes: bytes | None,
    input_fn,
    out,
) -> str:
    """Show the diff for one conflict and return the operator's decision.

    Prints the unified diff between the user's *current* content and the
    *new* shipped content, then loops on ``input_fn`` until a valid choice is
    given.  Returns :data:`KEEP_CURRENT` or :data:`TAKE_NEW`.
    """
    from carpenter.packages import reconcile

    p = lambda *a: print(*a, file=out)  # noqa: E731
    p("")
    p("  " + "-" * 68)
    p(f"  CONFLICT: {delta.path}  [{delta.status.value}]")
    p("  " + "-" * 68)

    if current_bytes is None:
        p("  (you deleted this file locally; the new version ships it)")
    elif new_bytes is None:
        p("  (the new version removes this file; you modified it locally)")
    else:
        diff = reconcile.unified_diff(
            delta.path, current_bytes, new_bytes,
            a_label="current", b_label="new",
        )
        p(diff.rstrip("\n") if diff else "  (no textual difference)")

    p("")
    while True:
        try:
            resp = input_fn(
                f"  [{delta.path}] keep your version (k) or take the new "
                f"version (n)? [k/n]: "
            )
        except EOFError:
            # No more input on a stream we believed was interactive: treat as
            # abort rather than silently picking a side.
            raise _AbortUpgrade("input stream closed during conflict prompt")
        choice = (resp or "").strip().lower()
        if choice in ("k", "keep", "keep-current", "current", "mine"):
            return KEEP_CURRENT
        if choice in ("n", "new", "take-new", "theirs"):
            return TAKE_NEW
        p("    Please answer 'k' (keep current) or 'n' (take new).")


class _AbortUpgrade(Exception):
    """Internal: operator/stream aborted the upgrade mid-gather."""


# ── resolved-tree assembly ───────────────────────────────────────────


def _build_resolved_tree(
    plan,
    *,
    new_tree: dict[str, bytes],
    current_tree: dict[str, bytes],
    decisions: dict[str, str],
) -> dict[str, bytes]:
    """Materialize the final ``path -> bytes`` tree from plan + decisions.

    Auto-applicable deltas adopt the obvious side; conflicts use the
    gathered ``decisions``.  A path that resolves to "absent" (user deletion
    honoured, upstream removal adopted) is simply omitted from the result.
    """
    from carpenter.packages.reconcile import FileStatus

    resolved: dict[str, bytes] = {}
    for delta in plan.deltas:
        path = delta.path
        status = delta.status

        if status in (
            FileStatus.UNCHANGED,
            FileStatus.USER_ONLY,
            FileStatus.ADDED_USER,
        ):
            # Keep the user's current content (USER_ONLY may be a deletion →
            # absent from current_tree → correctly omitted).
            if path in current_tree:
                resolved[path] = current_tree[path]
        elif status in (
            FileStatus.UPSTREAM_ONLY,
            FileStatus.ADDED_UPSTREAM,
            FileStatus.CONVERGED,
        ):
            # Adopt the new shipped content.
            if path in new_tree:
                resolved[path] = new_tree[path]
        elif status == FileStatus.REMOVED_UPSTREAM:
            # Upstream removed it and the user hadn't touched it → drop.
            pass
        elif status.is_conflict:
            decision = decisions.get(path)
            if decision == KEEP_CURRENT:
                if path in current_tree:
                    resolved[path] = current_tree[path]
            elif decision == TAKE_NEW:
                if path in new_tree:
                    resolved[path] = new_tree[path]
            else:  # pragma: no cover - guarded by caller
                raise _AbortUpgrade(
                    f"no decision recorded for conflict {path!r}",
                )
        else:  # pragma: no cover - exhaustive enum
            raise _AbortUpgrade(f"unhandled status {status!r} for {path!r}")
    return resolved


# ── summary output ───────────────────────────────────────────────────


def _summarize(plan, decisions, *, out) -> None:
    """Print counts (unchanged / adopted / kept / conflicts) + decisions."""
    from carpenter.packages.reconcile import FileStatus

    counts = {
        "unchanged": 0,
        "adopted": 0,
        "kept": 0,
        "removed": 0,
        "conflicts": 0,
    }
    for d in plan.deltas:
        if d.status == FileStatus.UNCHANGED:
            counts["unchanged"] += 1
        elif d.status in (
            FileStatus.UPSTREAM_ONLY,
            FileStatus.ADDED_UPSTREAM,
            FileStatus.CONVERGED,
        ):
            counts["adopted"] += 1
        elif d.status in (FileStatus.USER_ONLY, FileStatus.ADDED_USER):
            counts["kept"] += 1
        elif d.status == FileStatus.REMOVED_UPSTREAM:
            counts["removed"] += 1
        elif d.status.is_conflict:
            counts["conflicts"] += 1

    p = lambda *a: print(*a, file=out)  # noqa: E731
    p("")
    p("  Reconcile summary:")
    p(f"    unchanged           : {counts['unchanged']}")
    p(f"    adopted (upstream)  : {counts['adopted']}")
    p(f"    kept (your edits)   : {counts['kept']}")
    p(f"    removed (upstream)  : {counts['removed']}")
    p(f"    conflicts           : {counts['conflicts']}")
    if decisions:
        p("")
        p("  Conflict resolutions:")
        for path in sorted(decisions):
            label = (
                "kept your version" if decisions[path] == KEEP_CURRENT
                else "took new version"
            )
            p(f"    • {path}: {label}")


# ── the command ──────────────────────────────────────────────────────


def _cmd_upgrade(
    argv: list[str],
    *,
    input_fn=None,
    out=None,
    interactive: bool | None = None,
    fetcher=None,
    conn=None,
) -> int:
    """Handle ``python3 -m carpenter_linux packages upgrade <name> --to ...``.

    The keyword-only args are injection seams for tests (and would let a GUI
    front-end reuse this flow): ``input_fn`` supplies conflict answers,
    ``out`` is the message stream, ``interactive`` overrides tty detection,
    ``fetcher`` overrides the GitHub archive fetcher, and ``conn`` supplies an
    existing SQLite connection (when given, the caller owns commit/rollback;
    otherwise a :func:`carpenter.db.db_transaction` is opened).  In normal CLI
    use they default to :func:`input`, ``sys.stderr``, ``sys.stdin.isatty()``,
    a real :class:`GitHubReleaseArchiveFetcher`, and a fresh DB transaction.
    """
    parser = argparse.ArgumentParser(
        prog="python3 -m carpenter_linux packages upgrade",
        description=(
            "Upgrade an installed capability package, reconciling local "
            "edits against the new version (dpkg/ucf-style three-way merge)."
        ),
    )
    parser.add_argument("name", help="Installed package name.")
    parser.add_argument(
        "--to", required=True,
        help="Target: a source directory containing the new version's "
             "manifest.yaml, or a bare version string to fetch from the "
             "published release.",
    )
    parser.add_argument(
        "--assume-yes", action="store_true",
        help="(reserved) future non-interactive conflict policy; currently "
             "conflicts always require an interactive decision.",
    )
    args = parser.parse_args(argv)

    if out is None:
        out = sys.stderr
    if input_fn is None:
        input_fn = input
    p = lambda *a: print(*a, file=out)  # noqa: E731

    from carpenter.db import db_transaction
    from carpenter.packages import archive_cache, reconcile
    from carpenter.packages.installer import get_install_record
    from carpenter.packages.reconcile_apply import (
        ReconcileApplyError,
        apply_reconciled_install,
    )

    from .github_fetcher import (
        ArchiveFetchError,
        GitHubReleaseArchiveFetcher,
    )

    if fetcher is None:
        fetcher = GitHubReleaseArchiveFetcher()

    # When a connection is injected (tests / a future embedded caller) the
    # caller owns the transaction; otherwise open one that commits on success
    # and rolls back on failure.
    import contextlib

    if conn is not None:
        conn_ctx = contextlib.nullcontext(conn)
    else:
        conn_ctx = db_transaction()

    with conn_ctx as conn:
        record = get_install_record(conn, args.name)
        if record is None:
            p(f"ERROR: package {args.name!r} is not installed.")
            return 1

        installed_version = record["version"]
        installed_hash = record["hash"]
        install_path = Path(record["install_path"])
        if not install_path.is_dir():
            p(
                f"ERROR: install path for {args.name!r} not found at "
                f"{install_path}."
            )
            return 1

        # ── resolve shipped_new (the target version) ──────────────────
        if _looks_like_source_dir(args.to):
            new_source = Path(args.to).expanduser().resolve()
            new_tree = _read_tree_from_dir(new_source)
            try:
                from carpenter.packages.manifest import load_manifest
                new_version = load_manifest(
                    new_source / "manifest.yaml"
                ).version
            except Exception as exc:  # noqa: BLE001
                p(f"ERROR: could not read target manifest: {exc}")
                return 1
            p(f"  Target: {new_source} (version {new_version})")
        else:
            new_version = args.to
            # Fetch + verify the new version's archive against its own root
            # hash.  We don't have a recorded hash for the *new* version
            # locally, so verification of shipped_new is by-construction
            # against the archive the publisher produced; the apply step
            # re-hashes the materialized tree regardless.  For a bare-version
            # target we require the new tree from the release.
            try:
                fetched = Path(fetcher.fetch(args.name, new_version))
            except ArchiveFetchError as exc:
                p(
                    f"ERROR: could not fetch the target version "
                    f"{args.name}@{new_version}: {exc}"
                )
                p(
                    "  (Pass --to <source_dir> to upgrade from a local "
                    "source tree instead.)"
                )
                return 1
            new_tree = archive_cache._expand_to_tree(fetched)
            fetched.unlink(missing_ok=True)
            p(f"  Target: release {args.name}@{new_version}")

        # ── resolve current (on-disk install tree) ────────────────────
        current_tree = _read_tree_from_dir(install_path)

        # ── resolve shipped_old (pristine tree of installed version) ──
        two_way = False
        try:
            old_tree = archive_cache.load_pristine_tree(
                args.name, installed_version, installed_hash,
                fetcher=fetcher,
            )
        except archive_cache.ArchiveVerificationError as exc:
            # A cached/fetched archive that fails the recorded root hash is a
            # hard integrity problem; refuse rather than silently fall back.
            p(
                f"ERROR: pristine archive for {args.name}@{installed_version} "
                f"failed hash verification: {exc}"
            )
            return 1
        except (archive_cache.ArchiveCacheError, ArchiveFetchError) as exc:
            # Unrecoverable shipped_old (no cache, no release, offline) →
            # degrade to two-way (current vs new), never block.
            two_way = True
            old_tree = dict(current_tree)
            p(
                f"  NOTE: could not recover the pristine tree for the "
                f"installed version {installed_version} ({exc})."
            )
            p(
                "  Degrading to TWO-WAY reconcile (your current copy vs the "
                "new version)."
            )

        # ── classify ──────────────────────────────────────────────────
        plan = reconcile.classify(old_tree, new_tree, current_tree)

        conflicts = plan.conflicts()
        if interactive is None:
            interactive = bool(
                getattr(sys.stdin, "isatty", lambda: False)()
            )

        # ── gather ALL conflict resolutions BEFORE applying ──────────
        decisions: dict[str, str] = {}
        if conflicts:
            if not interactive:
                p("")
                p(
                    f"ERROR: {len(conflicts)} file conflict(s) need a "
                    f"decision but no interactive terminal is available."
                )
                for d in conflicts:
                    p(f"    • {d.path}  [{d.status.value}]")
                p("  Nothing was applied. Re-run on a terminal to resolve.")
                return 1

            p("")
            mode = "TWO-WAY" if two_way else "THREE-WAY"
            p(
                f"  {mode} reconcile of {args.name}: "
                f"{installed_version} -> {new_version}"
            )
            p(
                f"  {len(conflicts)} conflict(s) to resolve. You will decide "
                f"each before anything is applied."
            )
            try:
                for delta in conflicts:
                    decisions[delta.path] = _prompt_conflict(
                        delta,
                        old_bytes=old_tree.get(delta.path),
                        new_bytes=new_tree.get(delta.path),
                        current_bytes=current_tree.get(delta.path),
                        input_fn=input_fn,
                        out=out,
                    )
            except _AbortUpgrade as exc:
                p("")
                p(f"  Upgrade aborted ({exc}). Nothing was applied.")
                return 1

        # ── build resolved tree + apply atomically ────────────────────
        try:
            resolved_tree = _build_resolved_tree(
                plan,
                new_tree=new_tree,
                current_tree=current_tree,
                decisions=decisions,
            )
        except _AbortUpgrade as exc:
            p(f"  Upgrade aborted ({exc}). Nothing was applied.")
            return 1

        if "manifest.yaml" not in resolved_tree:
            # The reconcile must yield a usable package; refuse rather than
            # produce a manifest-less install.
            p(
                "ERROR: the reconciled tree has no manifest.yaml; refusing "
                "to apply. Nothing was changed."
            )
            return 1

        try:
            result = apply_reconciled_install(
                args.name, new_version, resolved_tree,
                conn=conn, dest_path=install_path,
            )
        except ReconcileApplyError as exc:
            p(f"ERROR: applying the reconciled upgrade failed: {exc}")
            p("  The prior install was left untouched.")
            return 1

    # ── report (outside the txn; it committed on success) ─────────────
    _summarize(plan, decisions, out=out)
    p("")
    print(
        f"Upgraded {result.name} {installed_version} -> {result.version} "
        f"(hash {result.hash[:12]}, {result.files_written} file(s) written)"
    )
    if result.kb_articles_installed:
        print(f"  KB articles refreshed: {result.kb_articles_installed}")
    print("")
    print(
        "  Restart the server to load the upgraded package — the registry "
        "scans installed packages at startup."
    )
    print("")
    return 0


# ── dispatcher ───────────────────────────────────────────────────────


_SUBCOMMANDS = {
    "upgrade": _cmd_upgrade,
}


def cmd_packages(argv: list[str]) -> int:
    """Dispatch ``python3 -m carpenter_linux packages <subcommand> ...``.

    Only the Linux-platform-specific ``upgrade`` subcommand lives here; the
    operator install/uninstall/list commands live in ``carpenter-core``'s
    ``carpenter.cli_packages`` and are reachable via ``python3 -m carpenter
    packages``.  Returns an int exit code.
    """
    if not argv or argv[0] in ("-h", "--help"):
        usage = (
            "usage: python3 -m carpenter_linux packages <command> [options]\n"
            "\n"
            "commands:\n"
            "  upgrade <name> --to <version|source_dir>\n"
        )
        if argv and argv[0] in ("-h", "--help"):
            print(usage)
            return 0
        print(usage, file=sys.stderr)
        return 2

    sub = argv[0]
    handler = _SUBCOMMANDS.get(sub)
    if handler is None:
        print(
            f"ERROR: unknown packages subcommand {sub!r}. "
            f"Known: {', '.join(sorted(_SUBCOMMANDS))}.",
            file=sys.stderr,
        )
        return 2
    return handler(argv[1:])
