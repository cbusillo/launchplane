from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol
import click

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.storage.factory import resolve_database_url
from control_plane.storage.postgres import PostgresRecordStore

GIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")
LONG_LIVED_RELEASE_TUPLE_CHANNELS = {"testing", "prod"}


class ReleaseTupleRecordStore(Protocol):
    def list_release_tuple_records(self) -> tuple[ReleaseTupleRecord, ...]: ...


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


def should_mint_release_tuple_for_channel(channel_name: str) -> bool:
    return channel_name.strip() in LONG_LIVED_RELEASE_TUPLE_CHANNELS


def _require_stable_release_tuple_channel(channel_name: str, *, scope: str) -> str:
    normalized_channel_name = channel_name.strip()
    if normalized_channel_name not in LONG_LIVED_RELEASE_TUPLE_CHANNELS:
        supported_channels = ", ".join(sorted(LONG_LIVED_RELEASE_TUPLE_CHANNELS))
        raise click.ClickException(
            f"{scope} only supports stable remote channels {supported_channels}; got {normalized_channel_name!r}. "
            "Use Launchplane preview records for preview/dev runtime instead of minting or tracking release tuples there."
        )
    return normalized_channel_name


def render_release_tuple_catalog_toml(records: tuple[ReleaseTupleRecord, ...]) -> str:
    lines = ["schema_version = 1", ""]
    for record in sorted(records, key=lambda item: (item.context, item.channel)):
        channel_name = _require_stable_release_tuple_channel(
            record.channel,
            scope=f"Release tuple record {record.tuple_id}",
        )
        lines.append(
            f"[contexts.{_toml_bare_key(record.context)}.channels.{_toml_bare_key(channel_name)}]"
        )
        lines.append(f"tuple_id = {_toml_string(record.tuple_id)}")
        lines.append("")
        lines.append(
            f"[contexts.{_toml_bare_key(record.context)}.channels.{_toml_bare_key(channel_name)}.repo_shas]"
        )
        for repo_name, git_sha in sorted(record.repo_shas.items()):
            lines.append(f"{_toml_bare_key(repo_name)} = {_toml_string(git_sha)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_release_tuple_record_from_artifact_manifest(
    *,
    context_name: str,
    channel_name: str,
    artifact_manifest: ArtifactIdentityManifest,
    deployment_record_id: str,
    minted_at: str,
) -> ReleaseTupleRecord:
    normalized_channel_name = _require_stable_release_tuple_channel(
        channel_name,
        scope="Release tuple minting",
    )
    repo_shas = repo_shas_from_artifact_manifest(
        context_name=context_name,
        artifact_manifest=artifact_manifest,
    )
    return ReleaseTupleRecord(
        tuple_id=_artifact_release_tuple_id(
            context_name=context_name,
            channel_name=normalized_channel_name,
            artifact_id=artifact_manifest.artifact_id,
        ),
        context=context_name,
        channel=normalized_channel_name,
        artifact_id=artifact_manifest.artifact_id,
        repo_shas=repo_shas,
        image_repository=artifact_manifest.image.repository,
        image_digest=artifact_manifest.image.digest,
        deployment_record_id=deployment_record_id,
        provenance="ship",
        minted_at=minted_at,
    )


def build_promoted_release_tuple_record(
    *,
    source_tuple: ReleaseTupleRecord,
    to_channel_name: str,
    deployment_record_id: str,
    promotion_record_id: str,
    minted_at: str,
) -> ReleaseTupleRecord:
    normalized_channel_name = _require_stable_release_tuple_channel(
        to_channel_name,
        scope="Release tuple promotion",
    )
    return ReleaseTupleRecord(
        tuple_id=_artifact_release_tuple_id(
            context_name=source_tuple.context,
            channel_name=normalized_channel_name,
            artifact_id=source_tuple.artifact_id,
        ),
        context=source_tuple.context,
        channel=normalized_channel_name,
        artifact_id=source_tuple.artifact_id,
        repo_shas=dict(source_tuple.repo_shas),
        image_repository=source_tuple.image_repository,
        image_digest=source_tuple.image_digest,
        deployment_record_id=deployment_record_id,
        promotion_record_id=promotion_record_id,
        promoted_from_channel=source_tuple.channel,
        provenance="promotion",
        minted_at=minted_at,
    )


def require_source_release_tuple_for_promotion(
    *,
    source_tuple: ReleaseTupleRecord,
    artifact_id: str,
    context_name: str,
    from_channel_name: str,
) -> ReleaseTupleRecord:
    if source_tuple.context != context_name or source_tuple.channel != from_channel_name:
        raise click.ClickException(
            "Promotion source release tuple does not match the requested source lane. "
            f"Tuple={source_tuple.context}/{source_tuple.channel} request={context_name}/{from_channel_name}."
        )
    if source_tuple.artifact_id != artifact_id:
        raise click.ClickException(
            "Promotion requires the source lane release tuple to match the requested artifact. "
            f"Tuple artifact={source_tuple.artifact_id} request artifact={artifact_id}."
        )
    return source_tuple


def repo_shas_from_artifact_manifest(
    *,
    context_name: str,
    artifact_manifest: ArtifactIdentityManifest,
) -> dict[str, str]:
    repo_shas = {
        _primary_repo_name_for_context(context_name): _require_tuple_git_sha(
            artifact_manifest.source_commit,
            label=f"artifact {artifact_manifest.artifact_id} source_commit",
        )
    }
    for addon_source in artifact_manifest.addon_sources:
        repo_name = _repo_name_from_repository(addon_source.repository)
        git_sha = _require_tuple_git_sha(
            addon_source.ref,
            label=f"artifact {artifact_manifest.artifact_id} addon source {addon_source.repository}",
        )
        previous_sha = repo_shas.get(repo_name)
        if previous_sha is not None and previous_sha != git_sha:
            raise click.ClickException(
                "Artifact manifest cannot mint a release tuple because it has conflicting "
                f"refs for repo {repo_name}: {previous_sha} and {git_sha}."
            )
        repo_shas[repo_name] = git_sha
    return repo_shas


def _artifact_release_tuple_id(*, context_name: str, channel_name: str, artifact_id: str) -> str:
    return f"{context_name}-{channel_name}-{artifact_id}"


def _primary_repo_name_for_context(context_name: str) -> str:
    return f"tenant-{context_name.strip()}"


def _repo_name_from_repository(repository: str) -> str:
    repo_name = repository.strip().removesuffix(".git").rsplit("/", maxsplit=1)[-1]
    if not repo_name:
        raise click.ClickException("Artifact manifest addon source requires a repository name.")
    if repo_name.startswith("odoo-"):
        repo_name = repo_name.removeprefix("odoo-")
    return repo_name


def _require_tuple_git_sha(value: str, *, label: str) -> str:
    normalized_value = value.strip()
    if not GIT_SHA_PATTERN.match(normalized_value):
        raise click.ClickException(
            f"{label} must be a 7-40 character hexadecimal git sha before it can mint a release tuple."
        )
    return normalized_value


def _toml_bare_key(value: str) -> str:
    normalized_value = value.strip()
    if re.match(r"^[A-Za-z0-9_-]+$", normalized_value):
        return normalized_value
    return _toml_string(normalized_value)


def _toml_string(value: str) -> str:
    return json.dumps(value)


def load_release_tuple_catalog() -> ReleaseTupleCatalog:
    database_url = resolve_database_url()
    if not database_url:
        raise click.ClickException(
            "Launchplane release tuples are DB-backed runtime authority. "
            "Set LAUNCHPLANE_DATABASE_URL before resolving a stable release tuple baseline."
        )
    database_catalog = _load_optional_release_tuple_catalog_from_database(database_url=database_url)
    if database_catalog is None:
        raise click.ClickException(
            "No Launchplane release tuple records were found in the configured database. "
            "Write release tuple records before resolving a stable release tuple baseline."
        )
    return database_catalog


def resolve_release_tuple(
    *,
    context_name: str,
    channel_name: str,
) -> ReleaseTupleDefinition:
    catalog = load_release_tuple_catalog()
    context_definition = catalog.contexts.get(context_name)
    if context_definition is None:
        raise click.ClickException(
            f"Launchplane release tuple records have no context definition for {context_name!r}."
        )
    release_tuple = context_definition.channels.get(channel_name)
    if release_tuple is None:
        raise click.ClickException(
            f"Launchplane release tuple records have no channel definition for {context_name}/{channel_name}."
        )
    return release_tuple


def _load_optional_release_tuple_catalog_from_database(
    *, database_url: str
) -> ReleaseTupleCatalog | None:
    record_store: PostgresRecordStore | None = None
    try:
        record_store = PostgresRecordStore(database_url=database_url)
        record_store.ensure_schema()
        return load_optional_release_tuple_catalog_from_store(
            record_store=record_store,
            source_label="Launchplane Postgres storage",
        )
    except Exception as error:
        raise click.ClickException(
            f"Could not load release tuples from Launchplane Postgres storage: {error}"
        ) from error
    finally:
        try:
            if record_store is not None:
                record_store.close()
        except Exception:
            pass


def load_optional_release_tuple_catalog_from_store(
    *,
    record_store: ReleaseTupleRecordStore,
    source_label: str = "Launchplane release tuple records",
) -> ReleaseTupleCatalog | None:
    records = record_store.list_release_tuple_records()
    if not records:
        return None
    return build_release_tuple_catalog_from_records(records, source_label=source_label)


def build_release_tuple_catalog_from_records(
    records: tuple[ReleaseTupleRecord, ...],
    *,
    source_label: str = "Launchplane release tuple records",
) -> ReleaseTupleCatalog:
    merged_contexts: dict[str, dict[str, ReleaseTupleDefinition]] = {}
    seen_tuple_ids: set[str] = set()
    for record in sorted(records, key=lambda item: (item.context, item.channel)):
        normalized_channel_name = _require_stable_release_tuple_channel(
            record.channel,
            scope=f"{source_label} record {record.tuple_id}",
        )
        if record.tuple_id in seen_tuple_ids:
            raise click.ClickException(
                f"Duplicate release tuple id {record.tuple_id!r} found in {source_label}."
            )
        seen_tuple_ids.add(record.tuple_id)
        context_channels = merged_contexts.setdefault(record.context, {})
        context_channels[normalized_channel_name] = ReleaseTupleDefinition(
            tuple_id=record.tuple_id,
            repo_shas=dict(record.repo_shas),
        )
    return _build_release_tuple_catalog_from_context_map(merged_contexts)


def _build_release_tuple_catalog_from_context_map(
    context_map: dict[str, dict[str, ReleaseTupleDefinition]],
) -> ReleaseTupleCatalog:
    seen_tuple_ids: set[str] = set()
    contexts: dict[str, ReleaseTupleContextDefinition] = {}
    for context_name, channels_map in sorted(context_map.items()):
        channels: dict[str, ReleaseTupleDefinition] = {}
        for channel_name, release_tuple in sorted(channels_map.items()):
            normalized_channel_name = _require_stable_release_tuple_channel(
                channel_name,
                scope=f"release tuple catalog merge for context {context_name}",
            )
            if release_tuple.tuple_id in seen_tuple_ids:
                raise click.ClickException(
                    f"Duplicate release tuple id {release_tuple.tuple_id!r} found while merging catalogs."
                )
            seen_tuple_ids.add(release_tuple.tuple_id)
            channels[normalized_channel_name] = ReleaseTupleDefinition(
                tuple_id=release_tuple.tuple_id,
                repo_shas=dict(release_tuple.repo_shas),
            )
        contexts[context_name] = ReleaseTupleContextDefinition(channels=channels)
    return ReleaseTupleCatalog(schema_version=1, contexts=contexts)
