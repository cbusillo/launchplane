from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

import click

RELEASE_TUPLES_FILE_ENV_VAR = "ODOO_CONTROL_PLANE_RELEASE_TUPLES_FILE"
DEFAULT_RELEASE_TUPLES_FILE = "config/release-tuples.toml"
GIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")


@dataclass(frozen=True)
class ReleaseTupleDefinition:
    tuple_id: str
    repo_shas: dict[str, str]


@dataclass(frozen=True)
class ReleaseTupleContextDefinition:
    channels: dict[str, ReleaseTupleDefinition]


@dataclass(frozen=True)
class ReleaseTupleCatalog:
    schema_version: int
    contexts: dict[str, ReleaseTupleContextDefinition]


def resolve_release_tuples_file(control_plane_root: Path) -> Path:
    configured_file = os.environ.get(RELEASE_TUPLES_FILE_ENV_VAR, "").strip()
    if configured_file:
        candidate_path = Path(configured_file)
        if not candidate_path.is_absolute():
            candidate_path = control_plane_root / candidate_path
        return candidate_path
    return control_plane_root / DEFAULT_RELEASE_TUPLES_FILE


def load_release_tuple_catalog(*, control_plane_root: Path) -> ReleaseTupleCatalog:
    tuples_file = resolve_release_tuples_file(control_plane_root)
    if not tuples_file.exists():
        raise click.ClickException(
            "Missing control-plane release tuples file. "
            f"Create {tuples_file} or point {RELEASE_TUPLES_FILE_ENV_VAR} at an alternate file."
        )
    try:
        payload = tomllib.loads(tuples_file.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise click.ClickException(f"Invalid release tuples file {tuples_file}: {error}") from error
    return _parse_release_tuple_catalog(payload, source_file=tuples_file)


def resolve_release_tuple(
    *,
    control_plane_root: Path,
    context_name: str,
    channel_name: str,
) -> ReleaseTupleDefinition:
    catalog = load_release_tuple_catalog(control_plane_root=control_plane_root)
    context_definition = catalog.contexts.get(context_name)
    if context_definition is None:
        raise click.ClickException(
            f"Release tuples file has no context definition for {context_name!r}."
        )
    release_tuple = context_definition.channels.get(channel_name)
    if release_tuple is None:
        raise click.ClickException(
            f"Release tuples file has no channel definition for {context_name}/{channel_name}."
        )
    return release_tuple


def _parse_release_tuple_catalog(
    payload: dict[str, object],
    *,
    source_file: Path,
) -> ReleaseTupleCatalog:
    schema_version = _read_required_int(payload, "schema_version", scope="release_tuples")
    contexts_table = _read_optional_table(payload, "contexts", scope="release_tuples")
    contexts: dict[str, ReleaseTupleContextDefinition] = {}
    seen_tuple_ids: set[str] = set()
    for context_name, raw_context in contexts_table.items():
        if not isinstance(context_name, str) or not context_name.strip():
            raise click.ClickException(
                f"Expected release_tuples.contexts keys in {source_file} to be non-empty strings."
            )
        context_table = _ensure_table(raw_context, scope=f"release_tuples.contexts.{context_name}")
        channels_table = _read_optional_table(
            context_table,
            "channels",
            scope=f"release_tuples.contexts.{context_name}",
        )
        channels: dict[str, ReleaseTupleDefinition] = {}
        for channel_name, raw_channel in channels_table.items():
            if not isinstance(channel_name, str) or not channel_name.strip():
                raise click.ClickException(
                    f"Expected release_tuples.contexts.{context_name}.channels keys to be non-empty strings."
                )
            channel_table = _ensure_table(
                raw_channel,
                scope=f"release_tuples.contexts.{context_name}.channels.{channel_name}",
            )
            tuple_id = _read_required_non_empty_string(
                channel_table,
                "tuple_id",
                scope=f"release_tuples.contexts.{context_name}.channels.{channel_name}",
            )
            if tuple_id in seen_tuple_ids:
                raise click.ClickException(
                    f"Duplicate release tuple id {tuple_id!r} found in {source_file}."
                )
            seen_tuple_ids.add(tuple_id)
            repo_shas = _read_required_git_sha_map(
                channel_table,
                "repo_shas",
                scope=f"release_tuples.contexts.{context_name}.channels.{channel_name}",
            )
            channels[channel_name] = ReleaseTupleDefinition(tuple_id=tuple_id, repo_shas=repo_shas)
        contexts[context_name] = ReleaseTupleContextDefinition(channels=channels)
    return ReleaseTupleCatalog(schema_version=schema_version, contexts=contexts)


def _read_required_int(source: dict[str, object], key: str, *, scope: str) -> int:
    value = source.get(key)
    if not isinstance(value, int):
        raise click.ClickException(f"Expected {scope}.{key} to be an integer.")
    return value


def _read_optional_table(source: dict[str, object], key: str, *, scope: str) -> dict[str, object]:
    value = source.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise click.ClickException(f"Expected {scope}.{key} to be a table when present.")
    return value


def _ensure_table(value: object, *, scope: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise click.ClickException(f"Expected {scope} to be a table.")
    return value


def _read_required_non_empty_string(source: dict[str, object], key: str, *, scope: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise click.ClickException(f"Expected {scope}.{key} to be a non-empty string.")
    return value.strip()


def _read_required_git_sha_map(
    source: dict[str, object],
    key: str,
    *,
    scope: str,
) -> dict[str, str]:
    value = source.get(key)
    if not isinstance(value, dict):
        raise click.ClickException(f"Expected {scope}.{key} to be a table.")
    repo_shas: dict[str, str] = {}
    for raw_repo, raw_sha in value.items():
        if not isinstance(raw_repo, str) or not raw_repo.strip():
            raise click.ClickException(f"Expected {scope}.{key} keys to be non-empty strings.")
        if not isinstance(raw_sha, str) or not raw_sha.strip():
            raise click.ClickException(
                f"Expected {scope}.{key}.{raw_repo} to be a non-empty git sha string."
            )
        normalized_sha = raw_sha.strip()
        if not GIT_SHA_PATTERN.match(normalized_sha):
            raise click.ClickException(
                f"Expected {scope}.{key}.{raw_repo} to be a 7-40 character hexadecimal git sha."
            )
        repo_shas[raw_repo.strip()] = normalized_sha
    if not repo_shas:
        raise click.ClickException(f"Expected {scope}.{key} to include at least one repo sha.")
    return repo_shas
