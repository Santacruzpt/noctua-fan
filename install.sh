#!/bin/sh
# Install noctua-fan on Raspberry Pi OS (Bookworm or later). Run: sudo ./install.sh
set -eu
cd "$(dirname "$0")"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

CONFIG_TXT=/boot/firmware/config.txt
[ -f "$CONFIG_TXT" ] || CONFIG_TXT=/boot/config.txt
OVERLAY="dtoverlay=pwm,pin=18,func=2"

apt-get install -y python3-lgpio

install -m 0755 noctua_fan.py /usr/local/bin/noctua-fan
[ -f /etc/noctua-fan.toml ] || install -m 0644 noctua-fan.toml /etc/noctua-fan.toml
install -m 0644 noctua-fan.service /etc/systemd/system/noctua-fan.service

REBOOT=0
if ! grep -qx "$OVERLAY" "$CONFIG_TXT"; then
    printf '\n# Noctua fan: hardware PWM0 on GPIO18\n%s\n' "$OVERLAY" >> "$CONFIG_TXT"
    REBOOT=1
fi

systemctl daemon-reload
systemctl enable noctua-fan.service

if [ "$REBOOT" -eq 1 ]; then
    echo "PWM overlay added to $CONFIG_TXT -- reboot to start the fan service."
else
    systemctl restart noctua-fan.service
    echo "noctua-fan running. Try: noctua-fan status"
fi
