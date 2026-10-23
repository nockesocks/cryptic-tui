#!/bin/sh
# Cryptic Vaults installer — installs dependencies and the `cryptic-vaults` command.
#
#   From a clone:   ./install.sh
#   One-liner:      curl -fsSL https://raw.githubusercontent.com/nockesocks/cryptic-vaults/main/install.sh | sh
#   Uninstall:      ./install.sh --uninstall
set -eu

REPO_RAW="https://raw.githubusercontent.com/nockesocks/cryptic-vaults/main"
LIB_DIR="/usr/local/lib/cryptic-vaults"
BIN="/usr/local/bin/cryptic-vaults"

say()  { printf '\033[1;35m::\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

fetch() {  # fetch <repo path> <dest>: copy from this clone, or download when run via curl | sh
    if [ -f "$SRC_DIR/$1" ]; then cp "$SRC_DIR/$1" "$2"
    else curl -fsSL "$REPO_RAW/$1" -o "$2"
    fi || die "download of $1 failed"
}
SRC_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo .)"

# ── macOS: Cryptic Vaults runs in a container inside a Colima VM (docker/cryptic-vaults-mac.py) ──
install_macos() {
    SHARE="$HOME/.local/share/cryptic-vaults"
    SHIM="$HOME/.local/bin/cryptic-vaults"
    CONF="$HOME/.config/cryptic-vaults/tui/mac.json"
    if [ "${1:-}" = "--uninstall" ]; then
        say "removing Cryptic Vaults (its VM, image and command)"
        colima delete -f cryptic-vaults >/dev/null 2>&1 || true
        rm -rf "$SHARE"
        rm -f "$SHIM"
        say "done (your settings in ~/.config/cryptic-vaults/tui were left alone)"
        exit 0
    fi
    /usr/bin/python3 -c '' 2>/dev/null || die "python3 missing — install the command line tools: xcode-select --install"
    for c in colima docker qemu-img; do
        command -v "$c" >/dev/null 2>&1 || die "$c not found — install with: brew install colima docker docker-compose qemu"
    done
    docker compose version >/dev/null 2>&1 || die "docker compose not found — brew install docker-compose"

    say "installing to $SHARE"
    mkdir -p "$SHARE/docker" "$HOME/.local/bin" "$(dirname "$CONF")"
    fetch cryptic_vaults.py "$SHARE/cryptic_vaults.py"
    for f in Dockerfile compose.yml smb.conf entrypoint.sh cryptic-vaults-mac.py; do
        fetch "docker/$f" "$SHARE/docker/$f"
    done
    chmod 755 "$SHARE/docker/entrypoint.sh"

    if [ ! -f "$CONF" ]; then
        printf 'Folder where unlocked sticks appear [~/cryptic-vaults]: '
        read -r folder < /dev/tty || folder=""
        folder="${folder:-~/cryptic-vaults}"
        printf '{"mount_folder": "%s"}\n' "$folder" > "$CONF"
    fi

    /usr/bin/python3 "$SHARE/docker/cryptic-vaults-mac.py" --build
    printf '#!/bin/sh\nexec /usr/bin/python3 "%s/docker/cryptic-vaults-mac.py" "$@"\n' "$SHARE" > "$SHIM"
    chmod 755 "$SHIM"
    case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) warn "add ~/.local/bin to your PATH" ;; esac
    say "done — type  cryptic-vaults  to start"
    exit 0
}
[ "$(uname)" = Darwin ] && install_macos "$@"

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else
    command -v sudo >/dev/null 2>&1 || die "needs root or sudo"
    SUDO="sudo"
fi

if [ "${1:-}" = "--uninstall" ]; then
    say "removing Cryptic Vaults"
    $SUDO rm -f "$BIN"
    $SUDO rm -rf "$LIB_DIR"
    say "done (your settings in ~/.config/cryptic-vaults were left alone)"
    exit 0
fi

# ── 1. system packages ────────────────────────────────────────────────
say "installing dependencies"
if command -v pacman >/dev/null 2>&1; then
    $SUDO pacman -S --needed --noconfirm python python-textual cryptsetup util-linux \
        e2fsprogs btrfs-progs dosfstools exfatprogs smartmontools
elif command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -q
    $SUDO apt-get install -y python3 python3-venv cryptsetup util-linux e2fsprogs \
        btrfs-progs dosfstools exfatprogs smartmontools udev
    $SUDO apt-get install -y python3-textual 2>/dev/null || true
elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf install -y python3 cryptsetup util-linux e2fsprogs btrfs-progs \
        dosfstools exfatprogs smartmontools
    $SUDO dnf install -y python3-textual 2>/dev/null || true
elif command -v zypper >/dev/null 2>&1; then
    $SUDO zypper --non-interactive install python3 cryptsetup util-linux e2fsprogs \
        btrfsprogs dosfstools exfatprogs smartmontools
else
    warn "unknown package manager — install python3, cryptsetup and util-linux yourself"
fi

command -v python3 >/dev/null 2>&1 || die "python3 not found"
command -v cryptsetup >/dev/null 2>&1 || die "cryptsetup not found"

# ── 2. Textual (system package, or a private venv as fallback) ────────
PY="python3"
if python3 -c 'import textual' 2>/dev/null; then
    say "Textual found: $(python3 -c 'import textual; print(textual.__version__)')"
else
    say "Textual not packaged here — installing it into $LIB_DIR/venv"
    $SUDO python3 -m venv "$LIB_DIR/venv" || die "python3 -m venv failed (install python3-venv)"
    $SUDO "$LIB_DIR/venv/bin/pip" install --quiet --upgrade pip textual
    PY="$LIB_DIR/venv/bin/python"
fi

# ── 3. the app ────────────────────────────────────────────────────────
SRC="$(mktemp)"
trap 'rm -f "$SRC"' EXIT
fetch cryptic_vaults.py "$SRC"

say "installing to $LIB_DIR and $BIN"
$SUDO mkdir -p "$LIB_DIR"
$SUDO install -m 755 "$SRC" "$LIB_DIR/cryptic_vaults.py"
$SUDO sh -c "cat > '$BIN'" <<EOF
#!/bin/sh
exec $PY $LIB_DIR/cryptic_vaults.py "\$@"
EOF
$SUDO chmod 755 "$BIN"

say "done — type  cryptic-vaults  to start"
