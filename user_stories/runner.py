#!/usr/bin/env python3
"""
Carpenter Acceptance Test Runner

Usage:
    python3 user_stories/runner.py              # Run all stories
    python3 user_stories/runner.py s001         # Run by prefix
    python3 user_stories/runner.py s001 s002    # Multiple stories

Environment Variables:
    CARPENTER_TEST_URL    Server URL          (default: http://localhost:<port from config>)
    CARPENTER_TEST_TOKEN  UI auth token       (default: read from ~/carpenter/.env)
    CARPENTER_TEST_DB     SQLite DB path      (default: read from ~/carpenter/config/config.yaml)
    CARPENTER_LAUNCH_SCRIPT  Optional path to a launch script; if set and server
                             is down, the runner will start it automatically.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FOR CLAUDE CODE AGENTS running this script:

When a test FAILS, you will see a DIAGNOSTIC section with the DB state.
Your job is to:

1. Read the DIAGNOSTIC carefully — arcs, messages, and work queue tell the story
2. Cross-reference with source code in the repo root (see REPO_ROOT)
3. Common root causes and their fixes:

   Arc auto-dispatch not firing
     → Check carpenter/core/arcs/dispatch_handler.py
     → Check that it is registered in carpenter/api/http.py lifespan
     → Look for scan_for_ready_arcs() heartbeat registration

   Agent not creating arcs / wrong arc structure
     → The invocation.py arc_planning system prompt section guides arc creation
     → Check that arc.create() / arc.add_child() are used correctly
     → CRITICAL: arc.create() and arc.add_child() return int, NOT dict!
       (See MEMORY.md — this is a documented recurring bug)

   Arcs stuck in 'pending' status
     → carpenter/core/arcs/dispatch_handler.py may not be running
     → Or arcs have unmet predecessors (check step_order, parent completion)

   Agent not using messaging.send for chat messages from arcs
     → Check the system prompt; agent may need guidance
     → The messaging.send callback posts a message to the conversation

   Server errors / 500 responses
     → Check /tmp/carpenter_acceptance.log or server stdout

4. After fixing source files, restart the server:
     kill $(pgrep -f "python3 -m carpenter") 2>/dev/null
     python3 -m carpenter > /tmp/carpenter_server.log 2>&1 &
     sleep 5  # Wait for startup

5. Re-run the failing story:
     python3 user_stories/runner.py s002

6. If you cannot determine the root cause after investigating, ESCALATE to the
   user (the parent agent) with the full diagnostic output and your analysis.
   Do not silently give up or mark as non-fixable without asking.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

# Allow running from repo root or from within user_stories/
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from user_stories.framework import (
    AcceptanceStory,
    AssertionFailure,
    DBInspector,
    StoryResult,
    CarpenterClient,
)


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------


def _load_config() -> dict:
    """Assemble runtime config from env vars and config.yaml."""
    try:
        import yaml
        cfg_path = Path(os.environ.get(
            "CARPENTER_CONFIG",
            Path.home() / "carpenter" / "config" / "config.yaml"
        ))
        file_cfg: dict = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    except Exception:
        file_cfg = {}

    # Discover UI token: CARPENTER_TEST_TOKEN env var > UI_TOKEN in {base_dir}/.env
    token = os.environ.get("CARPENTER_TEST_TOKEN")
    if not token:
        base_dir = Path(file_cfg.get("base_dir", Path.home() / "carpenter"))
        dot_env = Path(base_dir) / ".env"
        if dot_env.exists():
            for line in dot_env.read_text().splitlines():
                line = line.strip()
                if line.startswith("UI_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break

    port = file_cfg.get("port", 7842)

    # Optional launch script: CARPENTER_LAUNCH_SCRIPT env var only — not
    # assumed from repo layout, because the script lives outside the platform repo.
    launch_script = os.environ.get("CARPENTER_LAUNCH_SCRIPT", "")

    base_dir_str = file_cfg.get("base_dir", str(Path.home() / "carpenter"))
    default_workspaces = str(Path(base_dir_str) / "data" / "workspaces")

    return {
        "url": os.environ.get("CARPENTER_TEST_URL", f"http://localhost:{port}"),
        "token": token or None,
        "db_path": os.environ.get(
            "CARPENTER_TEST_DB", file_cfg.get("database_path", "")
        ),
        "launch_script": launch_script,
        "workspaces_dir": file_cfg.get("workspaces_dir", default_workspaces),
    }


# ---------------------------------------------------------------------------
# Server management
# ---------------------------------------------------------------------------


def ensure_server_running(cfg: dict) -> bool:
    """Check server is up; attempt to start it if not. Return True if ready."""
    client = CarpenterClient(cfg["url"], cfg.get("token"))
    if client.is_running():
        print(f"  ✓ Server running at {cfg['url']}")
        return True

    print(f"  ✗ Server not responding at {cfg['url']}")
    launch = cfg.get("launch_script", "")
    if launch and Path(launch).exists():
        log_path = "/tmp/carpenter_acceptance_server.log"
        print(f"  Starting server via {launch} ...")
        subprocess.Popen(
            ["bash", launch],
            cwd=str(REPO_ROOT),
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
        )
        for i in range(30):
            time.sleep(2)
            if client.is_running():
                print(f"  ✓ Server started ({(i+1)*2}s). Log: {log_path}")
                return True
        print(f"  ✗ Server failed to start within 60s. Log: {log_path}")
        return False
    else:
        print(f"  Server not running. Start it manually, e.g.:")
        print(f"    python3 -m carpenter &")
        print(f"  Or set CARPENTER_LAUNCH_SCRIPT=/path/to/launch.sh to auto-start.")
        return False


# ---------------------------------------------------------------------------
# Story discovery
# ---------------------------------------------------------------------------


def discover_stories(prefix_filter: list[str] | None = None) -> list[AcceptanceStory]:
    """Import all story modules and return instantiated AcceptanceStory objects."""
    stories_dir = Path(__file__).parent
    story_files = sorted(stories_dir.glob("s[0-9]*.py"))
    stories: list[AcceptanceStory] = []

    for path in story_files:
        stem = path.stem  # e.g. "s001_describe_abilities"
        if prefix_filter:
            if not any(stem.startswith(p) or stem == p for p in prefix_filter):
                continue

        spec = importlib.util.spec_from_file_location(f"acceptance_stories.{stem}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        for attr in dir(mod):
            obj = getattr(mod, attr)
            if not (isinstance(obj, type) and issubclass(obj, AcceptanceStory)):
                continue
            if obj is AcceptanceStory:
                continue
            # Skip framework-defined base/scaffold classes that the
            # story modules import (e.g. ``ChangeReviewStory``). Those
            # live in ``user_stories.framework`` and are abstract — they
            # have a default ``name = "unnamed"`` and no ``request_text``.
            if getattr(obj, "__module__", "").endswith(".framework"):
                continue
            stories.append(obj())

    return stories


# ---------------------------------------------------------------------------
# Story execution
# ---------------------------------------------------------------------------


def run_story(
    story: AcceptanceStory,
    client: CarpenterClient,
    db: DBInspector,
) -> StoryResult:
    """Execute one story. Returns StoryResult. Prints progress to stdout."""
    print(f"\n{'━'*70}")
    print(f"  STORY  {story.name}")
    if story.description:
        print(f"  DESC   {story.description}")
    print(f"{'━'*70}")

    start = time.monotonic()
    result: StoryResult | None = None
    try:
        result = story.run(client, db)
        result.duration_s = time.monotonic() - start
        print(f"\n  ✓  PASSED  ({result.duration_s:.1f}s)")
        if result.message:
            print(f"     {result.message}")
        return result

    except (AssertionFailure, AssertionError) as exc:
        duration = time.monotonic() - start
        err_msg = exc.message if isinstance(exc, AssertionFailure) else str(exc)
        extra = getattr(exc, "diagnostics", {})
        print(f"\n  ✗  FAILED: {err_msg}")
        result = StoryResult(
            name=story.name,
            passed=False,
            error=err_msg,
            diagnostics=extra,
            duration_s=duration,
        )
        _print_diagnostics(result, db)
        return result

    except BaseException as exc:
        # pytest.skip() raises pytest.outcomes.Skipped, which inherits
        # from BaseException (not Exception). Catch it here so a story
        # declaring an unmet prerequisite reports cleanly as a skip
        # instead of crashing the whole runner.
        if type(exc).__name__ == "Skipped":
            duration = time.monotonic() - start
            reason = str(exc)
            print(f"\n  ⊝  SKIPPED: {reason}")
            return StoryResult(
                name=story.name,
                passed=True,
                skipped=True,
                message=reason,
                duration_s=duration,
            )
        raise

    except TimeoutError as exc:
        duration = time.monotonic() - start
        print(f"\n  ✗  TIMEOUT: {exc}")
        result = StoryResult(
            name=story.name,
            passed=False,
            error=f"TIMEOUT: {exc}",
            duration_s=duration,
        )
        _print_diagnostics(result, db)
        return result

    except Exception as exc:
        duration = time.monotonic() - start
        tb = traceback.format_exc()
        print(f"\n  ✗  ERROR: {type(exc).__name__}: {exc}")
        print(f"     {tb}")
        result = StoryResult(
            name=story.name,
            passed=False,
            error=f"{type(exc).__name__}: {exc}\n{tb}",
            duration_s=duration,
        )
        _print_diagnostics(result, db)
        return result

    finally:
        try:
            story.cleanup(client, db)
        except Exception as exc:
            print(f"  ! Cleanup error in {story.name}: {exc}")


def _print_diagnostics(result: StoryResult, db: DBInspector) -> None:
    """Print rich DB state to help an agent diagnose and fix failures."""
    print("\n  ┌─ DIAGNOSTIC ─────────────────────────────────────────────────┐")

    # Inline diagnostics from AssertionFailure
    if result.diagnostics:
        for k, v in result.diagnostics.items():
            val_str = str(v)[:300].replace("\n", "↵")
            print(f"  │  {k}: {val_str}")
        print("  │")

    # Conversation messages (if conversation_id is known)
    conv_id = result.diagnostics.get("conversation_id")
    if conv_id and db is not None:
        try:
            msgs = db.get_messages(conv_id)
            print(f"  │  Conversation {conv_id} messages ({len(msgs)} total):")
            for line in db.format_messages_table(msgs).splitlines():
                print(f"  │ {line}")
            print("  │")
        except Exception as e:
            print(f"  │  [Could not read conversation messages: {e}]")

    # Recent arcs
    try:
        arcs = db.get_arcs(limit=30)
        print(f"  │  Recent Arcs (last {len(arcs)}, newest first):")
        for line in db.format_arcs_table(arcs).splitlines():
            print(f"  │ {line}")
    except Exception as e:
        print(f"  │  [Could not read arcs: {e}]")

    print("  │")

    # Work queue
    try:
        wq = db.get_work_queue(limit=15)
        print(f"  │  Work Queue (last {len(wq)}):")
        if wq:
            for item in wq:
                payload_preview = str(item.get("payload_json", ""))[:80]
                print(f"  │    [{item['id']}] {str(item.get('event_type','?')):<30} {payload_preview}")
        else:
            print("  │    (empty)")
    except Exception as e:
        print(f"  │  [Could not read work_queue: {e}]")

    print("  └──────────────────────────────────────────────────────────────┘\n")


# ---------------------------------------------------------------------------
# Post-suite workspace cleanup
# ---------------------------------------------------------------------------


def _sweep_orphaned_workspaces(workspaces_dir: str, run_start_time: float) -> None:
    """Remove workspace dirs created after run_start_time (orphans from crashes)."""
    ws_path = Path(workspaces_dir)
    if not ws_path.is_dir():
        return
    removed = []
    for entry in ws_path.iterdir():
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_ctime >= run_start_time:
                shutil.rmtree(entry)
                removed.append(entry.name)
        except OSError:
            pass
    if removed:
        print(f"  Swept {len(removed)} orphaned workspace(s):")
        for name in removed:
            print(f"    - {name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    run_start_time = time.time()
    args = sys.argv[1:]
    prefix_filter = args if args else None

    print("\nCarpenter Acceptance Test Runner")
    print("=" * 70)

    cfg = _load_config()
    print(f"  URL:   {cfg['url']}")
    print(f"  DB:    {cfg['db_path'] or '(not found)'}")
    print(f"  Token: {'set' if cfg.get('token') else 'NOT SET — protected endpoints will 401'}")
    print()

    if not ensure_server_running(cfg):
        print("\n  Cannot run tests: server is not available.")
        return 1

    if not cfg.get("db_path"):
        print("\n  WARNING: DB path not found. DB assertions will fail.")

    client = CarpenterClient(cfg["url"], cfg.get("token"))
    db = DBInspector(cfg["db_path"]) if cfg.get("db_path") else None

    stories = discover_stories(prefix_filter)
    if not stories:
        label = str(prefix_filter) if prefix_filter else "any"
        print(f"\n  No stories found matching: {label}")
        print(f"  Story files live in: {Path(__file__).parent}/")
        return 1

    print(f"\n  Running {len(stories)} story/stories...\n")
    results = [run_story(s, client, db) for s in stories]

    # Summary
    print(f"\n{'━'*70}")
    print("  SUMMARY")
    print(f"{'━'*70}")
    skipped = sum(1 for r in results if r.skipped)
    passed = sum(1 for r in results if r.passed and not r.skipped)
    failed = sum(1 for r in results if not r.passed)
    for r in results:
        if r.skipped:
            icon = "⊝"
        elif r.passed:
            icon = "✓"
        else:
            icon = "✗"
        print(f"  {icon}  {r.name}  ({r.duration_s:.1f}s)")
        if r.skipped and r.message:
            print(f"       → skipped: {r.message}")
        elif not r.passed:
            first_line = r.error.splitlines()[0] if r.error else "unknown"
            print(f"       → {first_line}")
    print()
    skip_suffix = f", {skipped} skipped" if skipped else ""
    print(
        f"  {passed}/{len(results) - skipped} passed"
        f"{skip_suffix}"
        + ("  🎉" if failed == 0 else "")
    )
    print()

    _sweep_orphaned_workspaces(cfg["workspaces_dir"], run_start_time)

    if failed > 0:
        return 1
    # Exit code 77 is the autotools/Automake convention for "skipped",
    # letting outer test runners (e.g., run-comprehensive-tests.sh)
    # distinguish "not applicable" from "broken".
    if skipped > 0 and passed == 0:
        return 77
    return 0


if __name__ == "__main__":
    sys.exit(main())
