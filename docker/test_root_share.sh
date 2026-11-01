#!/bin/sh
# On the Mac: a throwaway Samba container on its own port, mounted the way the wrapper mounts a share.
HELP='NAME
  test_root_share.sh - check --root-share with the real Mac SMB client
  Author: Cryptic Vaults

USAGE
  docker/test_root_share.sh <empty-dir>

ARGS:

  <empty-dir>      where to mount the test share
  --help           shows this menu

EXAMPLES:

  /path/to/empty-folder

  Run with the VM up after changing the share user settings. Safe
  beside a running session: it uses port 4451 and its own container.

  docker/test_root_share.sh ~/scratch/mnt'

[ "${1:-}" = --help ] && { echo "$HELP"; exit 0; }
[ -d "${1:-}" ] || { echo "usage: test_root_share.sh <empty-dir>; see --help"; exit 2; }
MNT=$1
HERE=$(cd "$(dirname "$0")" && pwd)
NAME=cv-rootshare-test
PW=test-pw-only
D() { docker --context colima-cryptic-vaults "$@"; }
stop() { /sbin/umount "$MNT" 2>/dev/null; D rm -f "$NAME" >/dev/null 2>&1; }
fail() { echo "FAIL: $1"; stop; exit 1; }
trap stop EXIT

start() {  # $1: 0 or 1, the flag's value
  stop
  D run -d --name "$NAME" --network host -v "$HERE:/t:ro" -e CRYPTIC_VAULTS_SMB_PW=$PW -e CRYPTIC_VAULTS_SMB_ROOT="$1" \
    --entrypoint sh cryptic-vaults:latest -c '
      sed "s/^  smb ports = .*/  smb ports = 4451/" /t/smb.conf > /etc/samba/smb.conf
      mkdir -p /srv/v/user && chown 1000:1000 /srv/v/user
      echo secret > /srv/v/user/rootfile && chmod 400 /srv/v/user/rootfile
      exec sh /t/entrypoint.sh' >/dev/null || fail "container did not start"
  sleep 2
  D exec "$NAME" sh -c 'printf "[v]\n  path = /srv/v\n" > /etc/samba/shares.conf; smbcontrol smbd reload-config'
  i=0
  until /sbin/mount_smbfs -N "//cryptic-vaults:$PW@localhost:4451/v" "$MNT" 2>/dev/null; do
    i=$((i + 1)); [ $i -lt 20 ] || fail "could not mount the share"; sleep 1
  done
}
owner() { D exec "$NAME" stat -c %u "/srv/v/user/$1"; }

echo "flag off: the root-only file stays unreadable (waits up to 20s for the mount)"
start 0
cat "$MNT/user/rootfile" >/dev/null 2>&1 && fail "flag off read a root-only file"

echo "flag on: read, create and overwrite (waits up to 20s for the mount)"
start 1
[ "$(cat "$MNT/user/rootfile")" = secret ] || fail "flag on could not read a root-only file"
echo hello > "$MNT/user/created" || fail "flag on could not create a file"
[ "$(owner created)" = 1000 ] || fail "new file owner is $(owner created), not the folder's"
echo changed > "$MNT/user/rootfile" || fail "flag on could not overwrite a root-only file"
[ "$(D exec "$NAME" cat /srv/v/user/rootfile)" = changed ] || fail "the overwrite did not reach the disk"
[ "$(owner rootfile)" = 0 ] || fail "overwrite changed the owner to $(owner rootfile)"
echo PASS
