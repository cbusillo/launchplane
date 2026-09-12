"""Demand-triggered provider inspection orchestration for ordinary delivery."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import math
import time
from typing import Literal, Protocol, TypeAlias

from control_plane.contracts.ordinary_agent_client import OrdinaryAgentFiniteClientRequest
from control_plane.contracts.provider_delivery_readiness import (
    PROVIDER_INSPECTION_CLEANUP_SECONDS,
    PROVIDER_INSPECTION_MAX_RETRY_AFTER_SECONDS,
    ProviderDeliveryInspectionAttemptV1,
    ProviderDeliveryInspectionReservationV1,
    ProviderDeliveryReadinessDecision,
    ProviderDeliveryReadinessReason,
    ProviderDeliveryReadinessReceiptV1,
)
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.ordinary_agent_authentication import OrdinaryAgentTokenProof
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.provider_delivery_inspection_github import (
    ProviderDeliveryInspectionCapabilityError,
    inspect_provider_delivery_protection,
)
from control_plane.provider_delivery_inspection_profile import (
    ProviderDeliveryInspectionProfileStore,
    ProviderDeliveryInspectionProfileError,
    ResolvedProviderDeliveryInspectionProfile,
    resolve_provider_delivery_inspection_profile,
)


ProviderDeliveryInspectionStart: TypeAlias = (
    ProviderDeliveryReadinessDecision | ProviderDeliveryInspectionReservationV1
)


class ProviderDeliveryReadinessStore(ProviderDeliveryInspectionProfileStore, Protocol):
    def precheck_provider_delivery_inspection_for_client(
        self, *, proof: OrdinaryAgentTokenProof, request: OrdinaryAgentFiniteClientRequest
    ) -> None: ...

    def precheck_provider_delivery_inspection_for_job(self, *, request_id: str) -> None: ...

    def reserve_provider_delivery_inspection_for_client(
        self,
        *,
        proof: OrdinaryAgentTokenProof,
        request: OrdinaryAgentFiniteClientRequest,
        profile: ResolvedProviderDeliveryInspectionProfile,
    ) -> ProviderDeliveryInspectionStart: ...

    def reserve_provider_delivery_inspection_for_job(
        self,
        *,
        request_id: str,
        profile: ResolvedProviderDeliveryInspectionProfile,
    ) -> ProviderDeliveryInspectionStart: ...

    def mark_provider_delivery_inspection_minting(
        self,
        *,
        attempt_id: str,
        expected_revision: int,
        app_id: int,
        installation_id: int,
    ) -> ProviderDeliveryInspectionAttemptV1: ...

    def mark_provider_delivery_inspection_issued(
        self,
        *,
        attempt_id: str,
        expected_revision: int,
        app_id: int,
        installation_id: int,
        repository_id: int,
        token_expires_at: int,
    ) -> ProviderDeliveryInspectionAttemptV1: ...

    def mark_provider_delivery_inspection_issue_unknown(
        self, *, attempt_id: str, expected_revision: int
    ) -> ProviderDeliveryInspectionAttemptV1: ...

    def close_provider_delivery_inspection_without_token(
        self, *, attempt_id: str, expected_revision: int
    ) -> ProviderDeliveryInspectionAttemptV1: ...

    def close_provider_delivery_inspection_custody(
        self,
        *,
        attempt_id: str,
        expected_revision: int,
        outcome: Literal["confirmed_revoked", "cleanup_unknown"],
    ) -> ProviderDeliveryInspectionAttemptV1: ...

    def finish_provider_delivery_inspection(
        self,
        *,
        attempt_id: str,
        expected_revision: int,
        result: object | None = None,
        capability_reason: ProviderDeliveryReadinessReason | None = None,
        retry_not_before: int | None = None,
        proof: OrdinaryAgentTokenProof | None = None,
        client_request: OrdinaryAgentFiniteClientRequest | None = None,
    ) -> ProviderDeliveryReadinessDecision: ...


def ensure_provider_delivery_readiness_for_client(
    *,
    store: ProviderDeliveryReadinessStore,
    proof: OrdinaryAgentTokenProof,
    request: OrdinaryAgentFiniteClientRequest,
    inspect: Callable[..., object] = inspect_provider_delivery_protection,
    wall_time: Callable[[], float] = time.time,
) -> ProviderDeliveryReadinessReceiptV1:
    store.precheck_provider_delivery_inspection_for_client(proof=proof, request=request)
    try:
        profile = resolve_provider_delivery_inspection_profile(record_store=store)
    except ProviderDeliveryInspectionProfileError as error:
        raise OrdinaryAgentSessionAdmissionDenied(
            "provider_inspection_profile_unavailable"
        ) from error
    start = store.reserve_provider_delivery_inspection_for_client(
        proof=proof, request=request, profile=profile
    )
    return _run_or_select(
        store=store,
        start=start,
        profile=profile,
        inspect=inspect,
        wall_time=wall_time,
        client_proof=proof,
        client_request=request,
    )


def ensure_provider_delivery_readiness_for_job(
    *,
    store: ProviderDeliveryReadinessStore,
    request_id: str,
    inspect: Callable[..., object] = inspect_provider_delivery_protection,
    wall_time: Callable[[], float] = time.time,
) -> ProviderDeliveryReadinessReceiptV1:
    store.precheck_provider_delivery_inspection_for_job(request_id=request_id)
    try:
        profile = resolve_provider_delivery_inspection_profile(record_store=store)
    except ProviderDeliveryInspectionProfileError as error:
        raise OrdinaryAgentSessionAdmissionDenied(
            "provider_inspection_profile_unavailable"
        ) from error
    start = store.reserve_provider_delivery_inspection_for_job(
        request_id=request_id, profile=profile
    )
    return _run_or_select(
        store=store,
        start=start,
        profile=profile,
        inspect=inspect,
        wall_time=wall_time,
    )


def _run_or_select(
    *,
    store: ProviderDeliveryReadinessStore,
    start: ProviderDeliveryInspectionStart,
    profile: ResolvedProviderDeliveryInspectionProfile,
    inspect: Callable[..., object],
    wall_time: Callable[[], float],
    client_proof: OrdinaryAgentTokenProof | None = None,
    client_request: OrdinaryAgentFiniteClientRequest | None = None,
) -> ProviderDeliveryReadinessReceiptV1:
    if isinstance(start, ProviderDeliveryReadinessDecision):
        return _require_ready(start)
    attempt = start.attempt

    def remaining_provider_seconds() -> float:
        return max(0.0, attempt.dispatch_deadline - wall_time())

    def remaining_cleanup_seconds() -> float:
        cleanup_deadline = attempt.dispatch_deadline + PROVIDER_INSPECTION_CLEANUP_SECONDS
        return max(0.0, cleanup_deadline - wall_time())

    def before_token_mint(app_id: int, installation_id: int) -> None:
        nonlocal attempt
        attempt = store.mark_provider_delivery_inspection_minting(
            attempt_id=attempt.attempt_id,
            expected_revision=attempt.revision,
            app_id=app_id,
            installation_id=installation_id,
        )

    def token_issued(token: GitHubAppInstallationToken) -> None:
        nonlocal attempt
        attempt = store.mark_provider_delivery_inspection_issued(
            attempt_id=attempt.attempt_id,
            expected_revision=attempt.revision,
            app_id=token.app_id,
            installation_id=token.installation_id,
            repository_id=token.repository_id,
            token_expires_at=int(datetime.fromisoformat(token.expires_at).timestamp()),
        )

    def token_cleanup(outcome: Literal["confirmed_revoked", "cleanup_unknown"]) -> None:
        nonlocal attempt
        attempt = store.close_provider_delivery_inspection_custody(
            attempt_id=attempt.attempt_id,
            expected_revision=attempt.revision,
            outcome=outcome,
        )

    try:
        result = inspect(
            profile=profile,
            repository=attempt.binding.target.repository,
            repository_id=attempt.binding.target.repository_id,
            repository_owner_id=attempt.binding.repository_owner_id,
            base_branch=attempt.binding.target.base_branch,
            ordinary_delivery_app_id=attempt.binding.ordinary_delivery_app_id,
            expectation=start.expectation,
            remaining_provider_seconds=remaining_provider_seconds,
            remaining_cleanup_seconds=remaining_cleanup_seconds,
            before_token_mint=before_token_mint,
            token_issued=token_issued,
            token_cleanup=token_cleanup,
        )
    except ProviderDeliveryInspectionCapabilityError as error:
        if attempt.custody_phase == "reserved":
            attempt = store.close_provider_delivery_inspection_without_token(
                attempt_id=attempt.attempt_id,
                expected_revision=attempt.revision,
            )
        elif attempt.custody_phase == "minting":
            attempt = store.mark_provider_delivery_inspection_issue_unknown(
                attempt_id=attempt.attempt_id,
                expected_revision=attempt.revision,
            )
        reason = _capability_reason(error.reason_code)
        decision = store.finish_provider_delivery_inspection(
            attempt_id=attempt.attempt_id,
            expected_revision=attempt.revision,
            capability_reason=reason,
            retry_not_before=error.retry_not_before,
            proof=client_proof,
            client_request=client_request,
        )
        return _require_ready(decision)
    decision = store.finish_provider_delivery_inspection(
        attempt_id=attempt.attempt_id,
        expected_revision=attempt.revision,
        result=result,
        proof=client_proof,
        client_request=client_request,
    )
    return _require_ready(decision)


def _capability_reason(reason_code: str) -> ProviderDeliveryReadinessReason:
    if reason_code == "provider_permission_denied":
        return "provider_inspection_permission_denied"
    if reason_code == "provider_attempt_deadline":
        return "provider_inspection_deadline"
    if reason_code == "cleanup_unknown":
        return "provider_inspection_custody_fenced"
    return "provider_wait"


def _require_ready(
    decision: ProviderDeliveryReadinessDecision,
) -> ProviderDeliveryReadinessReceiptV1:
    if decision.status == "ready":
        assert decision.receipt is not None
        return decision.receipt
    raise OrdinaryAgentSessionAdmissionDenied(
        decision.reason_code,
        retry_not_before=decision.retry_not_before,
        server_observed_at=decision.server_observed_at,
    )


def provider_delivery_retry_after_seconds(error: OrdinaryAgentSessionAdmissionDenied) -> int:
    if error.retry_not_before is None:
        return 1
    return max(
        1,
        min(
            PROVIDER_INSPECTION_MAX_RETRY_AFTER_SECONDS,
            math.ceil(error.retry_not_before - (error.server_observed_at or int(time.time()))),
        ),
    )
