#!/usr/bin/env python3
"""
Gitet GUI - a friendly, Synaptic-style package manager for GitHub releases.

Search GitHub, install, update and remove apps, read READMEs and pick exactly
which package type to install. It is a front-end for the `gitet` command-line
tool (this file talks to `gitet ... --json`).

On first run it copies itself to ~/.local/bin/gitet-gui and adds a launcher to
your application menu. Needs PyQt6 (or PySide6) and the `gitet` command.

    gitet-gui              start the app
    gitet-gui --install    (re)install the launcher only
    gitet-gui --uninstall  remove the launcher and the gitet-gui command
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

GUI_VERSION = "1.0.0"
MIN_GITET = (1, 3, 0)
REPO_RAW = "https://raw.githubusercontent.com/AstroMeYT/Gitet/main"
GUI_URL = f"{REPO_RAW}/gitet-gui.py"
CLI_URL = f"{REPO_RAW}/gitet.py"

HOME = Path.home()
XDG_DATA = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share")
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
BIN_DIR = HOME / ".local/bin"
DATA_DIR = XDG_DATA / "gitet"
TOKEN_FILE = XDG_CONFIG / "gitet" / "token"
APPS_DIR = XDG_DATA / "applications"
ICON_DIR = XDG_DATA / "icons/hicolor/scalable/apps"
GUI_BIN = BIN_DIR / "gitet-gui"
DIRECT_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#4f9cff"/><stop offset="1" stop-color="#2b5fd9"/></linearGradient></defs>
<rect width="128" height="128" rx="28" fill="url(#g)"/>
<path d="M64 24v52M43 57l21 21 21-21" stroke="#fff" stroke-width="10" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M34 94h60" stroke="#fff" stroke-width="10" stroke-linecap="round"/></svg>
"""


# --------------------------------------------------------------------------
# helpers that don't need Qt (self-install, finding the gitet CLI)
# --------------------------------------------------------------------------
def vtuple(s: str) -> tuple:
    m = re.search(r"(\d+(?:\.\d+)*)", s or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else (0,)


def human(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def last_line(text: str) -> str:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip() and not l.startswith("@progress")]
    return lines[-1] if lines else ""


def ensure_path() -> None:
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    if str(BIN_DIR) in os.environ.get("PATH", "").split(":"):
        return
    line = '\nexport PATH="$HOME/.local/bin:$PATH"  # added by gitet\n'
    for rc in (".profile", ".bashrc", ".zshrc"):
        p = HOME / rc
        if rc != ".profile" and not p.exists():
            continue
        text = p.read_text(errors="ignore") if p.exists() else ""
        if ".local/bin" not in text:
            with open(p, "a") as f:
                f.write(line)
    fish = XDG_CONFIG / "fish"
    if fish.is_dir() and not (fish / "conf.d/gitet.fish").exists():
        (fish / "conf.d").mkdir(exist_ok=True)
        (fish / "conf.d/gitet.fish").write_text("fish_add_path -g $HOME/.local/bin\n")


def install_launcher(exe: Path) -> None:
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    (ICON_DIR / "gitet-gui.svg").write_text(ICON_SVG)
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    (APPS_DIR / "gitet-gui.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Gitet\nGenericName=GitHub Package Manager\n"
        "Comment=Install and update apps straight from GitHub releases\n"
        f'Exec="{exe}"\nIcon=gitet-gui\nTerminal=false\nCategories=System;PackageManager;Settings;\n'
        "Keywords=github;install;packages;appimage;flatpak;\nStartupWMClass=gitet-gui\n")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    helper = DATA_DIR / "askpass.sh"  # lets sudo ask for the password through a dialog
    helper.write_text(f'#!/bin/sh\nexec "{exe}" --askpass "$@"\n')
    os.chmod(helper, 0o755)
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(APPS_DIR)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def self_path() -> Path:
    return Path(os.path.realpath(__file__))


def ensure_installed() -> None:
    here = self_path()
    if GUI_BIN.exists() and GUI_BIN.resolve() == here:
        install_launcher(GUI_BIN)
        return
    try:
        newer = GUI_BIN.exists() and vtuple(re.search(r'^GUI_VERSION = "([\d.]+)"', GUI_BIN.read_text(errors="ignore"), re.M).group(1)) > vtuple(GUI_VERSION)
    except (OSError, AttributeError):
        newer = False
    if not newer:
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        tmp = GUI_BIN.with_suffix(".new")
        shutil.copyfile(here, tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, GUI_BIN)
        ensure_path()
        print(f"installed gitet-gui to {GUI_BIN}", file=sys.stderr)
    install_launcher(GUI_BIN)


def uninstall_self() -> None:
    for p in (GUI_BIN, APPS_DIR / "gitet-gui.desktop", ICON_DIR / "gitet-gui.svg", DATA_DIR / "askpass.sh"):
        p.unlink(missing_ok=True)
    print("gitet-gui removed (the gitet command and your apps were kept)", file=sys.stderr)


def find_gitet():
    cands = [os.environ.get("GITET_BIN"), shutil.which("gitet"), str(BIN_DIR / "gitet"), "/usr/local/bin/gitet",
             str(self_path().parent / "gitet.py")]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def cli_version(path: str) -> tuple:
    try:
        p = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30)
        return vtuple(p.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return (0,)


def download_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": f"gitet-gui/{GUI_VERSION}", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# Qt
# --------------------------------------------------------------------------
try:
    from PyQt6 import QtCore, QtGui, QtWidgets
    from PyQt6.QtCore import pyqtSignal as Signal
    QT = "PyQt6"
except ImportError:
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
        from PySide6.QtCore import Signal
        QT = "PySide6"
    except ImportError:
        QT = None

if QT:
    Qt = QtCore.Qt
    W = QtWidgets
    UR = Qt.ItemDataRole.UserRole


def sanitize_markdown(src: str) -> str:
    """Qt's Markdown view can't show remote images or most HTML - tidy those up."""
    def clean(t: str) -> str:
        t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
        t = re.sub(r"<img\b[^>]*?\balt\s*=\s*[\"']([^\"']*)[\"'][^>]*>",
                   lambda m: f"*[{m.group(1)}]*" if m.group(1).strip() else "", t, flags=re.S | re.I)
        t = re.sub(r"<img\b[^>]*>", "", t, flags=re.S | re.I)
        t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", lambda m: f"*[{m.group(1)}]*" if m.group(1).strip() else "", t)
        t = re.sub(r"<br\s*/?>", "  \n", t, flags=re.I)
        t = re.sub(r"<h([1-6])[^>]*>(.*?)</h\1>", lambda m: f"\n\n{'#' * int(m.group(1))} {m.group(2).strip()}\n\n",
                   t, flags=re.S | re.I)
        t = re.sub(r"<a\b[^>]*?href=[\"']([^\"']*)[\"'][^>]*>(.*?)</a>", r"[\2](\1)", t, flags=re.S | re.I)
        return re.sub(r"</?(?:p|div|center|details|summary|section|picture|source|span|table|thead|tbody|tr|td|th|"
                      r"sub|sup|kbd|font|b|i|em|strong|ul|ol|li|hr)\b[^>]*>", "\n", t, flags=re.I)

    def clean_inline(chunk: str) -> str:  # leave `inline code` alone
        parts = re.split(r"(`[^`\n]*`)", chunk)
        return "".join(p if i % 2 else clean(p) for i, p in enumerate(parts))

    chunks = re.split(r"(^\s*(?:`{3,}|~{3,}).*?^\s*(?:`{3,}|~{3,})\s*$)", src.replace("\r\n", "\n"), flags=re.M | re.S)
    return "".join(c if i % 2 else clean_inline(c) for i, c in enumerate(chunks))


if QT:
    class Job(QtCore.QObject):
        """One `gitet ...` process. Streams stderr lines and '@progress N' updates."""
        done = Signal(int, str, str)
        line = Signal(str)
        progress = Signal(int)

        def __init__(self, cli: str, args: list, parent=None):
            super().__init__(parent)
            self.cli, self.args = cli, args
            self.out = bytearray()
            self.err_lines: list = []
            self._buf = ""
            self._finished = False
            self.proc = QtCore.QProcess(self)
            env = QtCore.QProcessEnvironment.systemEnvironment()
            env.insert("GITET_PROGRESS", "1")
            env.insert("GITET_NO_SELF_INSTALL", "1")
            env.insert("PATH", f"{BIN_DIR}:{env.value('PATH')}")
            askpass = DATA_DIR / "askpass.sh"
            if askpass.exists():
                env.insert("SUDO_ASKPASS", str(askpass))
            self.proc.setProcessEnvironment(env)
            self.proc.readyReadStandardOutput.connect(lambda: self.out.extend(bytes(self.proc.readAllStandardOutput())))
            self.proc.readyReadStandardError.connect(self._on_err)
            self.proc.finished.connect(self._on_finished)
            self.proc.errorOccurred.connect(self._on_error)

        def start(self):
            self.proc.start(self.cli, self.args)

        def _emit_line(self, l: str):
            if l.startswith("@progress "):
                try:
                    self.progress.emit(int(l.split()[1]))
                except ValueError:
                    pass
            elif l.strip():
                self.err_lines.append(l)
                self.line.emit(l)

        def _on_err(self):
            self._buf += bytes(self.proc.readAllStandardError()).decode("utf-8", "replace").replace("\r", "\n")
            *lines, self._buf = self._buf.split("\n")
            for l in lines:
                self._emit_line(l)

        def _on_finished(self, code, _status):
            self._on_err()
            if self._buf:
                self._emit_line(self._buf)
                self._buf = ""
            self.out.extend(bytes(self.proc.readAllStandardOutput()))
            if not self._finished:
                self._finished = True
                self.done.emit(int(code), self.out.decode("utf-8", "replace"), "\n".join(self.err_lines[-30:]))

        def _on_error(self, err):
            if err == QtCore.QProcess.ProcessError.FailedToStart and not self._finished:
                self._finished = True
                self.done.emit(-1, "", f"couldn't start {self.cli}")

        def kill(self):
            self._finished = True
            self.proc.kill()


    def icon(theme: str, fallback) -> "QtGui.QIcon":
        ic = QtGui.QIcon.fromTheme(theme)
        if ic.isNull():
            ic = QtWidgets.QApplication.style().standardIcon(fallback)
        return ic


    SP = W.QStyle.StandardPixmap


    class TokenDialog(W.QDialog):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle("GitHub token")
            self.removed = False
            lay = W.QVBoxLayout(self)
            lay.addWidget(W.QLabel(
                "A personal access token raises GitHub's rate limit (searching and update checks)\n"
                "and is stored only in ~/.config/gitet/token. It needs no special permissions."))
            self.edit = W.QLineEdit()
            self.edit.setEchoMode(W.QLineEdit.EchoMode.Password)
            self.edit.setPlaceholderText("ghp_...  (saved)" if TOKEN_FILE.exists() else "ghp_...")
            lay.addWidget(self.edit)
            link = W.QLabel('<a href="https://github.com/settings/tokens">Create a token on GitHub</a>')
            link.setOpenExternalLinks(True)
            lay.addWidget(link)
            row = W.QHBoxLayout()
            save, rm, cancel = W.QPushButton("Save"), W.QPushButton("Remove token"), W.QPushButton("Cancel")
            rm.setEnabled(TOKEN_FILE.exists())
            save.clicked.connect(self.accept)
            cancel.clicked.connect(self.reject)
            rm.clicked.connect(self._remove)
            for b in (rm, cancel, save):
                row.addWidget(b)
            lay.addLayout(row)
            save.setDefault(True)

        def _remove(self):
            self.removed = True
            self.accept()


    class FetchThread(QtCore.QThread):
        result = Signal(str, str)  # text, error

        def __init__(self, url, parent=None):
            super().__init__(parent)
            self.url = url

        def run(self):
            try:
                self.result.emit(download_text(self.url), "")
            except (urllib.error.URLError, OSError) as e:
                self.result.emit("", str(getattr(e, "reason", e)))


    class MainWindow(W.QMainWindow):
        COLS = ["", "Package", "Installed", "Latest", "Type", "★", "Description"]

        def __init__(self, cli: str):
            super().__init__()
            self.cli = cli
            self.setWindowTitle("Gitet")
            self.setWindowIcon(QtGui.QIcon(str(ICON_DIR / "gitet-gui.svg")))
            self.resize(1200, 780)
            self.installed: dict = {}
            self.updates: dict = {}
            self.latest: dict = {}
            self.results: list = []
            self.desc: dict = {}
            self.stars: dict = {}
            self.view = "installed"
            self.queue: deque = deque()
            self.task = None
            self.jobs: set = set()
            self.readme_cache: dict = {}
            self.types_cache: dict = {}
            self.detail_repo = None
            self.types_data = None
            self.failures: list = []
            self.checking = False
            self.empty_text = ""
            self.auto_on = False
            self.auto_hours = 6
            self.gitet_version = ".".join(map(str, cli_version(cli)))
            self._build_ui()
            self._build_menus()
            self.sel_timer = QtCore.QTimer(self, singleShot=True, interval=220)
            self.sel_timer.timeout.connect(self.on_selection_settled)
            self.set_view("installed")
            QtCore.QTimer.singleShot(0, self.refresh)

        # ------------------------------------------------------------ UI
        def _build_ui(self):
            tb = self.addToolBar("Main")
            tb.setMovable(False)
            tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            tb.setIconSize(QtCore.QSize(20, 20))
            self.search = W.QLineEdit()
            self.search.setClearButtonEnabled(True)
            self.search.setMinimumWidth(380)
            self.search.setPlaceholderText("Search GitHub (press Enter) - or paste owner/repo. Filters the list you're in as you type.")
            self.search.returnPressed.connect(self.do_search)
            self.search.textChanged.connect(self.on_filter_changed)
            tb.addWidget(self.search)
            tb.addSeparator()
            self.act_refresh = self._action("Refresh", icon("view-refresh", SP.SP_BrowserReload), self.refresh, "Ctrl+R",
                                            "Reload installed apps and check for updates")
            self.act_update_all = self._action("Update all", icon("system-software-update", SP.SP_ArrowUp),
                                               self.update_all, "Ctrl+U", "Update every app that has a newer release")
            self.act_auto = self._action("Auto-updates", icon("preferences-system-time", SP.SP_BrowserReload),
                                         self.toggle_auto, None, "Check for updates in the background", checkable=True)
            tb.addAction(self.act_refresh)
            tb.addAction(self.act_update_all)
            tb.addSeparator()
            tb.addAction(self.act_auto)

            split = W.QSplitter(Qt.Orientation.Horizontal)
            self.setCentralWidget(split)

            self.side = W.QListWidget()
            self.side.setMinimumWidth(150)
            self.side.setMaximumWidth(280)
            self.side.setIconSize(QtCore.QSize(18, 18))
            self.side.setSpacing(2)
            for key, text, ic in (("installed", "Installed", icon("drive-harddisk", SP.SP_DriveHDIcon)),
                                  ("updates", "Updates", icon("software-update-available", SP.SP_ArrowUp)),
                                  ("search", "Search results", icon("edit-find", SP.SP_FileDialogContentsView))):
                it = W.QListWidgetItem(ic, text)
                it.setData(UR, key)
                self.side.addItem(it)
            self.side.currentRowChanged.connect(self.on_side_click)
            split.addWidget(self.side)

            right = W.QSplitter(Qt.Orientation.Vertical)
            split.addWidget(right)
            split.setStretchFactor(0, 0)
            split.setStretchFactor(1, 1)
            split.setSizes([200, 1000])

            # package list
            top = W.QWidget()
            tl = W.QVBoxLayout(top)
            tl.setContentsMargins(0, 4, 0, 0)
            head = W.QHBoxLayout()
            self.list_title = W.QLabel()
            f = self.list_title.font()
            f.setBold(True)
            f.setPointSize(f.pointSize() + 2)
            self.list_title.setFont(f)
            head.addWidget(self.list_title)
            head.addStretch(1)
            self.type_filter = W.QComboBox()
            self.type_filter.addItem("All types", "")
            for k, label in (("flatpak", "Flatpak"), ("deb", "DEB"), ("rpm", "RPM"), ("pacman", "pacman pkg"),
                             ("appimage", "AppImage"), ("archive", "Archive")):
                self.type_filter.addItem(label, k)
            self.type_filter.currentIndexChanged.connect(lambda _: self.populate())
            head.addWidget(self.type_filter)
            tl.addLayout(head)
            self.stack = W.QStackedWidget()
            self.table = W.QTableWidget(0, len(self.COLS))
            self.table.setHorizontalHeaderLabels(self.COLS)
            self.table.setSelectionBehavior(W.QAbstractItemView.SelectionBehavior.SelectRows)
            self.table.setSelectionMode(W.QAbstractItemView.SelectionMode.ExtendedSelection)
            self.table.setEditTriggers(W.QAbstractItemView.EditTrigger.NoEditTriggers)
            self.table.setAlternatingRowColors(True)
            self.table.setShowGrid(False)
            self.table.verticalHeader().setVisible(False)
            self.table.setSortingEnabled(True)
            self.table.horizontalHeader().setSortIndicator(0, Qt.SortOrder.AscendingOrder)
            self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.table.customContextMenuRequested.connect(self.context_menu)
            hh = self.table.horizontalHeader()
            hh.setStretchLastSection(True)
            for c, w in ((0, 28), (1, 250), (2, 95), (3, 95), (4, 90), (5, 60)):
                hh.setSectionResizeMode(c, W.QHeaderView.ResizeMode.Interactive)
                self.table.setColumnWidth(c, w)
            self.table.itemSelectionChanged.connect(self.on_selection)
            self.empty = W.QLabel()
            self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.empty.setWordWrap(True)
            self.empty.setStyleSheet("color: gray; font-size: 14px; padding: 40px;")
            self.stack.addWidget(self.table)
            self.stack.addWidget(self.empty)
            tl.addWidget(self.stack)
            right.addWidget(top)

            # detail panel
            det = W.QWidget()
            dl = W.QVBoxLayout(det)
            dl.setContentsMargins(6, 6, 6, 0)
            self.title = W.QLabel("Select a package")
            tf = self.title.font()
            tf.setBold(True)
            tf.setPointSize(tf.pointSize() + 4)
            self.title.setFont(tf)
            self.subtitle = W.QLabel()
            self.subtitle.setWordWrap(True)
            self.info = W.QLabel()
            self.info.setStyleSheet("color: gray;")
            dl.addWidget(self.title)
            dl.addWidget(self.subtitle)
            dl.addWidget(self.info)
            brow = W.QHBoxLayout()
            self.btn_install = W.QPushButton(icon("list-add", SP.SP_DialogApplyButton), "Install")
            self.btn_update = W.QPushButton(icon("system-software-update", SP.SP_ArrowUp), "Update")
            self.btn_remove = W.QPushButton(icon("edit-delete", SP.SP_TrashIcon), "Remove")
            self.btn_launch = W.QPushButton(icon("media-playback-start", SP.SP_MediaPlay), "Launch")
            self.btn_github = W.QPushButton(icon("internet-web-browser", SP.SP_DirLinkIcon), "GitHub")
            self.btn_install.setStyleSheet("QPushButton { background:#2f7cf6; color:white; font-weight:bold; padding:6px 16px; border-radius:4px; }"
                                           "QPushButton:hover { background:#4a8ff8; }")
            self.btn_remove.setStyleSheet("QPushButton { padding:6px 14px; } QPushButton:hover { color:#d33; }")
            for b in (self.btn_install, self.btn_update, self.btn_remove, self.btn_launch, self.btn_github):
                b.setVisible(False)
                brow.addWidget(b)
            brow.addStretch(1)
            dl.addLayout(brow)
            self.btn_install.clicked.connect(self.act_install)
            self.btn_update.clicked.connect(self.act_update)
            self.btn_remove.clicked.connect(self.act_remove)
            self.btn_launch.clicked.connect(self.act_launch)
            self.btn_github.clicked.connect(self.act_github)

            self.tabs = W.QTabWidget()
            # README
            rw = W.QWidget()
            rl = W.QVBoxLayout(rw)
            rl.setContentsMargins(0, 4, 0, 0)
            rbar = W.QHBoxLayout()
            self.chk_plain = W.QCheckBox("Plain text view")
            self.chk_plain.toggled.connect(lambda _: self.render_readme())
            rbar.addWidget(self.chk_plain)
            rbar.addStretch(1)
            rl.addLayout(rbar)
            self.readme = W.QTextBrowser()
            self.readme.setOpenExternalLinks(True)
            rl.addWidget(self.readme)
            self.tabs.addTab(rw, "README")
            # Packages
            pw = W.QWidget()
            pl = W.QVBoxLayout(pw)
            pl.setContentsMargins(0, 4, 0, 0)
            self.pkg_info = W.QLabel("Select a package to see what it offers for your system.")
            self.pkg_info.setWordWrap(True)
            pl.addWidget(self.pkg_info)
            self.tree = W.QTreeWidget()
            self.tree.setHeaderLabels(["Download", "Size", ""])
            self.tree.setColumnWidth(0, 520)
            self.tree.itemSelectionChanged.connect(self.update_pkg_buttons)
            self.tree.itemDoubleClicked.connect(lambda *_: self.install_selected_file())
            pl.addWidget(self.tree)
            prow = W.QHBoxLayout()
            self.chk_pin = W.QCheckBox("Pin this version (don't auto-update)")
            self.btn_best = W.QPushButton("Install recommended")
            self.btn_file = W.QPushButton("Install selected file")
            self.btn_best.clicked.connect(self.install_recommended)
            self.btn_file.clicked.connect(self.install_selected_file)
            prow.addWidget(self.chk_pin)
            prow.addStretch(1)
            prow.addWidget(self.btn_best)
            prow.addWidget(self.btn_file)
            pl.addLayout(prow)
            self.tabs.addTab(pw, "Packages")
            # Activity
            lw = W.QWidget()
            ll = W.QVBoxLayout(lw)
            ll.setContentsMargins(0, 4, 0, 0)
            self.log = W.QPlainTextEdit()
            self.log.setReadOnly(True)
            self.log.setMaximumBlockCount(5000)
            self.log.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
            ll.addWidget(self.log)
            clr = W.QPushButton("Clear")
            clr.clicked.connect(self.log.clear)
            ll.addWidget(clr, alignment=Qt.AlignmentFlag.AlignRight)
            self.log_tab = lw
            self.tabs.addTab(lw, "Activity")
            dl.addWidget(self.tabs, 1)
            right.addWidget(det)
            right.setStretchFactor(0, 3)
            right.setStretchFactor(1, 4)

            self.status_label = W.QLabel("Ready")
            self.progress = W.QProgressBar()
            self.progress.setMaximumWidth(180)
            self.progress.setTextVisible(False)
            self.progress.setVisible(False)
            self.statusBar().addWidget(self.status_label, 1)
            self.statusBar().addPermanentWidget(self.progress)
            self.update_pkg_buttons()

            s = QtCore.QSettings("gitet", "gitet-gui")
            if s.contains("geometry"):
                self.restoreGeometry(s.value("geometry"))
            self._splits = (split, right)
            for i, sp in enumerate(self._splits):
                if s.contains(f"split{i}"):
                    sp.restoreState(s.value(f"split{i}"))

        def _action(self, text, ic, slot, shortcut=None, tip="", checkable=False):
            a = QtGui.QAction(ic, text, self)
            a.setCheckable(checkable)
            if shortcut:
                a.setShortcut(QtGui.QKeySequence(shortcut))
            if tip:
                a.setToolTip(tip)
                a.setStatusTip(tip)
            if checkable:
                a.triggered.connect(slot)
            else:
                a.triggered.connect(lambda _=False: slot())
            return a

        def _build_menus(self):
            mb = self.menuBar()
            m = mb.addMenu("&File")
            m.addAction(self._action("Export app list…", icon("document-save", SP.SP_DialogSaveButton), self.export_list))
            m.addAction(self._action("Import app list…", icon("document-open", SP.SP_DialogOpenButton), self.import_list))
            m.addSeparator()
            m.addAction(self._action("Quit", icon("application-exit", SP.SP_DialogCloseButton), self.close, "Ctrl+Q"))
            m = mb.addMenu("&Packages")
            m.addAction(self._action("Search GitHub", icon("edit-find", SP.SP_FileDialogContentsView),
                                     lambda: (self.search.setFocus(), self.search.selectAll()), "Ctrl+F"))
            m.addAction(self._action("Install selected", icon("list-add", SP.SP_DialogApplyButton), self.act_install, "Ctrl+I"))
            m.addAction(self._action("Update selected", icon("system-software-update", SP.SP_ArrowUp), self.act_update))
            m.addAction(self._action("Remove selected", icon("edit-delete", SP.SP_TrashIcon), self.act_remove, "Del"))
            m.addAction(self._action("Reinstall selected", icon("view-refresh", SP.SP_BrowserReload), self.act_reinstall))
            m.addSeparator()
            m.addAction(self.act_update_all)
            m.addAction(self.act_refresh)
            m = mb.addMenu("&Settings")
            m.addAction(self.act_auto)
            m.addAction(self._action("GitHub token…", icon("dialog-password", SP.SP_DialogHelpButton), self.edit_token))
            m = mb.addMenu("&Help")
            m.addAction(self._action("Check for Gitet updates…", icon("system-software-update", SP.SP_ArrowUp), self.check_gitet_update))
            m.addAction(self._action("Update Gitet GUI…", icon("system-software-update", SP.SP_ArrowUp), self.check_gui_update))
            m.addSeparator()
            m.addAction(self._action("About", icon("help-about", SP.SP_MessageBoxInformation), self.about))

        # ------------------------------------------------------------ CLI plumbing
        def query(self, args: list, cb):
            """Run a read-only gitet command with --json; cb(data, error_message)."""
            args = args if "--json" in args else args + ["--json"]
            job = Job(self.cli, args, self)
            self.jobs.add(job)

            def done(code, out, err):
                self.jobs.discard(job)
                job.deleteLater()
                data, msg = None, ""
                try:
                    data = json.loads(out) if out.strip() else None
                except ValueError:
                    data = None
                if isinstance(data, dict) and data.get("error"):
                    msg, data = data["error"], None
                elif data is None:
                    msg = last_line(err) or f"gitet exited with code {code}"
                cb(data, msg)

            job.done.connect(done)
            job.start()

        def enqueue(self, title: str, args: list):
            self.queue.append((title, args))
            self.tabs.setCurrentWidget(self.log_tab)
            self._pump()

        def _pump(self):
            if self.task or not self.queue:
                return
            title, args = self.queue.popleft()
            job = Job(self.cli, args, self)
            self.task = job
            self.set_status(title + "…" + (f"  ({len(self.queue)} more queued)" if self.queue else ""))
            self.progress.setVisible(True)
            self.progress.setRange(0, 0)
            self.log_line(f"▶ {title}    [gitet {' '.join(args)}]")
            job.line.connect(self.log_line)
            job.progress.connect(self._on_progress)
            job.done.connect(lambda code, out, err: self._task_done(job, title, code, out, err))
            job.start()

        def _on_progress(self, n: int):
            self.progress.setRange(0, 100)
            self.progress.setValue(n)

        def _task_done(self, job, title, code, out, err):
            job.deleteLater()
            self.task = None
            data = None
            try:
                data = json.loads(out) if out.strip() else None
            except ValueError:
                pass
            fails = []
            if isinstance(data, dict):
                fails += [f"{r.get('repo')}: {r.get('error', 'failed')}" for r in data.get("results", []) if not r.get("ok")]
                fails += [f"{f['repo']}: {f['error']}" for f in data.get("failed", [])]
                fails += [f"{p}: needs administrator rights - skipped" for p in data.get("pending", [])]
                if data.get("error"):
                    fails.append(data["error"])
            if code != 0 and not fails:
                fails.append(f"{title}: {last_line(err) or 'failed (exit code %d)' % code}")
            self.log_line(("✖ " if fails else "✔ ") + title + (" - failed" if fails else " - done"))
            self.failures += fails
            self.readme_cache.clear()
            self.types_cache.clear()
            if self.queue:
                self._pump()
                return
            self.progress.setVisible(False)
            self.set_status("Finished with errors" if self.failures else "Done")
            self.refresh()
            if self.failures:
                W.QMessageBox.warning(self, "Something went wrong", "\n".join(self.failures[:12]))
                self.failures = []

        def log_line(self, text: str):
            self.log.appendPlainText(text)

        def set_status(self, text: str):
            self.status_label.setText(text)

        # ------------------------------------------------------------ data
        def refresh(self):
            self.query(["list"], self._got_installed)
            self.query(["auto", "status"], self._got_auto)
            self.check_updates()

        def _got_installed(self, data, msg):
            if data is None:
                self.set_status(f"Couldn't read installed apps: {msg}")
                return
            self.installed = {a["repo"]: a for a in data["apps"]}
            for r, a in self.installed.items():
                if a.get("description"):
                    self.desc.setdefault(r, a["description"])
            self.populate()
            self.update_buttons()

        def check_updates(self):
            if self.checking:
                return
            self.checking = True
            self.set_status("Checking for updates…")
            self.side_counts()
            self.query(["update", "--check"], self._got_updates)

        def _got_updates(self, data, msg):
            self.checking = False
            if data is None:
                self.set_status(f"Update check failed: {msg}")
                self.side_counts()
                return
            self.updates = {a["repo"]: a for a in data.get("available", [])}
            self.latest = {c["repo"]: c["tag"] for c in data.get("current", [])}
            self.latest.update({r: a["latest"] for r, a in self.updates.items()})
            n = len(self.updates)
            self.set_status(f"{n} update{'s' if n != 1 else ''} available" if n else "Everything is up to date")
            self.populate()
            self.update_buttons()

        def _got_auto(self, data, msg):
            if data:
                self.auto_on, self.auto_hours = bool(data["auto"]), int(data.get("every_hours", 6))
                self.act_auto.blockSignals(True)
                self.act_auto.setChecked(self.auto_on)
                self.act_auto.blockSignals(False)

        # ------------------------------------------------------------ list view
        def set_view(self, key: str):
            self.view = key
            for i in range(self.side.count()):
                if self.side.item(i).data(UR) == key and self.side.currentRow() != i:
                    self.side.blockSignals(True)
                    self.side.setCurrentRow(i)
                    self.side.blockSignals(False)
            self.type_filter.setVisible(key != "search")
            self.populate()

        def on_side_click(self, row: int):
            if row < 0:
                return
            key = self.side.item(row).data(UR)
            if key != "search" and self.search.text():
                self.search.blockSignals(True)
                self.search.clear()
                self.search.blockSignals(False)
            self.set_view(key)

        def side_counts(self):
            counts = {"installed": len(self.installed), "updates": len(self.updates), "search": len(self.results)}
            names = {"installed": "Installed", "updates": "Updates", "search": "Search results"}
            for i in range(self.side.count()):
                it = self.side.item(i)
                k = it.data(UR)
                label = f"{names[k]} ({counts[k]})" if (counts[k] or k == "installed") else names[k]
                if k == "updates" and self.checking:
                    label = "Updates (checking…)"
                it.setText(label)
                f = it.font()
                f.setBold(k == "updates" and counts[k] > 0)
                it.setFont(f)
            self.act_update_all.setText(f"Update all ({len(self.updates)})" if self.updates else "Update all")
            self.act_update_all.setEnabled(bool(self.updates))

        def rows_for_view(self) -> list:
            q = self.search.text().strip().lower() if self.view != "search" else ""
            want = self.type_filter.currentData() if self.view != "search" else ""
            rows = []
            if self.view == "search":
                repos = [r["repo"] for r in self.results]
            elif self.view == "updates":
                repos = sorted(self.updates, key=str.lower)
            else:
                repos = sorted(self.installed, key=str.lower)
            for repo in repos:
                inst = self.installed.get(repo)
                if want and (not inst or inst["method"] != want):
                    continue
                d = self.desc.get(repo, "")
                if q and q not in repo.lower() and q not in d.lower():
                    continue
                rows.append({"repo": repo, "inst": inst, "desc": d, "stars": self.stars.get(repo),
                             "latest": self.updates[repo]["latest"] if repo in self.updates else self.latest.get(repo, "")})
            return rows

        def populate(self):
            keep = set(self.selected_repos())
            rows = self.rows_for_view()
            titles = {"installed": "Installed packages", "updates": "Available updates", "search": "Search results"}
            self.list_title.setText(titles[self.view])
            self.table.blockSignals(True)
            self.table.setSortingEnabled(False)
            self.table.setRowCount(len(rows))
            for r, row in enumerate(rows):
                inst, repo = row["inst"], row["repo"]
                has_upd = repo in self.updates
                it0 = W.QTableWidgetItem()
                if has_upd:
                    it0.setIcon(icon("software-update-available", SP.SP_ArrowUp))
                    it0.setToolTip("Update available")
                elif inst:
                    it0.setIcon(icon("emblem-ok-symbolic", SP.SP_DialogApplyButton))
                    it0.setToolTip("Installed")
                it0.setData(Qt.ItemDataRole.DisplayRole, "0" if has_upd else "1" if inst else "2")
                it0.setForeground(QtGui.QBrush(QtGui.QColor(0, 0, 0, 0)))  # sort key only; the icon carries the meaning
                name = W.QTableWidgetItem(repo)
                name.setData(UR, repo)
                if has_upd:
                    f = name.font()
                    f.setBold(True)
                    name.setFont(f)
                cells = [it0, name, W.QTableWidgetItem(inst["tag"] if inst else ""),
                         W.QTableWidgetItem(row["latest"]), W.QTableWidgetItem(inst["label"] if inst else ""),
                         W.QTableWidgetItem(), W.QTableWidgetItem(row["desc"])]
                if row["stars"] is not None:
                    cells[5].setData(Qt.ItemDataRole.DisplayRole, int(row["stars"]))
                for c, item in enumerate(cells):
                    self.table.setItem(r, c, item)
            self.table.setSortingEnabled(True)
            sel = self.table.selectionModel()
            for r in range(self.table.rowCount()):
                if self.table.item(r, 1).data(UR) in keep:
                    sel.select(self.table.model().index(r, 0),
                               QtCore.QItemSelectionModel.SelectionFlag.Select | QtCore.QItemSelectionModel.SelectionFlag.Rows)
            self.table.blockSignals(False)
            self.side_counts()
            if rows:
                self.stack.setCurrentWidget(self.table)
            else:
                self.stack.setCurrentWidget(self.empty)
                self.empty.setText(self.empty_message())
            self.on_selection()

        def empty_message(self) -> str:
            if self.view == "installed":
                if self.installed:
                    return "No installed packages match your filter."
                return "Nothing installed yet.\n\nType a name in the search box and press Enter,\nor paste an owner/repo like  dpaulat/SupercellWX"
            if self.view == "updates":
                return "Checking for updates…" if self.checking else "Everything is up to date ✔"
            return self.empty_text or "Type a name above and press Enter to search GitHub.\nYou can also paste owner/repo or a GitHub URL."

        def on_filter_changed(self, _text):
            if self.view != "search":
                self.populate()

        def selected_repos(self) -> list:
            rows = sorted({i.row() for i in self.table.selectedItems()})
            return [self.table.item(r, 1).data(UR) for r in rows if self.table.item(r, 1)]

        # ------------------------------------------------------------ search
        def do_search(self):
            q = self.search.text().strip()
            if not q:
                return
            m = DIRECT_RE.match(q)
            if m and ("/" in q) and " " not in q:
                repo = f"{m.group(1)}/{m.group(2)}"
                self.results = [{"repo": repo, "description": "", "stars": None}]
                self.empty_text = ""
                self.set_view("search")
                if self.table.rowCount():
                    self.table.selectRow(0)
                return
            self.results = []
            self.empty_text = f"Searching GitHub for “{q}”…"
            self.set_view("search")
            self.set_status(f"Searching for {q}…")
            self.query(["search", q], lambda d, msg, q=q: self._got_search(q, d, msg))

        def _got_search(self, q, data, msg):
            if data is None:
                self.empty_text = f"Search failed: {msg}"
                self.set_status("Search failed")
            else:
                self.results = data["results"]
                for r in self.results:
                    self.desc[r["repo"]] = r.get("description") or ""
                    self.stars[r["repo"]] = r.get("stars", 0)
                self.empty_text = f"No GitHub repositories found for “{q}”."
                self.set_status(f"{len(self.results)} result(s) for “{q}”")
            if self.view == "search":
                self.populate()
                if self.table.rowCount():
                    self.table.selectRow(0)

        # ------------------------------------------------------------ details
        def on_selection(self):
            self.update_buttons()
            self.sel_timer.start()

        def on_selection_settled(self):
            repos = self.selected_repos()
            if len(repos) == 1:
                self.show_detail(repos[0])
            elif repos:
                self.detail_repo = None
                self.title.setText(f"{len(repos)} packages selected")
                self.subtitle.setText(", ".join(repos[:6]) + ("…" if len(repos) > 6 else ""))
                self.info.setText("")
            else:
                self.detail_repo = None
                self.title.setText("Select a package")
                self.subtitle.setText("")
                self.info.setText("")
                self.readme.clear()
                self.tree.clear()
                self.pkg_info.setText("Select a package to see what it offers for your system.")
                self.types_data = None
                self.update_pkg_buttons()

        def show_detail(self, repo: str):
            self.detail_repo = repo
            self.title.setText(repo)
            self.subtitle.setText(self.desc.get(repo, ""))
            self.refresh_info(repo)
            if repo in self.readme_cache:
                self.render_readme()
            else:
                self.readme.setPlainText("Loading README…")
                self.query(["readme", repo], lambda d, m, r=repo: self._got_readme(r, d, m))
            if repo in self.types_cache:
                self.fill_types(repo)
            else:
                self.tree.clear()
                self.types_data = None
                self.pkg_info.setText("Looking at the latest release…")
                self.update_pkg_buttons()
                self.query(["types", repo], lambda d, m, r=repo: self._got_types(r, d, m))

        def refresh_info(self, repo: str):
            inst = self.installed.get(repo)
            bits = []
            if inst:
                bits.append(f"Installed {inst['tag']} as {inst['label']}" + (" · pinned" if inst.get("pinned") else ""))
                if repo in self.updates:
                    bits.append(f"update available: {self.updates[repo]['latest']}")
            else:
                bits.append("Not installed")
            if self.stars.get(repo):
                bits.append(f"★ {self.stars[repo]:,}")
            self.info.setText("  ·  ".join(bits))

        def _got_readme(self, repo, data, msg):
            self.readme_cache[repo] = data if data else {"error": msg}
            if repo == self.detail_repo:
                self.render_readme()

        def render_readme(self):
            d = self.readme_cache.get(self.detail_repo)
            if d is None:
                return
            if "error" in d:
                self.readme.setPlainText(d["error"])
                return
            self.readme.document().setBaseUrl(QtCore.QUrl(f"https://github.com/{self.detail_repo}/blob/HEAD/"))
            if self.chk_plain.isChecked() or not d.get("markdown"):
                self.readme.setPlainText(d["text"])
            else:
                self.readme.setMarkdown(sanitize_markdown(d["source"]))

        def _got_types(self, repo, data, msg):
            self.types_cache[repo] = data if data else {"error": msg}
            if data:
                self.desc[repo] = data.get("description") or self.desc.get(repo, "")
                self.stars[repo] = data.get("stars", self.stars.get(repo))
            if repo == self.detail_repo:
                self.fill_types(repo)
                self.subtitle.setText(self.desc.get(repo, ""))
                self.refresh_info(repo)
                self.populate_descriptions_only()

        def populate_descriptions_only(self):
            for r in range(self.table.rowCount()):
                repo = self.table.item(r, 1).data(UR)
                if self.desc.get(repo) and not self.table.item(r, 6).text():
                    self.table.item(r, 6).setText(self.desc[repo])

        def fill_types(self, repo: str):
            d = self.types_cache.get(repo) or {}
            self.tree.clear()
            self.types_data = None
            if "error" in d:
                self.pkg_info.setText(d["error"])
                self.update_pkg_buttons()
                return
            self.types_data = d
            sysinfo = d["system"]
            head = f"Latest release {d['tag']}  ·  your system: {sysinfo['arch']}, {sysinfo['os']}"
            if not d["types"]:
                head += "\nNo Flatpak, package, AppImage or archive in this release fits your system."
            elif d.get("auto_pick"):
                head += f"\nGitet would install: {d['auto_pick']['asset']}"
            self.pkg_info.setText(head)
            for t in d["types"]:
                top = W.QTreeWidgetItem([t["label"], "", "" if t["usable"] else f"can't install here: {t['reason']}"])
                top.setData(0, UR, (t["kind"], None))
                f = top.font(0)
                f.setBold(True)
                top.setFont(0, f)
                self.tree.addTopLevelItem(top)
                for a in t["assets"]:
                    child = W.QTreeWidgetItem([a["name"], human(a["size"]), "★ recommended" if a["auto_pick"] else ""])
                    child.setData(0, UR, (t["kind"], a["name"]))
                    top.addChild(child)
                    if not t["usable"]:
                        child.setFlags(child.flags() & ~Qt.ItemFlag.ItemIsSelectable & ~Qt.ItemFlag.ItemIsEnabled)
                if not t["usable"]:
                    top.setFlags(top.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                top.setExpanded(True)
            if d["hidden"]:
                other = W.QTreeWidgetItem([f"Not compatible ({len(d['hidden'])})", "", ""])
                other.setFlags(other.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                for h in d["hidden"]:
                    c = W.QTreeWidgetItem([h["name"], "", h["reason"]])
                    c.setFlags(c.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                    other.addChild(c)
                self.tree.addTopLevelItem(other)
            self.update_pkg_buttons()

        def update_pkg_buttons(self):
            d = self.types_data
            self.btn_best.setEnabled(bool(d and d.get("auto_pick")) and self.detail_repo is not None)
            sel = self.tree.selectedItems()
            payload = sel[0].data(0, UR) if sel else None
            self.btn_best.setText("Reinstall recommended" if self.detail_repo in self.installed else "Install recommended")
            self.btn_file.setEnabled(bool(payload) and self.detail_repo is not None)
            self.btn_file.setText("Install selected type" if payload and payload[1] is None else "Install selected file")

        def install_recommended(self):
            repo = self.detail_repo
            if not repo:
                return
            args = ["install", repo, "--json", "-y"] + (["--force"] if repo in self.installed else [])
            if self.chk_pin.isChecked() and self.types_data:
                args += ["--tag", self.types_data["tag"]]
            self.enqueue(("Reinstall " if repo in self.installed else "Install ") + repo, args)

        def install_selected_file(self):
            sel = self.tree.selectedItems()
            payload = sel[0].data(0, UR) if sel else None
            if not payload or not self.detail_repo:
                return
            kind, asset = payload
            args = ["install", self.detail_repo, "--type", kind, "--json", "-y", "--force"]
            if asset:
                args += ["--asset", asset]
            if self.chk_pin.isChecked() and self.types_data:
                args += ["--tag", self.types_data["tag"]]
            self.enqueue(f"Install {self.detail_repo} ({kind}{': ' + asset if asset else ''})", args)

        # ------------------------------------------------------------ actions
        def update_buttons(self):
            repos = self.selected_repos()
            inst = [r for r in repos if r in self.installed]
            notinst = [r for r in repos if r not in self.installed]
            upd = [r for r in inst if r in self.updates]
            n = lambda lst, word: word if len(lst) <= 1 else f"{word} {len(lst)}"
            self.btn_install.setVisible(bool(notinst))
            self.btn_install.setText(n(notinst, "Install"))
            self.btn_update.setVisible(bool(upd))
            self.btn_update.setText(n(upd, "Update"))
            self.btn_remove.setVisible(bool(inst))
            self.btn_remove.setText(n(inst, "Remove"))
            one = repos[0] if len(repos) == 1 else None
            self.btn_launch.setVisible(bool(one and self.installed.get(one, {}).get("link")))
            self.btn_github.setVisible(bool(one))
            self.update_pkg_buttons()

        def act_install(self):
            repos = [r for r in self.selected_repos() if r not in self.installed] or (
                [self.detail_repo] if self.detail_repo and self.detail_repo not in self.installed else [])
            if repos:
                self.enqueue("Install " + (repos[0] if len(repos) == 1 else f"{len(repos)} packages"),
                             ["install", *repos, "--json", "-y"])

        def act_reinstall(self):
            repos = [r for r in self.selected_repos() if r in self.installed]
            if repos:
                self.enqueue("Reinstall " + (repos[0] if len(repos) == 1 else f"{len(repos)} packages"),
                             ["install", *repos, "--force", "--json", "-y"])

        def act_update(self):
            repos = [r for r in self.selected_repos() if r in self.updates]
            if repos:
                self.enqueue("Update " + (repos[0] if len(repos) == 1 else f"{len(repos)} packages"),
                             ["update", *repos, "--json"])

        def update_all(self):
            if self.updates:
                self.enqueue(f"Update {len(self.updates)} package(s)", ["update", "--json"])

        def act_remove(self):
            repos = [r for r in self.selected_repos() if r in self.installed]
            if not repos:
                return
            box = W.QMessageBox(self)
            box.setIcon(W.QMessageBox.Icon.Question)
            box.setWindowTitle("Remove packages")
            box.setText(f"Remove {repos[0]}?" if len(repos) == 1 else f"Remove {len(repos)} packages?")
            box.setInformativeText("\n".join(repos[:15]) if len(repos) > 1 else "")
            purge = W.QCheckBox("Also delete the app's data (Flatpak only)")
            box.setCheckBox(purge)
            box.setStandardButtons(W.QMessageBox.StandardButton.Yes | W.QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(W.QMessageBox.StandardButton.Cancel)
            if box.exec() != W.QMessageBox.StandardButton.Yes:
                return
            args = ["remove", *repos, "-y", "--json"] + (["--purge"] if purge.isChecked() else [])
            self.enqueue("Remove " + (repos[0] if len(repos) == 1 else f"{len(repos)} packages"), args)

        def act_launch(self):
            repos = self.selected_repos()
            link = self.installed.get(repos[0], {}).get("link") if len(repos) == 1 else None
            if link:
                QtCore.QProcess.startDetached(link, [])
                self.set_status(f"Launched {Path(link).name}")

        def act_github(self):
            repos = self.selected_repos()
            if len(repos) == 1:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl(f"https://github.com/{repos[0]}"))

        def context_menu(self, pos):
            repos = self.selected_repos()
            if not repos:
                return
            inst = [r for r in repos if r in self.installed]
            m = W.QMenu(self)
            if len(inst) < len(repos):
                m.addAction("Install", self.act_install)
            if any(r in self.updates for r in inst):
                m.addAction("Update", self.act_update)
            if inst:
                m.addAction("Reinstall", self.act_reinstall)
                m.addAction("Remove", self.act_remove)
            if len(repos) == 1:
                m.addSeparator()
                if self.installed.get(repos[0], {}).get("link"):
                    m.addAction("Launch", self.act_launch)
                m.addAction("Choose package type / file…", lambda: self.tabs.setCurrentIndex(1))
                m.addAction("Open on GitHub", self.act_github)
                m.addAction("Copy name", lambda: QtWidgets.QApplication.clipboard().setText(repos[0]))
            m.exec(self.table.viewport().mapToGlobal(pos))

        # ------------------------------------------------------------ settings & misc
        def toggle_auto(self, checked: bool):
            if checked:
                hours, ok = W.QInputDialog.getInt(self, "Automatic updates", "Check for updates every (hours):",
                                                  self.auto_hours, 1, 168)
                if not ok:
                    self.act_auto.setChecked(False)
                    return
                args = ["auto", "on", "--every", str(hours)]
            else:
                args = ["auto", "off"]
            self.query(args, self._auto_changed)

        def _auto_changed(self, data, msg):
            if data is None:
                W.QMessageBox.warning(self, "Auto-updates", msg)
                self.query(["auto", "status"], self._got_auto)
                return
            self._got_auto(data, "")
            self.set_status("Automatic updates " + ("on" if data["auto"] else "off"))

        def export_list(self):
            path, _ = W.QFileDialog.getSaveFileName(self, "Export app list", str(HOME / "gitet-apps.json"), "JSON (*.json)")
            if path:
                self.enqueue("Export app list", ["export", path])

        def import_list(self):
            path, _ = W.QFileDialog.getOpenFileName(self, "Import app list", str(HOME), "JSON (*.json)")
            if path:
                self.enqueue("Import app list", ["import", path])

        def edit_token(self):
            dlg = TokenDialog(self)
            if dlg.exec() != W.QDialog.DialogCode.Accepted:
                return
            if dlg.removed:
                TOKEN_FILE.unlink(missing_ok=True)
                self.set_status("GitHub token removed")
            elif dlg.edit.text().strip():
                TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
                TOKEN_FILE.write_text(dlg.edit.text().strip() + "\n")
                os.chmod(TOKEN_FILE, 0o600)
                self.set_status("GitHub token saved")

        def check_gitet_update(self):
            self.set_status("Checking for a newer gitet…")

            def cb(d, msg):
                if d is None:
                    W.QMessageBox.warning(self, "Gitet", msg)
                elif d["update_available"]:
                    if W.QMessageBox.question(self, "Gitet", f"gitet {d['latest']} is available (you have {d['current']}).\n\nUpdate now?") \
                            == W.QMessageBox.StandardButton.Yes:
                        self.enqueue("Update gitet", ["self-update", "--json"])
                else:
                    W.QMessageBox.information(self, "Gitet", f"gitet {d['current']} is up to date.")
                self.set_status("Ready")

            self.query(["self-update", "--check"], cb)

        def check_gui_update(self):
            self.set_status("Checking for a newer Gitet GUI…")
            th = FetchThread(GUI_URL, self)
            self._gui_thread = th

            def done(text, error):
                self.set_status("Ready")
                m = re.search(r'^GUI_VERSION = "([\d.]+)"', text, re.M)
                if error or not m or not text.startswith("#!"):
                    W.QMessageBox.warning(self, "Gitet GUI", f"Couldn't check for updates: {error or 'unexpected file'}")
                elif vtuple(m.group(1)) <= vtuple(GUI_VERSION):
                    W.QMessageBox.information(self, "Gitet GUI", f"Gitet GUI {GUI_VERSION} is up to date.")
                elif W.QMessageBox.question(self, "Gitet GUI", f"Gitet GUI {m.group(1)} is available (you have {GUI_VERSION}).\n\nUpdate now?") \
                        == W.QMessageBox.StandardButton.Yes:
                    try:
                        compile(text, "gitet-gui.py", "exec")
                        tmp = GUI_BIN.with_suffix(".new")
                        tmp.write_text(text)
                        os.chmod(tmp, 0o755)
                        os.replace(tmp, GUI_BIN)
                        W.QMessageBox.information(self, "Gitet GUI", "Updated. Restart Gitet to use the new version.")
                    except (SyntaxError, OSError) as e:
                        W.QMessageBox.warning(self, "Gitet GUI", f"Update failed: {e}")

            th.result.connect(done)
            th.start()

        def about(self):
            W.QMessageBox.about(
                self, "About Gitet",
                f"<h3>Gitet GUI {GUI_VERSION}</h3><p>A friendly package manager for GitHub releases.</p>"
                f"<p>gitet command-line: {self.gitet_version}<br>Qt binding: {QT}</p>"
                '<p><a href="https://github.com/AstroMeYT/Gitet">github.com/AstroMeYT/Gitet</a></p>')

        def closeEvent(self, ev):
            if self.task:
                if W.QMessageBox.question(self, "Task running", "A task is still running. Quit anyway?") \
                        != W.QMessageBox.StandardButton.Yes:
                    ev.ignore()
                    return
                self.task.kill()
            s = QtCore.QSettings("gitet", "gitet-gui")
            s.setValue("geometry", self.saveGeometry())
            for i, sp in enumerate(self._splits):
                s.setValue(f"split{i}", sp.saveState())
            ev.accept()


    def run_askpass(app, args: list) -> int:
        prompt = " ".join(a for a in args if a != "--askpass") or "Password:"
        text, ok = W.QInputDialog.getText(None, "Authentication required", prompt, W.QLineEdit.EchoMode.Password)
        if ok:
            print(text)
            return 0
        return 1


    def ensure_cli(app) -> str | None:
        cli = find_gitet()
        if cli and cli_version(cli) >= MIN_GITET:
            return cli
        what = "isn't installed" if not cli else "is too old for this version of the GUI"
        if W.QMessageBox.question(None, "Gitet command-line tool",
                                  f"The gitet command {what}.\n\nDownload the latest version from GitHub now?") \
                != W.QMessageBox.StandardButton.Yes:
            return None
        try:
            text = download_text(CLI_URL)
            compile(text, "gitet.py", "exec")
        except (urllib.error.URLError, OSError, SyntaxError) as e:
            W.QMessageBox.critical(None, "Gitet", f"Couldn't download gitet: {getattr(e, 'reason', e)}")
            return None
        tmp = Path(tempfile.mkdtemp(prefix="gitet-dl-")) / "gitet.py"
        tmp.write_text(text)
        os.chmod(tmp, 0o755)
        subprocess.run([str(tmp), "--version"], capture_output=True, text=True, timeout=60)  # first run installs itself
        shutil.rmtree(tmp.parent, ignore_errors=True)
        cli = find_gitet()
        if cli and cli_version(cli) >= MIN_GITET:
            return cli
        W.QMessageBox.critical(None, "Gitet", "gitet is still missing or outdated after the download.")
        return None


def missing_qt() -> int:
    msg = ("Gitet GUI needs PyQt6 (or PySide6).\n"
           "  Debian/Ubuntu:  sudo apt install python3-pyqt6\n"
           "  Fedora:         sudo dnf install python3-pyqt6\n"
           "  Arch:           sudo pacman -S python-pyqt6\n"
           "  or:             pip install --user PyQt6")
    print(msg, file=sys.stderr)
    if sys.stdin.isatty() and input("\nTry `pip install --user PyQt6` now? [y/N] ").strip().lower() in ("y", "yes"):
        for extra in ([], ["--break-system-packages"]):
            if subprocess.run([sys.executable, "-m", "pip", "install", "--user", "PyQt6", *extra]).returncode == 0:
                os.execv(sys.executable, [sys.executable, str(self_path()), *sys.argv[1:]])
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", "Gitet GUI", "PyQt6 is required. Install python3-pyqt6 and try again."])
    return 1


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print(f"gitet-gui {GUI_VERSION}")
        return 0
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0
    if "--uninstall" in args:
        uninstall_self()
        return 0
    if "--askpass" not in args and not os.environ.get("GITET_GUI_NO_SELF_INSTALL"):
        ensure_installed()
    if "--install" in args:
        return 0
    if not QT:
        return missing_qt()
    app = W.QApplication(sys.argv)
    app.setApplicationName("Gitet")
    app.setDesktopFileName("gitet-gui")
    if "--askpass" in args:
        return run_askpass(app, args)
    cli = ensure_cli(app)
    if not cli:
        return 1
    win = MainWindow(cli)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
