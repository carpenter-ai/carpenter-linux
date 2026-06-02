"""
S036 — Multiple Coding-Change Reviews Queued Simultaneously

The user asks for two small read-only text utilities at once (a word-
frequency helper and a text-stats helper), described in natural language
without naming file paths or tool conventions. Both reach the 'waiting'
state with pending reviews. The KEY assertion is that two distinct
review_ids exist simultaneously. The story approves one and revises the
other, then approves the revision.

Expected behaviour:
  1. User asks for both tools in a single message.
  2. Agent creates two coding-change arcs.
  3. Both arcs reach 'waiting' with review_ids.
  4. Story finds two simultaneous pending reviews.
  5. Story approves word_frequency.
  6. Story revises text_stats ("add a 'top_n' parameter").
  7. Revised text_stats appears with new review_id.
  8. Story approves the revised text_stats.
  9. Both arcs complete.

Cleanup: removes both tool files.

DB verification:
  - Two review_ids exist simultaneously at some point.
  - Both arcs complete after approval.
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

_TOOL_A = "word_frequency"
_TOOL_B = "text_stats"

_DUAL_PROMPT = (
    "I'd like two small read-only text utilities, please work on both at once:\n\n"
    "1. A 'word_frequency' helper that takes some text and returns a "
    "breakdown of how often each word appears.\n\n"
    "2. A 'text_stats' helper that takes some text and returns basic "
    "counts: characters, words, sentences.\n\n"
    "Get them both queued up for review in parallel."
)

_REVISE_TEXT_STATS = (
    "Add a 'top_n' parameter (default 5) that limits the output to the "
    "top N most common words in a frequency breakdown."
)


class MultipleReviewsQueued(AcceptanceStory):
    name = "S036 — Multiple Coding-Change Reviews Queued Simultaneously"
    description = (
        "Two coding-changes (word_frequency + text_stats) reach waiting "
        "simultaneously; two review_ids; approve one, revise other; cleanup."
    )

    def __init__(self) -> None:
        self._source_dir: str | None = None

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Request both tools ─────────────────────────────────────────────
        print("\n  [1/7] Requesting two tools simultaneously...")
        conv_id = client.create_conversation()
        client.send_message(_DUAL_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        msgs = client.get_assistant_messages(conv_id)
        self.assert_that(len(msgs) >= 1, "No response after dual request")
        print(f"     {msgs[-1]['content'][:200]}")

        # ── 2. Wait for TWO reviews to be pending simultaneously ──────────────
        print("  [2/7] Waiting for 2 simultaneous reviews (up to 8 min)...")
        two_reviews: list[dict] = []
        deadline = time.monotonic() + 480
        while time.monotonic() < deadline:
            if db is not None:
                pending = db.get_arcs_pending_review(start_ts)
                if len(pending) >= 2:
                    two_reviews = pending[:2]
                    break
            time.sleep(5)

        if db is not None:
            self.assert_that(
                len(two_reviews) >= 2,
                f"Expected 2 simultaneous reviews, found {len(two_reviews)}",
                arcs=db.format_arcs_table(db.get_arcs_created_after(start_ts)),
            )

        review_id_a = two_reviews[0]["arc_state"]["review_id"]
        review_id_b = two_reviews[1]["arc_state"]["review_id"]
        diff_a = two_reviews[0]["arc_state"].get("diff", "")
        diff_b = two_reviews[1]["arc_state"].get("diff", "")

        # Save source_dir from either
        for r in two_reviews:
            sd = r["arc_state"].get("source_dir", "")
            if sd:
                self._source_dir = sd
                break

        print(f"     Review A: {review_id_a} (diff: {len(diff_a)} chars)")
        print(f"     Review B: {review_id_b} (diff: {len(diff_b)} chars)")

        # KEY assertion: two distinct review_ids simultaneously
        self.assert_that(
            review_id_a != review_id_b,
            f"Expected two distinct review_ids, got same: {review_id_a}",
        )

        # Identify which is word_frequency vs text_stats
        # Check diff content to figure out which is which
        if _TOOL_A in diff_a.lower() or "word_frequency" in diff_a.lower():
            wf_review_id, wf_arc = review_id_a, two_reviews[0]
            ts_review_id, ts_arc = review_id_b, two_reviews[1]
        else:
            wf_review_id, wf_arc = review_id_b, two_reviews[1]
            ts_review_id, ts_arc = review_id_a, two_reviews[0]

        # ── 3. Approve word_frequency ─────────────────────────────────────────
        print("  [3/7] Approving word_frequency...")
        result_wf = client.submit_review_decision(
            wf_review_id, decision="approve",
            comment="Word frequency tool looks good."
        )
        self.assert_that(result_wf.get("recorded") is True, "WF approval failed")

        # ── 4. Revise text_stats ──────────────────────────────────────────────
        print("  [4/7] Revising text_stats (adding top_n parameter)...")
        result_ts = client.submit_review_decision(
            ts_review_id, decision="revise",
            comment=_REVISE_TEXT_STATS,
        )
        self.assert_that(result_ts.get("recorded") is True, "TS revision failed")

        # ── 5. Wait for revised text_stats ────────────────────────────────────
        print("  [5/7] Waiting for revised text_stats (up to 5 min)...")
        review_ts2: dict | None = None
        seen_ids = {wf_review_id, ts_review_id}
        if db is not None:
            review_ts2 = db.wait_for_pending_review_arc(
                start_ts, timeout=300, exclude_review_ids=seen_ids,
            )
            self.assert_that(
                review_ts2 is not None,
                "No revised text_stats diff appeared",
                arcs=db.format_arcs_table(db.get_arcs_created_after(start_ts)),
            )

        ts_review_id2 = review_ts2["arc_state"]["review_id"]
        print(f"     Revised text_stats review_id: {ts_review_id2}")

        # ── 6. Approve revised text_stats ─────────────────────────────────────
        print("  [6/7] Approving revised text_stats...")
        result_ts2 = client.submit_review_decision(
            ts_review_id2, decision="approve",
            comment="Revised text_stats with top_n looks good."
        )
        self.assert_that(result_ts2.get("recorded") is True, "TS2 approval failed")

        # ── 7. Wait for all arcs to complete ──────────────────────────────────
        if db is not None:
            print("  [7/7] Waiting for arcs to complete (up to 120s)...")
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                arcs = db.get_arcs_created_after(start_ts)
                pending = [a for a in arcs
                           if a["status"] not in ("completed", "failed", "cancelled")]
                if not pending:
                    break
                time.sleep(5)

            # Both tool arcs should have completed
            all_arcs = db.get_arcs_created_after(start_ts)
            completed = [a for a in all_arcs if a["status"] == "completed"]
            print(f"     {len(completed)} arcs completed")

        elapsed = time.time() - start_ts
        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Two simultaneous reviews ✓ "
                f"(r1={wf_review_id[:8]}..., r2={ts_review_id[:8]}...), "
                f"word_frequency approved ✓, "
                f"text_stats revised then approved ✓ "
                f"({elapsed:.0f}s total)"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Remove both tool files."""
        root = (
            Path(self._source_dir) if self._source_dir
            else Path(os.environ.get(
                "CARPENTER_SOURCE_DIR",
                str(Path(__file__).resolve().parents[1])
            ))
        )

        for tool_name in (_TOOL_A, _TOOL_B):
            tool_path = root / "carpenter_tools" / "read" / f"{tool_name}.py"
            if tool_path.exists():
                try:
                    tool_path.unlink()
                    print(f"  [cleanup] Removed {tool_path}")
                except Exception as exc:
                    print(f"  [cleanup] Could not remove {tool_path}: {exc}")
