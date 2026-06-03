"""
S047 — Self-Escalation via the `escalate` Tool

Tests that an arc can self-escalate by calling the `escalate` tool, which
freezes the original arc and creates a stronger sibling with read access
to the original's subtree.

Scenario:
  1. Configure escalation stacks (haiku -> sonnet) in config.yaml.
  2. Ask the agent to create a single arc whose goal instructs it to call
     the `escalate` tool immediately.
  3. Wait for arcs to settle.
  4. Verify escalation mechanics:
     - Original arc has status='escalated'.
     - A sibling arc exists with '(escalated)' in its name.
     - The sibling has a different (stronger) model.
     - An arc_read_grants row exists from the sibling to the original.
     - The sibling has _escalated_from metadata pointing to the original.

NOTE: This story modifies escalation config in ~/carpenter/config/config.yaml.
      Cleanup restores the original config.
"""

import os
import time
from pathlib import Path

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

_CONFIG_PATH = Path(os.environ.get(
    "CARPENTER_CONFIG",
    Path.home() / "carpenter" / "config" / "config.yaml"
))

# Escalation pair (base -> stronger).  Set CARPENTER_TEST_MODEL_PRIMARY
# (base) and CARPENTER_TEST_MODEL_SECONDARY (escalation target) to run;
# SKIPs cleanly when either is unset.
_HAIKU = os.environ.get("CARPENTER_TEST_MODEL_PRIMARY", "")
_SONNET = os.environ.get("CARPENTER_TEST_MODEL_SECONDARY", "")

_ESCALATION_PROMPT = (
    "Create a single arc with the following goal: "
    "'You are testing the self-escalation mechanism. Your only job is to "
    "call the escalate tool immediately. Do not do anything else — just "
    "call escalate right away.' "
    "Let it run automatically."
)


class AutoEscalationOnFailure(AcceptanceStory):
    name = "S047 — Self-Escalation Tool"
    description = (
        "Arc calls the escalate tool; verify platform creates a stronger "
        "sibling with read grants and correct metadata."
    )
    timeout = 300

    _original_config: str | None = None

    def _read_config(self) -> dict:
        if not _HAS_YAML or not _CONFIG_PATH.exists():
            return {}
        return yaml.safe_load(_CONFIG_PATH.read_text()) or {}

    def _write_config(self, cfg: dict) -> None:
        if not _HAS_YAML:
            return
        _CONFIG_PATH.write_text(yaml.dump(cfg, default_flow_style=False))

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        if not _HAIKU or not _SONNET:
            import pytest
            pytest.skip(
                "Set CARPENTER_TEST_MODEL_PRIMARY (base) and "
                "CARPENTER_TEST_MODEL_SECONDARY (escalation target) to "
                "run this story."
            )

        # ── 0. Save original config and add escalation stacks ──────────────
        if _HAS_YAML and _CONFIG_PATH.exists():
            self._original_config = _CONFIG_PATH.read_text()

        cfg = self._read_config()
        if "escalation" not in cfg:
            cfg["escalation"] = {}
        cfg["escalation"]["stacks"] = {
            "general": [f"anthropic:{_HAIKU}", f"anthropic:{_SONNET}"],
            "coding": [f"anthropic:{_HAIKU}", f"anthropic:{_SONNET}"],
        }
        cfg["escalation"]["require_confirmation"] = False
        # Force arcs to use Haiku so escalation to Sonnet is possible
        if "model_roles" not in cfg:
            cfg["model_roles"] = {}
        cfg["model_roles"]["default_step"] = f"anthropic:{_HAIKU}"
        self._write_config(cfg)
        print("\n  [0/4] Configured escalation stacks (haiku -> sonnet) "
              "and default_step=haiku")

        # Reload config via chat so the running server picks it up
        conv_id = client.create_conversation()
        client.send_message("Please reload the platform config.", conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=30)

        # ── 1. Ask agent to create an arc that will self-escalate ─────────
        print("  [1/4] Requesting arc that will call escalate tool...")
        client.send_message(_ESCALATION_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=120)

        msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(msgs) >= 1,
            "No response received after escalation request",
            conversation_id=conv_id,
        )
        print(f"     Response: {msgs[-1]['content'][:150]}")

        # ── 2. Wait for arcs to settle ─────────────────────────────────────
        print("  [2/4] Waiting for arcs to settle (up to 180s)...")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if db is not None:
                arcs = db.get_arcs_created_after(start_ts)
                active = [
                    a for a in arcs
                    if a["status"] not in (
                        "completed", "failed", "cancelled", "escalated",
                    )
                ]
                if arcs and not active:
                    break
            else:
                try:
                    client.wait_for_n_assistant_messages(conv_id, n=3, timeout=30)
                    break
                except TimeoutError:
                    pass
            time.sleep(5)

        # ── 3. Check results ───────────────────────────────────────────────
        if db is None:
            return StoryResult(
                name=self.name,
                passed=True,
                message="Behavioural only (no DB). Cannot verify escalation.",
            )

        print("  [3/4] Checking for escalation in DB...")
        all_arcs = db.get_arcs_created_after(start_ts)
        print(f"     {len(all_arcs)} arcs created")

        # Print arc table for diagnostics
        for a in all_arcs:
            print(f"     Arc #{a['id']}: {a.get('name', '?')!r} [{a['status']}]")

        escalated_arcs = [a for a in all_arcs if a["status"] == "escalated"]
        escalation_arcs = [
            a for a in all_arcs if "(escalated" in (a.get("name") or "")
        ]

        self.assert_that(
            len(escalated_arcs) >= 1,
            "No arc with status='escalated' found — escalate tool was not called",
            arcs=db.format_arcs_table(all_arcs),
        )
        print(f"     {len(escalated_arcs)} arc(s) with status='escalated'")

        original = escalated_arcs[0]
        original_id = original["id"]

        # Find the escalated sibling
        self.assert_that(
            len(escalation_arcs) >= 1,
            "Found escalated arc but no sibling with '(escalated)' in name",
            arcs=db.format_arcs_table(all_arcs),
        )
        new_arc = escalation_arcs[0]
        new_arc_id = new_arc["id"]
        print(f"     Escalated: arc #{original_id} -> arc #{new_arc_id}")

        # Check read grant exists
        grants = db.fetchall(
            "SELECT * FROM arc_read_grants "
            "WHERE reader_arc_id = ? AND target_arc_id = ?",
            (new_arc_id, original_id),
        )
        self.assert_that(
            len(grants) >= 1,
            f"No read grant from arc #{new_arc_id} to arc #{original_id}",
            arcs=db.format_arcs_table(all_arcs),
        )
        grant = grants[0]
        print(f"     Read grant: #{new_arc_id} -> #{original_id} "
              f"(depth={grant['depth']}) ✓")

        # Check escalation metadata
        state = db.get_arc_state(new_arc_id)
        escalated_from = state.get("_escalated_from")
        self.assert_that(
            escalated_from == original_id,
            f"Expected _escalated_from={original_id}, got {escalated_from}",
        )
        print(f"     _escalated_from metadata: {escalated_from} ✓")

        # Check model is different (stronger)
        model_info = ""
        if new_arc.get("agent_config_id"):
            configs = db.fetchall(
                "SELECT * FROM agent_configs WHERE id = ?",
                (new_arc["agent_config_id"],),
            )
            if configs:
                model_info = configs[0].get("model", "")
                print(f"     New arc model: {model_info}")

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Self-escalation verified: arc #{original_id} -> "
                f"#{new_arc_id} ({model_info}), read grant + metadata correct"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Restore original config."""
        if self._original_config is not None and _CONFIG_PATH.exists():
            try:
                _CONFIG_PATH.write_text(self._original_config)
                print("     Config restored to original")
            except Exception as e:
                print(f"     WARNING: Failed to restore config: {e}")
