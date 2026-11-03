#!/usr/bin/env python3
"""
gitet - install and update apps straight from GitHub releases.

    gitet install dpaulat/SupercellWX
    gitet update
    gitet auto on

Package hierarchy (first one that exists for your system wins):
    1. Flatpak bundles   (.flatpak / .flatpakref)
    2. System packages   (.deb / .rpm / .pkg.tar.zst, matching your distro)
    3. AppImages         (added to ~/.local/bin and your app launcher)

Only assets matching your CPU architecture are considered.

On first run gitet copies itself to ~/.local/bin/gitet (use
`gitet self-install --system` for /usr/local/bin). It never runs a daemon:
when automatic updates are off nothing of gitet is running. When they are on,
a systemd user timer (or a cron entry) launches a short one-shot check.

Environment: GITHUB_TOKEN / GH_TOKEN raises the GitHub API rate limit.
             GITET_NO_SELF_INSTALL=1 skips the self-install step.
"""
from __future__ import annotations

import argparse
import fcntl
import fnmatch
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "1.0.0"

HOME = Path.home()
XDG_DATA = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share")
XDG_CACHE = Path(os.environ.get("XDG_CACHE_HOME") or HOME / ".cache")
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
DATA_DIR = XDG_DATA / "gitet"
CACHE_DIR = XDG_CACHE / "gitet"
STATE_FILE = DATA_DIR / "state.json"
LOCK_FILE = DATA_DIR / ".lock"
CRON_LOG = DATA_DIR / "auto-update.log"
API_CACHE = CACHE_DIR / "api.json"
BIN_DIR = HOME / ".local/bin"
APPS_DIR = XDG_DATA / "applications"
UNIT_DIR = XDG_CONFIG / "systemd/user"
USER_BIN = BIN_DIR / "gitet"
SYSTEM_BIN = Path("/usr/local/bin/gitet")
CRON_TAG = "# gitet-auto"

NONINTERACTIVE = False  # True for background runs: never prompt for a password

LABEL = {"flatpak": "Flatpak", "deb": "DEB", "rpm": "RPM",
         "pacman": "pacman pkg", "appimage": "AppImage"}


# --------------------------------------------------------------------------
# output helpers / errors
# --------------------------------------------------------------------------
def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if sys.stderr.isatty() else s


def say(msg: str) -> None:
    print(f"{_c('1;36', '::')} {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"{_c('1;33', '!!')} {msg}", file=sys.stderr)


def ok(msg: str) -> None:
    print(f"{_c('1;32', '✓')} {msg}", file=sys.stderr)


def err(msg: str) -> None:
    print(f"{_c('1;31', 'error:')} {msg}", file=sys.stderr)


class GitetError(Exception):
    pass


class InstallError(GitetError):
    pass


class NeedRoot(InstallError):
    pass


class ApiError(GitetError):
    def __init__(self, msg: str, code: int = 0):
        super().__init__(msg)
        self.code = code


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", s.lower()).strip("-") or "app"


# --------------------------------------------------------------------------
# process helpers
# --------------------------------------------------------------------------
def run(cmd: list, **kw) -> int:
    try:
        return subprocess.run(cmd, **kw).returncode
    except FileNotFoundError:
        return 127


def out(cmd: list) -> str:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        return p.stdout if p.returncode == 0 else ""
    except FileNotFoundError:
        return ""


def sudo_cmd(cmd: list) -> list:
    if os.geteuid() == 0:
        return cmd
    if not shutil.which("sudo"):
        raise InstallError("root is required but sudo is not installed")
    if NONINTERACTIVE:
        if run(["sudo", "-n", "true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
            raise NeedRoot("needs root (run `gitet update` manually)")
        return ["sudo", "-n"] + cmd
    return ["sudo"] + cmd


# --------------------------------------------------------------------------
# state + locking
# --------------------------------------------------------------------------
class Lock:
    def __init__(self, wait: bool = True):
        self.wait = wait
        self.f = None

    def __enter__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.f = open(LOCK_FILE, "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | (0 if self.wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise GitetError("another gitet is running")
        return self

    def __exit__(self, *a):
        if self.f:
            self.f.close()


def load_state() -> dict:
    try:
        s = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        s = {}
    cfg = s.setdefault("config", {})
    cfg.setdefault("auto", False)
    cfg.setdefault("every_hours", 6)
    s.setdefault("apps", {})
    return s


def save_state(s: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2, sort_keys=True))
    os.replace(tmp, STATE_FILE)


def find_app(state: dict, name: str) -> str:
    parsed = parse_repo(name)
    n = (f"{parsed[0]}/{parsed[1]}" if parsed else name).lower().strip()
    apps = state["apps"]
    exact = [k for k in apps if k.lower() == n]
    if exact:
        return exact[0]
    short = [k for k in apps if k.split("/")[1].lower() == n]
    if len(short) == 1:
        return short[0]
    if len(short) > 1:
        raise GitetError(f"'{name}' is ambiguous: {', '.join(short)}")
    raise GitetError(f"'{name}' is not installed (see: gitet list)")


# --------------------------------------------------------------------------
# GitHub API
# --------------------------------------------------------------------------
_cache = None


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(API_CACHE.read_text())
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save_cache() -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        while len(_cache) > 150:
            _cache.pop(next(iter(_cache)))
        API_CACHE.write_text(json.dumps(_cache))
    except OSError:
        pass


def api(path: str, params: dict | None = None, cache: bool = True):
    url = "https://api.github.com" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": f"gitet/{VERSION}",
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    ent = _load_cache().get(url) if cache else None
    if ent:
        headers["If-None-Match"] = ent["etag"]  # 304s don't count against the rate limit
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            data = json.load(r)
            etag = r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304 and ent:
            return ent["data"]
        if e.code == 404:
            raise ApiError(f"not found on GitHub ({path}); private repos need GITHUB_TOKEN", 404)
        if e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0":
            raise ApiError("GitHub API rate limit reached - set GITHUB_TOKEN or wait a while", 403)
        raise ApiError(f"GitHub API error {e.code}", e.code)
    except urllib.error.URLError as e:
        raise ApiError(f"network error: {e.reason}")
    if cache and etag:
        _load_cache()[url] = {"etag": etag, "data": data}
        _save_cache()
    return data


def parse_repo(s: str):
    s = s.strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)", s)
    if not m:
        m = re.fullmatch(r"([\w.-]+)/([\w.-]+)", s)
    if not m:
        return None
    owner, name = m.groups()
    if name.endswith(".git"):
        name = name[:-4]
    return owner, name


def resolve_repo(arg: str, yes: bool = False) -> dict:
    parsed = parse_repo(arg)
    if not parsed:  # bare word -> search GitHub
        items = api("/search/repositories", {"q": arg, "per_page": 5}, cache=False).get("items", [])
        if not items:
            raise GitetError(f"no GitHub repository found for '{arg}'")
        if len(items) > 1 and not yes:
            if not sys.stdin.isatty():
                raise GitetError(f"'{arg}' is ambiguous; use owner/repo (try: gitet search {arg})")
            for i, it in enumerate(items, 1):
                print(f"  {i}. {it['full_name']}  ★{it['stargazers_count']}  {(it.get('description') or '')[:60]}",
                      file=sys.stderr)
            pick = input("Pick [1-%d, Enter=1, q=quit]: " % len(items)).strip().lower()
            if pick == "q":
                raise GitetError("cancelled")
            items = [items[int(pick) - 1]] if pick.isdigit() and 1 <= int(pick) <= len(items) else items[:1]
        parsed = tuple(items[0]["full_name"].split("/", 1))
    return api(f"/repos/{parsed[0]}/{parsed[1]}")


def get_release(full: str, tag: str | None = None, pre: bool = False) -> dict:
    if tag:
        return api(f"/repos/{full}/releases/tags/{urllib.parse.quote(tag)}")
    if not pre:
        try:
            return api(f"/repos/{full}/releases/latest")
        except ApiError as e:
            if e.code != 404:
                raise
    for r in api(f"/repos/{full}/releases", {"per_page": 15}):
        if not r.get("draft"):
            return r
    raise GitetError(f"{full} has no releases")


# --------------------------------------------------------------------------
# host detection (arch / distro)
# --------------------------------------------------------------------------
ARCH_PATTERNS = {
    "x86_64": r"x86[_-]?64|amd64|x64|linux[_-]?64|64[_-]?bit",
    "aarch64": r"aarch64|arm64|armv8[a-z]*",
    "armv7": r"armv7[a-z]*|armhf|arm32",
    "armv6": r"armv6[a-z]*|armel",
    "i686": r"i[3-6]86|ia32|x86|linux[_-]?32|32[_-]?bit",
}
ARCH_RE = {a: re.compile(rf"(?<![a-z0-9])(?:{p})(?![a-z0-9])") for a, p in ARCH_PATTERNS.items()}

EXCLUDE = re.compile(
    r"(?<![a-z0-9])(?:win(?:dows|32|64)?|mac(?:os)?|darwin|osx|android|freebsd|openbsd|netbsd|ios)(?![a-z0-9])"
    r"|\.(?:exe|msi|dmg|apk|ipa|sig|asc|sha256|sha256sum|zsync|txt|json|ya?ml)$"
    r"|debuginfo|debugsource|dbgsym|-debug[._-]|\.src\.rpm$|-devel[._-]|-dev_")

DISTRO_WORDS = ("ubuntu", "debian", "fedora", "opensuse", "suse", "centos", "rhel",
                "rocky", "alma", "manjaro", "el7", "el8", "el9")

DEB_ARCH = {"amd64": "x86_64", "arm64": "aarch64", "armhf": "armv7", "armel": "armv6", "i386": "i686"}
RPM_ARCH = {"x86_64": "x86_64", "aarch64": "aarch64", "armv7hl": "armv7", "armv7l": "armv7",
            "i686": "i686", "i586": "i686", "i386": "i686"}
ELF_MACHINES = {0x3E: "x86_64", 0xB7: "aarch64", 0x28: "armv7", 0x03: "i686"}


def host_arch() -> str:
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("aarch64", "arm64", "armv8l", "armv8b"):
        return "aarch64"
    if m.startswith("armv7"):
        return "armv7"
    if m.startswith("armv6"):
        return "armv6"
    if re.fullmatch(r"i[3-6]86", m):
        return "i686"
    return m


def arch_compat(found: str, host: str) -> bool:
    return found == host or (found == "armv7" and host.startswith("armv"))


def arch_tokens(name: str) -> set:
    n = name.lower()
    found = {a for a, rx in ARCH_RE.items() if rx.search(n)}
    if "x86_64" in found:
        found.discard("i686")  # "x86" is a prefix of "x86_64"
    return found


def arch_score(name: str, host: str):
    """3 = explicit arch match, 2 = arch-independent, 1 = unspecified, None = wrong arch."""
    found = arch_tokens(name)
    if found:
        return 3 if any(arch_compat(f, host) for f in found) else None
    if re.search(r"noarch|[_.-]all\.deb$", name.lower()):
        return 2
    return 1


def read_os_release() -> dict:
    d = {}
    for p in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            for line in open(p):
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    d[k] = v.strip("\"'")
            break
        except OSError:
            continue
    return d


def pkg_family(osr: dict):
    ids = {osr.get("ID", "").lower(), *osr.get("ID_LIKE", "").lower().split()}
    if ids & {"debian", "ubuntu"}:
        return "deb"
    if ids & {"fedora", "rhel", "centos", "suse", "opensuse", "opensuse-leap", "opensuse-tumbleweed",
              "sles", "mageia", "rocky", "almalinux", "amzn", "ol"}:
        return "rpm"
    if ids & {"arch", "manjaro", "endeavouros", "artix"}:
        return "pacman"
    for tool, fam in (("apt-get", "deb"), ("dpkg", "deb"), ("dnf", "rpm"),
                      ("zypper", "rpm"), ("yum", "rpm"), ("pacman", "pacman")):
        if shutil.which(tool):
            return fam
    return None


def classify(name: str):
    n = name.lower()
    if EXCLUDE.search(n):
        return None
    if n.endswith((".flatpak", ".flatpakref")):
        return "flatpak"
    if n.endswith(".appimage"):
        return "appimage"
    if n.endswith(".deb"):
        return "deb"
    if n.endswith(".rpm"):
        return "rpm"
    if n.endswith((".pkg.tar.zst", ".pkg.tar.xz")):
        return "pacman"
    return None


def distro_bonus(name: str, osr: dict) -> int:
    n = name.lower()
    did = osr.get("ID", "").lower()
    vid = osr.get("VERSION_ID", "")
    mine = {w for w in {did, *osr.get("ID_LIKE", "").lower().split()} if w}
    bonus = 0
    if did and did in n:
        bonus += 20
    if vid and vid in n:
        bonus += 10
    if any(w in n for w in DISTRO_WORDS) and not any(w in n for w in mine):
        bonus -= 15
    return bonus


KIND_ORDER = ("flatpak", "deb", "rpm", "pacman", "appimage")
FLATPAK_MARK = "# gitet-managed"


def kind_support(kind: str, fam):
    if kind == "flatpak":
        return (True, "") if shutil.which("flatpak") else (False, "flatpak isn't installed")
    if kind == "appimage" or kind == fam:
        return True, ""
    return False, f"this system uses {fam or 'no known package format'}, not {kind}"


def hidden_reason(name: str, host: str) -> str:
    n = name.lower()
    if EXCLUDE.search(n):
        return "not an installable app file (other OS, debug or source package)"
    if not classify(n):
        return "not a Flatpak, system package or AppImage"
    return f"built for another architecture (this system: {host})"


def analyze(assets: list, pattern: str | None = None):
    """Split release assets into usable ({kind: [best first]}) and hidden ([(name, reason)])."""
    host = host_arch()
    osr = read_os_release()
    pat = None
    if pattern:
        pat = pattern.lower()
        if not any(ch in pat for ch in "*?["):
            pat = f"*{pat}*"
    scored, hidden = {}, []
    for a in assets:
        name = a["name"]
        if pat and not fnmatch.fnmatch(name.lower(), pat):
            hidden.append((name, "doesn't match --asset"))
            continue
        kind = classify(name)
        sc = arch_score(name, host) if kind else None
        if not kind or sc is None:
            hidden.append((name, hidden_reason(name, host)))
            continue
        bonus = distro_bonus(name, osr) if kind in ("deb", "rpm", "pacman") else 0
        scored.setdefault(kind, []).append((sc * 100 + bonus, a))
    usable = {k: [a for _, a in sorted(v, key=lambda t: -t[0])] for k, v in scored.items()}
    return usable, hidden


def build_plan(assets: list, only: set | None = None, pattern: str | None = None):
    """Return ([(kind, [assets best-first])...] in hierarchy order, notes)."""
    fam = pkg_family(read_os_release())
    usable, _ = analyze(assets, pattern)
    plan, notes = [], []
    for kind in KIND_ORDER:
        lst = usable.get(kind)
        if not lst:
            continue
        if only is None and kind not in ("flatpak", fam, "appimage"):
            continue
        if only is not None and kind not in only:
            continue
        good, why = kind_support(kind, fam)
        if not good:
            notes.append(f"a {LABEL[kind]} is available but {why} - skipping it")
            continue
        plan.append((kind, lst))
    return plan, notes


# --------------------------------------------------------------------------
# download + verification
# --------------------------------------------------------------------------
def download(asset: dict, dest: Path) -> None:
    req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": f"gitet/{VERSION}"})
    sha = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or asset.get("size") or 0)
            done, last = 0, 0.0
            while True:
                chunk = r.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                if sys.stderr.isatty() and time.time() - last > 0.1:
                    last = time.time()
                    pct = f"{done * 100 // total:3d}%" if total else "   "
                    print(f"\r   {pct}  {human(done)} / {human(total)}   ", end="", file=sys.stderr)
            if sys.stderr.isatty():
                print("\r" + " " * 50 + "\r", end="", file=sys.stderr)
    except (urllib.error.URLError, OSError) as e:
        raise InstallError(f"download failed: {getattr(e, 'reason', e)}")
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:") and digest[7:] != sha.hexdigest():
        raise InstallError("checksum mismatch - download corrupted, aborting")


def elf_arch(path: Path):
    try:
        with open(path, "rb") as f:
            h = f.read(20)
    except OSError:
        return None
    if len(h) < 20 or h[:4] != b"\x7fELF":
        return None
    machine = struct.unpack("<H" if h[5] == 1 else ">H", h[18:20])[0]
    return ELF_MACHINES.get(machine)


def verify_arch(kind: str, path: Path) -> None:
    """Double-check the real architecture of the downloaded file."""
    host, got = host_arch(), None
    if kind == "appimage":
        got = elf_arch(path)
    elif kind == "deb" and shutil.which("dpkg-deb"):
        v = out(["dpkg-deb", "-f", str(path), "Architecture"]).strip()
        got = None if v == "all" else DEB_ARCH.get(v)
    elif kind == "rpm" and shutil.which("rpm"):
        v = out(["rpm", "-qp", "--qf", "%{ARCH}", str(path)]).strip()
        got = None if v == "noarch" else RPM_ARCH.get(v)
    if got and not arch_compat(got, host):
        raise InstallError(f"architecture mismatch: package is {got}, system is {host}")


# --------------------------------------------------------------------------
# installers
# --------------------------------------------------------------------------
def flatpak_apps() -> set:
    return set(out(["flatpak", "list", "--user", "--app", "--columns=application"]).split())


def run_tee(cmd: list):
    """Run a command, echo its output live and also return it."""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except FileNotFoundError:
        return 127, ""
    buf = []
    for line in p.stdout:
        sys.stderr.write(line)
        buf.append(line)
    p.wait()
    return p.returncode, "".join(buf)


def last_line(log: str) -> str:
    lines = [l.strip() for l in log.splitlines() if l.strip()]
    return lines[-1] if lines else "unknown error"


def flatpak_exports() -> Path:
    return Path(os.environ.get("FLATPAK_USER_DIR") or XDG_DATA / "flatpak") / "exports"


def best_flatpak_icon(app_id: str):
    best, best_size = None, -1
    for p in (flatpak_exports() / "share/icons/hicolor").glob(f"*/apps/{app_id}.*"):
        if p.suffix not in (".png", ".svg"):
            continue
        d = p.parent.parent.name
        size = 10 ** 6 if d == "scalable" else int(d.split("x")[0]) if d.split("x")[0].isdigit() else 0
        if size > best_size:
            best, best_size = p, size
    return best


def flatpak_integrate(app_id: str, slug: str, full: str) -> dict:
    """Put the app on PATH and in the start menu right away (no re-login needed)."""
    ensure_path()
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    icon = best_flatpak_icon(app_id)
    exported = flatpak_exports() / "share/applications" / f"{app_id}.desktop"
    desktop = APPS_DIR / f"{app_id}.desktop"
    if exported.exists():
        lines = []
        for line in exported.read_text(errors="ignore").splitlines():
            if icon and line.startswith("Icon=") and line[5:].strip().startswith(app_id):
                line = f"Icon={icon}"
            lines.append(line)
        desktop.write_text("\n".join(lines) + "\n")
    else:
        desktop.write_text("[Desktop Entry]\nType=Application\n"
                           f"Name={full.split('/')[1]}\nExec=flatpak run {shlex.quote(app_id)}\n"
                           + (f"Icon={icon}\n" if icon else "") + "Terminal=false\nCategories=Utility;\n")
    link = BIN_DIR / slug
    if (link.exists() or link.is_symlink()) and not _ours(link):
        link = BIN_DIR / f"{slug}-gitet"
    if link.exists() and not _ours(link):
        warn(f"{link} exists and isn't ours; not creating a command for this Flatpak")
        link = None
    else:
        if link.is_symlink():
            link.unlink()
        link.write_text(f'#!/bin/sh\n{FLATPAK_MARK}\nexec flatpak run {shlex.quote(app_id)} "$@"\n')
        os.chmod(link, 0o755)
    refresh_desktop_db()
    return {"flatpak_id": app_id, "link": str(link) if link else "", "desktop": str(desktop)}


def install_flatpak(path: Path, full: str, slug: str, prev) -> dict:
    if not out(["flatpak", "remotes", "--user"]).strip():  # bundles pull runtimes from flathub
        run(["flatpak", "remote-add", "--user", "--if-not-exists", "flathub",
             "https://dl.flathub.org/repo/flathub.flatpakrepo"])
    base = ["flatpak", "install", "--user", "-y"]
    base += ["--from"] if path.suffix == ".flatpakref" else ["--bundle"]
    before = flatpak_apps()
    rc, log = run_tee(base + [str(path)])
    new = flatpak_apps() - before
    if rc != 0 and not new and not prev:
        warn("flatpak reported an error - retrying without the runtime dependency check")
        rc, log2 = run_tee(base + ["--no-deps", str(path)])
        log += log2
        new = flatpak_apps() - before
    if rc != 0 and not new and not prev:
        raise InstallError(f"flatpak install failed: {last_line(log)}")
    if rc != 0:
        warn(f"flatpak said: {last_line(log)}")
        warn("the app is installed, but if it won't start it may need a runtime that isn't on Flathub - "
             "try again with --type appimage (or --type deb/rpm)")
    app_id = new.pop() if len(new) == 1 else (prev or {}).get("flatpak_id")
    if not app_id:
        guess = [a for a in flatpak_apps() if slug.replace("-", "") in a.lower().replace("-", "")]
        app_id = guess[0] if len(guess) == 1 else None
    if not app_id:
        raise InstallError("installed, but couldn't determine the Flatpak app id")
    return flatpak_integrate(app_id, slug, full)


def install_deb(path: Path, full: str, slug: str, prev) -> dict:
    name = out(["dpkg-deb", "-f", str(path), "Package"]).strip() or slug
    if shutil.which("apt-get"):
        cmd = ["apt-get", "install", "-y", str(path)]  # resolves dependencies
    elif shutil.which("dpkg"):
        cmd = ["dpkg", "-i", str(path)]
    else:
        raise InstallError("neither apt-get nor dpkg found")
    if run(sudo_cmd(cmd)) != 0:
        raise InstallError("package installation failed")
    return {"pkg": name}


def install_rpm(path: Path, full: str, slug: str, prev) -> dict:
    name = out(["rpm", "-qp", "--qf", "%{NAME}", str(path)]).strip() or slug
    if shutil.which("dnf"):
        cmd = ["dnf", "install", "-y", str(path)]
    elif shutil.which("zypper"):
        cmd = ["zypper", "--non-interactive", "install", "--allow-unsigned-rpm", str(path)]
    elif shutil.which("yum"):
        cmd = ["yum", "install", "-y", str(path)]
    elif shutil.which("rpm"):
        cmd = ["rpm", "-Uvh", str(path)]
    else:
        raise InstallError("no rpm-capable package manager found")
    if run(sudo_cmd(cmd)) != 0:
        raise InstallError("package installation failed")
    return {"pkg": name}


def install_pacman(path: Path, full: str, slug: str, prev) -> dict:
    parts = out(["pacman", "-Qp", str(path)]).split()
    name = parts[0] if parts else slug
    if run(sudo_cmd(["pacman", "-U", "--noconfirm", str(path)])) != 0:
        raise InstallError("package installation failed")
    return {"pkg": name}


def parse_desktop(p: Path) -> dict:
    d, inside = {}, False
    for line in p.read_text(errors="ignore").splitlines():
        s = line.strip()
        if s.startswith("["):
            inside = s == "[Desktop Entry]"
        elif inside and "=" in s and not s.startswith("#"):
            k, v = s.split("=", 1)
            if "[" not in k:  # skip localized keys
                d[k.strip()] = v.strip()
    return d


def sniff_icon_ext(p: Path) -> str:
    head = p.read_bytes()[:64]
    if head.startswith(b"\x89PNG"):
        return ".png"
    if b"<svg" in head or head.lstrip().startswith(b"<?xml"):
        return ".svg"
    return p.suffix if p.suffix in (".png", ".svg", ".xpm") else ".png"


def extract_meta(appimage: Path, slug: str) -> dict:
    """Best-effort: read the AppImage's own .desktop entry and icon."""
    meta = {"Name": slug, "icon": ""}
    try:
        with open(appimage, "rb") as f:
            if f.read(11)[8:11] != b"AI\x02":  # only type-2 supports safe extraction
                return meta
    except OSError:
        return meta
    tmp = Path(tempfile.mkdtemp(prefix="gitet-x-"))

    def extract(pattern: str) -> None:
        try:
            subprocess.run([str(appimage), "--appimage-extract", pattern], cwd=tmp, timeout=180,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            pass

    try:
        extract("*.desktop")
        root = tmp / "squashfs-root"
        desktops = sorted(root.glob("*.desktop"))
        icon_name = ""
        if desktops:
            d = parse_desktop(desktops[0])
            icon_name = d.get("Icon", "")
            for k in ("Name", "Comment", "Categories", "MimeType", "StartupWMClass", "Terminal"):
                if d.get(k):
                    meta[k] = d[k]
            m = re.search(r"%[fFuU]", d.get("Exec", ""))
            meta["code"] = m.group(0) if m else ""
        names = [icon_name] if icon_name and "/" not in icon_name else []
        for n in names:
            extract(n + ".png")
            extract(n + ".svg")
        extract(".DirIcon")
        for cand in [root / f"{n}{e}" for n in names for e in (".png", ".svg")] + [root / ".DirIcon"]:
            try:
                real = cand.resolve(strict=True)
            except OSError:
                continue
            if not str(real).startswith(str(root.resolve())) or not real.is_file():
                continue  # ignore symlinks escaping the extracted tree
            dest_dir = DATA_DIR / "icons"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / (slug + sniff_icon_ext(real))
            shutil.copyfile(real, dest)
            meta["icon"] = str(dest)
            break
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return meta


def write_desktop(slug: str, exe: Path, meta: dict, repo: str) -> Path:
    def clean(v: str) -> str:
        return v.replace("\n", " ").strip()

    exec_line = f'Exec="{exe}"' + (f" {meta['code']}" if meta.get("code") else "")
    lines = ["[Desktop Entry]", "Type=Application", f"Name={clean(meta['Name'])}"]
    if meta.get("Comment"):
        lines.append(f"Comment={clean(meta['Comment'])}")
    lines.append(exec_line)
    if meta.get("icon"):
        lines.append(f"Icon={meta['icon']}")
    lines.append(f"Terminal={meta.get('Terminal', 'false')}")
    lines.append(f"Categories={meta.get('Categories') or 'Utility;'}")
    for k in ("MimeType", "StartupWMClass"):
        if meta.get(k):
            lines.append(f"{k}={clean(meta[k])}")
    lines.append(f"X-Gitet-Repo={repo}")
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    p = APPS_DIR / f"gitet-{slug}.desktop"
    p.write_text("\n".join(lines) + "\n")
    return p


def refresh_desktop_db() -> None:
    if shutil.which("update-desktop-database"):
        run(["update-desktop-database", str(APPS_DIR)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ensure_path() -> bool:
    """Make sure ~/.local/bin is on PATH for future shells. True if we changed something."""
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    if str(BIN_DIR) in os.environ.get("PATH", "").split(":"):
        return False
    changed = False
    line = '\nexport PATH="$HOME/.local/bin:$PATH"  # added by gitet\n'
    for rc in (".profile", ".bashrc", ".zshrc"):
        p = HOME / rc
        if rc != ".profile" and not p.exists():
            continue
        text = p.read_text(errors="ignore") if p.exists() else ""
        if ".local/bin" in text:
            continue
        with open(p, "a") as f:
            f.write(line)
        changed = True
    fish = XDG_CONFIG / "fish"
    if fish.is_dir() and not (fish / "conf.d/gitet.fish").exists():
        (fish / "conf.d").mkdir(exist_ok=True)
        (fish / "conf.d/gitet.fish").write_text("fish_add_path -g $HOME/.local/bin\n")
        changed = True
    return changed


def install_appimage(path: Path, full: str, slug: str, prev) -> dict:
    ensure_path()
    app_dir = DATA_DIR / "appimages" / slug
    app_dir.mkdir(parents=True, exist_ok=True)
    target = app_dir / f"{slug}.AppImage"
    os.chmod(path, 0o755)
    meta = extract_meta(path, slug)
    if meta["Name"] == slug:
        meta["Name"] = full.split("/")[1]
    shutil.move(str(path), str(target) + ".new")
    os.replace(str(target) + ".new", target)

    link = BIN_DIR / slug
    if link.is_symlink() or link.exists():
        if not (link.is_symlink() and Path(os.readlink(link)) == target):
            warn(f"{link} already exists and isn't ours; using {slug}-gitet instead")
            link = BIN_DIR / f"{slug}-gitet"
            if link.is_symlink() or link.exists():
                link.unlink()
    link.symlink_to(target)

    desktop = write_desktop(slug, target, meta, full)
    refresh_desktop_db()
    return {"path": str(target), "link": str(link), "desktop": str(desktop), "icon": meta.get("icon", "")}


INSTALLERS = {"flatpak": install_flatpak, "deb": install_deb, "rpm": install_rpm,
              "pacman": install_pacman, "appimage": install_appimage}


def _ours(p: Path) -> bool:
    try:
        return p.is_file() and not p.is_symlink() and FLATPAK_MARK in p.read_text(errors="ignore")[:300]
    except OSError:
        return False


def remove_record(rec: dict, purge: bool = False) -> None:
    kind = rec["method"]
    if kind == "flatpak":
        app_id = rec.get("flatpak_id")
        if not app_id:
            raise GitetError("unknown Flatpak id; remove it manually with `flatpak uninstall`")
        if app_id in flatpak_apps():
            cmd = ["flatpak", "uninstall", "--user", "-y"] + (["--delete-data"] if purge else []) + [app_id]
            if run(cmd) != 0:
                raise GitetError("flatpak uninstall failed")
        else:
            warn(f"{app_id} is already gone from Flatpak")
    elif kind in ("deb", "rpm", "pacman"):
        pkg = rec.get("pkg")
        if kind == "deb":
            cmd = ["apt-get", "remove", "-y", pkg] if shutil.which("apt-get") else ["dpkg", "-r", pkg]
        elif kind == "rpm":
            if shutil.which("dnf"):
                cmd = ["dnf", "remove", "-y", pkg]
            elif shutil.which("zypper"):
                cmd = ["zypper", "--non-interactive", "remove", pkg]
            elif shutil.which("yum"):
                cmd = ["yum", "remove", "-y", pkg]
            else:
                cmd = ["rpm", "-e", pkg]
        else:
            cmd = ["pacman", "-R", "--noconfirm", pkg]
        if run(sudo_cmd(cmd)) != 0:
            raise GitetError("package removal failed")
    elif kind == "appimage" and rec.get("path"):
        shutil.rmtree(Path(rec["path"]).parent, ignore_errors=True)
    # launcher entries / PATH commands that gitet created
    if rec.get("link"):
        link = Path(rec["link"])
        if link.is_symlink() or _ours(link):
            link.unlink()
    for k in ("desktop", "icon"):
        if rec.get(k):
            Path(rec[k]).unlink(missing_ok=True)
    refresh_desktop_db()


# --------------------------------------------------------------------------
# install / update logic
# --------------------------------------------------------------------------
def workdir(kind: str) -> Path:
    if kind in ("flatpak", "appimage"):
        base = CACHE_DIR / "dl"
        base.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(dir=base))
    d = Path(tempfile.mkdtemp(prefix="gitet-"))
    os.chmod(d, 0o755)  # let apt's sandbox user read the .deb
    return d


def do_install(full: str, rel: dict, only: set | None, dry: bool, prev, pattern=None, choose=False):
    tag = rel["tag_name"]
    assets = rel.get("assets") or []
    osr = read_os_release()
    desc = f"{host_arch()}, {osr.get('PRETTY_NAME') or osr.get('NAME') or 'Linux'}"
    plan, notes = build_plan(assets, only, pattern)
    say(f"system: {desc}")
    for n in notes:
        warn(n)
    if not plan:
        names = ", ".join(a["name"] for a in assets[:12]) or "(none)"
        what = (f" of type {'/'.join(sorted(only))}" if only else "") + (f" matching '{pattern}'" if pattern else "")
        raise GitetError(f"no compatible file{what} for {desc} in {tag}\n"
                         f"   release assets: {names}\n   (see what fits: gitet types {full})")
    if choose:
        if not sys.stdin.isatty():
            raise GitetError("--choose needs a terminal")
        flat = [(k, a) for k, lst in plan for a in lst]
        for i, (k, a) in enumerate(flat, 1):
            print(f"  {i}. {LABEL[k]:<10} {a['name']}  ({human(a.get('size', 0))})", file=sys.stderr)
        pick = input(f"Pick [1-{len(flat)}, Enter=1, q=quit]: ").strip().lower()
        if pick == "q":
            raise GitetError("cancelled")
        k, a = flat[int(pick) - 1 if pick.isdigit() and 1 <= int(pick) <= len(flat) else 0]
        plan = [(k, [a])]
    if dry:
        for i, (kind, lst) in enumerate(plan, 1):
            print(f"  {i}. {LABEL[kind]:<10} {lst[0]['name']}  ({human(lst[0].get('size', 0))})"
                  + ("   <- would install" if i == 1 else ""))
        return None
    problems = []
    for kind, lst in plan:
        for asset in lst[:2]:
            say(f"{LABEL[kind]}: {asset['name']} ({human(asset.get('size', 0))})")
            wd = workdir(kind)
            try:
                dest = wd / re.sub(r"[^\w.+-]", "_", asset["name"])
                download(asset, dest)
                verify_arch(kind, dest)
                extra = INSTALLERS[kind](dest, full, slugify(full.split("/")[1]), prev)
            except NeedRoot:
                raise
            except InstallError as e:
                warn(f"{asset['name']}: {e}")
                problems.append(f"{asset['name']}: {e}")
                continue
            finally:
                shutil.rmtree(wd, ignore_errors=True)
            rec = {"repo": full, "tag": tag, "method": kind, "asset": asset["name"],
                   "installed_at": int(time.time())}
            rec.update(extra)
            return rec
        if kind != plan[-1][0]:
            warn(f"{LABEL[kind]} failed, trying next option...")
    raise GitetError("every install method failed:\n   " + "\n   ".join(problems))


def install_one(state: dict, arg: str, args) -> None:
    meta = resolve_repo(arg, yes=args.yes)
    full = meta["full_name"]
    key = next((k for k in state["apps"] if k.lower() == full.lower()), full)
    prev = state["apps"].get(key)
    say(full + (f" - {meta['description']}" if meta.get("description") else ""))
    rel = get_release(full, tag=args.tag, pre=args.pre)
    tag = rel["tag_name"]
    if prev and prev["tag"] == tag and not args.force and not args.dry_run:
        ok(f"{full} {tag} is already installed (use --force to reinstall)")
        return
    say(f"release {tag}" + (" (pre-release)" if rel.get("prerelease") else "")
        + (f", {rel['published_at'][:10]}" if rel.get("published_at") else ""))
    rec = do_install(full, rel, args._only, args.dry_run, prev,
                     getattr(args, "asset", None), getattr(args, "choose", False))
    if args.dry_run:
        return
    rec["pinned"] = bool(args.tag)
    rec["pre"] = bool(args.pre or rel.get("prerelease"))
    if prev and prev["method"] != rec["method"]:
        try:
            remove_record(prev)
        except GitetError as e:
            warn(f"couldn't remove previous {prev['method']} install: {e}")
    state["apps"][key] = rec
    save_state(state)
    ok(f"installed {full} {tag} via {LABEL[rec['method']]}")
    if rec.get("link"):
        say(f"run it with `{Path(rec['link']).name}` or from your app launcher")


def update_apps(state: dict, keys: list, check: bool = False, force: bool = False, auto: bool = False):
    updated, pending, failed, current = [], [], [], []
    for key in keys:
        rec = state["apps"][key]
        try:
            if rec.get("pinned") and not force:
                say(f"{key}: pinned to {rec['tag']}, skipping")
                continue
            rel = get_release(key, pre=rec.get("pre", False))
            tag = rel["tag_name"]
            if tag == rec["tag"] and not force:
                current.append(key)
                continue
            if check:
                print(f"{key}  {rec['tag']} -> {tag}")
                updated.append(key)
                continue
            say(f"{key}: {rec['tag']} -> {tag}")
            new = do_install(key, rel, {rec["method"]}, False, rec)
            new["pinned"] = rec.get("pinned", False)
            new["pre"] = rec.get("pre", False)
            state["apps"][key] = new
            save_state(state)
            updated.append(f"{key} {tag}")
            ok(f"updated {key} to {tag}")
        except NeedRoot:
            pending.append(key)
            warn(f"{key}: update needs root, skipped")
        except GitetError as e:
            failed.append((key, str(e).splitlines()[0]))
            warn(f"{key}: {e}")
    return updated, pending, failed, current


# --------------------------------------------------------------------------
# automatic updates (systemd user timer, cron fallback) - no daemon
# --------------------------------------------------------------------------
def installed_path() -> Path:
    for p in (USER_BIN, SYSTEM_BIN):
        if p.exists():
            return p
    return Path(os.path.realpath(__file__))


def systemd_ok() -> bool:
    return bool(shutil.which("systemctl")) and run(
        ["systemctl", "--user", "show-environment"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def cron_lines() -> list:
    p = subprocess.run(["crontab", "-l"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return p.stdout.splitlines() if p.returncode == 0 else []


def cron_write(lines: list) -> None:
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True)


def auto_enable(hours: int) -> str:
    exe = installed_path()
    if systemd_ok():
        UNIT_DIR.mkdir(parents=True, exist_ok=True)
        (UNIT_DIR / "gitet-update.service").write_text(
            "[Unit]\nDescription=Gitet automatic app updates\n\n[Service]\nType=oneshot\n"
            f"ExecStart={exe} _auto-update\nNice=19\nIOSchedulingClass=idle\n"
            f"Environment=PATH={os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}\n")
        (UNIT_DIR / "gitet-update.timer").write_text(
            "[Unit]\nDescription=Check for app updates via gitet\n\n[Timer]\nOnBootSec=10min\n"
            f"OnUnitActiveSec={hours}h\nRandomizedDelaySec=15min\n\n[Install]\nWantedBy=timers.target\n")
        run(["systemctl", "--user", "daemon-reload"])
        if run(["systemctl", "--user", "enable", "--now", "gitet-update.timer"]) != 0:
            raise GitetError("couldn't enable the systemd timer")
        return "systemd user timer"
    if shutil.which("crontab"):
        sched = f"17 */{hours} * * *" if hours < 24 else "17 3 * * *"
        env = 'PATH="$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"'
        lines = [l for l in cron_lines() if CRON_TAG not in l]
        lines.append(f"{sched} env {env} {exe} _auto-update >> {CRON_LOG} 2>&1 {CRON_TAG}")
        cron_write(lines)
        return "cron job"
    raise GitetError("neither systemd (user) nor cron is available for scheduling")


def auto_disable() -> None:
    if shutil.which("systemctl"):
        run(["systemctl", "--user", "disable", "--now", "gitet-update.timer"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for n in ("gitet-update.service", "gitet-update.timer"):
        (UNIT_DIR / n).unlink(missing_ok=True)
    if shutil.which("systemctl"):
        run(["systemctl", "--user", "daemon-reload"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if shutil.which("crontab"):
        lines = cron_lines()
        if any(CRON_TAG in l for l in lines):
            cron_write([l for l in lines if CRON_TAG not in l])


def notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        env = dict(os.environ)
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
        subprocess.run(["notify-send", "-a", "Gitet", title, body], env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------------
# self-install
# --------------------------------------------------------------------------
def self_path() -> Path:
    return Path(os.path.realpath(__file__))


def _version_of(path: Path):
    try:
        m = re.search(r'^VERSION = "([\d.]+)"', path.read_text(errors="ignore"), re.M)
        return tuple(int(x) for x in m.group(1).split(".")) if m else (0,)
    except OSError:
        return (0,)


def self_install(system: bool = False) -> None:
    src = self_path()
    dest = SYSTEM_BIN if system else USER_BIN
    if system:
        if run(sudo_cmd(["install", "-m", "755", str(src), str(dest)])) != 0:
            raise GitetError("couldn't install to /usr/local/bin")
        ok(f"installed gitet to {dest}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".new")
    shutil.copyfile(src, tmp)
    os.chmod(tmp, 0o755)
    os.replace(tmp, dest)
    ok(f"installed gitet to {dest}")
    if ensure_path():
        warn("added ~/.local/bin to your shell startup files - open a new terminal to use `gitet`")
    elif str(BIN_DIR) not in os.environ.get("PATH", "").split(":"):
        warn("~/.local/bin isn't on PATH for this session; log out/in or open a new terminal")


def ensure_installed() -> None:
    here = self_path()
    for t in (USER_BIN, SYSTEM_BIN):
        if t.exists() and t.resolve() == here:
            return
    for t in (USER_BIN, SYSTEM_BIN):
        if t.exists() and (t.read_bytes() == here.read_bytes() or _version_of(t) > _version_of(here)):
            return  # an equal or newer copy is already installed
    self_install()


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_install(args) -> int:
    only = None
    if args.type:
        if args.type == "package":
            fam = pkg_family(read_os_release())
            if not fam:
                raise GitetError("no system package format known for this distro")
            only = {fam}
        else:
            only = {args.type}
    args._only = only
    rc = 0
    with Lock():
        state = load_state()
        for arg in args.repos:
            try:
                install_one(state, arg, args)
            except GitetError as e:
                err(f"{arg}: {e}")
                rc = 1
    return rc


def cmd_types(args) -> int:
    meta = resolve_repo(args.repo)
    full = meta["full_name"]
    rel = get_release(full, tag=args.tag, pre=args.pre)
    assets = rel.get("assets") or []
    osr = read_os_release()
    fam = pkg_family(osr)
    usable, hidden = analyze(assets)
    plan, _ = build_plan(assets)
    pick = plan[0][1][0]["name"] if plan else None
    print(f"{full}  {rel['tag_name']}")
    print(f"system: {host_arch()}, {osr.get('PRETTY_NAME') or osr.get('NAME') or 'Linux'}"
          f"  (system packages: {fam or 'none known'})")
    if not usable:
        print("\nNo Flatpak, package or AppImage in this release matches your system.")
    for kind in KIND_ORDER:
        lst = usable.get(kind)
        if not lst:
            continue
        good, why = kind_support(kind, fam)
        print(f"\n{LABEL[kind]}  --type {kind}" + ("" if good else f"   [can't install here: {why}]"))
        for a in lst:
            print(f"   {a['name']}  ({human(a.get('size', 0))})" + ("   <- auto-pick" if a["name"] == pick else ""))
    if hidden:
        if args.all:
            print("\nNot usable on this system:")
            for n, why in hidden:
                print(f"   {n}  - {why}")
        else:
            print(f"\n{len(hidden)} other file(s) hidden (wrong architecture/OS or not an app) - use --all to list")
    if usable:
        print(f"\nInstall a specific kind:  gitet install {full} --type <kind>"
              f"\nor a specific file:       gitet install {full} --asset <name>   (or --choose)")
    return 0 if usable else 1


def cmd_remove(args) -> int:
    rc = 0
    with Lock():
        state = load_state()
        for name in args.names:
            try:
                key = find_app(state, name)
                rec = state["apps"][key]
                if not args.yes:
                    if not sys.stdin.isatty():
                        raise GitetError("pass -y to delete without a prompt")
                    ans = input(f"Delete {key} {rec['tag']} ({LABEL[rec['method']]})? [y/N] ")
                    if ans.strip().lower() not in ("y", "yes"):
                        say(f"kept {key}")
                        continue
                try:
                    remove_record(rec, purge=args.purge)
                except GitetError as e:
                    if not args.force:
                        raise GitetError(f"{e} (use --force to forget it anyway)")
                    warn(f"{e} - removing it from gitet's list anyway")
                del state["apps"][key]
                save_state(state)
                ok(f"deleted {key}")
            except GitetError as e:
                err(str(e))
                rc = 1
    return rc


def cmd_update(args) -> int:
    with Lock():
        state = load_state()
        keys = [find_app(state, n) for n in args.names] or sorted(state["apps"])
        if not keys:
            say("nothing installed yet")
            return 0
        updated, _, failed, current = update_apps(state, keys, check=args.check, force=args.force)
    if args.check:
        if not updated:
            ok("everything is up to date")
    else:
        ok(f"{len(updated)} updated, {len(current)} already current" + (f", {len(failed)} failed" if failed else ""))
    return 1 if failed else 0


def cmd_list(args) -> int:
    apps = load_state()["apps"]
    if not apps:
        print("No apps installed. Try: gitet install owner/repo", file=sys.stderr)
        return 0
    w = max(len(k) for k in apps)
    for k in sorted(apps, key=str.lower):
        r = apps[k]
        flags = " [pinned]" if r.get("pinned") else ""
        print(f"{k:<{w}}  {r['tag']:<14} {LABEL[r['method']]:<10}{flags}")
    return 0


def cmd_search(args) -> int:
    items = api("/search/repositories", {"q": " ".join(args.query), "per_page": 10}, cache=False).get("items", [])
    if not items:
        print("no results", file=sys.stderr)
        return 1
    for it in items:
        print(f"{it['full_name']}  ★{it['stargazers_count']}")
        if it.get("description"):
            print(f"    {it['description'][:100]}")
    return 0


def cmd_auto(args) -> int:
    with Lock():
        state = load_state()
        cfg = state["config"]
        if args.action == "on":
            cfg["every_hours"] = args.every or cfg.get("every_hours", 6)
            how = auto_enable(cfg["every_hours"])
            cfg["auto"] = True
            save_state(state)
            ok(f"automatic updates ON (every {cfg['every_hours']}h via {how})")
        elif args.action == "off":
            auto_disable()
            cfg["auto"] = False
            save_state(state)
            ok("automatic updates OFF - gitet is fully dormant")
        else:
            print(f"automatic updates: {'on' if cfg['auto'] else 'off'}"
                  + (f" (every {cfg['every_hours']}h)" if cfg["auto"] else ""))
            if cfg["auto"] and shutil.which("systemctl"):
                print("timer: " + (out(["systemctl", "--user", "is-active", "gitet-update.timer"]).strip() or "not systemd"))
    return 0


def cmd_auto_update(args) -> int:
    global NONINTERACTIVE
    NONINTERACTIVE = True
    state = load_state()
    if not state["config"]["auto"]:
        return 0  # dormant
    try:
        if CRON_LOG.exists() and CRON_LOG.stat().st_size > 256 * 1024:
            CRON_LOG.write_text("")
        with Lock(wait=False):
            state = load_state()
            updated, pending, failed, _ = update_apps(state, sorted(state["apps"]), auto=True)
    except GitetError:
        return 0
    parts = []
    if updated:
        parts.append("Updated: " + ", ".join(updated))
    if pending:
        parts.append("Needs root, run `gitet update`: " + ", ".join(pending))
    if parts:
        notify("Gitet", "\n".join(parts))
    return 0


def cmd_export(args) -> int:
    apps = load_state()["apps"]
    data = {"gitet": 1, "exported": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "apps": [{"repo": k, "tag": v["tag"], "pinned": bool(v.get("pinned")),
                      "pre": bool(v.get("pre")), "method": v["method"]} for k, v in sorted(apps.items())]}
    text = json.dumps(data, indent=2) + "\n"
    if args.file and args.file != "-":
        Path(args.file).write_text(text)
        ok(f"exported {len(apps)} apps to {args.file}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_import(args) -> int:
    try:
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
        entries = json.loads(text)["apps"]
    except (OSError, ValueError, KeyError) as e:
        raise GitetError(f"couldn't read export file: {e}")
    rc = 0
    with Lock():
        state = load_state()
        for e in entries:
            ns = argparse.Namespace(tag=e["tag"] if (e.get("pinned") or args.exact) else None,
                                    pre=bool(e.get("pre")), force=False, yes=True,
                                    dry_run=False, _only=None, asset=None, choose=False)
            try:
                install_one(state, e["repo"], ns)
            except GitetError as ex:
                err(f"{e.get('repo')}: {ex}")
                rc = 1
    return rc


def cmd_self_install(args) -> int:
    self_install(system=args.system)
    return 0


def cmd_self_uninstall(args) -> int:
    with Lock():
        auto_disable()
        for t in (USER_BIN,):
            t.unlink(missing_ok=True)
        if SYSTEM_BIN.exists():
            run(sudo_cmd(["rm", "-f", str(SYSTEM_BIN)]))
        if args.purge:
            shutil.rmtree(DATA_DIR, ignore_errors=True)
            shutil.rmtree(CACHE_DIR, ignore_errors=True)
    ok("gitet removed" + (" (state purged; installed apps were left in place)" if args.purge else
                          " (your installed apps and list were kept)"))
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gitet", description="Install apps straight from GitHub releases "
        "(Flatpak > DEB/RPM > AppImage).",
        epilog="examples:\n  gitet install dpaulat/SupercellWX\n  gitet install owner/repo --dry-run\n"
               "  gitet types owner/repo               # what fits my system?\n  gitet install owner/repo --type appimage\n  gitet delete owner/repo\n  gitet update\n  gitet auto on --every 12\n  gitet export apps.json   # then on another PC:\n"
               "  gitet import apps.json",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"gitet {VERSION}")
    sub = p.add_subparsers(dest="cmd", metavar="command")

    s = sub.add_parser("install", aliases=["i"], help="install apps from GitHub (owner/repo or URL)")
    s.add_argument("repos", nargs="+", metavar="owner/repo")
    s.add_argument("-t", "--tag", help="install this release tag (pins the app)")
    s.add_argument("--pre", action="store_true", help="allow pre-releases")
    s.add_argument("--type", "--only", dest="type",
                   choices=["flatpak", "package", "deb", "rpm", "pacman", "appimage"],
                   help="install this kind instead of auto-selecting ('package' = your distro's format)")
    s.add_argument("--asset", metavar="NAME", help="install the release file matching this name (substring or glob)")
    s.add_argument("--choose", action="store_true", help="pick from the compatible files interactively")
    s.add_argument("-n", "--dry-run", action="store_true", help="show what would be installed")
    s.add_argument("-f", "--force", action="store_true", help="reinstall even if up to date")
    s.add_argument("-y", "--yes", action="store_true", help="don't prompt")
    s.set_defaults(fn=cmd_install)

    s = sub.add_parser("remove", aliases=["rm", "uninstall", "delete", "del"], help="delete installed apps")
    s.add_argument("names", nargs="+")
    s.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    s.add_argument("--purge", action="store_true", help="also delete the app's data (Flatpak)")
    s.add_argument("-f", "--force", action="store_true", help="drop it from gitet's list even if removal fails")
    s.set_defaults(fn=cmd_remove)

    s = sub.add_parser("update", aliases=["upgrade"], help="update installed apps")
    s.add_argument("names", nargs="*", help="apps to update (default: all)")
    s.add_argument("-c", "--check", action="store_true", help="only list available updates")
    s.add_argument("-f", "--force", action="store_true", help="reinstall / ignore pins")
    s.set_defaults(fn=cmd_update)

    s = sub.add_parser("list", aliases=["ls"], help="list installed apps")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("types", aliases=["info", "formats"],
                       help="list the package types a repo's latest release offers for this system")
    s.add_argument("repo", metavar="owner/repo")
    s.add_argument("-t", "--tag", help="look at this release tag")
    s.add_argument("--pre", action="store_true", help="include pre-releases")
    s.add_argument("--all", action="store_true", help="also list files that don't fit this system")
    s.set_defaults(fn=cmd_types)

    s = sub.add_parser("search", help="search GitHub repositories")
    s.add_argument("query", nargs="+")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("auto", help="toggle automatic background updates (default: off)")
    s.add_argument("action", choices=["on", "off", "status"], nargs="?", default="status")
    s.add_argument("--every", type=int, metavar="HOURS", help="check interval (default 6)")
    s.set_defaults(fn=cmd_auto)

    s = sub.add_parser("export", help="export your app list (to a file or stdout)")
    s.add_argument("file", nargs="?")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("import", help="install every app from an export file")
    s.add_argument("file")
    s.add_argument("--exact", action="store_true", help="install the exact exported versions")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("self-install", help="(re)install gitet itself")
    s.add_argument("--system", action="store_true", help="install to /usr/local/bin (uses sudo)")
    s.set_defaults(fn=cmd_self_install)

    s = sub.add_parser("self-uninstall", help="remove gitet (keeps your apps)")
    s.add_argument("--purge", action="store_true", help="also delete gitet's app list and cache")
    s.set_defaults(fn=cmd_self_uninstall)

    s = sub.add_parser("_auto-update")
    s.set_defaults(fn=cmd_auto_update)
    return p


def main() -> int:
    if not sys.platform.startswith("linux"):
        err("gitet only supports Linux")
        return 1
    if os.geteuid() == 0 and not os.environ.get("GITET_ALLOW_ROOT"):
        err("run gitet as your normal user - it calls sudo itself when a step needs root")
        return 1
    argv = sys.argv[1:]
    try:
        if (not argv or argv[0] != "_auto-update") and not os.environ.get("GITET_NO_SELF_INSTALL"):
            ensure_installed()
        parser = build_parser()
        args = parser.parse_args(argv)
        if not getattr(args, "fn", None):
            parser.print_help()
            return 0
        return args.fn(args) or 0
    except GitetError as e:
        err(str(e))
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
