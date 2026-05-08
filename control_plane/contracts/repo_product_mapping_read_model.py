from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord


RepoProductClassification = Literal[
    "managed_runtime",
    "active_awareness",
    "support_dependency",
    "out_of_scope",
]


class RepoProductMappingProductStore(Protocol):
    def list_product_profile_records(
        self,
        *,
        driver_id: str = "",
    ) -> tuple[LaunchplaneProductProfileRecord, ...]: ...


class RepoProductMappingWorkRequestStore(Protocol):
    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]: ...


class RepoProductMappingEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    classification: RepoProductClassification
    product: str = ""
    display_name: str = ""
    driver_id: str = ""
    contexts: tuple[str, ...] = ()
    environments: tuple[str, ...] = ()
    preview_context: str = ""
    source: Literal["product_profile", "every_code_work_request", "explicit"]
    updated_at: str = ""

    @model_validator(mode="after")
    def _validate_entry(self) -> "RepoProductMappingEntry":
        if not self.repository.strip() or "/" not in self.repository.strip():
            raise ValueError("repo product mapping entry requires owner/repo repository")
        if self.classification == "managed_runtime" and not self.product.strip():
            raise ValueError("managed runtime repo mapping requires product")
        self.contexts = _dedupe_non_empty(self.contexts)
        self.environments = _dedupe_non_empty(self.environments)
        return self


class RepoProductMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    generated_at: str
    repositories: tuple[RepoProductMappingEntry, ...]

    @model_validator(mode="after")
    def _validate_mapping(self) -> "RepoProductMapping":
        if not self.generated_at.strip():
            raise ValueError("repo product mapping requires generated_at")
        repositories = [entry.repository.strip().lower() for entry in self.repositories]
        if len(repositories) != len(set(repositories)):
            raise ValueError("repo product mapping repositories must be unique")
        return self


def build_repo_product_mapping_from_records(
    *,
    generated_at: str,
    product_profiles: tuple[LaunchplaneProductProfileRecord, ...],
    work_requests: tuple[EveryCodeWorkRequestRecord, ...] = (),
) -> RepoProductMapping:
    entries: dict[str, RepoProductMappingEntry] = {}
    for profile in product_profiles:
        repository = profile.repository.strip()
        if not repository or "/" not in repository:
            continue
        entries[repository.lower()] = _product_profile_entry(profile)

    for request in work_requests:
        repository = request.repository.strip()
        if not repository or "/" not in repository:
            continue
        entries.setdefault(
            repository.lower(),
            RepoProductMappingEntry(
                repository=repository,
                classification="active_awareness",
                display_name=repository.rsplit("/", maxsplit=1)[-1],
                source="every_code_work_request",
                updated_at=request.updated_at or request.queued_at,
            ),
        )
    return RepoProductMapping(
        generated_at=generated_at,
        repositories=tuple(sorted(entries.values(), key=lambda entry: entry.repository.lower())),
    )


def _product_profile_entry(profile: LaunchplaneProductProfileRecord) -> RepoProductMappingEntry:
    contexts = tuple(lane.context for lane in profile.lanes) + profile.historical_contexts
    environments = tuple(lane.instance for lane in profile.lanes)
    return RepoProductMappingEntry(
        repository=profile.repository,
        classification="managed_runtime",
        product=profile.product,
        display_name=profile.display_name,
        driver_id=profile.driver_id,
        contexts=contexts,
        environments=environments,
        preview_context=profile.preview.context if profile.preview.enabled else "",
        source="product_profile",
        updated_at=profile.updated_at,
    )


def _dedupe_non_empty(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_value in values:
        value = raw_value.strip()
        if value and value not in normalized:
            normalized.append(value)
    return tuple(normalized)
