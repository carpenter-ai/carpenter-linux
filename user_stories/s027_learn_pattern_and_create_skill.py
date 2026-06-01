"""
S027 — Learn Pattern and Create KB Entry

Phase 1: The user walks the agent through a deploy flow manually —
"check git status, run tests, build, deploy." This establishes a pattern
in arc history.

Phase 2: The reflection system runs (or is triggered), identifying the
repeated pattern.

Phase 3: The user says "deploy again." The agent recognises the pattern
and may create a KB entry to document the flow, or at least replays the
steps from memory.

Expected behaviour:
  1. User walks through a deploy-like workflow step by step.
  2. Agent executes each step and records in arc history.
  3. User says "deploy again" in a later message.
  4. Agent recognises the pattern (possibly creating a KB entry) and
     replays the steps or indicates KB entry creation.

Cleanup: removes any KB entries created during the test.

DB verification:
  - Arc history shows the deploy steps.
  - Agent's "deploy again" response references prior steps or a KB entry.
"""

import time
import sqlite3

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_STEP1_PROMPT = (
    "Let's practice a manual deploy workflow. Step 1: check the git status "
    "of the carpenter-core repo. Tell me what you see."
)

_STEP2_PROMPT = (
    "Good, that was step 1. Now step 2 of the deploy workflow: describe "
    "what running the test suite would involve. Don't actually run tests — "
    "just explain the testing step in a deploy process (e.g. pytest, test "
    "results, pass/fail outcomes)."
)

_STEP3_PROMPT = (
    "Good. Step 3 of the deploy workflow: describe the build and deploy "
    "step. Explain what building and deploying the application would "
    "involve (e.g. packaging, server restart, production deployment)."
)

_REPLAY_PROMPT = (
    "I need to deploy again. Can you repeat that same deploy workflow "
    "we just did? If you've learned the pattern, you could even create "
    "a knowledge base entry for it. If you do create a KB entry, please "
    "place it under the path prefix 's027-' (for example "
    "'s027-deploy-workflow') so it's easy to identify as a test artifact."
)

# Cleanup deletes only KB entries whose path starts with this prefix,
# so we can't wipe sibling-test or user KB rows that happen to mention "deploy".
_KB_PATH_PREFIX = "s027-"


class LearnPatternAndCreateSkill(AcceptanceStory):
    name = "S027 — Learn Pattern and Create KB Entry"
    description = (
        "User walks through deploy flow; agent learns pattern; user says "
        "'deploy again'; agent replays or creates KB entry. Cleanup removes KB entry."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── Phase 1: Walk through deploy flow ─────────────────────────────────
        print("\n  [1/4] Phase 1: Walking through deploy steps...")
        conv_id, resp1 = client.chat(_STEP1_PROMPT, timeout=180)
        print(f"     Step 1: {resp1[:150]}")
        self.assert_that(
            any(kw in resp1.lower() for kw in
                ("git", "status", "branch", "commit", "clean")),
            "Step 1 response does not mention git concepts",
            response_preview=resp1[:400],
        )

        _, resp2 = client.chat(_STEP2_PROMPT, conversation_id=conv_id, timeout=180)
        print(f"     Step 2: {resp2[:150]}")
        self.assert_that(
            any(kw in resp2.lower() for kw in
                ("test", "pass", "fail", "suite", "run")),
            "Step 2 response does not mention testing concepts",
            response_preview=resp2[:400],
        )

        _, resp3 = client.chat(_STEP3_PROMPT, conversation_id=conv_id, timeout=180)
        print(f"     Step 3: {resp3[:150]}")
        self.assert_that(
            any(kw in resp3.lower() for kw in
                ("build", "deploy", "release", "production", "server")),
            "Step 3 response does not mention deploy concepts",
            response_preview=resp3[:400],
        )

        # ── Phase 2: Brief pause for reflection ──────────────────────────────
        print("  [2/4] Phase 2: Allowing reflection time...")
        time.sleep(5)

        # ── Phase 3: Ask to deploy again ──────────────────────────────────────
        print("  [3/4] Phase 3: Requesting 'deploy again'...")
        _, replay_resp = client.chat(
            _REPLAY_PROMPT, conversation_id=conv_id, timeout=180
        )
        print(f"     Replay: {replay_resp[:300]}")

        replay_lower = replay_resp.lower()
        self.assert_that(
            any(kw in replay_lower for kw in
                ("deploy", "git", "test", "build", "workflow", "step",
                 "pattern", "knowledge", "kb", "repeat", "same flow")),
            "Replay response does not reference the deploy workflow",
            response_preview=replay_resp[:600],
        )

        # ── 4. Check for KB entry creation (optional) ─────────────────────────
        kb_created = False
        if db is not None:
            print("  [4/4] Checking for KB entry creation...")
            # Only look at entries the agent tagged with our prefix, so we don't
            # conflate user-authored or sibling-run KB rows with a story artifact.
            deploy_entries = db.get_kb_entries(path_prefix=_KB_PATH_PREFIX)
            if deploy_entries:
                kb_created = True
                print(f"     KB entry created: {deploy_entries[0]['path']} ✓")
            else:
                print("     No deploy KB entry created (agent may have replayed manually)")

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Deploy flow walked through ✓, "
                f"replay response references workflow ✓"
                + (", deploy KB entry created ✓" if kb_created else "")
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Remove KB entries this story created (scoped to the s027- prefix).

        Previously this matched any KB entry with 'deploy' in its path, which
        would also wipe user-authored entries and entries created by sibling
        test runs. The replay prompt now asks the agent to place any KB entry
        under the 's027-' prefix, so cleanup can filter on that prefix.
        """
        if db is None:
            return
        try:
            conn = sqlite3.connect(db.db_path)
            try:
                cur = conn.execute(
                    "DELETE FROM kb_entries WHERE path LIKE ?",
                    (f"{_KB_PATH_PREFIX}%",),
                )
                if cur.rowcount:
                    print(
                        f"  [cleanup] Removed {cur.rowcount} KB entry/entries "
                        f"matching '{_KB_PATH_PREFIX}%'"
                    )
                # Also drop any inbound kb_links pointing at the removed paths.
                conn.execute(
                    "DELETE FROM kb_links WHERE source_path LIKE ?",
                    (f"{_KB_PATH_PREFIX}%",),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            print(f"  [cleanup] KB entry cleanup failed: {exc}")
