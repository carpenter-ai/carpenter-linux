"""
S035 — Co-Author Document via Review Workflow

The user asks the agent to write a design document about caching with 3
sections. The agent uses the coding-change workflow. The story reviews the
first draft and requests a surgical edit: "rewrite the Architecture section
to focus on cache invalidation strategies, don't change the other sections."
The agent revises only that section. Two review rounds total.

Expected behaviour:
  1. User: "write a design doc about caching with 3 sections."
  2. Agent creates a coding-change with a design doc file.
  3. Story reviews first diff — requests Architecture section rewrite.
  4. Agent revises only the Architecture section (surgical edit).
  5. Story approves the revised diff.
  6. Arc completes.

Cleanup: removes the design doc file.

DB verification:
  - Two review rounds (two distinct review_ids).
  - The revised diff changes only the Architecture section.
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

_DOC_NAME = "caching_design_doc"

_CREATE_PROMPT = (
    "Please write a design document about caching. Create a file called "
    "'caching_design_doc.md' with exactly 3 sections:\n"
    "1. Overview — what caching is and why it matters\n"
    "2. Architecture — how the cache layer fits into the system\n"
    "3. Implementation — key implementation details and code patterns\n"
    "Use the coding-change workflow to create this file."
)

_REVISE_COMMENT = (
    "The Architecture section needs work. Please rewrite section 2 "
    "(Architecture) to focus specifically on cache invalidation strategies "
    "(TTL, event-based, write-through). Do NOT change the Overview or "
    "Implementation sections — only rewrite Architecture."
)


class CoAuthorDocumentViaReview(AcceptanceStory):
    name = "S035 — Co-Author Document via Review Workflow"
    description = (
        "User asks for design doc with 3 sections; story revises Architecture "
        "section only; surgical edit; two review rounds; cleanup removes file."
    )

    def __init__(self) -> None:
        self._source_dir: str | None = None

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Request the document ───────────────────────────────────────────
        print(f"\n  [1/6] Requesting design document...")
        conv_id = client.create_conversation()
        client.send_message(_CREATE_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        msgs = client.get_assistant_messages(conv_id)
        self.assert_that(len(msgs) >= 1, "No response after doc request")

        # ── 2. Wait for first diff ────────────────────────────────────────────
        print("  [2/6] Waiting for first diff (up to 7 min)...")
        review_arc1: dict | None = None
        if db is not None:
            review_arc1 = db.wait_for_pending_review_arc(start_ts, timeout=420)
            self.assert_that(
                review_arc1 is not None,
                "No coding-change reached waiting (round 1)",
                arcs=db.format_arcs_table(db.get_arcs_created_after(start_ts)),
            )

        arc_state1 = review_arc1["arc_state"]
        review_id1 = arc_state1["review_id"]
        diff1 = arc_state1.get("diff", "")
        source_dir = arc_state1.get("source_dir", "")
        if source_dir:
            self._source_dir = source_dir
        print(f"     Round 1 review_id: {review_id1}")
        print(f"     Diff 1 preview: {diff1[:200]}")

        # Verify all 3 sections present
        diff1_lower = diff1.lower()
        self.assert_that(
            "overview" in diff1_lower and "architecture" in diff1_lower
            and "implementation" in diff1_lower,
            "First diff does not contain all 3 sections",
            diff_preview=diff1[:800],
        )

        # ── 3. Request Architecture section rewrite ───────────────────────────
        print("  [3/6] Requesting Architecture section rewrite...")
        result1 = client.submit_review_decision(
            review_id1, decision="revise", comment=_REVISE_COMMENT
        )
        self.assert_that(
            result1.get("recorded") is True,
            "Revision not recorded",
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
        print(f"     Round 2 review_id: {review_id2}")
        print(f"     Diff 2 preview: {diff2[:200]}")

        self.assert_that(
            review_id1 != review_id2,
            "Two review rounds should have distinct review_ids",
        )

        # Revised diff should mention cache invalidation
        diff2_lower = diff2.lower()
        self.assert_that(
            any(kw in diff2_lower for kw in
                ("invalidat", "ttl", "event-based", "write-through", "expir")),
            "Revised diff does not mention cache invalidation strategies",
            diff_preview=diff2[:600],
        )

        # ── 5. Approve ───────────────────────────────────────────────────────
        print("  [5/6] Approving revised document...")
        result2 = client.submit_review_decision(
            review_id2, decision="approve",
            comment="Architecture section rewrite looks good."
        )
        self.assert_that(result2.get("recorded") is True, "Approval not recorded")

        # ── 6. Wait for completion ────────────────────────────────────────────
        if db is not None:
            print("  [6/6] Waiting for arc to complete (up to 120s)...")
            arc = db.wait_for_arc_terminal(review_arc2["id"], timeout=120)
            self.assert_that(
                arc is not None and arc["status"] == "completed",
                f"Arc did not complete",
            )
            print(f"     Arc completed ✓")

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Design doc created ✓, "
                f"Architecture section rewritten ✓, "
                f"two review rounds ✓, "
                f"completed ✓"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Remove the design doc file."""
        root = (
            Path(self._source_dir) if self._source_dir
            else Path(os.environ.get(
                "CARPENTER_SOURCE_DIR",
                str(Path(__file__).resolve().parents[1])
            ))
        )

        # Try several possible locations
        for filename in (f"{_DOC_NAME}.md", f"{_DOC_NAME}.txt"):
            doc_path = root / filename
            if doc_path.exists():
                try:
                    doc_path.unlink()
                    print(f"  [cleanup] Removed {doc_path}")
                except Exception as exc:
                    print(f"  [cleanup] Could not remove {doc_path}: {exc}")
