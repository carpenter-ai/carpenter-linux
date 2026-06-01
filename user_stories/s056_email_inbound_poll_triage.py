"""
S056 — Gmail inbound poll + triage platform integration.

Platform-integration check for the carpenter-gmail inbound pipeline.
This story exercises the running platform's trigger -> event -> work
queue -> arc-tree-creation path end-to-end:

- ``GmailPollTrigger`` (PollableTrigger) polls Gmail
  ``users.history.list`` and emits one ``email.received`` event per
  newly-arrived message id (we mock ``urlopen`` so no network).
- The package's ``email.received`` trigger subscription routes each
  event to ``handlers.triage_inbound.handle_email_received``, which
  spawns the ``email_triage`` arc tree (PLANNER -> EXECUTOR ->
  REVIEWER -> JUDGE).
- Trigger payload is MINIMAL: ``provider_message_id``,
  ``received_history_id``, ``account``.  No subject / from / body
  fields ever leave the trigger.

This story owns the integration assertions: the trigger's
``check()`` cycle drives event-bus emission with the correct
idempotency keys; the subscribed handler, when invoked, builds the
right arc tree; Resources are provenanced for the JUDGE; the work
queue receives ``arc.dispatch``.  It does NOT own manifest shape /
trigger declaration / template wiring — those are asserted in
``carpenter-gmail::manifest_shape``.

What this story verifies (STRICT)

  1. The ``GmailPollTrigger`` and ``handlers.triage_inbound`` modules
     load cleanly and expose the expected entrypoints.
  2. With a mocked ``urllib.request.urlopen`` that returns a synthetic
     Gmail history response, ``check()`` emits exactly TWO
     ``email.received`` events with stable idempotency keys
     ``gmail-poll-<mid>`` and watermark advances via CAS.
  3. The emitted event payloads carry ONLY the minimal fields
     (provider_message_id, received_history_id, account, plus the
     platform-stamped ``_trigger`` / ``_trigger_type`` /
     ``_source_package``).  In particular: no ``subject``, ``from``,
     ``snippet``, ``body``, or ``headers`` fields.  This is the
     load-bearing trust property (I3) — the trigger never exposes raw
     Gmail content; the trust pipeline does.
  4. Invoking ``handle_email_received`` with one of the emitted
     payloads constructs the FULL 4-arc tree:
       - children = [EXECUTOR (untrusted), REVIEWER (trusted),
         JUDGE (trusted)] in step order.
       - parent PLANNER seeds expected_account_email,
         received_history_id, template_name=email_triage,
         extract_kind=EmailTriageExtract, briefing_resource_id,
         _primary_resource_id.
       - EXECUTOR seeds provider_message_id, raw_resource_path,
         raw_resource_id.
       - REVIEWER has briefing + raw inputs, extract output.
       - JUDGE seeds _review_target_resource_id.
  5. Resource provenance: raw_email Resource is untrusted
     (produced_by_template=NULL); briefing is born-trusted
     (produced_by_template='email_triage', template_verdict='approved');
     extract is pending (produced_by_template='email_triage',
     template_verdict='pending').
  6. The work_queue has an arc.dispatch entry pointing at the
     EXECUTOR.

Why no LLM round-trip
---------------------

This story verifies the trigger -> subscription -> arc-tree-creation
front door.  The EXECUTOR's actual Gmail dispatch (and the REVIEWER /
JUDGE invocations) are exercised by carpenter-core's
trust-pipeline tests and would require real OAuth credentials here.
Mirrors s055's "verify shape, not network behaviour" pattern.

DB cleanup: removes any arcs / arc_state / arc_history / arc_resources
/ resources / events / work_queue / package_state rows created during
the test.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from user_stories.framework import (
    AcceptanceStory,
    CarpenterClient,
    DBInspector,
    StoryResult,
)


_PACKAGES_DIR_CANDIDATES = (
    os.environ.get("CARPENTER_PACKAGES_DIR", ""),
    "/home/pi/repos/carpenter-packages/packages",
    str(Path.home() / "repos" / "carpenter-packages" / "packages"),
)


def _find_email_package() -> Path | None:
    for candidate in _PACKAGES_DIR_CANDIDATES:
        if not candidate:
            continue
        path = Path(candidate) / "carpenter-gmail"
        if path.is_dir() and (path / "triggers" / "gmail_poll.py").is_file():
            return path
    return None


class _FakeUrlResponse:
    """Minimal stand-in for ``urllib.request.urlopen``'s context manager."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class EmailInboundPollTriage(AcceptanceStory):
    name = (
        "S056 — Gmail inbound poll + triage platform integration"
    )
    description = (
        "Verify GmailPollTrigger emits minimal email.received events and "
        "the email.received subscription spawns the full triage arc tree "
        "(PLANNER + EXECUTOR + REVIEWER + JUDGE) with correct Resource "
        "provenance and arc-state seeding.  Manifest-shape assertions "
        "for the trigger and template live in the package's own "
        "::manifest_shape story."
    )
    timeout = 600

    # cleanup records
    _created_arc_ids: list[int]
    _created_resource_ids: list[int]
    _created_event_ids: list[int]
    _seeded_state_keys: list[str]
    _saved_token_env: str | None
    _had_token_env: bool

    # constants shared between trigger setup and assertions
    _TEST_PACKAGE_NAME = "carpenter-gmail"
    _TEST_WATERMARK = "9000"
    _TEST_NEW_WATERMARK = "9100"
    _TEST_MESSAGE_IDS = ("msg-s056-aaa", "msg-s056-bbb")
    _TEST_ACCOUNT = "ben@example.com"

    def __init__(self) -> None:
        self._created_arc_ids = []
        self._created_resource_ids = []
        self._created_event_ids = []
        self._seeded_state_keys = []
        self._saved_token_env = None
        self._had_token_env = False

    # ------------------------------------------------------------------
    # Setup / teardown helpers
    # ------------------------------------------------------------------

    def _load_package_modules(self, pkg_dir: Path):
        """Import the carpenter-gmail tools/handlers/trigger modules via
        the same loader the platform uses at install time.

        Returns ``(tools_mod, handler_mod, trigger_mod)``.
        """
        from carpenter.packages.loaders import _import_package_module

        _import_package_module("carpenter-gmail", "data_models", pkg_dir)
        _import_package_module("carpenter-gmail", "scripts", pkg_dir)
        tools_mod = _import_package_module(
            "carpenter-gmail", "tools", pkg_dir,
        )
        handler_mod = _import_package_module(
            "carpenter-gmail", "handlers.triage_inbound", pkg_dir,
        )
        # The trigger module lives at ``triggers/gmail_poll.py`` — import
        # it directly so we can call its class.
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "carpenter_gmail_s056_gmail_poll",
            pkg_dir / "triggers" / "gmail_poll.py",
        )
        trigger_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(trigger_mod)
        return tools_mod, handler_mod, trigger_mod

    def _seed_packages_table(self) -> None:
        """Insert a minimal installed_packages row so package_state's FK
        is satisfied for the trigger's watermark writes.
        """
        from carpenter.db import db_transaction
        from carpenter.packages.installer import ensure_installer_tables

        with db_transaction() as db:
            ensure_installer_tables(db)
        with db_transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO installed_packages "
                "(name, version, hash, source_path, install_path, installed_at) "
                "VALUES (?, '0.7.0', 'abc', '/tmp/s', '/tmp/d', "
                "'2026-05-20T00:00:00Z')",
                (self._TEST_PACKAGE_NAME,),
            )

    def _seed_package_state(self) -> None:
        """Seed the watermark + account so check() does a real poll
        instead of running the first-run getProfile path.
        """
        from carpenter.packages.state import PackageStateHandle

        h = PackageStateHandle(self._TEST_PACKAGE_NAME)
        h.set("history_id", self._TEST_WATERMARK)
        h.set("gmail_account_email", self._TEST_ACCOUNT)
        self._seeded_state_keys.extend([
            "history_id", "gmail_account_email",
        ])

    def _set_token_env(self) -> None:
        from carpenter import config

        if "GMAIL_OAUTH_ACCESS_TOKEN" in os.environ:
            self._had_token_env = True
            self._saved_token_env = os.environ["GMAIL_OAUTH_ACCESS_TOKEN"]
        os.environ["GMAIL_OAUTH_ACCESS_TOKEN"] = "fake-token-s056"
        # The handler-triggered arc-tree creation also wants an
        # operator_email so the expected-account seed is non-empty.  We
        # pass the account via the event payload itself, so config is a
        # belt-and-braces fallback.
        config.CONFIG["operator_email"] = self._TEST_ACCOUNT

    def _restore_token_env(self) -> None:
        from carpenter import config

        if self._had_token_env and self._saved_token_env is not None:
            os.environ["GMAIL_OAUTH_ACCESS_TOKEN"] = self._saved_token_env
        else:
            os.environ.pop("GMAIL_OAUTH_ACCESS_TOKEN", None)
        config.CONFIG.pop("operator_email", None)

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _resources_for_arc(
        self, db: DBInspector, arc_id: int,
    ) -> list[dict]:
        return db.fetchall(
            "SELECT r.id, r.content_type, r.produced_by_template, "
            "r.template_verdict, ar.role "
            "FROM arc_resources ar JOIN resources r "
            "ON ar.resource_id = r.id "
            "WHERE ar.arc_id = ? "
            "ORDER BY r.id",
            (arc_id,),
        )

    def _work_queue_for_arc(
        self, db: DBInspector, arc_id: int,
    ) -> list[dict]:
        return db.fetchall(
            "SELECT id, event_type, idempotency_key, payload_json "
            "FROM work_queue WHERE idempotency_key = ?",
            (f"arc_dispatch:{arc_id}",),
        )

    def _fetch_events(self, db: DBInspector) -> list[dict]:
        return db.fetchall(
            "SELECT id, event_type, payload_json, idempotency_key "
            "FROM events WHERE event_type = ? "
            "AND payload_json LIKE ? "
            "ORDER BY id",
            ("email.received", "%s056%"),
        )

    # ------------------------------------------------------------------
    # Main story
    # ------------------------------------------------------------------

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        self.assert_that(db is not None, "DB inspector required")
        _ = time.time()

        pkg_dir = _find_email_package()
        self.assert_that(
            pkg_dir is not None,
            "carpenter-gmail package source not found.  Checked: "
            + ", ".join(c for c in _PACKAGES_DIR_CANDIDATES if c),
        )

        # Manifest version pinning + triage-asset declaration checks
        # are owned by the package's own ``::manifest_shape`` story —
        # we just need the modules to load for the integration checks
        # below.

        # ── 1. Module loading + handler signature ────────────────────────
        print("\n  [1/7] Loading package modules and verifying handler...")
        tools_mod, handler_mod, trigger_mod = self._load_package_modules(
            pkg_dir,
        )
        self.assert_that(
            hasattr(handler_mod, "handle_email_received"),
            "handlers.triage_inbound must export handle_email_received",
        )
        self.assert_that(
            callable(handler_mod.handle_email_received),
            "handle_email_received must be callable",
        )
        self.assert_that(
            trigger_mod.GmailPollTrigger.trigger_type() == "gmail_poll",
            f"GmailPollTrigger.trigger_type() must be 'gmail_poll'; "
            f"got: {trigger_mod.GmailPollTrigger.trigger_type()!r}",
        )
        # Sanity: tools module exposes the helper the handler uses.
        self.assert_that(
            hasattr(tools_mod, "_create_triage_arc_tree"),
            "carpenter_gmail.tools must expose _create_triage_arc_tree",
        )
        print("     modules loaded; GmailPollTrigger + handler wired")

        # ── 3. Drive a real poll cycle against a mocked Gmail ────────────
        print("  [2/7] Driving GmailPollTrigger.check() with mocked Gmail...")
        from carpenter.packages.state import PackageStateHandle

        self._seed_packages_table()
        self._seed_package_state()
        self._set_token_env()

        handle = PackageStateHandle(self._TEST_PACKAGE_NAME)

        history_response = {
            "historyId": self._TEST_NEW_WATERMARK,
            "history": [
                {
                    "id": "9050",
                    "messagesAdded": [
                        {"message": {"id": self._TEST_MESSAGE_IDS[0]}},
                    ],
                },
                {
                    "id": self._TEST_NEW_WATERMARK,
                    "messagesAdded": [
                        {"message": {"id": self._TEST_MESSAGE_IDS[1]}},
                    ],
                },
            ],
        }
        captured_urls: list[str] = []

        def _fake_urlopen(req, timeout=None):
            url = getattr(req, "full_url", None) or req
            captured_urls.append(url)
            return _FakeUrlResponse(json.dumps(history_response).encode())

        trig = trigger_mod.GmailPollTrigger(
            "s056-probe",
            {"cadence_seconds": 900, "event_type": "email.received"},
            source_package=self._TEST_PACKAGE_NAME,
            package_state=handle,
        )
        with patch.object(
            trigger_mod.urllib.request, "urlopen", _fake_urlopen,
        ):
            trig.check()

        self.assert_that(
            len(captured_urls) == 1,
            f"check() must issue exactly one Gmail HTTP call; got: "
            f"{len(captured_urls)} ({captured_urls})",
        )
        self.assert_that(
            "/history" in captured_urls[0]
            and f"startHistoryId={self._TEST_WATERMARK}" in captured_urls[0],
            f"check() must call users.history.list with the seeded "
            f"watermark; got URL: {captured_urls[0]!r}",
        )
        print("     history.list called with watermark=" + self._TEST_WATERMARK)

        # ── 4. Two events emitted with stable idempotency keys ───────────
        print("  [3/7] Verifying event-bus emissions...")
        events = self._fetch_events(db)
        self._created_event_ids.extend(e["id"] for e in events)
        self.assert_that(
            len(events) == 2,
            f"Expected exactly 2 email.received events; got: {len(events)} "
            f"({[(e['id'], e['idempotency_key']) for e in events]})",
        )
        idem_keys = {e["idempotency_key"] for e in events}
        expected_idem = {
            f"gmail-poll-{mid}" for mid in self._TEST_MESSAGE_IDS
        }
        self.assert_that(
            idem_keys == expected_idem,
            f"event idempotency_key set must be {expected_idem!r}; "
            f"got: {idem_keys!r}",
        )
        # Watermark advanced via CAS.
        self.assert_that(
            handle.get("history_id") == self._TEST_NEW_WATERMARK,
            f"watermark must advance to {self._TEST_NEW_WATERMARK!r}; "
            f"got: {handle.get('history_id')!r}",
        )
        print(
            f"     2 events emitted; watermark advanced "
            f"{self._TEST_WATERMARK} -> {self._TEST_NEW_WATERMARK}"
        )

        # ── 5. Payload trust contract: NO body/subject/from leakage ──────
        print("  [4/7] Verifying event payloads carry no Gmail content...")
        FORBIDDEN = ("subject", "from", "snippet", "body", "headers")
        ALLOWED_NON_PLATFORM = {
            "provider_message_id", "received_history_id", "account",
        }
        ALLOWED_PLATFORM = {
            "_trigger", "_trigger_type", "_source_package",
        }
        for ev in events:
            payload = json.loads(ev["payload_json"])
            for forbidden in FORBIDDEN:
                self.assert_that(
                    forbidden not in payload,
                    f"event {ev['id']} payload must NOT contain "
                    f"{forbidden!r} (Gmail content leak); got: "
                    f"{sorted(payload.keys())}",
                )
            # Required minimal fields all present.
            for required in ALLOWED_NON_PLATFORM:
                self.assert_that(
                    required in payload,
                    f"event {ev['id']} payload must contain {required!r}; "
                    f"got: {sorted(payload.keys())}",
                )
            self.assert_that(
                payload["_source_package"] == self._TEST_PACKAGE_NAME,
                f"event {ev['id']} _source_package must be "
                f"{self._TEST_PACKAGE_NAME!r}; got: "
                f"{payload.get('_source_package')!r}",
            )
            self.assert_that(
                payload["received_history_id"] == self._TEST_NEW_WATERMARK,
                f"event {ev['id']} received_history_id must be "
                f"{self._TEST_NEW_WATERMARK!r}; got: "
                f"{payload.get('received_history_id')!r}",
            )
            # Defence in depth: no other unknown keys.
            extra = set(payload.keys()) - (
                ALLOWED_NON_PLATFORM | ALLOWED_PLATFORM
            )
            self.assert_that(
                not extra,
                f"event {ev['id']} payload has unexpected extra keys "
                f"{extra!r}; payload: {sorted(payload.keys())}",
            )
        print("     all event payloads minimal; no body/subject/from leakage")

        # ── 6. Subscription handler spawns the triage arc tree ───────────
        print("  [5/7] Invoking handle_email_received and checking arc tree...")
        target_payload = json.loads(events[0]["payload_json"])
        # The handler returns None; arc id is observable only via the DB.
        # Snapshot the arc id space so we can identify what got created.
        arcs_before = {
            r["id"] for r in db.fetchall("SELECT id FROM arcs")
        }
        handler_mod.handle_email_received(target_payload)
        arcs_after = {
            r["id"] for r in db.fetchall("SELECT id FROM arcs")
        }
        new_arc_ids = sorted(arcs_after - arcs_before)
        # PLANNER + 3 children == 4 new arcs.
        self.assert_that(
            len(new_arc_ids) == 4,
            f"handle_email_received must create exactly 4 arcs (PLANNER "
            f"+ EXECUTOR + REVIEWER + JUDGE); got: {len(new_arc_ids)} "
            f"({new_arc_ids})",
        )
        # Identify the parent (PLANNER, no parent_id).
        parent_rows = db.fetchall(
            "SELECT id, agent_type FROM arcs WHERE id IN ({})"
            " AND parent_id IS NULL".format(
                ",".join("?" * len(new_arc_ids)),
            ),
            tuple(new_arc_ids),
        )
        self.assert_that(
            len(parent_rows) == 1 and parent_rows[0]["agent_type"] == "PLANNER",
            f"Expected exactly one PLANNER parent among the new arcs; "
            f"got: {parent_rows}",
        )
        parent_id = parent_rows[0]["id"]
        children = db.get_arc_children(parent_id)
        self._created_arc_ids.extend(new_arc_ids)
        print(f"     spawned parent arc id={parent_id} with 3 children")

        # ── 7. Strict arc-tree shape + arc-state seeding ─────────────────
        print("  [6/7] Strict assertions on triage arc tree shape...")
        agent_types = [c["agent_type"] for c in children]
        self.assert_that(
            agent_types == ["EXECUTOR", "REVIEWER", "JUDGE"],
            f"triage children must be [EXECUTOR, REVIEWER, JUDGE] in "
            f"step order; got: {agent_types}",
        )
        executor, reviewer, judge = children
        self.assert_that(
            executor["integrity_level"] == "untrusted",
            f"triage EXECUTOR must be untrusted; got: "
            f"{executor['integrity_level']!r}",
        )
        self.assert_that(
            reviewer["integrity_level"] == "trusted",
            f"triage REVIEWER must be trusted; got: "
            f"{reviewer['integrity_level']!r}",
        )
        self.assert_that(
            judge["integrity_level"] == "trusted",
            f"triage JUDGE must be trusted; got: "
            f"{judge['integrity_level']!r}",
        )

        # Parent state: expected_account, template_name, extract_kind,
        # briefing_resource_id, _primary_resource_id, received_history_id.
        parent_state = db.get_arc_state(parent_id)
        self.assert_that(
            parent_state.get("expected_account_email") == self._TEST_ACCOUNT,
            f"parent expected_account_email must be {self._TEST_ACCOUNT!r}; "
            f"got: {parent_state.get('expected_account_email')!r}",
        )
        self.assert_that(
            parent_state.get("template_name") == "email_triage",
            f"parent template_name must be 'email_triage'; got: "
            f"{parent_state.get('template_name')!r}",
        )
        self.assert_that(
            parent_state.get("extract_kind") == "EmailTriageExtract",
            f"parent extract_kind must be 'EmailTriageExtract'; got: "
            f"{parent_state.get('extract_kind')!r}",
        )
        self.assert_that(
            parent_state.get("received_history_id") == self._TEST_NEW_WATERMARK,
            f"parent received_history_id must propagate from event "
            f"payload ({self._TEST_NEW_WATERMARK!r}); got: "
            f"{parent_state.get('received_history_id')!r}",
        )
        self.assert_that(
            "briefing_resource_id" in parent_state,
            f"parent must seed briefing_resource_id; state keys: "
            f"{sorted(parent_state.keys())}",
        )
        self.assert_that(
            "_primary_resource_id" in parent_state,
            f"parent must seed _primary_resource_id; state keys: "
            f"{sorted(parent_state.keys())}",
        )

        # EXECUTOR state: provider_message_id matches the event payload.
        executor_state = db.get_arc_state(executor["id"])
        self.assert_that(
            executor_state.get("provider_message_id")
            == target_payload["provider_message_id"],
            f"EXECUTOR provider_message_id must propagate from event "
            f"({target_payload['provider_message_id']!r}); got: "
            f"{executor_state.get('provider_message_id')!r}",
        )
        for required in ("raw_resource_path", "raw_resource_id"):
            self.assert_that(
                required in executor_state,
                f"EXECUTOR must seed {required!r}; state keys: "
                f"{sorted(executor_state.keys())}",
            )

        # REVIEWER state.
        reviewer_state = db.get_arc_state(reviewer["id"])
        for required in (
            "briefing_resource_id", "raw_resource_path",
            "raw_resource_id", "extract_resource_id",
            "extract_kind", "template_name",
        ):
            self.assert_that(
                required in reviewer_state,
                f"REVIEWER must seed {required!r}; state keys: "
                f"{sorted(reviewer_state.keys())}",
            )
        self.assert_that(
            reviewer_state.get("template_name") == "email_triage",
            f"REVIEWER template_name must be 'email_triage'; got: "
            f"{reviewer_state.get('template_name')!r}",
        )

        # JUDGE state.
        judge_state = db.get_arc_state(judge["id"])
        self.assert_that(
            "_review_target_resource_id" in judge_state,
            f"JUDGE must seed _review_target_resource_id; state keys: "
            f"{sorted(judge_state.keys())}",
        )
        print("     arc-state seeding correct on all four arcs")

        # ── 8. Resource provenance + work-queue dispatch ─────────────────
        print("  [7/7] Verifying Resource provenance and work-queue...")
        executor_resources = self._resources_for_arc(db, executor["id"])
        raw_outputs = [
            r for r in executor_resources if r["role"] == "output"
        ]
        self.assert_that(
            len(raw_outputs) == 1,
            f"EXECUTOR must have exactly one raw_email output Resource; "
            f"got: {raw_outputs}",
        )
        raw = raw_outputs[0]
        self.assert_that(
            raw["produced_by_template"] is None,
            f"raw_email Resource must be untrusted "
            f"(produced_by_template=NULL); got: "
            f"{raw['produced_by_template']!r}",
        )
        self._created_resource_ids.append(raw["id"])

        reviewer_resources = self._resources_for_arc(db, reviewer["id"])
        reviewer_inputs = [
            r for r in reviewer_resources if r["role"] == "input"
        ]
        reviewer_outputs = [
            r for r in reviewer_resources if r["role"] == "output"
        ]
        self.assert_that(
            len(reviewer_inputs) == 2,
            f"REVIEWER must have 2 input Resources (briefing + raw); "
            f"got: {len(reviewer_inputs)}",
        )
        self.assert_that(
            len(reviewer_outputs) == 1,
            f"REVIEWER must have 1 output Resource (pending extract); "
            f"got: {len(reviewer_outputs)}",
        )
        briefings = [
            r for r in reviewer_inputs
            if r["produced_by_template"] == "email_triage"
            and r["template_verdict"] == "approved"
        ]
        self.assert_that(
            len(briefings) == 1,
            f"REVIEWER must have a born-trusted briefing Resource "
            f"(produced_by_template='email_triage', "
            f"template_verdict='approved'); got inputs: {reviewer_inputs}",
        )
        self._created_resource_ids.append(briefings[0]["id"])

        extract = reviewer_outputs[0]
        self.assert_that(
            extract["produced_by_template"] == "email_triage",
            f"REVIEWER extract must have "
            f"produced_by_template='email_triage'; got: "
            f"{extract['produced_by_template']!r}",
        )
        self.assert_that(
            extract["template_verdict"] == "pending",
            f"REVIEWER extract must have template_verdict='pending' "
            f"(JUDGE hasn't graduated it yet); got: "
            f"{extract['template_verdict']!r}",
        )
        self._created_resource_ids.append(extract["id"])

        # Work queue: arc.dispatch entry for the EXECUTOR.
        work_rows = self._work_queue_for_arc(db, executor["id"])
        self.assert_that(
            len(work_rows) >= 1,
            f"must enqueue arc.dispatch for EXECUTOR id={executor['id']} "
            f"(idempotency_key='arc_dispatch:{executor['id']}'); "
            f"work rows: {work_rows}",
        )
        print(
            "     Resource provenance OK (raw untrusted, briefing approved, "
            "extract pending); arc.dispatch enqueued"
        )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                "GmailPollTrigger emits exactly 2 "
                "minimal email.received events (no subject/from/body "
                "leakage) per mocked poll cycle; watermark CAS-advances "
                f"{self._TEST_WATERMARK} -> {self._TEST_NEW_WATERMARK}; "
                "handle_email_received spawns the full triage arc tree "
                "(PLANNER + EXECUTOR(untrusted) + REVIEWER + JUDGE) with "
                "correct Resource provenance (raw untrusted, briefing "
                "approved, extract pending), arc-state seeding "
                "(received_history_id propagates to parent), and "
                "arc.dispatch enqueued for the EXECUTOR."
            ),
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        try:
            self._restore_token_env()
        except Exception as exc:  # noqa: BLE001
            print(f"  [cleanup] token env restore error: {exc}")

        if db is None:
            return

        conn = sqlite3.connect(db.db_path)
        try:
            deleted_arcs: list[str] = []
            for arc_id in self._created_arc_ids:
                conn.execute(
                    "DELETE FROM arc_state WHERE arc_id = ?", (arc_id,),
                )
                conn.execute(
                    "DELETE FROM arc_history WHERE arc_id = ?", (arc_id,),
                )
                conn.execute(
                    "DELETE FROM arc_resources WHERE arc_id = ?", (arc_id,),
                )
                conn.execute(
                    "DELETE FROM conversation_arcs WHERE arc_id = ?",
                    (arc_id,),
                )
                conn.execute(
                    "DELETE FROM work_queue WHERE "
                    "idempotency_key = ? OR idempotency_key = ?",
                    (
                        f"arc_dispatch:{arc_id}",
                        f"arc.dispatch:{arc_id}",
                    ),
                )
                conn.execute(
                    "DELETE FROM arcs WHERE id = ?", (arc_id,),
                )
                deleted_arcs.append(str(arc_id))

            deleted_resources: list[str] = []
            for rid in self._created_resource_ids:
                conn.execute(
                    "DELETE FROM arc_resources WHERE resource_id = ?",
                    (rid,),
                )
                conn.execute(
                    "DELETE FROM resources WHERE id = ?", (rid,),
                )
                deleted_resources.append(str(rid))

            # Clear the events we wrote (idempotency keys + plain delete).
            for eid in self._created_event_ids:
                conn.execute("DELETE FROM events WHERE id = ?", (eid,))

            # Clear package_state rows for keys we touched.
            for key in self._seeded_state_keys + [
                "gmail_poll_in_progress",
                "gmail_poll_backoff_until",
            ]:
                conn.execute(
                    "DELETE FROM package_state "
                    "WHERE package_name = ? AND key = ?",
                    (self._TEST_PACKAGE_NAME, key),
                )

            conn.commit()
            if deleted_arcs:
                print(
                    f"  [cleanup] Removed arc rows: "
                    f"{', '.join(deleted_arcs)}"
                )
            if deleted_resources:
                print(
                    f"  [cleanup] Removed resource rows: "
                    f"{', '.join(deleted_resources)}"
                )
            if self._created_event_ids:
                print(
                    f"  [cleanup] Removed event rows: "
                    f"{', '.join(str(e) for e in self._created_event_ids)}"
                )
        except Exception as exc:  # noqa: BLE001
            print(f"  [cleanup] DB cleanup error: {exc}")
        finally:
            conn.close()
