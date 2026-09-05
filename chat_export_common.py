#!/usr/bin/env python3
"""Shared helpers for Chat Export Hub live exporters."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

SAFE_COMPONENT_BYTES = 240
SAFE_WINDOWS_PATH_UNITS = 240
ATOMIC_TEMP_SUFFIX = ".tmp"
LOG_MAX_BYTES = 10 * 1024 * 1024

SKIP_USER_PREFIXES = (
    "<user_info>",
    "<system-reminder>",
    "<environment_context>",
    "<agent_skills>",
    "<available_skills>",
    "# AGENTS.md",
)

SCRUB_NEEDLES = (
    '"encrypted_content"',
    "auth.json",
    "API_KEY",
    "api_key",
    "Bearer ",
    "iCubeAuthInfo://",
)

SCRUB_LINE = "[omitted sensitive or internal line from preserved tool output]\n"

USER_QUERY_RE = re.compile(
    r"<user_query>\s*(.*?)\s*</user_query>",
    re.DOTALL | re.IGNORECASE,
)


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ATOMIC_TEMP_SUFFIX)
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ATOMIC_TEMP_SUFFIX)
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )
    tmp.replace(path)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def append_log_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = line.rstrip("\r\n") + "\n"
    incoming_bytes = len(text.encode("utf-8"))
    try:
        current_bytes = path.stat().st_size
    except FileNotFoundError:
        current_bytes = 0
    if current_bytes and current_bytes + incoming_bytes > LOG_MAX_BYTES:
        rotated = path.with_name(path.name + ".1")
        try:
            rotated.unlink()
        except FileNotFoundError:
            pass
        path.replace(rotated)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(text)


def write_console_line(line: str, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    if stream is None:
        return
    try:
        stream.write(line.rstrip("\r\n") + "\n")
        stream.flush()
    except (AttributeError, OSError, ValueError):
        pass


def emit_runtime_line(
    line: str,
    log_file: Path | None,
    *,
    error: bool = False,
    quiet: bool = False,
) -> None:
    if log_file is not None:
        try:
            append_log_line(log_file, line)
        except Exception as exc:
            write_console_line(
                f"{now_iso()} ERROR: could not write log file {log_file}: {exc}",
                error=True,
            )
    if error or not quiet:
        write_console_line(line, error=error)


def write_runtime_status(path: Path | None, status: dict[str, Any]) -> None:
    if path is None:
        return
    atomic_write_json(path, status)


def sanitize_filename_part(text: str, limit: int = 80) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", text)
    text = text.strip(" ._") or "untitled"
    return text[:limit].rstrip() or "untitled"


def truncate_utf8(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip(" ._")


def utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def truncate_utf16(text: str, max_units: int) -> str:
    if max_units <= 0:
        return ""
    used = 0
    chars: list[str] = []
    for char in text:
        units = utf16_units(char)
        if used + units > max_units:
            break
        chars.append(char)
        used += units
    return "".join(chars).rstrip(" ._")


def filesystem_safe_output_path(output_dir: Path, fixed_stem: str, title: str) -> Path:
    safe_title = sanitize_filename_part(title)
    suffix = ".txt"
    reserved_tail = suffix + ATOMIC_TEMP_SUFFIX

    component_budget = SAFE_COMPONENT_BYTES - len((fixed_stem + reserved_tail).encode("utf-8"))
    if component_budget < len("untitled".encode("utf-8")):
        raise ValueError(f"output filename prefix is too long: {fixed_stem!r}")
    safe_title = truncate_utf8(safe_title, component_budget)

    if sys.platform == "win32":
        absolute_dir = output_dir.expanduser().absolute()
        fixed_path = str(absolute_dir / f"{fixed_stem}{reserved_tail}")
        path_budget = SAFE_WINDOWS_PATH_UNITS - utf16_units(fixed_path)
        if path_budget < utf16_units("untitled"):
            raise ValueError(
                f"output directory is too long for safe Windows paths: {absolute_dir}"
            )
        safe_title = truncate_utf16(safe_title, path_budget)

    safe_title = safe_title or "untitled"
    return output_dir / f"{fixed_stem}{safe_title}{suffix}"


def one_line(text: str, limit: int = 100) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "..."
    return text


def source_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]


def first_iso_timestamp_prefix(timestamp: str | None, fallback: int) -> str:
    if timestamp:
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", timestamp)
        if match:
            try:
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                return dt.astimezone().strftime("%Y%m%d_%H%M%S")
            except ValueError:
                year, month, day, hour, minute, second = match.groups()
                return f"{year}{month}{day}_{hour}{minute}{second}"
    return f"{fallback:03d}"


def epoch_to_iso(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        n = float(value)
    except (TypeError, ValueError):
        text = str(value).strip()
        if not text:
            return ""
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt.astimezone().isoformat(timespec="seconds")
        except ValueError:
            return text
    if n <= 0:
        return ""
    if n > 1e14:
        n /= 1_000_000.0
    elif n > 1e11:
        n /= 1000.0
    try:
        return datetime.fromtimestamp(n).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return str(value)


def scrub_internal_lines(text: str) -> str:
    lines = []
    for line in text.splitlines(keepends=True):
        if any(needle in line for needle in SCRUB_NEEDLES):
            lines.append(SCRUB_LINE)
        else:
            lines.append(line)
    return "".join(lines)


def emit_block(lines: list[str], title: str, body: str, timestamp: str | None = None) -> None:
    if not body or not str(body).strip():
        return
    label = title if timestamp is None else f"{timestamp}  {title}"
    lines.extend(
        [
            "",
            "=" * 88,
            label,
            "-" * 88,
            str(body).rstrip(),
        ]
    )


def content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text" or "text" in content:
            text = str(content.get("text") or "")
            if text:
                return text
        return json.dumps(content, ensure_ascii=False, indent=2)
    if not isinstance(content, list):
        return str(content)
    chunks: list[str] = []
    for item in content:
        if isinstance(item, str):
            chunks.append(item)
            continue
        if not isinstance(item, dict):
            chunks.append(str(item))
            continue
        item_type = item.get("type")
        if item_type in ("text", "input_text", "output_text", "summary_text", "reasoning_text"):
            chunks.append(str(item.get("text") or ""))
        elif item_type in ("image", "input_image"):
            url = item.get("image_url") or item.get("url") or ""
            if isinstance(url, str) and url.startswith("data:"):
                header = url.split(",", 1)[0]
                chunks.append(f"[image: {header}, {len(url)} characters]")
            else:
                chunks.append(f"[image: {url or item_type}]")
        else:
            chunks.append(f"[{item_type or 'item'}: {json.dumps(item, ensure_ascii=False)}]")
    return "\n\n".join(chunk for chunk in chunks if chunk and str(chunk).strip()).strip()


def extract_user_query(text: str) -> str:
    matches = USER_QUERY_RE.findall(text or "")
    if matches:
        return "\n\n".join(m.strip() for m in matches if m.strip()).strip()
    return (text or "").strip()


def should_skip_user_text(text: str) -> bool:
    stripped = (text or "").lstrip()
    if not stripped:
        return True
    return any(stripped.startswith(prefix) for prefix in SKIP_USER_PREFIXES)


def clip_text(text: str, limit: int = 120_000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n\n[truncated {len(text) - limit} characters]"


def empty_counts() -> dict[str, int]:
    return {
        "user": 0,
        "assistant": 0,
        "tool_call": 0,
        "tool_output": 0,
        "reasoning": 0,
        "system": 0,
        "parse_error": 0,
    }


def write_manifest(
    output_dir: Path,
    source_label: str,
    records: list[dict[str, Any]],
    changed: int,
    removed: int,
) -> None:
    lines = [
        "Chat Export Hub live export manifest",
        f"Source: {source_label}",
        f"Generated: {now_iso()}",
        f"Tracked: {len(records)}  changed={changed}  removed={removed}",
        "",
    ]
    for number, record in enumerate(records, 1):
        counts = record.get("counts") or {}
        lines.extend(
            [
                f"{number:03d}. {record.get('title', 'untitled')}",
                f"     Kind: {record.get('kind', 'session')}",
                f"     Session ID: {record.get('session_id', record.get('source_id', ''))}",
            ]
        )
        if record.get("created"):
            lines.append(f"     Created: {record['created']}")
        if record.get("updated"):
            lines.append(f"     Updated: {record['updated']}")
        if record.get("cwd"):
            lines.append(f"     CWD: {record['cwd']}")
        if record.get("model"):
            lines.append(f"     Model: {record['model']}")
        if record.get("product"):
            lines.append(f"     Product: {record['product']}")
        lines.append(
            "     Messages: "
            f"user={counts.get('user', 0)}, "
            f"assistant={counts.get('assistant', 0)}, "
            f"tool_calls={counts.get('tool_call', 0)}, "
            f"tool_outputs={counts.get('tool_output', 0)}, "
            f"reasoning={counts.get('reasoning', 0)}"
        )
        lines.extend(
            [
                f"     Source: {record.get('source', '')}",
                f"     TXT: {record.get('output', '')}",
                "",
            ]
        )
    atomic_write_text(output_dir / "MANIFEST.txt", "\n".join(lines))


def prune_removed_sources(
    old_sources: dict[str, Any],
    seen_sources: set[str],
) -> int:
    removed = 0
    for source_key, old_record in old_sources.items():
        if source_key in seen_sources:
            continue
        old_output = old_record.get("output")
        if old_output:
            try:
                Path(old_output).unlink()
                removed += 1
            except FileNotFoundError:
                pass
    return removed


def reuse_or_replace_output(old_output: Any, new_output: Path) -> None:
    if old_output and old_output != str(new_output):
        try:
            Path(old_output).unlink()
        except FileNotFoundError:
            pass


def file_fingerprint(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (0, 0)
    return (int(stat.st_mtime_ns), int(stat.st_size))


def run_watcher_loop(
    *,
    once: bool,
    interval: float,
    log_file: Path | None,
    status_file: Path | None,
    extra_status: dict[str, Any],
    scan_fn: Callable[[], tuple[int, int, int]],
    quiet: bool,
    label: str,
) -> int:
    process_started_at = now_iso()
    while True:
        scan_started_at = now_iso()
        status: dict[str, Any] = {
            "status": "running",
            "mode": "once" if once else "continuous",
            "pid": os.getpid(),
            "process_started_at": process_started_at,
            "scan_started_at": scan_started_at,
            "updated_at": scan_started_at,
            **extra_status,
        }
        try:
            write_runtime_status(status_file, status)
            total, changed, removed = scan_fn()
            scan_finished_at = now_iso()
            summary_text = f"{label} tracked={total} changed={changed} removed={removed}"
            emit_runtime_line(f"{scan_finished_at} {summary_text}", log_file, quiet=quiet)
            status.update(
                {
                    "status": "completed" if once else "running",
                    "scan_finished_at": scan_finished_at,
                    "updated_at": scan_finished_at,
                    "summaries": [summary_text],
                    "tracked": total,
                    "changed": changed,
                    "removed": removed,
                }
            )
            write_runtime_status(status_file, status)
        except Exception as exc:
            scan_finished_at = now_iso()
            emit_runtime_line(
                f"{scan_finished_at} ERROR: {type(exc).__name__}: {exc}",
                log_file,
                error=True,
            )
            status.update(
                {
                    "status": "error",
                    "scan_finished_at": scan_finished_at,
                    "updated_at": scan_finished_at,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            try:
                write_runtime_status(status_file, status)
            except Exception as status_exc:
                emit_runtime_line(
                    f"{now_iso()} ERROR: could not write status file {status_file}: {status_exc}",
                    log_file,
                    error=True,
                )
            if once:
                return 1
        if once:
            return 0
        time.sleep(max(interval, 1.0))
