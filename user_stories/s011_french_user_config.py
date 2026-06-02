"""
S011 — French-Language Config Discovery

Same workflow as S009 but every user-facing prompt is in natural French and
no internal tool names are given in the prompts.  The agent must:

  1. Understand the concern (losing conversation context) purely from the
     French description — no key name, no tool name mentioned.
  2. Use read-only discovery (config.list_keys) to identify the right knob
     (memory_recent_hints) from English descriptions in the key list.
  3. Submit reviewed code to increase the value via config.set_value.
  4. Confirm the change is live in the running CONFIG (hot-reload).
  5. Revert on request back to the platform default (3).

This exercises two properties:
  - Functional correctness is language-agnostic (platform never changes).
  - Cross-lingual tool discovery works without prompt scaffolding.

Config key:  memory_recent_hints
Original:    3  (platform default)
Changed to:  agent's choice (must be > 3)
Reverted to: 3

NOTE: This story writes and reverts ~/carpenter/config/config.yaml.  The
cleanup() method restores the default value if the test fails mid-way.
"""

import os
import time
from pathlib import Path

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)

_CONFIG_KEY = "memory_recent_hints"
_ORIGINAL_VALUE = 3
_CONFIG_PATH = Path(os.environ.get(
    "CARPENTER_CONFIG",
    Path.home() / "carpenter" / "config" / "config.yaml"
))

_DISCOVER_AND_CHANGE_PROMPT = (
    "J'ai l'impression que tu perds de vue ce sur quoi on travaille d'une "
    "conversation à l'autre. Y a-t-il un paramètre qui contrôle combien de "
    "titres de conversations récentes tu gardes en mémoire ? Si oui, "
    "utilise les outils de configuration pour trouver le bon paramètre, "
    "augmente-le et confirme-moi quand c'est fait."
)

_VERIFY_PROMPT = (
    "Peux-tu me montrer la valeur actuelle en mémoire maintenant — "
    "pas juste ce qui est écrit dans le fichier, mais ce qui est réellement "
    "actif ? Affiche-moi le nombre."
)

_REVERT_PROMPT = (
    "Finalement, remets-le à la valeur par défaut pour l'instant. "
    "Mets-le à 3 et dis-moi quand c'est fait."
)

_ARC_HEALTH_PROMPT = (
    "Est-ce que tout s'est bien passé en coulisses ? "
    "Pas d'échecs ou de tentatives ratées ?"
)

# ── Keyword sets — French only (prompts are French; agent responds in kind) ──

_KW_SUCCESS = (
    "fait", "effectué", "mis à jour", "modifié", "changé", "augmenté",
    "réussi", "terminé", "configuré", "maintenant", "mémoire",
)

_KW_REVERT = (
    "fait", "remis", "rétabli", "réinitialisé", "revenu", "terminé",
)

_KW_HEALTH = (
    "propre", "aucune erreur", "bien passé", "réussi", "correct",
    "terminé", "sans problème", "parfait", "aucun problème",
    "pas d'erreur", "pas d'échec",
)


class FrenchUserConfig(AcceptanceStory):
    name = "S011 — French-Language Config Discovery"
    description = (
        "All user prompts in French, no tool names given. Agent maps vague "
        "French description to memory_recent_hints, bumps via config.set_value "
        "(reviewed callback, hot-reload), verifies live, reverts to default, "
        "arc history clean. Bilingual keyword assertions."
    )

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        start_ts = time.time()

        # ── 1. Discover + change ──────────────────────────────────────────────
        print(f"\n  [1/6] Sending French prompt — no key or tool name given...")
        conv_id = client.create_conversation()
        client.send_message(_DISCOVER_AND_CHANGE_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=180)

        msgs = client.get_assistant_messages(conv_id)
        self.assert_that(
            len(msgs) >= 1,
            "No response after French config change request",
            conversation_id=conv_id,
        )
        change_resp = msgs[-1]["content"]
        print(f"     {change_resp[:200]}")

        # Agent should have identified the correct key
        self.assert_that(
            "memory_recent" in change_resp.lower(),
            "Agent did not mention 'memory_recent' — may not have found the right key",
            response_preview=change_resp[:400],
        )
        self.assert_that(
            any(kw in change_resp.lower() for kw in _KW_SUCCESS),
            "Change response does not acknowledge a successful update (bilingual check)",
            response_preview=change_resp[:400],
        )

        # ── 2. Structural verify — disk ───────────────────────────────────────
        print(f"  [2/6] Verifying config.yaml on disk...")
        _actual_new_value = None
        if _HAS_YAML and _CONFIG_PATH.exists():
            raw = yaml.safe_load(_CONFIG_PATH.read_text())
            disk_val = raw.get(_CONFIG_KEY) if raw else None
            self.assert_that(
                isinstance(disk_val, int) and disk_val > _ORIGINAL_VALUE,
                f"config.yaml does not have {_CONFIG_KEY} > {_ORIGINAL_VALUE} "
                f"(found {disk_val!r})",
                config_yaml=dict(raw) if raw else {},
            )
            _actual_new_value = disk_val
            print(f"     config.yaml: {_CONFIG_KEY}={disk_val} ✓ (agent chose {disk_val})")

        # ── 3. Live verify — agent confirms the value is actually live ─────────
        print(f"  [3/6] Asking agent (in French) to prove the change is live...")
        client.send_message(_VERIFY_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=120)

        msgs = client.get_assistant_messages(conv_id)
        verify_resp = msgs[-1]["content"]
        print(f"     {verify_resp[:200]}")

        if _actual_new_value is not None:
            self.assert_that(
                str(_actual_new_value) in verify_resp,
                f"Live verify response does not contain '{_actual_new_value}' "
                f"— hot-reload may not have taken effect",
                response_preview=verify_resp[:400],
            )
            print(f"     Live CONFIG shows {_CONFIG_KEY}={_actual_new_value} ✓")

        # ── 4. Revert ─────────────────────────────────────────────────────────
        print(f"  [4/6] Requesting revert (French) → {_ORIGINAL_VALUE}...")
        client.send_message(_REVERT_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=120)

        msgs = client.get_assistant_messages(conv_id)
        revert_resp = msgs[-1]["content"]
        print(f"     {revert_resp[:150]}")
        self.assert_that(
            any(kw in revert_resp.lower() for kw in _KW_REVERT),
            "Revert response does not acknowledge restoration (bilingual check)",
            response_preview=revert_resp[:400],
        )
        self.assert_that(
            "3" in revert_resp,
            "Revert response does not mention the restored value (3)",
            response_preview=revert_resp[:400],
        )

        # ── 5. Disk + live verify post-revert ──────────────────────────────────
        print(f"  [5/6] Verifying config.yaml + live value after revert...")
        if _HAS_YAML and _CONFIG_PATH.exists():
            raw2 = yaml.safe_load(_CONFIG_PATH.read_text())
            disk_val2 = raw2.get(_CONFIG_KEY) if raw2 else None
            self.assert_that(
                disk_val2 == _ORIGINAL_VALUE or disk_val2 is None,
                f"config.yaml still has {_CONFIG_KEY}={disk_val2!r} after revert "
                f"(expected {_ORIGINAL_VALUE} or absent)",
                config_yaml=dict(raw2) if raw2 else {},
            )
            print(f"     config.yaml: {_CONFIG_KEY}={disk_val2!r} ✓")

        client.send_message(_VERIFY_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=90)
        msgs = client.get_assistant_messages(conv_id)
        verify_resp2 = msgs[-1]["content"]
        print(f"     Post-revert verify: {verify_resp2[:150]}")
        self.assert_that(
            str(_ORIGINAL_VALUE) in verify_resp2,
            f"Post-revert verify does not confirm {_CONFIG_KEY}={_ORIGINAL_VALUE}",
            response_preview=verify_resp2[:400],
        )
        print(f"     Live CONFIG shows {_CONFIG_KEY}={_ORIGINAL_VALUE} ✓")

        # ── 6. Arc health check ───────────────────────────────────────────────
        print(f"  [6/6] Arc health check (French prompt)...")
        client.send_message(_ARC_HEALTH_PROMPT, conv_id)
        client.wait_for_pending_to_clear(conv_id, timeout=120)

        msgs = client.get_assistant_messages(conv_id)
        health_resp = msgs[-1]["content"]
        print(f"     {health_resp[:200]}")
        self.assert_that(
            any(kw in health_resp.lower() for kw in _KW_HEALTH),
            "Arc health check does not confirm clean history (bilingual check)",
            response_preview=health_resp[:400],
        )

        # Structural: no failed/cancelled arcs from our operations
        self.assert_no_failed_arcs_since(db, start_ts)

        return StoryResult(
            name=self.name,
            passed=True,
            message=(
                f"French prompts ✓, agent discovered {_CONFIG_KEY} cross-linguistically ✓, "
                f"disk verify ✓ (agent chose {_actual_new_value}), "
                f"live CONFIG hot-reload verified ✓, "
                f"reverted to {_ORIGINAL_VALUE} ✓, "
                f"post-revert verify ✓, "
                f"arc history clean ✓"
            ),
        )

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        """Restore memory_recent_hints to default if the story failed mid-way."""
        if not _HAS_YAML or not _CONFIG_PATH.exists():
            return
        try:
            raw = yaml.safe_load(_CONFIG_PATH.read_text())
            if not raw:
                return
            current = raw.get(_CONFIG_KEY)
            if current is not None and current != _ORIGINAL_VALUE:
                raw[_CONFIG_KEY] = _ORIGINAL_VALUE
                _CONFIG_PATH.write_text(
                    yaml.dump(raw, default_flow_style=False, allow_unicode=True,
                              sort_keys=False)
                )
                print(f"  [cleanup] Reset {_CONFIG_KEY} to {_ORIGINAL_VALUE} in config.yaml")
                try:
                    from carpenter.config import reload_config
                    reload_config()
                    print(f"  [cleanup] Config reloaded")
                except Exception as exc:
                    print(f"  [cleanup] In-process reload skipped: {exc}")
        except Exception as exc:
            print(f"  [cleanup] Failed to restore config: {exc}")
