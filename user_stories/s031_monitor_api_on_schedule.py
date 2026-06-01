"""
S031 — Monitor API Endpoint on Schedule

The user asks Carpenter to check httpbin.org/status/200 every 30 seconds
and notify if the status changes. The agent sets up a cron schedule.
After a wait, the story verifies via DB state that the cron exists and
has fired, then cancels and verifies cleanup.

Pattern: follows s013's cron setup/cancel pattern. Verification is
DB-state-driven (events table, cron_entries table) rather than polling
chat messages, because cron-delivered messages share role='assistant'
with the agent's own replies and would race the agent's cancel-ack.

Expected behaviour:
  1. User: "check httpbin.org/status/200 every 30 seconds, tell me if
     status changes."
  2. Agent creates a cron entry that fires recurringly.
  3. >=2 timer.fired events for that cron land in the events table
     within the wait window (deterministic DB read).
  4. User cancels the monitoring.
  5. Agent removes (or disables) the cron entry.

Cleanup: removes any monitoring cron entries created by this run.
"""

import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_CRON_NAME_PREFIX = "s031-monitor"

_SETUP_PROMPT = (
    "Please monitor the API endpoint https://httpbin.org/status/200 "
    "every 30 seconds. Check its status and send me a message in this "
    "conversation each time you check. Let me know if anything changes. "
    "When you create the recurring schedule, please name the cron entry "
    "starting with `s031-monitor` (for example `s031-monitor-httpbin`) "
    "so it's easy to identify as a test artifact."
)

_CANCEL_PROMPT = (
    "Please stop monitoring that API endpoint. Cancel the recurring check."
)

# Cron's minimum granularity is 1 minute. We need >=2 fires AFTER the
# cron is created. Setup typically takes 10-40s but can take up to ~90s
# when the agent's first attempt is rejected by the verification arc
# and it has to retry. We capture wait_start_ts BEFORE sending the
# setup prompt so the entire window counts, and pad the wait window
# with enough slack to absorb a retry plus two cron boundaries
# (worst case: ~90s setup + 60s for fire #1 + 60s for fire #2 = 210s).
_WAIT_SECONDS = 220


class MonitorAPIOnSchedule(AcceptanceStory):
    name = "S031 — Monitor API Endpoint on Schedule"
    description = (
        "User asks agent to check httpbin every 30 seconds; cron "
        "verified via DB (timer.fired events); cancel removes cron."
    )
    timeout = 300

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _list_story_crons(db: DBInspector) -> list[dict]:
        """Return all cron rows belonging to this story.

        Uses a SINGLE prefix query (no UNION with a '%httpbin%' clause)
        so a cron named ``s031-monitor-httpbin`` is counted once, not
        twice. Story owns the ``s031-monitor`` prefix; any leftover
        httpbin cron without that prefix is somebody else's problem.
        """
        return db._query(
            "SELECT * FROM cron_entries WHERE name LIKE ?",
            (f"{_CRON_NAME_PREFIX}%",),
        )

    @staticmethod
    def _sweep_story_crons(db_path: str, label: str) -> int:
        """Delete all cron rows with the story prefix. Returns row count."""
        import sqlite3
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute(
                "DELETE FROM cron_entries WHERE name LIKE ?",
                (f"{_CRON_NAME_PREFIX}%",),
            )
            conn.commit()
            if cur.rowcount:
                print(
                    f"  [{label}] Removed {cur.rowcount} cron(s) "
                    f"matching '{_CRON_NAME_PREFIX}%'"
                )
            return cur.rowcount or 0
        finally:
            conn.close()

    @staticmethod
    def _count_cron_fires(
        db: DBInspector, cron_names: list[str], since_ts: float,
    ) -> int:
        """Count distinct timer.fired events for the given cron names.

        Reads the events table directly. Events emitted by
        ``trigger_manager.check_cron()`` carry ``source = 'cron:<name>'``
        and a unique ``idempotency_key``, so this count is
        deterministic — no message-polling race.
        """
        if not cron_names:
            return 0
        from datetime import datetime, timezone
        since_iso = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        placeholders = ",".join("?" * len(cron_names))
        sources = [f"cron:{n}" for n in cron_names]
        rows = db._query(
            f"SELECT COUNT(*) AS n FROM events "
            f"WHERE event_type = 'timer.fired' "
            f"AND source IN ({placeholders}) "
            f"AND created_at >= ?",
            (*sources, since_iso),
        )
        return rows[0]["n"] if rows else 0

    # ------------------------------------------------------------------
    # Main story
    # ------------------------------------------------------------------

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 0. Pre-run sweep ──────────────────────────────────────────
        # A prior failed run can leave a stale ``s031-monitor*`` cron
        # behind. Sweep so each run starts clean (also prevents the
        # platform-level idempotent upsert from picking up a stale
        # entry's params).
        if db is not None:
            try:
                self._sweep_story_crons(db.db_path, "pre-sweep")
            except Exception as exc:
                print(f"  [pre-sweep] Cron pre-sweep failed: {exc}")

        # ── 1. Set up monitoring ──────────────────────────────────────
        print("\n  [1/4] Setting up API monitoring...")
        conv_id = client.create_conversation()
        # Capture wait_start_ts BEFORE sending the setup prompt so the
        # fire-count query covers the entire test run. The pre-sweep
        # above removed any pre-existing s031-monitor cron, so events
        # with source='cron:s031-monitor*' emitted before this moment
        # cannot originate from this run.
        wait_start_ts = time.time()
        client.send_message(_SETUP_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=120)

        setup_msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(setup_msgs) >= 1,
            "No acknowledgement after monitoring setup request",
            conversation_id=conv_id,
        )
        ack = setup_msgs[-1]["content"]
        print(f"     Ack: {ack[:200]}")

        self.assert_that(
            any(kw in ack.lower() for kw in
                ("monitor", "check", "every", "30 second", "schedule",
                 "httpbin", "recurring", "set up", "will check")),
            "Acknowledgement does not indicate monitoring was set up",
            response_preview=ack[:500],
        )

        # ── 1b. Verify cron entry exists in DB (deterministic) ────────
        if db is not None:
            crons = self._list_story_crons(db)
            self.assert_that(
                len(crons) >= 1,
                f"Agent did not create a cron with prefix "
                f"'{_CRON_NAME_PREFIX}' after setup request",
                conversation_id=conv_id,
                ack_preview=ack[:300],
            )
            print(
                f"     Cron(s) created: "
                f"{[(c['name'], c['cron_expr']) for c in crons]}"
            )

        # ── 2. Wait for cron fires (DB-state verification) ────────────
        print(
            f"  [2/4] Waiting up to {_WAIT_SECONDS}s for "
            f">=2 cron fires (events table)..."
        )

        deadline = time.monotonic() + _WAIT_SECONDS
        fires_seen = 0
        while time.monotonic() < deadline:
            if db is not None:
                crons = self._list_story_crons(db)
                cron_names = [c["name"] for c in crons]
                fires_seen = self._count_cron_fires(
                    db, cron_names, wait_start_ts
                )
                if fires_seen >= 2:
                    break
            time.sleep(15)

        elapsed = time.time() - start_ts
        print(f"     Elapsed: {elapsed:.0f}s, cron fires observed: {fires_seen}")

        if db is not None:
            self.assert_that(
                fires_seen >= 2,
                f"Expected >=2 cron fires within {_WAIT_SECONDS}s, "
                f"got {fires_seen}",
                conversation_id=conv_id,
            )

        # ── 3. Cancel monitoring ──────────────────────────────────────
        print("  [3/4] Cancelling monitoring...")

        # Take a baseline of assistant messages BEFORE sending the
        # cancel prompt so we can isolate the agent's cancel reply from
        # any cron-delivered messages that arrive concurrently.
        pre_cancel_count = len(client.get_assistant_messages(conv_id))

        client.send_message(_CANCEL_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=120)

        post_cancel_msgs = client.get_assistant_messages(conv_id)
        new_after_cancel = post_cancel_msgs[pre_cancel_count:]

        # The agent's actual cancel reply is the LAST new assistant
        # message after the cancel prompt was sent. Cron-delivered
        # messages that interleave are also in this slice, but the
        # agent's reply lands after wait_for_pending_to_clear returns,
        # which means it's the most recent one.
        cancel_reply = (
            new_after_cancel[-1]["content"] if new_after_cancel else ""
        )
        print(f"     Cancel reply: {cancel_reply[:200]}")

        # The agent's reply should acknowledge cancellation. Some cron
        # messages can land in the same slice though, so if the last
        # message looks like a cron status report rather than a cancel
        # ack, fall back to DB verification: was the cron removed?
        ack_looks_like_cancel = any(
            kw in cancel_reply.lower() for kw in
            ("stop", "cancel", "remov", "no longer", "done", "disabled")
        )

        # ── 4. DB assertions ──────────────────────────────────────────
        if db is None:
            self.assert_that(
                ack_looks_like_cancel,
                "Cancel reply does not confirm monitoring stopped "
                "(no DB available for fallback verification)",
                response_preview=cancel_reply[:500],
            )
            return StoryResult(
                name=self.name,
                passed=True,
                message=(
                    f"Behavioural: {fires_seen} fires ✓, cancelled ✓"
                ),
            )

        print("  [4/4] Verifying cron cleanup in DB...")
        # Give the agent's remove_cron tool call time to commit if
        # it's still in flight (cron handler is in the conv loop, so
        # it should already be done by the time pending cleared, but
        # be defensive against eventual consistency).
        leftover = []
        for _ in range(6):
            leftover = db._query(
                "SELECT * FROM cron_entries WHERE name LIKE ? "
                "AND enabled = 1",
                (f"{_CRON_NAME_PREFIX}%",),
            )
            if not leftover:
                break
            time.sleep(2)

        self.assert_that(
            len(leftover) == 0,
            f"Monitoring cron still active ({len(leftover)} entries) "
            f"after cancel request",
            crons=leftover,
            cancel_reply_preview=cancel_reply[:300],
        )

        # If the textual ack didn't look like a cancel but the cron
        # *was* in fact removed, accept the DB evidence — the agent
        # did the right thing even if its prose was ambiguous.
        if not ack_looks_like_cancel:
            print(
                "     [note] Cancel reply was ambiguous but the cron "
                "was removed; accepting DB evidence."
            )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"{fires_seen} cron fires ✓, "
                f"cancelled ✓, "
                f"no leftover cron ✓ "
                f"({elapsed:.0f}s total)"
            ),
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Remove any monitoring cron entries this story created."""
        if db is None:
            return
        try:
            self._sweep_story_crons(db.db_path, "cleanup")
        except Exception as exc:
            print(f"  [cleanup] Cron cleanup failed: {exc}")
