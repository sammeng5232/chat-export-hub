#!/usr/bin/env python3
"""Incrementally export WorkBuddy / WorkBuddy AI local chats to TXT."""

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


def parse_args() -> argparse.Namespace:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=str(home / ".workbuddy"))
    parser.add_argument("--output-dir", default=str(home / "workbuddy_cn_chat_live_exports"))
    parser.add_argument("--label", default="workbuddy")
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--log-file")
    parser.add_argument("--status-file")
    return parser.parse_args()


def connect_ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def pretty(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except TypeError:
        return str(value)


def load_db_sessions(db: Path) -> dict[str, dict[str, Any]]:
    con = connect_ro(db)
    if con is None:
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        rows = con.execute(
            """
            SELECT id, cwd, title, custom_title, status, created_at, updated_at,
                   deleted_at, mode, model, last_activity_at, project_id
            FROM sessions
            """
        ).fetchall()
    except sqlite3.Error:
        con.close()
        return {}
    for row in rows:
        sid = str(row["id"])
        title = (row["custom_title"] or row["title"] or "").strip()
        out[sid] = {
            "id": sid,
            "cwd": row["cwd"] or "",
            "title": title,
            "status": row["status"] or "",
            "created": epoch_to_iso(row["created_at"]),
            "updated": epoch_to_iso(row["last_activity_at"] or row["updated_at"]),
            "model": row["model"] or "",
            "mode": row["mode"] or "",
            "deleted": bool(row["deleted_at"]),
            "project_id": row["project_id"] or "",
        }
    con.close()
    return out


def iter_jsonl_files(projects_root: Path) -> list[Path]:
    if not projects_root.exists():
        return []
    return sorted(p for p in projects_root.rglob("*.jsonl") if p.is_file())


def session_id_from_jsonl(path: Path) -> str:
    return path.stem


def first_user_title(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "ai-title":
                    title = obj.get("title") or obj.get("text") or ""
                    if str(title).strip():
                        return one_line(str(title), 80)
                if obj.get("type") == "message" and str(obj.get("role") or "").lower() == "user":
                    text = extract_user_query(content_to_text(obj.get("content") or obj.get("text")))
                    if should_skip_user_text(text):
                        continue
                    if text.strip():
                        return one_line(text, 80)
    except OSError:
        return ""
    return ""


def export_jsonl(
    path: Path,
    meta: dict[str, Any],
    title: str,
    output_path: Path,
    label: str,
) -> dict[str, Any]:
    counts = empty_counts()
    session_id = meta.get("id") or session_id_from_jsonl(path)
    lines: list[str] = [
        f"Local {label} chat export",
        f"Title: {title}",
        f"Session ID: {session_id}",
    ]
    if meta.get("created"):
        lines.append(f"Created: {meta['created']}")
    if meta.get("updated"):
        lines.append(f"Updated: {meta['updated']}")
    if meta.get("cwd"):
        lines.append(f"CWD: {meta['cwd']}")
    if meta.get("model"):
        lines.append(f"Model: {meta['model']}")
    if meta.get("mode"):
        lines.append(f"Mode: {meta['mode']}")
    lines.extend(
        [
            f"Source: {path}",
            f"Exported At: {now_iso()}",
            "",
            "Note: file-history-snapshot events are omitted.",
        ]
    )

    if not path.exists():
        emit_block(lines, "WARNING", f"Missing transcript {path}")
    else:
        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    counts["parse_error"] += 1
                    emit_block(lines, f"PARSE ERROR line {line_no}", str(exc))
                    continue
                etype = str(obj.get("type") or "")
                ts = epoch_to_iso(obj.get("timestamp") or obj.get("created_at") or obj.get("time"))
                if etype in {"file-history-snapshot", "file_history_snapshot"}:
                    continue
                if etype == "ai-title":
                    continue
                if etype == "message":
                    role = str(obj.get("role") or "unknown").lower()
                    text = content_to_text(obj.get("content") if "content" in obj else obj.get("text"))
                    if role == "user":
                        text = extract_user_query(text)
                        if should_skip_user_text(text):
                            continue
                        if not text.strip():
                            continue
                        emit_block(lines, "USER", text, ts)
                        counts["user"] += 1
                    elif role == "assistant":
                        if text.strip():
                            emit_block(lines, "ASSISTANT", text, ts)
                            counts["assistant"] += 1
                    elif role == "system":
                        counts["system"] += 1
                    else:
                        if text.strip():
                            emit_block(lines, role.upper(), text, ts)
                    continue
                if etype == "reasoning":
                    body = content_to_text(obj.get("text") or obj.get("content") or obj.get("summary"))
                    if body.strip():
                        emit_block(lines, "REASONING", clip_text(body), ts)
                        counts["reasoning"] += 1
                    continue
                if etype in {"function_call", "tool_call", "tool-call"}:
                    fn = obj.get("function") if isinstance(obj.get("function"), dict) else {}
                    name = (
                        obj.get("name")
                        or obj.get("tool")
                        or fn.get("name")
                        or "tool"
                    )
                    call_id = obj.get("id") or obj.get("call_id") or obj.get("callID") or ""
                    arguments = (
                        obj.get("arguments")
                        or obj.get("input")
                        or obj.get("params")
                        or fn.get("arguments")
                    )
                    body = [f"Name: {name}"]
                    if call_id:
                        body.append(f"Call ID: {call_id}")
                    if arguments is not None:
                        body.extend(["Input:", clip_text(pretty(arguments), 40_000)])
                    emit_block(lines, "TOOL CALL", "\n".join(body), ts)
                    counts["tool_call"] += 1
                    continue
                if etype in {"function_call_result", "tool_result", "tool-result", "function_result"}:
                    call_id = (
                        obj.get("call_id")
                        or obj.get("id")
                        or obj.get("tool_call_id")
                        or ""
                    )
                    output = (
                        obj.get("output")
                        or obj.get("result")
                        or obj.get("content")
                        or obj.get("error")
                    )
                    body_parts = []
                    if call_id:
                        body_parts.append(f"Call ID: {call_id}")
                    if output is not None:
                        body_parts.append(clip_text(pretty(output), 80_000))
                    emit_block(lines, "TOOL OUTPUT", "\n".join(body_parts) or "(empty)", ts)
                    counts["tool_output"] += 1
                    continue
                if etype in {"system", "system_reminder"}:
                    continue

    output_text = scrub_internal_lines("\n".join(lines) + "\n")
    atomic_write_text(output_path, output_text)
    return {
        "session_id": session_id,
        "source_id": session_id,
        "title": title,
        "kind": "session",
        "source": str(path),
        "output": str(output_path),
        "created": meta.get("created", ""),
        "updated": meta.get("updated", ""),
        "cwd": meta.get("cwd", ""),
        "model": meta.get("model", ""),
        "counts": counts,
        "bytes": output_path.stat().st_size,
    }


def scan_once(home: Path, output_dir: Path, label: str) -> tuple[int, int, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / ".export_state.json"
    state_obj = load_json(state_path, {"sources": {}})
    old_sources: dict[str, Any] = state_obj.get("sources") or {}
    new_sources: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    changed = 0

    db = home / "workbuddy.db"
    projects = home / "projects"
    db_sessions = load_db_sessions(db)
    jsonl_files = iter_jsonl_files(projects)

    found_ids: set[str] = set()
    items: list[tuple[str, Path | None, dict[str, Any]]] = []
    for path in jsonl_files:
        sid = session_id_from_jsonl(path)
        found_ids.add(sid)
        meta = dict(db_sessions.get(sid) or {"id": sid})
        items.append((sid, path, meta))
    for sid, meta in db_sessions.items():
        if sid in found_ids or meta.get("deleted"):
            continue
        items.append((sid, None, meta))

    for ordinal, (sid, path, meta) in enumerate(items, 1):
        source_key = str(path) if path is not None else f"{home}#session:{sid}"
        seen.add(source_key)
        title = meta.get("title") or ""
        if path is not None and (not title or title.lower() in {"untitled", "new session"}):
            title = first_user_title(path) or title
        if not title:
            title = sid
        title = one_line(title, 80)
        prefix = first_iso_timestamp_prefix(meta.get("created"), ordinal)
        output_path = filesystem_safe_output_path(output_dir, f"{prefix}__{sid}__", title)
        old = old_sources.get(source_key, {})
        mtime_ns = size = 0
        if path is not None:
            mtime_ns, size = file_fingerprint(path)
        db_updated = meta.get("updated") or ""
        must_export = (
            not old
            or old.get("mtime_ns") != mtime_ns
            or old.get("size") != size
            or old.get("db_updated") != db_updated
            or old.get("title") != title
            or old.get("output") != str(output_path)
            or not Path(old.get("output") or "").exists()
            or not output_path.exists()
        )
        if path is None and not must_export:
            record = dict(old)
            record["output"] = str(output_path)
        elif must_export:
            export_path = path if path is not None else home / "sessions" / f"{sid}.missing.jsonl"
            record = export_jsonl(export_path if path is not None else Path(str(export_path)), meta, title, output_path, label)
            reuse_or_replace_output(old.get("output"), output_path)
            changed += 1
        else:
            record = {
                "session_id": sid,
                "source_id": sid,
                "title": title,
                "kind": "session",
                "source": source_key,
                "output": str(output_path),
                "created": meta.get("created") or old.get("created", ""),
                "updated": meta.get("updated") or old.get("updated", ""),
                "cwd": meta.get("cwd") or old.get("cwd", ""),
                "model": meta.get("model") or old.get("model", ""),
                "counts": old.get("counts") or empty_counts(),
                "bytes": old.get(
                    "bytes",
                    output_path.stat().st_size if output_path.exists() else 0,
                ),
            }
        record["mtime_ns"] = mtime_ns
        record["size"] = size
        record["db_updated"] = db_updated
        new_sources[source_key] = record
        records.append(record)

    removed = prune_removed_sources(old_sources, seen)
    write_manifest(output_dir, f"{label} {home}", records, changed, removed)
    atomic_write_json(
        state_path,
        {"updated_at": now_iso(), "home": str(home), "label": label, "sources": new_sources},
    )
    return len(records), changed, removed


def main() -> int:
    args = parse_args()
    home = Path(args.home).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    return run_watcher_loop(
        once=args.once,
        interval=args.interval,
        log_file=Path(args.log_file).expanduser() if args.log_file else None,
        status_file=Path(args.status_file).expanduser() if args.status_file else None,
        extra_status={"home": str(home), "output_dir": str(output_dir), "label": args.label},
        scan_fn=lambda: scan_once(home, output_dir, args.label),
        quiet=args.quiet,
        label=args.label,
    )


if __name__ == "__main__":
    raise SystemExit(main())
