from typing import cast

from control_plane.artifact_release_inputs import verified_release_inputs
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256
from control_plane.contracts.product_profile_record import (
    FRESH_TESTING_LOCAL_CREDENTIAL_KEYS,
    LaunchplaneProductProfileRecord,
)
from control_plane.contracts.release_review import (
    InitialVersionReview,
    InitialVersionSource,
    ReleaseChecklist,
    InitialVersionTestNotes,
)
from control_plane.storage.postgres import PostgresRecordStore


INITIAL_TEST_NOTES_REQUIRED = (
    "The complete initial version needs Client test notes before release review."
)


def initial_version_inputs_sha256(checklist: ReleaseChecklist) -> str:
    initial = checklist.initial_version
    if initial is None:
        raise ValueError("This checklist is not an initial-version review.")
    without_notes = checklist.model_copy(
        update={
            "initial_version": initial.model_copy(update={"client_test_notes": ""}),
        }
    )
    from control_plane.release_review import checklist_digest

    return checklist_digest(without_notes)


def initial_version_review(
    store: object,
    profile: LaunchplaneProductProfileRecord,
    artifact: ArtifactIdentityManifest,
) -> tuple[InitialVersionReview, tuple[str, ...]]:
    from control_plane.odoo_first_empty_inputs import resolve_first_empty_authority
    from control_plane.workflows.odoo_stable_target_replacement import (
        _resolve_recorded_runtime_values,
    )

    record_store = cast(PostgresRecordStore, store)
    context = next(lane.context for lane in profile.lanes if lane.instance == "prod")
    authority = resolve_first_empty_authority(
        record_store, profile.product, context, allow_release_history=True
    )
    sources, unknown = verified_release_inputs(artifact)
    keys = tuple(
        sorted(
            {
                binding.binding_key
                for delivery in authority.bundle.secret_delivery_read_sets
                for binding in delivery.bindings
                if delivery.includes(binding)
            }
        )
    )
    blockers = list(unknown)
    if not FRESH_TESTING_LOCAL_CREDENTIAL_KEYS <= set(keys):
        blockers.append("The initial version's managed Odoo runtime credentials are incomplete.")
    return InitialVersionReview(
        artifact_manifest_sha256=nonvalidating_compact_sorted_json_sha256(
            artifact.model_dump(mode="json")
        ),
        image_reference=f"{artifact.image.repository}@{artifact.image.digest}",
        sources=(
            InitialVersionSource(
                name="Product", repository=profile.repository, commit=artifact.source_commit
            ),
            *(
                InitialVersionSource(name=name, repository=source.repository, commit=source.commit)
                for name, source in sorted(sources.items())
                if source.has_source_identity
            ),
        ),
        target_context=context,
        target_name=authority.target.target_name,
        runtime_values={
            key: value
            for key, value in _resolve_recorded_runtime_values(
                record_store=record_store, context=context, instance="prod"
            ).items()
            if key not in keys
        },
        managed_secret_keys=keys,
    ), tuple(blockers)


def recorded_initial_version_notes(
    checklist: ReleaseChecklist, notes: InitialVersionTestNotes | None
) -> ReleaseChecklist:
    if checklist.initial_version is None or notes is None:
        return checklist
    if notes.inputs_sha256 != initial_version_inputs_sha256(checklist):
        return checklist
    return checklist.model_copy(
        update={
            "initial_version": checklist.initial_version.model_copy(
                update={"client_test_notes": notes.client_test_notes}
            ),
        }
    )
