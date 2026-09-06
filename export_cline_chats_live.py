#!/usr/bin/env python3
"""Export Cline CLI sessions (~/.cline/data/sessions) to TXT."""

from __future__ import annotations

import argparse
import json
import re
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
    wsl_agent_homes,
    write_manifest,
)

USER_INPUT_RE = re.compile(
    r"<user_input[^>]*>\s*(.*?)\s*</user_input>", re.DOTALL | re.IGNORECASE
)


def parse_args() -> argparse.Namespace:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=str(home / ".cline"))
    parser.add_argument("--output-dir", default=str(home / "cline_chat_live_exports"))
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


def unwrap_user_input(text: str) -> str:
    matches = USER_INPUT_RE.findall(text or "")
    if matches:
        return "\n\n".join(m.strip() for m in matches if m.strip()).strip()
    return extract_user_query(text)


def render_message(lines: list[str], counts: dict[str, int], msg: dict[str, Any]) -> None:
    role = str(msg.get("role") or "").lower()
    ts = epoch_to_iso(msg.get("ts"))
    text_parts: list[str] = []
    for item in msg.get("content") or []:
        if isinstance(item, str):
            text_parts.append(item)
            continue
        if not isinstance(item, dict):
            text_parts.append(str(item))
            continue
        itype = str(item.get("type") or "")
        if itype == "text":
            text_parts.append(str(item.get("text") or ""))
        elif itype in ("thinking", "reasoning"):
            body = str(item.get("thinking") or item.get("text") or "").strip()
            if body:
                emit_block(lines, "REASONING", clip_text(body), ts)
                counts["reasoning"] += 1
        elif itype in ("tool_use", "tool_call"):
            name = item.get("name") or "tool"
            emit_block(
                lines,
                "TOOL CALL",
                f"Name: {name}\n{clip_text(pretty(item.get('input')), 20_000)}",
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
        text = unwrap_user_input(text)
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
        text = unwrap_user_input(content_to_text(msg.get("content")))
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

    scan_homes: list[tuple[Path, str]] = [(home, "")] + wsl_agent_homes(".cline")
    for scan_home, home_tag in scan_homes:
        sessions_root = scan_home / "data" / "sessions"
        if not sessions_root.is_dir():
            continue
        for session_dir in sorted(p for p in sessions_root.iterdir() if p.is_dir()):
            sid = session_dir.name
            messages_path = session_dir / f"{sid}.messages.json"
            if not messages_path.is_file():
                continue
            ordinal += 1
            source_key = str(messages_path)
            seen.add(source_key)
            mtime_ns, size = file_fingerprint(messages_path)
            meta = load_json(session_dir / f"{sid}.json", {})
            meta_meta = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
            title = one_line(str(meta_meta.get("title") or ""), 80)
            model = str(meta.get("model") or "")
            cwd = str(meta.get("cwd") or "")
            created = str(meta.get("started_at") or "")
            try:
                updated = str(meta.get("ended_at") or "") or epoch_to_iso(
                    messages_path.stat().st_mtime
                )
            except OSError:
                updated = ""
            prefix = first_iso_timestamp_prefix(created or None, ordinal)
            if not title:
                payload = load_json(messages_path, {})
                messages = (
                    [m for m in payload.get("messages") or [] if isinstance(m, dict)]
                    if isinstance(payload, dict)
                    else []
                )
                title = guess_title(messages)
            stem = (
                f"{prefix}__{sid}__" if not home_tag else f"{prefix}__{home_tag}_{sid}__"
            )
            output_path = filesystem_safe_output_path(
                output_dir, stem, title or f"Cline {sid}"
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
                payload = load_json(messages_path, {})
                messages = (
                    [m for m in payload.get("messages") or [] if isinstance(m, dict)]
                    if isinstance(payload, dict)
                    else []
                )
                if not title:
                    title = guess_title(messages) or f"Cline {sid}"
                counts = empty_counts()
                lines = [
                    "Local Cline CLI chat export",
                    f"Title: {title}",
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
                lines.extend([f"Source: {messages_path}", f"Exported At: {now_iso()}"])
                for msg in messages:
                    render_message(lines, counts, msg)
                atomic_write_text(output_path, scrub_internal_lines("\n".join(lines) + "\n"))
                record = {
                    "session_id": sid,
                    "source_id": sid,
                    "title": title,
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
                record["title"] = old.get("title") or title
                record["created"] = old.get("created") or created
                record["updated"] = old.get("updated") or updated
            record["mtime_ns"] = mtime_ns
            record["size"] = size
            new_sources[source_key] = record
            records.append(record)

    removed = prune_removed_sources(old_sources, seen, retained_sources=new_sources, retained_records=records)
    write_manifest(output_dir, "Cline CLI", records, changed, removed)
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
        label="cline",
    )


if __name__ == "__main__":
    raise SystemExit(main())
