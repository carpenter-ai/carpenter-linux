"""
S040 — Ollama Tool Calling Smoke Test (per-conversation pin, naturalized)

Tests that the agent can, on a natural user request, switch a SINGLE
conversation over to an Ollama-protocol backend without touching the
server's global ``ai_provider`` default, and then successfully call a
tool on the new backend.

The story reaches the Ollama backend by asking the chat agent, via a
natural prompt, to pin this conversation to a specific provider/model.
That should invoke the ``set_conversation_model`` chat tool which
writes to the ``conversations`` row (see KB article
``ai/per-conversation-model``). The global config is NOT modified.

Prerequisites:
  - The running Carpenter server has a reachable Ollama-protocol endpoint.
    Configure ``ollama_url`` in ``config.yaml`` once; the story does NOT
    assume the server's default provider is ollama.
  - The model the agent should pin to must be supplied. The story
    consults, in order: the ``OLLAMA_MODEL`` env var, then the
    ``ollama_model`` key in ``config.yaml``. If neither is present
    the story skips — the platform does not ship a default LLM.

Usage:
    python -m user_stories.runner --story s040
"""

import os
import sqlite3
import time
from pathlib import Path

import pytest

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_SWITCH_PROMPT_TEMPLATE = (
    "For just THIS conversation, please switch us over to using the "
    "Ollama model {model} running on my desktop. Keep the default "
    "provider/model for other conversations unchanged — I don't want "
    "you to edit config.yaml."
)

# Second prompt: force a tool invocation on the new backend. We ask the
# model to search the knowledge base — that maps cleanly to a single
# ``kb_search`` tool call, which small tool-capable models (e.g.,
# qwen3:8b) can handle in one step. Avoid asking for arithmetic: the
# ultra-core tool set has no calculator, and the reviewer on
# ``submit_code`` rejects trivial "print(...)" code, causing loops.
_TOOL_PROMPT = (
    "Great. Now please search your knowledge base for articles about "
    "\"chat\" and tell me which articles you find. Use the knowledge "
    "base tool to look this up rather than answering from memory."
)

# Timeout multiplier for slow Ollama inference.
_TIMEOUT_MULT = 6


class _SlowClient(CarpenterClient):
    """CarpenterClient with longer timeouts for Ollama backends."""

    def wait_for_pending_to_clear(
        self, conversation_id: int, timeout: int = 60, poll_interval: float = 0.5
    ) -> None:
        return super().wait_for_pending_to_clear(
            conversation_id, timeout=timeout * _TIMEOUT_MULT,
            poll_interval=poll_interval,
        )

    def wait_for_n_assistant_messages(
        self, conversation_id: int, n: int, timeout: int = 120,
        poll_interval: float = 1.0,
    ) -> list[dict]:
        return super().wait_for_n_assistant_messages(
            conversation_id, n, timeout=timeout * _TIMEOUT_MULT,
            poll_interval=poll_interval,
        )


class OllamaToolCalling(AcceptanceStory):
    name = "S040 — Ollama per-conversation pin + tool calling"
    description = (
        "Asks the chat agent in natural language to switch THIS "
        "conversation to an Ollama-protocol backend (no global config "
        "change), then asks it to search the knowledge base. Verifies "
        "the conversation override was recorded, the global config "
        "was NOT touched, and kb_search was invoked on turn 2."
    )
    timeout = 600

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()
        model = self._resolve_ollama_model()
        if not model:
            pytest.skip(
                "requires OLLAMA_MODEL env var or ollama_model key in "
                "config.yaml — the platform does not ship a default LLM"
            )
        print(f"\n  Target Ollama model: {model}")

        slow_client = _SlowClient(
            client.base_url,
            token=client._token,
            timeout=client._default_timeout,
        )

        # Snapshot global config (by content + mtime) so we can later
        # assert the agent did NOT mutate it.
        config_path = self._locate_config_yaml()
        config_snapshot = None
        if config_path is not None:
            config_snapshot = (
                config_path.stat().st_mtime_ns,
                config_path.read_bytes(),
            )
            print(f"  Global config: {config_path} (snapshotted)")
        self._config_path = config_path
        self._config_snapshot = config_snapshot
        self._conv_id: int | None = None

        # ── Turn 1: ask the agent to pin this conversation ────────────
        print("\n  [1/3] Asking agent to pin this conversation to Ollama...")
        conv_id, resp1 = slow_client.chat(
            _SWITCH_PROMPT_TEMPLATE.format(model=model),
            timeout=120,
        )
        self._conv_id = conv_id
        print(f"  Turn 1 response ({len(resp1)} chars): {resp1[:240]!r}")

        # Verify the override was actually recorded in the DB.
        override_row = self._fetch_conv_override(db, conv_id)
        print(f"  conversations row override: {override_row}")
        self.assert_that(
            override_row is not None,
            f"Conversation #{conv_id} not found in DB after turn 1",
        )
        self.assert_that(
            (override_row.get("ai_provider") or "").lower() == "ollama",
            "Agent did not pin conversation to ollama. "
            f"ai_provider={override_row.get('ai_provider')!r}",
            response_preview=resp1[:400],
        )
        # Verify the pinned model matches what we asked for. We check
        # for substring containment rather than equality to tolerate the
        # agent normalizing the tag (e.g. ``qwen3:8b`` vs ``qwen3:8b-instruct``).
        pinned_model = (override_row.get("model") or "").lower()
        self.assert_that(
            bool(pinned_model) and model.split(":")[0].lower() in pinned_model,
            f"Agent did not pin to the requested model {model!r}. "
            f"model={override_row.get('model')!r}",
            response_preview=resp1[:400],
        )

        # Verify global config NOT modified.
        if config_snapshot is not None and config_path is not None:
            after_mtime = config_path.stat().st_mtime_ns
            after_bytes = config_path.read_bytes()
            self.assert_that(
                after_mtime == config_snapshot[0]
                and after_bytes == config_snapshot[1],
                "Global config.yaml was modified — the agent was "
                "supposed to leave it alone and only pin THIS conversation.",
            )
            print("  Global config.yaml unchanged — good.")

        # ── Turn 2: ask for a tool-requiring computation ──────────────
        print("\n  [2/3] Asking a tool-requiring question...")
        tool_calls_before = self._count_tool_calls(db, conv_id)
        _, resp2 = slow_client.chat(
            _TOOL_PROMPT, conversation_id=conv_id, timeout=240,
        )
        print(f"  Turn 2 response ({len(resp2)} chars): {resp2[:300]!r}")

        tool_calls_after = self._count_tool_calls(db, conv_id)
        tool_calls_delta = tool_calls_after - tool_calls_before
        print(f"  Tool calls during turn 2: {tool_calls_delta}")
        self.assert_that(
            tool_calls_delta >= 1,
            "Turn 2 did not invoke any tools — the agent answered from "
            "memory instead of using a tool as instructed.",
            response_preview=resp2[:400],
        )

        # ── Turn 3: verify the tool was actually kb_search ────────────
        print("\n  [3/3] Verifying kb_search was the tool invoked...")
        kb_search_calls = self._count_tool_calls_by_name(
            db, conv_id, "kb_search"
        )
        self.assert_that(
            kb_search_calls >= 1,
            "Turn 2 invoked tools but none were kb_search — the model "
            "reached for a different tool than asked.",
            response_preview=resp2[:400],
        )

        duration = time.time() - start_ts
        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"Pinned conv #{conv_id} to "
                f"{override_row.get('ai_provider')}:{override_row.get('model')}; "
                f"tool calls on turn 2: {tool_calls_delta}; "
                f"global config unchanged."
            ),
            diagnostics={
                "conversation_id": conv_id,
                "override": override_row,
                "tool_calls_turn2": tool_calls_delta,
            },
            duration_s=duration,
        )

    # ── Helpers ────────────────────────────────────────────────────────

    @classmethod
    def _resolve_ollama_model(cls) -> str | None:
        """Find the Ollama model the user wants the agent to pin to.

        Priority: ``OLLAMA_MODEL`` env var → ``ollama_model`` key in
        the server's ``config.yaml``. Returns ``None`` if neither is
        present — the platform ships no default LLM.
        """
        env_model = os.environ.get("OLLAMA_MODEL", "").strip()
        if env_model:
            return env_model
        config_path = cls._locate_config_yaml()
        if config_path is None:
            return None
        try:
            for raw in config_path.read_text().splitlines():
                line = raw.split("#", 1)[0].rstrip()
                if not line or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                if key.strip() == "ollama_model":
                    return value.strip().strip('"').strip("'") or None
        except OSError:
            return None
        return None

    @staticmethod
    def _locate_config_yaml():
        # Honour CARPENTER_CONFIG override first; fall back to the
        # home-relative default.  No install-specific paths.
        env_override = os.environ.get("CARPENTER_CONFIG", "").strip()
        candidates: list[Path] = []
        if env_override:
            candidates.append(Path(env_override))
        candidates.append(Path.home() / "carpenter" / "config" / "config.yaml")
        for cand in candidates:
            if cand.is_file():
                return cand
        return None

    @staticmethod
    def _fetch_conv_override(db: DBInspector, conv_id: int) -> dict | None:
        rows = db._query(
            "SELECT ai_provider, model FROM conversations WHERE id = ?",
            (conv_id,),
        )
        return rows[0] if rows else None

    @staticmethod
    def _count_tool_calls(db: DBInspector, conv_id: int) -> int:
        rows = db._query(
            "SELECT COUNT(*) AS cnt FROM tool_calls WHERE conversation_id = ?",
            (conv_id,),
        )
        return int(rows[0]["cnt"]) if rows else 0

    @staticmethod
    def _count_tool_calls_by_name(
        db: DBInspector, conv_id: int, tool_name: str
    ) -> int:
        rows = db._query(
            "SELECT COUNT(*) AS cnt FROM tool_calls "
            "WHERE conversation_id = ? AND tool_name = ?",
            (conv_id, tool_name),
        )
        return int(rows[0]["cnt"]) if rows else 0

    def cleanup(self, client: CarpenterClient, db: DBInspector) -> None:
        """Clear the per-conversation pin and archive the test conversation.

        Scoped narrowly by conversation id: does NOT touch any other
        conversations or use broad pattern deletes.
        """
        conv_id = getattr(self, "_conv_id", None)
        if conv_id is None or db is None:
            return
        try:
            conn = sqlite3.connect(db.db_path)
            try:
                conn.execute(
                    "UPDATE conversations "
                    "SET ai_provider = NULL, model = NULL, archived = 1 "
                    "WHERE id = ?",
                    (conv_id,),
                )
                conn.commit()
                print(f"  [cleanup] Cleared pin on conversation #{conv_id} "
                      "and archived it.")
            finally:
                conn.close()
        except Exception as exc:
            print(f"  [cleanup] Cleanup failed for conv #{conv_id}: {exc}")

        if (
            getattr(self, "_config_snapshot", None) is not None
            and getattr(self, "_config_path", None) is not None
        ):
            try:
                now_bytes = self._config_path.read_bytes()
                if now_bytes != self._config_snapshot[1]:
                    print(
                        "  [cleanup] WARNING: global config.yaml changed "
                        "during the story run."
                    )
            except Exception:
                pass
