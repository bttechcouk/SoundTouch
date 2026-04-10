#!/usr/bin/env bash
# SoundTouch Controller — Ubuntu installer
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_FILE="$SCRIPT_DIR/soundtouch_controller.py"
SERVICE_SRC="$SCRIPT_DIR/soundtouch.service"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
LOCAL_IP="$(hostname -I | awk '{print $1}')"

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
  python3 -c "import edge_tts" &>/dev/null 2>&1 || MISSING_PIP+=(edge-tts)

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

# 5. Firewall (ufw) — open required ports
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Firewall setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Ports we need:
#   8888/tcp  — SoundTouch web UI
#   1900/udp  — SSDP multicast (Alexa device discovery)
#   8082/tcp  — Hue bridge emulator (Alexa smart home control)
#   80/tcp    — Alexa hardcodes port 80 for Hue bridge HTTP (redirected via iptables → 8082)

UFW_ACTIVE=false
if command -v ufw &>/dev/null && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  UFW_ACTIVE=true
fi

if [ "$UFW_ACTIVE" = true ]; then
  echo "  ufw is active — opening required ports…"
  sudo ufw allow 8888/tcp        comment 'SoundTouch web UI'          2>/dev/null && echo "  ✓  8888/tcp        (web UI)"
  sudo ufw allow 1900/udp        comment 'SoundTouch SSDP/Alexa'      2>/dev/null && echo "  ✓  1900/udp        (SSDP discovery)"
  sudo ufw allow 49152:49172/tcp comment 'SoundTouch WeMo devices'    2>/dev/null && echo "  ✓  49152:49172/tcp (Alexa WeMo device ports)"
  sudo ufw allow 80/tcp          comment 'SoundTouch Alexa port 80'   2>/dev/null && echo "  ✓  80/tcp          (Alexa port 80 redirect)"
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
  echo "    sudo ufw allow 49152:49172/tcp"
  echo "    sudo ufw allow 80/tcp"
fi

# 5b. iptables port 80 → 8082 redirect for Alexa Hue bridge
#
# Amazon Echo firmware ignores the port in the SSDP LOCATION header and always
# probes port 80 for the Hue bridge HTTP API.  We redirect inbound port 80 TCP
# to 8082 (where the bridge actually listens) using an iptables NAT rule.
# The rule is made persistent via iptables-persistent / netfilter-persistent.
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Alexa port 80 → 8082 redirect"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

RULE_EXISTS=false
if sudo iptables -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8082 2>/dev/null; then
  RULE_EXISTS=true
fi

if [ "$RULE_EXISTS" = true ]; then
  echo "  ✓  iptables redirect already in place (port 80 → 8082)"
else
  if sudo iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8082 2>/dev/null; then
    echo "  ✓  iptables: added port 80 → 8082 redirect"

    # Persist the rule across reboots
    if command -v netfilter-persistent &>/dev/null; then
      sudo netfilter-persistent save 2>/dev/null && echo "  ✓  Rule saved via netfilter-persistent"
    elif command -v iptables-save &>/dev/null; then
      # Fall back to saving rules file directly if iptables-persistent is installed
      RULES_FILE=/etc/iptables/rules.v4
      if [ -f "$RULES_FILE" ]; then
        sudo iptables-save | sudo tee "$RULES_FILE" > /dev/null && echo "  ✓  Rule saved to $RULES_FILE"
      else
        echo "  ⚠  Rule added for this session but NOT persistent across reboots."
        echo "     Install iptables-persistent to make it permanent:"
        echo "       sudo apt-get install -y iptables-persistent"
        echo "     Then re-run this installer."
      fi
    fi
  else
    echo "  ⚠  Could not add iptables redirect (try running install.sh with sudo)"
    echo "     Run this manually to fix Alexa discovery:"
    echo "       sudo iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8082"
  fi
fi

# Also check for avahi-daemon (it also binds to port 1900 for mDNS/SSDP)
if systemctl is-active --quiet avahi-daemon 2>/dev/null; then
  echo ""
  echo "  ⚠  avahi-daemon is running and shares port 1900."
  echo "     Alexa discovery should still work (both processes receive"
  echo "     multicast packets) but if discovery fails, try:"
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

  echo "✓  Service installed and started"
  echo ""
  echo "  Useful commands:"
  echo "    systemctl --user status  soundtouch   # check status"
  echo "    systemctl --user stop    soundtouch   # stop"
  echo "    systemctl --user start   soundtouch   # start"
  echo "    systemctl --user restart soundtouch   # restart"
  echo "    journalctl --user -u soundtouch -f    # live logs"
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
