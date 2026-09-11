from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, cast
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent_custody import OrdinaryAgentCustodyCandidate
from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentClaimedJob,
    OrdinaryAgentJobClaimFence,
    OrdinaryAgentProviderQuotaKey,
    OrdinaryAgentProviderWaitRecord,
    OrdinaryAgentQualificationAttemptRecord,
    OrdinaryAgentQualificationReadCustodyReservation,
)
from control_plane.contracts.ordinary_agent_qualification import (
    OrdinaryAgentQualificationSetup,
    OrdinaryRepositoryAdminObservation,
    qualification_identity,
)
from control_plane.contracts.ordinary_agent_snapshot import OrdinaryAgentProviderRequestCounts
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentQualificationFiniteRequestV2,
)
from control_plane.ordinary_agent_qualification_job import (
    _completed_disposition,
    advance_ordinary_agent_qualification_job,
)
from control_plane.ordinary_agent_read_transport import ordinary_agent_read_failure_reason
from control_plane.ordinary_agent_github_transport import (
    OrdinaryAgentProviderDeferred,
    require_installation_provider_ready,
)
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied


class _Store:
    def __init__(
        self,
        *,
        authority_error: str | None = None,
        custody_error: str | None = None,
        result_denial: str | None = None,
        readiness_error_at: int | None = None,
        existing_attempt: OrdinaryAgentQualificationAttemptRecord | None = None,
        provider_wait: OrdinaryAgentProviderWaitRecord | None = None,
    ) -> None:
        self.authority_error = authority_error
        self.custody_error = custody_error
        self.result_denial = result_denial
        self.received_claim: OrdinaryAgentJobClaimFence | None = None
        self.failure_calls = 0
        self.readiness_error_at = readiness_error_at
        self.readiness_calls = 0
        self.existing_attempt = existing_attempt
        self.provider_wait = provider_wait
        self.received_setups: list[object | None] = []

    def reserve_ordinary_agent_qualification_attempt(
        self, *, claim_fence: object, setup: object | None
    ) -> OrdinaryAgentQualificationAttemptRecord:
        self.received_setups.append(setup)
        if self.existing_attempt is not None:
            return self.existing_attempt
        if setup is None:
            raise OrdinaryAgentSessionAdmissionDenied("qualification_setup_required")
        return _attempt()

    def reserve_ordinary_agent_qualification_custody_attempt(
        self,
        *,
        claim_fence: OrdinaryAgentJobClaimFence,
        attempt_id: str,
        expected_attempt_revision: int,
    ) -> OrdinaryAgentQualificationReadCustodyReservation:
        self.received_claim = claim_fence
        if self.custody_error is not None:
            raise OrdinaryAgentSessionAdmissionDenied(self.custody_error)
        return _reservation()

    def require_ordinary_agent_qualification_read_authority(
        self, *, claim_fence: OrdinaryAgentJobClaimFence, attempt_id: str
    ) -> None:
        if self.authority_error == "qualification_read_authority_lost":
            raise OrdinaryAgentSessionAdmissionDenied("qualification_read_authority_lost")

    def require_ordinary_agent_qualification_runtime_readiness(
        self, *, claim_fence: OrdinaryAgentJobClaimFence, attempt_id: str
    ) -> None:
        self.readiness_calls += 1
        if self.readiness_calls == self.readiness_error_at:
            raise OrdinaryAgentSessionAdmissionDenied("activation_not_current")

    def record_ordinary_agent_qualification_failure(
        self, **kwargs: object
    ) -> OrdinaryAgentQualificationAttemptRecord:
        self.failure_calls += 1
        return _attempt().model_copy(update={"state": "incomplete", "next_due_at": 120})

    def record_ordinary_agent_qualification_result(self, **kwargs: object) -> object:
        if self.result_denial is not None:
            raise OrdinaryAgentSessionAdmissionDenied(self.result_denial)
        return _attempt()

    def record_provider_wait(self, **kwargs: object) -> object:
        raise AssertionError("no provider request should record a wait")

    def read_provider_wait(self, **kwargs: object) -> OrdinaryAgentProviderWaitRecord | None:
        return self.provider_wait

    def read_ordinary_agent_custody_issue_attempt(self, attempt_id: str) -> object:
        del attempt_id
        return SimpleNamespace(state="closed")


def _request() -> OrdinaryAgentQualificationFiniteRequestV2:
    return OrdinaryAgentQualificationFiniteRequestV2(
        request_id="qualification-request",
        idempotency_key="qualification-request",
        principal_id="agent_one",
        session_id="session_one",
        lease_id="lease_one",
        target=OrdinaryAgentTarget(
            repository_id=1, repository="owner/repository", base_branch="main"
        ),
        admitted_at=1,
        expires_at=200,
    )


def _setup() -> OrdinaryAgentQualificationSetup:
    return OrdinaryAgentQualificationSetup(
        source_activation_operation_id="activation-1",
        source_activation_binding_sha256="a" * 64,
        target=_request().target,
        managed_set_id="ordinary-agent.qualification",
        managed_rule_id="agent.qualification.main",
        administrator_github_id=42,
        administrator_login="administrator",
        administrator_login_normalized="administrator",
        attestation_expires_at=190,
    )


def _attempt() -> OrdinaryAgentQualificationAttemptRecord:
    return OrdinaryAgentQualificationAttemptRecord(
        attempt_id="qualification-read-attempt",
        request_id=_request().request_id,
        binding_revision=1,
        scope_sha256=_request().scope_sha256,
        principal_id="agent_one",
        credential_id="credential_one",
        credential_version=1,
        attempt_ordinal=1,
        setup=_setup(),
        custody_record_id="custody-record",
        custody_sha256="b" * 64,
        repository_inventory_record_id="inventory-record",
        repository_inventory_revision=1,
        repository_inventory_digest="c" * 64,
        github_app_id=1,
        github_installation_id=2,
        managed_secret_binding_id="secret-binding",
        managed_secret_id="secret-record",
        managed_secret_version_id="secret-version",
        provider_inspection_sha256="d" * 64,
        installed_permission_ceiling_sha256="e" * 64,
        read_profile_sha256="f" * 64,
        created_at=1,
        updated_at=1,
    )


def _reservation() -> OrdinaryAgentQualificationReadCustodyReservation:
    return OrdinaryAgentQualificationReadCustodyReservation(
        read_attempt_id=_attempt().attempt_id,
        attempt_revision=2,
        custody_attempt_id="custody-attempt",
        custody_ordinal=1,
        idempotency_key="qualification-custody-key",
        candidate=OrdinaryAgentCustodyCandidate(
            principal_id="agent_one",
            repository_id=1,
            repository="owner/repository",
            base_branch="main",
            credential_id="credential_one",
            credential_version=1,
            secret_id="secret-record",
            secret_binding_id="secret-binding",
            secret_version_id="secret-version",
            expected_app_id=1,
            expected_installation_id=2,
            effect_profile="merge_train_snapshot",
        ),
    )


def _not_admin_observation() -> OrdinaryRepositoryAdminObservation:
    body = {
        "schema_version": 1,
        "status": "administrator_not_admin",
        "expected": qualification_identity(github_id=42, login="administrator").model_dump(
            mode="json"
        ),
        "observed": qualification_identity(github_id=42, login="administrator").model_dump(
            mode="json"
        ),
        "entry_count": 1,
        "counts": OrdinaryAgentProviderRequestCounts(
            rest_core_requests=1, graphql_requests=0, graphql_points=0
        ).model_dump(mode="json"),
        "observed_at": 10,
    }
    return OrdinaryRepositoryAdminObservation.model_validate(
        {**body, "observation_sha256": canonical_json_sha256(body)}
    )


class OrdinaryAgentQualificationJobTests(unittest.TestCase):
    def claimed(self) -> OrdinaryAgentClaimedJob:
        return OrdinaryAgentClaimedJob(
            request=_request(),
            claim_fence=OrdinaryAgentJobClaimFence(
                request_id=_request().request_id, worker_id="worker_one", generation=7
            ),
            claim_expires_at=100,
        )

    def test_custody_reservation_receives_original_claim_generation(self) -> None:
        store = _Store(custody_error="job_claim_lost")
        disposition = advance_ordinary_agent_qualification_job(
            claimed=self.claimed(), store=cast(Any, store), setup_resolver=lambda **_: _setup()
        )
        self.assertEqual(disposition.reason_code, "job_claim_lost")
        self.assertEqual(store.received_claim, self.claimed().claim_fence)

    def test_fresh_attempt_resolves_setup_only_after_history_reports_none(self) -> None:
        store = _Store(custody_error="job_claim_lost")
        resolved = 0

        def resolve(**_: object) -> OrdinaryAgentQualificationSetup:
            nonlocal resolved
            resolved += 1
            return _setup()

        advance_ordinary_agent_qualification_job(
            claimed=self.claimed(), store=cast(Any, store), setup_resolver=resolve
        )

        self.assertEqual(store.received_setups, [None, _setup()])
        self.assertEqual(resolved, 1)

    def test_existing_completed_history_needs_no_fresh_setup_or_provider_get(self) -> None:
        observation = _not_admin_observation()
        attempt = _attempt().model_copy(
            update={
                "state": "completed",
                "custody_attempt_ids": ("custody-attempt",),
                "result": observation,
                "reason_code": observation.status,
            }
        )
        store = _Store(existing_attempt=attempt)

        with patch(
            "control_plane.ordinary_agent_qualification_job.observe_repository_administrator"
        ) as provider_get:
            disposition = advance_ordinary_agent_qualification_job(
                claimed=self.claimed(),
                store=cast(Any, store),
                setup_resolver=lambda **_: (_ for _ in ()).throw(
                    AssertionError("existing history must not resolve fresh setup")
                ),
            )

        self.assertEqual(disposition.status, "blocked")
        self.assertEqual(disposition.reason_code, "administrator_not_admin")
        self.assertEqual(store.received_setups, [None])
        provider_get.assert_not_called()

    def test_restored_maintenance_denial_is_bounded_without_fresh_setup_or_get(self) -> None:
        restored = _attempt().model_copy(
            update={
                "state": "incomplete",
                "custody_attempt_ids": ("custody-attempt",),
                "reason_code": "cleanup_unknown",
            }
        )
        store = _Store(existing_attempt=restored, custody_error="activation_not_current")
        observed = datetime(2030, 1, 1, tzinfo=timezone.utc)

        with patch(
            "control_plane.ordinary_agent_qualification_job.observe_repository_administrator"
        ) as provider_get:
            disposition = advance_ordinary_agent_qualification_job(
                claimed=self.claimed(),
                store=cast(Any, store),
                setup_resolver=lambda **_: (_ for _ in ()).throw(
                    AssertionError("restored history must not resolve fresh setup")
                ),
                utc_now=lambda: observed,
            )

        self.assertEqual(disposition.status, "waiting")
        self.assertEqual(disposition.next_due_at, int(observed.timestamp()) + 30)
        self.assertEqual(disposition.reason_code, "activation_not_current")
        self.assertEqual(store.received_setups, [None])
        provider_get.assert_not_called()

    def test_setup_resolution_denial_is_a_finite_wait(self) -> None:
        store = _Store()
        observed = datetime(2030, 1, 1, tzinfo=timezone.utc)

        disposition = advance_ordinary_agent_qualification_job(
            claimed=self.claimed(),
            store=cast(Any, store),
            setup_resolver=lambda **_: (_ for _ in ()).throw(
                OrdinaryAgentSessionAdmissionDenied("activation_expired")
            ),
            utc_now=lambda: observed,
        )

        self.assertEqual(disposition.status, "waiting")
        self.assertEqual(disposition.next_due_at, int(observed.timestamp()) + 30)
        self.assertEqual(disposition.reason_code, "activation_expired")

    def test_revocation_after_mint_denies_get_and_closes_custody_context(self) -> None:
        store = _Store(authority_error="qualification_read_authority_lost")
        context_closed = False

        @contextmanager
        def minted(**kwargs: object) -> Iterator[object]:
            nonlocal context_closed

            class Lease:
                class Token:
                    token = "provider-token"
                    installation_id = 2
                    expires_at = "2030-01-01T00:00:00Z"

                installation_token = Token()

            try:
                yield Lease()
            finally:
                context_closed = True

        def no_get(**kwargs: object) -> object:
            raise AssertionError("revoked authority must deny the collaborator GET")

        with (
            patch(
                "control_plane.ordinary_agent_qualification_job.ordinary_agent_provider_token_lease",
                minted,
            ),
            patch(
                "control_plane.ordinary_agent_qualification_job.observe_repository_administrator",
                no_get,
            ),
        ):
            disposition = advance_ordinary_agent_qualification_job(
                claimed=self.claimed(), store=cast(Any, store), setup_resolver=lambda **_: _setup()
            )
        self.assertTrue(context_closed)
        self.assertEqual(disposition.status, "waiting")
        self.assertIsNotNone(disposition.next_due_at)
        self.assertEqual(disposition.reason_code, "qualification_read_authority_lost")
        self.assertEqual(store.failure_calls, 0)

    def test_pre_mint_readiness_denial_is_not_recorded_as_provider_failure(self) -> None:
        store = _Store(readiness_error_at=1)
        observed = datetime(2030, 1, 1, tzinfo=timezone.utc)

        @contextmanager
        def denied_before_mint(**kwargs: object) -> Iterator[object]:
            callback = kwargs["before_token_mint"]
            assert callable(callback)
            callback(1, 2)
            raise AssertionError("readiness denial must prevent token mint")
            yield

        with patch(
            "control_plane.ordinary_agent_qualification_job.ordinary_agent_provider_token_lease",
            denied_before_mint,
        ):
            disposition = advance_ordinary_agent_qualification_job(
                claimed=self.claimed(),
                store=cast(Any, store),
                setup_resolver=lambda **_: _setup(),
                utc_now=lambda: observed,
            )

        self.assertEqual(disposition.status, "waiting")
        self.assertEqual(disposition.next_due_at, int(observed.timestamp()) + 30)
        self.assertEqual(disposition.reason_code, "activation_not_current")
        self.assertEqual(store.failure_calls, 0)

    def test_pre_get_readiness_denial_preserves_reason_without_provider_failure(self) -> None:
        store = _Store(readiness_error_at=2)
        observed = datetime(2030, 1, 1, tzinfo=timezone.utc)

        @contextmanager
        def minted(**kwargs: object) -> Iterator[object]:
            callback = kwargs["before_token_mint"]
            assert callable(callback)
            callback(1, 2)

            class Lease:
                class Token:
                    token = "provider-token"
                    installation_id = 2
                    expires_at = "2030-01-01T00:00:00Z"

                installation_token = Token()

            yield Lease()

        with (
            patch(
                "control_plane.ordinary_agent_qualification_job.ordinary_agent_provider_token_lease",
                minted,
            ),
            patch(
                "control_plane.ordinary_agent_qualification_job.observe_repository_administrator"
            ) as provider_get,
        ):
            disposition = advance_ordinary_agent_qualification_job(
                claimed=self.claimed(),
                store=cast(Any, store),
                setup_resolver=lambda **_: _setup(),
                utc_now=lambda: observed,
            )

        self.assertEqual(disposition.status, "waiting")
        self.assertEqual(disposition.next_due_at, int(observed.timestamp()) + 30)
        self.assertEqual(disposition.reason_code, "activation_not_current")
        self.assertEqual(store.failure_calls, 0)
        provider_get.assert_not_called()

    def test_fenced_positive_attempt_never_completes_after_closed_custody(self) -> None:
        attempt = _attempt().model_copy(
            update={
                "state": "fenced",
                "reason_code": "cleanup_unknown",
                "custody_attempt_ids": ("custody-attempt",),
            }
        )
        store = SimpleNamespace(
            read_ordinary_agent_custody_issue_attempt=lambda _: SimpleNamespace(state="closed")
        )
        disposition = _completed_disposition(store=cast(Any, store), attempt=attempt, retry_at=100)
        self.assertEqual(disposition.status, "reconciliation_required")
        self.assertEqual(disposition.next_due_at, 100)
        self.assertEqual(disposition.reason_code, "cleanup_unknown")

    def test_completed_attempt_with_open_custody_remains_paced(self) -> None:
        attempt = _attempt().model_copy(
            update={"state": "completed", "custody_attempt_ids": ("custody-attempt",)}
        )
        store = SimpleNamespace(
            read_ordinary_agent_custody_issue_attempt=lambda _: SimpleNamespace(
                state="issued", residual_expires_at="2030-01-01T00:10:00+00:00"
            )
        )

        disposition = _completed_disposition(
            store=cast(Any, store), attempt=attempt, retry_at=1_893_456_030
        )

        self.assertEqual(disposition.status, "waiting")
        self.assertEqual(disposition.next_due_at, 1_893_456_600)
        self.assertEqual(disposition.reason_code, "read_custody_fenced")

    def test_store_result_denial_is_paced_without_provider_failure(self) -> None:
        store = _Store(result_denial="qualification_provenance_conflict")

        @contextmanager
        def minted(**kwargs: object) -> Iterator[object]:
            class Lease:
                class Token:
                    token = "provider-token"
                    installation_id = 2
                    expires_at = "2030-01-01T00:00:00Z"

                installation_token = Token()

            yield Lease()

        with (
            patch(
                "control_plane.ordinary_agent_qualification_job.ordinary_agent_provider_token_lease",
                minted,
            ),
            patch(
                "control_plane.ordinary_agent_qualification_job.observe_repository_administrator",
                return_value=object(),
            ),
        ):
            disposition = advance_ordinary_agent_qualification_job(
                claimed=self.claimed(), store=cast(Any, store), setup_resolver=lambda **_: _setup()
            )
        self.assertEqual(disposition.status, "waiting")
        self.assertIsNotNone(disposition.next_due_at)
        self.assertEqual(disposition.reason_code, "qualification_provenance_conflict")
        self.assertEqual(store.failure_calls, 0)

    def test_active_provider_wait_preserves_wait_classification(self) -> None:
        wait = OrdinaryAgentProviderWaitRecord(
            quota_key=OrdinaryAgentProviderQuotaKey(
                authority_kind="installation", authority_id=2, resource_class="core"
            ),
            retry_not_before=200,
            observed_at=100,
            classification="primary_rate_limit",
        )
        with self.assertRaises(OrdinaryAgentProviderDeferred) as raised:
            require_installation_provider_ready(
                app_id=1,
                installation_id=2,
                resource_classes=("core",),
                read_provider_wait=lambda quota_key: (
                    wait if quota_key.authority_kind == "installation" else None
                ),
                utc_now=lambda: datetime.fromtimestamp(100, timezone.utc),
            )
        self.assertEqual(ordinary_agent_read_failure_reason(raised.exception), "provider_wait")

    def test_cached_provider_wait_defers_before_custody_without_failure_history(self) -> None:
        wait = OrdinaryAgentProviderWaitRecord(
            quota_key=OrdinaryAgentProviderQuotaKey(
                authority_kind="installation", authority_id=2, resource_class="core"
            ),
            retry_not_before=240,
            observed_at=100,
            classification="primary_rate_limit",
        )
        store = _Store(provider_wait=wait)

        disposition = advance_ordinary_agent_qualification_job(
            claimed=self.claimed(),
            store=cast(Any, store),
            setup_resolver=lambda **_: _setup(),
            utc_now=lambda: datetime.fromtimestamp(100, timezone.utc),
        )

        self.assertEqual(disposition.status, "waiting")
        self.assertEqual(disposition.next_due_at, 240)
        self.assertEqual(disposition.reason_code, "provider_wait")
        self.assertIsNone(store.received_claim)
        self.assertEqual(store.failure_calls, 0)
