from __future__ import annotations

from collections.abc import Callable
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    product_profiles_share_repository,
)
from control_plane.contracts.read_coverage import ReadCoverage, read_coverage

RepoProductClassification = Literal[
    "managed_runtime", "active_awareness", "support_dependency", "out_of_scope"
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
    driver_id: str = Field(
        default="",
        exclude_if=lambda value: value == "",
        json_schema_extra={"x-launchplane-optional-response": True},
    )
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
    ambiguous_repositories: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    ambiguous_repository_product_references: tuple[AmbiguousRepositoryProductReference, ...] = (
        Field(
            default=(),
            exclude_if=lambda value: not value,
            json_schema_extra={"x-launchplane-optional-response": True},
        )
    )
    product_reference_coverage: ReadCoverage | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )

    @model_validator(mode="after")
    def _validate_mapping(self) -> "RepoProductMapping":
        if not self.generated_at.strip():
            raise ValueError("repo product mapping requires generated_at")
        repositories = [entry.repository.strip().lower() for entry in self.repositories]
        if len(repositories) != len(set(repositories)):
            raise ValueError("repo product mapping repositories must be unique")
        return self


class AmbiguousRepositoryProductReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    products: tuple[str, ...]


def build_repo_product_mapping_from_records(
    *,
    generated_at: str,
    product_profiles: tuple[LaunchplaneProductProfileRecord, ...],
    work_requests: tuple[EveryCodeWorkRequestRecord, ...] = (),
    product_reference_allowed: Callable[[str], bool] | None = None,
    product_reference_coverage: ReadCoverage | None = None,
) -> RepoProductMapping:
    entries: dict[str, RepoProductMappingEntry] = {}
    ambiguous, reference_products = _ambiguous_repository_groups(product_profiles)
    for profile in product_profiles:
        repository = profile.repository.strip()
        if not repository or "/" not in repository:
            continue
        key = repository.lower()
        if key in ambiguous:
            continue
        entries[key] = _product_profile_entry(profile)

    for request in work_requests:
        repository = request.repository.strip()
        if not repository or "/" not in repository:
            continue
        if repository.lower() in ambiguous:
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
        ambiguous_repositories=tuple(sorted(ambiguous)),
        ambiguous_repository_product_references=tuple(
            AmbiguousRepositoryProductReference(
                repository=repository,
                products=tuple(
                    product
                    for product in products
                    if product_reference_allowed is None or product_reference_allowed(product)
                ),
            )
            for repository, products in sorted(reference_products.items())
        ),
        product_reference_coverage=product_reference_coverage,
    )


def _ambiguous_repository_groups(
    product_profiles: tuple[LaunchplaneProductProfileRecord, ...],
) -> tuple[set[str], dict[str, tuple[str, ...]]]:
    remaining = set(range(len(product_profiles)))
    groups: list[set[int]] = []
    while remaining:
        group = {remaining.pop()}
        changed = True
        while changed:
            changed = False
            for index in tuple(remaining):
                if any(
                    product_profiles_share_repository(
                        product_profiles[index], product_profiles[member]
                    )
                    for member in group
                ):
                    remaining.remove(index)
                    group.add(index)
                    changed = True
        groups.append(group)
    ambiguous: set[str] = set()
    references: dict[str, tuple[str, ...]] = {}
    for group in groups:
        products = tuple(sorted({product_profiles[index].product for index in group}))
        if len(products) < 2:
            continue
        repositories = {
            product_profiles[index].repository.strip().lower()
            for index in group
            if product_profiles[index].repository.strip()
        }
        ambiguous.update(repositories)
        for repository in repositories:
            references[repository] = products
    return ambiguous, references


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


def build_repo_product_mapping_service_payload(
    *,
    generated_at: str,
    product_store: RepoProductMappingProductStore,
    work_request_store: RepoProductMappingWorkRequestStore,
    product_reference_allowed: Callable[[str], bool] | None = None,
    authorization_complete: bool = True,
) -> dict[str, object]:
    product_profiles = tuple(
        profile for profile in product_store.list_product_profile_records() if profile.is_active
    )
    work_requests = work_request_store.list_every_code_work_request_records(limit=None)
    mapping = build_repo_product_mapping_from_records(
        generated_at=generated_at,
        product_profiles=product_profiles,
        work_requests=work_requests,
        product_reference_allowed=product_reference_allowed,
        product_reference_coverage=read_coverage(
            product_store,
            collections=("product_profiles",),
            population="active_product_profiles",
            authorization_complete=authorization_complete,
            exclusions=("inactive_products",),
        ),
    )
    return {
        "mapping": mapping.model_dump(mode="json"),
        "source": {
            "product_count": len(product_profiles),
            "work_request_count": len(work_requests),
        },
    }
