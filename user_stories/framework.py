"""
Carpenter Acceptance Test Framework

Provides the building blocks for writing acceptance stories:
- CarpenterClient  — HTTP interaction with the running server
- DBInspector          — Direct SQLite read access for verifying internal state
- AcceptanceStory      — Base class for acceptance stories
- StoryResult          — Rich result container
- AssertionFailure     — Exception raised by failed assertions
"""

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class AssertionFailure(Exception):
    """Raised by story assertions to signal a test failure."""
    message: str
    diagnostics: dict = field(default_factory=dict)


@dataclass
class StoryResult:
    name: str
    passed: bool
    message: str = ""
    error: str = ""
    diagnostics: dict = field(default_factory=dict)
    duration_s: float = 0.0
    skipped: bool = False

    def __str__(self) -> str:
        if self.skipped:
            status = "SKIP"
        else:
            status = "PASS" if self.passed else "FAIL"
        return f"[{status}] {self.name} ({self.duration_s:.1f}s)"


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class CarpenterClient:
    """HTTP client for interacting with Carpenter's chat API.

    Transient-error retry policy
    ----------------------------
    Both ``_get`` and ``_post`` retry on:
    - ``httpx.ReadTimeout`` / ``httpx.ConnectError`` /
      ``httpx.RemoteProtocolError`` / ``httpx.ConnectTimeout`` /
      ``httpx.ReadError``  (network glitches, server restart mid-call,
      connection reset)
    - HTTP 5xx responses                 (server-side hiccup)
    - HTTP 408 / 429                     (transient)
    - HTTP 400 — retried at most once. Most 400s are real client errors,
      but the carpenter server has produced occasional transient 400s
      (likely race conditions in chat init). One retry costs <1s and
      catches the transient case without papering over real bugs.

    Up to 4 attempts total with exponential backoff (0.5s, 1s, 2s) plus
    small jitter. Only persistent failures (= actual outage, not glitch)
    surface to callers.
    """

    # Status codes we treat as transient and retry up to MAX_ATTEMPTS.
    _TRANSIENT_STATUS_CODES = (408, 429, 500, 502, 503, 504)
    _RETRYABLE_EXCEPTIONS = (
        httpx.ReadTimeout,
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.RemoteProtocolError,
        httpx.ReadError,
    )
    _MAX_ATTEMPTS = 4
    _BASE_BACKOFF_S = 0.5

    def __init__(self, base_url: str, token: str | None = None, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._default_timeout = timeout
        self._headers: dict[str, str] = {}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    def _backoff_sleep(self, attempt: int) -> None:
        import random
        delay = self._BASE_BACKOFF_S * (2 ** attempt) + random.uniform(0, 0.2)
        time.sleep(delay)

    def _request_with_retries(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        **kw,
    ) -> httpx.Response:
        """Issue an HTTP request with transient-failure retries.

        Returns the final ``httpx.Response`` (which may carry a non-2xx
        status if the caller is willing to handle it — we do NOT raise
        on non-2xx; we only retry transient ones).  Raises the last
        exception only if every attempt raised a retryable network
        exception.
        """
        url = f"{self.base_url}{path}"
        kw.setdefault("timeout", self._default_timeout)
        if method == "GET":
            kw.setdefault("follow_redirects", False)

        last_exc: Exception | None = None
        last_response: httpx.Response | None = None
        # Allow at most one retry for HTTP 400.
        retried_400 = False
        for attempt in range(self._MAX_ATTEMPTS):
            try:
                if method == "GET":
                    response = httpx.get(url, headers=self._headers, **kw)
                else:
                    response = httpx.post(
                        url, json=json_body, headers=self._headers, **kw,
                    )
            except self._RETRYABLE_EXCEPTIONS as exc:
                last_exc = exc
                if attempt < self._MAX_ATTEMPTS - 1:
                    self._backoff_sleep(attempt)
                    continue
                raise

            last_response = response
            sc = response.status_code

            if sc in self._TRANSIENT_STATUS_CODES:
                if attempt < self._MAX_ATTEMPTS - 1:
                    self._backoff_sleep(attempt)
                    continue
                return response  # exhausted retries; let caller decide

            if sc == 400 and not retried_400:
                retried_400 = True
                if attempt < self._MAX_ATTEMPTS - 1:
                    self._backoff_sleep(attempt)
                    continue
                return response

            return response

        # Unreachable in practice — loop always returns or raises.
        if last_response is not None:
            return last_response
        assert last_exc is not None
        raise last_exc

    def _get(self, path: str, **kw) -> httpx.Response:
        return self._request_with_retries("GET", path, **kw)

    def _post(self, path: str, json_body: dict, **kw) -> httpx.Response:
        return self._request_with_retries(
            "POST", path, json_body=json_body, **kw,
        )

    def is_running(self) -> bool:
        try:
            r = httpx.get(f"{self.base_url}/", timeout=5, follow_redirects=False)
            return r.status_code in (200, 401, 302)
        except Exception:
            return False

    def create_conversation(self) -> int:
        """Create a fresh conversation and return its integer ID."""
        r = self._get("/new")
        if r.status_code == 302:
            loc = r.headers.get("location", "")
            params = parse_qs(urlparse(loc).query)
            if "c" in params:
                return int(params["c"][0])
        raise RuntimeError(
            f"Failed to create conversation: {r.status_code} {r.text[:200]}"
        )

    def send_message(self, text: str, conversation_id: int) -> dict:
        """Send a chat message. Returns {event_id, conversation_id}."""
        r = self._post(
            "/api/chat", json_body={"text": text, "conversation_id": conversation_id}
        )
        if r.status_code != 202:
            raise RuntimeError(
                f"POST /api/chat failed: {r.status_code} {r.text[:200]}"
            )
        return r.json()

    def is_pending(self, conversation_id: int) -> bool:
        """Return True if the AI is still processing a response."""
        r = self._get(f"/api/chat/pending?c={conversation_id}")
        r.raise_for_status()
        return r.json().get("pending", False)

    def get_history(self, conversation_id: int) -> list[dict]:
        """Return all messages for a conversation as a list of dicts."""
        r = self._get(f"/api/chat/history?conversation_id={conversation_id}")
        r.raise_for_status()
        return r.json().get("messages", [])

    def get_assistant_messages(self, conversation_id: int) -> list[dict]:
        """Return only assistant-role messages with non-empty content.

        Empty assistant messages can appear when system notifications
        (e.g. module-reload, verification-arc creation) trigger an
        invocation that produces no visible text.  Filtering them out
        prevents ``msgs[-1]`` from landing on an empty response.
        """
        return [
            m for m in self.get_history(conversation_id)
            if m["role"] == "assistant" and m.get("content")
        ]

    def wait_for_pending_to_clear(
        self, conversation_id: int, timeout: int = 60, poll_interval: float = 0.5
    ) -> None:
        """Block until the AI is no longer processing. Raises TimeoutError.

        Args:
            conversation_id: Conversation to monitor
            timeout: Maximum seconds to wait
            poll_interval: Seconds between status checks (default 0.5s)
        """
        deadline = time.monotonic() + timeout
        # Check immediately — no initial sleep needed (API is fast)
        while time.monotonic() < deadline:
            if not self.is_pending(conversation_id):
                return
            time.sleep(poll_interval)
        raise TimeoutError(
            f"AI still pending after {timeout}s for conversation {conversation_id}"
        )

    def chat(
        self,
        text: str,
        conversation_id: int | None = None,
        timeout: int = 60,
    ) -> tuple[int, str]:
        """Send a message, wait for the AI to respond.

        Returns (conversation_id, last_assistant_message_content).
        Creates a new conversation if conversation_id is None.
        """
        if conversation_id is None:
            conversation_id = self.create_conversation()
        self.send_message(text, conversation_id)
        self.wait_for_pending_to_clear(conversation_id, timeout=timeout)
        msgs = self.get_assistant_messages(conversation_id)
        if not msgs:
            raise RuntimeError("AI produced no assistant message after pending cleared")
        return conversation_id, msgs[-1]["content"]

    def wait_for_n_assistant_messages(
        self,
        conversation_id: int,
        n: int,
        timeout: int = 120,
        poll_interval: float = 1.0,
    ) -> list[dict]:
        """Poll until there are at least *n* assistant messages. Return them.

        Args:
            conversation_id: Conversation to monitor
            n: Minimum number of assistant messages to wait for
            timeout: Maximum seconds to wait
            poll_interval: Seconds between checks (default 1.0s)
        """
        deadline = time.monotonic() + timeout
        # Check immediately in case messages already exist (fast-path)
        msgs = self.get_assistant_messages(conversation_id)
        if len(msgs) >= n:
            return msgs

        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            msgs = self.get_assistant_messages(conversation_id)
            if len(msgs) >= n:
                return msgs

        raise TimeoutError(
            f"Expected ≥{n} assistant messages, only got {len(msgs)} after {timeout}s "
            f"(conversation {conversation_id})"
        )

    # Patterns that indicate an "I'm still working" acknowledgement —
    # NOT a substantive reply.  Stories that need to wait for a real
    # answer (after a long-running background arc completes) should
    # treat messages matching any of these as non-terminal.
    ACK_PATTERNS: tuple[str, ...] = (
        "in progress",
        "is in progress",
        "i'll wait",
        "i will wait",
        "i'll let you know",
        "i will let you know",
        "still working",
        "still processing",
        "still running",
        "let me know when",
        "wait for the result",
        "wait for the fetch",
        "wait for it to complete",
        "once the fetch",
        "once it completes",
        "once it is complete",
        "once it's complete",
        "once it finishes",
        "once it's done",
        "once it is done",
        "i've started",
        "i have started",
        "i've kicked off",
        "i have kicked off",
        "i've queued",
        "i have queued",
        "i've scheduled",
        "i have scheduled",
        "i've initiated",
        "i have initiated",
        "i've triggered",
        "i have triggered",
        "i've launched",
        "i have launched",
        "i've dispatched",
        "i have dispatched",
        "i'm fetching",
        "i am fetching",
        "fetching the",
        "working on it",
        "background",
        "stand by",
        "standby",
        "just a moment",
        "give me a moment",
        "one moment",
        "one sec",
        "please wait",
    )

    @classmethod
    def looks_like_ack(cls, text: str) -> bool:
        """Return True if *text* looks like a non-substantive ack reply.

        Used by stories that wait for a real answer after a background
        arc completes (e.g. an untrusted-fetch pipeline). An ack
        ("the fetch is in progress, i'll wait") satisfies the
        ``wait_for_pending_to_clear`` contract but isn't the answer
        the test is looking for.
        """
        if not text:
            return True
        low = text.lower()
        return any(p in low for p in cls.ACK_PATTERNS)

    def wait_for_non_ack_message(
        self,
        conversation_id: int,
        after_index: int,
        timeout: int = 300,
        poll_interval: float = 5.0,
        ack_patterns: tuple[str, ...] | None = None,
    ) -> dict:
        """Wait for an assistant message after *after_index* that isn't an ack.

        Polls ``get_assistant_messages(conversation_id)`` and returns
        the first message at index >= ``after_index`` whose content
        does NOT match any ack pattern.

        Args:
            conversation_id: conversation to watch
            after_index: only messages whose 0-based position in the
                assistant-message list is >= this value are considered.
                Pass ``len(get_assistant_messages(conv))`` BEFORE sending
                the prompt (or right after, to skip the initial ack).
            timeout: max seconds to wait
            poll_interval: seconds between polls
            ack_patterns: override ACK_PATTERNS for this call

        Raises:
            TimeoutError if no non-ack message appears in time.
        """
        deadline = time.monotonic() + timeout
        patterns = ack_patterns if ack_patterns is not None else self.ACK_PATTERNS

        def _is_ack(text: str) -> bool:
            if not text:
                return True
            low = text.lower()
            return any(p in low for p in patterns)

        last_msgs: list[dict] = []
        while time.monotonic() < deadline:
            msgs = self.get_assistant_messages(conversation_id)
            last_msgs = msgs
            for m in msgs[after_index:]:
                if not _is_ack(m["content"]):
                    return m
            time.sleep(poll_interval)

        # One last check after the loop
        msgs = self.get_assistant_messages(conversation_id)
        for m in msgs[after_index:]:
            if not _is_ack(m["content"]):
                return m

        preview_msgs = [
            m["content"][:120].replace("\n", " ")
            for m in last_msgs[after_index:]
        ]
        raise TimeoutError(
            f"No non-ack assistant message after index {after_index} "
            f"within {timeout}s (conversation {conversation_id}). "
            f"Saw {len(last_msgs) - after_index} ack-only messages: "
            f"{preview_msgs!r}"
        )

    def wait_for_message_matching(
        self,
        conversation_id: int,
        after_index: int,
        keywords: tuple[str, ...] | list[str],
        timeout: int = 300,
        poll_interval: float = 5.0,
        min_chars: int = 0,
        require_all: bool = False,
    ) -> dict:
        """Wait for an assistant message after *after_index* containing keyword(s).

        Useful when a single chat turn produces several intermediate
        messages (the agent narrating its work) before delivering a
        final substantive reply. By polling for keyword presence
        directly we avoid both the ack-pattern false-negative and the
        race-against-pending-clear failure mode.

        Args:
            conversation_id: conversation to watch
            after_index: only messages whose 0-based position in the
                assistant-message list is >= this value are considered.
            keywords: case-insensitive substrings to look for
            timeout: max seconds to wait
            poll_interval: seconds between polls
            min_chars: minimum content length to qualify (filters short
                "let me check X" intermediate messages)
            require_all: if True, all keywords must appear; default
                is any-of

        Returns:
            The first (oldest) qualifying message in the new tail.
        Raises:
            TimeoutError if no matching message appears in time.
        """
        deadline = time.monotonic() + timeout
        kws_low = [k.lower() for k in keywords]

        def _matches(text: str) -> bool:
            if not text or len(text) < min_chars:
                return False
            low = text.lower()
            if require_all:
                return all(k in low for k in kws_low)
            return any(k in low for k in kws_low)

        last_msgs: list[dict] = []
        while time.monotonic() < deadline:
            msgs = self.get_assistant_messages(conversation_id)
            last_msgs = msgs
            for m in msgs[after_index:]:
                if _matches(m["content"]):
                    return m
            time.sleep(poll_interval)

        # Final check after loop
        msgs = self.get_assistant_messages(conversation_id)
        for m in msgs[after_index:]:
            if _matches(m["content"]):
                return m

        preview_msgs = [
            m["content"][:200].replace("\n", " ")
            for m in last_msgs[after_index:]
        ]
        raise TimeoutError(
            f"No assistant message matching keywords {list(keywords)!r} "
            f"(min_chars={min_chars}, require_all={require_all}) "
            f"after index {after_index} within {timeout}s "
            f"(conversation {conversation_id}). "
            f"Saw {len(last_msgs) - after_index} non-matching messages: "
            f"{preview_msgs!r}"
        )

    def submit_review_decision(
        self,
        review_id: str,
        decision: str,
        comment: str = "",
    ) -> dict:
        """Submit approve/reject/revise for a pending coding-change diff review.

        Args:
            review_id: UUID from arc_state['review_id'].
            decision:  "approve", "reject", or "revise".
            comment:   Optional feedback (required when decision="revise").

        Returns:
            Server response dict with at least {"recorded": True} on success.
        """
        r = self._post(
            f"/api/review/{review_id}/decide",
            json_body={"decision": decision, "comment": comment},
        )
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Database inspector
# ---------------------------------------------------------------------------


class DBInspector:
    """Direct read-only SQLite access for verifying internal platform state.

    Opens the database in read-only mode for each query to avoid locking
    the live server's connection.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # --- Arc queries ---

    def get_arcs(self, limit: int = 50) -> list[dict]:
        return self._query(
            "SELECT * FROM arcs ORDER BY id DESC LIMIT ?", (limit,)
        )

    def get_arc(self, arc_id: int) -> dict | None:
        rows = self._query("SELECT * FROM arcs WHERE id = ?", (arc_id,))
        return rows[0] if rows else None

    def get_arc_children(self, parent_id: int) -> list[dict]:
        return self._query(
            "SELECT * FROM arcs WHERE parent_id = ? ORDER BY step_order",
            (parent_id,),
        )

    def get_arc_by_role(
        self, parent_id: int, step_role: str
    ) -> dict | None:
        """Return the child arc under ``parent_id`` whose ``step_role`` matches.

        Hides the join through ``workflow_templates`` for stories built around
        the D2 (template_name, step_role) identity. Falls back to step ``name``
        for arcs that predate the ``step_role`` column or whose template did
        not declare a role for that step. Returns the first match by
        ``step_order`` (roles are not strictly unique within a template).
        """
        rows = self._query(
            "SELECT * FROM arcs "
            "WHERE parent_id = ? AND step_role = ? "
            "ORDER BY step_order LIMIT 1",
            (parent_id, step_role),
        )
        if rows:
            return rows[0]
        # Fallback for arcs predating the step_role column.
        rows = self._query(
            "SELECT * FROM arcs "
            "WHERE parent_id = ? AND name = ? "
            "ORDER BY step_order LIMIT 1",
            (parent_id, step_role),
        )
        return rows[0] if rows else None

    def get_arc_state(self, arc_id: int) -> dict[str, Any]:
        rows = self._query(
            "SELECT key, value_json FROM arc_state WHERE arc_id = ?", (arc_id,)
        )
        return {r["key"]: json.loads(r["value_json"]) for r in rows}

    def get_arcs_created_after(self, since_ts: float) -> list[dict]:
        """Return arcs created at or after the given Unix timestamp (UTC)."""
        # SQLite stores CURRENT_TIMESTAMP as 'YYYY-MM-DD HH:MM:SS' in UTC
        since_iso = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        return self._query(
            "SELECT * FROM arcs WHERE created_at >= ? ORDER BY id", (since_iso,)
        )

    # Arc statuses that are terminal — no further work will happen on
    # the arc once it reaches one of these.
    TERMINAL_ARC_STATUSES: tuple[str, ...] = (
        "completed", "failed", "cancelled", "rejected",
    )

    def get_root_arcs_for_conversation(
        self, conversation_id: int, since_ts: float | None = None,
    ) -> list[dict]:
        """Return root arcs (parent_id IS NULL) linked to a conversation.

        Uses the ``conversation_arcs`` join table. Optionally filters to
        arcs created at/after ``since_ts``.
        """
        if since_ts is not None:
            since_iso = datetime.fromtimestamp(
                since_ts, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S")
            return self._query(
                "SELECT a.* FROM arcs a "
                "JOIN conversation_arcs ca ON ca.arc_id = a.id "
                "WHERE ca.conversation_id = ? "
                "AND a.parent_id IS NULL "
                "AND a.created_at >= ? "
                "ORDER BY a.id",
                (conversation_id, since_iso),
            )
        return self._query(
            "SELECT a.* FROM arcs a "
            "JOIN conversation_arcs ca ON ca.arc_id = a.id "
            "WHERE ca.conversation_id = ? AND a.parent_id IS NULL "
            "ORDER BY a.id",
            (conversation_id,),
        )

    def wait_for_arcs_terminal(
        self,
        arc_ids: list[int] | set[int],
        timeout: int = 300,
        poll_interval: float = 5.0,
    ) -> dict[int, str]:
        """Block until every arc in ``arc_ids`` reaches a terminal status.

        Returns a dict ``{arc_id: status}`` once all are terminal. If
        the deadline expires before all are terminal, raises
        ``TimeoutError`` with the still-pending arcs listed.
        """
        ids = list(arc_ids)
        if not ids:
            return {}
        deadline = time.monotonic() + timeout
        placeholders = ",".join("?" * len(ids))
        last_pending: list[dict] = []
        while time.monotonic() < deadline:
            rows = self._query(
                f"SELECT id, status, name FROM arcs WHERE id IN ({placeholders})",
                tuple(ids),
            )
            pending = [
                r for r in rows
                if r["status"] not in self.TERMINAL_ARC_STATUSES
            ]
            last_pending = pending
            if not pending:
                return {r["id"]: r["status"] for r in rows}
            time.sleep(poll_interval)

        rows = self._query(
            f"SELECT id, status, name FROM arcs WHERE id IN ({placeholders})",
            tuple(ids),
        )
        pending = [
            r for r in rows
            if r["status"] not in self.TERMINAL_ARC_STATUSES
        ]
        if not pending:
            return {r["id"]: r["status"] for r in rows}
        pending_descr = [
            f"#{r['id']} {r['name'][:30]!r} status={r['status']}"
            for r in pending
        ]
        raise TimeoutError(
            f"{len(pending)} arc(s) still non-terminal after {timeout}s: "
            f"{pending_descr}"
        )

    def get_arc_history(self, arc_id: int) -> list[dict]:
        return self._query(
            "SELECT * FROM arc_history WHERE arc_id = ? ORDER BY id", (arc_id,)
        )

    def wait_for_pending_review_arc(
        self,
        since_ts: float,
        timeout: int = 300,
        poll_interval: float = 5.0,
        exclude_review_ids: "set[str] | frozenset[str]" = frozenset(),
    ) -> dict | None:
        """Block until a coding-change arc is in 'waiting' state with a review_id.

        Polls ``get_arcs_pending_review(since_ts)`` and returns the first
        arc whose ``review_id`` is NOT in ``exclude_review_ids`` (use this
        to skip a review you already handled when waiting for a revised
        diff). Returns ``None`` on timeout — callers can decide whether
        that's a failure or not (most call sites assert separately so
        they can attach their own diagnostics).

        Returned dict has the ``arc_state`` key already populated, matching
        ``get_arcs_pending_review`` output.
        """
        deadline = time.monotonic() + timeout
        excluded = frozenset(exclude_review_ids)
        while time.monotonic() < deadline:
            pending = self.get_arcs_pending_review(since_ts)
            for arc in pending:
                rid = arc["arc_state"].get("review_id")
                if rid not in excluded:
                    return arc
            time.sleep(poll_interval)
        return None

    def wait_for_arc_terminal(
        self,
        arc_id: int,
        timeout: int = 120,
        poll_interval: float = 3.0,
    ) -> dict | None:
        """Block until a single arc reaches a terminal status.

        Returns the arc dict once status is terminal, or the last-seen
        arc dict on timeout (or ``None`` if the arc never existed).
        Callers assert against the returned status separately so they
        can attach their own diagnostics.
        """
        deadline = time.monotonic() + timeout
        last_arc: dict | None = None
        while time.monotonic() < deadline:
            last_arc = self.get_arc(arc_id)
            if last_arc and last_arc["status"] in self.TERMINAL_ARC_STATUSES:
                return last_arc
            time.sleep(poll_interval)
        return last_arc

    # --- Message queries ---

    def get_messages(self, conversation_id: int) -> list[dict]:
        return self._query(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        )

    def get_arc_messages(self, conversation_id: int) -> list[dict]:
        """Return messages that were sent by arc executors (arc_id IS NOT NULL)."""
        return self._query(
            "SELECT * FROM messages "
            "WHERE conversation_id = ? AND arc_id IS NOT NULL ORDER BY id",
            (conversation_id,),
        )

    # --- Coding-change / review queries ---

    def get_arcs_pending_review(self, since_ts: float) -> list[dict]:
        """Return arcs that are waiting for human review.

        These are arcs in 'waiting' status that have a 'review_id' key in
        their arc_state, created at or after since_ts.  Each returned dict
        includes an extra 'arc_state' key containing the full state dict.
        """
        since_iso = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        rows = self._query(
            "SELECT a.* FROM arcs a "
            "JOIN arc_state s ON s.arc_id = a.id "
            "WHERE a.status = 'waiting' AND s.key = 'review_id' "
            "AND a.created_at >= ? "
            "ORDER BY a.id",
            (since_iso,),
        )
        result = []
        for row in rows:
            state = self.get_arc_state(row["id"])
            result.append({**row, "arc_state": state})
        return result

    # --- KB queries ---

    def get_kb_entries(self, path_prefix: str | None = None) -> list[dict]:
        """Return knowledge base entries from the kb_entries table.

        Pass path_prefix= to filter to entries starting with that path.
        """
        if path_prefix is not None:
            return self._query(
                "SELECT * FROM kb_entries WHERE path LIKE ? ORDER BY path",
                (path_prefix + "%",),
            )
        return self._query("SELECT * FROM kb_entries ORDER BY path")

    def get_arc_template_name(self, arc_id: int) -> str | None:
        """Return the workflow_templates.name for an arc's template, or None."""
        rows = self._query(
            "SELECT t.name AS template_name "
            "FROM arcs a "
            "JOIN workflow_templates t ON t.id = a.template_id "
            "WHERE a.id = ?",
            (arc_id,),
        )
        return rows[0]["template_name"] if rows else None

    def get_verification_arcs_for(
        self, implementation_arc_id: int
    ) -> list[dict]:
        """Return verification sibling arcs targeting *implementation_arc_id*.

        Verifier arcs (``lint-yaml``, ``verify-kb-format``,
        ``judge-verification``, ``post-verification-docs``, etc.) share
        the implementation arc's ``parent_id`` and link back via
        ``arcs.verification_target_id``. They are NOT children of the
        implementation arc — using ``get_arc_children`` to find them
        will incorrectly return an empty list.

        Returns the verifier rows ordered by ``step_order`` (which the
        workflow template assigns).
        """
        return self._query(
            "SELECT * FROM arcs "
            "WHERE verification_target_id = ? "
            "ORDER BY COALESCE(step_order, 0), id",
            (implementation_arc_id,),
        )

    def get_verification_arc_by_role(
        self, implementation_arc_id: int, step_role: str
    ) -> dict | None:
        """Return the verifier arc with the given ``step_role`` (or name).

        Falls back to matching on ``arcs.name`` when ``step_role`` is
        unpopulated. Returns the first match by ``step_order``.
        """
        rows = self._query(
            "SELECT * FROM arcs "
            "WHERE verification_target_id = ? AND step_role = ? "
            "ORDER BY COALESCE(step_order, 0), id LIMIT 1",
            (implementation_arc_id, step_role),
        )
        if rows:
            return rows[0]
        rows = self._query(
            "SELECT * FROM arcs "
            "WHERE verification_target_id = ? AND name = ? "
            "ORDER BY COALESCE(step_order, 0), id LIMIT 1",
            (implementation_arc_id, step_role),
        )
        return rows[0] if rows else None

    # --- Trust audit log queries ---

    def get_workflow_selected_event_after(
        self, since_ts: float
    ) -> dict | None:
        """Return the most recent ``integrity.workflow_selected`` event.

        Reads ``trust_audit_log`` filtered to events at/after ``since_ts``
        and decodes ``details_json``. Used by change-style stories to
        verify which workflow template was picked
        (``coding-change``, ``yaml-change``, ``kb-change``, …) by the
        path classifier — see
        :func:`carpenter.security.platform_paths.select_workflow_for_paths`.

        Returns the decoded details dict (with an extra ``"_id"`` key
        carrying the row id) or ``None`` if no such event exists.
        """
        since_iso = datetime.fromtimestamp(
            since_ts, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
        rows = self._query(
            "SELECT id, details_json FROM trust_audit_log "
            "WHERE event_type = 'integrity.workflow_selected' "
            "AND created_at >= ? "
            "ORDER BY id DESC LIMIT 1",
            (since_iso,),
        )
        if not rows:
            return None
        details = json.loads(rows[0]["details_json"] or "{}")
        details["_id"] = rows[0]["id"]
        return details

    # --- Generic query ---

    def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute an arbitrary read-only SQL query and return all rows as dicts."""
        return self._query(sql, params)

    # --- Work queue ---

    def get_work_queue(self, limit: int = 20) -> list[dict]:
        return self._query(
            "SELECT * FROM work_queue ORDER BY id DESC LIMIT ?", (limit,)
        )

    def get_conversations(self, limit: int = 10) -> list[dict]:
        return self._query(
            "SELECT * FROM conversations ORDER BY id DESC LIMIT ?", (limit,)
        )

    # --- Formatting helpers for diagnostic output ---

    def format_arcs_table(self, arcs: list[dict]) -> str:
        if not arcs:
            return "  (none)"
        lines = [
            f"  {'ID':>4} | {'Name':<28} | {'Status':<10} | "
            f"{'Par':>4} | {'Ord':>3} | {'Taint':<8} | {'Agent':<10}"
        ]
        lines.append("  " + "-" * 84)
        for a in arcs:
            lines.append(
                f"  {a['id']:>4} | {str(a.get('name',''))[:28]:<28} | "
                f"{str(a.get('status','')):<10} | "
                f"{str(a.get('parent_id') or ''):>4} | "
                f"{str(a.get('step_order') or '0'):>3} | "
                f"{str(a.get('integrity_level','')):<8} | "
                f"{str(a.get('agent_type','')):<10}"
            )
        return "\n".join(lines)

    def format_messages_table(self, messages: list[dict]) -> str:
        if not messages:
            return "  (none)"
        lines = []
        for m in messages:
            arc_tag = f" [arc={m['arc_id']}]" if m.get("arc_id") else ""
            preview = str(m.get("content", ""))[:100].replace("\n", "↵")
            lines.append(f"  [{m['role']}{arc_tag}] {preview}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Story base class
# ---------------------------------------------------------------------------


class AcceptanceStory:
    """Base class for an acceptance story.

    Subclasses must:
    - Set class attributes `name` and `description`
    - Implement `run(client, db)` which performs the scenario and checks

    Assertion helpers:
    - `self.assert_that(condition, message)` — generic boolean assert
    - `self.assert_contains(text, substring)` — case-insensitive substring check

    Per-run artifact naming
    -----------------------
    Every AcceptanceStory instance gets a fresh short UUID (``self.run_id``,
    8 hex chars) the first time it is used.  Combined with a subclass's
    ``artifact_prefix`` (e.g. ``"s053"``), this gives a namespace that is
    guaranteed unique even across concurrent runs of the same story:

        name = self.artifact_name("morning-briefing")
        #     -> "s053-ab12cd34-morning-briefing"

    Use ``self.artifact_name(base)`` for any persistent artifact the story
    creates (cron names, arc names, KB paths, file basenames, etc.) and
    have cleanup filter with::

        WHERE name LIKE f"{self.artifact_prefix}-{self.run_id}-%"

    For scratch directories, use ``self.run_workspace()`` — a per-run dir
    under ``/dev/shm/carpenter-acceptance/`` that can never collide with
    another run and lives on the ramdisk so pytest temp churn doesn't
    touch the SD card.
    """

    name: str = "unnamed"
    description: str = ""
    timeout: int = 300  # Default timeout in seconds for test execution

    #: Short story identifier used as the first segment of artifact names
    #: (e.g. ``"s053"``).  Subclasses should override.  Defaults to the
    #: class name lowercased, which is rarely what you want.
    artifact_prefix: str = ""

    # ------------------------------------------------------------------
    # Artifact naming helpers
    # ------------------------------------------------------------------

    @property
    def run_id(self) -> str:
        """Lazy-initialised short per-run UUID (8 hex chars).

        We do NOT use ``__init__`` because several existing subclasses
        override ``__init__`` without calling ``super().__init__()`` and
        we don't want this to be a breaking change for them.
        """
        rid = self.__dict__.get("_run_id")
        if rid is None:
            rid = uuid.uuid4().hex[:8]
            self.__dict__["_run_id"] = rid
        return rid

    def artifact_name(self, base: str) -> str:
        """Return a globally-unique artifact name for this run.

        Format: ``{artifact_prefix}-{run_id}-{base}``.  Falls back to
        ``{class-name}-{run_id}-{base}`` if ``artifact_prefix`` isn't set.
        """
        prefix = self.artifact_prefix or type(self).__name__.lower()
        return f"{prefix}-{self.run_id}-{base}"

    def artifact_name_pattern(self) -> str:
        """Return the SQL ``LIKE`` pattern covering every artifact of this run.

        Example: ``"s053-ab12cd34-%"``.  Use in cleanup queries like::

            DELETE FROM cron_entries WHERE name LIKE ?
            # params: (self.artifact_name_pattern(),)
        """
        prefix = self.artifact_prefix or type(self).__name__.lower()
        return f"{prefix}-{self.run_id}-%"

    def run_workspace(self) -> Path:
        """Return a per-run scratch directory on the ramdisk.

        Lives under ``/dev/shm/carpenter-acceptance/{prefix}-{run_id}/``
        so concurrent runs of the same story can't collide and heavy temp
        I/O doesn't hit the SD card.  Created on first access.
        """
        prefix = self.artifact_prefix or type(self).__name__.lower()
        ws = Path("/dev/shm/carpenter-acceptance") / f"{prefix}-{self.run_id}"
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    def run(
        self, client: CarpenterClient, db: DBInspector
    ) -> StoryResult:
        raise NotImplementedError

    def cleanup(
        self, client: CarpenterClient, db: "DBInspector | None"
    ) -> None:
        """Called after run() completes (pass or fail). Override to remove test state."""

    def assert_that(
        self, condition: bool, message: str, **diagnostics: Any
    ) -> None:
        if not condition:
            raise AssertionFailure(message, diagnostics)

    def assert_contains(
        self, text: str, substring: str, context: str = ""
    ) -> None:
        msg = f"Expected to find {substring!r} in response"
        if context:
            msg += f" ({context})"
        self.assert_that(
            substring.lower() in text.lower(),
            msg,
            text_preview=text[:400],
        )

    def assert_no_failed_arcs_since(
        self,
        db: "DBInspector | None",
        since_ts: float,
        workflow_label: str = "",
    ) -> None:
        """Assert that no arcs created since ``since_ts`` are failed/cancelled.

        Cosmetic helper that replaces the recurring
        ``bad = [a for a in db.get_arcs_created_after(start_ts)
                if a["status"] in ("failed", "cancelled")]`` boilerplate.
        No-op if ``db`` is None.

        Args:
            db: DBInspector instance (or None — call is then a no-op).
            since_ts: epoch seconds; only arcs at/after this are checked.
            workflow_label: optional label prefixed to the failure message
                (e.g., "Add workflow" or "Remove workflow"). When omitted
                the message is the generic "{n} arc(s) ended in failed/cancelled".
        """
        if db is None:
            return
        all_arcs = db.get_arcs_created_after(since_ts)
        bad = [a for a in all_arcs if a["status"] in ("failed", "cancelled")]
        if workflow_label:
            msg = (
                f"{workflow_label} had {len(bad)} failed/cancelled arc(s) — "
                f"workflow should complete without a string of failures"
            )
        else:
            msg = f"{len(bad)} arc(s) ended in failed/cancelled"
        self.assert_that(
            len(bad) == 0,
            msg,
            arcs=db.format_arcs_table(bad),
        )

    def result(self, message: str = "") -> "StoryResult":
        """Return a passing StoryResult for this story. Convenience helper."""
        return StoryResult(name=self.name, passed=True, message=message)


# ---------------------------------------------------------------------------
# ChangeReviewStory — template-method scaffold for review-and-approve stories
# ---------------------------------------------------------------------------


class ChangeReviewStory(AcceptanceStory):
    """Template-method base class for the *single-round approve* change flow.

    The pattern this captures (shared by s005, s044, s045, and the close
    cousins s011/s012/s018/s025/s032/s041) is::

        1.  send a chat prompt that asks for a change
        2.  wait for the change arc to reach 'waiting' with a review_id
        3.  inspect the diff / arc_state                          [HOOK]
        4.  submit approval
        5.  wait for the arc to reach a terminal state
        6.  assert no failed/cancelled arcs in this session
        7.  optional post-apply verification                      [HOOK]

    Subclasses customise the story-specific bits via class attributes and
    overridable hooks; the inherited ``run()`` owns the sequence.

    Multi-round (revise → approve) flows are intentionally NOT supported
    here — the decision-loop shape varies too much between callers
    (s007, s023, s035). Those stay imperative.

    Required class attributes
    -------------------------
    ``request_text``
        The user-facing chat prompt sent in step 1.
    ``ack_keywords``
        Case-insensitive substrings; the initial assistant response must
        contain at least one. (Keep this lenient — the agent's wording
        is non-deterministic.)

    Optional overrides
    ------------------
    ``initial_response_timeout``       step-1 pending-clear timeout (s).
    ``review_wait_timeout``            step-2 poll timeout (s).
    ``terminal_wait_timeout``          step-5 poll timeout (s).
    ``approve_comment``                comment string submitted with the
                                       approval (default ``"Approved."``).
    ``inspect_diff(diff, arc_state)``  story-specific diff-content
                                       assertions.
    ``post_apply(client, db, conv_id, review_arc)``
                                       optional extra verification after
                                       the arc completes.

    The hook signatures keep ``client`` and ``db`` available so subclasses
    can drop into ad-hoc behavioural / DB checks without re-deriving them.
    """

    #: Chat prompt sent in step 1.
    request_text: str = ""

    #: Acknowledgement keywords for the initial response. Lenient by default.
    ack_keywords: tuple[str, ...] = (
        "coding", "modif", "change", "arc", "implement", "add", "work",
        "done", "updated", "edit", "kb", "yaml",
    )

    #: Comment passed with the approval decision.
    approve_comment: str = "Approved."

    #: Step 1 pending-clear timeout in seconds.
    initial_response_timeout: int = 90

    #: Step 2 review-arc poll timeout in seconds.
    review_wait_timeout: int = 300

    #: Step 5 terminal-status poll timeout in seconds.
    terminal_wait_timeout: int = 120

    # ── Hooks for subclasses ─────────────────────────────────────────────

    def inspect_diff(self, diff: str, arc_state: dict) -> None:
        """Story-specific diff / arc_state assertions.

        Called after the change arc reaches ``waiting`` and before the
        approval is submitted. Subclasses use ``self.assert_that`` /
        ``self.assert_contains`` to check that the diff contains the
        expected substring(s) or that ``arc_state`` carries the expected
        metadata (e.g. workflow template name, source dir, changed files).

        Default: asserts that ``diff`` is non-empty. Override to add more
        specific checks.
        """
        self.assert_that(
            bool(diff),
            "Diff is empty — change agent produced no changes",
            arc_state_keys=list(arc_state.keys()),
        )

    def post_apply(
        self,
        client: "CarpenterClient",
        db: "DBInspector | None",
        conv_id: int,
        review_arc: dict,
    ) -> None:
        """Optional verification after the arc reaches ``completed``.

        Use for file-existence checks, audit-log assertions, follow-up
        chat turns, etc. Default: no-op.
        """

    # ── The owned sequence ───────────────────────────────────────────────

    def run(
        self, client: "CarpenterClient", db: "DBInspector"
    ) -> "StoryResult":
        if not self.request_text:
            raise RuntimeError(
                f"{type(self).__name__}: must set class attribute "
                f"`request_text` to a non-empty prompt."
            )

        start_ts = time.time()
        self._start_ts = start_ts  # exposed for hook use

        # ── 1. Send the request ─────────────────────────────────────────
        print(f"\n  [1/5] Sending change request...")
        conv_id = client.create_conversation()
        client.send_message(self.request_text, conv_id)
        client.wait_for_pending_to_clear(
            conv_id, timeout=self.initial_response_timeout,
        )

        msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(msgs) >= 1,
            "No response after change request",
            conversation_id=conv_id,
        )
        init_resp = msgs[-1]["content"]
        print(f"     {init_resp[:140]}")
        self.assert_that(
            any(kw in init_resp.lower() for kw in self.ack_keywords),
            "Initial response does not acknowledge the change request",
            response_preview=init_resp[:400],
            ack_keywords=list(self.ack_keywords),
        )

        # ── 2. Wait for the review arc ──────────────────────────────────
        print(
            f"  [2/5] Waiting for change arc to reach 'waiting' "
            f"(≤{self.review_wait_timeout}s)..."
        )
        review_arc = None
        if db is not None:
            review_arc = db.wait_for_pending_review_arc(
                start_ts, timeout=self.review_wait_timeout,
            )
            self.assert_that(
                review_arc is not None,
                "Change arc never reached 'waiting' with a review_id",
                arcs=db.format_arcs_table(
                    db.get_arcs_created_after(start_ts)
                ),
            )

        arc_state = review_arc["arc_state"]
        review_id = arc_state["review_id"]
        diff = arc_state.get("diff", "")
        print(f"     Arc {review_arc['id']} waiting, review_id={review_id}")
        print(f"     Diff preview: {diff[:240]}")

        # ── 3. Story-specific diff inspection ───────────────────────────
        print(f"  [3/5] Inspecting diff (story-specific)...")
        self.inspect_diff(diff, arc_state)

        # ── 4. Approve ──────────────────────────────────────────────────
        print(f"  [4/5] Approving (review_id={review_id})...")
        result = client.submit_review_decision(
            review_id, decision="approve", comment=self.approve_comment,
        )
        self.assert_that(
            result.get("recorded") is True,
            "Approval was not recorded by the server",
            server_response=result,
        )

        # ── 5. Wait for terminal + clean-history check ──────────────────
        print(
            f"  [5/5] Waiting for arc {review_arc['id']} to complete "
            f"(≤{self.terminal_wait_timeout}s)..."
        )
        if db is not None:
            final_arc = db.wait_for_arc_terminal(
                review_arc["id"], timeout=self.terminal_wait_timeout,
            )
            self.assert_that(
                final_arc is not None
                and final_arc["status"] == "completed",
                f"Change arc did not complete "
                f"(status="
                f"{final_arc['status'] if final_arc else 'not found'})",
                arcs=db.format_arcs_table(
                    db.get_arcs_created_after(start_ts)
                ),
            )
            print(f"     Arc completed ✓")
            self.assert_no_failed_arcs_since(db, start_ts)

        # ── Post-apply (optional, story-specific) ───────────────────────
        self.post_apply(client, db, conv_id, review_arc)

        return self.result(
            f"change arc {review_arc['id']} approved and completed ✓"
        )
