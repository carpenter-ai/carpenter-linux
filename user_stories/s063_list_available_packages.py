"""
S063 — "What Can I Install?"

Discovery, so a person can ask for a package by name instead of
pasting a URL. The operator configures a package source; the person
asks what is on offer; the agent reads each candidate's
``manifest.yaml`` out of a quarantined fetch and hands back the exact
spec strings the install tool takes — installing nothing, running none
of the fetched code, and leaving no fetched tree behind.

Preconditions (environment variables; the story SKIPS when absent)
------------------------------------------------------------------

  CARPENTER_TEST_PKG_GIT_URL
      The fixture repository to configure as a package source. Must
      need no read credential: a config reload makes a
      ``package_sources`` entry live, but it cannot put a
      ``credential_ref`` token into the running server's environment.
  CARPENTER_TEST_PKG_NAME
      The manifest name of a package the listing must find.
  CARPENTER_TEST_PKG_SUBDIR_TEMPLATE  (optional)
      ``subdir_template`` for a repository holding several packages,
      e.g. ``packages/{name}``. Omit when the repository root is itself
      the package.

The story adds its own source under a story-specific alias and
restores ``config.yaml`` byte for byte afterwards. It refuses to run if
that alias is somehow already configured, rather than shadowing an
operator's source.

What this story asserts (STRICT)
--------------------------------

1. With the source configured and the config reloaded — no restart —
   the running platform sees it: asked what it can install, the agent
   calls ``list_available_packages`` and the configured alias comes
   back.
2. Asked for that source by name, the agent calls
   ``list_available_packages(source=<alias>)`` and the tool's recorded
   result names the fixture package with its version and an install
   ``spec:`` line.
3. That spec is *pinned*: a git listing emits
   ``git+<url>@<40-hex commit>``, so what a person asks to install next
   is the revision the listing actually read.
4. Nothing was installed and nothing was requested: no
   ``installed_packages`` row appeared, no ``pending_package_installs``
   row was created, and ``request_package_install`` was never called.
5. No fetched tree is left behind: the fetch quarantine holds exactly
   what it held before the listing.
6. The listing was cached on disk (that cache's TTL is what stops a
   curious agent making the platform clone a remote on every
   question), and the cached document carries manifest-derived fields —
   name, version, description, spec — for the fixture package.

What it does NOT assert: that no package code was executed. That is a
property of the code path (``discover`` reads ``manifest.yaml`` via
``yaml.safe_load`` and deliberately does not run the static preview),
and it is carpenter-core's unit tests that can prove a negative about
execution. What this story can and does prove are its observable
consequences — nothing installed, nothing requested, no tree left
behind.

Cleanup: removes the listing cache file the story caused, deletes any
request row (there should be none), and restores ``config.yaml``.
"""

from __future__ import annotations

import json
import re
import time

from user_stories.framework import (
    AcceptanceStory,
    CarpenterClient,
    DBInspector,
    StoryResult,
)
from user_stories import package_install_support as support


#: Alias the story configures. Matches core's bare-name grammar
#: (lowercase, digits, dot, dash, underscore) and is distinctive enough
#: that the cache file it produces can be cleaned up by prefix.
SOURCE_ALIAS = "s063-story-source"

_PINNED_GIT_SPEC_RE = re.compile(r"git\+\S+@[0-9a-f]{40}")


class ListAvailablePackages(AcceptanceStory):
    name = "S063 — What Can I Install?"
    description = (
        "A configured package source is listed over chat: manifests only, "
        "with install specs pinned to the revision read, installing "
        "nothing and leaving no fetched tree behind."
    )
    artifact_prefix = "s063"
    timeout = 900

    def __init__(self) -> None:
        self._config: support.ConfigPatch | None = None

    # ------------------------------------------------------------------

    def run(self, client: CarpenterClient, db: DBInspector) -> StoryResult:
        self.assert_that(db is not None, "DB inspector required")
        env = support.require_env(support.ENV_GIT_URL, support.ENV_NAME)
        url = support.git_spec(env[support.ENV_GIT_URL])
        name = env[support.ENV_NAME]
        template = support.optional_env(support.ENV_SUBDIR_TEMPLATE)

        start_ts = time.time()
        staging_before = support.staging_children()
        requests_before = support.count_request_rows(db)
        # Baseline rather than "absent": the fixture package may already
        # be installed on this deployment for other reasons, and a
        # listing must simply not change that either way.
        installed_before = support.install_record(db, name)

        self._config = support.ConfigPatch(client, self.artifact_prefix)
        self._config.add_package_source(SOURCE_ALIAS, url, template)
        self._config.allowlist_host(support.host_of(url))
        self._config.apply()

        # ── 1. The reloaded config is live: ask what is on offer ────
        print(f"\n  [1/5] Asking what can be installed")
        conv_id, reply = client.chat(
            "What capability packages can I install? Please check the "
            "configured package sources.",
            timeout=300,
        )
        print(f"     reply ({len(reply)} chars): {reply[:240]}")
        listings = support.tool_calls(db, conv_id, "list_available_packages")
        self.assert_that(
            bool(listings),
            "The agent did not call list_available_packages. Either it "
            "answered from memory, or the tool was not offered to it (it "
            "is declared always_available=False).",
            tools_called=support.tool_names_called(db, conv_id),
            reply_preview=reply[:400],
        )
        first_results = "\n".join(c["result_text"] or "" for c in listings)
        self.assert_contains(
            first_results, SOURCE_ALIAS,
            "the source this story configured must appear in the listing "
            "of configured sources — proof the running platform picked up "
            "the config reload without a restart",
        )

        # ── 2. Ask for that source by name ──────────────────────────
        print(f"  [2/5] Asking for the packages in {SOURCE_ALIAS!r}")
        _, detail_reply = client.chat(
            f"Please list the packages in the {SOURCE_ALIAS} source, with "
            f"the exact spec string for each one.",
            conversation_id=conv_id, timeout=300,
        )
        print(f"     reply ({len(detail_reply)} chars): {detail_reply[:240]}")
        all_listings = support.tool_calls(
            db, conv_id, "list_available_packages",
        )
        by_alias = [
            c for c in all_listings
            if SOURCE_ALIAS in (c["input_json"] or "")
        ]
        self.assert_that(
            bool(by_alias),
            f"No list_available_packages call named the "
            f"{SOURCE_ALIAS!r} source",
            inputs=[c["input_json"] for c in all_listings],
            reply_preview=detail_reply[:400],
        )
        detail = "\n".join(c["result_text"] or "" for c in by_alias)
        self.assert_contains(
            detail, name, "the listing must name the fixture package",
        )
        self.assert_contains(
            detail, "spec:",
            "the listing must hand back an install spec per package",
        )
        self.assert_contains(
            detail, "Nothing was installed",
            "the tool must say plainly that it installed nothing",
        )

        # ── 3. The spec is pinned to what was read ──────────────────
        print("  [3/5] Checking the emitted spec is pinned")
        pinned = _PINNED_GIT_SPEC_RE.search(detail)
        self.assert_that(
            pinned is not None,
            "The listing's spec string is not pinned to a 40-hex commit. "
            "An unpinned spec means the install would fetch whatever the "
            "branch points at later, not what was listed.",
            listing_preview=detail[:600],
        )
        print(f"     pinned spec: {pinned.group(0)[:72]}")

        # ── 4. Nothing installed, nothing requested ─────────────────
        print("  [4/5] Asserting the listing installed and requested nothing")
        installed_after = support.install_record(db, name)
        self.assert_that(
            (installed_after or {}).get("hash")
            == (installed_before or {}).get("hash"),
            f"A listing changed what is installed for {name!r}",
            before=(installed_before or {}).get("hash"),
            after=(installed_after or {}).get("hash"),
        )
        self.assert_that(
            support.count_request_rows(db) == requests_before,
            f"A listing created {support.count_request_rows(db) - requests_before} "
            f"package-install request row(s)",
        )
        called = support.tool_names_called(db, conv_id)
        for forbidden in (
            "request_package_install", "install_package", "uninstall_package",
        ):
            self.assert_that(
                forbidden not in called,
                f"The agent called {forbidden!r} while merely being asked "
                f"what is available",
                tools_called=called,
            )

        # ── 5. No fetched tree left; the listing was cached ─────────
        print("  [5/5] Checking the quarantine and the listing cache")
        staging_after = support.staging_children()
        self.assert_that(
            staging_after == staging_before,
            "The listing left something in the fetch quarantine. A "
            "listing must never leave a fetched tree behind: it is "
            "untrusted input that nothing is going to install.",
            before=staging_before, after=staging_after,
        )
        cache_files = self._cache_files()
        self.assert_that(
            bool(cache_files),
            "No listing cache file was written for this source. The "
            "cache's TTL is what bounds how often an agent can make the "
            "platform talk to a remote.",
            cache_dir=str(support.listing_cache_dir()),
        )
        cached = json.loads(cache_files[0].read_text(encoding="utf-8"))
        self.assert_that(
            cached.get("source") == SOURCE_ALIAS,
            f"The cached listing names source {cached.get('source')!r}",
        )
        entries = [
            p for p in (cached.get("packages") or [])
            if isinstance(p, dict) and p.get("name") == name
        ]
        self.assert_that(
            bool(entries),
            f"The cached listing has no entry for {name!r}",
            cached_names=[
                p.get("name") for p in (cached.get("packages") or [])
            ],
        )
        entry = entries[0]
        for field in ("name", "version", "spec"):
            self.assert_that(
                bool(entry.get(field)),
                f"The cached entry for {name!r} has no {field!r}, so the "
                f"manifest was not really read",
                entry=entry,
            )
        print(
            f"     cached {len(cached.get('packages') or [])} package(s) "
            f"from {str(cached.get('resolved') or '')[:12]}"
        )

        elapsed = time.time() - start_ts
        return self.result(
            f"{SOURCE_ALIAS} listed over chat in {elapsed:.0f}s: "
            f"{name} v{entry.get('version')} offered with a pinned spec, "
            f"nothing installed, no tree left behind"
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _cache_files() -> list:
        """Listing cache files belonging to this story's alias.

        The cache file is named ``<alias>-<source_id>.json``, so the
        story-specific alias makes its own files identifiable without
        touching an operator's.
        """
        cache_dir = support.listing_cache_dir()
        if not cache_dir.is_dir():
            return []
        return sorted(cache_dir.glob(f"{SOURCE_ALIAS}-*.json"))

    def cleanup(self, client: CarpenterClient, db: "DBInspector | None") -> None:
        for path in self._cache_files():
            try:
                path.unlink()
                print(f"  [cleanup] removed listing cache {path.name}")
            except OSError as exc:
                print(f"  ! could not remove {path.name}: {exc}")
        if self._config is not None:
            try:
                self._config.restore()
            except Exception as exc:  # noqa: BLE001 — report, never mask
                print(f"  ! CONFIG NOT RESTORED: {exc}")
