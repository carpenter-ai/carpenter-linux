"""
S022 — Iterative Bug Fix (Ralph Loop)

The user reports a bug in a Celsius-to-Fahrenheit converter that produces
wrong results for negative numbers. The agent uses the impl+monitor sibling
arc pattern to iterate on the fix: implement, test, detect failure, revise,
until all test cases pass.

Formula: F = C * 9/5 + 32
Test cases: 0->32, 100->212, -40->-40, 37->98.6

Expected behaviour:
  1. User reports the bug with test cases.
  2. Agent creates a coding-change arc to fix the implementation.
  3. Agent may iterate (multiple impl+monitor cycles) to get all test
     cases passing.
  4. The final implementation correctly handles negative numbers.
  5. Story approves the diff and verifies correctness.

This is a long-running story (300s+ timeout) due to iteration.

DB verification:
  - At least one coding-change arc created.
  - The arc reaches 'waiting' with a diff for review.
  - After approval, the arc completes.

Built on the ``ChangeReviewStory`` scaffold — the request → review →
approve → terminal sequence lives in the base class. We only customise
the diff-content assertion and the on-disk cleanup.
"""

from __future__ import annotations

import os
from pathlib import Path

from user_stories.framework import (
    CarpenterClient,
    ChangeReviewStory,
    DBInspector,
)

_TOOL_NAME = "celsius_to_fahrenheit"

_BUG_REPORT_PROMPT = (
    "I have a bug in my temperature converter. The Celsius to Fahrenheit "
    "conversion is broken for negative numbers. The correct formula is "
    "F = C * 9/5 + 32. Please create a tool called 'celsius_to_fahrenheit' "
    "that correctly handles all these test cases:\n"
    "  - 0C -> 32F\n"
    "  - 100C -> 212F\n"
    "  - -40C -> -40F\n"
    "  - 37C -> 98.6F\n"
    "Use the coding-change workflow to add it as a read tool in "
    "carpenter_tools/read/celsius_to_fahrenheit.py."
)


class IterativeBugFix(ChangeReviewStory):
    name = "S022 — Iterative Bug Fix (Ralph Loop)"
    description = (
        "User reports C->F conversion bug for negative numbers; agent iterates "
        "with impl+monitor pattern until all test cases pass; 300s+ timeout."
    )
    artifact_prefix = "s022"

    # The agent's initial response usually mentions the domain
    # (temperature/celsius/fahrenheit) or the work it's about to start.
    ack_keywords = (
        "celsius", "fahrenheit", "temperature", "convert", "fix",
        "coding", "implement", "tool", "change", "arc", "add", "work",
    )

    request_text = _BUG_REPORT_PROMPT
    approve_comment = "Looks correct. All test cases should pass."

    # Iterative impl+monitor loop can take longer than the default.
    review_wait_timeout = 300
    terminal_wait_timeout = 120

    def __init__(self) -> None:
        self._source_dir: str | None = None

    # ── Hooks ────────────────────────────────────────────────────────

    def inspect_diff(self, diff: str, arc_state: dict) -> None:
        super().inspect_diff(diff, arc_state)

        # Remember the source dir so cleanup() can find the artifact
        # regardless of where the platform is rooted.
        source_dir = arc_state.get("source_dir", "")
        if source_dir:
            self._source_dir = source_dir

        diff_lower = diff.lower()
        self.assert_that(
            any(kw in diff_lower for kw in ("celsius", "fahrenheit", "9/5", "1.8", "32")),
            "Diff does not contain temperature conversion logic",
            diff_preview=diff[:600],
        )

    # ── Cleanup ──────────────────────────────────────────────────────

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Remove any celsius_to_fahrenheit artifacts the agent created.

        Glob-based so we catch variants like ``celsius_to_fahrenheit_tool.py``
        or files dropped in ``tool_backends/`` / ``act/`` in addition to the
        canonical ``carpenter_tools/read/`` location. Also strips any matching
        lines from ``carpenter/api/callbacks.py`` in case the agent registered
        the tool there.
        """
        root = (
            Path(self._source_dir) if self._source_dir
            else Path(os.environ.get(
                "CARPENTER_SOURCE_DIR",
                str(Path(__file__).resolve().parents[1])
            ))
        )

        # Glob the likely homes for a tool file: read/, act/, tool_backends/,
        # plus the package-root config_seed/chat_tools/ used by some variants.
        scan_dirs = (
            root / "carpenter_tools" / "read",
            root / "carpenter_tools" / "act",
            root / "carpenter" / "tool_backends",
            root / "config_seed" / "chat_tools",
        )
        removed: list[Path] = []
        for d in scan_dirs:
            if not d.is_dir():
                continue
            for pat in (f"{_TOOL_NAME}.py", f"{_TOOL_NAME}_*.py", f"*{_TOOL_NAME}*.py"):
                for match in d.glob(pat):
                    try:
                        match.unlink()
                        removed.append(match)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  [cleanup] Could not remove {match}: {exc}")
        if removed:
            print(
                f"  [cleanup] Removed {len(removed)} artifact(s): "
                f"{[str(p) for p in removed]}"
            )

        # Strip any references from callbacks.py (some agents register the
        # tool there in addition to dropping the file).
        callbacks_path = root / "carpenter" / "api" / "callbacks.py"
        if callbacks_path.exists():
            try:
                original = callbacks_path.read_text()
                filtered = "\n".join(
                    line for line in original.splitlines()
                    if _TOOL_NAME not in line
                )
                if original.endswith("\n"):
                    filtered += "\n"
                if filtered != original:
                    callbacks_path.write_text(filtered)
                    print(f"  [cleanup] Removed {_TOOL_NAME} references from callbacks.py")
            except Exception as exc:  # noqa: BLE001
                print(f"  [cleanup] Could not clean callbacks.py: {exc}")
