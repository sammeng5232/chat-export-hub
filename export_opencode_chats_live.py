#!/usr/bin/env python3
"""Incrementally export local OpenCode sessions (opencode.db) to TXT."""

from __future__ import annotations

import argparse
import json
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

SKIP_PART_TYPES = frozenset(
    {
        "step-start",
        "step-finish",
        "step_start",
        "step_finish",
        "compaction",
        "snapshot",
    }
)


def parse_args() -> argparse.Namespace:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=str(home / ".local" / "share" / "opencode" / "opencode.db"),
    )
    parser.add_argument("--output-dir", default=str(home / "opencode_chat_live_exports"))
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--log-file")
    parser.add_argument("--status-file")
    parser.add_argument("--include-reasoning", action="store_true", default=True)
    parser.add_argument("--no-reasoning", action="store_true")
    return parser.parse_args()


def connect_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def parse_json(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}


def pretty(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except TypeError:
        return str(value)


def db_fingerprint(db: Path) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for label, path in (
        ("db", db),
        ("wal", Path(str(db) + "-wal")),
        ("shm", Path(str(db) + "-shm")),
    ):
        mtime_ns, size = file_fingerprint(path)
        out[label] = [mtime_ns, size]
    return out


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})")}


def load_sessions(con: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = table_columns(con, "session")
    wanted = [
        "id",
        "project_id",
        "workspace_id",
        "parent_id",
        "slug",
        "directory",
        "path",
        "title",
        "version",
        "summary",
        "metadata",
        "cost",
        "agent",
        "model",
        "time_created",
        "time_updated",
    ]
    select_cols = [c for c in wanted if c in cols]
    if "id" not in cols:
        return []
    rows = con.execute(
        f"""
        SELECT {", ".join(select_cols)}
        FROM session
        ORDER BY {"time_updated DESC, " if "time_updated" in cols else ""}
                 {"time_created DESC" if "time_created" in cols else "id"}
        """
    ).fetchall()
    sessions = []
    by_id: dict[str, sqlite3.Row] = {}
    for row in rows:
        by_id[str(row["id"])] = row
    for row in rows:
        parent_id = str(row["parent_id"] or "").strip()
        parent_title = ""
        if parent_id and parent_id in by_id:
            parent_title = str(by_id[parent_id]["title"] or "").strip()
        sessions.append(
            {
                "id": str(row["id"]),
                "parent_id": parent_id,
                "parent_title": parent_title,
                "slug": row["slug"] or "",
                "directory": row["directory"] or row["path"] or "",
                "title": (row["title"] or "").strip(),
                "agent": row["agent"] or "",
                "model": row["model"] or "",
                "created": epoch_to_iso(row["time_created"]),
                "updated": epoch_to_iso(row["time_updated"]),
                "time_updated": row["time_updated"],
                "time_created": row["time_created"],
            }
        )
    return sessions


def first_user_title(con: sqlite3.Connection, session_id: str) -> str:
    try:
        rows = con.execute(
            """
            SELECT m.data AS mdata, p.data AS pdata
            FROM message m
            JOIN part p ON p.message_id = m.id
            WHERE m.session_id = ?
            ORDER BY m.time_created, p.time_created
            """,
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return ""
    for row in rows:
        msg = parse_json(row["mdata"])
        if str(msg.get("role") or "").lower() != "user":
            continue
        part = parse_json(row["pdata"])
        if part.get("type") != "text":
            continue
        text = extract_user_query(str(part.get("text") or content_to_text(part)))
        if should_skip_user_text(text):
            continue
        if text.strip():
            return one_line(text, 80)
    return ""


def export_session(
    con: sqlite3.Connection,
    session: dict[str, Any],
    title: str,
    output_path: Path,
    *,
    include_reasoning: bool,
    header_label: str = "Local OpenCode chat export",
) -> dict[str, Any]:
    session_id = session["id"]
    counts = empty_counts()
    lines: list[str] = [
        header_label,
        f"Title: {title}",
        f"Session ID: {session_id}",
    ]
    if session.get("parent_id"):
        lines.append(f"Parent session: {session['parent_id']}")
        if session.get("parent_title"):
            lines.append(f"Parent title: {session['parent_title']}")
    if session.get("created"):
        lines.append(f"Created: {session['created']}")
    if session.get("updated"):
        lines.append(f"Updated: {session['updated']}")
    if session.get("directory"):
        lines.append(f"CWD: {session['directory']}")
    if session.get("model"):
        lines.append(f"Model: {session['model']}")
    if session.get("agent"):
        lines.append(f"Agent: {session['agent']}")
    lines.extend(
        [
            f"Source: {session.get('source_db', '')}",
            f"Exported At: {now_iso()}",
            "",
            "Note: step-start/step-finish/compaction parts and embedded file payloads are omitted.",
        ]
    )

    try:
        messages = con.execute(
            """
            SELECT id, time_created, time_updated, data
            FROM message
            WHERE session_id = ?
            ORDER BY time_created, id
            """,
            (session_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        emit_block(lines, "WARNING", f"Could not read messages: {exc}")
        messages = []

    parts_by_message: dict[str, list[sqlite3.Row]] = {}
    try:
        for part in con.execute(
            """
            SELECT id, message_id, time_created, data
            FROM part
            WHERE session_id = ?
            ORDER BY time_created, id
            """,
            (session_id,),
        ):
            parts_by_message.setdefault(str(part["message_id"]), []).append(part)
    except sqlite3.Error as exc:
        emit_block(lines, "WARNING", f"Could not read parts: {exc}")

    for msg_row in messages:
        msg = parse_json(msg_row["data"])
        role = str(msg.get("role") or "unknown").lower()
        ts = epoch_to_iso(msg_row["time_created"])
        parts = parts_by_message.get(str(msg_row["id"]), [])
        if not parts:
            text = content_to_text(msg.get("content") or msg.get("text"))
            if role == "user":
                text = extract_user_query(text)
                if should_skip_user_text(text):
                    continue
                emit_block(lines, "USER", text, ts)
                counts["user"] += 1
            elif role == "assistant" and text.strip():
                emit_block(lines, "ASSISTANT", text, ts)
                counts["assistant"] += 1
            continue

        text_chunks: list[str] = []
        for part_row in parts:
            part = parse_json(part_row["data"])
            ptype = str(part.get("type") or "")
            pts = epoch_to_iso(part_row["time_created"]) or ts
            if ptype in SKIP_PART_TYPES:
                continue
            if ptype == "text":
                chunk = str(part.get("text") or content_to_text(part) or "").strip()
                if chunk:
                    text_chunks.append(chunk)
                continue
            if ptype == "reasoning":
                if not include_reasoning:
                    continue
                body = str(part.get("text") or "").strip()
                if body:
                    emit_block(lines, "REASONING", clip_text(body), pts)
                    counts["reasoning"] += 1
                continue
            if ptype == "tool":
                name = part.get("tool") or part.get("name") or "tool"
                call_id = part.get("callID") or part.get("call_id") or ""
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                status = state.get("status") or part.get("status") or ""
                inp = state.get("input")
                if inp is None:
                    inp = part.get("input") or part.get("arguments")
                out = state.get("output")
                if out is None:
                    out = part.get("output") or part.get("result")
                error = state.get("error") or part.get("error")
                call_lines = [f"Name: {name}"]
                if call_id:
                    call_lines.append(f"Call ID: {call_id}")
                if status:
                    call_lines.append(f"Status: {status}")
                if inp is not None:
                    call_lines.extend(["Input:", clip_text(pretty(inp), 40_000)])
                emit_block(lines, "TOOL CALL", "\n".join(call_lines), pts)
                counts["tool_call"] += 1
                if out is not None or error:
                    out_lines = []
                    if call_id:
                        out_lines.append(f"Call ID: {call_id}")
                    if error:
                        out_lines.extend(["Error:", clip_text(pretty(error), 20_000)])
                    if out is not None:
                        out_lines.extend(["Output:", clip_text(pretty(out), 80_000)])
                    emit_block(lines, "TOOL OUTPUT", "\n".join(out_lines), pts)
                    counts["tool_output"] += 1
                continue
            if ptype == "file":
                filename = part.get("filename") or part.get("name") or "file"
                mime = part.get("mime") or part.get("mimeType") or ""
                emit_block(
                    lines,
                    "FILE",
                    f"[file: {filename}]" + (f" ({mime})" if mime else ""),
                    pts,
                )
                continue
            emit_block(
                lines,
                f"OTHER ({ptype or 'part'})",
                clip_text(pretty({k: v for k, v in part.items() if k != "data"}), 8_000),
                pts,
            )

        if text_chunks:
            body = "\n\n".join(text_chunks)
            if role == "user":
                body = extract_user_query(body)
                if should_skip_user_text(body):
                    continue
                emit_block(lines, "USER", body, ts)
                counts["user"] += 1
            elif role == "assistant":
                emit_block(lines, "ASSISTANT", body, ts)
                counts["assistant"] += 1
            elif role == "system":
                counts["system"] += 1
            else:
                emit_block(lines, role.upper(), body, ts)

    output_text = scrub_internal_lines("\n".join(lines) + "\n")
    atomic_write_text(output_path, output_text)
    return {
        "session_id": session_id,
        "source_id": session_id,
        "title": title,
        "kind": "session",
        "source": f"{session.get('source_db', '')}#session:{session_id}",
        "output": str(output_path),
        "created": session.get("created", ""),
        "updated": session.get("updated", ""),
        "cwd": session.get("directory", ""),
        "model": session.get("model", ""),
        "parent_id": session.get("parent_id", ""),
        "counts": counts,
        "bytes": output_path.stat().st_size,
        "time_updated": session.get("time_updated"),
    }


def session_title(con: sqlite3.Connection, session: dict[str, Any]) -> str:
    title = session.get("title") or ""
    if title and title.lower() not in {"untitled", "new session", "new chat"}:
        return one_line(title, 80)
    guessed = first_user_title(con, session["id"])
    if guessed:
        return guessed
    if session.get("parent_title"):
        return one_line(f"{session['parent_title']} / {session['id'][-8:]}", 80)
    return session["id"]


def scan_once(db: Path, output_dir: Path, *, include_reasoning: bool) -> tuple[int, int, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / ".export_state.json"
    state_obj = load_json(state_path, {"sources": {}})
    old_sources: dict[str, Any] = state_obj.get("sources") or {}
    new_sources: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    changed = 0

    if not db.exists():
        removed = prune_removed_sources(old_sources, seen)
        write_manifest(output_dir, f"OpenCode (missing {db})", records, changed, removed)
        atomic_write_json(
            state_path,
            {"updated_at": now_iso(), "db": str(db), "missing": True, "sources": new_sources},
        )
        return 0, 0, removed

    fingerprint = db_fingerprint(db)
    con = connect_ro(db)
    try:
        sessions = load_sessions(con)
        for ordinal, session in enumerate(sessions, 1):
            session["source_db"] = str(db)
            source_key = f"opencode:{session['id']}"
            seen.add(source_key)
            title = session_title(con, session)
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
                record = export_session(
                    con, session, title, output_path, include_reasoning=include_reasoning
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
    write_manifest(output_dir, f"OpenCode {db}", records, changed, removed)
    atomic_write_json(
        state_path,
        {
            "updated_at": now_iso(),
            "db": str(db),
            "fingerprint": fingerprint,
            "sources": new_sources,
        },
    )
    return len(records), changed, removed


def main() -> int:
    args = parse_args()
    db = Path(args.db).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    include_reasoning = bool(args.include_reasoning) and not args.no_reasoning
    return run_watcher_loop(
        once=args.once,
        interval=args.interval,
        log_file=Path(args.log_file).expanduser() if args.log_file else None,
        status_file=Path(args.status_file).expanduser() if args.status_file else None,
        extra_status={"db": str(db), "output_dir": str(output_dir)},
        scan_fn=lambda: scan_once(db, output_dir, include_reasoning=include_reasoning),
        quiet=args.quiet,
        label="opencode",
    )


if __name__ == "__main__":
    raise SystemExit(main())
