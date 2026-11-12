#!/usr/bin/env bash
# SoundTouch Controller — installer for Raspberry Pi OS / Debian / Ubuntu.
#
#   bash install.sh             interactive
#   bash install.sh --yes       unattended: install everything incl. Node.js and services
#
# Safe to re-run: it's also how you update after a `git pull` (re-renders the
# services and restarts them; data/ is never touched).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_FILE="$APP_DIR/soundtouch_controller.py"
MATTER_DIR="$APP_DIR/matter_bridge"
DATA_DIR="$APP_DIR/data"
UNIT_DIR="$HOME/.config/systemd/user"
WEB_PORT=8888                 # the Matter bridge expects the controller on 8888
NODE_MIN=20                   # oldest Node.js the bridge runs on
NODE_INSTALL=22               # LTS line installed from NodeSource when needed
PY_MIN="3.10"

ASSUME_YES=false
WANT_SERVICE=""               # "" = ask
WANT_MATTER=""
for arg in "$@"; do
  case "$arg" in
    -y|--yes)      ASSUME_YES=true ;;
    --service)     WANT_SERVICE=y ;;
    --no-service)  WANT_SERVICE=n ;;
    --no-matter)   WANT_MATTER=n ;;
    -h|--help)
      sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
      echo "Options: --yes  --service  --no-service  --no-matter"
      exit 0 ;;
    *) echo "Unknown option: $arg (try --help)"; exit 1 ;;
  esac
done

# ── helpers ──────────────────────────────────────────────────────────────────
hr()   { echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; }
step() { echo; hr; echo "  $*"; hr; }
ok()   { echo "✓  $*"; }
info() { echo "→  $*"; }
warn() { echo "⚠  $*"; }
die()  { echo "❌  $*" >&2; exit 1; }

# ask "question" default(y|n) → returns 0 for yes
ask() {
  local q="$1" def="$2" reply
  if [ "$ASSUME_YES" = true ] || [ ! -t 0 ]; then [ "$def" = y ] || [ "$ASSUME_YES" = true ]; return; fi
  local hint="[y/N]"; [ "$def" = y ] && hint="[Y/n]"
  read -rp "  $q $hint " reply
  reply="${reply:-$def}"
  [[ "$reply" =~ ^[Yy] ]]
}

APT_UPDATED=false
apt_install() {
  if [ "$APT_UPDATED" = false ]; then sudo apt-get update -qq || true; APT_UPDATED=true; fi
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
}

hr; echo "  SoundTouch Controller — Installer"; hr

# ── 0. Sanity checks ─────────────────────────────────────────────────────────
[ "$(id -u)" -ne 0 ] || die "Run this as your normal user (not root / sudo) — it uses sudo itself where needed."
command -v apt-get &>/dev/null || die "This installer needs a Debian-based OS (Raspberry Pi OS, Debian, Ubuntu)."
command -v sudo &>/dev/null || die "sudo is required."
[ -f "$APP_FILE" ] || die "soundtouch_controller.py not found next to install.sh."

ARCH="$(uname -m)"
MODEL="$(tr -d '\0' 2>/dev/null </proc/device-tree/model || true)"
ok "System: ${MODEL:-$(. /etc/os-release && echo "$PRETTY_NAME")} ($ARCH)"
if [ "$ARCH" = armv6l ]; then
  warn "ARMv6 (original Pi Zero / Pi 1): Node.js isn't available, so the Alexa bridge can't run."
  warn "The web controller will still work. A Pi Zero 2 W, Pi 4 or Pi 5 is recommended."
  WANT_MATTER=n
fi

# ── 1. Python ────────────────────────────────────────────────────────────────
step "Python"
command -v python3 &>/dev/null || { info "Installing Python 3…"; apt_install python3; }
PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 -c "import sys; sys.exit(sys.version_info < tuple(map(int, '$PY_MIN'.split('.'))))" \
  || die "Python $PY_VER found; $PY_MIN or newer is needed (Raspberry Pi OS Bookworm or later)."
ok "Python $PY_VER"

# Distro packages first (no PEP 668 friction, prebuilt Pillow on ARM), then pip
# only for whatever still won't import.
declare -A APT_FOR=( [requests]=python3-requests [zeroconf]=python3-zeroconf [PIL]=python3-pil [gtts]=python3-gtts )
declare -A PIP_FOR=( [requests]=requests [zeroconf]=zeroconf [PIL]=Pillow [gtts]=gTTS )
missing_modules() {
  local m out=()
  for m in requests zeroconf PIL gtts; do python3 -c "import $m" &>/dev/null || out+=("$m"); done
  echo "${out[@]:-}"
}

MISSING="$(missing_modules)"
if [ -n "$MISSING" ]; then
  for m in $MISSING; do
    info "apt: ${APT_FOR[$m]}"
    apt_install "${APT_FOR[$m]}" &>/dev/null || true
  done
fi
MISSING="$(missing_modules)"
if [ -n "$MISSING" ]; then
  python3 -m pip --version &>/dev/null || apt_install python3-pip
  for m in $MISSING; do
    info "pip: ${PIP_FOR[$m]}"
    python3 -m pip install --quiet --user --break-system-packages "${PIP_FOR[$m]}" 2>/dev/null \
      || python3 -m pip install --quiet --user "${PIP_FOR[$m]}" || true
  done
fi
for m in requests zeroconf; do
  python3 -c "import $m" &>/dev/null || die "Required Python module '$m' could not be installed."
done
ok "requests, zeroconf"
python3 -c "import PIL"  &>/dev/null && ok "Pillow (album art)"       || warn "Pillow missing — album art falls back to plain icons"
python3 -c "import gtts" &>/dev/null && ok "gTTS (announcements)"     || warn "gTTS missing — the Announce feature is disabled"

mkdir -p "$DATA_DIR"
chmod +x "$APP_FILE"

# ── 2. Node.js + Matter bridge (Alexa) ───────────────────────────────────────
step "Matter bridge (Alexa)"
MATTER_READY=false
NODE_BIN=""

node_major() { node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0; }

if [ "$WANT_MATTER" = n ]; then
  info "Skipping the Alexa bridge."
else
  if ! command -v node &>/dev/null || [ "$(node_major)" -lt "$NODE_MIN" ]; then
    if command -v node &>/dev/null; then
      warn "Node.js $(node --version) is too old for the bridge (needs v$NODE_MIN+)."
    else
      warn "Node.js isn't installed."
    fi
    if ask "Install Node.js $NODE_INSTALL LTS from NodeSource now?" y; then
      apt_install ca-certificates curl gnupg
      curl -fsSL "https://deb.nodesource.com/setup_${NODE_INSTALL}.x" | sudo -E bash - \
        && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs \
        || warn "Node.js install failed."
      hash -r
    fi
  fi

  if command -v node &>/dev/null && [ "$(node_major)" -ge "$NODE_MIN" ]; then
    NODE_BIN="$(command -v node)"
    ok "Node.js $(node --version)"
    info "Installing bridge packages (can take a few minutes on a Pi)…"
    NPM_CMD=install; [ -f "$MATTER_DIR/package-lock.json" ] && NPM_CMD=ci
    if ( cd "$MATTER_DIR" && npm "$NPM_CMD" --omit=dev --no-fund --no-audit --loglevel=error ); then
      MATTER_READY=true
      ok "Bridge packages installed"
    else
      warn "npm $NPM_CMD failed — the Alexa bridge won't be installed. The web controller still works."
    fi
  else
    warn "No Node.js $NODE_MIN+ — skipping the Alexa bridge. The web controller works without it."
  fi
fi

# ── 3. Spotify Client ID (optional) ──────────────────────────────────────────
step "Spotify (optional)"
SP_APP="$DATA_DIR/spotify/app.json"
if [ -n "${SPOTIFY_CLIENT_ID:-}" ] || [ -s "$SP_APP" ]; then
  ok "Spotify Client ID already configured"
elif [ "$ASSUME_YES" = false ] && [ -t 0 ]; then
  echo "  The Spotify tab needs a Client ID from https://developer.spotify.com/dashboard"
  echo "  (redirect URI: http://127.0.0.1:$WEB_PORT/api/spotify/callback). Leave blank to skip."
  read -rp "  Spotify Client ID: " SP_ID
  SP_ID="$(echo "$SP_ID" | tr -d '[:space:]')"
  if [ -n "$SP_ID" ]; then
    mkdir -p "$DATA_DIR/spotify"; chmod 700 "$DATA_DIR/spotify"
    printf '{"client_id": "%s"}\n' "$SP_ID" > "$SP_APP"; chmod 600 "$SP_APP"
    ok "Saved to data/spotify/app.json"
  else
    info "Skipped — add it later by re-running this script."
  fi
else
  info "Not configured (re-run interactively, or write data/spotify/app.json)."
fi

# ── 4. Firewall ──────────────────────────────────────────────────────────────
step "Firewall"
if command -v ufw &>/dev/null && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  sudo ufw allow $WEB_PORT/tcp comment 'SoundTouch web UI'    >/dev/null && ok "$WEB_PORT/tcp (web UI)"
  sudo ufw allow 1900/udp comment 'SoundTouch SSDP/DLNA'       >/dev/null && ok "1900/udp (DLNA discovery)"
  sudo ufw allow 5353/udp comment 'SoundTouch mDNS discovery'  >/dev/null && ok "5353/udp (speaker discovery)"
  sudo ufw allow 5540/udp comment 'SoundTouch Matter bridge'   >/dev/null && ok "5540/udp (Alexa)"
else
  ok "No active ufw firewall — nothing to open"
fi

# ── 5. Services ──────────────────────────────────────────────────────────────
step "Background services"
if [ -z "$WANT_SERVICE" ]; then
  echo "  Runs the controller (and Alexa bridge) at boot and restarts it if it crashes."
  if ask "Install as services?" y; then WANT_SERVICE=y; else WANT_SERVICE=n; fi
fi

render() {   # render <template> <dest>
  sed -e "s|@APP_DIR@|$APP_DIR|g" \
      -e "s|@PYTHON@|$(command -v python3)|g" \
      -e "s|@NODE@|$NODE_BIN|g" \
      -e "s|@PORT@|$WEB_PORT|g" \
      "$1" > "$2"
}

SERVICES_OK=false
if [ "$WANT_SERVICE" = y ]; then
  mkdir -p "$UNIT_DIR"

  # Lingering = user services start at boot, without anyone logging in.
  loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER" \
    || warn "Couldn't enable lingering — services will only run while you're logged in."

  # A user manager must be reachable (it isn't under some `sudo su` shells).
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

  render "$APP_DIR/soundtouch.service" "$UNIT_DIR/soundtouch.service"
  UNITS=(soundtouch.service)
  if [ "$MATTER_READY" = true ]; then
    render "$APP_DIR/soundtouch-matter.service" "$UNIT_DIR/soundtouch-matter.service"
    UNITS+=(soundtouch-matter.service)
  fi

  systemctl --user daemon-reload
  systemctl --user enable "${UNITS[@]}" >/dev/null 2>&1
  systemctl --user restart "${UNITS[@]}"
  ok "Installed and (re)started: ${UNITS[*]}"

  # Wait for the web UI to answer.
  info "Waiting for the controller to come up…"
  for _ in $(seq 1 30); do
    if python3 - "$WEB_PORT" <<'PY' 2>/dev/null
import sys, urllib.request
urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/speakers", timeout=2)
PY
    then SERVICES_OK=true; break; fi
    sleep 1
  done
  if [ "$SERVICES_OK" = true ]; then
    ok "Controller is answering on port $WEB_PORT"
  else
    warn "Controller didn't answer within 30 s. Check: journalctl --user -u soundtouch -n 50"
  fi
else
  info "Skipped. Run it by hand with:  python3 $APP_FILE"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo; hr; echo "  Done!"; hr
echo
echo "  Open on any phone or computer on the same Wi-Fi:"
echo "    http://${LOCAL_IP:-<this-ip>}:$WEB_PORT"
echo "    http://$(hostname).local:$WEB_PORT"
echo
echo "  Next steps:"
echo "   • Give this Pi and each speaker a fixed (reserved) address in the router's"
echo "     DHCP settings, so nothing moves around after a power cut."
if [ "$MATTER_READY" = true ]; then
  echo "   • Alexa: open Settings → Alexa Integration and scan the QR code in the"
  echo "     Alexa app (Devices → + → Add Device → Matter)."
fi
if [ "$WANT_SERVICE" = y ]; then
  echo
  echo "  Useful commands:"
  echo "    systemctl --user status soundtouch        # is it running?"
  echo "    journalctl --user -u soundtouch -f        # live logs"
  [ "$MATTER_READY" = true ] && echo "    journalctl --user -u soundtouch-matter -f # Alexa bridge logs"
  echo "    git pull && bash install.sh --yes         # update"
fi
echo
