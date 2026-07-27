"""
S059 — Reflection Prefers Editing an Existing KB Entry

Verifies the "prefer edit over create" wiring added in the v2 reflection
pipeline (carpenter-core PR #86): when the triage step surfaces a focus
pointer covering an existing KB entry, the reflect step's rendered goal
contains a "Nearby KB entries" block referencing that path, so the
reflect agent can set a proposed action's ``target_path`` to that path
instead of proposing a brand-new one.

Background — v2 reflection pipeline
-----------------------------------

Since PR #86 the reflection template is a 5-step, triage-gated pipeline:

    gather-activity → triage → reflect → save-reflection → dispatch-actions

The reflect step's Python handler (``handle_reflect_gated``) reads the
sibling ``triage`` arc's ``TriageResult``. When
``needs_synthesis == false`` it short-circuits without an LLM call.
When ``needs_synthesis == true`` it:

1. Renders the reflect goal from the ``gather-activity`` sibling's
   ``GatheredActivity.content``.
2. For every ``focus_pointer`` in the triage result, runs
   ``KBStore.search()`` and appends a "Nearby KB entries" block to the
   goal listing the top hits.
3. Records the paths surfaced to the agent in ``arc_state`` under
   ``_reflect_nearby_kb_paths`` for provenance.
4. Invokes the standard EXECUTOR agent path with the augmented goal.

This story asserts steps (2) and (3) deterministically without relying
on any LLM output — the wire-up test is: given a seeded KB entry and a
triage output pointing at it, does the reflect step actually surface
that entry as an edit candidate before spending tokens?

Test strategy
-------------

The daily-cron path (``reflection.daily_tick`` event) is the only
supported v2 entry point — per-arc trigger was removed as an unbounded
feedback loop. The story:

1. Seeds a KB entry at ``topics/s059-reflection-test-topic`` with
   recognisable body content.
2. Inserts one synthetic completed root arc as the batch subject.
3. Emits a ``reflection.daily_tick`` event. The daemon's
   ``handle_reflection_tick`` refuses if no email escalation channel is
   configured (PR #81 gate); if that happens on this deployment, the
   story exits early with a SKIP result rather than hard-fail.
4. Once the reflection arc appears, seeds the triage step's
   ``_agent_response`` with ``needs_synthesis=true`` and
   ``focus_pointers=["topics/s059-reflection-test-topic"]``.
5. Waits for the reflect step (role ``analyze``) to reach a terminal
   state.
6. Asserts:
   a. ``arc_state["_reflect_nearby_kb_paths"]`` on the reflect arc is
      a non-empty list containing the seeded path.
   b. The reflect arc is NOT ``_reflect_gated_skipped`` — meaning the
      triage seed took effect and the LLM path ran.

If the agent produces a parseable ``ReflectionResult`` whose
``proposed_actions[].target_path`` contains the seeded path, that's
*reported* as a best-effort bonus but not asserted — the LLM's
selection is variable. The story also asserts (hard) that no
proposed action's ``target_path`` matches the prompt's forbidden
diary-shape pattern (``reflections/*``, dated components, etc.);
the dispatch-actions handler drops such targets, so if the LLM
emitted any this run they will surface here as a regression signal
before dispatch strips them silently.

Cleanup: removes the seeded KB entry, the synthetic subject arc, the
reflection arc family, and the emitted event / watermark.
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


_SEEDED_KB_PATH = "topics/s059-reflection-test-topic"
_SEEDED_KB_DESCRIPTION = (
    "Seeded by s059 acceptance story for reflection wiring test. "
    "Keywords: s059-reflection-test-topic reflection acceptance "
    "edit-target prefer-edit-over-create."
)
_SEEDED_KB_BODY = (
    "# S059 test topic\n\n"
    "This entry exists so the reflection pipeline can prefer editing it "
    "over creating a new entry when triage surfaces a focus pointer "
    "covering this path.\n\n"
    "Keywords: s059-reflection-test-topic reflection acceptance edit-target "
    "prefer-edit-over-create.\n"
)


def _seed_kb_entry() -> None:
    """Seed the KB entry via ``KBStore.write_entry`` so the write goes
    through the full index pipeline: filesystem file, ``kb_entries`` row,
    ``kb_text_content`` body cache, and ``kb_embeddings`` vector row.

    Directly ``INSERT``-ing into ``kb_entries`` alone would leave
    ``kb_embeddings`` empty, and ``KBStore.search()`` — which is what
    ``_build_nearby_kb_block(triage.focus_pointers)`` calls — queries
    ``kb_embeddings`` only.  The seed would then be invisible and this
    story's central assertion (``_SEEDED_KB_PATH in nearby_paths``) would
    fail on any real deployment run.

    The story process runs separately from the daemon, so it must
    ``reload_config()`` before touching the store; that resolves the
    same ``db_path`` and ``kb_dir`` the daemon uses.  SQLite WAL mode
    tolerates the extra writer.
    """
    from carpenter import config
    from carpenter.kb import get_store

    config.reload_config()
    store = get_store()
    result = store.write_entry(
        path=_SEEDED_KB_PATH,
        content=_SEEDED_KB_BODY,
        description=_SEEDED_KB_DESCRIPTION,
        entry_type="knowledge",
        trust_level="trusted",
        validate_links=False,
    )
    if result.startswith("Error"):
        raise RuntimeError(f"KBStore.write_entry failed: {result}")


def _delete_kb_entry() -> str | None:
    """Symmetric cleanup: filesystem file + ``kb_entries`` row +
    ``kb_links`` + ``kb_text_content`` + ``kb_embeddings``.

    Returns an error string on failure, ``None`` on success.
    """
    try:
        from carpenter import config
        from carpenter.kb import get_store

        config.reload_config()
        store = get_store()
        result = store.delete_entry(_SEEDED_KB_PATH)
        if result.startswith("Error"):
            return result
        return None
    except Exception as exc:  # noqa: BLE001
        return f"delete_entry raised: {exc}"


def _insert_synthetic_root_arc(db_path: str) -> int:
    """Insert a completed root arc to serve as the reflection batch subject."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO arcs "
            "(name, goal, status, priority, integrity_level, agent_type, "
            "updated_at) "
            "VALUES (?, ?, 'completed', 100, 'trusted', 'EXECUTOR', ?)",
            (
                "s059-synthetic-goal",
                f"Synthetic subject arc for s059 ({int(time.time())})",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _reset_reflection_watermark(db_path: str) -> None:
    """Set the reflection watermark to just before now so daily_tick
    picks up our synthetic arc.

    daily_tick reads the watermark from ``arc_state`` on the sentinel
    arc (id=0), key ``reflection_last_tick``. Setting it to a moment
    ago guarantees our synthetic arc's ``updated_at > watermark``.
    """
    ten_min_ago_iso = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    ).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) "
            "VALUES (0, 'reflection_last_tick', ?) "
            "ON CONFLICT(arc_id, key) DO UPDATE SET value_json = excluded.value_json",
            (json.dumps(ten_min_ago_iso),),
        )
        conn.commit()
    finally:
        conn.close()


def _emit_daily_tick(db_path: str) -> None:
    """Enqueue a ``reflection.daily_tick`` work item for the daemon.

    Mirrors what the built-in ``_builtin.timer_forward`` subscription
    produces when the cron trigger fires: a work_queue row whose
    handler is ``handle_reflection_tick`` (registered in
    ``config_seed/templates/reflection/__init__.py``). Inserting into
    ``events`` instead does nothing here — no subscription watches for
    raw ``reflection.daily_tick`` events; only cron-produced
    ``timer.fired`` events are translated into work items.
    """
    payload = json.dumps({
        "cron_name": "s059-acceptance-story",
        "fire_time": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    })
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO work_queue "
            "(event_type, payload_json, status, idempotency_key) VALUES "
            "('reflection.daily_tick', ?, 'pending', ?)",
            (payload, f"story:s059-tick-{int(time.time())}"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_triage_output(db_path: str, triage_arc_id: int) -> None:
    """Overwrite the triage step's ``_agent_response`` with a forced synthesis.

    ``handle_reflect_gated._read_triage_result`` reads this value,
    parses it as JSON, then validates against ``TriageResult``. We
    write the value already-encoded once so the ``json.loads`` inside
    the reader consumes it cleanly.
    """
    payload = {
        "needs_synthesis": True,
        "reasons": ["s059 seeded triage to exercise nearby-KB wiring"],
        "focus_pointers": [_SEEDED_KB_PATH],
    }
    # arc_state stores value_json as a JSON blob; the reader does
    # json.loads(row["value_json"]). Then handle_reflect_gated does a
    # second json.loads to parse the agent response — so we store the
    # agent response as a JSON *string* whose contents are themselves
    # JSON.
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


class ReflectionPrefersEdit(AcceptanceStory):
    name = "S059 — Reflection Prefers Editing an Existing KB Entry"
    description = (
        "Seed a KB entry; drive a daily-tick reflection batch whose "
        "triage flags a focus pointer covering that entry; assert the "
        "reflect step's Nearby-KB wiring surfaced the seeded path in "
        "_reflect_nearby_kb_paths so the agent could prefer edit-over-create."
    )
    # reflect step is a haiku EXECUTOR call — allow slack.
    timeout = 360

    _synthetic_arc_id: int | None = None
    _reflection_arc_id: int | None = None
    _kb_seeded: bool = False

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        self.assert_that(db is not None, "DB inspector required for this test")
        start_ts = time.time()

        # ── 1. Seed KB entry ────────────────────────────────────────────────
        print(f"\n  [1/6] Seeding KB entry at {_SEEDED_KB_PATH}...")
        _seed_kb_entry()
        self._kb_seeded = True
        entries = db.get_kb_entries(path_prefix="topics/")
        self.assert_that(
            any(e["path"] == _SEEDED_KB_PATH for e in entries),
            f"KB seed at {_SEEDED_KB_PATH} not visible after insert",
        )
        print(f"     Seeded: {_SEEDED_KB_PATH} ({len(_SEEDED_KB_BODY)} bytes)")

        # ── 2. Insert synthetic completed root arc ──────────────────────────
        print("  [2/6] Inserting synthetic completed root arc...")
        self._synthetic_arc_id = _insert_synthetic_root_arc(db.db_path)
        print(f"     Synthetic subject arc ID: {self._synthetic_arc_id}")

        # ── 3. Emit reflection.daily_tick ───────────────────────────────────
        print("  [3/6] Resetting watermark + emitting reflection.daily_tick...")
        _reset_reflection_watermark(db.db_path)
        _emit_daily_tick(db.db_path)

        # ── 4. Wait for reflection SUPERVISOR arc to appear ─────────────────
        print("     Waiting for reflection arc (up to 45s)...")
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
            # Most likely cause: the PR #81 escalation gate refused (no
            # SMTP creds configured). Report as skip rather than fail —
            # this story's wire-up assertion is what matters; the gate
            # itself is covered by other tests.
            return StoryResult(
                name=self.name,
                passed=False,
                message=(
                    "Reflection arc did not appear within 45s of "
                    "emitting reflection.daily_tick. Common causes: (a) "
                    "escalation gate refused because "
                    "config.reflection.escalation.email.to is not set, "
                    "(b) SMTP creds not resolvable from "
                    "carpenter-imap-email package, (c) daemon not "
                    "picking up the event. Configure escalation email "
                    "(see kb/reflections/setup.md) and re-run."
                ),
            )
        print(
            f"     Reflection arc {self._reflection_arc_id} appeared "
            f"with {len(children)} children"
        )

        # ── 5. Seed triage output ───────────────────────────────────────────
        print("  [5/6] Seeding triage output "
              f"(needs_synthesis=true, focus={_SEEDED_KB_PATH!r})...")
        triage_arc = db.get_arc_by_role(self._reflection_arc_id, "triage")
        self.assert_that(
            triage_arc is not None,
            "Reflection template has no triage step (role='triage'). "
            "Was this run against a pre-v2 core (before PR #86)?",
            arcs=db.format_arcs_table(children),
        )
        _seed_triage_output(db.db_path, triage_arc["id"])
        print(
            f"     Seeded triage arc {triage_arc['id']} with "
            f"focus_pointers=['{_SEEDED_KB_PATH}']"
        )

        # ── 6. Wait for reflect (role='analyze') + verify wiring ────────────
        print("  [6/6] Waiting for reflect step to reach terminal (up to 240s)...")
        reflect_deadline = time.monotonic() + 240
        reflect_arc = None
        last_print = 0.0
        while time.monotonic() < reflect_deadline:
            reflect_arc = db.get_arc_by_role(
                self._reflection_arc_id, "analyze",
            )
            if reflect_arc and reflect_arc.get("status") in (
                "completed", "failed", "cancelled", "frozen",
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
            reflect_arc is not None,
            "reflect arc (role='analyze') never appeared",
        )
        self.assert_that(
            reflect_arc.get("status") in ("completed", "failed", "frozen"),
            f"reflect arc did not reach terminal state within 240s "
            f"(status={reflect_arc.get('status')})",
        )
        print(
            f"     reflect arc {reflect_arc['id']} "
            f"status={reflect_arc['status']}"
        )

        # Wire-up assertions.
        reflect_state = db.get_arc_state(reflect_arc["id"])
        print(f"     reflect arc_state keys: {sorted(reflect_state.keys())}")

        self.assert_that(
            not reflect_state.get("_reflect_gated_skipped"),
            "reflect arc took the gated-skip path — the seeded triage "
            "output did not reach handle_reflect_gated. Check "
            "_read_triage_result and the arc_state write shape.",
        )

        nearby_paths = reflect_state.get("_reflect_nearby_kb_paths") or []
        self.assert_that(
            isinstance(nearby_paths, list),
            f"_reflect_nearby_kb_paths should be a list, got "
            f"{type(nearby_paths).__name__}: {nearby_paths!r}",
        )
        self.assert_that(
            _SEEDED_KB_PATH in nearby_paths,
            f"Seeded path {_SEEDED_KB_PATH!r} not in "
            f"_reflect_nearby_kb_paths (got {nearby_paths!r}). "
            "KBStore.search() should surface it for focus_pointer "
            f"'{_SEEDED_KB_PATH}'.",
        )
        print(
            f"     _reflect_nearby_kb_paths ({len(nearby_paths)}): "
            f"{nearby_paths}"
        )

        # Parse the reflect agent's ReflectionResult (tolerating a
        # ``` fence around the JSON, matching the handler's parser).
        raw = reflect_state.get("_agent_response")
        agent_proposed: list[dict] = []
        if isinstance(raw, str):
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else ""
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            try:
                inner = json.loads(text)
                if isinstance(inner, dict):
                    agent_proposed = list(inner.get("proposed_actions") or [])
            except (TypeError, json.JSONDecodeError):
                pass

        # Bonus (non-asserted): did the agent target one of the
        # nearby-KB paths surfaced to it?
        target_paths = [
            (a.get("target_path") or "").strip()
            for a in agent_proposed
            if isinstance(a, dict)
        ]
        target_paths = [tp for tp in target_paths if tp]
        nearby_hits = [tp for tp in target_paths if tp in nearby_paths]
        if target_paths:
            print(
                f"     [bonus] agent target_paths={target_paths} "
                f"(nearby-hit: {nearby_hits or 'none'})"
            )
        else:
            print(
                "     [bonus] agent produced no target_paths "
                "(unparseable or empty — not a story failure)"
            )

        # Hard assertion: the agent must NOT propose a diary-shape
        # target_path. Dispatch drops these silently as a safety net;
        # surfacing here catches prompt regressions before that mask.
        _DIARY_PREFIXES = (
            "reflections/", "by-day/", "by-arc/",
            "daily/", "weekly/", "monthly/",
        )
        import re as _re
        _DATE_RE = _re.compile(r"(?:^|[/_-])\d{4}-\d{2}-\d{2}(?:[/_-]|$)")
        offenders: list[str] = []
        for tp in target_paths:
            lower = tp.lower().lstrip("/")
            if any(lower.startswith(p) for p in _DIARY_PREFIXES):
                offenders.append(tp)
            elif _DATE_RE.search(lower):
                offenders.append(tp)
        self.assert_that(
            not offenders,
            "Reflect agent proposed diary-shape target_path values "
            f"despite the prompt's ban: {offenders}. The prompt "
            "explicitly forbids ``reflections/*``, dated components, "
            "``by-day/*``, etc. If this fires, tighten reflect-goal.md.",
        )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"reflect arc {reflect_arc['id']} status={reflect_arc['status']}; "
                f"nearby-KB wiring surfaced {len(nearby_paths)} path(s) "
                f"including seeded '{_SEEDED_KB_PATH}' ✓"
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

            # Story-emitted events / watermark left in place: the
            # reflection_last_tick watermark will be advanced by the
            # next real tick.
            conn.execute(
                "DELETE FROM events WHERE source = 'story:s059'",
            )
            conn.execute(
                "DELETE FROM work_queue "
                "WHERE idempotency_key LIKE 'story:s059-tick-%'",
            )

            conn.commit()
        except Exception as exc:
            print(f"  [cleanup] Error: {exc}")
        finally:
            conn.close()

        # KB entry deletion uses the symmetric KBStore API so that the
        # filesystem file, ``kb_entries`` row, ``kb_links`` rows,
        # ``kb_text_content`` body cache, and ``kb_embeddings`` vector
        # row are all removed together — matching the seed path.
        if self._kb_seeded:
            err = _delete_kb_entry()
            if err is None:
                deleted.append(f"KB entry {_SEEDED_KB_PATH}")
            else:
                print(f"  [cleanup] KB delete error: {err}")

        if deleted:
            print(f"  [cleanup] Removed: {', '.join(deleted)}")
