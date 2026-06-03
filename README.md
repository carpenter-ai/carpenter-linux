# Carpenter for Linux

Linux platform package for the [Carpenter](https://github.com/carpenter-ai/carpenter-core) AI agent platform.

## What This Provides

- **LinuxPlatform** -- process restart via `os.execv`, file protection, systemd service generation, graceful process termination
- **Sandbox methods** -- Landlock, user/mount namespaces, bubblewrap, AppArmor (auto-detected at startup)

## Deployment Targets

Both bare-metal (systemd) and Docker deployments are first-class targets and must be kept in sync. When new config keys are added to `carpenter-core`'s `config.py` DEFAULTS, update **both** `docker/config.yaml` and `install.sh` to match.

## Usage

```bash
# Run directly (injects Linux platform into carpenter-core and starts the server)
python3 -m carpenter_linux

# Install as editable package
pip install -e .
```

## Acceptance Tests

47 end-to-end acceptance stories live in `user_stories/`. Run them against a live server:

```bash
python3 user_stories/runner.py          # all stories
python3 user_stories/runner.py s001     # single story
```

See `user_stories/runner.py` header for environment variable configuration.
