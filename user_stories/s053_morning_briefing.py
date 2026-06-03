"""
S053 — Daily Morning Briefing

The user asks Carpenter to set up a recurring morning briefing.  The
agent chooses how to deliver it — what the message says, which tool /
scheduling pattern to use, what cron schedule is reasonable for a test.

The story passes when:
  1. At least one active cron entry exists that will deliver a
     briefing-like message to the conversation (``cron.message``
     pattern, with a ``message`` payload).
  2. A new assistant message arrives in the conversation within the
     test window.
  3. Claude Haiku judges the arriving message to be a plausible
     morning briefing (or a keyword heuristic confirms this when the
     API key is unavailable).
  4. The cron is still active after firing (recurring, not one-shot).

The prompt is terse.  One small pragmatic hint is given — "every
minute" scheduling — so the test can observe a fire within its time
budget.  Everything else (wording, tool choice, KB pattern) is left
to the agent.

Cleanup: removes cron_entries whose name starts with ``s053``.
"""

import json
import os
import time
from pathlib import Path

import httpx

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

# Max seconds to wait for a delivered briefing message after the
# cron has been created.  "* * * * *" fires within ~60 s.
_WAIT_FOR_DELIVERY_SECONDS = 180

# Oracle model for the semantic briefing check.  Set
# CARPENTER_TEST_ORACLE_MODEL=<anthropic-model-id> to enable; when unset
# the AI classifier is skipped and the keyword heuristic is used instead.
_ANTHROPIC_MODEL = os.environ.get("CARPENTER_TEST_ORACLE_MODEL", "")
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"

# Heuristic: words/phrases that suggest a morning briefing when the
# Haiku classifier is unavailable.
_BRIEFING_KW = (
    "morning", "good morning", "today", "weather", "forecast",
    "temperature", "news", "headline", "headlines", "briefing",
    "agenda", "summary", "update",
)


def _load_anthropic_key() -> str | None:
    """Try env first, then ~/carpenter/.env (ANTHROPIC_API_KEY)."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    dot_env = Path.home() / "carpenter" / ".env"
    if dot_env.exists():
        for line in dot_env.read_text().splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    return None


def _ai_classify_yes_no(question: str, text: str) -> tuple[bool | None, str]:
    """Ask Claude Haiku a YES/NO question about *text*.

    Returns ``(True, raw)`` for YES, ``(False, raw)`` for NO,
    ``(None, reason)`` if the call failed or the answer was unparseable.
    """
    if not _ANTHROPIC_MODEL:
        return None, "CARPENTER_TEST_ORACLE_MODEL not set"
    api_key = _load_anthropic_key()
    if not api_key:
        return None, "no ANTHROPIC_API_KEY available"

    prompt = (
        f"{question}\n\n"
        f"Text to evaluate:\n"
        f"---\n{text[:4000]}\n---\n\n"
        f"Answer with a single word on the first line: YES or NO. "
        f"Optionally add a one-sentence reason on the next line."
    )
    body = {
        "model": _ANTHROPIC_MODEL,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    try:
        r = httpx.post(_ANTHROPIC_URL, json=body, headers=headers, timeout=30)
    except httpx.HTTPError as exc:
        return None, f"httpx error: {exc}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    try:
        data = r.json()
        raw = data["content"][0]["text"].strip()
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        return None, f"parse error: {exc}"

    first_line = raw.splitlines()[0].strip().upper() if raw else ""
    if first_line.startswith("YES"):
        return True, raw
    if first_line.startswith("NO"):
        return False, raw
    return None, f"unclassifiable response: {raw[:120]!r}"


def _build_prompt(cron_name: str) -> str:
    return (
        "Please set up a recurring morning briefing for me — a short "
        "daily message with the kind of thing you'd want first thing in "
        "the morning (weather, headlines, whatever you think makes "
        "sense).\n\n"
        "So I can actually see it working during this session, schedule "
        "it to fire every minute (cron expression `* * * * *`), and "
        f"please name the cron entry `{cron_name}` so I can find it "
        "later.\n\n"
        "Reply briefly once it's set up."
    )


class MorningBriefing(AcceptanceStory):
    name = "S053 — Daily Morning Briefing"
    description = (
        "User asks for a recurring morning briefing; agent picks the "
        "content, tool, and scheduling pattern; a briefing-like "
        "message is delivered; content is verified semantically."
    )
    timeout = 420  # ~7 min total budget
    artifact_prefix = "s053"

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        self.assert_that(
            db is not None,
            "DBInspector is required for this story",
        )

        # ── 1. Send the request ───────────────────────────────────────
        print("\n  [1/4] Requesting a recurring morning briefing...")
        cron_name = self.artifact_name("briefing")
        conv_id = client.create_conversation()
        client.send_message(_build_prompt(cron_name), conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=150)

        setup_msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(setup_msgs) >= 1,
            "No acknowledgement after briefing request",
            conversation_id=conv_id,
        )
        ack = setup_msgs[-1]["content"]
        print(f"     Ack: {ack[:200]}")

        # ── 2. Verify *some* active cron entry exists ─────────────────
        print("  [2/4] Looking for an active briefing cron...")
        candidates = db._query(
            "SELECT * FROM cron_entries "
            "WHERE name LIKE ? AND enabled = 1 "
            "ORDER BY id DESC",
            (self.artifact_name_pattern(),),
        )

        # Pick crons that look like they'll deliver a message to the
        # conversation (cron.message with a non-empty message payload).
        briefing_crons: list[dict] = []
        for c in candidates:
            if c.get("event_type") != "cron.message":
                continue
            try:
                payload = json.loads(c.get("event_payload_json") or "{}")
            except json.JSONDecodeError:
                continue
            msg = (payload.get("message") or "").strip()
            if msg:
                briefing_crons.append(c)

        self.assert_that(
            len(briefing_crons) >= 1,
            "No active cron.message entry with a message payload was "
            "found. The agent should schedule a recurring cron that "
            "delivers a briefing message to the conversation.",
            hint=(
                "Expected a cron_entries row with event_type="
                "'cron.message' and event_payload.message set."
            ),
            candidates=[
                {k: c.get(k) for k in ("name", "cron_expr", "event_type")}
                for c in candidates
            ],
        )
        cron_row = briefing_crons[0]
        cron_payload = json.loads(cron_row["event_payload_json"])
        cron_message = cron_payload.get("message", "").strip()
        print(
            f"     Found cron '{cron_row.get('name')}' "
            f"({cron_row.get('cron_expr')}); "
            f"payload message length: {len(cron_message)}"
        )

        # ── 3. Wait for a new assistant message to land ───────────────
        baseline_count = len(client.get_history(conv_id))
        print(
            f"  [3/4] Waiting up to {_WAIT_FOR_DELIVERY_SECONDS}s for "
            f"the briefing to be delivered..."
        )

        briefing_msg: dict | None = None
        deadline = time.monotonic() + _WAIT_FOR_DELIVERY_SECONDS
        while time.monotonic() < deadline:
            all_msgs = client.get_history(conv_id)
            new_msgs = all_msgs[baseline_count:]
            for m in new_msgs:
                if m.get("role") != "assistant":
                    continue
                content = (m.get("content") or "").strip()
                if not content:
                    continue
                # Accept either exact payload match OR a substantial
                # new assistant message that looks briefing-shaped.
                if content == cron_message:
                    briefing_msg = m
                    break
                lower = content.lower()
                if any(kw in lower for kw in _BRIEFING_KW) and len(content) > 20:
                    briefing_msg = m
                    break
            if briefing_msg:
                break
            time.sleep(5)

        elapsed = time.time() - start_ts
        print(f"     Elapsed: {elapsed:.0f}s")
        self.assert_that(
            briefing_msg is not None,
            f"No briefing-like assistant message arrived after "
            f"{elapsed:.0f}s.",
            conversation_id=conv_id,
            hint=(
                "The cron.message handler should deliver "
                "event_payload['message'] directly to the conversation."
            ),
        )
        briefing_text = briefing_msg["content"]
        print(f"     Delivered ({len(briefing_text)} chars): "
              f"{briefing_text[:160]}")

        # ── 4. Semantic content check + recurrence check ──────────────
        print("  [4/4] Verifying content + that the cron is still active...")

        verdict, detail = _ai_classify_yes_no(
            question=(
                "Does the following text read like a short morning "
                "briefing — the sort of thing you might want delivered "
                "first thing in the morning (weather, headlines, "
                "day-summary, agenda, news update, etc.)? Made-up or "
                "sample data is fine. Answer NO only if the text is "
                "clearly unrelated (e.g. an error message, a system "
                "status dump with no briefing content, or pure noise)."
            ),
            text=briefing_text,
        )
        if verdict is None:
            print(f"     Haiku unavailable ({detail}); using keyword "
                  f"heuristic.")
            lower = briefing_text.lower()
            hits = sum(1 for k in _BRIEFING_KW if k in lower)
            self.assert_that(
                hits >= 1,
                f"Briefing text does not look like a morning briefing "
                f"(keyword hits={hits}).",
                briefing=briefing_text[:800],
            )
            verdict_str = f"heuristic-pass ({hits} markers)"
        else:
            self.assert_that(
                verdict,
                "Haiku judged the delivered message NOT to be a "
                "plausible morning briefing.",
                briefing=briefing_text[:800],
                haiku_detail=detail,
            )
            verdict_str = f"haiku-pass ({detail[:100]})"

        still_active = db._query(
            "SELECT * FROM cron_entries WHERE id = ? AND enabled = 1",
            (cron_row["id"],),
        )
        self.assert_that(
            len(still_active) == 1,
            "Recurring cron entry is no longer active after firing — "
            "it should remain enabled (recurring, not one-shot).",
            cron_id=cron_row["id"],
        )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Briefing cron created ✓, message delivered "
                f"({elapsed:.0f}s) ✓, content verdict: {verdict_str}, "
                f"cron still active ✓"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: DBInspector) -> None:
        """Remove any cron entries created by this run."""
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
                    print(f"  [cleanup] Removed {cur.rowcount} cron(s) "
                          f"matching '{pattern}'")
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            print(f"  [cleanup] Cron cleanup failed: {exc}")
