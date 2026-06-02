"""
S007 — Add (with Revision), Verify Visibility, and Remove an Action Tool

The user asks Carpenter to add a new `github_gist` action tool that can
post data to the GitHub Gist API — an externally dangerous operation that must
pass through the reviewed coding-change workflow.

Review round 1: The story plays the human reviewer and requests a revision —
asking the coding agent to add a `private` boolean parameter before accepting.

Review round 2: The story approves the revised diff.

After the tool is applied the story verifies two properties:

  Write-mode visible:   carpenter_tools/act/github_gist.py exists on disk.
                        The chat agent confirms it can use the tool from
                        submitted (reviewed) code.

  Read-mode invisible:  carpenter_tools/read/github_gist.py does NOT exist.
                        The chat agent confirms it cannot call github_gist
                        directly — it is not among its direct/read-only tools.

The user then asks the agent to remove the tool.  The story approves the
removal diff and verifies:
  - carpenter_tools/act/github_gist.py no longer exists on disk.
  - The chat agent confirms it no longer has access to github_gist.

Health checks:
  - No arc in the add workflow reaches failed/cancelled status.
  - No arc in the remove workflow reaches failed/cancelled status.
  (A "string of failures" is a sign of a struggling workflow and is
  flagged as a test failure.)

NOTE: This story makes a transient change to platform source — adds then
removes carpenter_tools/act/github_gist.py.  A second run is safe; the
coding agent will find the file absent, re-add it, then remove it again.
"""

import os
import time
from pathlib import Path

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_TOOL_NAME = "github_gist"

_ADD_PROMPT = (
    "Please add a new action tool called 'github_gist' to the platform. "
    "It should live in carpenter_tools/act/github_gist.py and implement a "
    "callback-based tool that posts a gist to the GitHub Gist API. "
    "The tool should accept parameters: description (str), filename (str), "
    "content (str). Use the @tool() decorator from carpenter_tools.tool_meta "
    "and follow the callback pattern used by other tools in carpenter_tools/act/. "
    "Use the platform coding-change workflow to make the modification."
)

_REVISE_COMMENT = (
    "Please also add a boolean parameter 'private' (default False) that "
    "controls whether the created gist will be private (secret=True) or "
    "public (secret=False). Pass it through to the API payload."
)

_WRITE_MODE_CHECK_PROMPT = (
    "Can you check whether the file carpenter_tools/act/github_gist.py now "
    "exists in the platform source? Just list or check the directory — "
    "do not call any external API or submit code."
)

_READ_MODE_CHECK_PROMPT = (
    "Please list all the tools you can call directly in this conversation "
    "without submitting code for review. Is 'github_gist' among them? "
    "I expect it should NOT be, since it is an action tool that requires "
    "reviewed code submission."
)

_REMOVE_PROMPT = (
    "Please remove the 'github_gist' tool from the platform. "
    "Delete carpenter_tools/act/github_gist.py and remove any references to "
    "it (imports, registrations, etc.). "
    "Use the platform coding-change workflow to make the change."
)

_REMOVAL_CHECK_PROMPT = (
    "Please verify: does carpenter_tools/act/github_gist.py still exist? "
    "Can you still access the github_gist tool?"
)


class AddReviseAndRemoveActionTool(AcceptanceStory):
    name = "S007 — Add (Revise), Verify Visibility, and Remove an Action Tool"
    # Three review rounds (add → revise → remove) on haiku at ~80–90s per
    # round push past the default 300s budget.  Recent successful run on
    # main took 264s; ~600s gives comfortable headroom.
    timeout = 600
    description = (
        "Adds github_gist action tool via coding-change with one revision round; "
        "verifies write-mode visible / read-mode invisible; removes tool and "
        "verifies absence. Asserts no failure arcs in either workflow."
    )

    def __init__(self) -> None:
        self._source_dir: str | None = None  # saved for cleanup()

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Ask the agent to add the tool ─────────────────────────────────
        print(f"\n  [1/8] Requesting '{_TOOL_NAME}' tool addition...")
        conv_id = client.create_conversation()
        client.send_message(_ADD_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(msgs) >= 1,
            "No response after tool-addition request",
            conversation_id=conv_id,
        )
        init_resp = msgs[-1]["content"]
        print(f"     {init_resp[:120]}")
        self.assert_that(
            any(kw in init_resp.lower() for kw in
                ("coding", "modif", "change", "arc", "implement", "add", "work")),
            "Initial response does not acknowledge the coding-change task",
            response_preview=init_resp[:400],
        )

        # ── 2. Wait for the first diff review ────────────────────────────────
        print(f"  [2/8] Waiting for first diff review (≤5 min)...")
        review_arc1: dict | None = None
        if db is not None:
            review_arc1 = db.wait_for_pending_review_arc(start_ts, timeout=300)
            self.assert_that(
                review_arc1 is not None,
                "Coding-change arc never reached 'waiting' (round 1)",
                arcs=db.format_arcs_table(db.get_arcs_created_after(start_ts)),
            )

        arc_state1 = review_arc1["arc_state"]
        review_id1 = arc_state1["review_id"]
        diff1 = arc_state1.get("diff", "")
        source_dir = arc_state1.get("source_dir", "")
        if source_dir:
            self._source_dir = source_dir
        changed1 = arc_state1.get("changed_files", [])
        print(f"     Arc {review_arc1['id']} waiting. Files: {changed1}")
        print(f"     Diff preview: {diff1[:200]}")

        self.assert_that(
            bool(diff1),
            "First diff is empty — coding agent produced no changes",
            arc_id=review_arc1["id"],
        )
        self.assert_that(
            _TOOL_NAME in diff1 or "github" in diff1.lower(),
            f"First diff does not mention '{_TOOL_NAME}'",
            diff_preview=diff1[:600],
        )

        # ── 3. Request a revision ─────────────────────────────────────────────
        print(f"  [3/8] Submitting revision request (adding 'private' param)...")
        result1 = client.submit_review_decision(
            review_id1,
            decision="revise",
            comment=_REVISE_COMMENT,
        )
        self.assert_that(
            result1.get("recorded") is True,
            "Revision request was not recorded by the server",
            server_response=result1,
        )
        print(f"     Revision submitted. Waiting for revised diff (≤5 min)...")

        # ── 4. Wait for the revised diff (different review_id) ───────────────
        print(f"  [4/8] Waiting for revised diff (≤5 min)...")
        review_arc2: dict | None = None
        if db is not None:
            review_arc2 = db.wait_for_pending_review_arc(
                start_ts, timeout=300, exclude_review_ids={review_id1},
            )
            self.assert_that(
                review_arc2 is not None,
                "No revised diff appeared after revision request (round 2)",
                arcs=db.format_arcs_table(db.get_arcs_created_after(start_ts)),
            )

        arc_state2 = review_arc2["arc_state"]
        review_id2 = arc_state2["review_id"]
        diff2 = arc_state2.get("diff", "")
        changed2 = arc_state2.get("changed_files", [])
        print(f"     Arc {review_arc2['id']} waiting (revised). Files: {changed2}")
        print(f"     Revised diff preview: {diff2[:200]}")

        self.assert_that(
            bool(diff2),
            "Revised diff is empty",
            arc_id=review_arc2["id"],
        )
        # The revised diff should mention the private parameter
        self.assert_that(
            "private" in diff2.lower() or "secret" in diff2.lower(),
            "Revised diff does not mention 'private' or 'secret' parameter",
            diff_preview=diff2[:600],
        )

        # ── 5. Approve the revised diff ───────────────────────────────────────
        add_start_ts = time.time()
        print(f"  [5/8] Approving revised diff (review_id={review_id2})...")
        result2 = client.submit_review_decision(
            review_id2,
            decision="approve",
            comment=f"Correct — adds {_TOOL_NAME} with private parameter.",
        )
        self.assert_that(
            result2.get("recorded") is True,
            "Approval was not recorded by the server",
            server_response=result2,
        )

        # Wait for the add arc to complete
        print(f"  [5/8] Waiting for add arc to complete (≤120s)...")
        if db is not None:
            final_add_arc = db.wait_for_arc_terminal(review_arc2["id"], timeout=120)
            self.assert_that(
                final_add_arc is not None
                and final_add_arc["status"] == "completed",
                f"Add coding-change arc did not complete "
                f"(status={final_add_arc['status'] if final_add_arc else 'not found'})",
                arcs=db.format_arcs_table(db.get_arcs_created_after(start_ts)),
            )
            print(f"     Add arc completed ✓")

            # Health check: no failures in the add workflow
            self.assert_no_failed_arcs_since(
                db, start_ts, workflow_label="Add workflow"
            )

        # ── 6. Verify write-mode visibility ───────────────────────────────────
        print(f"  [6/8] Verifying write-mode visibility...")

        # Structural: file exists on disk
        act_path: Path | None = None
        read_path: Path | None = None
        if self._source_dir:
            act_path = Path(self._source_dir) / "carpenter_tools" / "act" / f"{_TOOL_NAME}.py"
            read_path = Path(self._source_dir) / "carpenter_tools" / "read" / f"{_TOOL_NAME}.py"
            self.assert_that(
                act_path.exists(),
                f"carpenter_tools/act/{_TOOL_NAME}.py does not exist after add",
                path=str(act_path),
            )
            print(f"     {act_path} exists ✓")

        # Behavioural: ask the agent
        client.send_message(_WRITE_MODE_CHECK_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=60)
        msgs = client.get_assistant_messages(conv_id)
        write_resp = msgs[-1]["content"]
        print(f"     Write-mode check: {write_resp[:150]}")
        self.assert_that(
            "github_gist" in write_resp.lower() or "gist" in write_resp.lower(),
            "Write-mode check response does not acknowledge github_gist file",
            response_preview=write_resp[:400],
        )
        self.assert_that(
            any(kw in write_resp.lower() for kw in
                ("exist", "found", "yes", "there", "present", "see",
                 "creat", "success", "implement", "has been")),
            "Write-mode check does not confirm the file exists",
            response_preview=write_resp[:400],
        )

        # ── 7. Verify read-mode invisibility ──────────────────────────────────
        print(f"  [7/8] Verifying read-mode invisibility...")

        # Structural: file NOT in read/ directory
        if read_path is not None:
            self.assert_that(
                not read_path.exists(),
                f"carpenter_tools/read/{_TOOL_NAME}.py unexpectedly exists — "
                f"action tools must not appear in the read-only tool set",
                path=str(read_path),
            )
            print(f"     {read_path} absent ✓")

        # Behavioural: ask the agent about direct tool availability
        client.send_message(_READ_MODE_CHECK_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=60)
        msgs = client.get_assistant_messages(conv_id)
        read_resp = msgs[-1]["content"]
        print(f"     Read-mode check: {read_resp[:150]}")
        self.assert_that(
            # Agent should say it does NOT have github_gist as a direct tool
            "github_gist" not in read_resp.lower()
            or any(neg in read_resp.lower() for neg in
                   ("not", "no ", "cannot", "can't", "don't", "doesn't",
                    "unavailable", "only via", "submit", "action tool",
                    "code submission")),
            "Read-mode check response suggests github_gist is a direct tool "
            "(expected: agent confirms it is NOT directly callable)",
            response_preview=read_resp[:400],
        )

        # ── 8. Remove the tool ────────────────────────────────────────────────
        remove_start_ts = time.time()
        print(f"  [8/8] Requesting '{_TOOL_NAME}' removal...")
        client.send_message(_REMOVE_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)

        msgs = client.get_assistant_messages(conv_id)
        remove_init_resp = msgs[-1]["content"]
        print(f"     {remove_init_resp[:120]}")
        self.assert_that(
            any(kw in remove_init_resp.lower() for kw in
                ("coding", "delet", "remov", "change", "arc", "work")),
            "Remove response does not acknowledge the coding-change task",
            response_preview=remove_init_resp[:400],
        )

        # Wait for removal diff
        print(f"  [8/8] Waiting for removal diff (≤5 min)...")
        review_arc_rm: dict | None = None
        if db is not None:
            review_arc_rm = db.wait_for_pending_review_arc(
                remove_start_ts, timeout=300,
            )
            self.assert_that(
                review_arc_rm is not None,
                "Removal coding-change arc never reached 'waiting'",
                arcs=db.format_arcs_table(
                    db.get_arcs_created_after(remove_start_ts)
                ),
            )

        arc_state_rm = review_arc_rm["arc_state"]
        review_id_rm = arc_state_rm["review_id"]
        diff_rm = arc_state_rm.get("diff", "")
        changed_rm = arc_state_rm.get("changed_files", [])
        print(f"     Removal arc {review_arc_rm['id']} waiting. Files: {changed_rm}")
        print(f"     Removal diff preview: {diff_rm[:200]}")

        self.assert_that(
            bool(diff_rm),
            "Removal diff is empty — coding agent made no changes",
            arc_id=review_arc_rm["id"],
        )

        # Approve the removal
        result_rm = client.submit_review_decision(
            review_id_rm,
            decision="approve",
            comment=f"Correct — removes {_TOOL_NAME} as requested.",
        )
        self.assert_that(
            result_rm.get("recorded") is True,
            "Removal approval was not recorded",
            server_response=result_rm,
        )

        # Wait for removal arc to complete
        print(f"  [8/8] Waiting for removal arc to complete (≤120s)...")
        if db is not None:
            final_rm_arc = db.wait_for_arc_terminal(review_arc_rm["id"], timeout=120)
            self.assert_that(
                final_rm_arc is not None
                and final_rm_arc["status"] == "completed",
                f"Removal arc did not complete "
                f"(status={final_rm_arc['status'] if final_rm_arc else 'not found'})",
                arcs=db.format_arcs_table(
                    db.get_arcs_created_after(remove_start_ts)
                ),
            )
            print(f"     Removal arc completed ✓")

            # Health check: no failures in the remove workflow
            self.assert_no_failed_arcs_since(
                db, remove_start_ts, workflow_label="Remove workflow"
            )

        # Structural: file is gone
        if act_path is not None:
            self.assert_that(
                not act_path.exists(),
                f"carpenter_tools/act/{_TOOL_NAME}.py still exists after removal",
                path=str(act_path),
            )
            print(f"     {act_path} removed ✓")

        # Behavioural: agent confirms removal
        client.send_message(_REMOVAL_CHECK_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=60)
        msgs = client.get_assistant_messages(conv_id)
        removal_conf = msgs[-1]["content"]
        print(f"     Removal confirmation: {removal_conf[:150]}")
        self.assert_that(
            any(kw in removal_conf.lower() for kw in
                ("not exist", "gone", "delet", "remov", "no longer",
                 "doesn't exist", "does not exist", "not found", "absent")),
            "Removal confirmation does not indicate github_gist is gone",
            response_preview=removal_conf[:400],
        )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"'{_TOOL_NAME}' added via coding-change ✓, "
                f"revision round completed ✓, "
                f"write-mode visible ✓, "
                f"read-mode invisible ✓, "
                f"removed via coding-change ✓, "
                f"absence confirmed ✓, "
                f"zero failure arcs in both workflows ✓"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Remove all github_gist artifacts if the story failed before removal step.

        The coding agent touches three locations:
          - carpenter_tools/act/github_gist.py         (new file)
          - carpenter/tool_backends/github_gist.py  (new file)
          - carpenter/api/callbacks.py            (modified: import + 2 entries)
        """
        root = (
            Path(self._source_dir) if self._source_dir
            else Path(os.environ.get("CARPENTER_SOURCE_DIR", str(Path(__file__).resolve().parents[1])))
        )

        # The coding agent edits the *running* server's source tree, which
        # may live in a separate clone (``platform_server_dir`` in config).
        # Walk both the runner's source repo AND the live server repo so
        # cleanup is correct regardless of which clone the daemon uses.
        cleanup_roots: list[Path] = [root]
        try:
            import yaml as _yaml
            cfg_path = Path.home() / "carpenter" / "config" / "config.yaml"
            cfg = _yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
            server_dir = cfg.get("platform_server_dir")
            if server_dir:
                server_path = Path(server_dir).resolve()
                if server_path != root.resolve() and server_path not in [p.resolve() for p in cleanup_roots]:
                    cleanup_roots.append(server_path)
        except Exception as exc:  # noqa: BLE001
            print(f"  [cleanup] Could not resolve platform_server_dir: {exc}")

        # Delete the canonical new files (kept for backward compatibility
        # with the original cleanup contract).
        for r in cleanup_roots:
            for rel in (
                f"carpenter_tools/act/{_TOOL_NAME}.py",
                f"carpenter/tool_backends/{_TOOL_NAME}.py",
            ):
                p = r / rel
                if p.exists():
                    try:
                        p.unlink()
                        print(f"  [cleanup] Removed {p}")
                    except Exception as exc:
                        print(f"  [cleanup] Could not remove {p}: {exc}")

        # Glob-walk candidate tool homes. The coding agent may pick a name
        # variant (``github_gist_tool.py``, ``gist.py`` containing the
        # github_gist symbol) or drop the file in read/ or config_seed/.
        # Bounded to specific symbol + specific directories so we cannot
        # delete unrelated files.
        scan_dirs: list[Path] = []
        for r in cleanup_roots:
            scan_dirs.extend([
                r / "carpenter_tools" / "act",
                r / "carpenter_tools" / "read",
                r / "carpenter" / "tool_backends",
                r / "config_seed" / "chat_tools",
            ])
        glob_removed: list[Path] = []
        for d in scan_dirs:
            if not d.is_dir():
                continue
            for pat in (
                f"{_TOOL_NAME}.py",
                f"{_TOOL_NAME}_*.py",
                f"*{_TOOL_NAME}*.py",
            ):
                for match in d.glob(pat):
                    if _TOOL_NAME not in match.name:
                        continue
                    try:
                        match.unlink()
                        glob_removed.append(match)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  [cleanup] Could not remove {match}: {exc}")
        if glob_removed:
            print(
                f"  [cleanup] Glob-removed {len(glob_removed)} artifact(s): "
                f"{[str(p) for p in glob_removed]}"
            )

        # Strip ``github_gist`` references from registry-ish files. The
        # coding agent typically edits ``carpenter/api/callbacks.py`` to
        # register the action callback; some variants may also touch
        # ``chat_tool_registry.py``.  Cover both the runner source repo
        # and the live server repo.
        registry_paths: list[Path] = []
        for r in cleanup_roots:
            for registry_rel in (
                ("carpenter", "api", "callbacks.py"),
                ("carpenter", "chat_tool_registry.py"),
            ):
                registry_paths.append(r.joinpath(*registry_rel))
        for reg_path in registry_paths:
            if reg_path.exists():
                try:
                    original = reg_path.read_text()
                    filtered = "\n".join(
                        line for line in original.splitlines()
                        if _TOOL_NAME not in line
                    )
                    if original.endswith("\n"):
                        filtered += "\n"
                    if filtered != original:
                        reg_path.write_text(filtered)
                        print(
                            f"  [cleanup] Removed {_TOOL_NAME} references "
                            f"from {reg_path}"
                        )
                except Exception as exc:  # noqa: BLE001
                    print(f"  [cleanup] Could not clean {reg_path}: {exc}")

        # If the coding agent edited ``carpenter_tools/{read,act}/__init__.py``
        # to import the new tool, restore those files via git checkout
        # rather than line-stripping (which could remove unrelated
        # imports on the same line).
        import subprocess as _subprocess
        for r in cleanup_roots:
            for init_rel in (
                ("carpenter_tools", "read", "__init__.py"),
                ("carpenter_tools", "act", "__init__.py"),
            ):
                init_path = r.joinpath(*init_rel)
                if not init_path.exists():
                    continue
                try:
                    if _TOOL_NAME in init_path.read_text():
                        _subprocess.run(
                            ["git", "checkout", "--", str(init_path.relative_to(r))],
                            cwd=str(r),
                            check=False,
                            capture_output=True,
                        )
                        print(
                            f"  [cleanup] git-restored {init_path} "
                            f"(contained {_TOOL_NAME})"
                        )
                except Exception as exc:  # noqa: BLE001
                    print(f"  [cleanup] Could not restore {init_path}: {exc}")
