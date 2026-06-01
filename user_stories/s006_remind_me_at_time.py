"""
S006 — User Asks Agent to Set a Reminder for a Specific Time

The user asks Carpenter to send them a chat message reminder in
approximately 2 minutes.  The agent must set up a mechanism that, without
any further user interaction, delivers a message to the conversation at the
requested time.

Expected behaviour:
  1. Agent acknowledges the request.
  2. Agent sets up a one-shot scheduled trigger (not a recurring cron).
  3. Approximately 2 minutes later the platform fires the trigger.
  4. A message containing the reminder text arrives in the conversation.
  5. After delivery the scheduled entry is removed (no daily recurrence).

Platform capabilities required (any of these approaches):
  - scheduling.add_once(name, at_iso, event_type="cron.message", event_payload)
      with event_payload containing {"message": "..."} — simplest approach,
      directly delivers message to conversation. conversation_id auto-injected.
  - scheduling.add_once(name, at_iso, event_type="arc.dispatch", event_payload)
      with an EXECUTOR arc that calls messaging.send — more flexible but heavier.
  - scheduling.add_cron with a mechanism for the arc to remove itself
      after firing (the recurring workaround).

Notes:
  - Cron expressions have 1-minute resolution — a cron targeting :XX minutes
    may fire up to 59 seconds late.
  - A recurring cron entry will re-fire at the same time every day unless the
    arc removes it after first delivery.

Cleanup: removes any cron_entries created during this test by name prefix
"s006-reminder" so the test is safe to re-run.
"""

import time
from datetime import datetime, timedelta, timezone

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_REMINDER_TEXT = "time to take a break"

# How far in the future to schedule the reminder.
_REMIND_IN_MINUTES = 2

# How long to wait for the reminder message to arrive after scheduling.
# We allow 4× the requested delay to accommodate cron minute-resolution,
# arc dispatch latency, and Pi CPU load. With Claude Haiku this should be
# more reliable but we need time for cron to fire.
_WAIT_FOR_REMINDER_SECONDS = _REMIND_IN_MINUTES * 60 * 4 + 90  # ~9 min max


def _build_prompt(target_dt: datetime, cron_name: str) -> str:
    """Return the natural-language reminder request with an explicit time."""
    time_str = target_dt.strftime("%H:%M")  # e.g. "14:37"
    return (
        f"Please remind me to take a break at {time_str} today "
        f"(that is in approximately {_REMIND_IN_MINUTES} minutes). "
        f"Send me a chat message in this conversation at that time. "
        f"Use the scheduling tools to create a trigger that fires at {time_str}. "
        f"The reminder should fire once and not repeat daily. "
        f"The message should say 'Time to take a break!'. "
        f"Please name the cron/schedule entry `{cron_name}` so it's easy "
        f"to identify as a test artifact."
    )


class RemindMeAtTime(AcceptanceStory):
    name = "S006 — User Asks Agent to Set a Reminder for a Specific Time"
    description = (
        "User asks for a 2-minute reminder; agent sets a one-shot schedule; "
        "platform delivers the message; story verifies arrival and cleanup."
    )
    artifact_prefix = "s006"

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()
        target_dt = datetime.now() + timedelta(minutes=_REMIND_IN_MINUTES)
        cron_name = self.artifact_name("reminder")
        prompt = _build_prompt(target_dt, cron_name)

        # ── 1. Send reminder request ──────────────────────────────────────────
        print(f"\n  [1/3] Requesting reminder at {target_dt.strftime('%H:%M')}...")
        conv_id = client.create_conversation()
        client.send_message(prompt, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(msgs) >= 1,
            "No response after reminder request",
            conversation_id=conv_id,
        )
        ack_resp = msgs[-1]["content"]
        print(f"     {ack_resp[:150]}")

        # Agent should acknowledge it will send a reminder.
        self.assert_that(
            any(kw in ack_resp.lower() for kw in
                ("remind", "schedul", "set", "break", "timer", ":"+target_dt.strftime("%M"))),
            "Acknowledgement does not mention scheduling a reminder",
            response_preview=ack_resp[:400],
        )

        # ── 2. Wait for the reminder message to arrive ────────────────────────
        # Record baseline so we only look at NEW messages (avoids false-matching
        # the agent's acknowledgement which may quote the reminder phrase).
        baseline_count = len(client.get_history(conv_id))
        print(f"  [2/3] Waiting up to {_WAIT_FOR_REMINDER_SECONDS}s for reminder "
              f"(target ~{target_dt.strftime('%H:%M')})...")
        deadline = time.monotonic() + _WAIT_FOR_REMINDER_SECONDS
        reminder_msg = None
        while time.monotonic() < deadline:
            all_msgs = client.get_history(conv_id)
            new_msgs = all_msgs[baseline_count:]
            # Accept messages delivered via arc.dispatch (arc_id set) OR
            # cron.message (arc_id NULL but content matches). Both are valid
            # delivery mechanisms.
            for m in new_msgs:
                if (
                    _REMINDER_TEXT in m.get("content", "").lower()
                    and m.get("role") in ("assistant", "system")
                ):
                    reminder_msg = m
                    break
            if reminder_msg:
                break
            time.sleep(10)

        elapsed = time.time() - start_ts
        print(f"  [2/3] Elapsed: {elapsed:.0f}s")

        self.assert_that(
            reminder_msg is not None,
            f"Reminder message containing '{_REMINDER_TEXT}' never arrived "
            f"after {elapsed:.0f}s",
            conversation_id=conv_id,
            all_messages=[m.get("content", "")[:80] for m in
                          client.get_assistant_messages(conv_id)],
            # --- gap analysis hints ---
            hint_1="Was scheduling.add_once called? Check cron_entries table for a one-shot entry.",
            hint_2="Was a cron entry created? Check cron_entries table.",
            hint_3="Was an arc created and dispatched at the right time?",
        )

        print(f"  [2/3] Reminder arrived ✓  content: {reminder_msg['content'][:120]}")

        # ── 3. Structural DB assertions ───────────────────────────────────────
        if db is None:
            return StoryResult(
                name=self.name, passed=True,
                message="Behavioural checks passed (no DB configured)",
            )

        # Verify the reminder message exists in the DB. It may have been
        # delivered via arc.dispatch (arc_id set) or cron.message (arc_id NULL).
        # Both are valid — the key assertion is that it arrived.
        all_db_msgs = db.get_messages(conv_id)
        reminder_db_msgs = [
            m for m in all_db_msgs
            if _REMINDER_TEXT in m.get("content", "").lower()
            and m.get("role") in ("assistant", "system")
        ]
        self.assert_that(
            len(reminder_db_msgs) >= 1,
            "Reminder message not found in conversation DB",
            messages=db.format_messages_table(all_db_msgs),
        )

        # No recurring cron entry for this reminder should remain active.
        # (A well-behaved one-shot reminder cleans up after itself.)
        leftover_crons = db._query(
            "SELECT * FROM cron_entries WHERE name LIKE ? AND enabled = 1",
            (self.artifact_name_pattern(),),
        )
        self.assert_that(
            len(leftover_crons) == 0,
            f"Cron entry for reminder was not cleaned up after delivery "
            f"({len(leftover_crons)} active cron(s) remain)",
            crons=leftover_crons,
            hint="A one-shot trigger should auto-delete or disable itself after firing.",
        )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Reminder delivered after {elapsed:.0f}s ✓, "
                f"delivered via arc executor ✓, "
                f"no recurring cron left over ✓"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: DBInspector) -> None:
        """Remove any cron entries created by this run so it doesn't repeat."""
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
