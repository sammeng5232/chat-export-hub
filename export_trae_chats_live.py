#!/usr/bin/env python3
"""Incrementally export Trae / Trae CN / TRAE SOLO local chats to TXT.

Reads the live ModularData ai-agent SQLite file (page-encrypted). A shared-read
copy is decrypted into a temp file, then sessions are extracted with PK-safe
queries because some decrypted pages are malformed.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
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

# Local Trae page cipher (same recipe as the on-disk helper). Do not log.
_PAGE_KEY = bytes.fromhex(
    "3605f6691095a993f03d5009c918352ef5be31ae31e8f000212b81ff058da773"
)
_PS, _RES = 4096, 80
_CTLEN = _PS - _RES
_SQLITE_MAGIC = b"SQLite format 3\x00"


def parse_args() -> argparse.Namespace:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--products",
        default="Trae,TRAE SOLO",
        help="Comma-separated AppData product folder names",
    )
    parser.add_argument("--output-dir", default=str(home / "trae_chat_live_exports"))
    parser.add_argument("--label", default="trae")
    parser.add_argument("--memory-root", action="append", default=None)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--log-file")
    parser.add_argument("--status-file")
    return parser.parse_args()


def product_names(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def product_db(name: str) -> Path:
    appdata = Path(os.environ.get("APPDATA", ""))
    return appdata / name / "ModularData" / "ai-agent" / "database.db"


def default_memory_roots(products: list[str]) -> list[Path]:
    home = Path.home()
    roots: list[Path] = []
    if any("CN" in p.upper() for p in products):
        roots.append(home / ".trae-cn" / "memory")
    if any("CN" not in p.upper() for p in products):
        roots.append(home / ".trae" / "memory")
    return roots


def pretty(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except TypeError:
        return str(value)


def shared_read_bytes(path: Path) -> bytes:
    try:
        with path.open("rb") as f:
            return f.read()
    except OSError:
        pass
    if sys.platform != "win32":
        raise OSError(f"cannot read {path}")
    import ctypes
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    FILE_SHARE = 0x00000007  # read | write | delete
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID = wintypes.HANDLE(-1).value
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    CreateFileW = kernel32.CreateFileW
    CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    CreateFileW.restype = wintypes.HANDLE
    ReadFile = kernel32.ReadFile
    ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    ReadFile.restype = wintypes.BOOL
    GetFileSizeEx = kernel32.GetFileSizeEx
    GetFileSizeEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
    GetFileSizeEx.restype = wintypes.BOOL
    CloseHandle = kernel32.CloseHandle

    handle = CreateFileW(
        str(path), GENERIC_READ, FILE_SHARE, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None
    )
    if handle == INVALID:
        raise OSError(f"CreateFile failed for {path} (winerr {ctypes.get_last_error()})")
    try:
        size = ctypes.c_longlong(0)
        if not GetFileSizeEx(handle, ctypes.byref(size)):
            raise OSError(f"GetFileSizeEx failed for {path}")
        buf = ctypes.create_string_buffer(int(size.value))
        done = wintypes.DWORD(0)
        offset = 0
        total = int(size.value)
        while offset < total:
            chunk = min(total - offset, 8 * 1024 * 1024)
            if not ReadFile(handle, ctypes.byref(buf, offset), chunk, ctypes.byref(done), None):
                raise OSError(f"ReadFile failed for {path}")
            if done.value == 0:
                break
            offset += done.value
        return buf.raw[:offset]
    finally:
        CloseHandle(handle)


def decrypt_pages(data: bytes) -> bytes:
    from Crypto.Cipher import AES

    if len(data) < _PS:
        raise ValueError("encrypted database is too small")
    n = len(data) // _PS
    out = bytearray()
    ec = AES.new(_PAGE_KEY, AES.MODE_ECB)
    for i in range(n):
        page = data[i * _PS : (i + 1) * _PS]
        iv = page[_CTLEN : _CTLEN + 16]
        if i == 0:
            ct = page[16:_CTLEN]
            pt = AES.new(_PAGE_KEY, AES.MODE_CBC, iv).decrypt(ct)
            d1 = ec.decrypt(page[16:32])
            pt16 = bytes(a ^ b for a, b in zip(d1, iv))
            out += _SQLITE_MAGIC + pt16 + pt[16:]
        else:
            out += AES.new(_PAGE_KEY, AES.MODE_CBC, iv).decrypt(page[:_CTLEN])
        out += b"\x00" * _RES
    out[20] = 0
    out[28:32] = n.to_bytes(4, "big")
    return bytes(out)


def decrypt_to_temp(src: Path) -> Path:
    raw = shared_read_bytes(src)
    prefix = "trae_plain_" if raw.startswith(_SQLITE_MAGIC) else "trae_dec_"
    payload = raw if raw.startswith(_SQLITE_MAGIC) else decrypt_pages(raw)
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=".db")
    os.close(fd)
    tmp = Path(name)
    tmp.write_bytes(payload)
    return tmp


def connect_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    return con


def table_cols(con: sqlite3.Connection, name: str) -> list[str]:
    try:
        return [str(r[1]) for r in con.execute(f'PRAGMA table_info("{name}")').fetchall()]
    except sqlite3.Error:
        return []


def table_max_id(con: sqlite3.Connection, name: str) -> int:
    try:
        row = con.execute(
            "SELECT seq FROM sqlite_sequence WHERE name=?", (name,)
        ).fetchone()
        if row and row[0]:
            return int(row[0])
    except sqlite3.Error:
        pass
    return 0


def pk_scan(con: sqlite3.Connection, table: str, cols: str, limit: int) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    if limit <= 0:
        limit = 256
    misses = 0
    for i in range(1, limit + 1):
        try:
            row = con.execute(f'SELECT {cols} FROM "{table}" WHERE id=?', (i,)).fetchone()
        except sqlite3.DatabaseError:
            misses += 1
            continue
        if row is None:
            misses += 1
            if misses > 64 and i > 32:
                # allow holes; stop after a long empty tail past known data
                if i > limit:
                    break
            continue
        misses = 0
        rows.append(row)
    return rows


def safe_fetchall(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    try:
        return list(con.execute(sql, params).fetchall())
    except sqlite3.DatabaseError:
        return []


def parse_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value
    return value


def load_sessions(con: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = table_cols(con, "chat_session")
    if not cols:
        return []
    wanted = [
        "id",
        "session_id",
        "project_id",
        "created_at",
        "updated_at",
        "deleted_at",
        "session_title",
        "work_mode",
        "session_type",
    ]
    select = ", ".join(c for c in wanted if c in cols)
    rows = safe_fetchall(con, f"SELECT {select} FROM chat_session")
    if not rows:
        max_id = table_max_id(con, "chat_session") or 64
        rows = pk_scan(con, "chat_session", select, max_id + 32)
    sessions: list[dict[str, Any]] = []
    for row in rows:
        keys = row.keys()
        sid = str(row["session_id"] if "session_id" in keys else row["id"])
        title = ""
        if "session_title" in keys:
            title = str(row["session_title"] or "").strip()
        sessions.append(
            {
                "pk": row["id"] if "id" in keys else sid,
                "session_id": sid,
                "project_id": row["project_id"] if "project_id" in keys else "",
                "title": title,
                "created": epoch_to_iso(row["created_at"] if "created_at" in keys else None),
                "updated": epoch_to_iso(row["updated_at"] if "updated_at" in keys else None),
                "deleted": bool(row["deleted_at"]) if "deleted_at" in keys else False,
                "work_mode": row["work_mode"] if "work_mode" in keys else "",
                "session_type": row["session_type"] if "session_type" in keys else "",
            }
        )
    return sessions


def load_general_by_message_id(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    cols = table_cols(con, "chat_message_general")
    if not cols:
        return {}
    select = ", ".join(
        c for c in ("id", "message_id", "content", "created_at", "updated_at") if c in cols
    )
    rows = safe_fetchall(con, f"SELECT {select} FROM chat_message_general")
    if not rows:
        max_id = table_max_id(con, "chat_message_general") or 256
        rows = pk_scan(con, "chat_message_general", select, max_id + 32)
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        mid = str(row["message_id"] if "message_id" in row.keys() else row["id"])
        out[mid] = {
            "content": row["content"] if "content" in row.keys() else "",
            "created": epoch_to_iso(row["created_at"] if "created_at" in row.keys() else None),
        }
    return out


def load_task_by_message_id(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    cols = table_cols(con, "chat_message_task")
    if not cols:
        return {}
    prefer = [
        "id",
        "message_id",
        "content",
        "text",
        "result",
        "summary",
        "title",
        "created_at",
        "updated_at",
    ]
    select = ", ".join(c for c in prefer if c in cols) or "id"
    max_id = table_max_id(con, "chat_message_task") or 256
    rows = pk_scan(con, "chat_message_task", select, max_id + 32)
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        keys = set(row.keys())
        mid = str(row["message_id"] if "message_id" in keys else row["id"])
        body = ""
        for key in ("content", "text", "result", "summary", "title"):
            if key in keys and row[key]:
                body = content_to_text(parse_json(row[key]) if key != "title" else row[key])
                if body.strip():
                    break
        out[mid] = {
            "content": body,
            "created": epoch_to_iso(row["created_at"] if "created_at" in keys else None),
        }
    return out


def load_messages(con: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = table_cols(con, "chat_message")
    if not cols:
        return []
    prefer = [
        "id",
        "session_id",
        "message_id",
        "message_type",
        "message_role",
        "message_index",
        "created_at",
        "updated_at",
        "deleted_at",
    ]
    select = ", ".join(c for c in prefer if c in cols)
    max_id = table_max_id(con, "chat_message") or 1024
    rows = pk_scan(con, "chat_message", select, max(max_id + 64, 256))
    out: list[dict[str, Any]] = []
    for row in rows:
        keys = set(row.keys())
        if "deleted_at" in keys and row["deleted_at"]:
            continue
        out.append(
            {
                "id": row["id"] if "id" in keys else None,
                "session_ref": row["session_id"] if "session_id" in keys else None,
                "message_id": str(row["message_id"]) if "message_id" in keys and row["message_id"] is not None else "",
                "message_type": str(row["message_type"] or "") if "message_type" in keys else "",
                "role": str(row["message_role"] or "").lower() if "message_role" in keys else "",
                "index": row["message_index"] if "message_index" in keys else 0,
                "created": epoch_to_iso(row["created_at"] if "created_at" in keys else None),
            }
        )
    out.sort(key=lambda m: (m.get("index") or 0, m.get("id") or 0))
    return out


def load_turns(con: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = table_cols(con, "chat_turn")
    if not cols:
        return []
    prefer = [
        "id",
        "session_id",
        "rewritten_user_message",
        "turn_status",
        "agent_name",
        "created_at",
        "updated_at",
    ]
    select = ", ".join(c for c in prefer if c in cols)
    max_id = table_max_id(con, "chat_turn") or 256
    rows = pk_scan(con, "chat_turn", select, max_id + 32)
    out: list[dict[str, Any]] = []
    for row in rows:
        keys = set(row.keys())
        out.append(
            {
                "session_ref": row["session_id"] if "session_id" in keys else None,
                "rewritten": row["rewritten_user_message"] if "rewritten_user_message" in keys else "",
                "status": row["turn_status"] if "turn_status" in keys else "",
                "agent": row["agent_name"] if "agent_name" in keys else "",
                "created": epoch_to_iso(row["created_at"] if "created_at" in keys else None),
            }
        )
    return out


def load_history(con: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = table_cols(con, "history_v2")
    if not cols:
        return []
    prefer = ["id", "session_id", "messages", "created_at", "updated_at"]
    select = ", ".join(c for c in prefer if c in cols) or "id"
    max_id = table_max_id(con, "history_v2") or 1024
    rows = pk_scan(con, "history_v2", select, max(max_id + 32, 128))
    out: list[dict[str, Any]] = []
    for row in rows:
        keys = set(row.keys())
        payload = parse_json(row["messages"]) if "messages" in keys else None
        out.append(
            {
                "session_ref": row["session_id"] if "session_id" in keys else None,
                "payload": payload,
                "created": epoch_to_iso(row["created_at"] if "created_at" in keys else None),
            }
        )
    return out


def session_matches(session: dict[str, Any], ref: Any) -> bool:
    if ref is None:
        return False
    return str(ref) in {str(session["pk"]), str(session["session_id"])}


def emit_history_payload(lines: list[str], counts: dict[str, int], payload: Any) -> None:
    obj = payload
    if isinstance(obj, str):
        parsed = parse_json(obj)
        obj = parsed if parsed is not None else obj
    messages: list[Any] = []
    if isinstance(obj, list):
        messages = obj
    elif isinstance(obj, dict):
        for key in ("raw_messages", "messages", "items"):
            if isinstance(obj.get(key), list):
                messages = obj[key]
                break
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").lower()
        text = content_to_text(item.get("content") or item.get("text"))
        if role == "user":
            text = extract_user_query(text)
            if should_skip_user_text(text) or not text.strip():
                continue
            emit_block(lines, "USER", text)
            counts["user"] += 1
        elif role == "assistant":
            if text.strip():
                emit_block(lines, "ASSISTANT", text)
                counts["assistant"] += 1
        elif role in {"tool", "function"}:
            emit_block(lines, "TOOL CALL", clip_text(pretty(item), 20_000))
            counts["tool_call"] += 1
        tools = item.get("tool_calls") or item.get("toolCalls")
        if isinstance(tools, list):
            for call in tools:
                emit_block(lines, "TOOL CALL", clip_text(pretty(call), 20_000))
                counts["tool_call"] += 1


def load_memory_files(roots: list[Path]) -> dict[str, list[Path]]:
    by_sid: dict[str, list[Path]] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("session_memory_*.jsonl"):
            if not path.is_file():
                continue
            stem = path.stem
            sid = stem[len("session_memory_") :] if stem.startswith("session_memory_") else stem
            by_sid.setdefault(sid, []).append(path)
    return by_sid


def emit_memory_jsonl(lines: list[str], counts: dict[str, int], paths: list[Path]) -> None:
    for path in sorted(paths):
        emit_block(lines, "MEMORY SOURCE", str(path))
        try:
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
                    if not isinstance(obj, dict):
                        continue
                    role = str(obj.get("role") or obj.get("type") or "").lower()
                    text = content_to_text(
                        obj.get("content")
                        or obj.get("text")
                        or obj.get("summary")
                        or obj.get("message")
                    )
                    ts = epoch_to_iso(obj.get("created_at_ms") or obj.get("timestamp") or obj.get("time"))
                    if role in {"user", "human"}:
                        text = extract_user_query(text)
                        if should_skip_user_text(text) or not text.strip():
                            continue
                        emit_block(lines, "USER", text, ts)
                        counts["user"] += 1
                    elif role in {"assistant", "ai"}:
                        if text.strip():
                            emit_block(lines, "ASSISTANT", text, ts)
                            counts["assistant"] += 1
                    elif "summary" in obj and str(obj.get("summary") or "").strip():
                        emit_block(lines, "MEMORY", clip_text(str(obj.get("summary"))), ts)
                    elif text.strip():
                        emit_block(lines, (role or "MEMORY").upper(), text, ts)
        except OSError as exc:
            emit_block(lines, "WARNING", f"Could not read {path}: {exc}")


def export_session(
    session: dict[str, Any],
    *,
    product: str,
    source_db: Path,
    messages: list[dict[str, Any]],
    general: dict[str, dict[str, Any]],
    tasks: dict[str, dict[str, Any]],
    turns: list[dict[str, Any]],
    history: list[dict[str, Any]],
    memory_paths: list[Path],
    output_path: Path,
) -> dict[str, Any]:
    counts = empty_counts()
    title = session.get("title") or session["session_id"]
    lines = [
        "Local Trae chat export",
        f"Title: {title}",
        f"Session ID: {session['session_id']}",
        f"Product: {product}",
        f"Source: {source_db}",
    ]
    if session.get("created"):
        lines.append(f"Created: {session['created']}")
    if session.get("updated"):
        lines.append(f"Updated: {session['updated']}")
    if session.get("work_mode"):
        lines.append(f"Work mode: {session['work_mode']}")
    lines.extend([f"Exported At: {now_iso()}", ""])
    lines.append(
        "Note: some decrypted pages may be malformed; assistant/tool turns are best-effort."
    )

    related_msgs = [m for m in messages if session_matches(session, m.get("session_ref"))]
    if not related_msgs and messages:
        # some builds store the hex session id only on a subset of rows
        related_msgs = [
            m
            for m in messages
            if str(m.get("session_ref") or "") == str(session["session_id"])
        ]

    emitted_user = set()
    for msg in related_msgs:
        mid = msg.get("message_id") or ""
        role = msg.get("role") or ""
        ts = msg.get("created") or ""
        body = ""
        if mid in general:
            body = content_to_text(parse_json(general[mid].get("content")))
            ts = ts or general[mid].get("created") or ""
        if not body and mid in tasks:
            body = tasks[mid].get("content") or ""
            ts = ts or tasks[mid].get("created") or ""
        if role == "user":
            body = extract_user_query(body)
            if should_skip_user_text(body) or not body.strip():
                continue
            key = body.strip()
            if key in emitted_user:
                continue
            emitted_user.add(key)
            emit_block(lines, "USER", body, ts)
            counts["user"] += 1
        elif role == "assistant":
            if body.strip():
                emit_block(lines, "ASSISTANT", body, ts)
                counts["assistant"] += 1
        elif role in {"tool", "function"}:
            emit_block(lines, "TOOL CALL", clip_text(body or pretty(msg), 20_000), ts)
            counts["tool_call"] += 1
        elif body.strip():
            emit_block(lines, (role or msg.get("message_type") or "OTHER").upper(), body, ts)

    for turn in turns:
        if not session_matches(session, turn.get("session_ref")):
            continue
        rewritten = extract_user_query(content_to_text(turn.get("rewritten") or ""))
        if rewritten.strip() and rewritten.strip() not in emitted_user:
            emit_block(lines, "USER", rewritten, turn.get("created") or "")
            counts["user"] += 1
            emitted_user.add(rewritten.strip())

    hist_emitted = False
    for item in history:
        if item.get("session_ref") is not None and not session_matches(session, item.get("session_ref")):
            continue
        if item.get("payload") is None:
            continue
        # If history rows lack session_id, only use them when this is the sole session
        if item.get("session_ref") is None:
            continue
        emit_history_payload(lines, counts, item.get("payload"))
        hist_emitted = True
    if not hist_emitted and len(history) == 1 and history[0].get("session_ref") is None:
        emit_history_payload(lines, counts, history[0].get("payload"))

    if memory_paths:
        emit_memory_jsonl(lines, counts, memory_paths)

    if counts["user"] == 0 and counts["assistant"] == 0:
        emit_block(
            lines,
            "WARNING",
            "No readable messages for this session (encrypted pages may be malformed).",
        )

    atomic_write_text(output_path, scrub_internal_lines("\n".join(lines) + "\n"))
    return {
        "session_id": session["session_id"],
        "source_id": session["session_id"],
        "title": one_line(title, 80),
        "kind": "session",
        "source": f"{source_db}#session:{session['session_id']}",
        "product": product,
        "output": str(output_path),
        "created": session.get("created", ""),
        "updated": session.get("updated", ""),
        "cwd": "",
        "model": session.get("work_mode") or "",
        "counts": counts,
        "bytes": output_path.stat().st_size,
    }


def fingerprint_paths(paths: list[Path]) -> list[list[Any]]:
    out: list[list[Any]] = []
    for path in paths:
        mtime_ns, size = file_fingerprint(path)
        out.append([str(path), mtime_ns, size])
    return out


def collect_db_files(db: Path) -> list[Path]:
    files = [db]
    for suffix in ("-wal", "-shm"):
        extra = Path(str(db) + suffix)
        if extra.exists():
            files.append(extra)
    return files


def scan_product(
    product: str,
    output_dir: Path,
    memory_map: dict[str, list[Path]],
    old_sources: dict[str, Any],
    new_sources: dict[str, Any],
    records: list[dict[str, Any]],
    seen: set[str],
    ordinal_start: int,
) -> tuple[int, int]:
    db = product_db(product)
    changed = 0
    ordinal = ordinal_start
    if not db.exists():
        return changed, ordinal

    dec: Path | None = None
    try:
        dec = decrypt_to_temp(db)
        con = connect_ro(dec)
        try:
            sessions = load_sessions(con)
            messages = load_messages(con)
            general = load_general_by_message_id(con)
            tasks = load_task_by_message_id(con)
            turns = load_turns(con)
            history = load_history(con)
        finally:
            con.close()
    except Exception as exc:
        source_key = f"{product}:decrypt-error"
        seen.add(source_key)
        ordinal += 1
        title = one_line(f"{product} decrypt error", 80)
        prefix = first_iso_timestamp_prefix(None, ordinal)
        output_path = filesystem_safe_output_path(output_dir, f"{prefix}__{product}__", title)
        lines = [
            "Local Trae chat export",
            f"Product: {product}",
            f"Source: {db}",
            f"Exported At: {now_iso()}",
            "",
        ]
        emit_block(lines, "WARNING", f"Could not decrypt or open database: {type(exc).__name__}: {exc}")
        atomic_write_text(output_path, scrub_internal_lines("\n".join(lines) + "\n"))
        record = {
            "session_id": source_key,
            "source_id": source_key,
            "title": title,
            "kind": "error",
            "source": str(db),
            "product": product,
            "output": str(output_path),
            "created": "",
            "updated": now_iso(),
            "cwd": "",
            "model": "",
            "counts": empty_counts(),
            "bytes": output_path.stat().st_size,
            "fingerprint": fingerprint_paths(collect_db_files(db)),
        }
        new_sources[source_key] = record
        records.append(record)
        return 1, ordinal
    finally:
        if dec is not None:
            try:
                dec.unlink()
            except OSError:
                pass

    db_fp = fingerprint_paths(collect_db_files(db))
    for session in sessions:
        if session.get("deleted"):
            continue
        sid = session["session_id"]
        source_key = f"{product}:{sid}"
        seen.add(source_key)
        ordinal += 1
        title = one_line(session.get("title") or sid, 80)
        prefix = first_iso_timestamp_prefix(session.get("created"), ordinal)
        output_path = filesystem_safe_output_path(output_dir, f"{prefix}__{sid}__", title)
        mem_paths = memory_map.get(sid) or []
        mem_fp = fingerprint_paths(mem_paths)
        old = old_sources.get(source_key, {})
        must_export = (
            not old
            or old.get("fingerprint") != db_fp
            or old.get("memory_fp") != mem_fp
            or old.get("title") != title
            or old.get("output") != str(output_path)
            or not Path(old.get("output") or "").exists()
            or not output_path.exists()
        )
        if must_export:
            record = export_session(
                session,
                product=product,
                source_db=db,
                messages=messages,
                general=general,
                tasks=tasks,
                turns=turns,
                history=history,
                memory_paths=mem_paths,
                output_path=output_path,
            )
            reuse_or_replace_output(old.get("output"), output_path)
            changed += 1
        else:
            record = {
                "session_id": sid,
                "source_id": sid,
                "title": title,
                "kind": "session",
                "source": f"{db}#session:{sid}",
                "product": product,
                "output": str(output_path),
                "created": session.get("created") or old.get("created", ""),
                "updated": session.get("updated") or old.get("updated", ""),
                "cwd": "",
                "model": session.get("work_mode") or old.get("model", ""),
                "counts": old.get("counts") or empty_counts(),
                "bytes": old.get("bytes", output_path.stat().st_size if output_path.exists() else 0),
            }
        record["fingerprint"] = db_fp
        record["memory_fp"] = mem_fp
        new_sources[source_key] = record
        records.append(record)
    return changed, ordinal


def scan_once(
    products: list[str],
    output_dir: Path,
    memory_roots: list[Path],
    label: str,
) -> tuple[int, int, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / ".export_state.json"
    old_sources: dict[str, Any] = (load_json(state_path, {"sources": {}}) or {}).get("sources") or {}
    new_sources: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    changed = 0
    ordinal = 0
    memory_map = load_memory_files(memory_roots)

    for product in products:
        extra, ordinal = scan_product(
            product,
            output_dir,
            memory_map,
            old_sources,
            new_sources,
            records,
            seen,
            ordinal,
        )
        changed += extra

    # Memory-only sessions (no matching decrypted row)
    known_ids = {rec.get("session_id") for rec in records}
    for sid, paths in memory_map.items():
        if sid in known_ids:
            continue
        source_key = f"memory:{sid}"
        seen.add(source_key)
        ordinal += 1
        title = one_line(sid, 80)
        prefix = first_iso_timestamp_prefix(None, ordinal)
        output_path = filesystem_safe_output_path(output_dir, f"{prefix}__{sid}__", title)
        old = old_sources.get(source_key, {})
        mem_fp = fingerprint_paths(paths)
        must_export = (
            not old
            or old.get("memory_fp") != mem_fp
            or not Path(old.get("output") or "").exists()
        )
        if must_export:
            counts = empty_counts()
            lines = [
                "Local Trae chat export",
                f"Title: {title}",
                f"Session ID: {sid}",
                "Product: memory",
                f"Exported At: {now_iso()}",
                "",
            ]
            emit_memory_jsonl(lines, counts, paths)
            atomic_write_text(output_path, scrub_internal_lines("\n".join(lines) + "\n"))
            reuse_or_replace_output(old.get("output"), output_path)
            record = {
                "session_id": sid,
                "source_id": sid,
                "title": title,
                "kind": "memory",
                "source": str(paths[0]) if paths else "",
                "product": "memory",
                "output": str(output_path),
                "created": "",
                "updated": now_iso(),
                "cwd": "",
                "model": "",
                "counts": counts,
                "bytes": output_path.stat().st_size,
            }
            changed += 1
        else:
            record = dict(old)
        record["memory_fp"] = mem_fp
        new_sources[source_key] = record
        records.append(record)

    removed = prune_removed_sources(old_sources, seen, retained_sources=new_sources, retained_records=records)
    write_manifest(output_dir, f"{label} {','.join(products)}", records, changed, removed)
    atomic_write_json(
        state_path,
        {
            "updated_at": now_iso(),
            "products": products,
            "memory_roots": [str(p) for p in memory_roots],
            "sources": new_sources,
        },
    )
    return len(records), changed, removed


def main() -> int:
    args = parse_args()
    products = product_names(args.products)
    output_dir = Path(args.output_dir).expanduser()
    if args.memory_root:
        memory_roots = [Path(p).expanduser() for p in args.memory_root]
    else:
        memory_roots = default_memory_roots(products)
    return run_watcher_loop(
        once=args.once,
        interval=args.interval,
        log_file=Path(args.log_file).expanduser() if args.log_file else None,
        status_file=Path(args.status_file).expanduser() if args.status_file else None,
        extra_status={
            "products": products,
            "output_dir": str(output_dir),
            "label": args.label,
        },
        scan_fn=lambda: scan_once(products, output_dir, memory_roots, args.label),
        quiet=args.quiet,
        label=args.label,
    )


if __name__ == "__main__":
    raise SystemExit(main())
