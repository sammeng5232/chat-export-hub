#!/usr/bin/env python3
"""Chat Export Hub — unified desktop UI for multi-agent chat live exports.

Performance notes (v2.1):
  - QTableView + model (virtualized painting) instead of QTableWidget
  - Reload state/logs only when file mtimes change
  - Soft status refresh between full data reloads
  - Longer default interval; never blocks UI on export (QThread)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# Sibling modules importable as script or frozen .exe
_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

# PyInstaller's PySide6 runtime hook does not always register the sibling
# shiboken6 directory for the 6.11 Python 3.14 wheels. Register both bundled
# directories before loading QtCore so QtCore.pyd can resolve its DLLs in
# one-file and one-directory builds.
if getattr(sys, "frozen", False):
    _BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", _TOOLS_DIR))
    for _dll_dir in (_BUNDLE_DIR / "PySide6", _BUNDLE_DIR / "shiboken6"):
        if _dll_dir.is_dir():
            try:
                os.add_dll_directory(str(_dll_dir))
            except (AttributeError, OSError):
                pass
            os.environ["PATH"] = str(_dll_dir) + os.pathsep + os.environ.get("PATH", "")

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from chat_export_agents import AgentSpec, agent_by_id, enabled_agents
from chat_export_i18n import LANG_EN, LANG_ZH, i18n
from chat_export_search_index import SearchIndex

HOME = Path.home()
APP_VERSION = "2.3.0"


def app_name() -> str:
    return i18n.t("app_name")
# Full data reload cadence (state files). Status chips refresh more often.
REFRESH_MS = 8000
STATUS_MS = 4000
PID_CACHE_TTL = 5.0


def find_python() -> str:
    candidates: list[Path] = []
    if not getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable))
    for env_key in ("PYTHON_EXECUTABLE", "PYTHONHOME"):
        val = os.environ.get(env_key)
        if val:
            candidates.append(Path(val))
            candidates.append(Path(val) / "python.exe")
    local = os.environ.get("LOCALAPPDATA", "")
    candidates.extend(
        [
            Path(r"C:\Program Files\PyManager\python.exe"),
            Path(local) / "Programs" / "Python" / "Python314" / "python.exe",
            Path(local) / "Programs" / "Python" / "Python313" / "python.exe",
            Path(local) / "Programs" / "Python" / "Python312" / "python.exe",
            HOME / "AppData" / "Local" / "Programs" / "Python" / "Python314" / "python.exe",
        ]
    )
    for path in candidates:
        try:
            if path and path.is_file() and "ChatExport" not in path.name:
                if path.name.lower() in ("python.exe", "python3.exe", "python"):
                    return str(path)
        except OSError:
            continue
    from shutil import which

    for name in ("python.exe", "python"):
        found = which(name)
        if found and "ChatExport" not in found:
            return found
    raise RuntimeError(
        "Could not find python.exe to run exporters. Install Python or set PYTHON_EXECUTABLE."
    )


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def file_fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def human_bytes(n: int | float | None) -> str:
    if n is None:
        return "—"
    try:
        size = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def short_time(value: str | None) -> str:
    if not value:
        return "—"
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text[:19] if len(text) >= 19 else text


def source_location(source: str, source_key: str = "", output: str = "") -> str:
    """Return a compact visible location label for an exported record."""
    haystack = " ".join((source_key or "", source or "", output or ""))
    normalized = haystack.replace("\\", "/")
    match = re.search(r"//wsl(?:\.localhost|\$)/([^/\\]+)", normalized, re.IGNORECASE)
    if match:
        return f"WSL: {match.group(1)}"
    match = re.search(r"@wsl-([^:/\\]+)", haystack, re.IGNORECASE)
    if match:
        return f"WSL: {match.group(1)}"
    match = re.search(r"@vbox-([^:/\\]+)", haystack, re.IGNORECASE)
    if match:
        return f"VM: {match.group(1)}"
    match = re.search(r"@ssh-([^:/\\]+)", haystack, re.IGNORECASE)
    if match:
        return f"SSH: {match.group(1)}"
    match = re.search(r"chatexporthub/staging/(?:vbox|ssh)-([^/\\]+)/", normalized, re.IGNORECASE)
    if match:
        return f"VM: {match.group(1)}" if "/staging/vbox-" in normalized.lower() else f"SSH: {match.group(1)}"
    return "Local"


_pid_cache: dict[int, tuple[float, bool]] = {}


def process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    pid = int(pid)
    now = time.monotonic()
    cached = _pid_cache.get(pid)
    if cached and now - cached[0] < PID_CACHE_TTL:
        return cached[1]
    alive = False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            alive = True
    except Exception:
        alive = False
    _pid_cache[pid] = (now, alive)
    return alive


def task_state(task_name: str | None) -> str:
    if not task_name:
        return "—"
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-ScheduledTask -TaskName '{task_name}' -ErrorAction SilentlyContinue).State",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return (completed.stdout or "").strip() or "Not found"
    except Exception as exc:
        return f"error: {exc}"


def normalize_counts(rec: dict[str, Any], layout: str) -> tuple[int, int, int]:
    counts = rec.get("counts") or {}
    if not isinstance(counts, dict):
        counts = {}
    kind = str(rec.get("kind") or rec.get("state") or "")
    if kind == "prompt-history":
        return int(counts.get("prompts") or 0), 0, 0
    user = int(counts.get("user") or counts.get("prompts") or 0)
    assistant = int(counts.get("assistant") or 0)
    tools = int(counts.get("tool_call") or 0) + int(counts.get("tool_output") or 0)
    return user, assistant, tools


def parse_summary_pair(summaries: list[Any], agent_id: str) -> tuple[int | None, int | None]:
    token = agent_id.lower()
    for raw in summaries or []:
        s = str(raw)
        low = s.lower()
        if token not in low:
            continue
        tracked = changed = None
        if "tracked=" in s:
            try:
                tracked = int(s.split("tracked=")[1].split()[0])
            except (IndexError, ValueError):
                pass
        if "changed=" in s:
            try:
                changed = int(s.split("changed=")[1].split()[0])
            except (IndexError, ValueError):
                pass
        return tracked, changed
    return None, None


@dataclass(slots=True)
class ExportRow:
    agent_id: str
    agent_name: str
    agent_color: str
    location: str
    title: str
    kind: str
    session_id: str
    model: str
    user: int
    assistant: int
    tools: int
    bytes: int
    updated: str
    created: str
    output: str
    source: str
    cwd: str
    state: str
    # preformatted for cheap paint
    size_text: str = ""
    updated_text: str = ""

    def __post_init__(self) -> None:
        if not self.size_text:
            self.size_text = human_bytes(self.bytes)
        if not self.updated_text:
            self.updated_text = short_time(self.updated or self.created)


class ExportTableModel(QAbstractTableModel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[ExportRow] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else 11

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        cols = i18n.columns()
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(cols):
            return cols[section]
        return None

    def retranslate(self) -> None:
        # Force header labels to refresh for the new language.
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, 10)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                row.agent_name,
                row.location,
                row.title,
                row.kind,
                row.session_id,
                row.model,
                str(row.user),
                str(row.assistant),
                str(row.tools),
                row.size_text,
                row.updated_text,
            )[col]
        if role == Qt.ItemDataRole.ForegroundRole and col == 0:
            return QColor(row.agent_color)
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (6, 7, 8, 9):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.UserRole:
            # numeric / raw sort keys
            return (
                row.agent_name,
                row.location,
                row.title,
                row.kind,
                row.session_id,
                row.model,
                row.user,
                row.assistant,
                row.tools,
                row.bytes,
                row.updated or row.created,
            )[col]
        if role == Qt.ItemDataRole.UserRole + 1:
            return row.output
        if role == Qt.ItemDataRole.UserRole + 2:
            return row.agent_id
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{row.output}\n{row.source}"
        return None

    def set_rows(self, rows: list[ExportRow]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, row: int) -> ExportRow | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None


class ExportFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._agent_id: str | None = None  # None = all
        self._needle = ""
        self._content_matches: set[str] | None = None  # None = content search inactive
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setSortRole(Qt.ItemDataRole.UserRole)

    def set_agent_id(self, agent_id: str | None) -> None:
        if agent_id != self._agent_id:
            self._agent_id = agent_id
            self.invalidateFilter()

    def set_needle(self, text: str) -> None:
        needle = (text or "").strip().lower()
        if needle != self._needle:
            self._needle = needle
            self.invalidateFilter()

    def set_content_matches(self, matches: set[str] | None) -> None:
        """Session file paths that matched the last full-text content search.

        None means content search is inactive (checkbox off, or empty query);
        rows are then filtered on metadata only, same as before this feature.
        """
        if matches != self._content_matches:
            self._content_matches = matches
            self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        if not isinstance(model, ExportTableModel):
            return True
        row = model.row_at(source_row)
        if row is None:
            return False
        if self._agent_id is not None and row.agent_id != self._agent_id:
            return False
        if not self._needle:
            return True
        hay = " ".join(
            [
                row.agent_name,
                row.agent_id,
                row.location,
                row.title,
                row.kind,
                row.session_id,
                row.model,
                row.output,
                row.source,
                row.cwd,
                row.state,
            ]
        ).lower()
        if self._needle in hay:
            return True
        return self._content_matches is not None and row.output in self._content_matches


class ExportWorker(QThread):
    finished_ok = Signal(str, str)
    finished_err = Signal(str, str)

    def __init__(self, agents: list[AgentSpec], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.agents = agents

    def run(self) -> None:
        messages: list[str] = []
        try:
            python = find_python()
        except RuntimeError as exc:
            self.finished_err.emit("all", str(exc))
            return
        for agent in self.agents:
            if not agent.exporter or not agent.exporter.exists():
                self.finished_err.emit(agent.id, f"Exporter not found: {agent.exporter}")
                return
            cmd = [python, str(agent.exporter), *agent.export_args]
            try:
                completed = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=600,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception as exc:
                self.finished_err.emit(agent.id, str(exc))
                return
            out = (completed.stdout or completed.stderr or "").strip()
            if completed.returncode != 0:
                self.finished_err.emit(agent.id, f"exit {completed.returncode}: {out or 'no output'}")
                return
            messages.append(f"{agent.short}: {out or 'ok'}")
        label = self.agents[0].id if len(self.agents) == 1 else "all"
        self.finished_ok.emit(label, "\n".join(messages))


class IndexSyncWorker(QThread):
    """Reindexes changed export files into the content search DB off the UI thread."""

    finished_ok = Signal(int)
    finished_err = Signal(str)

    def __init__(
        self, index: SearchIndex, records: list[tuple[str, str, str]], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._index = index
        self._records = records

    def run(self) -> None:
        try:
            count = self._index.sync(self._records)
        except Exception as exc:  # pragma: no cover - defensive, mirrors ExportWorker
            self.finished_err.emit(str(exc))
            return
        self.finished_ok.emit(count)


class IndexSearchWorker(QThread):
    """Runs one content-search query against the FTS5 index off the UI thread."""

    finished_ok = Signal(str, list)
    finished_err = Signal(str, str)

    def __init__(self, index: SearchIndex, query: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._index = index
        self._query = query

    def run(self) -> None:
        try:
            matches = self._index.search(self._query)
        except Exception as exc:  # pragma: no cover - defensive, mirrors ExportWorker
            self.finished_err.emit(self._query, str(exc))
            return
        self.finished_ok.emit(self._query, list(matches))


class StatCard(QLabel):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self._last: tuple[str, str | None] = ("", None)
        self.setMinimumWidth(120)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setWordWrap(True)
        self.set_value("—")
        self.setStyleSheet(
            "QLabel { background:#1e1e2e; color:#cdd6f4; border:1px solid #313244;"
            " border-radius:10px; padding:10px 12px; }"
        )

    def set_title(self, title: str) -> None:
        if title == self._title:
            return
        self._title = title
        # Force repaint with current value
        val, sub = self._last
        self._last = ("", None)
        self.set_value(val or "—", sub)

    def set_value(self, value: str, subtitle: str | None = None) -> None:
        key = (value, subtitle)
        if key == self._last:
            return
        self._last = key
        # Chinese titles should not be uppercased into awkward forms
        title_disp = self._title if i18n.lang == LANG_ZH else self._title.upper()
        sub = (
            f'<div style="color:#a6adc8;font-size:11px;margin-top:2px;">{subtitle}</div>'
            if subtitle
            else ""
        )
        self.setText(
            f'<div style="color:#89b4fa;font-size:11px;font-weight:600;">{title_disp}</div>'
            f'<div style="color:#cdd6f4;font-size:18px;font-weight:700;margin-top:3px;">{value}</div>'
            f"{sub}"
        )


class HubWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.agents = enabled_agents()
        self.selected_agent_id: str | None = None
        self._all_rows: list[ExportRow] = []
        self._worker: ExportWorker | None = None
        self._state_fps: dict[str, tuple[int, int] | None] = {}
        self._log_fps: dict[str, tuple[int, int] | None] = {}
        self._last_log_text = ""
        self._status_cache: dict[str, dict[str, Any]] = {}

        # Content search (SQLite FTS5 index over exported session bodies)
        self._search_index = SearchIndex()
        self._index_sync_worker: IndexSyncWorker | None = None
        self._index_search_worker: IndexSearchWorker | None = None
        self._pending_sync_records: list[tuple[str, str, str]] | None = None
        self._pending_search_query: str | None = None

        self.setWindowTitle(f"{app_name()} v{APP_VERSION}")
        self.resize(1280, 800)
        self.setMinimumSize(960, 600)

        self._build_ui()
        self._apply_theme()
        self.retranslate_ui()

        self.data_timer = QTimer(self)
        self.data_timer.timeout.connect(self.refresh_data)
        self.data_timer.start(REFRESH_MS)

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.refresh_status_only)
        self.status_timer.start(STATUS_MS)

        # Initial load
        self.refresh_data(force=True)

    def _build_ui(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)

        self.act_refresh = QAction(self)
        self.act_refresh.triggered.connect(lambda: self.refresh_data(force=True))
        tb.addAction(self.act_refresh)

        self.act_export_selected = QAction(self)
        self.act_export_selected.triggered.connect(self.export_selected)
        tb.addAction(self.act_export_selected)

        self.act_export_all = QAction(self)
        self.act_export_all.triggered.connect(self.export_all)
        tb.addAction(self.act_export_all)

        self.act_open_folder = QAction(self)
        self.act_open_folder.triggered.connect(self.open_folder)
        tb.addAction(self.act_open_folder)

        self.act_open_log = QAction(self)
        self.act_open_log.triggered.connect(self.open_log)
        tb.addAction(self.act_open_log)

        self.act_task_status = QAction(self)
        self.act_task_status.triggered.connect(self.show_task_status)
        tb.addAction(self.act_task_status)

        tb.addSeparator()
        self.lbl_lang = QLabel()
        self.lbl_lang.setStyleSheet("color:#a6adc8; margin-left:8px;")
        tb.addWidget(self.lbl_lang)
        self.lang_combo = QComboBox()
        self.lang_combo.setMinimumWidth(110)
        self.lang_combo.addItem("English", LANG_EN)
        self.lang_combo.addItem("中文", LANG_ZH)
        idx = self.lang_combo.findData(i18n.lang)
        if idx >= 0:
            self.lang_combo.setCurrentIndex(idx)
        self.lang_combo.currentIndexChanged.connect(self._on_language_changed)
        tb.addWidget(self.lang_combo)

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(12)

        left = QVBoxLayout()
        self.left_title = QLabel()
        self.left_title.setStyleSheet("font-size:14px;font-weight:700;color:#cdd6f4;")
        left.addWidget(self.left_title)

        self.agent_list = QListWidget()
        self.agent_list.setMinimumWidth(190)
        self.agent_list.setMaximumWidth(240)
        self.agent_list.currentItemChanged.connect(self._on_agent_changed)
        left.addWidget(self.agent_list, 1)

        self.chk_auto = QCheckBox()
        self.chk_auto.setChecked(True)
        self.chk_auto.toggled.connect(self._toggle_auto)
        left.addWidget(self.chk_auto)

        self.hint = QLabel()
        self.hint.setStyleSheet("color:#6c7086;font-size:11px;")
        self.hint.setWordWrap(True)
        left.addWidget(self.hint)

        left_wrap = QWidget()
        left_wrap.setLayout(left)
        root.addWidget(left_wrap)

        right = QVBoxLayout()
        right.setSpacing(10)

        header = QHBoxLayout()
        self.lbl_title = QLabel()
        self.lbl_title.setStyleSheet("font-size:22px;font-weight:700;color:#cdd6f4;")
        header.addWidget(self.lbl_title)
        header.addStretch(1)
        self.lbl_status = QLabel()
        self.lbl_status.setStyleSheet(
            "font-size:13px;font-weight:600;padding:6px 12px;"
            "border-radius:8px;background:#313244;color:#cdd6f4;"
        )
        header.addWidget(self.lbl_status)
        right.addLayout(header)

        self.lbl_meta = QLabel("")
        self.lbl_meta.setStyleSheet("color:#a6adc8;font-size:12px;")
        self.lbl_meta.setWordWrap(True)
        right.addWidget(self.lbl_meta)

        cards = QHBoxLayout()
        cards.setSpacing(8)
        self.card_agents = StatCard("")
        self.card_tracked = StatCard("")
        self.card_changed = StatCard("")
        self.card_size = StatCard("")
        self.card_msgs = StatCard("")
        self.card_tools = StatCard("")
        self.card_scan = StatCard("")
        for c in (
            self.card_agents,
            self.card_tracked,
            self.card_changed,
            self.card_size,
            self.card_msgs,
            self.card_tools,
            self.card_scan,
        ):
            cards.addWidget(c)
        right.addLayout(cards)

        self.agent_status_row = QHBoxLayout()
        self.agent_status_labels: dict[str, QLabel] = {}
        for agent in self.agents:
            lab = QLabel(f"{agent.short}: —")
            lab.setStyleSheet(
                f"background:#181825;border:1px solid {agent.color}55;"
                f"border-left:3px solid {agent.color};border-radius:6px;"
                f"padding:6px 10px;color:#cdd6f4;font-size:12px;"
            )
            lab.setWordWrap(True)
            self.agent_status_labels[agent.id] = lab
            self.agent_status_row.addWidget(lab, 1)
        right.addLayout(self.agent_status_row)

        search_row = QHBoxLayout()
        self.lbl_filter = QLabel()
        search_row.addWidget(self.lbl_filter)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        # Debounce filter typing
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(200)
        self._filter_timer.timeout.connect(self._apply_filter_now)
        self.search.textChanged.connect(lambda _t: self._filter_timer.start())
        search_row.addWidget(self.search, 1)
        self.chk_content_search = QCheckBox()
        self.chk_content_search.setChecked(True)
        self.chk_content_search.toggled.connect(lambda _checked: self._apply_filter_now())
        search_row.addWidget(self.chk_content_search)
        self.lbl_count = QLabel("")
        self.lbl_count.setStyleSheet("color:#a6adc8;")
        search_row.addWidget(self.lbl_count)
        right.addLayout(search_row)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self.model = ExportTableModel(self)
        self.proxy = ExportFilterProxy(self)
        self.proxy.setSourceModel(self.model)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)
        self.table.setWordWrap(False)
        # Fixed row height keeps large tables cheap to layout/paint.
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.doubleClicked.connect(self.open_selected_export)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for col in (0, 1, 3, 4, 5, 6, 7, 8, 9, 10):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        # Sort by Updated desc initially after first load
        self.table.sortByColumn(10, Qt.SortOrder.DescendingOrder)
        splitter.addWidget(self.table)

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        self.lbl_log = QLabel()
        bl.addWidget(self.lbl_log)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(200)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(10)
        self.log_view.setFont(mono)
        bl.addWidget(self.log_view)
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        # Keep log panel smaller so table dominates
        splitter.setSizes([560, 140])
        right.addWidget(splitter, 1)

        footer = QHBoxLayout()
        self.btn_open_export = QPushButton()
        self.btn_open_export.clicked.connect(self.open_selected_export)
        footer.addWidget(self.btn_open_export)
        self.btn_reveal = QPushButton()
        self.btn_reveal.clicked.connect(self.reveal_selected)
        footer.addWidget(self.btn_reveal)
        self.btn_copy = QPushButton()
        self.btn_copy.clicked.connect(self.copy_selected_path)
        footer.addWidget(self.btn_copy)
        footer.addStretch(1)
        self.lbl_exporting = QLabel("")
        self.lbl_exporting.setStyleSheet("color:#f9e2af;")
        footer.addWidget(self.lbl_exporting)
        right.addLayout(footer)

        right_wrap = QWidget()
        right_wrap.setLayout(right)
        root.addWidget(right_wrap, 1)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self._populate_agent_list()

    def _on_language_changed(self, _index: int) -> None:
        lang = self.lang_combo.currentData()
        if lang not in (LANG_EN, LANG_ZH) or lang == i18n.lang:
            return
        i18n.set_lang(lang)
        self.retranslate_ui()
        self.refresh_status_only()
        self._update_stats_from_rows()
        self.status.showMessage(i18n.t("lang_switched"), 3000)

    def retranslate_ui(self) -> None:
        """Apply current language to all static UI chrome."""
        name = app_name()
        self.setWindowTitle(f"{name} v{APP_VERSION}")
        QApplication.instance().setApplicationName(name)  # type: ignore[union-attr]

        self.act_refresh.setText(i18n.t("act_refresh"))
        self.act_export_selected.setText(i18n.t("act_export_selected"))
        self.act_export_all.setText(i18n.t("act_export_all"))
        self.act_open_folder.setText(i18n.t("act_open_folder"))
        self.act_open_log.setText(i18n.t("act_open_log"))
        self.act_task_status.setText(i18n.t("act_task_status"))
        self.lbl_lang.setText(i18n.t("language") + ":")

        self.left_title.setText(i18n.t("agents"))
        self.chk_auto.setText(i18n.t("auto_refresh"))
        self.hint.setText(
            i18n.t("sidebar_hint", version=APP_VERSION, seconds=REFRESH_MS // 1000)
        )

        self.card_agents.set_title(i18n.t("card_agents"))
        self.card_tracked.set_title(i18n.t("card_tracked"))
        self.card_changed.set_title(i18n.t("card_changed"))
        self.card_size.set_title(i18n.t("card_size"))
        self.card_msgs.set_title(i18n.t("card_msgs"))
        self.card_tools.set_title(i18n.t("card_tools"))
        self.card_scan.set_title(i18n.t("card_scan"))

        self.lbl_filter.setText(i18n.t("filter"))
        self.search.setPlaceholderText(i18n.t("filter_placeholder"))
        self.chk_content_search.setText(i18n.t("chk_content_search"))
        self.chk_content_search.setToolTip(i18n.t("chk_content_search_tip"))
        self.lbl_log.setText(i18n.t("log_label"))
        self.btn_open_export.setText(i18n.t("btn_open_export"))
        self.btn_reveal.setText(i18n.t("btn_reveal"))
        self.btn_copy.setText(i18n.t("btn_copy_path"))

        self.model.retranslate()
        self._retranslate_agent_list()
        self._update_header_for_selection()
        self._update_count_label()

    def _populate_agent_list(self) -> None:
        current = self.selected_agent_id if hasattr(self, "selected_agent_id") else None
        self.agent_list.blockSignals(True)
        self.agent_list.clear()
        all_item = QListWidgetItem(i18n.t("all_agents"))
        all_item.setData(Qt.ItemDataRole.UserRole, None)
        self.agent_list.addItem(all_item)
        for agent in self.agents:
            display = i18n.agent_name(agent.id, agent.name)
            item = QListWidgetItem(f"●  {display}")
            item.setData(Qt.ItemDataRole.UserRole, agent.id)
            item.setForeground(QColor(agent.color))
            item.setToolTip(
                f"{agent.notes}\n{agent.output_dir}\n{agent.source_home or '—'}"
            )
            self.agent_list.addItem(item)
        # Restore selection
        row = 0
        if current is not None:
            for i in range(self.agent_list.count()):
                if self.agent_list.item(i).data(Qt.ItemDataRole.UserRole) == current:
                    row = i
                    break
        self.agent_list.setCurrentRow(row)
        self.agent_list.blockSignals(False)

    def _retranslate_agent_list(self) -> None:
        """Update agent list labels without resetting selection unexpectedly."""
        self._populate_agent_list()

    def _apply_theme(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        app.setStyle("Fusion")
        palette = QPalette()
        bg, base, alt, text, hi = (
            QColor("#11111b"),
            QColor("#1e1e2e"),
            QColor("#181825"),
            QColor("#cdd6f4"),
            QColor("#89b4fa"),
        )
        palette.setColor(QPalette.ColorRole.Window, bg)
        palette.setColor(QPalette.ColorRole.WindowText, text)
        palette.setColor(QPalette.ColorRole.Base, base)
        palette.setColor(QPalette.ColorRole.AlternateBase, alt)
        palette.setColor(QPalette.ColorRole.Text, text)
        palette.setColor(QPalette.ColorRole.Button, base)
        palette.setColor(QPalette.ColorRole.ButtonText, text)
        palette.setColor(QPalette.ColorRole.Highlight, hi)
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#11111b"))
        app.setPalette(palette)
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background:#11111b; color:#cdd6f4; }
            QToolBar { background:#181825; border-bottom:1px solid #313244; spacing:8px; padding:4px; }
            QToolBar QToolButton { background:#313244; color:#cdd6f4; border-radius:6px; padding:6px 10px; margin:2px; }
            QToolBar QToolButton:hover { background:#45475a; }
            QListWidget { background:#1e1e2e; border:1px solid #313244; border-radius:8px; padding:4px; outline:none; }
            QListWidget::item { padding:8px 10px; border-radius:6px; margin:2px; }
            QListWidget::item:selected { background:#313244; }
            QListWidget::item:hover { background:#262637; }
            QLineEdit { background:#1e1e2e; border:1px solid #313244; border-radius:6px; padding:6px 8px; color:#cdd6f4; }
            QTableView {
                background:#1e1e2e; gridline-color:#313244; border:1px solid #313244;
                border-radius:8px; selection-background-color:#89b4fa; selection-color:#11111b;
            }
            QHeaderView::section {
                background:#181825; color:#a6adc8; padding:6px; border:none;
                border-bottom:1px solid #313244; border-right:1px solid #313244;
            }
            QPlainTextEdit { background:#181825; border:1px solid #313244; border-radius:8px; color:#a6adc8; }
            QPushButton { background:#313244; color:#cdd6f4; border:none; border-radius:6px; padding:8px 14px; }
            QPushButton:hover { background:#45475a; }
            QCheckBox { color:#a6adc8; }
            QStatusBar { background:#181825; color:#a6adc8; }
            QSplitter::handle { background:#313244; height:3px; }
            """
        )

    # --- selection / filter ----------------------------------------------
    def _on_agent_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        self.selected_agent_id = None if current is None else current.data(Qt.ItemDataRole.UserRole)
        self.proxy.set_agent_id(self.selected_agent_id)
        self._update_header_for_selection()
        self._update_stats_from_rows()
        self._load_log(force=False)
        self._update_count_label()

    def _toggle_auto(self, checked: bool) -> None:
        if checked:
            self.data_timer.start(REFRESH_MS)
            self.status_timer.start(STATUS_MS)
        else:
            self.data_timer.stop()
            self.status_timer.stop()

    def _apply_filter_now(self) -> None:
        needle = self.search.text().strip()
        self.proxy.set_needle(needle)
        if self.chk_content_search.isChecked() and needle:
            self._start_content_search(needle)
        else:
            self._pending_search_query = None
            self.proxy.set_content_matches(None)
        self._update_count_label()

    # --- content search (SQLite FTS5) -------------------------------------
    def _start_content_search(self, needle: str) -> None:
        if self._index_search_worker is not None and self._index_search_worker.isRunning():
            self._pending_search_query = needle
            return
        self._pending_search_query = None
        worker = IndexSearchWorker(self._search_index, needle, self)
        worker.finished_ok.connect(self._on_content_search_ok)
        worker.finished_err.connect(self._on_content_search_err)
        self._index_search_worker = worker
        worker.start()

    def _on_content_search_ok(self, query: str, matches: list) -> None:
        current = self.search.text().strip()
        if self.chk_content_search.isChecked() and query == current:
            self.proxy.set_content_matches(set(matches))
            self._update_count_label()
        self._drain_pending_search()

    def _on_content_search_err(self, _query: str, _message: str) -> None:
        # Non-fatal: content matches simply stay as they were; metadata
        # filtering (title/path/session id/…) keeps working regardless.
        self._drain_pending_search()

    def _drain_pending_search(self) -> None:
        pending = self._pending_search_query
        self._pending_search_query = None
        if pending is not None:
            self._start_content_search(pending)

    def _kick_content_sync(self) -> None:
        records = [
            (row.output, row.session_id, row.agent_id) for row in self._all_rows if row.output
        ]
        if self._index_sync_worker is not None and self._index_sync_worker.isRunning():
            self._pending_sync_records = records
            return
        self._start_index_sync(records)

    def _start_index_sync(self, records: list[tuple[str, str, str]]) -> None:
        worker = IndexSyncWorker(self._search_index, records, self)
        worker.finished_ok.connect(self._on_index_sync_ok)
        worker.finished_err.connect(self._on_index_sync_err)
        self._index_sync_worker = worker
        worker.start()

    def _on_index_sync_ok(self, count: int) -> None:
        if count:
            self.status.showMessage(i18n.t("status_index_ready", n=count), 4000)
        pending = self._pending_sync_records
        self._pending_sync_records = None
        if pending is not None:
            self._start_index_sync(pending)
        elif count and self.chk_content_search.isChecked():
            needle = self.search.text().strip()
            if needle:
                # Newly (re)indexed sessions may now match an active query.
                self._start_content_search(needle)

    def _on_index_sync_err(self, message: str) -> None:
        self.status.showMessage(i18n.t("status_index_error", message=message), 4000)
        pending = self._pending_sync_records
        self._pending_sync_records = None
        if pending is not None:
            self._start_index_sync(pending)

    def _update_count_label(self) -> None:
        self.lbl_count.setText(
            i18n.t("count_exports", shown=self.proxy.rowCount(), total=len(self._all_rows))
        )

    def _selected_agents(self) -> list[AgentSpec]:
        if self.selected_agent_id is None:
            return list(self.agents)
        agent = agent_by_id(self.selected_agent_id)
        return [agent] if agent else []

    # --- data loading ----------------------------------------------------
    def _state_changed(self) -> bool:
        changed = False
        for agent in self.agents:
            fp = file_fingerprint(agent.state_file())
            key = agent.id
            if self._state_fps.get(key) != fp:
                self._state_fps[key] = fp
                changed = True
        return changed

    def _collect_rows(self) -> list[ExportRow]:
        rows: list[ExportRow] = []
        for agent in self.agents:
            state = load_json(agent.state_file())
            sources = state.get("sources") or {}
            if not isinstance(sources, dict):
                continue
            for source_key, rec in sources.items():
                if not isinstance(rec, dict):
                    continue
                user, asst, tools = normalize_counts(rec, agent.layout)
                session_id = str(rec.get("session_id") or rec.get("source_id") or "")
                kind = str(rec.get("kind") or rec.get("state") or "session")
                updated = str(rec.get("updated") or "")
                created = str(rec.get("created") or "")
                nbytes = int(rec.get("bytes") or 0)
                rows.append(
                    ExportRow(
                        agent_id=agent.id,
                        agent_name=agent.short,
                        agent_color=agent.color,
                        location=source_location(
                            str(rec.get("source") or source_key),
                            str(source_key),
                            str(rec.get("output") or ""),
                        ),
                        title=str(rec.get("title") or "Untitled"),
                        kind=kind,
                        session_id=session_id,
                        model=str(rec.get("model") or ""),
                        user=user,
                        assistant=asst,
                        tools=tools,
                        bytes=nbytes,
                        updated=updated,
                        created=created,
                        output=str(rec.get("output") or ""),
                        source=str(rec.get("source") or source_key),
                        cwd=str(rec.get("cwd") or ""),
                        state=str(rec.get("state") or kind),
                    )
                )
        return rows

    def refresh_data(self, force: bool = False) -> None:
        t0 = time.perf_counter()
        if not force and not self._state_changed():
            # Still refresh lightweight status chips
            self.refresh_status_only()
            self.status.showMessage(
                i18n.t(
                    "status_uptodate",
                    n=len(self._all_rows),
                    time=datetime.now().strftime("%H:%M:%S"),
                )
            )
            return

        # Mark fingerprints even on force
        for agent in self.agents:
            self._state_fps[agent.id] = file_fingerprint(agent.state_file())

        self._all_rows = self._collect_rows()
        self.model.set_rows(self._all_rows)
        self._kick_content_sync()
        self._update_stats_from_rows()
        self.refresh_status_only()
        self._load_log(force=False)
        self._update_count_label()
        elapsed = (time.perf_counter() - t0) * 1000
        self.status.showMessage(
            i18n.t(
                "status_reload",
                ms=elapsed,
                n=len(self._all_rows),
                size=human_bytes(sum(r.bytes for r in self._all_rows)),
                time=datetime.now().strftime("%H:%M:%S"),
            )
        )

    def refresh_status_only(self) -> None:
        live_count = 0
        latest_scan = ""
        changed_total = 0

        for agent in self.agents:
            status = load_json(agent.resolved_status())
            self._status_cache[agent.id] = status
            n_sources = sum(1 for r in self._all_rows if r.agent_id == agent.id)
            status_name = str(status.get("status") or status.get("state") or "unknown")
            pid = status.get("pid")
            alive = process_alive(int(pid)) if pid else False
            summaries = status.get("summaries") or []
            tracked_s, changed_s = parse_summary_pair(summaries, agent.id)
            if tracked_s is None:
                tracked_s = status.get("tracked") if agent.id == "grok" else n_sources
            if changed_s is None:
                changed_s = status.get("changed") if agent.id == "grok" else 0
            try:
                changed_total += int(changed_s or 0)
            except (TypeError, ValueError):
                pass

            scan = (
                status.get("scan_finished_at")
                or status.get("updated_at")
                or ""
            )
            if scan and scan > latest_scan:
                latest_scan = scan

            if status_name.lower() == "running" and alive:
                live_count += 1
                chip = i18n.t(
                    "chip_live",
                    short=agent.short,
                    tracked=tracked_s,
                    changed=changed_s,
                )
                color = "#a6e3a1"
            elif status_name.lower() == "running" and not alive:
                chip = i18n.t("chip_stale", short=agent.short, n=n_sources)
                color = "#f9e2af"
            elif status_name.lower() in ("error", "failed"):
                chip = i18n.t("chip_error", short=agent.short)
                color = "#f38ba8"
            elif n_sources:
                chip = i18n.t(
                    "chip_files",
                    short=agent.short,
                    n=n_sources,
                    scan=short_time(scan),
                )
                color = "#89b4fa"
            else:
                chip = i18n.t("chip_nodata", short=agent.short)
                color = "#6c7086"

            lab = self.agent_status_labels.get(agent.id)
            if lab is not None:
                lab.setText(chip)
                lab.setStyleSheet(
                    f"background:#181825;border:1px solid {agent.color}55;"
                    f"border-left:3px solid {agent.color};border-radius:6px;"
                    f"padding:6px 10px;color:{color};font-size:12px;"
                )

        if self.selected_agent_id is None:
            if live_count == len(self.agents):
                badge = i18n.t(
                    "badge_all_live", n=live_count, total=len(self.agents)
                )
                fg, bg = "#a6e3a1", "#1e3a2f"
            elif live_count > 0:
                badge = i18n.t(
                    "badge_partial", n=live_count, total=len(self.agents)
                )
                fg, bg = "#f9e2af", "#3a321e"
            else:
                badge = i18n.t(
                    "badge_watchers", n=live_count, total=len(self.agents)
                )
                fg, bg = "#a6adc8", "#313244"
            self.lbl_title.setText(i18n.t("title_all", app=app_name()))
            self.card_changed.set_value(
                str(changed_total), i18n.t("sub_last_scan_sum")
            )
        else:
            agent = agent_by_id(self.selected_agent_id)
            name = (
                i18n.agent_name(agent.id, agent.name)
                if agent
                else self.selected_agent_id
            )
            self.lbl_title.setText(i18n.t("title_agent", app=app_name(), name=name))
            status = self._status_cache.get(self.selected_agent_id) or {}
            pid = status.get("pid")
            alive = process_alive(int(pid)) if pid else False
            st = str(status.get("status") or "unknown")
            if st.lower() == "running" and alive:
                badge = i18n.t("badge_live_pid", pid=pid)
                fg, bg = "#a6e3a1", "#1e3a2f"
            elif st.lower() == "running":
                badge = i18n.t("badge_stale_pid", pid=pid)
                fg, bg = "#f9e2af", "#3a321e"
            else:
                badge = i18n.t("badge_status", status=st.upper())
                fg, bg = "#a6adc8", "#313244"
            summaries = status.get("summaries") or []
            _t, ch = parse_summary_pair(summaries, self.selected_agent_id or "")
            if ch is None and agent and agent.id == "grok":
                ch = status.get("changed")
            self.card_changed.set_value(
                str(ch if ch is not None else "—"), i18n.t("sub_last_scan")
            )

        self.lbl_status.setText(badge)
        self.lbl_status.setStyleSheet(
            f"font-size:13px;font-weight:600;padding:6px 12px;"
            f"border-radius:8px;background:{bg};color:{fg};"
        )
        self.card_scan.set_value(
            short_time(latest_scan),
            i18n.t("sub_data_interval", s=REFRESH_MS // 1000),
        )
        self._update_header_for_selection()

    def _update_stats_from_rows(self) -> None:
        agent_ids = {a.id for a in self._selected_agents()}
        rows = [r for r in self._all_rows if r.agent_id in agent_ids]
        total_bytes = sum(r.bytes for r in rows)
        total_user = sum(r.user for r in rows)
        total_asst = sum(r.assistant for r in rows)
        total_tools = sum(r.tools for r in rows)
        self.card_agents.set_value(
            str(len(self._selected_agents())),
            i18n.t("sub_registered", n=len(self.agents)),
        )
        self.card_tracked.set_value(str(len(rows)), i18n.t("sub_in_view"))
        self.card_size.set_value(
            human_bytes(total_bytes), i18n.t("sub_exports", n=len(rows))
        )
        self.card_msgs.set_value(
            str(total_user + total_asst),
            i18n.t("sub_user_asst", u=total_user, a=total_asst),
        )
        self.card_tools.set_value(str(total_tools), i18n.t("sub_calls_outputs"))

    def _update_header_for_selection(self) -> None:
        agents = self._selected_agents()
        if len(agents) == 1:
            a = agents[0]
            self.lbl_meta.setText(
                i18n.t(
                    "meta_one",
                    output=a.output_dir,
                    source=a.source_home or "—",
                    task=a.task_name or "—",
                    notes=a.notes,
                )
            )
        else:
            parts = [f"{a.short}→{a.output_dir.name}" for a in agents]
            self.lbl_meta.setText(
                "  ·  ".join(parts) + i18n.t("meta_all_suffix", n=len(self.agents))
            )

    def _load_log(self, force: bool = False) -> None:
        agents = self._selected_agents()
        logs: list[Path] = []
        seen: set[str] = set()
        for a in agents:
            p = a.resolved_log()
            key = str(p)
            if key not in seen:
                seen.add(key)
                logs.append(p)

        # Skip work if fingerprints unchanged
        if not force:
            fps = {str(p): file_fingerprint(p) for p in logs}
            if fps == {k: self._log_fps.get(k) for k in fps}:
                return
            self._log_fps.update(fps)

        chunks: list[str] = []
        for log_path in logs:
            header = f"── {log_path.name} ──"
            if not log_path.exists():
                chunks.append(f"{header}\n{i18n.t('no_log_yet')}")
                continue
            try:
                # Read only the tail efficiently for large logs
                data = log_path.read_bytes()
                if len(data) > 64_000:
                    data = data[-64_000:]
                text = data.decode("utf-8", errors="replace")
                lines = text.splitlines()[-40:]
                chunks.append(header + "\n" + "\n".join(lines))
            except OSError as exc:
                chunks.append(f"{header}\n(error: {exc})")

        new_text = "\n\n".join(chunks) if chunks else i18n.t("no_logs")
        if new_text == self._last_log_text:
            return
        self._last_log_text = new_text
        sb = self.log_view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 20
        self.log_view.setPlainText(new_text)
        if at_bottom:
            sb.setValue(sb.maximum())

    # --- actions ---------------------------------------------------------
    def _selected_output(self) -> Path | None:
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return None
        idx = indexes[0]
        src = self.proxy.mapToSource(idx)
        path = self.model.data(self.model.index(src.row(), 0), Qt.ItemDataRole.UserRole + 1)
        return Path(str(path)) if path else None

    def open_selected_export(self) -> None:
        path = self._selected_output()
        if path is None:
            QMessageBox.information(self, app_name(), i18n.t("select_row"))
            return
        if not path.exists():
            QMessageBox.warning(
                self, app_name(), i18n.t("file_not_found", path=path)
            )
            return
        os.startfile(str(path))  # type: ignore[attr-defined]

    def reveal_selected(self) -> None:
        path = self._selected_output()
        if path is not None and path.exists():
            subprocess.run(
                ["explorer", "/select,", str(path)],
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        agents = self._selected_agents()
        if agents:
            agents[0].output_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(str(agents[0].output_dir))  # type: ignore[attr-defined]

    def copy_selected_path(self) -> None:
        path = self._selected_output()
        if path is None:
            QMessageBox.information(self, app_name(), i18n.t("select_row"))
            return
        QGuiApplication.clipboard().setText(str(path))
        self.status.showMessage(i18n.t("status_copied", path=path), 3000)

    def open_folder(self) -> None:
        seen: set[str] = set()
        for a in self._selected_agents():
            key = str(a.output_dir)
            if key in seen:
                continue
            seen.add(key)
            a.output_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(str(a.output_dir))  # type: ignore[attr-defined]

    def open_log(self) -> None:
        for a in self._selected_agents():
            log = a.resolved_log()
            if log.exists():
                os.startfile(str(log))  # type: ignore[attr-defined]
                return
        QMessageBox.information(self, app_name(), i18n.t("no_log"))

    def show_task_status(self) -> None:
        lines: list[str] = []
        seen_tasks: set[str] = set()
        for a in self.agents:
            status = load_json(a.resolved_status())
            pid = status.get("pid")
            alive = process_alive(int(pid)) if pid else False
            tname = a.task_name or "—"
            tstate = "—"
            if a.task_name and a.task_name not in seen_tasks:
                tstate = task_state(a.task_name)
                seen_tasks.add(a.task_name)
            elif a.task_name:
                tstate = i18n.t("task_same")
            lines.append(
                f"[{a.short}] task={tname}  state={tstate}\n"
                f"     pid={pid or '—'} alive={alive}  status={status.get('status') or '—'}\n"
                f"     output={a.output_dir}"
            )
        QMessageBox.information(
            self, i18n.t("task_dialog_title"), "\n\n".join(lines)
        )

    def export_selected(self) -> None:
        self._start_export(self._selected_agents())

    def export_all(self) -> None:
        self._start_export(list(self.agents))

    def _start_export(self, agents: list[AgentSpec]) -> None:
        if self._worker and self._worker.isRunning():
            QMessageBox.information(self, app_name(), i18n.t("export_running"))
            return
        missing = [a.short for a in agents if not a.exporter or not a.exporter.exists()]
        if missing:
            QMessageBox.warning(
                self,
                app_name(),
                i18n.t("exporter_missing", names=", ".join(missing)),
            )
            return
        self.lbl_exporting.setText(
            i18n.t("exporting", names=", ".join(a.short for a in agents))
        )
        self.status.showMessage(i18n.t("status_export_bg"))
        self._worker = ExportWorker(agents, self)
        self._worker.finished_ok.connect(self._on_export_ok)
        self._worker.finished_err.connect(self._on_export_err)
        self._worker.start()

    def _on_export_ok(self, _agent_id: str, message: str) -> None:
        self.lbl_exporting.setText("")
        self.status.showMessage(i18n.t("status_export_done"), 5000)
        self.refresh_data(force=True)
        QMessageBox.information(
            self, app_name(), i18n.t("export_finished", message=message)
        )

    def _on_export_err(self, agent_id: str, message: str) -> None:
        self.lbl_exporting.setText("")
        self.status.showMessage(i18n.t("status_export_fail"), 5000)
        QMessageBox.warning(
            self,
            app_name(),
            i18n.t("export_failed", agent=agent_id, message=message),
        )


def resolve_app_icon() -> QIcon:
    """Load the custom chat-export icon for window / taskbar."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        # onefile extract dir + beside the exe
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "chat_export_hub.ico")
            candidates.append(Path(meipass) / "assets" / "chat_export_hub.ico")
        candidates.append(Path(sys.executable))
        candidates.append(Path(sys.executable).with_name("chat_export_hub.ico"))
    candidates.extend(
        [
            _TOOLS_DIR / "assets" / "chat_export_hub.ico",
            _TOOLS_DIR / "chat_export_hub.ico",
        ]
    )
    for path in candidates:
        try:
            if path.is_file():
                icon = QIcon(str(path))
                if not icon.isNull():
                    return icon
        except OSError:
            continue
    return QIcon()


def main() -> int:
    # High-DPI / smoother UI on Windows
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName(app_name())
    app.setApplicationVersion(APP_VERSION)
    icon = resolve_app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
    win = HubWindow()
    if not icon.isNull():
        win.setWindowIcon(icon)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
