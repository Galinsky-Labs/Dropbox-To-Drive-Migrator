#!/usr/bin/env bash
# Installs dbx2gdrive as an always-on background service on a Debian/Ubuntu server
# (e.g. a Google Cloud VM). Files stream Dropbox -> Google Drive through the server's
# memory; nothing is stored on its disk except the small progress database.
#
#   curl -fsSL https://raw.githubusercontent.com/Galinsky-Labs/Dropbox-To-Drive-Migrator/main/deploy/install-vm.sh | sudo bash
#
# Extra `migrate` options can be passed via MIGRATE_ARGS, e.g.
#   ... | sudo MIGRATE_ARGS="--workers 8 --dest-folder-name 'From Dropbox'" bash
set -euo pipefail

REPO="${REPO:-https://github.com/Galinsky-Labs/Dropbox-To-Drive-Migrator.git}"
APP=/opt/dbx2gdrive
HOME_DIR=/var/lib/dbx2gdrive
MIGRATE_ARGS="${MIGRATE_ARGS:-}"

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }

echo "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
# A fresh VM runs its own package updates for the first few minutes; wait for them
# instead of failing on the package-manager lock.
APT_WAIT="-o DPkg::Lock::Timeout=900"
if fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; then
  echo "    (the VM is still installing its own first-boot updates; waiting for them to finish...)"
fi
apt-get $APT_WAIT update -qq
apt-get $APT_WAIT install -y -qq git python3 python3-venv >/dev/null

# Small VMs (e.g. e2-micro, 1 GB) have no swap; a memory spike then freezes the whole machine,
# including SSH. A modest swap file turns that into a slowdown instead.
if ! swapon --show | grep -q . && [ "$(awk '/MemTotal/ {print $2}' /proc/meminfo)" -lt 3000000 ]; then
  echo "==> Adding a 1 GB swap file"
  if [ ! -f /swapfile ]; then
    { fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile; } || echo "    (could not create swap; continuing)"
  fi
  swapon /swapfile 2>/dev/null || true
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> Installing dbx2gdrive"
if [ -d "$APP/src/.git" ]; then
  git -C "$APP/src" pull -q --ff-only
else
  install -d "$APP"
  git clone -q --depth 1 "$REPO" "$APP/src"
fi
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q "$APP/src"

id dbx2gdrive >/dev/null 2>&1 || useradd --system --home-dir "$HOME_DIR" --create-home --shell /usr/sbin/nologin dbx2gdrive
install -d -o dbx2gdrive -g dbx2gdrive -m 700 "$HOME_DIR"

# `dbx2gdrive` on this machine always acts on the service's sign-ins and progress database.
cat > /usr/local/bin/dbx2gdrive <<WRAP
#!/bin/sh
exec sudo -u dbx2gdrive DBX2GDRIVE_HOME=$HOME_DIR $APP/venv/bin/dbx2gdrive "\$@"
WRAP
chmod 755 /usr/local/bin/dbx2gdrive

if { [ ! -f "$HOME_DIR/dropbox_token.json" ] || [ ! -f "$HOME_DIR/google_token.json" ]; } && ! ( : </dev/tty ) 2>/dev/null; then
  echo "Sign-ins are missing and there is no terminal to ask for them. Run this script from an SSH window." >&2
  exit 1
fi
if [ ! -f "$HOME_DIR/dropbox_token.json" ]; then
  echo
  echo "==> Step 1 of 2: connect Dropbox (read-only)"
  read -r -p "    Paste your Dropbox app key: " DBX_KEY < /dev/tty
  /usr/local/bin/dbx2gdrive auth dropbox --app-key "$DBX_KEY" < /dev/tty
fi
if [ ! -f "$HOME_DIR/google_token.json" ]; then
  echo
  echo "==> Step 2 of 2: connect Google Drive"
  /usr/local/bin/dbx2gdrive auth google --client-secret - --manual < /dev/tty
fi

echo "==> Creating the background service"
cat > /etc/systemd/system/dbx2gdrive.service <<UNIT
[Unit]
Description=Dropbox to Google Drive migration
Wants=network-online.target
After=network-online.target

[Service]
User=dbx2gdrive
Environment=DBX2GDRIVE_HOME=$HOME_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP/venv/bin/dbx2gdrive migrate --service --log-file $HOME_DIR/migration.log $MIGRATE_ARGS
Restart=always
RestartSec=60
# Keep the machine responsive (SSH, status checks) while the migration runs flat out:
# low CPU/IO priority, and a memory cap so only this service - never sshd - can run out.
# If it hits the cap, systemd restarts it and the migration resumes where it left off.
Nice=10
IOSchedulingClass=idle
MemoryHigh=60%
MemoryMax=75%

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable -q dbx2gdrive
systemctl restart dbx2gdrive

cat <<DONE

==> The migration is running in the background. You can close this window.

  Progress:        open "_Migration status.txt" in the destination folder in Google Drive
                   (or here: sudo dbx2gdrive status)
  Problem files:   sudo dbx2gdrive status --problems
  Live log:        sudo journalctl -u dbx2gdrive -f
  Stop / start:    sudo systemctl stop dbx2gdrive / sudo systemctl start dbx2gdrive
  Google sign-in expired (7-day limit in Testing mode):
                   sudo dbx2gdrive auth google --client-secret - --manual
                   sudo systemctl restart dbx2gdrive
DONE
