"""
S065 — Upgrade an Installed Package and Keep an Operator's Edit

A capability package is installed from a remote source at an older
revision. An operator then edits two files in the installed tree, the
way an operator does: one file upstream has not touched since, and one
upstream has changed in the newer revision. Upgrading must keep the
first edit and must refuse the second until someone says, explicitly,
that the new version wins there.

The upgrade verb is
``python3 -m carpenter packages update <name> [--ref <ref>]`` and it is
**operator-only by design**: there is no chat tool, no HTTP
route and no arc that performs it, because an upgrade can overwrite an
operator's own edits and the person who made them is the only one who
can weigh that. So this story shells out to the CLI rather than asking
in chat — and asserts the output a person would read, including the
exit codes, because that output *is* the interface.

Preconditions (environment variables; the story SKIPS when absent)
------------------------------------------------------------------

  CARPENTER_TEST_PKG_GIT_URL / CARPENTER_TEST_PKG_NAME
      The fixture repository and the manifest name of its package.
      Must need no read credential.
  CARPENTER_TEST_PKG_OLD_REF
      A ref (tag or commit) holding an OLDER version of the package.
      The story installs this one.
  CARPENTER_TEST_PKG_REF
      A ref carrying a NEWER version of the same package. Required
      here, unlike in s060: it is what ``--ref`` is pointed at.
  CARPENTER_TEST_PKG_EDIT_FILE
      Path, relative to the package root, of a file that is byte-identical
      in both refs and where an appended comment line is harmless. The
      operator's edit to this file must survive the upgrade.
  CARPENTER_TEST_PKG_CONFLICT_FILE
      Path, relative to the package root, of a file that DIFFERS between
      the two refs. Edited locally as well, it becomes a conflict.
  CARPENTER_TEST_PKG_SUBDIR  (optional)
      Package subdirectory inside the repository.

What the fixture has to provide
-------------------------------

Nothing here is simulated. An update follows a ref, so for a package
installed at one ref to have anything to upgrade *to*, a newer version
of it must exist at another ref — that is a fixture prerequisite, and
both refs are required environment variables. The story moves between
them the way an operator moves a package from one tag to the next:
``packages update <name> --ref <ref>``, which fetches that ref and then
REMEMBERS it as the package's recorded ``source_ref``, so later updates
follow it too. The story writes nothing to the database itself.

Everything else is the real thing as well: the real fetch, the real
three-way merge against the real pristine archive of the installed
version, the real conflict refusal, the real on-disk result.

What this story asserts (STRICT)
--------------------------------

1. The older version installs through the normal two-phase chat flow,
   recording ``source_ref`` as the older ref.
2. Both fixture files exist in the installed tree, and the operator's
   edits land in them.
3. ``packages update <name> --ref <newer> --yes`` REFUSES: exit code 1,
   an error naming the conflicting file and saying nothing was changed.
   The plan it printed names the ref it was given — the ``--ref`` line
   that says the ref will be recorded when the update is applied — and
   says ``Strategy : reconcile``, a three-way merge against the pristine
   tree of the installed version, so the refusal is the conflict gate
   and not the fallback that replaces the tree wholesale.
4. The refusal changed nothing: the installed version, hash, recorded
   ref and both files on disk are exactly as they were. In particular
   the ref is NOT recorded by a run that refused to apply anything.
5. The same command with ``--accept-conflicts`` then succeeds: exit code
   0, and its report says the operator's edit was KEPT in the untouched
   file and DISCARDED in the conflicting one.
6. On disk, that is true: the operator's marker is still in the file
   upstream did not change, and gone from the file it did.
7. The install record moved to the new version, with ``source_resolved``
   now a different commit and ``shipped_hash`` recorded — so the next
   upgrade has a pristine anchor to merge against.
8. ``source_ref`` is now the newer ref. That is the operator-visible
   half of ``--ref``: the package goes on being followed from where it
   was pointed, rather than snapping back to the ref it was installed
   at. (Asserted on the recorded ref rather than by a third fetch: a
   plain update afterwards would re-plan against the merged tree, which
   is a different property from this one.)

Cleanup: uninstalls the package, removes a leftover tree, deletes its
request row, restores ``config.yaml`` byte for byte.
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


class UpdatePackageKeepsOperatorEdit(AcceptanceStory):
    name = "S065 — Upgrade a Package and Keep an Operator's Edit"
    description = (
        "An older version is installed, edited locally, then upgraded: "
        "the untouched file's edit survives, and a conflicting file is "
        "refused until --accept-conflicts is given."
    )
    artifact_prefix = "s065"
    timeout = 1500

    def __init__(self) -> None:
        self._token = ""
        self._installed_name = ""
        self._config: support.ConfigPatch | None = None

    # ------------------------------------------------------------------

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        self.assert_that(db is not None, "DB inspector required")
        env = support.require_env(
            support.ENV_GIT_URL, support.ENV_NAME,
            support.ENV_OLD_REF, support.ENV_REF,
            support.ENV_EDIT_FILE, support.ENV_CONFLICT_FILE,
        )
        url = env[support.ENV_GIT_URL]
        name = env[support.ENV_NAME]
        old_ref = env[support.ENV_OLD_REF]
        new_ref = env[support.ENV_REF]
        edit_rel = env[support.ENV_EDIT_FILE]
        conflict_rel = env[support.ENV_CONFLICT_FILE]
        subdir = support.optional_env(support.ENV_SUBDIR)
        self.assert_that(
            old_ref != new_ref,
            f"{support.ENV_OLD_REF} and {support.ENV_REF} are the same "
            f"ref ({old_ref!r}); there would be nothing to upgrade to",
        )
        self.assert_that(
            edit_rel != conflict_rel,
            f"{support.ENV_EDIT_FILE} and {support.ENV_CONFLICT_FILE} "
            f"name the same file ({edit_rel!r})",
        )
        old_spec = support.git_spec(url, ref=old_ref, subdir=subdir)

        if support.install_record(db, name) is not None:
            import pytest
            pytest.skip(
                f"package {name!r} is already installed on this "
                f"deployment; this story will not replace a package it "
                f"did not install"
            )
        if not support.cli_is_available():
            import pytest
            pytest.skip(
                "`python3 -m carpenter packages list` does not run under "
                "this story's interpreter, so the upgrade verb cannot be "
                "exercised — and its exit code 1 would be "
                "indistinguishable from the conflict refusal this story "
                "asserts. Run the suite with the interpreter that can "
                "import carpenter."
            )

        start_ts = time.time()
        marker = f"# {self.artifact_name('operator-edit')}"
        self._config = support.ConfigPatch(client, self.artifact_prefix)
        self._config.allowlist_host(support.host_of(url))
        self._config.apply()

        # ── 1. Install the older version through the chat flow ──────
        print(f"\n  [1/6] Installing {name} at {old_ref} over chat")
        conv_id, reply = client.chat(
            "Please install the capability package at this exact "
            f"revision: {old_spec}",
            timeout=300,
        )
        token = support.find_approval_token(reply)
        self.assert_that(
            bool(token),
            "No approval URL came back for the older revision",
            tools_called=support.tool_names_called(db, conv_id),
            reply_preview=reply[:600],
        )
        self._token = token
        decision = support.post_decision(
            client, token, "approve", support.grants_for([name]),
        )
        self.assert_that(
            decision.status_code == 200,
            f"The approval was refused: {decision.status_code} "
            f"{decision.text[:200]}",
        )
        self._installed_name = name
        installed = support.wait_for_request_status(
            db, token,
            (support.STATUS_INSTALLED, support.STATUS_FAILED), timeout=600,
        )
        self.assert_that(
            installed["status"] == support.STATUS_INSTALLED,
            f"Installing the older revision failed: "
            f"{installed.get('error')!r}",
            result=installed.get("result"),
        )
        before = support.install_record(db, name)
        self.assert_that(
            (before["source_ref"] or "") == old_ref,
            f"The install should record the older ref {old_ref!r}, got "
            f"{before['source_ref']!r}",
        )
        old_version = before["version"]
        old_hash = before["hash"]
        old_resolved = before["source_resolved"]
        tree = Path(before["install_path"])
        print(f"     installed v{old_version} at {tree}")

        # ── 2. The operator edits two files ─────────────────────────
        print("  [2/6] Making the operator's edits")
        edit_path = tree / edit_rel
        conflict_path = tree / conflict_rel
        for rel, path in ((edit_rel, edit_path), (conflict_rel, conflict_path)):
            self.assert_that(
                path.is_file(),
                f"{rel!r} is not a file in the installed tree; the "
                f"fixture variables must name paths relative to the "
                f"package root",
                install_path=str(tree),
            )
        for path in (edit_path, conflict_path):
            original = path.read_text(encoding="utf-8")
            suffix = "" if original.endswith("\n") else "\n"
            path.write_text(
                f"{original}{suffix}{marker}\n", encoding="utf-8",
            )
        self.assert_that(
            marker in edit_path.read_text(encoding="utf-8")
            and marker in conflict_path.read_text(encoding="utf-8"),
            "The operator's edits did not land on disk",
        )
        print(f"     appended {marker!r} to both files")

        # ── 3. The upgrade refuses the conflict ─────────────────────
        print(
            f"  [3/6] Running `packages update --ref {new_ref}` — "
            f"expecting a refusal"
        )
        refused = support.run_packages_cli(
            "update", name, "--ref", new_ref, "--yes",
        )
        plan_and_error = (refused.stderr or "") + (refused.stdout or "")
        self.assert_that(
            refused.returncode == 1,
            f"The upgrade should refuse a conflicting file with exit code "
            f"1, got {refused.returncode}",
            stdout=(refused.stdout or "")[-600:],
            stderr=(refused.stderr or "")[-600:],
        )
        self.assert_contains(
            plan_and_error, "Strategy : reconcile",
            "the plan must be a three-way merge against the pristine tree "
            "of the installed version; a 'clobber' plan here would mean "
            "the pristine archive was missing and local edits were never "
            "going to survive",
        )
        ref_lines = [
            line for line in plan_and_error.splitlines() if "--ref;" in line
        ]
        self.assert_that(
            len(ref_lines) == 1 and new_ref in ref_lines[0],
            f"The plan must name the ref it was given, and say it will be "
            f"recorded when the update is applied; got {ref_lines!r}",
            plan=plan_and_error[:800],
        )
        self.assert_contains(
            plan_and_error, "CONFLICTS",
            "the plan must name the conflict block",
        )
        self.assert_contains(
            plan_and_error, conflict_rel,
            "the refusal must name the conflicting file",
        )
        self.assert_contains(
            plan_and_error, "Nothing was changed",
            "the refusal must say that nothing was changed",
        )
        self.assert_contains(
            plan_and_error, "--accept-conflicts",
            "the refusal must name the flag that accepts the new version",
        )
        print(f"     refused: {(refused.stderr or '').strip()[-220:]}")

        # ── 4. The refusal changed nothing ──────────────────────────
        print("  [4/6] Asserting the refusal changed nothing")
        mid = support.install_record(db, name)
        self.assert_that(
            mid["version"] == old_version and mid["hash"] == old_hash,
            f"The refused upgrade changed the install record: "
            f"{old_version}/{old_hash[:12]} -> "
            f"{mid['version']}/{mid['hash'][:12]}",
        )
        self.assert_that(
            (mid["source_ref"] or "") == old_ref,
            f"The refused upgrade recorded the new ref anyway "
            f"({mid['source_ref']!r}): a run that applied nothing must "
            f"not move where the package is followed from",
        )
        self.assert_that(
            marker in edit_path.read_text(encoding="utf-8")
            and marker in conflict_path.read_text(encoding="utf-8"),
            "The refused upgrade touched the operator's files",
        )

        # ── 5. Accepting conflicts applies the merge ────────────────
        print("  [5/6] Running the same command with --accept-conflicts")
        applied = support.run_packages_cli(
            "update", name, "--ref", new_ref, "--yes", "--accept-conflicts",
        )
        report = (applied.stdout or "") + (applied.stderr or "")
        self.assert_that(
            applied.returncode == 0,
            f"The accepted upgrade failed with exit code "
            f"{applied.returncode}",
            stdout=(applied.stdout or "")[-800:],
            stderr=(applied.stderr or "")[-800:],
        )
        self.assert_contains(
            report, f"Updated {name}",
            "the report must say what it updated",
        )
        self.assert_contains(
            report, "Your edits were KEPT in:",
            "the report must say which of the operator's edits survived",
        )
        kept_section = report.split("Your edits were KEPT in:", 1)[1]
        kept_section = kept_section.split("Your edits were DISCARDED", 1)[0]
        self.assert_contains(
            kept_section, edit_rel,
            f"{edit_rel!r} must be reported as KEPT. If it is reported as "
            f"a conflict instead, upstream changed it between the two "
            f"refs and the fixture variable is pointing at the wrong file",
        )
        self.assert_contains(
            report, "Your edits were DISCARDED in favour of the new version:",
            "the report must say which edit it discarded",
        )
        discarded_section = report.split(
            "Your edits were DISCARDED in favour of the new version:", 1,
        )[1]
        self.assert_contains(
            discarded_section, conflict_rel,
            f"{conflict_rel!r} must be reported as DISCARDED",
        )
        self.assert_contains(
            report, "Restart needed:",
            "the report must tell the operator whether the running "
            "platform needs a restart to run the new code",
        )
        print(f"     {(applied.stdout or '').strip().splitlines()[0][:160]}")

        # ── 6. On disk and in the record ────────────────────────────
        print("  [6/6] Checking the merged tree and the new record")
        self.assert_that(
            marker in edit_path.read_text(encoding="utf-8"),
            f"The operator's edit to {edit_rel!r} did not survive the "
            f"upgrade — the whole point of the three-way merge",
            file_tail=edit_path.read_text(encoding="utf-8")[-200:],
        )
        self.assert_that(
            marker not in conflict_path.read_text(encoding="utf-8"),
            f"The operator's edit to {conflict_rel!r} is still there, but "
            f"--accept-conflicts means the new version's content wins "
            f"every conflicting file",
            file_tail=conflict_path.read_text(encoding="utf-8")[-200:],
        )
        after = support.install_record(db, name)
        self.assert_that(
            after["hash"] != old_hash,
            "The install record's hash did not change, so nothing was "
            "really applied",
        )
        self.assert_that(
            after["source_resolved"] != old_resolved
            and len(after["source_resolved"] or "") == 40,
            f"source_resolved should be the newer commit, got "
            f"{after['source_resolved']!r} (was {old_resolved!r})",
        )
        self.assert_that(
            bool(after.get("shipped_hash")),
            "shipped_hash is empty after a reconciled upgrade, so the "
            "next upgrade would have no pristine anchor and would treat "
            "surviving operator edits as shipped content",
        )
        self.assert_that(
            after["hash"] != after["shipped_hash"],
            "hash and shipped_hash are equal after a merge that kept an "
            "operator edit: the tree on disk is not what upstream "
            "shipped, and the two columns must say so",
            hash=after["hash"], shipped_hash=after["shipped_hash"],
        )
        self.assert_that(
            (after["source_ref"] or "") == new_ref,
            f"source_ref should now be the ref --ref named ({new_ref!r}), "
            f"got {after['source_ref']!r}. The flag is persistent by "
            f"design: the package goes on being followed from where it "
            f"was pointed instead of snapping back to {old_ref!r}.",
        )

        elapsed = time.time() - start_ts
        return self.result(
            f"{name} upgraded {old_version} -> {after['version']}: the "
            f"operator's edit to {edit_rel} survived, {conflict_rel} was "
            f"refused until --accept-conflicts, and source_ref now "
            f"follows {new_ref} ({elapsed:.0f}s)"
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
