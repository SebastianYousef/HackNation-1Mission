#!/usr/bin/env bash
# One-time install on the live server (run as the normal user, no sudo):
#   bash ~/app/deploy/autodeploy/install.sh
# Installs a systemd --user timer that checks origin/main every minute and runs ~/atlas/redeploy.sh when it moved.
# Linger is already on for this user, so the timer keeps running after logout and reboot.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
unit_dir="$HOME/.config/systemd/user"
mkdir -p "$unit_dir" "$HOME/atlas/autodeploy"
install -m 0755 "$here/atlas-autodeploy.sh" "$HOME/atlas/atlas-autodeploy.sh"
install -m 0644 "$here/atlas-autodeploy.service" "$unit_dir/atlas-autodeploy.service"
install -m 0644 "$here/atlas-autodeploy.timer" "$unit_dir/atlas-autodeploy.timer"
# start from what is live now, so the first tick does not redeploy for nothing
git -C "$HOME/app" rev-parse HEAD > "$HOME/atlas/autodeploy/deployed_sha"
systemctl --user daemon-reload
systemctl --user enable --now atlas-autodeploy.timer
systemctl --user list-timers atlas-autodeploy.timer --no-pager
echo "Installed. Log: ~/atlas/autodeploy/autodeploy.log. Stop with: systemctl --user disable --now atlas-autodeploy.timer"
