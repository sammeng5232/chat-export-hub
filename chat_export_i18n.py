#!/usr/bin/env python3
"""English / Chinese strings for Chat Export Hub."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HOME = Path.home()
CONFIG_PATH = HOME / ".grok" / "tools" / "chat_export_hub_settings.json"

LANG_EN = "en"
LANG_ZH = "zh"
SUPPORTED = (LANG_EN, LANG_ZH)

# key -> {en, zh}
STRINGS: dict[str, dict[str, str]] = {
    # App
    "app_name": {"en": "Chat Export Hub", "zh": "对话导出中心"},
    "language": {"en": "Language", "zh": "语言"},
    "lang_en": {"en": "English", "zh": "English"},
    "lang_zh": {"en": "中文", "zh": "中文"},
    # Toolbar
    "act_refresh": {"en": "Refresh", "zh": "刷新"},
    "act_export_selected": {"en": "Export selected", "zh": "导出当前"},
    "act_export_all": {"en": "Export all once", "zh": "全部导出一次"},
    "act_open_folder": {"en": "Open folder", "zh": "打开文件夹"},
    "act_open_log": {"en": "Open log", "zh": "打开日志"},
    "act_task_status": {"en": "Task status", "zh": "任务状态"},
    # Sidebar
    "agents": {"en": "Agents", "zh": "代理"},
    "all_agents": {"en": "◉  All agents", "zh": "◉  全部代理"},
    "auto_refresh": {"en": "Auto-refresh", "zh": "自动刷新"},
    "sidebar_hint": {
        "en": "Lightweight mode v{version}\nData every {seconds}s\nAdd agents in chat_export_agents.py",
        "zh": "轻量模式 v{version}\n每 {seconds} 秒检查数据\n在 chat_export_agents.py 中添加代理",
    },
    # Header / status
    "status_prefix": {"en": "Status: —", "zh": "状态：—"},
    "title_all": {"en": "{app}  ·  All agents", "zh": "{app}  ·  全部代理"},
    "title_agent": {"en": "{app}  ·  {name}", "zh": "{app}  ·  {name}"},
    "badge_all_live": {
        "en": "● ALL LIVE  ·  {n}/{total}",
        "zh": "● 全部运行中  ·  {n}/{total}",
    },
    "badge_partial": {
        "en": "● PARTIAL  ·  {n}/{total} live",
        "zh": "● 部分运行  ·  {n}/{total} 在线",
    },
    "badge_watchers": {
        "en": "○ WATCHERS  ·  {n}/{total} live",
        "zh": "○ 监视器  ·  {n}/{total} 在线",
    },
    "badge_live_pid": {"en": "● LIVE  ·  pid {pid}", "zh": "● 运行中  ·  PID {pid}"},
    "badge_stale_pid": {"en": "○ STALE  ·  pid {pid}", "zh": "○ 已过期  ·  PID {pid}"},
    "badge_status": {"en": "○ {status}", "zh": "○ {status}"},
    # Agent chips
    "chip_live": {
        "en": "{short}: LIVE · {tracked} · Δ{changed}",
        "zh": "{short}：运行中 · {tracked} · Δ{changed}",
    },
    "chip_stale": {
        "en": "{short}: STALE · {n} files",
        "zh": "{short}：已过期 · {n} 个文件",
    },
    "chip_error": {"en": "{short}: ERROR", "zh": "{short}：错误"},
    "chip_files": {
        "en": "{short}: {n} files · {scan}",
        "zh": "{short}：{n} 个文件 · {scan}",
    },
    "chip_nodata": {"en": "{short}: no data", "zh": "{short}：无数据"},
    # Stat cards
    "card_agents": {"en": "Agents", "zh": "代理数"},
    "card_tracked": {"en": "Tracked", "zh": "已跟踪"},
    "card_changed": {"en": "Last Δ", "zh": "上次变更"},
    "card_size": {"en": "Size", "zh": "体积"},
    "card_msgs": {"en": "Messages", "zh": "消息"},
    "card_tools": {"en": "Tool I/O", "zh": "工具调用"},
    "card_scan": {"en": "Last scan", "zh": "上次扫描"},
    "sub_registered": {"en": "{n} registered", "zh": "已注册 {n}"},
    "sub_in_view": {"en": "in view", "zh": "当前视图"},
    "sub_last_scan": {"en": "last scan", "zh": "上次扫描"},
    "sub_last_scan_sum": {"en": "last scan (sum)", "zh": "上次扫描（合计）"},
    "sub_exports": {"en": "{n} exports", "zh": "{n} 条导出"},
    "sub_user_asst": {"en": "user {u} · asst {a}", "zh": "用户 {u} · 助手 {a}"},
    "sub_calls_outputs": {"en": "calls + outputs", "zh": "调用 + 输出"},
    "sub_data_interval": {"en": "data {s}s", "zh": "数据间隔 {s}s"},
    # Table / filter
    "filter": {"en": "Filter:", "zh": "筛选："},
    "filter_placeholder": {
        "en": "Filter by agent, title, session id, kind, model, path…",
        "zh": "按代理、标题、会话 ID、类型、模型、路径筛选…",
    },
    "count_exports": {
        "en": "{shown} / {total} exports",
        "zh": "{shown} / {total} 条导出",
    },
    "col_agent": {"en": "Agent", "zh": "代理"},
    "col_location": {"en": "Location", "zh": "位置"},
    "col_title": {"en": "Title", "zh": "标题"},
    "col_kind": {"en": "Kind", "zh": "类型"},
    "col_session": {"en": "Session ID", "zh": "会话 ID"},
    "col_model": {"en": "Model", "zh": "模型"},
    "col_user": {"en": "User", "zh": "用户"},
    "col_asst": {"en": "Asst", "zh": "助手"},
    "col_tools": {"en": "Tools", "zh": "工具"},
    "col_size": {"en": "Size", "zh": "大小"},
    "col_updated": {"en": "Updated", "zh": "更新时间"},
    # Log / footer
    "log_label": {
        "en": "Watcher log (tail, updates only when log changes)",
        "zh": "监视器日志（仅在变更时更新尾部）",
    },
    "btn_open_export": {"en": "Open export", "zh": "打开导出"},
    "btn_reveal": {"en": "Reveal in Explorer", "zh": "在资源管理器中显示"},
    "btn_copy_path": {"en": "Copy path", "zh": "复制路径"},
    "exporting": {"en": "Exporting… {names}", "zh": "正在导出… {names}"},
    # Meta
    "meta_one": {
        "en": "Output: {output}    ·    Source: {source}    ·    Task: {task}    ·    {notes}",
        "zh": "输出：{output}    ·    来源：{source}    ·    任务：{task}    ·    {notes}",
    },
    "meta_all_suffix": {
        "en": "    ·    {n} agents registered",
        "zh": "    ·    已注册 {n} 个代理",
    },
    # Status bar
    "status_uptodate": {
        "en": "Up to date · {n} exports · checked {time}",
        "zh": "已是最新 · {n} 条导出 · 检查于 {time}",
    },
    "status_reload": {
        "en": "Data reload {ms:.0f} ms · {n} exports · {size} · {time}",
        "zh": "数据重载 {ms:.0f} ms · {n} 条导出 · {size} · {time}",
    },
    "status_copied": {"en": "Copied: {path}", "zh": "已复制：{path}"},
    "status_export_bg": {
        "en": "Running export in background…",
        "zh": "正在后台导出…",
    },
    "status_export_done": {"en": "Export finished", "zh": "导出完成"},
    "status_export_fail": {"en": "Export failed", "zh": "导出失败"},
    # Dialogs
    "select_row": {
        "en": "Select an export row first.",
        "zh": "请先选择一条导出记录。",
    },
    "file_not_found": {"en": "File not found:\n{path}", "zh": "找不到文件：\n{path}"},
    "no_log": {
        "en": "No watcher log found for this selection.",
        "zh": "当前选择没有监视器日志。",
    },
    "task_dialog_title": {"en": "Task / watcher status", "zh": "任务 / 监视器状态"},
    "export_running": {
        "en": "An export is already running.",
        "zh": "已有导出任务正在运行。",
    },
    "exporter_missing": {
        "en": "Exporter missing for: {names}",
        "zh": "缺少导出脚本：{names}",
    },
    "export_finished": {
        "en": "Export finished.\n\n{message}",
        "zh": "导出完成。\n\n{message}",
    },
    "export_failed": {
        "en": "Export failed ({agent}):\n\n{message}",
        "zh": "导出失败（{agent}）：\n\n{message}",
    },
    "no_log_yet": {"en": "(no log yet)", "zh": "（尚无日志）"},
    "no_logs": {"en": "(no logs)", "zh": "（无日志）"},
    "task_same": {"en": "(same task as above)", "zh": "（与上方同一任务）"},
    "lang_switched": {
        "en": "Language: English",
        "zh": "语言：中文",
    },
}

# Agent display names (optional overrides; short stays technical)
AGENT_NAMES: dict[str, dict[str, str]] = {
    "codex": {"en": "OpenAI Codex", "zh": "OpenAI Codex"},
    "claude": {"en": "Claude Code CLI", "zh": "Claude Code CLI"},
    "claude-gui": {"en": "Claude Code GUI", "zh": "Claude Code GUI"},
    "grok": {"en": "Grok CLI", "zh": "Grok CLI"},
    "opencode": {"en": "OpenCode", "zh": "OpenCode"},
    "kilocode": {"en": "Kilo Code", "zh": "Kilo Code"},
    "kirocode": {"en": "Kiro", "zh": "Kiro"},
    "workbuddy": {"en": "WorkBuddy", "zh": "WorkBuddy"},
    "workbuddy-cn": {"en": "WorkBuddy CN", "zh": "WorkBuddy CN"},
    "trae": {"en": "Trae", "zh": "Trae"},
    "trae-cn": {"en": "Trae CN", "zh": "Trae CN"},
    "qwencode": {"en": "Qwen Code", "zh": "Qwen Code"},
}


class I18n:
    def __init__(self, lang: str | None = None) -> None:
        self.lang = LANG_EN
        if lang in SUPPORTED:
            self.lang = lang
        else:
            self.lang = load_saved_lang()

    def set_lang(self, lang: str) -> None:
        if lang not in SUPPORTED:
            return
        self.lang = lang
        save_lang(lang)

    def t(self, key: str, **kwargs: Any) -> str:
        entry = STRINGS.get(key) or {}
        text = entry.get(self.lang) or entry.get(LANG_EN) or key
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, ValueError):
                return text
        return text

    def agent_name(self, agent_id: str, fallback: str) -> str:
        entry = AGENT_NAMES.get(agent_id) or {}
        return entry.get(self.lang) or entry.get(LANG_EN) or fallback

    def columns(self) -> tuple[str, ...]:
        return (
            self.t("col_agent"),
            self.t("col_location"),
            self.t("col_title"),
            self.t("col_kind"),
            self.t("col_session"),
            self.t("col_model"),
            self.t("col_user"),
            self.t("col_asst"),
            self.t("col_tools"),
            self.t("col_size"),
            self.t("col_updated"),
        )


def load_saved_lang() -> str:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        lang = data.get("language")
        if lang in SUPPORTED:
            return lang
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
        pass
    # Default: Chinese if Windows UI language is Chinese, else English
    try:
        import locale

        loc = (locale.getdefaultlocale()[0] or "").lower()
        if loc.startswith("zh"):
            return LANG_ZH
    except Exception:
        pass
    return LANG_EN


def save_lang(lang: str) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    data["language"] = lang
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# Module-level singleton used by the UI
i18n = I18n()
