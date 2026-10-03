"""Exact source-control change comparisons used when carrying decisions across base merges."""

from collections.abc import Callable
from typing import Final
from urllib.parse import quote

_COMPARE_FILE_LIMIT: Final = 300
SourceControlRead = Callable[[str], object]
ChangeFingerprint = tuple[tuple[str, str, str, str, str], ...]


def change_fingerprint(
    *, repository: str, base: str, head: str, read: SourceControlRead, include_blobs: bool = True
) -> ChangeFingerprint | None:
    """The pull request's change against `base`: from their merge base to `head`.

    Equal fingerprints mean equal resulting blobs and equal patches, so the change
    is byte-identical. A file the provider gives no patch for (binary, or too large)
    is not exact, and neither is a truncated file list. With include_blobs=False,
    compare text patches alone: a base merge can incorporate unrelated base edits
    in the same file while preserving the dependency patch. Patchless pure renames
    still need the exact blob.
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
        fingerprint.append(
            (
                _text(entry.get("filename")),
                _text(entry.get("previous_filename")),
                status,
                _text(entry.get("sha")) if include_blobs or not patch else "",
                patch if isinstance(patch, str) else "",
            )
        )
    return tuple(sorted(fingerprint))


def _object(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _path(repository: str) -> str:
    owner, name = repository.strip().split("/", 1)
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"
