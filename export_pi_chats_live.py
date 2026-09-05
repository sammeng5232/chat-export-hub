#!/usr/bin/env python3
"""Export pi agent sessions (~/.pi/agent/sessions) to TXT."""

from __future__ import annotations

import argparse
import json
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
    parser.add_argument("--home", default=str(home / ".pi"))
    parser.add_argument("--output-dir", default=str(home / "pi_chat_live_exports"))
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


def parse_session_file(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    session_meta: dict[str, Any] = {}
    messages: list[dict[str, Any]] = []
    model = ""
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
                if not isinstance(obj, dict):
                    continue
                otype = str(obj.get("type") or "")
                if otype == "session":
                    session_meta = obj
                elif otype == "model_change":
                    provider = str(obj.get("provider") or "")
                    model_id = str(obj.get("modelId") or "")
                    model = f"{provider}/{model_id}".strip("/")
                elif otype == "message":
                    msg = obj.get("message")
                    if isinstance(msg, dict):
                        messages.append(msg)
    except OSError:
        return {}, [], ""
    return session_meta, messages, model


def render_message(lines: list[str], counts: dict[str, int], msg: dict[str, Any]) -> None:
    role = str(msg.get("role") or "").lower()
    ts = epoch_to_iso(msg.get("timestamp"))
    text_parts: list[str] = []
    for item in msg.get("content") or []:
        if isinstance(item, str):
            text_parts.append(item)
            continue
        if not isinstance(item, dict):
            text_parts.append(str(item))
            continue
        itype = str(item.get("type") or "")
        if itype in ("text", "output_text"):
            text_parts.append(str(item.get("text") or ""))
        elif itype in ("thinking", "reasoning"):
            body = str(item.get("thinking") or item.get("text") or "").strip()
            if body:
                emit_block(lines, "REASONING", clip_text(body), ts)
                counts["reasoning"] += 1
        elif itype in ("tool_call", "tool_use"):
            name = item.get("name") or item.get("tool") or "tool"
            emit_block(
                lines,
                "TOOL CALL",
                f"Name: {name}\n{clip_text(pretty(item.get('input') or item.get('arguments')), 20_000)}",
                ts,
            )
            counts["tool_call"] += 1
        elif itype in ("tool_result", "tool_output"):
            body = item.get("content")
            if body is None:
                body = item.get("output")
            if body is None:
                body = item
            emit_block(lines, "TOOL OUTPUT", clip_text(content_to_text(body), 40_000), ts)
            counts["tool_output"] += 1
        else:
            emit_block(lines, f"OTHER ({itype or 'part'})", clip_text(pretty(item), 8_000), ts)
    text = "\n\n".join(part for part in text_parts if part.strip())
    if role == "user":
        text = extract_user_query(text)
        if should_skip_user_text(text):
            return
        if text.strip():
            emit_block(lines, "USER", text, ts)
            counts["user"] += 1
    elif role == "assistant":
        if text.strip():
            emit_block(lines, "ASSISTANT", text, ts)
            counts["assistant"] += 1
    elif text.strip():
        emit_block(lines, role.upper() or "OTHER", text, ts)


def guess_title(messages: list[dict[str, Any]]) -> str:
    for msg in messages:
        if str(msg.get("role") or "").lower() != "user":
            continue
        text = extract_user_query(content_to_text(msg.get("content")))
        if text.strip() and not should_skip_user_text(text):
            return one_line(text, 80)
    return ""


def scan_once(home: Path, output_dir: Path) -> tuple[int, int, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / ".export_state.json"
    state_obj = load_json(state_path, {"sources": {}})
    old_sources: dict[str, Any] = state_obj.get("sources") or {}
    new_sources: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    changed = 0
    ordinal = 0

    sessions_root = home / "agent" / "sessions"
    if sessions_root.is_dir():
        for path in sorted(sessions_root.glob("*/*.jsonl")):
            ordinal += 1
            source_key = str(path)
            seen.add(source_key)
            mtime_ns, size = file_fingerprint(path)
            session_meta, messages, model = parse_session_file(path)
            sid = str(session_meta.get("id") or path.stem)
            created = str(session_meta.get("timestamp") or "")
            cwd = str(session_meta.get("cwd") or "")
            try:
                updated = epoch_to_iso(path.stat().st_mtime)
            except OSError:
                updated = ""
            prefix = first_iso_timestamp_prefix(created or None, ordinal)
            title = guess_title(messages)
            output_path = filesystem_safe_output_path(
                output_dir, f"{prefix}__{sid[:36]}__", title or f"Pi {sid[:16]}"
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
                counts = empty_counts()
                lines = [
                    "Local pi agent chat export",
                    f"Title: {title or 'untitled'}",
                    f"Session ID: {sid}",
                ]
                if created:
                    lines.append(f"Created: {created}")
                if updated:
                    lines.append(f"Updated: {updated}")
                if cwd:
                    lines.append(f"CWD: {cwd}")
                if model:
                    lines.append(f"Model: {model}")
                lines.extend([f"Source: {path}", f"Exported At: {now_iso()}"])
                for msg in messages:
                    render_message(lines, counts, msg)
                atomic_write_text(output_path, scrub_internal_lines("\n".join(lines) + "\n"))
                record = {
                    "session_id": sid,
                    "source_id": sid,
                    "title": title or "untitled",
                    "kind": "session",
                    "source": source_key,
                    "output": str(output_path),
                    "created": created,
                    "updated": updated,
                    "cwd": cwd,
                    "model": model,
                    "counts": counts,
                    "bytes": output_path.stat().st_size,
                }
                reuse_or_replace_output(old.get("output"), output_path)
                changed += 1
            else:
                record = dict(old)
                record["title"] = old.get("title") or title or "untitled"
                record["created"] = old.get("created") or created
                record["updated"] = old.get("updated") or updated
            record["mtime_ns"] = mtime_ns
            record["size"] = size
            new_sources[source_key] = record
            records.append(record)

    removed = prune_removed_sources(old_sources, seen)
    write_manifest(output_dir, "Pi Agent", records, changed, removed)
    atomic_write_json(
        state_path,
        {
            "updated_at": now_iso(),
            "home": str(home),
            "sources": new_sources,
        },
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
        extra_status={"home": str(home), "output_dir": str(output_dir)},
        scan_fn=lambda: scan_once(home, output_dir),
        quiet=args.quiet,
        label="pi",
    )


if __name__ == "__main__":
    raise SystemExit(main())
