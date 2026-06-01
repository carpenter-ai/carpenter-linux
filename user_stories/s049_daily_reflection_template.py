"""
S049 — Reflection Template End-to-End (Per-Arc Trigger)

Verifies the full per-arc reflection flow: completing a root arc triggers
the reflection subscription, which creates a reflection arc with the correct
4-step template structure, runs AI analysis, and persists the result to KB.

Background: reflections are no longer cadence-based (daily/weekly/monthly).
Since PR #250, a reflection fires automatically when any non-reflection root
arc reaches "completed" status, via an arc.status_changed subscription.

Per D2 PR-α (carpenter-core PR #298), template arcs now carry a
``step_role`` column populated from the template YAML. This story asserts
on the role-based identity ``(template_name, step_role)`` rather than on
the human-readable ``arc.name``, treating the latter as presentation only.

Trigger mechanism:
  1. Insert a synthetic root arc (status=completed) into the arcs table.
  2. Emit an arc.status_changed event with is_root=True, new_status=completed.
  3. The server's event processor picks it up and invokes the reflection
     subscription, creating a reflection arc from the template.

Expected behaviour:
  1. A reflection parent arc is created (template_name="reflection",
     priority=1000 / idle).
  2. The reflection template instantiates 4 child arcs in order, asserted
     by their ``step_role``:
     - role=prepare  (order=0; was "gather-activity")
     - role=analyze  (order=1; was "reflect"; EXECUTOR, runs AI)
     - role=persist  (order=2; was "save-reflection")
     - role=dispatch (order=3; was "dispatch-actions")
  3. The analyze arc produces AI analysis text.
  4. The persist step writes a KB entry at reflections/by-arc/{arc_id}.
  5. All arcs reach a terminal state.

DB/KB verification:
  - Parent arc has template_name="reflection" (joined through
    workflow_templates), priority=1000.
  - Four child arcs with the expected step_roles and ordering.
  - arc_state on reflection arc contains "reflected_arc_id".
  - KB entry exists at path "reflections/by-arc/{synthetic_arc_id}".
  - KB entry content is non-trivial (> 50 chars).

Cleanup: removes the synthetic arc, its reflection arc family, and KB entry.
"""

import json
import sqlite3
import time
from datetime import datetime, timezone

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

# Expected step roles in the reflection template (must match reflection.yaml).
# Per D2 PR-α: identity for the dispatch path is (template_name, step_role).
# Names are kept as presentation; assertions key on roles.
EXPECTED_ROLES = ["prepare", "analyze", "persist", "dispatch"]


def _insert_synthetic_root_arc(db_path: str, name: str = "test-goal") -> int:
    """Insert a completed root arc and return its ID.

    The arc is marked completed directly so we can emit the event immediately.
    We do NOT go through arc_history or notify the server's internal arc
    manager — we just need a real arc row to reference.
    """
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO arcs "
            "(name, goal, status, priority, integrity_level, agent_type) "
            "VALUES (?, ?, 'completed', 100, 'trusted', 'EXECUTOR')",
            (name, f"Synthetic test arc for s049 acceptance story ({int(time.time())})"),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _emit_arc_status_changed(db_path: str, arc_id: int) -> None:
    """Emit an arc.status_changed event that triggers the reflection subscription.

    Mirrors what carpenter.core.engine.triggers.arc_lifecycle.emit_status_changed()
    does: inserts a row into the events table with the required payload shape.
    The server's event processor will pick it up and invoke any matching
    subscriptions (including the reflection one).
    """
    payload = json.dumps({
        "arc_id": arc_id,
        "old_status": "active",
        "new_status": "completed",
        "is_root": True,
        # No template_name — simulates a plain user-goal arc.
        # The reflection subscription filter requires template_name != 'reflection',
        # and $ne matches when the key is absent.
    })
    idempotency_key = f"arc-{arc_id}-active-completed"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO events "
            "(event_type, payload_json, source, processed, priority, idempotency_key) "
            "VALUES ('arc.status_changed', ?, ?, 0, 0, ?)",
            (payload, f"arc:{arc_id}", idempotency_key),
        )
        conn.commit()
    finally:
        conn.close()


class ReflectionTemplateEndToEnd(AcceptanceStory):
    name = "S049 — Reflection Template End-to-End"
    description = (
        "Complete a root arc; verify per-arc reflection flow: parent arc, "
        "4 template steps (gather-activity/reflect/save-reflection/dispatch-actions), "
        "AI execution, and KB persistence."
    )
    timeout = 300  # AI model call can take time

    # Track IDs for cleanup
    _synthetic_arc_id: int | None = None
    _reflection_arc_id: int | None = None

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        self.assert_that(db is not None, "DB inspector required for this test")
        start_ts = time.time()

        # ── 1. Create synthetic root arc + emit event ───────────────────────
        print("\n  [1/5] Inserting synthetic completed root arc + emitting event...")
        self._synthetic_arc_id = _insert_synthetic_root_arc(db.db_path)
        print(f"     Synthetic arc ID: {self._synthetic_arc_id}")
        _emit_arc_status_changed(db.db_path, self._synthetic_arc_id)
        print(f"     arc.status_changed event emitted for arc {self._synthetic_arc_id}")

        # ── 2. Wait for reflection arc + 4 children to appear ───────────────
        print("  [2/5] Waiting for reflection arc (template_name='reflection') "
              "+ 4 children (up to 30s)...")
        reflection_arc = None
        children = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            new_arcs = db.get_arcs_created_after(start_ts)
            # Identify the spawned arc by its template_name (D2 PR-α
            # identity), not by arc.name. Skip the synthetic root arc
            # we injected as the trigger source.
            candidates = []
            for a in new_arcs:
                if a["id"] == self._synthetic_arc_id:
                    continue
                if a.get("parent_id") is not None:
                    continue
                if db.get_arc_template_name(a["id"]) == "reflection":
                    candidates.append(a)
            if candidates:
                reflection_arc = candidates[0]
                self._reflection_arc_id = reflection_arc["id"]
                children = db.get_arc_children(self._reflection_arc_id)
                if len(children) >= 4:
                    break
            time.sleep(1)

        self.assert_that(
            reflection_arc is not None,
            "Reflection arc was not created within 30s after emitting "
            "arc.status_changed. Is the server running and subscription loaded?",
        )
        self._reflection_arc_id = reflection_arc["id"]
        print(f"     Reflection arc ID: {self._reflection_arc_id}")

        # ── 3. Verify arc tree structure ─────────────────────────────────────
        print("  [3/5] Verifying arc tree structure...")

        # Refresh reflection arc
        reflection_arc = db.get_arc(self._reflection_arc_id)
        self.assert_that(
            reflection_arc is not None,
            "Could not re-fetch reflection arc",
        )

        # Re-confirm template_name on the parent for the role-based identity.
        parent_template_name = db.get_arc_template_name(self._reflection_arc_id)
        self.assert_that(
            parent_template_name == "reflection",
            f"Parent arc template_name should be 'reflection', got "
            f"{parent_template_name!r}",
        )

        # Priority should be 1000 (idle — reflections run at background priority)
        self.assert_that(
            reflection_arc.get("priority") == 1000,
            f"Reflection arc priority should be 1000 (idle), got "
            f"{reflection_arc.get('priority')}",
        )

        child_roles = [c.get("step_role") for c in children]
        child_names = [c["name"] for c in children]
        print(f"     Children ({len(children)}): roles={child_roles} names={child_names}")

        self.assert_that(
            len(children) == 4,
            f"Expected 4 child arcs, got {len(children)}: roles={child_roles}",
            arcs=db.format_arcs_table(children),
        )

        # Assert presence + ordering by step_role (D2 PR-α identity).
        role_map: dict[str, dict] = {}
        for role in EXPECTED_ROLES:
            arc = db.get_arc_by_role(self._reflection_arc_id, role)
            self.assert_that(
                arc is not None,
                f"Missing child arc with step_role={role!r}. "
                f"Got roles={child_roles}, names={child_names}",
                arcs=db.format_arcs_table(children),
            )
            role_map[role] = arc

        for i, role in enumerate(EXPECTED_ROLES):
            arc = role_map[role]
            self.assert_that(
                arc.get("step_order") == i,
                f"step_role={role!r} should have step_order={i}, got "
                f"{arc.get('step_order')}",
            )

        # The analyze step is the only LLM-driven one (EXECUTOR).
        analyze_arc = role_map["analyze"]
        self.assert_that(
            analyze_arc.get("agent_type") == "EXECUTOR",
            f"analyze arc should be EXECUTOR, got "
            f"{analyze_arc.get('agent_type')}",
        )

        # Verify reflected_arc_id in arc_state points back to our synthetic arc
        refl_state = db.get_arc_state(self._reflection_arc_id)
        print(f"     Reflection arc_state keys: {list(refl_state.keys())}")
        self.assert_that(
            "reflected_arc_id" in refl_state,
            f"arc_state missing 'reflected_arc_id'. Keys: {list(refl_state.keys())}",
        )
        self.assert_that(
            refl_state["reflected_arc_id"] == self._synthetic_arc_id,
            f"reflected_arc_id should be {self._synthetic_arc_id}, "
            f"got {refl_state['reflected_arc_id']}",
        )
        print("     Arc tree structure verified ✓")

        # ── 4. Wait for all arcs to complete ────────────────────────────────
        print("  [4/5] Waiting for reflection arcs to complete (up to 240s)...")
        arc_deadline = time.monotonic() + 240
        last_print = 0

        while time.monotonic() < arc_deadline:
            all_arcs = [db.get_arc(self._reflection_arc_id)] + \
                       db.get_arc_children(self._reflection_arc_id)
            pending = [
                a for a in all_arcs if a is not None and
                a.get("status") not in ("completed", "failed", "cancelled", "frozen")
            ]
            if not pending:
                break

            now = time.monotonic()
            if now - last_print >= 5:
                statuses = ", ".join(
                    f"{a['name']}={a['status']}" for a in pending[:5]
                )
                print(f"     Still waiting: {statuses}")
                last_print = now
            time.sleep(2)
        else:
            all_arcs = [db.get_arc(self._reflection_arc_id)] + \
                       db.get_arc_children(self._reflection_arc_id)
            statuses = ", ".join(
                f"{a['name']}={a.get('status')}" for a in all_arcs if a
            )
            self.assert_that(
                False,
                f"Reflection arcs did not complete within 240s. Statuses: {statuses}",
                arcs=db.format_arcs_table([a for a in all_arcs if a]),
            )

        # Check for failures
        final_children = db.get_arc_children(self._reflection_arc_id)
        failed = [c for c in final_children if c.get("status") == "failed"]
        if failed:
            for f in failed:
                state = db.get_arc_state(f["id"])
                print(f"     FAILED arc {f['name']}: {state.get('error', 'unknown')}")

        self.assert_that(
            len(failed) == 0,
            f"{len(failed)} arc(s) failed: "
            + ", ".join(f"{f['name']} (id={f['id']})" for f in failed),
            arcs=db.format_arcs_table(final_children),
        )
        print("     All reflection arcs completed ✓")

        # ── 5. Verify KB entry ───────────────────────────────────────────────
        print("  [5/5] Verifying KB entry...")

        expected_kb_path = f"reflections/by-arc/{self._synthetic_arc_id}"
        kb_entries = db.get_kb_entries(path_prefix="reflections/by-arc/")
        matching = [e for e in kb_entries if e["path"] == expected_kb_path]

        self.assert_that(
            len(matching) >= 1,
            f"No KB entry at '{expected_kb_path}'. "
            f"Available by-arc entries: "
            f"{[e['path'] for e in kb_entries]}",
        )

        # Verify content is substantive (save-reflection ran AI output)
        # KB stores content on disk; check byte_count as proxy
        kb_entry = matching[0]
        print(f"     KB entry: {kb_entry['path']} ({kb_entry.get('byte_count', 0)} bytes)")
        self.assert_that(
            kb_entry.get("byte_count", 0) > 50,
            f"KB entry byte_count too small ({kb_entry.get('byte_count', 0)}); "
            "expected substantive reflection content",
        )
        self.assert_that(
            kb_entry.get("entry_type") == "reflection",
            f"KB entry_type should be 'reflection', got '{kb_entry.get('entry_type')}'",
        )

        print(f"     KB entry '{expected_kb_path}' verified ✓")

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Reflection arc created (id={self._reflection_arc_id}, "
                f"template_name='reflection', priority=1000) ✓, "
                f"4 template steps verified by step_role "
                f"({', '.join(EXPECTED_ROLES)}) ✓, "
                f"reflected_arc_id={self._synthetic_arc_id} ✓, "
                f"arcs completed ✓, "
                f"KB entry at {expected_kb_path} ✓"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Remove synthetic arc, reflection arc family, and KB entry."""
        if db is None:
            return

        conn = sqlite3.connect(db.db_path)
        try:
            deleted = []

            if self._reflection_arc_id:
                # Remove grandchildren (dispatch-actions may spawn child arcs)
                child_ids = [
                    r[0] for r in conn.execute(
                        "SELECT id FROM arcs WHERE parent_id = ?",
                        (self._reflection_arc_id,),
                    ).fetchall()
                ]
                for cid in child_ids:
                    grandchild_ids = [
                        r[0] for r in conn.execute(
                            "SELECT id FROM arcs WHERE parent_id = ?", (cid,)
                        ).fetchall()
                    ]
                    for gcid in grandchild_ids:
                        conn.execute("DELETE FROM arc_state WHERE arc_id = ?", (gcid,))
                        conn.execute("DELETE FROM arc_history WHERE arc_id = ?", (gcid,))
                        conn.execute("DELETE FROM arcs WHERE id = ?", (gcid,))
                    conn.execute("DELETE FROM arc_state WHERE arc_id = ?", (cid,))
                    conn.execute("DELETE FROM arc_history WHERE arc_id = ?", (cid,))
                    conn.execute("DELETE FROM arcs WHERE id = ?", (cid,))
                deleted.append(f"{len(child_ids)} reflection child arcs")

                # Remove reflection parent arc
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

                # Remove KB entry
                expected_kb_path = f"reflections/by-arc/{self._synthetic_arc_id}"
                conn.execute(
                    "DELETE FROM kb_entries WHERE path = ?",
                    (expected_kb_path,),
                )
                deleted.append(f"KB entry {expected_kb_path}")

            if self._synthetic_arc_id:
                conn.execute(
                    "DELETE FROM arc_state WHERE arc_id = ?",
                    (self._synthetic_arc_id,),
                )
                conn.execute(
                    "DELETE FROM arcs WHERE id = ?",
                    (self._synthetic_arc_id,),
                )
                # Clean up the event we emitted
                conn.execute(
                    "DELETE FROM events WHERE source = ? "
                    "AND event_type = 'arc.status_changed'",
                    (f"arc:{self._synthetic_arc_id}",),
                )
                deleted.append(f"synthetic arc {self._synthetic_arc_id}")

            conn.commit()
            if deleted:
                print(f"  [cleanup] Removed: {', '.join(deleted)}")
        except Exception as exc:
            print(f"  [cleanup] Error: {exc}")
        finally:
            conn.close()
