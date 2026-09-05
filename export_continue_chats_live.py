#!/usr/bin/env python3
"""Export Continue extension sessions (~/.continue/sessions) to TXT."""

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
    parser.add_argument("--home", default=str(home / ".continue"))
    parser.add_argument("--output-dir", default=str(home / "continue_chat_live_exports"))
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


def render_entry(lines: list[str], counts: dict[str, int], entry: dict[str, Any]) -> None:
    msg = entry.get("message") if isinstance(entry.get("message"), dict) else {}
    if not msg:
        return
    role = str(msg.get("role") or "").lower()
    ts = epoch_to_iso(msg.get("timestamp") or entry.get("timestamp"))
    text = content_to_text(msg.get("content"))
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
    elif role in ("thinking", "reasoning"):
        if text.strip():
            emit_block(lines, "REASONING", clip_text(text), ts)
            counts["reasoning"] += 1
    elif role == "tool":
        emit_block(lines, "TOOL CALL", clip_text(pretty(msg), 20_000), ts)
        counts["tool_call"] += 1
    elif text.strip():
        emit_block(lines, role.upper() or "OTHER", text, ts)


def guess_title(history: list[dict[str, Any]]) -> str:
    for entry in history:
        msg = entry.get("message") if isinstance(entry.get("message"), dict) else {}
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

    sessions_dir = home / "sessions"
    index: dict[str, dict[str, Any]] = {}
    if (sessions_dir / "sessions.json").is_file():
        idx = load_json(sessions_dir / "sessions.json", [])
        if isinstance(idx, list):
            for entry in idx:
                if isinstance(entry, dict) and entry.get("sessionId"):
                    index[str(entry["sessionId"])] = entry

    if sessions_dir.is_dir():
        for path in sorted(sessions_dir.glob("*.json")):
            if path.name == "sessions.json":
                continue
            ordinal += 1
            source_key = str(path)
            seen.add(source_key)
            mtime_ns, size = file_fingerprint(path)
            data = load_json(path, {})
            if not isinstance(data, dict):
                data = {}
            sid = str(data.get("sessionId") or path.stem)
            idx_entry = index.get(sid, {})
            title = one_line(str(data.get("title") or idx_entry.get("title") or ""), 80)
            history = [e for e in data.get("history") or [] if isinstance(e, dict)]
            created = (
                epoch_to_iso(idx_entry.get("dateCreated"))
                or epoch_to_iso(data.get("createdAt"))
                or ""
            )
            try:
                updated = epoch_to_iso(path.stat().st_mtime)
            except OSError:
                updated = ""
            cwd = str(data.get("workspaceDirectory") or idx_entry.get("workspaceDirectory") or "")
            prefix = first_iso_timestamp_prefix(created or None, ordinal)
            if not title:
                title = guess_title(history)
            output_path = filesystem_safe_output_path(
                output_dir, f"{prefix}__{sid}__", title or f"Continue {sid}"
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
                    "Local Continue chat export",
                    f"Title: {title}",
                    f"Session ID: {sid}",
                ]
                if created:
                    lines.append(f"Created: {created}")
                if updated:
                    lines.append(f"Updated: {updated}")
                if cwd:
                    lines.append(f"CWD: {cwd}")
                lines.extend([f"Source: {path}", f"Exported At: {now_iso()}"])
                for entry in history:
                    render_entry(lines, counts, entry)
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
                    "model": "",
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

    removed = prune_removed_sources(old_sources, seen)
    write_manifest(output_dir, "Continue", records, changed, removed)
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
        label="continue",
    )


if __name__ == "__main__":
    raise SystemExit(main())
