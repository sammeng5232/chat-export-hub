#!/usr/bin/env python3
"""Export Cursor chats live: IDE composers plus cursor-agent CLI sessions.

Three stores are merged into one export folder:

* ``<Cursor>/User/globalStorage/state.vscdb`` — the IDE chats. ``composerHeaders``
  indexes every composer, ``composerData:<id>`` holds the title and the ordered
  bubble list, and ``bubbleId:<composer>:<bubble>`` holds each message with its
  thinking block and tool call/result.
* ``<Cursor>/User/workspaceStorage/<ws>/state.vscdb`` — per-workspace prompt and
  generation history (``aiService.prompts`` / ``aiService.generations``) plus the
  legacy ``workbench.panel.aichat.view.aichat.chatdata`` pane store.
* ``~/.cursor`` — the cursor-agent CLI: ``chats/<hash>/<session>/meta.json`` for
  title/cwd and ``projects/<project>/agent-transcripts/<id>/<id>.jsonl`` for the
  turns. Transcripts whose id is already covered by an IDE composer are skipped;
  the composer store is the richer of the two.

Windows, Linux and macOS layouts are all probed, locally and across every WSL
distro / VirtualBox VM / remote SSH machine that ``wsl_agent_homes`` resolves.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Iterable

_TOOLS = Path(__file__).resolve().parent
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from chat_export_common import (
    atomic_write_json,
    atomic_write_text,
    clip_text,
    content_to_text,
    empty_counts,
    emit_block,
    epoch_to_iso,
    extract_user_query,
    file_fingerprint,
    filesystem_safe_output_path,
    first_iso_timestamp_prefix,
    load_json,
    now_iso,
    one_line,
    prune_removed_sources,
    reuse_or_replace_output,
    run_watcher_loop,
    scrub_internal_lines,
    should_skip_user_text,
    stage_wsl_sqlite,
    wsl_agent_homes,
    write_manifest,
)

# Cursor's "User" directory relative to a user profile, per platform.
USER_DIR_RELATIVES = (
    "AppData/Roaming/Cursor/User",
    ".config/Cursor/User",
    "Library/Application Support/Cursor/User",
)

# Bubble roles in the composer store; anything that is not a user bubble is
# rendered as assistant output (thinking, text, tool call, tool result).
BUBBLE_USER = 1


def parse_args() -> argparse.Namespace:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(home / "cursor_chat_live_exports"))
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--log-file")
    parser.add_argument("--status-file")
    return parser.parse_args()


def pretty(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except TypeError:
        return str(value)


def maybe_json(value: Any) -> Any:
    """Cursor stores tool args/results as JSON *strings*; unwrap for display."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in "{[":
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
    return value


def uri_to_path(uri: str) -> str:
    if not uri:
        return ""
    if not uri.startswith("file://"):
        return uri
    rest = urllib.parse.unquote(uri[len("file://"):]).lstrip("/")
    if re.match(r"^[A-Za-z]:", rest):
        return rest.replace("/", os.sep)
    return "/" + rest


def _lexical_text(node: Any) -> str:
    """Flatten a Lexical editor-state tree (Cursor's ``richText``) to text."""
    if isinstance(node, list):
        return "".join(_lexical_text(child) for child in node)
    if not isinstance(node, dict):
        return ""
    if isinstance(node.get("root"), dict):
        return _lexical_text(node["root"])
    if node.get("type") == "linebreak":
        return "\n"
    text = node.get("text")
    if isinstance(text, str) and text:
        return text
    inner = _lexical_text(node.get("children"))
    if node.get("type") in {"paragraph", "heading", "quote", "listitem", "code"}:
        return inner + "\n"
    return inner


def rich_text(value: Any) -> str:
    if not value:
        return ""
    payload = value
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return value.strip()
    return _lexical_text(payload).strip()


# ---------------------------------------------------------------------------
# Store discovery
# ---------------------------------------------------------------------------


def cursor_user_dirs() -> list[tuple[Path, str]]:
    """(Cursor ``User`` dir, tag) locally and on every reachable remote home."""
    out: list[tuple[Path, str]] = []
    seen: set[str] = set()
    home = Path.home()
    candidates: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "Cursor" / "User")
    for relative in USER_DIR_RELATIVES:
        candidates.append(home / Path(relative))
    for path in candidates:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.is_dir():
                out.append((path, ""))
        except OSError:
            continue
    for relative in USER_DIR_RELATIVES:
        for remote_dir, tag in wsl_agent_homes(relative):
            out.append((remote_dir, f"@{tag}"))
    return out


def staged_db(path: Path) -> Path | None:
    """Read-only-safe copy of a live SQLite store (folds the WAL in)."""
    try:
        if not path.is_file():
            return None
    except OSError:
        return None
    return stage_wsl_sqlite(path)


def global_dbs() -> list[tuple[Path, Path, str, Path]]:
    """(staged db, display db, tag, user dir) for every globalStorage store."""
    out: list[tuple[Path, Path, str, Path]] = []
    for user_dir, tag in cursor_user_dirs():
        display = user_dir / "globalStorage" / "state.vscdb"
        staged = staged_db(display)
        if staged is not None:
            out.append((staged, display, tag, user_dir))
    return out


def workspace_folders(user_dir: Path) -> dict[str, str]:
    """workspaceStorage id -> opened folder path, for the CWD column."""
    folders: dict[str, str] = {}
    root = user_dir / "workspaceStorage"
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return folders
    for entry in entries:
        meta = load_json(entry / "workspace.json", {})
        if isinstance(meta, dict):
            target = meta.get("folder") or meta.get("workspace") or ""
            if target:
                folders[entry.name] = uri_to_path(str(target))
    return folders


def workspace_dbs() -> list[tuple[Path, Path, str, str, str]]:
    """(staged db, display db, tag, workspace id, folder) per workspaceStorage."""
    out: list[tuple[Path, Path, str, str, str]] = []
    for user_dir, tag in cursor_user_dirs():
        folders = workspace_folders(user_dir)
        root = user_dir / "workspaceStorage"
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            display = entry / "state.vscdb"
            staged = staged_db(display)
            if staged is not None:
                out.append((staged, display, tag, entry.name, folders.get(entry.name, "")))
    return out


def cursor_cli_homes() -> list[tuple[Path, str]]:
    """(``.cursor`` dir, tag) locally and on every reachable remote home."""
    out: list[tuple[Path, str]] = []
    local = Path.home() / ".cursor"
    try:
        if local.is_dir():
            out.append((local, ""))
    except OSError:
        pass
    for remote_dir, tag in wsl_agent_homes(".cursor"):
        out.append((remote_dir, f"@{tag}"))
    return out


def connect_ro(db: Path) -> sqlite3.Connection | None:
    try:
        return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


# ---------------------------------------------------------------------------
# IDE composers
# ---------------------------------------------------------------------------


def load_composers(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every composer in a globalStorage store, with its bubble fingerprint."""
    composers: list[dict[str, Any]] = []
    try:
        rows = con.execute(
            "SELECT composerId, workspaceId, createdAt, lastUpdatedAt, isArchived,"
            " isSubagent, subagentTypeName FROM composerHeaders"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    if not rows:
        # Older Cursor builds kept the index only in cursorDiskKV.
        try:
            keys = con.execute(
                "SELECT key FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
            ).fetchall()
        except sqlite3.Error:
            keys = []
        rows = [(key[len("composerData:"):], "", 0, 0, 0, 0, "") for (key,) in keys]

    for composer_id, workspace_id, created, updated, archived, subagent, subtype in rows:
        composer_id = str(composer_id)
        try:
            count, total = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(value)), 0) FROM cursorDiskKV"
                " WHERE key LIKE ?",
                (f"bubbleId:{composer_id}:%",),
            ).fetchone()
        except sqlite3.Error:
            count, total = 0, 0
        if not count:
            continue  # drafts and empty tabs
        composers.append(
            {
                "id": composer_id,
                "workspace_id": str(workspace_id or ""),
                "created": epoch_to_iso(created),
                "updated": epoch_to_iso(updated or created),
                "archived": bool(archived),
                "subagent": bool(subagent),
                "subagent_type": str(subtype or ""),
                "bubble_count": int(count),
                "bubble_bytes": int(total or 0),
            }
        )
    return composers


def composer_bubbles(
    con: sqlite3.Connection, composer_id: str
) -> tuple[str, list[dict[str, Any]], str]:
    """(title, ordered bubbles, model) for one composer."""
    data: Any = {}
    row = con.execute(
        "SELECT value FROM cursorDiskKV WHERE key = ?", (f"composerData:{composer_id}",)
    ).fetchone()
    if row and row[0]:
        try:
            data = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            data = {}
    if not isinstance(data, dict):
        data = {}

    stored: dict[str, dict[str, Any]] = {}
    for key, value in con.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
        (f"bubbleId:{composer_id}:%",),
    ):
        try:
            payload = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            stored[str(key).rsplit(":", 1)[-1]] = payload

    ordered: list[dict[str, Any]] = []
    headers = data.get("fullConversationHeadersOnly")
    if isinstance(headers, list) and headers:
        for header in headers:
            if not isinstance(header, dict):
                continue
            bubble = stored.pop(str(header.get("bubbleId") or ""), None)
            if bubble is None:
                continue
            if bubble.get("type") is None:
                bubble["type"] = header.get("type")
            if header.get("createdAt") and not bubble.get("createdAt"):
                bubble["createdAt"] = header["createdAt"]
            ordered.append(bubble)
    # Bubbles the header list does not mention (older stores, or a write that
    # landed mid-scan) keep their own chronological order at the end.
    ordered.extend(sorted(stored.values(), key=lambda b: str(b.get("createdAt") or "")))

    model = ""
    config = data.get("modelConfig")
    if isinstance(config, dict):
        model = str(config.get("modelName") or "")
    title = one_line(str(data.get("name") or ""), 80)
    return title, ordered, model


def emit_bubble(lines: list[str], counts: dict[str, int], bubble: dict[str, Any]) -> None:
    timestamp = epoch_to_iso(bubble.get("createdAt"))
    text = str(bubble.get("text") or "").strip() or rich_text(bubble.get("richText"))

    if bubble.get("type") == BUBBLE_USER:
        body = extract_user_query(text)
        if body.strip() and not should_skip_user_text(body):
            emit_block(lines, "USER", clip_text(body), timestamp)
            counts["user"] += 1
        return

    thinking = bubble.get("thinking")
    if isinstance(thinking, dict):
        thought = str(thinking.get("text") or "").strip()
        if thought:
            emit_block(lines, "REASONING", clip_text(thought, 40_000), timestamp)
            counts["reasoning"] += 1
    if text:
        emit_block(lines, "ASSISTANT", clip_text(text), timestamp)
        counts["assistant"] += 1

    tool = bubble.get("toolFormerData")
    if isinstance(tool, dict) and tool:
        name = str(tool.get("name") or "tool")
        status = str(tool.get("status") or "")
        header = f"TOOL CALL ({name})" + (f" [{status}]" if status else "")
        args = tool.get("params") or tool.get("rawArgs")
        emit_block(lines, header, clip_text(pretty(maybe_json(args)), 20_000), timestamp)
        counts["tool_call"] += 1
        result = tool.get("result")
        if result:
            emit_block(
                lines,
                f"TOOL OUTPUT ({name})",
                clip_text(pretty(maybe_json(result)), 20_000),
                timestamp,
            )
            counts["tool_output"] += 1
    for extra in bubble.get("toolResults") or []:
        emit_block(lines, "TOOL OUTPUT", clip_text(pretty(extra), 20_000), timestamp)
        counts["tool_output"] += 1


def export_composer(
    composer: dict[str, Any],
    source_key: str,
    title: str,
    cwd: str,
    bubbles: list[dict[str, Any]],
    model: str,
    output_path: Path,
) -> dict[str, Any]:
    counts = empty_counts()
    kind = "subagent" if composer["subagent"] else "composer"
    lines = [
        "Local Cursor chat export",
        f"Title: {title}",
        f"Session ID: {composer['id']}",
        f"Kind: {kind}",
        f"Workspace: {composer['workspace_id']}",
        f"CWD: {cwd}",
        f"Model: {model}",
        f"Source: {source_key}",
        f"Exported At: {now_iso()}",
        "",
    ]
    for bubble in bubbles:
        emit_bubble(lines, counts, bubble)
    atomic_write_text(output_path, scrub_internal_lines("\n".join(lines) + "\n"))
    return {
        "session_id": composer["id"],
        "source_id": composer["id"],
        "title": title,
        "kind": kind,
        "source": source_key,
        "output": str(output_path),
        "created": composer["created"],
        "updated": composer["updated"],
        "cwd": cwd,
        "model": model,
        "counts": counts,
        "bytes": output_path.stat().st_size,
    }


# ---------------------------------------------------------------------------
# cursor-agent CLI sessions
# ---------------------------------------------------------------------------


def cli_transcripts(cursor_home: Path) -> dict[str, Path]:
    """session id -> transcript JSONL under ``.cursor/projects``."""
    found: dict[str, Path] = {}
    root = cursor_home / "projects"
    try:
        projects = sorted(root.iterdir())
    except OSError:
        return found
    for project in projects:
        try:
            sessions = sorted((project / "agent-transcripts").iterdir())
        except OSError:
            continue
        for session in sessions:
            path = session / f"{session.name}.jsonl"
            if path.is_file():
                found[session.name] = path
                continue
            for candidate in sorted(session.glob("*.jsonl")):
                found[session.name] = candidate
                break
    return found


def cli_sessions(cursor_home: Path) -> list[dict[str, Any]]:
    """Session dirs under ``.cursor/chats/<workspace hash>/<session id>``."""
    sessions: list[dict[str, Any]] = []
    try:
        workspaces = sorted((cursor_home / "chats").iterdir())
    except OSError:
        return sessions
    for workspace in workspaces:
        try:
            entries = sorted(workspace.iterdir())
        except OSError:
            continue
        for entry in entries:
            meta = load_json(entry / "meta.json", {})
            if not isinstance(meta, dict):
                meta = {}
            sessions.append(
                {
                    "id": entry.name,
                    "dir": entry,
                    "workspace": workspace.name,
                    "title": one_line(str(meta.get("title") or ""), 80),
                    "cwd": str(meta.get("cwd") or ""),
                    "created": epoch_to_iso(meta.get("createdAtMs")),
                    "updated": epoch_to_iso(meta.get("updatedAtMs")),
                    "has_conversation": bool(meta.get("hasConversation")),
                }
            )
    return sessions


def emit_transcript(lines: list[str], counts: dict[str, int], path: Path) -> None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            counts["parse_error"] += 1
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "turn_ended":
            error = str(entry.get("error") or "")
            status = str(entry.get("status") or "")
            if error or status not in {"", "success"}:
                emit_block(lines, "TURN ENDED", f"status: {status}\n{error}".strip())
            continue
        role = str(entry.get("role") or "").lower()
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else entry.get("content")
        if role == "user":
            body = extract_user_query(content_to_text(content))
            if body.strip() and not should_skip_user_text(body):
                emit_block(lines, "USER", clip_text(body))
                counts["user"] += 1
            continue
        if role != "assistant":
            body = content_to_text(content)
            if body.strip():
                emit_block(lines, (role or "OTHER").upper(), clip_text(body))
            continue
        parts = content if isinstance(content, list) else [content]
        spoken: list[str] = []
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "tool_use":
                name = str(part.get("name") or "tool")
                emit_block(
                    lines,
                    f"TOOL CALL ({name})",
                    clip_text(pretty(part.get("input")), 20_000),
                )
                counts["tool_call"] += 1
                continue
            chunk = content_to_text(part)
            if chunk.strip():
                spoken.append(chunk)
        if spoken:
            emit_block(lines, "ASSISTANT", clip_text("\n\n".join(spoken)))
            counts["assistant"] += 1


def export_cli_session(
    session: dict[str, Any],
    source_key: str,
    title: str,
    transcript: Path | None,
    output_path: Path,
) -> dict[str, Any]:
    counts = empty_counts()
    lines = [
        "Local Cursor chat export",
        f"Title: {title}",
        f"Session ID: {session['id']}",
        "Kind: cli",
        f"Workspace: {session.get('workspace', '')}",
        f"CWD: {session.get('cwd', '')}",
        f"Source: {source_key}",
        f"Exported At: {now_iso()}",
        "",
    ]
    if transcript is not None:
        emit_transcript(lines, counts, transcript)
    if not counts["user"]:
        # No transcript yet (or a prompt-only session): keep the typed prompts.
        prompts = load_json(Path(session["dir"]) / "prompt_history.json", [])
        if isinstance(prompts, list):
            for prompt in prompts:
                body = extract_user_query(str(prompt or ""))
                if body.strip() and not should_skip_user_text(body):
                    emit_block(lines, "USER", clip_text(body))
                    counts["user"] += 1
    atomic_write_text(output_path, scrub_internal_lines("\n".join(lines) + "\n"))
    return {
        "session_id": session["id"],
        "source_id": session["id"],
        "title": title,
        "kind": "cli",
        "source": source_key,
        "output": str(output_path),
        "created": session.get("created", ""),
        "updated": session.get("updated", ""),
        "cwd": session.get("cwd", ""),
        "model": "",
        "counts": counts,
        "bytes": output_path.stat().st_size,
    }


# ---------------------------------------------------------------------------
# Workspace prompt history (legacy chat pane + aiService keys)
# ---------------------------------------------------------------------------


WORKSPACE_KEYS = (
    "workbench.panel.aichat.view.aichat.chatdata",
    "aiService.prompts",
    "aiService.generations",
)


def workspace_entries(db: Path) -> list[tuple[str, Any]]:
    con = connect_ro(db)
    if con is None:
        return []
    out: list[tuple[str, Any]] = []
    try:
        for key in WORKSPACE_KEYS:
            row = con.execute("SELECT value FROM ItemTable WHERE key = ?", (key,)).fetchone()
            if not row or not row[0]:
                continue
            raw = row[0]
            if isinstance(raw, (bytes, bytearray)):
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if payload in ([], {}, None):
                continue
            out.append((key, payload))
    except sqlite3.Error:
        return out
    finally:
        con.close()
    return out


def export_workspace(
    source_key: str,
    title: str,
    workspace_id: str,
    cwd: str,
    entries: list[tuple[str, Any]],
    output_path: Path,
) -> dict[str, Any]:
    counts = empty_counts()
    lines = [
        "Local Cursor chat export",
        f"Title: {title}",
        f"Session ID: {workspace_id}",
        "Kind: workspace",
        f"CWD: {cwd}",
        f"Source: {source_key}",
        f"Exported At: {now_iso()}",
        "",
    ]
    for key, payload in entries:
        if key == "aiService.prompts" and isinstance(payload, list):
            for item in payload:
                text = item.get("text") if isinstance(item, dict) else item
                body = extract_user_query(str(text or ""))
                if body.strip() and not should_skip_user_text(body):
                    emit_block(lines, "USER", clip_text(body))
                    counts["user"] += 1
            continue
        if key == "aiService.generations" and isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                body = str(item.get("textDescription") or item.get("description") or "")
                if body.strip():
                    emit_block(
                        lines,
                        f"GENERATION ({item.get('type', '')})",
                        clip_text(body),
                        epoch_to_iso(item.get("unixMs")),
                    )
                    counts["assistant"] += 1
            continue
        tabs = payload.get("tabs") if isinstance(payload, dict) else None
        if isinstance(tabs, list):
            for tab in tabs:
                if not isinstance(tab, dict):
                    continue
                for bubble in tab.get("bubbles") or []:
                    if not isinstance(bubble, dict):
                        continue
                    body = content_to_text(bubble.get("text") or bubble.get("content"))
                    if not body.strip():
                        continue
                    if str(bubble.get("type") or "").lower() == "user":
                        body = extract_user_query(body)
                        if should_skip_user_text(body):
                            continue
                        emit_block(lines, "USER", clip_text(body))
                        counts["user"] += 1
                    else:
                        emit_block(lines, "ASSISTANT", clip_text(body))
                        counts["assistant"] += 1
            continue
        emit_block(lines, key.upper(), clip_text(pretty(payload), 20_000))
    atomic_write_text(output_path, scrub_internal_lines("\n".join(lines) + "\n"))
    return {
        "session_id": workspace_id,
        "source_id": workspace_id,
        "title": title,
        "kind": "workspace",
        "source": source_key,
        "output": str(output_path),
        "created": "",
        "updated": "",
        "cwd": cwd,
        "model": "",
        "counts": counts,
        "bytes": output_path.stat().st_size,
    }


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


def fallback_title(bubbles: Iterable[dict[str, Any]], session_id: str) -> str:
    for bubble in bubbles:
        if bubble.get("type") != BUBBLE_USER:
            continue
        text = str(bubble.get("text") or "").strip() or rich_text(bubble.get("richText"))
        text = extract_user_query(text)
        if text.strip() and not should_skip_user_text(text):
            return one_line(text, 80)
    return f"Cursor {session_id[:8]}"


def _scan_global_stores(
    output_dir: Path,
    old_sources: dict[str, Any],
    new_sources: dict[str, Any],
    records: list[dict[str, Any]],
    seen: set[str],
    composer_ids: set[str],
    ordinal: int,
) -> tuple[int, int]:
    changed = 0
    for db, display_db, tag, user_dir in global_dbs():
        con = connect_ro(db)
        if con is None:
            continue
        try:
            folders = workspace_folders(user_dir)
            for composer in load_composers(con):
                ordinal += 1
                composer_ids.add(composer["id"])
                source_key = f"cursor-ide{tag}:{composer['id']}"
                seen.add(source_key)
                cwd = folders.get(composer["workspace_id"], "")
                old = old_sources.get(source_key, {})
                fingerprint = [
                    composer["updated"],
                    composer["bubble_count"],
                    composer["bubble_bytes"],
                ]
                prefix = first_iso_timestamp_prefix(composer["created"] or None, ordinal)
                stem = (
                    f"{prefix}__{composer['id']}__"
                    if not tag
                    else f"{prefix}__{tag}_{composer['id']}__"
                )
                must_export = (
                    not old
                    or old.get("fingerprint") != fingerprint
                    or not Path(old.get("output") or "").exists()
                )
                if must_export:
                    title, bubbles, model = composer_bubbles(con, composer["id"])
                    if not title:
                        title = fallback_title(bubbles, composer["id"])
                    output_path = filesystem_safe_output_path(output_dir, stem, title)
                    record = export_composer(
                        composer, source_key, title, cwd, bubbles, model, output_path
                    )
                    reuse_or_replace_output(old.get("output"), output_path)
                    changed += 1
                else:
                    record = dict(old)
                record["fingerprint"] = fingerprint
                record["source_db"] = str(display_db)
                new_sources[source_key] = record
                records.append(record)
        finally:
            con.close()
    return changed, ordinal


def _scan_workspace_stores(
    output_dir: Path,
    old_sources: dict[str, Any],
    new_sources: dict[str, Any],
    records: list[dict[str, Any]],
    seen: set[str],
    ordinal: int,
) -> tuple[int, int]:
    changed = 0
    for db, display_db, tag, workspace_id, folder in workspace_dbs():
        entries = workspace_entries(db)
        if not entries:
            continue
        ordinal += 1
        source_key = f"cursor-workspace{tag}:{workspace_id}"
        seen.add(source_key)
        title = one_line(f"Cursor workspace {Path(folder).name or workspace_id}", 80)
        prefix = first_iso_timestamp_prefix(None, ordinal)
        stem = (
            f"{prefix}__{workspace_id}__"
            if not tag
            else f"{prefix}__{tag}_{workspace_id}__"
        )
        output_path = filesystem_safe_output_path(output_dir, stem, title)
        old = old_sources.get(source_key, {})
        mtime_ns, size = file_fingerprint(display_db)
        must_export = (
            not old
            or old.get("mtime_ns") != mtime_ns
            or old.get("size") != size
            or old.get("output") != str(output_path)
            or not Path(old.get("output") or "").exists()
        )
        if must_export:
            record = export_workspace(
                source_key, title, workspace_id, folder, entries, output_path
            )
            reuse_or_replace_output(old.get("output"), output_path)
            changed += 1
        else:
            record = dict(old)
        record["mtime_ns"] = mtime_ns
        record["size"] = size
        record["source_db"] = str(display_db)
        new_sources[source_key] = record
        records.append(record)
    return changed, ordinal


def _scan_cli_homes(
    output_dir: Path,
    old_sources: dict[str, Any],
    new_sources: dict[str, Any],
    records: list[dict[str, Any]],
    seen: set[str],
    composer_ids: set[str],
    ordinal: int,
) -> tuple[int, int]:
    changed = 0
    for cursor_home, tag in cursor_cli_homes():
        transcripts = cli_transcripts(cursor_home)
        claimed: set[str] = set()
        for session in cli_sessions(cursor_home):
            session_id = session["id"]
            transcript = transcripts.get(session_id)
            if transcript is not None:
                claimed.add(session_id)
            if session_id in composer_ids:
                continue  # the IDE composer store holds the richer copy
            if transcript is None and not session["has_conversation"]:
                continue
            ordinal += 1
            source_key = f"cursor-cli{tag}:{session_id}"
            seen.add(source_key)
            title = session["title"] or f"Cursor agent {session_id[:8]}"
            prefix = first_iso_timestamp_prefix(session["created"] or None, ordinal)
            stem = (
                f"{prefix}__{session_id}__"
                if not tag
                else f"{prefix}__{tag}_{session_id}__"
            )
            output_path = filesystem_safe_output_path(output_dir, stem, title)
            old = old_sources.get(source_key, {})
            fingerprint = [
                list(file_fingerprint(Path(session["dir"]) / "meta.json")),
                list(file_fingerprint(Path(session["dir"]) / "prompt_history.json")),
                list(file_fingerprint(transcript)) if transcript else [0, 0],
            ]
            must_export = (
                not old
                or old.get("fingerprint") != fingerprint
                or old.get("output") != str(output_path)
                or not Path(old.get("output") or "").exists()
            )
            if must_export:
                record = export_cli_session(session, source_key, title, transcript, output_path)
                reuse_or_replace_output(old.get("output"), output_path)
                changed += 1
            else:
                record = dict(old)
            record["fingerprint"] = fingerprint
            new_sources[source_key] = record
            records.append(record)

        # Transcripts with no chats/<hash>/<id> dir and no IDE composer.
        for session_id, transcript in sorted(transcripts.items()):
            if session_id in claimed or session_id in composer_ids:
                continue
            ordinal += 1
            source_key = f"cursor-transcript{tag}:{session_id}"
            seen.add(source_key)
            session = {
                "id": session_id,
                "dir": transcript.parent,
                "workspace": transcript.parent.parent.parent.name,
                "cwd": "",
                "created": epoch_to_iso(transcript.stat().st_ctime),
                "updated": epoch_to_iso(transcript.stat().st_mtime),
            }
            title = f"Cursor agent {session_id[:8]}"
            prefix = first_iso_timestamp_prefix(session["created"] or None, ordinal)
            stem = (
                f"{prefix}__{session_id}__"
                if not tag
                else f"{prefix}__{tag}_{session_id}__"
            )
            output_path = filesystem_safe_output_path(output_dir, stem, title)
            old = old_sources.get(source_key, {})
            mtime_ns, size = file_fingerprint(transcript)
            must_export = (
                not old
                or old.get("mtime_ns") != mtime_ns
                or old.get("size") != size
                or old.get("output") != str(output_path)
                or not Path(old.get("output") or "").exists()
            )
            if must_export:
                record = export_cli_session(session, source_key, title, transcript, output_path)
                reuse_or_replace_output(old.get("output"), output_path)
                changed += 1
            else:
                record = dict(old)
            record["mtime_ns"] = mtime_ns
            record["size"] = size
            new_sources[source_key] = record
            records.append(record)
    return changed, ordinal


def scan_once(output_dir: Path) -> tuple[int, int, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / ".export_state.json"
    old_sources: dict[str, Any] = (load_json(state_path, {"sources": {}}) or {}).get("sources") or {}
    new_sources: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    composer_ids: set[str] = set()
    changed = 0
    ordinal = 0

    delta, ordinal = _scan_global_stores(
        output_dir, old_sources, new_sources, records, seen, composer_ids, ordinal
    )
    changed += delta
    delta, ordinal = _scan_workspace_stores(
        output_dir, old_sources, new_sources, records, seen, ordinal
    )
    changed += delta
    delta, ordinal = _scan_cli_homes(
        output_dir, old_sources, new_sources, records, seen, composer_ids, ordinal
    )
    changed += delta

    removed = prune_removed_sources(
        old_sources, seen, retained_sources=new_sources, retained_records=records
    )
    write_manifest(output_dir, "Cursor", records, changed, removed)
    atomic_write_json(state_path, {"updated_at": now_iso(), "sources": new_sources})
    return len(records), changed, removed


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir).expanduser()
    return run_watcher_loop(
        once=args.once,
        interval=args.interval,
        log_file=Path(args.log_file).expanduser() if args.log_file else None,
        status_file=Path(args.status_file).expanduser() if args.status_file else None,
        extra_status={"output_dir": str(output_dir)},
        scan_fn=lambda: scan_once(output_dir),
        quiet=args.quiet,
        label="cursor",
    )


if __name__ == "__main__":
    raise SystemExit(main())
