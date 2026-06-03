"""
S055 — Gmail write-tool platform integration (4-arc tree dispatch).

End-to-end platform check for the carpenter-gmail Phase 1.5 v2 write
tools — ``pkg_gmail_archive_email`` / ``pkg_gmail_mark_read_email`` /
``pkg_gmail_draft_email``.

Each tool, when invoked at the chat boundary, must build the full
4-arc tree the running carpenter daemon's executor and validator
expect:

  PLANNER (trusted) -> EXECUTOR (untrusted) + REVIEWER (constrained,
  no KB) + JUDGE (deterministic Python via ``_try_package_judge``).

This story owns the platform integration assertions: that the
platform actually stitches that tree together (children, integrity
levels, arc-state seeding, Resource provenance, work-queue dispatch)
when these tools fire.  It does NOT own:

* manifest version pinning, OAuth scopes, or KB articles
  (``carpenter-gmail::manifest_shape``);
* chat-tool decorator metadata (``::chat_tool_registry``);
* JUDGE handler input/output golden cases
  (``::judge_handlers_accept_reject``);
* EXECUTOR script dispatch-verb allowlist (``::scripts_pass_ast_lint``);
* fail-closed behaviour when ``expected_account_email`` is unset and
  chat-boundary allowlist prechecks — these are package-internal
  preconditions tested in the package's own stories.

What this story verifies (STRICT)

  1. With ``operator_email`` configured and an allowlisted recipient,
     each of ``archive_email`` / ``mark_read_email`` / ``draft_email``
     constructs the FULL 4-arc tree — the platform batch validator
     MUST accept it.
  2. Per-arc shape: agent_types are exactly
     ``[EXECUTOR, REVIEWER, JUDGE]`` in step order; integrity_level is
     untrusted / trusted / trusted.
  3. Resource wiring: the EXECUTOR has a raw receipt Resource output
     (untrusted, produced_by_template=NULL); the REVIEWER has both the
     briefing Resource (born-trusted, ``produced_by_template`` set to
     this template's name with ``template_verdict='approved'``) and
     the raw receipt as inputs, and a pending extract Resource as
     output (``produced_by_template`` set, ``template_verdict='pending'``).
  4. Arc-state seeding: the EXECUTOR has ``expected_account_email``,
     ``raw_resource_path``, and ``raw_resource_id``; the parent
     PLANNER has ``template_name`` (one of ``email_write_archive`` /
     ``email_write_mark_read`` / ``email_write_draft``), the matching
     ``extract_kind``, and ``staged_to_addresses`` (empty list for
     archive/mark-read, the recipient list for draft).
  5. The work_queue has an ``arc.dispatch`` entry pointing at the
     EXECUTOR arc id (idempotency_key ``arc_dispatch:<executor_id>``).

Why this story does NOT round-trip through the LLM
--------------------------------------------------

The Phase 1.5 v2 write tools all declare ``requires_user_confirm=True``,
which means the chat agent cannot execute them autonomously — they
require a real human approval click at the chat boundary.  Driving
that approval flow from a headless acceptance harness would require
mocking the human-confirm UI, which adds substantially more surface
than the trust shape we actually want to assert.  Instead this story
loads the package's chat-tool entrypoints directly (the same path the
``carpenter-core`` unit tests use via
``carpenter.packages.loaders._import_package_module``) and exercises
them in-process with seeded policies and config.  This matches the
existing pattern of stories like s050 that drive work-queue injection
directly rather than going through the LLM.

Gmail mocking
-------------

We do NOT exercise the EXECUTOR's actual Gmail dispatch — that's not
the story's job.  The EXECUTOR's script body is statically verified
(no agent-side code generation), and its dispatch surface is
AST-linted in the package's own ``::scripts_pass_ast_lint`` story.
What this story verifies is the chat-tool front door: arcs created
with the right shape and trust levels, Resource provenance wired so
the JUDGE has something to graduate, and the audit trail laid down.
The running carpenter daemon's executor sandbox is what runs the
actual Gmail call, and the EXECUTOR will hit a deterministic
``RuntimeError("GMAIL_OAUTH_ACCESS_TOKEN not in environment")`` until
the user has authorized — we assert the arc tree gets constructed
correctly; whether the EXECUTOR succeeds or fails at the network is
out of scope here.

DB cleanup: removes any arcs / arc_state / arc_history / arc_resources
/ resources / conversation_arcs / work_queue entries created during
the test.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)


# Resolution order matches the carpenter-core unit-test fixture so the
# story runs against the same package source tree that the chat agent
# would load at install time.
_PACKAGES_DIR_CANDIDATES = (
    os.environ.get("CARPENTER_PACKAGES_DIR", ""),
    str(Path.home() / "repos" / "carpenter-packages" / "packages"),
)


_TEMPLATE_BY_TOOL = {
    "pkg_gmail_archive_email":    "email_write_archive",
    "pkg_gmail_mark_read_email":  "email_write_mark_read",
    "pkg_gmail_draft_email":      "email_write_draft",
}

_EXTRACT_KIND_BY_TEMPLATE = {
    "email_write_archive":   "EmailArchiveResult",
    "email_write_mark_read": "EmailMarkReadResult",
    "email_write_draft":     "EmailDraftResult",
}


def _find_email_package() -> Path | None:
    for candidate in _PACKAGES_DIR_CANDIDATES:
        if not candidate:
            continue
        path = Path(candidate) / "carpenter-gmail"
        if path.is_dir():
            return path
    return None


class EmailArchiveAndDraft(AcceptanceStory):
    name = "S055 — Gmail write-tool platform integration (4-arc tree dispatch)"
    description = (
        "Verify pkg_gmail_archive_email / pkg_gmail_mark_read_email / "
        "pkg_gmail_draft_email construct the full PLANNER + EXECUTOR + "
        "REVIEWER + JUDGE pipeline when fired against the running "
        "platform.  Strict assertions on arc tree shape, integrity "
        "levels, Resource provenance, arc-state seeding, and "
        "work-queue dispatch.  Package-internal assertions (manifest "
        "shape, chat-tool decorator metadata, fail-closed precondition, "
        "allowlist precheck) live in the carpenter-gmail package's own "
        "stories."
    )
    timeout = 600

    # Records for cleanup
    _created_arc_ids: list[int]
    _created_resource_ids: list[int]
    _allowlisted_email: str
    _previously_in_allowlist: bool
    _saved_operator_email: str | None

    def __init__(self) -> None:
        self._created_arc_ids = []
        self._created_resource_ids = []
        self._allowlisted_email = "phase15-test-recipient@example.com"
        self._previously_in_allowlist = False
        self._saved_operator_email = None

    # ------------------------------------------------------------------
    # Setup / teardown helpers
    # ------------------------------------------------------------------

    def _load_package_tools(self, pkg_dir: Path):
        """Import the carpenter-gmail tools module via the same loader
        the platform uses at install time.

        This populates ``sys.modules`` under namespaced names so the
        relative ``from .scripts import`` inside tools.py resolves.
        """
        from carpenter.packages.loaders import _import_package_module

        _import_package_module("carpenter-gmail", "data_models", pkg_dir)
        _import_package_module("carpenter-gmail", "scripts", pkg_dir)
        return _import_package_module(
            "carpenter-gmail", "tools", pkg_dir,
        )

    def _seed_email_allowlist(self) -> None:
        """Add the test recipient to SecurityPolicies.email if missing."""
        from carpenter.security import get_policies
        from carpenter.security import policy_store

        pol = get_policies()
        existing = set(pol.get_allowlist("email"))
        if self._allowlisted_email in existing:
            self._previously_in_allowlist = True
            return
        policy_store.add_to_allowlist("email", self._allowlisted_email)
        pol.add("email", self._allowlisted_email)

    def _clear_email_allowlist(self) -> None:
        if self._previously_in_allowlist:
            return
        from carpenter.security import get_policies
        from carpenter.security import policy_store

        try:
            policy_store.remove_from_allowlist(
                "email", self._allowlisted_email,
            )
            get_policies().remove("email", self._allowlisted_email)
        except Exception:  # noqa: BLE001
            pass

    def _set_operator_email(self, value: str) -> None:
        from carpenter import config

        if self._saved_operator_email is None:
            self._saved_operator_email = config.CONFIG.get("operator_email")
        config.CONFIG["operator_email"] = value

    def _restore_operator_email(self) -> None:
        from carpenter import config

        if self._saved_operator_email is None:
            config.CONFIG.pop("operator_email", None)
        else:
            config.CONFIG["operator_email"] = self._saved_operator_email
        self._saved_operator_email = None

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _all_arc_ids(self, db: DBInspector) -> list[int]:
        rows = db.fetchall("SELECT id FROM arcs")
        return [r["id"] for r in rows]

    def _resources_for_arc(
        self, db: DBInspector, arc_id: int,
    ) -> list[dict]:
        """Return resources linked to an arc with their provenance fields."""
        return db.fetchall(
            "SELECT r.id, r.content_type, r.produced_by_template, "
            "r.template_verdict, ar.role "
            "FROM arc_resources ar JOIN resources r "
            "ON ar.resource_id = r.id "
            "WHERE ar.arc_id = ? "
            "ORDER BY r.id",
            (arc_id,),
        )

    def _work_queue_for_arc(
        self, db: DBInspector, arc_id: int,
    ) -> list[dict]:
        return db.fetchall(
            "SELECT id, event_type, idempotency_key, payload_json "
            "FROM work_queue WHERE idempotency_key = ?",
            (f"arc_dispatch:{arc_id}",),
        )

    # ------------------------------------------------------------------
    # Per-tool strict assertion helper
    # ------------------------------------------------------------------

    def _assert_full_pipeline(
        self,
        *,
        db: DBInspector,
        tool_name: str,
        tool_fn,
        tool_input: dict,
        expected_staged_to: list[str],
    ) -> int:
        """Invoke a write tool and assert the full 4-arc tree was built.

        Returns the parent PLANNER arc id (caller appends to cleanup list).
        """
        template_name = _TEMPLATE_BY_TOOL[tool_name]
        extract_kind = _EXTRACT_KIND_BY_TEMPLATE[template_name]

        result = json.loads(tool_fn(tool_input))
        self.assert_that(
            "arc_id" in result,
            f"{tool_name} must return arc_id (full pipeline); "
            f"got: {result}",
        )
        parent_id = int(result["arc_id"])

        # ── Children: exactly [EXECUTOR, REVIEWER, JUDGE] in step order
        children = db.get_arc_children(parent_id)
        self.assert_that(
            len(children) == 3,
            f"{tool_name} parent arc must have exactly 3 children; "
            f"got: {len(children)} ({[c.get('name') for c in children]})",
        )
        agent_types = [c["agent_type"] for c in children]
        self.assert_that(
            agent_types == ["EXECUTOR", "REVIEWER", "JUDGE"],
            f"{tool_name} children must be [EXECUTOR, REVIEWER, JUDGE] "
            f"in step order; got: {agent_types}",
        )
        executor, reviewer, judge = children
        self.assert_that(
            executor["integrity_level"] == "untrusted",
            f"{tool_name} EXECUTOR must be untrusted; got: "
            f"{executor['integrity_level']!r}",
        )
        self.assert_that(
            reviewer["integrity_level"] == "trusted",
            f"{tool_name} REVIEWER must be trusted; got: "
            f"{reviewer['integrity_level']!r}",
        )
        self.assert_that(
            judge["integrity_level"] == "trusted",
            f"{tool_name} JUDGE must be trusted; got: "
            f"{judge['integrity_level']!r}",
        )

        # ── Parent arc-state seeding
        parent_state = db.get_arc_state(parent_id)
        self.assert_that(
            parent_state.get("expected_account_email") == "ben@example.com",
            f"{tool_name} parent arc must seed expected_account_email; "
            f"state: {parent_state!r}",
        )
        self.assert_that(
            parent_state.get("template_name") == template_name,
            f"{tool_name} parent arc must seed template_name="
            f"{template_name!r}; got: {parent_state.get('template_name')!r}",
        )
        self.assert_that(
            parent_state.get("extract_kind") == extract_kind,
            f"{tool_name} parent arc must seed extract_kind="
            f"{extract_kind!r}; got: {parent_state.get('extract_kind')!r}",
        )
        self.assert_that(
            parent_state.get("staged_to_addresses") == expected_staged_to,
            f"{tool_name} parent arc staged_to_addresses must be "
            f"{expected_staged_to!r}; got: "
            f"{parent_state.get('staged_to_addresses')!r}",
        )
        self.assert_that(
            "briefing_resource_id" in parent_state,
            f"{tool_name} parent arc must seed briefing_resource_id; "
            f"state keys: {sorted(parent_state.keys())}",
        )
        self.assert_that(
            "_primary_resource_id" in parent_state,
            f"{tool_name} parent arc must seed _primary_resource_id; "
            f"state keys: {sorted(parent_state.keys())}",
        )

        # ── EXECUTOR arc-state seeding
        executor_state = db.get_arc_state(executor["id"])
        for required_key in (
            "expected_account_email",
            "raw_resource_path",
            "raw_resource_id",
        ):
            self.assert_that(
                required_key in executor_state,
                f"{tool_name} EXECUTOR must seed {required_key!r}; "
                f"state keys: {sorted(executor_state.keys())}",
            )
        self.assert_that(
            executor_state["expected_account_email"] == "ben@example.com",
            f"{tool_name} EXECUTOR expected_account_email must be "
            f"'ben@example.com'; got: "
            f"{executor_state['expected_account_email']!r}",
        )

        # ── REVIEWER arc-state seeding (briefing + raw + extract refs)
        reviewer_state = db.get_arc_state(reviewer["id"])
        for required_key in (
            "briefing_resource_id",
            "raw_resource_path",
            "raw_resource_id",
            "extract_resource_id",
            "extract_kind",
            "template_name",
        ):
            self.assert_that(
                required_key in reviewer_state,
                f"{tool_name} REVIEWER must seed {required_key!r}; "
                f"state keys: {sorted(reviewer_state.keys())}",
            )
        self.assert_that(
            reviewer_state["template_name"] == template_name,
            f"{tool_name} REVIEWER template_name must be "
            f"{template_name!r}; got: {reviewer_state['template_name']!r}",
        )

        # ── JUDGE arc-state seeding (review target)
        judge_state = db.get_arc_state(judge["id"])
        self.assert_that(
            "_review_target_resource_id" in judge_state,
            f"{tool_name} JUDGE must seed _review_target_resource_id; "
            f"state keys: {sorted(judge_state.keys())}",
        )

        # ── Resource wiring on EXECUTOR: raw output, NOT yet graduated
        executor_resources = self._resources_for_arc(db, executor["id"])
        raw_outputs = [
            r for r in executor_resources if r["role"] == "output"
        ]
        self.assert_that(
            len(raw_outputs) == 1,
            f"{tool_name} EXECUTOR must have exactly one output Resource "
            f"(the raw receipt); got: {raw_outputs}",
        )
        raw = raw_outputs[0]
        self.assert_that(
            raw["produced_by_template"] is None,
            f"{tool_name} EXECUTOR raw receipt must have "
            f"produced_by_template=NULL (it's untrusted, JUDGE has not "
            f"graduated it yet); got: {raw['produced_by_template']!r}",
        )
        self._created_resource_ids.append(raw["id"])

        # ── Resource wiring on REVIEWER: briefing input + raw input + extract output
        reviewer_resources = self._resources_for_arc(db, reviewer["id"])
        reviewer_inputs = [
            r for r in reviewer_resources if r["role"] == "input"
        ]
        reviewer_outputs = [
            r for r in reviewer_resources if r["role"] == "output"
        ]
        self.assert_that(
            len(reviewer_inputs) == 2,
            f"{tool_name} REVIEWER must have 2 input Resources "
            f"(briefing + raw receipt); got: {len(reviewer_inputs)}",
        )
        self.assert_that(
            len(reviewer_outputs) == 1,
            f"{tool_name} REVIEWER must have 1 output Resource (the "
            f"pending extract); got: {len(reviewer_outputs)}",
        )

        # Briefing: born-trusted, produced_by_template == template_name,
        # template_verdict == 'approved'.
        briefings = [
            r for r in reviewer_inputs
            if r["produced_by_template"] == template_name
            and r["template_verdict"] == "approved"
        ]
        self.assert_that(
            len(briefings) == 1,
            f"{tool_name} REVIEWER must have a born-trusted briefing "
            f"Resource (produced_by_template={template_name!r}, "
            f"template_verdict='approved'); got inputs: {reviewer_inputs}",
        )
        self._created_resource_ids.append(briefings[0]["id"])

        # The other input is the raw receipt; verify it's the same id we
        # found on the EXECUTOR's output.
        other_inputs = [
            r for r in reviewer_inputs if r["id"] != briefings[0]["id"]
        ]
        self.assert_that(
            len(other_inputs) == 1 and other_inputs[0]["id"] == raw["id"],
            f"{tool_name} REVIEWER's non-briefing input must be the "
            f"EXECUTOR's raw receipt (id={raw['id']}); got: {other_inputs}",
        )

        # Extract: pending, produced_by_template == template_name.
        extract = reviewer_outputs[0]
        self.assert_that(
            extract["produced_by_template"] == template_name,
            f"{tool_name} REVIEWER extract must have produced_by_template="
            f"{template_name!r}; got: {extract['produced_by_template']!r}",
        )
        self.assert_that(
            extract["template_verdict"] == "pending",
            f"{tool_name} REVIEWER extract must have template_verdict="
            f"'pending' (JUDGE hasn't run yet); got: "
            f"{extract['template_verdict']!r}",
        )
        self._created_resource_ids.append(extract["id"])

        # ── Work queue: an arc.dispatch entry for the EXECUTOR
        work_rows = self._work_queue_for_arc(db, executor["id"])
        self.assert_that(
            len(work_rows) >= 1,
            f"{tool_name} must enqueue arc.dispatch for EXECUTOR "
            f"id={executor['id']} (idempotency_key="
            f"'arc_dispatch:{executor['id']}'); work rows: {work_rows}",
        )

        # Track everything for cleanup
        self._created_arc_ids.extend([
            parent_id, executor["id"], reviewer["id"], judge["id"],
        ])
        return parent_id

    # ------------------------------------------------------------------
    # Main story
    # ------------------------------------------------------------------

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        self.assert_that(db is not None, "DB inspector required")
        _ = time.time()  # not used; cleanup is id-driven

        pkg_dir = _find_email_package()
        if pkg_dir is None:
            import pytest
            pytest.skip(
                "carpenter-gmail package source not found.  "
                "Set CARPENTER_PACKAGES_DIR or check out the package at "
                "~/repos/carpenter-packages/packages.  Checked: "
                + ", ".join(c for c in _PACKAGES_DIR_CANDIDATES if c)
            )

        # Load the package's chat-tool entrypoints via the same loader
        # the platform uses at install time.  We do NOT re-assert the
        # package's own manifest / decorator / precondition contracts
        # here — the package's own ``::manifest_shape``,
        # ``::chat_tool_registry``, and ``::judge_handlers_accept_reject``
        # stories own those.
        tools = self._load_package_tools(pkg_dir)
        for name in (
            "pkg_gmail_archive_email",
            "pkg_gmail_mark_read_email",
            "pkg_gmail_draft_email",
        ):
            self.assert_that(
                getattr(tools, name, None) is not None,
                f"Expected chat tool {name!r} on tools module",
            )

        # Seed the allowlist so draft_email reaches arc construction.
        # Operator email is the in-script expected_account; the chat
        # boundary refuses to construct arcs without it.
        self._seed_email_allowlist()

        # ── 1. archive_email builds the full 4-arc tree ─────────────────
        print("\n  [1/3] Verifying archive_email builds full 4-arc tree...")
        self._set_operator_email("ben@example.com")
        try:
            archive_parent = self._assert_full_pipeline(
                db=db,
                tool_name="pkg_gmail_archive_email",
                tool_fn=tools.pkg_gmail_archive_email,
                tool_input={"provider_message_id": "msg-archive-001"},
                expected_staged_to=[],
            )
            print(
                f"     archive_email OK (parent arc id={archive_parent}, "
                f"4-arc tree intact)"
            )
        finally:
            self._restore_operator_email()

        # ── 2. mark_read_email builds the full 4-arc tree ───────────────
        print("  [2/3] Verifying mark_read_email builds full 4-arc tree...")
        self._set_operator_email("ben@example.com")
        try:
            mark_read_parent = self._assert_full_pipeline(
                db=db,
                tool_name="pkg_gmail_mark_read_email",
                tool_fn=tools.pkg_gmail_mark_read_email,
                tool_input={"provider_message_id": "msg-markread-001"},
                expected_staged_to=[],
            )
            print(
                f"     mark_read_email OK (parent arc id="
                f"{mark_read_parent}, 4-arc tree intact)"
            )
        finally:
            self._restore_operator_email()

        # ── 3. draft_email builds the full 4-arc tree ───────────────────
        print("  [3/3] Verifying draft_email builds full 4-arc tree...")
        self._set_operator_email("ben@example.com")
        try:
            draft_parent = self._assert_full_pipeline(
                db=db,
                tool_name="pkg_gmail_draft_email",
                tool_fn=tools.pkg_gmail_draft_email,
                tool_input={
                    "to": [self._allowlisted_email],
                    "subject": "phase 1.5 v2 acceptance",
                    "body": (
                        "Acceptance-test draft.  Verifies that the chat "
                        "tool wires up a full 4-arc tree (PLANNER + "
                        "EXECUTOR + REVIEWER + JUDGE) with Resource "
                        "provenance set so the JUDGE can graduate the "
                        "extract."
                    ),
                },
                expected_staged_to=[self._allowlisted_email],
            )
            print(
                f"     draft_email OK (parent arc id={draft_parent}, "
                f"4-arc tree intact)"
            )
        finally:
            self._restore_operator_email()

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                "archive/mark_read/draft each construct the full "
                "PLANNER + EXECUTOR(untrusted) + REVIEWER + JUDGE "
                "4-arc tree with correct Resource provenance "
                "(briefing born-trusted, raw receipt untrusted, "
                "extract pending), arc-state seeding, and a "
                "work-queue arc.dispatch entry for the EXECUTOR."
            ),
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        # Restore policies / config first so the daemon is left clean
        # regardless of what happened in run().
        try:
            self._clear_email_allowlist()
        except Exception as exc:  # noqa: BLE001
            print(f"  [cleanup] allowlist clear error: {exc}")
        try:
            self._restore_operator_email()
        except Exception as exc:  # noqa: BLE001
            print(f"  [cleanup] operator_email restore error: {exc}")

        if db is None:
            return

        conn = sqlite3.connect(db.db_path)
        try:
            deleted_arcs: list[str] = []
            for arc_id in self._created_arc_ids:
                conn.execute(
                    "DELETE FROM arc_state WHERE arc_id = ?", (arc_id,),
                )
                conn.execute(
                    "DELETE FROM arc_history WHERE arc_id = ?", (arc_id,),
                )
                conn.execute(
                    "DELETE FROM arc_resources WHERE arc_id = ?", (arc_id,),
                )
                conn.execute(
                    "DELETE FROM conversation_arcs WHERE arc_id = ?",
                    (arc_id,),
                )
                conn.execute(
                    "DELETE FROM work_queue WHERE "
                    "idempotency_key = ? OR idempotency_key = ?",
                    (
                        f"arc_dispatch:{arc_id}",
                        f"arc.dispatch:{arc_id}",
                    ),
                )
                conn.execute(
                    "DELETE FROM arcs WHERE id = ?", (arc_id,),
                )
                deleted_arcs.append(str(arc_id))

            deleted_resources: list[str] = []
            for rid in self._created_resource_ids:
                conn.execute(
                    "DELETE FROM arc_resources WHERE resource_id = ?",
                    (rid,),
                )
                conn.execute(
                    "DELETE FROM resources WHERE id = ?", (rid,),
                )
                deleted_resources.append(str(rid))

            conn.commit()
            if deleted_arcs:
                print(
                    f"  [cleanup] Removed arc rows: "
                    f"{', '.join(deleted_arcs)}"
                )
            if deleted_resources:
                print(
                    f"  [cleanup] Removed resource rows: "
                    f"{', '.join(deleted_resources)}"
                )
        except Exception as exc:  # noqa: BLE001
            print(f"  [cleanup] DB cleanup error: {exc}")
        finally:
            conn.close()
