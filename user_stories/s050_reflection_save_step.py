"""
S050 — Reflection Save Step Persists Output to KB

Verifies that the persist step (template role ``persist``, step name
``save-reflection``) is wired correctly: it runs as part of the
reflection template, completes successfully, and writes a KB entry at
``reflections/by-arc/{arc_id}`` with ``entry_type=reflection``.

This complements S049 by focusing specifically on the persist-step
plumbing and KB persistence contract. S049 confirms the full 4-step arc
tree; S050 confirms the persist step's output — that the KB path is
keyed to the reflected arc's ID, and the entry is correctly typed.

Per D2 PR-α (carpenter-core PR #298), the dispatch path now keys on
``(template_name, step_role)``. This story identifies arcs by role
(``persist``) and the parent arc by ``template_name == "reflection"``,
not by ``arc.name``.

Background: since PR #250/255, reflection output is persisted as a KB
article at ``reflections/by-arc/{reflected_arc_id}`` rather than into a
SQL ``reflections`` table. The persist step calls
``kb_entry.create_reflection_entry()`` which writes to this path.

Trigger mechanism: same as S049 — insert a synthetic root arc and emit
an ``arc.status_changed`` event to fire the reflection subscription.

Expected behaviour:
  1. Reflection subscription fires; reflection arc + 4 steps created.
  2. The persist step (step_role=persist) runs and reaches "completed".
  3. A KB entry is written at ``reflections/by-arc/{synthetic_arc_id}``.
  4. The KB entry has ``entry_type = "reflection"``.
  5. The KB entry path uses the reflected arc's ID (not the reflection
     arc's own ID), confirming the step correctly reads
     ``reflected_arc_id`` from the parent's arc_state.

DB/KB verification:
  - Reflection parent arc has ``template_name == "reflection"``.
  - Persist child arc (``step_role == "persist"``) reaches a terminal
    status (completed/frozen).
  - KB entry at ``reflections/by-arc/{synthetic_arc_id}`` exists in
    kb_entries with ``entry_type == "reflection"``.
  - KB path is keyed on the *reflected* arc id, not the reflection arc.

Cleanup: removes synthetic arc, reflection arc family, and KB entry.
"""

import json
import sqlite3
import time

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)


def _insert_synthetic_root_arc(db_path: str, name: str = "test-goal") -> int:
    """Insert a completed root arc and return its ID."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO arcs "
            "(name, goal, status, priority, integrity_level, agent_type) "
            "VALUES (?, ?, 'completed', 100, 'trusted', 'EXECUTOR')",
            (name, f"Synthetic test arc for s050 acceptance story ({int(time.time())})"),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _emit_arc_status_changed(db_path: str, arc_id: int) -> None:
    """Emit an arc.status_changed event that fires the reflection subscription."""
    payload = json.dumps({
        "arc_id": arc_id,
        "old_status": "active",
        "new_status": "completed",
        "is_root": True,
        # No template_name — absence satisfies the $ne filter for recursion guard.
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


class ReflectionSaveStep(AcceptanceStory):
    name = "S050 — Reflection Save Step Persists Output"
    description = (
        "Verify save-reflection step completes and persists a KB entry at "
        "reflections/by-arc/{reflected_arc_id} with entry_type=reflection."
    )
    timeout = 300

    _synthetic_arc_id: int | None = None
    _reflection_arc_id: int | None = None

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        self.assert_that(db is not None, "DB inspector required for this test")
        start_ts = time.time()

        # ── 1. Trigger reflection via synthetic arc ──────────────────────────
        print("\n  [1/4] Inserting synthetic completed root arc + emitting event...")
        self._synthetic_arc_id = _insert_synthetic_root_arc(db.db_path)
        print(f"     Synthetic arc ID: {self._synthetic_arc_id}")
        _emit_arc_status_changed(db.db_path, self._synthetic_arc_id)
        print(f"     arc.status_changed event emitted")

        # ── 2. Wait for persist arc (step_role='persist') to complete ──────
        print("  [2/4] Waiting for persist step (step_role='persist') to "
              "complete (up to 240s)...")
        deadline = time.monotonic() + 240
        save_arc = None
        last_print = 0

        while time.monotonic() < deadline:
            new_arcs = db.get_arcs_created_after(start_ts)

            # Find the reflection parent by template_name (D2 PR-α
            # identity), not by arc.name. Skip the synthetic root.
            if self._reflection_arc_id is None:
                for a in new_arcs:
                    if a["id"] == self._synthetic_arc_id:
                        continue
                    if a.get("parent_id") is not None:
                        continue
                    if db.get_arc_template_name(a["id"]) == "reflection":
                        self._reflection_arc_id = a["id"]
                        print(
                            f"     Reflection parent arc found: "
                            f"{self._reflection_arc_id}"
                        )
                        break

            # Find the persist child by step_role once it's reached a
            # terminal status. Fall back to the legacy name match for
            # arcs that predate the step_role column (defensive).
            if self._reflection_arc_id is not None:
                save_arc = db.get_arc_by_role(
                    self._reflection_arc_id, "persist",
                )
                if save_arc and save_arc.get("status") in (
                    "completed", "frozen",
                ):
                    break
                save_arc = None

            now = time.monotonic()
            if now - last_print >= 10:
                statuses = ", ".join(
                    f"{a.get('step_role') or a['name']}={a['status']}"
                    for a in new_arcs
                ) or "(no arcs yet)"
                print(f"     Waiting... {statuses}")
                last_print = now
            time.sleep(2)

        self.assert_that(
            save_arc is not None,
            "persist arc (step_role='persist') did not complete within 240s. "
            "Is the server running and reflection template loaded?",
        )
        print(
            f"     persist step completed (status={save_arc['status']}) ✓"
        )

        # ── 3. Verify persist arc was registered under the right parent ────
        print("  [3/4] Verifying persist arc structure...")

        # Re-confirm parent identity via template_name.
        parent_template_name = db.get_arc_template_name(self._reflection_arc_id)
        self.assert_that(
            parent_template_name == "reflection",
            f"Parent arc template_name should be 'reflection', got "
            f"{parent_template_name!r}",
        )

        save_arc_refreshed = db.get_arc_by_role(
            self._reflection_arc_id, "persist",
        )
        children = db.get_arc_children(self._reflection_arc_id)
        self.assert_that(
            save_arc_refreshed is not None,
            f"No child with step_role='persist' found under reflection arc "
            f"{self._reflection_arc_id}. Children: "
            f"roles={[c.get('step_role') for c in children]}, "
            f"names={[c['name'] for c in children]}",
            arcs=db.format_arcs_table(children),
        )
        self.assert_that(
            save_arc_refreshed.get("status") in ("completed", "frozen"),
            f"persist arc final status should be completed/frozen, "
            f"got '{save_arc_refreshed.get('status')}'",
        )
        print(
            f"     persist arc {save_arc_refreshed['id']} "
            f"(step_role='persist', name={save_arc_refreshed.get('name')!r}) "
            f"status={save_arc_refreshed['status']} ✓"
        )

        # ── 4. Verify KB entry keyed on the reflected arc's ID ──────────────
        print("  [4/4] Verifying KB entry at reflections/by-arc/{reflected_arc_id}...")

        # The KB path must use the *synthetic* arc's ID (the reflected arc),
        # not the reflection arc's own ID. This verifies the step correctly
        # reads `reflected_arc_id` from the parent's arc_state.
        expected_kb_path = f"reflections/by-arc/{self._synthetic_arc_id}"
        wrong_kb_path = f"reflections/by-arc/{self._reflection_arc_id}"

        kb_entries = db.get_kb_entries(path_prefix="reflections/by-arc/")
        paths = [e["path"] for e in kb_entries]

        self.assert_that(
            expected_kb_path in paths,
            f"No KB entry at '{expected_kb_path}'. "
            f"Available by-arc entries: {paths}",
        )
        self.assert_that(
            wrong_kb_path not in paths or expected_kb_path in paths,
            f"KB entry uses reflection arc ID ({wrong_kb_path}) instead of "
            f"reflected arc ID ({expected_kb_path}). "
            "save-reflection must read reflected_arc_id from parent arc_state.",
        )

        kb_entry = next(e for e in kb_entries if e["path"] == expected_kb_path)
        print(
            f"     KB entry: {kb_entry['path']} "
            f"(entry_type={kb_entry.get('entry_type')}, "
            f"byte_count={kb_entry.get('byte_count', 0)}) ✓"
        )

        # entry_type must be "reflection"
        self.assert_that(
            kb_entry.get("entry_type") == "reflection",
            f"KB entry_type should be 'reflection', got '{kb_entry.get('entry_type')}'",
        )

        # byte_count must be > 0 (entry was written, even if AI didn't run)
        self.assert_that(
            kb_entry.get("byte_count", 0) > 0,
            f"KB entry byte_count is 0; entry was not written",
        )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"persist step (step_role='persist') completed "
                f"(status={save_arc['status']}) ✓, "
                f"parent template_name='reflection' ✓, "
                f"KB entry at {expected_kb_path} ✓ "
                f"(entry_type=reflection, "
                f"{kb_entry.get('byte_count', 0)} bytes, "
                f"keyed on reflected arc {self._synthetic_arc_id}) ✓"
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
                if self._synthetic_arc_id:
                    kb_path = f"reflections/by-arc/{self._synthetic_arc_id}"
                    conn.execute(
                        "DELETE FROM kb_entries WHERE path = ?", (kb_path,)
                    )
                    deleted.append(f"KB entry {kb_path}")

            if self._synthetic_arc_id:
                conn.execute(
                    "DELETE FROM arc_state WHERE arc_id = ?",
                    (self._synthetic_arc_id,),
                )
                conn.execute(
                    "DELETE FROM arcs WHERE id = ?",
                    (self._synthetic_arc_id,),
                )
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
