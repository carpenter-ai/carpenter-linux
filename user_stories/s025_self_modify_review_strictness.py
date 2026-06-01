"""
S025 — Self-Modify Review Strictness

The user asks Carpenter to relax its review strictness (e.g., allow
auto-approval for small changes). The agent uses the config tool to modify
the config. The story approves the change, verifies the config was updated,
then asks the agent to revert it.

Expected behaviour:
  1. User asks to relax review strictness.
  2. Agent uses config.set_value or coding-change to modify review config.
  3. Story verifies config was changed.
  4. User asks to revert.
  5. Agent reverts config to original value.

Cleanup restores the original config value.

DB verification:
  - Config change was applied (disk or DB).
  - Revert was applied.
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

_CONFIG_PATH = Path(os.environ.get(
    "CARPENTER_CONFIG",
    Path.home() / "carpenter" / "config" / "config.yaml"
))
_CONFIG_KEY = "review_auto_approve_threshold"

_RELAX_PROMPT = (
    "I'd like to relax the review strictness. Can you change the "
    "review_auto_approve_threshold config setting to 50? That way small "
    "changes under 50 lines won't need manual review. Use the config tool."
)

_VERIFY_PROMPT = (
    "What is the current value of review_auto_approve_threshold? "
    "Read it from the config."
)

_REVERT_PROMPT = (
    "Actually, let's be strict again. Set review_auto_approve_threshold "
    "back to 0. Confirm when done."
)


class SelfModifyReviewStrictness(AcceptanceStory):
    name = "S025 — Self-Modify Review Strictness"
    description = (
        "User asks to relax review strictness; agent changes config; "
        "story verifies; then reverts. Cleanup restores original."
    )

    def __init__(self):
        super().__init__()
        self._original_value = None

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # Record original value
        if _HAS_YAML and _CONFIG_PATH.exists():
            raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            self._original_value = raw.get(_CONFIG_KEY, 0)
        else:
            self._original_value = 0
        print(f"\n  [0/5] Original {_CONFIG_KEY} = {self._original_value}")

        # ── 1. Ask to relax ───────────────────────────────────────────────────
        print("  [1/5] Requesting review strictness relaxation...")
        conv_id, relax_resp = client.chat(_RELAX_PROMPT, timeout=180)
        print(f"     {relax_resp[:200]}")

        self.assert_that(
            any(kw in relax_resp.lower() for kw in
                ("done", "set", "changed", "updated", "threshold",
                 "50", "review", "config")),
            "Response does not acknowledge config change",
            response_preview=relax_resp[:400],
        )

        # ── 2. Verify via agent ───────────────────────────────────────────────
        print("  [2/5] Verifying config change via agent...")
        _, verify_resp = client.chat(
            _VERIFY_PROMPT, conversation_id=conv_id, timeout=90
        )
        print(f"     {verify_resp[:200]}")

        self.assert_that(
            "50" in verify_resp,
            "Verification response does not show value 50",
            response_preview=verify_resp[:400],
        )

        # ── 3. Verify on disk ────────────────────────────────────────────────
        print("  [3/5] Verifying config.yaml on disk...")
        if _HAS_YAML and _CONFIG_PATH.exists():
            raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            disk_val = raw.get(_CONFIG_KEY)
            self.assert_that(
                disk_val == 50,
                f"config.yaml {_CONFIG_KEY} expected 50, found {disk_val!r}",
                config=raw,
            )
            print(f"     Disk: {_CONFIG_KEY} = {disk_val} ✓")

        # ── 4. Revert ────────────────────────────────────────────────────────
        print("  [4/5] Requesting revert...")
        _, revert_resp = client.chat(
            _REVERT_PROMPT, conversation_id=conv_id, timeout=180
        )
        print(f"     {revert_resp[:200]}")

        self.assert_that(
            any(kw in revert_resp.lower() for kw in
                ("done", "set", "reverted", "back", "0", "strict", "reset")),
            "Revert response does not acknowledge restoration",
            response_preview=revert_resp[:400],
        )

        # ── 5. Verify revert on disk ─────────────────────────────────────────
        print("  [5/5] Verifying revert on disk...")
        if _HAS_YAML and _CONFIG_PATH.exists():
            raw2 = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            disk_val2 = raw2.get(_CONFIG_KEY, 0)
            self.assert_that(
                disk_val2 == 0 or disk_val2 is None,
                f"After revert, {_CONFIG_KEY} expected 0, found {disk_val2!r}",
                config=raw2,
            )
            print(f"     Disk: {_CONFIG_KEY} = {disk_val2} ✓")

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
                f"Review strictness relaxed to 50 ✓, "
                f"verified ✓, "
                f"reverted to 0 ✓"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Restore original review_auto_approve_threshold."""
        if not _HAS_YAML or not _CONFIG_PATH.exists():
            return
        try:
            raw = yaml.safe_load(_CONFIG_PATH.read_text())
            if not raw:
                return
            current = raw.get(_CONFIG_KEY)
            target = self._original_value if self._original_value is not None else 0
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
            print(f"  [cleanup] Failed to restore config: {exc}")
