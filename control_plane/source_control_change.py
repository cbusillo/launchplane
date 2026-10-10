"""Exact source-control change comparisons used when carrying decisions across base merges."""

from collections.abc import Callable
import re
from typing import Final
from urllib.parse import quote

_COMPARE_FILE_LIMIT: Final = 300
SourceControlRead = Callable[[str], object]
ChangeFingerprint = tuple[tuple[str, str, str, str, str, str], ...]


def change_fingerprint(
    *,
    repository: str,
    base: str,
    head: str,
    read: SourceControlRead,
    include_blobs: bool = True,
    changed_lines_only: bool = False,
    ignore_hunk_positions: bool = False,
    include_file_modes: bool = False,
) -> ChangeFingerprint | None:
    """The pull request's change against `base`: from their merge base to `head`.

    Equal fingerprints mean equal resulting blobs and equal patches, so the change
    is byte-identical. A file the provider gives no patch for (binary, or too large)
    is not exact, and neither is a truncated file list. With include_blobs=False,
    compare text patches alone: a base merge can incorporate unrelated base edits
    in the same file while preserving the dependency patch. Patchless pure renames
    still need the exact blob. With changed_lines_only=True, compare each file's
    name, status, and the lines the change adds and removes, in order, without
    hunk positions, context lines, or blobs (a pure rename compares by name), so
    a base edit that only moves or surrounds the change in the same file leaves it
    equal. With ignore_hunk_positions=True, ignore only hunk line numbers; retain
    context, hunk boundaries and section names so relocating a change during a
    rebase cannot carry merely because its added and removed lines are the same.
    With include_file_modes=True, require a complete head tree and compare each
    feature file's mode too; an executable-bit or symlink change is a new patch.
    """

    comparison = _object(
        read(f"/repos/{_path(repository)}/compare/{base}...{head.strip().lower()}")
    )
    files = comparison.get("files")
    if not isinstance(files, list) or len(files) >= _COMPARE_FILE_LIMIT:
        return None
    file_modes = (
        _head_file_modes(repository=repository, head=head, read=read) if include_file_modes else {}
    )
    if file_modes is None:
        return None
    fingerprint: list[tuple[str, str, str, str, str, str]] = []
    for item in files:
        entry = _object(item)
        status = _text(entry.get("status"))
        filename = _text(entry.get("filename"))
        file_mode = ""
        if include_file_modes:
            if status == "removed":
                if filename in file_modes:
                    return None
                file_mode = "removed"
            else:
                file_mode = file_modes.get(filename, "")
                if file_mode not in {"100644", "100755", "120000"}:
                    return None
        patch = entry.get("patch")
        pure_rename = status == "renamed" and entry.get("changes") == 0
        if not isinstance(patch, str) and not pure_rename:
            return None
        text = patch if isinstance(patch, str) else ""
        blob = _text(entry.get("sha")) if include_blobs or not text else ""
        if changed_lines_only:
            blob, text = "", _changed_lines(text)
        elif ignore_hunk_positions:
            text = re.sub(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", "@@", text, flags=re.MULTILINE)
        fingerprint.append(
            (
                filename,
                _text(entry.get("previous_filename")),
                status,
                blob,
                text,
                file_mode,
            )
        )
    return tuple(sorted(fingerprint))


def _head_file_modes(
    *, repository: str, head: str, read: SourceControlRead
) -> dict[str, str] | None:
    tree = _object(
        read(
            f"/repos/{_path(repository)}/git/trees/{quote(head.strip().lower(), safe='')}?recursive=1"
        )
    )
    entries = tree.get("tree")
    if tree.get("truncated") is not False or not isinstance(entries, list):
        return None
    modes: dict[str, str] = {}
    for item in entries:
        entry = _object(item)
        if entry.get("type") == "blob":
            path = _text(entry.get("path"))
            if path in modes:
                return None
            modes[path] = _text(entry.get("mode"))
    return modes


def _changed_lines(patch: str) -> str:
    """The added and removed lines of a unified-diff patch, in order.

    Hunk headers and context lines are dropped. A "no newline at end of file"
    marker is kept only after an added or removed line, where it is part of that
    line's content.
    """

    lines: list[str] = []
    after_change = False
    for line in patch.split("\n"):
        changed = line.startswith(("+", "-"))
        if changed or (after_change and line.startswith("\\")):
            lines.append(line)
        after_change = changed
    return "\n".join(lines)


def _object(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _path(repository: str) -> str:
    owner, name = repository.strip().split("/", 1)
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"
