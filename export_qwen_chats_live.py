#!/usr/bin/env python3
"""Export Qwen Code sessions (~/.qwen/projects/<cwd>/chats/*.jsonl) to TXT."""

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


def parse_args() -> argparse.Namespace:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=str(home / ".qwen"))
    parser.add_argument("--output-dir", default=str(home / "qwen_chat_live_exports"))
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


def read_jsonl_records(path: Path, counts: dict[str, int]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    counts["parse_error"] += 1
                    continue
                if isinstance(obj, dict):
                    records.append(obj)
    except OSError:
        return []
    return records


def render_record(
    lines: list[str], counts: dict[str, int], rec: dict[str, Any]
) -> None:
    rtype = str(rec.get("type") or "").lower()
    if rtype == "system":
        # ui_telemetry / slash_command / attribution_snapshot /
        # file_history_snapshot — internal bookkeeping, not chat content.
        return
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return
    role = str(msg.get("role") or "").lower()
    ts = epoch_to_iso(rec.get("timestamp"))
    parts = msg.get("parts") or []
    if not isinstance(parts, list):
        parts = []
    text_parts: list[str] = []
    for part in parts:
        if isinstance(part, str):
            text_parts.append(part)
            continue
        if not isinstance(part, dict):
            text_parts.append(str(part))
            continue
        if part.get("thought"):
            body = str(part.get("text") or "").strip()
            if body:
                emit_block(lines, "REASONING", clip_text(body), ts)
                counts["reasoning"] += 1
            continue
        if "functionCall" in part:
            call = part.get("functionCall") or {}
            name = call.get("name") or "tool"
            args = call.get("args")
            if args is None:
                args = call.get("parameters")
            emit_block(
                lines,
                "TOOL CALL",
                f"Name: {name}\n{clip_text(pretty(args), 20_000)}",
                ts,
            )
            counts["tool_call"] += 1
            continue
        if "functionResponse" in part:
            resp = part.get("functionResponse") or {}
            name = resp.get("name") or "tool"
            body = resp.get("response")
            if body is None:
                body = resp
            emit_block(
                lines,
                "TOOL OUTPUT",
                f"Name: {name}\n{clip_text(content_to_text(body), 40_000)}",
                ts,
            )
            counts["tool_output"] += 1
            continue
        if "text" in part:
            text_parts.append(str(part.get("text") or ""))
            continue
        # inlineData / fileData / unknown part shapes
        keys = ",".join(list(part.keys())[:4]) or "part"
        emit_block(lines, f"OTHER ({keys})", clip_text(pretty(part), 8_000), ts)
    text = "\n\n".join(part for part in text_parts if part.strip())
    if role == "user":
        if not text.strip() or should_skip_user_text(text):
            return
        emit_block(lines, "USER", text, ts)
        counts["user"] += 1
    elif role in ("assistant", "model"):
        if text.strip():
            emit_block(lines, "ASSISTANT", text, ts)
            counts["assistant"] += 1
    elif text.strip():
        emit_block(lines, role.upper() or "OTHER", text, ts)


def record_text(rec: dict[str, Any]) -> str:
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return ""
    chunks: list[str] = []
    for part in msg.get("parts") or []:
        if isinstance(part, dict) and not part.get("thought") and "text" in part:
            chunks.append(str(part.get("text") or ""))
    return "\n\n".join(chunk for chunk in chunks if chunk.strip())


def guess_title(records: list[dict[str, Any]]) -> str:
    for rec in records:
        if str(rec.get("type") or "").lower() != "user":
            continue
        text = record_text(rec).strip()
        if not text or should_skip_user_text(text) or text.startswith("/"):
            continue
        return one_line(text, 80)
    return ""


def session_meta(records: list[dict[str, Any]]) -> dict[str, str]:
    created = ""
    updated = ""
    cwd = ""
    version = ""
    models: list[str] = []
    for rec in records:
        ts = str(rec.get("timestamp") or "")
        if ts:
            if not created:
                created = ts
            updated = ts
        if not cwd and rec.get("cwd"):
            cwd = str(rec["cwd"])
        if rec.get("version"):
            version = str(rec["version"])
        if rec.get("model") and str(rec["model"]) not in models:
            models.append(str(rec["model"]))
    return {
        "created": epoch_to_iso(created) if created else "",
        "updated": epoch_to_iso(updated) if updated else "",
        "cwd": cwd,
        "version": version,
        "model": ", ".join(models),
    }


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

    scan_homes: list[tuple[Path, str]] = [(home, "")] + wsl_agent_homes(".qwen")
    for scan_home, home_tag in scan_homes:
        projects_root = scan_home / "projects"
        if not projects_root.is_dir():
            continue
        for project_dir in sorted(p for p in projects_root.iterdir() if p.is_dir()):
            chats_dir = project_dir / "chats"
            if not chats_dir.is_dir():
                continue
            for session_file in sorted(
                p for p in chats_dir.iterdir() if p.is_file() and p.suffix == ".jsonl"
            ):
                sid = session_file.stem
                ordinal += 1
                source_key = str(session_file)
                seen.add(source_key)
                mtime_ns, size = file_fingerprint(session_file)
                old = old_sources.get(source_key, {})
                if (
                    old
                    and old.get("mtime_ns") == mtime_ns
                    and old.get("size") == size
                    and Path(old.get("output") or "").exists()
                ):
                    # Unchanged since the last scan — reuse the prior export.
                    record = dict(old)
                    record["mtime_ns"] = mtime_ns
                    record["size"] = size
                    new_sources[source_key] = record
                    records.append(record)
                    continue

                counts = empty_counts()
                session_records = read_jsonl_records(session_file, counts)
                meta = session_meta(session_records)
                if not meta["updated"]:
                    try:
                        meta["updated"] = epoch_to_iso(session_file.stat().st_mtime)
                    except OSError:
                        pass
                if not meta["cwd"]:
                    runtime = load_json(
                        session_file.with_name(sid + ".runtime.json"), {}
                    )
                    work_dir = (
                        str(runtime.get("work_dir") or "")
                        if isinstance(runtime, dict)
                        else ""
                    )
                    if work_dir:
                        meta["cwd"] = work_dir
                title = guess_title(session_records) or f"Qwen {sid}"
                prefix = first_iso_timestamp_prefix(meta["created"] or None, ordinal)
                stem = (
                    f"{prefix}__{sid}__" if not home_tag else f"{prefix}__{home_tag}_{sid}__"
                )
                output_path = filesystem_safe_output_path(output_dir, stem, title)
                lines = [
                    "Local Qwen Code chat export",
                    f"Title: {title}",
                    f"Session ID: {sid}",
                ]
                if meta["created"]:
                    lines.append(f"Created: {meta['created']}")
                if meta["updated"]:
                    lines.append(f"Updated: {meta['updated']}")
                if meta["cwd"]:
                    lines.append(f"CWD: {meta['cwd']}")
                if meta["model"]:
                    lines.append(f"Model: {meta['model']}")
                if meta["version"]:
                    lines.append(f"Qwen Version: {meta['version']}")
                lines.extend([f"Source: {session_file}", f"Exported At: {now_iso()}"])
                for rec in session_records:
                    render_record(lines, counts, rec)
                atomic_write_text(
                    output_path, scrub_internal_lines("\n".join(lines) + "\n")
                )
                record = {
                    "session_id": sid,
                    "source_id": sid,
                    "title": title,
                    "kind": "session",
                    "source": source_key,
                    "output": str(output_path),
                    "created": meta["created"],
                    "updated": meta["updated"],
                    "cwd": meta["cwd"],
                    "model": meta["model"],
                    "counts": counts,
                    "bytes": output_path.stat().st_size,
                }
                reuse_or_replace_output(old.get("output"), output_path)
                changed += 1
                record["mtime_ns"] = mtime_ns
                record["size"] = size
                new_sources[source_key] = record
                records.append(record)

    removed = prune_removed_sources(
        old_sources, seen, retained_sources=new_sources, retained_records=records
    )
    write_manifest(output_dir, "Qwen Code", records, changed, removed)
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
        label="qwen",
    )


if __name__ == "__main__":
    raise SystemExit(main())
