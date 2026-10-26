from collections.abc import Callable
from datetime import datetime
from control_plane.contracts.privileged_operation import (
    ManagedSecretReencryptionPlanInput,
    PrivilegedOperationActor,
    PrivilegedOperationConflictError,
    build_privileged_operation_id,
)
from control_plane.privileged_operation_service import (
    DEFAULT_PRIVILEGED_OPERATION_TTL_SECONDS,
    PrivilegedOperationWriteResult,
    _utc_now,
    create_typed_privileged_operation_plan,
)


def create_privileged_operation_plan(
    *,
    record_store: object,
    requester_github_id: int,
    requester_login: str,
    source_event_id: str,
    request: ManagedSecretReencryptionPlanInput,
    expires_in_seconds: int = DEFAULT_PRIVILEGED_OPERATION_TTL_SECONDS,
    now: Callable[[], datetime] = _utc_now,
) -> PrivilegedOperationWriteResult:

    actor = PrivilegedOperationActor(
        identity_type="github_human",
        github_id=requester_github_id,
        login=requester_login,
    )
    expected_operation_id = build_privileged_operation_id(
        github_id=requester_github_id,
        source_event_id=source_event_id,
    )
    result = create_typed_privileged_operation_plan(
        record_store=record_store,
        descriptor_id="managed-secret-reencryption",
        actor=actor,
        source_kind="browser_api",
        source_event_id=source_event_id,
        request=request,
        expires_in_seconds=expires_in_seconds,
        now=now,
    )
    if result.record.operation_id != expected_operation_id:
        raise PrivilegedOperationConflictError(
            "Managed-secret privileged-operation identity compatibility was not preserved."
        )
    return result
