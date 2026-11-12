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
    ReleaseReviewStatus,
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
    return nonvalidating_compact_sorted_json_sha256(without_notes.model_dump(mode="json"))


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
        runtime_values=_resolve_recorded_runtime_values(
            record_store=record_store, context=context, instance="prod"
        ),
        managed_secret_keys=keys,
    ), tuple(blockers)


def recorded_initial_version_notes(
    checklist: ReleaseChecklist, profile: LaunchplaneProductProfileRecord
) -> ReleaseChecklist:
    request = profile.release_request
    if (
        checklist.initial_version is None
        or request is None
        or request.initial_version_review is None
    ):
        return checklist
    notes = request.initial_version_review
    if (
        request.production != checklist.production
        or request.candidate != checklist.candidate
        or notes.inputs_sha256 != initial_version_inputs_sha256(checklist)
    ):
        return checklist
    return checklist.model_copy(
        update={
            "initial_version": checklist.initial_version.model_copy(
                update={"client_test_notes": notes.client_test_notes}
            ),
        }
    )


def prepare_initial_version_notes(
    store: object,
    profile: LaunchplaneProductProfileRecord,
    review: ReleaseReviewStatus,
    notes: str,
) -> ReleaseReviewStatus:
    from control_plane.release_review import (
        CLIENT_APPROVAL_REQUIRED,
        RELEASE_RECORD_PENDING,
        checklist_blockers,
        checklist_digest,
    )

    checklist = review.checklist
    if checklist is None or checklist.initial_version is None or not notes.strip():
        raise ValueError("Complete initial-version test notes require its current checklist.")
    replacement = checklist.model_copy(
        update={
            "initial_version": checklist.initial_version.model_copy(
                update={"client_test_notes": notes.strip()}
            ),
        }
    )
    digest = checklist_digest(replacement)
    matching = [
        record
        for record in cast(PostgresRecordStore, store).list_release_review_decision_records(
            product=profile.product
        )
        if record.checklist_digest == digest
    ]
    latest = max(matching, key=lambda record: (record.decided_at, record.record_id), default=None)
    replaced_decision_blockers = (
        {"The Client requested changes.", RELEASE_RECORD_PENDING}
        if digest != review.checklist_digest and review.latest_decision is not None
        else set()
    )
    blockers = tuple(
        dict.fromkeys(
            (
                *(
                    blocker
                    for blocker in review.blockers
                    if blocker not in {INITIAL_TEST_NOTES_REQUIRED, CLIENT_APPROVAL_REQUIRED}
                    and blocker not in replaced_decision_blockers
                    and blocker not in checklist_blockers(checklist, profile=profile)
                ),
                *checklist_blockers(replacement, profile=profile),
            )
        )
    )
    return review.model_copy(
        update={
            "checklist": replacement,
            "checklist_digest": digest,
            "latest_decision": latest,
            "approved": False,
            "checklist_complete": not blockers,
            "blockers": blockers or (CLIENT_APPROVAL_REQUIRED,),
        }
    )
