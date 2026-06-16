"""Carpenter Linux entry point — injects platform and sandbox, then starts server."""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def _update_plugins_json(path: str, plugin_name: str, shared_folder: str) -> None:
    """Add or update a plugin entry in plugins.json, creating the file if needed."""
    p = Path(path)
    if p.exists():
        with open(p) as f:
            data = json.load(f)
    else:
        data = {"plugins": {}}

    data.setdefault("plugins", {})
    data["plugins"][plugin_name] = {
        "enabled": True,
        "description": f"External tool plugin: {plugin_name}",
        "transport": "file-watch",
        "transport_config": {
            "shared_folder": shared_folder,
        },
    }

    with open(p, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _cmd_setup_plugin(argv: list[str]) -> None:
    """Handle: python3 -m carpenter_linux setup-plugin [options]

    Sets up a plugin watcher — creates the shared folder structure, generates
    watcher files (watcher.py + watcher_config.json + systemd service), and
    registers the plugin in plugins.json.

    After running this command, start the watcher with:
        systemctl --user enable --now carpenter-plugin-watcher@<name>
    """
    parser = argparse.ArgumentParser(
        prog="python3 -m carpenter_linux setup-plugin",
        description="Set up a Carpenter plugin watcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Auto-detect Claude Code and set up with defaults:
  python3 -m carpenter_linux setup-plugin --name claude-code

  # Explicit command:
  python3 -m carpenter_linux setup-plugin --name my-tool \\
      --command /usr/local/bin/my-tool --run-it \\
      --prompt-mode stdin
""",
    )
    parser.add_argument(
        "--name", required=True,
        help="Plugin name (alphanumeric, hyphens, underscores)",
    )
    parser.add_argument(
        "--shared-folder",
        help="Shared folder path (default: ~/carpenter-shared/{name})",
    )
    parser.add_argument(
        "--command", nargs="+",
        help="Command to run as a list of args (auto-detected if not given)",
    )
    parser.add_argument(
        "--prompt-mode", choices=["stdin", "file", "arg"], default="stdin",
        help="How to pass the prompt to the command (default: stdin)",
    )
    parser.add_argument(
        "--install-dir",
        help="Watcher install dir (default: ~/.config/carpenter/watchers/{name})",
    )
    parser.add_argument(
        "--plugins-json",
        help="Path to plugins.json (default: {base_dir}/plugins.json)",
    )
    parser.add_argument(
        "--enable-service", action="store_true",
        help="Run 'systemctl --user enable --now' after setup",
    )

    args = parser.parse_args(argv)
    name = args.name

    # Validate name
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        print(
            f"ERROR: Plugin name must contain only letters, numbers, hyphens, "
            f"and underscores. Got: {name!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    shared_folder = args.shared_folder or str(Path.home() / "carpenter-shared" / name)
    install_dir = args.install_dir or str(
        Path.home() / ".config" / "carpenter" / "watchers" / name
    )

    # Auto-detect command
    command = args.command
    if command is None:
        claude_bin = shutil.which("claude")
        if claude_bin:
            # --dangerously-skip-permissions is required for non-interactive
            # file writes. Prompts sent via the plugin have already passed
            # Carpenter's code review pipeline, so this is safe here.
            command = [claude_bin, "--print", "--dangerously-skip-permissions"]
            print(f"  Auto-detected Claude Code at: {claude_bin}")
        else:
            command = ["echo", "Replace this with your tool command"]
            print(
                "  WARNING: 'claude' not found in PATH. Using placeholder command.",
                file=sys.stderr,
            )
            print(
                f"  Edit {install_dir}/watcher_config.json to set the correct command.",
                file=sys.stderr,
            )

    # 1. Create shared folder structure
    shared = Path(shared_folder)
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "triggered").mkdir(exist_ok=True)
    (shared / "completed").mkdir(exist_ok=True)
    print(f"  Created shared folder: {shared_folder}")

    # 2. Generate watcher files
    from .plugins.watcher_setup import generate_watcher_setup
    generated = generate_watcher_setup(
        plugin_name=name,
        shared_folder=shared_folder,
        target_dir=install_dir,
        command=command,
        prompt_mode=args.prompt_mode,
    )
    for kind, path in generated.items():
        print(f"  Generated: {path}")

    # 3. Update plugins.json
    from carpenter.config import CONFIG
    base_dir = CONFIG.get("base_dir", str(Path.home() / "carpenter"))
    plugins_json = args.plugins_json or str(Path(base_dir) / "config" / "plugins.json")
    _update_plugins_json(plugins_json, name, shared_folder)
    print(f"  Updated plugins.json: {plugins_json}")

    # 4. Install systemd service file via platform abstraction
    service_src = Path(install_dir) / "carpenter-plugin-watcher@.service"
    if service_src.exists():
        from carpenter.platform import get_platform
        platform = get_platform()
        service_content = service_src.read_text()
        installed = platform.install_service(
            "carpenter-plugin-watcher@", service_content,
        )
        if installed:
            systemd_dir = Path.home() / ".config" / "systemd" / "user"
            service_dst = systemd_dir / "carpenter-plugin-watcher@.service"
            print(f"  Installed systemd service: {service_dst}")
        else:
            print("  WARNING: Could not install systemd service", file=sys.stderr)

    # 5. Optionally enable and start
    service_instance = f"carpenter-plugin-watcher@{name}"
    if args.enable_service:
        ret = os.system(f"systemctl --user enable --now {service_instance}")
        if ret == 0:
            print(f"  Service enabled and started: {service_instance}")
        else:
            print(f"  WARNING: Failed to enable service (exit {ret})", file=sys.stderr)

    print("")
    print(f"Plugin '{name}' set up successfully.")
    print("")
    print("  To start the watcher:")
    print(f"    systemctl --user enable --now {service_instance}")
    print("")
    print("  To check status:")
    print(f"    systemctl --user status {service_instance}")
    print(f"    journalctl --user -u {service_instance} -f")
    print("")
    print("  To use from reviewed executor code:")
    print("    from carpenter_tools.act import plugin")
    print(f"    result = plugin.submit_task(plugin_name={name!r}, prompt='...')")
    print("")


def main():
    # setup-plugin is handled here (Linux-specific: systemd, watcher)
    if len(sys.argv) > 1 and sys.argv[1] == "setup-plugin":
        _cmd_setup_plugin(sys.argv[2:])
        return

    # setup-credential delegates to core
    if len(sys.argv) > 1 and sys.argv[1] == "setup-credential":
        from carpenter.__main__ import main as core_main
        core_main()
        return

    # packages upgrade — the Linux-platform reconcile/upgrade CLI.
    if len(sys.argv) > 1 and sys.argv[1] == "packages":
        from carpenter_linux.packages.upgrade_cli import cmd_packages
        sys.exit(cmd_packages(sys.argv[2:]))

    # Inject Linux platform
    from carpenter.platform import set_platform
    from carpenter_linux.platform import LinuxPlatform
    set_platform(LinuxPlatform())

    # Inject sandbox detection + methods
    from carpenter.sandbox import set_sandbox_provider, register_sandbox_method
    from carpenter_linux.sandbox.detector import detect
    from carpenter_linux.sandbox import (
        landlock_sandbox,
        namespace_sandbox,
        bubblewrap_sandbox,
        apparmor_sandbox,
    )
    set_sandbox_provider(detect)
    register_sandbox_method(
        "landlock", landlock_sandbox.build_command, landlock_sandbox.build_shell_command
    )
    register_sandbox_method(
        "namespace", namespace_sandbox.build_command, namespace_sandbox.build_shell_command
    )
    register_sandbox_method(
        "bubblewrap", bubblewrap_sandbox.build_command, bubblewrap_sandbox.build_shell_command
    )
    register_sandbox_method(
        "apparmor", apparmor_sandbox.build_command, apparmor_sandbox.build_shell_command
    )

    # Start server
    from carpenter.server import run_server
    run_server()


if __name__ == "__main__":
    main()
