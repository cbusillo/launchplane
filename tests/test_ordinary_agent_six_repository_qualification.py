"""Measured NORMAL qualification for the inactive ordinary-agent worker."""

from __future__ import annotations

from collections.abc import Callable
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from control_plane.contracts.ordinary_agent_custody import OrdinaryAgentCustodyIssueAttempt
from control_plane.contracts.ordinary_agent_activation import (
    OrdinaryAgentDeliveryActivationRecord,
)
from control_plane.contracts.ordinary_agent_effect import OrdinaryAgentClaimedJob
from control_plane.contracts.ordinary_agent_effect import OrdinaryAgentEffectRecord
from control_plane.contracts.ordinary_agent_effect import OrdinaryAgentJobAttemptDisposition
from control_plane.contracts.ordinary_agent_effect import OrdinaryAgentJobView
from control_plane.contracts.ordinary_agent_effect import OrdinaryAgentProviderQuotaKey
from control_plane.contracts.ordinary_agent_effect import OrdinaryAgentLandingPreparation
from control_plane.contracts.ordinary_agent_noop import OrdinaryAgentNoOpLandingFinalization
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
    OrdinaryAgentLeaseRecord,
)
from control_plane.ordinary_agent_job_worker import (
    OrdinaryAgentJobScanState,
    run_ordinary_agent_job_once,
)
from control_plane.ordinary_agent_controller_store import OrdinaryAgentProgressAdapter
from control_plane.ordinary_agent_merge_train_job import (
    advance_ordinary_agent_merge_train_job,
)
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.storage.postgres import (
    LaunchplaneOrdinaryAgentCustodyIssueAttemptRow,
    LaunchplaneOrdinaryAgentEffectRow,
    LaunchplaneOrdinaryAgentFiniteRequestRow,
    LaunchplaneOrdinaryAgentJobClaimRow,
    LaunchplaneOrdinaryAgentLandingPreparationRow,
    LaunchplaneOrdinaryAgentLeaseRow,
    LaunchplaneOrdinaryAgentNoOpLandingFinalizationRow,
    PostgresRecordStore,
)
from tests.support.ordinary_agent_qualification import (
    MeasuredOrdinaryGitHubScenario,
    OrdinaryQualificationFleet,
    QualificationClock,
)


class OrdinaryAgentSixRepositoryQualificationTests(unittest.TestCase):
    private_key: str

    @classmethod
    def setUpClass(cls) -> None:
        private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
        cls.private_key = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(directory.name) / 'qualification.sqlite3'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.clock = QualificationClock()
        self.database_clock = self.enterContext(
            patch.object(
                self.store,
                "_database_mutation_timestamp",
                side_effect=lambda _: self.clock.now().isoformat(),
            )
        )
        self.enterContext(
            patch.object(
                self.store,
                "_require_and_project_guarded_readiness",
                return_value=(Mock(spec=OrdinaryAgentDeliveryActivationRecord), self.clock.epoch),
            )
        )
        self.enterContext(
            patch(
                "control_plane.ordinary_agent_custody.control_plane_secrets._decrypt_secret_value",
                return_value=self.private_key,
            )
        )
        self.fleet: OrdinaryQualificationFleet | None = None

    def create_fleet(
        self,
        repository_count: int,
        *,
        request_expires_in: int = 10_000,
        continuation_expires_in: int = 20_000,
    ) -> OrdinaryQualificationFleet:
        self.fleet = OrdinaryQualificationFleet.create(
            store=self.store,
            clock=self.clock,
            repository_count=repository_count,
            request_expires_in=request_expires_in,
            continuation_expires_in=continuation_expires_in,
        )
        return self.fleet

    def run_worker(
        self,
        *,
        fleet: OrdinaryQualificationFleet,
        states: dict[str, OrdinaryAgentJobScanState],
        worker_id: str,
        dispositions: list[OrdinaryAgentJobAttemptDisposition] | None = None,
        failures: list[Exception] | None = None,
        expected_failure: type[Exception] | None = None,
        allow_failure: bool = False,
    ) -> int:
        provider: MeasuredOrdinaryGitHubScenario = fleet.provider

        def advance(claimed: OrdinaryAgentClaimedJob) -> OrdinaryAgentJobAttemptDisposition:
            try:
                with provider.bind(
                    worker_id=worker_id,
                    request_id=claimed.request.request_id,
                    repository=claimed.request.target.repository,
                ):
                    disposition = advance_ordinary_agent_merge_train_job(
                        claimed=claimed,
                        store=self.store,
                        api_request=provider.api_request,
                        effect_transport_factory=provider.transport_for,
                        monotonic=self.clock.monotonic,
                        utc_now=self.clock.now,
                    )
                if dispositions is not None:
                    dispositions.append(disposition)
                return disposition
            except Exception as error:
                if failures is not None:
                    failures.append(error)
                raise

        result = run_ordinary_agent_job_once(
            record_store=self.store,
            state=states[worker_id],
            worker_id=worker_id,
            lease_seconds=30,
            advance_job=advance,
        )
        if allow_failure:
            pass
        elif expected_failure is None:
            self.assertIsNone(result.failure_phase, repr(failures[-1]) if failures else None)
        else:
            self.assertEqual(result.failure_phase, "advance_or_finish")
            self.assertTrue(failures and isinstance(failures[-1], expected_failure))
        return result.processed

    def views(self, fleet: OrdinaryQualificationFleet) -> tuple[OrdinaryAgentJobView, ...]:
        return tuple(
            self.store.read_ordinary_agent_job(proof=proof, request_id=request.request_id)
            for proof, request in zip(fleet.proofs, fleet.requests, strict=True)
        )

    def pump_until(
        self,
        *,
        fleet: OrdinaryQualificationFleet,
        states: dict[str, OrdinaryAgentJobScanState],
        predicate: Callable[[tuple[OrdinaryAgentJobView, ...]], bool],
        max_polls: int = 200,
    ) -> tuple[OrdinaryAgentJobView, ...]:
        for _ in range(max_polls):
            processed = sum(
                self.run_worker(fleet=fleet, states=states, worker_id=worker_id)
                for worker_id in states
            )
            views = self.views(fleet)
            if predicate(views):
                return views
            if processed == 0:
                deadlines = [
                    view.next_due_at
                    for view in views
                    if view.next_due_at is not None and view.next_due_at > self.clock.epoch
                ]
                self.assertTrue(deadlines, repr(views))
                self.clock.advance_to(min(deadlines))
        self.fail("qualification journey exceeded its bounded poll count")

    @staticmethod
    def provider_error(status: int, *, retry_after: int | None = None) -> MergeTrainGitHubError:
        headers = Message()
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        http_error = HTTPError(
            url="https://api.github.com/qualification",
            code=status,
            msg="injected qualification response",
            hdrs=headers,
            fp=None,
        )
        error = MergeTrainGitHubError(
            "injected qualification response",
            status_code=status,
        )
        error.__cause__ = http_error
        return error

    def test_normal_six_repository_workload_stays_within_provider_limits(self) -> None:
        fleet = self.create_fleet(6)
        provider = fleet.provider
        states = {
            worker_id: OrdinaryAgentJobScanState()
            for worker_id in ("qualification-worker-a", "qualification-worker-b")
        }
        stage_durations: list[float] = []
        processed_by_worker = {worker_id: 0 for worker_id in states}
        worker_failures: list[Exception] = []

        def run_worker(worker_id: str) -> int:
            def advance(claimed: OrdinaryAgentClaimedJob) -> OrdinaryAgentJobAttemptDisposition:
                try:
                    with provider.bind(
                        worker_id=worker_id,
                        request_id=claimed.request.request_id,
                        repository=claimed.request.target.repository,
                    ):
                        return advance_ordinary_agent_merge_train_job(
                            claimed=claimed,
                            store=self.store,
                            api_request=provider.api_request,
                            effect_transport_factory=provider.transport_for,
                            monotonic=self.clock.monotonic,
                            utc_now=self.clock.now,
                        )
                except Exception as error:
                    worker_failures.append(error)
                    raise

            started = self.clock.monotonic()
            result = run_ordinary_agent_job_once(
                record_store=self.store,
                state=states[worker_id],
                worker_id=worker_id,
                lease_seconds=30,
                advance_job=advance,
            )
            if result.processed:
                stage_durations.append(self.clock.monotonic() - started)
            self.assertIsNone(
                result.failure_phase,
                repr(worker_failures[-1]) if worker_failures else None,
            )
            processed_by_worker[worker_id] += result.processed
            return result.processed

        for _ in range(400):
            processed = sum(run_worker(worker_id) for worker_id in states)
            views = tuple(
                self.store.read_ordinary_agent_job(proof=proof, request_id=request.request_id)
                for proof, request in zip(fleet.proofs, fleet.requests, strict=True)
            )
            if all(view.status == "completed" for view in views):
                break
            if processed == 0:
                next_due = [
                    view.next_due_at
                    for view in views
                    if view.next_due_at is not None and view.next_due_at > self.clock.epoch
                ]
                self.assertTrue(
                    next_due,
                    "ordinary workload stopped without a durable deadline: "
                    + repr(
                        [
                            (view.request_id, view.status, view.reason_code, view.next_due_at)
                            for view in views
                        ]
                    )
                    + " phases="
                    + repr([call.phase for call in provider.calls]),
                )
                self.clock.advance_to(min(next_due))
        else:
            self.fail("ordinary workload exceeded the bounded worker poll count")

        self.assertTrue(all(count > 0 for count in processed_by_worker.values()))
        self.assertEqual(
            [(view.status, view.completed_effects, view.total_effects) for view in views],
            [("completed", 6, 6)] * 6,
        )
        with self.store._session_factory() as session:
            lease_rows = tuple(
                session.scalars(
                    select(LaunchplaneOrdinaryAgentLeaseRow).order_by(
                        LaunchplaneOrdinaryAgentLeaseRow.lease_id
                    )
                )
            )
            request_rows = tuple(
                session.scalars(
                    select(LaunchplaneOrdinaryAgentFiniteRequestRow).order_by(
                        LaunchplaneOrdinaryAgentFiniteRequestRow.request_id
                    )
                )
            )
            effect_rows = tuple(
                session.scalars(
                    select(LaunchplaneOrdinaryAgentEffectRow).order_by(
                        LaunchplaneOrdinaryAgentEffectRow.request_id,
                        LaunchplaneOrdinaryAgentEffectRow.action_ordinal,
                    )
                )
            )
            custody_rows = tuple(
                session.scalars(
                    select(LaunchplaneOrdinaryAgentCustodyIssueAttemptRow).order_by(
                        LaunchplaneOrdinaryAgentCustodyIssueAttemptRow.attempt_id
                    )
                )
            )
            claim_rows = tuple(
                session.scalars(
                    select(LaunchplaneOrdinaryAgentJobClaimRow).order_by(
                        LaunchplaneOrdinaryAgentJobClaimRow.request_id
                    )
                )
            )
        leases = tuple(OrdinaryAgentLeaseRecord.model_validate(row.payload) for row in lease_rows)
        persisted_requests = tuple(
            OrdinaryAgentFiniteRequestRecord.model_validate(row.payload) for row in request_rows
        )
        effects = tuple(
            OrdinaryAgentEffectRecord.model_validate(row.payload) for row in effect_rows
        )
        custody_attempts = tuple(
            OrdinaryAgentCustodyIssueAttempt.model_validate(row.payload) for row in custody_rows
        )
        self.assertEqual(
            [(lease.budget.actions_used, lease.budget.pull_requests_used) for lease in leases],
            [(6, 2)] * 6,
        )
        self.assertEqual(
            [(request.status, request.refresh_used) for request in persisted_requests],
            [("completed", 0)] * 6,
        )
        self.assertEqual(len(effects), 36)
        self.assertEqual(
            [(effect.command.kind, effect.state) for effect in effects],
            [
                pair
                for _ in range(6)
                for pair in (
                    ("candidate_ref_prepare", "completed"),
                    ("candidate_head_merge", "completed"),
                    ("candidate_head_merge", "completed"),
                    ("pull_request_landing", "completed"),
                    ("pull_request_landing", "completed"),
                    ("candidate_ref_delete", "retained_no_conditional_delete"),
                )
            ],
        )
        self.assertEqual(
            [effect.action_ordinal for effect in effects],
            [ordinal for _ in range(6) for ordinal in range(1, 7)],
        )
        self.assertEqual(
            [
                (
                    effect.dispatch_count,
                    effect.dispatch_custody_count,
                    effect.reconciliation_count,
                    effect.reconciliation_custody_count,
                )
                for effect in effects
            ],
            [
                counts
                for _ in range(6)
                for counts in (
                    (1, 1, 0, 0),
                    (1, 1, 0, 0),
                    (1, 1, 0, 0),
                    (1, 1, 0, 0),
                    (1, 1, 0, 0),
                    (0, 0, 0, 0),
                )
            ],
        )
        self.assertEqual(
            [(attempt.state, attempt.close_reason) for attempt in custody_attempts],
            [("closed", "confirmed_revoked")] * len(custody_attempts),
        )
        self.assertEqual(
            [(row.status, row.released_controller) for row in claim_rows],
            [("completed", None)] * 6,
        )
        phase_counts = {
            phase: sum(call.phase == phase for call in provider.calls)
            for phase in {call.phase for call in provider.calls}
        }
        self.assertEqual(phase_counts["snapshot"], 6)
        self.assertEqual(phase_counts["candidate_prepare"], 6)
        self.assertEqual(phase_counts["candidate_merge"], 12)
        self.assertEqual(phase_counts["landing_merge"], 12)
        self.assertEqual(phase_counts["installation_lookup"], phase_counts["token_mint"])
        self.assertEqual(phase_counts["token_mint"], phase_counts["token_revoke"])
        self.assertEqual(phase_counts["token_mint"], len(custody_attempts))
        self.assertEqual(provider.active_token_count, 0)
        self.assertEqual(
            provider.requested_pull_request_numbers(),
            {
                pull_request.number
                for repository in provider.repositories.values()
                for pull_request in repository.bound_pull_requests
            },
        )
        for repository in provider.repositories.values():
            self.assertTrue(all(item.state == "MERGED" for item in repository.bound_pull_requests))
            self.assertTrue(all(item.state == "OPEN" for item in repository.unbound_pull_requests))

        raw_calls = len(provider.calls)
        graphql_points = sum(call.graphql_cost for call in provider.calls)
        self.assertLessEqual(raw_calls, 300)
        self.assertLessEqual(graphql_points, 120)
        self.assertLessEqual(max(call.graphql_cost for call in provider.calls), 10)
        self.assertLessEqual(max(stage_durations), 110)
        self.assertEqual(sum(phase_counts.values()), raw_calls)
        self.assertEqual([call.sequence for call in provider.calls], list(range(1, raw_calls + 1)))
        self.assertTrue(
            all(
                call.completed_at is not None and call.outcome == "success"
                for call in provider.calls
            )
        )
        self.assertTrue(
            all(call.worker_id in states for call in provider.calls),
            "every provider request must be attributed to one scan client",
        )
        self.assertTrue(
            all(call.request_id.startswith("qualification-request-") for call in provider.calls)
        )
        self.assertEqual({call.repository for call in provider.calls}, set(provider.repositories))
        for call in provider.calls:
            repository = provider.repositories[call.repository]
            if call.phase in {"installation_lookup", "token_mint"}:
                self.assertEqual((call.authority_kind, call.authority_id), ("app", provider.app_id))
            else:
                self.assertEqual(
                    (call.authority_kind, call.authority_id),
                    ("installation", repository.installation_id),
                )

    def test_quota_wait_does_not_block_another_installation_and_plain_403_adds_no_wait(
        self,
    ) -> None:
        fleet = self.create_fleet(
            2,
            request_expires_in=30,
            continuation_expires_in=1_000,
        )
        provider = fleet.provider
        first, second = tuple(provider.repositories.values())
        provider.fail_next(
            repository=first.repository,
            phase="snapshot",
            error=self.provider_error(429, retry_after=120),
        )
        states = {
            worker_id: OrdinaryAgentJobScanState()
            for worker_id in ("quota-worker-a", "quota-worker-b")
        }

        before_wait = int(self.clock.epoch)
        self.run_worker(fleet=fleet, states=states, worker_id="quota-worker-a")
        wait = self.store.read_provider_wait(
            quota_key=OrdinaryAgentProviderQuotaKey(
                authority_kind="installation",
                authority_id=first.installation_id,
                resource_class="secondary",
            )
        )
        assert wait is not None
        self.assertGreaterEqual(wait.retry_not_before, before_wait + 120)
        self.assertLessEqual(wait.retry_not_before, before_wait + 121)
        first_view = self.views(fleet)[0]
        self.assertGreaterEqual(first_view.next_due_at or 0, wait.retry_not_before)
        self.assertGreater(wait.retry_not_before, fleet.requests[0].expires_at)

        views = self.pump_until(
            fleet=fleet,
            states=states,
            predicate=lambda items: items[1].status == "completed",
        )
        self.assertNotEqual(views[0].status, "completed")
        self.assertLess(self.clock.epoch, wait.retry_not_before)
        delayed_calls = len(
            [call for call in provider.calls if call.repository == first.repository]
        )
        for worker_id in states:
            self.run_worker(fleet=fleet, states=states, worker_id=worker_id)
        self.assertEqual(
            len([call for call in provider.calls if call.repository == first.repository]),
            delayed_calls,
        )

        provider.fail_next(
            repository=first.repository,
            phase="candidate_prepare",
            error=self.provider_error(403),
        )
        self.clock.advance_to(wait.retry_not_before)
        blocked = self.pump_until(
            fleet=fleet,
            states=states,
            predicate=lambda items: items[0].status == "blocked",
        )[0]
        self.assertEqual(blocked.reason_code, "prior_effect_unresolved")
        self.assertIsNone(
            self.store.read_provider_wait(
                quota_key=OrdinaryAgentProviderQuotaKey(
                    authority_kind="installation",
                    authority_id=first.installation_id,
                    resource_class="core",
                )
            )
        )
        errors = [
            call
            for call in provider.calls
            if call.repository == first.repository and call.outcome == "error"
        ]
        self.assertEqual(
            [(call.phase, call.error_type) for call in errors],
            [
                ("snapshot", "MergeTrainGitHubError"),
                ("candidate_prepare", "MergeTrainGitHubError"),
            ],
        )

    def test_completed_landing_restarts_without_provider_resend_before_later_cleanup(
        self,
    ) -> None:
        fleet = self.create_fleet(1)
        provider = fleet.provider
        states = {"checkpoint-worker-a": OrdinaryAgentJobScanState()}
        failures: list[Exception] = []
        original_write = OrdinaryAgentProgressAdapter.write_merge_train_batch_landing_plan_record
        interrupted = False

        def interrupt_first_landed_checkpoint(
            adapter: OrdinaryAgentProgressAdapter,
            record: object,
        ) -> object:
            nonlocal interrupted
            entries = getattr(getattr(record, "landing_plan", None), "entries", ())
            if not interrupted and any(
                getattr(entry, "status", None) == "merged" for entry in entries
            ):
                interrupted = True
                raise RuntimeError("qualification checkpoint interrupted")
            return original_write(adapter, record)  # type: ignore[arg-type]

        with patch.object(
            OrdinaryAgentProgressAdapter,
            "write_merge_train_batch_landing_plan_record",
            new=interrupt_first_landed_checkpoint,
        ):
            for _ in range(20):
                self.run_worker(
                    fleet=fleet,
                    states=states,
                    worker_id="checkpoint-worker-a",
                    failures=failures,
                    allow_failure=True,
                )
                if failures:
                    break
                views = self.views(fleet)
                if all(view.next_due_at is not None for view in views):
                    self.clock.advance_to(min(view.next_due_at or 0 for view in views))

        self.assertTrue(interrupted)
        self.assertIsInstance(failures[-1], RuntimeError)
        completed_landing = [
            effect
            for effect in self._effects()
            if effect.command.kind == "pull_request_landing" and effect.state == "completed"
        ]
        self.assertEqual(len(completed_landing), 1)
        calls_before_recovery = len(provider.calls)
        merge_calls_before_recovery = sum(call.phase == "landing_merge" for call in provider.calls)

        self.clock.advance_to(int(self.clock.epoch) + 31)
        recovery_states = {"checkpoint-worker-b": OrdinaryAgentJobScanState()}
        dispositions: list[OrdinaryAgentJobAttemptDisposition] = []
        self.run_worker(
            fleet=fleet,
            states=recovery_states,
            worker_id="checkpoint-worker-b",
            dispositions=dispositions,
        )
        self.assertEqual(len(provider.calls), calls_before_recovery)
        self.assertEqual(dispositions[-1].status, "waiting")
        self.assertEqual(
            sum(call.phase == "landing_merge" for call in provider.calls),
            merge_calls_before_recovery,
        )
        self.assertNotEqual(self.views(fleet)[0].status, "completed")

        completed = self.pump_until(
            fleet=fleet,
            states=recovery_states,
            predicate=lambda views: views[0].status == "completed",
        )[0]
        self.assertEqual((completed.completed_effects, completed.total_effects), (6, 6))
        self.assertEqual(sum(call.phase == "landing_merge" for call in provider.calls), 2)

    def test_unknown_landing_waits_then_observes_without_resend_while_other_repo_progresses(
        self,
    ) -> None:
        fleet = self.create_fleet(2)
        provider = fleet.provider
        first, _second = tuple(provider.repositories.values())
        provider.fail_next(
            repository=first.repository,
            phase="landing_merge",
            error=MergeTrainGitHubError("qualification landing response lost"),
            after_dispatch=True,
        )
        states = {
            worker_id: OrdinaryAgentJobScanState()
            for worker_id in ("unknown-worker-a", "unknown-worker-b")
        }

        for _ in range(40):
            processed = sum(
                self.run_worker(fleet=fleet, states=states, worker_id=worker_id)
                for worker_id in states
            )
            failed_merge = next(
                (
                    call
                    for call in provider.calls
                    if call.repository == first.repository
                    and call.phase == "landing_merge"
                    and call.outcome == "error"
                ),
                None,
            )
            if failed_merge is not None:
                break
            if processed == 0:
                deadlines = [
                    view.next_due_at
                    for view in self.views(fleet)
                    if view.next_due_at is not None and view.next_due_at > self.clock.epoch
                ]
                self.assertTrue(deadlines)
                self.clock.advance_to(min(deadlines))
        else:
            self.fail("unknown landing response was not reached")

        landing = [
            effect
            for effect in self._effects()
            if effect.request_id == fleet.requests[0].request_id
            and effect.command.kind == "pull_request_landing"
        ][0]
        self.assertEqual((landing.state, landing.dispatch_count), ("reconciliation_required", 1))
        assert landing.next_observation_at is not None
        calls_after_unknown = len(
            [call for call in provider.calls if call.repository == first.repository]
        )
        ledger_position_after_unknown = len(provider.calls)
        merge_calls = sum(
            call.repository == first.repository and call.phase == "landing_merge"
            for call in provider.calls
        )

        independent = self.pump_until(
            fleet=fleet,
            states=states,
            predicate=lambda views: views[1].status == "completed",
        )
        self.assertLess(self.clock.epoch, landing.next_observation_at)
        self.assertNotEqual(independent[0].status, "completed")
        self.assertEqual(
            len([call for call in provider.calls if call.repository == first.repository]),
            calls_after_unknown,
        )

        self.clock.advance_to(landing.next_observation_at)
        for _ in range(3):
            self.run_worker(fleet=fleet, states=states, worker_id="unknown-worker-a")
            current = next(
                effect for effect in self._effects() if effect.effect_id == landing.effect_id
            )
            if current.reconciliation_count:
                break
        recovered = [effect for effect in self._effects() if effect.effect_id == landing.effect_id][
            0
        ]
        self.assertEqual((recovered.state, recovered.dispatch_count), ("completed_observed", 1))
        self.assertEqual(
            sum(
                call.repository == first.repository and call.phase == "landing_merge"
                for call in provider.calls
            ),
            merge_calls,
        )
        self.assertEqual(
            [
                call.phase
                for call in provider.calls[ledger_position_after_unknown:]
                if call.repository == first.repository
            ],
            [
                "installation_lookup",
                "token_mint",
                "reconciliation_pull_request",
                "reconciliation_commit",
                "reconciliation_ref",
                "token_revoke",
            ],
        )

    def test_expiry_before_dispatch_blocks_without_provider_calls(self) -> None:
        fleet = self.create_fleet(
            1,
            request_expires_in=30,
            continuation_expires_in=60,
        )
        self.clock.advance_to(fleet.requests[0].continuation_expires_at or 0)
        states = {"expiry-worker": OrdinaryAgentJobScanState()}
        dispositions: list[OrdinaryAgentJobAttemptDisposition] = []

        self.run_worker(
            fleet=fleet,
            states=states,
            worker_id="expiry-worker",
            dispositions=dispositions,
        )

        self.assertEqual(
            (dispositions[-1].status, dispositions[-1].reason_code),
            ("blocked", "job_expired"),
        )
        view = self.views(fleet)[0]
        self.assertEqual((view.status, view.total_effects), ("blocked", 0))
        self.assertEqual(fleet.provider.calls, [])

    def test_session_cancellation_after_unknown_dispatch_cannot_use_revoked_authority(
        self,
    ) -> None:
        fleet = self.create_fleet(1)
        provider = fleet.provider
        repository = next(iter(provider.repositories.values()))
        provider.fail_next(
            repository=repository.repository,
            phase="landing_merge",
            error=MergeTrainGitHubError("qualification landing response lost"),
            after_dispatch=True,
        )
        states = {"cancel-worker-a": OrdinaryAgentJobScanState()}
        for _ in range(20):
            self.run_worker(fleet=fleet, states=states, worker_id="cancel-worker-a")
            unresolved = [
                effect for effect in self._effects() if effect.state == "reconciliation_required"
            ]
            if unresolved:
                break
            view = self.views(fleet)[0]
            if view.next_due_at is not None:
                self.clock.advance_to(view.next_due_at)
        else:
            self.fail("unknown landing was not persisted before cancellation")

        effect = unresolved[0]
        calls_before_cancel = len(provider.calls)
        mutations_before_cancel = sum(
            call.method in {"POST", "PUT", "PATCH", "DELETE"}
            and call.phase not in {"token_mint", "token_revoke"}
            for call in provider.calls
        )
        self.store.cancel_ordinary_agent_session(
            proof=fleet.proofs[0],
            session_id=fleet.requests[0].session_id,
        )
        cancelled_states = {"cancel-worker-b": OrdinaryAgentJobScanState()}
        for _ in range(3):
            self.run_worker(
                fleet=fleet,
                states=cancelled_states,
                worker_id="cancel-worker-b",
            )

        view = self.views(fleet)[0]
        self.assertTrue(view.cancellation_requested)
        self.assertNotEqual(view.status, "completed")
        self.assertEqual(len(provider.calls), calls_before_cancel)
        self.assertEqual(
            sum(
                call.method in {"POST", "PUT", "PATCH", "DELETE"}
                and call.phase not in {"token_mint", "token_revoke"}
                for call in provider.calls
            ),
            mutations_before_cancel,
        )
        persisted = next(item for item in self._effects() if item.effect_id == effect.effect_id)
        self.assertEqual(
            (persisted.state, persisted.dispatch_count), ("reconciliation_required", 1)
        )

    def test_genuine_containment_skips_one_entry_then_lands_next_and_cleans_up_later(
        self,
    ) -> None:
        fleet = self.create_fleet(1)
        provider = fleet.provider
        repository = next(iter(provider.repositories.values()))
        first, second = repository.bound_pull_requests
        initial_base_sha = repository.base_sha
        repository.configure_candidate_no_op(first.number)
        states = {"noop-worker": OrdinaryAgentJobScanState()}

        for _ in range(20):
            self.run_worker(fleet=fleet, states=states, worker_id="noop-worker")
            with self.store._session_factory() as session:
                rows = tuple(
                    session.scalars(select(LaunchplaneOrdinaryAgentNoOpLandingFinalizationRow))
                )
            if rows:
                break
            view = self.views(fleet)[0]
            if view.next_due_at is not None:
                self.clock.advance_to(view.next_due_at)
        else:
            self.fail("qualification no-op finalization was not reached")

        self.assertEqual(len(rows), 1)
        finalization = OrdinaryAgentNoOpLandingFinalization.model_validate(rows[0].payload)
        self.assertEqual(finalization.successor.landing_plan.entries[0].status, "skipped")
        self.assertEqual(finalization.successor.landing_plan.entries[1].status, "planned")
        self.assertEqual(
            finalization.successor.landing_plan.entries[0].recorded_rolling_base_sha,
            initial_base_sha,
        )
        self.assertNotEqual(self.views(fleet)[0].status, "completed")
        self.assertEqual(
            sum(
                call.phase == "landing_merge" and str(first.number) in call.path
                for call in provider.calls
            ),
            0,
        )

        completed = self.pump_until(
            fleet=fleet,
            states=states,
            predicate=lambda views: views[0].status == "completed",
        )[0]
        self.assertEqual(completed.status, "completed")
        self.assertEqual((first.state, second.state), ("OPEN", "MERGED"))
        self.assertEqual(second.base_sha_at_merge, initial_base_sha)
        self.assertEqual(sum(call.phase == "landing_merge" for call in provider.calls), 1)
        with self.store._session_factory() as session:
            preparations = tuple(
                OrdinaryAgentLandingPreparation.model_validate(row.payload)
                for row in session.scalars(
                    select(LaunchplaneOrdinaryAgentLandingPreparationRow).order_by(
                        LaunchplaneOrdinaryAgentLandingPreparationRow.action_ordinal
                    )
                )
            )
        self.assertEqual(len(preparations), 2)
        self.assertIsNone(preparations[0].effect_id)
        self.assertEqual(preparations[1].expected_base_sha, initial_base_sha)
        self.assertEqual(
            sum(effect.command.kind == "pull_request_landing" for effect in self._effects()),
            1,
        )
        no_op_custody = self.store.read_ordinary_agent_custody_issue_attempt(
            preparations[0].custody_attempt_id
        )
        self.assertEqual(
            (no_op_custody.state, no_op_custody.close_reason),
            (
                "closed",
                "confirmed_revoked",
            ),
        )

    def _effects(self) -> tuple[OrdinaryAgentEffectRecord, ...]:
        with self.store._session_factory() as session:
            return tuple(
                OrdinaryAgentEffectRecord.model_validate(row.payload)
                for row in session.scalars(
                    select(LaunchplaneOrdinaryAgentEffectRow).order_by(
                        LaunchplaneOrdinaryAgentEffectRow.request_id,
                        LaunchplaneOrdinaryAgentEffectRow.action_ordinal,
                    )
                )
            )


if __name__ == "__main__":
    unittest.main()
