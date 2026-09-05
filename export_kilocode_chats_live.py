#!/usr/bin/env python3
"""Export Kilo Code (VS Code / Cursor extension) chats when local history exists."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

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
    write_manifest,
)

# Kilo Code CLI stores sessions in an opencode-compatible SQLite database
# (session / message / part tables). Reuse the OpenCode exporter's DB logic.
from export_opencode_chats_live import (
    connect_ro as _db_connect_ro,
    export_session as _db_export_session,
    load_sessions as _db_load_sessions,
    session_title as _db_session_title,
)

HOST_GLOBAL = (
    Path(os.environ.get("APPDATA", "")) / "Code" / "User" / "globalStorage",
    Path(os.environ.get("APPDATA", "")) / "Cursor" / "User" / "globalStorage",
    Path(os.environ.get("APPDATA", "")) / "Code - Insiders" / "User" / "globalStorage",
    Path(os.environ.get("APPDATA", "")) / "VSCodium" / "User" / "globalStorage",
)
KILO_DIR_NAMES = ("kilocode.kilo-code", "kilocode.kilo-code-pre-release")
TASK_FILES = (
    "ui_messages.json",
    "api_conversation_history.json",
    "cline_messages.json",
    "messages.json",
    "chat_history.json",
)


def kilo_db_paths() -> list[Path]:
    candidates: list[Path] = []
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        candidates.append(Path(data_home) / "kilo" / "kilo.db")
    candidates.append(Path.home() / ".local" / "share" / "kilo" / "kilo.db")
    seen: set[str] = set()
    out: list[Path] = []
    for path in candidates:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if path.exists():
            out.append(path)
    return out


def parse_args() -> argparse.Namespace:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(home / "kilocode_chat_live_exports"))
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


def kilo_roots() -> list[Path]:
    roots: list[Path] = []
    for host in HOST_GLOBAL:
        if not host:
            continue
        for name in KILO_DIR_NAMES:
            path = host / name
            if path.exists():
                roots.append(path)
    extra = Path.home() / ".kilocode"
    if extra.exists():
        roots.append(extra)
    return roots


def iter_task_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in {"node_modules", "cache"}]
        for name in filenames:
            if name in TASK_FILES or name.endswith(".jsonl"):
                found.append(Path(dirpath) / name)
    return found


def messages_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("messages", "ui_messages", "history", "conversation", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


def export_messages(
    source: Path,
    title: str,
    session_id: str,
    messages: list[dict[str, Any]],
    output_path: Path,
    extra_meta: dict[str, Any],
) -> dict[str, Any]:
    counts = empty_counts()
    lines = [
        "Local Kilo Code chat export",
        f"Title: {title}",
        f"Session ID: {session_id}",
        f"Source: {source}",
        f"Exported At: {now_iso()}",
        "",
    ]
    for extra_key in ("cwd", "model", "created", "updated"):
        if extra_meta.get(extra_key):
            lines.insert(-1, f"{extra_key.capitalize()}: {extra_meta[extra_key]}")

    for obj in messages:
        role = str(obj.get("role") or obj.get("type") or obj.get("speaker") or "").lower()
        ts = epoch_to_iso(obj.get("ts") or obj.get("time") or obj.get("createdAt") or obj.get("timestamp"))
        if role in {"say", "ask"} and obj.get("say"):
            role = "assistant" if obj.get("say") not in {"user", "user_feedback"} else "user"
            if obj.get("say") in {"api_req_started", "tool", "command"}:
                role = "tool"
        text = content_to_text(obj.get("text") or obj.get("content") or obj.get("message") or obj.get("value"))
        if role in {"user", "human"}:
            text = extract_user_query(text)
            if should_skip_user_text(text):
                continue
            if text.strip():
                emit_block(lines, "USER", text, ts)
                counts["user"] += 1
            continue
        if role in {"assistant", "ai", "bot", "say"}:
            if text.strip():
                emit_block(lines, "ASSISTANT", text, ts)
                counts["assistant"] += 1
            continue
        if role in {"tool", "tool_call", "function"} or obj.get("tool"):
            name = obj.get("tool") or obj.get("name") or "tool"
            emit_block(lines, "TOOL CALL", f"Name: {name}\n{clip_text(pretty(obj), 20_000)}", ts)
            counts["tool_call"] += 1
            continue
        if role in {"tool_result", "tool-result"}:
            emit_block(lines, "TOOL OUTPUT", clip_text(text or pretty(obj), 40_000), ts)
            counts["tool_output"] += 1
            continue
        if text.strip():
            emit_block(lines, (role or "OTHER").upper(), text, ts)

    atomic_write_text(output_path, scrub_internal_lines("\n".join(lines) + "\n"))
    return {
        "session_id": session_id,
        "source_id": session_id,
        "title": title,
        "kind": "session",
        "source": str(source),
        "output": str(output_path),
        "created": extra_meta.get("created", ""),
        "updated": extra_meta.get("updated", ""),
        "cwd": extra_meta.get("cwd", ""),
        "model": extra_meta.get("model", ""),
        "counts": counts,
        "bytes": output_path.stat().st_size,
    }


def scan_vscdb_histories() -> list[tuple[Path, str, list[dict[str, Any]], dict[str, Any]]]:
    results: list[tuple[Path, str, list[dict[str, Any]], dict[str, Any]]] = []
    appdata = Path(os.environ.get("APPDATA", ""))
    for host in ("Code", "Cursor", "Code - Insiders", "VSCodium"):
        db = appdata / host / "User" / "globalStorage" / "state.vscdb"
        if not db.exists():
            continue
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            rows = con.execute("SELECT key, value FROM ItemTable").fetchall()
            con.close()
        except sqlite3.Error:
            continue
        for key, value in rows:
            low = str(key).lower()
            if not any(token in low for token in ("kilo", "kilocode", "taskhistory", "task_history")):
                continue
            raw = value
            if isinstance(raw, (bytes, bytearray)):
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
            try:
                payload = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, json.JSONDecodeError):
                continue
            items = payload if isinstance(payload, list) else []
            if isinstance(payload, dict):
                for cand in ("taskHistory", "history", "tasks", "value"):
                    if isinstance(payload.get(cand), list):
                        items = payload[cand]
                        break
            if not items:
                continue
            # taskHistory is usually metadata, not full transcripts
            results.append(
                (
                    db,
                    str(key),
                    [x for x in items if isinstance(x, dict)],
                    {"kind": "index"},
                )
            )
    return results


def scan_once(output_dir: Path) -> tuple[int, int, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / ".export_state.json"
    state_obj = load_json(state_path, {"sources": {}})
    old_sources: dict[str, Any] = state_obj.get("sources") or {}
    new_sources: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    changed = 0
    ordinal = 0

    for root in kilo_roots():
        for path in iter_task_files(root):
            ordinal += 1
            source_key = str(path)
            seen.add(source_key)
            mtime_ns, size = file_fingerprint(path)
            session_id = path.parent.name if path.parent.name not in KILO_DIR_NAMES else path.stem
            title = one_line(f"Kilo {session_id}", 80)
            prefix = first_iso_timestamp_prefix(None, ordinal)
            try:
                created = epoch_to_iso(path.stat().st_ctime)
                updated = epoch_to_iso(path.stat().st_mtime)
            except OSError:
                created = updated = ""
            output_path = filesystem_safe_output_path(
                output_dir, f"{prefix}__{session_id}__", title
            )
            old = old_sources.get(source_key, {})
            must_export = (
                not old
                or old.get("mtime_ns") != mtime_ns
                or old.get("size") != size
                or old.get("output") != str(output_path)
                or not Path(old.get("output") or "").exists()
            )
            if must_export:
                payload: Any
                if path.suffix == ".jsonl":
                    payload = []
                    with path.open("r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                payload.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
                else:
                    payload = load_json(path, [])
                messages = messages_from_payload(payload)
                if not messages and isinstance(payload, list):
                    messages = [x for x in payload if isinstance(x, dict)]
                guessed = ""
                for msg in messages:
                    if str(msg.get("role") or "").lower() in {"user", "human"}:
                        guessed = one_line(
                            extract_user_query(content_to_text(msg.get("text") or msg.get("content"))),
                            80,
                        )
                        if guessed:
                            break
                if guessed:
                    title = guessed
                    output_path = filesystem_safe_output_path(
                        output_dir, f"{prefix}__{session_id}__", title
                    )
                record = export_messages(
                    path,
                    title,
                    session_id,
                    messages,
                    output_path,
                    {"created": created, "updated": updated},
                )
                reuse_or_replace_output(old.get("output"), output_path)
                changed += 1
            else:
                record = {
                    "session_id": session_id,
                    "source_id": session_id,
                    "title": old.get("title") or title,
                    "kind": "session",
                    "source": source_key,
                    "output": str(output_path),
                    "created": old.get("created", created),
                    "updated": old.get("updated", updated),
                    "cwd": old.get("cwd", ""),
                    "model": old.get("model", ""),
                    "counts": old.get("counts") or empty_counts(),
                    "bytes": old.get("bytes", 0),
                }
            record["mtime_ns"] = mtime_ns
            record["size"] = size
            new_sources[source_key] = record
            records.append(record)

    for db in kilo_db_paths():
        con = _db_connect_ro(db)
        try:
            db_sessions = _db_load_sessions(con)
            for ordinal, session in enumerate(db_sessions, 1):
                session["source_db"] = str(db)
                source_key = f"kilo-cli:{session['id']}"
                seen.add(source_key)
                title = _db_session_title(con, session)
                prefix = first_iso_timestamp_prefix(session.get("created"), ordinal)
                output_path = filesystem_safe_output_path(
                    output_dir, f"{prefix}__{session['id']}__", title
                )
                old = old_sources.get(source_key, {})
                must_export = (
                    not old
                    or old.get("time_updated") != session.get("time_updated")
                    or old.get("title") != title
                    or old.get("output") != str(output_path)
                    or not Path(old.get("output") or "").exists()
                    or not output_path.exists()
                )
                if must_export:
                    record = _db_export_session(
                        con,
                        session,
                        title,
                        output_path,
                        include_reasoning=True,
                        header_label="Local Kilo Code (CLI) chat export",
                    )
                    reuse_or_replace_output(old.get("output"), output_path)
                    changed += 1
                else:
                    record = {
                        "session_id": session["id"],
                        "source_id": session["id"],
                        "title": title,
                        "kind": "session",
                        "source": f"{db}#session:{session['id']}",
                        "output": str(output_path),
                        "created": session.get("created") or old.get("created", ""),
                        "updated": session.get("updated") or old.get("updated", ""),
                        "cwd": session.get("directory") or old.get("cwd", ""),
                        "model": session.get("model") or old.get("model", ""),
                        "parent_id": session.get("parent_id", ""),
                        "counts": old.get("counts") or empty_counts(),
                        "bytes": old.get(
                            "bytes",
                            output_path.stat().st_size if output_path.exists() else 0,
                        ),
                        "time_updated": session.get("time_updated"),
                    }
                new_sources[source_key] = record
                records.append(record)
        finally:
            con.close()

    removed = prune_removed_sources(old_sources, seen)
    write_manifest(output_dir, "Kilo Code", records, changed, removed)
    atomic_write_json(
        state_path,
        {
            "updated_at": now_iso(),
            "roots": [str(p) for p in kilo_roots()],
            "dbs": [str(p) for p in kilo_db_paths()],
            "sources": new_sources,
        },
    )
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
        label="kilocode",
    )


if __name__ == "__main__":
    raise SystemExit(main())
