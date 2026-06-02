"""S044 — yaml-change Workflow Selects on YAML-only Edit

The user, in natural language, asks the agent to make a tiny edit to a
template YAML file (``config_seed/templates/dark-factory.yaml``). Because
the only affected path is a ``.yaml`` file under
``config_seed/templates/``, the platform's
:func:`carpenter.security.platform_paths.select_workflow_for_paths`
classifier picks the ``yaml-change`` workflow (not the default
``coding-change``).

This story plays the role of the human reviewer: it polls for the
change arc to reach ``waiting``, verifies the
``integrity.workflow_selected`` audit row picked ``yaml-change``,
confirms the deterministic ``lint-yaml`` verification step ran, then
approves the diff and waits for the arc to complete.

Built on the ``ChangeReviewStory`` scaffold — the story body is just
the hooks (target prompt, diff inspection, post-apply audit / step
verification, and cleanup that restores the file).

DB verification
---------------
- ``trust_audit_log`` has an ``integrity.workflow_selected`` row with
  ``chosen_template == "yaml-change"``.
- The change arc has a child step with role ``"lint-yaml"`` (the
  deterministic YAML lint) and it reached a terminal status.
- Parent change arc reaches ``status='completed'``.
- No arc in this session ends ``failed`` / ``cancelled``.

Cleanup
-------
Captures the original file contents at run start and writes them back
on teardown — independent of whether the arc applied a commit or not.
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

# The single file the agent will be asked to touch. Picked because:
#   - It is a real platform template (so the lint-yaml step runs the
#     full ``verify_yaml_template`` check, not just safe_load).
#   - It is NOT on the chat-tool / dispatch hot path: the
#     ``dark-factory`` template is an autonomous spec-driven workflow
#     used opt-in by long-running development arcs, never instantiated
#     by routine chat or scheduled triggers in the live daemon.
#   - It already has a top-level ``description:`` field that's a single
#     line — easy to edit, easy to restore.
_TARGET_REL_PATH = "config_seed/templates/dark-factory.yaml"

#: New description we want the agent to write. Distinctive enough that
#: we can grep for it in the diff and verify it landed on disk.
_NEW_DESCRIPTION_SENTINEL = (
    "Autonomous spec-driven development workflow with iterative "
    "implementation and holdout validation (s044 acceptance test)"
)


def _resolve_source_dir() -> Path:
    """Resolve the platform source dir the same way the platform does.

    Reads ``platform_server_dir`` from ``~/carpenter/config/config.yaml``
    when present; falls back to ``CARPENTER_SOURCE_DIR`` or this repo's
    parent. Returns an absolute Path.
    """
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


class YamlChangeTemplateWorkflow(ChangeReviewStory):
    name = "S044 — yaml-change Workflow Selects on YAML-only Edit"
    description = (
        "Agent edits config_seed/templates/dark-factory.yaml via "
        "create_change_arc(affected_paths=[...]); platform picks "
        "yaml-change template; lint-yaml verifier runs; story approves; "
        "arc completes."
    )
    artifact_prefix = "s044"
    timeout = 600  # Coding-change pipeline needs >300s on Pi.

    # Hand-tuned because the agent's response after a YAML-only request
    # tends to mention "yaml", "template", or the file name rather than
    # "coding".
    ack_keywords = (
        "yaml", "template", "edit", "modif", "change", "arc",
        "update", "description", "dark-factory",
    )

    request_text = (
        "Please edit the file `config_seed/templates/dark-factory.yaml` "
        "to update its top-level `description:` field. Set the new "
        f"description to exactly:\n\n  {_NEW_DESCRIPTION_SENTINEL}\n\n"
        "Use the platform coding-change workflow and pass "
        "`affected_paths=[\"config_seed/templates/dark-factory.yaml\"]` "
        "so the right specialised workflow is selected. Do not change "
        "anything else in the file."
    )

    approve_comment = "Approved — description update only."

    # ── Per-run state ────────────────────────────────────────────────

    _source_dir: Path | None = None
    _target_path: Path | None = None
    _original_text: str | None = None

    def __init__(self) -> None:
        # Resolve + snapshot the target file as soon as we're instantiated
        # so cleanup() can always restore even if run() fails before any
        # work happens.
        self._source_dir = _resolve_source_dir()
        self._target_path = self._source_dir / _TARGET_REL_PATH
        if self._target_path.exists():
            self._original_text = self._target_path.read_text()
        else:
            self._original_text = None

    # ── Hooks ────────────────────────────────────────────────────────

    def inspect_diff(self, diff: str, arc_state: dict) -> None:
        # Sanity: diff is non-empty (the base assertion).
        super().inspect_diff(diff, arc_state)

        # The diff should touch our target file. The coding agent
        # generates a unified diff that names the file in its header.
        target_name = Path(_TARGET_REL_PATH).name
        self.assert_that(
            target_name in diff,
            f"Diff does not reference {target_name!r}",
            diff_preview=diff[:600],
            changed_files=arc_state.get("changed_files"),
        )

        # arc_state.changed_files (when present) should ALSO be all-yaml.
        # If the agent silently picked up other files we want to know.
        changed = arc_state.get("changed_files") or []
        if changed:
            non_yaml = [
                f for f in changed
                if not (str(f).lower().endswith(".yaml")
                        or str(f).lower().endswith(".yml"))
            ]
            self.assert_that(
                not non_yaml,
                f"Change touched non-YAML file(s): {non_yaml!r} — "
                f"yaml-change should never have been selected if so",
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

        # ── A. integrity.workflow_selected picked yaml-change ────────
        event = db.get_workflow_selected_event_after(self._start_ts)
        self.assert_that(
            event is not None,
            "No integrity.workflow_selected audit row was written "
            "after this run started — affected_paths may have been "
            "missing, so the platform defaulted to coding-change "
            "without classifying",
        )
        self.assert_that(
            event.get("chosen_template") == "yaml-change",  # type: ignore[union-attr]
            f"integrity.workflow_selected.chosen_template="
            f"{event.get('chosen_template')!r}, expected 'yaml-change'",  # type: ignore[union-attr]
            audit_event=event,
        )
        categories = event.get("categories") or []  # type: ignore[union-attr]
        self.assert_that(
            all(c == "yaml" for c in categories),
            f"audit categories include non-yaml entries: {categories!r}",
            audit_event=event,
        )

        # ── B. lint-yaml verifier step ran ───────────────────────────
        # Verifier arcs are NOT children of the implementation arc —
        # they share its parent and link back via
        # ``arcs.verification_target_id``. See
        # ``carpenter.core.arcs.verification.create_verification_arcs``.
        lint_step = db.get_verification_arc_by_role(
            review_arc["id"], "lint-yaml"
        )
        verifiers = db.get_verification_arcs_for(review_arc["id"])
        self.assert_that(
            lint_step is not None,
            "No verifier arc with step_role/name 'lint-yaml' "
            "(verification_target_id=" f"{review_arc['id']}) — the "
            "yaml-change template's deterministic lint step did not run",
            verifiers=[
                {"id": v["id"], "name": v.get("name"),
                 "step_role": v.get("step_role"),
                 "status": v.get("status")}
                for v in verifiers
            ],
        )
        self.assert_that(
            lint_step.get("status") in db.TERMINAL_ARC_STATUSES,  # type: ignore[union-attr]
            f"lint-yaml step did not reach a terminal status "
            f"(status={lint_step.get('status')!r})",  # type: ignore[union-attr]
            lint_step=lint_step,
        )
        # Sanity: the verifier reached `completed` (not `failed`), which
        # means our YAML was valid by the deterministic check.
        self.assert_that(
            lint_step.get("status") == "completed",  # type: ignore[union-attr]
            f"lint-yaml ran but did not pass "
            f"(status={lint_step.get('status')!r}) — the agent's "
            "diff produced invalid YAML",  # type: ignore[union-attr]
            lint_step=lint_step,
        )

        # ── C. On-disk verification ──────────────────────────────────
        # The new description text should be in the file now.
        if self._target_path is not None and self._target_path.exists():
            disk = self._target_path.read_text()
            self.assert_that(
                _NEW_DESCRIPTION_SENTINEL in disk,
                "Target file does not contain the requested "
                "description text after approval",
                target=str(self._target_path),
                disk_preview=disk[:400],
            )

    # ── Cleanup ──────────────────────────────────────────────────────

    def cleanup(
        self, client: CarpenterClient, db: "DBInspector | None"
    ) -> None:
        """Restore the YAML file from the snapshot taken at __init__."""
        target = self._target_path
        original = self._original_text

        if target is None or original is None:
            return

        try:
            current = target.read_text() if target.exists() else None
            if current != original:
                target.write_text(original)
                print(f"  [cleanup] Restored {target} to original contents")
        except Exception as exc:  # noqa: BLE001
            print(f"  [cleanup] Could not restore {target}: {exc}")

        # If the coding-change pipeline applied the diff via git commit,
        # the most recent commit in the source repo will mention our
        # sentinel. Best-effort revert so the repo is left clean.
        if self._source_dir is None:
            return
        try:
            log = subprocess.run(
                ["git", "log", "--oneline", "-1", "--format=%s%n%b"],
                cwd=str(self._source_dir),
                capture_output=True, text=True, timeout=5,
            )
            if log.returncode == 0 and (
                "s044" in log.stdout.lower()
                or "dark-factory" in log.stdout.lower()
                or "acceptance test" in log.stdout.lower()
            ):
                subprocess.run(
                    ["git", "reset", "--hard", "HEAD~1"],
                    cwd=str(self._source_dir),
                    capture_output=True, text=True, timeout=10,
                )
                print(
                    "  [cleanup] Reverted last commit "
                    "(contained s044 / dark-factory / acceptance test)"
                )
        except Exception as exc:  # noqa: BLE001
            print(f"  [cleanup] git revert check failed: {exc}")
