#!/usr/bin/env python3
"""Grok Chat Export Monitor — desktop UI for live export status and statistics.

Reads (never modifies) the live-export artifacts produced by export_grok_chats_live.py:

  ~/grok_chat_live_exports/
    .export_state.json
    watcher.status.json
    watcher.log
    MANIFEST.txt
    *.txt exports
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QPlainTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)


APP_NAME = "Grok Export Monitor"
APP_VERSION = "1.0.0"
REFRESH_MS = 2000

HOME = Path.home()
DEFAULT_OUTPUT = HOME / "grok_chat_live_exports"
DEFAULT_GROK_HOME = HOME / ".grok"
EXPORTER = DEFAULT_GROK_HOME / "tools" / "export_grok_chats_live.py"
WATCHER_PS1 = DEFAULT_GROK_HOME / "tools" / "run_grok_chat_export_watcher.ps1"
TASK_NAME = "Grok Chat Export Watcher"


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def human_bytes(n: int | float | None) -> str:
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    units = ["B", "KB", "MB", "GB"]
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def short_time(value: str | None) -> str:
    if not value:
        return "—"
    text = str(value).strip()
    # ISO with optional fractional seconds / Z
    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text[:19] if len(text) >= 19 else text


def process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        # Windows: OpenProcess via tasklist is heavy; os.kill(pid, 0) works on Unix.
        # On Windows, use ctypes or tasklist. Prefer a lightweight approach:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


def task_state() -> str:
    """Best-effort scheduled-task state."""
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue).State",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (completed.stdout or "").strip()
        return out or "Not found"
    except Exception as exc:
        return f"error: {exc}"


class StatCard(QLabel):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self._value = "—"
        self.setMinimumWidth(140)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setWordWrap(True)
        self._paint()
        self.setStyleSheet(
            """
            QLabel {
                background: #1e1e2e;
                color: #cdd6f4;
                border: 1px solid #313244;
                border-radius: 10px;
                padding: 12px 14px;
            }
            """
        )

    def set_value(self, value: str, subtitle: str | None = None) -> None:
        self._value = value
        self._subtitle = subtitle
        self._paint()

    def _paint(self) -> None:
        sub = getattr(self, "_subtitle", None)
        sub_html = f'<div style="color:#a6adc8;font-size:11px;margin-top:2px;">{sub}</div>' if sub else ""
        self.setText(
            f'<div style="color:#89b4fa;font-size:11px;font-weight:600;letter-spacing:0.4px;">'
            f"{self._title.upper()}</div>"
            f'<div style="color:#cdd6f4;font-size:20px;font-weight:700;margin-top:4px;">'
            f"{self._value}</div>{sub_html}"
        )


class MonitorWindow(QMainWindow):
    def __init__(self, output_dir: Path | None = None) -> None:
        super().__init__()
        self.output_dir = Path(output_dir or DEFAULT_OUTPUT)
        self.setWindowTitle(f"{APP_NAME}  ·  {self.output_dir}")
        self.resize(1180, 760)
        self.setMinimumSize(900, 560)

        self._build_ui()
        self._apply_dark_theme()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(REFRESH_MS)
        self.refresh()

    # --- UI construction -------------------------------------------------
    def _build_ui(self) -> None:
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setIconSize(toolbar.iconSize())
        self.addToolBar(toolbar)

        act_refresh = QAction("Refresh now", self)
        act_refresh.triggered.connect(self.refresh)
        toolbar.addAction(act_refresh)

        act_export = QAction("Export once", self)
        act_export.triggered.connect(self.run_export_once)
        toolbar.addAction(act_export)

        act_folder = QAction("Open export folder", self)
        act_folder.triggered.connect(self.open_export_folder)
        toolbar.addAction(act_folder)

        act_log = QAction("Open log", self)
        act_log.triggered.connect(self.open_log)
        toolbar.addAction(act_log)

        act_task = QAction("Task status", self)
        act_task.triggered.connect(self.show_task_status)
        toolbar.addAction(act_task)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 10)
        root.setSpacing(12)

        # Header
        header = QHBoxLayout()
        title = QLabel(APP_NAME)
        title.setStyleSheet("font-size: 22px; font-weight: 700; color: #cdd6f4;")
        header.addWidget(title)
        header.addStretch(1)
        self.lbl_status = QLabel("Status: —")
        self.lbl_status.setStyleSheet(
            "font-size: 13px; font-weight: 600; padding: 6px 12px; "
            "border-radius: 8px; background: #313244; color: #cdd6f4;"
        )
        header.addWidget(self.lbl_status)
        root.addLayout(header)

        self.lbl_meta = QLabel("")
        self.lbl_meta.setStyleSheet("color: #a6adc8; font-size: 12px;")
        self.lbl_meta.setWordWrap(True)
        root.addWidget(self.lbl_meta)

        # Stat cards
        cards = QHBoxLayout()
        cards.setSpacing(10)
        self.card_tracked = StatCard("Tracked")
        self.card_changed = StatCard("Last changed")
        self.card_size = StatCard("Export size")
        self.card_msgs = StatCard("Messages")
        self.card_tools = StatCard("Tool I/O")
        self.card_updated = StatCard("Last scan")
        for card in (
            self.card_tracked,
            self.card_changed,
            self.card_size,
            self.card_msgs,
            self.card_tools,
            self.card_updated,
        ):
            cards.addWidget(card)
        root.addLayout(cards)

        # Search + table / log splitter
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Filter:"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by title, session id, model, path…")
        self.search.textChanged.connect(self.apply_filter)
        search_row.addWidget(self.search, 1)
        self.lbl_count = QLabel("")
        self.lbl_count.setStyleSheet("color: #a6adc8;")
        search_row.addWidget(self.lbl_count)
        root.addLayout(search_row)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            [
                "Title",
                "Kind",
                "Session ID",
                "Model",
                "User",
                "Asst",
                "Tools",
                "Size",
                "Updated",
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)
        self.table.doubleClicked.connect(self.open_selected_export)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 9):
            header_view.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        splitter.addWidget(self.table)

        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addWidget(QLabel("Recent watcher log"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(10)
        self.log_view.setFont(mono)
        bottom_layout.addWidget(self.log_view)
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

        # Footer buttons
        footer = QHBoxLayout()
        btn_open = QPushButton("Open selected export")
        btn_open.clicked.connect(self.open_selected_export)
        footer.addWidget(btn_open)
        btn_reveal = QPushButton("Reveal in Explorer")
        btn_reveal.clicked.connect(self.reveal_selected)
        footer.addWidget(btn_reveal)
        footer.addStretch(1)
        btn_copy = QPushButton("Copy path")
        btn_copy.clicked.connect(self.copy_selected_path)
        footer.addWidget(btn_copy)
        root.addLayout(footer)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Ready")

        # row data cache: row -> record dict
        self._records: list[dict[str, Any]] = []
        self._all_rows: list[dict[str, Any]] = []

    def _apply_dark_theme(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        app.setStyle("Fusion")
        palette = QPalette()
        bg = QColor("#11111b")
        base = QColor("#1e1e2e")
        alt = QColor("#181825")
        text = QColor("#cdd6f4")
        highlight = QColor("#89b4fa")
        palette.setColor(QPalette.ColorRole.Window, bg)
        palette.setColor(QPalette.ColorRole.WindowText, text)
        palette.setColor(QPalette.ColorRole.Base, base)
        palette.setColor(QPalette.ColorRole.AlternateBase, alt)
        palette.setColor(QPalette.ColorRole.Text, text)
        palette.setColor(QPalette.ColorRole.Button, base)
        palette.setColor(QPalette.ColorRole.ButtonText, text)
        palette.setColor(QPalette.ColorRole.Highlight, highlight)
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#11111b"))
        palette.setColor(QPalette.ColorRole.ToolTipBase, base)
        palette.setColor(QPalette.ColorRole.ToolTipText, text)
        app.setPalette(palette)
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #11111b; color: #cdd6f4; }
            QToolBar { background: #181825; border-bottom: 1px solid #313244; spacing: 8px; padding: 4px; }
            QToolBar QToolButton {
                background: #313244; color: #cdd6f4; border-radius: 6px;
                padding: 6px 10px; margin: 2px;
            }
            QToolBar QToolButton:hover { background: #45475a; }
            QLineEdit {
                background: #1e1e2e; border: 1px solid #313244; border-radius: 6px;
                padding: 6px 8px; color: #cdd6f4;
            }
            QTableWidget {
                background: #1e1e2e; gridline-color: #313244; border: 1px solid #313244;
                border-radius: 8px; selection-background-color: #89b4fa; selection-color: #11111b;
            }
            QHeaderView::section {
                background: #181825; color: #a6adc8; padding: 6px; border: none;
                border-bottom: 1px solid #313244; border-right: 1px solid #313244;
            }
            QPlainTextEdit {
                background: #181825; border: 1px solid #313244; border-radius: 8px;
                color: #a6adc8;
            }
            QPushButton {
                background: #313244; color: #cdd6f4; border: none; border-radius: 6px;
                padding: 8px 14px;
            }
            QPushButton:hover { background: #45475a; }
            QStatusBar { background: #181825; color: #a6adc8; }
            QSplitter::handle { background: #313244; height: 3px; }
            """
        )

    # --- data loading ----------------------------------------------------
    def paths(self) -> dict[str, Path]:
        d = self.output_dir
        return {
            "state": d / ".export_state.json",
            "status": d / "watcher.status.json",
            "log": d / "watcher.log",
            "manifest": d / "MANIFEST.txt",
        }

    def refresh(self) -> None:
        p = self.paths()
        status = load_json(p["status"])
        state = load_json(p["state"])
        sources: dict[str, Any] = state.get("sources") or {}

        # Watcher status
        status_name = str(status.get("status") or status.get("state") or "unknown")
        pid = status.get("pid")
        alive = process_alive(int(pid)) if pid else False
        if status_name.lower() in ("running",) and alive:
            badge = f"● LIVE  ·  pid {pid}"
            color = "#a6e3a1"
            bg = "#1e3a2f"
        elif status_name.lower() in ("running",) and not alive:
            badge = f"○ STALE  ·  pid {pid or '—'} not running"
            color = "#f9e2af"
            bg = "#3a321e"
        elif status_name.lower() in ("completed",):
            badge = "○ IDLE (last run completed)"
            color = "#89b4fa"
            bg = "#1e2a3a"
        elif status_name.lower() in ("error", "failed"):
            badge = f"● ERROR  ·  {status.get('error') or status.get('message') or ''}"
            color = "#f38ba8"
            bg = "#3a1e24"
        else:
            badge = f"○ {status_name.upper()}"
            color = "#a6adc8"
            bg = "#313244"
        self.lbl_status.setText(badge)
        self.lbl_status.setStyleSheet(
            f"font-size: 13px; font-weight: 600; padding: 6px 12px; "
            f"border-radius: 8px; background: {bg}; color: {color};"
        )

        tracked = status.get("tracked")
        if tracked is None:
            tracked = len(sources)
        changed = status.get("changed")
        if changed is None:
            summaries = status.get("summaries") or []
            changed_txt = "—"
            for s in summaries:
                if "changed=" in str(s):
                    try:
                        changed_txt = str(s).split("changed=")[1].split()[0]
                    except Exception:
                        pass
            changed_display = changed_txt
        else:
            changed_display = str(changed)

        total_bytes = 0
        total_user = total_asst = total_tools = 0
        rows: list[dict[str, Any]] = []
        for source_key, rec in sources.items():
            if not isinstance(rec, dict):
                continue
            counts = rec.get("counts") or {}
            b = int(rec.get("bytes") or 0)
            total_bytes += b
            if rec.get("kind") == "prompt-history":
                u = int(counts.get("prompts") or 0)
                a = 0
                t = 0
            else:
                u = int(counts.get("user") or 0)
                a = int(counts.get("assistant") or 0)
                t = int(counts.get("tool_call") or 0) + int(counts.get("tool_output") or 0)
            total_user += u
            total_asst += a
            total_tools += t
            rows.append(
                {
                    "title": rec.get("title") or "Untitled",
                    "kind": rec.get("kind") or "session",
                    "session_id": rec.get("session_id") or "",
                    "model": rec.get("model") or "",
                    "user": u,
                    "assistant": a,
                    "tools": t,
                    "bytes": b,
                    "updated": rec.get("updated") or rec.get("created") or "",
                    "output": rec.get("output") or "",
                    "source": rec.get("source") or source_key,
                    "cwd": rec.get("cwd") or "",
                    "counts": counts,
                }
            )

        # Sort by updated desc by default for display rebuild
        rows.sort(key=lambda r: r.get("updated") or "", reverse=True)
        self._all_rows = rows
        self.apply_filter()

        self.card_tracked.set_value(str(tracked), f"{len(rows)} in state file")
        self.card_changed.set_value(str(changed_display), "this scan")
        self.card_size.set_value(human_bytes(total_bytes), f"{len(rows)} files")
        self.card_msgs.set_value(
            str(total_user + total_asst),
            f"user {total_user} · asst {total_asst}",
        )
        self.card_tools.set_value(str(total_tools), "calls + outputs")
        self.card_updated.set_value(
            short_time(status.get("scan_finished_at") or status.get("updated_at") or state.get("updated_at")),
            f"mode: {status.get('mode') or '—'}",
        )

        self.lbl_meta.setText(
            f"Export dir: {self.output_dir}    ·    "
            f"Watcher started: {short_time(status.get('process_started_at'))}    ·    "
            f"Auto-refresh every {REFRESH_MS // 1000}s"
        )

        # Log tail
        log_path = p["log"]
        log_text = ""
        if log_path.exists():
            try:
                raw = log_path.read_text(encoding="utf-8", errors="replace")
                lines = raw.splitlines()
                log_text = "\n".join(lines[-80:])
            except OSError as exc:
                log_text = f"(could not read log: {exc})"
        else:
            log_text = f"(no log yet at {log_path})"
        # Keep scroll near bottom if already near bottom
        sb = self.log_view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 20
        self.log_view.setPlainText(log_text)
        if at_bottom:
            sb.setValue(sb.maximum())

        self.status.showMessage(
            f"Refreshed {datetime.now().strftime('%H:%M:%S')}  ·  "
            f"state mtime {short_time(state.get('updated_at'))}  ·  "
            f"{human_bytes(sum(r['bytes'] for r in rows))} total"
        )

    def apply_filter(self) -> None:
        needle = (self.search.text() or "").strip().lower()
        if needle:
            filtered = [
                r
                for r in self._all_rows
                if needle
                in " ".join(
                    [
                        str(r.get("title") or ""),
                        str(r.get("session_id") or ""),
                        str(r.get("model") or ""),
                        str(r.get("kind") or ""),
                        str(r.get("output") or ""),
                        str(r.get("cwd") or ""),
                        str(r.get("source") or ""),
                    ]
                ).lower()
            ]
        else:
            filtered = list(self._all_rows)

        self._records = filtered
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(filtered))
        for row, rec in enumerate(filtered):
            values = [
                rec["title"],
                rec["kind"],
                rec["session_id"],
                rec["model"],
                str(rec["user"]),
                str(rec["assistant"]),
                str(rec["tools"]),
                human_bytes(rec["bytes"]),
                short_time(rec["updated"]),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col in (4, 5, 6):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    # numeric sort
                    try:
                        item.setData(Qt.ItemDataRole.UserRole, int(value))
                    except ValueError:
                        pass
                elif col == 7:
                    item.setData(Qt.ItemDataRole.UserRole, int(rec["bytes"] or 0))
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                item.setData(Qt.ItemDataRole.UserRole + 1, rec.get("output") or "")
                self.table.setItem(row, col, item)
        self.table.setSortingEnabled(True)
        self.lbl_count.setText(f"{len(filtered)} / {len(self._all_rows)} exports")

    # --- actions ---------------------------------------------------------
    def _selected_output(self) -> Path | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        path = item.data(Qt.ItemDataRole.UserRole + 1)
        if not path:
            # fallback via records
            if 0 <= row < len(self._records):
                path = self._records[row].get("output")
        if not path:
            return None
        return Path(str(path))

    def open_selected_export(self) -> None:
        path = self._selected_output()
        if path is None:
            QMessageBox.information(self, APP_NAME, "Select an export row first.")
            return
        if not path.exists():
            QMessageBox.warning(self, APP_NAME, f"File not found:\n{path}")
            return
        os.startfile(str(path))  # type: ignore[attr-defined]

    def reveal_selected(self) -> None:
        path = self._selected_output()
        if path is None or not path.exists():
            # reveal folder instead
            if self.output_dir.exists():
                os.startfile(str(self.output_dir))  # type: ignore[attr-defined]
            return
        subprocess.run(
            ["explorer", "/select,", str(path)],
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def copy_selected_path(self) -> None:
        path = self._selected_output()
        if path is None:
            QMessageBox.information(self, APP_NAME, "Select an export row first.")
            return
        QGuiApplication.clipboard().setText(str(path))
        self.status.showMessage(f"Copied: {path}", 3000)

    def open_export_folder(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.output_dir))  # type: ignore[attr-defined]

    def open_log(self) -> None:
        log = self.paths()["log"]
        if not log.exists():
            QMessageBox.information(self, APP_NAME, f"No log yet:\n{log}")
            return
        os.startfile(str(log))  # type: ignore[attr-defined]

    def run_export_once(self) -> None:
        if not EXPORTER.exists():
            QMessageBox.warning(self, APP_NAME, f"Exporter not found:\n{EXPORTER}")
            return
        self.status.showMessage("Running one-shot export…")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            completed = subprocess.run(
                [sys.executable, str(EXPORTER), "--once", "--output-dir", str(self.output_dir)],
                capture_output=True,
                text=True,
                timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            msg = (completed.stdout or completed.stderr or "").strip() or f"exit {completed.returncode}"
            if completed.returncode == 0:
                self.status.showMessage(f"Export done: {msg}", 5000)
            else:
                QMessageBox.warning(self, APP_NAME, f"Export failed (exit {completed.returncode}):\n{msg}")
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not run exporter:\n{exc}")
        finally:
            QApplication.restoreOverrideCursor()
            self.refresh()

    def show_task_status(self) -> None:
        state = task_state()
        status = load_json(self.paths()["status"])
        pid = status.get("pid")
        alive = process_alive(int(pid)) if pid else False
        QMessageBox.information(
            self,
            "Scheduled task",
            f"Task name: {TASK_NAME}\n"
            f"Task state: {state}\n"
            f"Watcher PID: {pid or '—'}\n"
            f"Process alive: {alive}\n"
            f"Status file: {self.paths()['status']}\n"
            f"Exporter: {EXPORTER}",
        )


def main() -> int:
    # Allow override: grok_export_monitor.exe --output-dir D:\path
    output_dir = DEFAULT_OUTPUT
    args = sys.argv[1:]
    if "--output-dir" in args:
        i = args.index("--output-dir")
        if i + 1 < len(args):
            output_dir = Path(args[i + 1])
    elif args and not args[0].startswith("-"):
        output_dir = Path(args[0])

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    win = MonitorWindow(output_dir)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
