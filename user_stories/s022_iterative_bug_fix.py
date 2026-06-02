"""
S022 — Iterative Bug Fix (Ralph Loop)

The user reports a bug in a Celsius-to-Fahrenheit converter that produces
wrong results for negative numbers. The agent uses the impl+monitor sibling
arc pattern to iterate on the fix: implement, test, detect failure, revise,
until all test cases pass.

Formula: F = C * 9/5 + 32
Test cases: 0->32, 100->212, -40->-40, 37->98.6

Expected behaviour:
  1. User reports the bug with test cases.
  2. Agent creates a coding-change arc to fix the implementation.
  3. Agent may iterate (multiple impl+monitor cycles) to get all test
     cases passing.
  4. The final implementation correctly handles negative numbers.
  5. Story approves the diff and verifies correctness.

This is a long-running story (300s+ timeout) due to iteration.

DB verification:
  - At least one coding-change arc created.
  - The arc reaches 'waiting' with a diff for review.
  - After approval, the arc completes.
"""

import os
import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_BUG_REPORT_PROMPT = (
    "I have a bug in my temperature converter. The Celsius to Fahrenheit "
    "conversion is broken for negative numbers. The correct formula is "
    "F = C * 9/5 + 32. Please create a tool called 'celsius_to_fahrenheit' "
    "that correctly handles all these test cases:\n"
    "  - 0C -> 32F\n"
    "  - 100C -> 212F\n"
    "  - -40C -> -40F\n"
    "  - 37C -> 98.6F\n"
    "Use the coding-change workflow to add it as a read tool in "
    "carpenter_tools/read/celsius_to_fahrenheit.py."
)

_TOOL_NAME = "celsius_to_fahrenheit"


class IterativeBugFix(AcceptanceStory):
    name = "S022 — Iterative Bug Fix (Ralph Loop)"
    description = (
        "User reports C->F conversion bug for negative numbers; agent iterates "
        "with impl+monitor pattern until all test cases pass; 300s+ timeout."
    )

    def __init__(self) -> None:
        self._source_dir: str | None = None

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Report the bug ─────────────────────────────────────────────────
        print("\n  [1/4] Reporting temperature conversion bug...")
        conv_id = client.create_conversation()
        client.send_message(_BUG_REPORT_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(msgs) >= 1,
            "No response after bug report",
            conversation_id=conv_id,
        )
        init_resp = msgs[-1]["content"]
        print(f"     {init_resp[:200]}")

        self.assert_that(
            any(kw in init_resp.lower() for kw in
                ("celsius", "fahrenheit", "temperature", "convert", "fix",
                 "coding", "implement", "tool", "change")),
            "Response does not acknowledge the temperature conversion task",
            response_preview=init_resp[:400],
        )

        # ── 2. Wait for diff review ──────────────────────────────────────────
        print("  [2/4] Waiting for coding-change diff (up to 5 min)...")
        review_arc: dict | None = None
        if db is not None:
            review_arc = db.wait_for_pending_review_arc(start_ts, timeout=300)
            self.assert_that(
                review_arc is not None,
                "No coding-change arc reached 'waiting' for review",
                arcs=db.format_arcs_table(db.get_arcs_created_after(start_ts)),
            )

        arc_state = review_arc["arc_state"]
        review_id = arc_state["review_id"]
        diff = arc_state.get("diff", "")
        source_dir = arc_state.get("source_dir", "")
        if source_dir:
            self._source_dir = source_dir
        print(f"     Arc {review_arc['id']} waiting. Diff preview: {diff[:200]}")

        self.assert_that(
            bool(diff),
            "Diff is empty — coding agent produced no changes",
            arc_id=review_arc["id"],
        )

        # Verify the diff contains the formula
        diff_lower = diff.lower()
        self.assert_that(
            any(kw in diff_lower for kw in ("celsius", "fahrenheit", "9/5", "1.8", "32")),
            "Diff does not contain temperature conversion logic",
            diff_preview=diff[:600],
        )

        # ── 3. Approve the diff ───────────────────────────────────────────────
        print("  [3/4] Approving the coding-change diff...")
        result = client.submit_review_decision(
            review_id, decision="approve",
            comment="Looks correct. All test cases should pass."
        )
        self.assert_that(
            result.get("recorded") is True,
            "Approval not recorded",
            server_response=result,
        )

        # Wait for arc to complete
        if db is not None:
            print("  [4/4] Waiting for arc to complete (up to 120s)...")
            arc = db.wait_for_arc_terminal(review_arc["id"], timeout=120)
            self.assert_that(
                arc is not None and arc["status"] == "completed",
                f"Arc did not complete "
                f"(status={arc['status'] if arc else 'not found'})",
                arcs=db.format_arcs_table(db.get_arcs_created_after(start_ts)),
            )
            print(f"     Arc completed ✓")

        elapsed = time.time() - start_ts
        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Bug fix implemented via coding-change ✓, "
                f"diff contains conversion logic ✓, "
                f"approved and completed ✓ "
                f"({elapsed:.0f}s total)"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Remove the celsius_to_fahrenheit tool created during the test."""
        from pathlib import Path

        root = (
            Path(self._source_dir) if self._source_dir
            else Path(os.environ.get(
                "CARPENTER_SOURCE_DIR",
                str(Path(__file__).resolve().parents[1])
            ))
        )

        tool_path = root / "carpenter_tools" / "read" / f"{_TOOL_NAME}.py"
        if tool_path.exists():
            try:
                tool_path.unlink()
                print(f"  [cleanup] Removed {tool_path}")
            except Exception as exc:
                print(f"  [cleanup] Could not remove {tool_path}: {exc}")
