"""Storage behavior for controller-free ordinary-agent qualification reads."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import event

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerStateRecord,
    build_merge_train_controller_key,
)
from control_plane.contracts.ordinary_agent_effect import (
    MAX_CUSTODY_MINT_ATTEMPTS_PER_DISPATCH_CHILD,
    MAX_SNAPSHOT_PROVIDER_ATTEMPTS,
    MIN_RECONCILIATION_BACKOFF_SECONDS,
    OrdinaryAgentClaimedJob,
    OrdinaryAgentJobAttemptDisposition,
    OrdinaryAgentJobClaimFence,
    OrdinaryAgentQualificationAttemptRecord,
    OrdinaryAgentQualificationReadCustodyReservation,
    parse_ordinary_agent_read_attempt,
)
from control_plane.contracts.ordinary_agent_custody import (
    GITHUB_TOKEN_MAXIMUM_LIFETIME_SECONDS,
    KNOWN_TOKEN_CLOCK_SKEW_SECONDS,
)
from control_plane.contracts.ordinary_agent_qualification import (
    OrdinaryAgentQualificationAttestation,
    OrdinaryAgentQualificationSetup,
    OrdinaryRepositoryAdminObservation,
    qualification_identity,
)
from control_plane.contracts.ordinary_agent_activation import (
    OrdinaryAgentDeliveryActivationExecutionEvidence,
    OrdinaryAgentDeliveryActivationRecord,
    OrdinaryAgentDeliveryActivationScope,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentJobBinding,
    OrdinaryAgentLeaseRecord,
    OrdinaryAgentQualificationFiniteRequestV2,
)
from control_plane.contracts.ordinary_agent_snapshot import OrdinaryAgentProviderRequestCounts
from control_plane.github_app_identity import (
    GitHubAppInstallationToken,
    ordinary_agent_effect_permissions,
)
from control_plane.ordinary_agent_session_approval import approve_existing_ordinary_agent_session
from control_plane.ordinary_agent_qualification_job import (
    advance_ordinary_agent_qualification_job,
)
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.storage.postgres import (
    LaunchplaneOrdinaryAgentFiniteRequestRow,
    LaunchplaneOrdinaryAgentJobClaimRow,
    LaunchplaneOrdinaryAgentLeaseRow,
    LaunchplaneOrdinaryAgentReadAttemptRow,
    LaunchplaneMergeTrainControllerStateRow,
    PostgresRecordStore,
)
from tests import test_ordinary_agent_session_storage as session_support
from tests import test_ordinary_agent_activation_storage as activation_support
from tests.support.ordinary_agent_lifecycle import replace_policy_without_ordinary_agent_rule


class QualificationStorageScenario:
    """Reuse the real session/enrollment fixture against SQLite or PostgreSQL."""

    def __init__(
        self,
        test_case: unittest.TestCase,
        *,
        store: PostgresRecordStore | None = None,
        request_id: str = "qualification-request-one",
    ) -> None:
        self.test_case = test_case
        self.fixture = session_support.OrdinaryAgentSessionStorageTests()
        if store is None:
            directory = TemporaryDirectory()
            test_case.addCleanup(directory.cleanup)
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory.name) / 'db.sqlite3'}"
            )
            test_case.addCleanup(store.close)
            store.ensure_schema()
        self.store = store
        self.fixture.prepare_store(store, installation_id=77)
        test_case.addCleanup(self.fixture.doCleanups)
        self.fixture.enroll()

        attenuation = self.fixture.attenuation.model_copy(
            update={
                "actions": ("preflight", "guarded_merge"),
                "action_limit": 4,
                "pull_request_limit": 1,
                "refresh_allowance": 0,
            }
        )
        operation_id = self.store.propose_ordinary_agent_session(
            proof=self.fixture.proof,
            operation_id=f"{request_id}-session",
            attenuation=attenuation,
        ).operation_id
        issued = approve_existing_ordinary_agent_session(
            store=self.store,
            manager=self.fixture.manager,
            cookie_header=self.fixture.manager.session_cookie_header(self.fixture.human),
            csrf_token=self.fixture.manager.csrf_token(self.fixture.human),
            principal_id="agent_one",
            operation_id=operation_id,
        )
        self.session = issued.session
        self.lease = next(item for item in issued.leases if item.action == "preflight")
        self.request = self.store.admit_ordinary_agent_finite_request(
            proof=self.fixture.proof,
            request=OrdinaryAgentQualificationFiniteRequestV2(
                request_id=request_id,
                idempotency_key=request_id,
                principal_id=issued.session.principal_id,
                session_id=issued.session.session_id,
                lease_id=self.lease.lease_id,
                target=self.lease.target,
                admitted_at=self.fixture.now,
                expires_at=self.fixture.now + 100,
                continuation_expires_at=self.fixture.now + 200,
            ),
        )
        self.setup = OrdinaryAgentQualificationSetup(
            source_activation_operation_id="activation-one",
            source_activation_binding_sha256="a" * 64,
            target=self.request.target,
            managed_set_id=self.lease.managed_set_id,
            managed_rule_id=self.lease.managed_rule_id,
            administrator_github_id=self.fixture.human.identity.github_id,
            administrator_login=self.fixture.human.identity.login,
            administrator_login_normalized=self.fixture.human.identity.login.casefold(),
            attestation_expires_at=self.fixture.now + 90,
        )
        # These tests isolate qualification attempt/custody persistence. Runtime
        # activation resolution has dedicated end-to-end storage coverage.
        self.readiness_patch = patch.object(
            self.store,
            "_require_ordinary_agent_runtime_readiness",
            return_value=(Mock(spec=OrdinaryAgentDeliveryActivationRecord), (), self.fixture.now),
        )
        setup_resolution = patch.object(
            self.store,
            "_ordinary_agent_qualification_setup_from_activation",
            return_value=self.setup,
        )
        self.readiness_patch.start()
        setup_resolution.start()
        test_case.addCleanup(self.readiness_patch.stop)
        test_case.addCleanup(setup_resolution.stop)

    def claim(
        self, *, worker_id: str = "qualification-worker", lease_seconds: int = 60
    ) -> OrdinaryAgentClaimedJob:
        claimed = self.store.claim_due_ordinary_agent_job(
            worker_id=worker_id, lease_seconds=lease_seconds
        )
        self.test_case.assertIsNotNone(claimed)
        assert claimed is not None
        self.test_case.assertEqual(claimed.request.request_id, self.request.request_id)
        return claimed

    def reserve_attempt(
        self, claimed: OrdinaryAgentClaimedJob | None = None
    ) -> OrdinaryAgentQualificationAttemptRecord:
        claimed = claimed or self.claim()
        return self.store.reserve_ordinary_agent_qualification_attempt(
            claim_fence=claimed.claim_fence, setup=self.setup
        )

    def reserve_custody(
        self,
        *,
        claimed: OrdinaryAgentClaimedJob,
        attempt: OrdinaryAgentQualificationAttemptRecord,
    ) -> OrdinaryAgentQualificationReadCustodyReservation:
        return self.store.reserve_ordinary_agent_qualification_custody_attempt(
            claim_fence=claimed.claim_fence,
            attempt_id=attempt.attempt_id,
            expected_attempt_revision=attempt.revision,
        )

    def issue(self, reservation: OrdinaryAgentQualificationReadCustodyReservation) -> None:
        acquired, _ = self.store.acquire_ordinary_agent_custody_issue_attempt(
            attempt_id=reservation.custody_attempt_id,
            idempotency_key_sha256=hashlib.sha256(reservation.idempotency_key.encode()).hexdigest(),
            request_sha256=canonical_json_sha256(
                {
                    "candidate": reservation.candidate.model_dump(mode="json"),
                    "request": reservation.request_payload,
                }
            ),
            candidate=reservation.candidate,
            requested_permissions=ordinary_agent_effect_permissions(
                reservation.candidate.effect_profile
            ),
            dispatch_window_seconds=30,
        )
        self.test_case.assertEqual(acquired, "acquired")
        installation_id = reservation.candidate.expected_installation_id
        assert installation_id is not None
        self.store.mark_ordinary_agent_custody_issued(
            attempt_id=reservation.custody_attempt_id,
            app_id=reservation.candidate.expected_app_id,
            installation_id=installation_id,
            token_expires_at=datetime.fromtimestamp(
                self.fixture.now + 300, timezone.utc
            ).isoformat(),
            residual_expires_at=datetime.fromtimestamp(
                self.fixture.now + 360, timezone.utc
            ).isoformat(),
        )

    def observation(self, *, status: str = "qualified") -> OrdinaryRepositoryAdminObservation:
        identity = qualification_identity(
            github_id=self.setup.administrator_github_id,
            login=self.setup.administrator_login,
        )
        body = {
            "schema_version": 1,
            "status": status,
            "expected": identity.model_dump(mode="json"),
            "observed": identity.model_dump(mode="json") if status == "qualified" else None,
            "entry_count": 1,
            "counts": OrdinaryAgentProviderRequestCounts(
                rest_core_requests=1, graphql_requests=0, graphql_points=0
            ).model_dump(mode="json"),
            "observed_at": self.fixture.now,
        }
        return OrdinaryRepositoryAdminObservation.model_validate(
            {**body, "observation_sha256": canonical_json_sha256(body)}
        )

    def attestation(
        self,
        *,
        attempt: OrdinaryAgentQualificationAttemptRecord,
        custody_attempt_id: str,
        observation: OrdinaryRepositoryAdminObservation,
        update: dict[str, object] | None = None,
    ) -> OrdinaryAgentQualificationAttestation:
        body: dict[str, object] = {
            "schema_version": 1,
            "request_id": attempt.request_id,
            "scope_sha256": attempt.scope_sha256,
            "binding_revision": attempt.binding_revision,
            "target": attempt.setup.target.model_dump(mode="json"),
            "lease_action": "preflight",
            "source_activation_operation_id": attempt.setup.source_activation_operation_id,
            "source_activation_binding_sha256": attempt.setup.source_activation_binding_sha256,
            "administrator": qualification_identity(
                github_id=attempt.setup.administrator_github_id,
                login=attempt.setup.administrator_login,
            ).model_dump(mode="json"),
            "principal_id": attempt.principal_id,
            "credential_id": attempt.credential_id,
            "credential_version": attempt.credential_version,
            "policy_managed_set_id": attempt.setup.managed_set_id,
            "policy_managed_rule_id": attempt.setup.managed_rule_id,
            "custody_record_id": attempt.custody_record_id,
            "custody_sha256": attempt.custody_sha256,
            "repository_inventory_record_id": attempt.repository_inventory_record_id,
            "repository_inventory_revision": attempt.repository_inventory_revision,
            "repository_inventory_digest": attempt.repository_inventory_digest,
            "github_app_id": attempt.github_app_id,
            "github_installation_id": attempt.github_installation_id,
            "managed_secret_binding_id": attempt.managed_secret_binding_id,
            "managed_secret_id": attempt.managed_secret_id,
            "managed_secret_version_id": attempt.managed_secret_version_id,
            "provider_inspection_sha256": attempt.provider_inspection_sha256,
            "installed_permission_ceiling_sha256": attempt.installed_permission_ceiling_sha256,
            "effect_profile": "merge_train_snapshot",
            "read_profile_sha256": attempt.read_profile_sha256,
            "custody_attempt_id": custody_attempt_id,
            "observation": observation.model_dump(mode="json"),
            "expires_at": attempt.setup.attestation_expires_at,
        }
        body.update(update or {})
        return OrdinaryAgentQualificationAttestation.model_validate(
            {**body, "attestation_sha256": canonical_json_sha256(body)}
        )

    def persisted_lease(self) -> OrdinaryAgentLeaseRecord:
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneOrdinaryAgentLeaseRow, self.lease.lease_id)
            assert row is not None
            return OrdinaryAgentLeaseRecord.model_validate(row.payload)

    def rebind(self, *, binding_revision: int) -> None:
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneOrdinaryAgentFiniteRequestRow, self.request.request_id)
            assert row is not None
            current = OrdinaryAgentQualificationFiniteRequestV2.model_validate(row.payload)
            self.request = current.model_copy(update={"binding_revision": binding_revision})
            row.payload = self.store._payload_dict(self.request)
            session.commit()

    def seed_matching_stale_controller(self) -> MergeTrainControllerStateRecord:
        controller = MergeTrainControllerStateRecord(
            ordinary_job_binding=OrdinaryAgentJobBinding(
                request_id=self.request.request_id,
                scope_sha256=self.request.scope_sha256,
                binding_revision=self.request.binding_revision,
            ),
            controller_key=build_merge_train_controller_key(
                repository=self.request.target.repository,
                base_branch=self.request.target.base_branch,
            ),
            repository=self.request.target.repository,
            base_branch=self.request.target.base_branch,
            policy_key="stale-policy",
            policy_sha256="b" * 64,
            status="running",
            updated_at="2026-09-10T00:00:00Z",
            lease_owner="stale-worker",
            lease_acquired_at="2026-09-10T00:00:00Z",
            lease_expires_at="2026-09-10T00:00:01Z",
            heartbeat_at="2026-09-10T00:00:00Z",
            active_action="head_refresh",
            active_phase="prepare",
        )
        with self.store._session_factory() as session:
            row = LaunchplaneMergeTrainControllerStateRow(controller_key=controller.controller_key)
            self.store._sync_merge_train_controller_state_row(row, controller)
            session.add(row)
            session.commit()
        return controller

    def record_positive(
        self, *, claimed: OrdinaryAgentClaimedJob
    ) -> tuple[
        OrdinaryAgentQualificationAttemptRecord,
        OrdinaryAgentQualificationReadCustodyReservation,
    ]:
        attempt = self.reserve_attempt(claimed)
        reservation = self.reserve_custody(claimed=claimed, attempt=attempt)
        self.issue(reservation)
        observation = self.observation()
        attestation = self.attestation(
            attempt=attempt,
            custody_attempt_id=reservation.custody_attempt_id,
            observation=observation,
        )
        completed = self.store.record_ordinary_agent_qualification_result(
            attempt_id=attempt.attempt_id,
            custody_attempt_id=reservation.custody_attempt_id,
            result=observation,
            attestation=attestation,
        )
        return completed, reservation


class OrdinaryAgentQualificationStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = QualificationStorageScenario(self)

    def test_first_attempt_charges_once_for_replay_retry_and_later_binding_revision(self) -> None:
        claimed = self.scenario.claim(lease_seconds=120)
        attempt = self.scenario.reserve_attempt(claimed)
        self.assertEqual(self.scenario.persisted_lease().budget.actions_used, 1)
        self.assertEqual(self.scenario.reserve_attempt(claimed), attempt)
        self.assertEqual(self.scenario.persisted_lease().budget.actions_used, 1)

        reservation = self.scenario.reserve_custody(claimed=claimed, attempt=attempt)
        self.scenario.issue(reservation)
        failed = self.scenario.store.record_ordinary_agent_qualification_failure(
            attempt_id=attempt.attempt_id,
            custody_attempt_id=reservation.custody_attempt_id,
            reason_code="provider_transport",
            counts=OrdinaryAgentProviderRequestCounts(
                rest_core_requests=0, graphql_requests=0, graphql_points=0
            ),
        )
        self.scenario.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=reservation.custody_attempt_id, reason="confirmed_revoked"
        )
        self.scenario.fixture.clock.return_value = datetime.fromtimestamp(
            self.scenario.fixture.now + MIN_RECONCILIATION_BACKOFF_SECONDS,
            timezone.utc,
        ).isoformat()
        retried = self.scenario.reserve_attempt(claimed)
        self.assertNotEqual(retried.attempt_id, failed.attempt_id)
        self.assertEqual(retried.attempt_ordinal, failed.attempt_ordinal + 1)
        self.assertEqual(self.scenario.persisted_lease().budget.actions_used, 1)

        self.scenario.rebind(binding_revision=2)
        later_revision = self.scenario.reserve_attempt(claimed)
        self.assertEqual(later_revision.binding_revision, 2)
        self.assertEqual(later_revision.attempt_ordinal, 1)
        self.assertEqual(self.scenario.persisted_lease().budget.actions_used, 1)

    def test_pre_mint_readiness_rechecks_revoked_activation_without_second_charge(self) -> None:
        claimed = self.scenario.claim(lease_seconds=120)
        attempt = self.scenario.reserve_attempt(claimed)
        self.scenario.reserve_custody(claimed=claimed, attempt=attempt)
        actions_used = self.scenario.persisted_lease().budget.actions_used

        scope = OrdinaryAgentDeliveryActivationScope(
            target=self.scenario.request.target,
            managed_set_id=self.scenario.lease.managed_set_id,
            managed_rule_id=self.scenario.lease.managed_rule_id,
        )
        installed = activation_support._record(
            operation_id="qualification-runtime-readiness",
            installed_at="2026-09-10T00:00:00Z",
            expires_at="2026-09-12T00:00:00Z",
            scope=scope,
        )
        installed_event = activation_support._event(
            installed,
            action="installed",
            source_operation_id=installed.source_setup_operation_id,
        )
        self.scenario.store.install_ordinary_agent_delivery_activation(installed, installed_event)
        revoked = activation_support._revoked(installed, occurred_at="2026-09-10T00:01:00Z")
        self.scenario.store.revoke_ordinary_agent_delivery_activation(
            revoked,
            activation_support._event(
                revoked,
                action="revoked",
                source_operation_id="qualification-runtime-readiness-revoke",
                previous=installed,
            ),
        )
        self.scenario.readiness_patch.stop()

        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "activation_not_current"):
            self.scenario.store.require_ordinary_agent_qualification_runtime_readiness(
                claim_fence=claimed.claim_fence,
                attempt_id=attempt.attempt_id,
            )
        self.assertEqual(
            self.scenario.persisted_lease().budget.actions_used,
            actions_used,
        )

    def test_revoked_activation_paces_closed_pre_mint_custody_without_provider_retry(
        self,
    ) -> None:
        claimed = self.scenario.claim(lease_seconds=30)
        attempt = self.scenario.reserve_attempt(claimed)
        reservation = self.scenario.reserve_custody(claimed=claimed, attempt=attempt)
        acquired, _ = self.scenario.store.acquire_ordinary_agent_custody_issue_attempt(
            attempt_id=reservation.custody_attempt_id,
            idempotency_key_sha256=hashlib.sha256(reservation.idempotency_key.encode()).hexdigest(),
            request_sha256=canonical_json_sha256(
                {
                    "candidate": reservation.candidate.model_dump(mode="json"),
                    "request": reservation.request_payload,
                }
            ),
            candidate=reservation.candidate,
            requested_permissions=ordinary_agent_effect_permissions(
                reservation.candidate.effect_profile
            ),
            dispatch_window_seconds=30,
        )
        self.assertEqual(acquired, "acquired")
        self.scenario.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=reservation.custody_attempt_id,
            reason="not_dispatched",
        )
        self.scenario.store.finish_ordinary_agent_job_attempt(
            claim_fence=claimed.claim_fence,
            disposition=OrdinaryAgentJobAttemptDisposition(
                status="waiting",
                next_due_at=self.scenario.fixture.now + 30,
                reason_code="activation_not_current",
            ),
        )

        scope = OrdinaryAgentDeliveryActivationScope(
            target=self.scenario.request.target,
            managed_set_id=self.scenario.lease.managed_set_id,
            managed_rule_id=self.scenario.lease.managed_rule_id,
        )
        installed = activation_support._record(
            operation_id="qualification-maintenance-readiness",
            installed_at="2026-09-10T00:00:00Z",
            expires_at="2026-09-12T00:00:00Z",
            scope=scope,
        )
        self.scenario.store.install_ordinary_agent_delivery_activation(
            installed,
            activation_support._event(
                installed,
                action="installed",
                source_operation_id=installed.source_setup_operation_id,
            ),
        )
        revoked = activation_support._revoked(installed, occurred_at="2026-09-10T00:01:00Z")
        self.scenario.store.revoke_ordinary_agent_delivery_activation(
            revoked,
            activation_support._event(
                revoked,
                action="revoked",
                source_operation_id="qualification-maintenance-readiness-revoke",
                previous=installed,
            ),
        )
        self.scenario.readiness_patch.stop()
        self.scenario.fixture.clock.return_value = datetime.fromtimestamp(
            self.scenario.fixture.now + 31, timezone.utc
        ).isoformat()
        reclaimed = self.scenario.claim(worker_id="maintenance-worker", lease_seconds=30)
        provider_calls = 0

        def reject_provider_call(**_: object) -> object:
            nonlocal provider_calls
            provider_calls += 1
            raise AssertionError("readiness denial must precede a fresh provider request")

        disposition = advance_ordinary_agent_qualification_job(
            claimed=reclaimed,
            store=self.scenario.store,
            setup_resolver=lambda **_: (_ for _ in ()).throw(
                AssertionError("existing history must not resolve fresh setup")
            ),
            api_request=reject_provider_call,
            utc_now=lambda: datetime.fromtimestamp(self.scenario.fixture.now + 31, timezone.utc),
        )
        self.assertEqual(disposition.status, "waiting")
        self.assertEqual(disposition.reason_code, "activation_not_current")
        self.assertEqual(provider_calls, 0)
        self.assertEqual(self.scenario.persisted_lease().budget.actions_used, 1)
        self.scenario.store.finish_ordinary_agent_job_attempt(
            claim_fence=reclaimed.claim_fence,
            disposition=disposition,
        )
        with self.scenario.store._session_factory() as session:
            claim_row = session.get(
                LaunchplaneOrdinaryAgentJobClaimRow, self.scenario.request.request_id
            )
            assert claim_row is not None
            self.assertGreater(claim_row.next_due_at, self.scenario.fixture.now + 31)

    def test_post_mint_readiness_denial_recovers_with_successor_without_recharge(
        self,
    ) -> None:
        claimed = self.scenario.claim(lease_seconds=120)
        readiness_calls = 0

        def readiness(**_: object) -> None:
            nonlocal readiness_calls
            readiness_calls += 1
            if readiness_calls == 2:
                raise OrdinaryAgentSessionAdmissionDenied("activation_not_current")

        def mint(**kwargs: object) -> GitHubAppInstallationToken:
            callback = kwargs["before_token_mint"]
            assert callable(callback)
            callback(self.scenario.fixture.envelope.custody.github_app_id, 77)
            issued_at = kwargs["now"]
            assert isinstance(issued_at, datetime)
            return GitHubAppInstallationToken(
                token="qualification-token",
                app_id=self.scenario.fixture.envelope.custody.github_app_id,
                installation_id=77,
                repository_id=self.scenario.request.target.repository_id,
                repository=self.scenario.request.target.repository,
                expires_at=(issued_at + timedelta(seconds=300)).isoformat(),
            )

        provider_observation = Mock(return_value=self.scenario.observation())

        def revoke_only(**kwargs: object) -> None:
            self.assertEqual(kwargs["method"], "DELETE")
            self.assertEqual(kwargs["path"], "/installation/token")
            return None

        with (
            patch.object(
                self.scenario.store,
                "require_ordinary_agent_qualification_runtime_readiness",
                side_effect=readiness,
            ),
            patch(
                "control_plane.ordinary_agent_custody.resolve_ordinary_agent_github_app_identity",
                return_value=SimpleNamespace(
                    identity=SimpleNamespace(
                        app_id=self.scenario.fixture.envelope.custody.github_app_id
                    )
                ),
            ),
            patch(
                "control_plane.ordinary_agent_custody.mint_ordinary_agent_installation_token",
                side_effect=mint,
            ),
            patch(
                "control_plane.ordinary_agent_qualification_job.observe_repository_administrator",
                provider_observation,
            ),
        ):
            first = advance_ordinary_agent_qualification_job(
                claimed=claimed,
                store=self.scenario.store,
                setup_resolver=lambda **_: self.scenario.setup,
                api_request=revoke_only,
                monotonic=lambda: 0,
                utc_now=lambda: datetime.fromtimestamp(self.scenario.fixture.now, timezone.utc),
            )
            self.assertEqual(first.status, "waiting")
            self.assertEqual(first.reason_code, "activation_not_current")
            provider_observation.assert_not_called()
            self.scenario.store.finish_ordinary_agent_job_attempt(
                claim_fence=claimed.claim_fence, disposition=first
            )

            assert first.next_due_at is not None
            retry_at = first.next_due_at
            self.scenario.fixture.clock.return_value = datetime.fromtimestamp(
                retry_at, timezone.utc
            ).isoformat()
            reclaimed = self.scenario.claim(
                worker_id="post-mint-readiness-recovery", lease_seconds=120
            )
            completed = advance_ordinary_agent_qualification_job(
                claimed=reclaimed,
                store=self.scenario.store,
                setup_resolver=lambda **_: self.scenario.setup,
                api_request=revoke_only,
                monotonic=lambda: 0,
                utc_now=lambda: datetime.fromtimestamp(retry_at, timezone.utc),
            )

        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.reason_code, "qualification_attested")
        provider_observation.assert_called_once()
        finished = self.scenario.store.finish_ordinary_agent_job_attempt(
            claim_fence=reclaimed.claim_fence, disposition=completed
        )
        self.assertEqual(finished.status, "completed")
        with self.scenario.store._session_factory() as session:
            attempts = tuple(
                parse_ordinary_agent_read_attempt(row.payload)
                for row in session.query(LaunchplaneOrdinaryAgentReadAttemptRow)
                .order_by(LaunchplaneOrdinaryAgentReadAttemptRow.attempt_ordinal)
                .all()
            )
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0].state, "incomplete")
        self.assertEqual(attempts[0].reason_code, "read_outcome_missing")
        self.assertIsNone(attempts[0].failure_counts)
        self.assertEqual(attempts[1].state, "completed")
        self.assertEqual(attempts[1].attempt_ordinal, 2)
        self.assertEqual(self.scenario.persisted_lease().budget.actions_used, 1)

    def test_known_expired_custody_without_outcome_terminalizes_reading_attempt(
        self,
    ) -> None:
        claimed = self.scenario.claim(lease_seconds=120)
        attempt = self.scenario.reserve_attempt(claimed)
        reservation = self.scenario.reserve_custody(claimed=claimed, attempt=attempt)
        self.scenario.issue(reservation)
        self.scenario.fixture.clock.return_value = datetime.fromtimestamp(
            self.scenario.fixture.now + 360, timezone.utc
        ).isoformat()
        reclaimed = self.scenario.claim(worker_id="missing-outcome-known-expiry", lease_seconds=120)

        disposition = advance_ordinary_agent_qualification_job(
            claimed=reclaimed,
            store=self.scenario.store,
            setup_resolver=lambda **_: (_ for _ in ()).throw(
                AssertionError("terminal recovery must not resolve fresh setup")
            ),
            api_request=lambda **_: (_ for _ in ()).throw(
                AssertionError("terminal recovery must not call the provider")
            ),
            utc_now=lambda: datetime.fromtimestamp(self.scenario.fixture.now + 360, timezone.utc),
        )

        self.assertEqual(disposition.status, "blocked")
        self.assertEqual(disposition.reason_code, "qualification_request_terminal")
        custody = self.scenario.store.read_ordinary_agent_custody_issue_attempt(
            reservation.custody_attempt_id
        )
        self.assertEqual(custody.state, "closed")
        self.assertEqual(custody.close_reason, "known_expired")
        with self.scenario.store._session_factory() as session:
            row = session.get(LaunchplaneOrdinaryAgentReadAttemptRow, attempt.attempt_id)
            assert row is not None
            recovered = parse_ordinary_agent_read_attempt(row.payload)
        self.assertEqual(recovered.state, "exhausted")
        self.assertEqual(recovered.reason_code, "qualification_request_terminal")
        self.assertEqual(self.scenario.persisted_lease().budget.actions_used, 1)

    def test_closed_pre_mint_custody_cap_persists_exhausted_attempt(self) -> None:
        claimed = self.scenario.claim(lease_seconds=120)
        attempt = self.scenario.reserve_attempt(claimed)
        for _ in range(MAX_CUSTODY_MINT_ATTEMPTS_PER_DISPATCH_CHILD):
            reservation = self.scenario.reserve_custody(claimed=claimed, attempt=attempt)
            acquired, _ = self.scenario.store.acquire_ordinary_agent_custody_issue_attempt(
                attempt_id=reservation.custody_attempt_id,
                idempotency_key_sha256=hashlib.sha256(
                    reservation.idempotency_key.encode()
                ).hexdigest(),
                request_sha256=canonical_json_sha256(
                    {
                        "candidate": reservation.candidate.model_dump(mode="json"),
                        "request": reservation.request_payload,
                    }
                ),
                candidate=reservation.candidate,
                requested_permissions=ordinary_agent_effect_permissions(
                    reservation.candidate.effect_profile
                ),
                dispatch_window_seconds=30,
            )
            self.assertEqual(acquired, "acquired")
            self.scenario.store.close_ordinary_agent_custody_issue_attempt(
                attempt_id=reservation.custody_attempt_id, reason="not_dispatched"
            )
            attempt = self.scenario.reserve_attempt(claimed)

        self.assertEqual(attempt.state, "exhausted")
        self.assertEqual(attempt.reason_code, "read_custody_attempts_exhausted")
        self.assertEqual(self.scenario.persisted_lease().budget.actions_used, 1)

    def test_cancellation_between_attempt_and_custody_reclaims_for_terminalization(
        self,
    ) -> None:
        claimed = self.scenario.claim(lease_seconds=120)
        original_reserve = self.scenario.store.reserve_ordinary_agent_qualification_attempt
        cancelled = False

        def reserve_then_cancel(
            *,
            claim_fence: OrdinaryAgentJobClaimFence,
            setup: OrdinaryAgentQualificationSetup | None,
        ) -> OrdinaryAgentQualificationAttemptRecord:
            nonlocal cancelled
            record = original_reserve(claim_fence=claim_fence, setup=setup)
            if not cancelled:
                cancelled = True
                self.scenario.store.cancel_ordinary_agent_session(
                    proof=self.scenario.fixture.proof,
                    session_id=self.scenario.session.session_id,
                )
            return record

        no_provider = Mock(side_effect=AssertionError("cancelled work must not call provider"))
        with patch.object(
            self.scenario.store,
            "reserve_ordinary_agent_qualification_attempt",
            side_effect=reserve_then_cancel,
        ):
            waiting = advance_ordinary_agent_qualification_job(
                claimed=claimed,
                store=self.scenario.store,
                setup_resolver=lambda **_: self.scenario.setup,
                api_request=no_provider,
                utc_now=lambda: datetime.fromtimestamp(self.scenario.fixture.now, timezone.utc),
            )

        self.assertEqual(waiting.status, "waiting")
        self.assertEqual(waiting.reason_code, "job_not_dispatchable")
        self.scenario.store.finish_ordinary_agent_job_attempt(
            claim_fence=claimed.claim_fence, disposition=waiting
        )
        assert waiting.next_due_at is not None
        retry_at = waiting.next_due_at
        self.scenario.fixture.clock.return_value = datetime.fromtimestamp(
            retry_at, timezone.utc
        ).isoformat()
        reclaimed = self.scenario.claim(worker_id="cancelled-terminalization", lease_seconds=120)
        terminal = advance_ordinary_agent_qualification_job(
            claimed=reclaimed,
            store=self.scenario.store,
            setup_resolver=lambda **_: (_ for _ in ()).throw(
                AssertionError("terminal maintenance must not resolve setup")
            ),
            api_request=no_provider,
            utc_now=lambda: datetime.fromtimestamp(retry_at, timezone.utc),
        )

        self.assertEqual(terminal.status, "blocked")
        self.assertEqual(terminal.reason_code, "qualification_request_terminal")
        self.scenario.store.finish_ordinary_agent_job_attempt(
            claim_fence=reclaimed.claim_fence, disposition=terminal
        )
        self.assertIsNone(
            self.scenario.store.claim_due_ordinary_agent_job(
                worker_id="cancelled-terminal", lease_seconds=30
            )
        )
        no_provider.assert_not_called()
        self.assertEqual(self.scenario.persisted_lease().budget.actions_used, 1)

    def test_cancelled_session_restores_closed_positive_history_without_provider_retry(
        self,
    ) -> None:
        claimed = self.scenario.claim(lease_seconds=120)
        attempt = self.scenario.reserve_attempt(claimed)
        reservation = self.scenario.reserve_custody(claimed=claimed, attempt=attempt)
        self.scenario.issue(reservation)
        observation = self.scenario.observation()
        attestation = self.scenario.attestation(
            attempt=attempt,
            custody_attempt_id=reservation.custody_attempt_id,
            observation=observation,
        )
        self.scenario.store.record_ordinary_agent_qualification_result(
            attempt_id=attempt.attempt_id,
            custody_attempt_id=reservation.custody_attempt_id,
            result=observation,
            attestation=attestation,
        )
        self.scenario.store.mark_ordinary_agent_custody_cleanup_unknown(
            attempt_id=reservation.custody_attempt_id
        )
        self.scenario.store.record_ordinary_agent_qualification_failure(
            attempt_id=attempt.attempt_id,
            custody_attempt_id=reservation.custody_attempt_id,
            reason_code="cleanup_unknown",
            counts=observation.counts,
        )
        self.scenario.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=reservation.custody_attempt_id, reason="confirmed_revoked"
        )
        self.scenario.store.cancel_ordinary_agent_session(
            proof=self.scenario.fixture.proof,
            session_id=self.scenario.session.session_id,
        )

        provider_calls = 0

        def reject_provider_call(**_: object) -> object:
            nonlocal provider_calls
            provider_calls += 1
            raise AssertionError("historical recovery must not make a provider request")

        disposition = advance_ordinary_agent_qualification_job(
            claimed=claimed,
            store=self.scenario.store,
            setup_resolver=lambda **_: (_ for _ in ()).throw(
                AssertionError("historical recovery must not resolve fresh setup")
            ),
            api_request=reject_provider_call,
            utc_now=lambda: datetime.fromtimestamp(self.scenario.fixture.now, timezone.utc),
        )

        self.assertEqual(disposition.status, "completed")
        self.assertEqual(disposition.reason_code, "qualification_attested")
        self.assertEqual(provider_calls, 0)
        self.assertEqual(self.scenario.persisted_lease().budget.actions_used, 1)

    def test_issue_unknown_maintenance_is_paced_across_claim_finish_and_reclaim(
        self,
    ) -> None:
        claimed = self.scenario.claim(lease_seconds=120)
        attempt = self.scenario.reserve_attempt(claimed)
        reservation = self.scenario.reserve_custody(claimed=claimed, attempt=attempt)
        acquired, _ = self.scenario.store.acquire_ordinary_agent_custody_issue_attempt(
            attempt_id=reservation.custody_attempt_id,
            idempotency_key_sha256=hashlib.sha256(reservation.idempotency_key.encode()).hexdigest(),
            request_sha256=canonical_json_sha256(
                {
                    "candidate": reservation.candidate.model_dump(mode="json"),
                    "request": reservation.request_payload,
                }
            ),
            candidate=reservation.candidate,
            requested_permissions=ordinary_agent_effect_permissions(
                reservation.candidate.effect_profile
            ),
            dispatch_window_seconds=30,
        )
        self.assertEqual(acquired, "acquired")
        self.scenario.store.mark_ordinary_agent_custody_issue_unknown(
            attempt_id=reservation.custody_attempt_id
        )
        unknown = self.scenario.store.read_ordinary_agent_custody_issue_attempt(
            reservation.custody_attempt_id
        )
        conservative_expiry = (
            int(datetime.fromisoformat(unknown.dispatch_deadline).timestamp())
            + GITHUB_TOKEN_MAXIMUM_LIFETIME_SECONDS
            + KNOWN_TOKEN_CLOCK_SKEW_SECONDS
        )
        self.scenario.store.record_ordinary_agent_qualification_failure(
            attempt_id=attempt.attempt_id,
            custody_attempt_id=reservation.custody_attempt_id,
            reason_code="provider_transport",
            counts=OrdinaryAgentProviderRequestCounts(
                rest_core_requests=1, graphql_requests=0, graphql_points=0
            ),
        )

        def advance(current: OrdinaryAgentClaimedJob) -> OrdinaryAgentJobAttemptDisposition:
            return advance_ordinary_agent_qualification_job(
                claimed=current,
                store=self.scenario.store,
                setup_resolver=lambda **_: (_ for _ in ()).throw(
                    AssertionError("open historical custody must not resolve fresh setup")
                ),
                api_request=lambda **_: (_ for _ in ()).throw(
                    AssertionError("open historical custody must not call the provider")
                ),
                utc_now=lambda: datetime.fromtimestamp(
                    int(
                        datetime.fromisoformat(self.scenario.fixture.clock.return_value).timestamp()
                    ),
                    timezone.utc,
                ),
            )

        disposition = advance(claimed)
        self.assertEqual(disposition.status, "waiting")
        self.assertEqual(disposition.reason_code, "read_custody_fenced")
        self.assertEqual(disposition.next_due_at, conservative_expiry)
        self.scenario.store.finish_ordinary_agent_job_attempt(
            claim_fence=claimed.claim_fence, disposition=disposition
        )
        self.assertIsNone(
            self.scenario.store.claim_due_ordinary_agent_job(
                worker_id="too-early", lease_seconds=30
            )
        )

        self.scenario.fixture.clock.return_value = datetime.fromtimestamp(
            conservative_expiry - 1, timezone.utc
        ).isoformat()
        self.assertIsNone(
            self.scenario.store.claim_due_ordinary_agent_job(
                worker_id="before-conservative-expiry", lease_seconds=30
            )
        )
        self.scenario.fixture.clock.return_value = datetime.fromtimestamp(
            conservative_expiry, timezone.utc
        ).isoformat()
        reclaimed = self.scenario.claim(worker_id="known-expiry", lease_seconds=30)
        terminal = advance(reclaimed)
        self.assertEqual(terminal.status, "blocked")
        self.assertEqual(terminal.reason_code, "qualification_request_terminal")
        self.scenario.store.finish_ordinary_agent_job_attempt(
            claim_fence=reclaimed.claim_fence, disposition=terminal
        )
        self.assertIsNone(
            self.scenario.store.claim_due_ordinary_agent_job(
                worker_id="terminal-no-maintenance", lease_seconds=30
            )
        )
        closed = self.scenario.store.read_ordinary_agent_custody_issue_attempt(
            reservation.custody_attempt_id
        )
        self.assertEqual(closed.state, "closed")
        self.assertEqual(closed.close_reason, "known_expired")
        self.assertEqual(self.scenario.persisted_lease().budget.actions_used, 1)

    def test_cancelled_issued_custody_waits_for_expiry_then_stops_maintenance(
        self,
    ) -> None:
        claimed = self.scenario.claim(lease_seconds=120)
        attempt = self.scenario.reserve_attempt(claimed)
        reservation = self.scenario.reserve_custody(claimed=claimed, attempt=attempt)
        self.scenario.issue(reservation)
        self.scenario.store.cancel_ordinary_agent_session(
            proof=self.scenario.fixture.proof,
            session_id=self.scenario.session.session_id,
        )

        def no_new_work(**_: object) -> OrdinaryAgentQualificationSetup | None:
            raise AssertionError("terminal maintenance must not start new provider work")

        waiting = advance_ordinary_agent_qualification_job(
            claimed=claimed,
            store=self.scenario.store,
            setup_resolver=no_new_work,
            api_request=no_new_work,
            utc_now=lambda: datetime.fromtimestamp(self.scenario.fixture.now, timezone.utc),
        )
        self.assertEqual(waiting.status, "waiting")
        self.assertEqual(waiting.reason_code, "read_custody_fenced")
        self.assertEqual(waiting.next_due_at, self.scenario.fixture.now + 360)
        self.scenario.store.finish_ordinary_agent_job_attempt(
            claim_fence=claimed.claim_fence, disposition=waiting
        )
        self.assertIsNone(
            self.scenario.store.claim_due_ordinary_agent_job(
                worker_id="before-known-expiry", lease_seconds=30
            )
        )

        self.scenario.fixture.clock.return_value = datetime.fromtimestamp(
            self.scenario.fixture.now + 360, timezone.utc
        ).isoformat()
        reclaimed = self.scenario.claim(worker_id="known-expiry", lease_seconds=30)
        terminal = advance_ordinary_agent_qualification_job(
            claimed=reclaimed,
            store=self.scenario.store,
            setup_resolver=no_new_work,
            api_request=no_new_work,
            utc_now=lambda: datetime.fromtimestamp(self.scenario.fixture.now + 360, timezone.utc),
        )
        self.assertEqual(terminal.status, "blocked")
        self.assertEqual(terminal.reason_code, "qualification_request_terminal")
        self.scenario.store.finish_ordinary_agent_job_attempt(
            claim_fence=reclaimed.claim_fence, disposition=terminal
        )
        self.assertIsNone(
            self.scenario.store.claim_due_ordinary_agent_job(
                worker_id="terminal-no-maintenance", lease_seconds=30
            )
        )
        custody = self.scenario.store.read_ordinary_agent_custody_issue_attempt(
            reservation.custody_attempt_id
        )
        self.assertEqual(custody.state, "closed")
        self.assertEqual(custody.close_reason, "known_expired")
        self.assertEqual(self.scenario.persisted_lease().budget.actions_used, 1)

    def test_cancelled_request_without_attempt_stops_after_one_claim(self) -> None:
        claimed = self.scenario.claim(lease_seconds=120)
        self.scenario.store.cancel_ordinary_agent_session(
            proof=self.scenario.fixture.proof,
            session_id=self.scenario.session.session_id,
        )

        disposition = advance_ordinary_agent_qualification_job(
            claimed=claimed,
            store=self.scenario.store,
            setup_resolver=lambda **_: (_ for _ in ()).throw(
                AssertionError("terminal request must not resolve fresh setup")
            ),
            api_request=lambda **_: (_ for _ in ()).throw(
                AssertionError("terminal request must not call the provider")
            ),
            utc_now=lambda: datetime.fromtimestamp(self.scenario.fixture.now, timezone.utc),
        )

        self.assertEqual(disposition.status, "blocked")
        self.assertEqual(disposition.reason_code, "qualification_request_terminal")
        self.scenario.store.finish_ordinary_agent_job_attempt(
            claim_fence=claimed.claim_fence, disposition=disposition
        )
        self.assertIsNone(
            self.scenario.store.claim_due_ordinary_agent_job(
                worker_id="terminal-no-attempt", lease_seconds=30
            )
        )
        self.assertEqual(self.scenario.persisted_lease().budget.actions_used, 0)

    def test_qualification_setup_binds_immutable_installed_activation(self) -> None:
        installed_at = datetime.fromtimestamp(
            self.scenario.fixture.now - 10, timezone.utc
        ).isoformat()
        expires_at = datetime.fromtimestamp(
            self.scenario.fixture.now + 90, timezone.utc
        ).isoformat()
        installed = activation_support._record(
            operation_id="qualification-installed-binding",
            installed_at=installed_at,
            expires_at=expires_at,
            scope=OrdinaryAgentDeliveryActivationScope(
                target=self.scenario.request.target,
                managed_set_id=self.scenario.lease.managed_set_id,
                managed_rule_id=self.scenario.lease.managed_rule_id,
            ),
        )
        projected = OrdinaryAgentDeliveryActivationRecord.model_validate(
            {
                **installed.model_dump(mode="json"),
                "effective_state": "guarded",
                "revision": installed.revision + 1,
                "updated_at": datetime.fromtimestamp(
                    self.scenario.fixture.now, timezone.utc
                ).isoformat(),
                "activation_sha256": "",
            }
        )
        execution = OrdinaryAgentDeliveryActivationExecutionEvidence(
            action="setup",
            result_status="ok",
            result_digest="c" * 64,
            changed=True,
            activation_id=installed.activation_id,
            activation_revision=installed.revision,
            activation_sha256=installed.activation_sha256,
            desired_state=installed.desired_state,
            effective_state=installed.effective_state,
            source_operation_id=installed.source_setup_operation_id,
            reconciliation_required=False,
        )
        operation = SimpleNamespace(
            approval=SimpleNamespace(
                approver=SimpleNamespace(
                    identity_type="github_human",
                    github_id=self.scenario.fixture.human.identity.github_id,
                    login=self.scenario.fixture.human.identity.login,
                )
            ),
            execution=execution,
        )

        with patch.object(
            self.scenario.store,
            "_locked_authz_transition_operation",
            return_value=operation,
        ):
            setup = PostgresRecordStore._ordinary_agent_qualification_setup_from_activation(
                self.scenario.store,
                None,
                request=self.scenario.request,
                activation=projected,
                observed_at=self.scenario.fixture.now,
            )

        self.assertEqual(setup.source_activation_binding_sha256, installed.activation_sha256)
        self.assertNotEqual(setup.source_activation_binding_sha256, projected.activation_sha256)

    def test_exhausted_action_budget_denies_first_attempt_without_partial_history(self) -> None:
        self.scenario.fixture.exhaust_lease_actions(self.scenario.lease.lease_id)
        claimed = self.scenario.claim()
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "budget_exhausted"):
            self.scenario.reserve_attempt(claimed)
        with self.scenario.store._session_factory() as session:
            self.assertIsNone(session.query(LaunchplaneOrdinaryAgentReadAttemptRow).first())

    def test_reclaimed_job_does_not_let_original_claim_reserve_custody(self) -> None:
        original = self.scenario.claim(lease_seconds=10)
        attempt = self.scenario.reserve_attempt(original)
        self.scenario.fixture.clock.return_value = datetime.fromtimestamp(
            self.scenario.fixture.now + 11, timezone.utc
        ).isoformat()
        replacement = self.scenario.claim(worker_id="replacement", lease_seconds=60)
        self.assertGreater(replacement.claim_fence.generation, original.claim_fence.generation)
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "job_claim_lost"):
            self.scenario.reserve_custody(claimed=original, attempt=attempt)

    def test_qualification_claim_never_adopts_matching_stale_controller(self) -> None:
        self.scenario.seed_matching_stale_controller()
        self.assertIsNone(self.scenario.claim().controller_fence)

    def test_qualification_finish_never_reads_or_mutates_matching_stale_controller(self) -> None:
        controller = self.scenario.seed_matching_stale_controller()
        claimed = self.scenario.claim()
        completed, reservation = self.scenario.record_positive(claimed=claimed)
        self.scenario.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=reservation.custody_attempt_id, reason="confirmed_revoked"
        )
        statements: list[str] = []

        def capture_statement(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            statements.append(statement)

        event.listen(self.scenario.store._engine, "before_cursor_execute", capture_statement)
        try:
            view = self.scenario.store.finish_ordinary_agent_job_attempt(
                claim_fence=claimed.claim_fence,
                disposition=OrdinaryAgentJobAttemptDisposition(
                    status="completed", reason_code="qualification_attested"
                ),
            )
        finally:
            event.remove(self.scenario.store._engine, "before_cursor_execute", capture_statement)

        self.assertEqual(view.status, "completed")
        self.assertEqual(completed.state, "completed")
        self.assertFalse(
            any("launchplane_merge_train_controller_states" in item for item in statements)
        )
        self.assertEqual(
            self.scenario.store.read_merge_train_controller_state_record(controller.controller_key),
            controller,
        )

    def test_qualification_finish_rejects_missing_fenced_and_unclosed_evidence(self) -> None:
        claimed = self.scenario.claim()
        disposition = OrdinaryAgentJobAttemptDisposition(
            status="completed", reason_code="qualification_attested"
        )
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "qualification_completion_unproven"
        ):
            self.scenario.store.finish_ordinary_agent_job_attempt(
                claim_fence=claimed.claim_fence, disposition=disposition
            )

        completed, reservation = self.scenario.record_positive(claimed=claimed)
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "read_custody_fenced"):
            self.scenario.store.finish_ordinary_agent_job_attempt(
                claim_fence=claimed.claim_fence, disposition=disposition
            )

        self.scenario.store.mark_ordinary_agent_custody_cleanup_unknown(
            attempt_id=reservation.custody_attempt_id
        )
        assert completed.result is not None
        fenced = self.scenario.store.record_ordinary_agent_qualification_failure(
            attempt_id=completed.attempt_id,
            custody_attempt_id=reservation.custody_attempt_id,
            reason_code="cleanup_unknown",
            counts=completed.result.counts,
        )
        self.assertEqual(fenced.state, "fenced")
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "read_custody_fenced"):
            self.scenario.store.finish_ordinary_agent_job_attempt(
                claim_fence=claimed.claim_fence, disposition=disposition
            )

    def test_historical_qualification_evidence_can_finish_after_authority_revocation(self) -> None:
        claimed = self.scenario.claim()
        _, reservation = self.scenario.record_positive(claimed=claimed)
        self.scenario.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=reservation.custody_attempt_id, reason="confirmed_revoked"
        )
        replace_policy_without_ordinary_agent_rule(
            self.scenario.store, current=self.scenario.fixture.policy
        )
        with self.assertRaises(OrdinaryAgentSessionAdmissionDenied):
            self.scenario.store.reauthorize_ordinary_agent_finite_job(
                request_id=self.scenario.request.request_id
            )

        view = self.scenario.store.finish_ordinary_agent_job_attempt(
            claim_fence=claimed.claim_fence,
            disposition=OrdinaryAgentJobAttemptDisposition(
                status="completed", reason_code="qualification_attested"
            ),
        )
        self.assertEqual(view.status, "completed")

    def test_old_binding_unknown_custody_fences_new_binding_completion(self) -> None:
        claimed = self.scenario.claim(lease_seconds=120)
        old_attempt = self.scenario.reserve_attempt(claimed)
        old_reservation = self.scenario.reserve_custody(claimed=claimed, attempt=old_attempt)

        self.scenario.rebind(binding_revision=2)
        _, current_reservation = self.scenario.record_positive(claimed=claimed)
        self.scenario.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=current_reservation.custody_attempt_id,
            reason="confirmed_revoked",
        )
        self.scenario.issue(old_reservation)
        self.scenario.store.mark_ordinary_agent_custody_cleanup_unknown(
            attempt_id=old_reservation.custody_attempt_id
        )
        self.scenario.store.record_ordinary_agent_qualification_failure(
            attempt_id=old_attempt.attempt_id,
            custody_attempt_id=old_reservation.custody_attempt_id,
            reason_code="cleanup_unknown",
            counts=OrdinaryAgentProviderRequestCounts(
                rest_core_requests=0, graphql_requests=0, graphql_points=0
            ),
        )

        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "read_custody_fenced"):
            self.scenario.store.finish_ordinary_agent_job_attempt(
                claim_fence=claimed.claim_fence,
                disposition=OrdinaryAgentJobAttemptDisposition(
                    status="completed", reason_code="qualification_attested"
                ),
            )

    def test_positive_cleanup_unknown_stays_fenced_until_closed_then_restores(self) -> None:
        claimed = self.scenario.claim()
        attempt = self.scenario.reserve_attempt(claimed)
        reservation = self.scenario.reserve_custody(claimed=claimed, attempt=attempt)
        self.scenario.issue(reservation)
        observation = self.scenario.observation()
        attestation = self.scenario.attestation(
            attempt=attempt,
            custody_attempt_id=reservation.custody_attempt_id,
            observation=observation,
        )
        completed = self.scenario.store.record_ordinary_agent_qualification_result(
            attempt_id=attempt.attempt_id,
            custody_attempt_id=reservation.custody_attempt_id,
            result=observation,
            attestation=attestation,
        )
        self.scenario.store.mark_ordinary_agent_custody_cleanup_unknown(
            attempt_id=reservation.custody_attempt_id
        )
        fenced = self.scenario.store.record_ordinary_agent_qualification_failure(
            attempt_id=attempt.attempt_id,
            custody_attempt_id=reservation.custody_attempt_id,
            reason_code="cleanup_unknown",
            counts=observation.counts,
        )
        self.assertEqual(fenced.state, "fenced")
        self.assertEqual(fenced.result, completed.result)
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "read_custody_fenced"
        ) as raised:
            self.scenario.reserve_attempt(claimed)
        self.assertEqual(raised.exception.retry_not_before, self.scenario.fixture.now + 360)

        self.scenario.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=reservation.custody_attempt_id, reason="confirmed_revoked"
        )
        restored = self.scenario.reserve_attempt(claimed)
        self.assertEqual(restored.state, "completed")
        self.assertEqual(restored.result, observation)
        self.assertEqual(restored.custody_attempt_ids, (reservation.custody_attempt_id,))
        self.assertEqual(self.scenario.persisted_lease().budget.actions_used, 1)
        with self.scenario.store._session_factory() as fresh_session:
            row = fresh_session.get(LaunchplaneOrdinaryAgentReadAttemptRow, attempt.attempt_id)
            assert row is not None
            persisted = parse_ordinary_agent_read_attempt(row.payload)
        self.assertEqual(persisted, restored)
        self.assertEqual(self.scenario.reserve_attempt(claimed), restored)

    def test_closed_cleanup_without_result_persists_incomplete_and_reserves_successor(self) -> None:
        claimed = self.scenario.claim()
        attempt = self.scenario.reserve_attempt(claimed)
        reservation = self.scenario.reserve_custody(claimed=claimed, attempt=attempt)
        self.scenario.issue(reservation)
        self.scenario.store.mark_ordinary_agent_custody_cleanup_unknown(
            attempt_id=reservation.custody_attempt_id
        )
        fenced = self.scenario.store.record_ordinary_agent_qualification_failure(
            attempt_id=attempt.attempt_id,
            custody_attempt_id=reservation.custody_attempt_id,
            reason_code="cleanup_unknown",
            counts=OrdinaryAgentProviderRequestCounts(
                rest_core_requests=0, graphql_requests=0, graphql_points=0
            ),
        )
        self.assertEqual(fenced.state, "fenced")
        self.scenario.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=reservation.custody_attempt_id, reason="confirmed_revoked"
        )

        successor = self.scenario.reserve_attempt(claimed)
        self.assertEqual(successor.state, "reserved")
        self.assertEqual(successor.attempt_ordinal, attempt.attempt_ordinal + 1)
        with self.scenario.store._session_factory() as fresh_session:
            row = fresh_session.get(LaunchplaneOrdinaryAgentReadAttemptRow, attempt.attempt_id)
            assert row is not None
            restored = parse_ordinary_agent_read_attempt(row.payload)
        self.assertEqual(restored.state, "incomplete")
        self.assertEqual(self.scenario.persisted_lease().budget.actions_used, 1)

    def test_provider_retry_cap_counts_failures_across_binding_revisions(self) -> None:
        claimed = self.scenario.claim(lease_seconds=120)
        zero_counts = OrdinaryAgentProviderRequestCounts(
            rest_core_requests=0, graphql_requests=0, graphql_points=0
        )
        for index in range(MAX_SNAPSHOT_PROVIDER_ATTEMPTS):
            attempt = self.scenario.reserve_attempt(claimed)
            reservation = self.scenario.reserve_custody(claimed=claimed, attempt=attempt)
            self.scenario.issue(reservation)
            self.scenario.store.record_ordinary_agent_qualification_failure(
                attempt_id=attempt.attempt_id,
                custody_attempt_id=reservation.custody_attempt_id,
                reason_code="provider_transport",
                counts=zero_counts,
            )
            self.scenario.store.close_ordinary_agent_custody_issue_attempt(
                attempt_id=reservation.custody_attempt_id, reason="confirmed_revoked"
            )
            if index == 0:
                self.scenario.rebind(binding_revision=2)
            self.scenario.fixture.clock.return_value = datetime.fromtimestamp(
                self.scenario.fixture.now + (index + 1) * MIN_RECONCILIATION_BACKOFF_SECONDS,
                timezone.utc,
            ).isoformat()

        exhausted = self.scenario.reserve_attempt(claimed)
        self.assertEqual(exhausted.state, "exhausted")
        self.assertEqual(exhausted.reason_code, "read_attempts_exhausted")
        with self.scenario.store._session_factory() as fresh_session:
            attempts = fresh_session.query(LaunchplaneOrdinaryAgentReadAttemptRow).all()
        self.assertEqual(len(attempts), MAX_SNAPSHOT_PROVIDER_ATTEMPTS)
        self.assertEqual(self.scenario.persisted_lease().budget.actions_used, 1)

    def test_terminal_history_returns_current_binding_after_older_higher_ordinal(self) -> None:
        claimed = self.scenario.claim(lease_seconds=120)
        zero_counts = OrdinaryAgentProviderRequestCounts(
            rest_core_requests=0, graphql_requests=0, graphql_points=0
        )
        for index in range(2):
            attempt = self.scenario.reserve_attempt(claimed)
            reservation = self.scenario.reserve_custody(claimed=claimed, attempt=attempt)
            self.scenario.issue(reservation)
            self.scenario.store.record_ordinary_agent_qualification_failure(
                attempt_id=attempt.attempt_id,
                custody_attempt_id=reservation.custody_attempt_id,
                reason_code="provider_transport",
                counts=zero_counts,
            )
            self.scenario.store.close_ordinary_agent_custody_issue_attempt(
                attempt_id=reservation.custody_attempt_id, reason="confirmed_revoked"
            )
            self.scenario.fixture.clock.return_value = datetime.fromtimestamp(
                self.scenario.fixture.now + (index + 1) * MIN_RECONCILIATION_BACKOFF_SECONDS,
                timezone.utc,
            ).isoformat()

        self.scenario.rebind(binding_revision=2)
        completed, reservation = self.scenario.record_positive(claimed=claimed)
        self.scenario.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=reservation.custody_attempt_id, reason="confirmed_revoked"
        )
        self.scenario.store.cancel_ordinary_agent_session(
            proof=self.scenario.fixture.proof,
            session_id=self.scenario.session.session_id,
        )

        recovered = self.scenario.store.reserve_ordinary_agent_qualification_attempt(
            claim_fence=claimed.claim_fence, setup=None
        )

        self.assertEqual(recovered.attempt_id, completed.attempt_id)
        self.assertEqual(recovered.binding_revision, 2)
        self.assertEqual(recovered.state, "completed")

    def test_issued_result_can_be_recorded_after_session_revocation(self) -> None:
        claimed = self.scenario.claim()
        attempt = self.scenario.reserve_attempt(claimed)
        reservation = self.scenario.reserve_custody(claimed=claimed, attempt=attempt)
        self.scenario.issue(reservation)
        observation = self.scenario.observation()
        attestation = self.scenario.attestation(
            attempt=attempt,
            custody_attempt_id=reservation.custody_attempt_id,
            observation=observation,
        )
        self.scenario.store.cancel_ordinary_agent_session(
            proof=self.scenario.fixture.proof, session_id=self.scenario.session.session_id
        )

        recorded = self.scenario.store.record_ordinary_agent_qualification_result(
            attempt_id=attempt.attempt_id,
            custody_attempt_id=reservation.custody_attempt_id,
            result=observation,
            attestation=attestation,
        )
        self.assertEqual(recorded.state, "completed")
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "job_(?:claim_lost|not_dispatchable)"
        ):
            self.scenario.store.require_ordinary_agent_qualification_read_authority(
                claim_fence=claimed.claim_fence, attempt_id=attempt.attempt_id
            )

    def test_setup_and_inventory_drift_fail_before_custody_reservation(self) -> None:
        claimed = self.scenario.claim()
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "qualification_setup_conflict"
        ):
            self.scenario.store.reserve_ordinary_agent_qualification_attempt(
                claim_fence=claimed.claim_fence,
                setup=self.scenario.setup.model_copy(update={"managed_rule_id": "changed.rule"}),
            )

        attempt = self.scenario.reserve_attempt(claimed)
        inventory = self.scenario.store.list_repository_inventory_records(
            repository_id=str(self.scenario.request.target.repository_id)
        )[0]
        self.scenario.store.write_repository_inventory_record(
            type(inventory).model_validate(
                {
                    **inventory.model_dump(
                        exclude={"record_id", "inventory_digest", "supersedes_record_id"}
                    ),
                    "inventory_revision": inventory.inventory_revision + 1,
                    "recorded_at": "2026-09-10T00:00:00Z",
                    "reason": "exercise qualification inventory drift",
                    "supersedes_record_id": inventory.record_id,
                }
            )
        )
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "inventory_drift"):
            self.scenario.reserve_custody(claimed=claimed, attempt=attempt)

    def test_attestation_must_bind_setup_secret_inventory_and_provider_provenance(self) -> None:
        claimed = self.scenario.claim()
        attempt = self.scenario.reserve_attempt(claimed)
        reservation = self.scenario.reserve_custody(claimed=claimed, attempt=attempt)
        self.scenario.issue(reservation)
        observation = self.scenario.observation()
        changes = {
            "source_activation_binding_sha256": "b" * 64,
            "policy_managed_rule_id": "changed.rule",
            "repository_inventory_digest": "c" * 64,
            "managed_secret_version_id": "changed-secret-version",
            "provider_inspection_sha256": "d" * 64,
            "installed_permission_ceiling_sha256": "e" * 64,
            "read_profile_sha256": "f" * 64,
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                attestation = self.scenario.attestation(
                    attempt=attempt,
                    custody_attempt_id=reservation.custody_attempt_id,
                    observation=observation,
                    update={field: value},
                )
                with self.assertRaisesRegex(
                    OrdinaryAgentSessionAdmissionDenied, "qualification_provenance_conflict"
                ):
                    self.scenario.store.record_ordinary_agent_qualification_result(
                        attempt_id=attempt.attempt_id,
                        custody_attempt_id=reservation.custody_attempt_id,
                        result=observation,
                        attestation=attestation,
                    )

        valid = self.scenario.attestation(
            attempt=attempt,
            custody_attempt_id=reservation.custody_attempt_id,
            observation=observation,
        )
        self.assertEqual(
            self.scenario.store.record_ordinary_agent_qualification_result(
                attempt_id=attempt.attempt_id,
                custody_attempt_id=reservation.custody_attempt_id,
                result=observation,
                attestation=valid,
            ).attestation,
            valid,
        )


if __name__ == "__main__":
    unittest.main()
