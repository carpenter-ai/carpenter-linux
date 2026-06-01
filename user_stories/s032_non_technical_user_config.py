"""
S032 — Non-Technical User Changes Config

The user says "I keep forgetting what we talked about last time, can you
fix that?" — no technical vocabulary at all. The agent must discover the
relevant config key (memory_recent_hints), change it to improve memory
recall, and explain the change in plain language.

Pattern: follows s009's discover-and-change-config pattern, but with
even less technical prompting.

Expected behaviour:
  1. User describes problem in plain, non-technical language.
  2. Agent discovers memory_recent_hints via config.list_keys.
  3. Agent changes the value (increases it).
  4. Agent explains what it did in plain, non-technical language.
  5. Cleanup reverts the config.

DB verification:
  - Config was changed on disk.
  - Agent's explanation avoids jargon or explains it simply.
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

_CONFIG_KEY = "memory_recent_hints"
_ORIGINAL_VALUE = 3
_CONFIG_PATH = Path(os.environ.get(
    "CARPENTER_CONFIG",
    Path.home() / "carpenter" / "config" / "config.yaml"
))

_PLAIN_PROMPT = (
    "I keep forgetting what we talked about last time. Every new "
    "conversation it's like starting from scratch. Can you fix that "
    "so you remember more of our past chats? Please use your config "
    "tools to find and change the right setting — I think there's "
    "something about how many recent conversations you remember."
)

_REVERT_PROMPT = (
    "Actually, put that setting back to how it was before. "
    "The default is fine."
)


class NonTechnicalUserConfig(AcceptanceStory):
    name = "S032 — Non-Technical User Changes Config"
    description = (
        "User says 'I keep forgetting what we talked about' — no tech vocab; "
        "agent discovers memory_recent_hints, changes it, explains plainly."
    )

    def __init__(self):
        super().__init__()
        self._original_value = None

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # Record original value
        if _HAS_YAML and _CONFIG_PATH.exists():
            raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            self._original_value = raw.get(_CONFIG_KEY, _ORIGINAL_VALUE)
        else:
            self._original_value = _ORIGINAL_VALUE
        print(f"\n  [0/4] Original {_CONFIG_KEY} = {self._original_value}")

        # ── 1. Send plain-language request ────────────────────────────────────
        print("  [1/4] Sending non-technical request...")
        conv_id, response = client.chat(_PLAIN_PROMPT, timeout=180)
        print(f"     {response[:300]}")

        resp_lower = response.lower()

        # Agent should have identified and changed the setting
        self.assert_that(
            any(kw in resp_lower for kw in
                ("memory", "remember", "conversation", "recent", "hint",
                 "changed", "updated", "increased", "improved")),
            "Response does not indicate a memory-related change was made",
            response_preview=response[:600],
        )

        # Agent should explain in plain language (not just dump config keys)
        self.assert_that(
            any(kw in resp_lower for kw in
                ("now", "should", "will", "better", "more", "remember",
                 "recall", "past", "previous")),
            "Response does not explain the change in plain language",
            response_preview=response[:600],
        )

        # ── 2. Verify config changed on disk ─────────────────────────────────
        print("  [2/4] Verifying config on disk...")
        if _HAS_YAML and _CONFIG_PATH.exists():
            raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            disk_val = raw.get(_CONFIG_KEY)
            self.assert_that(
                isinstance(disk_val, int) and disk_val > self._original_value,
                f"Config not updated: {_CONFIG_KEY} = {disk_val!r} "
                f"(expected > {self._original_value})",
                config=raw,
            )
            print(f"     {_CONFIG_KEY} = {disk_val} ✓ (was {self._original_value})")

        # ── 3. Revert ────────────────────────────────────────────────────────
        print("  [3/4] Requesting revert...")
        _, revert_resp = client.chat(
            _REVERT_PROMPT, conversation_id=conv_id, timeout=120
        )
        print(f"     {revert_resp[:200]}")

        self.assert_that(
            any(kw in revert_resp.lower() for kw in
                ("done", "reverted", "back", "default", "restored", "reset",
                 "original")),
            "Revert response does not confirm restoration",
            response_preview=revert_resp[:400],
        )

        # ── 4. Verify revert ─────────────────────────────────────────────────
        print("  [4/4] Verifying revert on disk...")
        if _HAS_YAML and _CONFIG_PATH.exists():
            raw2 = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            disk_val2 = raw2.get(_CONFIG_KEY, _ORIGINAL_VALUE)
            self.assert_that(
                disk_val2 == _ORIGINAL_VALUE or disk_val2 is None,
                f"After revert: {_CONFIG_KEY} = {disk_val2!r} "
                f"(expected {_ORIGINAL_VALUE})",
                config=raw2,
            )
            print(f"     {_CONFIG_KEY} = {disk_val2} ✓")

        # Arc health
        if db is not None:
            all_arcs = db.get_arcs_created_after(start_ts)
            bad = [a for a in all_arcs if a["status"] in ("failed", "cancelled")]
            self.assert_that(
                len(bad) == 0,
                f"{len(bad)} arc(s) failed/cancelled",
                arcs=db.format_arcs_table(bad),
            )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Non-technical request understood ✓, "
                f"config changed ✓, "
                f"explained plainly ✓, "
                f"reverted ✓"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Restore memory_recent_hints to default."""
        if not _HAS_YAML or not _CONFIG_PATH.exists():
            return
        try:
            raw = yaml.safe_load(_CONFIG_PATH.read_text())
            if not raw:
                return
            current = raw.get(_CONFIG_KEY)
            target = self._original_value if self._original_value is not None else _ORIGINAL_VALUE
            if current is not None and current != target:
                raw[_CONFIG_KEY] = target
                _CONFIG_PATH.write_text(
                    yaml.dump(raw, default_flow_style=False, allow_unicode=True,
                              sort_keys=False)
                )
                print(f"  [cleanup] Restored {_CONFIG_KEY} to {target}")
                try:
                    from carpenter.config import reload_config
                    reload_config()
                except Exception:
                    pass
        except Exception as exc:
            print(f"  [cleanup] Config restore failed: {exc}")
