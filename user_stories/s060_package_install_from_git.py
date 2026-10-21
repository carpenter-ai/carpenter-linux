"""
S060 — Install a Capability Package from a Remote Git Repository, over Chat

The headline story for remote package installs. A person asks in chat
for a capability package held in a git repository to be installed. The
agent may not install it: it can only *ask*, by minting a one-time
approval link. A human opens that link, reads where the bytes came
from, approves, and the platform installs the package itself on the
work queue — outside the model turn — then hot-loads it and reports
back into the conversation.

Preconditions (environment variables; the story SKIPS when absent)
------------------------------------------------------------------

  CARPENTER_TEST_PKG_GIT_URL
      The fixture repository, e.g. ``git+https://<host>/<org>/<repo>.git``
      (the ``git+`` prefix is added if omitted). Must need no read
      credential: a reload of the config makes a ``package_sources``
      entry live, but it cannot put a ``credential_ref`` token into the
      running server's environment.
  CARPENTER_TEST_PKG_NAME
      The ``name`` in that package's ``manifest.yaml``.
  CARPENTER_TEST_PKG_TOOL
      The name of a chat tool the package contributes. The story asks
      the agent to call it after the install, which is what proves the
      package hot-loaded into the *running* process rather than merely
      landing on disk.
  CARPENTER_TEST_PKG_SUBDIR    (optional)
      Path of the package root inside the repository, when the
      repository holds several. Omit when the repository root is the
      package.
  CARPENTER_TEST_PKG_REF       (optional)
      Tag, branch or commit to pin the request to.
  CARPENTER_TEST_PKG_SIDE_EFFECT_PATH  (optional, strengthens step 4)
      A path the fixture package's own code creates the first time it
      is imported. When set, the story asserts it does NOT exist before
      the human consents and DOES exist afterwards — the sharpest
      available form of "no fetched code ran before consent". When
      unset, the story falls back to asserting the install destination
      and the install record are both absent, and says so in its
      output.

What this story asserts (STRICT)
--------------------------------

 1. The agent called ``request_package_install`` with the source spec
    it was given, and called no install tool of any kind: not
    ``install_package``, not ``uninstall_package``. The evidence is the
    ``tool_calls`` audit rows, not the agent's description of itself.
 2. A ``pending_package_installs`` row exists in status ``requested``,
    bound to the conversation that asked, with the spec recorded
    verbatim in ``sources_json``.
 3. The one-time approval URL came back in the chat reply. The tool
    returns a ``FinalReply``, so the URL reaches the person verbatim
    rather than paraphrased.
 4. Nothing was installed and no fetched code ran before consent: no
    ``installed_packages`` row, no install destination on disk, and —
    when ``CARPENTER_TEST_PKG_SIDE_EFFECT_PATH`` is set — no marker
    from the package's own code.
 5. The approval page opens with NO platform credential (a human reads
    it from whatever carried the link), shows the provenance of the
    exact bytes — resolved commit, tree hash, source spec, host — and
    states the capability position: the grant boxes when the package
    declares ``platform_capabilities``, or the explicit "declares no
    platform_capabilities" sentence when it does not.
 6. The GET mutated nothing: the row is still ``requested`` afterwards.
 7. The authenticated ``POST .../decide`` records the approval and
    queues the install.
 8. The work queue installs it: the row reaches ``installed`` and its
    ``result_json`` reports this package as ``installed``, loaded live
    with ``needs_restart`` false.
 9. The install record carries the provenance columns — ``source_kind``
    ``git``, a redacted-safe URL, the requested ref, the resolved
    commit, the subdir, the stable ``source_id``, ``fetched_at`` — and
    its ``hash`` equals the tree hash the operator approved.
10. The package's chat tool is actually callable in the running
    process: asked to use it, the agent does, and the call is recorded.
11. The conversation that asked was told the outcome.

Fixture requirement asserted, not assumed: the package must declare no
``allowlist_proposals``. Those merge into ``security_policies`` at
install time and uninstall deliberately does NOT remove them (a one-way
ratchet), so a fixture that proposes any cannot be cleaned up and the
story refuses to install it.

Cleanup: uninstalls the package, removes a leftover install tree,
deletes its own request row, and restores ``config.yaml`` byte for
byte.
"""

from __future__ import annotations

import time
from pathlib import Path

from user_stories.framework import (
    AcceptanceStory,
    CarpenterClient,
    DBInspector,
    StoryResult,
)
from user_stories import package_install_support as support


class InstallPackageFromGitRepo(AcceptanceStory):
    name = "S060 — Install a Capability Package from a Git Repository"
    description = (
        "Person asks in chat; agent mints an approval link and installs "
        "nothing; a human approves; the work queue installs and hot-loads "
        "the package; its chat tool is then callable."
    )
    artifact_prefix = "s060"
    timeout = 1200

    def __init__(self) -> None:
        self._token = ""
        self._installed_name = ""
        self._config: support.ConfigPatch | None = None

    # ------------------------------------------------------------------

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        self.assert_that(db is not None, "DB inspector required")
        env = support.require_env(
            support.ENV_GIT_URL, support.ENV_NAME, support.ENV_TOOL,
        )
        url = env[support.ENV_GIT_URL]
        name = env[support.ENV_NAME]
        tool = env[support.ENV_TOOL]
        subdir = support.optional_env(support.ENV_SUBDIR)
        ref = support.optional_env(support.ENV_REF)
        marker = support.optional_env(support.ENV_SIDE_EFFECT)
        spec = support.git_spec(url, ref=ref, subdir=subdir)

        if support.install_record(db, name) is not None:
            import pytest
            pytest.skip(
                f"package {name!r} is already installed on this deployment; "
                f"this story will not replace a package it did not install. "
                f"Uninstall it first if it is a leftover: "
                f"python3 -m carpenter packages uninstall {name}"
            )

        start_ts = time.time()
        self._config = support.ConfigPatch(client, self.artifact_prefix)
        self._config.allowlist_host(support.host_of(url))
        self._config.apply()

        # ── 1. Ask in chat ──────────────────────────────────────────
        print(f"\n  [1/9] Asking in chat to install {spec}")
        conv_id, reply = client.chat(
            "Please install the capability package from this source: "
            f"{spec}",
            timeout=300,
        )
        print(f"     reply ({len(reply)} chars): {reply[:200]}")

        called = support.tool_names_called(db, conv_id)
        self.assert_that(
            "request_package_install" in called,
            "The agent did not call request_package_install. Either it "
            "chose another route, or the tool was not offered to it "
            "(it is declared always_available=False, so a deployment "
            "with a small context window omits it unless "
            "tool_lists.core_tools_add names it).",
            tools_called=called,
            reply_preview=reply[:400],
            conversation_id=conv_id,
        )
        for forbidden in ("install_package", "uninstall_package"):
            self.assert_that(
                forbidden not in called,
                f"The agent called {forbidden!r}: a chat agent must not "
                f"install a package itself, only ask for one",
                tools_called=called,
            )
        requests = support.tool_calls(db, conv_id, "request_package_install")
        self.assert_that(
            len(requests) == 1,
            f"Expected exactly one request_package_install call, got "
            f"{len(requests)} — the tool's own description says not to "
            f"call it repeatedly, because the operator sees one page per "
            f"call",
            inputs=[r["input_json"] for r in requests],
        )
        input_problems = support.spec_problems(
            requests[0]["input_json"] or "", url, ref=ref, subdir=subdir,
        )
        self.assert_that(
            not input_problems,
            "The recorded tool input is not the source it was given: "
            + "; ".join(input_problems),
            input_json=requests[0]["input_json"],
        )

        # ── 2. The request row ──────────────────────────────────────
        print("  [2/9] Checking the pending_package_installs row")
        token = support.find_approval_token(reply)
        self.assert_that(
            bool(token),
            "No approval URL in the reply. request_package_install "
            "returns its text as a FinalReply precisely so the one-time "
            "link reaches the person verbatim.",
            reply_preview=reply[:600],
        )
        self._token = token
        row = support.request_row(db, token)
        self.assert_that(
            row is not None, "The approval token names no request row",
            token_prefix=token[:8],
        )
        self.assert_that(
            row["status"] == support.STATUS_REQUESTED,
            f"A fresh request should be {support.STATUS_REQUESTED!r}, "
            f"not {row['status']!r}",
        )
        self.assert_that(
            row["conversation_id"] == conv_id,
            f"The request records conversation {row['conversation_id']}, "
            f"but it was made in {conv_id}; the approval page's "
            f"attribution block depends on this",
        )
        self.assert_that(
            len(row["sources"] or ()) == 1,
            f"The request should cover exactly one source, got "
            f"{row['sources']!r}",
        )
        problems = support.spec_problems(
            (row["sources"] or [""])[0], url, ref=ref, subdir=subdir,
        )
        self.assert_that(
            not problems,
            "The recorded source is not the one that was asked for: "
            + "; ".join(problems),
            recorded=row["sources"],
            asked=spec,
        )
        entry = support.preview_entry(row, name)
        self.assert_that(
            bool(entry),
            f"The preview holds no package called {name!r}",
            preview_names=[
                (e.get("manifest") or {}).get("name")
                for e in (row.get("preview") or ())
            ],
        )
        self.assert_that(
            not entry.get("allowlist_proposals"),
            "This fixture proposes security-policy (allowlist) entries. "
            "Those merge into security_policies at install time and "
            "uninstall does NOT remove them (a one-way ratchet), so the "
            "story cannot clean up after itself and refuses to install "
            "it. Use a fixture package that proposes none.",
            allowlist_proposals=entry.get("allowlist_proposals"),
        )
        resolved = str(entry.get("resolved") or "")
        tree_hash = str(entry.get("tree_hash") or "")
        self.assert_that(
            len(resolved) == 40 and all(c in "0123456789abcdef" for c in resolved),
            f"A git source should resolve to a 40-hex commit, got "
            f"{resolved!r}",
        )
        self.assert_that(
            len(tree_hash) == 64,
            f"The preview should carry a sha256 tree hash, got "
            f"{tree_hash!r}",
        )
        print(f"     request {token[:8]} resolved={resolved[:12]} "
              f"tree={tree_hash[:12]}")

        # ── 3. Nothing installed, and no fetched code ran ───────────
        print("  [3/9] Asserting nothing was installed before consent")
        self.assert_that(
            support.install_record(db, name) is None,
            f"{name!r} has an installed_packages row before anyone "
            f"approved the install",
        )
        dest = support.install_dest(name)
        self.assert_that(
            not dest.exists(),
            "The install destination exists before consent",
            destination=str(dest),
        )
        if marker:
            self.assert_that(
                not Path(marker).exists(),
                "The fixture package's side-effect marker already exists, "
                "so either a previous run left it behind or fetched code "
                "ran before the human consented",
                marker=marker,
            )
            print("     side-effect marker absent (strict form) ✓")
        else:
            print(
                "     no CARPENTER_TEST_PKG_SIDE_EFFECT_PATH set: proving "
                "absence of the install only, not of every side effect"
            )

        # ── 4. The approval page, with no credential ────────────────
        print("  [4/9] Opening the approval page with no platform token")
        page = support.get_approval_page_unauthenticated(client, token)
        self.assert_that(
            page.status_code == 200,
            f"The approval page must open from a plain link (a human "
            f"reads it from whatever carried it); got "
            f"{page.status_code}",
            body_preview=page.text[:300],
        )
        body = page.text
        for needle, what in (
            (resolved, "the resolved commit"),
            (tree_hash, "the full tree hash"),
            (tree_hash[:12], "the short tree hash"),
            (name, "the package name"),
            ("Provenance", "the provenance block"),
            ("web_egress_allowlist", "the egress-policy key"),
        ):
            self.assert_contains(body, needle, f"the page must show {what}")
        self.assert_contains(
            body, f"Grants for {name}",
            "the page must show this package's grant boxes",
        )
        caps = entry.get("capabilities") or []
        if caps:
            self.assert_contains(
                body, "PLATFORM-LEVEL TRUST",
                "a package declaring platform capabilities must say so",
            )
            for cap in caps:
                verb = str((cap or {}).get("verb") or "")
                if verb:
                    self.assert_contains(
                        body, verb, "every requested verb must be named",
                    )
            print(f"     page shows {len(caps)} capability verb(s) + grants")
        else:
            self.assert_contains(
                body, "platform_capabilities",
                "a package declaring none must say that explicitly",
            )
            print(
                "     fixture declares no platform_capabilities; page states "
                "that, and the write-tools grant is the only box"
            )
        self.assert_contains(
            body, "not executed",
            "the page must say the package's code has not been run",
        )
        after_get = support.request_row(db, token)
        self.assert_that(
            after_get["status"] == support.STATUS_REQUESTED,
            f"Reading the page changed the row to "
            f"{after_get['status']!r}; a GET must mutate nothing",
        )

        # ── 5. Approve, authenticated ───────────────────────────────
        print("  [5/9] Approving (authenticated POST)")
        decision = support.post_decision(
            client, token, "approve", support.grants_for([name]),
        )
        self.assert_that(
            decision.status_code == 200,
            f"The authenticated approval was refused: "
            f"{decision.status_code} {decision.text[:200]}",
        )
        payload = decision.json()
        self.assert_that(
            payload.get("recorded") is True and payload.get("queued") is True,
            f"The approval should be recorded and queued, got {payload!r}",
        )
        self._installed_name = name

        # ── 6. The work queue installs it ───────────────────────────
        print("  [6/9] Waiting for the work queue to install it")
        final = support.wait_for_request_status(
            db, token,
            (support.STATUS_INSTALLED, support.STATUS_FAILED), timeout=600,
        )
        self.assert_that(
            final["status"] == support.STATUS_INSTALLED,
            f"The install failed: {final.get('error')!r}",
            result=final.get("result"),
        )
        block = support.package_block(final, name)
        self.assert_that(
            block.get("status") == "installed",
            f"result_json reports {name!r} as "
            f"{block.get('status')!r}, not 'installed'",
            block=block,
        )
        load = block.get("load") or {}
        self.assert_that(
            load.get("loaded") is True and load.get("needs_restart") is False,
            f"The package did not hot-load into the running process: "
            f"{load!r}",
        )
        print(f"     installed v{block.get('version')} "
              f"hash {block.get('hash_short')}, loaded live ✓")

        # ── 7. Provenance on the install record ─────────────────────
        print("  [7/9] Checking the provenance columns")
        record = support.install_record(db, name)
        self.assert_that(
            record is not None,
            f"No installed_packages row for {name!r} after a successful "
            f"install",
        )
        for column in support.PROVENANCE_COLUMNS:
            self.assert_that(
                record.get(column) is not None,
                f"Provenance column {column!r} is NULL — a remote install "
                f"must record where its bytes came from",
                record={k: record.get(k) for k in support.PROVENANCE_COLUMNS},
            )
        self.assert_that(
            record["source_kind"] == "git",
            f"source_kind should be 'git', got {record['source_kind']!r}",
        )
        self.assert_that(
            record["source_resolved"] == resolved,
            f"source_resolved {record['source_resolved']!r} does not match "
            f"the approved commit {resolved!r}",
        )
        self.assert_that(
            record["source_id"] == entry.get("source_id"),
            f"source_id {record['source_id']!r} does not match the "
            f"preview's {entry.get('source_id')!r}",
        )
        self.assert_that(
            (record["source_ref"] or "") == ref,
            f"source_ref should be the requested ref {ref!r}, got "
            f"{record['source_ref']!r}",
        )
        self.assert_that(
            (record["source_subdir"] or "") == subdir,
            f"source_subdir should be {subdir!r}, got "
            f"{record['source_subdir']!r}",
        )
        self.assert_that(
            record["hash"] == tree_hash,
            "The installed hash is not the tree hash the operator "
            "approved — the bytes installed are not the bytes previewed",
            approved=tree_hash, installed=record["hash"],
        )
        self.assert_that(
            bool(record.get("install_path")),
            "install_path is empty on the install record",
        )
        if marker:
            self.assert_that(
                Path(marker).exists(),
                "The fixture package's side-effect marker is still absent "
                "after the install, so the package's own code never ran "
                "even once",
                marker=marker,
            )
            print("     side-effect marker appeared only after consent ✓")

        # ── 8. The conversation was told ────────────────────────────
        print("  [8/9] Checking the outcome landed in the conversation")
        outcome = [
            m for m in support.system_messages(db, conv_id)
            if "Package install" in (m.get("content") or "")
        ]
        self.assert_that(
            bool(outcome),
            "The conversation that asked was never told the outcome",
            system_messages=[
                (m.get("content") or "")[:120]
                for m in support.system_messages(db, conv_id)
            ],
        )
        self.assert_contains(
            outcome[-1]["content"], name,
            "the outcome message must name the package",
        )

        # ── 9. The package's chat tool is callable ──────────────────
        print(f"  [9/9] Asking the agent to use {tool}")
        _, tool_reply = client.chat(
            f"The {name} package is installed now. Please call its "
            f"{tool} tool and tell me exactly what it returned.",
            conversation_id=conv_id, timeout=300,
        )
        print(f"     reply: {tool_reply[:200]}")
        self.assert_that(
            tool in support.tool_names_called(db, conv_id),
            f"The package's chat tool {tool!r} was never called, so the "
            f"hot-load did not make it reachable in the running process",
            tools_called=support.tool_names_called(db, conv_id),
            reply_preview=tool_reply[:400],
        )

        elapsed = time.time() - start_ts
        return self.result(
            f"{name} v{block.get('version')} requested in chat, approved on "
            f"a one-time link, installed by the work queue, hot-loaded and "
            f"callable in {elapsed:.0f}s"
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
