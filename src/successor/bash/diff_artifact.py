"""Structured diff artifacts for tool-card rendering.

Successor's renderer wants semantic rows, not raw patch text. This
module provides a small immutable shape for file diffs plus helpers to
parse unified diff text and to synthesize unified diffs from before/after
file contents.

The shape is deliberately narrow:

  - ChangeArtifact holds optional prelude lines plus one or more files
  - ChangedFile holds status/path metadata plus zero or more hunks
  - DiffHunk holds the exact unified diff lines for one hunk

This is enough for both:

  1. Explicit diff commands (`git diff`, `git show`, `diff -u`)
  2. Post-exec change capture for deterministic file mutations
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass


_HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_lines>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_lines>\d+))? @@"
)
_DIFF_GIT_RE = re.compile(r"^diff --git a/(?P<old>.+) b/(?P<new>.+)$")


@dataclass(frozen=True, slots=True)
class DiffHunk:
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    status: str = "modified"  # modified | added | deleted | renamed | binary | note
    old_path: str | None = None
    notes: tuple[str, ...] = ()
    hunks: tuple[DiffHunk, ...] = ()


@dataclass(frozen=True, slots=True)
class ChangeArtifact:
    prelude: tuple[str, ...] = ()
    files: tuple[ChangedFile, ...] = ()

    @property
    def has_diff_rows(self) -> bool:
        return bool(self.prelude or self.files)


@dataclass(slots=True)
class _ChangedFileBuilder:
    path: str
    status: str = "modified"
    old_path: str | None = None
    notes: list[str] | None = None
    hunks: list[DiffHunk] | None = None

    def __post_init__(self) -> None:
        if self.notes is None:
            self.notes = []
        if self.hunks is None:
            self.hunks = []

    def freeze(self) -> ChangedFile:
        return ChangedFile(
            path=self.path,
            status=self.status,
            old_path=self.old_path,
            notes=tuple(self.notes or ()),
            hunks=tuple(self.hunks or ()),
        )


def parse_unified_diff(text: str) -> ChangeArtifact | None:
    """Parse git-style or generic unified diff text.

    Returns None when the text does not look like a unified diff at all.
    """
    if not text.strip():
        return None

    lines = text.splitlines()
    prelude: list[str] = []
    files: list[ChangedFile] = []
    current: _ChangedFileBuilder | None = None
    current_hunk: dict | None = None
    pending_old_path: str | None = None
    saw_diff = False

    def flush_hunk() -> None:
        nonlocal current_hunk
        if current is None or current_hunk is None:
            current_hunk = None
            return
        current.hunks.append(DiffHunk(
            old_start=current_hunk["old_start"],
            old_lines=current_hunk["old_lines"],
            new_start=current_hunk["new_start"],
            new_lines=current_hunk["new_lines"],
            lines=tuple(current_hunk["lines"]),
        ))
        current_hunk = None

    def flush_file() -> None:
        nonlocal current
        flush_hunk()
        if current is None:
            return
        files.append(current.freeze())
        current = None

    for line in lines:
        m = _DIFF_GIT_RE.match(line)
        if m:
            flush_file()
            current = _ChangedFileBuilder(
                path=m.group("new"),
                old_path=m.group("old"),
                status="modified",
            )
            pending_old_path = m.group("old")
            saw_diff = True
            continue

        if line.startswith("rename from "):
            if current is None:
                current = _ChangedFileBuilder(path=line[len("rename from "):], status="renamed")
            current.old_path = line[len("rename from "):]
            current.status = "renamed"
            saw_diff = True
            continue

        if line.startswith("rename to "):
            new_path = line[len("rename to "):]
            if current is None:
                current = _ChangedFileBuilder(path=new_path, status="renamed")
            current.path = new_path
            current.status = "renamed"
            saw_diff = True
            continue

        if line.startswith("new file"):
            if current is None:
                current = _ChangedFileBuilder(path="(new file)", status="added")
            current.status = "added"
            saw_diff = True
            continue

        if line.startswith("deleted file"):
            if current is None:
                current = _ChangedFileBuilder(path="(deleted file)", status="deleted")
            current.status = "deleted"
            saw_diff = True
            continue

        if line.startswith("Binary files "):
            if current is None:
                current = _ChangedFileBuilder(path="(binary file)", status="binary")
            current.status = "binary"
            current.notes.append(line)
            saw_diff = True
            continue

        if line.startswith("--- "):
            flush_hunk()
            pending_old_path = _normalize_patch_path(line[4:])
            if current is None:
                current = _ChangedFileBuilder(
                    path=_normalize_patch_path(line[4:]),
                    old_path=pending_old_path,
                )
            saw_diff = True
            continue

        if line.startswith("+++ "):
            new_path = _normalize_patch_path(line[4:])
            if current is None:
                current = _ChangedFileBuilder(
                    path=new_path,
                    old_path=pending_old_path,
                )
            else:
                if current.old_path is None:
                    current.old_path = pending_old_path
                current.path = new_path
            if current.old_path == "/dev/null":
                current.status = "added"
            if current.path == "/dev/null":
                current.status = "deleted"
            if current.path == "/dev/null" and current.old_path:
                current.path = current.old_path
            saw_diff = True
            continue

        hunk_match = _HUNK_RE.match(line)
        if hunk_match:
            if current is None:
                current = _ChangedFileBuilder(path="(patch)")
            flush_hunk()
            current_hunk = {
                "old_start": int(hunk_match.group("old_start")),
                "old_lines": int(hunk_match.group("old_lines") or "1"),
                "new_start": int(hunk_match.group("new_start")),
                "new_lines": int(hunk_match.group("new_lines") or "1"),
                "lines": [line],
            }
            saw_diff = True
            continue

        if current_hunk is not None:
            if (
                line.startswith((" ", "+", "-"))
                or line == ""
                or line == r"\ No newline at end of file"
            ):
                current_hunk["lines"].append(line)
                continue

        if current is not None:
            current.notes.append(line)
            continue

        prelude.append(line)

    flush_file()
    if not saw_diff:
        return None
    return ChangeArtifact(prelude=tuple(prelude), files=tuple(files))


def build_change_artifact_from_text(
    path: str,
    before: str | None,
    after: str | None,
    *,
    status: str | None = None,
    old_path: str | None = None,
) -> ChangeArtifact | None:
    """Build a ChangeArtifact from before/after file contents."""
    if before == after:
        return None

    effective_status = status or _status_for_transition(before, after)
    if before is None and after == "":
        return ChangeArtifact(files=(
            ChangedFile(
                path=path,
                status="added",
                notes=("created empty file",),
            ),
        ))
    if before == "" and after is None:
        return ChangeArtifact(files=(
            ChangedFile(
                path=path,
                status="deleted",
                notes=("deleted empty file",),
            ),
        ))

    fromfile = old_path or path
    tofile = path
    old_lines = [] if before is None else before.splitlines()
    new_lines = [] if after is None else after.splitlines()
    patch_lines = list(difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=_display_patch_path(fromfile, side="old", exists=before is not None),
        tofile=_display_patch_path(tofile, side="new", exists=after is not None),
        n=3,
        lineterm="",
    ))
    patch = "\n".join(patch_lines)
    artifact = parse_unified_diff(patch)
    if artifact is None or not artifact.files:
        return ChangeArtifact(files=(
            ChangedFile(
                path=path,
                status=effective_status,
                old_path=old_path,
                notes=("file changed",),
            ),
        ))

    files = list(artifact.files)
    first = files[0]
    files[0] = ChangedFile(
        path=path,
        status=effective_status,
        old_path=old_path,
        notes=first.notes,
        hunks=first.hunks,
    )
    return ChangeArtifact(prelude=artifact.prelude, files=tuple(files))


def note_artifact(
    path: str,
    note: str,
    *,
    status: str = "note",
    old_path: str | None = None,
) -> ChangeArtifact:
    return ChangeArtifact(files=(
        ChangedFile(
            path=path,
            status=status,
            old_path=old_path,
            notes=(note,),
        ),
    ))


def _status_for_transition(before: str | None, after: str | None) -> str:
    if before is None and after is not None:
        return "added"
    if before is not None and after is None:
        return "deleted"
    return "modified"


def _normalize_patch_path(raw: str) -> str:
    text = raw.strip()
    if "\t" in text:
        text = text.split("\t", 1)[0]
    if text.startswith("a/") or text.startswith("b/"):
        return text[2:]
    return text


def _display_patch_path(path: str, *, side: str, exists: bool) -> str:
    if not exists:
        return "/dev/null"
    if path.startswith(("a/", "b/")):
        return path
    prefix = "a" if side == "old" else "b"
    return f"{prefix}/{path}"
