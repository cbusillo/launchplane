"""Exact source-control change comparisons used when carrying decisions across base merges."""

from collections.abc import Callable
from dataclasses import dataclass
import re
from typing import Final
from urllib.parse import quote

_COMPARE_FILE_LIMIT: Final = 300
_HUNK_HEADER: Final = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$", re.MULTILINE)
SourceControlRead = Callable[[str], object]
ChangeFingerprint = tuple[tuple[str, str, str, str, str, str], ...]


@dataclass(frozen=True)
class SourceRevision:
    commit: str
    committed_at: str


def read_branch_revision(
    *, repository: str, branch: str, read: SourceControlRead
) -> SourceRevision | None:
    source = _object(read(f"/repos/{_path(repository)}/commits/{quote(branch, safe='')}"))
    commit = _text(source.get("sha"))
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        return None
    committer = _object(_object(source.get("commit")).get("committer"))
    return SourceRevision(commit=commit, committed_at=_text(committer.get("date")))


def revision_contains(
    *, repository: str, ancestor: str, head: str, read: SourceControlRead
) -> bool | None:
    if ancestor == head:
        return True
    comparison = _object(
        read(
            f"/repos/{_path(repository)}/compare/{quote(ancestor, safe='')}...{quote(head, safe='')}"
        )
    )
    status = comparison.get("status")
    if status in {"ahead", "identical"}:
        return True
    if status in {"behind", "diverged"}:
        return False
    return None


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
        filename = _filename(entry.get("filename"))
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
            text = _HUNK_HEADER.sub(lambda match: f"@@{match.group(5)}", text)
        fingerprint.append(
            (
                filename,
                _filename(entry.get("previous_filename")),
                status,
                blob,
                text,
                file_mode,
            )
        )
    return tuple(sorted(fingerprint))


def patch_positions_match_base_update(
    *, repository: str, base: str, accepted_head: str, head: str, read: SourceControlRead
) -> bool:
    """Prove matching hunks only moved by their base's line shifts, not to another block."""
    prefix = f"/repos/{_path(repository)}/compare/"
    accepted = _object(read(f"{prefix}{base}...{accepted_head.strip().lower()}"))
    updated = _object(read(f"{prefix}{base}...{head.strip().lower()}"))
    old_base = _text(_object(accepted.get("merge_base_commit")).get("sha")).lower()
    new_base = _text(_object(updated.get("merge_base_commit")).get("sha")).lower()
    if any(re.fullmatch(r"[0-9a-f]{40}", revision) is None for revision in (old_base, new_base)):
        return False
    before, after = _comparison_files(accepted), _comparison_files(updated)
    if before is None or after is None or before.keys() != after.keys():
        return False
    base_changes: dict[str, dict[str, object]] = {}
    if old_base != new_base:
        delta = _object(read(f"{prefix}{old_base}...{new_base}"))
        changes = _comparison_files(delta)
        if delta.get("status") not in {"ahead", "identical"} or changes is None:
            return False
        base_changes = changes
    for filename, entry in before.items():
        old_hunks, new_hunks = _patch_hunks(entry), _patch_hunks(after[filename])
        if old_hunks is None or new_hunks is None or len(old_hunks) != len(new_hunks):
            return False
        base_path = _filename(entry.get("previous_filename")) or filename
        change = base_changes.get(base_path)
        shifts = () if change is None else _patch_edits(change)
        if shifts is None or (change is not None and change.get("status") != "modified"):
            return False
        for old, new in zip(old_hunks, new_hunks):
            offset = 0
            start, count, new_start, new_count = old
            anchor = start + (not count)
            for edit_start, edit_count, added_count in shifts:
                if (edit_count and edit_start + edit_count <= anchor) or (
                    not edit_count and (edit_start < anchor or (count and edit_start == anchor))
                ):
                    offset += added_count - edit_count
                elif edit_start < anchor + max(count, 1):
                    return False
            if new != (start + offset, count, new_start + offset, new_count):
                return False
    return True


def _comparison_files(value: dict[str, object]) -> dict[str, dict[str, object]] | None:
    files = value.get("files")
    if not isinstance(files, list) or len(files) >= _COMPARE_FILE_LIMIT:
        return None
    entries: dict[str, dict[str, object]] = {}
    for item in files:
        entry = _object(item)
        filename = _filename(entry.get("filename"))
        if not filename or filename in entries:
            return None
        entries[filename] = entry
    return entries


def _patch_hunks(entry: dict[str, object]) -> tuple[tuple[int, int, int, int], ...] | None:
    patch = entry.get("patch")
    if entry.get("status") == "renamed" and entry.get("changes") == 0 and not patch:
        return ()
    if not isinstance(patch, str):
        return None
    hunks: list[tuple[int, int, int, int]] = []
    old_lines = new_lines = 0
    for line in patch.rstrip("\n").split("\n"):
        header = _HUNK_HEADER.fullmatch(line)
        if header:
            if hunks and (old_lines, new_lines) != (hunks[-1][1], hunks[-1][3]):
                return None
            hunks.append((int(header[1]), int(header[2] or 1), int(header[3]), int(header[4] or 1)))
            old_lines = new_lines = 0
        elif hunks and line.startswith((" ", "+", "-")):
            old_lines += line[0] in {" ", "-"}
            new_lines += line[0] in {" ", "+"}
        elif not (hunks and line.startswith("\\")):
            return None
    if not hunks or (old_lines, new_lines) != (hunks[-1][1], hunks[-1][3]):
        return None
    return tuple(hunks)


def _patch_edits(entry: dict[str, object]) -> tuple[tuple[int, int, int], ...] | None:
    """Actual base edits, excluding the context included around its diff hunks."""
    if _patch_hunks(entry) is None or not isinstance(entry.get("patch"), str):
        return None
    edits: list[list[int]] = []
    cursor = 0
    current: list[int] | None = None
    for line in str(entry["patch"]).rstrip("\n").split("\n"):
        header = _HUNK_HEADER.fullmatch(line)
        if header:
            cursor = int(header[1]) + (int(header[2] or 1) == 0)
            current = None
        elif line.startswith(" "):
            cursor += 1
            current = None
        elif line.startswith(("+", "-")):
            if current is None:
                current = [cursor, 0, 0]
                edits.append(current)
            if line.startswith("-"):
                current[1] += 1
                cursor += 1
            else:
                current[2] += 1
    return tuple((start, removed, added) for start, removed, added in edits)


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
            path = _filename(entry.get("path"))
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


def _filename(value: object) -> str:
    # Whitespace is legal in a path and is part of the feature's identity.
    return value if isinstance(value, str) else ""


def _path(repository: str) -> str:
    owner, name = repository.strip().split("/", 1)
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"
