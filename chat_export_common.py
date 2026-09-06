#!/usr/bin/env python3
"""Shared helpers for Chat Export Hub live exporters."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
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


def is_wsl_source_record(source_key: str, record: dict[str, Any] | None = None) -> bool:
    """Return whether provenance identifies a remote source (WSL or VM)."""
    values = [str(source_key)]
    if record:
        for field in ("source", "source_db", "session_dir"):
            value = record.get(field)
            if value:
                values.append(str(value))
    for value in values:
        normalized = value.replace("\\", "/").lower()
        if normalized.startswith(("//wsl.localhost/", "//wsl$/")):
            return True
        if "@wsl-" in normalized:
            return True
        if "@vbox-" in normalized:
            return True
        if "@ssh-" in normalized:
            return True
        if "/chatexporthub/staging/vbox-" in normalized:
            return True
        if "/chatexporthub/staging/ssh-" in normalized:
            return True
    return False


def prune_removed_sources(
    old_sources: dict[str, Any],
    seen_sources: set[str],
    *,
    retained_sources: dict[str, Any] | None = None,
    retained_records: list[dict[str, Any]] | None = None,
) -> int:
    """Prune disappeared Windows sources while retaining reachable WSL exports.

    A WSL source is retained in both output collections only while its existing
    TXT still exists. This makes a temporary WSL/UNC outage non-destructive;
    a later scan seeing the source reconciles the record normally.
    """
    removed = 0
    for source_key, old_record in old_sources.items():
        if source_key in seen_sources:
            continue
        old_output = old_record.get("output")
        if is_wsl_source_record(source_key, old_record) and old_output and Path(old_output).exists():
            if retained_sources is not None:
                retained_sources[source_key] = old_record
            if retained_records is not None:
                retained_records.append(old_record)
            continue
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
            status["wsl_distros"] = wsl_distro_names()
            status["wsl_homes"] = [str(home) for home, _tag in wsl_user_homes()]
        except Exception:
            status["wsl_distros"] = []
            status["wsl_homes"] = []
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


# ---------------------------------------------------------------------------
# WSL support: discover agent data inside every registered WSL distro and
# expose it to the Windows-side exporters via \\wsl.localhost UNC paths.
# Works for any distro registered at scan time, so future WSL installs are
# picked up automatically.
# ---------------------------------------------------------------------------

_WSL_UNC_PREFIXES = ("//wsl.localhost/", "//wsl$/")


def wsl_distro_names() -> list[str]:
    """Names of every WSL distribution registered for the current user."""
    names: list[str] = []
    override = os.environ.get("CHAT_EXPORT_WSL_DISTROS", "")
    if override.strip():
        names.extend(x.strip() for x in override.split(",") if x.strip())
    try:
        import winreg
    except ImportError:
        return names
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Lxss"
        )
    except OSError:
        key = None
    if key is not None:
        index = 0
        while True:
            try:
                sub = winreg.EnumKey(key, index)
                index += 1
            except OSError:
                break
            try:
                with winreg.OpenKey(key, sub) as sk:
                    name = winreg.QueryValueEx(sk, "DistributionName")[0]
                if isinstance(name, str) and name.strip():
                    names.append(name.strip())
            except OSError:
                continue
    # Store-version WSL can expose the distro through the service while the
    # per-user registry view is unavailable to a watcher process. Fall back
    # to the documented list command and tolerate its UTF-16 console output.
    if not names and os.name == "nt":
        try:
            raw = subprocess.check_output(
                ["wsl.exe", "-l", "-q"],
                stderr=subprocess.DEVNULL,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            text = raw.decode("utf-16", errors="ignore")
            if "\x00" in text:
                text = raw.decode("utf-16-le", errors="ignore")
            for line in text.splitlines():
                name = line.replace("\x00", "").strip().lstrip("* ")
                if name and name.lower() not in {"windows subsystem for linux distributions:"}:
                    names.append(name)
        except (OSError, subprocess.SubprocessError, UnicodeError):
            pass
    return list(dict.fromkeys(names))


def wsl_user_homes() -> list[tuple[Path, str]]:
    """(home_path, tag) for each user home directory in every WSL distro.

    Returns UNC paths like ``\\\\wsl.localhost\\Ubuntu\\home\\mengz`` plus a
    stable tag like ``wsl-Ubuntu`` used for source keys / file names. Distros
    that are not running (or not reachable) are skipped silently.
    """
    out: list[tuple[Path, str]] = []
    users_override = [x.strip() for x in os.environ.get("CHAT_EXPORT_WSL_USERS", "").split(",") if x.strip()]
    for distro in wsl_distro_names():
        home_root = Path(rf"\\wsl.localhost\{distro}") / "home"
        try:
            users = (
                [home_root / user for user in users_override]
                if users_override
                else sorted(p for p in home_root.iterdir() if p.is_dir())
            )
        except OSError:
            continue
        for user_dir in users:
            try:
                if user_dir.is_dir():
                    out.append((user_dir, f"wsl-{distro}"))
            except OSError:
                continue
    return out


def wsl_agent_homes(relative: str) -> list[tuple[Path, str]]:
    """Existing ``<relative>`` agent directories across all WSL distros.

    ``relative`` is a POSIX-style path relative to a WSL user home, e.g.
    ``.claude`` or ``.local/share/kilo``. Returns ``(dir, tag)`` pairs.

    Results also fold in Oracle VM (VirtualBox) staged homes (``vbox-<vm>``
    tags) and configured remote SSH machines (``ssh-<alias>`` tags). Every
    exporter — including the bytecode-only Codex/Claude loader, which cannot
    be edited — resolves remote homes through this one function, so the
    union gives all of them remote coverage.
    """
    rel = relative.replace("/", os.sep) if os.sep != "/" else relative
    out: list[tuple[Path, str]] = []
    for home, tag in wsl_user_homes():
        candidate = home / rel
        try:
            if candidate.is_dir():
                out.append((candidate, tag))
        except OSError:
            continue
    out.extend(vbox_agent_homes(relative))
    out.extend(remote_ssh_agent_homes(relative))
    return out


def wsl_tag_for_path(path: Path) -> str:
    """Derive the WSL tag for a UNC path, or '' for non-WSL paths."""
    text = str(path).replace("\\", "/").lower()
    for prefix in _WSL_UNC_PREFIXES:
        if text.startswith(prefix):
            rest = str(path).replace("\\", "/")[len(prefix):]
            distro = rest.split("/", 1)[0]
            return f"wsl-{distro}"
    return ""


def stage_wsl_sqlite(unc_db: Path) -> Path | None:
    """Copy a WSL SQLite database to a local staging dir when it changed.

    WAL-mode databases cannot be opened read-only through the WSL 9P share,
    so exporters read a local copy instead. The db plus its ``-wal`` file are
    copied only when their (mtime, size) fingerprint changes; the wal is then
    checkpointed into the copy so it can be opened ``mode=ro``. Returns the
    staged path, or None when the source is unreachable.
    """
    tag_dir = source_hash(str(unc_db))
    staging_root = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        / "ChatExportHub"
        / "staging"
        / tag_dir
    )
    staged_db = staging_root / unc_db.name
    marker_path = staging_root / "staging.json"

    try:
        fp = file_fingerprint(unc_db)
        wal = Path(str(unc_db) + "-wal")
        wal_fp = file_fingerprint(wal) if wal.exists() else (0, 0)
    except OSError:
        return None

    marker: dict[str, Any] = {}
    if marker_path.is_file():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            marker = {}
    if (
        staged_db.is_file()
        and marker.get("db") == list(fp)
        and marker.get("wal") == list(wal_fp)
    ):
        return staged_db

    try:
        staging_root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(unc_db, staged_db)
        staged_wal = Path(str(staged_db) + "-wal")
        if wal_fp != (0, 0) and wal.is_file():
            shutil.copyfile(wal, staged_wal)
        elif staged_wal.exists():
            staged_wal.unlink()
        if staged_wal.exists():
            # Fold the WAL into the main db so mode=ro readers can open it.
            con = sqlite3.connect(staged_db)
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                con.close()
            staged_wal.unlink(missing_ok=True)
        marker_path.write_text(
            json.dumps({"db": list(fp), "wal": list(wal_fp)}), encoding="utf-8"
        )
        return staged_db
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Oracle VM (VirtualBox) support: pull agent directories from every running
# VirtualBox VM through its key-based SSH alias and scan local staged copies.
#
# VMs are discovered via VBoxManage. A VM contributes data only while it is
# running AND ~/.ssh/config defines a Host entry named exactly like the VM
# (e.g. ``Host vm1``) with working key auth — no passwords are stored. Both
# Windows and Linux guests are supported; the guest profile root resolves
# through ``whoami``.
# ---------------------------------------------------------------------------

VBOX_MANAGE_CANDIDATES = [
    r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe",
    r"C:\Program Files (x86)\Oracle\VirtualBox\VBoxManage.exe",
]
VBOX_SYNC_INTERVAL = float(os.environ.get("CHAT_EXPORT_VBOX_SYNC_INTERVAL", "300"))
VBOX_ATTEMPT_BACKOFF = float(os.environ.get("CHAT_EXPORT_VBOX_BACKOFF", "120"))
VBOX_LOCK_STALE_AFTER = 900.0
VBOX_SSH_TIMEOUT = 600.0

# Directories pulled from each VM user profile. Whole agent homes except
# .claude/.codex, which accumulate caches no exporter reads.
VBOX_PULL_RELPATHS = [
    ".claude/projects",
    ".claude/history.jsonl",
    ".claude.json",
    ".codex/sessions",
    ".codex/archived_sessions",
    ".grok",
    ".local/share/kilo",
    ".local/share/opencode",
    ".kiro",
    "AppData/Roaming/Kiro/User/globalStorage/kiro.kiroagent",
    "AppData/Roaming/Kiro/User/globalStorage/state.vscdb",
    ".workbuddy",
    ".workbuddy-ai",
    ".cline",
    ".continue",
    ".pi",
    "AppData/Roaming/TRAE SOLO",
    "AppData/Roaming/TRAE SOLO CN",
]

_vbox_vm_cache: tuple[float, list[tuple[str, bool]]] = (0.0, [])
_ssh_hosts_cache: tuple[float, dict[str, dict[str, str]]] = (0.0, {})


def vbox_vms() -> list[tuple[str, bool]]:
    """(name, running) for every registered VirtualBox VM, cached 60s."""
    global _vbox_vm_cache
    now = time.time()
    if now - _vbox_vm_cache[0] < 60.0:
        return _vbox_vm_cache[1]
    result: list[tuple[str, bool]] = []
    exe = os.environ.get("CHAT_EXPORT_VBOXMANAGE") or next(
        (p for p in VBOX_MANAGE_CANDIDATES if Path(p).is_file()), None
    )
    if exe:
        try:
            listed = subprocess.run(
                [exe, "list", "vms"],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            running = subprocess.run(
                [exe, "list", "runningvms"],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if listed.returncode == 0:
                names = re.findall(r'^"(.+)"\s+\{', listed.stdout, re.M)
                live = set(re.findall(r'^"(.+)"\s+\{', running.stdout or "", re.M))
                result = [(name, name in live) for name in names]
        except (OSError, subprocess.SubprocessError):
            result = []
    _vbox_vm_cache = (now, result)
    return result


def ssh_config_hosts() -> dict[str, dict[str, str]]:
    """Alias -> options parsed from ~/.ssh/config, cached 60s."""
    global _ssh_hosts_cache
    now = time.time()
    if now - _ssh_hosts_cache[0] < 60.0:
        return _ssh_hosts_cache[1]
    hosts: dict[str, dict[str, str]] = {}
    config = Path.home() / ".ssh" / "config"
    if config.is_file():
        current: list[str] = []
        try:
            lines = config.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, sep, value = stripped.partition(" ")
            if not sep:
                continue
            key = key.lower()
            if key == "host":
                current = value.split()
                for alias in current:
                    if "*" not in alias and "?" not in alias:
                        hosts.setdefault(alias, {})
            elif current:
                for alias in current:
                    if "*" not in alias and "?" not in alias:
                        hosts[alias][key] = value
    _ssh_hosts_cache = (now, hosts)
    return hosts


def _ssh_run(alias: str, command: str, timeout: float = 30.0) -> str | None:
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", alias, command],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _vbox_guest_profile(alias: str) -> Path | None:
    """Profile root of the SSH user inside the VM (Windows or Linux guest)."""
    probe = _ssh_run(alias, "cmd /c echo %OS%")
    if probe is not None and "Windows_NT" in probe:
        user = (_ssh_run(alias, "whoami") or "").strip()
        name = user.splitlines()[0].strip() if user else ""
        name = name.split("\\")[-1].strip()
        return Path(f"C:/Users/{name}") if name else None
    # Linux/macOS: ask the remote shell instead of guessing — home is not
    # always /home/<user> (e.g. SCRP uses /home/users/<user>).
    home = (_ssh_run(alias, "echo $HOME") or "").strip()
    if home.startswith("/"):
        return Path(home)
    user = (_ssh_run(alias, "whoami") or "").strip()
    name = user.splitlines()[0].strip() if user else ""
    name = name.split("@")[0].strip()
    return Path(f"/home/{name}") if name else None


def checkpoint_staged_dbs(root: Path) -> None:
    """Fold any -wal files into staged SQLite databases after a raw copy."""
    try:
        files = list(root.rglob("*"))
    except OSError:
        return
    for db in files:
        if not db.is_file():
            continue
        lowered = db.name.lower()
        if not (lowered.endswith(".db") or lowered.endswith(".vscdb")):
            continue
        wal = Path(str(db) + "-wal")
        if not wal.is_file():
            continue
        try:
            con = sqlite3.connect(db)
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                con.close()
            wal.unlink(missing_ok=True)
            Path(str(db) + "-shm").unlink(missing_ok=True)
        except (sqlite3.Error, OSError):
            continue


def _vbox_staging_root(tag: str) -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "ChatExportHub" / "staging" / tag


def _sync_ssh_machine(
    alias: str,
    tag: str,
    *,
    interval: float = VBOX_SYNC_INTERVAL,
    backoff: float = VBOX_ATTEMPT_BACKOFF,
    compress: bool = False,
) -> bool:
    """Refresh (when due) and report whether the staged remote copy is usable.

    Works for VirtualBox VMs and plain remote SSH machines alike. Gates: at
    most one sync per ``interval``, a failure ``backoff``, and an exclusive
    lock file so the many exporter processes never pull the same machine
    simultaneously. While another process holds the lock the existing staged
    copy is reused. Key auth only (BatchMode); unreachable or password-only
    machines are skipped until they become reachable.
    """
    staging_root = _vbox_staging_root(tag)
    home_dir = staging_root / "home"
    lock = staging_root / ".lock"
    last = staging_root / ".last"
    attempt = staging_root / ".attempt"

    def _age(path: Path) -> float:
        try:
            return max(0.0, time.time() - path.stat().st_mtime)
        except OSError:
            return float("inf")

    if _age(lock) < VBOX_LOCK_STALE_AFTER:
        return home_dir.is_dir()
    if _age(last) < interval:
        return home_dir.is_dir()
    if _age(attempt) < backoff:
        return home_dir.is_dir()

    try:
        staging_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    attempt.touch()
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return home_dir.is_dir()
    try:
        profile = _vbox_guest_profile(alias)
        if profile is None:
            return False
        include = " ".join(f'"{p}"' for p in VBOX_PULL_RELPATHS)
        remote = f'tar -c{"z" if compress else ""}f - -C "{profile.as_posix()}" {include}'
        pull_tar = staging_root / "pull.tar"
        try:
            with open(pull_tar, "wb") as fh:
                subprocess.run(
                    [
                        "ssh",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "ConnectTimeout=15",
                        alias,
                        remote,
                    ],
                    stdout=fh,
                    stderr=subprocess.DEVNULL,
                    timeout=VBOX_SSH_TIMEOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except (OSError, subprocess.SubprocessError):
            pull_tar.unlink(missing_ok=True)
            return False
        try:
            ok = pull_tar.is_file() and pull_tar.stat().st_size > 0
        except OSError:
            ok = False
        if not ok:
            pull_tar.unlink(missing_ok=True)
            return False
        # bsdtar exits non-zero when some requested dirs are missing; the
        # archive still contains every entry that exists, which is fine.
        tmp_dir = staging_root / ".pull.tmp"
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True)
        try:
            subprocess.run(
                ["tar", "-xzf" if compress else "-xf", str(pull_tar), "-C", str(tmp_dir)],
                check=True,
                capture_output=True,
                timeout=VBOX_SSH_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            pull_tar.unlink(missing_ok=True)
            return False
        pull_tar.unlink(missing_ok=True)
        checkpoint_staged_dbs(tmp_dir)
        retired = staging_root / ".home.retired"
        shutil.rmtree(retired, ignore_errors=True)
        if home_dir.exists():
            home_dir.rename(retired)
        tmp_dir.rename(home_dir)
        shutil.rmtree(retired, ignore_errors=True)
        last.touch()
        return True
    finally:
        lock.unlink(missing_ok=True)


def vbox_agent_homes(relative: str) -> list[tuple[Path, str]]:
    """Staged ``<relative>`` agent directories for every reachable VM.

    A VM contributes entries while it runs and ~/.ssh/config defines a Host
    alias named exactly like the VM with working key auth. Tags look like
    ``vbox-vm1``. Data is pulled at most every VBOX_SYNC_INTERVAL seconds
    into ``%LOCALAPPDATA%\\ChatExportHub\\staging\\vbox-<vm>\\home``.
    """
    rel = relative.replace("/", os.sep) if os.sep != "/" else relative
    out: list[tuple[Path, str]] = []
    hosts = ssh_config_hosts()
    for vm, running in vbox_vms():
        if not running or vm not in hosts:
            continue
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", vm).strip("_") or "vm"
        tag = f"vbox-{safe}"
        try:
            if not _sync_ssh_machine(vm, tag):
                continue
        except OSError:
            continue
        candidate = _vbox_staging_root(tag) / "home" / rel
        try:
            if candidate.is_dir():
                out.append((candidate, tag))
        except OSError:
            continue
    return out


# ---------------------------------------------------------------------------
# Remote SSH machines beyond VirtualBox (e.g. university servers reachable
# only from certain networks). Aliases come from chat_export_remote_hosts.json
# beside this module ({"hosts": ["alias", ...]}) or the CHAT_EXPORT_REMOTE_HOSTS
# environment variable. Each alias must have a working key-auth Host entry in
# ~/.ssh/config. Unreachable machines are skipped with a backoff and their
# existing exports are retained, so coverage resumes automatically once the
# network path exists (campus / VPN).
# ---------------------------------------------------------------------------

REMOTE_SYNC_INTERVAL = float(os.environ.get("CHAT_EXPORT_REMOTE_SYNC_INTERVAL", "900"))
REMOTE_ATTEMPT_BACKOFF = float(os.environ.get("CHAT_EXPORT_REMOTE_BACKOFF", "600"))
_remote_hosts_cache: tuple[float, list[str]] = (0.0, [])


def configured_remote_hosts() -> list[str]:
    """SSH aliases of extra remote machines to cover, cached 60s."""
    global _remote_hosts_cache
    override = os.environ.get("CHAT_EXPORT_REMOTE_HOSTS", "")
    if override.strip():
        return [x.strip() for x in override.split(",") if x.strip()]
    now = time.time()
    if now - _remote_hosts_cache[0] < 60.0:
        return _remote_hosts_cache[1]
    hosts: list[str] = []
    # Frozen .exe: __file__ points at the PyInstaller temp dir, so prefer the
    # JSON sitting next to the executable; fall back to beside this module
    # (source runs). First existing file wins.
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).with_name("chat_export_remote_hosts.json"))
    candidates.append(Path(__file__).with_name("chat_export_remote_hosts.json"))
    config = next((c for c in candidates if c.is_file()), None)
    if config is not None:
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            value = data.get("hosts", [])
            if isinstance(value, list):
                hosts.extend(str(x).strip() for x in value if str(x).strip())
    hosts = [h for h in dict.fromkeys(hosts) if h]
    _remote_hosts_cache = (now, hosts)
    return hosts


def remote_ssh_agent_homes(relative: str) -> list[tuple[Path, str]]:
    """Staged ``<relative>`` agent directories for configured remote machines.

    Tags look like ``ssh-scrp``. Data is pulled at most every
    REMOTE_SYNC_INTERVAL seconds (default 15 min — these machines are often
    reached over slower networks); failures back off for
    REMOTE_ATTEMPT_BACKOFF seconds before retrying.
    """
    rel = relative.replace("/", os.sep) if os.sep != "/" else relative
    out: list[tuple[Path, str]] = []
    for alias in configured_remote_hosts():
        if alias not in ssh_config_hosts():
            continue
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", alias).strip("_") or "remote"
        tag = f"ssh-{safe}"
        try:
            if not _sync_ssh_machine(
                alias,
                tag,
                interval=REMOTE_SYNC_INTERVAL,
                backoff=REMOTE_ATTEMPT_BACKOFF,
                compress=True,
            ):
                continue
        except OSError:
            continue
        candidate = _vbox_staging_root(tag) / "home" / rel
        try:
            if candidate.is_dir():
                out.append((candidate, tag))
        except OSError:
            continue
    return out
