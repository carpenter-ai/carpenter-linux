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


# ---------------------------------------------------------------------------
# get_workflow_selected_event_after
# ---------------------------------------------------------------------------


def _make_inspector_with_query(rows_seq: "list[list[dict]]") -> DBInspector:
    """Inspector whose ``_query`` returns successive elements of rows_seq."""
    db = DBInspector("/nonexistent")
    state = {"i": 0}

    def fake(_sql: str, _params: tuple = ()) -> list[dict]:
        i = state["i"]
        state["i"] = min(i + 1, len(rows_seq) - 1)
        return rows_seq[i]

    db._query = fake  # type: ignore[assignment]
    return db


def test_get_workflow_selected_event_after_returns_decoded_details() -> None:
    db = _make_inspector_with_query([
        [{"id": 42, "details_json": '{"chosen_template": "yaml-change", '
                                    '"force_human": false, '
                                    '"categories": ["yaml"]}'}],
    ])
    result = db.get_workflow_selected_event_after(since_ts=0.0)
    assert result is not None
    assert result["chosen_template"] == "yaml-change"
    assert result["force_human"] is False
    assert result["_id"] == 42


def test_get_workflow_selected_event_after_returns_none_when_missing() -> None:
    db = _make_inspector_with_query([[]])
    assert db.get_workflow_selected_event_after(since_ts=0.0) is None


def test_get_workflow_selected_event_after_handles_empty_json() -> None:
    """An empty/null details_json should round-trip to ``{}`` (plus _id)."""
    db = _make_inspector_with_query([[{"id": 9, "details_json": None}]])
    result = db.get_workflow_selected_event_after(since_ts=0.0)
    assert result == {"_id": 9}


# ---------------------------------------------------------------------------
# get_verification_arcs_for / get_verification_arc_by_role
# ---------------------------------------------------------------------------


def test_get_verification_arc_by_role_finds_via_step_role() -> None:
    """Helper finds verifier rows linked by verification_target_id."""
    db = _make_inspector_with_query([
        [{"id": 5324, "name": "lint-yaml", "step_role": "lint-yaml",
          "status": "completed", "step_order": 0,
          "verification_target_id": 5323}],
    ])
    result = db.get_verification_arc_by_role(5323, "lint-yaml")
    assert result is not None
    assert result["name"] == "lint-yaml"
    assert result["status"] == "completed"


def test_get_verification_arc_by_role_finds_via_prefixed_step_role() -> None:
    """Caller passes the raw role; stored step_role is ``verifier-<role>``.

    This is the real shape of data in carpenter-core: ``verify-kb-format``
    is the step name (and arc name), but
    ``carpenter/core/arcs/verification.py`` persists ``step_role`` as
    ``verifier-kb-format``. The first SQL query must hit on the prefixed
    variant — no name-fallback needed.
    """
    db = _make_inspector_with_query([
        [{"id": 5420, "name": "verify-kb-format",
          "step_role": "verifier-kb-format",
          "status": "completed", "step_order": 0,
          "verification_target_id": 5419}],
    ])
    result = db.get_verification_arc_by_role(5419, "verify-kb-format")
    assert result is not None
    assert result["id"] == 5420
    assert result["step_role"] == "verifier-kb-format"


def test_get_verification_arc_by_role_accepts_already_prefixed_role() -> None:
    """If a caller passes the prefixed role, helper still works (no
    ``verifier-verifier-foo`` double-prefix)."""
    db = _make_inspector_with_query([
        [{"id": 7, "name": "lint-yaml", "step_role": "verifier-lint-yaml",
          "status": "completed", "step_order": 0,
          "verification_target_id": 6}],
    ])
    result = db.get_verification_arc_by_role(6, "verifier-lint-yaml")
    assert result is not None
    assert result["id"] == 7


def test_get_verification_arc_by_role_step_role_query_uses_both_forms() -> None:
    """The single step_role query must include both raw and prefixed
    variants in its parameters — that's the whole point of the fix."""
    captured: dict[str, Any] = {}

    db = DBInspector("/nonexistent")

    def fake(sql: str, params: tuple = ()) -> list[dict]:
        captured.setdefault("calls", []).append((sql, params))
        return []

    db._query = fake  # type: ignore[assignment]
    db.get_verification_arc_by_role(42, "lint-yaml")
    first_sql, first_params = captured["calls"][0]
    assert "step_role IN" in first_sql
    assert "lint-yaml" in first_params
    assert "verifier-lint-yaml" in first_params


def test_get_verification_arc_by_role_falls_back_to_name() -> None:
    """When step_role lookup yields nothing (defense-in-depth for arcs
    constructed without a step_role), fall back to name match."""
    # First call (by step_role) returns empty; second (by name) hits.
    db = _make_inspector_with_query([
        [],
        [{"id": 99, "name": "verify-kb-format", "step_role": None,
          "status": "completed", "step_order": 0,
          "verification_target_id": 42}],
    ])
    result = db.get_verification_arc_by_role(42, "verify-kb-format")
    assert result is not None
    assert result["id"] == 99


def test_get_verification_arc_by_role_returns_none_when_missing() -> None:
    db = _make_inspector_with_query([[], []])
    assert db.get_verification_arc_by_role(1, "nothing") is None


def test_get_verification_arcs_for_returns_ordered_list() -> None:
    rows = [
        {"id": 11, "name": "lint-yaml", "step_order": 0,
         "verification_target_id": 5},
        {"id": 12, "name": "judge", "step_order": 1,
         "verification_target_id": 5},
    ]
    db = _make_inspector_with_query([rows])
    result = db.get_verification_arcs_for(5)
    assert [r["id"] for r in result] == [11, 12]


# ---------------------------------------------------------------------------
# ChangeReviewStory scaffold
# ---------------------------------------------------------------------------


from user_stories.framework import (  # noqa: E402 — imported here so the
    ChangeReviewStory,                # other tests above don't pay the
    CarpenterClient,                  # ChangeReviewStory cost when only
)                                     # the helpers are under test.


class _StubClient:
    """Minimal stand-in for CarpenterClient that the scaffold can call."""

    def __init__(self, init_response: str = "OK, working on the change.") -> None:
        self._init_response = init_response
        self.created = 0
        self.sent: list[tuple[int, str]] = []
        self.approvals: list[tuple[str, str, str]] = []
        self.pending_cleared = False

    def create_conversation(self) -> int:
        self.created += 1
        return 100 + self.created

    def send_message(self, text: str, conv_id: int) -> None:
        self.sent.append((conv_id, text))

    def wait_for_pending_to_clear(self, conv_id: int, timeout: int = 60) -> None:
        self.pending_cleared = True

    def get_assistant_messages(self, conv_id: int) -> list[dict]:
        return [{"role": "assistant", "content": self._init_response}]

    def submit_review_decision(
        self, review_id: str, decision: str, comment: str = "",
    ) -> dict:
        self.approvals.append((review_id, decision, comment))
        return {"recorded": True}


class _StubDB:
    """Minimal stand-in for DBInspector covering the scaffold's calls."""

    def __init__(
        self,
        review_arc: "dict | None",
        final_arc: "dict | None",
        all_arcs: list[dict] | None = None,
    ) -> None:
        self._review = review_arc
        self._final = final_arc
        self._all = all_arcs or []

    def wait_for_pending_review_arc(self, *_a, **_kw):
        return self._review

    def wait_for_arc_terminal(self, *_a, **_kw):
        return self._final

    def get_arc(self, arc_id):
        return self._final

    def get_arcs_created_after(self, _ts):
        return self._all

    def format_arcs_table(self, arcs):
        return f"(table of {len(arcs)} arcs)"


class _MinimalScaffoldStory(ChangeReviewStory):
    """Concrete subclass that records the hooks it ran."""

    name = "test-minimal-scaffold"
    artifact_prefix = "test"
    request_text = "Please make a tiny change."
    ack_keywords = ("change", "working", "ok")

    inspect_calls: list[tuple[str, dict]]
    post_apply_calls: list[tuple[int, dict]]

    def __init__(self) -> None:
        self.inspect_calls = []
        self.post_apply_calls = []

    def inspect_diff(self, diff: str, arc_state: dict) -> None:
        self.inspect_calls.append((diff, arc_state))
        super().inspect_diff(diff, arc_state)

    def post_apply(self, client, db, conv_id, review_arc) -> None:
        self.post_apply_calls.append((conv_id, review_arc))


def test_change_review_story_runs_through_happy_path() -> None:
    review_arc = {
        "id": 11,
        "arc_state": {"review_id": "rid-11", "diff": "+ new line\n"},
    }
    final_arc = {"id": 11, "status": "completed", "name": "x"}
    story = _MinimalScaffoldStory()
    client = _StubClient()
    db = _StubDB(
        review_arc=review_arc,
        final_arc=final_arc,
        all_arcs=[final_arc],
    )

    result = story.run(client, db)  # type: ignore[arg-type]

    assert result.passed is True
    # Prompt was sent once.
    assert len(client.sent) == 1
    assert client.sent[0][1] == "Please make a tiny change."
    # Approval was submitted with the configured comment.
    assert client.approvals == [("rid-11", "approve", "Approved.")]
    # Hooks fired exactly once each, with the expected args.
    assert len(story.inspect_calls) == 1
    assert story.inspect_calls[0][0] == "+ new line\n"
    assert len(story.post_apply_calls) == 1
    assert story.post_apply_calls[0][1] is review_arc


def test_change_review_story_rejects_empty_request_text() -> None:
    class _NoPromptStory(ChangeReviewStory):
        name = "no-prompt"
        artifact_prefix = "noprompt"
        # request_text intentionally left as default ""

    story = _NoPromptStory()
    client = _StubClient()
    db = _StubDB(review_arc=None, final_arc=None)

    with pytest.raises(RuntimeError) as exc_info:
        story.run(client, db)  # type: ignore[arg-type]
    assert "request_text" in str(exc_info.value)


def test_change_review_story_fails_on_missing_ack() -> None:
    review_arc = {
        "id": 11,
        "arc_state": {"review_id": "rid-11", "diff": "+x\n"},
    }
    story = _MinimalScaffoldStory()
    # Client returns a response that has zero overlap with ack_keywords.
    client = _StubClient(init_response="zzz qqq")
    db = _StubDB(review_arc=review_arc, final_arc=None)

    with pytest.raises(AssertionFailure) as exc_info:
        story.run(client, db)  # type: ignore[arg-type]
    assert "Initial response does not acknowledge" in exc_info.value.message


def test_change_review_story_fails_when_arc_does_not_reach_terminal() -> None:
    review_arc = {
        "id": 22,
        "arc_state": {"review_id": "rid-22", "diff": "diff content"},
    }
    final_arc = {"id": 22, "status": "running", "name": "stuck"}
    story = _MinimalScaffoldStory()
    client = _StubClient()
    db = _StubDB(
        review_arc=review_arc,
        final_arc=final_arc,
        all_arcs=[final_arc],
    )

    with pytest.raises(AssertionFailure) as exc_info:
        story.run(client, db)  # type: ignore[arg-type]
    assert "did not complete" in exc_info.value.message
