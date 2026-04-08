"""Deterministic post-exec change capture for mutating bash cards.

The bash parser already knows a surprising amount before execution:
`write-file`, `delete-file`, `copy-files`, `move-files`, and similar
cards expose target paths in structured params. This module turns that
preview metadata into a narrow before/after capture plan so settled tool
cards can show user-facing diffs without replacing the real stdout/stderr
that the model sees in tool-result messages.

Only deterministic target shapes are supported here. Opaque mutations
(`python -c`, arbitrary shell scripts, package managers) still render as
ordinary tool cards unless the command itself emits a diff.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .cards import ToolCard
from .diff_artifact import (
    ChangeArtifact,
    build_change_artifact_from_text,
    note_artifact,
)


MAX_CAPTURE_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    display_path: str
    abs_path: str
    exists: bool
    is_dir: bool = False
    is_symlink: bool = False
    text: str | None = None
    binary: bool = False
    too_large: bool = False
    unreadable: bool = False


@dataclass(frozen=True, slots=True)
class ChangeCapture:
    verb: str
    cwd: str
    primary_before: FileSnapshot | None = None
    secondary_before: FileSnapshot | None = None
    primary_display: str | None = None
    secondary_display: str | None = None


def begin_change_capture(card: ToolCard, *, cwd: str | None) -> ChangeCapture | None:
    """Capture before-state for deterministic mutating commands."""
    resolved_cwd = os.path.abspath(cwd or os.getcwd())

    if card.verb in ("write-file", "create-file", "delete-file", "delete-tree", "create-directory"):
        raw_path = _single_path_param(card, "path")
        if raw_path is None:
            return None
        return ChangeCapture(
            verb=card.verb,
            cwd=resolved_cwd,
            primary_before=_snapshot(raw_path, cwd=resolved_cwd),
            primary_display=raw_path,
        )

    if card.verb == "copy-files":
        source = _single_path_param(card, "source")
        dest = _single_path_param(card, "destination")
        if source is None or dest is None:
            return None
        return ChangeCapture(
            verb=card.verb,
            cwd=resolved_cwd,
            primary_before=_snapshot(dest, cwd=resolved_cwd),
            primary_display=dest,
            secondary_display=source,
        )

    if card.verb == "move-files":
        source = _single_path_param(card, "source")
        dest = _single_path_param(card, "destination")
        if source is None or dest is None:
            return None
        return ChangeCapture(
            verb=card.verb,
            cwd=resolved_cwd,
            primary_before=_snapshot(source, cwd=resolved_cwd),
            secondary_before=_snapshot(dest, cwd=resolved_cwd),
            primary_display=source,
            secondary_display=dest,
        )

    return None


def finalize_change_capture(capture: ChangeCapture | None) -> ChangeArtifact | None:
    """Compute a user-facing change artifact from a before/after capture."""
    if capture is None:
        return None

    if capture.verb in ("write-file", "create-file", "copy-files"):
        if capture.primary_display is None or capture.primary_before is None:
            return None
        after = _snapshot(capture.primary_display, cwd=capture.cwd)
        return _artifact_for_transition(
            capture.primary_display,
            capture.primary_before,
            after,
        )

    if capture.verb == "delete-file":
        if capture.primary_display is None or capture.primary_before is None:
            return None
        after = _snapshot(capture.primary_display, cwd=capture.cwd)
        return _artifact_for_transition(
            capture.primary_display,
            capture.primary_before,
            after,
        )

    if capture.verb == "delete-tree":
        if capture.primary_display is None:
            return None
        after = _snapshot(capture.primary_display, cwd=capture.cwd)
        if after.exists:
            return None
        return note_artifact(
            capture.primary_display,
            "deleted directory tree",
            status="deleted",
        )

    if capture.verb == "create-directory":
        if capture.primary_display is None:
            return None
        after = _snapshot(capture.primary_display, cwd=capture.cwd)
        if not after.exists or not after.is_dir:
            return None
        return note_artifact(
            capture.primary_display,
            "created directory",
            status="added",
        )

    if capture.verb == "move-files":
        if (
            capture.primary_before is None
            or capture.secondary_before is None
            or capture.primary_display is None
            or capture.secondary_display is None
        ):
            return None
        src_after = _snapshot(capture.primary_display, cwd=capture.cwd)
        dest_after = _snapshot(capture.secondary_display, cwd=capture.cwd)

        if not src_after.exists and dest_after.exists:
            if (
                capture.primary_before.text is not None
                and dest_after.text is not None
                and capture.primary_before.text == dest_after.text
                and not capture.secondary_before.exists
            ):
                return note_artifact(
                    capture.secondary_display,
                    f"renamed from {capture.primary_display}",
                    status="renamed",
                    old_path=capture.primary_display,
                )
            if capture.secondary_before.text is not None or dest_after.text is not None:
                return _artifact_for_transition(
                    capture.secondary_display,
                    capture.secondary_before,
                    dest_after,
                )
            return note_artifact(
                capture.secondary_display,
                f"moved from {capture.primary_display}",
                status="renamed",
                old_path=capture.primary_display,
            )
        return None

    return None


def _artifact_for_transition(
    display_path: str,
    before: FileSnapshot,
    after: FileSnapshot,
) -> ChangeArtifact | None:
    if before.exists == after.exists and before.text == after.text:
        return None

    if before.is_dir or after.is_dir:
        if not before.exists and after.exists:
            return note_artifact(display_path, "created directory", status="added")
        if before.exists and not after.exists:
            return note_artifact(display_path, "deleted directory", status="deleted")
        return note_artifact(display_path, "directory changed")

    if before.binary or after.binary:
        if not before.exists and after.exists:
            return note_artifact(display_path, "created binary file", status="added")
        if before.exists and not after.exists:
            return note_artifact(display_path, "deleted binary file", status="deleted")
        return note_artifact(display_path, "binary file changed", status="binary")

    if before.too_large or after.too_large:
        if not before.exists and after.exists:
            return note_artifact(display_path, "created large file", status="added")
        if before.exists and not after.exists:
            return note_artifact(display_path, "deleted large file", status="deleted")
        return note_artifact(display_path, "large file changed")

    if before.unreadable or after.unreadable:
        return note_artifact(display_path, "file changed (unreadable)")

    return build_change_artifact_from_text(
        display_path,
        before.text if before.exists else None,
        after.text if after.exists else None,
    )


def _single_path_param(card: ToolCard, key: str) -> str | None:
    for param_key, value in card.params:
        if param_key != key:
            continue
        text = str(value).strip()
        if not text or text == "(missing)" or ", " in text:
            return None
        return text
    return None


def _resolve_path(raw_path: str, *, cwd: str) -> str:
    expanded = os.path.expandvars(os.path.expanduser(raw_path))
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(cwd, expanded))


def _snapshot(raw_path: str, *, cwd: str) -> FileSnapshot:
    abs_path = _resolve_path(raw_path, cwd=cwd)
    if not os.path.lexists(abs_path):
        return FileSnapshot(display_path=raw_path, abs_path=abs_path, exists=False)

    is_symlink = os.path.islink(abs_path)
    if os.path.isdir(abs_path):
        return FileSnapshot(
            display_path=raw_path,
            abs_path=abs_path,
            exists=True,
            is_dir=True,
            is_symlink=is_symlink,
        )

    try:
        size = os.path.getsize(abs_path)
    except OSError:
        return FileSnapshot(
            display_path=raw_path,
            abs_path=abs_path,
            exists=True,
            unreadable=True,
            is_symlink=is_symlink,
        )

    if size > MAX_CAPTURE_BYTES:
        return FileSnapshot(
            display_path=raw_path,
            abs_path=abs_path,
            exists=True,
            too_large=True,
            is_symlink=is_symlink,
        )

    try:
        with open(abs_path, "rb") as fh:
            payload = fh.read(MAX_CAPTURE_BYTES + 1)
    except OSError:
        return FileSnapshot(
            display_path=raw_path,
            abs_path=abs_path,
            exists=True,
            unreadable=True,
            is_symlink=is_symlink,
        )

    if b"\x00" in payload:
        return FileSnapshot(
            display_path=raw_path,
            abs_path=abs_path,
            exists=True,
            binary=True,
            is_symlink=is_symlink,
        )

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return FileSnapshot(
            display_path=raw_path,
            abs_path=abs_path,
            exists=True,
            binary=True,
            is_symlink=is_symlink,
        )

    return FileSnapshot(
        display_path=raw_path,
        abs_path=abs_path,
        exists=True,
        text=text,
        is_symlink=is_symlink,
    )
