"""
S061 — One Approval, Two Packages from Two Different Repositories

A person asks for two capability packages at once, held in two
*different* remote repositories. The agent must put a single page in
front of the human covering both — not two pages, and not one page per
source — and a single approval must install both, with the outcome
reported per package.

This is the batch half of the install design: packages install in
request order, each in its own transaction, and the first failure stops
the batch and compensates (fresh installs are rolled back, upgrades are
left alone) rather than leaving a silent half-state. This story
exercises the success path and asserts the per-package reporting that
the failure path depends on.

Preconditions (environment variables; the story SKIPS when absent)
------------------------------------------------------------------

  CARPENTER_TEST_PKG_GIT_URL / CARPENTER_TEST_PKG_NAME
      The first fixture repository and the manifest name of the package
      in it. Must need no read credential.
  CARPENTER_TEST_PKG2_GIT_URL / CARPENTER_TEST_PKG2_NAME
      A *second*, different repository and its package. The story
      asserts the two sources get different ``source_id`` values, which
      is the point of using two repositories rather than two
      directories of one.
  CARPENTER_TEST_PKG_SUBDIR, CARPENTER_TEST_PKG_REF       (optional)
  CARPENTER_TEST_PKG2_SUBDIR, CARPENTER_TEST_PKG2_REF     (optional)
      Package subdirectory and ref for each, as in s060.

What this story asserts (STRICT)
--------------------------------

1. The agent called ``request_package_install`` exactly ONCE, with both
   specs in one ``sources`` list.
2. One ``pending_package_installs`` row covers both packages, and its
   preview has one entry per package with the two source specs
   recorded verbatim in request order.
3. The single approval page lists both packages, with a provenance
   block (resolved commit and tree hash) and a grant section for each.
4. One authenticated ``POST .../decide`` approves the whole set.
5. Both packages install: the row reaches ``installed`` and
   ``result_json`` carries a block per package, each ``installed``,
   each with its own version, hash and live-load report.
6. Both ``installed_packages`` rows exist with provenance recorded, and
   their ``source_id`` values differ — two remotes are two sources even
   when they publish similar packages.
7. The conversation is told the outcome once, with a line naming each
   package.

Fixture requirement asserted, not assumed: neither package may declare
``allowlist_proposals`` (uninstall cannot remove those), and the two
manifests must declare different names — core refuses a batch that
would install the same name twice, which would make this story a test
of that refusal instead.

Cleanup: uninstalls both packages, deletes the request row, restores
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


class InstallTwoPackagesOneApproval(AcceptanceStory):
    name = "S061 — One Approval, Two Packages, Two Repositories"
    description = (
        "A single chat request covering two remote repositories produces "
        "one approval page; one approval installs both and reports each "
        "package's outcome separately."
    )
    artifact_prefix = "s061"
    timeout = 1500

    def __init__(self) -> None:
        self._token = ""
        self._installed: list[str] = []
        self._config: support.ConfigPatch | None = None

    # ------------------------------------------------------------------

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        self.assert_that(db is not None, "DB inspector required")
        env = support.require_env(
            support.ENV_GIT_URL, support.ENV_NAME,
            support.ENV_GIT_URL2, support.ENV_NAME2,
        )
        url_a = env[support.ENV_GIT_URL]
        name_a = env[support.ENV_NAME]
        url_b = env[support.ENV_GIT_URL2]
        name_b = env[support.ENV_NAME2]
        spec_a = support.git_spec(
            url_a,
            ref=support.optional_env(support.ENV_REF),
            subdir=support.optional_env(support.ENV_SUBDIR),
        )
        spec_b = support.git_spec(
            url_b,
            ref=support.optional_env(support.ENV_REF2),
            subdir=support.optional_env(support.ENV_SUBDIR2),
        )

        self.assert_that(
            name_a != name_b,
            "The two fixture packages must have different manifest names: "
            "core refuses a batch that would install one name twice, so "
            "equal names would make this a test of that refusal",
        )
        for name in (name_a, name_b):
            if support.install_record(db, name) is not None:
                import pytest
                pytest.skip(
                    f"package {name!r} is already installed on this "
                    f"deployment; this story will not replace a package it "
                    f"did not install"
                )

        start_ts = time.time()
        self._config = support.ConfigPatch(client, self.artifact_prefix)
        self._config.allowlist_host(support.host_of(url_a))
        self._config.allowlist_host(support.host_of(url_b))
        self._config.apply()

        # ── 1. One chat request for both ────────────────────────────
        print(f"\n  [1/6] Asking for both packages in one message")
        conv_id, reply = client.chat(
            "Please install these two capability packages for me:\n"
            f"1. {spec_a}\n2. {spec_b}\n"
            "One approval for both is fine.",
            timeout=420,
        )
        print(f"     reply ({len(reply)} chars): {reply[:200]}")

        requests = support.tool_calls(db, conv_id, "request_package_install")
        self.assert_that(
            len(requests) == 1,
            f"Expected ONE request covering both sources, got "
            f"{len(requests)} call(s): a second call would put a second "
            f"page in front of the operator for the same ask",
            inputs=[r["input_json"] for r in requests],
            tools_called=support.tool_names_called(db, conv_id),
            reply_preview=reply[:400],
        )
        recorded_input = requests[0]["input_json"] or ""
        for url, ref_var, subdir_var in (
            (url_a, support.ENV_REF, support.ENV_SUBDIR),
            (url_b, support.ENV_REF2, support.ENV_SUBDIR2),
        ):
            problems = support.spec_problems(
                recorded_input, url,
                ref=support.optional_env(ref_var),
                subdir=support.optional_env(subdir_var),
            )
            self.assert_that(
                not problems,
                "The one tool call does not cover both sources: "
                + "; ".join(problems),
                input_json=recorded_input,
            )

        # ── 2. One row, two previews ────────────────────────────────
        print("  [2/6] Checking the single request row")
        token = support.find_approval_token(reply)
        self.assert_that(
            bool(token), "No approval URL in the reply",
            reply_preview=reply[:600],
        )
        self._token = token
        row = support.request_row(db, token)
        self.assert_that(row is not None, "The token names no request row")
        self.assert_that(
            row["status"] == support.STATUS_REQUESTED,
            f"Expected status 'requested', got {row['status']!r}",
        )
        recorded_sources = list(row["sources"] or ())
        self.assert_that(
            len(recorded_sources) == 2,
            f"One request should cover two sources, got "
            f"{recorded_sources!r}",
        )
        for index, (url, ref_var, subdir_var) in enumerate((
            (url_a, support.ENV_REF, support.ENV_SUBDIR),
            (url_b, support.ENV_REF2, support.ENV_SUBDIR2),
        )):
            problems = support.spec_problems(
                recorded_sources[index], url,
                ref=support.optional_env(ref_var),
                subdir=support.optional_env(subdir_var),
            )
            self.assert_that(
                not problems,
                f"Recorded source {index} is not the one asked for "
                f"(request order matters: packages install in it): "
                + "; ".join(problems),
                recorded=recorded_sources,
            )
        self.assert_that(
            len(row["preview"] or ()) == 2,
            f"The preview should describe two packages, got "
            f"{len(row['preview'] or ())}",
        )
        entry_a = support.preview_entry(row, name_a)
        entry_b = support.preview_entry(row, name_b)
        for name, entry in ((name_a, entry_a), (name_b, entry_b)):
            self.assert_that(
                bool(entry), f"The preview holds no package named {name!r}",
                preview_names=[
                    (e.get("manifest") or {}).get("name")
                    for e in (row.get("preview") or ())
                ],
            )
            self.assert_that(
                not entry.get("allowlist_proposals"),
                f"{name!r} proposes security-policy entries, which "
                f"uninstall cannot remove (one-way ratchet); use a "
                f"fixture that proposes none",
                allowlist_proposals=entry.get("allowlist_proposals"),
            )
        self.assert_that(
            entry_a.get("source_id") != entry_b.get("source_id"),
            "The two sources share a source_id, so they are not two "
            "different remotes as this story requires",
            source_ids=[entry_a.get("source_id"), entry_b.get("source_id")],
        )
        print(f"     one request {token[:8]} covering {name_a} + {name_b}")

        self.assert_that(
            support.install_record(db, name_a) is None
            and support.install_record(db, name_b) is None,
            "A package was installed before anyone approved the request",
        )

        # ── 3. One page, both packages ──────────────────────────────
        print("  [3/6] Opening the one approval page")
        page = support.get_approval_page_unauthenticated(client, token)
        self.assert_that(
            page.status_code == 200,
            f"The approval page did not open: {page.status_code}",
            body_preview=page.text[:300],
        )
        body = page.text
        self.assert_contains(
            body, "2 package(s)", "the page must say it covers two packages",
        )
        for name, entry in ((name_a, entry_a), (name_b, entry_b)):
            self.assert_contains(body, name, f"{name} must be listed")
            self.assert_contains(
                body, str(entry.get("resolved") or ""),
                f"{name}'s resolved commit must be shown",
            )
            self.assert_contains(
                body, str(entry.get("tree_hash") or ""),
                f"{name}'s tree hash must be shown",
            )
            self.assert_contains(
                body, f"Grants for {name}",
                f"{name} must have its own grant section",
            )

        # ── 4. One approval ─────────────────────────────────────────
        print("  [4/6] Approving both with one authenticated POST")
        decision = support.post_decision(
            client, token, "approve",
            support.grants_for([name_a, name_b]),
        )
        self.assert_that(
            decision.status_code == 200,
            f"The approval was refused: {decision.status_code} "
            f"{decision.text[:200]}",
        )
        self.assert_that(
            decision.json().get("queued") is True,
            f"The batch install was not queued: {decision.json()!r}",
        )
        self._installed = [name_a, name_b]

        # ── 5. Both install, reported per package ───────────────────
        print("  [5/6] Waiting for the batch to install")
        final = support.wait_for_request_status(
            db, token,
            (support.STATUS_INSTALLED, support.STATUS_FAILED), timeout=900,
        )
        self.assert_that(
            final["status"] == support.STATUS_INSTALLED,
            f"The batch failed: {final.get('error')!r}",
            result=final.get("result"),
        )
        blocks = (final.get("result") or {}).get("packages") or []
        self.assert_that(
            len(blocks) == 2,
            f"result_json should report two packages, got {len(blocks)}",
            result=final.get("result"),
        )
        self.assert_that(
            [b.get("name") for b in blocks] == [name_a, name_b],
            f"The per-package report should follow request order, got "
            f"{[b.get('name') for b in blocks]!r}",
        )
        for block in blocks:
            self.assert_that(
                block.get("status") == "installed",
                f"{block.get('name')!r} reports "
                f"{block.get('status')!r}, not 'installed'",
                block=block,
            )
            self.assert_that(
                bool(block.get("version")) and bool(block.get("hash")),
                f"{block.get('name')!r} has no version/hash in its report",
                block=block,
            )
            load = block.get("load") or {}
            self.assert_that(
                load.get("loaded") is True
                and load.get("needs_restart") is False,
                f"{block.get('name')!r} did not hot-load: {load!r}",
            )
        self.assert_that(
            not (final.get("result") or {}).get("rolled_back"),
            f"Something was rolled back in a successful batch: "
            f"{(final.get('result') or {}).get('rolled_back')!r}",
        )
        print(
            "     both installed: "
            + ", ".join(
                f"{b.get('name')} v{b.get('version')}" for b in blocks
            )
        )

        # ── 6. Two install records, two sources ─────────────────────
        print("  [6/6] Checking both install records")
        rec_a = support.install_record(db, name_a)
        rec_b = support.install_record(db, name_b)
        for name, record, entry in (
            (name_a, rec_a, entry_a), (name_b, rec_b, entry_b),
        ):
            self.assert_that(
                record is not None,
                f"No installed_packages row for {name!r}",
            )
            self.assert_that(
                record["source_kind"] == "git"
                and record["source_resolved"] == entry.get("resolved")
                and record["source_id"] == entry.get("source_id"),
                f"{name!r}'s provenance does not match what was approved",
                record={
                    k: record.get(k) for k in support.PROVENANCE_COLUMNS
                },
                approved={
                    "resolved": entry.get("resolved"),
                    "source_id": entry.get("source_id"),
                },
            )
            self.assert_that(
                record["hash"] == entry.get("tree_hash"),
                f"{name!r} installed bytes other than the approved tree",
                approved=entry.get("tree_hash"), installed=record["hash"],
            )
        self.assert_that(
            rec_a["source_id"] != rec_b["source_id"],
            "Both install records share a source_id",
        )

        outcome = [
            m for m in support.system_messages(db, conv_id)
            if "Package install" in (m.get("content") or "")
        ]
        self.assert_that(
            len(outcome) == 1,
            f"One approval should produce one outcome message, got "
            f"{len(outcome)}",
            messages=[(m.get("content") or "")[:120] for m in outcome],
        )
        for name in (name_a, name_b):
            self.assert_contains(
                outcome[0]["content"], name,
                "the outcome message must report each package",
            )

        elapsed = time.time() - start_ts
        return self.result(
            f"{name_a} and {name_b} installed from two repositories on one "
            f"approval in {elapsed:.0f}s, each reported separately"
        )

    # ------------------------------------------------------------------

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        for name in self._installed:
            support.uninstall_quietly(name)
            support.remove_install_tree(name)
            if db is not None and support.install_record(db, name) is not None:
                support.delete_install_record(db, name)
                print(f"  [cleanup] removed the install row for {name}")
        if db is not None and self._token:
            removed = support.delete_request_rows(db, [self._token])
            print(f"  [cleanup] removed {removed} request row(s)")
        if self._config is not None:
            try:
                self._config.restore()
            except Exception as exc:  # noqa: BLE001 — report, never mask
                print(f"  ! CONFIG NOT RESTORED: {exc}")
