# Cryptic Vaults on macOS: the Linux tool and its disk tools, plus Samba to share mounts back to the Mac.
# Built by install.sh from the repo root: docker build -f docker/Dockerfile .
FROM debian:trixie-slim
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 python3-textual cryptsetup-bin util-linux fdisk udev \
        e2fsprogs btrfs-progs xfsprogs dosfstools exfatprogs ntfs-3g \
        samba samba-vfs-modules \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 -s /usr/sbin/nologin cryptic-vaults
COPY docker/smb.conf /etc/samba/smb.conf
COPY docker/entrypoint.sh /entrypoint.sh
COPY cryptic_vaults.py /app/cryptic_vaults.py
ENTRYPOINT ["/entrypoint.sh"]
