"""
S062 — The Model Cannot Approve Its Own Package Install

This is the story that pins the security decision at the centre of the
remote-install design, so it asserts the refusal as hard as it asserts
the success.

The approval link is returned to the chat agent that asked for the
install and is then persisted in the conversation. So the model holds
the token that names the request, and the platform ships an HTTP POST
dispatch verb. If possession of the link were enough to approve, a
prompt-injected agent could request and approve in one move, cutting
out the only human the design rests on. Therefore:

* ``GET /api/package-install/<token>`` is **open** — a human has to be
  able to read the page from whatever chat app or mail client carried
  the link, and no bearer token travels there.
* ``POST /api/package-install/<token>/decide`` is **not** open. It
  needs a session authenticated to the platform *in addition to* the
  single-use token in the path.
* The refusal must not spend the token either, or the attack becomes a
  denial of service: ask, let the refusal burn the link, and the human
  never gets to decide.

Preconditions (environment variables; the story SKIPS when absent)
------------------------------------------------------------------

  CARPENTER_TEST_PKG_GIT_URL / CARPENTER_TEST_PKG_NAME
      The fixture repository and the manifest name of its package, as
      in s060. Must need no read credential.
  CARPENTER_TEST_PKG_SUBDIR, CARPENTER_TEST_PKG_REF  (optional)

The story also SKIPS when the deployment has no ``ui_token``
configured: with no token the auth middleware passes everything
through, "the model cannot approve its own install" is not a property
of that deployment, and a pass here would be a lie. The check is a
probe of a protected endpoint, not a guess.

What this story asserts (STRICT)
--------------------------------

1. The unauthenticated ``POST .../decide`` is refused with 401.
2. That refusal changed nothing: the row is still ``requested``, with
   ``decided_at`` NULL, no grants recorded, and no
   ``package.install.approved`` work item enqueued for it.
3. The token was not spent: the page still opens afterwards, and the
   page itself says in words that approving needs a signed-in session.
4. The authenticated ``POST .../decide`` then succeeds and queues the
   install.
5. A replay of that POST — the same token, authenticated — is refused
   with 410 Gone, and the replay does not enqueue a second install.
6. The approved install still completes, so the refusals above cost the
   operator nothing.

Cleanup: uninstalls the package, deletes the request row, restores
``config.yaml`` byte for byte.
"""

from __future__ import annotations

import time

from user_stories.framework import (
    AcceptanceStory,
    CarpenterClient,
    DBInspector,
    StoryResult,
)
from user_stories import package_install_support as support


class ModelCannotApproveOwnInstall(AcceptanceStory):
    name = "S062 — The Model Cannot Approve Its Own Install"
    description = (
        "The unauthenticated decide POST is refused with 401 without "
        "spending the token; the authenticated one succeeds; a replay is "
        "410."
    )
    artifact_prefix = "s062"
    timeout = 1200

    def __init__(self) -> None:
        self._token = ""
        self._installed_name = ""
        self._config: support.ConfigPatch | None = None

    # ------------------------------------------------------------------

    def _approved_work_items(self, db: DBInspector, request_id: int) -> list[dict]:
        """Work-queue rows enqueued for this request, by idempotency key.

        The decide route keys the item ``pkg_install:<row id>`` and puts
        the row id — never the token — in the payload, so this is the
        exact and only item that an approval of this request can create.
        """
        return db.fetchall(
            "SELECT * FROM work_queue WHERE event_type = ? "
            "AND idempotency_key = ?",
            ("package.install.approved", f"pkg_install:{request_id}"),
        )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        self.assert_that(db is not None, "DB inspector required")
        env = support.require_env(support.ENV_GIT_URL, support.ENV_NAME)
        url = env[support.ENV_GIT_URL]
        name = env[support.ENV_NAME]
        spec = support.git_spec(
            url,
            ref=support.optional_env(support.ENV_REF),
            subdir=support.optional_env(support.ENV_SUBDIR),
        )

        if not support.auth_is_enforced(client):
            import pytest
            pytest.skip(
                "This deployment has no 'ui_token' configured, so the auth "
                "middleware passes every request through and the decide "
                "route cannot refuse anyone. The property this story pins "
                "does not exist here; reporting a pass would be false."
            )
        if support.install_record(db, name) is not None:
            import pytest
            pytest.skip(
                f"package {name!r} is already installed on this "
                f"deployment; this story will not replace a package it "
                f"did not install"
            )

        start_ts = time.time()
        self._config = support.ConfigPatch(client, self.artifact_prefix)
        self._config.allowlist_host(support.host_of(url))
        self._config.apply()

        # ── 1. Get a real pending request ───────────────────────────
        print(f"\n  [1/6] Asking in chat for {name}")
        conv_id, reply = client.chat(
            f"Please install the capability package at {spec}", timeout=300,
        )
        token = support.find_approval_token(reply)
        self.assert_that(
            bool(token),
            "No approval URL in the reply, so there is no token to try to "
            "misuse",
            tools_called=support.tool_names_called(db, conv_id),
            reply_preview=reply[:600],
        )
        self._token = token
        row = support.request_row(db, token)
        self.assert_that(
            row is not None and row["status"] == support.STATUS_REQUESTED,
            f"Expected a 'requested' row, got "
            f"{row['status'] if row else 'none'!r}",
        )
        request_id = int(row["id"])
        self.assert_that(
            not self._approved_work_items(db, request_id),
            "An install work item exists before anyone decided",
        )
        print(f"     request {token[:8]} (row {request_id}) is pending")

        # ── 2. The unauthenticated POST is refused ──────────────────
        print("  [2/6] POSTing a decision with NO platform credential")
        refused = support.post_decision_unauthenticated(
            client, token, "approve", support.grants_for([name]),
        )
        self.assert_that(
            refused.status_code == 401,
            f"The unauthenticated approval was NOT refused with 401: got "
            f"{refused.status_code} {refused.text[:300]}. Possession of "
            f"the link must not be enough to install software — the model "
            f"that asked holds that link.",
        )
        print(f"     refused: {refused.status_code} {refused.text[:120]}")

        # ── 3. The refusal spent nothing and changed nothing ────────
        print("  [3/6] Asserting the refusal changed nothing")
        after = support.request_row(db, token)
        self.assert_that(
            after["status"] == support.STATUS_REQUESTED,
            f"The refused POST moved the row to {after['status']!r}; a "
            f"refusal must leave the request decidable by a human",
        )
        self.assert_that(
            after["decided_at"] is None,
            f"The refused POST stamped decided_at "
            f"({after['decided_at']!r})",
        )
        self.assert_that(
            not after.get("grants"),
            f"The refused POST recorded grants: {after.get('grants')!r}",
        )
        self.assert_that(
            not self._approved_work_items(db, request_id),
            "The refused POST enqueued an install work item",
        )
        self.assert_that(
            support.install_record(db, name) is None,
            f"{name!r} was installed by a refused request",
        )

        page = support.get_approval_page_unauthenticated(client, token)
        self.assert_that(
            page.status_code == 200,
            f"The link stopped working after the refused POST "
            f"({page.status_code}) — a refusal that spends the token is a "
            f"denial of service against the human who has to decide",
        )
        self.assert_contains(
            page.text, "APPROVING needs you to be signed in",
            "the page must say that reading it is not deciding it",
        )
        print("     row untouched, no work item, link still opens ✓")

        # ── 4. The authenticated POST succeeds ──────────────────────
        print("  [4/6] POSTing the same decision, authenticated")
        accepted = support.post_decision(
            client, token, "approve", support.grants_for([name]),
        )
        self.assert_that(
            accepted.status_code == 200,
            f"The authenticated approval was refused: "
            f"{accepted.status_code} {accepted.text[:300]}",
        )
        payload = accepted.json()
        self.assert_that(
            payload.get("recorded") is True and payload.get("queued") is True,
            f"Expected the approval recorded and queued, got {payload!r}",
        )
        self._installed_name = name
        items = self._approved_work_items(db, request_id)
        self.assert_that(
            len(items) == 1,
            f"Expected exactly one install work item, got {len(items)}",
        )

        # ── 5. A replay is 410, and enqueues nothing more ───────────
        print("  [5/6] Replaying the authenticated POST")
        replay = support.post_decision(
            client, token, "approve", support.grants_for([name]),
        )
        self.assert_that(
            replay.status_code == 410,
            f"A replayed decision should be 410 Gone (the token is "
            f"single-use), got {replay.status_code} {replay.text[:200]}",
        )
        self.assert_that(
            len(self._approved_work_items(db, request_id)) == 1,
            "The replay enqueued a second install for the same request",
        )
        replayed_row = support.request_row(db, token)
        self.assert_that(
            replayed_row["status"] != support.STATUS_REQUESTED,
            f"The replay reset the row to {replayed_row['status']!r}",
        )

        # ── 6. The approved install still completes ─────────────────
        print("  [6/6] Waiting for the approved install to finish")
        final = support.wait_for_request_status(
            db, token,
            (support.STATUS_INSTALLED, support.STATUS_FAILED), timeout=600,
        )
        self.assert_that(
            final["status"] == support.STATUS_INSTALLED,
            f"The install the human DID approve failed: "
            f"{final.get('error')!r}",
            result=final.get("result"),
        )
        self.assert_that(
            support.install_record(db, name) is not None,
            f"No installed_packages row for {name!r} after a successful "
            f"approval",
        )

        elapsed = time.time() - start_ts
        return self.result(
            f"unauthenticated decide refused with 401 without spending the "
            f"token, authenticated decide installed {name}, replay 410 "
            f"({elapsed:.0f}s)"
        )

    # ------------------------------------------------------------------

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        if self._installed_name:
            support.uninstall_quietly(self._installed_name)
            support.remove_install_tree(self._installed_name)
            if db is not None and support.install_record(
                db, self._installed_name,
            ) is not None:
                support.delete_install_record(db, self._installed_name)
                print("  [cleanup] removed the install row the CLI left")
        if db is not None and self._token:
            removed = support.delete_request_rows(db, [self._token])
            print(f"  [cleanup] removed {removed} request row(s)")
        if self._config is not None:
            try:
                self._config.restore()
            except Exception as exc:  # noqa: BLE001 — report, never mask
                print(f"  ! CONFIG NOT RESTORED: {exc}")
