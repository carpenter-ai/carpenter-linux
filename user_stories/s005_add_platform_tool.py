"""
S005 — Agent Adds a Platform Tool via the Coding-Change Workflow

The user asks Carpenter to add a new built-in chat tool called
`double_string`.  The agent creates a coding-change arc that targets the
platform source directory, the built-in coding agent writes the necessary
changes, and the arc enters `waiting` state with a diff for review.

This story plays the role of the human reviewer: it reads the diff from the
arc's state, verifies it mentions `double_string`, and approves it via
POST /api/review/{review_id}/decide.  After the patch is applied the story
sends a follow-up message asking the agent to use the new tool.

Expected behaviour:
  1. Agent acknowledges the request and creates a coding-change arc.
  2. Built-in coding agent writes the double_string tool into the platform.
  3. diff is generated; arc transitions to 'waiting'.
  4. Story (as human) reads and approves the diff.
  5. Patch applied; platform hot-reloads the affected module.
  6. Agent uses double_string('hello') and returns 'hellohello'.

DB verification:
  - Coding-change root arc reaches status='completed'.
  - No child arc remains in failed/cancelled.
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

_TOOL_NAME = "double_string"

_ADD_PROMPT = (
    "Please add a new built-in chat tool called 'double_string' to the platform. "
    "It should accept a single parameter 'text' (a string) and return the text "
    "concatenated with itself (doubled).  For example double_string('hi') returns "
    "'hihi'.  Use the platform coding-change workflow to make the modification "
    "to the platform source."
)

_USE_PROMPT = (
    f"Please use the {_TOOL_NAME} tool to double the string 'hello'."
)


class AddPlatformToolViaCodingChange(AcceptanceStory):
    name = "S005 — Agent Adds a Platform Tool via Coding-Change"
    description = (
        "Agent adds double_string chat tool via coding-change arc; "
        "story approves the diff; agent then uses the new tool."
    )
    timeout = 600  # Coding-change pipeline needs more than 300s on Pi

    _source_dir: str | None = None

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # Discover platform source directory for cleanup.
        # Use environment variable if set, otherwise fall back to this repo's parent.
        self._source_dir = os.environ.get("CARPENTER_SOURCE_DIR")
        if not self._source_dir:
            self._source_dir = str(Path(__file__).resolve().parents[1])

        # ── 1. Ask the agent to add the tool ─────────────────────────────────
        print(f"\n  [1/5] Requesting '{_TOOL_NAME}' tool addition...")
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

        # ── 2. Poll for the arc to enter 'waiting' (coding agent is running) ─
        # The built-in coding agent reads, writes, edits files via its tool
        # loop — allow up to 5 minutes on the Pi.
        print(f"  [2/5] Waiting for coding-change arc to reach 'waiting' (≤5 min)...")
        review_arc = None
        if db is not None:
            review_arc = db.wait_for_pending_review_arc(start_ts, timeout=300)
            self.assert_that(
                review_arc is not None,
                "Coding-change arc never reached 'waiting' with a review_id",
                arcs=db.format_arcs_table(db.get_arcs_created_after(start_ts)),
            )

        arc_state = review_arc["arc_state"]
        review_id = arc_state["review_id"]
        diff_content = arc_state.get("diff", "")
        changed_files = arc_state.get("changed_files", [])
        print(f"     Arc {review_arc['id']} waiting. Files: {changed_files}")
        print(f"     Diff preview: {diff_content[:300]}")

        # ── 3. Inspect the diff ───────────────────────────────────────────────
        print(f"  [3/5] Inspecting diff...")
        self.assert_that(
            bool(diff_content),
            "Diff is empty — coding agent produced no changes",
            arc_id=review_arc["id"],
        )
        self.assert_that(
            _TOOL_NAME in diff_content,
            f"Diff does not mention '{_TOOL_NAME}' — wrong changes were made",
            diff_preview=diff_content[:600],
        )

        # ── 4. Approve the diff (acting as human reviewer) ───────────────────
        print(f"  [4/5] Approving diff (review_id={review_id})...")
        result = client.submit_review_decision(
            review_id,
            decision="approve",
            comment=f"Correct — adds {_TOOL_NAME} as requested.",
        )
        self.assert_that(
            result.get("recorded") is True,
            "Approval was not recorded by the server",
            server_response=result,
        )

        # ── 5. Wait for the arc to complete (patch is being applied) ─────────
        print(f"  [5/5] Waiting for arc {review_arc['id']} to complete (≤120s)...")
        if db is not None:
            final_arc = db.wait_for_arc_terminal(review_arc["id"], timeout=120)
            self.assert_that(
                final_arc is not None and final_arc["status"] == "completed",
                f"Coding-change arc did not complete "
                f"(status={final_arc['status'] if final_arc else 'not found'})",
                arcs=db.format_arcs_table(db.get_arcs_created_after(start_ts)),
            )
            print(f"     Arc completed ✓")

            # No child arc should be in a failed/cancelled state
            self.assert_no_failed_arcs_since(db, start_ts)

        # ── 6. Use the new tool ───────────────────────────────────────────────
        # The coding-change patches config_seed/chat_tools/ in the source repo,
        # but the server loads tools from the runtime dir (~/carpenter/config/chat_tools/).
        # Sync the patched file so the hot-reload picks it up.
        try:
            import yaml as _yaml
            cfg_path = Path.home() / "carpenter" / "config" / "config.yaml"
            cfg = _yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
            server_dir = cfg.get("platform_server_dir", str(Path(self._source_dir)))
            self._sync_chat_tools_to_runtime(Path(server_dir))
        except Exception as exc:
            print(f"  [sync] Warning: could not sync chat tools: {exc}")

        # Flush the work queue: arc.chat_notify and kb.work_summary items from
        # the completed arcs would consume API rate limit budget and block our
        # test message.  Completing them in the DB prevents new claims.
        if db is not None:
            import sqlite3 as _sql3
            _conn = _sql3.connect(db.db_path)
            try:
                _conn.execute(
                    "UPDATE work_queue SET status='completed' "
                    "WHERE status IN ('pending', 'claimed')"
                )
                _conn.commit()
                print(f"  [flush] Flushed pending work queue items")
            finally:
                _conn.close()

        # The patch application can trigger a server restart (hot-reload SEGV
        # on Pi).  Wait for the server to be available before sending.
        print(f"  Using new '{_TOOL_NAME}' tool...")
        for _wait in range(30):
            if client.is_running():
                break
            time.sleep(2)
        else:
            self.assert_that(False, "Server did not come back after patch application")

        # Wait for the hot-reload heartbeat to detect the file change (every 5s)
        # and for any in-flight API calls to complete.
        time.sleep(15)

        # Use a fresh conversation — the old one may have stale pending state
        # from the server restart or arc.chat_notify re-invocations.
        conv_id2 = client.create_conversation()
        client.send_message(_USE_PROMPT, conv_id2)
        client.wait_for_pending_to_clear(conv_id2, timeout=120)

        msgs = client.get_assistant_messages(conv_id2)
        use_resp = msgs[-1]["content"]
        print(f"     {use_resp[:150]}")
        self.assert_that(
            "hellohello" in use_resp.lower(),
            "Response does not contain 'hellohello' (expected double of 'hello')",
            response_preview=use_resp[:400],
        )

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"'{_TOOL_NAME}' added via coding-change ✓, "
                f"diff approved ✓, arc completed ✓, "
                f"double_string('hello')='hellohello' ✓"
            ),
        )

    @staticmethod
    def _strip_double_string_from_py(path: Path) -> bool:
        """Remove the double_string function (decorator + def) from a .py file.

        Removes the @chat_tool(...) decorator block and the
        def double_string(...) function body.  Returns True if modified.
        """
        if not path.exists():
            return False
        lines = path.read_text().splitlines(keepends=True)
        new_lines: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.strip().startswith("def double_string"):
                # Walk backwards to remove preceding @chat_tool(...) decorator
                while new_lines and new_lines[-1].strip() != "":
                    new_lines.pop()
                # Remove trailing blank lines before decorator
                while new_lines and new_lines[-1].strip() == "":
                    new_lines.pop()
                # Skip def line + body (indented or blank lines)
                i += 1
                while i < len(lines) and (lines[i].startswith((" ", "\t")) or lines[i].strip() == ""):
                    i += 1
                continue
            new_lines.append(line)
            i += 1
        result = "".join(new_lines)
        if not result.endswith("\n"):
            result += "\n"
        if result != "".join(lines):
            path.write_text(result)
            return True
        return False

    @staticmethod
    def _sync_chat_tools_to_runtime(source_dir: Path) -> None:
        """Copy config_seed/chat_tools/ from source repo to runtime config dir."""
        import shutil
        seed_dir = source_dir / "config_seed" / "chat_tools"
        runtime_dir = Path.home() / "carpenter" / "config" / "chat_tools"
        if not seed_dir.is_dir() or not runtime_dir.is_dir():
            return
        for py_file in seed_dir.glob("*.py"):
            dest = runtime_dir / py_file.name
            shutil.copy2(str(py_file), str(dest))
            print(f"  [sync] Copied {py_file.name} to runtime chat_tools")

    def _strip_double_string_from_chat_tools(self, root: Path) -> None:
        """Remove double_string from config_seed/chat_tools and runtime chat_tools."""
        # Source repo config_seed
        for utils in [
            root / "config_seed" / "chat_tools" / "utilities.py",
            Path.home() / "carpenter" / "config" / "chat_tools" / "utilities.py",
        ]:
            try:
                if self._strip_double_string_from_py(utils):
                    print(f"  [cleanup] Removed double_string from {utils}")
            except Exception as exc:
                print(f"  [cleanup] Could not clean {utils}: {exc}")

        # Server dir config_seed
        try:
            import yaml as _yaml
            cfg_path = Path.home() / "carpenter" / "config" / "config.yaml"
            cfg = _yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
            server_dir = cfg.get("platform_server_dir")
            if server_dir:
                sv_utils = Path(server_dir) / "config_seed" / "chat_tools" / "utilities.py"
                if self._strip_double_string_from_py(sv_utils):
                    print(f"  [cleanup] Removed double_string from {sv_utils}")
        except Exception as exc:
            print(f"  [cleanup] Could not clean server chat_tools: {exc}")

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Remove double_string artifacts from platform source."""
        root = Path(self._source_dir) if self._source_dir else Path(__file__).resolve().parents[1]

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

        # Glob-walk candidate tool homes. The coding agent may drop the tool
        # file in any of these locations under several name variants
        # (``double_string.py``, ``double_string_tool.py``,
        # ``my_double_string.py``).  Bounded to specific symbol + specific
        # directories so we cannot accidentally delete unrelated files.
        scan_dirs: list[Path] = []
        for r in cleanup_roots:
            scan_dirs.extend([
                r / "carpenter_tools" / "read",
                r / "carpenter_tools" / "act",
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
                    # Skip files whose names happen to share an unrelated
                    # substring (defensive — the glob already constrains).
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

        # Strip ``double_string`` references from registry-ish files so a
        # stale import or registration cannot block a later run.  Cover
        # both the runner source repo and the live server repo.
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
        # to import the new tool, restoring those files line-by-line is
        # fragile (may also strip unrelated imports on the same line).
        # Use ``git checkout`` to restore the file to its tracked state if
        # the tool name appears in it.  Operate on cleanup_roots so we
        # fix both runner-source and live-server clones.
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

        # Remove tool YAML entry if it was added to a separate file.
        # The coding agent may use a numeric prefix (e.g. 13-double_string.yaml),
        # so glob for any file matching *double_string*.yaml in config-seed/tools/.
        tool_defaults_dir = root / "config-seed" / "tools"
        if tool_defaults_dir.is_dir():
            for pattern in (f"*{_TOOL_NAME}*", f"*double?string*"):
                for match in tool_defaults_dir.glob(f"{pattern}.yaml"):
                    try:
                        match.unlink()
                        print(f"  [cleanup] Removed {match}")
                    except Exception as exc:
                        print(f"  [cleanup] Could not remove {match}: {exc}")

        # Remove tool YAML files from runtime tools dir (~/carpenter/config/tools/).
        # The coding agent copies config-seed/tools/ into the runtime dir at deploy,
        # and different runs may produce slightly different filenames.
        try:
            import yaml as _yaml
            cfg_path = Path.home() / "carpenter" / "config" / "config.yaml"
            cfg = _yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
            base_dir = cfg.get("base_dir", str(Path.home() / "carpenter"))
            runtime_tools_dir = Path(cfg.get("tools_dir", str(Path(base_dir) / "config" / "tools")))
            if runtime_tools_dir.is_dir():
                for pattern in (f"*{_TOOL_NAME}*", f"*double?string*"):
                    for match in runtime_tools_dir.glob(f"{pattern}.yaml"):
                        try:
                            match.unlink()
                            print(f"  [cleanup] Removed runtime tool {match}")
                        except Exception as exc:
                            print(f"  [cleanup] Could not remove {match}: {exc}")

            # Also clean the server dir's config-seed/tools/ since deploy copies there.
            server_dir = cfg.get("platform_server_dir")
            if server_dir:
                server_td = Path(server_dir) / "config-seed" / "tools"
                if server_td.is_dir():
                    for pattern in (f"*{_TOOL_NAME}*", f"*double?string*"):
                        for match in server_td.glob(f"{pattern}.yaml"):
                            try:
                                match.unlink()
                                print(f"  [cleanup] Removed server config-seed/tools {match}")
                            except Exception as exc:
                                print(f"  [cleanup] Could not remove {match}: {exc}")
        except Exception as exc:
            print(f"  [cleanup] Could not clean runtime/server tool dirs: {exc}")

        # Remove double_string from Python chat_tools modules (config_seed and runtime).
        # The coding agent adds the function to config_seed/chat_tools/utilities.py.
        self._strip_double_string_from_chat_tools(root)

        # Remove tool backend module if created
        for suffix in (".py", ".cpython-311.pyc"):
            mod = root / "carpenter" / "tool_backends" / f"{_TOOL_NAME}{suffix}"
            if mod.exists():
                try:
                    mod.unlink()
                    print(f"  [cleanup] Removed {mod}")
                except Exception as exc:
                    print(f"  [cleanup] Could not remove {mod}: {exc}")

        # Revert double_string handler from invocation.py if present
        inv_path = root / "carpenter" / "agent" / "invocation.py"
        if inv_path.exists():
            try:
                text = inv_path.read_text()
                # Remove the elif block for double_string
                marker = (
                    '        elif tool_name == "double_string":\n'
                    '            text = tool_input["text"]\n'
                    '            return text + text\n'
                )
                if marker in text:
                    text = text.replace(marker, "")
                    inv_path.write_text(text)
                    print(f"  [cleanup] Removed double_string handler from invocation.py")
            except Exception as exc:
                print(f"  [cleanup] Could not revert invocation.py: {exc}")

        # Remove double_string entry from 03-utilities.yaml if present
        utils_yaml = root / "config-seed" / "tools" / "03-utilities.yaml"
        if utils_yaml.exists():
            try:
                import re
                text = utils_yaml.read_text()
                # Remove the YAML block for double_string
                pattern = r'\n  - name: double_string\n(?:    .*\n)*'
                cleaned = re.sub(pattern, '\n', text)
                if cleaned != text:
                    utils_yaml.write_text(cleaned)
                    print(f"  [cleanup] Removed double_string from 03-utilities.yaml")
            except Exception as exc:
                print(f"  [cleanup] Could not revert 03-utilities.yaml: {exc}")

        # Discard the coding-change commit if it was applied
        import subprocess
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "-1", "--format=%s"],
                cwd=str(root), capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and "double_string" in result.stdout.lower():
                subprocess.run(
                    ["git", "reset", "--hard", "HEAD~1"],
                    cwd=str(root), capture_output=True, text=True, timeout=10,
                )
                print(f"  [cleanup] Reverted last commit (contained double_string)")
        except Exception as exc:
            print(f"  [cleanup] Could not check/revert git commit: {exc}")

        print(f"  [cleanup] Done")
