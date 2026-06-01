"""
S012 — Config-Controlled Chat Response Language

The user sets the chat_language config key to 'de' (German).  From that point
forward the agent must respond in German regardless of what language the user
writes in.  Resetting the key to '' restores English responses.

Story flow:
  1. User (in English): set chat_language to 'de' and confirm.
     Agent responds in English (directive not yet active).
  2. Structural check: config.yaml on disk shows chat_language: de.
  3. User (in English): asks a simple factual question.
     Agent responds in German — language directive is now active.
  4. User (in English): reset chat_language to '' and confirm.
     Agent may still respond in German (directive active when it receives
     the message), but the config change takes effect for future turns.
  5. User (in English): asks the same simple question.
     Agent responds in English — directive removed.
  6. Arc health check: asks agent whether everything was clean.
     Agent responds in English; structural arc check.

Config key:  chat_language
Changed to:  'de'
Reverted to: ''

NOTE: This story writes and reverts ~/carpenter/config/config.yaml.  The
cleanup() method restores the empty-string default if the test fails mid-way.
"""

import os
import time
from pathlib import Path

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_CONFIG_KEY = "chat_language"
_ORIGINAL_VALUE = ""
_TARGET_VALUE = "de"
_CONFIG_PATH = Path(os.environ.get(
    "CARPENTER_CONFIG",
    Path.home() / "carpenter" / "config" / "config.yaml"
))

_SET_PROMPT = (
    "Please set the chat_language config value to 'de' and confirm when done."
)

_FACTUAL_QUESTION = (
    "What is today's date?"
)

_RESET_PROMPT = (
    "Please reset chat_language to the default (empty string) and confirm."
)

_ARC_HEALTH_PROMPT = (
    "Was all of that clean? No failures?"
)

# ── German keyword constants (single-language) ────────────────────────────────

_KW_SET_DONE = (
    "erledigt", "fertig", "gesetzt", "aktualisiert", "geändert",
    "gemacht", "fertiggestellt", "abgeschlossen", "konfiguriert",
)

# Common German function words that don't appear in English responses.
# The leading/trailing spaces prevent matching inside longer words.
_KW_GERMAN_MARKERS = (
    " ich ", " sie ", " ist ", " und ", " der ", " die ", " das ",
    " zu ", " für ", " bei ", " von ", " auf ", " mit ", " sind ",
    " wurde ", " haben ", " werden ", " einem ", " einer ",
)

_KW_HEALTH = (
    "fehler", "problem", "alles", "sauber", "erfolgreich", "abgeschlossen",
    "keine", "gut", "in ordnung", "reibungslos",
)


def _is_german(text: str) -> bool:
    """Return True if text contains at least one German function-word marker."""
    lower = text.lower()
    return any(kw in lower for kw in _KW_GERMAN_MARKERS)


class ChatLanguageConfig(AcceptanceStory):
    name = "S012 — Config-Controlled Chat Response Language"
    description = (
        "chat_language ISO 639-1 config key injects language directive into "
        "system prompt; agent responds in German to English prompts; revert "
        "restores English."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Set chat_language = 'de' ───────────────────────────────────────
        print(f"\n  [1/6] Asking agent to set chat_language to 'de'...")
        conv_id = client.create_conversation()
        client.send_message(_SET_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=180)

        msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(msgs) >= 1,
            "No response after set chat_language request",
            conversation_id=conv_id,
        )
        set_resp = msgs[-1]["content"]
        print(f"     {set_resp[:200]}")

        # Step 1 response should be in English (directive not active yet)
        # and confirm the change was made.
        self.assert_that(
            any(kw in set_resp.lower() for kw in
                ("done", "set", "updated", "changed", "configured",
                 "confirmed", "success", "complet", "ok", "now")),
            "Set response does not acknowledge a successful update",
            response_preview=set_resp[:400],
        )

        # ── 2. Structural verify — disk ───────────────────────────────────────
        print(f"  [2/6] Verifying config.yaml on disk shows chat_language: de...")
        if _HAS_YAML and _CONFIG_PATH.exists():
            raw = yaml.safe_load(_CONFIG_PATH.read_text())
            disk_val = raw.get(_CONFIG_KEY) if raw else None
            self.assert_that(
                disk_val == _TARGET_VALUE,
                f"config.yaml does not have {_CONFIG_KEY}={_TARGET_VALUE!r} "
                f"(found {disk_val!r})",
                config_yaml=dict(raw) if raw else {},
            )
            print(f"     config.yaml: {_CONFIG_KEY}={disk_val!r} ✓")

        # ── 3. German response check ─────────────────────────────────────────
        print(f"  [3/6] Asking a simple question — expect German response...")
        client.send_message(_FACTUAL_QUESTION, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=120)

        msgs = client.get_assistant_messages(conv_id)
        german_resp = msgs[-1]["content"]
        print(f"     {german_resp[:300]}")

        self.assert_that(
            _is_german(german_resp),
            "Agent did not respond in German after chat_language='de' was set — "
            "language directive may not be in the system prompt",
            response_preview=german_resp[:400],
        )
        print(f"     German markers detected ✓")

        # ── 4. Reset chat_language to '' ─────────────────────────────────────
        print(f"  [4/6] Asking agent to reset chat_language to ''...")
        client.send_message(_RESET_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=120)

        msgs = client.get_assistant_messages(conv_id)
        reset_resp = msgs[-1]["content"]
        print(f"     {reset_resp[:200]}")
        # Agent may respond in German here (directive still active when it
        # received the message) — we accept either language for the reset ACK.
        self.assert_that(
            any(kw in reset_resp.lower() for kw in
                ("done", "reset", "cleared", "removed", "set", "updated",
                 "changed", "empty", "default", "ok", "complet",
                 # German equivalents in case directive was still active:
                 "erledigt", "fertig", "gesetzt", "zurückgesetzt",
                 "entfernt", "gelöscht", "aktualisiert")),
            "Reset response does not acknowledge the change",
            response_preview=reset_resp[:400],
        )

        # ── 5. English response check ─────────────────────────────────────────
        print(f"  [5/6] Asking the same question — expect English response...")
        client.send_message(_FACTUAL_QUESTION, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=120)

        msgs = client.get_assistant_messages(conv_id)
        english_resp = msgs[-1]["content"]
        print(f"     {english_resp[:300]}")

        self.assert_that(
            not _is_german(english_resp),
            "Agent responded in German after chat_language was reset to '' — "
            "language directive may still be in the system prompt",
            response_preview=english_resp[:400],
        )
        print(f"     No German markers — English response confirmed ✓")

        # ── 6. Arc health check ───────────────────────────────────────────────
        print(f"  [6/6] Requesting arc health check...")
        client.send_message(_ARC_HEALTH_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=60)

        msgs = client.get_assistant_messages(conv_id)
        health_resp = msgs[-1]["content"]
        print(f"     {health_resp[:200]}")
        self.assert_that(
            any(kw in health_resp.lower() for kw in
                ("no fail", "clean", "healthy", "success", "all complet",
                 "no unexpect", "no retry", "no cancel", "no error",
                 "look good", "looks good", "completed successfully",
                 "no issues")),
            "Arc health check does not confirm clean history",
            response_preview=health_resp[:400],
        )

        # Structural: no failed/cancelled arcs from our operations
        if db is not None:
            all_arcs = db.get_arcs_created_after(start_ts)
            bad = [a for a in all_arcs if a["status"] in ("failed", "cancelled")]
            self.assert_that(
                len(bad) == 0,
                f"{len(bad)} arc(s) ended in failed/cancelled",
                arcs=db.format_arcs_table(bad),
            )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"chat_language set to 'de' ✓, "
                f"disk verify ✓, "
                f"German response confirmed ✓, "
                f"chat_language reset to '' ✓, "
                f"English response confirmed ✓, "
                f"arc history clean ✓"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Restore chat_language to '' if the story failed mid-way."""
        if not _HAS_YAML or not _CONFIG_PATH.exists():
            return
        try:
            raw = yaml.safe_load(_CONFIG_PATH.read_text())
            if not raw:
                return
            current = raw.get(_CONFIG_KEY)
            # Reset if the key is present and non-empty
            if current:
                raw[_CONFIG_KEY] = _ORIGINAL_VALUE
                _CONFIG_PATH.write_text(
                    yaml.dump(raw, default_flow_style=False, allow_unicode=True,
                              sort_keys=False)
                )
                print(f"  [cleanup] Reset {_CONFIG_KEY} to {_ORIGINAL_VALUE!r} in config.yaml")
                try:
                    from carpenter.config import reload_config
                    reload_config()
                    print(f"  [cleanup] Config reloaded")
                except Exception as exc:
                    print(f"  [cleanup] In-process reload skipped: {exc}")
        except Exception as exc:
            print(f"  [cleanup] Failed to restore config: {exc}")
