from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_pr_feedback_record import PreviewPrFeedbackRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord


ProtectedArtifactReason = Literal[
    "stable-inventory",
    "release-tuple",
    "active-preview-generation",
    "active-preview-feedback",
]


class ProtectedArtifactEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str = ""
    context: str
    instance: str = ""
    environment_kind: Literal["stable", "preview"]
    artifact_id: str = ""
    image_references: tuple[str, ...] = ()
    source_git_ref: str = ""
    reason: ProtectedArtifactReason
    source_record_type: str
    source_record_id: str
    image_repository: str = ""
    image_digest: str = ""
    preview_id: str = ""
    preview_generation_id: str = ""
    anchor_repo: str = ""
    anchor_pr_number: int = 0

    @model_validator(mode="after")
    def _validate_entry(self) -> "ProtectedArtifactEntry":
        if not self.context.strip():
            raise ValueError("protected artifact entry requires context")
        if self.environment_kind == "stable" and not self.instance.strip():
            raise ValueError("stable protected artifact entry requires instance")
        if self.environment_kind == "preview" and not self.preview_id.strip():
            raise ValueError("preview protected artifact entry requires preview_id")
        if not self.artifact_id.strip() and not self.image_references:
            raise ValueError("protected artifact entry requires artifact_id or image_references")
        if not self.source_record_type.strip():
            raise ValueError("protected artifact entry requires source_record_type")
        if not self.source_record_id.strip():
            raise ValueError("protected artifact entry requires source_record_id")
        return self


class ProtectedArtifactSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str = ""
    context: str = ""
    entries: tuple[ProtectedArtifactEntry, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    image_references: tuple[str, ...] = ()
    image_digests: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class ProtectedArtifactStore(Protocol):
    def list_artifact_manifests(self) -> tuple[ArtifactIdentityManifest, ...]: ...

    def list_environment_inventory(self) -> tuple[EnvironmentInventory, ...]: ...

    def list_product_profile_records(self) -> tuple[LaunchplaneProductProfileRecord, ...]: ...

    def list_release_tuple_records(self) -> tuple[ReleaseTupleRecord, ...]: ...

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]: ...

    def list_preview_generation_records(
        self,
        *,
        preview_id: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewGenerationRecord, ...]: ...

    def list_preview_pr_feedback_records(
        self,
        *,
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewPrFeedbackRecord, ...]: ...


_ACTIVE_PREVIEW_STATES = {"pending", "active", "failed", "paused", "teardown_pending"}
_PROTECTED_PREVIEW_GENERATION_STATES = {
    "resolving",
    "building",
    "deploying",
    "verifying",
    "ready",
}
_READY_PREVIEW_FEEDBACK_STATUSES = {"ready"}


def _normalized_values(values: list[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        stripped_value = value.strip()
        if stripped_value and stripped_value not in normalized:
            normalized.append(stripped_value)
    return tuple(sorted(normalized))


def _profile_by_context(
    profiles: tuple[LaunchplaneProductProfileRecord, ...],
) -> dict[str, LaunchplaneProductProfileRecord]:
    by_context: dict[str, LaunchplaneProductProfileRecord] = {}
    for profile in profiles:
        for lane in profile.lanes:
            by_context.setdefault(lane.context, profile)
        if profile.preview.enabled and profile.preview.context:
            by_context.setdefault(profile.preview.context, profile)
    return by_context


def _manifest_by_artifact_id(
    manifests: tuple[ArtifactIdentityManifest, ...],
) -> dict[str, ArtifactIdentityManifest]:
    return {manifest.artifact_id: manifest for manifest in manifests}


def _product_for_context(
    profile_map: dict[str, LaunchplaneProductProfileRecord], context: str
) -> str:
    profile = profile_map.get(context)
    return profile.product if profile is not None else ""


def _manifest_image_references(manifest: ArtifactIdentityManifest | None) -> tuple[str, ...]:
    if manifest is None:
        return ()
    references = [f"{manifest.image.repository}@{manifest.image.digest}"]
    references.extend(
        f"{manifest.image.repository}:{tag}" for tag in manifest.image.tags if tag.strip()
    )
    return _normalized_values(references)


def _entry_image_references(
    *,
    manifest: ArtifactIdentityManifest | None,
    runtime_image_reference: str = "",
    extra_image_references: list[str] | None = None,
) -> tuple[str, ...]:
    references = list(_manifest_image_references(manifest))
    references.append(runtime_image_reference)
    references.extend(extra_image_references or [])
    return _normalized_values(references)


def _missing_manifest_warning(artifact_id: str, source_label: str) -> str:
    return (
        f"Protected artifact {artifact_id} from {source_label} has no stored artifact "
        "manifest; cleanup consumers must still treat the artifact id as protected."
    )


def _unresolved_live_artifact_warning(source_label: str, source_record_id: str) -> str:
    return (
        f"Live {source_label} record {source_record_id} has no artifact id or image "
        "reference; cleanup consumers must fail closed for this scope."
    )


def _active_preview_records(
    record_store: ProtectedArtifactStore,
    *,
    context_name: str,
) -> tuple[PreviewRecord, ...]:
    records = record_store.list_preview_records(context_name=context_name)
    return tuple(record for record in records if record.state in _ACTIVE_PREVIEW_STATES)


def _protected_preview_generations(
    record_store: ProtectedArtifactStore, preview: PreviewRecord
) -> tuple[PreviewGenerationRecord, ...]:
    candidate_ids = (
        preview.serving_generation_id,
        preview.active_generation_id,
        preview.latest_generation_id,
    )
    generations_by_id = {
        generation.generation_id: generation
        for generation in record_store.list_preview_generation_records(
            preview_id=preview.preview_id
        )
    }
    generations: list[PreviewGenerationRecord] = []
    for generation_id in candidate_ids:
        generation = generations_by_id.get(generation_id)
        if (
            generation is not None
            and generation.state in _PROTECTED_PREVIEW_GENERATION_STATES
            and generation not in generations
        ):
            generations.append(generation)
    if generations:
        return tuple(generations)
    return tuple(
        generation for generation in generations_by_id.values() if generation.state == "ready"
    )


def _latest_ready_preview_feedback(
    record_store: ProtectedArtifactStore, preview: PreviewRecord
) -> PreviewPrFeedbackRecord | None:
    records = record_store.list_preview_pr_feedback_records(context_name=preview.context)
    for record in records:
        if (
            record.anchor_repo == preview.anchor_repo
            and record.anchor_pr_number == preview.anchor_pr_number
            and record.status in _READY_PREVIEW_FEEDBACK_STATUSES
        ):
            return record
    return None


def _stable_inventory_entries(
    record_store: ProtectedArtifactStore,
    *,
    profile_map: dict[str, LaunchplaneProductProfileRecord],
    manifests: dict[str, ArtifactIdentityManifest],
    product: str,
    context_name: str,
) -> tuple[list[ProtectedArtifactEntry], list[str]]:
    entries: list[ProtectedArtifactEntry] = []
    warnings: list[str] = []
    for inventory in record_store.list_environment_inventory():
        inventory_product = _product_for_context(profile_map, inventory.context)
        if product and inventory_product != product:
            continue
        if context_name and inventory.context != context_name:
            continue
        artifact_id = inventory.artifact_identity.artifact_id if inventory.artifact_identity else ""
        manifest = manifests.get(artifact_id)
        if artifact_id and manifest is None:
            warnings.append(_missing_manifest_warning(artifact_id, "environment_inventory"))
        runtime_image_reference = ""
        if inventory.runtime_identity and inventory.runtime_identity.image_reference:
            runtime_image_reference = inventory.runtime_identity.image_reference
        image_references = _entry_image_references(
            manifest=manifest,
            runtime_image_reference=runtime_image_reference,
        )
        source_record_id = f"{inventory.context}-{inventory.instance}"
        if not artifact_id and not image_references:
            warnings.append(
                _unresolved_live_artifact_warning("environment_inventory", source_record_id)
            )
            continue
        entries.append(
            ProtectedArtifactEntry(
                product=inventory_product,
                context=inventory.context,
                instance=inventory.instance,
                environment_kind="stable",
                artifact_id=artifact_id,
                image_references=image_references,
                source_git_ref=inventory.source_git_ref,
                reason="stable-inventory",
                source_record_type="environment_inventory",
                source_record_id=source_record_id,
                image_repository=manifest.image.repository if manifest is not None else "",
                image_digest=manifest.image.digest if manifest is not None else "",
            )
        )
    return entries, warnings


def _release_tuple_entries(
    record_store: ProtectedArtifactStore,
    *,
    profile_map: dict[str, LaunchplaneProductProfileRecord],
    manifests: dict[str, ArtifactIdentityManifest],
    product: str,
    context_name: str,
) -> tuple[list[ProtectedArtifactEntry], list[str]]:
    entries: list[ProtectedArtifactEntry] = []
    warnings: list[str] = []
    for record in record_store.list_release_tuple_records():
        release_product = _product_for_context(profile_map, record.context)
        if product and release_product != product:
            continue
        if context_name and record.context != context_name:
            continue
        manifest = manifests.get(record.artifact_id)
        if manifest is None:
            warnings.append(_missing_manifest_warning(record.artifact_id, "release_tuple"))
        repository = record.image_repository or (manifest.image.repository if manifest else "")
        digest = record.image_digest or (manifest.image.digest if manifest else "")
        image_references = []
        if repository and digest:
            image_references.append(f"{repository}@{digest}")
        image_references.extend(_manifest_image_references(manifest))
        entries.append(
            ProtectedArtifactEntry(
                product=release_product,
                context=record.context,
                instance=record.channel,
                environment_kind="stable",
                artifact_id=record.artifact_id,
                image_references=_normalized_values(image_references),
                source_git_ref=next(iter(record.repo_shas.values()), ""),
                reason="release-tuple",
                source_record_type="release_tuple",
                source_record_id=record.tuple_id,
                image_repository=repository,
                image_digest=digest,
            )
        )
    return entries, warnings


def _preview_entries(
    record_store: ProtectedArtifactStore,
    *,
    profile_map: dict[str, LaunchplaneProductProfileRecord],
    manifests: dict[str, ArtifactIdentityManifest],
    product: str,
    context_name: str,
) -> tuple[list[ProtectedArtifactEntry], list[str]]:
    entries: list[ProtectedArtifactEntry] = []
    warnings: list[str] = []
    contexts = sorted(
        context_name
        for context_name, profile in profile_map.items()
        if profile.preview.enabled and profile.preview.context == context_name
    )
    if context_name:
        contexts = [context_name]
    for preview_context in contexts:
        preview_product = _product_for_context(profile_map, preview_context)
        if product and preview_product != product:
            continue
        for preview in _active_preview_records(record_store, context_name=preview_context):
            for generation in _protected_preview_generations(record_store, preview):
                artifact_id = generation.artifact_id.strip()
                manifest = manifests.get(artifact_id)
                runtime_image_reference = ""
                if generation.runtime_identity and generation.runtime_identity.image_reference:
                    runtime_image_reference = generation.runtime_identity.image_reference
                image_references = _entry_image_references(
                    manifest=manifest,
                    runtime_image_reference=runtime_image_reference,
                )
                if artifact_id and manifest is None:
                    warnings.append(_missing_manifest_warning(artifact_id, "preview_generation"))
                if not artifact_id and not image_references:
                    warnings.append(
                        _unresolved_live_artifact_warning(
                            "preview_generation", generation.generation_id
                        )
                    )
                    continue
                entries.append(
                    ProtectedArtifactEntry(
                        product=preview_product,
                        context=preview.context,
                        environment_kind="preview",
                        artifact_id=artifact_id,
                        image_references=image_references,
                        source_git_ref=generation.anchor_summary.head_sha,
                        reason="active-preview-generation",
                        source_record_type="preview_generation",
                        source_record_id=generation.generation_id,
                        preview_id=preview.preview_id,
                        preview_generation_id=generation.generation_id,
                        anchor_repo=preview.anchor_repo,
                        anchor_pr_number=preview.anchor_pr_number,
                        image_repository=manifest.image.repository if manifest is not None else "",
                        image_digest=manifest.image.digest if manifest is not None else "",
                    )
                )
            feedback = _latest_ready_preview_feedback(record_store, preview)
            if feedback is not None:
                feedback_image_references = _normalized_values(
                    [feedback.immutable_image_reference, feedback.refresh_image_reference]
                )
                if feedback_image_references:
                    entries.append(
                        ProtectedArtifactEntry(
                            product=preview_product or feedback.product,
                            context=preview.context,
                            environment_kind="preview",
                            image_references=feedback_image_references,
                            source_git_ref=feedback.revision,
                            reason="active-preview-feedback",
                            source_record_type="preview_pr_feedback",
                            source_record_id=feedback.feedback_id,
                            preview_id=preview.preview_id,
                            anchor_repo=preview.anchor_repo,
                            anchor_pr_number=preview.anchor_pr_number,
                        )
                    )
    return entries, warnings


def build_protected_artifact_set(
    record_store: ProtectedArtifactStore,
    *,
    product: str = "",
    context_name: str = "",
) -> ProtectedArtifactSet:
    normalized_product = product.strip()
    normalized_context = context_name.strip()
    profiles = record_store.list_product_profile_records()
    profile_map = _profile_by_context(profiles)
    manifests = _manifest_by_artifact_id(record_store.list_artifact_manifests())
    stable_entries, stable_warnings = _stable_inventory_entries(
        record_store,
        profile_map=profile_map,
        manifests=manifests,
        product=normalized_product,
        context_name=normalized_context,
    )
    release_tuple_entries, release_tuple_warnings = _release_tuple_entries(
        record_store,
        profile_map=profile_map,
        manifests=manifests,
        product=normalized_product,
        context_name=normalized_context,
    )
    preview_entries, preview_warnings = _preview_entries(
        record_store,
        profile_map=profile_map,
        manifests=manifests,
        product=normalized_product,
        context_name=normalized_context,
    )
    entries = [*stable_entries, *release_tuple_entries, *preview_entries]
    artifact_ids = _normalized_values([entry.artifact_id for entry in entries])
    image_references = _normalized_values(
        [image_reference for entry in entries for image_reference in entry.image_references]
    )
    image_digests = _normalized_values([entry.image_digest for entry in entries])
    warnings = _normalized_values([*stable_warnings, *release_tuple_warnings, *preview_warnings])
    return ProtectedArtifactSet(
        product=normalized_product,
        context=normalized_context,
        entries=tuple(entries),
        artifact_ids=artifact_ids,
        image_references=image_references,
        image_digests=image_digests,
        warnings=warnings,
    )
