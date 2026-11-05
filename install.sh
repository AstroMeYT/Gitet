#!/usr/bin/env bash
# Gitet installer - https://github.com/AstroMeYT/Gitet
#
#   curl -fsSL https://raw.githubusercontent.com/AstroMeYT/Gitet/main/install.sh | bash
#
# Options (pass after `bash -s --` when piping):
#   --system      install to /usr/local/bin (uses sudo) instead of ~/.local/bin
#   --uninstall   remove gitet (your installed apps are kept)
#
# Environment:
#   GITET_REF   branch/tag to install from (default: main)
#   GITET_URL   full URL of gitet.py (overrides the above)
set -euo pipefail

REPO="AstroMeYT/Gitet"
REF="${GITET_REF:-main}"
URL="${GITET_URL:-https://raw.githubusercontent.com/${REPO}/${REF}/gitet.py}"

SYSTEM=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --system) SYSTEM=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,12p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [ -t 2 ]; then B=$'\033[1;36m'; G=$'\033[1;32m'; Y=$'\033[1;33m'; R=$'\033[1;31m'; N=$'\033[0m'; else B=; G=; Y=; R=; N=; fi
say()  { printf '%s::%s %s\n' "$B" "$N" "$*" >&2; }
ok()   { printf '%s✓%s %s\n' "$G" "$N" "$*" >&2; }
warn() { printf '%s!!%s %s\n' "$Y" "$N" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

[ "$(uname -s)" = "Linux" ] || die "Gitet only supports Linux"
[ "$(id -u)" -ne 0 ] || die "run this as your normal user (it uses sudo itself with --system)"

if [ "$SYSTEM" -eq 1 ]; then
  DEST_DIR="/usr/local/bin"
  SUDO="sudo"
  command -v sudo >/dev/null 2>&1 || die "sudo is required for --system"
else
  DEST_DIR="${HOME}/.local/bin"
  SUDO=""
fi
DEST="${DEST_DIR}/gitet"

# ---------------------------------------------------------------- uninstall
if [ "$UNINSTALL" -eq 1 ]; then
  for f in "${HOME}/.local/bin/gitet" "/usr/local/bin/gitet"; do
    if [ -e "$f" ]; then
      if [ -w "$f" ]; then rm -f "$f"; else sudo rm -f "$f"; fi
      ok "removed $f"
    fi
  done
  systemctl --user disable --now gitet-update.timer >/dev/null 2>&1 || true
  rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/gitet-update."{service,timer}
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  ok "gitet removed (apps it installed and ~/.local/share/gitet were kept)"
  exit 0
fi

# ---------------------------------------------------------------- checks
command -v python3 >/dev/null 2>&1 || die "python3 is required (3.8 or newer)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || die "python3 3.8 or newer is required (found $(python3 -V 2>&1))"

# ---------------------------------------------------------------- fetch
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
SRC="$TMP/gitet"

# When run from a checkout (./install.sh), prefer the gitet.py next to this script.
HERE=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [ -n "$HERE" ] && [ -f "$HERE/gitet.py" ] && [ -z "${GITET_URL:-}" ]; then
  say "using local $HERE/gitet.py"
  cp "$HERE/gitet.py" "$SRC"
else
  say "downloading $URL"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$SRC" || die "download failed"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$SRC" "$URL" || die "download failed"
  else
    die "curl or wget is required"
  fi
fi

head -n1 "$SRC" | grep -q '^#!.*python' || die "downloaded file doesn't look like gitet (bad URL or branch?)"
python3 -m py_compile "$SRC" 2>/dev/null || die "downloaded gitet.py has a syntax error"

# ---------------------------------------------------------------- install
$SUDO mkdir -p "$DEST_DIR"
$SUDO install -m 755 "$SRC" "$DEST"
ok "installed gitet to $DEST"

# ---------------------------------------------------------------- PATH (user install only)
if [ "$SYSTEM" -eq 0 ]; then
  case ":$PATH:" in
    *":$DEST_DIR:"*) ;;
    *)
      LINE='export PATH="$HOME/.local/bin:$PATH"  # added by gitet'
      for rc in .profile .bashrc .zshrc; do
        f="$HOME/$rc"
        [ "$rc" = ".profile" ] || [ -f "$f" ] || continue
        if ! grep -qs '\.local/bin' "$f"; then printf '\n%s\n' "$LINE" >> "$f"; fi
      done
      FISH="${XDG_CONFIG_HOME:-$HOME/.config}/fish"
      if [ -d "$FISH" ] && [ ! -f "$FISH/conf.d/gitet.fish" ]; then
        mkdir -p "$FISH/conf.d"
        echo 'fish_add_path -g $HOME/.local/bin' > "$FISH/conf.d/gitet.fish"
      fi
      warn "added ~/.local/bin to your PATH - open a new terminal (or run: export PATH=\"\$HOME/.local/bin:\$PATH\")"
      ;;
  esac
fi

echo >&2
say "try it:  gitet install dpaulat/SupercellWX"
