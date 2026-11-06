#!/usr/bin/env python3
"""
gitet - install and update apps straight from GitHub (or GitLab / Codeberg) releases.

    gitet install dpaulat/SupercellWX          # GitHub is the default host
    gitet install gitlab:group/project         # GitLab
    gitet install codeberg:owner/repo          # Codeberg
    gitet update
    gitet auto on

Package hierarchy (first one that exists for your system wins):
    1. Flatpak bundles   (.flatpak / .flatpakref)
    2. System packages   (.deb / .rpm / .pkg.tar.zst, matching your distro)
    3. AppImages         (added to ~/.local/bin and your app launcher)
    4. Archives          (.tar.gz/.xz/.bz2/.zst, .zip or a bare executable; programs linked into ~/.local/bin)

Only assets matching your CPU architecture are considered.

On first run gitet copies itself to ~/.local/bin/gitet (use
`gitet self-install --system` for /usr/local/bin). It never runs a daemon:
when automatic updates are off nothing of gitet is running. When they are on,
a systemd user timer (or a cron entry) launches a short one-shot check.

Environment: GITHUB_TOKEN / GH_TOKEN raises the GitHub API rate limit (GITLAB_TOKEN, CODEBERG_TOKEN and
             GITEA_TOKEN work for the other hosts).
             GITET_NO_SELF_INSTALL=1 skips the self-install step.
"""
from __future__ import annotations

import argparse
import fcntl
import base64
import contextlib
import fnmatch
import hashlib
import html
import json
import os
import platform
import re
import shlex
import shutil
import struct
import tarfile
import subprocess
import sys
import tempfile
import textwrap
import time
import unicodedata
import zipfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "1.4.0"

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
SELF_URL = "https://raw.githubusercontent.com/AstroMeYT/Gitet/main/gitet.py"

NONINTERACTIVE = False  # True for background runs: never prompt for a password

LABEL = {"flatpak": "Flatpak", "deb": "DEB", "rpm": "RPM",
         "pacman": "pacman pkg", "appimage": "AppImage", "archive": "Archive"}


def jprint(obj) -> None:
    sys.stdout.write(json.dumps(obj, indent=2) + "\n")
    sys.stdout.flush()


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


def human(n) -> str:
    if n is None:
        return "size unknown"
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
    t = parse_target(name)
    n = (t.canonical() if t else name).lower().strip()
    apps = state["apps"]
    exact = [k for k in apps if k.lower() == n]
    if exact:
        return exact[0]
    short = [k for k in apps if repo_name(k).lower() == n]
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


# --------------------------------------------------------------------------
# hosting providers: GitHub (default), GitLab, Codeberg / Gitea / Forgejo
# --------------------------------------------------------------------------
TOKEN_ENV = {"github": ("GITHUB_TOKEN", "GH_TOKEN"), "gitlab": ("GITLAB_TOKEN",),
             "codeberg": ("CODEBERG_TOKEN", "FORGEJO_TOKEN", "GITEA_TOKEN"),
             "gitea": ("GITEA_TOKEN", "FORGEJO_TOKEN")}


def token_for(name: str):
    for var in TOKEN_ENV.get(name, ()):
        if os.environ.get(var):
            return os.environ[var]
    for fname in [f"token-{name}"] + (["token"] if name == "github" else []):
        try:
            tok = (XDG_CONFIG / "gitet" / fname).read_text().strip()
            if tok:
                return tok
        except OSError:
            continue
    return None


def http_json(url: str, headers: dict | None = None, cache: bool = True, label: str = "the server", hint: str = ""):
    h = {"User-Agent": f"gitet/{VERSION}"}
    h.update(headers or {})
    ent = _load_cache().get(url) if cache else None
    if ent:
        h["If-None-Match"] = ent["etag"]  # 304s don't count against rate limits
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
            etag = r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304 and ent:
            return ent["data"]
        if e.code == 404:
            raise ApiError(f"not found on {label}" + (f" (private repos need {hint})" if hint else ""), 404)
        if e.code == 429 or e.headers.get("X-RateLimit-Remaining") == "0":
            raise ApiError(f"{label} rate limit reached - " + (f"set {hint} or " if hint else "") + "wait a while", 403)
        if e.code in (401, 403):
            raise ApiError(f"{label} refused the request ({e.code})" + (f" - check {hint}" if hint else ""), e.code)
        raise ApiError(f"{label} API error {e.code}", e.code)
    except urllib.error.URLError as e:
        raise ApiError(f"network error: {e.reason}")
    except ValueError:
        raise ApiError(f"{label} returned something that isn't JSON")
    if cache and etag:
        _load_cache()[url] = {"etag": etag, "data": data}
        _save_cache()
    return data


def http_text(url: str, headers: dict | None = None, label: str = "the server") -> str:
    h = {"User-Agent": f"gitet/{VERSION}"}
    h.update(headers or {})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise ApiError(f"{label}: HTTP {e.code}", e.code)
    except urllib.error.URLError as e:
        raise ApiError(f"network error: {e.reason}")


def api(path: str, params: dict | None = None, cache: bool = True):
    """GitHub REST API."""
    url = "https://api.github.com" + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = token_for("github")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return http_json(url, headers, cache, label="GitHub", hint="GITHUB_TOKEN")


class Target:
    """A repository on some host. `canonical()` is the id gitet stores and prints:
    owner/repo (GitHub), gitlab:group/repo, codeberg:owner/repo, gitea:host/owner/repo."""

    def __init__(self, kind: str, host: str, path: str, prefix: str):
        self.kind, self.host, self.path, self.prefix = kind, host, path, prefix

    def canonical(self) -> str:
        if self.kind == "github":
            return self.path
        if self.prefix == "gitlab":
            return "gitlab:" + (self.path if self.host == "gitlab.com" else f"{self.host}/{self.path}")
        if self.prefix == "codeberg":
            return "codeberg:" + self.path
        return f"gitea:{self.host}/{self.path}"


URL_HOSTS = {"github.com": ("github", ""), "gitlab.com": ("gitlab", "gitlab"), "codeberg.org": ("gitea", "codeberg")}
PREFIXES = {"github": ("github", "", "github.com"), "gitlab": ("gitlab", "gitlab", "gitlab.com"),
            "codeberg": ("gitea", "codeberg", "codeberg.org"), "gitea": ("gitea", "gitea", ""),
            "forgejo": ("gitea", "gitea", "")}


def _strip_git(path: str) -> str:
    return path[:-4] if path.endswith(".git") else path


def parse_target(s: str):
    """owner/repo, github:/gitlab:/codeberg:/gitea:host/ prefixes, or a repo URL -> Target (or None)."""
    s = s.strip()
    m = re.match(r"^(github|gitlab|codeberg|gitea|forgejo):(?!//)(.+)$", s, re.I)
    if m:
        kind, prefix, host = PREFIXES[m.group(1).lower()]
        segs = [x for x in m.group(2).strip().strip("/").split("/") if x]
        if kind == "gitea" and prefix == "gitea":  # self-hosted: gitea:host/owner/repo
            if len(segs) != 3 or "." not in segs[0]:
                return None
            host = segs.pop(0)
        elif kind == "gitlab" and len(segs) >= 3 and "." in segs[0]:  # gitlab:host/group/repo
            host = segs.pop(0)
        need_two = kind != "gitlab"
        if (need_two and len(segs) != 2) or len(segs) < 2:
            return None
        return Target(kind, host, _strip_git("/".join(segs)), prefix)
    m = re.match(r"^(?:https?://)?(?:www\.)?([\w.-]+)/(.+)$", s)
    if m and m.group(1).lower() in URL_HOSTS:
        host = m.group(1).lower()
        kind, prefix = URL_HOSTS[host]
        segs = [x for x in m.group(2).split("?")[0].split("#")[0].split("/") if x]
        if kind == "gitlab":
            segs = segs[:segs.index("-")] if "-" in segs else segs
        else:
            segs = segs[:2]
        if len(segs) < 2:
            return None
        return Target(kind, host, _strip_git("/".join(segs)), prefix)
    m = re.fullmatch(r"([\w.-]+)/([\w.-]+)", s)
    if m:
        return Target("github", "github.com", _strip_git(f"{m.group(1)}/{m.group(2)}"), "")
    return None


def pick_readme(items: list):
    cands = [i for i in items if re.fullmatch(r"readme(\.\w+)?", i.get("name", ""), re.I)]
    rank = lambda i: (0 if i["name"].lower().endswith((".md", ".markdown")) else 1 if "." not in i["name"] else 2, i["name"])
    return sorted(cands, key=rank)[0] if cands else None


class Provider:
    label = ""

    def __init__(self, host: str, prefix: str):
        self.host, self.prefix = host, prefix

    @property
    def name(self) -> str:
        return self.prefix or "github"

    def canonical(self, path: str) -> str:
        return Target(self.kind, self.host, path, self.prefix).canonical()


class GitHub(Provider):
    kind = "github"

    def repo(self, path: str) -> dict:
        d = api(f"/repos/{path}")
        return {"full_name": d["full_name"], "description": d.get("description") or "",
                "html_url": d.get("html_url"), "stargazers_count": d.get("stargazers_count", 0)}

    def release(self, path: str, tag, pre: bool) -> dict:
        if tag:
            return api(f"/repos/{path}/releases/tags/{urllib.parse.quote(tag)}")
        if not pre:
            try:
                return api(f"/repos/{path}/releases/latest")
            except ApiError as e:
                if e.code != 404:
                    raise
        for r in api(f"/repos/{path}/releases", {"per_page": 15}):
            if not r.get("draft"):
                return r
        raise GitetError(f"{path} has no releases")

    def readme(self, path: str) -> dict:
        try:
            d = api(f"/repos/{path}/readme")
        except ApiError as e:
            if e.code == 404:
                raise GitetError(f"{path} doesn't have a README")
            raise
        try:
            text = base64.b64decode(d.get("content", "")).decode("utf-8", errors="replace")
        except ValueError:
            raise GitetError("couldn't decode the README")
        return {"name": d.get("name") or "README", "text": text, "html_url": d.get("html_url")}

    def search(self, q: str, limit: int) -> list:
        items = api("/search/repositories", {"q": q, "per_page": limit}, cache=False).get("items", [])
        return [{"repo": it["full_name"], "description": it.get("description") or "", "stars": it.get("stargazers_count", 0),
                 "language": it.get("language") or "", "url": it.get("html_url"),
                 "updated": it.get("pushed_at") or it.get("updated_at")} for it in items]


class GitLab(Provider):
    kind = "gitlab"
    label = "GitLab"

    def _headers(self) -> dict:
        tok = token_for("gitlab")
        return {"PRIVATE-TOKEN": tok} if tok else {}

    def _url(self, path: str, params=None) -> str:
        return f"https://{self.host}/api/v4{path}" + (("?" + urllib.parse.urlencode(params)) if params else "")

    def _get(self, path: str, params=None, cache: bool = True):
        return http_json(self._url(path, params), self._headers(), cache, label=f"GitLab ({self.host})", hint="GITLAB_TOKEN")

    @staticmethod
    def pid(path: str) -> str:
        return urllib.parse.quote(path, safe="")

    def repo(self, path: str) -> dict:
        d = self._get(f"/projects/{self.pid(path)}")
        return {"full_name": self.canonical(d["path_with_namespace"]), "description": d.get("description") or "",
                "html_url": d.get("web_url"), "stargazers_count": d.get("star_count", 0)}

    def release(self, path: str, tag, pre: bool) -> dict:
        pid = self.pid(path)
        if tag:
            r = self._get(f"/projects/{pid}/releases/{urllib.parse.quote(tag, safe='')}")
        else:
            rels = self._get(f"/projects/{pid}/releases", {"per_page": 20, "order_by": "released_at", "sort": "desc"})
            r = next((x for x in rels if pre or not x.get("upcoming_release")), None)
            if not r:
                raise GitetError(f"{self.canonical(path)} has no releases")
        web = f"https://{self.host}/{path}"
        assets, seen = [], set()

        def add(url: str, label: str):
            if not url.startswith("http") or url in seen:
                return
            base = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])
            name = base if classify(base) else (label if classify(label) else base)
            seen.add(url)
            assets.append({"name": name, "size": None, "browser_download_url": url})

        for l in ((r.get("assets") or {}).get("links") or []):
            add(l.get("direct_asset_url") or l.get("url") or "", l.get("name") or "")
        for m in re.finditer(r"\[([^\]]*)\]\(([^)\s]+)\)", r.get("description") or ""):  # files uploaded into the notes
            add(web + m.group(2) if m.group(2).startswith("/") else m.group(2), m.group(1))
        return {"tag_name": r["tag_name"], "published_at": r.get("released_at") or r.get("created_at"),
                "prerelease": bool(r.get("upcoming_release")), "assets": assets}

    def readme(self, path: str) -> dict:
        pid, full = self.pid(path), self.canonical(path)
        try:
            proj = self._get(f"/projects/{pid}")
            tree = self._get(f"/projects/{pid}/repository/tree", {"per_page": 100})
        except ApiError as e:
            if e.code == 404:
                raise GitetError(f"{full} doesn't have a README")
            raise
        f = pick_readme([i for i in tree if i.get("type") == "blob"])
        if not f:
            raise GitetError(f"{full} doesn't have a README")
        branch = proj.get("default_branch") or "HEAD"
        text = http_text(self._url(f"/projects/{pid}/repository/files/{urllib.parse.quote(f['path'], safe='')}/raw",
                                   {"ref": branch}), self._headers(), label=f"GitLab ({self.host})")
        return {"name": f["name"], "text": text, "html_url": f"{proj.get('web_url')}/-/blob/{branch}/{f['path']}"}

    def search(self, q: str, limit: int) -> list:
        items = self._get("/projects", {"search": q, "order_by": "star_count", "sort": "desc", "per_page": limit,
                                        "simple": "true"}, cache=False)
        return [{"repo": self.canonical(it["path_with_namespace"]), "description": it.get("description") or "",
                 "stars": it.get("star_count", 0), "language": "", "url": it.get("web_url"),
                 "updated": it.get("last_activity_at")} for it in items]


class Gitea(Provider):
    """Codeberg, Forgejo and Gitea share one API."""
    kind = "gitea"

    @property
    def label(self) -> str:
        return "Codeberg" if self.prefix == "codeberg" else f"Gitea ({self.host})"

    def _headers(self) -> dict:
        tok = token_for(self.name)
        return {"Authorization": f"token {tok}"} if tok else {}

    def _url(self, path: str, params=None) -> str:
        return f"https://{self.host}/api/v1{path}" + (("?" + urllib.parse.urlencode(params)) if params else "")

    def _get(self, path: str, params=None, cache: bool = True):
        hint = {"codeberg": "CODEBERG_TOKEN"}.get(self.name, "GITEA_TOKEN")
        return http_json(self._url(path, params), self._headers(), cache, label=self.label, hint=hint)

    def repo(self, path: str) -> dict:
        d = self._get(f"/repos/{path}")
        return {"full_name": self.canonical(d["full_name"]), "description": d.get("description") or "",
                "html_url": d.get("html_url"), "stargazers_count": d.get("stars_count", 0)}

    def release(self, path: str, tag, pre: bool) -> dict:
        if tag:
            return self._get(f"/repos/{path}/releases/tags/{urllib.parse.quote(tag)}")
        if not pre:
            try:
                return self._get(f"/repos/{path}/releases/latest")
            except ApiError as e:
                if e.code != 404:
                    raise
        for r in self._get(f"/repos/{path}/releases", {"limit": 20}):
            if not r.get("draft"):
                return r
        raise GitetError(f"{self.canonical(path)} has no releases")

    def readme(self, path: str) -> dict:
        full = self.canonical(path)
        try:
            items = self._get(f"/repos/{path}/contents")
        except ApiError as e:
            if e.code == 404:
                raise GitetError(f"{full} doesn't have a README")
            raise
        f = pick_readme([i for i in items if isinstance(i, dict) and i.get("type") == "file"]) if isinstance(items, list) else None
        if not f:
            raise GitetError(f"{full} doesn't have a README")
        text = http_text(self._url(f"/repos/{path}/raw/{urllib.parse.quote(f['path'])}"), self._headers(), label=self.label)
        return {"name": f["name"], "text": text, "html_url": f"https://{self.host}/{path}"}

    def search(self, q: str, limit: int) -> list:
        d = self._get("/repos/search", {"q": q, "limit": limit, "sort": "stars", "order": "desc"}, cache=False)
        return [{"repo": self.canonical(it["full_name"]), "description": it.get("description") or "",
                 "stars": it.get("stars_count", 0), "language": it.get("language") or "", "url": it.get("html_url"),
                 "updated": it.get("updated_at")} for it in (d.get("data") or [])]


PROVIDER_CLASSES = {"github": GitHub, "gitlab": GitLab, "gitea": Gitea}


def provider_for(t: Target) -> Provider:
    return PROVIDER_CLASSES[t.kind](t.host, t.prefix)


def default_provider(name: str) -> Provider:
    kind, prefix, host = PREFIXES[name]
    return PROVIDER_CLASSES[kind](host, prefix)


def repo_name(full: str) -> str:
    return full.rsplit("/", 1)[-1]


def resolve_repo(arg: str, yes: bool = False) -> dict:
    t = parse_target(arg)
    if t is None:  # a search term: "name" (GitHub) or "gitlab:name" / "codeberg:name"
        m = re.match(r"^(github|gitlab|codeberg):([^/:]+)$", arg.strip(), re.I)
        pname, term = (m.group(1).lower(), m.group(2)) if m else ("github", arg)
        items = default_provider(pname).search(term, 5)
        if not items:
            raise GitetError(f"no repository found for '{arg}'")
        if len(items) > 1 and not yes:
            if not sys.stdin.isatty():
                raise GitetError(f"'{arg}' is ambiguous; use the full name (try: gitet search {arg})")
            for i, it in enumerate(items, 1):
                print(f"  {i}. {it['repo']}  ★{it['stars']}  {it['description'][:60]}", file=sys.stderr)
            pick = input("Pick [1-%d, Enter=1, q=quit]: " % len(items)).strip().lower()
            if pick == "q":
                raise GitetError("cancelled")
            items = [items[int(pick) - 1]] if pick.isdigit() and 1 <= int(pick) <= len(items) else items[:1]
        t = parse_target(items[0]["repo"])
        if t is None:
            raise GitetError(f"couldn't understand '{items[0]['repo']}'")
    return provider_for(t).repo(t.path)


def get_release(full: str, tag: str | None = None, pre: bool = False) -> dict:
    t = parse_target(full)
    if t is None:
        raise GitetError(f"unrecognised repository '{full}'")
    return provider_for(t).release(t.path, tag, pre)


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
    r"|debuginfo|debugsource|dbgsym|-debug[._-]|\.src\.rpm$|-devel[._-]|-dev_"
    r"|(?<![a-z0-9])(?:src|source|sources|docs?|completions?|symbols|sbom|dbg)(?![a-z0-9])")

ARCHIVE_EXT = (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".tbz", ".tar.zst", ".tzst", ".zip")

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
    if n.endswith(ARCHIVE_EXT):
        return "archive"
    if "linux" in n and not re.search(r"\.[a-z][a-z0-9]{1,4}$", n):  # bare executable, e.g. tool-linux-amd64
        return "archive"
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


KIND_ORDER = ("flatpak", "deb", "rpm", "pacman", "appimage", "archive")
FLATPAK_MARK = "# gitet-managed"


def kind_support(kind: str, fam):
    if kind == "flatpak":
        return (True, "") if shutil.which("flatpak") else (False, "flatpak isn't installed")
    if kind in ("appimage", "archive") or kind == fam:
        return True, ""
    return False, f"this system uses {fam or 'no known package format'}, not {kind}"


def hidden_reason(name: str, host: str) -> str:
    n = name.lower()
    if EXCLUDE.search(n):
        return "not an installable app file (other OS, debug or source package)"
    if not classify(n):
        return "not a Flatpak, system package or AppImage"
    return f"built for another architecture (this system: {host})"


def libc_bonus(name: str, osr: dict) -> int:
    n = name.lower()
    alpine = osr.get("ID", "").lower() == "alpine"
    if "musl" in n:
        return 20 if alpine else 5  # static musl builds run nearly everywhere
    if alpine and ("gnu" in n or "glibc" in n):
        return -20
    return 0


def analyze(assets: list, pattern: str | None = None):
    """Split release assets into usable ({kind: [best first]}) and hidden ([(name, reason)])."""
    host = host_arch()
    osr = read_os_release()
    pat = None
    if pattern:
        pat = pattern.lower()
        if pat in {a["name"].lower() for a in assets}:
            pat = fnmatch.translate(pat) and re.sub(r"([*?\[])", r"[\1]", pat)  # exact file name
        elif not any(ch in pat for ch in "*?["):
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
        bonus = (distro_bonus(name, osr) if kind in ("deb", "rpm", "pacman")
                 else libc_bonus(name, osr) if kind == "archive" else 0)
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
        if only is None and kind not in ("flatpak", fam, "appimage", "archive"):
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
                           f"Name={repo_name(full)}\nExec=flatpak run {shlex.quote(app_id)}\n"
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
        meta["Name"] = repo_name(full)
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


def _is_exec_file(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            head = f.read(4)
    except OSError:
        return False
    return head == b"\x7fELF" or head[:2] == b"#!"


def safe_extract(path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()

    def inside(p: Path) -> bool:
        r = p.resolve()
        return r == root or str(r).startswith(str(root) + os.sep)

    name = path.name.lower()
    try:
        if name.endswith((".tar.zst", ".tzst")):
            if not shutil.which("zstd"):
                raise InstallError("this archive is .zst compressed - install zstd first")
            if run(["tar", "--zstd", "-xf", str(path), "-C", str(dest)]) != 0:
                raise InstallError("couldn't extract the archive")
        elif zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    if not inside(dest / info.filename):
                        raise InstallError("archive contains an unsafe path")
                    zf.extract(info, dest)
                    mode = (info.external_attr >> 16) & 0o777
                    if mode and not info.is_dir():
                        os.chmod(dest / info.filename, mode | 0o200)
        elif tarfile.is_tarfile(path):
            with tarfile.open(path) as tf:
                members = []
                for m in tf.getmembers():
                    target = dest / m.name
                    if m.isdev() or not inside(target):
                        continue
                    if m.issym() and not inside(target.parent / m.linkname):
                        continue
                    if m.islnk() and not inside(dest / m.linkname):
                        continue
                    members.append(m)
                kw = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
                tf.extractall(dest, members=members, **kw)
        else:
            raise InstallError("unrecognised archive format")
    except (tarfile.TarError, zipfile.BadZipFile, OSError) as e:
        raise InstallError(f"couldn't extract the archive: {e}")


def pick_executables(root: Path, slug: str) -> list:
    def depth(p: Path) -> int:
        return len(p.relative_to(root).parts)

    files = [p for p in root.rglob("*") if p.is_file() and ".so" not in p.name and _is_exec_file(p)]
    pref = [p for p in files if (p.parent == root or p.parent.name == "bin") and depth(p) <= 3]
    chosen = ([p for p in pref if os.access(p, os.X_OK)]
              or [p for p in files if os.access(p, os.X_OK) and depth(p) <= 3]
              or [p for p in pref if elf_arch(p) or _is_exec_file(p)])
    key = slug.replace("-", "").replace("_", "")
    chosen.sort(key=lambda p: (key not in p.name.lower().replace("-", "").replace("_", ""), depth(p), p.name))
    return chosen[:12]


def command_name(p: Path, slug: str) -> str:
    if re.search(r"(?i)(linux|x86|amd64|x64|arm64|aarch64|armv7)", p.name):
        return slug
    return p.name


def place_link(name: str, target: Path, app_dir: Path, prev) -> Path | None:
    link = BIN_DIR / name
    if link.is_symlink() or link.exists():
        ours = link.is_symlink() and (str(os.readlink(link)).startswith(str(app_dir))
                                      or str(link) in ((prev or {}).get("links") or []))
        if ours:
            link.unlink()
        else:
            warn(f"{link} already exists and isn't ours; using {name}-gitet instead")
            link = BIN_DIR / f"{name}-gitet"
            if link.is_symlink() or link.exists():
                link.unlink()
    link.symlink_to(target)
    return link


def install_archive(path: Path, full: str, slug: str, prev) -> dict:
    ensure_path()
    host = host_arch()
    work = path.parent / "x"
    with open(path, "rb") as f:
        head = f.read(4)
    if head == b"\x7fELF":  # a bare executable
        got = elf_arch(path)
        if got and not arch_compat(got, host):
            raise InstallError(f"architecture mismatch: binary is {got}, system is {host}")
        stage = work / slug
        stage.mkdir(parents=True)
        shutil.copyfile(path, stage / slug)
        os.chmod(stage / slug, 0o755)
    else:
        safe_extract(path, work / "x")
        stage = work / "x"
        entries = list(stage.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            stage = entries[0]
    exes = pick_executables(stage, slug)
    if not exes:
        raise InstallError("couldn't find an executable program inside the archive")
    for e in exes:
        got = elf_arch(e)
        if got and not arch_compat(got, host):
            raise InstallError(f"architecture mismatch: {e.name} is {got}, system is {host}")
        if not os.access(e, os.X_OK):
            os.chmod(e, 0o755)
    rels = [e.relative_to(stage) for e in exes]

    app_dir = DATA_DIR / "apps" / slug
    app_dir.parent.mkdir(parents=True, exist_ok=True)
    staged, old = app_dir.with_name(slug + ".new"), app_dir.with_name(slug + ".old")
    shutil.rmtree(staged, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    shutil.move(str(stage), str(staged))
    if app_dir.exists():
        os.replace(app_dir, old)
    os.replace(staged, app_dir)
    shutil.rmtree(old, ignore_errors=True)

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    links = []
    for rel in rels:
        exe = app_dir / rel
        links.append(str(place_link(command_name(exe, slug), exe, app_dir, prev)))
    for oldlink in (prev or {}).get("links") or []:
        if oldlink not in links and Path(oldlink).is_symlink():
            Path(oldlink).unlink()
    rec = {"path": str(app_dir), "links": links, "link": links[0], "desktop": "", "icon": ""}

    desktops = sorted(app_dir.rglob("*.desktop"))[:1]  # desktop apps usually ship their own launcher entry
    if desktops:
        d = parse_desktop(desktops[0])
        meta = {"Name": d.get("Name") or repo_name(full), "icon": ""}
        for k in ("Comment", "Categories", "MimeType", "StartupWMClass", "Terminal"):
            if d.get(k):
                meta[k] = d[k]
        m = re.search(r"%[fFuU]", d.get("Exec", ""))
        meta["code"] = m.group(0) if m else ""
        icon_name = d.get("Icon", "")
        cands = [Path(icon_name)] if icon_name.startswith("/") else []
        if icon_name and not cands:
            cands = sorted(app_dir.rglob(f"{icon_name}.svg")) + sorted(
                app_dir.rglob(f"{icon_name}.png"), key=lambda q: q.stat().st_size, reverse=True)
        for c in cands:
            if c.is_file():
                (DATA_DIR / "icons").mkdir(parents=True, exist_ok=True)
                dest = DATA_DIR / "icons" / (slug + sniff_icon_ext(c))
                shutil.copyfile(c, dest)
                meta["icon"] = str(dest)
                break
        rec["desktop"] = str(write_desktop(slug, Path(links[0]), meta, full))
        rec["icon"] = meta["icon"]
        refresh_desktop_db()
    return rec


INSTALLERS = {"flatpak": install_flatpak, "deb": install_deb, "rpm": install_rpm,
              "pacman": install_pacman, "appimage": install_appimage, "archive": install_archive}


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
    elif kind == "archive" and rec.get("path"):
        shutil.rmtree(rec["path"], ignore_errors=True)
    # launcher entries / PATH commands that gitet created
    for l in dict.fromkeys([rec.get("link")] + list(rec.get("links") or [])):
        if l:
            link = Path(l)
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
    if kind in ("flatpak", "appimage", "archive"):
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
                extra = INSTALLERS[kind](dest, full, slugify(repo_name(full)), prev)
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
        return dict(prev, already=True)
    say(f"release {tag}" + (" (pre-release)" if rel.get("prerelease") else "")
        + (f", {rel['published_at'][:10]}" if rel.get("published_at") else ""))
    rec = do_install(full, rel, args._only, args.dry_run, prev,
                     getattr(args, "asset", None), getattr(args, "choose", False))
    if args.dry_run:
        return None
    rec["pinned"] = bool(args.tag)
    rec["pre"] = bool(args.pre or rel.get("prerelease"))
    rec["description"] = meta.get("description") or ""
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
    return rec


def update_apps(state: dict, keys: list, check: bool = False, force: bool = False, auto: bool = False) -> dict:
    r = {k: [] for k in ("updated", "available", "pending", "failed", "current", "pinned")}
    for key in keys:
        rec = state["apps"][key]
        try:
            if rec.get("pinned") and not force:
                say(f"{key}: pinned to {rec['tag']}, skipping")
                r["pinned"].append({"repo": key, "tag": rec["tag"]})
                continue
            rel = get_release(key, pre=rec.get("pre", False))
            tag = rel["tag_name"]
            if tag == rec["tag"] and not force:
                r["current"].append({"repo": key, "tag": tag})
                continue
            if check:
                r["available"].append({"repo": key, "installed": rec["tag"], "latest": tag, "method": rec["method"]})
                continue
            say(f"{key}: {rec['tag']} -> {tag}")
            new = do_install(key, rel, {rec["method"]}, False, rec)
            new["pinned"] = rec.get("pinned", False)
            new["pre"] = rec.get("pre", False)
            new["description"] = rec.get("description", "")
            state["apps"][key] = new
            save_state(state)
            r["updated"].append({"repo": key, "from": rec["tag"], "to": tag})
            ok(f"updated {key} to {tag}")
        except NeedRoot:
            r["pending"].append(key)
            warn(f"{key}: update needs root, skipped")
        except GitetError as e:
            r["failed"].append({"repo": key, "error": str(e).splitlines()[0]})
            warn(f"{key}: {e}")
    return r


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
    if args.json and args.dry_run:
        raise GitetError("--json can't be combined with --dry-run (use: gitet types owner/repo --json)")
    rc, results = 0, []
    with Lock():
        state = load_state()
        for arg in args.repos:
            try:
                rec = install_one(state, arg, args)
                if rec:
                    results.append({"repo": rec["repo"], "ok": True, "tag": rec["tag"], "method": rec["method"],
                                    "asset": rec.get("asset"), "already": bool(rec.get("already"))})
                else:
                    results.append({"repo": arg, "ok": True, "dry_run": True})
            except GitetError as e:
                err(f"{arg}: {e}")
                results.append({"repo": arg, "ok": False, "error": str(e)})
                rc = 1
    if args.json:
        jprint({"results": results})
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
    pretty = osr.get("PRETTY_NAME") or osr.get("NAME") or "Linux"
    types = []
    for kind in KIND_ORDER:
        lst = usable.get(kind)
        if lst:
            good, why = kind_support(kind, fam)
            types.append({"kind": kind, "label": LABEL[kind], "usable": good, "reason": why,
                          "assets": [{"name": a["name"], "size": a.get("size", 0), "auto_pick": a["name"] == pick}
                                     for a in lst]})
    data = {"repo": full, "tag": rel["tag_name"], "published_at": rel.get("published_at"),
            "url": meta.get("html_url"), "description": meta.get("description") or "",
            "stars": meta.get("stargazers_count", 0),
            "system": {"arch": host_arch(), "os": pretty, "package_format": fam},
            "auto_pick": {"kind": plan[0][0], "asset": pick} if plan else None,
            "types": types, "hidden": [{"name": n, "reason": w} for n, w in hidden]}
    if args.json:
        jprint(data)
        return 0 if types else 1
    print(f"{full}  {data['tag']}")
    print(f"system: {host_arch()}, {pretty}  (system packages: {fam or 'none known'})")
    if not types:
        print("\nNo Flatpak, package, AppImage or archive in this release matches your system.")
    for t in types:
        print(f"\n{t['label']}  --type {t['kind']}" + ("" if t["usable"] else f"   [can't install here: {t['reason']}]"))
        for a in t["assets"]:
            print(f"   {a['name']}  ({human(a['size'])})" + ("   <- auto-pick" if a["auto_pick"] else ""))
    if hidden:
        if args.all:
            print("\nNot usable on this system:")
            for n, why in hidden:
                print(f"   {n}  - {why}")
        else:
            print(f"\n{len(hidden)} other file(s) hidden (wrong architecture/OS or not an app) - use --all to list")
    if types:
        print(f"\nInstall a specific kind:  gitet install {full} --type <kind>"
              f"\nor a specific file:       gitet install {full} --asset <name>   (or --choose)")
    return 0 if types else 1


def cmd_remove(args) -> int:
    rc, results = 0, []
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
                        results.append({"repo": key, "ok": True, "kept": True})
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
                results.append({"repo": key, "ok": True})
            except GitetError as e:
                err(str(e))
                results.append({"repo": name, "ok": False, "error": str(e)})
                rc = 1
    if args.json:
        jprint({"results": results})
    return rc


def cmd_update(args) -> int:
    with (contextlib.nullcontext() if args.check else Lock()):
        state = load_state()
        keys = [find_app(state, n) for n in args.names] or sorted(state["apps"])
        if not keys:
            say("nothing installed yet")
        r = update_apps(state, keys, check=args.check, force=args.force)
    if args.json:
        jprint(r)
    elif args.check:
        for a in r["available"]:
            print(f"{a['repo']}  {a['installed']} -> {a['latest']}")
        if not r["available"]:
            ok("everything is up to date")
    elif keys:
        ok(f"{len(r['updated'])} updated, {len(r['current'])} already current"
           + (f", {len(r['failed'])} failed" if r["failed"] else ""))
    return 1 if r["failed"] else 0


def cmd_list(args) -> int:
    apps = load_state()["apps"]
    if args.json:
        jprint({"gitet": VERSION, "apps": [dict(r, repo=k, label=LABEL[r["method"]])
                                           for k, r in sorted(apps.items(), key=lambda kv: kv[0].lower())]})
        return 0
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
    q = " ".join(args.query)
    names = ["github", "gitlab", "codeberg"] if args.on == "all" else [args.on]
    items, errors = [], []
    for n in names:
        try:
            for it in default_provider(n).search(q, 20):
                items.append(dict(it, host=n))
        except GitetError as e:
            if len(names) == 1:
                raise
            errors.append(f"{n}: {e}")
            warn(f"{n}: {e}")
    if args.json:
        jprint({"results": items, "errors": errors})
        return 0
    if not items:
        print("no results", file=sys.stderr)
        return 1
    for it in items[:30 if len(names) > 1 else 10]:
        print(f"{it['repo']}  ★{it['stars']}")
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
        timer = ""
        if cfg["auto"] and shutil.which("systemctl"):
            timer = out(["systemctl", "--user", "is-active", "gitet-update.timer"]).strip() or "not systemd"
        if args.json:
            jprint({"auto": cfg["auto"], "every_hours": cfg["every_hours"], "timer": timer})
        elif args.action == "status":
            print(f"automatic updates: {'on' if cfg['auto'] else 'off'}"
                  + (f" (every {cfg['every_hours']}h)" if cfg["auto"] else ""))
            if timer:
                print("timer: " + timer)
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
            r = update_apps(state, sorted(state["apps"]), auto=True)
            updated = [f"{u['repo']} {u['to']}" for u in r["updated"]]
            pending = r["pending"]
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


# --------------------------------------------------------------------------
# README viewing (Markdown -> plain text, stdlib only)
# --------------------------------------------------------------------------
def dw(s: str) -> int:
    """Display width (wide CJK = 2, combining marks = 0)."""
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, w: int, align: str) -> str:
    extra = max(w - dw(s), 0)
    if align == "r":
        return " " * extra + s
    if align == "c":
        return " " * (extra // 2) + s + " " * (extra - extra // 2)
    return s + " " * extra


_HR = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_ATX = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
_LIST = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_CODE = re.compile(r"^\s*\x02(\d+)\x02\s*$")
_TBL_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_SETEXT = re.compile(r"^\s{0,3}(=+|-+)\s*$")


def _indent(l: str) -> int:
    return len(l) - len(l.lstrip(" "))


def _split_row(line: str) -> list:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


class MdText:
    """Small Markdown-to-plain-text converter: headings, lists (as '-'), tables, quotes, code, links..."""

    def __init__(self, width: int = 80):
        self.width = max(width, 30)
        self.codes: list = []
        self.spans: list = []
        self.refs: dict = {}

    # ---- preprocessing -------------------------------------------------
    def _span(self, s: str) -> str:
        self.spans.append(s.strip())
        return f"\x05{len(self.spans) - 1}\x05"

    def _grab_fences(self, text: str) -> str:
        lines, out, i = text.split("\n"), [], 0
        while i < len(lines):
            m = re.match(r"^(\s*)(`{3,}|~{3,})(.*)$", lines[i])
            if m and not (m.group(2)[0] == "`" and "`" in m.group(3)):
                indent, fence = m.group(1), m.group(2)
                close = re.compile(rf"^\s*{re.escape(fence[0])}{{{len(fence)},}}\s*$")
                j, body = i + 1, []
                while j < len(lines) and not close.match(lines[j]):
                    body.append(lines[j][len(indent):] if lines[j].startswith(indent) else lines[j].lstrip())
                    j += 1
                self.codes.append((m.group(3).strip(), body))
                out.append(f"{indent}\x02{len(self.codes) - 1}\x02")
                i = j + 1
            else:
                out.append(lines[i])
                i += 1
        return "\n".join(out)

    def _clean_html(self, t: str) -> str:
        F = re.S | re.I
        t = re.sub(r"<!--.*?-->", "", t, flags=re.S)

        def pre(m):
            body = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip("\n")
            self.codes.append(("", body.split("\n")))
            return f"\n\n\x02{len(self.codes) - 1}\x02\n\n"

        t = re.sub(r"<pre\b[^>]*>(.*?)</pre\s*>", pre, t, flags=F)
        t = re.sub(r"<br\s*/?>", "\x03", t, flags=re.I)
        t = re.sub(r"<hr\s*/?>", "\n\n---\n\n", t, flags=re.I)
        t = re.sub(r"<h([1-6])\b[^>]*>(.*?)</h\1\s*>",
                   lambda m: "\n\n" + "#" * int(m.group(1)) + " " + re.sub(r"\s+", " ", m.group(2)).strip() + "\n\n",
                   t, flags=F)
        t = re.sub(r"<img\b[^>]*?\balt\s*=\s*(?:\"([^\"]*)\"|'([^']*)')[^>]*>",
                   lambda m: f"![{m.group(1) or m.group(2) or ''}](x)", t, flags=F)
        t = re.sub(r"<img\b[^>]*>", "", t, flags=F)
        t = re.sub(r"<a\b[^>]*?\bhref\s*=\s*(?:\"([^\"]*)\"|'([^']*)')[^>]*>(.*?)</a\s*>",
                   lambda m: f"[{m.group(3).strip()}]({m.group(1) or m.group(2)})", t, flags=F)
        t = re.sub(r"<(b|strong)\b[^>]*>(.*?)</\1\s*>", r"**\2**", t, flags=F)
        t = re.sub(r"<(i|em)\b[^>]*>(.*?)</\1\s*>", r"*\2*", t, flags=F)
        t = re.sub(r"<code\b[^>]*>(.*?)</code\s*>", lambda m: self._span(re.sub(r"<[^>]+>", "", m.group(1))), t, flags=F)
        t = re.sub(r"<li\b[^>]*>", "\n- ", t, flags=re.I)
        blockish = r"p|div|details|summary|center|section|article|header|footer|table|thead|tbody|tr|ul|ol|blockquote|figure|figcaption"
        t = re.sub(rf"</(?:{blockish}|li)\s*>", "\n\n", t, flags=re.I)
        t = re.sub(rf"<(?:{blockish})\b[^>]*>", "\n\n", t, flags=re.I)
        t = re.sub(r"</t[dh]\s*>", "  ", t, flags=re.I)
        return re.sub(r"</?[A-Za-z][\w:-]*(?:\s[^<>]*)?/?>", "", t)

    def _extract_refs(self, text: str) -> str:
        keep = []
        for l in text.split("\n"):
            m = re.match(r"^\s{0,3}\[([^\]^][^\]]*)\]:\s*<?(\S+?)>?(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*$", l)
            if m:
                self.refs[m.group(1).strip().lower()] = m.group(2)
            else:
                keep.append(l)
        return "\n".join(keep)

    # ---- inline --------------------------------------------------------
    def inline(self, t: str) -> str:
        esc: list = []

        def e(m):
            esc.append(m.group(1))
            return f"\x04{len(esc) - 1}\x04"

        t = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>~<])", e, t)
        t = re.sub(r"\[(!\[[^\]]*\]\([^)]*\))\]\([^)]*\)", r"\1", t)  # badge link -> just the badge
        t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", lambda m: f"\x06{m.group(1).strip()}\x06" if m.group(1).strip() else "", t)
        t = re.sub(r"!\[([^\]]*)\]\[[^\]]*\]", lambda m: f"\x06{m.group(1).strip()}\x06" if m.group(1).strip() else "", t)

        def link(m):
            text, url = m.group(1).strip(), m.group(2).strip()
            if not url or url.startswith("#") or text == url:
                return text or url
            return f"{text} ({url})" if text else url

        t = re.sub(r"\[([^\]]*)\]\(\s*<?([^)\s>]*)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)", link, t)

        def ref(m, text, key):
            url = self.refs.get(key.strip().lower())
            if not url:
                return m.group(0)
            return text if (url.startswith("#") or text == url) else f"{text} ({url})"

        t = re.sub(r"\[([^\]]+)\]\[([^\]]*)\]", lambda m: ref(m, m.group(1), m.group(2) or m.group(1)), t)
        t = re.sub(r"\[([^\]^]+)\](?![(\[])", lambda m: ref(m, m.group(1), m.group(1)), t)
        t = re.sub(r"<(https?://[^>\s]+|mailto:[^>\s]+|[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+)>", r"\1", t)
        t = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"\1", t)
        t = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"\1", t)
        t = re.sub(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])", r"\1", t)
        t = re.sub(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)", r"\1", t)
        t = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"\1", t)
        t = html.unescape(t).replace("\xa0", " ")
        t = re.sub(r"[ \t]+", " ", t)
        t = re.sub(r"\x06([^\x06]*)\x06", r"[\1]", t)
        t = re.sub(r"\x05(\d+)\x05", lambda m: "`" + self.spans[int(m.group(1))] + "`", t)
        return re.sub(r"\x04(\d+)\x04", lambda m: esc[int(m.group(1))], t)

    # ---- blocks --------------------------------------------------------
    def _heading(self, lvl: int, txt: str, width: int) -> list:
        txt = txt.replace("\x03", " ").strip()
        if not txt:
            return []
        if lvl >= 4:
            return textwrap.wrap("» " + txt, width, subsequent_indent="  ", break_long_words=False)
        lines = textwrap.wrap(txt, width, break_long_words=False) or [txt]
        return lines + [{1: "=", 2: "-", 3: "~"}[lvl] * min(max(dw(l) for l in lines), width)]

    def _table(self, header: list, rows: list, aligns: list, width: int) -> list:
        ncol = len(header)

        def fix(r):
            return [self.inline(c).replace("\x03", " ").strip() for c in (r + [""] * ncol)[:ncol]]

        data = [fix(header)] + [fix(r) for r in rows]
        colw = [max(1, max(dw(r[c]) for r in data)) for c in range(ncol)]
        avail = width - (3 * ncol + 1)
        while sum(colw) > avail:
            k = max(range(ncol), key=lambda c: colw[c])
            if colw[k] <= 6:
                break
            colw[k] -= 1
        if sum(colw) > avail:  # too many columns for this width: "header: value" lists
            out = []
            for r in data[1:]:
                out += [f"{h}: {v}" for h, v in zip(data[0], r) if v]
                out.append("")
            return out[:-1]
        aligns = (aligns + ["l"] * ncol)[:ncol]

        def border(ch):
            return "+" + "+".join(ch * (w + 2) for w in colw) + "+"

        def row(cells):
            wrapped = [textwrap.wrap(c, colw[i], break_long_words=True, break_on_hyphens=False) or [""]
                       for i, c in enumerate(cells)]
            return ["| " + " | ".join(_pad(w[k] if k < len(w) else "", colw[i], aligns[i])
                                       for i, w in enumerate(wrapped)) + " |"
                    for k in range(max(len(w) for w in wrapped))]

        out = [border("-")] + row(data[0]) + [border("=")]
        for r in data[1:]:
            out += row(r)
        return out + [border("-")]

    def _blocks(self, lines: list, width: int) -> list:
        out: list = []
        i, n = 0, len(lines)

        def gap():
            if out and out[-1] != "":
                out.append("")

        def table_at(k):
            if k + 1 >= n or "|" not in lines[k]:
                return False
            sep = lines[k + 1]
            return bool(_TBL_SEP.match(sep)) and "-" in sep and ("|" in sep or len(_split_row(lines[k])) == 1)

        def starts_block(k):
            l = lines[k]
            return bool(_ATX.match(l) or _HR.match(l) or _CODE.match(l) or _QUOTE.match(l)
                        or _LIST.match(l) or table_at(k))

        while i < n:
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            m = _CODE.match(line)
            if m:
                gap()
                out += [("    " + b).rstrip() for b in self.codes[int(m.group(1))][1]]
                gap()
                i += 1
                continue
            m = _ATX.match(line)
            if m:
                gap()
                out += self._heading(len(m.group(1)), self.inline(m.group(2)), width)
                gap()
                i += 1
                continue
            if _HR.match(line):
                gap()
                out.append("─" * min(width, 60))
                gap()
                i += 1
                continue
            if _QUOTE.match(line):
                block = []
                while i < n and _QUOTE.match(lines[i]):
                    block.append(_QUOTE.match(lines[i]).group(1))
                    i += 1
                alert = re.fullmatch(r"\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]", block[0].strip())
                if alert:
                    block[0] = alert.group(1) + ":"
                inner = self._blocks(block, max(width - 2, 20))
                while inner and inner[-1] == "":
                    inner.pop()
                gap()
                out += [("| " + l).rstrip() for l in inner]
                gap()
                continue
            if table_at(i):
                hdr = _split_row(line)
                aligns = ["c" if c.startswith(":") and c.endswith(":") else "r" if c.endswith(":") else "l"
                          for c in _split_row(lines[i + 1])]
                i += 2
                rows = []
                while i < n and lines[i].strip() and "|" in lines[i] and not (_ATX.match(lines[i]) or _QUOTE.match(lines[i])):
                    rows.append(_split_row(lines[i]))
                    i += 1
                gap()
                out += self._table(hdr, rows, aligns, width)
                gap()
                continue
            m = _LIST.match(line)
            if m:
                base = len(m.group(1))
                j, blk = i, []

                def kind(l):
                    mk = _LIST.match(l).group(2)
                    return "o" if mk[0].isdigit() else mk

                first_kind = kind(line)
                while j < n:
                    l = lines[j]
                    if not l.strip():
                        k = j + 1
                        while k < n and not lines[k].strip():
                            k += 1
                        if k < n and (_indent(lines[k]) > base or (
                                _LIST.match(lines[k]) and _indent(lines[k]) == base and not _HR.match(lines[k])
                                and kind(lines[k]) == first_kind)):
                            blk.extend(lines[j:k])
                            j = k
                            continue
                        break
                    ind = _indent(l)
                    if j == i or ind > base or (ind == base and _LIST.match(l) and not _HR.match(l)
                                                and kind(l) == first_kind):
                        blk.append(l)
                        j += 1
                    elif ind >= base and not starts_block(j):
                        blk.append(l)  # lazy continuation of the previous line
                        j += 1
                    else:
                        break
                items: list = []
                for l in blk:
                    lm = _LIST.match(l)
                    if lm and len(lm.group(1)) <= base + 1 and not _HR.match(l):
                        items.append([lm.group(2), [lm.group(3)], len(lm.group(1)) + len(lm.group(2)) + 1])
                    elif items:
                        items[-1][1].append(l)
                num = None
                for marker, body, ci in items:
                    body2 = [body[0]] + [b[min(_indent(b), ci):] if b.strip() else "" for b in body[1:]]
                    if marker[0].isdigit():
                        num = int(re.match(r"\d+", marker).group()) if num is None else num + 1
                        prefix = f"{num}{marker[-1]} "
                    else:
                        prefix = "- "
                    inner = self._blocks(body2, max(width - len(prefix), 20))
                    while inner and inner[-1] == "":
                        inner.pop()
                    if all(b.strip() for b in body2) and not any(_CODE.match(b) for b in body2):
                        inner = [l for l in inner if l != ""]  # tight item
                    inner = inner or [""]
                    out.append((prefix + inner[0]).rstrip())
                    out += [(" " * len(prefix) + l).rstrip() if l else "" for l in inner[1:]]
                i = j
                gap()
                continue
            # paragraph (or setext heading)
            para, head = [], None
            while i < n and lines[i].strip():
                if para:
                    sm = _SETEXT.match(lines[i])
                    if sm:
                        head = 1 if sm.group(1)[0] == "=" else 2
                        i += 1
                        break
                    if starts_block(i):
                        break
                para.append(lines[i])
                i += 1
            if head:
                gap()
                out += self._heading(head, self.inline(" ".join(p.strip() for p in para)), width)
                gap()
                continue
            pieces, buf = [], []
            for l in para:
                buf.append(l.strip())
                if l.endswith("  ") or l.endswith("\\"):  # hard line break
                    t = " ".join(buf)
                    pieces.append(t[:-1] if t.endswith("\\") else t)
                    buf = []
            if buf:
                pieces.append(" ".join(buf))
            text_lines = []
            for pc in pieces:
                for sub in pc.split("\x03"):
                    txt = self.inline(sub).strip()
                    if txt:
                        text_lines += textwrap.wrap(txt, width, break_long_words=False, break_on_hyphens=False)
            if text_lines:
                gap()
                out += text_lines
                gap()
        return out

    def convert(self, md: str) -> str:
        md = md.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
        text = self._grab_fences(md)
        text = re.sub(r"(?<!`)(`+)([^`\n].*?)\1(?!`)", lambda m: self._span(m.group(2)), text)
        text = self._extract_refs(self._clean_html(text))
        res = []
        for l in self._blocks(text.split("\n"), self.width):
            l = l.replace("\x03", " ").rstrip()
            if l == "" and (not res or res[-1] == ""):
                continue
            res.append(l)
        while res and res[-1] == "":
            res.pop()
        return "\n".join(res)


def page(text: str, no_pager: bool = False) -> None:
    if sys.stdout.isatty() and not no_pager and text.count("\n") >= shutil.get_terminal_size((80, 24)).lines - 1:
        cmd = shlex.split(os.environ.get("PAGER") or "less -FRX")
        if cmd and shutil.which(cmd[0]):
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, text=True)
            except OSError:
                proc = None
            if proc:
                try:
                    proc.communicate(text)
                except BrokenPipeError:
                    pass
                return
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BrokenPipeError:
        pass


def cmd_readme(args) -> int:
    meta = resolve_repo(args.repo)
    full = meta["full_name"]
    t = parse_target(full)
    data = provider_for(t).readme(t.path)
    raw = data["text"]
    name = data["name"]
    is_md = name.lower().endswith((".md", ".markdown", ".mdown", ".mkd", ".mdx"))
    if args.json:
        width = args.width or 90
        jprint({"repo": full, "name": name, "url": data.get("html_url"), "markdown": is_md, "source": raw,
                "text": MdText(width).convert(raw) if is_md else raw})
        return 0
    if args.raw:
        body = raw
    elif is_md:
        body = MdText(args.width or min(shutil.get_terminal_size((80, 24)).columns, 100)).convert(raw)
    else:
        say(f"{name} isn't Markdown - showing it as-is")
        body = raw
    head = f"{full} - {name}"
    page(f"{head}\n{'=' * len(head)}\n\n{body.rstrip()}\n", args.no_pager)
    return 0


def cmd_self_update(args) -> int:
    url = os.environ.get("GITET_URL") or SELF_URL
    say(f"checking {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"gitet/{VERSION}", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
    except (urllib.error.URLError, OSError) as e:
        raise GitetError(f"couldn't fetch the latest gitet: {getattr(e, 'reason', e)}")
    text = data.decode("utf-8", errors="replace")
    m = re.search(r'^VERSION = "([\d.]+)"', text, re.M)
    if not text.startswith("#!") or "python" not in text.splitlines()[0] or not m:
        raise GitetError("downloaded file doesn't look like gitet - aborting")
    try:
        compile(text, "gitet.py", "exec")
    except SyntaxError as e:
        raise GitetError(f"downloaded gitet.py has a syntax error ({e.msg}) - aborting")
    remote = tuple(int(x) for x in m.group(1).split("."))
    local = tuple(int(x) for x in VERSION.split("."))
    latest = m.group(1)
    if args.json and (args.check or (remote <= local and not args.force)):
        jprint({"current": VERSION, "latest": latest, "update_available": remote > local})
        return 0
    if remote <= local and not args.force:
        ok(f"gitet {VERSION} is up to date" if remote == local else
           f"gitet {VERSION} is newer than the published {latest}; nothing to do")
        return 0
    if args.check:
        print(f"gitet {VERSION} -> {latest}")
        return 0
    here = self_path()
    dest = next((t for t in (USER_BIN, SYSTEM_BIN) if t.exists() and t.resolve() == here), None) or installed_path()
    tmp = Path(tempfile.mkdtemp(prefix="gitet-up-")) / "gitet"
    try:
        tmp.write_bytes(data)
        os.chmod(tmp, 0o755)
        if os.access(dest.parent, os.W_OK) and (not dest.exists() or os.access(dest, os.W_OK)):
            staged = dest.with_suffix(".new")
            shutil.copyfile(tmp, staged)
            os.chmod(staged, 0o755)
            os.replace(staged, dest)
        elif run(sudo_cmd(["install", "-m", "755", str(tmp), str(dest)])) != 0:
            raise GitetError(f"couldn't write {dest}")
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    ok(f"gitet updated {VERSION} -> {m.group(1)} ({dest})")
    if args.json:
        jprint({"current": latest, "latest": latest, "update_available": False, "updated": True})
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
               "  gitet install gitlab:group/project   # other hosts: gitlab: codeberg: gitea:host/owner/repo\n  gitet types owner/repo               # what fits my system?\n  gitet readme owner/repo              # read its README\n  gitet install owner/repo --type appimage\n  gitet delete owner/repo\n  gitet update\n  gitet self-update          # update gitet itself\n  gitet auto on --every 12\n  gitet export apps.json   # then on another PC:\n"
               "  gitet import apps.json",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"gitet {VERSION}")
    sub = p.add_subparsers(dest="cmd", metavar="command")

    s = sub.add_parser("install", aliases=["i"], help="install apps from GitHub (owner/repo or URL)")
    s.add_argument("repos", nargs="+", metavar="owner/repo")
    s.add_argument("-t", "--tag", help="install this release tag (pins the app)")
    s.add_argument("--pre", action="store_true", help="allow pre-releases")
    s.add_argument("--type", "--only", dest="type",
                   choices=["flatpak", "package", "deb", "rpm", "pacman", "appimage", "archive"],
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

    s = sub.add_parser("readme", help="show a repo's README as formatted plain text")
    s.add_argument("repo", metavar="owner/repo")
    s.add_argument("--raw", action="store_true", help="show the original Markdown source")
    s.add_argument("-w", "--width", type=int, help="wrap width (default: terminal width, max 100)")
    s.add_argument("--no-pager", action="store_true", help="print everything instead of using a pager")
    s.set_defaults(fn=cmd_readme)

    s = sub.add_parser("search", help="search GitHub (default), GitLab or Codeberg")
    s.add_argument("query", nargs="+")
    s.add_argument("--on", choices=["github", "gitlab", "codeberg", "all"], default="github", help="where to search")
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

    s = sub.add_parser("self-update", help="update gitet itself to the latest version")
    s.add_argument("-c", "--check", action="store_true", help="only check if a newer version exists")
    s.add_argument("-f", "--force", action="store_true", help="reinstall even if already current")
    s.set_defaults(fn=cmd_self_update)

    s = sub.add_parser("self-uninstall", help="remove gitet (keeps your apps)")
    s.add_argument("--purge", action="store_true", help="also delete gitet's app list and cache")
    s.set_defaults(fn=cmd_self_uninstall)

    s = sub.add_parser("_auto-update")
    s.set_defaults(fn=cmd_auto_update)
    for name in ("install", "remove", "update", "list", "types", "search", "readme", "auto", "self-update"):
        sub.choices[name].add_argument("--json", action="store_true", help="machine-readable JSON on stdout")
    return p


def main() -> int:
    if not sys.platform.startswith("linux"):
        err("gitet only supports Linux")
        return 1
    if os.geteuid() == 0 and not os.environ.get("GITET_ALLOW_ROOT"):
        err("run gitet as your normal user - it calls sudo itself when a step needs root")
        return 1
    argv = sys.argv[1:]
    args = None
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
        if args is not None and getattr(args, "json", False):
            jprint({"error": str(e)})
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
