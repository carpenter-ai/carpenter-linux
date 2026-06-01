"""
S028 — Reflection Proposes Schedule

Day 1: user asks "what's the weather like today?" Day 2 (simulated):
user asks the same question again. The reflection system runs, identifies
the repeated pattern, and proposes a scheduled weather check at 6:30am
via proposed_action.

Expected behaviour:
  1. User asks about weather in conversation A.
  2. User asks about weather in conversation B (separate conv).
  3. The agent (or reflection system) recognises the pattern.
  4. A proposed_action or schedule suggestion appears — either the agent
     proactively suggests scheduling, or the reflection creates a
     proposal for a 6:30am cron.
  5. The response mentions scheduling or recurring.

Cleanup: removes any cron entries created.

DB verification:
  - Two conversations with weather queries.
  - Agent suggests or creates a recurring schedule.
"""

import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_WEATHER_PROMPT_1 = (
    "What's the weather like today? I always check the weather first thing "
    "in the morning."
)

_WEATHER_PROMPT_2 = (
    "What's the weather like today? I find myself asking you this every "
    "morning. Is there a way you could just tell me automatically? "
    "If you do set up a scheduled weather check, please name the cron "
    "entry starting with `s028-` (for example `s028-morning-weather`) "
    "so it's easy to identify as a test artifact."
)

# Cleanup only deletes cron_entries rows whose name starts with this prefix.
# Previously this story matched '%weather%' which was far too broad — it
# would wipe sibling test runs' weather crons and any user schedule that
# happened to mention weather.
_CRON_NAME_PREFIX = "s028-"


class ReflectionProposesSchedule(AcceptanceStory):
    name = "S028 — Reflection Proposes Schedule"
    description = (
        "User asks about weather twice across conversations; agent/reflection "
        "identifies pattern and proposes scheduled weather check at 6:30am."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. First weather question ─────────────────────────────────────────
        print("\n  [1/3] Conversation 1: First weather question...")
        conv_a, resp1 = client.chat(_WEATHER_PROMPT_1, timeout=90)
        print(f"     Conv A (id={conv_a}): {resp1[:200]}")

        self.assert_that(
            any(kw in resp1.lower() for kw in
                ("weather", "temperature", "forecast", "today", "morning")),
            "First response does not address weather",
            response_preview=resp1[:400],
        )

        # Brief pause between "days"
        time.sleep(3)

        # ── 2. Second weather question (hints at pattern) ─────────────────────
        print("  [2/3] Conversation 2: Second weather question with hint...")
        conv_b, resp2 = client.chat(_WEATHER_PROMPT_2, timeout=120)
        print(f"     Conv B (id={conv_b}): {resp2[:300]}")

        self.assert_that(
            conv_a != conv_b,
            "Expected different conversations",
        )

        # ── 3. Check for scheduling suggestion ────────────────────────────────
        print("  [3/3] Checking for schedule proposal...")
        resp2_lower = resp2.lower()

        # The agent should suggest or create a recurring schedule
        has_schedule_suggestion = any(kw in resp2_lower for kw in
            ("schedule", "recurring", "automat", "every morning",
             "every day", "cron", "6:30", "daily", "remind",
             "set up", "regular", "routine"))

        self.assert_that(
            has_schedule_suggestion,
            "Response does not suggest or mention scheduling weather checks",
            response_preview=resp2[:600],
        )

        # Check for cron entries in DB if available.
        # Scoped to our story-specific prefix so we don't confuse a sibling
        # run's or pre-existing weather cron for a story artifact.
        cron_created = False
        if db is not None:
            crons = db._query(
                "SELECT * FROM cron_entries WHERE name LIKE ? AND enabled = 1",
                (f"{_CRON_NAME_PREFIX}%",),
            )
            if crons:
                cron_created = True
                print(f"     Cron entry created: {crons[0].get('name', 'unknown')} ✓")

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Weather asked in 2 conversations ✓, "
                f"scheduling suggested ✓"
                + (", cron entry created ✓" if cron_created else "")
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Remove cron entries this story created (scoped to s028- prefix).

        The old implementation matched '%weather%' which wiped sibling-run
        artifacts and any user weather schedule.  The Phase-2 prompt now
        asks the agent to prefix its cron name with 's028-', so cleanup
        can filter safely.
        """
        if db is None:
            return
        import sqlite3
        try:
            conn = sqlite3.connect(db.db_path)
            try:
                cur = conn.execute(
                    "DELETE FROM cron_entries WHERE name LIKE ?",
                    (f"{_CRON_NAME_PREFIX}%",),
                )
                if cur.rowcount:
                    print(
                        f"  [cleanup] Removed {cur.rowcount} cron(s) "
                        f"matching '{_CRON_NAME_PREFIX}%'"
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            print(f"  [cleanup] Cron cleanup failed: {exc}")
