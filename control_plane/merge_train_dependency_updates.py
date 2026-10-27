import json
import re
from collections.abc import Callable, Sequence
from typing import Literal

from control_plane.contracts.merge_train_policy import DependencyUpdateFormat, DependencyUpdateType

DependencyUpdateClass = Literal["patch_or_minor", "needs_review"]

_TRAILER = re.compile(r"^---\s*\nupdated-dependencies:\s*\n(?P<body>.*?)^[.]{3}\s*$", re.M | re.S)
_ENTRY_START = re.compile(r"^- dependency-name:\s*(?P<name>\S+)\s*$", re.M)
_ENTRY_FIELD = re.compile(r"^\s+(?P<key>[a-z-]+):\s*(?P<value>\S+)\s*$", re.M)
_VERSION = re.compile(
    r"^v?(?P<major>\d+)(?:[.](?P<minor>\d+))?(?:[.](?P<patch>\d+))?(?P<suffix>.*)$"
)


def classify_dependency_update(
    commit_messages: Sequence[str],
    *,
    metadata_format: DependencyUpdateFormat,
    allowed_update_types: Sequence[DependencyUpdateType],
) -> DependencyUpdateClass:
    read_updates = UPDATE_FORMAT_PARSERS.get(metadata_format)
    if not commit_messages or read_updates is None:
        return "needs_review"
    for message in commit_messages:
        updates = read_updates(message)
        if not updates or not all(
            _update_qualifies(update, allowed_update_types) for update in updates
        ):
            return "needs_review"
    return "patch_or_minor"


def _dependabot_updates(message: str) -> list[dict[str, str]]:
    trailer = _TRAILER.search(message)
    if trailer is None:
        return []
    updates = []
    for name, fields in _trailer_entries(trailer.group("body")):
        new = fields.get("dependency-version", "")
        old = _previous_version(message, name=name, new_version=new)
        declared = fields.get("update-type", "").removeprefix("version-update:semver-")
        updates.append(
            {
                "depName": name,
                "updateType": declared or _version_update_type(old, new),
                "currentVersion": old,
                "newVersion": new,
            }
        )
    return updates


def _renovate_updates(message: str) -> list[dict[str, str]]:
    trailers = re.findall(r"^Renovate-Updates: (.+)$", message, re.M)
    if len(trailers) != 1:
        return []
    try:
        updates = json.loads(trailers[0])
    except ValueError:
        return []
    if not isinstance(updates, list) or not updates:
        return []
    if any(
        not isinstance(update, dict)
        or any(value is not None and not isinstance(value, str) for value in update.values())
        for update in updates
    ):
        return []
    return [{key: value or "" for key, value in update.items()} for update in updates]


UPDATE_FORMAT_PARSERS: dict[DependencyUpdateFormat, Callable[[str], list[dict[str, str]]]] = {
    "dependabot_trailer": _dependabot_updates,
    "renovate_metadata": _renovate_updates,
}
_DIGEST = re.compile(r"(?:[0-9a-f]{40}|(?:sha256:)?[0-9a-f]{64})")


def _update_qualifies(
    update: dict[str, str], allowed_update_types: Sequence[DependencyUpdateType]
) -> bool:
    if not update.get("depName") or update.get("updateType") not in allowed_update_types:
        return False
    if update.get("updateType") == "digest":
        old_value, new_value = update.get("currentValue", ""), update.get("newValue", "")
        return bool(
            old_value
            and old_value == new_value
            and _DIGEST.fullmatch(update.get("currentDigest", ""))
            and _DIGEST.fullmatch(update.get("newDigest", ""))
        )
    old, new = update.get("currentVersion", ""), update.get("newVersion", "")
    return update.get("updateType") == _version_update_type(old, new)


def _version_update_type(old_version: str, new_version: str) -> str:
    old, new = _VERSION.match(old_version), _VERSION.match(new_version)
    if old is None or new is None or old.group("suffix") != new.group("suffix"):
        return ""
    if old.group("major") != new.group("major"):
        return ""
    if (old.group("minor") or "0") != (new.group("minor") or "0"):
        return "minor" if old.group("major") != "0" else ""
    return "patch"


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
        name = start.group("name")
        if len(name) >= 2 and name[0] in ("'", '"') and name[-1] == name[0]:
            name = name[1:-1]
        entries.append((name, fields))
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
