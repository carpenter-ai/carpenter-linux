"""
S064 — Install from an Archive URL, Pinned by sha256

The second supported remote source kind: a release archive at an
``https://`` URL, pinned with ``#sha256=<64 hex>``. The digest is the
whole integrity story for an archive — there is no commit to anchor to
— so this story proves it is enforced before it proves it works: a
wrong digest is refused with nothing written and nothing extracted, and
only the correct digest installs.

Preconditions (environment variables; the story SKIPS when absent)
------------------------------------------------------------------

  CARPENTER_TEST_PKG_ARCHIVE_URL
      A ``.tar.gz`` (``.tgz`` / ``.tar``) archive of one capability
      package, reachable without a credential.
  CARPENTER_TEST_PKG_ARCHIVE_SHA256
      The correct sha256 of those bytes, 64 lowercase hex characters.
  CARPENTER_TEST_PKG_ARCHIVE_NAME
      The manifest name of the package inside the archive.

What this story asserts (STRICT)
--------------------------------

1. Asked to install the archive with a wrong ``#sha256=`` — the real
   digest with one hex digit flipped, so it is well-formed and fails
   the digest check rather than spec parsing — the request is REFUSED:
   the ``request_package_install`` tool's recorded result says so and
   names the sha256 check.
2. That refusal left nothing behind: no ``pending_package_installs``
   row, so no approval page can exist for it; no new entry in the fetch
   quarantine; nothing installed. There is no operator checkbox for a
   tree that failed its digest, because the request never reaches a
   page.
3. With the correct digest, the request is made, previewed as an
   ``archive`` source whose ``resolved`` value IS the sha256, and the
   approval page shows that digest.
4. One authenticated approval installs it, and the install record
   carries ``source_kind`` ``archive``, the URL, the digest in
   ``source_resolved``, and a ``hash`` equal to the approved tree hash.
5. ``source_ref`` is empty for an archive: there is no ref to follow,
   and the pin is the digest.

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


class InstallArchivePinnedBySha256(AcceptanceStory):
    name = "S064 — Install from an Archive URL Pinned by sha256"
    description = (
        "A wrong #sha256= is refused with nothing written; the correct "
        "digest installs, and the digest is recorded as the provenance "
        "anchor."
    )
    artifact_prefix = "s064"
    timeout = 1200

    def __init__(self) -> None:
        self._token = ""
        self._installed_name = ""
        self._config: support.ConfigPatch | None = None

    # ------------------------------------------------------------------

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        self.assert_that(db is not None, "DB inspector required")
        env = support.require_env(
            support.ENV_ARCHIVE_URL,
            support.ENV_ARCHIVE_SHA256,
            support.ENV_ARCHIVE_NAME,
        )
        url = env[support.ENV_ARCHIVE_URL]
        digest = env[support.ENV_ARCHIVE_SHA256].lower()
        name = env[support.ENV_ARCHIVE_NAME]
        self.assert_that(
            len(digest) == 64 and all(c in "0123456789abcdef" for c in digest),
            f"{support.ENV_ARCHIVE_SHA256} must be 64 lowercase hex "
            f"characters, got {digest!r}",
        )
        good_spec = support.archive_spec(url, digest)
        bad_spec = support.archive_spec(url, support.corrupt_digest(digest))

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

        rows_before = support.count_request_rows(db)
        staging_before = support.staging_children()

        # ── 1. The wrong digest is refused ──────────────────────────
        print(f"\n  [1/5] Asking to install with a WRONG sha256")
        conv_id, reply = client.chat(
            "Please install the capability package from this archive, "
            f"exactly as written: {bad_spec}\n"
            "Do not change the source or try it without the digest — if "
            "it is refused, just tell me what the refusal said.",
            timeout=300,
        )
        print(f"     reply ({len(reply)} chars): {reply[:240]}")
        attempts = support.tool_calls(db, conv_id, "request_package_install")
        self.assert_that(
            len(attempts) >= 1,
            "The agent never tried: without a request_package_install "
            "call there is no refusal to assert on",
            tools_called=support.tool_names_called(db, conv_id),
            reply_preview=reply[:400],
        )
        refusal = attempts[-1]["result_text"] or ""
        self.assert_contains(
            refusal, "refused",
            "the tool's result must say the request was refused",
        )
        self.assert_contains(
            refusal, "sha256",
            "the refusal must name the sha256 check, so the person is "
            "told which pin failed",
        )
        print(f"     refusal, verbatim: {refusal[:240]}")

        # ── 2. The refusal left nothing behind ──────────────────────
        print("  [2/5] Asserting the refused request wrote nothing")
        self.assert_that(
            support.count_request_rows(db) == rows_before,
            f"The refused request wrote "
            f"{support.count_request_rows(db) - rows_before} "
            f"pending_package_installs row(s). A tree that fails its "
            f"digest must never reach an approval page: there is no "
            f"checkbox for it.",
        )
        self.assert_that(
            not support.find_approval_token(reply),
            "An approval URL came back for a refused request",
            reply_preview=reply[:400],
        )
        staging_after_refusal = support.staging_children()
        self.assert_that(
            staging_after_refusal == staging_before,
            "The refused fetch left something in the quarantine",
            before=staging_before, after=staging_after_refusal,
        )
        self.assert_that(
            support.install_record(db, name) is None,
            f"{name!r} was installed from an archive that failed its "
            f"digest check",
        )

        # ── 3. The correct digest is requested and previewed ────────
        print("  [3/5] Asking again with the correct sha256")
        conv_id2, reply2 = client.chat(
            "That digest was wrong, sorry. Please install this one "
            f"instead: {good_spec}",
            timeout=300,
        )
        print(f"     reply ({len(reply2)} chars): {reply2[:240]}")
        token = support.find_approval_token(reply2)
        self.assert_that(
            bool(token),
            "No approval URL came back for the correctly pinned archive",
            tools_called=support.tool_names_called(db, conv_id2),
            reply_preview=reply2[:600],
        )
        self._token = token
        row = support.request_row(db, token)
        self.assert_that(
            row is not None and row["status"] == support.STATUS_REQUESTED,
            f"Expected a 'requested' row, got "
            f"{row['status'] if row else 'none'!r}",
        )
        entry = support.preview_entry(row, name)
        self.assert_that(
            bool(entry),
            f"The preview holds no package named {name!r}",
            preview_names=[
                (e.get("manifest") or {}).get("name")
                for e in (row.get("preview") or ())
            ],
        )
        self.assert_that(
            entry.get("kind") == "archive",
            f"The source should be previewed as an archive, got "
            f"{entry.get('kind')!r}",
        )
        self.assert_that(
            (entry.get("resolved") or "").lower() == digest,
            f"An archive's resolved value is its sha256; expected "
            f"{digest!r}, got {entry.get('resolved')!r}",
        )
        self.assert_that(
            not entry.get("allowlist_proposals"),
            "This fixture proposes security-policy entries, which "
            "uninstall cannot remove (one-way ratchet); use a fixture "
            "that proposes none",
            allowlist_proposals=entry.get("allowlist_proposals"),
        )
        page = support.get_approval_page_unauthenticated(client, token)
        self.assert_that(
            page.status_code == 200,
            f"The approval page did not open: {page.status_code}",
        )
        self.assert_contains(
            page.text, digest,
            "the page must show the digest the bytes were pinned to",
        )

        # ── 4. Approve, and it installs ─────────────────────────────
        print("  [4/5] Approving and waiting for the install")
        decision = support.post_decision(
            client, token, "approve", support.grants_for([name]),
        )
        self.assert_that(
            decision.status_code == 200,
            f"The approval was refused: {decision.status_code} "
            f"{decision.text[:200]}",
        )
        self._installed_name = name
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
            f"result_json reports {name!r} as {block.get('status')!r}",
            block=block,
        )

        # ── 5. The digest is the recorded provenance anchor ─────────
        print("  [5/5] Checking the archive provenance")
        record = support.install_record(db, name)
        self.assert_that(
            record is not None,
            f"No installed_packages row for {name!r}",
        )
        self.assert_that(
            record["source_kind"] == "archive",
            f"source_kind should be 'archive', got "
            f"{record['source_kind']!r}",
        )
        self.assert_that(
            (record["source_resolved"] or "").lower() == digest,
            f"source_resolved should be the sha256 {digest!r}, got "
            f"{record['source_resolved']!r}",
        )
        self.assert_that(
            (record["source_ref"] or "") == "",
            f"An archive has no ref to follow, so source_ref should be "
            f"empty; got {record['source_ref']!r}",
        )
        self.assert_that(
            bool(record["source_url"]) and "***" not in record["source_url"],
            f"source_url should be a usable, credential-free URL, got "
            f"{record['source_url']!r}",
        )
        self.assert_that(
            record["hash"] == entry.get("tree_hash"),
            "The installed hash is not the tree hash that was approved",
            approved=entry.get("tree_hash"), installed=record["hash"],
        )

        elapsed = time.time() - start_ts
        return self.result(
            f"a wrong #sha256= was refused with nothing written; the "
            f"correct digest installed {name} v{block.get('version')} and "
            f"was recorded as its provenance anchor ({elapsed:.0f}s)"
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
