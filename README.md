# Chat Export Hub

Unified desktop app for **live chat history exports** across multiple AI agents.

## Launch

- Shortcut: `~/Desktop/Apps/DIY/Chat Export Hub.lnk`
- Tools exe: `~/.grok/tools/ChatExportHub.exe` (do not copy the exe onto the Desktop)

## Built-in agents

| Agent | Export folder | Source |
|-------|---------------|--------|
| OpenAI Codex | `~/codex_chat_live_exports` | `~/.codex` (Codex Claude Chat Export Watcher) |
| Claude Code CLI | `~/claude_code_chat_live_exports` | `~/.claude` terminal + VS Code sessions (combined task + Claude Code Chat Export Splitter) |
| Claude Code GUI | `~/claude_code_gui_chat_live_exports` | `~/.claude` desktop-app sessions, entrypoint `claude-desktop` (Claude Code Chat Export Splitter) |
| Grok CLI | `~/grok_chat_live_exports` | `~/.grok` (Grok Chat Export Watcher) |
| OpenCode | `~/opencode_chat_live_exports` | `~/.local/share/opencode/opencode.db` — CLI + desktop (OpenCode Chat Export Watcher) |
| Kilo Code | `~/kilocode_chat_live_exports` | `~/.local/share/kilo/kilo.db` (Kilo CLI) + VS Code / Cursor `kilocode.kilo-code` globalStorage (Kilo Code Chat Export Watcher) |
| Kiro | `~/kiro_chat_live_exports` | `~/.kiro` + `%APPDATA%/Kiro` |
| WorkBuddy | `~/workbuddy_chat_live_exports` | `~/.workbuddy-ai` |
| WorkBuddy CN | `~/workbuddy_cn_chat_live_exports` | `~/.workbuddy/projects` |
| Trae | `~/trae_chat_live_exports` | Trae + TRAE SOLO `ModularData/ai-agent` |
| Trae CN | `~/trae_cn_chat_live_exports` | Trae CN + TRAE SOLO CN `ModularData/ai-agent` |
| Cline CLI | `~/cline_chat_live_exports` | `~/.cline/data/sessions` (Cline Chat Export Watcher) |
| Continue | `~/continue_chat_live_exports` | `~/.continue/sessions` (Continue Chat Export Watcher) |
| Pi Agent | `~/pi_chat_live_exports` | `~/.pi/agent/sessions` (Pi Chat Export Watcher) |
| Qwen Code | `~/qwen_chat_live_exports` | `~/.qwen/projects/<cwd>/chats` (Qwen Code Chat Export Watcher) |
| Cursor | `~/cursor_chat_live_exports` | Cursor IDE `User/globalStorage/state.vscdb` composers + `workspaceStorage` prompt history + cursor-agent CLI `~/.cursor` (Cursor Chat Export Watcher) |

## Watcher tasks

Scheduled tasks keep exports live (logon trigger + 5-minute revival, 30s scan interval):

| Task name | Installer |
|-----------|------------|
| Codex Claude Chat Export Watcher | `~/.codex/tools/install_chat_export_task.ps1` |
| Claude Code Chat Export Splitter | `install_claude_code_split_task.ps1` (derives the Claude Code CLI / GUI states from the combined exporter's output every 15s) |
| Grok Chat Export Watcher | `install_grok_chat_export_task.ps1` |
| OpenCode Chat Export Watcher | `install_opencode_chat_export_task.ps1` |
| Kilo Code Chat Export Watcher | `install_kilo_chat_export_task.ps1` |
| Cline Chat Export Watcher | `install_cline_chat_export_task.ps1` |
| Continue Chat Export Watcher | `install_continue_chat_export_task.ps1` |
| Pi Chat Export Watcher | `install_pi_chat_export_task.ps1` |
| Qwen Code Chat Export Watcher | `install_qwen_chat_export_task.ps1` |
| Cursor Chat Export Watcher | `install_cursor_chat_export_task.ps1` |

Agents without a watcher task export only when triggered from the hub UI.

## Remote sources: WSL distros + Oracle VMs + remote SSH machines

Every exporter scans the local Windows home **plus** remote agent homes and
merges the results into the same export folders:

- **WSL** — every distro registered for the current user, read through
  `\\wsl.localhost\<distro>` UNC paths (auto-discovered; SQLite databases are
  staged locally because WAL dbs cannot be read over 9P). Tags: `wsl-<distro>`.
- **Oracle VM (VirtualBox)** — every VM that is running and has a key-auth
  `Host <vm-name>` entry in `~/.ssh/config` (e.g. `Host vm1`). Agent dirs are
  `tar`-pulled over SSH into `%LOCALAPPDATA%\ChatExportHub\staging\vbox-<vm>\`
  at most every 5 minutes, SQLite WALs are folded into the staged copies, and
  the staging lock keeps the many watcher processes from pulling the same VM
  twice. No VM passwords are stored anywhere. Tags: `vbox-<vm>`.
  Works for Windows and Linux guests; the guest profile resolves via `whoami`.
- **Remote SSH machines** (e.g. `scrp` = scrp-login.econ.cuhk.edu.hk) —
  aliases listed in `chat_export_remote_hosts.json` (beside the exporters;
  git-ignored) or `CHAT_EXPORT_REMOTE_HOSTS`. Each alias needs a key-auth
  `Host` entry in `~/.ssh/config`. Sync every 15 min (compressed tar) with a
  10-minute failure backoff. Tags: `ssh-<alias>`.
  - SCRP note: `scrp`, `scrp1` and `scrp2` are login nodes that share one
    home filesystem, so the single `scrp` alias covers histories from all
    three; extra aliases would only duplicate every export. SCRP is reachable
    only from the CUHK network / VPN — the outage-safe retention keeps
    existing exports while off-campus and resumes syncing automatically.

Remote sessions carry a `@wsl-…` / `@vbox-…` / `@ssh-…` marker in their source
keys (and usually in the file name), show a **WSL: … / VM: … / SSH: …** label
in the hub's Location column, and survive outages: while a remote source is
unreachable its existing exports are retained, not pruned. Machines that are
only reachable from campus/VPN simply resume syncing once the network path
exists.

To cover a new VirtualBox VM: install Guest SSH with key auth, add a NAT port
forward, and append a `Host <vm-name>` block to `~/.ssh/config` named exactly
like the VirtualBox VM. To cover another server: add the key-auth ssh config
entry plus the alias in `chat_export_remote_hosts.json`. No exporter changes
needed. Tune with `CHAT_EXPORT_VBOX_SYNC_INTERVAL` (default 300) and
`CHAT_EXPORT_REMOTE_SYNC_INTERVAL` (default 900) if desired.

## UI features

- Sidebar: **All agents** or one agent
- Live status chips per agent (LIVE / STALE / counts)
- Combined stats: tracked files, size, messages, tool I/O
- Filterable table of every export (Location column: Local / WSL / VM / SSH)
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
- `chat_export_common.py` — shared exporter helpers incl. WSL/VM remote-home discovery, SQLite staging, and outage-safe pruning
- `export_grok_chats_live.py` — Grok exporter
- `export_opencode_chats_live.py` — OpenCode exporter (session/message/part SQLite schema, reused by Kilo CLI)
- `export_workbuddy_chats_live.py` — WorkBuddy / WorkBuddy CN
- `export_kilocode_chats_live.py` — Kilo Code (CLI `kilo.db` + VS Code extension storage)
- `export_kiro_chats_live.py` — Kiro
- `export_trae_chats_live.py` — Trae / Trae CN
- `export_cline_chats_live.py` — Cline CLI
- `export_continue_chats_live.py` — Continue extension
- `export_pi_chats_live.py` — pi agent
- `export_qwen_chats_live.py` — Qwen Code CLI
- `export_cursor_chats_live.py` — Cursor (IDE composer/bubble store, workspace `aiService` history, cursor-agent CLI sessions and agent transcripts)
- `split_claude_code_exports.py` — Claude Code CLI / GUI splitter (post-processes the combined exporter's output by session entrypoint: `cli` / `sdk-cli` / `claude-vscode` stay CLI, `claude-desktop` becomes GUI)
- `grok_export_monitor.py` — Grok export monitor
- `install_*_chat_export_task.ps1` — scheduled watcher installers
- `build_chat_export_hub.ps1` — PyInstaller build + sign + shortcuts
- `~/.codex/tools/export_codex_claude_chats_live.py` — Codex + Claude exporter (lives with the Codex install; currently a loader that exec's the last verified bytecode build because the source was lost — it still picks up WSL/VM support through `chat_export_common`)
