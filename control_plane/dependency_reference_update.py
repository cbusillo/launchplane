from collections.abc import Callable, Sequence
from pathlib import PurePosixPath
import re

import yaml

from control_plane.merge_train_dependency_updates import dependency_version_update_type


def dependency_reference_file(filename: str) -> bool:
    file = PurePosixPath(filename)
    return (
        file.name == "Dockerfile"
        or file.name.endswith(".Dockerfile")
        or (
            file.suffix in {".yml", ".yaml"}
            and (
                file.parent == PurePosixPath(".github/workflows")
                or (filename.startswith(".github/actions/") and file.stem == "action")
                or file.name.startswith(("compose.", "docker-compose."))
            )
        )
    )


def dependency_references_only(
    filename: str,
    old: str,
    new: str,
    *,
    read: Callable[[str], object],
    declared_updates: Sequence[dict[str, str]],
) -> bool:
    if not dependency_reference_file(filename) or old == new:
        return False
    file = PurePosixPath(filename)
    if file.name == "Dockerfile" or file.name.endswith(".Dockerfile"):
        return _docker_references_only(old, new, declared_updates)
    try:
        changes = _yaml_changes(yaml.compose(old), yaml.compose(new))
    except (yaml.YAMLError, RecursionError, ValueError):
        return False
    return bool(changes) and all(
        _yaml_reference_change(
            filename, path, before, after, read=read, declared_updates=declared_updates
        )
        for path, before, after in changes
    )


def _yaml_changes(
    old: yaml.Node | None, new: yaml.Node | None, path: tuple[str | int, ...] = ()
) -> list[tuple[tuple[str | int, ...], str, str]]:
    if (
        len(path) > 32
        or type(old) is not type(new)
        or old is None
        or new is None
        or old.tag != new.tag
    ):
        raise ValueError("dependency structure changed")
    if isinstance(old, yaml.MappingNode) and isinstance(new, yaml.MappingNode):
        before, after = _yaml_mapping(old), _yaml_mapping(new)
        if before.keys() != after.keys():
            raise ValueError("dependency mapping changed")
        return [
            change
            for key in before
            for change in _yaml_changes(before[key], after[key], (*path, key))
        ]
    if isinstance(old, yaml.SequenceNode) and isinstance(new, yaml.SequenceNode):
        if len(old.value) != len(new.value):
            raise ValueError("dependency sequence changed")
        return [
            change
            for index, before in enumerate(old.value)
            for change in _yaml_changes(before, new.value[index], (*path, index))
        ]
    if not isinstance(old, yaml.ScalarNode) or not isinstance(new, yaml.ScalarNode):
        raise ValueError("unsupported dependency node")
    return [] if old.value == new.value else [(path, old.value, new.value)]


def _yaml_mapping(node: yaml.MappingNode) -> dict[str, yaml.Node]:
    result: dict[str, yaml.Node] = {}
    for key, value in node.value:
        if not isinstance(key, yaml.ScalarNode) or key.value in result or key.value == "<<":
            raise ValueError("ambiguous dependency mapping")
        result[key.value] = value
    return result


def _yaml_reference_change(
    filename: str,
    path: tuple[str | int, ...],
    old: str,
    new: str,
    *,
    read: Callable[[str], object],
    declared_updates: Sequence[dict[str, str]],
) -> bool:
    if not path:
        return False
    workflow = filename.startswith(".github/")
    if (
        workflow
        and path[-1] == "uses"
        and (
            (len(path) == 3 and path[0] == "jobs")
            or (len(path) == 5 and path[0] == "jobs" and path[2] == "steps")
            or (len(path) == 4 and path[:2] == ("runs", "steps"))
        )
    ):
        return _action_reference_forward(old, new, read=read, declared_updates=declared_updates)
    if (
        workflow
        and len(path) >= 5
        and path[-2] == "with"
        and path[-1]
        in {"python-version", "node-version", "go-version", "ruby-version", "java-version"}
        and "steps" in path
    ):
        return _version_forward(old, new) and any(
            update.get("depName") == str(path[-1]).removesuffix("-version")
            and _declared_values(update) == (old, new)
            for update in declared_updates
        )
    if path[-1] == "image" and (
        (not workflow and len(path) == 3 and path[0] == "services")
        or (workflow and len(path) == 5 and path[0] == "jobs" and path[2] == "services")
        or (workflow and len(path) == 4 and path[0] == "jobs" and path[2] == "container")
    ):
        return _image_reference_forward(old, new, declared_updates)
    if workflow and len(path) == 3 and path[0] == "jobs" and path[2] == "container":
        return _image_reference_forward(old, new, declared_updates)
    return False


def _version_forward(old: str, new: str) -> bool:
    return dependency_version_update_type(old, new) in {"patch", "minor"}


def _declared_values(update: dict[str, str]) -> tuple[str, str]:
    return (
        update.get("currentValue") or update.get("currentVersion", ""),
        update.get("newValue") or update.get("newVersion", ""),
    )


def _image_reference_forward(
    old: str, new: str, declared_updates: Sequence[dict[str, str]]
) -> bool:
    pattern = (
        r"(?P<name>[a-zA-Z0-9][a-zA-Z0-9./:_-]*):(?P<version>[^:@\s]+)(?:@sha256:[0-9a-f]{64})?"
    )
    before, after = re.fullmatch(pattern, old), re.fullmatch(pattern, new)
    return bool(
        before is not None
        and after is not None
        and before["name"] == after["name"]
        and _version_forward(before["version"], after["version"])
        and any(
            update.get("depName") == before["name"]
            and _declared_values(update) == (before["version"], after["version"])
            and all(
                reference
                == f"{before['name']}:{value}"
                + (f"@{update[digest_key]}" if update.get(digest_key) else "")
                for reference, value, digest_key in (
                    (old, before["version"], "currentDigest"),
                    (new, after["version"], "newDigest"),
                )
            )
            for update in declared_updates
        )
    )


def _action_reference_forward(
    old: str,
    new: str,
    *,
    read: Callable[[str], object],
    declared_updates: Sequence[dict[str, str]],
) -> bool:
    pattern = r"(?P<name>[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_./-]+)?)@(?P<ref>[^\s@]+)"
    before, after = re.fullmatch(pattern, old), re.fullmatch(pattern, new)
    if before is None or after is None or before["name"] != after["name"]:
        return False
    if any(part in {".", ".."} for part in before["name"].split("/")):
        return False
    old_ref, new_ref = before["ref"], after["ref"]
    matching_updates = [
        update
        for update in declared_updates
        if update.get("depName") == before["name"]
        and (
            (update.get("currentDigest"), update.get("newDigest")) == (old_ref, new_ref)
            or _declared_values(update) == (old_ref, new_ref)
        )
    ]
    if not matching_updates:
        return False
    if _version_forward(old_ref, new_ref):
        return True
    if not all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in (old_ref, new_ref)):
        return False
    if not any(
        re.fullmatch(r"v?\d+(?:[.]\d+)*(?:-[a-zA-Z0-9.-]+)?", _declared_values(update)[0])
        for update in matching_updates
    ):
        return False
    repository = "/".join(before["name"].split("/")[:2])
    comparison = read(f"/repos/{repository}/compare/{old_ref}...{new_ref}")
    if not isinstance(comparison, dict):
        return False
    base, merge_base, commits = (
        comparison.get("base_commit"),
        comparison.get("merge_base_commit"),
        comparison.get("commits"),
    )
    return bool(
        comparison.get("status") == "ahead"
        and isinstance(base, dict)
        and base.get("sha") == old_ref
        and isinstance(merge_base, dict)
        and merge_base.get("sha") == old_ref
        and isinstance(commits, list)
        and commits
        and isinstance(commits[-1], dict)
        and commits[-1].get("sha") == new_ref
    )


def _docker_references_only(old: str, new: str, declared_updates: Sequence[dict[str, str]]) -> bool:
    before, after = old.splitlines(), new.splitlines()
    if len(before) != len(after):
        return False
    pattern = r"(?P<prefix>#\s*syntax=|FROM(?:\s+--platform=\S+)?\s+)(?P<image>\S+)(?P<suffix>(?:\s+AS\s+\S+)?\s*)"
    changed = False
    for old_line, new_line in zip(before, after, strict=True):
        if old_line == new_line:
            continue
        old_match = re.fullmatch(pattern, old_line, re.I)
        new_match = re.fullmatch(pattern, new_line, re.I)
        if (
            old_match is None
            or new_match is None
            or old_match["prefix"] != new_match["prefix"]
            or old_match["suffix"] != new_match["suffix"]
            or not _image_reference_forward(
                old_match["image"], new_match["image"], declared_updates
            )
        ):
            return False
        changed = True
    return changed
