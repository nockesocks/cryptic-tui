#!/bin/sh
# Start Samba with this session's password, then idle; the wrapper runs the TUI with `docker exec`.
set -e
: "${CRYPTIC_VAULTS_SMB_PW:?set by the Mac wrapper}"
printf '%s\n%s\n' "$CRYPTIC_VAULTS_SMB_PW" "$CRYPTIC_VAULTS_SMB_PW" | smbpasswd -s -a cryptic-vaults >/dev/null
if [ "${CRYPTIC_VAULTS_SMB_ROOT:-}" = 1 ]; then  # --root-share; new files take their parent folder's owner, not root's
  sed -i 's/^  force user = .*/  force user = root\n  inherit owner = unix only/' /etc/samba/smb.conf
fi
: > /etc/samba/shares.conf
mkdir -p /media /ctl/shares
rm -f /ctl/shares/*
smbd -D
exec sleep infinity
