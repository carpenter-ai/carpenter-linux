"""S045 — kb-change Workflow Selects on KB-only Edit

The user, in natural language, asks the agent to add a short note to
the knowledge base. Because the only affected path is a ``.md`` file
under a ``/kb/`` segment, the platform's
:func:`carpenter.security.platform_paths.select_workflow_for_paths`
classifier picks the ``kb-change`` workflow (not the default
``coding-change``).

This story plays the role of the human reviewer: it polls for the
change arc to reach ``waiting``, verifies the
``integrity.workflow_selected`` audit row picked ``kb-change``,
confirms the deterministic ``verify-kb-format`` verification step ran,
then approves the diff and waits for the arc to complete.

Built on the ``ChangeReviewStory`` scaffold — story body is the hooks
(target prompt, diff inspection, post-apply audit / step verification,
and cleanup).

DB verification
---------------
- ``trust_audit_log`` has an ``integrity.workflow_selected`` row with
  ``chosen_template == "kb-change"``.
- The change arc has a child step with role ``"verify-kb-format"``
  (the deterministic KB-format check) and it reached a terminal status.
- Parent change arc reaches ``status='completed'``.
- No arc in this session ends ``failed`` / ``cancelled``.

Cleanup
-------
Deletes the new KB file (glob-based — agent may pick a slightly
different filename within the requested namespace). Also reverts the
last commit in the source repo when it carries our marker.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from user_stories.framework import (
    CarpenterClient,
    ChangeReviewStory,
    DBInspector,
)

# Stable namespace for the test artifact. The exact filename can vary
# slightly (the agent may pluralize or add a date prefix), so cleanup
# globs the namespace. ``s045-*`` is enough to scope this run; the
# scaffold's ``run_id`` is not used in the KB path because the agent's
# natural-language prompt would have to include it verbatim, and that
# noise reduces the chance the agent picks the right kb-segment shape.
_KB_REL_DIR = "config_seed/kb/scratch"
_KB_FILENAME_HINT = "s045-acceptance-test-note.md"

#: Distinctive marker we ask the agent to put in the note. We grep for
#: it in the diff and in the resulting file (when present) to confirm
#: this is the artifact we created and not a stray pre-existing entry.
_NOTE_SENTINEL = "s045 acceptance test marker — kb-change verification"


def _resolve_source_dir() -> Path:
    env = os.environ.get("CARPENTER_SOURCE_DIR")
    if env:
        return Path(env).resolve()
    cfg_path = Path.home() / "carpenter" / "config" / "config.yaml"
    if cfg_path.exists():
        try:
            import yaml as _yaml
            cfg = _yaml.safe_load(cfg_path.read_text()) or {}
            server_dir = cfg.get("platform_server_dir")
            if server_dir:
                return Path(server_dir).resolve()
        except Exception:  # noqa: BLE001
            pass
    return Path(__file__).resolve().parents[1]


class KbChangeWorkflow(ChangeReviewStory):
    name = "S045 — kb-change Workflow Selects on KB-only Edit"
    description = (
        "Agent adds a new note under config_seed/kb/scratch/ via "
        "create_change_arc(affected_paths=[...]); platform picks "
        "kb-change template; verify-kb-format verifier runs; story "
        "approves; arc completes."
    )
    artifact_prefix = "s045"
    timeout = 600

    # Lenient — the agent often replies "added", "saved", "kb", "note".
    ack_keywords = (
        "kb", "knowledge", "note", "add", "save", "wrote",
        "modif", "change", "arc", "scratch",
    )

    request_text = (
        "Please add a short knowledge-base note. Create a NEW file at "
        f"`{_KB_REL_DIR}/{_KB_FILENAME_HINT}` containing exactly one "
        f"paragraph that includes the phrase:\n\n  {_NOTE_SENTINEL}\n\n"
        "Use the platform coding-change workflow and pass "
        f"`affected_paths=[\"{_KB_REL_DIR}/{_KB_FILENAME_HINT}\"]` "
        "so the kb-change specialised workflow is selected. Do not "
        "modify any other files."
    )

    approve_comment = "Approved — new KB scratch note."

    # ── Per-run state ────────────────────────────────────────────────

    _source_dir: Path | None = None

    def __init__(self) -> None:
        self._source_dir = _resolve_source_dir()

    # ── Hooks ────────────────────────────────────────────────────────

    def inspect_diff(self, diff: str, arc_state: dict) -> None:
        super().inspect_diff(diff, arc_state)

        # The diff should reference our KB filename hint OR our note
        # sentinel. We don't insist on both because the unified-diff
        # header path can vary (relative vs absolute) but the marker
        # is content-line and will appear verbatim.
        self.assert_that(
            _NOTE_SENTINEL in diff or _KB_FILENAME_HINT in diff,
            "Diff references neither the KB filename hint nor the "
            "note sentinel — agent likely wrote a different file",
            diff_preview=diff[:600],
            changed_files=arc_state.get("changed_files"),
        )

        # arc_state.changed_files (when present) should ALL be KB
        # markdown — the kb-change workflow is only selected when
        # every path is in the kb category.
        changed = arc_state.get("changed_files") or []
        if changed:
            non_kb = [
                f for f in changed
                if not (str(f).lower().endswith(".md")
                        and ("/kb/" in str(f) or os.sep + "kb"
                             + os.sep in str(f)))
            ]
            self.assert_that(
                not non_kb,
                f"Change touched non-KB file(s): {non_kb!r} — "
                f"kb-change should never have been selected if so",
                changed_files=changed,
            )

    def post_apply(
        self,
        client: CarpenterClient,
        db: "DBInspector | None",
        conv_id: int,
        review_arc: dict,
    ) -> None:
        if db is None:
            return

        # ── A. integrity.workflow_selected picked kb-change ──────────
        event = db.get_workflow_selected_event_after(self._start_ts)
        self.assert_that(
            event is not None,
            "No integrity.workflow_selected audit row was written "
            "after this run started — affected_paths may have been "
            "missing, so the platform defaulted to coding-change",
        )
        self.assert_that(
            event.get("chosen_template") == "kb-change",  # type: ignore[union-attr]
            f"integrity.workflow_selected.chosen_template="
            f"{event.get('chosen_template')!r}, expected 'kb-change'",  # type: ignore[union-attr]
            audit_event=event,
        )
        categories = event.get("categories") or []  # type: ignore[union-attr]
        self.assert_that(
            all(c == "kb" for c in categories),
            f"audit categories include non-kb entries: {categories!r}",
            audit_event=event,
        )

        # ── B. verify-kb-format verifier step ran ────────────────────
        # Verifier arcs are NOT children of the implementation arc —
        # they share its parent and link back via
        # ``arcs.verification_target_id``. See
        # ``carpenter.core.arcs.verification.create_verification_arcs``.
        kb_step = db.get_verification_arc_by_role(
            review_arc["id"], "verify-kb-format"
        )
        verifiers = db.get_verification_arcs_for(review_arc["id"])
        self.assert_that(
            kb_step is not None,
            "No verifier arc with step_role/name 'verify-kb-format' "
            "(verification_target_id=" f"{review_arc['id']}) — the "
            "kb-change template's deterministic format-check step did "
            "not run",
            verifiers=[
                {"id": v["id"], "name": v.get("name"),
                 "step_role": v.get("step_role"),
                 "status": v.get("status")}
                for v in verifiers
            ],
        )
        self.assert_that(
            kb_step.get("status") in db.TERMINAL_ARC_STATUSES,  # type: ignore[union-attr]
            f"verify-kb-format step did not reach a terminal status "
            f"(status={kb_step.get('status')!r})",  # type: ignore[union-attr]
            kb_step=kb_step,
        )
        self.assert_that(
            kb_step.get("status") == "completed",  # type: ignore[union-attr]
            f"verify-kb-format ran but did not pass "
            f"(status={kb_step.get('status')!r}) — the agent's KB "
            "file failed the deterministic format check",  # type: ignore[union-attr]
            kb_step=kb_step,
        )

    # ── Cleanup ──────────────────────────────────────────────────────

    def cleanup(
        self, client: CarpenterClient, db: "DBInspector | None"
    ) -> None:
        """Remove any KB file the agent created under our namespace."""
        if self._source_dir is None:
            return

        kb_dir = self._source_dir / _KB_REL_DIR
        if kb_dir.is_dir():
            # Glob both the exact filename and any close variants, plus
            # any file whose name starts with ``s045``.
            patterns = (
                _KB_FILENAME_HINT,
                "s045*.md",
                "s045*.markdown",
                "*acceptance-test-note*",
            )
            removed: list[Path] = []
            for pat in patterns:
                for match in kb_dir.glob(pat):
                    try:
                        match.unlink()
                        removed.append(match)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  [cleanup] Could not unlink {match}: {exc}")
            if removed:
                print(
                    f"  [cleanup] Removed {len(removed)} KB file(s): "
                    f"{[p.name for p in removed]}"
                )

            # If the directory is now empty AND we created it (rare —
            # `scratch/` may have existed before us), leave it alone.
            # Best to be conservative; just do nothing.

        # Also strip any kb_entries / kb_links rows that point at our
        # path. The platform autogenerates these from on-disk files at
        # startup but a hot-reload may have indexed them already.
        if db is not None:
            try:
                import sqlite3 as _sql3
                conn = _sql3.connect(db.db_path)
                try:
                    # KB path under the table excludes the leading dir;
                    # it's whatever the kb tool stored. Match the
                    # namespace LIKE 'scratch/s045%' OR by sentinel.
                    conn.execute(
                        "DELETE FROM kb_entries WHERE path LIKE ? "
                        "OR path LIKE ?",
                        ("scratch/s045%", "%s045-acceptance-test-note%"),
                    )
                    conn.execute(
                        "DELETE FROM kb_links WHERE source_path LIKE ? "
                        "OR target_path LIKE ?",
                        ("scratch/s045%", "scratch/s045%"),
                    )
                    conn.commit()
                finally:
                    conn.close()
            except Exception as exc:  # noqa: BLE001
                print(f"  [cleanup] DB kb_entries cleanup failed: {exc}")

        # Revert last commit if it carries our marker.
        #
        # SAFETY: refuse the reset if (a) HEAD touches files outside
        # the s045 KB scratch namespace — that means a real commit got
        # matched by the marker; or (b) the working tree has dirty
        # files outside our namespace — `git reset --hard` would
        # discard those. Better to leave the test commit than to nuke
        # a developer's work-in-progress.
        try:
            log = subprocess.run(
                ["git", "log", "--oneline", "-1", "--format=%s%n%b"],
                cwd=str(self._source_dir),
                capture_output=True, text=True, timeout=5,
            )
            if not (log.returncode == 0 and (
                "s045" in log.stdout.lower()
                or "kb scratch" in log.stdout.lower()
                or "acceptance test" in log.stdout.lower()
            )):
                return

            # All files in HEAD must be under our KB scratch namespace.
            head_files = subprocess.run(
                ["git", "diff-tree", "--no-commit-id", "--name-only",
                 "-r", "HEAD"],
                cwd=str(self._source_dir),
                capture_output=True, text=True, timeout=5,
            )
            if head_files.returncode != 0:
                return
            changed = [
                p.strip() for p in head_files.stdout.splitlines() if p.strip()
            ]
            scope_prefix = _KB_REL_DIR.rstrip("/") + "/"
            outside_scope = [p for p in changed if not p.startswith(scope_prefix)]
            if not changed or outside_scope:
                print(
                    "  [cleanup] Refusing git reset: HEAD touches "
                    f"{changed!r}, not just files under {scope_prefix!r}"
                    " — marker likely matched a real commit."
                )
                return

            # Working tree must not have uncommitted changes outside
            # our scope.
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(self._source_dir),
                capture_output=True, text=True, timeout=5,
            )
            other_dirty = [
                ln for ln in status.stdout.splitlines()
                if ln.strip() and scope_prefix not in ln
            ]
            if other_dirty:
                print(
                    "  [cleanup] Refusing git reset: working tree has "
                    f"{len(other_dirty)} uncommitted change(s) outside "
                    f"{scope_prefix!r} — would lose work."
                )
                return

            subprocess.run(
                ["git", "reset", "--hard", "HEAD~1"],
                cwd=str(self._source_dir),
                capture_output=True, text=True, timeout=10,
            )
            print(
                "  [cleanup] Reverted last commit "
                "(only touched files under KB scratch namespace)"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  [cleanup] git revert check failed: {exc}")
