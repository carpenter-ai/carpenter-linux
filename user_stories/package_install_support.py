"""Shared helpers for the remote capability-package install stories (s060-s065).

Not a story: the runner only discovers modules matching ``s[0-9]*.py``,
so this module is imported by the stories and never collected as one.

It exists because six stories drive the same two-phase flow — ask in
chat, read the approval page, decide on it, wait for the work queue —
and a copy of that sequence in each one would drift apart. Everything
deployment-specific arrives through an environment variable; nothing
here contains a host, a path, a repository or a package name.

The flow these helpers wrap (carpenter-core, DESIGN D11)
-------------------------------------------------------

1. The chat agent calls ``request_package_install(sources=[...])``.
   That resolves each source spec, fetches it into a quarantine
   directory, previews it **without executing any fetched code**,
   writes a ``pending_package_installs`` row in status ``requested``
   and returns a one-time approval URL under
   ``/api/package-install/<token>``. The tool returns its text as a
   ``FinalReply``, so the URL reaches the conversation verbatim.
2. A human opens that URL. The GET is deliberately outside the
   platform's token gate (the link has to open from a chat app or a
   mail client), mutates nothing, and installs nothing.
3. ``POST /api/package-install/<token>/decide`` records the decision.
   That route **is** inside the token gate: the link is handed back to
   the model that asked for the install, so possession of it is not
   evidence a human acted. Approving enqueues
   ``package.install.approved`` and returns; the install itself runs on
   the work queue, outside the model turn.
4. The work handler installs each package, hot-loads it, writes
   ``result_json`` and moves the row to ``installed`` (or ``failed``),
   then writes one visible ``system`` message into the conversation
   that asked.

Config keys, and why a reload is enough
---------------------------------------

``package_sources`` and ``web_egress_allowlist`` are both read at call
time, not captured at startup:

* ``carpenter.packages.sources._source_entries`` calls
  ``config.get_config("package_sources", [])`` on every parse, and
  ``configured_sources()`` re-parses per call.
* ``carpenter.security.egress._load_allowlist`` calls
  ``config.get_config("web_egress_allowlist", [])`` inside
  ``check_url``, which the fetch dispatcher calls per fetch.
* ``carpenter.config.reload_config`` re-reads config.yaml and mutates
  the ``CONFIG`` dict **in place**, so every holder of that reference
  sees the new values, and ``POST /api/credentials/reload-config`` is
  exactly that call.

So :class:`ConfigPatch` edits config.yaml, POSTs the reload, and
restores the file byte for byte — no restart, and nothing left changed.
One thing a reload does **not** do: export a ``credential_ref`` token
into the server process's environment. These stories therefore only
work against a source that needs no read credential.

Rate limits
-----------

``package_install.max_open_requests`` (default 3) and
``max_requests_per_hour`` (default 5) are counted in SQL against
``pending_package_installs`` itself. Every story here deletes its own
rows in ``cleanup()``, which is what keeps a full-suite run — five
stories that each make a request — from hitting the hourly cap.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

# ── environment-variable preconditions ──────────────────────────────

#: The git fixture every git-sourced story shares.
ENV_GIT_URL = "CARPENTER_TEST_PKG_GIT_URL"
ENV_NAME = "CARPENTER_TEST_PKG_NAME"
ENV_SUBDIR = "CARPENTER_TEST_PKG_SUBDIR"
ENV_REF = "CARPENTER_TEST_PKG_REF"
ENV_TOOL = "CARPENTER_TEST_PKG_TOOL"
ENV_SIDE_EFFECT = "CARPENTER_TEST_PKG_SIDE_EFFECT_PATH"

#: A second, different repository (s061).
ENV_GIT_URL2 = "CARPENTER_TEST_PKG2_GIT_URL"
ENV_NAME2 = "CARPENTER_TEST_PKG2_NAME"
ENV_SUBDIR2 = "CARPENTER_TEST_PKG2_SUBDIR"
ENV_REF2 = "CARPENTER_TEST_PKG2_REF"

#: Discovery (s063).
ENV_SUBDIR_TEMPLATE = "CARPENTER_TEST_PKG_SUBDIR_TEMPLATE"

#: Archive fixture (s064).
ENV_ARCHIVE_URL = "CARPENTER_TEST_PKG_ARCHIVE_URL"
ENV_ARCHIVE_SHA256 = "CARPENTER_TEST_PKG_ARCHIVE_SHA256"
ENV_ARCHIVE_NAME = "CARPENTER_TEST_PKG_ARCHIVE_NAME"

#: Upgrade fixture (s065).
ENV_OLD_REF = "CARPENTER_TEST_PKG_OLD_REF"
ENV_EDIT_FILE = "CARPENTER_TEST_PKG_EDIT_FILE"
ENV_CONFLICT_FILE = "CARPENTER_TEST_PKG_CONFLICT_FILE"


def require_env(*names: str) -> dict[str, str]:
    """Return the named environment variables, or skip the story.

    Follows s015's precondition shape, but raises ``pytest.skip`` so the
    runner reports SKIP rather than a pass that proved nothing (the
    runner catches ``Skipped`` explicitly — see ``run_story``).
    """
    values = {name: (os.environ.get(name) or "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        import pytest
        pytest.skip(
            "Missing environment variable(s): "
            + ", ".join(missing)
            + ". These name the test package fixture; nothing about them "
            "can be inferred, so the story cannot run without them."
        )
    return values


def optional_env(name: str) -> str:
    """Return an optional environment variable, or ``""``."""
    return (os.environ.get(name) or "").strip()


# ── source specs ────────────────────────────────────────────────────

def git_spec(url: str, *, ref: str = "", subdir: str = "") -> str:
    """Compose a ``git+https://...[@ref][#subdir=...]`` source spec.

    Accepts the fixture URL with or without the ``git+`` prefix, because
    an operator setting the variable will reasonably write either; the
    platform's grammar requires the prefix (``sources._parse_git``).
    """
    spec = url.strip()
    if not spec.startswith("git+"):
        spec = f"git+{spec}"
    if ref:
        spec = f"{spec}@{ref}"
    if subdir:
        spec = f"{spec}#subdir={subdir}"
    return spec


def archive_spec(url: str, sha256: str) -> str:
    """Compose an ``https://.../pkg.tar.gz#sha256=<64 hex>`` source spec."""
    return f"{url.strip()}#sha256={sha256.strip().lower()}"


def host_of(url: str) -> str:
    """The hostname of a source URL, with any ``git+`` prefix removed."""
    bare = url.strip()
    if bare.startswith("git+"):
        bare = bare[4:]
    try:
        return (urlsplit(bare).hostname or "").strip()
    except ValueError:
        return ""


def spec_problems(
    recorded: str, url: str, *, ref: str = "", subdir: str = "",
) -> list[str]:
    """Why ``recorded`` is not the spec for this source — empty if it is.

    Compared by component rather than string equality: the model passes
    the spec a person gave it, and a model that reformats an equivalent
    spec (dropping a ``git+`` prefix it considered noise, say) has still
    requested the right source. What must not drift is the location, the
    ref and the subdirectory, because those decide which bytes arrive.
    """
    bare = url.strip()
    if bare.startswith("git+"):
        bare = bare[4:]
    problems: list[str] = []
    if bare not in (recorded or ""):
        problems.append(f"does not name the source URL {bare!r}")
    if ref and ref not in (recorded or ""):
        problems.append(f"does not carry the ref {ref!r}")
    if subdir and subdir not in (recorded or ""):
        problems.append(f"does not carry the subdir {subdir!r}")
    return problems


def corrupt_digest(sha256: str) -> str:
    """Return a well-formed but WRONG sha256 for the same length.

    Flips one hex digit, so the value is still 64 hex characters and is
    refused by the digest check rather than by spec parsing — which is
    the distinction s064 exists to prove.
    """
    digest = sha256.strip().lower()
    first = digest[0]
    return ("1" if first == "0" else "0") + digest[1:]


# ── platform layout, read from the live config ──────────────────────

def config_yaml_path() -> Path:
    """The live ``config.yaml``, honouring ``CARPENTER_CONFIG``.

    Same resolution order the runner and carpenter-core use, so a
    relocated deployment is read correctly instead of assumed.
    """
    override = optional_env("CARPENTER_CONFIG")
    if override:
        return Path(override)
    return Path.home() / "carpenter" / "config" / "config.yaml"


def load_config() -> dict:
    """The parsed live config, or ``{}`` when it cannot be read."""
    path = config_yaml_path()
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — absent or malformed is "unknown"
        return {}


def base_dir() -> Path:
    """The platform ``base_dir``, with carpenter-core's own fallback."""
    configured = load_config().get("base_dir")
    if configured:
        return Path(str(configured)).expanduser()
    return Path.home() / "carpenter"


def install_dest(package_name: str) -> Path:
    """Where an installed package's tree lands.

    Mirrors ``carpenter.packages.installer.default_install_dest``:
    ``<base_dir>/packages/<name>``.
    """
    return base_dir() / "packages" / package_name


def staging_root() -> Path:
    """The fetch quarantine root (``fetch.staging_root_dir``)."""
    return base_dir() / "cache" / "package-staging"


def listing_cache_dir() -> Path:
    """The discovery listing cache (``discover.LISTING_DIR_NAME``)."""
    return base_dir() / "cache" / "package-listings"


# ── config.yaml patching ────────────────────────────────────────────

_BLOCK_START = "# >>> acceptance story {label}: added at runtime, removed again"
_BLOCK_END = "# <<< acceptance story {label}"


class ConfigPatch:
    """Append a ``package_sources`` / ``web_egress_allowlist`` block, then undo it.

    Append-only on purpose. The alternative — parse, mutate, re-dump —
    rewrites the operator's whole file and loses its comments for the
    duration of the story, so a crash mid-run leaves a config nobody
    recognises. Appending leaves the original text byte-identical and
    adds a commented block at the end; a later YAML key wins over an
    earlier one, so the appended block is what the platform reads.

    ``restore()`` writes the original bytes back and reloads again, and
    is safe to call when ``apply()`` never ran or already failed.
    """

    def __init__(self, client, label: str):
        self._client = client
        self._label = label
        self._path = config_yaml_path()
        self._original: bytes | None = None
        self._applied = False
        self._sources: list[dict] = []
        self._hosts: list[str] = []

    # -- declaration -------------------------------------------------

    def add_package_source(
        self, name: str, url: str, subdir_template: str = "",
    ) -> None:
        """Declare one ``package_sources`` entry (keys core allows only)."""
        entry: dict[str, str] = {"name": name, "url": url}
        if subdir_template:
            entry["subdir_template"] = subdir_template
        self._sources.append(entry)

    def allowlist_host(self, host: str) -> None:
        """Add a host to ``web_egress_allowlist`` if it is not already there.

        A publicly routable host needs no entry — ``egress.check_url``
        allows any globally routable address — so this is a no-op for a
        fixture on the public internet and the one thing that makes a
        fixture on a private network reachable at all.
        """
        if host and host not in self._hosts:
            self._hosts.append(host)

    # -- application -------------------------------------------------

    def apply(self) -> None:
        """Write the block and make the running platform read it."""
        if not self._sources and not self._hosts:
            return
        original = self._path.read_bytes()
        existing = yaml.safe_load(original.decode("utf-8")) or {}

        # A host the operator already allowlisted needs no entry from us,
        # and neither does a publicly routable one — ``egress.check_url``
        # allows any globally routable address. Adding an entry anyway
        # would be a config write with nothing behind it.
        already = {
            h.strip().lower()
            for h in (existing.get("web_egress_allowlist") or [])
            if isinstance(h, str)
        }
        self._hosts = [h for h in self._hosts if h.lower() not in already]
        if not self._sources and not self._hosts:
            print("  [config] nothing to add; config.yaml left untouched")
            return
        self._original = original

        merged_sources = list(existing.get("package_sources") or [])
        declared = {
            s.get("name") for s in merged_sources if isinstance(s, dict)
        }
        for entry in self._sources:
            if entry["name"] in declared:
                raise RuntimeError(
                    f"a package source named {entry['name']!r} is already "
                    f"configured on this deployment; the story will not "
                    f"shadow an operator's own source"
                )
            merged_sources.append(entry)

        merged_hosts = [
            h for h in (existing.get("web_egress_allowlist") or [])
            if isinstance(h, str)
        ]
        for host in self._hosts:
            if host not in merged_hosts:
                merged_hosts.append(host)

        block: dict = {}
        if self._sources:
            block["package_sources"] = merged_sources
        if self._hosts:
            block["web_egress_allowlist"] = merged_hosts

        text = self._original.decode("utf-8")
        if not text.endswith("\n"):
            text += "\n"
        text += (
            "\n"
            + _BLOCK_START.format(label=self._label)
            + "\n"
            + yaml.safe_dump(block, sort_keys=False, default_flow_style=False)
            + _BLOCK_END.format(label=self._label)
            + "\n"
        )
        self._path.write_text(text, encoding="utf-8")
        self._applied = True
        reload_config(self._client)
        print(
            f"  [config] added {len(self._sources)} package source(s) and "
            f"{len(self._hosts)} egress host(s); reloaded"
        )

    def restore(self) -> None:
        """Put the file back exactly as it was, and reload again."""
        if not self._applied or self._original is None:
            return
        self._path.write_bytes(self._original)
        self._applied = False
        restored = self._path.read_bytes()
        if restored != self._original:
            raise RuntimeError(
                "config.yaml was not restored byte-for-byte — refusing to "
                "pretend the box is unchanged"
            )
        reload_config(self._client)
        print("  [config] restored config.yaml byte-for-byte; reloaded")

    def __enter__(self) -> "ConfigPatch":
        self.apply()
        return self

    def __exit__(self, *exc_info) -> None:
        self.restore()


def reload_config(client) -> None:
    """POST the config reload, and fail loudly if it did not happen.

    Deliberately not best-effort: a story that silently kept the old
    config would assert against a platform that never saw its source
    entry and blame the feature for it.
    """
    response = httpx.post(
        f"{client.base_url}/api/credentials/reload-config", timeout=30.0,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"config reload failed: {response.status_code} "
            f"{response.text[:200]}"
        )


# ── the approval link ───────────────────────────────────────────────

REQUEST_URL_PREFIX = "/api/package-install/"

_TOKEN_RE = re.compile(
    re.escape(REQUEST_URL_PREFIX)
    + r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def find_approval_token(text: str) -> str:
    """Return the approval token in a chat reply, or ``""``.

    The reply is a ``FinalReply``, so the URL reaches the conversation
    verbatim rather than paraphrased — which is what makes this a
    reliable assertion rather than a hopeful one.
    """
    match = _TOKEN_RE.search(text or "")
    return match.group(1) if match else ""


def approval_path(token: str) -> str:
    return f"{REQUEST_URL_PREFIX}{token}"


def get_approval_page_unauthenticated(client, token: str) -> httpx.Response:
    """GET the approval page with NO platform credential of any kind.

    Uses a bare ``httpx`` call rather than the framework client, whose
    ``Authorization`` header would make this prove nothing: the whole
    point is that a human reading the link from a chat app can open the
    page.
    """
    return httpx.get(
        f"{client.base_url}{approval_path(token)}",
        timeout=60.0, follow_redirects=False,
    )


def post_decision_unauthenticated(
    client, token: str, decision: str, grants: dict | None = None,
) -> httpx.Response:
    """POST a decision with no platform credential. Expected to be refused."""
    return httpx.post(
        f"{client.base_url}{approval_path(token)}/decide",
        json={"decision": decision, "grants": grants or {}},
        timeout=60.0,
    )


def post_decision(
    client, token: str, decision: str, grants: dict | None = None,
) -> httpx.Response:
    """POST a decision WITH the platform token the runner was given.

    Goes through the framework client's request path so the bearer token
    is attached; the response is returned unraised, because 401/409/410
    are outcomes these stories assert on rather than errors.
    """
    return client._post(
        f"{approval_path(token)}/decide",
        {"decision": decision, "grants": grants or {}},
        timeout=60.0,
    )


def grants_for(
    package_names, *, capabilities: bool = False, write_chat_tools: bool = True,
) -> dict:
    """Build a ``grants`` body.

    Defaults chosen so a story cleans up after itself:

    * ``capabilities=False`` — a package installs either way, and an
      unticked grant simply registers no platform verbs. Granting
      platform-level trust to a fixture is not needed to prove the flow.
    * ``write_chat_tools=True`` — a package's write-capable chat tools
      are refused at install time unless this is ticked, and a story
      that wants to call the package's tool afterwards needs it
      registered. ``_normalize_grants`` forces the flag false for a
      package that declares no chat tools, so this cannot grant more
      than the preview showed.
    """
    return {
        name: {
            "capabilities": capabilities,
            "write_chat_tools": write_chat_tools,
        }
        for name in package_names
    }


def auth_is_enforced(client) -> bool:
    """Whether this deployment has a ``ui_token`` configured at all.

    With no ``ui_token`` the auth middleware passes everything through,
    so "the model cannot approve its own install" is not a property of
    the deployment and the story that pins it must skip rather than
    report a pass it did not earn.
    """
    try:
        response = httpx.get(
            f"{client.base_url}/api/chat/history?conversation_id=1",
            timeout=15.0, follow_redirects=False,
        )
    except Exception:  # noqa: BLE001 — treat unreachable as unknown
        return False
    return response.status_code == 401


# ── pending_package_installs ────────────────────────────────────────

PENDING_TABLE = "pending_package_installs"

STATUS_REQUESTED = "requested"
STATUS_APPROVED = "approved"
STATUS_INSTALLING = "installing"
STATUS_INSTALLED = "installed"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"


def request_row(db, token: str) -> dict | None:
    """One ``pending_package_installs`` row by token, JSON decoded."""
    rows = db.fetchall(
        f"SELECT * FROM {PENDING_TABLE} WHERE token = ?", (token,),
    )
    if not rows:
        return None
    row = dict(rows[0])
    for column, key in (
        ("sources_json", "sources"),
        ("preview_json", "preview"),
        ("grants_json", "grants"),
        ("result_json", "result"),
    ):
        raw = row.get(column)
        try:
            row[key] = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            row[key] = None
    return row


def count_request_rows(db) -> int:
    rows = db.fetchall(f"SELECT COUNT(*) AS n FROM {PENDING_TABLE}")
    return int(rows[0]["n"]) if rows else 0


def wait_for_request_status(
    db, token: str, statuses, timeout: int = 300, poll_interval: float = 3.0,
) -> dict:
    """Block until the request row reaches one of ``statuses``.

    This is the honest way to wait for the install: the work queue, not
    the chat turn, is what performs it, and the row is where that work
    records itself.
    """
    wanted = set(statuses)
    deadline = time.monotonic() + timeout
    row: dict | None = None
    while time.monotonic() < deadline:
        row = request_row(db, token)
        if row and row["status"] in wanted:
            return row
        time.sleep(poll_interval)
    raise TimeoutError(
        f"package install request stayed in "
        f"{row['status'] if row else 'missing'!r} for {timeout}s, "
        f"waiting for one of {sorted(wanted)}; "
        f"error={row.get('error') if row else None!r}"
    )


def package_block(row: dict, name: str) -> dict:
    """One package's block out of a finished row's ``result_json``."""
    result = row.get("result") or {}
    for block in result.get("packages") or ():
        if isinstance(block, dict) and block.get("name") == name:
            return block
    return {}


def preview_entry(row: dict, name: str) -> dict:
    """One package's preview entry out of a request row."""
    for entry in row.get("preview") or ():
        if not isinstance(entry, dict):
            continue
        if (entry.get("manifest") or {}).get("name") == name:
            return entry
    return {}


def delete_request_rows(db, tokens) -> int:
    """Delete this story's request rows. Returns the number removed.

    The rows are what the rate limiter counts, so leaving them behind
    would make a later story in the same suite run be refused for
    "asking too often" — a failure with nothing to do with the feature.
    """
    wanted = [t for t in tokens if t]
    if not wanted or db is None:
        return 0
    conn = sqlite3.connect(db.db_path)
    try:
        removed = 0
        for token in wanted:
            cursor = conn.execute(
                f"DELETE FROM {PENDING_TABLE} WHERE token = ?", (token,),
            )
            removed += cursor.rowcount or 0
        conn.commit()
        return removed
    finally:
        conn.close()


# ── installed_packages ─────────────────────────────────────────────

PROVENANCE_COLUMNS = (
    "source_kind",
    "source_url",
    "source_ref",
    "source_resolved",
    "source_subdir",
    "source_id",
    "fetched_at",
)


def install_record(db, name: str) -> dict | None:
    """One ``installed_packages`` row, or ``None``."""
    rows = db.fetchall(
        "SELECT * FROM installed_packages WHERE name = ?", (name,),
    )
    return dict(rows[0]) if rows else None


def delete_install_record(db, name: str) -> int:
    """Last-resort cleanup of an install row the CLI could not remove.

    Only ever called after ``packages uninstall`` has already failed —
    the CLI is the real uninstall (it drops capability grants, archives
    or wipes per-package state, reclaims cached archives). This just
    stops a story leaving a row behind that points at a tree it also
    removed. ``installed_packages_templates`` is deleted explicitly
    because sqlite3 does not enable foreign keys by default, so the
    declared cascade would not fire here.
    """
    conn = sqlite3.connect(db.db_path)
    try:
        conn.execute(
            "DELETE FROM installed_packages_templates WHERE package_name = ?",
            (name,),
        )
        cursor = conn.execute(
            "DELETE FROM installed_packages WHERE name = ?", (name,),
        )
        conn.commit()
        return cursor.rowcount or 0
    finally:
        conn.close()


def set_recorded_ref(db, name: str, ref: str) -> None:
    """Repoint an install's recorded ``source_ref``.

    Used by s065 only, and the one piece of scaffolding in these
    stories: ``packages update`` takes no ``--ref`` and deliberately
    follows ``installed_packages.source_ref``, because following the ref
    is what an update *is*. A fixture repository is static, so the only
    way to make a newer version available to a recorded ref is to point
    the record at the newer ref — exactly the state a deployment is in
    when the branch it tracks has moved. See s065's docstring.
    """
    conn = sqlite3.connect(db.db_path)
    try:
        conn.execute(
            "UPDATE installed_packages SET source_ref = ? WHERE name = ?",
            (ref, name),
        )
        conn.commit()
    finally:
        conn.close()


# ── tool_calls ──────────────────────────────────────────────────────

def tool_calls(db, conversation_id: int, tool_name: str = "") -> list[dict]:
    """The recorded chat-tool calls of a conversation.

    ``tool_calls`` is written by ``agent.invocation`` for every
    ``tool_use`` block, with the tool's verbatim result text — so it is
    the evidence of which tool the model chose, independent of how the
    model later described what it did.
    """
    if tool_name:
        return db.fetchall(
            "SELECT * FROM tool_calls "
            "WHERE conversation_id = ? AND tool_name = ? ORDER BY id",
            (conversation_id, tool_name),
        )
    return db.fetchall(
        "SELECT * FROM tool_calls WHERE conversation_id = ? ORDER BY id",
        (conversation_id,),
    )


def tool_names_called(db, conversation_id: int) -> list[str]:
    return [c["tool_name"] for c in tool_calls(db, conversation_id)]


def system_messages(db, conversation_id: int) -> list[dict]:
    """Visible ``system`` messages — where the install outcome lands."""
    return db.fetchall(
        "SELECT * FROM messages "
        "WHERE conversation_id = ? AND role = 'system' "
        "AND COALESCE(hidden, 0) = 0 ORDER BY id",
        (conversation_id,),
    )


# ── the operator CLI ────────────────────────────────────────────────

def run_packages_cli(*args: str, timeout: int = 600):
    """Run ``python3 -m carpenter packages <args>`` and return the result.

    Uses the interpreter running the story, which is the one that can
    import carpenter (the framework and several stories already rely on
    that). ``check=False``: an exit code of 1 is an outcome s065
    asserts on, not an error.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "carpenter", "packages", *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    print(
        f"  [cli] packages {' '.join(args)} -> exit {completed.returncode}"
    )
    return completed


def cli_is_available() -> bool:
    """Whether this interpreter can run ``python3 -m carpenter packages``.

    A story that shells out to the operator CLI must know this before
    it installs anything: without it, the CLI's exit code 1 would be
    indistinguishable from the refusal the story is trying to assert.
    """
    try:
        completed = run_packages_cli("list", timeout=120)
    except Exception:  # noqa: BLE001 — unavailable is the answer
        return False
    return completed.returncode == 0


def uninstall_quietly(name: str) -> None:
    """Best-effort ``packages uninstall`` for cleanup.

    Never raises: cleanup runs after a failure too, and a package that
    was never installed is not a cleanup problem.

    Note what uninstall deliberately does NOT do: the running platform
    keeps the package's already-imported modules and registered chat
    tools until it is restarted, and security-policy entries the
    package contributed are never removed (a one-way ratchet). These
    stories do not restart the platform, and they refuse to install a
    fixture that proposes any policy entries.
    """
    if not name:
        return
    try:
        completed = run_packages_cli("uninstall", name, "--force", timeout=300)
    except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
        print(f"  ! could not uninstall {name}: {exc}")
        return
    if completed.returncode == 0:
        print(f"  [cleanup] uninstalled {name}")
    else:
        tail = (completed.stderr or completed.stdout or "").strip()[-200:]
        print(f"  [cleanup] uninstall {name} -> {completed.returncode}: {tail}")


def remove_install_tree(name: str) -> None:
    """Remove a leftover install directory, if uninstall did not.

    Only ever called for a package this story installed, and only on the
    path ``installer.default_install_dest`` computes for its name.
    """
    import shutil

    dest = install_dest(name)
    if dest.is_dir():
        shutil.rmtree(dest, ignore_errors=True)
        print(f"  [cleanup] removed leftover install tree for {name}")


def staging_children() -> list[str]:
    """Names of whatever is currently in the fetch quarantine."""
    root = staging_root()
    if not root.is_dir():
        return []
    try:
        return sorted(p.name for p in root.iterdir())
    except OSError:
        return []
