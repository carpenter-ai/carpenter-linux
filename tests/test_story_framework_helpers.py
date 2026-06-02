"""Unit tests for the user_stories.framework helpers added in
refactor/story-pattern-extraction:

  - DBInspector.wait_for_pending_review_arc
  - DBInspector.wait_for_arc_terminal
  - AcceptanceStory.assert_no_failed_arcs_since

These exercise the helpers' control flow directly against a stub
DBInspector — we don't spin up the full server. The tests are fast and
deterministic so they're safe to run in CI without the test-runner
wrapper.
"""

import time
from typing import Any
from unittest.mock import patch

import pytest

from user_stories.framework import (
    AcceptanceStory,
    AssertionFailure,
    DBInspector,
)


# ---------------------------------------------------------------------------
# wait_for_pending_review_arc
# ---------------------------------------------------------------------------


def _make_inspector_with_pending(pending_seq: "list[list[dict]]") -> DBInspector:
    """Construct a DBInspector whose ``get_arcs_pending_review`` returns
    each element of ``pending_seq`` on successive calls (sticky on last).

    The dbinspector is real but constructed with a bogus db_path; we then
    monkey-patch ``get_arcs_pending_review`` so no SQLite is touched.
    """
    db = DBInspector("/nonexistent")
    state = {"i": 0}

    def fake(_since_ts: float) -> list[dict]:
        i = state["i"]
        state["i"] = min(i + 1, len(pending_seq) - 1)
        return pending_seq[i]

    db.get_arcs_pending_review = fake  # type: ignore[assignment]
    return db


def test_wait_for_pending_review_arc_returns_first_match() -> None:
    arc = {"id": 7, "arc_state": {"review_id": "r-7"}}
    db = _make_inspector_with_pending([[], [], [arc]])
    with patch("time.sleep"):
        result = db.wait_for_pending_review_arc(
            since_ts=0.0, timeout=10, poll_interval=0.0
        )
    assert result is arc


def test_wait_for_pending_review_arc_skips_excluded_review_id() -> None:
    old = {"id": 1, "arc_state": {"review_id": "r-old"}}
    new = {"id": 2, "arc_state": {"review_id": "r-new"}}
    # First poll: only old visible. Second poll: both. Helper must skip old.
    db = _make_inspector_with_pending([[old], [old, new]])
    with patch("time.sleep"):
        result = db.wait_for_pending_review_arc(
            since_ts=0.0, timeout=10, poll_interval=0.0,
            exclude_review_ids={"r-old"},
        )
    assert result is new


def test_wait_for_pending_review_arc_returns_none_on_timeout() -> None:
    db = _make_inspector_with_pending([[]])
    with patch("time.sleep"):
        result = db.wait_for_pending_review_arc(
            since_ts=0.0, timeout=0.01, poll_interval=0.0
        )
    assert result is None


# ---------------------------------------------------------------------------
# wait_for_arc_terminal
# ---------------------------------------------------------------------------


def _make_inspector_with_arc_seq(arc_seq: "list[dict | None]") -> DBInspector:
    db = DBInspector("/nonexistent")
    state = {"i": 0}

    def fake_get_arc(_arc_id: int) -> "dict | None":
        i = state["i"]
        state["i"] = min(i + 1, len(arc_seq) - 1)
        return arc_seq[i]

    db.get_arc = fake_get_arc  # type: ignore[assignment]
    return db


def test_wait_for_arc_terminal_returns_when_completed() -> None:
    db = _make_inspector_with_arc_seq([
        {"id": 5, "status": "running"},
        {"id": 5, "status": "running"},
        {"id": 5, "status": "completed"},
    ])
    with patch("time.sleep"):
        result = db.wait_for_arc_terminal(arc_id=5, timeout=10, poll_interval=0.0)
    assert result is not None
    assert result["status"] == "completed"


def test_wait_for_arc_terminal_returns_failed_status() -> None:
    db = _make_inspector_with_arc_seq([{"id": 9, "status": "failed"}])
    with patch("time.sleep"):
        result = db.wait_for_arc_terminal(arc_id=9, timeout=10, poll_interval=0.0)
    assert result is not None
    assert result["status"] == "failed"


def test_wait_for_arc_terminal_returns_last_seen_on_timeout() -> None:
    db = _make_inspector_with_arc_seq([{"id": 3, "status": "running"}])
    with patch("time.sleep"):
        result = db.wait_for_arc_terminal(
            arc_id=3, timeout=0.01, poll_interval=0.0
        )
    # Non-terminal on timeout — returned for caller diagnostics.
    assert result is not None
    assert result["status"] == "running"


def test_wait_for_arc_terminal_handles_missing_arc() -> None:
    db = _make_inspector_with_arc_seq([None])
    with patch("time.sleep"):
        result = db.wait_for_arc_terminal(
            arc_id=999, timeout=0.01, poll_interval=0.0
        )
    assert result is None


# ---------------------------------------------------------------------------
# assert_no_failed_arcs_since
# ---------------------------------------------------------------------------


class _DummyStory(AcceptanceStory):
    name = "dummy"


class _FakeDB:
    def __init__(self, arcs: list[dict]) -> None:
        self._arcs = arcs

    def get_arcs_created_after(self, _ts: float) -> list[dict]:
        return self._arcs

    def format_arcs_table(self, arcs: list[dict]) -> str:
        return f"(table of {len(arcs)} arcs)"


def test_assert_no_failed_arcs_since_passes_on_clean() -> None:
    story = _DummyStory()
    db = _FakeDB([
        {"id": 1, "status": "completed", "name": "ok"},
        {"id": 2, "status": "running", "name": "in-flight"},
    ])
    story.assert_no_failed_arcs_since(db, since_ts=0.0)  # type: ignore[arg-type]


def test_assert_no_failed_arcs_since_raises_on_failure() -> None:
    story = _DummyStory()
    db = _FakeDB([
        {"id": 1, "status": "completed", "name": "ok"},
        {"id": 2, "status": "failed", "name": "bad"},
    ])
    with pytest.raises(AssertionFailure) as exc_info:
        story.assert_no_failed_arcs_since(db, since_ts=0.0)  # type: ignore[arg-type]
    assert "1 arc(s) ended in failed/cancelled" in exc_info.value.message


def test_assert_no_failed_arcs_since_uses_workflow_label() -> None:
    story = _DummyStory()
    db = _FakeDB([
        {"id": 1, "status": "cancelled", "name": "x"},
    ])
    with pytest.raises(AssertionFailure) as exc_info:
        story.assert_no_failed_arcs_since(
            db, since_ts=0.0, workflow_label="Add workflow"  # type: ignore[arg-type]
        )
    assert "Add workflow had 1 failed/cancelled arc(s)" in exc_info.value.message


def test_assert_no_failed_arcs_since_noop_when_db_none() -> None:
    story = _DummyStory()
    story.assert_no_failed_arcs_since(None, since_ts=0.0)  # should not raise
