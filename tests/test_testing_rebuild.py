"""Serving behavior and original-operation recovery of testing rebuilds."""

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.testing_rebuild import (
    TestingCopy,
    TestingCopyReadiness,
    TestingCopyReceipt,
    TestingRebuildPlan,
    TestingRebuildRecord,
    TestingRouteObservation,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_rebuild_storage import RecordTestingRebuildStore
from control_plane.workflows.testing_rebuild import execute_testing_rebuild


def rebuild_plan(source: str = "production") -> TestingRebuildPlan:
    copies = []
    for name in ("old", "new"):
        copies.append(
            TestingCopy(
                copy_id=name,
                target_id=name + "-target",
                private_url=f"http://{name}:8069",
                resource_binding=name + "-storage",
                runtime_identity=RuntimeIdentity(
                    product="example",
                    context="example",
                    instance="testing",
                    deployment_record_id=name + "-deployment",
                    artifact_id=name + "-artifact",
                    source_git_ref=name + "-source",
                    image_digest="sha256:" + "a" * 64,
                ),
            )
        )
    return TestingRebuildPlan.model_validate(
        {
            "operation_id": "rebuild-example",
            "product": "example",
            "context": "example",
            "base_url": "https://testing.example.invalid",
            "route_id": "testing-route",
            "authority_digest": "b" * 64,
            "source": source,
            "source_reference": "" if source == "empty" else "managed-source",
            "old": copies[0],
            "new": copies[1],
        }
    )


class TestingProvider:
    def __init__(self, plan: TestingRebuildPlan) -> None:
        self.plan = plan
        self.serving = plan.old
        self.published = plan.old
        self.base_url = plan.base_url
        self.route_id = plan.route_id
        self.host = plan.host
        self.publish_flags: list[bool] = []
        self.pending_switch = False
        self.refuse_stop = False
        self.stop_refused_before_dispatch = False
        self.lose_stop_reply = False
        self.old_running = True
        self.actions: list[str] = []
        self.fail_prepare = False
        self.fail_readiness = False
        self.bad_public = False
        self.lose_switch_reply = False
        self.lose_publish_reply = False
        self.deny_retirement = False
        self.foreign_host = False
        self.stale_readiness = False
        self.fail_second_public_probe = False
        self.fail_after_publication = False
        self.new_public_probes = 0
        self.now = datetime.now(UTC)

    def exclusive(self, plan: TestingRebuildPlan) -> nullcontext[None]:
        return nullcontext()

    def guard(self, plan: TestingRebuildPlan) -> None:
        if self.deny_retirement and self.published == plan.new:
            raise PermissionError("fixture authorization_denied")

    def current_route(self, plan: TestingRebuildPlan) -> TestingRouteObservation:
        if self.serving == plan.new:
            self.new_public_probes += 1
            if self.fail_second_public_probe and self.new_public_probes > 1:
                self.bad_public = True
            if self.fail_after_publication and self.published == plan.new:
                self.bad_public = True
        return TestingRouteObservation(
            route_id=self.route_id,
            host="foreign.invalid" if self.foreign_host else self.host,
            target_url=self.serving.private_url,
            slot_id=self.serving.copy_id,
            applied=True,
            authoritative_router_count=1,
            observed_at=self.now,
            public_health=HealthcheckEvidence(
                verified=True,
                urls=(self.base_url + "/launchplane/health",),
                timeout_seconds=10,
                status="fail" if self.bad_public and self.serving == plan.new else "pass",
                observed_runtime_identity=self.serving.runtime_identity,
            ),
        )

    def prepare(self, plan: TestingRebuildPlan) -> TestingCopyReceipt:
        self.actions.append("prepare")
        if self.serving != plan.old or self.published != plan.old or not self.old_running:
            raise AssertionError("The old copy must serve throughout preparation.")
        if self.fail_prepare:
            raise ValueError("fixture restore failed")
        return TestingCopyReceipt(
            plan_digest=plan.digest,
            copy_id=plan.new.copy_id,
            evidence_id="independent-copy",
            independent_resources_verified=True,
            local_credentials_verified=True,
            live_connections_removed_before_start=True,
        )

    def readiness(self, plan: TestingRebuildPlan) -> TestingCopyReadiness:
        self.actions.append("readiness")
        if self.fail_readiness:
            raise ValueError("fixture unhealthy candidate")
        return TestingCopyReadiness(
            copy_id=plan.new.copy_id,
            host=plan.host,
            observed_at=self.now - timedelta(minutes=5) if self.stale_readiness else self.now,
            runtime_identity=plan.new.runtime_identity,
            healthy=True,
            evidence_id="private-health",
        )

    def switch(
        self,
        plan: TestingRebuildPlan,
        previous: TestingCopy,
        desired: TestingCopy,
        *,
        reconcile_only: bool,
    ) -> None:
        if self.serving != previous:
            raise ValueError("fixture route compare-and-switch conflict")
        if self.pending_switch and reconcile_only:
            raise TimeoutError("fixture switch still in flight")
        self.actions.append("switch:" + desired.copy_id)
        if self.pending_switch:
            raise TimeoutError("fixture switch in flight")
        self.serving = desired
        if self.lose_switch_reply:
            self.lose_switch_reply = False
            raise TimeoutError("fixture lost switch reply")

    def publish(
        self, plan: TestingRebuildPlan, desired: TestingCopy, *, forward_started: bool
    ) -> None:
        if self.serving != desired or (desired == plan.new and self.bad_public):
            raise AssertionError("Only a publicly healthy copy may become authoritative.")
        self.publish_flags.append(forward_started)
        self.actions.append("publish:" + desired.copy_id)
        self.published = desired
        if self.lose_publish_reply:
            self.lose_publish_reply = False
            raise TimeoutError("fixture lost publication reply")

    def retire_old(self, plan: TestingRebuildPlan) -> None:
        if self.serving != plan.new or self.published != plan.new or self.bad_public:
            raise AssertionError("Retirement requires a healthy authoritative successor.")
        if self.refuse_stop:
            self.stop_refused_before_dispatch = True
            raise PermissionError("fixture stop refused before dispatch")
        self.actions.append("retire")
        self.old_running = False
        if self.lose_stop_reply:
            raise TimeoutError("fixture stop reply lost")

    def unstarted_retirement(self, plan: TestingRebuildPlan) -> TestingCopyReadiness | None:
        if not self.stop_refused_before_dispatch or not self.old_running:
            return None
        return TestingCopyReadiness(
            copy_id=plan.old.copy_id,
            host=self.host,
            observed_at=self.now,
            runtime_identity=plan.old.runtime_identity,
            healthy=True,
            evidence_id="original-stop-proven-undispatched",
        )


class TestingRebuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database_url = f"sqlite+pysqlite:///{Path(self.temporary.name) / 'state.sqlite'}"
        self.records = PostgresRecordStore(database_url=self.database_url)
        self.records.ensure_schema()
        self.store = RecordTestingRebuildStore(self.records)
        self.plan = rebuild_plan()
        self.provider = TestingProvider(self.plan)

    def run_rebuild(self) -> TestingRebuildRecord:
        return execute_testing_rebuild(
            self.plan, self.store, self.provider, clock=lambda: self.provider.now
        )

    def test_rebuild_and_both_restore_sources_keep_the_address(self) -> None:
        for source in ("empty", "production", "upstream"):
            with self.subTest(source=source):
                self.plan = rebuild_plan(source).model_copy(update={"operation_id": source})
                self.provider = TestingProvider(self.plan)
                result = self.run_rebuild()
                self.assertEqual(result.step, "complete")
                self.assertEqual(self.provider.serving, self.plan.new)
                self.assertFalse(self.provider.old_running)
                self.assertLess(
                    self.provider.actions.index("readiness"),
                    self.provider.actions.index("switch:new"),
                )
                self.assertLess(
                    self.provider.actions.index("publish:new"),
                    self.provider.actions.index("retire"),
                )

    def test_failed_restore_or_readiness_keeps_the_old_copy_serving(self) -> None:
        for failure in ("fail_prepare", "fail_readiness", "stale_readiness"):
            with self.subTest(failure=failure):
                self.plan = rebuild_plan().model_copy(update={"operation_id": failure})
                self.provider = TestingProvider(self.plan)
                setattr(self.provider, failure, True)
                with self.assertRaises(ValueError):
                    self.run_rebuild()
                self.assertEqual(self.provider.serving, self.plan.old)
                self.assertEqual(self.provider.published, self.plan.old)
                self.assertTrue(self.provider.old_running)
                self.assertNotIn("switch:new", self.provider.actions)
                self.assertNotIn("retire", self.provider.actions)

    def test_failed_public_health_restores_the_old_route_and_preserves_copy(self) -> None:
        self.provider.bad_public = True
        result = self.run_rebuild()
        self.assertEqual(result.step, "failed")
        self.assertEqual(self.provider.serving, self.plan.old)
        self.assertEqual(self.provider.published, self.plan.old)
        self.assertTrue(self.provider.old_running)
        self.assertNotIn("retire", self.provider.actions)
        self.assertEqual(self.provider.publish_flags, [False])

    def test_health_failure_before_publication_restores_old_serving_and_metadata(self) -> None:
        self.provider.fail_second_public_probe = True
        result = self.run_rebuild()
        self.assertEqual(result.step, "failed")
        self.assertEqual(self.provider.serving, self.plan.old)
        self.assertEqual(self.provider.published, self.plan.old)
        self.assertTrue(self.provider.old_running)
        self.assertNotIn("retire", self.provider.actions)

    def test_missing_stale_or_wrong_address_public_evidence_never_proves_failure_or_success(
        self,
    ) -> None:
        for failure in (
            "missing",
            "unverified",
            "skipped",
            "stale",
            "future",
            "private-url",
            "missing-identity",
        ):
            with self.subTest(failure=failure):
                self.plan = rebuild_plan().model_copy(update={"operation_id": failure})
                self.provider = TestingProvider(self.plan)
                original = self.provider.current_route

                def uncertain(plan: TestingRebuildPlan) -> TestingRouteObservation:
                    observed = original(plan)
                    if observed.slot_id != plan.new.copy_id:
                        return observed
                    health = observed.public_health
                    assert health is not None
                    if failure == "missing":
                        return observed.model_copy(update={"public_health": None})
                    if failure in {"stale", "future"}:
                        return observed.model_copy(
                            update={
                                "observed_at": self.provider.now
                                + (
                                    timedelta(minutes=5)
                                    if failure == "future"
                                    else -timedelta(minutes=5)
                                )
                            }
                        )
                    updates: dict[str, dict[str, object]] = {
                        "unverified": {"verified": False},
                        "skipped": {"status": "skipped"},
                        "private-url": {"urls": (plan.new.private_url + "/health",)},
                        "missing-identity": {"observed_runtime_identity": None},
                    }
                    return observed.model_copy(
                        update={"public_health": health.model_copy(update=updates[failure])}
                    )

                with patch.object(self.provider, "current_route", side_effect=uncertain):
                    with self.assertRaises(ValueError):
                        self.run_rebuild()
                self.assertEqual(self.store.load(self.plan).step, "switching")
                self.assertNotIn("switch:old", self.provider.actions)
                self.assertNotIn("publish:new", self.provider.actions)
                self.assertNotIn("retire", self.provider.actions)
                self.assertTrue(self.provider.old_running)
                self.assertEqual(self.run_rebuild().step, "complete")

    def test_pending_switch_is_observed_without_second_dispatch(self) -> None:
        self.provider.pending_switch = True
        for _ in range(2):
            with self.assertRaises(TimeoutError):
                self.run_rebuild()
        self.assertEqual(self.provider.actions.count("switch:new"), 1)
        self.assertEqual(self.store.load(self.plan).step, "switching")
        self.assertTrue(self.provider.old_running)
        self.provider.serving = self.plan.new
        self.provider.pending_switch = False
        self.assertEqual(self.run_rebuild().step, "complete")

    def test_pending_inverse_switch_reconciles_without_second_dispatch(self) -> None:
        self.provider.bad_public = True
        original_switch = self.provider.switch

        def switch(*args: object, **kwargs: object) -> None:
            self.provider.pending_switch = args[2] == self.plan.old
            original_switch(*args, **kwargs)

        with patch.object(self.provider, "switch", side_effect=switch):
            for _ in range(2):
                with self.assertRaises(TimeoutError):
                    self.run_rebuild()
        self.assertEqual(self.provider.actions.count("switch:old"), 1)
        self.assertTrue(self.store.load(self.plan).rollback_started)
        self.provider.serving = self.plan.old
        self.provider.pending_switch = False
        self.assertEqual(self.run_rebuild().step, "failed")
        self.assertTrue(self.provider.old_running)

    def test_store_preserves_terminal_receipt_against_stale_writer(self) -> None:
        terminal = self.run_rebuild()
        stale = terminal.model_copy(update={"step": "retiring", "ended_at": None})
        with self.assertRaises(ValueError):
            self.store.save(stale)
        self.assertEqual(self.store.load(self.plan), terminal)
        self.store.save(terminal)

    def test_store_never_clears_effect_intents_or_regresses_progress(self) -> None:
        self.provider.lose_publish_reply = True
        with self.assertRaises(TimeoutError):
            self.run_rebuild()
        record = self.store.load(self.plan)
        for flag in ("publication_started", "retirement_started", "rollback_started"):
            with self.subTest(flag=flag):
                record = record.model_copy(update={flag: True})
                self.store.save(record)
                with self.assertRaises(ValueError):
                    self.store.save(record.model_copy(update={flag: False}))
                self.assertEqual(self.store.load(self.plan), record)
        with self.assertRaises(ValueError):
            self.store.save(record.model_copy(update={"step": "switching"}))
        self.assertEqual(self.store.load(self.plan), record)

    def test_undispatched_retirement_proof_and_rollback_are_saved_together(self) -> None:
        self.provider.refuse_stop = True
        with self.assertRaises(PermissionError):
            self.run_rebuild()
        self.provider.bad_public = True
        original_save = self.store.save
        checkpoints = []

        def save(record: TestingRebuildRecord) -> None:
            checkpoints.append(record)
            original_save(record)

        with patch.object(self.store, "save", side_effect=save):
            self.assertEqual(self.run_rebuild().step, "failed")
        recovery = next(record for record in checkpoints if record.retirement_recovery)
        self.assertEqual(recovery.step, "rolling_back")

    def test_stable_address_change_is_refused_against_existing_route_observation(self) -> None:
        self.plan = self.plan.model_copy(update={"base_url": "https://another.invalid"})
        with self.assertRaises(ValueError):
            self.run_rebuild()
        self.assertFalse(self.provider.actions)

    def test_retirement_refusal_has_a_proven_undispatched_recovery_path(self) -> None:
        self.provider.refuse_stop = True
        with self.assertRaises(PermissionError):
            self.run_rebuild()
        self.assertTrue(self.store.load(self.plan).retirement_started)
        self.assertNotIn("switch:old", self.provider.actions)
        self.provider.bad_public = True
        result = self.run_rebuild()
        self.assertEqual(result.step, "failed")
        self.assertIsNotNone(result.retirement_recovery)
        self.assertEqual(self.provider.serving, self.plan.old)
        self.assertTrue(self.provider.old_running)
        self.assertNotIn("retire", self.provider.actions)

    def test_unknown_stop_cannot_rollback_even_if_candidate_now_unhealthy(self) -> None:
        self.provider.lose_stop_reply = True
        with self.assertRaises(TimeoutError):
            self.run_rebuild()
        self.provider.bad_public = True
        with self.assertRaises(ValueError):
            self.run_rebuild()
        self.assertEqual(self.store.load(self.plan).step, "retiring")
        self.assertNotIn("switch:old", self.provider.actions)
        self.assertFalse(self.provider.old_running)

    def test_health_failure_before_old_stop_restores_old_route_and_metadata(self) -> None:
        self.provider.fail_after_publication = True
        self.assertEqual(self.run_rebuild().step, "failed")
        self.assertEqual(self.provider.serving, self.plan.old)
        self.assertEqual(self.provider.published, self.plan.old)
        self.assertTrue(self.provider.old_running)
        self.assertNotIn("retire", self.provider.actions)

    def test_uncertain_publication_outcome_must_be_resolved_before_inverse_metadata(self) -> None:
        self.provider.lose_publish_reply = True
        with self.assertRaises(TimeoutError):
            self.run_rebuild()
        self.assertTrue(self.store.load(self.plan).publication_started)
        self.provider.bad_public = True

        def unresolved(
            plan: TestingRebuildPlan, desired: TestingCopy, *, forward_started: bool
        ) -> None:
            self.assertTrue(forward_started)
            raise TimeoutError("fixture original publication outcome unavailable")

        with patch.object(self.provider, "publish", side_effect=unresolved):
            with self.assertRaises(TimeoutError):
                self.run_rebuild()
        self.assertEqual(self.store.load(self.plan).step, "rolling_back")
        self.assertEqual(self.provider.serving, self.plan.old)
        self.assertEqual(self.provider.published, self.plan.new)
        self.assertTrue(self.provider.old_running)
        self.assertEqual(self.run_rebuild().step, "failed")
        self.assertEqual(self.provider.publish_flags, [True, True])
        self.assertNotIn("retire", self.provider.actions)

    def test_operation_key_is_scoped_to_its_testing_lane(self) -> None:
        first = self.run_rebuild()
        other_context = "another-context"
        self.plan = TestingRebuildPlan.model_validate(
            {
                **self.plan.model_dump(),
                "context": other_context,
                "old": self.plan.old.model_copy(
                    update={
                        "runtime_identity": self.plan.old.runtime_identity.model_copy(
                            update={"context": other_context}
                        )
                    }
                ),
                "new": self.plan.new.model_copy(
                    update={
                        "runtime_identity": self.plan.new.runtime_identity.model_copy(
                            update={"context": other_context}
                        )
                    }
                ),
            }
        )
        self.provider = TestingProvider(self.plan)
        second = self.run_rebuild()
        self.assertEqual(second.step, "complete")
        self.assertEqual(self.store.load(first.plan), first)
        self.assertEqual(self.store.load(second.plan), second)

    def test_a_second_rebuild_reuses_the_same_address_and_route(self) -> None:
        first = self.run_rebuild()
        next_copy = self.plan.new.model_copy(
            update={
                "copy_id": "third",
                "target_id": "third-target",
                "private_url": "http://third:8069",
                "resource_binding": "third-storage",
                "runtime_identity": self.plan.new.runtime_identity.model_copy(
                    update={
                        "deployment_record_id": "third-deployment",
                        "artifact_id": "third-artifact",
                    }
                ),
            }
        )
        self.plan = self.plan.model_copy(
            update={
                "operation_id": "second-rebuild",
                "old": self.plan.new,
                "new": next_copy,
            }
        )
        self.provider.old_running = True
        second = self.run_rebuild()
        self.assertEqual(second.step, "complete")
        self.assertEqual(self.store.load(first.plan), first)
        self.assertEqual(execute_testing_rebuild(first.plan, self.store, self.provider), first)
        self.assertEqual(self.provider.serving, next_copy)

    def test_lost_publication_reply_then_bad_health_restores_both_original_authorities(
        self,
    ) -> None:
        self.provider.lose_publish_reply = True
        with self.assertRaises(TimeoutError):
            self.run_rebuild()
        self.assertEqual(self.provider.published, self.plan.new)
        self.provider.bad_public = True
        self.assertEqual(self.run_rebuild().step, "failed")
        self.assertEqual(self.provider.serving, self.plan.old)
        self.assertEqual(self.provider.published, self.plan.old)
        self.assertTrue(self.provider.old_running)

    def test_lost_switch_reply_is_reconciled_after_store_restart_without_reswitch(self) -> None:
        self.provider.lose_switch_reply = True
        with self.assertRaises(TimeoutError):
            self.run_rebuild()
        self.assertEqual(self.store.load(self.plan).step, "switching")
        self.assertTrue(self.provider.old_running)
        self.records = PostgresRecordStore(database_url=self.database_url)
        self.store = RecordTestingRebuildStore(self.records)
        result = self.run_rebuild()
        self.assertEqual(result.step, "complete")
        self.assertEqual(self.provider.actions.count("switch:new"), 1)

    def test_lost_publish_reply_does_not_retire_old_until_publication_is_reconciled(self) -> None:
        self.provider.lose_publish_reply = True
        with self.assertRaises(TimeoutError):
            self.run_rebuild()
        self.assertEqual(self.store.load(self.plan).step, "publishing")
        self.assertTrue(self.provider.old_running)
        self.assertEqual(self.run_rebuild().step, "complete")
        self.assertEqual(self.provider.actions.count("prepare"), 1)
        self.assertEqual(self.provider.actions.count("switch:new"), 1)

    def test_authorization_denial_stops_without_inverse_or_retirement(self) -> None:
        self.provider.deny_retirement = True
        with self.assertRaisesRegex(PermissionError, "authorization_denied"):
            self.run_rebuild()
        self.assertEqual(self.store.load(self.plan).step, "retiring")
        self.assertTrue(self.provider.old_running)
        self.assertNotIn("retire", self.provider.actions)
        self.assertNotIn("switch:old", self.provider.actions)

    def test_foreign_route_authority_refuses_before_preparation(self) -> None:
        self.provider.foreign_host = True
        with self.assertRaises(ValueError):
            self.run_rebuild()
        self.assertEqual(self.provider.actions, [])

    def test_changed_plan_cannot_replace_an_uncertain_operation(self) -> None:
        self.provider.fail_readiness = True
        with self.assertRaises(ValueError):
            self.run_rebuild()
        original = self.store.load(self.plan)
        self.plan = self.plan.model_copy(update={"base_url": "https://different.invalid"})
        with self.assertRaises(ValueError):
            self.run_rebuild()
        self.assertEqual(self.store.load(self.plan), original)
        with self.assertRaises(ValueError):
            self.store.save(original.model_copy(update={"plan": self.plan}))

    def test_production_or_shared_copy_cannot_enter_the_workflow(self) -> None:
        for changes in (
            {"instance": "prod"},
            {"new": self.plan.old.model_dump()},
            {
                "new": {
                    **self.plan.new.model_dump(),
                    "runtime_identity": {
                        **self.plan.new.runtime_identity.model_dump(),
                        "instance": "prod",
                    },
                }
            },
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                TestingRebuildPlan.model_validate({**self.plan.model_dump(), **changes})
