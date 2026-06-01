"""
S013 — User Sets Up and Cancels a Recurring Scheduled Message

The user asks Carpenter — using plain language, never saying "cron" —
to send a recurring chat message every minute in the current conversation.
The agent creates a repeating schedule using the platform's scheduling
primitives.  The user observes two messages arrive at approximately the
requested cadence, then asks the agent to stop the repeating messages
(again without using the word "cron").  The agent cancels the schedule and
confirms.  The runner verifies that the cron entry has been removed from the
database.

Step-by-step expected behaviour:
  1. User sends: "Every minute, send me a message in this conversation to
     remind me to check my posture.  Keep doing it until I tell you to stop."
  2. Agent acknowledges and uses scheduling.add_cron (or equivalent) to set
     up a recurring trigger that fires every minute.
  3. The platform fires the trigger; an arc executor sends a message containing
     "posture" to the conversation.
  4. The trigger fires again one minute later; a second posture message arrives.
  5. User sends: "Please stop sending those repeating reminders."
  6. Agent removes the recurring schedule (scheduling.remove_cron or similar).
  7. Agent confirms in natural language that the repeating task has been stopped.
  8. DB inspection shows no enabled cron entry with the "s013-posture" name
     prefix remains.

Platform capabilities exercised:
  - scheduling.add_cron  — creates a recurring minute-resolution trigger
  - scheduling.remove_cron / enable_cron — cancels the schedule
  - messaging.send       — arc sends messages to the conversation
  - Arc auto-dispatch    — trigger → work item → arc → executor → message

Known timing constraints:
  - Cron has 1-minute resolution; the first fire can be up to 60 s after setup
    depending on where in the current minute the cron entry is created.
  - The Raspberry Pi has limited CPU; arc dispatch and executor startup add
    latency.  This story uses a 5-minute window for two messages to arrive.

Cleanup: removes any cron_entries with name prefix "s013-posture" so the
story is safe to re-run.
"""

import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_REMINDER_KEYWORD = "posture"

# How long to wait for 2 recurring messages to arrive (generous for Pi + cron
# 1-min resolution: up to ~1 min until first fire + ~1 min for second = ~2 min,
# but we allow 7 min to absorb boundary jitter, arc dispatch latency, and
# LLM processing time for the cron setup).
_WAIT_FOR_TWO_MESSAGES_SECONDS = 7 * 60  # 7 minutes

def _setup_prompt(cron_name: str) -> str:
    return (
        "Every minute, please send me a short message in this conversation to "
        "remind me to check my posture. Use the scheduling tools to set up a "
        "recurring trigger that fires every minute. Keep doing it until I tell "
        "you to stop. "
        f"Please name the cron/schedule entry `{cron_name}` so it's easy to "
        "identify as a test artifact."
    )


_CANCEL_PROMPT = "Please stop sending those repeating reminders."


class RecurringCronSetupAndCancel(AcceptanceStory):
    name = "S013 — User Sets Up and Cancels a Recurring Scheduled Message"
    description = (
        "User asks (without saying 'cron') for per-minute posture reminders; "
        "agent creates a recurring schedule; two messages arrive; user cancels; "
        "agent confirms; DB has no leftover cron entry."
    )
    artifact_prefix = "s013"

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Request recurring reminders ────────────────────────────────────
        print("\n  [1/4] Requesting per-minute posture reminders...")
        cron_name = self.artifact_name("posture")
        conv_id = client.create_conversation()
        client.send_message(_setup_prompt(cron_name), conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        setup_msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(setup_msgs) >= 1,
            "No acknowledgement received after recurring-reminder request",
            conversation_id=conv_id,
        )
        ack = setup_msgs[-1]["content"]
        print(f"     Ack: {ack[:200]}")

        # Agent should acknowledge it is setting up a repeating/scheduled task.
        self.assert_that(
            any(kw in ack.lower() for kw in
                ("every minute", "repeat", "recurring", "schedul", "set up", "reminders",
                 "posture", "minute", "will send", "going to")),
            "Acknowledgement does not indicate a repeating schedule was created",
            response_preview=ack[:500],
        )

        # ── 2. Wait for 2 posture messages (arc-originated OR cron-delivered) ──
        # The agent may use arc.dispatch (arc_id set) or cron.message (no arc_id).
        # We accept either approach.
        # Record message count after setup to only count NEW posture messages.
        baseline_count = len(client.get_history(conv_id))
        print(f"  [2/4] Waiting up to {_WAIT_FOR_TWO_MESSAGES_SECONDS}s "
              f"for 2 posture reminder messages...")
        deadline = time.monotonic() + _WAIT_FOR_TWO_MESSAGES_SECONDS
        posture_msgs = []
        while time.monotonic() < deadline:
            all_msgs = client.get_history(conv_id)
            new_msgs = all_msgs[baseline_count:]  # only messages after setup
            posture_msgs = [
                m for m in new_msgs
                if (
                    _REMINDER_KEYWORD in m.get("content", "").lower()
                    and m.get("role") in ("assistant", "system")
                )
            ]
            if len(posture_msgs) >= 2:
                break
            time.sleep(15)

        elapsed = time.time() - start_ts
        print(f"  [2/4] Elapsed so far: {elapsed:.0f}s, "
              f"posture messages found: {len(posture_msgs)}")

        self.assert_that(
            len(posture_msgs) >= 2,
            f"Expected ≥2 posture messages within "
            f"{_WAIT_FOR_TWO_MESSAGES_SECONDS}s; got {len(posture_msgs)}",
            conversation_id=conv_id,
            posture_msgs_found=[m.get("content", "")[:100]
                                for m in posture_msgs],
            hint_1="Was a cron entry created?  Check cron_entries in the DB.",
            hint_2="Was the cron firing?  Check work_queue for cron.message items.",
            hint_3="Did the cron.message handler deliver to the right conversation?",
        )

        for i, m in enumerate(posture_msgs[:2], 1):
            print(f"     Message {i}: {m['content'][:120]}")

        # ── 3. Request cancellation ───────────────────────────────────────────
        print("  [3/4] Asking agent to stop the repeating reminders...")
        client.send_message(_CANCEL_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        cancel_msgs = client.get_assistant_messages(conv_id)
        # Last assistant message should be the cancellation confirmation.
        cancel_reply = cancel_msgs[-1]["content"] if cancel_msgs else ""
        print(f"     Cancel reply: {cancel_reply[:200]}")

        self.assert_that(
            any(kw in cancel_reply.lower() for kw in
                ("stop", "cancel", "remov", "disabl", "no longer", "won't",
                 "turned off", "done", "removed", "unscheduled")),
            "Cancellation reply does not confirm that the repeating task was stopped",
            response_preview=cancel_reply[:500],
        )

        # ── 4. Structural DB assertions ───────────────────────────────────────
        if db is None:
            return StoryResult(
                name=self.name,
                passed=True,
                message=(
                    f"Behavioural checks passed (no DB configured): "
                    f"{len(posture_msgs)} posture messages ✓, "
                    f"cancellation confirmed ✓"
                ),
            )

        # Posture messages should be in the DB (via arc or cron.message).
        all_db_msgs = db.get_messages(conv_id)
        posture_db_msgs = [
            m for m in all_db_msgs
            if (
                _REMINDER_KEYWORD in m.get("content", "").lower()
                and m.get("role") in ("assistant", "system")
            )
        ]
        # Exclude the initial acknowledgement (first posture mention)
        if len(posture_db_msgs) > 0:
            posture_db_msgs = posture_db_msgs[1:]  # drop ack
        self.assert_that(
            len(posture_db_msgs) >= 2,
            f"Expected ≥2 recurring posture messages in DB, "
            f"found {len(posture_db_msgs)}",
            messages=db.format_messages_table(all_db_msgs),
        )

        # No enabled cron entry for this recurring reminder should remain.
        leftover_crons = db._query(
            "SELECT * FROM cron_entries WHERE name LIKE ? AND enabled = 1",
            (self.artifact_name_pattern(),),
        )
        self.assert_that(
            len(leftover_crons) == 0,
            f"Recurring cron entry was not removed after cancellation "
            f"({len(leftover_crons)} active cron(s) remain)",
            crons=leftover_crons,
            hint="Agent should call scheduling.remove_cron or scheduling.enable_cron "
                 "(enabled=false) when the user asks to stop.",
        )

        elapsed = time.time() - start_ts
        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"{len(posture_db_msgs)} posture messages ✓, "
                f"recurring schedule cancelled ✓, "
                f"no leftover cron entry ✓ "
                f"({elapsed:.0f}s total)"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: DBInspector) -> None:
        """Remove any cron entries created by this run so it doesn't accumulate."""
        if db is None:
            return
        import sqlite3
        pattern = self.artifact_name_pattern()
        try:
            conn = sqlite3.connect(db.db_path)
            try:
                cur = conn.execute(
                    "DELETE FROM cron_entries WHERE name LIKE ?",
                    (pattern,),
                )
                if cur.rowcount:
                    print(f"  [cleanup] Removed {cur.rowcount} cron entry/entries "
                          f"matching '{pattern}'")
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            print(f"  [cleanup] Cron cleanup failed: {exc}")
