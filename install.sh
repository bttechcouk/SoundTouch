#!/usr/bin/env bash
# SoundTouch Controller — Ubuntu installer
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_FILE="$SCRIPT_DIR/soundtouch_controller.py"
SERVICE_SRC="$SCRIPT_DIR/soundtouch.service"
MATTER_SERVICE_SRC="$SCRIPT_DIR/soundtouch-matter.service"
MATTER_DIR="$SCRIPT_DIR/matter_bridge"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
LOCAL_IP="$(hostname -I | awk '{print $1}')"
MATTER_READY=false   # set true once Node.js + npm deps are in place

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  SoundTouch Controller — Installer"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 1. Check Python 3
if ! command -v python3 &>/dev/null; then
  echo "❌  Python 3 is required but not found."
  echo "    Run: sudo apt install python3"
  exit 1
fi
echo "✓  Python 3: $(python3 --version)"

# 2. Install pip if missing
if ! python3 -m pip --version &>/dev/null; then
  echo "→  Installing pip…"
  sudo apt-get install -y python3-pip
fi

# 3. Install Python dependencies
echo "→  Installing Python packages…"

pip_install() {
  PKGS_APT=(python3-requests python3-zeroconf python3-pil)
  MISSING_APT=()
  for pkg in "${PKGS_APT[@]}"; do
    dpkg -s "$pkg" &>/dev/null || MISSING_APT+=("$pkg")
  done
  if [ ${#MISSING_APT[@]} -gt 0 ]; then
    echo "  → apt: ${MISSING_APT[*]}"
    sudo apt-get install -y "${MISSING_APT[@]}" 2>/dev/null || true
  fi

  MISSING_PIP=()
  for pkg in requests zeroconf; do
    python3 -c "import $pkg" &>/dev/null 2>&1 || MISSING_PIP+=("$pkg")
  done
  python3 -c "import PIL" &>/dev/null 2>&1 || MISSING_PIP+=(Pillow)

  if [ ${#MISSING_PIP[@]} -gt 0 ]; then
    echo "  → pip: ${MISSING_PIP[*]}"
    if python3 -m pip install --quiet --break-system-packages "${MISSING_PIP[@]}" 2>/dev/null; then :
    elif python3 -m pip install --quiet --user "${MISSING_PIP[@]}" 2>/dev/null; then :
    else python3 -m pip install --quiet "${MISSING_PIP[@]}" || true
    fi
  fi
}
pip_install
echo "✓  Packages installed"

# 4. Make script executable
chmod +x "$APP_FILE"
echo "✓  Script is executable"

# 4b. Node.js + Matter bridge dependencies (Alexa smart home integration)
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Matter bridge (Alexa) — Node.js setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

NODE_BIN=""
NODE_OK=false
if command -v node &>/dev/null; then
  NODE_BIN="$(command -v node)"
  NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
  if [ "${NODE_MAJOR:-0}" -ge 20 ] 2>/dev/null; then
    NODE_OK=true
    echo "✓  Node.js: $(node --version)"
  else
    echo "⚠  Node.js $(node --version) found, but the Matter bridge needs v20 LTS or newer."
  fi
else
  echo "⚠  Node.js not found — the Matter bridge (Alexa integration) will not run."
fi

if [ "$NODE_OK" = true ] && [ -d "$MATTER_DIR" ]; then
  echo "→  Installing Matter bridge npm packages…"
  if ( cd "$MATTER_DIR" && npm install --no-fund --no-audit ); then
    echo "✓  Matter bridge dependencies installed"
    MATTER_READY=true
  else
    echo "⚠  npm install failed in $MATTER_DIR — the Matter bridge may not start."
  fi
elif [ "$NODE_OK" = false ]; then
  echo ""
  echo "  To enable the Matter / Alexa bridge, install Node.js 20 LTS then re-run:"
  echo "    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -"
  echo "    sudo apt-get install -y nodejs"
  echo ""
  echo "  (The SoundTouch web controller works fine without it.)"
fi

# 5. Firewall (ufw) — open required ports
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Firewall setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Ports we need:
#   8888/tcp  — SoundTouch web UI
#   1900/udp  — SSDP multicast (DLNA server announcements)
#   5540/udp  — Matter protocol (Alexa smart home)

UFW_ACTIVE=false
if command -v ufw &>/dev/null && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  UFW_ACTIVE=true
fi

if [ "$UFW_ACTIVE" = true ]; then
  echo "  ufw is active — opening required ports…"
  sudo ufw allow 8888/tcp comment 'SoundTouch web UI'        2>/dev/null && echo "  ✓  8888/tcp  (web UI)"
  sudo ufw allow 1900/udp comment 'SoundTouch SSDP/DLNA'     2>/dev/null && echo "  ✓  1900/udp  (SSDP / DLNA discovery)"
  sudo ufw allow 5540/udp comment 'SoundTouch Matter bridge' 2>/dev/null && echo "  ✓  5540/udp  (Matter / Alexa)"
  sudo ufw reload 2>/dev/null && echo "  ✓  ufw reloaded"
else
  if ! command -v ufw &>/dev/null; then
    echo "  ufw not installed — skipping (no firewall changes needed)"
  else
    echo "  ufw is installed but not active — no rules added"
  fi
  echo ""
  echo "  If you enable ufw later, run these commands:"
  echo "    sudo ufw allow 8888/tcp"
  echo "    sudo ufw allow 1900/udp"
  echo "    sudo ufw allow 5540/udp"
fi

# Also check for avahi-daemon (it also binds to port 1900 for mDNS/SSDP)
if systemctl is-active --quiet avahi-daemon 2>/dev/null; then
  echo ""
  echo "  ⚠  avahi-daemon is running and shares port 1900."
  echo "     DLNA/SSDP discovery should still work (both processes receive"
  echo "     multicast packets) but if it fails, try:"
  echo "     sudo systemctl stop avahi-daemon"
fi

# 6. Ask about running as a background service
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Run as a background service?"
echo ""
echo "  A systemd user service will:"
echo "  • Start automatically when you log in"
echo "  • Keep running after you close your SSH session"
echo "  • Restart automatically if it crashes"
echo ""
read -rp "  Install as a systemd service? [y/N] " INSTALL_SERVICE
echo ""

if [[ "$INSTALL_SERVICE" =~ ^[Yy]$ ]]; then
  # Write a resolved copy of the service file (substitutes real paths)
  mkdir -p "$SYSTEMD_USER_DIR"
  sed \
    -e "s|%h|$HOME|g" \
    -e "s|%i|$USER|g" \
    "$SERVICE_SRC" > "$SYSTEMD_USER_DIR/soundtouch.service"

  # Enable lingering so the service runs even when not logged in via SSH
  if command -v loginctl &>/dev/null; then
    loginctl enable-linger "$USER" 2>/dev/null || true
  fi

  systemctl --user daemon-reload
  systemctl --user enable --now soundtouch.service
  echo "✓  Controller service installed and started"

  # Matter bridge service — only if Node.js deps were installed in step 4b.
  if [ "$MATTER_READY" = true ] && [ -f "$MATTER_SERVICE_SRC" ]; then
    sed \
      -e "s|^WorkingDirectory=.*|WorkingDirectory=$MATTER_DIR|" \
      -e "s|^ExecStart=.*|ExecStart=$NODE_BIN matter_bridge.js|" \
      "$MATTER_SERVICE_SRC" > "$SYSTEMD_USER_DIR/soundtouch-matter.service"
    systemctl --user daemon-reload
    systemctl --user enable --now soundtouch-matter.service
    echo "✓  Matter bridge service installed and started"
  elif [ -f "$MATTER_SERVICE_SRC" ]; then
    echo "ℹ  Skipped Matter bridge service (Node.js 20+ / npm deps not installed)."
  fi

  echo ""
  echo "  Useful commands:"
  echo "    systemctl --user status  soundtouch   # check status"
  echo "    systemctl --user stop    soundtouch   # stop"
  echo "    systemctl --user start   soundtouch   # start"
  echo "    systemctl --user restart soundtouch   # restart"
  echo "    journalctl --user -u soundtouch -f    # live logs"
  if [ "$MATTER_READY" = true ]; then
    echo "    journalctl --user -u soundtouch-matter -f   # Matter bridge logs"
  fi
else
  echo "  Skipping service install."
  echo ""
  echo "  To run manually:"
  echo "    python3 $APP_FILE              # foreground (Ctrl+C to stop)"
  echo "    python3 $APP_FILE --daemon     # background (survives SSH disconnect)"
  echo ""
  echo "  To install the service later, re-run this script."
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Done!"
echo ""
echo "  Open in any browser on the same Wi-Fi:"
echo "    http://$LOCAL_IP:8888"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
