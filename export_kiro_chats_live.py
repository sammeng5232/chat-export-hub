#!/usr/bin/env python3
"""Export Kiro (kirocode) local chats when session stores exist."""

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
    stage_wsl_sqlite,
    wsl_agent_homes,
    write_manifest,
)


def parse_args() -> argparse.Namespace:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(home / "kiro_chat_live_exports"))
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--log-file")
    parser.add_argument("--status-file")
    return parser.parse_args()


def pretty(value: Any) -> str:
    if isinstance(value, str) or value is None:
        return value or ""
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except TypeError:
        return str(value)


def session_files() -> list[tuple[Path, str]]:
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", ""))
    roots: list[tuple[Path, str]] = [
        (home / ".kiro" / "sessions", ""),
        (home / ".kiro" / "chats", ""),
        (appdata / "Kiro" / "User" / "globalStorage" / "kiro.kiroagent", ""),
    ]
    # Kiro CLI/agent stores are also commonly present in WSL user homes.
    for wsl_root, tag in wsl_agent_homes(".kiro"):
        roots.extend(((wsl_root / "sessions", tag), (wsl_root / "chats", tag)))
    for wsl_root, tag in wsl_agent_homes(".config/Kiro/User/globalStorage/kiro.kiroagent"):
        roots.append((wsl_root, tag))
    # Windows-guest VMs keep the IDE store under AppData/Roaming.
    for wsl_root, tag in wsl_agent_homes("AppData/Roaming/Kiro/User/globalStorage/kiro.kiroagent"):
        roots.append((wsl_root, tag))

    found: list[tuple[Path, str]] = []
    for root, tag in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() in {".json", ".jsonl", ".md"} and path.stat().st_size > 2:
                found.append((path, tag))
    return found


def vscdb_sources() -> list[tuple[Path, str, Path]]:
    appdata = Path(os.environ.get("APPDATA", ""))
    out: list[tuple[Path, str, Path]] = []
    db = appdata / "Kiro" / "User" / "globalStorage" / "state.vscdb"
    if db.exists():
        out.append((db, "", db))
    for wsl_root, tag in wsl_agent_homes(".config/Kiro/User/globalStorage"):
        unc_db = wsl_root / "state.vscdb"
        if not unc_db.exists():
            continue
        staged = stage_wsl_sqlite(unc_db)
        if staged is not None:
            out.append((staged, tag, unc_db))
    # Windows-guest VMs keep state.vscdb under AppData/Roaming.
    for wsl_root, tag in wsl_agent_homes("AppData/Roaming/Kiro/User/globalStorage"):
        vm_db = wsl_root / "state.vscdb"
        if not vm_db.exists():
            continue
        staged = stage_wsl_sqlite(vm_db)
        if staged is not None:
            out.append((staged, tag, vm_db))
    return out


def vscdb_chat_entries() -> list[tuple[str, Any, Path, str, Path]]:
    """Read Kiro state stores, retaining WSL tag and display source path."""
    out: list[tuple[str, Any, Path, str, Path]] = []
    for db, tag, display_db in vscdb_sources():
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
            if not any(tok in low for tok in ("chat", "kiro", "session", "agent", "conversation")):
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
            out.append((str(key), payload, db, tag, display_db))
    return out


def export_payload(
    source: str,
    title: str,
    session_id: str,
    payload: Any,
    output_path: Path,
    extra: dict[str, Any],
) -> dict[str, Any]:
    counts = empty_counts()
    lines = [
        "Local Kiro chat export",
        f"Title: {title}",
        f"Session ID: {session_id}",
        f"Source: {source}",
        f"Exported At: {now_iso()}",
        "",
    ]
    messages: list[Any]
    if isinstance(payload, list):
        messages = payload
    elif isinstance(payload, dict):
        messages = []
        for key in ("messages", "entries", "items", "history", "conversations", "sessions"):
            if isinstance(payload.get(key), (list, dict)):
                value = payload[key]
                if isinstance(value, dict):
                    messages = list(value.values()) if value else []
                else:
                    messages = value
                break
        if not messages and payload:
            emit_block(lines, "STORE", clip_text(pretty(payload), 20_000))
    else:
        messages = []

    for obj in messages:
        if not isinstance(obj, dict):
            continue
        role = str(obj.get("role") or obj.get("type") or obj.get("author") or "").lower()
        ts = epoch_to_iso(obj.get("timestamp") or obj.get("createdAt") or obj.get("time"))
        text = content_to_text(obj.get("content") or obj.get("text") or obj.get("message") or obj.get("body"))
        if role in {"user", "human"}:
            text = extract_user_query(text)
            if should_skip_user_text(text) or not text.strip():
                continue
            emit_block(lines, "USER", text, ts)
            counts["user"] += 1
        elif role in {"assistant", "ai", "bot", "kiro"}:
            if text.strip():
                emit_block(lines, "ASSISTANT", text, ts)
                counts["assistant"] += 1
        elif role in {"tool", "tool_call"}:
            emit_block(lines, "TOOL CALL", clip_text(pretty(obj), 20_000), ts)
            counts["tool_call"] += 1
        elif text.strip():
            emit_block(lines, (role or "OTHER").upper(), text, ts)

    atomic_write_text(output_path, scrub_internal_lines("\n".join(lines) + "\n"))
    return {
        "session_id": session_id,
        "source_id": session_id,
        "title": title,
        "kind": "session" if counts["user"] or counts["assistant"] else "store",
        "source": source,
        "output": str(output_path),
        "created": extra.get("created", ""),
        "updated": extra.get("updated", ""),
        "cwd": extra.get("cwd", ""),
        "model": extra.get("model", ""),
        "counts": counts,
        "bytes": output_path.stat().st_size,
    }


def scan_once(output_dir: Path) -> tuple[int, int, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / ".export_state.json"
    old_sources: dict[str, Any] = (load_json(state_path, {"sources": {}}) or {}).get("sources") or {}
    new_sources: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    changed = 0
    ordinal = 0

    for path, home_tag in session_files():
        if path.name in {".export_state.json"}:
            continue
        ordinal += 1
        source_key = str(path)
        seen.add(source_key)
        mtime_ns, size = file_fingerprint(path)
        session_id = path.stem
        title = one_line(f"Kiro {session_id}", 80)
        prefix = first_iso_timestamp_prefix(None, ordinal)
        stem = (
            f"{prefix}__{session_id}__"
            if not home_tag
            else f"{prefix}__{home_tag}_{session_id}__"
        )
        output_path = filesystem_safe_output_path(output_dir, stem, title)
        old = old_sources.get(source_key, {})
        must_export = (
            not old
            or old.get("mtime_ns") != mtime_ns
            or old.get("size") != size
            or old.get("output") != str(output_path)
            or not Path(old.get("output") or "").exists()
        )
        if must_export:
            if path.suffix.lower() == ".jsonl":
                payload = []
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                payload.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
            elif path.suffix.lower() == ".md":
                payload = [{"role": "assistant", "content": path.read_text(encoding="utf-8", errors="replace")}]
            else:
                payload = load_json(path, {})
            record = export_payload(
                source_key,
                title,
                session_id,
                payload,
                output_path,
                {
                    "updated": epoch_to_iso(path.stat().st_mtime),
                    "created": epoch_to_iso(path.stat().st_ctime),
                },
            )
            reuse_or_replace_output(old.get("output"), output_path)
            changed += 1
        else:
            record = {
                "session_id": session_id,
                "source_id": session_id,
                "title": old.get("title") or title,
                "kind": old.get("kind", "session"),
                "source": source_key,
                "output": str(output_path),
                "created": old.get("created", ""),
                "updated": old.get("updated", ""),
                "cwd": "",
                "model": "",
                "counts": old.get("counts") or empty_counts(),
                "bytes": old.get("bytes", 0),
            }
        record["mtime_ns"] = mtime_ns
        record["size"] = size
        new_sources[source_key] = record
        records.append(record)

    skip_key_needles = (
        "onboarding",
        "hidden",
        "view.extension",
        "welcome",
        "telemetry",
        "memento",
    )
    for key, payload, db, home_tag, display_db in vscdb_chat_entries():
        low_key = key.lower()
        if any(tok in low_key for tok in skip_key_needles):
            continue
        if not isinstance(payload, (dict, list)):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("entries"), dict):
            if not payload["entries"]:
                continue
        elif isinstance(payload, dict) and not any(
            isinstance(payload.get(k), (list, dict))
            for k in ("messages", "entries", "items", "history", "sessions", "conversations")
        ):
            continue
        source_key = f"{display_db}#{key}"
        seen.add(source_key)
        ordinal += 1
        session_id = key.replace("/", "_")[-80:]
        title = one_line(f"Kiro store {key}", 80)
        prefix = first_iso_timestamp_prefix(None, ordinal)
        stem = (
            f"{prefix}__{session_id}__"
            if not home_tag
            else f"{prefix}__{home_tag}_{session_id}__"
        )
        output_path = filesystem_safe_output_path(output_dir, stem, title)
        old = old_sources.get(source_key, {})
        fp = file_fingerprint(db)
        must_export = (
            not old
            or old.get("mtime_ns") != fp[0]
            or old.get("size") != fp[1]
            or not Path(old.get("output") or "").exists()
        )
        if must_export:
            record = export_payload(source_key, title, session_id, payload, output_path, {})
            reuse_or_replace_output(old.get("output"), output_path)
            changed += 1
        else:
            record = dict(old)
        record["mtime_ns"], record["size"] = fp
        new_sources[source_key] = record
        records.append(record)

    removed = prune_removed_sources(old_sources, seen, retained_sources=new_sources, retained_records=records)
    write_manifest(output_dir, "Kiro", records, changed, removed)
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
        label="kirocode",
    )


if __name__ == "__main__":
    raise SystemExit(main())
