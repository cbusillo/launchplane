"""Classify a dependency-update pull request from its commit messages.

A pull request qualifies for label-free merge-train admission only when every
updated dependency moves within one major version. Anything the messages do not
prove, including a missing or unparseable version, is left for agent review.
"""

import re
from collections.abc import Sequence
from typing import Literal

DependencyUpdateClass = Literal["patch_or_minor", "needs_review"]

_TRAILER = re.compile(r"^---\s*\nupdated-dependencies:\s*\n(?P<body>.*?)^\.\.\.\s*$", re.M | re.S)
_ENTRY_START = re.compile(r"^- dependency-name:\s*(?P<name>\S+)\s*$", re.M)
_ENTRY_FIELD = re.compile(r"^\s+(?P<key>[a-z-]+):\s*(?P<value>\S+)\s*$", re.M)
_VERSION = re.compile(r"^v?(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:\.(?P<patch>\d+))?(?P<suffix>.*)$")


def classify_dependency_update(commit_messages: Sequence[str]) -> DependencyUpdateClass:
    """Return patch_or_minor only when every commit proves a within-major update."""
    if not commit_messages:
        return "needs_review"
    for message in commit_messages:
        if not _message_is_patch_or_minor(message):
            return "needs_review"
    return "patch_or_minor"


def _message_is_patch_or_minor(message: str) -> bool:
    trailer = _TRAILER.search(message)
    if trailer is None:
        return False
    entries = _trailer_entries(trailer.group("body"))
    if not entries:
        return False
    for name, fields in entries:
        if fields.get("update-type") == "version-update:semver-major":
            return False
        new_version = fields.get("dependency-version", "")
        old_version = _previous_version(message, name=name, new_version=new_version)
        if not old_version or not _within_major(old_version, new_version):
            return False
    return True


def _trailer_entries(body: str) -> list[tuple[str, dict[str, str]]]:
    starts = list(_ENTRY_START.finditer(body))
    entries: list[tuple[str, dict[str, str]]] = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        # Dependabot quotes YAML scalars that would otherwise parse as numbers ('2025.2').
        fields = {
            match.group("key"): match.group("value").strip("'\"")
            for match in _ENTRY_FIELD.finditer(body, start.end(), end)
        }
        entries.append((start.group("name"), fields))
    return entries


def _previous_version(message: str, *, name: str, new_version: str) -> str:
    if not new_version:
        return ""
    escaped_name = re.escape(name)
    escaped_new = re.escape(new_version)
    patterns = (
        rf"Updates `{escaped_name}` from (?P<old>\S+) to {escaped_new}(?:\s|$)",
        rf"Bumps \[{escaped_name}\]\([^)]*\) from (?P<old>\S+) to {escaped_new}\.?(?:\s|$)",
        rf"Bumps {escaped_name} from (?P<old>\S+) to {escaped_new}\.?(?:\s|$)",
    )
    found = {match.group("old") for pattern in patterns for match in re.finditer(pattern, message)}
    return found.pop() if len(found) == 1 else ""


def _within_major(old_version: str, new_version: str) -> bool:
    old = _VERSION.match(old_version)
    new = _VERSION.match(new_version)
    if old is None or new is None or old.group("suffix") != new.group("suffix"):
        return False
    if old.group("major") != new.group("major"):
        return False
    # Before 1.0, a minor bump is a breaking change under semver.
    if old.group("major") == "0" and (old.group("minor") or "0") != (new.group("minor") or "0"):
        return False
    return True
