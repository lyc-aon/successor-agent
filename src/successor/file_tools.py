"""Native read/write/edit file tools for the chat runtime."""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import tomllib

from .bash.cards import ToolCard
from .bash.diff_artifact import build_change_artifact_from_text
from .tool_runner import ToolExecutionResult, ToolProgress

_MAX_TEXT_BYTES = 512 * 1024
_MAX_INLINE_LINES = 4000
FILE_UNCHANGED_STUB = (
    "File unchanged since the last full read. The earlier read_file "
    "result in this conversation is still current; refer to that "
    "instead of re-reading it."
)


class FileToolError(RuntimeError):
    """Raised when a file tool request cannot be completed safely."""


@dataclass(slots=True)
class FileReadStateEntry:
    path: str
    content: str
    timestamp: float
    mtime_ns: int | None
    partial: bool
    offset: int | None = None
    limit: int | None = None


@dataclass(slots=True)
class FileReadTracker:
    last_key: tuple[str, int, int | None] | None = None
    consecutive: int = 0


@dataclass(slots=True, frozen=True)
class FileValidationResult:
    command: str
    ok: bool
    skipped: bool = False
    summary: str = ""
    output: str = ""

    def to_metadata(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "ok": self.ok,
            "skipped": self.skipped,
            "summary": self.summary,
            "output": self.output,
        }


def normalize_file_path(file_path: str, *, working_directory: str) -> str:
    text = str(file_path or "").strip()
    if not text:
        raise FileToolError("file_path is required")
    expanded = os.path.expandvars(os.path.expanduser(text))
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    base = working_directory or os.getcwd()
    return os.path.normpath(os.path.abspath(os.path.join(base, expanded)))


def read_file_preview_card(arguments: dict[str, Any], *, tool_call_id: str) -> ToolCard:
    path = str(arguments.get("file_path") or "").strip()
    offset = arguments.get("offset")
    limit = arguments.get("limit")
    params: list[tuple[str, str]] = []
    if path:
        params.append(("path", path))
    if isinstance(offset, int) and offset > 1:
        params.append(("offset", str(offset)))
    if isinstance(limit, int) and limit > 0:
        params.append(("limit", str(limit)))
    return ToolCard(
        verb="read-file",
        params=tuple(params),
        risk="safe",
        raw_command=path or "read_file",
        confidence=1.0,
        parser_name="native-read_file",
        tool_name="read_file",
        tool_arguments={
            key: value
            for key, value in arguments.items()
            if value not in (None, "", False)
        },
        raw_label_prefix="⟫",
        tool_call_id=tool_call_id,
    )


def write_file_preview_card(arguments: dict[str, Any], *, tool_call_id: str) -> ToolCard:
    path = str(arguments.get("file_path") or "").strip()
    return ToolCard(
        verb="write-file",
        params=(("path", path),) if path else (),
        risk="mutating",
        raw_command=path or "write_file",
        confidence=1.0,
        parser_name="native-write_file",
        tool_name="write_file",
        tool_arguments={
            key: value
            for key, value in arguments.items()
            if value not in (None, "", False)
        },
        raw_label_prefix="✎",
        tool_call_id=tool_call_id,
    )


def edit_file_preview_card(arguments: dict[str, Any], *, tool_call_id: str) -> ToolCard:
    path = str(arguments.get("file_path") or "").strip()
    replace_all = bool(arguments.get("replace_all"))
    params: list[tuple[str, str]] = []
    if path:
        params.append(("path", path))
    if replace_all:
        params.append(("replace_all", "true"))
    return ToolCard(
        verb="edit-file",
        params=tuple(params),
        risk="mutating",
        raw_command=path or "edit_file",
        confidence=1.0,
        parser_name="native-edit_file",
        tool_name="edit_file",
        tool_arguments={
            key: value
            for key, value in arguments.items()
            if value not in (None, "", False)
        },
        raw_label_prefix="✎",
        tool_call_id=tool_call_id,
    )


def run_read_file(
    arguments: dict[str, Any],
    *,
    preview: ToolCard,
    read_state: dict[str, FileReadStateEntry],
    read_tracker: FileReadTracker | None = None,
    working_directory: str,
    progress: ToolProgress | None = None,
) -> ToolExecutionResult:
    del progress
    path = normalize_file_path(
        str(arguments.get("file_path") or ""),
        working_directory=working_directory,
    )
    offset = _coerce_offset(arguments.get("offset"))
    limit = _coerce_limit(arguments.get("limit"))
    read_key = (path, offset, limit)
    repeated_count = _note_read_call(read_tracker, read_key)
    if repeated_count >= 4:
        raise FileToolError(
            "This exact path/range has been read 4 times consecutively without "
            "any intervening non-read tool call. Use the earlier read result or "
            "take a different step instead of re-reading it again.",
        )

    deduped = _maybe_reuse_prior_full_read(
        path,
        offset=offset,
        limit=limit,
        read_state=read_state,
    )
    if deduped is not None:
        output = deduped
        if repeated_count == 3:
            output = (
                "Warning: this is the third consecutive identical read of the "
                "same unchanged file region with no intervening non-read tool "
                "call.\n\n"
                f"{output}"
            )
        final_card = replace(
            preview,
            params=_replace_param(preview.params, "path", path),
            raw_command=path,
            output=output,
            exit_code=0,
            duration_ms=0.0,
        )
        return ToolExecutionResult(
            output=output,
            exit_code=0,
            final_card=final_card,
            metadata={
                "path": path,
                "offset": offset,
                "limit": limit,
                "unchanged": True,
                "repeated_read_count": repeated_count,
            },
        )

    text, mtime_ns = _read_text_file(path)
    lines = text.splitlines()
    total_lines = len(lines)

    start_index = max(0, offset - 1)
    if limit is None:
        end_index = total_lines
    else:
        end_index = min(total_lines, start_index + limit)

    partial = start_index > 0 or end_index < total_lines
    shown = lines[start_index:end_index]
    output = _format_read_output(
        path,
        shown,
        total_lines=total_lines,
        start_line=offset,
        partial=partial,
    )
    if repeated_count == 3:
        output = (
            "Warning: this is the third consecutive identical read of the same "
            "path/range with no intervening non-read tool call.\n\n"
            f"{output}"
        )
    read_state[path] = FileReadStateEntry(
        path=path,
        content=_normalize_newlines(text),
        timestamp=time.time(),
        mtime_ns=mtime_ns,
        partial=partial,
        offset=offset,
        limit=limit,
    )
    final_card = replace(
        preview,
        params=_replace_param(preview.params, "path", path),
        raw_command=path,
        output=output,
        exit_code=0,
        duration_ms=0.0,
    )
    return ToolExecutionResult(
        output=output,
        exit_code=0,
        final_card=final_card,
        metadata={
            "path": path,
            "offset": offset,
            "limit": limit,
            "partial": partial,
            "total_lines": total_lines,
            "repeated_read_count": repeated_count,
        },
    )


def run_write_file(
    arguments: dict[str, Any],
    *,
    preview: ToolCard,
    read_state: dict[str, FileReadStateEntry],
    working_directory: str,
    progress: ToolProgress | None = None,
) -> ToolExecutionResult:
    del progress
    path = normalize_file_path(
        str(arguments.get("file_path") or ""),
        working_directory=working_directory,
    )
    content = _require_string(arguments.get("content"), name="content")
    mode = str(arguments.get("mode", "overwrite")).lower()
    existing = _read_optional_text_file(path)
    if existing is not None:
        _require_full_read(path, read_state)
        _ensure_not_stale(path, current_text=existing[0], read_state=read_state)

    if mode == "append":
        final_content = (existing[0] if existing else "") + content
    else:
        final_content = content

    before_raw = existing[0] if existing is not None else None
    _write_text_file(path, final_content)
    after_raw = final_content
    after_normalized = _normalize_newlines(after_raw)
    stat_info = os.stat(path)
    read_state[path] = FileReadStateEntry(
        path=path,
        content=after_normalized,
        timestamp=time.time(),
        mtime_ns=getattr(stat_info, "st_mtime_ns", None),
        partial=False,
    )
    artifact = build_change_artifact_from_text(path, before_raw, after_raw)
    action = "created" if before_raw is None else "updated"
    validation = _validate_written_file(path, working_directory=working_directory)
    output = _format_mutation_output(f"{action} {path}", validation)
    final_card = replace(
        preview,
        params=_replace_param(preview.params, "path", path),
        raw_command=path,
        output=output,
        exit_code=0,
        duration_ms=0.0,
        change_artifact=artifact,
    )
    return ToolExecutionResult(
        output=output,
        exit_code=0,
        final_card=final_card,
        metadata={
            "path": path,
            "created": before_raw is None,
            **(
                {"validation": validation.to_metadata()}
                if validation is not None
                else {}
            ),
        },
    )


def run_edit_file(
    arguments: dict[str, Any],
    *,
    preview: ToolCard,
    read_state: dict[str, FileReadStateEntry],
    working_directory: str,
    progress: ToolProgress | None = None,
) -> ToolExecutionResult:
    del progress
    path = normalize_file_path(
        str(arguments.get("file_path") or ""),
        working_directory=working_directory,
    )
    old_string = _require_string(arguments.get("old_string"), name="old_string")
    new_string = _require_string(arguments.get("new_string"), name="new_string")
    replace_all = bool(arguments.get("replace_all"))
    if old_string == "":
        raise FileToolError("old_string cannot be empty. Use write_file for full-file writes.")
    if old_string == new_string:
        raise FileToolError(
            "old_string and new_string are identical; edit_file would make no changes",
        )
    existing = _read_optional_text_file(path)
    if existing is None:
        raise FileToolError("File does not exist. Use write_file to create new files.")
    raw_text, _mtime_ns = existing
    _require_full_read(path, read_state)
    _ensure_not_stale(path, current_text=raw_text, read_state=read_state)

    line_ending = _detect_line_ending(raw_text)
    normalized_text = _normalize_newlines(raw_text)
    normalized_old = _normalize_newlines(old_string)
    normalized_new = _normalize_newlines(new_string)

    matches = normalized_text.count(normalized_old)
    if matches == 0:
        raise FileToolError("old_string was not found in the current file content")
    if matches > 1 and not replace_all:
        raise FileToolError(
            f"old_string matched {matches} locations; set replace_all=true or provide a unique snippet",
        )

    replaced_normalized = (
        normalized_text.replace(normalized_old, normalized_new)
        if replace_all else
        normalized_text.replace(normalized_old, normalized_new, 1)
    )
    replaced_raw = _restore_newlines(replaced_normalized, line_ending)

    _write_text_file(path, replaced_raw)
    stat_info = os.stat(path)
    read_state[path] = FileReadStateEntry(
        path=path,
        content=replaced_normalized,
        timestamp=time.time(),
        mtime_ns=getattr(stat_info, "st_mtime_ns", None),
        partial=False,
    )
    artifact = build_change_artifact_from_text(path, raw_text, replaced_raw)
    count = matches if replace_all else 1
    noun = "occurrence" if count == 1 else "occurrences"
    validation = _validate_written_file(path, working_directory=working_directory)
    output = _format_mutation_output(
        f"replaced {count} {noun} in {path}",
        validation,
    )
    final_card = replace(
        preview,
        params=_replace_param(preview.params, "path", path),
        raw_command=path,
        output=output,
        exit_code=0,
        duration_ms=0.0,
        change_artifact=artifact,
    )
    return ToolExecutionResult(
        output=output,
        exit_code=0,
        final_card=final_card,
        metadata={
            "path": path,
            "replacement_count": count,
            "replace_all": replace_all,
            **(
                {"validation": validation.to_metadata()}
                if validation is not None
                else {}
            ),
        },
    )


def _read_text_file(path: str) -> tuple[str, int | None]:
    file_path = Path(path)
    if not file_path.exists():
        raise FileToolError(f"File does not exist: {path}")
    if file_path.is_dir():
        raise FileToolError(f"Path is a directory, not a file: {path}")
    st = file_path.stat()
    if stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode) or stat.S_ISFIFO(st.st_mode):
        raise FileToolError(f"Refusing to read device or stream path: {path}")
    if st.st_size > _MAX_TEXT_BYTES:
        raise FileToolError(
            f"File is too large to read safely ({st.st_size} bytes). Use offset/limit on a smaller text file.",
        )
    raw = file_path.read_bytes()
    if b"\x00" in raw:
        raise FileToolError(f"File appears to be binary and cannot be read as text: {path}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise FileToolError(f"File is not valid UTF-8 text: {path}") from exc
    return text, getattr(st, "st_mtime_ns", None)


def _maybe_reuse_prior_full_read(
    path: str,
    *,
    offset: int,
    limit: int | None,
    read_state: dict[str, FileReadStateEntry],
) -> str | None:
    entry = read_state.get(path)
    if entry is None or entry.partial or entry.offset is None:
        return None
    if entry.offset != offset or entry.limit != limit:
        return None
    file_path = Path(path)
    if not file_path.exists() or file_path.is_dir():
        return None
    try:
        stat_info = file_path.stat()
    except OSError:
        return None
    current_mtime_ns = getattr(stat_info, "st_mtime_ns", None)
    if current_mtime_ns != entry.mtime_ns:
        return None
    return FILE_UNCHANGED_STUB


def _read_optional_text_file(path: str) -> tuple[str, int | None] | None:
    file_path = Path(path)
    if not file_path.exists():
        return None
    return _read_text_file(path)


def _write_text_file(path: str, content: str) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8", newline="")


def _validate_written_file(
    path: str,
    *,
    working_directory: str,
) -> FileValidationResult | None:
    candidate = Path(path)
    suffix = candidate.suffix.lower()

    command: list[str] | None = None
    cwd: str | None = None
    summary = ""
    if suffix == ".py":
        ruff_root = _find_ruff_root(
            start=candidate.parent,
            working_directory=Path(working_directory),
        )
        if ruff_root is not None and shutil.which("ruff"):
            command = ["ruff", "check", path]
            cwd = str(ruff_root)
            summary = "ruff check"
        else:
            command = [sys.executable, "-m", "py_compile", path]
            summary = "py_compile"
    elif suffix == ".json":
        command = [sys.executable, "-m", "json.tool", path]
        summary = "json.tool"
    elif suffix in {".js", ".mjs", ".cjs"} and shutil.which("node"):
        command = ["node", "--check", path]
        summary = "node --check"

    if command is None:
        return None

    quoted = " ".join(shlex.quote(part) for part in command)
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return FileValidationResult(
            command=quoted,
            ok=False,
            summary=summary,
            output=str(exc),
        )

    output = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    detail = stderr or output
    if detail:
        detail = _clip_text(detail, limit=1600)
    return FileValidationResult(
        command=quoted,
        ok=(result.returncode == 0),
        summary=summary,
        output=detail,
    )


def _find_ruff_root(start: Path, *, working_directory: Path) -> Path | None:
    current = start.resolve()
    boundary = working_directory.expanduser().resolve()
    candidates = [current, *current.parents]
    for candidate in candidates:
        if not candidate.exists():
            continue
        if (candidate / "ruff.toml").is_file() or (candidate / ".ruff.toml").is_file():
            return candidate
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file() and _pyproject_declares_ruff(pyproject):
            return candidate
        if candidate == boundary:
            break
    return None


def _pyproject_declares_ruff(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    tool = data.get("tool")
    if isinstance(tool, dict) and isinstance(tool.get("ruff"), dict):
        return True
    project = data.get("project")
    if not isinstance(project, dict):
        return False
    optional = project.get("optional-dependencies")
    if not isinstance(optional, dict):
        return False
    for group in optional.values():
        if not isinstance(group, list):
            continue
        for item in group:
            if isinstance(item, str) and item.strip().lower().startswith("ruff"):
                return True
    return False


def _format_mutation_output(
    base_output: str,
    validation: FileValidationResult | None,
) -> str:
    if validation is None:
        return base_output
    if validation.ok:
        status_line = f"fast check: {validation.command} (ok)"
    else:
        status_line = f"fast check: {validation.command} (failed)"
    if validation.output:
        return f"{base_output}\n\n{status_line}\n\n{validation.output}"
    return f"{base_output}\n\n{status_line}"


def _clip_text(text: str, *, limit: int) -> str:
    compact = text.strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


def _coerce_offset(value: Any) -> int:
    if value in (None, ""):
        return 1
    if not isinstance(value, int) or value < 1:
        raise FileToolError("offset must be an integer greater than or equal to 1")
    return value


def _coerce_limit(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if not isinstance(value, int) or value < 1:
        raise FileToolError("limit must be a positive integer")
    return value


def _require_string(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise FileToolError(f"{name} must be a string")
    return value


def _require_full_read(path: str, read_state: dict[str, FileReadStateEntry]) -> FileReadStateEntry:
    entry = read_state.get(path)
    if entry is None:
        raise FileToolError("File has not been read yet. Read it first before writing to it.")
    if entry.partial:
        raise FileToolError(
            "File was only read partially. Read the full file before writing to it.",
        )
    return entry


def _ensure_not_stale(
    path: str,
    *,
    current_text: str,
    read_state: dict[str, FileReadStateEntry],
) -> None:
    entry = _require_full_read(path, read_state)
    if _normalize_newlines(current_text) != entry.content:
        raise FileToolError(
            "File has been modified since it was read. Read it again before writing to it.",
        )


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _detect_line_ending(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text:
        return "\r"
    return "\n"


def _restore_newlines(text: str, line_ending: str) -> str:
    if line_ending == "\n":
        return text
    return text.replace("\n", line_ending)


def _note_read_call(
    tracker: FileReadTracker | None,
    key: tuple[str, int, int | None],
) -> int:
    if tracker is None:
        return 1
    if tracker.last_key == key:
        tracker.consecutive += 1
    else:
        tracker.last_key = key
        tracker.consecutive = 1
    return tracker.consecutive


def note_non_read_tool_call(tracker: FileReadTracker | None) -> None:
    if tracker is None:
        return
    tracker.last_key = None
    tracker.consecutive = 0


def build_file_tool_recovery_nudge(tool_name: str, message: str) -> str:
    """Return a deterministic recovery reminder for common file-tool guards."""
    lowered = " ".join(str(message or "").lower().split())
    if not lowered:
        return ""
    if "file has not been read yet" in lowered:
        return (
            f"`{tool_name}` was refused because the file has not been fully read in this chat yet. "
            "Recover by calling `read_file` on the exact file path first, then retry the native file tool. "
            "Do not fall back to bash file mutation for this."
        )
    if "file was only read partially" in lowered:
        return (
            f"`{tool_name}` was refused because the file was only read partially. "
            "Recover by calling `read_file` on the FULL file with no offset/limit, then retry the native file tool. "
            "Do not use `sed`, `awk`, heredocs, or shell redirection to bypass the read-before-write guard."
        )
    if "modified since it was read" in lowered:
        return (
            f"`{tool_name}` was refused because the file changed after the last full read. "
            "Recover by calling `read_file` again on the current file contents, then re-apply the change with the native file tool."
        )
    if "matched" in lowered and "locations" in lowered:
        return (
            f"`{tool_name}` found an ambiguous target. "
            "Recover by reading the current file, then retry with a unique `old_string` or set `replace_all=true` only if replacing every occurrence is truly intended."
        )
    if "old_string was not found" in lowered:
        return (
            f"`{tool_name}` could not find the requested target in the current file. "
            "Recover by reading the latest file contents and basing the next exact edit on text that actually exists."
        )
    return ""


def _format_read_output(
    path: str,
    lines: list[str],
    *,
    total_lines: int,
    start_line: int,
    partial: bool,
) -> str:
    if total_lines > _MAX_INLINE_LINES and not partial and start_line == 1:
        raise FileToolError(
            f"File has {total_lines} lines. Use offset/limit to read specific regions instead of the whole file.",
        )
    if total_lines == 0:
        body = "[empty file]"
        range_label = "lines 0-0 of 0"
    elif not lines:
        body = "[requested range is past end of file]"
        range_label = f"lines {start_line}-{start_line - 1} of {total_lines}"
    else:
        first_line = start_line
        last_line = start_line + len(lines) - 1
        range_label = f"lines {first_line}-{last_line} of {total_lines}"
        width = max(2, len(str(total_lines)))
        body = "\n".join(
            f"{line_no:>{width}} | {line}"
            for line_no, line in enumerate(lines, start=first_line)
        )
    mode = "partial" if partial else "full"
    return f"File: {path}\nView: {mode} · {range_label}\n\n{body}"


def _replace_param(
    params: tuple[tuple[str, str], ...],
    name: str,
    value: str,
) -> tuple[tuple[str, str], ...]:
    if not params:
        return ((name, value),)
    replaced = False
    out: list[tuple[str, str]] = []
    for key, param_value in params:
        if key == name:
            out.append((key, value))
            replaced = True
        else:
            out.append((key, param_value))
    if not replaced:
        out.append((name, value))
    return tuple(out)
