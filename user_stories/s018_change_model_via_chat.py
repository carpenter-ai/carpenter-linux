"""
S018 — User Changes the Running Language Model via Chat

The user asks Carpenter to switch the language model used for chat
conversations, then the test runner verifies the model actually changed
by inspecting the api_calls table in the database.

Steps:
  1. Send an initial message to establish a baseline api_call record.
  2. Note the model used for that call from the api_calls table.
  3. Ask the agent to change the chat model to a different one
     (via config.set_value on model_roles.chat).
  4. Send a follow-up message (this invocation should use the NEW model).
  5. Check the api_calls table — the latest call should show the new model.
  6. Verify config.yaml on disk reflects the change.
  7. Revert to the original model and verify the revert.

DB verification:
  - api_calls table shows the new model name after the switch.
  - config.yaml has the updated model_roles.chat value.
  - After revert, model_roles.chat returns to its original value.

NOTE: This story modifies model_roles.chat in ~/carpenter/config/config.yaml.
The cleanup() method restores the original value if the test fails mid-way.
"""

import json
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

_CONFIG_PATH = Path(os.environ.get(
    "CARPENTER_CONFIG",
    Path.home() / "carpenter" / "config" / "config.yaml"
))

# Two models to switch between — both Anthropic, both cheap enough for testing.
_MODEL_A = "claude-haiku-4-5"
_MODEL_B = "claude-sonnet-4-6"

_CHANGE_PROMPT = (
    "Please change the chat model to `{target}`. "
    "Use the config tool to set `model_roles.chat` to `{target}`. "
    "Confirm when done."
)

_FOLLOWUP_PROMPT = (
    "Briefly confirm: what model are you running on right now? "
    "Just state the model name."
)

_REVERT_PROMPT = (
    "Please revert the chat model back to `{original}`. "
    "Set `model_roles.chat` to `{original}` using the config tool. "
    "Confirm when done."
)


class ChangeModelViaChat(AcceptanceStory):
    name = "S018 — User Changes the Running Language Model via Chat"
    description = (
        "User switches the chat model via config.set_value(model_roles.chat, ...), "
        "test runner verifies the api_calls table shows the new model, "
        "then reverts."
    )

    def __init__(self):
        super().__init__()
        self._original_model = None  # Saved for cleanup

    def _read_current_chat_model(self) -> str:
        """Read model_roles.chat from config.yaml."""
        if not _HAS_YAML or not _CONFIG_PATH.exists():
            return _MODEL_A  # Fallback assumption
        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        roles = raw.get("model_roles", {})
        return roles.get("chat", "") or _MODEL_A

    def _get_latest_api_call_model(self, db: DBInspector, conversation_id: int) -> str | None:
        """Return the model from the most recent api_call for this conversation."""
        rows = db.fetchall(
            "SELECT model FROM api_calls WHERE conversation_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        )
        return rows[0]["model"] if rows else None

    def _get_api_calls_after(self, db: DBInspector, after_id: int, conversation_id: int) -> list[dict]:
        """Return api_calls with id > after_id for the given conversation."""
        return db.fetchall(
            "SELECT id, model, created_at FROM api_calls "
            "WHERE conversation_id = ? AND id > ? ORDER BY id",
            (conversation_id, after_id),
        )

    def _get_max_api_call_id(self, db: DBInspector) -> int:
        """Return the current maximum api_call id (watermark)."""
        rows = db.fetchall("SELECT MAX(id) as max_id FROM api_calls")
        return rows[0]["max_id"] or 0 if rows else 0

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 0. Determine current and target models ────────────────────────────
        self._original_model = self._read_current_chat_model()
        # Pick a target that's different from what's currently configured
        if "haiku" in self._original_model.lower():
            target_model = _MODEL_B
        else:
            target_model = _MODEL_A
        print(f"\n  [0/7] Current chat model: {self._original_model}")
        print(f"        Target model:       {target_model}")

        # ── 1. Baseline message ───────────────────────────────────────────────
        print(f"\n  [1/7] Sending baseline message...")
        conv_id, baseline_resp = client.chat("Hello, what can you do?", timeout=120)
        print(f"     Got response ({len(baseline_resp)} chars)")

        # Small delay for api_call to be written
        time.sleep(2)

        baseline_model = self._get_latest_api_call_model(db, conv_id)
        print(f"     Baseline api_call model: {baseline_model}")
        self.assert_that(
            baseline_model is not None,
            "No api_call record found for baseline message",
            conversation_id=conv_id,
        )

        # Record watermark for later comparison
        watermark = self._get_max_api_call_id(db)

        # ── 2. Ask agent to change the chat model ────────────────────────────
        print(f"\n  [2/7] Asking agent to switch chat model to {target_model}...")
        _, change_resp = client.chat(
            _CHANGE_PROMPT.format(target=target_model),
            conversation_id=conv_id,
            timeout=180,
        )
        print(f"     {change_resp[:200]}")

        change_resp_lower = change_resp.lower()
        self.assert_that(
            any(kw in change_resp_lower for kw in
                ("done", "changed", "updated", "set", "success", "complet",
                 "model_roles", target_model.lower())),
            "Agent response does not acknowledge the model change",
            response_preview=change_resp[:400],
        )

        # ── 3. Verify config.yaml on disk ─────────────────────────────────────
        print(f"\n  [3/7] Verifying config.yaml on disk...")
        if _HAS_YAML and _CONFIG_PATH.exists():
            raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            roles = raw.get("model_roles", {})
            disk_val = roles.get("chat", "")
            self.assert_that(
                disk_val == target_model,
                f"config.yaml model_roles.chat expected {target_model!r}, "
                f"found {disk_val!r}",
                config_model_roles=roles,
            )
            print(f"     config.yaml: model_roles.chat = {disk_val} ✓")

        # ── 4. Send follow-up message (should use NEW model) ──────────────────
        print(f"\n  [4/7] Sending follow-up message (should use {target_model})...")
        # Record watermark AFTER the change request's api calls
        time.sleep(2)
        post_change_watermark = self._get_max_api_call_id(db)

        _, followup_resp = client.chat(
            _FOLLOWUP_PROMPT,
            conversation_id=conv_id,
            timeout=120,
        )
        print(f"     {followup_resp[:200]}")

        # ── 5. Verify the api_calls table shows the new model ─────────────────
        print(f"\n  [5/7] Checking api_calls table for model change...")
        time.sleep(2)

        new_calls = self._get_api_calls_after(db, post_change_watermark, conv_id)
        print(f"     Found {len(new_calls)} api_call(s) after watermark {post_change_watermark}")

        self.assert_that(
            len(new_calls) >= 1,
            f"No api_calls found after watermark {post_change_watermark} "
            f"for conversation {conv_id}",
            conversation_id=conv_id,
        )

        # Check that at least one post-change call used the new model
        new_models = [c["model"] for c in new_calls]
        print(f"     Models in post-change calls: {new_models}")

        # The API response model name should contain the target model's
        # identifying substring (handles minor format differences)
        target_key = target_model.split("-")[1]  # "sonnet" or "haiku"
        self.assert_that(
            any(target_key in m.lower() for m in new_models if m),
            f"Expected post-change api_calls to use a model containing "
            f"'{target_key}', but got: {new_models}",
            api_calls=new_calls,
            target_model=target_model,
            baseline_model=baseline_model,
        )
        print(f"     Model change verified in api_calls ✓")

        # Also verify it's different from the baseline
        baseline_key = self._original_model.split("-")[1]  # "haiku" or "sonnet"
        if baseline_key != target_key:
            self.assert_that(
                any(target_key in m.lower() for m in new_models if m)
                and target_key != baseline_key,
                f"Post-change model should differ from baseline ({baseline_key})",
                new_models=new_models,
                baseline_model=baseline_model,
            )
            print(f"     Model is different from baseline ({baseline_key} → {target_key}) ✓")

        # ── 6. Revert ─────────────────────────────────────────────────────────
        print(f"\n  [6/7] Reverting chat model to {self._original_model}...")
        _, revert_resp = client.chat(
            _REVERT_PROMPT.format(original=self._original_model),
            conversation_id=conv_id,
            timeout=180,
        )
        print(f"     {revert_resp[:200]}")

        revert_lower = revert_resp.lower()
        self.assert_that(
            any(kw in revert_lower for kw in
                ("done", "reverted", "changed", "set", "back", "reset",
                 "success", "complet", self._original_model.lower())),
            "Revert response does not acknowledge the model change",
            response_preview=revert_resp[:400],
        )

        # Verify disk after revert
        if _HAS_YAML and _CONFIG_PATH.exists():
            raw2 = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            roles2 = raw2.get("model_roles", {})
            disk_val2 = roles2.get("chat", "")
            self.assert_that(
                disk_val2 == self._original_model,
                f"After revert, config.yaml model_roles.chat expected "
                f"{self._original_model!r}, found {disk_val2!r}",
                config_model_roles=roles2,
            )
            print(f"     config.yaml reverted: model_roles.chat = {disk_val2} ✓")

        # ── 7. Arc health check ───────────────────────────────────────────────
        print(f"\n  [7/7] Checking for failed arcs...")
        if db is not None:
            all_arcs = db.get_arcs_created_after(start_ts)
            bad = [a for a in all_arcs if a["status"] in ("failed", "cancelled")]
            self.assert_that(
                len(bad) == 0,
                f"{len(bad)} arc(s) ended in failed/cancelled",
                arcs=db.format_arcs_table(bad),
            )
            print(f"     No failed arcs ✓")

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Baseline model: {self._original_model}, "
                f"switched to {target_model} ✓, "
                f"config.yaml verified ✓, "
                f"api_calls table confirmed new model ✓, "
                f"reverted to {self._original_model} ✓"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Restore model_roles.chat to its original value if the story failed mid-way."""
        if not self._original_model:
            return
        if not _HAS_YAML or not _CONFIG_PATH.exists():
            return
        try:
            raw = yaml.safe_load(_CONFIG_PATH.read_text())
            if not raw:
                return
            roles = raw.get("model_roles", {})
            current = roles.get("chat", "")
            if current and current != self._original_model:
                roles["chat"] = self._original_model
                raw["model_roles"] = roles
                _CONFIG_PATH.write_text(
                    yaml.dump(raw, default_flow_style=False, allow_unicode=True,
                              sort_keys=False)
                )
                print(f"  [cleanup] Restored model_roles.chat to {self._original_model}")
                try:
                    from carpenter.config import reload_config
                    reload_config()
                    print(f"  [cleanup] Config reloaded")
                except Exception as exc:
                    print(f"  [cleanup] In-process reload skipped: {exc}")
        except Exception as exc:
            print(f"  [cleanup] Failed to restore config: {exc}")
