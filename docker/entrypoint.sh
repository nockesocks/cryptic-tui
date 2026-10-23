#!/bin/sh
# Start Samba with this session's password, then idle; the wrapper runs the TUI with `docker exec`.
set -e
: "${CRYPTIC_VAULTS_SMB_PW:?set by the Mac wrapper}"
printf '%s\n%s\n' "$CRYPTIC_VAULTS_SMB_PW" "$CRYPTIC_VAULTS_SMB_PW" | smbpasswd -s -a cryptic-vaults >/dev/null
: > /etc/samba/shares.conf
mkdir -p /media /ctl/shares
rm -f /ctl/shares/*
smbd -D
exec sleep infinity
