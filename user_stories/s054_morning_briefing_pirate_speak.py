"""
S054 — Modify Morning Briefing To Pirate Speak

Builds on S053.  The user first asks for a recurring morning briefing,
then — before or after it fires — sends a terse follow-up asking for
the delivered text to be rewritten in pirate speak.  The agent is
expected to pick the scheduling tool, compose the briefing, and later
modify the existing cron in place rather than duplicating it.

The story passes when:
  1. After the setup request, at least one new cron entry created
     during this test is active (agent chose a scheduling tool).
  2. The scheduled payload reads like a plausible morning briefing
     (Haiku semantic check, with a keyword fallback).
  3. After the modification request, EXACTLY ONE active cron remains
     among the crons created during this test — modify in place,
     don't duplicate.
  4. A message arrives in the conversation that Haiku judges to be
     both a morning briefing AND written in pirate speak.
  5. The recurring cron remains active after firing.

Natural-prompt design (2026-04-12):
  * The setup prompt is terse; it only hints that an every-minute
    schedule is acceptable for testing.  It does NOT dictate the
    briefing text, the KB pattern, the tool name, or code.
  * The modify prompt is one sentence.  It does NOT supply pirate
    text for the agent to paste verbatim.
  * Assertions are semantic (Haiku yes/no) rather than exact-string,
    so the test measures whether the agent can actually compose a
    briefing + rewrite it in pirate speak — not whether it can copy
    a string the prompt already handed it.

Cleanup (2026-04-12, post per-run UUID): the setup prompt asks the
agent to use a specific cron name of the form ``s054-<run_id>-briefing``,
so queries and cleanup can filter by ``name LIKE 's054-<run_id>-%'``
without touching sibling runs' or pre-existing crons.
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

# Maximum seconds to wait for the pirate message after modification.
# Every-minute cron should fire within 60 s, but the modify step can
# consume significant budget when the agent iterates on submit_code,
# and next_fire_at is reset when the cron is updated — so allow a
# generous window.
_WAIT_FOR_DELIVERY_SECONDS = 240

_ANTHROPIC_MODEL = "claude-haiku-4-5"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"

# Pirate-speak keyword heuristic (fallback when Haiku is unavailable).
_PIRATE_KW = (
    "arr", "arrr", "ahoy", "matey", "avast", "ye ", " ye,", " ye.",
    "yarr", "shiver", "timbers", "scallywag", "landlubber", "bucko",
    "scurvy", "me hearties", "cap'n", "blimey", "booty", "plunder",
    "grog", "hornswaggle",
)

# Briefing-ish keyword heuristic for payload sanity-check fallback.
_BRIEFING_KW = (
    "weather", "temperature", "forecast", "headline", "headlines",
    "news", "today", "morning", "briefing", "°c", "°f", "degrees",
)

def _setup_prompt(cron_name: str) -> str:
    return (
        "Can you set up a daily morning briefing for me? Weather plus a "
        "headline or two is fine — you don't need to actually fetch "
        "anything, a plausible sample briefing is OK. To make it easy to "
        "see it working in this test, please use an every-minute schedule "
        "rather than a specific time of day. "
        f"Please name the cron/schedule entry `{cron_name}` so I can "
        "identify it later — and if I ask for a change, update that "
        "same entry in place rather than creating a second one."
    )

_MODIFY_PROMPT = (
    "Actually, can you update the existing briefing so it's written "
    "in pirate speak? I don't want a second one — just change the "
    "one you already set up."
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
    """Ask Claude Haiku a YES/NO question about *text*."""
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


def _extract_cron_message(row: dict) -> str:
    """Pull the human-visible message text from a cron row's payload.

    Tries ``event_payload_json['message']`` first, then falls back to
    other likely keys, then the whole payload JSON as a string.
    """
    payload_raw = row.get("event_payload_json") or "{}"
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        return payload_raw
    if isinstance(payload, dict):
        for key in ("message", "text", "body", "content", "briefing"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val
        return json.dumps(payload)
    return str(payload)


class ModifyBriefingToPirateSpeak(AcceptanceStory):
    name = "S054 — Modify Morning Briefing To Pirate Speak"
    description = (
        "User asks for a daily morning briefing, then asks to have it "
        "talk like a pirate.  Agent modifies the existing cron in "
        "place (no duplicate) and a pirate-speak briefing arrives."
    )
    timeout = 900  # ~15 min total budget (Haiku can iterate a lot on modify)
    artifact_prefix = "s054"

    def _active_story_crons(self, db: DBInspector) -> list[dict]:
        """Return cron_entries rows created by this run (name LIKE pattern)."""
        return db._query(
            "SELECT * FROM cron_entries WHERE name LIKE ? AND enabled = 1",
            (self.artifact_name_pattern(),),
        )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        self.assert_that(
            db is not None,
            "DBInspector is required for this story",
        )

        # Per-run unique cron name — cleanup and assertions filter on
        # ``self.artifact_name_pattern()`` so we never touch sibling
        # runs' or pre-existing crons.
        cron_name = self.artifact_name("briefing")
        print(f"  [0/6] Per-run cron name: {cron_name}")

        # ── 1. Send terse setup request ────────────────────────────────
        print("\n  [1/6] Asking for a daily morning briefing...")
        conv_id = client.create_conversation()
        client.send_message(_setup_prompt(cron_name), conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=180)

        setup_msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(setup_msgs) >= 1,
            "No acknowledgement after briefing request",
            conversation_id=conv_id,
        )
        print(f"     Ack: {setup_msgs[-1]['content'][:200]}")

        # ── 2. Verify at least one briefing-ish cron exists ────────────
        print("  [2/6] Looking for a scheduled briefing...")
        active = self._active_story_crons(db)
        self.assert_that(
            len(active) >= 1,
            "No active cron_entries row with our story prefix after setup "
            "request.  The agent should schedule a daily briefing via the "
            "scheduling tools (e.g. scheduling.add_cron) and use the name "
            f"we provided ({cron_name}).",
        )
        setup_row = active[0]
        setup_message = _extract_cron_message(setup_row)
        print(f"     Found {len(active)} cron(s); sample payload: "
              f"{setup_message[:200]}")

        # Semantic sanity check: does the payload look like a briefing?
        verdict, detail = _ai_classify_yes_no(
            question=(
                "Does the following text look like a short morning "
                "briefing (e.g. weather and/or a headline or two)?  "
                "Answer YES even if the data is clearly a made-up "
                "sample.  Answer NO only if it is unrelated (a status "
                "message, a raw config snippet, etc.)."
            ),
            text=setup_message,
        )
        if verdict is None:
            hits = sum(
                1 for k in _BRIEFING_KW if k in setup_message.lower()
            )
            self.assert_that(
                hits >= 1,
                "Initial cron payload does not look like a briefing "
                f"(keyword hits={hits}, Haiku unavailable: {detail})",
                payload=setup_message[:400],
            )
            print(f"     Briefing-ish (heuristic, {hits} markers) ✓")
        else:
            self.assert_that(
                verdict,
                "Haiku judged the scheduled payload NOT to look like a "
                "morning briefing",
                payload=setup_message[:400],
                haiku_detail=detail,
            )
            print(f"     Briefing-ish (haiku): {detail[:100]} ✓")

        # ── 3. Ask for pirate-speak modification (terse, no pirate text) ─
        print("  [3/6] Asking for the briefing to talk like a pirate...")
        client.send_message(_MODIFY_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=180)

        mod_msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(mod_msgs) >= 2,
            "No acknowledgement after modification request",
            conversation_id=conv_id,
        )
        mod_ack = mod_msgs[-1]["content"]
        print(f"     Ack: {mod_ack[:200]}")

        # ── 4. Exactly one new-in-this-story cron should be active ─────
        print("  [4/6] Checking for exactly one active briefing cron...")
        post_mod = self._active_story_crons(db)
        self.assert_that(
            len(post_mod) == 1,
            f"Expected exactly 1 active cron matching this run's prefix "
            f"after modification, found {len(post_mod)}.  "
            f"Agent should modify the existing cron in place rather "
            f"than duplicating it.",
            crons=[{k: v for k, v in c.items()
                    if k in ("name", "cron_expr", "event_type", "enabled")}
                   for c in post_mod],
        )
        mod_message = _extract_cron_message(post_mod[0])
        print(f"     Exactly 1 cron ✓; payload: {mod_message[:200]}")

        # ── 5. Wait for a new pirate-speak briefing to arrive ──────────
        baseline_count = len(client.get_history(conv_id))
        print(f"  [5/6] Waiting up to {_WAIT_FOR_DELIVERY_SECONDS}s for "
              f"a new briefing to be delivered...")

        # We don't know the exact text the agent chose, so we look
        # for any new "briefing-shaped" assistant message after the
        # modification: something with at least one pirate marker OR
        # at least one briefing marker, and reasonably long.  The
        # real semantic check happens in [6] via Haiku.
        briefing_msg: dict | None = None
        deadline = time.monotonic() + _WAIT_FOR_DELIVERY_SECONDS
        checked_ids: set = set()
        while time.monotonic() < deadline and briefing_msg is None:
            all_msgs = client.get_history(conv_id)
            new_msgs = all_msgs[baseline_count:]
            for m in new_msgs:
                if m.get("role") != "assistant":
                    continue
                mid = m.get("id") or id(m)
                if mid in checked_ids:
                    continue
                checked_ids.add(mid)
                content = (m.get("content") or "").strip()
                if len(content) < 40:
                    # Skip short acks / thinking fragments.
                    continue
                lower = content.lower()
                pirate_hits = sum(1 for k in _PIRATE_KW if k in lower)
                briefing_hits = sum(
                    1 for k in _BRIEFING_KW if k in lower
                )
                # Require at least one pirate marker (the whole point)
                # plus at least one briefing-ish marker, to avoid
                # picking up the agent's acknowledgement message.
                if pirate_hits >= 1 and briefing_hits >= 1:
                    briefing_msg = m
                    break
            if briefing_msg:
                break
            time.sleep(5)

        elapsed = time.time() - start_ts
        print(f"     Elapsed: {elapsed:.0f}s")
        self.assert_that(
            briefing_msg is not None,
            f"No pirate-speak briefing arrived after {elapsed:.0f}s.  "
            f"Expected the cron to fire and deliver a message that "
            f"contains pirate-speak markers.",
            conversation_id=conv_id,
            hint="Check work_queue for cron.message items and server logs.",
        )
        briefing_text = briefing_msg["content"]
        print(f"     Candidate briefing: {briefing_text[:200]}")

        # ── 6. Haiku: briefing AND pirate speak ────────────────────────
        print("  [6/6] Haiku verification...")
        verdict, detail = _ai_classify_yes_no(
            question=(
                "Is the following text BOTH (a) a short morning-briefing "
                "style message (e.g. weather and/or a headline or two) "
                "AND (b) written primarily in exaggerated pirate speak "
                "— the kind of dialect that uses words like 'arr', "
                "'ahoy', 'matey', 'avast', 'ye', 'shiver me timbers', "
                "'me hearties'?  Answer YES only if BOTH (a) and (b) "
                "are true.  Answer NO if it is plain English with only "
                "a passing pirate reference, or if it is not a briefing."
            ),
            text=briefing_text,
        )
        if verdict is None:
            print(f"     Haiku unavailable ({detail}); falling back to "
                  f"keyword heuristic.")
            lower = briefing_text.lower()
            pirate_hits = sum(1 for k in _PIRATE_KW if k in lower)
            briefing_hits = sum(1 for k in _BRIEFING_KW if k in lower)
            self.assert_that(
                pirate_hits >= 2 and briefing_hits >= 1,
                "Briefing does not read as both briefing and pirate "
                f"speak (pirate_hits={pirate_hits}, "
                f"briefing_hits={briefing_hits}, Haiku unavailable: "
                f"{detail})",
                briefing=briefing_text[:800],
            )
            verdict_str = (
                f"heuristic-pass (pirate={pirate_hits}, "
                f"briefing={briefing_hits})"
            )
        else:
            self.assert_that(
                verdict,
                "Haiku judged the delivered message NOT to be a pirate-"
                "speak morning briefing",
                briefing=briefing_text[:800],
                haiku_detail=detail,
            )
            verdict_str = f"haiku-pass ({detail[:100]})"
        print(f"     Verdict: {verdict_str}")

        # Recurring cron must still be active (not auto-deleted).
        still_active = self._active_story_crons(db)
        self.assert_that(
            len(still_active) == 1,
            f"Expected 1 recurring cron still active, found "
            f"{len(still_active)}.",
            crons=still_active,
        )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Briefing scheduled ✓, modified in place (1 active) ✓, "
                f"pirate-speak briefing delivered ({elapsed:.0f}s) ✓, "
                f"Haiku {verdict_str}"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: DBInspector) -> None:
        """Remove cron_entries this run created (name LIKE s054-<run_id>-%).

        Filtering on the per-run artifact prefix is race-free across
        concurrent test runs — the pre-UUID version of this cleanup
        diffed against a baseline snapshot and would match rows created
        by other sibling processes.
        """
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
