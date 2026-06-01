#!/bin/bash
# Carpenter install script
# Detects environment and configures sandbox, AI provider, directories, and database.

set -euo pipefail

# ── Repo directory (where this script lives) ──────────────────────────
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Color codes ───────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

info()    { printf "${CYAN}%s${NC}\n" "$*"; }
success() { printf "${GREEN}%s${NC}\n" "$*"; }
warn()    { printf "${YELLOW}WARNING: %s${NC}\n" "$*"; }
error()   { printf "${RED}ERROR: %s${NC}\n" "$*" >&2; exit 1; }

# ── Defaults ──────────────────────────────────────────────────────────
BASE_DIR="$HOME/carpenter"
AI_PROVIDER=""
AI_KEY_FILE=""
OLLAMA_URL="http://localhost:11434"
OLLAMA_MODEL=""
UI_TOKEN=""
SKIP_TOKEN=false
NON_INTERACTIVE=false
DEV_INSTALL=false
SANDBOX_METHOD=""
SANDBOX_ON_FAILURE=""
SETUP_PLUGIN=""         # "yes", "no", or "" (ask interactively)
PLUGIN_NAME=""          # Plugin name (default: "claude-code")
PLUGIN_COMMAND=""       # Watcher command override (auto-detected by default)
PORT=7842               # Server listen port

# ── Parse CLI arguments ──────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --ai-provider)
            AI_PROVIDER="$2"; shift 2 ;;
        --ai-key-file)
            AI_KEY_FILE="$2"; shift 2 ;;
        --ollama-url)
            OLLAMA_URL="$2"; shift 2 ;;
        --ollama-model)
            OLLAMA_MODEL="$2"; shift 2 ;;
        --ui-token)
            UI_TOKEN="$2"; shift 2 ;;
        --skip-token)
            SKIP_TOKEN=true; shift ;;
        --base-dir)
            BASE_DIR="$2"; shift 2 ;;
        --non-interactive)
            NON_INTERACTIVE=true; shift ;;
        --sandbox-method)
            SANDBOX_METHOD="$2"; shift 2 ;;
        --sandbox-on-failure)
            SANDBOX_ON_FAILURE="$2"; shift 2 ;;
        --dev)
            DEV_INSTALL=true; shift ;;
        --setup-plugin)
            SETUP_PLUGIN="yes"; shift ;;
        --no-plugin)
            SETUP_PLUGIN="no"; shift ;;
        --plugin-name)
            PLUGIN_NAME="$2"; shift 2 ;;
        --plugin-command)
            PLUGIN_COMMAND="$2"; shift 2 ;;
        --port)
            PORT="$2"; shift 2 ;;
        -h|--help)
            cat <<USAGE
Usage: install.sh [OPTIONS]

  --ai-provider PROVIDER   anthropic, ollama, tinfoil, or skip
  --ai-key-file PATH       File containing the API key (ANTHROPIC_API_KEY or TINFOIL_API_KEY)
  --ollama-url URL         Ollama API URL (default: http://localhost:11434)
  --ollama-model MODEL     Ollama model name (no default; required for ollama provider)
  --ui-token TOKEN         Set a UI access token (default: auto-generated)
  --skip-token             Skip token generation (no authentication)
  --base-dir DIR           Override base directory (default: ~/carpenter)
  --sandbox-method METHOD  Sandbox method: auto, landlock, namespace, apparmor, bubblewrap, none
  --sandbox-on-failure P   Sandbox failure policy: open or closed
  --non-interactive        Skip all prompts, use defaults/args
  --dev                    Install dev dependencies (pytest, etc.)
  --setup-plugin           Set up Claude Code plugin watcher (skip prompt)
  --no-plugin              Skip plugin setup (skip prompt)
  --plugin-name NAME       Plugin name to register (default: claude-code)
  --plugin-command CMD     Watcher command (default: auto-detected claude path)
  --port PORT              Server listen port (default: 7842)
  -h, --help               Show this help message
USAGE
            exit 0
            ;;
        *)
            error "Unknown option: $1  (use --help for usage)"
            ;;
    esac
done

# ── Helper: prompt with default ──────────────────────────────────────
# Usage: ask "Prompt text" DEFAULT_VALUE
# Returns the user's answer (or default if blank / non-interactive).
ask() {
    local prompt="$1"
    local default="$2"
    if $NON_INTERACTIVE; then
        echo "$default"
        return
    fi
    local answer
    read -r -p "$prompt [$default]: " answer
    echo "${answer:-$default}"
}

# ══════════════════════════════════════════════════════════════════════
# 1. Banner
# ══════════════════════════════════════════════════════════════════════
echo ""
printf "${BOLD}${CYAN}"
cat <<'BANNER'
  ╔═══════════════════════════════════════════╗
  ║         Carpenter Installer           ║
  ║   Pure-Python AI Agent Platform (CaMeL)   ║
  ╚═══════════════════════════════════════════╝
BANNER
printf "${NC}"
echo ""

# ══════════════════════════════════════════════════════════════════════
# 2. Environment Detection
# ══════════════════════════════════════════════════════════════════════
info "Detecting environment..."

IS_ROOT=false
CAN_SUDO_NOPASS=false
CAN_SUDO_PASS=false
HAS_DOCKER=false
IN_CONTAINER=false

if [[ "$(id -u)" -eq 0 ]]; then
    IS_ROOT=true
elif sudo -n true 2>/dev/null; then
    CAN_SUDO_NOPASS=true
elif groups 2>/dev/null | grep -qwE 'sudo|wheel|admin'; then
    CAN_SUDO_PASS=true
fi

if command -v docker &>/dev/null; then
    HAS_DOCKER=true
fi

if [[ -f /.dockerenv ]]; then
    IN_CONTAINER=true
fi

# Hardware detection (for informational display)
ARCH="$(uname -m)"
TOTAL_RAM_MB=0
if [[ -f /proc/meminfo ]]; then
    TOTAL_RAM_MB=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
elif command -v sysctl &>/dev/null; then
    TOTAL_RAM_MB=$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1024 / 1024 ))
fi

echo "  Root/sudo:         $(if $IS_ROOT; then echo 'root'; elif $CAN_SUDO_NOPASS; then echo 'sudo (passwordless)'; elif $CAN_SUDO_PASS; then echo 'sudo (password required)'; else echo 'no'; fi)"
echo "  Docker available:  $(if $HAS_DOCKER; then echo 'yes'; else echo 'no'; fi)"
echo "  Inside container:  $(if $IN_CONTAINER; then echo 'yes'; else echo 'no'; fi)"
echo "  Architecture:      $ARCH"
echo "  Total RAM:         ${TOTAL_RAM_MB} MB"
echo "  Repo directory:    $REPO_DIR"
echo "  Base directory:    $BASE_DIR"
echo ""


# ══════════════════════════════════════════════════════════════════════
# 3. Filesystem Sandbox Selection
# ══════════════════════════════════════════════════════════════════════
info "Detecting filesystem sandbox capabilities..."

# Step 1: Probe capabilities (silent)
HAS_NAMESPACE=false
HAS_BWRAP=false
HAS_LANDLOCK=false
HAS_APPARMOR=false

if command -v unshare &>/dev/null; then
    if unshare --user --map-root-user --mount bash -c "mount --make-rprivate / && echo ok" 2>/dev/null | grep -q ok; then
        HAS_NAMESPACE=true
    fi
fi

if command -v bwrap &>/dev/null; then
    HAS_BWRAP=true
fi

# Landlock: try the actual syscall via Python helper
if python3 -c "from carpenter_linux.sandbox._landlock_helper import probe_landlock_version; exit(0 if probe_landlock_version() > 0 else 1)" 2>/dev/null; then
    HAS_LANDLOCK=true
fi

# AppArmor: check aa-exec binary + filesystem
if command -v aa-exec &>/dev/null && [[ -d /sys/kernel/security/apparmor ]]; then
    HAS_APPARMOR=true
fi

echo "  Capabilities:"
echo "    Landlock:              $(if $HAS_LANDLOCK; then echo 'yes'; else echo 'no'; fi)"
echo "    User/mount namespaces: $(if $HAS_NAMESPACE; then echo 'yes'; else echo 'no'; fi)"
echo "    Bubblewrap (bwrap):    $(if $HAS_BWRAP; then echo 'yes'; else echo 'no'; fi)"
echo "    AppArmor:              $(if $HAS_APPARMOR; then echo 'yes'; else echo 'no'; fi)"
echo "    Docker:                $(if $HAS_DOCKER; then echo 'yes'; else echo 'no'; fi)"
echo ""

# Step 2: Build options and select
if [[ -n "$SANDBOX_METHOD" ]]; then
    # CLI-provided — validate
    case "$SANDBOX_METHOD" in
        auto|landlock|namespace|apparmor|bubblewrap|none) ;;
        *) error "Invalid sandbox method: $SANDBOX_METHOD. Use: auto, landlock, namespace, apparmor, bubblewrap, none" ;;
    esac
elif $NON_INTERACTIVE; then
    SANDBOX_METHOD="auto"
    echo "  Using auto-detection (non-interactive default)"
else
    echo ""
    echo "  Filesystem Sandbox Options:"
    SANDBOX_OPTIONS=()
    SANDBOX_LABELS=()
    if $HAS_LANDLOCK; then
        SANDBOX_OPTIONS+=("landlock")
        SANDBOX_LABELS+=("landlock     — Kernel-enforced filesystem rules (recommended)")
    fi
    if $HAS_NAMESPACE; then
        SANDBOX_OPTIONS+=("namespace")
        SANDBOX_LABELS+=("namespace    — Kernel-enforced via user/mount namespaces")
    fi
    if $HAS_APPARMOR; then
        SANDBOX_OPTIONS+=("apparmor")
        SANDBOX_LABELS+=("apparmor     — AppArmor MAC profile confinement")
    fi
    if $HAS_BWRAP; then
        SANDBOX_OPTIONS+=("bubblewrap")
        SANDBOX_LABELS+=("bubblewrap   — Lightweight container sandbox")
    elif ($IS_ROOT || $CAN_SUDO_NOPASS) && command -v apt &>/dev/null; then
        SANDBOX_OPTIONS+=("bubblewrap")
        SANDBOX_LABELS+=("bubblewrap   — Install and use (requires apt)")
    fi
    SANDBOX_OPTIONS+=("auto")
    SANDBOX_LABELS+=("auto         — Auto-detect best available at runtime")
    SANDBOX_OPTIONS+=("none")
    SANDBOX_LABELS+=("none         — No filesystem restriction (not recommended)")

    for i in "${!SANDBOX_LABELS[@]}"; do
        echo "    $((i+1))) ${SANDBOX_LABELS[$i]}"
    done
    echo ""
    read -r -p "  Choose sandbox method [1]: " SB_CHOICE
    SB_CHOICE="${SB_CHOICE:-1}"
    INDEX=$((SB_CHOICE - 1))
    if [[ $INDEX -lt 0 || $INDEX -ge ${#SANDBOX_OPTIONS[@]} ]]; then
        error "Invalid choice: $SB_CHOICE"
    fi
    SANDBOX_METHOD="${SANDBOX_OPTIONS[$INDEX]}"

    # If bubblewrap was selected but not installed, try to install it
    if [[ "$SANDBOX_METHOD" == "bubblewrap" ]] && ! $HAS_BWRAP; then
        if $IS_ROOT; then
            info "Installing bubblewrap..."
            apt install -y bubblewrap
        elif $CAN_SUDO_NOPASS; then
            info "Installing bubblewrap (via sudo)..."
            sudo apt install -y bubblewrap
        else
            warn "Cannot install bubblewrap without sudo. Run: sudo apt install bubblewrap"
            warn "Falling back to auto-detection."
            SANDBOX_METHOD="auto"
        fi
    fi

    # If apparmor was selected, install the profile
    if [[ "$SANDBOX_METHOD" == "apparmor" ]]; then
        info "Installing AppArmor profile..."
        if python3 -c "
from carpenter_linux.sandbox.apparmor_sandbox import install_profile
success = install_profile(['$BASE_DIR/data/workspaces', '$BASE_DIR/data/code', '$BASE_DIR/data/logs', '/tmp'])
exit(0 if success else 1)
" 2>/dev/null; then
            success "  AppArmor profile installed"
        else
            warn "Failed to install AppArmor profile. You may need to run with sudo."
            warn "Falling back to auto-detection."
            SANDBOX_METHOD="auto"
        fi
    fi
fi

# Step 3: On-failure policy
if [[ "$SANDBOX_METHOD" != "none" ]]; then
    if [[ -n "$SANDBOX_ON_FAILURE" ]]; then
        case "$SANDBOX_ON_FAILURE" in
            open|closed) ;;
            *) error "Invalid sandbox on_failure: $SANDBOX_ON_FAILURE. Use: open, closed" ;;
        esac
    elif $NON_INTERACTIVE; then
        SANDBOX_ON_FAILURE="open"
    else
        echo ""
        echo "  If sandbox fails at runtime:"
        echo "    1) Fall back to unsandboxed execution (default)"
        echo "    2) Refuse to execute (strict mode)"
        echo ""
        read -r -p "  Choose failure policy [1]: " FAIL_CHOICE
        FAIL_CHOICE="${FAIL_CHOICE:-1}"
        case "$FAIL_CHOICE" in
            1) SANDBOX_ON_FAILURE="open" ;;
            2) SANDBOX_ON_FAILURE="closed" ;;
            *) error "Invalid choice: $FAIL_CHOICE" ;;
        esac
    fi
else
    SANDBOX_ON_FAILURE="open"
fi

success "  Sandbox method: $SANDBOX_METHOD"
if [[ "$SANDBOX_METHOD" != "none" ]]; then
    echo "  On failure: $SANDBOX_ON_FAILURE"
fi
echo ""

# ══════════════════════════════════════════════════════════════════════
# 4. Create Directory Structure
# ══════════════════════════════════════════════════════════════════════
info "Creating directory structure under $BASE_DIR ..."

#
# NOTE: Keep this list minimal.  Anything under config/ that the server seeds
# (see carpenter.seed.SEED_MANIFEST) or derives from base_dir MUST NOT be
# pre-created here — pre-creating a seed target silently breaks
# shutil.copytree() in the seed installer, which refuses to overwrite an
# existing directory.  The server creates every other path it needs lazily.
DIRS=(
    "$BASE_DIR"
    "$BASE_DIR/data"
    "$BASE_DIR/data/logs"
    "$BASE_DIR/data/code"
    "$BASE_DIR/data/workspaces"
    "$BASE_DIR/config"
)

for dir in "${DIRS[@]}"; do
    if [[ -d "$dir" ]]; then
        echo "  [exists] $dir"
    else
        mkdir -p "$dir"
        echo "  [created] $dir"
    fi
done

echo ""

# ══════════════════════════════════════════════════════════════════════
# 5. Install Python Package
# ══════════════════════════════════════════════════════════════════════
info "Installing Carpenter Python package..."

INSTALL_EXTRA=""
if $DEV_INSTALL; then
    INSTALL_EXTRA="[dev]"
fi

install_package() {
    # Strategy 1: Standard pip install
    if command -v pip3 &>/dev/null; then
        if pip3 install --user -e "${REPO_DIR}${INSTALL_EXTRA}" 2>/dev/null; then
            return 0
        fi
        # Strategy 2: pip with --break-system-packages (Debian PEP 668)
        if pip3 install --user --break-system-packages -e "${REPO_DIR}${INSTALL_EXTRA}" 2>/dev/null; then
            return 0
        fi
    fi

    # Strategy 3: Create a .pth file in user site-packages (minimal install)
    local site_dir
    site_dir="$(python3 -c 'import site; print(site.getusersitepackages())')"
    if [[ -n "$site_dir" ]]; then
        mkdir -p "$site_dir"
        echo "$REPO_DIR" > "$site_dir/carpenter.pth"
        warn "Installed via .pth file (pip was unavailable or blocked by PEP 668)."
        warn "Dependencies must be installed separately."
        return 0
    fi

    return 1
}

echo "  Source: $REPO_DIR"
if install_package; then
    success "  Python package installed"
else
    error "Could not install package. Install manually: pip3 install -e '$REPO_DIR'"
fi

# Verify critical dependencies
info "Verifying critical dependencies..."
CRYPTOGRAPHY_OK=false
if python3 -c "from cryptography.fernet import Fernet; Fernet.generate_key()" 2>/dev/null; then
    CRYPTOGRAPHY_OK=true
    success "  cryptography: available"
else
    warn "cryptography library is NOT available!"
    echo ""
    echo "  ${BOLD}SECURITY NOTICE:${NC}"
    echo "  The cryptography library is required for encrypting untrusted arc output."
    echo "  Without it, sensitive data may be stored in plaintext."
    echo ""
    echo "  To fix, install the cryptography package:"
    echo "    ${BOLD}pip3 install --user cryptography>=41.0${NC}"
    echo ""
    echo "  Or if pip is blocked by PEP 668:"
    echo "    ${BOLD}pip3 install --user --break-system-packages cryptography>=41.0${NC}"
    echo ""
    if $NON_INTERACTIVE; then
        warn "Continuing with degraded security (non-interactive mode)"
    else
        read -r -p "  Press Enter to continue anyway, or Ctrl-C to abort: "
    fi
fi

echo ""

# ══════════════════════════════════════════════════════════════════════
# 6. AI Provider Selection
# ══════════════════════════════════════════════════════════════════════
CREDENTIAL_FILE=""
OLLAMA_URL_FINAL=""
OLLAMA_MODEL_FINAL=""

if [[ -z "$AI_PROVIDER" ]]; then
    if $NON_INTERACTIVE; then
        AI_PROVIDER="skip"
        info "Skipping AI provider configuration (non-interactive mode)"
    else
        echo ""
        info "AI Provider:"
        echo "  1) Anthropic (Claude) -- requires API key"
        echo "  2) Ollama             -- requires running Ollama server"
        echo "  3) Tinfoil            -- secure enclave inference, requires API key"
        echo "  4) Skip               -- configure later"
        echo ""
        read -r -p "  Choose AI provider [4]: " AI_CHOICE
        AI_CHOICE="${AI_CHOICE:-4}"
        case "$AI_CHOICE" in
            1) AI_PROVIDER="anthropic" ;;
            2) AI_PROVIDER="ollama" ;;
            3) AI_PROVIDER="tinfoil" ;;
            4) AI_PROVIDER="skip" ;;
            *) error "Invalid choice: $AI_CHOICE" ;;
        esac
    fi
fi

case "$AI_PROVIDER" in
    anthropic)
        info "Configuring Anthropic (Claude) provider..."
        API_KEY=""
        if [[ -n "$AI_KEY_FILE" ]]; then
            if [[ ! -f "$AI_KEY_FILE" ]]; then
                error "API key file not found: $AI_KEY_FILE"
            fi
            # Parse KEY=VALUE format or plain key file
            API_KEY="$(grep -oP '(?<=ANTHROPIC_API_KEY=)\S+' "$AI_KEY_FILE" 2>/dev/null || cat "$AI_KEY_FILE" | tr -d '[:space:]')"
            echo "  Read API key from $AI_KEY_FILE"
        else
            if $NON_INTERACTIVE; then
                error "Anthropic provider requires --ai-key-file in non-interactive mode."
            fi
            echo ""
            read -s -r -p "  Enter Anthropic API key: " API_KEY
            echo ""
        fi

        # Basic validation
        if [[ ! "$API_KEY" =~ ^sk-ant- ]]; then
            warn "API key does not start with 'sk-ant-'. It may be invalid."
        fi

        # Write ANTHROPIC_API_KEY to {base_dir}/.env
        DOT_ENV_FILE="$BASE_DIR/.env"
        python3 - "$DOT_ENV_FILE" "ANTHROPIC_API_KEY" "$API_KEY" <<'PYEOF'
import re, os, sys
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(path).read().splitlines() if os.path.isfile(path) else []
updated = False
new_lines = []
for line in lines:
    if re.match(rf'^{re.escape(key)}\s*=', line.strip()):
        new_lines.append(f'{key}={value}')
        updated = True
    else:
        new_lines.append(line)
if not updated:
    if new_lines and new_lines[-1].strip():
        new_lines.append('')
    new_lines.append(f'{key}={value}')
open(path, 'w').write('\n'.join(new_lines) + '\n')
os.chmod(path, 0o600)
PYEOF
        success "  Credentials written to $DOT_ENV_FILE"
        ;;

    ollama)
        info "Configuring Ollama provider..."

        # URL
        OLLAMA_URL_FINAL="$(ask "  Ollama API URL" "$OLLAMA_URL")"

        # Test connectivity
        OLLAMA_REACHABLE=false
        echo "  Testing connection to $OLLAMA_URL_FINAL ..."
        if curl -s --connect-timeout 5 "$OLLAMA_URL_FINAL/api/tags" >/dev/null 2>&1; then
            OLLAMA_REACHABLE=true
            success "  Ollama server is reachable"
        else
            warn "Could not reach Ollama server at $OLLAMA_URL_FINAL. Continuing anyway."
        fi

        # Model — Carpenter ships no default LLM. Require the user to
        # explicitly supply one (CLI flag, non-interactive env, or
        # interactive prompt). The available-models list is shown for
        # reference only; we never auto-pick from it.
        if [[ -n "$OLLAMA_MODEL" ]]; then
            # CLI-provided value wins (whether or not interactive).
            OLLAMA_MODEL_FINAL="$OLLAMA_MODEL"
        elif $NON_INTERACTIVE; then
            error "Ollama provider requires --ollama-model in non-interactive mode (no default ships)."
        else
            if $OLLAMA_REACHABLE; then
                echo ""
                echo "  Available models on Ollama server (for reference):"
                MODELS_JSON="$(curl -s --connect-timeout 5 "$OLLAMA_URL_FINAL/api/tags" 2>/dev/null || echo '{}')"
                if command -v python3 &>/dev/null; then
                    MODEL_LIST="$(python3 -c "
import json, sys
try:
    data = json.loads(sys.stdin.read())
    models = [m['name'] for m in data.get('models', [])]
    for i, m in enumerate(models, 1):
        print(f'    {i}) {m}')
    if not models:
        print('    (no models found)')
except Exception:
    print('    (could not parse model list)')
" <<< "$MODELS_JSON")"
                    echo "$MODEL_LIST"
                fi
                echo ""
            fi
            # No default — the user must type a name. Loop until they do.
            while [[ -z "$OLLAMA_MODEL_FINAL" ]]; do
                read -r -p "  Enter Ollama model name (e.g. qwen3:8b, llama3.1:8b): " OLLAMA_MODEL_FINAL
                OLLAMA_MODEL_FINAL="$(echo "$OLLAMA_MODEL_FINAL" | tr -d '[:space:]')"
                if [[ -z "$OLLAMA_MODEL_FINAL" ]]; then
                    warn "Model name is required — Carpenter ships no default."
                fi
            done
        fi

        success "  Ollama URL: $OLLAMA_URL_FINAL"
        success "  Ollama model: $OLLAMA_MODEL_FINAL"
        ;;

    tinfoil)
        info "Configuring Tinfoil provider..."
        API_KEY=""
        if [[ -n "$AI_KEY_FILE" ]]; then
            if [[ ! -f "$AI_KEY_FILE" ]]; then
                error "API key file not found: $AI_KEY_FILE"
            fi
            # Parse KEY=VALUE format or plain key file
            API_KEY="$(grep -oP '(?<=TINFOIL_API_KEY=)\S+' "$AI_KEY_FILE" 2>/dev/null || cat "$AI_KEY_FILE" | tr -d '[:space:]')"
            echo "  Read API key from $AI_KEY_FILE"
        else
            if $NON_INTERACTIVE; then
                error "Tinfoil provider requires --ai-key-file in non-interactive mode."
            fi
            echo ""
            read -s -r -p "  Enter Tinfoil API key: " API_KEY
            echo ""
        fi

        # Write to .env file (standard convention for Tinfoil API keys)
        CREDENTIAL_FILE="$BASE_DIR/.env"
        cat > "$CREDENTIAL_FILE" <<CRED
# Carpenter secrets
# Written by install.sh on $(date -Iseconds)
TINFOIL_API_KEY=$API_KEY
CRED
        chmod 600 "$CREDENTIAL_FILE"
        success "  API key written to $CREDENTIAL_FILE"
        ;;

    skip)
        echo "  You can configure AI later by editing $BASE_DIR/config/config.yaml"
        ;;

    *)
        error "Unknown AI provider: $AI_PROVIDER. Use 'anthropic', 'ollama', 'tinfoil', or 'skip'."
        ;;
esac

echo ""

# ══════════════════════════════════════════════════════════════════════
# 6b. Security / UI Token
# ══════════════════════════════════════════════════════════════════════
if [[ -n "$UI_TOKEN" ]]; then
    # CLI-provided token
    info "Using provided UI token."
elif $SKIP_TOKEN; then
    info "Skipping token generation (no authentication)."
    UI_TOKEN=""
elif $NON_INTERACTIVE; then
    # Auto-generate a random token in non-interactive mode
    UI_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    info "Auto-generated UI access token."
else
    echo ""
    info "Security / UI Token:"
    echo "  A UI token protects the web interface from unauthorized access."
    echo ""
    echo "  1) Generate a random token (recommended)"
    echo "  2) Enter a custom token"
    echo "  3) Skip (no authentication)"
    echo ""
    read -r -p "  Choose [1]: " TOKEN_CHOICE
    TOKEN_CHOICE="${TOKEN_CHOICE:-1}"
    case "$TOKEN_CHOICE" in
        1)
            UI_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
            success "  Generated random token."
            ;;
        2)
            read -r -p "  Enter token: " UI_TOKEN
            if [[ -z "$UI_TOKEN" ]]; then
                error "Token cannot be empty."
            fi
            ;;
        3)
            UI_TOKEN=""
            warn "No token set. The web UI will be unauthenticated."
            ;;
        *)
            error "Invalid choice: $TOKEN_CHOICE"
            ;;
    esac
fi

# Write UI_TOKEN to .env (tokens belong in .env, not in config.yaml)
if [[ -n "$UI_TOKEN" ]]; then
    DOT_ENV_FILE="$BASE_DIR/.env"
    python3 - "$DOT_ENV_FILE" "UI_TOKEN" "$UI_TOKEN" <<'PYEOF'
import re, os, sys
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(path).read().splitlines() if os.path.isfile(path) else []
updated = False
new_lines = []
for line in lines:
    if re.match(rf'^{re.escape(key)}\s*=', line.strip()):
        new_lines.append(f'{key}={value}')
        updated = True
    else:
        new_lines.append(line)
if not updated:
    if new_lines and new_lines[-1].strip():
        new_lines.append('')
    new_lines.append(f'{key}={value}')
open(path, 'w').write('\n'.join(new_lines) + '\n')
os.chmod(path, 0o600)
PYEOF
fi

echo ""

# ══════════════════════════════════════════════════════════════════════
# 6c. Notification Configuration
# ══════════════════════════════════════════════════════════════════════
NOTIFY_EMAIL_ENABLED=false
NOTIFY_EMAIL_MODE=""
NOTIFY_SMTP_HOST=""
NOTIFY_SMTP_PORT="587"
NOTIFY_SMTP_FROM=""
NOTIFY_SMTP_TO=""
NOTIFY_SMTP_USERNAME=""
NOTIFY_SMTP_PASSWORD=""
NOTIFY_SMTP_TLS=true
NOTIFY_COMMAND=""

if $NON_INTERACTIVE; then
    info "Skipping email notification configuration (non-interactive mode)"
    echo "  Notifications will be chat/log only."
else
    echo ""
    info "Email Notifications:"
    echo "  The platform can send email notifications for urgent events,"
    echo "  review requests, and security alerts."
    echo ""
    read -r -p "  Configure email notifications? (y/N): " NOTIFY_CHOICE
    NOTIFY_CHOICE="${NOTIFY_CHOICE:-N}"

    if [[ "$NOTIFY_CHOICE" =~ ^[Yy]$ ]]; then
        NOTIFY_EMAIL_ENABLED=true
        echo ""
        echo "  Email delivery method:"
        echo "    1) SMTP — connect to an SMTP server"
        echo "    2) Command — pipe message to a shell command (e.g., msmtp, sendmail)"
        echo ""
        read -r -p "  Choose [1]: " EMAIL_MODE_CHOICE
        EMAIL_MODE_CHOICE="${EMAIL_MODE_CHOICE:-1}"

        case "$EMAIL_MODE_CHOICE" in
            1)
                NOTIFY_EMAIL_MODE="smtp"
                NOTIFY_SMTP_HOST="$(ask "  SMTP host" "")"
                NOTIFY_SMTP_PORT="$(ask "  SMTP port" "587")"
                NOTIFY_SMTP_FROM="$(ask "  From address" "")"
                NOTIFY_SMTP_TO="$(ask "  To address" "")"
                NOTIFY_SMTP_USERNAME="$(ask "  SMTP username (blank for none)" "")"
                if [[ -n "$NOTIFY_SMTP_USERNAME" ]]; then
                    read -s -r -p "  SMTP password: " NOTIFY_SMTP_PASSWORD
                    echo ""
                fi
                echo ""
                echo "  Use STARTTLS?"
                echo "    1) Yes (recommended for port 587)"
                echo "    2) No"
                echo ""
                read -r -p "  Choose [1]: " TLS_CHOICE
                TLS_CHOICE="${TLS_CHOICE:-1}"
                case "$TLS_CHOICE" in
                    1) NOTIFY_SMTP_TLS=true ;;
                    2) NOTIFY_SMTP_TLS=false ;;
                    *) NOTIFY_SMTP_TLS=true ;;
                esac
                success "  SMTP configured: $NOTIFY_SMTP_HOST:$NOTIFY_SMTP_PORT -> $NOTIFY_SMTP_TO"
                ;;
            2)
                NOTIFY_EMAIL_MODE="command"
                echo ""
                echo "  Enter the shell command that receives the message on stdin."
                echo "  Examples: msmtp user@example.com, sendmail -t, /path/to/script.sh"
                echo ""
                read -r -p "  Command: " NOTIFY_COMMAND
                if [[ -z "$NOTIFY_COMMAND" ]]; then
                    warn "No command specified. Email notifications will be disabled."
                    NOTIFY_EMAIL_ENABLED=false
                else
                    success "  Email command configured: $NOTIFY_COMMAND"
                fi
                ;;
            *)
                warn "Invalid choice. Skipping email configuration."
                NOTIFY_EMAIL_ENABLED=false
                ;;
        esac
    else
        echo "  Notifications will be chat/log only."
    fi
fi

echo ""

# ══════════════════════════════════════════════════════════════════════
# 6d. TLS/SSL Configuration
# ══════════════════════════════════════════════════════════════════════
TLS_ENABLED=false
TLS_DOMAIN=""
TLS_CERT_PATH=""
TLS_KEY_PATH=""
TLS_CA_PATH=""

if $NON_INTERACTIVE; then
    info "Skipping TLS configuration (non-interactive mode)"
    echo "  TLS can be configured later in config.yaml."
else
    echo ""
    info "TLS/SSL Configuration:"
    echo "  Enable HTTPS for encrypted connections (recommended for non-loopback access)."
    echo "  Requires PEM certificate files (Let's Encrypt, self-signed, etc.)."
    echo ""
    read -r -p "  Configure TLS/HTTPS? (y/N): " TLS_CHOICE
    TLS_CHOICE="${TLS_CHOICE:-N}"

    if [[ "$TLS_CHOICE" =~ ^[Yy]$ ]]; then
        TLS_ENABLED=true

        TLS_DOMAIN="$(ask "  Domain name (must match certificate)" "")"
        if [[ -z "$TLS_DOMAIN" ]]; then
            warn "Domain name is required for TLS. Disabling TLS."
            TLS_ENABLED=false
        fi

        if $TLS_ENABLED; then
            TLS_CERT_PATH="$(ask "  Certificate file path (fullchain.pem)" "")"
            TLS_CERT_PATH="${TLS_CERT_PATH/#\~/$HOME}"
            if [[ -z "$TLS_CERT_PATH" ]]; then
                warn "Certificate path is required. Disabling TLS."
                TLS_ENABLED=false
            elif [[ ! -f "$TLS_CERT_PATH" ]]; then
                warn "Certificate file not found: $TLS_CERT_PATH (will be needed at startup)"
            fi
        fi

        if $TLS_ENABLED; then
            TLS_KEY_PATH="$(ask "  Private key file path (privkey.pem)" "")"
            TLS_KEY_PATH="${TLS_KEY_PATH/#\~/$HOME}"
            if [[ -z "$TLS_KEY_PATH" ]]; then
                warn "Key path is required. Disabling TLS."
                TLS_ENABLED=false
            elif [[ ! -f "$TLS_KEY_PATH" ]]; then
                warn "Key file not found: $TLS_KEY_PATH (will be needed at startup)"
            fi
        fi

        if $TLS_ENABLED; then
            echo ""
            echo "  Custom CA certificate (for self-signed certs):"
            echo "  Leave blank if using Let's Encrypt (system CA bundle is sufficient)."
            TLS_CA_PATH="$(ask "  CA cert path (blank for system CA)" "")"
            TLS_CA_PATH="${TLS_CA_PATH/#\~/$HOME}"
            if [[ -n "$TLS_CA_PATH" ]] && [[ ! -f "$TLS_CA_PATH" ]]; then
                warn "CA file not found: $TLS_CA_PATH (will be needed at startup)"
            fi
            success "  TLS configured: https://$TLS_DOMAIN:7842"
        fi
    else
        echo "  Serving HTTP only (suitable for loopback access)."
    fi
fi

echo ""

# ══════════════════════════════════════════════════════════════════════
# 6e. Chat Channels (Telegram, Signal)
# ══════════════════════════════════════════════════════════════════════
TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN_VALUE=""
TELEGRAM_ALLOWED_USERS=""

SIGNAL_ENABLED=false
SIGNAL_CLI_PATH=""
SIGNAL_ACCOUNT=""
SIGNAL_ALLOWED_NUMBERS=""

if $NON_INTERACTIVE; then
    info "Skipping chat channel configuration (non-interactive mode)"
    echo "  Chat channels can be configured later in config.yaml."
else
    echo ""
    info "Chat Channels:"
    echo "  Connect Carpenter to messaging platforms so you can chat"
    echo "  with your agent via Telegram, Signal, etc."
    echo ""

    # ── Telegram ──
    read -r -p "  Enable Telegram bot? (y/N): " TELE_CHOICE
    TELE_CHOICE="${TELE_CHOICE:-N}"

    if [[ "$TELE_CHOICE" =~ ^[Yy]$ ]]; then
        TELEGRAM_ENABLED=true
        echo ""
        echo "  Get a bot token from @BotFather on Telegram."
        read -s -r -p "  Bot token (or press Enter to set TELEGRAM_BOT_TOKEN later): " TELEGRAM_BOT_TOKEN_VALUE
        echo ""

        read -r -p "  Allowed Telegram user IDs (comma-separated, empty=allow all): " TELEGRAM_ALLOWED_USERS

        if [[ -n "$TELEGRAM_BOT_TOKEN_VALUE" ]]; then
            # Write bot token to .env
            DOT_ENV_FILE="$BASE_DIR/.env"
            python3 - "$DOT_ENV_FILE" "TELEGRAM_BOT_TOKEN" "$TELEGRAM_BOT_TOKEN_VALUE" <<'PYEOF'
import re, os, sys
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(path).read().splitlines() if os.path.isfile(path) else []
updated = False
new_lines = []
for line in lines:
    if re.match(rf'^{re.escape(key)}\s*=', line.strip()):
        new_lines.append(f'{key}={value}')
        updated = True
    else:
        new_lines.append(line)
if not updated:
    if new_lines and new_lines[-1].strip():
        new_lines.append('')
    new_lines.append(f'{key}={value}')
open(path, 'w').write('\n'.join(new_lines) + '\n')
os.chmod(path, 0o600)
PYEOF
            success "  Telegram bot token written to $DOT_ENV_FILE"
        else
            echo "  Set TELEGRAM_BOT_TOKEN in $BASE_DIR/.env before starting."
        fi
        success "  Telegram bot enabled (polling mode)"
    fi

    echo ""

    # ── Signal ──
    read -r -p "  Enable Signal? (y/N): " SIG_CHOICE
    SIG_CHOICE="${SIG_CHOICE:-N}"

    if [[ "$SIG_CHOICE" =~ ^[Yy]$ ]]; then
        SIGNAL_ENABLED=true

        # Detect signal-cli
        DEFAULT_SIGNAL_CLI="/usr/local/bin/signal-cli"
        if command -v signal-cli &>/dev/null; then
            DEFAULT_SIGNAL_CLI="$(command -v signal-cli)"
            echo "  Detected signal-cli: $DEFAULT_SIGNAL_CLI"
        fi

        SIGNAL_CLI_PATH="$(ask "  signal-cli path" "$DEFAULT_SIGNAL_CLI")"

        if [[ ! -f "$SIGNAL_CLI_PATH" ]]; then
            warn "signal-cli not found at $SIGNAL_CLI_PATH."
            echo "  Install signal-cli before starting: https://github.com/AsamK/signal-cli"
        elif [[ ! -x "$SIGNAL_CLI_PATH" ]]; then
            warn "signal-cli at $SIGNAL_CLI_PATH is not executable."
        fi

        read -r -p "  Registered phone number (e.g. +1234567890): " SIGNAL_ACCOUNT
        if [[ -z "$SIGNAL_ACCOUNT" ]]; then
            warn "Phone number is required. Register with signal-cli before starting."
        fi

        read -r -p "  Allowed phone numbers (comma-separated, empty=allow all): " SIGNAL_ALLOWED_NUMBERS

        echo ""
        echo "  Note: signal-cli requires a one-time phone number registration."
        echo "  See: signal-cli -a $SIGNAL_ACCOUNT register / link"
        success "  Signal enabled"
    fi
fi

echo ""

# ══════════════════════════════════════════════════════════════════════
# 7. Write config.yaml
# ══════════════════════════════════════════════════════════════════════
info "Writing configuration to $BASE_DIR/config/config.yaml ..."

CONFIG_FILE="$BASE_DIR/config/config.yaml"

# Build AI-specific config lines
AI_CONFIG=""
MODEL_ROLES_BLOCK=""
case "$AI_PROVIDER" in
    anthropic)
        AI_CONFIG="ai_provider: anthropic"
        MODEL_ROLES_BLOCK="# Centralized model selection — all model references go through these slots.
# Format: provider:model (e.g. anthropic:claude-sonnet-4-20250514)
# Resolution: named slot -> 'default' -> auto-detect from ai_provider.
model_roles:
  default: anthropic:claude-sonnet-4-20250514       # Fallback for any unset slot
  chat: anthropic:claude-sonnet-4-20250514           # Primary chat interface
  default_step: anthropic:claude-sonnet-4-20250514   # Default model for arc steps
  title: anthropic:claude-haiku-4-5-20251001         # Title generation (cheap/fast)
  summary: anthropic:claude-haiku-4-5-20251001       # Conversation summaries
  compaction: anthropic:claude-haiku-4-5-20251001    # Context compaction
  code_review: anthropic:claude-sonnet-4-20250514    # Code review pipeline
  review_judge: \"\"                                   # Final review judge (empty = default)
  reflection_daily: anthropic:claude-haiku-4-5-20251001
  reflection_weekly: \"\"                              # empty = uses default
  reflection_monthly: \"\""
        ;;
    ollama)
        AI_CONFIG="ai_provider: ollama
ollama_url: $OLLAMA_URL_FINAL
ollama_model: $OLLAMA_MODEL_FINAL"
        MODEL_ROLES_BLOCK="model_roles:
  default: ollama:$OLLAMA_MODEL_FINAL
  chat: ollama:$OLLAMA_MODEL_FINAL
  default_step: ollama:$OLLAMA_MODEL_FINAL
  title: ollama:$OLLAMA_MODEL_FINAL
  summary: ollama:$OLLAMA_MODEL_FINAL
  compaction: ollama:$OLLAMA_MODEL_FINAL
  code_review: ollama:$OLLAMA_MODEL_FINAL
  review_judge: \"\"
  reflection_daily: \"\"
  reflection_weekly: \"\"
  reflection_monthly: \"\""
        ;;
    tinfoil)
        AI_CONFIG="ai_provider: tinfoil
tinfoil_model: llama3-3-70b"
        MODEL_ROLES_BLOCK="model_roles:
  default: tinfoil:llama3-3-70b
  chat: tinfoil:llama3-3-70b
  default_step: tinfoil:llama3-3-70b
  title: tinfoil:llama3-3-70b
  summary: tinfoil:llama3-3-70b
  compaction: tinfoil:llama3-3-70b
  code_review: tinfoil:llama3-3-70b
  review_judge: \"\"
  reflection_daily: \"\"
  reflection_weekly: \"\"
  reflection_monthly: \"\""
        ;;
    skip)
        AI_CONFIG="# ai_provider: not configured (run install.sh again or edit manually)"
        MODEL_ROLES_BLOCK="# model_roles: not configured (set ai_provider first)
# model_roles:
#   default: anthropic:claude-sonnet-4-20250514
#   chat: \"\"
#   default_step: \"\"
#   title: \"\"
#   summary: \"\"
#   compaction: \"\"
#   code_review: \"\"
#   review_judge: \"\"
#   reflection_daily: \"\"
#   reflection_weekly: \"\"
#   reflection_monthly: \"\""
        ;;
esac

# Build connectors YAML block
CONNECTORS_BLOCK="# Chat channel connectors
# Channels bridge external messaging platforms to Carpenter conversations.
# Tokens/secrets should be set via env vars or .env, not here.
connectors: {}"

if $TELEGRAM_ENABLED || $SIGNAL_ENABLED; then
    CONNECTORS_BLOCK="# Chat channel connectors
connectors:"

    if $TELEGRAM_ENABLED; then
        # Format allowed_users as YAML list
        TELE_USERS_YAML="[]"
        if [[ -n "$TELEGRAM_ALLOWED_USERS" ]]; then
            TELE_USERS_YAML="[$(echo "$TELEGRAM_ALLOWED_USERS" | sed 's/[[:space:]]*//g; s/,/, /g')]"
        fi

        CONNECTORS_BLOCK="$CONNECTORS_BLOCK
  telegram:
    kind: channel
    transport: telegram
    enabled: true
    # bot_token loaded from TELEGRAM_BOT_TOKEN env var / .env
    bot_token: \"\"
    mode: polling            # polling (default) — no public IP needed
    # Webhook mode requires a public domain + TLS. Not part of standard install.
    # To enable later: set mode: webhook, webhook_path: /hooks/telegram
    allowed_users: $TELE_USERS_YAML
    parse_mode: MarkdownV2"
    fi

    if $SIGNAL_ENABLED; then
        # Format allowed_numbers as YAML list
        SIG_NUMS_YAML="[]"
        if [[ -n "$SIGNAL_ALLOWED_NUMBERS" ]]; then
            SIG_NUMS_YAML="[$(echo "$SIGNAL_ALLOWED_NUMBERS" | sed 's/[[:space:]]*//g; s/,/, /g')]"
        fi

        CONNECTORS_BLOCK="$CONNECTORS_BLOCK
  signal:
    kind: channel
    transport: signal
    enabled: true
    signal_cli_path: \"$SIGNAL_CLI_PATH\"
    account: \"$SIGNAL_ACCOUNT\"
    allowed_numbers: $SIG_NUMS_YAML"
    fi
fi

cat > "$CONFIG_FILE" <<YAML
# Carpenter configuration
# Generated by install.sh on $(date -Iseconds)
#
# Configuration precedence: credential env vars (ANTHROPIC_API_KEY, etc.) > {base_dir}/.env > this file > built-in defaults
# See carpenter/config.py for all available keys.

# base_dir is the only path the installer sets.  All other layout-dependent
# paths (database_path, log_dir, code_dir, workspaces_dir, templates_dir,
# tools_dir, data_models_dir, prompts_dir, coding_prompts_dir, kb.dir, ...)
# are derived from base_dir by the server at startup.  See
# carpenter/config.py::_DERIVED_PATH_DEFAULTS for the full mapping.
#
# To override a derived path, set it explicitly in this file.
base_dir: $BASE_DIR

sandbox:
  method: $SANDBOX_METHOD
  on_failure: $SANDBOX_ON_FAILURE

$AI_CONFIG

$MODEL_ROLES_BLOCK

# Agent roles — behavioural config for LM invocations (system prompt, temperature).
# No model field — the model comes from model_roles or per-arc agent_config.
agent_roles:
  security-reviewer:
    system_prompt: >
      You are a security reviewer for agent-generated code. Analyze for injection
      vulnerabilities, unsafe operations, privilege escalation, and exfiltration risks.
    auto_review_output_types: [python, shell]
    temperature: 0.2
  ux-reviewer:
    system_prompt: >
      You are a UX/safety reviewer. Ensure outputs are appropriate, helpful,
      and safe for end users.
    auto_review_output_types: [text, json]
  judge:
    system_prompt: >
      You are the final judge in a multi-reviewer process. Synthesize reviewer
      verdicts and render final approval or rejection.
    auto_review_output_types: []
    temperature: 0.1


host: "127.0.0.1"
port: $PORT
# ui_token is a SECRET — do not set it here.
# It is loaded from UI_TOKEN in {base_dir}/.env (written by the installer).

# TLS/SSL
tls_enabled: $TLS_ENABLED
tls_domain: "$TLS_DOMAIN"
tls_cert_path: "$TLS_CERT_PATH"
tls_key_path: "$TLS_KEY_PATH"
tls_ca_path: "$TLS_CA_PATH"
# tls_key_password: set TLS_KEY_PASSWORD in .env or environment if your private key is encrypted

# Notification channels
notifications:
  email:
    enabled: $NOTIFY_EMAIL_ENABLED
    mode: "$NOTIFY_EMAIL_MODE"
    smtp_host: "$NOTIFY_SMTP_HOST"
    smtp_port: $NOTIFY_SMTP_PORT
    smtp_from: "$NOTIFY_SMTP_FROM"
    smtp_to: "$NOTIFY_SMTP_TO"
    smtp_username: "$NOTIFY_SMTP_USERNAME"
    smtp_password: "$NOTIFY_SMTP_PASSWORD"
    smtp_tls: $NOTIFY_SMTP_TLS
    command: "$NOTIFY_COMMAND"
  batch_window: 60
  routing:
    reflection_actions: low
    review_needed: normal
    security_events: urgent

$CONNECTORS_BLOCK
YAML

success "  Configuration written to $CONFIG_FILE"
echo ""

# ══════════════════════════════════════════════════════════════════════
# 8. Initialize Database
# ══════════════════════════════════════════════════════════════════════
info "Initializing database..."

DB_PATH="$BASE_DIR/data/platform.db"

export CARPENTER_CONFIG="$CONFIG_FILE"

if python3 -c "
from carpenter.db import init_db
init_db()
print('  Database initialized successfully')
"; then
    success "  Database: $DB_PATH"
else
    warn "Database initialization failed. You can initialize it later with:"
    echo "    CARPENTER_CONFIG=$CONFIG_FILE python3 -c 'from carpenter.db import init_db; init_db()'"
fi

echo ""

# ══════════════════════════════════════════════════════════════════════
# 8a. Download Semantic Search Model
# ══════════════════════════════════════════════════════════════════════
info "Setting up semantic search model (all-MiniLM-L6-v2)..."

SEMANTIC_MODEL_DIR="$BASE_DIR/models/all-MiniLM-L6-v2"
SEMANTIC_MODEL_FILE="$SEMANTIC_MODEL_DIR/model.safetensors"

mkdir -p "$SEMANTIC_MODEL_DIR"

if [[ -f "$SEMANTIC_MODEL_FILE" ]]; then
    success "  Model already present: $SEMANTIC_MODEL_FILE"
else
    SEMANTIC_MODEL_URL="https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/model.safetensors"
    info "  Downloading model weights (~91 MB)..."
    echo "  URL: $SEMANTIC_MODEL_URL"

    if command -v wget &>/dev/null; then
        if wget -q --show-progress -O "$SEMANTIC_MODEL_FILE" "$SEMANTIC_MODEL_URL"; then
            success "  Downloaded: $SEMANTIC_MODEL_FILE"
        else
            rm -f "$SEMANTIC_MODEL_FILE"
            warn "Download failed. The model will be downloaded automatically on first use."
        fi
    elif command -v curl &>/dev/null; then
        if curl -L --progress-bar -o "$SEMANTIC_MODEL_FILE" "$SEMANTIC_MODEL_URL"; then
            success "  Downloaded: $SEMANTIC_MODEL_FILE"
        else
            rm -f "$SEMANTIC_MODEL_FILE"
            warn "Download failed. The model will be downloaded automatically on first use."
        fi
    else
        warn "Neither wget nor curl found. The model will be downloaded automatically on first use."
    fi
fi

echo ""

# ══════════════════════════════════════════════════════════════════════
# 8b. Systemd Service (optional)
# ══════════════════════════════════════════════════════════════════════
info "Systemd service setup..."

PYTHON_BIN="$(command -v python3)"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SYSTEMD_USER_DIR/carpenter.service"

if $NON_INTERACTIVE; then
    SETUP_SYSTEMD="no"
else
    echo ""
    ANSWER="$(ask "  Install systemd user service for auto-start?" "y")"
    [[ "$ANSWER" =~ ^[Yy] ]] && SETUP_SYSTEMD="yes" || SETUP_SYSTEMD="no"
fi

if [[ "$SETUP_SYSTEMD" == "yes" ]]; then
    mkdir -p "$SYSTEMD_USER_DIR"
    cat > "$SERVICE_FILE" <<SERVICE
[Unit]
Description=Carpenter AI Agent Platform
After=network.target

[Service]
Type=simple
ExecStart=$PYTHON_BIN -m carpenter
WorkingDirectory=%h
Environment=CARPENTER_CONFIG=$CONFIG_FILE
EnvironmentFile=-%h/carpenter/.env
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
SERVICE

    if systemctl --user daemon-reload 2>/dev/null; then
        success "  Service installed: $SERVICE_FILE"
        echo "  Enable and start with:"
        echo "    systemctl --user enable --now carpenter"
        echo "  Check status with:"
        echo "    systemctl --user status carpenter"
    else
        success "  Service file written: $SERVICE_FILE"
        echo "  (systemctl --user not available — start manually: $PYTHON_BIN -m carpenter)"
    fi
else
    echo "  Skipping systemd setup. To install later, re-run install.sh."
    echo "  Manual start: $PYTHON_BIN -m carpenter"
fi

echo ""

# ══════════════════════════════════════════════════════════════════════
# 9. Plugin Setup (optional: Claude Code or other external tool)
# ══════════════════════════════════════════════════════════════════════
info "Plugin setup..."

# Detect claude binary
CLAUDE_BIN=""
if command -v claude &>/dev/null; then
    CLAUDE_BIN="$(command -v claude)"
    echo "  Detected Claude Code: $CLAUDE_BIN"
fi

# Decide whether to set up a plugin
if [[ -z "$SETUP_PLUGIN" ]]; then
    if $NON_INTERACTIVE; then
        SETUP_PLUGIN="no"
    elif [[ -n "$CLAUDE_BIN" ]]; then
        ANSWER="$(ask "  Set up Claude Code plugin watcher? (recommended)" "y")"
        [[ "$ANSWER" =~ ^[Yy] ]] && SETUP_PLUGIN="yes" || SETUP_PLUGIN="no"
    else
        ANSWER="$(ask "  Set up a plugin watcher? (requires an external tool)" "n")"
        [[ "$ANSWER" =~ ^[Yy] ]] && SETUP_PLUGIN="yes" || SETUP_PLUGIN="no"
    fi
fi

if [[ "$SETUP_PLUGIN" == "yes" ]]; then
    [[ -z "$PLUGIN_NAME" ]] && PLUGIN_NAME="claude-code"

    # Build the setup-plugin command
    SETUP_ARGS=(--name "$PLUGIN_NAME")

    if [[ -n "$PLUGIN_COMMAND" ]]; then
        # User-supplied command
        SETUP_ARGS+=(--command $PLUGIN_COMMAND)
    elif [[ -n "$CLAUDE_BIN" ]]; then
        SETUP_ARGS+=(--command "$CLAUDE_BIN" --print)
    fi

    info "  Running: python3 -m carpenter setup-plugin ${SETUP_ARGS[*]}"
    if CARPENTER_CONFIG="$CONFIG_FILE" python3 -m carpenter setup-plugin "${SETUP_ARGS[@]}"; then
        success "  Plugin '$PLUGIN_NAME' configured."
        echo ""
        echo "  Start the watcher with:"
        echo "    systemctl --user enable --now carpenter-plugin-watcher@$PLUGIN_NAME"
        echo ""
        echo "  Then use from reviewed executor code:"
        echo "    from carpenter_tools.act import plugin"
        echo "    result = plugin.submit_task(plugin_name='$PLUGIN_NAME', prompt='...')"
    else
        warn "Plugin setup failed. You can set it up later with:"
        echo "    python3 -m carpenter setup-plugin --name $PLUGIN_NAME"
    fi
else
    echo "  Skipping plugin setup. To add a plugin later:"
    echo "    python3 -m carpenter setup-plugin --name <name>"
fi

echo ""

# ══════════════════════════════════════════════════════════════════════
# 10. Success Message
# ══════════════════════════════════════════════════════════════════════
printf "${GREEN}${BOLD}"
cat <<SUCCESS
====================================
  Carpenter installed!
====================================
SUCCESS
printf "${NC}"
echo ""
echo "  Configuration: $CONFIG_FILE"
echo "  Database:      $DB_PATH"
echo "  Sandbox:       $SANDBOX_METHOD (on_failure: $SANDBOX_ON_FAILURE)"
if [[ "$AI_PROVIDER" != "skip" ]]; then
    echo "  AI Provider:   $AI_PROVIDER"
fi
if $NOTIFY_EMAIL_ENABLED; then
    echo "  Notifications: email ($NOTIFY_EMAIL_MODE) + chat + log"
else
    echo "  Notifications: chat + log"
fi
CHANNELS_LIST="web"
if $TELEGRAM_ENABLED; then CHANNELS_LIST="$CHANNELS_LIST, telegram"; fi
if $SIGNAL_ENABLED; then CHANNELS_LIST="$CHANNELS_LIST, signal"; fi
echo "  Chat channels: $CHANNELS_LIST"
echo ""
echo "  To start the server:"
echo "    python3 -m carpenter"
echo ""
if $TLS_ENABLED; then
    BASE_URL="https://$TLS_DOMAIN:7842"
else
    BASE_URL="http://localhost:7842"
fi
if [[ -n "$UI_TOKEN" ]]; then
    echo "  Access URL: $BASE_URL/?token=$UI_TOKEN"
    echo "  UI Token:   $UI_TOKEN"
    echo ""
    echo "  Keep this token safe — it is required to access the web UI."
else
    echo "  Then open $BASE_URL in your browser."
    warn "No UI token set. The web interface is unauthenticated."
fi
echo ""
