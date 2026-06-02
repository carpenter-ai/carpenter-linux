"""
S009 — Agent Discovers Config Key from Vague Instruction

The user describes a problem in plain English — "you've been losing track of what
we've been working on" — without naming any config key.  The agent must:

  1. Recognise the concern relates to memory hints / recent conversation titles.
  2. Use ``config.list_keys`` (read-only) to inspect available settings and
     identify ``memory_recent_hints`` as the relevant knob.
  3. Submit reviewed code that increases the value via ``config.set_value``.
  4. Confirm the change is live; survive a follow-up "prove it" challenge.
  5. Revert on request back to the platform default (3).

This story tests the discovery workflow added by list_keys() — the agent gets
no hints in the prompts about which key to change.

Config key:  memory_recent_hints
Original:    3  (platform default)
Changed to:  agent's choice (must be > 3)
Reverted to: 3

NOTE: This story writes and reverts ~/carpenter/config/config.yaml.  The
cleanup() method restores the default value if the test fails mid-way.
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

_DISCOVER_AND_CHANGE_PROMPT = (
    "I feel like you've been losing track of what we've been working on lately. "
    "Find the config setting that controls how many recent conversation titles "
    "appear in your context window, increase it for me, and confirm once the "
    "change is live. Use the config tools to discover available settings and "
    "then change the appropriate value."
)

_VERIFY_PROMPT = (
    "Can you confirm that actually changed? Use config.get_value to read the "
    "current in-memory value right now and show me the number. No restart needed."
)

_REVERT_PROMPT = (
    "Actually, let's dial it back to the default for now. "
    "Put it back to 3 and let me know when it's done."
)

_ARC_HEALTH_PROMPT = (
    "Was all of that clean? No weird failures or retries behind the scenes?"
)


class DiscoverAndChangeConfig(AcceptanceStory):
    name = "S009 — Agent Discovers Config Key from Vague Instruction"
    description = (
        "User describes problem without naming a config key; agent uses "
        "config.list_keys to discover memory_recent_hints, bumps it via "
        "config.set_value (reviewed callback, hot-reload), verifies live, "
        "reverts to default, arc history clean."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Discover + change ──────────────────────────────────────────────
        print(f"\n  [1/6] Sending vague prompt — no key name given...")
        conv_id = client.create_conversation()
        client.send_message(_DISCOVER_AND_CHANGE_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=180)

        msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(msgs) >= 1,
            "No response after vague config change request",
            conversation_id=conv_id,
        )
        change_resp = msgs[-1]["content"]
        print(f"     {change_resp[:200]}")

        # Agent should have identified the key and acknowledged the change
        self.assert_that(
            "memory_recent" in change_resp.lower(),
            "Agent did not mention 'memory_recent' — may not have found the right key",
            response_preview=change_resp[:400],
        )
        self.assert_that(
            any(kw in change_resp.lower() for kw in
                ("done", "updated", "changed", "set", "success", "complet",
                 "ok", "bumped", "increased", "raised", "now")),
            "Change response does not acknowledge a successful update",
            response_preview=change_resp[:400],
        )

        # ── 2. Structural verify — disk ───────────────────────────────────────
        print(f"  [2/6] Verifying config.yaml on disk...")
        _actual_new_value = None
        if _HAS_YAML and _CONFIG_PATH.exists():
            raw = yaml.safe_load(_CONFIG_PATH.read_text())
            disk_val = raw.get(_CONFIG_KEY) if raw else None
            self.assert_that(
                isinstance(disk_val, int) and disk_val > _ORIGINAL_VALUE,
                f"config.yaml does not have {_CONFIG_KEY} > {_ORIGINAL_VALUE} "
                f"(found {disk_val!r})",
                config_yaml=dict(raw) if raw else {},
            )
            _actual_new_value = disk_val
            print(f"     config.yaml: {_CONFIG_KEY}={disk_val} ✓ (agent chose {disk_val})")

        # ── 3. Live verify — agent confirms the value is actually live ─────────
        print(f"  [3/6] Asking agent to prove the change is live...")
        client.send_message(_VERIFY_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=120)

        msgs = client.get_assistant_messages(conv_id)
        verify_resp = msgs[-1]["content"]
        print(f"     {verify_resp[:200]}")

        if _actual_new_value is not None:
            self.assert_that(
                str(_actual_new_value) in verify_resp,
                f"Live verify response does not contain '{_actual_new_value}' "
                f"— hot-reload may not have taken effect",
                response_preview=verify_resp[:400],
            )
            print(f"     Live CONFIG shows {_CONFIG_KEY}={_actual_new_value} ✓")

        # ── 4. Revert ─────────────────────────────────────────────────────────
        print(f"  [4/6] Requesting revert → {_ORIGINAL_VALUE}...")
        client.send_message(_REVERT_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=120)

        msgs = client.get_assistant_messages(conv_id)
        revert_resp = msgs[-1]["content"]
        print(f"     {revert_resp[:150]}")
        self.assert_that(
            any(kw in revert_resp.lower() for kw in
                ("done", "reverted", "set", "back", "reset",
                 "success", "complet", "ok", str(_ORIGINAL_VALUE))),
            "Revert response does not acknowledge restoration",
            response_preview=revert_resp[:400],
        )
        self.assert_that(
            "3" in revert_resp,
            "Revert response does not mention the restored value (3)",
            response_preview=revert_resp[:400],
        )

        # ── 5. Disk + live verify post-revert ──────────────────────────────────
        print(f"  [5/6] Verifying config.yaml + live value after revert...")
        if _HAS_YAML and _CONFIG_PATH.exists():
            raw2 = yaml.safe_load(_CONFIG_PATH.read_text())
            disk_val2 = raw2.get(_CONFIG_KEY) if raw2 else None
            self.assert_that(
                disk_val2 == _ORIGINAL_VALUE or disk_val2 is None,
                f"config.yaml still has {_CONFIG_KEY}={disk_val2!r} after revert "
                f"(expected {_ORIGINAL_VALUE} or absent)",
                config_yaml=dict(raw2) if raw2 else {},
            )
            print(f"     config.yaml: {_CONFIG_KEY}={disk_val2!r} ✓")

        client.send_message(_VERIFY_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)
        msgs = client.get_assistant_messages(conv_id)
        verify_resp2 = msgs[-1]["content"]
        print(f"     Post-revert verify: {verify_resp2[:150]}")
        self.assert_that(
            str(_ORIGINAL_VALUE) in verify_resp2,
            f"Post-revert verify does not confirm {_CONFIG_KEY}={_ORIGINAL_VALUE}",
            response_preview=verify_resp2[:400],
        )
        print(f"     Live CONFIG shows {_CONFIG_KEY}={_ORIGINAL_VALUE} ✓")

        # ── 6. Arc health check ───────────────────────────────────────────────
        print(f"  [6/6] Requesting arc health check from agent...")
        client.send_message(_ARC_HEALTH_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=120)

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
        self.assert_no_failed_arcs_since(db, start_ts)

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Agent discovered {_CONFIG_KEY} from vague description ✓, "
                f"disk verify ✓ (agent chose {_actual_new_value}), "
                f"live CONFIG hot-reload verified ✓, "
                f"reverted to {_ORIGINAL_VALUE} ✓, "
                f"post-revert verify ✓, "
                f"arc history clean ✓"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Restore memory_recent_hints to default if the story failed mid-way."""
        if not _HAS_YAML or not _CONFIG_PATH.exists():
            return
        try:
            raw = yaml.safe_load(_CONFIG_PATH.read_text())
            if not raw:
                return
            current = raw.get(_CONFIG_KEY)
            if current is not None and current != _ORIGINAL_VALUE:
                raw[_CONFIG_KEY] = _ORIGINAL_VALUE
                _CONFIG_PATH.write_text(
                    yaml.dump(raw, default_flow_style=False, allow_unicode=True,
                              sort_keys=False)
                )
                print(f"  [cleanup] Reset {_CONFIG_KEY} to {_ORIGINAL_VALUE} in config.yaml")
                try:
                    from carpenter.config import reload_config
                    reload_config()
                    print(f"  [cleanup] Config reloaded")
                except Exception as exc:
                    print(f"  [cleanup] In-process reload skipped: {exc}")
        except Exception as exc:
            print(f"  [cleanup] Failed to restore config: {exc}")
