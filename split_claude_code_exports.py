#!/usr/bin/env python3
"""Split Claude Code live exports into CLI and GUI (desktop app) agents.

The verified combined Codex/Claude exporter (bytecode loader under
``~/.codex/tools``) writes every ``~/.claude`` session transcript to
``~/claude_code_chat_live_exports`` no matter which Claude Code surface
created it.  This splitter post-processes that output into two agents:

* **Claude Code CLI** — sessions whose entrypoint is ``cli``, ``sdk-cli`` or
  ``claude-vscode`` (the VS Code extension), plus the shared prompt history.
  The raw TXT exports stay where the combined exporter wrote them; the
  splitter derives a filtered ``.export_state.cli.json`` beside the raw state.
* **Claude Code GUI** — sessions created by the Claude desktop app
  (entrypoint ``claude-desktop*``).  Their TXT exports are copied to
  ``~/claude_code_gui_chat_live_exports`` with a dedicated
  ``.export_state.json``.

Session records without an ``Entrypoint:`` header (workflow journals) are
attributed by inspecting sibling / parent session transcripts.

Roles:
  ``--role cli`` / ``--role gui``  hub "export now": run the combined exporter
                                   once, then split, print a summary, exit.
  ``--role split``                 background watcher: split only (cheap),
                                   looping every ``--interval`` seconds.

The raw exporter's own state and TXT files are never moved or rewritten, so
its incremental reuse logic (mtime/size/output-path checks) keeps working and
the existing "Codex Claude Chat Export Watcher" task is untouched.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_TOOLS = Path(__file__).resolve().parent
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from chat_export_common import (
    append_log_line,
    file_fingerprint,
    load_json,
    now_iso,
    write_console_line,
    write_manifest,
)

HOME = Path.home()

# Entrypoints that belong to the Claude desktop app (GUI).  Everything else
# (cli, sdk-cli, claude-vscode, ...) stays with the CLI agent.
GUI_ENTRYPOINT = "claude-desktop"

RAW_EXPORTER = HOME / ".codex" / "tools" / "export_codex_claude_chats_live.py"

DEFAULT_CLI_DIR = HOME / "claude_code_chat_live_exports"
DEFAULT_GUI_DIR = HOME / "claude_code_gui_chat_live_exports"

RAW_STATE_NAME = ".export_state.json"
CLI_STATE_NAME = ".export_state.cli.json"

HEADER_ENTRYPOINT_RE = re.compile(r"^Entrypoint:\s*(\S+)\s*$", re.MULTILINE)
JSONL_ENTRYPOINT_RE = re.compile(r'"entrypoint"\s*:\s*"([^"]+)"')

# How much of an exported TXT / source JSONL to read when looking for the
# entrypoint marker.  Both appear in the first records of a file.
TXT_HEADER_BYTES = 8192
JSONL_PROBE_BYTES = 65536


def _read_head(path: Path, limit: int) -> str:
    try:
        with path.open("rb") as handle:
            chunk = handle.read(limit)
    except OSError:
        return ""
    return chunk.decode("utf-8", errors="replace")


def txt_entrypoint(txt_path: Path) -> str:
    """Entrypoint recorded in an exported TXT header, '' when absent."""
    match = HEADER_ENTRYPOINT_RE.search(_read_head(txt_path, TXT_HEADER_BYTES))
    return match.group(1) if match else ""


def jsonl_entrypoint(jsonl_path: Path) -> str:
    """First entrypoint found near the top of a session transcript."""
    match = JSONL_ENTRYPOINT_RE.search(_read_head(jsonl_path, JSONL_PROBE_BYTES))
    return match.group(1) if match else ""


def infer_entrypoint(source_path: Path) -> str:
    """Attribute a source without its own Entrypoint header (journals).

    Looks at sibling session transcripts in the same directory first, then
    walks up looking for the owning session transcript (the
    ``<session-id>/<session-id>.jsonl`` layout under ~/.claude/projects).
    """
    try:
        siblings = sorted(
            p
            for p in source_path.parent.iterdir()
            if p.is_file() and p.suffix == ".jsonl" and p != source_path
        )
    except OSError:
        siblings = []
    for sibling in siblings:
        entrypoint = jsonl_entrypoint(sibling)
        if entrypoint:
            return entrypoint

    directory = source_path.parent
    for _ in range(8):
        parent = directory.parent
        if parent is None or parent == directory:
            break
        candidate = parent / (directory.name + ".jsonl")
        if candidate.is_file():
            entrypoint = jsonl_entrypoint(candidate)
            if entrypoint:
                return entrypoint
        directory = parent
    return ""


def is_gui_entrypoint(entrypoint: str) -> bool:
    return entrypoint == GUI_ENTRYPOINT or entrypoint.startswith(GUI_ENTRYPOINT + "-")


def _atomic_copy(src: Path, dst: Path) -> None:
    """Copy with a process-unique temp name so concurrent splits stay safe."""
    tmp = dst.with_name(f"{dst.name}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(src, tmp)
        tmp.replace(dst)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _atomic_write_json(path: Path, obj: Any) -> None:
    """atomic_write_json with a process-unique temp name (concurrent-safe)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )
    tmp.replace(path)


def classify_record(record: dict[str, Any]) -> bool:
    """Return True when a raw state record belongs to the GUI agent."""
    if str(record.get("kind") or "") == "prompt-history":
        return False
    output = record.get("output")
    entrypoint = txt_entrypoint(Path(output)) if output else ""
    if not entrypoint:
        source = record.get("source")
        if source:
            entrypoint = infer_entrypoint(Path(source))
    return is_gui_entrypoint(entrypoint)


def refresh_gui_copy(
    record: dict[str, Any], old_gui_record: dict[str, Any] | None, gui_dir: Path
) -> tuple[dict[str, Any] | None, bool, str | None]:
    """Mirror one GUI export into the GUI output dir.

    Returns ``(gui_record, changed, obsolete_output)``.  ``gui_record`` is
    None when the raw TXT is momentarily unavailable (export race) and no
    previous copy exists; the source is then skipped for this round.
    """
    raw_output = str(record.get("output") or "")
    src = Path(raw_output)
    fingerprint = file_fingerprint(src) if raw_output else (0, 0)
    if fingerprint == (0, 0):
        # Raw TXT missing right now (mid-replace / mid-prune race): keep the
        # previous copy if we have one, otherwise skip this round.
        if old_gui_record:
            return dict(old_gui_record), False, None
        return None, False, None

    dst = gui_dir / src.name
    prev_fp = tuple(old_gui_record.get("raw_fp") or []) if old_gui_record else None
    if prev_fp == fingerprint and dst.exists():
        gui_record = dict(old_gui_record)
        # Keep display fields authoritative from the raw record.
        for field in ("title", "kind", "created", "updated", "counts", "session_id", "source_id", "source"):
            if field in record:
                gui_record[field] = record[field]
        return gui_record, False, None

    _atomic_copy(src, dst)
    gui_record = dict(record)
    gui_record["output"] = str(dst)
    try:
        gui_record["bytes"] = dst.stat().st_size
    except OSError:
        gui_record["bytes"] = record.get("bytes") or 0
    gui_record["raw_output"] = raw_output
    gui_record["raw_fp"] = list(fingerprint)

    obsolete = None
    if old_gui_record and old_gui_record.get("output") and old_gui_record["output"] != str(dst):
        obsolete = str(old_gui_record["output"])
    return gui_record, True, obsolete


def split_once(cli_dir: Path, gui_dir: Path) -> dict[str, int] | None:
    """Derive CLI/GUI states from the raw exporter state.

    Returns a small counts dict, or None when the raw state is not available
    yet (first run before the combined exporter ever wrote it).
    """
    raw_state_path = cli_dir / RAW_STATE_NAME
    if not raw_state_path.is_file():
        return None
    raw_obj = load_json(raw_state_path, None)
    if not isinstance(raw_obj, dict):
        # Unparseable state — treat as transient, never wipe derived states.
        return None
    raw_sources = raw_obj.get("sources")
    if not isinstance(raw_sources, dict):
        raw_sources = {}

    gui_state_path = gui_dir / RAW_STATE_NAME
    cli_state_path = cli_dir / CLI_STATE_NAME
    old_gui_obj = load_json(gui_state_path, {})
    old_gui_sources = (
        old_gui_obj.get("sources") if isinstance(old_gui_obj, dict) else {}
    ) or {}
    old_cli_obj = load_json(cli_state_path, {})
    old_cli_sources = (
        old_cli_obj.get("sources") if isinstance(old_cli_obj, dict) else {}
    ) or {}

    gui_dir.mkdir(parents=True, exist_ok=True)

    cli_sources: dict[str, Any] = {}
    gui_sources: dict[str, Any] = {}
    gui_records: list[dict[str, Any]] = []
    gui_changed = 0
    obsolete_outputs: list[str] = []

    for source_key, record in raw_sources.items():
        if not isinstance(record, dict):
            continue
        if classify_record(record):
            gui_record, changed, obsolete = refresh_gui_copy(
                record, old_gui_sources.get(source_key), gui_dir
            )
            if obsolete:
                obsolete_outputs.append(obsolete)
            if gui_record is None:
                # Momentarily unavailable and never copied — keep any previous
                # entry so the GUI agent does not flicker.
                previous = old_gui_sources.get(source_key)
                if previous:
                    gui_sources[source_key] = previous
                    gui_records.append(previous)
                continue
            gui_changed += int(changed)
            gui_sources[source_key] = gui_record
            gui_records.append(gui_record)
        else:
            cli_sources[source_key] = record

    removed = 0
    for source_key, old_record in old_gui_sources.items():
        if source_key in gui_sources:
            continue
        old_output = old_record.get("output")
        if old_output:
            try:
                Path(old_output).unlink()
                removed += 1
            except OSError:
                pass
    for obsolete in obsolete_outputs:
        try:
            Path(obsolete).unlink()
        except OSError:
            pass

    # Only rewrite derived state / manifest when the source sets actually
    # changed, so the hub's mtime-based refresh stays quiet on idle cycles.
    if cli_sources != old_cli_sources or not cli_state_path.is_file():
        _atomic_write_json(
            cli_state_path,
            {
                "updated_at": now_iso(),
                "split_of": str(raw_state_path),
                "sources": cli_sources,
            },
        )
    if gui_sources != old_gui_sources or not gui_state_path.is_file():
        _atomic_write_json(
            gui_state_path,
            {
                "updated_at": now_iso(),
                "split_of": str(raw_state_path),
                "sources": gui_sources,
            },
        )
        try:
            write_manifest(
                gui_dir,
                "Claude Code GUI (desktop app)",
                gui_records,
                gui_changed,
                removed,
            )
        except OSError:
            pass

    return {
        "cli": len(cli_sources),
        "gui": len(gui_sources),
        "changed": gui_changed,
        "removed": removed,
    }


def run_raw_exporter(claude_home: Path, cli_dir: Path) -> tuple[int, str]:
    """One incremental pass of the verified combined exporter (claude only)."""
    cmd = [
        sys.executable,
        str(RAW_EXPORTER),
        "--claude-home",
        str(claude_home),
        "--claude-output-dir",
        str(cli_dir),
        "--no-codex",
        "--once",
        "--quiet",
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"raw exporter failed: {exc}"
    output = (completed.stdout or completed.stderr or "").strip()
    return completed.returncode, output


def summary_line(counts: dict[str, int] | None) -> str:
    if counts is None:
        return "claude tracked=0 changed=0 removed=0 | claude-gui tracked=0 changed=0 removed=0 (raw state not ready)"
    return (
        f"claude tracked={counts['cli']} changed=0 removed=0 | "
        f"claude-gui tracked={counts['gui']} changed={counts['changed']} removed={counts['removed']}"
    )


def parse_args() -> argparse.Namespace:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--role",
        choices=("cli", "gui", "split"),
        default="split",
        help="cli/gui: run the combined exporter once then split; split: split only",
    )
    parser.add_argument("--claude-home", default=str(home / ".claude"))
    parser.add_argument("--cli-output-dir", default=str(DEFAULT_CLI_DIR))
    parser.add_argument("--gui-output-dir", default=str(DEFAULT_GUI_DIR))
    parser.add_argument("--interval", type=float, default=0.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--log-file")
    parser.add_argument("--status-file")
    return parser.parse_args()


def write_status(status_file: Path | None, status: dict[str, Any]) -> None:
    if status_file is None:
        return
    try:
        _atomic_write_json(status_file, status)
    except OSError as exc:
        write_console_line(
            f"{now_iso()} ERROR: could not write status file {status_file}: {exc}",
            error=True,
        )


def run_once(
    args: argparse.Namespace,
    log_file: Path | None,
    status_file: Path | None,
    *,
    mode: str,
) -> int:
    """One full pass. Returns process exit code."""
    claude_home = Path(args.claude_home).expanduser()
    cli_dir = Path(args.cli_output_dir).expanduser()
    gui_dir = Path(args.gui_output_dir).expanduser()

    process_started_at = now_iso()
    scan_started_at = now_iso()
    status: dict[str, Any] = {
        "status": "running",
        "mode": mode,
        "pid": os.getpid(),
        "process_started_at": process_started_at,
        "scan_started_at": scan_started_at,
        "updated_at": scan_started_at,
        "claude_source": str(claude_home),
        "claude_output": str(cli_dir),
        "claude_gui_output": str(gui_dir),
    }
    write_status(status_file, status)

    exit_code = 0
    error_text = ""
    if args.role in ("cli", "gui"):
        if not RAW_EXPORTER.is_file():
            exit_code = 1
            error_text = f"combined exporter not found: {RAW_EXPORTER}"
            write_console_line(f"{now_iso()} ERROR: {error_text}", error=True)
            if log_file is not None:
                try:
                    append_log_line(log_file, f"{now_iso()} ERROR: {error_text}")
                except OSError:
                    pass
        else:
            rc, output = run_raw_exporter(claude_home, cli_dir)
            if rc != 0:
                # Non-fatal: the raw state on disk may still be perfectly
                # usable, so keep going and split whatever we have.
                error_text = output or f"raw exporter exit {rc}"
                write_console_line(f"{now_iso()} ERROR: {error_text}", error=True)
                if log_file is not None:
                    try:
                        append_log_line(log_file, f"{now_iso()} ERROR: {error_text}")
                    except OSError:
                        pass

    counts: dict[str, int] | None = None
    try:
        counts = split_once(cli_dir, gui_dir)
    except Exception as exc:  # never take the watcher down
        exit_code = 1
        error_text = f"{type(exc).__name__}: {exc}"
        write_console_line(f"{now_iso()} ERROR: {error_text}", error=True)
        if log_file is not None:
            try:
                append_log_line(log_file, f"{now_iso()} ERROR: {error_text}")
            except OSError:
                pass

    scan_finished_at = now_iso()
    text = summary_line(counts)
    if error_text:
        text = f"{'ERROR: ' + error_text if exit_code else 'WARNING: ' + error_text} | {text}"
    if log_file is not None:
        try:
            append_log_line(log_file, f"{scan_finished_at} {text}")
        except OSError:
            pass
    if not args.quiet:
        write_console_line(f"{scan_finished_at} {text}")

    status.update(
        {
            "status": "error" if exit_code != 0 else ("completed" if args.once else "running"),
            "scan_finished_at": scan_finished_at,
            "updated_at": scan_finished_at,
            "summaries": [
                f"claude tracked={counts['cli'] if counts else 0} changed=0 removed=0",
                f"claude-gui tracked={counts['gui'] if counts else 0} "
                f"changed={counts['changed'] if counts else 0} "
                f"removed={counts['removed'] if counts else 0}",
            ],
            "tracked": (counts["cli"] + counts["gui"]) if counts else 0,
            "changed": counts["changed"] if counts else 0,
            "removed": counts["removed"] if counts else 0,
        }
    )
    if exit_code != 0:
        status["error"] = error_text
    elif error_text:
        status["warning"] = error_text
    if counts is None:
        status["waiting_for_raw_state"] = True
    write_status(status_file, status)
    return exit_code


def main() -> int:
    args = parse_args()
    log_file = Path(args.log_file).expanduser() if args.log_file else None
    status_file = Path(args.status_file).expanduser() if args.status_file else None

    loop = args.interval and args.interval > 0 and not args.once
    if not loop:
        return run_once(args, log_file, status_file, mode="once")

    # Watcher mode.
    while True:
        code = run_once(args, log_file, status_file, mode="continuous")
        if code != 0:
            # Back off briefly after an error, then keep watching.
            time.sleep(30.0)
        time.sleep(max(args.interval, 5.0))


if __name__ == "__main__":
    raise SystemExit(main())
