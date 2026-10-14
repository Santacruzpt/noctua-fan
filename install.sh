#!/bin/sh
# Install noctua-fan on Raspberry Pi OS (Bookworm or later).
#   sudo ./install.sh          fan control service only
#   sudo ./install.sh --api    also the HTTP API on port 9102 (for Open WebUI)
set -eu
cd "$(dirname "$0")"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

API=0
case "${1:-}" in
    --api) API=1 ;;
    "") ;;
    *) echo "usage: sudo ./install.sh [--api]"; exit 1 ;;
esac

CONFIG_TXT=/boot/firmware/config.txt
[ -f "$CONFIG_TXT" ] || CONFIG_TXT=/boot/config.txt
OVERLAY="dtoverlay=pwm,pin=18,func=2"
TOKEN_FILE=/etc/noctua-fan/api-token

apt-get install -y python3-lgpio

install -m 0755 noctua_fan.py /usr/local/bin/noctua-fan
[ -f /etc/noctua-fan.toml ] || install -m 0644 noctua-fan.toml /etc/noctua-fan.toml
install -m 0644 noctua-fan.service /etc/systemd/system/noctua-fan.service

REBOOT=0
if ! grep -qx "$OVERLAY" "$CONFIG_TXT"; then
    printf '\n# Noctua fan: hardware PWM0 on GPIO18\n%s\n' "$OVERLAY" >> "$CONFIG_TXT"
    REBOOT=1
fi

if [ "$API" -eq 1 ]; then
    install -m 0644 noctua-fan-api.service /etc/systemd/system/noctua-fan-api.service
    install -d -m 0750 -g gpio /etc/noctua-fan
    if [ ! -s "$TOKEN_FILE" ]; then
        (umask 027; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$TOKEN_FILE")
        chgrp gpio "$TOKEN_FILE"
    fi
fi

systemctl daemon-reload
systemctl enable noctua-fan.service
[ "$API" -eq 0 ] || systemctl enable noctua-fan-api.service

if [ "$REBOOT" -eq 1 ]; then
    echo "PWM overlay added to $CONFIG_TXT -- reboot to start the fan service."
else
    systemctl restart noctua-fan.service
    [ "$API" -eq 0 ] || systemctl restart noctua-fan-api.service
    echo "noctua-fan running. Try: noctua-fan status"
fi
if [ "$API" -eq 1 ]; then
    echo "HTTP API on port 9102. Token for the Open WebUI tool (valve api_token):"
    echo "  sudo cat $TOKEN_FILE"
fi
