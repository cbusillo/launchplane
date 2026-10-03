"""Exact source-control change comparisons used when carrying decisions across base merges."""

from collections.abc import Callable
from typing import Final
from urllib.parse import quote

_COMPARE_FILE_LIMIT: Final = 300
SourceControlRead = Callable[[str], object]
ChangeFingerprint = tuple[tuple[str, str, str, str, str], ...]


def change_fingerprint(
    *,
    repository: str,
    base: str,
    head: str,
    read: SourceControlRead,
    include_blobs: bool = True,
    changed_lines_only: bool = False,
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
    equal.
    """

    comparison = _object(
        read(f"/repos/{_path(repository)}/compare/{base}...{head.strip().lower()}")
    )
    files = comparison.get("files")
    if not isinstance(files, list) or len(files) >= _COMPARE_FILE_LIMIT:
        return None
    fingerprint: list[tuple[str, str, str, str, str]] = []
    for item in files:
        entry = _object(item)
        status = _text(entry.get("status"))
        patch = entry.get("patch")
        pure_rename = status == "renamed" and entry.get("changes") == 0
        if not isinstance(patch, str) and not pure_rename:
            return None
        text = patch if isinstance(patch, str) else ""
        blob = _text(entry.get("sha")) if include_blobs or not text else ""
        if changed_lines_only:
            blob, text = "", _changed_lines(text)
        fingerprint.append(
            (
                _text(entry.get("filename")),
                _text(entry.get("previous_filename")),
                status,
                blob,
                text,
            )
        )
    return tuple(sorted(fingerprint))


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
