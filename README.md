# Chat Export Hub

Unified desktop app for **live chat history exports** across multiple AI agents.

## Launch

- Shortcut: `~/Desktop/Apps/DIY/Chat Export Hub.lnk`
- Tools exe: `~/.grok/tools/ChatExportHub.exe` (do not copy the exe onto the Desktop)

## Built-in agents

| Agent | Export folder | Source |
|-------|---------------|--------|
| OpenAI Codex | `~/codex_chat_live_exports` | `~/.codex` (Codex Claude Chat Export Watcher) |
| Claude Code | `~/claude_code_chat_live_exports` | `~/.claude` (same combined task) |
| Grok CLI | `~/grok_chat_live_exports` | `~/.grok` (Grok Chat Export Watcher) |
| OpenCode | `~/opencode_chat_live_exports` | `~/.local/share/opencode/opencode.db` — CLI + desktop (OpenCode Chat Export Watcher) |
| Kilo Code | `~/kilocode_chat_live_exports` | `~/.local/share/kilo/kilo.db` (Kilo CLI) + VS Code / Cursor `kilocode.kilo-code` globalStorage (Kilo Code Chat Export Watcher) |
| Kiro | `~/kiro_chat_live_exports` | `~/.kiro` + `%APPDATA%/Kiro` |
| WorkBuddy | `~/workbuddy_chat_live_exports` | `~/.workbuddy-ai` |
| WorkBuddy CN | `~/workbuddy_cn_chat_live_exports` | `~/.workbuddy/projects` |
| Trae | `~/trae_chat_live_exports` | Trae + TRAE SOLO `ModularData/ai-agent` |
| Trae CN | `~/trae_cn_chat_live_exports` | Trae CN + TRAE SOLO CN `ModularData/ai-agent` |

## Watcher tasks

Scheduled tasks keep exports live (logon trigger + 5-minute revival, 30s scan interval):

| Task name | Installer |
|-----------|------------|
| Codex Claude Chat Export Watcher | `~/.codex/tools/install_chat_export_task.ps1` |
| Grok Chat Export Watcher | `install_grok_chat_export_task.ps1` |
| OpenCode Chat Export Watcher | `install_opencode_chat_export_task.ps1` |
| Kilo Code Chat Export Watcher | `install_kilo_chat_export_task.ps1` |

Agents without a watcher task export only when triggered from the hub UI.

## UI features

- Sidebar: **All agents** or one agent
- Live status chips per agent (LIVE / STALE / counts)
- Combined stats: tracked files, size, messages, tool I/O
- Filterable table of every export
- Watcher log tail
- **Export selected / Export all once**
- Open export, reveal in Explorer, copy path, task status

## Add another agent later

1. Write (or wire) an exporter that produces:

   - `~/…_chat_live_exports/.export_state.json`  
     with a `sources` map of records containing at least  
     `title`, `output`, `bytes`, `counts`, `session_id` / `source_id`, `updated`

2. Edit `chat_export_agents.py` and append an `AgentSpec(...)`.

3. Rebuild:

   ```powershell
   ~/.grok/tools/build_chat_export_hub.ps1
   ```

No UI code changes required for simple agents that share the same state layout.

## Source

- `chat_export_hub.py` — GUI
- `chat_export_agents.py` — agent registry
- `chat_export_i18n.py` — strings
- `chat_export_common.py` — shared exporter helpers
- `export_grok_chats_live.py` — Grok exporter
- `export_opencode_chats_live.py` — OpenCode exporter (session/message/part SQLite schema, reused by Kilo CLI)
- `export_workbuddy_chats_live.py` — WorkBuddy / WorkBuddy CN
- `export_kilocode_chats_live.py` — Kilo Code (CLI `kilo.db` + VS Code extension storage)
- `export_kiro_chats_live.py` — Kiro
- `export_trae_chats_live.py` — Trae / Trae CN
- `grok_export_monitor.py` — Grok export monitor
- `install_*_chat_export_task.ps1` — scheduled watcher installers
- `build_chat_export_hub.ps1` — PyInstaller build + sign + shortcuts
- `~/.codex/tools/export_codex_claude_chats_live.py` — Codex + Claude exporter (lives with the Codex install)
