from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from control_plane.contracts.mediaforce_release import (
    MediaforceBackup,
    MediaforceCompletionReceipt,
    MediaforceHostReceipt,
    MediaforceQuiescence,
    MediaforceReleasePlan,
    MediaforceReleaseRequest,
    MediaforceReleaseResult,
    MediaforceRuntime,
    MediaforceRuntimeCheck,
)
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.mediaforce_release import (
    MEDIAFORCE_RELEASE_EXECUTION_ROUTE,
    MediaforceReleaseAdapter,
    MediaforceReleaseAuthority,
    MediaforceReleaseNotReady,
    request_mediaforce_release,
    run_mediaforce_release,
)
from control_plane.provider_operations import ProviderMutationUnknownError
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_release_review import HEAD, decision, profile, seed


class ReleaseHost:
    def __init__(self, plan: MediaforceReleasePlan) -> None:
        self.plan = plan
        self.runtime = plan.previous
        self.events: list[str] = []
        self.work: dict[str, int | bool] = {}
        self.backup_verified = True
        self.fail_candidate = False
        self.fail_recovery = False
        self.timeout_at = ""
        self.receipt: MediaforceCompletionReceipt | None = None
        self.evidence_age = timedelta()

    def fields(self, fence: MediaforceHostReceipt) -> dict[str, object]:
        return {
            "operation_key": fence.operation_key,
            "target_id": fence.target_id,
            "fence_token": fence.fence_token,
            "observed_at": datetime.now(timezone.utc) - self.evidence_age,
        }

    def event(self, name: str) -> None:
        self.events.append(name)
        if self.timeout_at == name:
            raise ProviderMutationUnknownError("Host response lost.")

    def fence(self, plan: MediaforceReleasePlan, operation_key: str) -> MediaforceHostReceipt:
        self.event("fence")
        return MediaforceHostReceipt(
            operation_key=operation_key,
            target_id=plan.target_id,
            fence_token="host-generation-1",
            observed_at=datetime.now(timezone.utc),
        )

    def quiescence(self, fence: MediaforceHostReceipt) -> MediaforceQuiescence:
        self.event("quiescence")
        return MediaforceQuiescence.model_validate(
            {
                **self.fields(fence),
                "pending_encode": 0,
                "active_encode": 0,
                "active_calibration": 0,
                "unfinished_scan_rows": 0,
                "active_evidence": 0,
                "scan_jobs": 0,
                "pending_publish": 0,
                "publishing_locks": 0,
                "remote_workers": 0,
                "admissions_closed": True,
                "producers_quiesced": True,
                **self.work,
            }
        )

    def backup(self, plan: MediaforceReleasePlan, fence: MediaforceHostReceipt) -> MediaforceBackup:
        self.event("backup")
        return MediaforceBackup.model_validate(
            {
                **self.fields(fence),
                "database_id": plan.database_id,
                "runtime": self.runtime,
                "backup_record_id": "verified-snapshot",
                "backup_sha256": "d" * 64,
                "verified": self.backup_verified,
            }
        )

    def replace(self, runtime: MediaforceRuntime, fence: MediaforceHostReceipt) -> None:
        self.runtime = runtime
        self.event("replace" if runtime == self.plan.candidate else "recover")

    def check(self, fence: MediaforceHostReceipt) -> MediaforceRuntimeCheck:
        self.event("check")
        failed = self.fail_candidate if self.runtime == self.plan.candidate else self.fail_recovery
        return MediaforceRuntimeCheck.model_validate(
            {
                **self.fields(fence),
                "runtime": self.runtime,
                "healthy": not failed,
                "post_checks_passed": not failed,
            }
        )

    def finish(
        self, result: MediaforceReleaseResult, fence: MediaforceHostReceipt
    ) -> MediaforceCompletionReceipt:
        self.receipt = MediaforceCompletionReceipt.model_validate(
            {
                **self.fields(fence),
                "result": result,
                "admissions_resumed": True,
            }
        )
        self.event("finish")
        return self.receipt

    def observe(
        self, plan: MediaforceReleasePlan, operation_key: str
    ) -> MediaforceCompletionReceipt | None:
        self.event("observe")
        return self.receipt


class MediaforceReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(directory.name) / 'records.db'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        seed(self.store)
        self.profile = profile().model_copy(
            update={
                "driver_id": "mediaforce-macos",
                "release_on_acceptance": "promote",
            }
        )
        self.store.write_product_profile_record(self.profile)
        self.decision = decision(self.store).model_copy(update={"release_start": "promote"})
        self.store.write_release_review_decision_record(self.decision)
        self.request = MediaforceReleaseRequest(product=self.profile.product, source_commit=HEAD)
        self.plan = MediaforceReleasePlan(
            request=self.request,
            repository=self.profile.repository,
            context="example-site",
            target_id="macos-host/login-item",
            database_id="database-volume",
            source_provenance_record_id="main-build-provenance",
            recovery_deployment_record_id="passing-production-deployment",
            candidate=MediaforceRuntime(
                version=self.decision.checklist.candidate, artifact_sha256="a" * 64
            ),
            previous=MediaforceRuntime(
                version=self.decision.checklist.production, artifact_sha256="b" * 64
            ),
            database_compatibility="backward-compatible",
        )
        self.authority = MediaforceReleaseAuthority(self.store, lambda _request: self.plan)
        self.host = ReleaseHost(self.plan)

    def request_release(self, key: str = "request-1") -> None:
        result = request_mediaforce_release(
            authority=self.authority,
            request=self.request,
            idempotency_key=key,
            requester_scope="agent:read-request",
            trace_id="request-trace",
        )
        self.assertIn(result.status, ("completed", "replayed"))

    def run_release(self, key: str = "request-1") -> object:
        return run_mediaforce_release(
            authority=self.authority,
            host=self.host,
            requester_scope="agent:read-request",
            request_key=key,
            worker_id="system-worker",
            trace_id="worker-trace",
        )

    def run_record(self) -> MediaforceReleaseResult:
        self.request_release()
        result = self.run_release()
        self.assertEqual(getattr(result, "status"), "completed")
        return MediaforceReleaseResult.model_validate(getattr(result, "response_payload"))

    def test_request_records_intent_without_acceptance_or_host_effect(self) -> None:
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"release_on_acceptance": "held"})
        )
        self.request_release()
        self.assertEqual(self.host.events, [])
        with self.assertRaises(MediaforceReleaseNotReady):
            self.run_release()
        self.assertEqual(self.host.events, [])

    def test_success_persists_acceptance_backup_quiescence_and_exact_runtime(self) -> None:
        result = self.run_record()
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.decision_record_id, self.decision.record_id)
        self.assertIsNotNone(result.backup)
        self.assertIsNotNone(result.quiescence)
        self.assertEqual(self.host.runtime, self.plan.candidate)
        before = list(self.host.events)
        replay = self.run_release()
        self.assertEqual(getattr(replay, "status"), "replayed")
        self.assertEqual(self.host.events, before)

    def test_every_work_domain_and_producer_fence_blocks_replacement(self) -> None:
        domains = (
            "pending_encode",
            "active_encode",
            "active_calibration",
            "unfinished_scan_rows",
            "active_evidence",
            "scan_jobs",
            "pending_publish",
            "publishing_locks",
            "remote_workers",
        )
        for index, domain in enumerate(domains + ("admissions_closed", "producers_quiesced")):
            with self.subTest(domain=domain):
                key = f"busy-{index}"
                self.request_release(key)
                self.host.work = {domain: 1 if domain in domains else False}
                self.host.events.clear()
                result = self.run_release(key)
                self.assertEqual(getattr(result, "response_payload")["status"], "blocked")
                self.assertNotIn("backup", self.host.events)
                self.assertNotIn("replace", self.host.events)

    def test_backup_must_be_verified(self) -> None:
        self.host.backup_verified = False
        result = self.run_record()
        self.assertEqual(result.reason, "backup_unverified")
        self.assertNotIn("replace", self.host.events)

    def test_stale_or_wrong_target_evidence_is_held(self) -> None:
        self.host.evidence_age = timedelta(minutes=1)
        self.request_release()
        result = self.run_release()
        self.assertEqual(getattr(result, "status"), "reconcile_required")
        self.assertNotIn("finish", self.host.events)
        self.assertNotIn("replace", self.host.events)

    def test_acceptance_change_during_backup_stops_before_replace(self) -> None:
        backup = self.host.backup

        def changed(plan: MediaforceReleasePlan, fence: MediaforceHostReceipt) -> MediaforceBackup:
            result = backup(plan, fence)
            self.store.write_product_profile_record(
                self.profile.model_copy(update={"release_on_acceptance": "held"})
            )
            return result

        with patch.object(self.host, "backup", side_effect=changed):
            result = self.run_record()
        self.assertEqual(result.reason, "acceptance_changed")
        self.assertNotIn("replace", self.host.events)

    def test_work_is_rechecked_after_backup(self) -> None:
        backup = self.host.backup

        def changed(plan: MediaforceReleasePlan, fence: MediaforceHostReceipt) -> MediaforceBackup:
            result = backup(plan, fence)
            self.host.work = {"remote_workers": 1}
            return result

        with patch.object(self.host, "backup", side_effect=changed):
            result = self.run_record()
        self.assertEqual(result.reason, "work_not_idle")
        self.assertNotIn("replace", self.host.events)

    def test_failed_post_checks_recover_pinned_runtime_after_forward_hold(self) -> None:
        self.host.fail_candidate = True
        replace = self.host.replace

        def changed(runtime: MediaforceRuntime, fence: MediaforceHostReceipt) -> None:
            replace(runtime, fence)
            self.store.write_product_profile_record(
                self.profile.model_copy(update={"release_on_acceptance": "held"})
            )

        with patch.object(self.host, "replace", side_effect=changed):
            result = self.run_record()
        self.assertEqual(result.status, "failed_recovered")
        self.assertEqual(self.host.runtime, self.plan.previous)
        self.assertIsNotNone(result.recovery_check)

    def test_failed_recovery_never_resumes_admissions(self) -> None:
        self.host.fail_candidate = self.host.fail_recovery = True
        self.request_release()
        result = self.run_release()
        self.assertEqual(getattr(result, "status"), "reconcile_required")
        self.assertNotIn("finish", self.host.events)

    def test_lost_replace_response_is_never_replayed_or_guessed_from_health(self) -> None:
        self.host.timeout_at = "replace"
        self.request_release()
        first = self.run_release()
        self.assertEqual(getattr(first, "status"), "reconcile_required")
        second = self.run_release()
        self.assertEqual(getattr(second, "status"), "reconcile_required")
        self.assertEqual(self.host.events.count("replace"), 1)
        self.assertNotIn("recover", self.host.events)
        self.assertNotIn("finish", self.host.events)
        self.request_release("request-2")
        busy = self.run_release("request-2")
        self.assertEqual(getattr(busy, "status"), "target_busy")

    def test_lost_finish_response_adopts_exact_host_receipt(self) -> None:
        self.host.timeout_at = "finish"
        self.request_release()
        first = self.run_release()
        self.assertEqual(getattr(first, "status"), "reconcile_required")
        second = self.run_release()
        self.assertEqual(getattr(second, "status"), "adopted")
        self.assertEqual(self.host.events.count("replace"), 1)
        self.assertEqual(getattr(second, "response_payload")["status"], "passed")

    def test_changed_resolver_plan_does_not_retarget_recorded_request(self) -> None:
        self.request_release()
        self.plan = self.plan.model_copy(update={"target_id": "other-host"})
        with self.assertRaises(MediaforceReleaseNotReady):
            self.run_release()
        self.assertEqual(self.host.events, [])

    def test_request_key_conflict_preserves_original_commit(self) -> None:
        self.request_release()
        other = self.request.model_copy(update={"source_commit": "c" * 40})
        plan = self.plan.model_copy(
            update={
                "request": other,
                "candidate": self.plan.candidate.model_copy(
                    update={
                        "version": self.plan.candidate.version.model_copy(
                            update={"source_commit": other.source_commit}
                        )
                    }
                ),
            }
        )
        authority = MediaforceReleaseAuthority(self.store, lambda _request: plan)
        result = request_mediaforce_release(
            authority=authority,
            request=other,
            idempotency_key="request-1",
            requester_scope="agent:read-request",
            trace_id="other",
        )
        self.assertEqual(result.status, "conflict")
        self.assertEqual(self.run_record().plan.request.source_commit, self.request.source_commit)

    def test_unknown_or_counts_only_work_cannot_construct_quiescence(self) -> None:
        with self.assertRaises(ValidationError):
            MediaforceQuiescence.model_validate({"counts": {"pending_encode": 0}})

    def test_lease_loss_before_replace_holds_fence(self) -> None:
        self.request_release()
        with patch.object(
            self.store,
            "checkpoint_mutation_provider_effect",
            side_effect=RuntimeError("lease lost"),
        ):
            result = self.run_release()
        self.assertEqual(getattr(result, "status"), "reconcile_required")
        self.assertEqual(self.host.events, [])

    def test_wrong_database_backup_keeps_admissions_held(self) -> None:
        backup = self.host.backup

        def wrong(plan: MediaforceReleasePlan, fence: MediaforceHostReceipt) -> MediaforceBackup:
            return backup(plan, fence).model_copy(update={"database_id": "other-database"})

        self.request_release()
        with patch.object(self.host, "backup", side_effect=wrong):
            result = self.run_release()
        self.assertEqual(getattr(result, "status"), "reconcile_required")
        self.assertNotIn("finish", self.host.events)

    def test_observer_rejects_receipt_from_another_operation(self) -> None:
        self.run_record()
        assert self.host.receipt is not None
        adapter = MediaforceReleaseAdapter(
            self.plan, self.authority, self.host, self.decision.record_id
        )
        observed = adapter.observe(
            "another-operation", "resume_admissions", adapter.reconciliation_key()
        )
        self.assertEqual(observed.outcome, "unknown")

    def test_no_recorded_request_cannot_start_worker(self) -> None:
        with self.assertRaises(MediaforceReleaseNotReady):
            self.run_release()

    def test_standing_director_acceptance_uses_immutable_admin_id(self) -> None:
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"release_on_acceptance": "director_standing"})
        )
        self.store.write_release_review_decision_record(
            self.decision.model_copy(update={"acceptance_source": "director_standing"})
        )
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "github_humans": [
                    {
                        "github_ids": [int(self.decision.actor_github_id)],
                        "roles": ["admin"],
                        "actions": ["authz_policy_grant.write"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                    }
                ],
            }
        )
        self.store.seed_authz_policy_if_absent(
            LaunchplaneAuthzPolicyRecord(
                record_id="active-admin-policy",
                source="test",
                updated_at=self.decision.decided_at,
                policy=policy,
            )
        )
        self.assertEqual(self.run_record().status, "passed")

    def test_standing_director_requires_current_admin_policy(self) -> None:
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"release_on_acceptance": "director_standing"})
        )
        self.store.write_release_review_decision_record(
            self.decision.model_copy(update={"acceptance_source": "director_standing"})
        )
        self.request_release()
        with self.assertRaises(MediaforceReleaseNotReady):
            self.run_release()
        self.assertEqual(self.host.events, [])

    def test_small_host_clock_skew_does_not_hold_a_valid_release(self) -> None:
        self.host.evidence_age = -timedelta(milliseconds=1)
        self.assertEqual(self.run_record().status, "passed")

    def test_unbounded_future_host_evidence_keeps_admissions_held(self) -> None:
        self.host.evidence_age = -timedelta(minutes=1)
        self.request_release()
        result = self.run_release()
        self.assertEqual(getattr(result, "status"), "reconcile_required")
        self.assertNotIn("finish", self.host.events)

    def test_request_replay_does_not_depend_on_live_resolver(self) -> None:
        self.request_release()
        resolver = unittest.mock.Mock(side_effect=ValueError("source temporarily unavailable"))
        authority = MediaforceReleaseAuthority(self.store, resolver)
        result = request_mediaforce_release(
            authority=authority,
            request=self.request,
            idempotency_key="request-1",
            requester_scope="agent:read-request",
            trace_id="replay",
        )
        self.assertEqual(result.status, "replayed")
        self.assertEqual(
            MediaforceReleasePlan.model_validate(result.response_payload["plan"]), self.plan
        )
        resolver.assert_not_called()

    def test_worker_preserves_original_intent_in_durable_store(self) -> None:
        self.request_release()
        result = self.run_release()
        record = getattr(result, "record")
        self.assertEqual(record.route_path, MEDIAFORCE_RELEASE_EXECUTION_ROUTE)
        intent = self.store.read_original_mutation_intent(record)
        self.assertEqual(MediaforceReleasePlan.model_validate(intent["plan"]), self.plan)
