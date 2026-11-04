# Gitet

Install and update Linux apps straight from GitHub releases with one command.

```bash
gitet install dpaulat/SupercellWX
```

Gitet finds the repo, grabs the latest release, picks the best package for your system, checks that it matches your CPU architecture, and installs it.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/AstroMeYT/Gitet/main/install.sh | bash
```

This puts `gitet` in `~/.local/bin` and adds that folder to your `PATH` if needed. Open a new terminal afterwards.

Other options:

```bash
# System-wide (/usr/local/bin, uses sudo)
curl -fsSL https://raw.githubusercontent.com/AstroMeYT/Gitet/main/install.sh | bash -s -- --system

# Uninstall gitet (apps it installed are kept)
curl -fsSL https://raw.githubusercontent.com/AstroMeYT/Gitet/main/install.sh | bash -s -- --uninstall
```

To also get the graphical app (see [Graphical interface](#graphical-interface)):

```bash
curl -fsSL https://raw.githubusercontent.com/AstroMeYT/Gitet/main/install.sh | bash -s -- --gui
```

Or run `gitet.py` directly (`chmod +x gitet.py && ./gitet.py`). On first run it installs itself.

**Requirements:** Linux and Python 3.8+. No third-party Python packages. `flatpak` is optional (needed only for Flatpak installs).

## How it picks a package

Gitet looks at the files in a repo's latest release and uses the first type that exists for your system:

| Priority | Type | Notes |
|---|---|---|
| 1 | **Flatpak** (`.flatpak`, `.flatpakref`) | Near-universal on Linux. Installed per-user. |
| 2 | **System package** (`.deb`, `.rpm`, `.pkg.tar.zst`) | Only the format your distro uses (detected from `/etc/os-release`). Needs sudo. |
| 3 | **AppImage** | Saved, symlinked into `~/.local/bin`, and given a launcher entry. |
| 4 | **Archive** (`.tar.gz`, `.tar.xz`, `.tar.bz2`, `.tar.zst`, `.zip`, or a bare executable) | For projects that only publish tarballs/binaries. Extracted safely to `~/.local/share/gitet/apps/`, and the programs inside are linked into `~/.local/bin`. A bundled `.desktop` file becomes a menu entry. |

Before installing it also:

- Skips files built for another architecture (x86_64/x64/amd64, aarch64/arm64, armv7/armhf, i686, ...) and other operating systems.
- Verifies the real architecture of the downloaded file and the SHA-256 checksum when GitHub provides one.
- Falls back to the next type if an install fails.

Flatpaks and AppImages are added to your `PATH` (a command named after the repo) and to your app menu.

## Usage

```bash
gitet install owner/repo          # or a GitHub URL, or just a name to search
gitet types owner/repo            # what does this repo offer for my system?
gitet readme owner/repo           # read the repo's README in your terminal
gitet update                      # update all apps (or: gitet update name)
gitet list                        # show installed apps
gitet delete owner/repo           # uninstall
```

### Choosing what gets installed

```bash
gitet install owner/repo --type appimage     # flatpak | deb | rpm | pacman | appimage | archive | package
gitet install owner/repo --asset "*arm64*"   # a specific release file (substring or glob)
gitet install owner/repo --choose            # pick from a numbered list
gitet install owner/repo --tag v1.2.0        # specific release (pins it; updates skip it)
gitet install owner/repo --pre               # allow pre-releases
gitet install owner/repo --dry-run           # show what would happen
```

`--type package` means your distro's own format. `gitet types owner/repo --all` also lists the files that don't fit your system and why.

### All commands

| Command | What it does |
|---|---|
| `install` (`i`) | Install one or more apps |
| `update` (`upgrade`) | Update apps. `--check` only lists updates, `--force` reinstalls |
| `delete` (`remove`, `rm`, `uninstall`) | Remove apps. `-y` skips the prompt, `--purge` deletes Flatpak data, `--force` drops it from the list even if removal fails |
| `list` (`ls`) | Show installed apps, versions and install type |
| `types` (`info`, `formats`) | List the package types a repo's release offers for your system |
| `readme` | Show a repo's README as formatted plain text. `--raw` shows the source, `-w N` sets the wrap width, `--no-pager` prints it all |
| `search` | Search GitHub repositories |
| `auto on\|off\|status` | Toggle automatic background updates |
| `export` / `import` | Save or restore your app list |
| `self-update` | Update gitet itself (`--check`, `--force`) |

Most commands accept `--json` for scripts (see [JSON output](#json-output)).
| `self-install` / `self-uninstall` | Install or remove gitet (`--system`, `--purge`) |

### Reading a README

```bash
gitet readme owner/repo
```

The Markdown is converted to plain text: headings are underlined, lists use `-`, tables are drawn with `+---+` borders (or turned into `Header: value` lists if they're too wide), quotes get a `|` prefix, links become `text (url)`, and badges become `[label]`. Long READMEs open in your pager (`$PAGER`, or `less`).

## JSON output

`install`, `remove`, `update`, `list`, `types`, `search`, `readme`, `auto` and `self-update` accept `--json`. The result goes to stdout as JSON (progress and messages stay on stderr), and errors look like `{"error": "..."}`.

```bash
gitet list --json
gitet update --check --json      # {"available": [{"repo", "installed", "latest", "method"}], "current": [...], ...}
gitet types owner/repo --json    # system info, every compatible file, the auto-pick, hidden files and why
gitet install owner/repo --json  # {"results": [{"repo", "ok", "tag", "method", "error"}]}
```

## Graphical interface

`gitet-gui.py` is a Synaptic-style front-end built with Qt (PyQt6 or PySide6). It is one standalone file: run it once and it installs itself as `gitet-gui` and adds **Gitet** to your app menu.

```bash
python3 gitet-gui.py
```

- Sidebar views: **Installed**, **Updates** and **Search results**. Typing in the search box filters the list you're in; press Enter to search GitHub (or paste `owner/repo` / a GitHub URL).
- Select one or more packages, then **Install**, **Update**, **Remove**, **Launch**, or open them on GitHub. Right-click for more (reinstall, choose package type).
- Tabs for each package: **README** (formatted or plain text), **Packages** (every compatible file for your system with the recommended one starred; install a specific type or file, optionally pinned) and **Activity** (live output and progress).
- **Update all**, background **Auto-updates** toggle, import/export of your app list, and update checks for gitet itself.
- Password prompts for system packages (`sudo`) appear as a dialog.
- **Settings → GitHub token** stores a token in `~/.config/gitet/token` to avoid GitHub's rate limit.

The GUI needs the `gitet` command (version 1.3+). If it's missing or too old, the GUI offers to download it. `gitet-gui --uninstall` removes the GUI launcher.

## Automatic updates

Off by default.

```bash
gitet auto on               # check every 6 hours
gitet auto on --every 12    # custom interval in hours
gitet auto off
```

Gitet never runs a daemon. With auto-updates off, nothing of gitet is running. With them on, a systemd user timer (or a cron entry if systemd isn't available) starts a short, low-priority check and exits.

System packages need root, so background runs skip them and send a desktop notification telling you to run `gitet update`.

## Moving to another machine

```bash
gitet export apps.json          # on the old system
gitet import apps.json          # on the new one
```

Import installs each app using the best type for the new system. Add `--exact` to install the exact exported versions.

## Updating Gitet

```bash
gitet self-update
```

## Where things live

| Path | Contents |
|---|---|
| `~/.local/bin/gitet` | The gitet program |
| `~/.local/share/gitet/state.json` | Installed apps and settings |
| `~/.local/share/gitet/appimages/` | Downloaded AppImages |
| `~/.local/share/gitet/apps/` | Extracted archives |
| `~/.config/gitet/token` | Optional GitHub token (also read from `GITHUB_TOKEN` / `GH_TOKEN`) |
| `~/.local/share/applications/` | Launcher entries created by gitet |
| `~/.cache/gitet/` | API cache and temporary downloads |

## Notes

- Run gitet as your normal user. It calls `sudo` itself only when a step needs it.
- Only public repositories are supported. Set `GITHUB_TOKEN` (or `GH_TOKEN`) to avoid GitHub's API rate limit.
- Gitet installs whatever the repo publishes. Only install software from authors you trust.
- Set `GITET_NO_SELF_INSTALL=1` to run `gitet.py` without it installing itself.
