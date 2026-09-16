#!/usr/bin/env python3
"""Agent registry for the Chat Export Hub.

Add a new AI agent by appending an AgentSpec below. The UI and export-once
actions discover agents from ``AGENTS`` — no other code changes required for
simple cases (shared export-state layout).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


HOME = Path.home()


@dataclass(frozen=True)
class AgentSpec:
    """One chat-history source that produces live TXT exports."""

    id: str
    name: str
    # Short label for badges / tabs
    short: str
    color: str  # CSS-ish hex used in the UI
    output_dir: Path
    # Optional home / source root (for display and future use)
    source_home: Path | None = None
    # State + status artifacts under output_dir (or absolute overrides)
    state_name: str = ".export_state.json"
    status_path: Path | None = None  # default: output_dir / watcher.status.json
    log_path: Path | None = None  # default: output_dir / watcher.log
    # One-shot exporter
    exporter: Path | None = None
    export_args: tuple[str, ...] = ()
    # Scheduled task name (Windows), if any
    task_name: str | None = None
    # How to parse per-record counts into user/assistant/tools
    # (handled in the hub; this flag only documents known layout)
    layout: str = "generic"  # codex | claude | grok | generic
    enabled: bool = True
    notes: str = ""

    def state_file(self) -> Path:
        return self.output_dir / self.state_name

    def resolved_status(self) -> Path:
        if self.status_path is not None:
            return self.status_path
        return self.output_dir / "watcher.status.json"

    def resolved_log(self) -> Path:
        if self.log_path is not None:
            return self.log_path
        return self.output_dir / "watcher.log"


# ---------------------------------------------------------------------------
# Built-in agents. To add Cursor / Continue / OpenCode / etc later:
#   1. Write or wire an exporter that writes .export_state.json like the others
#   2. Append another AgentSpec here
# ---------------------------------------------------------------------------

TOOLS = HOME / ".grok" / "tools"
CODEX_CLAUDE_EXPORTER = HOME / ".codex" / "tools" / "export_codex_claude_chats_live.py"
CLAUDE_SPLITTER = TOOLS / "split_claude_code_exports.py"
GROK_EXPORTER = TOOLS / "export_grok_chats_live.py"
OPENCODE_EXPORTER = TOOLS / "export_opencode_chats_live.py"
WORKBUDDY_EXPORTER = TOOLS / "export_workbuddy_chats_live.py"
KILO_EXPORTER = TOOLS / "export_kilocode_chats_live.py"
KIRO_EXPORTER = TOOLS / "export_kiro_chats_live.py"
TRAE_EXPORTER = TOOLS / "export_trae_chats_live.py"
CLINE_EXPORTER = TOOLS / "export_cline_chats_live.py"
CONTINUE_EXPORTER = TOOLS / "export_continue_chats_live.py"
PI_EXPORTER = TOOLS / "export_pi_chats_live.py"
QWEN_EXPORTER = TOOLS / "export_qwen_chats_live.py"
CURSOR_EXPORTER = TOOLS / "export_cursor_chats_live.py"
# Shared watcher status for the combined Codex+Claude scheduled task
CODEX_CLAUDE_STATUS = HOME / "codex_chat_live_exports" / "watcher.status.json"
CODEX_CLAUDE_LOG = HOME / "codex_chat_live_exports" / "watcher.log"
# Splitter task status/log (feeds the Claude Code CLI / GUI derived states)
CLAUDE_SPLIT_STATUS = HOME / "claude_code_chat_live_exports" / "watcher.status.json"
CLAUDE_SPLIT_LOG = HOME / "claude_code_chat_live_exports" / "watcher.log"


AGENTS: list[AgentSpec] = [
    AgentSpec(
        id="codex",
        name="OpenAI Codex",
        short="Codex",
        color="#10a37f",
        output_dir=HOME / "codex_chat_live_exports",
        source_home=HOME / ".codex",
        status_path=CODEX_CLAUDE_STATUS,
        log_path=CODEX_CLAUDE_LOG,
        exporter=CODEX_CLAUDE_EXPORTER,
        export_args=(
            "--codex-home",
            str(HOME / ".codex"),
            "--output-dir",
            str(HOME / "codex_chat_live_exports"),
            "--no-claude",
            "--once",
        ),
        task_name="Codex Claude Chat Export Watcher",
        layout="codex",
        notes="Sessions under ~/.codex/sessions and archived_sessions",
    ),
    AgentSpec(
        id="claude",
        name="Claude Code CLI",
        short="Claude CLI",
        color="#d97706",
        output_dir=HOME / "claude_code_chat_live_exports",
        source_home=HOME / ".claude",
        # Derived by the splitter from the combined exporter's raw state:
        # terminal (cli / sdk-cli) + VS Code extension (claude-vscode) sessions
        # plus the prompt history. Desktop-app sessions live in the GUI agent.
        state_name=".export_state.cli.json",
        status_path=CLAUDE_SPLIT_STATUS,
        log_path=CLAUDE_SPLIT_LOG,
        exporter=CLAUDE_SPLITTER,
        export_args=(
            "--role",
            "cli",
            "--once",
        ),
        task_name="Claude Code Chat Export Splitter",
        layout="claude",
        notes=(
            "Terminal + VS Code sessions under ~/.claude/projects "
            "(entrypoint cli / sdk-cli / claude-vscode) plus prompt history; "
            "raw exports come from the combined Codex+Claude watcher, the "
            "splitter task derives this agent's state"
        ),
    ),
    AgentSpec(
        id="claude-gui",
        name="Claude Code GUI",
        short="Claude GUI",
        color="#d97757",
        output_dir=HOME / "claude_code_gui_chat_live_exports",
        source_home=HOME / ".claude",
        status_path=CLAUDE_SPLIT_STATUS,
        log_path=CLAUDE_SPLIT_LOG,
        exporter=CLAUDE_SPLITTER,
        export_args=(
            "--role",
            "gui",
            "--once",
        ),
        task_name="Claude Code Chat Export Splitter",
        layout="claude",
        notes=(
            "Claude desktop app sessions under ~/.claude/projects "
            "(entrypoint claude-desktop), copied into their own export "
            "folder by the splitter task"
        ),
    ),
    AgentSpec(
        id="grok",
        name="Grok CLI",
        short="Grok",
        color="#8b5cf6",
        output_dir=HOME / "grok_chat_live_exports",
        source_home=HOME / ".grok",
        exporter=GROK_EXPORTER,
        export_args=(
            "--grok-home",
            str(HOME / ".grok"),
            "--output-dir",
            str(HOME / "grok_chat_live_exports"),
            "--once",
        ),
        task_name="Grok Chat Export Watcher",
        layout="grok",
        notes="Sessions under ~/.grok/sessions/<cwd>/<session-id>",
    ),
    AgentSpec(
        id="opencode",
        name="OpenCode",
        short="OpenCode",
        color="#22c55e",
        output_dir=HOME / "opencode_chat_live_exports",
        source_home=HOME / ".local" / "share" / "opencode",
        exporter=OPENCODE_EXPORTER,
        export_args=("--once",),
        task_name="OpenCode Chat Export Watcher",
        layout="generic",
        notes="CLI + desktop sessions in ~/.local/share/opencode/opencode.db",
    ),
    AgentSpec(
        id="kilocode",
        name="Kilo Code",
        short="Kilo",
        color="#f59e0b",
        output_dir=HOME / "kilocode_chat_live_exports",
        source_home=HOME / ".local" / "share" / "kilo",
        exporter=KILO_EXPORTER,
        export_args=("--once",),
        task_name="Kilo Code Chat Export Watcher",
        layout="generic",
        notes="Kilo CLI kilo.db (~/.local/share/kilo) + VS Code kilocode.kilo-code globalStorage",
    ),
    AgentSpec(
        id="kirocode",
        name="Kiro",
        short="Kiro",
        color="#06b6d4",
        output_dir=HOME / "kiro_chat_live_exports",
        source_home=HOME / ".kiro",
        exporter=KIRO_EXPORTER,
        export_args=("--once",),
        layout="generic",
        notes="~/.kiro sessions plus %APPDATA%/Kiro state.vscdb (empty-ok)",
    ),
    AgentSpec(
        id="workbuddy",
        name="WorkBuddy",
        short="WB",
        color="#3b82f6",
        output_dir=HOME / "workbuddy_chat_live_exports",
        source_home=HOME / ".workbuddy-ai",
        exporter=WORKBUDDY_EXPORTER,
        export_args=(
            "--home",
            str(HOME / ".workbuddy-ai"),
            "--output-dir",
            str(HOME / "workbuddy_chat_live_exports"),
            "--label",
            "workbuddy",
            "--once",
        ),
        layout="generic",
        notes="International WorkBuddy AI (~/.workbuddy-ai)",
    ),
    AgentSpec(
        id="workbuddy-cn",
        name="WorkBuddy CN",
        short="WB CN",
        color="#2563eb",
        output_dir=HOME / "workbuddy_cn_chat_live_exports",
        source_home=HOME / ".workbuddy",
        exporter=WORKBUDDY_EXPORTER,
        export_args=(
            "--home",
            str(HOME / ".workbuddy"),
            "--output-dir",
            str(HOME / "workbuddy_cn_chat_live_exports"),
            "--label",
            "workbuddy-cn",
            "--once",
        ),
        layout="generic",
        notes="WorkBuddy CN transcripts under ~/.workbuddy/projects",
    ),
    AgentSpec(
        id="trae",
        name="Trae",
        short="Trae",
        color="#64748b",
        output_dir=HOME / "trae_chat_live_exports",
        source_home=HOME / "AppData" / "Roaming" / "TRAE SOLO",
        exporter=TRAE_EXPORTER,
        export_args=(
            "--products",
            "Trae,TRAE SOLO",
            "--output-dir",
            str(HOME / "trae_chat_live_exports"),
            "--label",
            "trae",
            "--once",
        ),
        layout="generic",
        notes="Trae + TRAE SOLO ModularData ai-agent databases",
    ),
    AgentSpec(
        id="trae-cn",
        name="Trae CN",
        short="Trae CN",
        color="#0f766e",
        output_dir=HOME / "trae_cn_chat_live_exports",
        source_home=HOME / "AppData" / "Roaming" / "TRAE SOLO CN",
        exporter=TRAE_EXPORTER,
        export_args=(
            "--products",
            "Trae CN,TRAE SOLO CN",
            "--output-dir",
            str(HOME / "trae_cn_chat_live_exports"),
            "--label",
            "trae-cn",
            "--once",
        ),
        layout="generic",
        notes="Trae CN + TRAE SOLO CN encrypted chat databases",
    ),
    AgentSpec(
        id="cline",
        name="Cline CLI",
        short="Cline",
        color="#ef4444",
        output_dir=HOME / "cline_chat_live_exports",
        source_home=HOME / ".cline",
        exporter=CLINE_EXPORTER,
        export_args=("--once",),
        task_name="Cline Chat Export Watcher",
        layout="generic",
        notes="Cline CLI sessions under ~/.cline/data/sessions",
    ),
    AgentSpec(
        id="continue",
        name="Continue",
        short="Continue",
        color="#a855f7",
        output_dir=HOME / "continue_chat_live_exports",
        source_home=HOME / ".continue",
        exporter=CONTINUE_EXPORTER,
        export_args=("--once",),
        task_name="Continue Chat Export Watcher",
        layout="generic",
        notes="Continue extension sessions under ~/.continue/sessions",
    ),
    AgentSpec(
        id="pi",
        name="Pi Agent",
        short="Pi",
        color="#14b8a6",
        output_dir=HOME / "pi_chat_live_exports",
        source_home=HOME / ".pi",
        exporter=PI_EXPORTER,
        export_args=("--once",),
        task_name="Pi Chat Export Watcher",
        layout="generic",
        notes="pi agent sessions under ~/.pi/agent/sessions",
    ),
    AgentSpec(
        id="cursor",
        name="Cursor",
        short="Cursor",
        color="#94a3b8",
        output_dir=HOME / "cursor_chat_live_exports",
        source_home=HOME / ".cursor",
        exporter=CURSOR_EXPORTER,
        export_args=("--once",),
        task_name="Cursor Chat Export Watcher",
        layout="generic",
        notes=(
            "Cursor IDE composers in <Cursor>/User/globalStorage/state.vscdb "
            "plus workspaceStorage prompt history and cursor-agent CLI "
            "sessions under ~/.cursor"
        ),
    ),
    AgentSpec(
        id="qwencode",
        name="Qwen Code",
        short="Qwen",
        color="#6366f1",
        output_dir=HOME / "qwen_chat_live_exports",
        source_home=HOME / ".qwen",
        exporter=QWEN_EXPORTER,
        export_args=("--once",),
        task_name="Qwen Code Chat Export Watcher",
        layout="generic",
        notes="Qwen Code sessions under ~/.qwen/projects/<cwd>/chats",
    ),
]


def enabled_agents() -> list[AgentSpec]:
    return [a for a in AGENTS if a.enabled]


def agent_by_id(agent_id: str) -> AgentSpec | None:
    for agent in AGENTS:
        if agent.id == agent_id:
            return agent
    return None


def register_agent(spec: AgentSpec, *, replace: bool = False) -> None:
    """Runtime registration helper for plugins / future importers."""
    global AGENTS
    existing = [a for a in AGENTS if a.id == spec.id]
    if existing and not replace:
        raise ValueError(f"agent already registered: {spec.id}")
    if existing and replace:
        AGENTS = [spec if a.id == spec.id else a for a in AGENTS]
    else:
        AGENTS = list(AGENTS) + [spec]
