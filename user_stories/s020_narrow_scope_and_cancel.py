"""
S020 — Narrow Scope and Cancel Non-Relevant Arcs

The user asks the agent to plan a multi-step project, then narrows the
request to just the auth component. The agent cancels the non-auth
arcs with cascade.

Expected behaviour:
  1. User asks agent to PLAN an 8-step project (mostly non-auth).
  2. Agent creates ~8 arcs (one per step).
  3. As soon as the arcs appear in the DB, the user narrows to "auth only".
  4. Agent calls arc.cancel on the non-auth arcs.
  5. At least one arc lands in cancelled status.

DB verification:
  - >=3 arcs created within the run window.
  - At least one arc has status='cancelled' after the narrow lands.

Race-avoidance strategy:
  - We request MANY steps (8) so the work cannot all finish in the time
    it takes the chat agent to process the narrowing prompt.  With the
    default ``max_concurrent_handlers = 4`` and ~5-15s per planning arc
    on haiku, an 8-arc batch needs >=2 dispatch waves.  By the time the
    narrow is processed, several arcs are guaranteed to still be in
    pending/active status.
  - We send the narrowing prompt IMMEDIATELY after we observe arcs in
    the DB (no fixed sleep) — DB polling, not message polling, since
    these planning arcs don't necessarily emit visible assistant messages
    we can wait for.
  - The narrow prompt names the specific arc IDs to cancel (read from
    the DB) so the agent doesn't have to guess.

Note: explicitly PLANNING only — no code artifacts.
"""

import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

# Eight planning steps — one auth (to keep), seven others (to cancel).
# Each step asks for a "detailed" design with multiple sub-considerations
# so individual arcs take meaningful time (not just a 1-line reply).
_BIG_PROJECT_PROMPT = (
    "Please create an 8-step planning workflow for designing a REST API "
    "system. Spawn one separate arc per step. Each arc should write a "
    "detailed design write-up (cover at least 5 sub-points per step). "
    "The steps are:\n"
    "Step 1: Design JWT authentication (login, token refresh, revocation).\n"
    "Step 2: Design CRUD endpoints for a 'products' resource.\n"
    "Step 3: Design email notifications for new product creation.\n"
    "Step 4: Design rate-limiting middleware.\n"
    "Step 5: Design pagination and filtering for list endpoints.\n"
    "Step 6: Design caching strategy for hot-path reads.\n"
    "Step 7: Design audit logging for write endpoints.\n"
    "Step 8: Design API versioning approach.\n"
    "Spawn all 8 arcs in a single batch, then return immediately so I "
    "can review the plan while they run."
)


_PRE_SWEEP_GRACE = 0.5  # seconds — minimal pause for cancel to commit
_AGENT_WORK_TIMEOUT = 120  # seconds, for wait_for_pending_to_clear


class NarrowScopeAndCancel(AcceptanceStory):
    name = "S020 — Narrow Scope and Cancel Non-Relevant Arcs"
    description = (
        "User requests big multi-part project, then narrows to just auth "
        "while arcs are still in flight; agent cancels non-auth arcs; "
        "verifies cancelled status."
    )
    artifact_prefix = "s020"
    timeout = 300

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_new_arcs(db: DBInspector, start_ts: float) -> list[dict]:
        """Return arcs created after start_ts, excluding the always-present
        reflection / dispatch-actions / save-reflection / gather-activity
        subtrees that the platform auto-spawns for completed arcs.
        We're interested in the user-spawned planning arcs only."""
        arcs = db.get_arcs_created_after(start_ts)
        noise_names = {
            "reflection",
            "gather-activity",
            "reflect",
            "save-reflection",
            "dispatch-actions",
        }
        return [a for a in arcs if a.get("name") not in noise_names]

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Request big project ──────────────────────────────────────
        print("\n  [1/4] Requesting 8-step REST API planning project...")
        conv_id = client.create_conversation()
        client.send_message(_BIG_PROJECT_PROMPT, conv_id)
        client.wait_for_pending_to_clear(
            conv_id, timeout=_AGENT_WORK_TIMEOUT,
        )

        msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(msgs) >= 1,
            "No response after project request",
            conversation_id=conv_id,
        )
        plan_resp = msgs[-1]["content"]
        print(f"     {plan_resp[:200]}")

        # Verify the agent acknowledged a multi-step plan.  Accept
        # generic plan-vocabulary, not specific subject words, so the
        # check stays robust to phrasing changes.
        self.assert_that(
            any(kw in plan_resp.lower() for kw in
                ("auth", "step", "workflow", "arc", "spawn", "plan",
                 "created", "8", "eight")),
            "Planning response does not acknowledge multi-step project",
            response_preview=plan_resp[:400],
        )

        # ── 2. Wait for arcs to appear in DB ────────────────────────────
        # The chat agent has just finished its turn (pending cleared),
        # which means it has executed its arc-creation code.  Poll the
        # DB briefly to confirm the user-spawned arcs are now there.
        # We don't wait for them to START running — we just need them
        # to EXIST so we can list their IDs in the narrow prompt.
        print("  [2/4] Polling DB for spawned arcs (up to 20s)...")
        deadline = time.monotonic() + 20
        user_arcs: list[dict] = []
        while time.monotonic() < deadline:
            user_arcs = self._get_new_arcs(db, start_ts)
            # Look for top-level (no parent) planning arcs.  The agent's
            # spawned arcs are sibling roots, not children of the chat.
            top_level = [a for a in user_arcs if not a.get("parent_id")]
            if len(top_level) >= 3:
                user_arcs = top_level
                break
            time.sleep(1)

        self.assert_that(
            len(user_arcs) >= 3,
            f"Expected agent to spawn >=3 top-level planning arcs, "
            f"found {len(user_arcs)} within 20s",
            arcs=db.format_arcs_table(user_arcs) if user_arcs
                else "  (none)",
        )
        print(
            f"     Found {len(user_arcs)} user-spawned arcs: "
            f"{[(a['id'], a['name'][:30], a['status']) for a in user_arcs]}"
        )

        # ── 3. Narrow scope IMMEDIATELY ─────────────────────────────────
        # Build a narrow prompt that names the specific arc IDs to
        # cancel.  We identify the auth arc heuristically by name; if
        # we can't, we conservatively keep the first one and cancel
        # the rest (auth is step 1 in the prompt).
        auth_arc = next(
            (a for a in user_arcs
             if any(kw in (a.get("name") or "").lower()
                    for kw in ("auth", "jwt", "login", "token"))),
            user_arcs[0],
        )
        to_cancel = [a for a in user_arcs if a["id"] != auth_arc["id"]]

        # Cap at first 6 to keep the prompt short; cancelling >=1 is
        # what the assertion checks.
        cancel_ids = [a["id"] for a in to_cancel[:6]]
        print(
            f"  [3/4] Narrowing scope to auth (#{auth_arc['id']}); "
            f"cancelling: {cancel_ids}"
        )

        narrow_prompt = (
            "Actually, let's just focus on planning the authentication "
            "part for now. Please cancel the non-auth arcs by calling "
            "`arc.cancel(arc_id)` on each of these specific arc IDs: "
            f"{cancel_ids}. Keep arc #{auth_arc['id']} (auth) running. "
            "Run the cancel calls in a single submit_code block so they "
            "execute promptly."
        )

        client.send_message(narrow_prompt, conv_id)
        client.wait_for_pending_to_clear(
            conv_id, timeout=_AGENT_WORK_TIMEOUT,
        )

        narrow_msgs = client.get_assistant_messages(conv_id)
        narrow_resp = narrow_msgs[-1]["content"]
        print(f"     {narrow_resp[:200]}")

        self.assert_that(
            any(kw in narrow_resp.lower() for kw in
                ("cancel", "focus", "auth", "narrow", "only", "stop",
                 "remaining", "removed", "done")),
            "Narrowing response does not acknowledge scope change",
            response_preview=narrow_resp[:400],
        )

        # Tiny grace for the cancel commit to be visible.
        time.sleep(_PRE_SWEEP_GRACE)

        # ── 4. DB assertions ───────────────────────────────────────────
        if db is None:
            return StoryResult(
                name=self.name,
                passed=True,
                message="Behavioural checks passed (no DB configured)",
            )

        print("  [4/4] Verifying arc cancellation in DB...")
        new_arcs = self._get_new_arcs(db, start_ts)
        print(f"     {len(new_arcs)} non-noise arcs created")

        cancelled = [a for a in new_arcs if a["status"] == "cancelled"]
        self.assert_that(
            len(cancelled) >= 1,
            f"Expected at least 1 cancelled arc after narrowing, "
            f"found {len(cancelled)}. (Agent named "
            f"{cancel_ids} as targets.)",
            arcs=db.format_arcs_table(new_arcs),
            cancel_ids_requested=cancel_ids,
            narrow_response=narrow_resp[:300],
        )
        print(f"     {len(cancelled)} arc(s) cancelled ✓")

        # Auth arc (or some arc) should be completed or still active.
        # We don't strictly require the *auth* one to be in any
        # specific state — the platform can finish or still be running
        # it — we just want SOMETHING to have made progress.
        non_cancelled = [
            a for a in new_arcs if a["status"] != "cancelled"
        ]
        self.assert_that(
            len(non_cancelled) >= 1,
            f"Expected at least 1 non-cancelled arc (agent should "
            f"have preserved auth), found {len(non_cancelled)}",
            arcs=db.format_arcs_table(new_arcs),
        )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"{len(new_arcs)} user arcs, "
                f"{len(cancelled)} cancelled ✓, "
                f"{len(non_cancelled)} preserved ✓"
            ),
        )
