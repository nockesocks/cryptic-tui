<div align="center">

<img src="assets/icon.svg" width="112" alt="Cryptic icon">

# Cryptic TUI

*LUKS2 encrypted drives from your terminal, on Linux and macOS*

![Linux](https://img.shields.io/badge/Linux-FCC624?style=flat-square&logo=linux&logoColor=black)
![macOS](https://img.shields.io/badge/macOS-555?style=flat-square&logo=apple&logoColor=white)
![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776ab?style=flat-square&logo=python&logoColor=white)
![Textual](https://img.shields.io/badge/TUI-Textual-8d66ce?style=flat-square)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue?style=flat-square)](LICENSE)

[Features](#features) • [Install](#install) • [Usage](#usage) • [macOS](#on-macos) • [Troubleshooting](#troubleshooting)

</div>

<p align="center"><img src="assets/main.svg" alt="cryptic showing three USB drives, one of them unlocked and mounted" width="900"></p>

Cryptic TUI is a keyboard-driven manager for encrypted drives. Plug in a USB stick, unlock it, use your files, lock it again, without remembering a single `cryptsetup` flag. It can also encrypt a new drive, make encrypted container files, and wipe what you no longer need.

Everything it creates is standard **LUKS2**, the format `cryptsetup` uses on Linux. A drive made here opens on any Linux machine, with or without Cryptic, and in [Cryptic for Android](https://github.com/nockesocks/cryptic), so one stick moves between your desktop and your phone.

## Built for privacy

- No telemetry, analytics, ads or account.
- Nothing is sent anywhere. On macOS the only network traffic is between your Mac and its own local VM.
- Passphrases are passed straight to `cryptsetup` and never written to disk.
- Your system drive is protected, and anything that erases a drive makes you type its name first.

## Features

- **Unlock and mount** – LUKS2 and LUKS1 drives and partitions, with a second chance when you mistype the passphrase.
- **Encrypt a drive** – Fresh GPT, one partition, LUKS2 with AES-XTS-512, Argon2id and SHA-512, then ext4, exFAT, btrfs, XFS, FAT or NTFS, whichever tools are installed.
- **Encrypted containers** – Keep an encrypted "vault" file on a normal drive without touching the rest of it.
- **Format and wipe** – Quick signature wipe, zeros, random data, or TRIM for SSDs.
- **Edit** – Rename a drive or change its passphrase from its details.
- **Details** – Disk, USB, partition and LUKS header information, plus SMART health on Linux.
- **macOS support** – The same app on a Mac, with your unlocked drives showing up in Finder. See [On macOS](#on-macos).
- **Themes** – Three purple Catppuccin-style themes, with an optional transparent background.

## Install

### Linux

```sh
git clone https://github.com/nockesocks/cryptic-tui.git && cd cryptic-tui && ./install.sh
```

Or in one line:

```sh
curl -fsSL https://raw.githubusercontent.com/nockesocks/cryptic-tui/main/install.sh | sh
```

The installer adds what cryptic needs (Python, [Textual](https://textual.textualize.io), cryptsetup and the filesystem tools) with `pacman`, `apt`, `dnf` or `zypper`, then installs the `cryptic` command.

### macOS

cryptic runs in a small Linux VM on your Mac (tested on Apple silicon), so first install the tools it uses:

```sh
brew install colima docker docker-compose qemu
```

Then run the same installer. It asks where unlocked drives should appear (default `~/cryptic`) and builds cryptic's image.

```sh
git clone https://github.com/nockesocks/cryptic-tui.git && cd cryptic-tui && ./install.sh
```

> [!IMPORTANT]
> cryptic reads your drives directly, which macOS only allows with **Full Disk Access** (System Settings ▸ Privacy & Security ▸ Full Disk Access). Grant it to your terminal app **and** to any multiplexer you run inside it (tmux, zellij…), then restart them. Without it, drives never show up.

To remove cryptic, run `./install.sh --uninstall`. Your settings are kept.

## Usage

```sh
cryptic
```

Type your sudo password when asked: cryptic needs root to unlock and mount drives.

### Keys

| Key | What it does |
|---|---|
| `↑` `↓` | Pick a drive |
| `space` | Details |
| `m` | Mount (asks for the passphrase if the drive is encrypted) |
| `u` | Unmount, and lock it if it's encrypted |
| `e` | Encrypt (erases the drive) |
| `f` | Format (erases the drive) |
| `w` | Wipe (erases the drive) |
| `c` | Make an encrypted container file |
| `a` | Open an existing container file |
| `d` | Drives on this Mac (macOS only) |
| `r` | Rescan |
| `s` | Settings |
| `l` | Command log: every command cryptic has run |
| `ctrl+p` | Menu: themes, settings, log |
| `q` | Quit |

### Details

<p align="center"><img src="assets/details.svg" alt="The details panel of an unlocked drive" width="900"></p>

Press `space` on a drive to see everything about it. **:: edit ::** at the bottom renames the drive or changes its passphrase.

### How to…

<details>
<summary><b>Encrypt a USB stick</b></summary>

1. Plug it in and run `cryptic`.
2. Pick the drive marked `(usb)` and press `e`.
3. Type a passphrase twice.
4. Type the drive's name (for example `/dev/sdb`) to confirm, then press **Encrypt**.

</details>

<details>
<summary><b>Open an encrypted drive</b></summary>

1. Pick it (it says `● locked`) and press `m`.
2. Type your passphrase and press **Mount**.

</details>

<details>
<summary><b>Encrypt only part of a drive</b></summary>

1. Pick the drive and press `m` to mount it.
2. Press `c`, choose a file name and size (for example `10G`), and type a passphrase twice.
3. You get an encrypted vault file on the drive. Nothing else is touched.

To open it later, press `a` and give the path to the vault file.

</details>

<details>
<summary><b>Unplug safely</b></summary>

Pick the drive and press `u`. When it says `● locked`, unplug it.

</details>

## On macOS

macOS can't read LUKS or ext4, so cryptic brings its own Linux. When you start it, cryptic boots a small VM, plugs your USB drives into it, and opens the same app you'd use on Linux. Each drive you unlock is shared back to your Mac and appears in your mount folder, so Finder and every other app can use it.

- **Drives load on their own.** Plug one in before or after starting cryptic and it shows up within a few seconds.
- **Files appear in `~/cryptic/<name>`.** A mounted drive's details have **Open in Finder**, **Terminal here** and **Copy path** buttons.
- **`u` unmounts from the Mac first**, then locks the drive. If the Mac still has files open on it, cryptic asks you to close them.
- **`q` closes everything**, ejects the drives cryptic was holding (safe to unplug), and shuts the VM down.

### Choosing which drives cryptic takes

<p align="center"><img src="assets/mac-drives.svg" alt="The drives pane on macOS" width="900"></p>

Every drive is loaded into cryptic by default, including ones you might only want to copy files from. Two ways to hand one back:

- In a drive's details, press **Release to Mac**, or switch **Auto-load** off so cryptic leaves it alone next time.
- Press `d` for every drive plugged in right now, with its Auto-load switch and a **Load** or **Release** button.

cryptic remembers your choice for each drive, so a drive you released once stays with your Mac until you switch it back on.

> [!NOTE]
> On a Mac, SMART health isn't available: inside the VM a USB drive is a plain virtual disk.

## Troubleshooting

<details>
<summary><b>Unmounting says "target is busy"</b></summary>

Something still has files open on the drive. Close any terminal, file manager or app using it and press `u` again.

</details>

<details>
<summary><b>macOS: no drives show up, or the log says "Operation not permitted"</b></summary>

Your terminal, or a multiplexer inside it, lacks Full Disk Access. See the [macOS install](#macos) note, then restart the app you granted it to. Errors from the Mac side are written to `~/.config/cryptic/tui/cryptic-mac.log`.

</details>

<details>
<summary><b>macOS: cryptic took a drive I wanted to use on the Mac</b></summary>

Open its details and press **Release to Mac**, and switch **Auto-load** off so it isn't taken next time. Press `d` to bring it back later.

</details>

## Where things are

| | Linux | macOS |
|---|---|---|
| `cryptic` command | `/usr/local/bin/cryptic` | `~/.local/bin/cryptic` |
| The app | `/usr/local/lib/cryptic/` | `~/.local/share/cryptic/` |
| Settings | `~/.config/cryptic/settings.json` | `~/.config/cryptic/tui/` |

> [!NOTE]
> Cryptic TUI is free software under the [GNU General Public License v3.0](LICENSE): use it, study it, change it and share it. If you distribute a version of it, changed or not, its source goes with it under the same license.
