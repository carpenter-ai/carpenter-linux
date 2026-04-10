"""
S051 — Weather Query End-to-End

The user asks "What's the weather in Oxford, UK?" and expects the chat
agent to fetch the weather (via untrusted arc batch) and report back the
result in the same conversation — without the user sending another message.

This tests the full arc-completion → chat-notification pipeline:
  1. User asks about weather.
  2. Chat agent creates untrusted arc batch (EXECUTOR + REVIEWER + JUDGE).
  3. EXECUTOR fetches real weather data (encrypted in arc state).
  4. REVIEWER decrypts content at platform level (not via LLM sandbox).
  5. On completion, arc.chat_notify re-invokes the chat agent.
  6. Chat agent reads arc results and delivers weather info to the user.

The story passes when the conversation contains an assistant message with
weather-related content (temperature, conditions, etc.) for Oxford.  It also
verifies the response does NOT contain placeholder/empty data markers, which
would indicate the REVIEWER failed to decrypt the EXECUTOR's content.

Timeout: 300s (haiku), 300s (sonnet) — arc pipeline is multi-step.
"""

import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_WEATHER_PROMPT = "What's the weather in Oxford, UK?"


class WeatherQueryEndToEnd(AcceptanceStory):
    name = "S051 — Weather Query End-to-End"
    description = (
        "User asks about weather; agent creates untrusted arc batch to fetch "
        "it; arc completion notification delivers the result back to chat."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Ask about weather ─────────────────────────────────────────
        print("\n  [1/4] Asking about weather in Oxford...")
        conv_id = client.create_conversation()
        client.send_message(_WEATHER_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=120)

        msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(msgs) >= 1,
            "No initial response from chat agent",
            conversation_id=conv_id,
        )
        print(f"     Got initial response ({len(msgs)} message(s))")

        # ── 2. Wait for arc pipeline to complete ─────────────────────────
        print("  [2/4] Waiting for arc pipeline (up to 180s)...")
        if db is not None:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                arcs = db.get_arcs_created_after(start_ts)
                if arcs:
                    pending = [
                        a for a in arcs
                        if a["status"] not in ("completed", "failed", "cancelled")
                    ]
                    if not pending:
                        print(f"     All {len(arcs)} arc(s) settled")
                        break
                time.sleep(5)
            else:
                arcs = db.get_arcs_created_after(start_ts)
                pending = [
                    a for a in arcs
                    if a["status"] not in ("completed", "failed", "cancelled")
                ]
                if pending:
                    self.assert_that(
                        False,
                        f"{len(pending)} arc(s) still pending after 180s",
                        arcs=db.format_arcs_table(arcs),
                    )

        # ── 3. Wait for weather response to arrive via notification ──────
        # The notification re-invokes the chat agent, which should produce
        # a second assistant message with the actual weather info.
        print("  [3/4] Waiting for weather response (up to 120s)...")
        all_msgs = client.wait_for_n_assistant_messages(
            conv_id, n=2, timeout=120
        )
        print(f"     Got {len(all_msgs)} assistant messages total")

        # ── 4. Verify weather content ────────────────────────────────────
        print("  [4/4] Checking response contains weather info...")
        combined = " ".join(m["content"] for m in all_msgs).lower()

        # Should mention Oxford
        self.assert_that(
            "oxford" in combined,
            "Response does not mention Oxford",
            response_preview=combined[:800],
        )

        # Should contain weather-like content
        weather_keywords = (
            "temperature", "degrees", "celsius", "fahrenheit",
            "°c", "°f", "weather", "sunny", "cloudy", "rain",
            "overcast", "wind", "humidity", "forecast", "conditions",
            "warm", "cold", "cool", "mild",
        )
        self.assert_that(
            any(kw in combined for kw in weather_keywords),
            "Response does not contain weather-related information",
            response_preview=combined[:800],
        )

        # Must NOT contain placeholder/empty data markers — this catches
        # the bug where the REVIEWER couldn't decrypt fetched content and
        # returned empty summaries.
        placeholder_markers = (
            "not available", "n/a", "not specified",
            "{'value': none}", "data couldn't be parsed",
        )
        has_placeholder = any(m in combined for m in placeholder_markers)
        self.assert_that(
            not has_placeholder,
            "Response contains placeholder/empty data — fetch pipeline "
            "may not be decrypting content correctly",
            response_preview=combined[:800],
        )

        # ── DB assertions ────────────────────────────────────────────────
        if db is not None:
            new_arcs = db.get_arcs_created_after(start_ts)
            self.assert_that(
                len(new_arcs) >= 1,
                "No arcs were created for weather query",
            )

            completed = [a for a in new_arcs if a["status"] == "completed"]
            self.assert_that(
                len(completed) >= 1,
                "No arcs completed successfully",
                arcs=db.format_arcs_table(new_arcs),
            )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Weather query answered ✓, "
                f"{len(all_msgs)} assistant messages ✓, "
                f"Oxford + weather content verified ✓"
            ),
        )
