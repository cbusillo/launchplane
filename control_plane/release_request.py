import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.release_request import (
    ReleaseRequestDecisionRecord,
    ReleaseRequestSource,
)
from control_plane.contracts.release_review import ReleaseReviewStatus
from control_plane.release_invitation import (
    _accepted_release_in_progress,
    _last_client_decision,
    _ready,
    publish_release_invitation,
)
from control_plane.release_review import ReleaseReviewStore
from control_plane.storage.postgres import ProductProfileCompareWriteResult


def record_release_request(
    *,
    store: ReleaseReviewStore,
    profile: LaunchplaneProductProfileRecord,
    review: ReleaseReviewStatus,
    source: ReleaseRequestSource,
    reason: str,
    decision_reference: str,
    actor: str,
) -> LaunchplaneProductProfileRecord:
    if not _ready(review, profile):
        raise ValueError("The release is not ready for a Client request.")
    assert review.checklist is not None
    with store.release_review_publication_lock(record_id=f"invitation:{profile.product}"):
        if store.read_product_profile_record(profile.product) != profile:
            raise ValueError("The product changed before the release request was recorded.")
        client_decision = _last_client_decision(store, profile)
        if _accepted_release_in_progress(
            store, profile, client_decision, review.checklist.production, review.checklist.candidate
        ):
            raise ValueError("An accepted release is still in progress.")
        client_decision_id = client_decision.record_id if client_decision else ""
        record_id = hashlib.sha256(
            json.dumps(
                (
                    profile.product,
                    profile.repository,
                    profile.owner.github_id,
                    review.checklist_digest,
                    client_decision_id,
                ),
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if profile.release_request is not None and profile.release_request.record_id == record_id:
            return profile
        now = datetime.now(UTC).isoformat()
        request = ReleaseRequestDecisionRecord(
            record_id=record_id,
            product=profile.product,
            repository=profile.repository,
            owner_github_id=profile.owner.github_id,
            checklist_digest=review.checklist_digest,
            production=review.checklist.production,
            candidate=review.checklist.candidate,
            client_decision_record_id=client_decision_id,
            source=source,
            reason=reason,
            decision_reference=decision_reference,
            actor=actor,
            decided_at=now,
        )
        replacement = profile.model_copy(update={"release_request": request, "updated_at": now})
        result = store.compare_and_write_product_profile_record(
            expected_record=profile, replacement_record=replacement
        )
        if result is not None and (
            not isinstance(result, ProductProfileCompareWriteResult) or result.status != "written"
        ):
            raise ValueError("The product changed before the release request was recorded.")
        return replacement


def request_release_review(
    *,
    store: ReleaseReviewStore,
    profile: LaunchplaneProductProfileRecord,
    review: ReleaseReviewStatus,
    control_plane_root: Path,
    source: ReleaseRequestSource,
    reason: str,
    decision_reference: str,
    actor: str,
) -> bool:
    saved = record_release_request(
        store=store,
        profile=profile,
        review=review,
        source=source,
        reason=reason,
        decision_reference=decision_reference,
        actor=actor,
    )
    return publish_release_invitation(
        store=store,
        profile=saved,
        control_plane_root=control_plane_root,
        send_reminder=False,
    )
