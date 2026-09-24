"""Owner-supplied credentials are encrypted submissions, never runtime bindings."""

import hashlib
import json

from control_plane import secrets
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
    ProductSecretConfigRequirement,
)
from control_plane.contracts.secret_record import SecretAuditEvent, SecretRecord, SecretVersion
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.workflows.ship import utc_now_timestamp

OWNER_SUBMISSION_INTEGRATION = "owner_secret_submission"


class OwnerSecretSubmissionUnavailable(ValueError):
    """The requested submission must be refreshed before operator application."""


def requested_owner_secrets(
    profile: LaunchplaneProductProfileRecord, lane: ProductLaneProfile
) -> tuple[ProductSecretConfigRequirement, ...]:
    return tuple(
        requirement
        for requirement in profile.expected_config.managed_secret_bindings
        if requirement.owner_input is not None
        and requirement.context == lane.context
        and (not requirement.instance or requirement.instance == lane.instance)
    )


def owner_secret_environments(
    profile: LaunchplaneProductProfileRecord, requirement: ProductSecretConfigRequirement
) -> tuple[str, ...]:
    return tuple(
        sorted(
            lane.instance
            for lane in profile.lanes
            if lane.context == requirement.context
            and (not requirement.instance or requirement.instance == lane.instance)
        )
    )


def owner_secret_request_revision(
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    requirement: ProductSecretConfigRequirement,
) -> str:
    payload = {
        "product": profile.product,
        "owner_github_id": profile.owner.github_id,
        "context": lane.context,
        "environments": owner_secret_environments(profile, requirement),
        "requirement": requirement.model_dump(mode="json"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def owner_submission_record(
    store: PostgresRecordStore,
    *,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    requirement: ProductSecretConfigRequirement,
) -> SecretRecord | None:
    return store.find_secret_record(
        scope="context_instance" if requirement.instance else "context",
        integration=OWNER_SUBMISSION_INTEGRATION,
        name=owner_secret_request_revision(profile, lane, requirement),
        context=lane.context,
        instance=requirement.instance,
    )


def store_owner_secret_submission(
    store: PostgresRecordStore,
    *,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    requirement: ProductSecretConfigRequirement,
    value: str,
) -> None:
    revision = owner_secret_request_revision(profile, lane, requirement)
    existing = owner_submission_record(store, profile=profile, lane=lane, requirement=requirement)
    secret_id = secrets.expected_secret_id(
        integration=OWNER_SUBMISSION_INTEGRATION,
        name=revision,
        context=lane.context,
        instance=requirement.instance,
    )
    now = utc_now_timestamp()
    actor = f"github:{profile.owner.github_id}"
    ciphertext, key_id = secrets._encrypt_secret_value(value)
    version = SecretVersion(
        version_id=secrets._version_id(secret_id=secret_id),
        secret_id=secret_id,
        created_at=now,
        created_by=actor,
        key_id=key_id,
        ciphertext=ciphertext,
    )
    record = SecretRecord(
        secret_id=secret_id,
        scope="context_instance" if requirement.instance else "context",
        integration=OWNER_SUBMISSION_INTEGRATION,
        name=revision,
        context=lane.context,
        instance=requirement.instance,
        current_version_id=version.version_id,
        created_at=existing.created_at if existing else now,
        updated_at=now,
        updated_by=actor,
    )
    event = SecretAuditEvent(
        event_id=secrets._audit_event_id(secret_id=secret_id, event_type="created"),
        secret_id=secret_id,
        event_type="rotated" if existing else "created",
        recorded_at=now,
        actor=actor,
        detail="Owner submitted a requested credential; no runtime binding was changed.",
        metadata={
            "product": profile.product,
            "request_revision": revision,
            "submission_version_id": version.version_id,
        },
    )
    store.write_product_authority_bundle(
        ProductAuthorityBundle(
            expected_product_profiles=(profile,),
            secret_records=(record,),
            secret_versions=(version,),
            secret_audit_events=(event,),
        )
    )


def owner_submission_receipt(
    store: PostgresRecordStore, record: SecretRecord, *, owner_github_id: str
) -> SecretAuditEvent | None:
    events = store.list_secret_audit_events(secret_id=record.secret_id)
    submissions = {
        event.metadata["submission_version_id"]: event
        for event in events
        if event.actor == f"github:{owner_github_id}"
        and event.metadata.get("request_revision") == record.name
        and event.metadata.get("submission_version_id")
    }
    rotations = {
        event.metadata["new_version_id"]: event.metadata["old_version_id"]
        for event in events
        if event.metadata.get("rotation_plan_digest")
        and event.metadata.get("new_version_id")
        and event.metadata.get("old_version_id")
    }
    version_id = record.current_version_id
    seen: set[str] = set()
    while version_id and version_id not in seen:
        if version_id in submissions:
            return submissions[version_id]
        seen.add(version_id)
        version_id = rotations.get(version_id, "")
    return None


def resolve_owner_secret_submission(
    store: PostgresRecordStore,
    *,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    requirement: ProductSecretConfigRequirement,
    version_id: str,
) -> str:
    """Called only after the existing operator config authorization succeeds."""
    if requirement not in requested_owner_secrets(profile, lane) or not profile.owner.is_set:
        raise OwnerSecretSubmissionUnavailable(
            "This credential is not requested from the current Owner."
        )
    record = owner_submission_record(store, profile=profile, lane=lane, requirement=requirement)
    if record is None or record.status != "configured" or record.current_version_id != version_id:
        raise OwnerSecretSubmissionUnavailable(
            "The Owner submission changed; refresh and run a new dry-run."
        )
    version = store.read_secret_version(version_id)
    if (
        version.secret_id != record.secret_id
        or owner_submission_receipt(store, record, owner_github_id=profile.owner.github_id) is None
    ):
        raise OwnerSecretSubmissionUnavailable(
            "The Owner submission does not match the requested credential."
        )
    return secrets._decrypt_secret_value(version.ciphertext, version.key_id)
