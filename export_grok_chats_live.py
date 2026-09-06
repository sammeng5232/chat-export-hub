#!/usr/bin/env python3
"""Incrementally export local Grok CLI sessions to human-readable TXT files.

Mirrors the Codex / Claude Code live export layout:

  ~/.grok/sessions/<encoded-cwd>/<session-id>/chat_history.jsonl
  ~/.grok/sessions/<encoded-cwd>/<session-id>/summary.json
  ~/.grok/sessions/<encoded-cwd>/prompt_history.jsonl

  ->  ~/grok_chat_live_exports/<timestamp>__<session_id>__<title>.txt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote


_TOOLS = Path(__file__).resolve().parent
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from chat_export_common import prune_removed_sources, wsl_agent_homes


SAFE_COMPONENT_BYTES = 240
SAFE_WINDOWS_PATH_UNITS = 240
ATOMIC_TEMP_SUFFIX = ".tmp"
LOG_MAX_BYTES = 10 * 1024 * 1024

# Synthetic / injected user rows that are not real chat turns.
SKIP_SYNTHETIC_REASONS = frozenset(
    {
        "project_instructions",
        "system_reminder",
        "compaction_meta",
        "skills",
        "mcp_reminder",
    }
)

# Leading wrappers that are environment noise, not the user's request.
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
)

SCRUB_LINE = "[omitted sensitive or internal line from preserved tool output]\n"

USER_QUERY_RE = re.compile(
    r"<user_query>\s*(.*?)\s*</user_query>",
    re.DOTALL | re.IGNORECASE,
)
SESSION_ID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    home = Path.home()
    parser.add_argument("--grok-home", default=str(home / ".grok"))
    parser.add_argument("--output-dir", default=str(home / "grok_chat_live_exports"))
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--once", action="store_true", help="run one export pass and exit")
    parser.add_argument("--quiet", action="store_true", help="only print errors")
    parser.add_argument("--log-file", help="append scan and error lines to this UTF-8 log file")
    parser.add_argument("--status-file", help="atomically update runtime status in this JSON file")
    parser.add_argument(
        "--include-reasoning",
        action="store_true",
        help="include reasoning summaries (encrypted blobs are always omitted)",
    )
    parser.add_argument(
        "--include-system",
        action="store_true",
        help="include the full system prompt block (very long; off by default)",
    )
    return parser.parse_args()


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ATOMIC_TEMP_SUFFIX)
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


def atomic_write_json(path: Path, obj: Any) -> None:
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
    path.parent.mkdir(parents=True, exist_ok=True)
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


def source_hash(path: Path) -> str:
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:10]


def first_iso_timestamp_prefix(timestamp: str | None, fallback: int) -> str:
    if timestamp:
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", timestamp)
        if match:
            year, month, day, hour, minute, second = match.groups()
            # Prefer local wall-clock style used by the Codex/Claude exporters.
            try:
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                local = dt.astimezone()
                return local.strftime("%Y%m%d_%H%M%S")
            except ValueError:
                return f"{year}{month}{day}_{hour}{minute}{second}"
    return f"{fallback:03d}"


def decode_workspace_name(name: str) -> str:
    try:
        return unquote(name)
    except Exception:
        return name


def pretty_json_maybe(value: Any) -> str:
    if not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, indent=2)
    try:
        obj = json.loads(value)
    except Exception:
        return value
    return json.dumps(obj, ensure_ascii=False, indent=2)


def compact_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value).rstrip()


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
        if "text" in content:
            return str(content.get("text") or "")
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
        if item_type in ("text", "input_text", "output_text", "summary_text"):
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
    """Prefer the explicit <user_query> body when present."""
    matches = USER_QUERY_RE.findall(text or "")
    if matches:
        return "\n\n".join(m.strip() for m in matches if m.strip()).strip()
    return (text or "").strip()


def should_skip_user_text(text: str) -> bool:
    stripped = (text or "").lstrip()
    if not stripped:
        return True
    return any(stripped.startswith(prefix) for prefix in SKIP_USER_PREFIXES)


def reasoning_summary_text(obj: dict[str, Any]) -> str:
    summary = obj.get("summary")
    if not summary:
        return ""
    if isinstance(summary, str):
        return summary.strip()
    if isinstance(summary, list):
        return content_to_text(summary)
    return compact_output(summary)


def session_id_from_path(path: Path) -> str:
    match = SESSION_ID_RE.search(path.name)
    if match:
        return match.group(1)
    match = SESSION_ID_RE.search(str(path))
    return match.group(1) if match else path.stem


def iter_session_dirs(sessions_root: Path) -> list[Path]:
    sessions: list[Path] = []
    if not sessions_root.exists():
        return sessions
    for workspace in sorted(p for p in sessions_root.iterdir() if p.is_dir()):
        for child in sorted(p for p in workspace.iterdir() if p.is_dir()):
            if (child / "chat_history.jsonl").exists() or (child / "summary.json").exists():
                sessions.append(child)
    return sessions


def iter_prompt_history_files(sessions_root: Path) -> list[Path]:
    if not sessions_root.exists():
        return []
    return sorted(sessions_root.rglob("prompt_history.jsonl"))


def read_summary(session_dir: Path) -> dict[str, Any]:
    return load_json(session_dir / "summary.json", {})


def first_user_title(chat_path: Path) -> str:
    if not chat_path.exists():
        return "Untitled"
    with chat_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "user":
                continue
            if obj.get("synthetic_reason") in SKIP_SYNTHETIC_REASONS:
                continue
            text = extract_user_query(content_to_text(obj.get("content")))
            if should_skip_user_text(text):
                continue
            if text.strip():
                return one_line(text, 80)
    return "Untitled"


def title_for_session(session_dir: Path, summary: dict[str, Any]) -> str:
    for key in ("generated_title", "session_summary"):
        value = summary.get(key)
        if isinstance(value, str) and value.strip():
            return one_line(value, 80)
    return first_user_title(session_dir / "chat_history.jsonl")


def output_path_for_session(
    session_dir: Path,
    title: str,
    output_dir: Path,
    summary: dict[str, Any],
    ordinal: int,
    *,
    home_tag: str = "",
) -> Path:
    session_id = summary.get("info", {}).get("id") or session_id_from_path(session_dir)
    created = summary.get("created_at") or summary.get("last_active_at")
    prefix = first_iso_timestamp_prefix(created if isinstance(created, str) else None, ordinal)
    tagged_id = f"{home_tag}_{session_id}" if home_tag else session_id
    return filesystem_safe_output_path(output_dir, f"{prefix}__{tagged_id}__", title)


def export_session(
    session_dir: Path,
    title: str,
    output_path: Path,
    summary: dict[str, Any],
    *,
    include_reasoning: bool,
    include_system: bool,
) -> dict[str, Any]:
    chat_path = session_dir / "chat_history.jsonl"
    session_id = summary.get("info", {}).get("id") or session_id_from_path(session_dir)
    cwd = (summary.get("info") or {}).get("cwd") or decode_workspace_name(session_dir.parent.name)
    created = summary.get("created_at") or ""
    updated = summary.get("updated_at") or summary.get("last_active_at") or ""
    model = summary.get("current_model_id") or ""
    agent_name = summary.get("agent_name") or ""
    effort = summary.get("reasoning_effort") or ""

    counts = {
        "user": 0,
        "assistant": 0,
        "tool_call": 0,
        "tool_output": 0,
        "reasoning": 0,
        "system": 0,
        "backend_tool": 0,
        "synthetic_skipped": 0,
        "parse_error": 0,
    }

    lines: list[str] = [
        "Local Grok CLI chat export",
        f"Title: {title}",
        f"Session ID: {session_id}",
    ]
    if created:
        lines.append(f"Created: {created}")
    if updated:
        lines.append(f"Updated: {updated}")
    if cwd:
        lines.append(f"CWD: {cwd}")
    if model:
        lines.append(f"Model: {model}")
    if agent_name:
        lines.append(f"Agent: {agent_name}")
    if effort:
        lines.append(f"Reasoning effort: {effort}")
    lines.extend(
        [
            f"Source JSONL: {chat_path}",
            f"Session dir: {session_dir}",
            f"Exported At: {now_iso()}",
            "",
            (
                "Note: System setup, synthetic reminders/project instructions, and encrypted "
                "reasoning blobs are omitted by default. Tool calls and outputs are preserved "
                "from chat_history.jsonl; some sensitive lines may be scrubbed."
            ),
        ]
    )

    if not chat_path.exists():
        emit_block(lines, "WARNING", f"Missing chat_history.jsonl under {session_dir}")
    else:
        with chat_path.open("r", encoding="utf-8") as f:
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

                msg_type = obj.get("type") or obj.get("role") or "unknown"

                if msg_type == "system":
                    if not include_system:
                        continue
                    body = content_to_text(obj.get("content"))
                    emit_block(lines, "SYSTEM", body)
                    counts["system"] += 1
                    continue

                if msg_type == "user":
                    synthetic = obj.get("synthetic_reason")
                    if synthetic in SKIP_SYNTHETIC_REASONS:
                        counts["synthetic_skipped"] += 1
                        continue
                    text = extract_user_query(content_to_text(obj.get("content")))
                    if should_skip_user_text(text):
                        counts["synthetic_skipped"] += 1
                        continue
                    if not text.strip():
                        continue
                    label = "USER"
                    if synthetic:
                        label = f"USER ({synthetic})"
                    emit_block(lines, label, text)
                    counts["user"] += 1
                    continue

                if msg_type == "reasoning":
                    if not include_reasoning:
                        continue
                    body = reasoning_summary_text(obj)
                    if not body.strip():
                        continue
                    emit_block(lines, "REASONING SUMMARY", body)
                    counts["reasoning"] += 1
                    continue

                if msg_type == "assistant":
                    text = content_to_text(obj.get("content"))
                    if text.strip():
                        emit_block(lines, "ASSISTANT", text)
                        counts["assistant"] += 1
                    tool_calls = obj.get("tool_calls") or []
                    if isinstance(tool_calls, list):
                        for call in tool_calls:
                            if not isinstance(call, dict):
                                continue
                            name = call.get("name") or "tool"
                            call_id = call.get("id") or call.get("call_id") or ""
                            arguments = call.get("arguments")
                            body_parts = [f"Name: {name}"]
                            if call_id:
                                body_parts.append(f"Call ID: {call_id}")
                            if arguments is not None:
                                body_parts.extend(["Input:", pretty_json_maybe(arguments)])
                            emit_block(lines, "TOOL CALL", "\n".join(body_parts))
                            counts["tool_call"] += 1
                    continue

                if msg_type == "tool_result":
                    call_id = obj.get("tool_call_id") or obj.get("call_id") or ""
                    body = content_to_text(obj.get("content"))
                    if not body.strip() and obj.get("output") is not None:
                        body = compact_output(obj.get("output"))
                    if call_id:
                        body = f"Call ID: {call_id}\n\n{body}" if body else f"Call ID: {call_id}"
                    emit_block(lines, "TOOL OUTPUT", body)
                    counts["tool_output"] += 1
                    continue

                if msg_type == "backend_tool_call":
                    kind = obj.get("kind") or {}
                    tool_type = ""
                    if isinstance(kind, dict):
                        tool_type = kind.get("tool_type") or kind.get("type") or ""
                        action = kind.get("action") or {}
                        body = json.dumps(
                            {
                                "tool_type": tool_type,
                                "status": kind.get("status"),
                                "id": kind.get("id"),
                                "action": action,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    else:
                        body = compact_output(obj)
                    emit_block(lines, f"BACKEND TOOL ({tool_type or 'call'})", body)
                    counts["backend_tool"] += 1
                    continue

                # Unknown message types: dump compactly so history is not lost.
                emit_block(
                    lines,
                    f"OTHER ({msg_type})",
                    json.dumps(obj, ensure_ascii=False, indent=2)[:8000],
                )

    output_text = scrub_internal_lines("\n".join(lines) + "\n")
    atomic_write_text(output_path, output_text)
    return {
        "session_id": session_id,
        "title": title,
        "kind": "session",
        "source": str(chat_path if chat_path.exists() else session_dir),
        "session_dir": str(session_dir),
        "output": str(output_path),
        "created": created,
        "updated": updated,
        "cwd": cwd,
        "model": model,
        "counts": counts,
        "bytes": output_path.stat().st_size,
    }


def export_prompt_history(path: Path, output_path: Path) -> dict[str, Any]:
    counts = {"prompts": 0, "parse_error": 0}
    first_ts = ""
    first_session = ""
    lines: list[str] = [
        "Local Grok CLI prompt history export",
        "Title: Grok CLI prompt history",
        "Source kind: prompt-history",
        f"Source JSONL: {path}",
        f"Exported At: {now_iso()}",
        "",
        "Note: This is Grok CLI's prompt history file, not a full assistant transcript.",
    ]

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
            ts = obj.get("timestamp") or ""
            session_id = obj.get("session_id") or ""
            prompt = obj.get("prompt") or ""
            is_bash = obj.get("is_bash")
            if not first_ts and ts:
                first_ts = ts
            if not first_session and session_id:
                first_session = session_id
            body_parts = []
            if session_id:
                body_parts.append(f"Session ID: {session_id}")
            if is_bash is not None:
                body_parts.append(f"Is bash: {is_bash}")
            if body_parts:
                body_parts.append("")
            body_parts.append(str(prompt))
            emit_block(lines, "PROMPT", "\n".join(body_parts), ts if ts else None)
            counts["prompts"] += 1

    # Fill header fields discovered while scanning.
    if first_session:
        lines.insert(3, f"First Session ID: {first_session}")
    if first_ts:
        lines.insert(3, f"First Prompt: {first_ts}")

    output_text = scrub_internal_lines("\n".join(lines) + "\n")
    atomic_write_text(output_path, output_text)
    return {
        "session_id": "prompt_history",
        "title": "Grok CLI prompt history",
        "kind": "prompt-history",
        "source": str(path),
        "output": str(output_path),
        "created": first_ts,
        "updated": "",
        "counts": counts,
        "bytes": output_path.stat().st_size,
    }


def write_manifest(
    output_dir: Path,
    grok_home: Path,
    records: list[dict[str, Any]],
    changed: int,
    removed: int,
    source_roots: list[Path] | None = None,
) -> None:
    sessions_root = grok_home / "sessions"
    roots = source_roots or [sessions_root]
    lines = [
        "Local Grok CLI live chat export manifest",
        f"Export directory: {output_dir}",
        f"Last scan: {now_iso()}",
        f"Source root: {roots[0]}",
        f"Chat files tracked: {len(records)}",
        f"Changed this scan: {changed}",
        f"Removed stale outputs this scan: {removed}",
        "",
    ]
    for extra_root in roots[1:]:
        lines.insert(4, f"Source root: {extra_root}")
    for number, record in enumerate(sorted(records, key=lambda item: item["output"]), 1):
        counts = record.get("counts", {})
        lines.extend(
            [
                f"{number:03d}. {record['title']}",
                f"     Kind: {record.get('kind', 'session')}",
                f"     Session ID: {record.get('session_id', '')}",
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
        if record.get("kind") == "prompt-history":
            lines.append(
                f"     Prompts: {counts.get('prompts', 0)}, parse_errors={counts.get('parse_error', 0)}"
            )
        else:
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
                f"     Source: {record['source']}",
                f"     TXT: {record['output']}",
                "",
            ]
        )
    atomic_write_text(output_dir / "MANIFEST.txt", "\n".join(lines))


def scan_once(
    grok_home: Path,
    output_dir: Path,
    *,
    include_reasoning: bool,
    include_system: bool,
) -> tuple[int, int, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / ".export_state.json"
    state_obj = load_json(state_path, {"sources": {}})
    old_sources: dict[str, Any] = state_obj.get("sources", {})
    seen_sources: set[str] = set()
    new_sources: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    changed = 0

    scan_homes: list[tuple[Path, str]] = [(grok_home, "")] + wsl_agent_homes(".grok")
    source_roots = [scan_home / "sessions" for scan_home, _tag in scan_homes]

    # --- full sessions ---
    scan_sessions: list[tuple[Path, str]] = []
    for scan_home, home_tag in scan_homes:
        scan_sessions.extend((path, home_tag) for path in iter_session_dirs(scan_home / "sessions"))
    for ordinal, (session_dir, home_tag) in enumerate(scan_sessions, 1):
        chat_path = session_dir / "chat_history.jsonl"
        summary_path = session_dir / "summary.json"
        # Track the larger of chat_history / summary for change detection.
        track_path = chat_path if chat_path.exists() else summary_path
        if not track_path.exists():
            continue
        source_key = str(chat_path if chat_path.exists() else session_dir)
        seen_sources.add(source_key)
        stat = track_path.stat()
        # Also invalidate if summary changed (title etc.)
        summary_mtime = summary_path.stat().st_mtime_ns if summary_path.exists() else 0
        summary_size = summary_path.stat().st_size if summary_path.exists() else 0
        summary = read_summary(session_dir)
        title = title_for_session(session_dir, summary)
        output_path = output_path_for_session(
            session_dir,
            title,
            output_dir,
            summary,
            ordinal,
            home_tag=home_tag,
        )
        old_record = old_sources.get(source_key, {})
        old_output = old_record.get("output")
        must_export = (
            not old_record
            or old_record.get("mtime_ns") != stat.st_mtime_ns
            or old_record.get("size") != stat.st_size
            or old_record.get("summary_mtime_ns") != summary_mtime
            or old_record.get("summary_size") != summary_size
            or old_record.get("title") != title
            or old_output != str(output_path)
            or not Path(old_output or "").exists()
            or not output_path.exists()
        )
        if must_export:
            record = export_session(
                session_dir,
                title,
                output_path,
                summary,
                include_reasoning=include_reasoning,
                include_system=include_system,
            )
            changed += 1
            if old_output and old_output != str(output_path):
                try:
                    Path(old_output).unlink()
                except FileNotFoundError:
                    pass
        else:
            record = {
                "session_id": old_record.get("session_id")
                or summary.get("info", {}).get("id")
                or session_id_from_path(session_dir),
                "title": title,
                "kind": "session",
                "source": source_key,
                "session_dir": str(session_dir),
                "output": str(output_path),
                "created": old_record.get("created", summary.get("created_at", "")),
                "updated": summary.get("updated_at")
                or summary.get("last_active_at")
                or old_record.get("updated", ""),
                "cwd": old_record.get("cwd")
                or (summary.get("info") or {}).get("cwd")
                or "",
                "model": summary.get("current_model_id") or old_record.get("model", ""),
                "counts": old_record.get("counts", {}),
                "bytes": old_record.get(
                    "bytes",
                    output_path.stat().st_size if output_path.exists() else 0,
                ),
            }
        record.update(
            {
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
                "summary_mtime_ns": summary_mtime,
                "summary_size": summary_size,
            }
        )
        new_sources[source_key] = record
        records.append(record)

    # --- prompt history files ---
    scan_histories: list[tuple[Path, str]] = []
    for scan_home, home_tag in scan_homes:
        scan_histories.extend(
            (path, home_tag) for path in iter_prompt_history_files(scan_home / "sessions")
        )
    for ordinal, (hist_path, home_tag) in enumerate(scan_histories, 1):
        source_key = str(hist_path)
        seen_sources.add(source_key)
        stat = hist_path.stat()
        title = "Grok CLI prompt history"
        # Prefer local created-from-mtime style naming for stability.
        try:
            created_local = datetime.fromtimestamp(stat.st_mtime).astimezone()
            prefix = created_local.strftime("%Y%m%d_%H%M%S")
        except (OSError, OverflowError, ValueError):
            prefix = f"{ordinal:03d}"
        sh = source_hash(hist_path)
        history_stem = (
            f"{prefix}__{home_tag}_history__{sh}__"
            if home_tag
            else f"{prefix}__history__{sh}__"
        )
        output_path = filesystem_safe_output_path(output_dir, history_stem, title)
        old_record = old_sources.get(source_key, {})
        old_output = old_record.get("output")
        must_export = (
            not old_record
            or old_record.get("mtime_ns") != stat.st_mtime_ns
            or old_record.get("size") != stat.st_size
            or old_record.get("title") != title
            or old_output != str(output_path)
            or not Path(old_output or "").exists()
            or not output_path.exists()
        )
        if must_export:
            record = export_prompt_history(hist_path, output_path)
            changed += 1
            if old_output and old_output != str(output_path):
                try:
                    Path(old_output).unlink()
                except FileNotFoundError:
                    pass
        else:
            record = {
                "session_id": "prompt_history",
                "title": title,
                "kind": "prompt-history",
                "source": source_key,
                "output": str(output_path),
                "created": old_record.get("created", ""),
                "updated": "",
                "counts": old_record.get("counts", {}),
                "bytes": old_record.get(
                    "bytes",
                    output_path.stat().st_size if output_path.exists() else 0,
                ),
            }
        record.update({"mtime_ns": stat.st_mtime_ns, "size": stat.st_size})
        new_sources[source_key] = record
        records.append(record)

    removed = prune_removed_sources(
        old_sources,
        seen_sources,
        retained_sources=new_sources,
        retained_records=records,
    )

    write_manifest(output_dir, grok_home, records, changed, removed, source_roots)
    atomic_write_json(
        state_path,
        {
            "updated_at": now_iso(),
            "sources": new_sources,
        },
    )
    return len(records), changed, removed


def main() -> int:
    args = parse_args()
    grok_home = Path(args.grok_home)
    output_dir = Path(args.output_dir)
    log_file = Path(args.log_file).expanduser() if args.log_file else None
    status_file = Path(args.status_file).expanduser() if args.status_file else None
    process_started_at = now_iso()

    while True:
        scan_started_at = now_iso()
        status = {
            "status": "running",
            "mode": "once" if args.once else "continuous",
            "pid": os.getpid(),
            "process_started_at": process_started_at,
            "scan_started_at": scan_started_at,
            "updated_at": scan_started_at,
            "grok_home": str(grok_home),
            "output_dir": str(output_dir),
        }
        try:
            write_runtime_status(status_file, status)
            total, changed, removed = scan_once(
                grok_home,
                output_dir,
                include_reasoning=args.include_reasoning,
                include_system=args.include_system,
            )
            scan_finished_at = now_iso()
            summary_text = f"grok tracked={total} changed={changed} removed={removed}"
            line = f"{scan_finished_at} {summary_text}"
            emit_runtime_line(line, log_file, quiet=args.quiet)
            status.update(
                {
                    "status": "completed" if args.once else "running",
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
            line = f"{scan_finished_at} ERROR: {type(exc).__name__}: {exc}"
            emit_runtime_line(line, log_file, error=True)
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
        if args.once:
            break
        time.sleep(max(args.interval, 1.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
