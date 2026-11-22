#!/bin/bash
# Paste into the VM's "Startup script" field (Edit > Automation). Runs as root on every boot:
# pauses the migration, installs the latest version with its resource limits (so the VM stays
# reachable over SSH), then resumes. Sign-ins and progress are kept.
systemctl stop dbx2gdrive 2>/dev/null
if ! curl -fsSL https://raw.githubusercontent.com/Galinsky-Labs/Dropbox-To-Drive-Migrator/main/deploy/install-vm.sh | bash; then
  systemctl start dbx2gdrive 2>/dev/null
fi
