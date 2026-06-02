"""
S023 — Vague Rejection Triggers Rethink

The user asks for a `format_json` read tool via the coding-change workflow.
The story reviews the first diff, then sends a vague revision: "too complicated,
simplify." The agent must substantially rework the implementation (not just
tweak a few lines). Two distinct review_ids are generated.

Expected behaviour:
  1. User requests a format_json read tool.
  2. Agent creates a coding-change arc, produces a diff.
  3. Story sends revision: "This is too complicated. Simplify it."
  4. Agent reworks the implementation substantially.
  5. Second review appears with a different review_id.
  6. Story approves the simplified version.
  7. Cleanup removes format_json.py.

DB verification:
  - Two distinct review_ids appeared during the workflow.
  - The revised diff differs from the first diff.
  - The arc completes after approval.
"""

import time
from pathlib import Path
import os

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_TOOL_NAME = "format_json"

_ADD_PROMPT = (
    "Please create a new read tool called 'format_json' in "
    "carpenter_tools/read/format_json.py. It should take a raw JSON string "
    "as input and return a pretty-printed, indented version with syntax "
    "validation, error messages for invalid JSON, customizable indent level, "
    "and optional sorting of keys. Use the coding-change workflow."
)

_REVISE_COMMENT = (
    "This is too complicated. Simplify it — I just need basic JSON "
    "pretty-printing with default 2-space indent. Remove the extra options."
)


class VagueRejectionRethink(AcceptanceStory):
    name = "S023 — Vague Rejection Triggers Rethink"
    description = (
        "User requests format_json tool; story revises with 'too complicated, "
        "simplify'; agent reworks substantially; two review_ids; cleanup removes file."
    )

    def __init__(self) -> None:
        self._source_dir: str | None = None

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Request the tool ───────────────────────────────────────────────
        print(f"\n  [1/6] Requesting '{_TOOL_NAME}' tool...")
        conv_id = client.create_conversation()
        client.send_message(_ADD_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        msgs = client.get_assistant_messages(conv_id)
        self.assert_that(len(msgs) >= 1, "No response after tool request")

        # ── 2. Wait for first diff ────────────────────────────────────────────
        print("  [2/6] Waiting for first diff (up to 5 min)...")
        review_arc1: dict | None = None
        if db is not None:
            review_arc1 = db.wait_for_pending_review_arc(start_ts, timeout=300)
            self.assert_that(
                review_arc1 is not None,
                "No coding-change arc reached 'waiting' (round 1)",
                arcs=db.format_arcs_table(db.get_arcs_created_after(start_ts)),
            )

        arc_state1 = review_arc1["arc_state"]
        review_id1 = arc_state1["review_id"]
        diff1 = arc_state1.get("diff", "")
        source_dir = arc_state1.get("source_dir", "")
        if source_dir:
            self._source_dir = source_dir
        print(f"     Review 1: {review_id1}")
        print(f"     Diff 1 preview: {diff1[:200]}")

        self.assert_that(bool(diff1), "First diff is empty")

        # ── 3. Send vague revision ────────────────────────────────────────────
        print("  [3/6] Sending vague revision: 'too complicated, simplify'...")
        result1 = client.submit_review_decision(
            review_id1, decision="revise", comment=_REVISE_COMMENT
        )
        self.assert_that(
            result1.get("recorded") is True,
            "Revision not recorded",
            server_response=result1,
        )

        # ── 4. Wait for revised diff ─────────────────────────────────────────
        print("  [4/6] Waiting for revised diff (up to 5 min)...")
        review_arc2: dict | None = None
        if db is not None:
            review_arc2 = db.wait_for_pending_review_arc(
                start_ts, timeout=300, exclude_review_ids={review_id1},
            )
            self.assert_that(
                review_arc2 is not None,
                "No revised diff appeared (round 2)",
                arcs=db.format_arcs_table(db.get_arcs_created_after(start_ts)),
            )

        arc_state2 = review_arc2["arc_state"]
        review_id2 = arc_state2["review_id"]
        diff2 = arc_state2.get("diff", "")
        print(f"     Review 2: {review_id2}")
        print(f"     Diff 2 preview: {diff2[:200]}")

        # Two distinct review_ids
        self.assert_that(
            review_id1 != review_id2,
            f"Expected two distinct review_ids, got same: {review_id1}",
        )

        # The revised diff should be different
        self.assert_that(
            diff1 != diff2,
            "Revised diff is identical to the first — agent did not rework",
            diff1_preview=diff1[:300],
            diff2_preview=diff2[:300],
        )

        # ── 5. Approve the simplified version ─────────────────────────────────
        print("  [5/6] Approving simplified version...")
        result2 = client.submit_review_decision(
            review_id2, decision="approve",
            comment="Simplified version looks good."
        )
        self.assert_that(
            result2.get("recorded") is True,
            "Approval not recorded",
            server_response=result2,
        )

        # ── 6. Wait for completion ────────────────────────────────────────────
        if db is not None:
            print("  [6/6] Waiting for arc to complete (up to 120s)...")
            arc = db.wait_for_arc_terminal(review_arc2["id"], timeout=120)
            self.assert_that(
                arc is not None and arc["status"] == "completed",
                f"Arc did not complete "
                f"(status={arc['status'] if arc else 'not found'})",
            )
            print(f"     Arc completed ✓")

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Two distinct review rounds (r1={review_id1[:8]}..., "
                f"r2={review_id2[:8]}...) ✓, "
                f"diffs differ ✓, "
                f"approved and completed ✓"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Remove format_json.py artifacts."""
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
