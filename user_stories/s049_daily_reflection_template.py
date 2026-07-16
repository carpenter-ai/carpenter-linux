"""
S049 — Reflection Triage-Gate Skips Synthesis (v2)

Verifies the *negative* half of the v2 reflection triage gate
(carpenter-core PR #86): when the triage step returns
``needs_synthesis=false`` the reflect step short-circuits without an
LLM call, no KB write occurs, and downstream persist/dispatch steps
no-op cleanly.

This complements S059 (which covers the *positive* branch — triage
flags synthesis, reflect runs, nearby-KB wiring surfaces edit
candidates).

Background — v2 reflection pipeline
-----------------------------------

Since PR #86 the reflection template is a 5-step, triage-gated pipeline:

    gather-activity → triage → reflect → save-reflection → dispatch-actions

The KB is no longer a diary: ``save-reflection`` never writes
``reflections/by-day/*`` or ``reflections/by-arc/*`` entries. Knowledge
lands via ``dispatch-actions`` proposing reviewed kb-change action arcs
— and only when triage flags synthesis as warranted.

Test strategy
-------------

The daily-cron path (``reflection.daily_tick``) is the only supported
v2 entry point. The story:

1. Records existing ``reflections/*`` KB entry paths as a baseline
   (v2 should never create a new one from this run).
2. Inserts one synthetic completed root arc as the batch subject.
3. Emits a ``reflection.daily_tick`` event with the watermark reset.
4. Once the reflection SUPERVISOR arc + its 5 children appear, seeds
   the triage step's ``_agent_response`` with
   ``needs_synthesis=false``.
5. Waits for the reflect step (role ``analyze``) and the persist step
   (role ``persist``) to reach a terminal state.
6. Asserts:
   a. The template has exactly 5 child steps with roles
      ``[prepare, triage, analyze, persist, dispatch]``.
   b. ``arc_state["_reflect_gated_skipped"]`` on the reflect arc is
      truthy — the gate short-circuited before the LLM was invoked.
   c. ``arc_state["_reflect_nearby_kb_paths"]`` is NOT populated
      (the nearby-KB block is only built on the synthesis branch).
   d. No new ``reflections/*`` KB entry appeared during the run
      (v2 no-diary guarantee).

Cleanup: removes the synthetic subject arc, the reflection arc family,
and the emitted event.
"""

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from user_stories.framework import (
    AcceptanceStory,
    CarpenterClient,
    DBInspector,
    StoryResult,
)

# Expected step roles in the v2 reflection template (see reflection.yaml).
EXPECTED_ROLES = ["prepare", "triage", "analyze", "persist", "dispatch"]


def _insert_synthetic_root_arc(db_path: str) -> int:
    """Insert a completed root arc to serve as the batch subject."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO arcs "
            "(name, goal, status, priority, integrity_level, agent_type) "
            "VALUES (?, ?, 'completed', 100, 'trusted', 'EXECUTOR')",
            (
                "s049-synthetic-goal",
                f"Synthetic subject arc for s049 ({int(time.time())})",
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _reset_reflection_watermark(db_path: str) -> None:
    """Rewind the daily-tick watermark so our synthetic arc is in-scope."""
    ten_min_ago_iso = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    ).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) "
            "VALUES (0, 'reflection_last_tick', ?) "
            "ON CONFLICT(arc_id, key) DO UPDATE SET "
            "value_json = excluded.value_json",
            (json.dumps(ten_min_ago_iso),),
        )
        conn.commit()
    finally:
        conn.close()


def _emit_daily_tick(db_path: str) -> None:
    """Enqueue a ``reflection.daily_tick`` event for the daemon."""
    payload = json.dumps({"source": "s049-acceptance-story"})
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO events "
            "(event_type, payload_json, source, processed, priority, "
            "idempotency_key) VALUES "
            "('reflection.daily_tick', ?, 'story:s049', 0, 0, ?)",
            (payload, f"s049-tick-{int(time.time())}"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_triage_skip(db_path: str, triage_arc_id: int) -> None:
    """Seed the triage step's ``_agent_response`` with a skip verdict.

    ``handle_reflect_gated._read_triage_result`` reads ``_agent_response``
    from arc_state, ``json.loads`` it once to get the string payload, then
    ``json.loads`` again to parse the TriageResult. So we double-encode
    here, matching s059's positive-branch seed.
    """
    payload = {
        "needs_synthesis": False,
        "reasons": ["s049 seeded triage skip to exercise gate no-op path"],
        "focus_pointers": [],
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(arc_id, key) DO UPDATE SET "
            "value_json = excluded.value_json",
            (
                triage_arc_id,
                "_agent_response",
                json.dumps(json.dumps(payload)),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _reflection_kb_paths(db: DBInspector) -> set[str]:
    """Snapshot every ``reflections/*`` KB path currently in kb_entries."""
    return {
        e["path"]
        for e in db.get_kb_entries(path_prefix="reflections/")
    }


class ReflectionTriageGateSkips(AcceptanceStory):
    name = "S049 — Reflection Triage-Gate Skips Synthesis"
    description = (
        "Emit reflection.daily_tick; force triage to needs_synthesis=false; "
        "assert reflect short-circuits (_reflect_gated_skipped=true), the "
        "5-step template shape is intact, and no diary KB entry is written."
    )
    timeout = 300

    _synthetic_arc_id: int | None = None
    _reflection_arc_id: int | None = None

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        self.assert_that(db is not None, "DB inspector required for this test")
        start_ts = time.time()

        # ── 1. Baseline reflections/* KB paths ──────────────────────────────
        print("\n  [1/6] Snapshotting existing reflections/* KB paths...")
        baseline_paths = _reflection_kb_paths(db)
        print(f"     Baseline: {len(baseline_paths)} entry/entries")

        # ── 2. Synthetic subject arc + reset watermark + emit tick ──────────
        print("  [2/6] Inserting synthetic completed root arc...")
        self._synthetic_arc_id = _insert_synthetic_root_arc(db.db_path)
        print(f"     Synthetic subject arc ID: {self._synthetic_arc_id}")

        print("  [3/6] Resetting watermark + emitting reflection.daily_tick...")
        _reset_reflection_watermark(db.db_path)
        _emit_daily_tick(db.db_path)

        # ── 4. Wait for reflection SUPERVISOR + 5 children ──────────────────
        print("     Waiting for reflection arc + 5 children (up to 45s)...")
        reflection_arc = None
        children: list[dict] = []
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            new_arcs = db.get_arcs_created_after(start_ts)
            for a in new_arcs:
                if a["id"] == self._synthetic_arc_id:
                    continue
                if a.get("parent_id") is not None:
                    continue
                if db.get_arc_template_name(a["id"]) == "reflection":
                    reflection_arc = a
                    self._reflection_arc_id = a["id"]
                    children = db.get_arc_children(a["id"])
                    if len(children) >= 5:
                        break
            if reflection_arc and len(children) >= 5:
                break
            time.sleep(1.5)

        if reflection_arc is None:
            return StoryResult(
                name=self.name,
                passed=False,
                message=(
                    "Reflection arc did not appear within 45s of emitting "
                    "reflection.daily_tick. Common causes: (a) escalation "
                    "gate refused (no SMTP config), (b) daemon not "
                    "processing events, (c) reflection template not loaded. "
                    "See kb/reflections/setup.md."
                ),
            )
        print(
            f"     Reflection arc {self._reflection_arc_id} appeared "
            f"with {len(children)} children"
        )

        # ── 4b. Assert 5-step shape by role ─────────────────────────────────
        print("  [4/6] Verifying 5-step template shape by step_role...")
        child_roles = [c.get("step_role") for c in children]
        self.assert_that(
            len(children) == 5,
            f"Expected 5 children (v2 template), got {len(children)}: "
            f"roles={child_roles}",
            arcs=db.format_arcs_table(children),
        )
        for i, role in enumerate(EXPECTED_ROLES):
            arc = db.get_arc_by_role(self._reflection_arc_id, role)
            self.assert_that(
                arc is not None,
                f"Missing child arc with step_role={role!r}. "
                f"Got roles={child_roles}",
                arcs=db.format_arcs_table(children),
            )
            self.assert_that(
                arc.get("step_order") == i,
                f"step_role={role!r} should have step_order={i}, got "
                f"{arc.get('step_order')}",
            )
        print(f"     Roles verified: {child_roles}")

        # ── 5. Seed triage skip; wait for reflect + persist to terminate ────
        print("  [5/6] Seeding triage output (needs_synthesis=false)...")
        triage_arc = db.get_arc_by_role(self._reflection_arc_id, "triage")
        self.assert_that(
            triage_arc is not None,
            "No triage step under reflection arc — pre-v2 core?",
        )
        _seed_triage_skip(db.db_path, triage_arc["id"])
        print(f"     Seeded triage arc {triage_arc['id']} with skip verdict")

        print("     Waiting for reflect+persist to reach terminal (up to 240s)...")
        deadline = time.monotonic() + 240
        reflect_arc = None
        persist_arc = None
        last_print = 0.0
        terminal = ("completed", "failed", "cancelled", "frozen")
        while time.monotonic() < deadline:
            reflect_arc = db.get_arc_by_role(
                self._reflection_arc_id, "analyze",
            )
            persist_arc = db.get_arc_by_role(
                self._reflection_arc_id, "persist",
            )
            if (
                reflect_arc
                and reflect_arc.get("status") in terminal
                and persist_arc
                and persist_arc.get("status") in terminal
            ):
                break
            now = time.monotonic()
            if now - last_print >= 10:
                statuses = ", ".join(
                    f"{c.get('step_role')}={c.get('status')}"
                    for c in db.get_arc_children(self._reflection_arc_id)
                )
                print(f"     Waiting... {statuses}")
                last_print = now
            time.sleep(2)

        self.assert_that(
            reflect_arc is not None
            and reflect_arc.get("status") in terminal,
            f"reflect arc did not reach terminal state within 240s "
            f"(status={reflect_arc.get('status') if reflect_arc else None})",
        )
        self.assert_that(
            persist_arc is not None
            and persist_arc.get("status") in terminal,
            f"persist arc did not reach terminal state within 240s "
            f"(status={persist_arc.get('status') if persist_arc else None})",
        )
        print(
            f"     reflect={reflect_arc.get('status')} "
            f"persist={persist_arc.get('status')}"
        )

        # ── 6. Assert gate skipped, no diary, no nearby-KB wiring ───────────
        print("  [6/6] Asserting triage-gate skip semantics...")
        reflect_state = db.get_arc_state(reflect_arc["id"])
        print(f"     reflect arc_state keys: {sorted(reflect_state.keys())}")

        self.assert_that(
            bool(reflect_state.get("_reflect_gated_skipped")),
            "reflect arc did NOT take the gated-skip path — "
            "_reflect_gated_skipped is falsy. Seeded triage output did "
            "not reach handle_reflect_gated, or triage_result parsing "
            "misread needs_synthesis=false.",
        )
        # Nearby-KB wiring only runs on the synthesis branch.
        self.assert_that(
            not reflect_state.get("_reflect_nearby_kb_paths"),
            "_reflect_nearby_kb_paths should be empty/absent on skip "
            f"branch, got {reflect_state.get('_reflect_nearby_kb_paths')!r}. "
            "The nearby-KB block should only be built when triage flags "
            "synthesis.",
        )
        print("     Gate short-circuited ✓ (no LLM call, no nearby-KB block)")

        # v2 no-diary guarantee: no reflections/* path added.
        after_paths = _reflection_kb_paths(db)
        new_paths = after_paths - baseline_paths
        self.assert_that(
            not new_paths,
            f"v2 no-diary guarantee violated: new reflections/* KB "
            f"entries appeared: {sorted(new_paths)}. save-reflection "
            "must not write a diary in v2.",
        )
        print("     v2 no-diary guarantee held ✓")

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"reflection arc {self._reflection_arc_id}: 5-step shape "
                f"{child_roles} ✓, triage-gate skipped reflect "
                f"(_reflect_gated_skipped=true) ✓, no nearby-KB block ✓, "
                f"no new reflections/* KB entry ✓ (v2 no-diary)"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        if db is None:
            return
        conn = sqlite3.connect(db.db_path)
        try:
            deleted: list[str] = []

            if self._reflection_arc_id:
                child_ids = [
                    r[0] for r in conn.execute(
                        "SELECT id FROM arcs WHERE parent_id = ?",
                        (self._reflection_arc_id,),
                    ).fetchall()
                ]
                for cid in child_ids:
                    grand = [
                        r[0] for r in conn.execute(
                            "SELECT id FROM arcs WHERE parent_id = ?", (cid,),
                        ).fetchall()
                    ]
                    for gcid in grand:
                        conn.execute("DELETE FROM arc_state WHERE arc_id = ?", (gcid,))
                        conn.execute("DELETE FROM arc_history WHERE arc_id = ?", (gcid,))
                        conn.execute("DELETE FROM arcs WHERE id = ?", (gcid,))
                    conn.execute("DELETE FROM arc_state WHERE arc_id = ?", (cid,))
                    conn.execute("DELETE FROM arc_history WHERE arc_id = ?", (cid,))
                    conn.execute("DELETE FROM arcs WHERE id = ?", (cid,))
                deleted.append(f"{len(child_ids)} reflection child arcs")
                conn.execute(
                    "DELETE FROM arc_state WHERE arc_id = ?",
                    (self._reflection_arc_id,),
                )
                conn.execute(
                    "DELETE FROM arc_history WHERE arc_id = ?",
                    (self._reflection_arc_id,),
                )
                conn.execute(
                    "DELETE FROM arcs WHERE id = ?",
                    (self._reflection_arc_id,),
                )
                deleted.append(f"reflection arc {self._reflection_arc_id}")

            if self._synthetic_arc_id:
                conn.execute(
                    "DELETE FROM arc_state WHERE arc_id = ?",
                    (self._synthetic_arc_id,),
                )
                conn.execute(
                    "DELETE FROM arcs WHERE id = ?",
                    (self._synthetic_arc_id,),
                )
                deleted.append(f"synthetic arc {self._synthetic_arc_id}")

            conn.execute(
                "DELETE FROM events WHERE source = 'story:s049'",
            )

            conn.commit()
            if deleted:
                print(f"  [cleanup] Removed: {', '.join(deleted)}")
        except Exception as exc:
            print(f"  [cleanup] Error: {exc}")
        finally:
            conn.close()
