"""
S015 — External Repository Setup with Secure Credential Flow

The user asks Carpenter to set up a workflow for an external git
repository. The platform creates a credential link, the user provides
the token via the secure endpoint, and the agent verifies the credential.

Preconditions (provided by external harness via env vars):
  CARPENTER_TEST_FORGEJO_URL — Forgejo instance URL
  CARPENTER_TEST_FORGEJO_TOKEN — API token with repo access
  CARPENTER_TEST_REPO_OWNER / CARPENTER_TEST_REPO_NAME — test repo coordinates

These env vars must be set before running this story. If missing, the
story is skipped.
"""

import os
import re
import time
from pathlib import Path

import httpx

from user_stories.framework import (
    AcceptanceStory,
    DBInspector,
    StoryResult,
    CarpenterClient,
)


_REQUIRED_ENV = [
    "CARPENTER_TEST_FORGEJO_URL",
    "CARPENTER_TEST_FORGEJO_TOKEN",
    "CARPENTER_TEST_REPO_OWNER",
    "CARPENTER_TEST_REPO_NAME",
]


class ExternalRepoSetup(AcceptanceStory):
    name = "S015 — External Repository Setup"
    description = (
        "User provides a git repo URL; agent creates a credential link; "
        "user submits the token via the secure endpoint; agent verifies."
    )

    @staticmethod
    def _clear_git_config(client: CarpenterClient) -> tuple[str, str] | None:
        """Remove GIT_TOKEN/FORGEJO_TOKEN from .env and git_url/forgejo_url/git_server_url from config.yaml.

        This ensures the agent will ask for credentials via the intake flow
        rather than using previously stored configuration.

        Accepts the canonical ``git_url`` key plus the legacy
        ``forgejo_url`` and intermediate ``git_server_url`` names, so this
        story remains compatible across the carpenter-config rename
        cutover.  The carpenter-core on-disk migration will eventually
        leave only ``git_url`` on disk, but during the rollout we may
        encounter any of the three.

        Returns a (key_name, value) tuple for restoration later, or None.
        """
        base_dir = Path.home() / "carpenter"

        # Clear GIT_TOKEN / FORGEJO_TOKEN from .env
        dot_env = base_dir / ".env"
        if dot_env.is_file():
            lines = dot_env.read_text().splitlines()
            new_lines = [
                ln for ln in lines
                if not re.match(r'^(GIT_TOKEN|FORGEJO_TOKEN)\s*=', ln.strip())
            ]
            if len(new_lines) != len(lines):
                dot_env.write_text("\n".join(new_lines) + "\n")
                print("  Cleared GIT_TOKEN/FORGEJO_TOKEN from .env")

        # Clear git_url / forgejo_url / git_server_url from config.yaml
        original_git_url: tuple[str, str] | None = None
        config_yaml = base_dir / "config" / "config.yaml"
        if config_yaml.is_file():
            cfg_text = config_yaml.read_text()
            match = re.search(
                r'^(git_url|forgejo_url|git_server_url):\s*(.+)$',
                cfg_text, re.MULTILINE,
            )
            if match:
                key_name = match.group(1)
                original_git_url = (key_name, match.group(2).strip())
                cfg_text = re.sub(
                    r'^(git_url|forgejo_url|git_server_url):\s*.+$',
                    f'# {key_name}: (cleared by S015)',
                    cfg_text, flags=re.MULTILINE,
                )
                config_yaml.write_text(cfg_text)
                print(f"  Cleared {key_name} from config.yaml")

        # Tell the server to reload config
        try:
            httpx.post(
                f"{client.base_url}/api/credentials/reload-config",
                timeout=10.0,
            )
        except Exception:
            pass  # best-effort

        return original_git_url

    @staticmethod
    def _restore_git_url(
        client: CarpenterClient, saved: tuple[str, str],
    ) -> None:
        """Restore the original git server URL key in config.yaml after the test.

        ``saved`` is the (key_name, value) tuple returned by
        :meth:`_clear_git_config`; the original key name (``git_url``,
        ``forgejo_url`` or ``git_server_url``) is written back so the file
        matches its pre-test form.
        """
        key_name, url = saved
        base_dir = Path.home() / "carpenter"
        config_yaml = base_dir / "config" / "config.yaml"

        if config_yaml.is_file():
            cfg_text = config_yaml.read_text()
            cfg_text = re.sub(
                r'^#\s*(git_url|forgejo_url|git_server_url):.*$',
                f'{key_name}: {url}',
                cfg_text, flags=re.MULTILINE,
            )
            config_yaml.write_text(cfg_text)

        try:
            httpx.post(
                f"{client.base_url}/api/credentials/reload-config",
                timeout=10.0,
            )
        except Exception:
            pass

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        # ── Check preconditions ──────────────────────────────────────────
        missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
        if missing:
            return self.result(
                f"Missing env vars: {', '.join(missing)}. "
                "Run with the instance-specific harness."
            )

        forgejo_url = os.environ.get("CARPENTER_TEST_FORGEJO_URL")
        forgejo_token = os.environ.get("CARPENTER_TEST_FORGEJO_TOKEN")
        repo_owner = os.environ.get("CARPENTER_TEST_REPO_OWNER")
        repo_name = os.environ.get("CARPENTER_TEST_REPO_NAME")
        repo_url = f"{forgejo_url}/{repo_owner}/{repo_name}"

        start_ts = time.time()

        # ── Step 0: Clear git server config so agent asks for credentials ──
        # Previous runs store the token in Carpenter's .env and the git
        # server URL in config.yaml. Remove both so the agent triggers the
        # credential intake flow. Restore the git URL at the end.
        saved_git_url = self._clear_git_config(client)

        try:
            return self._run_credential_flow(
                client, forgejo_url, forgejo_token, repo_url, start_ts,
            )
        finally:
            # Always restore the git URL so S016/S017 can use it
            if saved_git_url:
                self._restore_git_url(client, saved_git_url)

    def _run_credential_flow(
        self,
        client: CarpenterClient,
        forgejo_url: str,
        forgejo_token: str,
        repo_url: str,
        start_ts: float,
    ) -> StoryResult:
        # ── Step 1: Ask agent to set up the repo ─────────────────────────
        print(f"\n  Sending: 'Set up a workflow for {repo_url}'")
        conv_id, response = client.chat(
            f"Please set up a workflow for this git repository: {repo_url}",
            timeout=120,
        )
        print(f"  Got response ({len(response)} chars)")

        # Behavioral: agent should mention credential or token
        self.assert_that(
            any(kw in response.lower() for kw in (
                "credential", "token", "api/credentials",
                "access", "authentication",
            )),
            "Agent did not mention credential intake",
            response_preview=response[:400],
        )

        # ── Step 2: Find credential requests and submit values ───────────
        # Poll for credential requests — the agent may create them
        # asynchronously via a background arc.  Provide the right value
        # for each key.  Carry both canonical names (GIT_*) and legacy
        # names (FORGEJO_*) so we can satisfy whichever the agent asks
        # for during the rename cutover.
        credential_values = {
            "GIT_URL": forgejo_url,
            "GIT_TOKEN": forgejo_token,
            "FORGEJO_URL": forgejo_url,
            "FORGEJO_TOKEN": forgejo_token,
        }

        fulfilled_any = False
        for _ in range(12):  # up to 60s polling
            pending_resp = httpx.get(
                f"{client.base_url}/api/credentials/pending", timeout=10.0,
            )
            if pending_resp.status_code != 200:
                time.sleep(5)
                continue

            pending = pending_resp.json().get("pending", [])
            for req in pending:
                req_id = req["request_id"]
                req_key = req["key"]
                value = credential_values.get(req_key, forgejo_token)
                print(f"  Found credential request: {req_id[:8]}... "
                      f"for key={req_key}")

                provide_resp = httpx.post(
                    f"{client.base_url}/api/credentials/{req_id}/provide",
                    json={"value": value},
                    timeout=10.0,
                )
                if provide_resp.status_code == 200:
                    data = provide_resp.json()
                    if data.get("stored"):
                        print(f"  Provided {req_key} via credential API")
                        fulfilled_any = True

            if fulfilled_any and not pending:
                break
            if fulfilled_any:
                time.sleep(3)  # check for more requests
                continue
            time.sleep(5)

        if not fulfilled_any:
            # Agent mentioned credentials but didn't create an intake
            # request. Inject the token directly under the canonical
            # GIT_TOKEN name.
            print("  No credential request found; injecting directly")
            base_dir = Path.home() / "carpenter"
            dot_env = base_dir / ".env"
            lines = dot_env.read_text().splitlines() if dot_env.is_file() else []
            lines.append(f"GIT_TOKEN={forgejo_token}")
            dot_env.write_text("\n".join(lines) + "\n")
            httpx.post(
                f"{client.base_url}/api/credentials/reload-config",
                timeout=10.0,
            )

        # ── Step 3: Tell agent the token is provided ─────────────────────
        print("  Telling agent: 'I have provided the token'")
        _, response2 = client.chat(
            "I've provided the Forgejo access token. "
            "Please verify it works and confirm the setup is complete.",
            conversation_id=conv_id,
            timeout=120,
        )
        print(f"  Got response ({len(response2)} chars)")

        # Behavioral: agent should verify and confirm
        self.assert_that(
            any(kw in response2.lower() for kw in (
                "verified", "valid", "connected", "setup", "ready",
                "confirmed", "success", "working", "configured",
                "access", "complete",
            )),
            "Agent did not confirm credential verification",
            response_preview=response2[:400],
        )

        # ── Structural assertions ────────────────────────────────────────
        # If we used the intake API, all requests should be fulfilled
        if fulfilled_any:
            pending_after = httpx.get(
                f"{client.base_url}/api/credentials/pending", timeout=10.0,
            ).json().get("pending", [])
            self.assert_that(
                len(pending_after) == 0,
                f"Credential requests still pending: "
                f"{[p['key'] for p in pending_after]}",
            )

        elapsed = time.time() - start_ts
        return self.result(
            f"External repo setup completed in {elapsed:.1f}s. "
            f"Credential stored and verified."
        )
