from typing import Any
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from datetime import timedelta
import os
import unittest
from unittest.mock import patch

from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold
from control_plane.contracts.generic_web_extensions import GenericWebLaneExtensions
from control_plane.generic_web_deploy_http import GenericWebDeployEnvelope
from control_plane.generic_web_testing_abandonment import inspect_testing_abandonment
from control_plane.generic_web_testing_settlement import testing_attempt_was_settled
from control_plane.event_deploy_recovery_reference import resolve_event_deploy_recovery_coordinates
from control_plane.http_app import idempotency_request_fingerprint
from control_plane.product_reconcile import RECONCILE_SOURCE, reconcile_reservation_scope
from control_plane.provider_operations import (
    ProviderMutationUnknownError,
    build_provider_operation_key,
    run_durable_provider_operation,
)
from control_plane.workflows.generic_web_deploy import GenericWebPostDeployGuard
from control_plane.workflows.generic_web_application_snapshot import recorded_application_lifecycle
from control_plane.workflows.generic_web_execution import EXECUTION_EVIDENCE_KEY, execution_receipt
from tests import test_generic_web_execution_receipts as execution_fixture
from tests.test_generic_web_deploy_recovery import _invoke_recovery, _invoke_recovery_apply
from tests.test_service import _invoke_app
from tests.test_generic_web_deploy_recovery import _OPERATOR_TOKEN, _create_recovery_app
from tests.support.disposable_lanes import record_safety
from tests.support.auth import local_operator_policy
from tests import test_product_reconcile as reconciliation
from control_plane.contracts.promotion_record import PostDeployUpdateEvidence
from control_plane.workflows.generic_web_deploy_provider import (
    decode_generic_web_provider_reconciliation_target,
)
from control_plane.workflows.generic_web_deploy import GenericWebPostDeployContext
from typing import cast


class GenericWebTestingAbandonmentTests(unittest.TestCase):
    database_url = ""
    retain_event_request = True

    def setUp(self) -> None:
        self.enterContext(
            patch.dict(
                os.environ, {"LAUNCHPLANE_MASTER_ENCRYPTION_KEY": "abandonment-fixture-root"}
            )
        )
        self.fixture = execution_fixture.ExecutionRecoveryTests()
        self.fixture.database_url = self.database_url
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        fixture.request = GenericWebDeployEnvelope.model_validate(fixture.request).model_dump(
            mode="json"
        )
        self.store = fixture.store
        fixture.app = _create_recovery_app(
            root=fixture.root,
            store=self.store,
            actions=("generic_web_deploy.execute", "product_environment.read"),
        )
        if self.store.database_dialect_name == "sqlite":
            self.enterContext(
                patch.object(
                    self.store,
                    "serialize_disposable_testing_resources",
                    side_effect=lambda: nullcontext(lambda: None),
                )
            )
        lane = fixture.profile.lanes[0].model_copy(
            update={
                "generic_web_extensions": GenericWebLaneExtensions(
                    verification_extension="verireel-testing"
                )
            }
        )
        fixture.profile = fixture.profile.model_copy(update={"lanes": (lane,)})
        self.store.write_product_profile_record(fixture.profile)
        self.key = f"{RECONCILE_SOURCE}:{fixture.profile.product}:{lane.context}:testing:original"

        def execute_verireel_testing_post_deploy(
            _root: object, _store: object, _context: object, guard: GenericWebPostDeployGuard
        ) -> None:
            guard.checkpoint_effect("post_deploy_fixture_signup")
            raise ProviderMutationUnknownError("The submitted fixture response was lost.")

        self.enterContext(
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.execute_verireel_testing_post_deploy",
                execute_verireel_testing_post_deploy,
            )
        )
        adapter = fixture.normal_adapter()
        adapter._retain_event_request = self.retain_event_request
        outcome = run_durable_provider_operation(
            store=self.store,
            scope=reconcile_reservation_scope(fixture.profile.product),
            route_path="/v1/drivers/generic-web/deploy",
            idempotency_key=self.key,
            request_fingerprint=idempotency_request_fingerprint(
                route_path="/v1/drivers/generic-web/deploy", payload=fixture.request
            ),
            lease_owner="original-worker",
            response_trace_id="original-response",
            adapter=adapter,
        )
        self.assertEqual(outcome.status, "reconcile_required")
        self.assertTrue(self.store.list_deployment_records(), outcome.response_payload)
        assert outcome.record is not None
        self.original = outcome.record
        self.deployment = fixture.normal_record(outcome)
        fixture.provider.original_operation_key = build_provider_operation_key(
            scope=self.original.scope,
            route_path=self.original.route_path,
            idempotency_key=self.original.idempotency_key,
            request_fingerprint=self.original.request_fingerprint,
            reconciliation_key=self.original.reconciliation_key,
        )
        self.enterContext(
            patch(
                "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
                return_value=fixture.provider,
            )
        )

    def inspect(self) -> dict[str, Any]:
        status, result = _invoke_recovery(
            self.fixture.app,
            original_deploy=self.fixture.request,
            idempotency_key=self.key,
            reason="Abandon the unqualified testing fixture without claiming its outcome.",
        )
        self.assertEqual(status, 200, result)
        return result

    def apply(self, digest: str) -> tuple[int, dict[str, Any]]:
        return _invoke_recovery_apply(
            self.fixture.app,
            original_deploy=self.fixture.request,
            idempotency_key=self.key,
            reason="Abandon the unqualified testing fixture without claiming its outcome.",
            expected_recovery_digest=digest,
        )

    def test_reviewed_abandonment_preserves_unknown_execution_and_lifecycle(self) -> None:
        plan = self.inspect()
        self.assertEqual(plan["proposed_action"], "abandon_testing_original")
        with patch(
            "control_plane.generic_web_deploy_provider_adapter.execute_generic_web_deploy_result"
        ) as deploy:
            status, result = self.apply(plan["recovery_digest"])
        self.assertEqual(status, 202, result)
        saved = self.store.read_idempotency_record(
            scope=self.original.scope,
            route_path=self.original.route_path,
            idempotency_key=self.key,
        )
        assert saved is not None
        self.assertEqual(saved.state, "completed")
        self.assertTrue(testing_attempt_was_settled(saved))
        self.assertEqual(
            saved.response_payload["abandonment"]["original"], self.original.model_dump(mode="json")
        )
        self.assertEqual(saved.response_payload["abandonment"]["original_outcome"], "unknown")
        after = self.store.read_deployment_record(self.deployment.record_id)
        self.assertEqual(execution_receipt(after), execution_receipt(self.deployment))
        self.assertEqual(
            recorded_application_lifecycle(after), recorded_application_lifecycle(self.deployment)
        )
        self.assertNotEqual(after.post_deploy_update.status, "pass")
        deploy.assert_not_called()

    def test_original_and_reference_replay_do_not_inspect_or_mutate_the_provider(self) -> None:
        coordinates = resolve_event_deploy_recovery_coordinates(
            store=self.store,
            product=self.fixture.profile.product,
            context=self.deployment.context,
            fingerprint=idempotency_request_fingerprint,
        )
        plan = self.inspect()
        status, first = self.apply(plan["recovery_digest"])
        self.assertEqual(status, 202, first)
        saved = self.store.read_idempotency_record(
            scope=self.original.scope, route_path=self.original.route_path, idempotency_key=self.key
        )
        with patch(
            "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
            side_effect=AssertionError(
                "A completed abandonment must replay without provider reads."
            ),
        ):
            status, replay = self.apply(plan["recovery_digest"])
            self.assertEqual(status, 202, replay)
            status, reference = _invoke_app(
                self.fixture.app,
                method="POST",
                path="/v1/admin/generic-web/deploy-recovery/apply",
                authorization=f"Bearer {_OPERATOR_TOKEN}",
                payload={
                    "product": self.fixture.profile.product,
                    "instance": "testing",
                    "recovery_reference": coordinates.reference,
                    "reason": "Abandon the unqualified testing fixture without claiming its outcome.",
                    "expected_recovery_digest": plan["recovery_digest"],
                },
            )
            self.assertEqual(status, 202, reference)
            status, readback = _invoke_app(
                self.fixture.app,
                method="POST",
                path="/v1/admin/generic-web/deploy-recovery/dry-run",
                authorization=f"Bearer {_OPERATOR_TOKEN}",
                payload={
                    "product": self.fixture.profile.product,
                    "instance": "testing",
                    "recovery_reference": coordinates.reference,
                    "reason": "Abandon the unqualified testing fixture without claiming its outcome.",
                },
            )
            self.assertEqual(status, 200, readback)
            self.assertEqual(readback["reservation_state"], "completed")
            self.assertEqual(readback["proposed_action"], "replay_completed")
            self.assertEqual(readback["recovery_digest"], first["recovery_digest"])
        self.assertEqual(reference["recovery_digest"], first["recovery_digest"])
        self.assertEqual(replay["recovery_digest"], first["recovery_digest"])
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=self.original.scope,
                route_path=self.original.route_path,
                idempotency_key=self.key,
            ),
            saved,
        )

    def test_production_adapter_cannot_use_testing_abandonment(self) -> None:
        adapter = self.fixture.normal_adapter()
        adapter._lane = adapter.lane.model_copy(update={"instance": "prod"})
        self.assertIsNone(
            inspect_testing_abandonment(
                self.store, adapter, self.original, self.fixture.provider.original_operation_key
            )
        )
        self.assertEqual(self.store.list_held_provider_target_reservations(), (self.original,))

    def test_legacy_original_without_saved_request_remains_held(self) -> None:
        legacy = GenericWebTestingAbandonmentTests()
        legacy.retain_event_request = False
        legacy.setUp()
        self.addCleanup(legacy.doCleanups)
        self.assertEqual(legacy.inspect()["proposed_action"], "hold_unknown")

    def test_other_held_attempt_on_the_lane_refuses_abandonment(self) -> None:
        other = self.original.model_copy(
            update={"record_id": "other-held", "idempotency_key": self.key + ":other"}
        )
        with patch.object(
            self.store,
            "list_held_provider_target_reservations",
            return_value=(self.original, other),
        ):
            self.assertEqual(self.inspect()["proposed_action"], "hold_unknown")

    def test_provider_reads_do_not_hold_global_authority_lock(self) -> None:
        plan = self.inspect()
        guarded = False

        @contextmanager
        def authority_guard() -> Iterator[Callable[[], None]]:
            nonlocal guarded
            guarded = True
            try:
                yield lambda: None
            finally:
                guarded = False

        observe = self.fixture.provider.observe_artifact_deploy

        def read_provider(**kwargs: Any) -> Any:
            self.assertFalse(guarded)
            return observe(**kwargs)

        with (
            patch.object(self.store, "serialize_disposable_testing_resources", authority_guard),
            patch.object(
                self.fixture.provider, "observe_artifact_deploy", side_effect=read_provider
            ),
        ):
            status, result = self.apply(plan["recovery_digest"])
        self.assertEqual(status, 202, result)

    def test_lost_authority_lock_does_not_complete_original(self) -> None:
        plan = self.inspect()

        def lost() -> None:
            raise RuntimeError("Authority connection lost.")

        with patch.object(
            self.store, "serialize_disposable_testing_resources", return_value=nullcontext(lost)
        ):
            status, _ = self.apply(plan["recovery_digest"])
        self.assertEqual(status, 409)
        self.assertEqual(self.store.list_held_provider_target_reservations(), (self.original,))

    def test_historical_production_alias_keeps_the_original_held(self) -> None:
        target = self.store.read_provider_target_record(
            context_name=self.deployment.context, instance_name="testing"
        )
        targets = self.store.list_provider_target_records()
        with patch.object(
            self.store,
            "list_provider_target_records",
            return_value=(*targets, target.model_copy(update={"instance": "prod"})),
        ):
            self.assertEqual(self.inspect()["proposed_action"], "hold_unknown")
        self.assertEqual(self.store.list_held_provider_target_reservations(), (self.original,))

    def test_protective_observation_added_after_review_refuses_abandonment(self) -> None:
        plan = self.inspect()
        record_safety(
            self.store,
            self.fixture.profile.product,
            self.deployment.context,
            "testing",
            data="only_copy",
        )
        status, _ = self.apply(plan["recovery_digest"])
        self.assertEqual(status, 409)
        self.assertEqual(self.store.list_held_provider_target_reservations(), (self.original,))

    def test_expired_protective_reports_still_refuse_an_abandonment_plan(self) -> None:
        for facts in (
            {"usage": "in_use"},
            {"data": "only_copy"},
            {"production_resources": "shared"},
            {"production_resources": "separation_ready"},
        ):
            with self.subTest(facts=facts):
                record_safety(
                    self.store,
                    self.fixture.profile.product,
                    self.deployment.context,
                    "testing",
                    **facts,
                )
                observation = self.store.read_disposable_lane_safety(
                    self.fixture.profile.product, self.deployment.context, "testing"
                )
                assert observation is not None
                self.store.write_disposable_lane_safety(
                    observation.model_copy(
                        update={
                            "observed_at": observation.observed_at - timedelta(hours=1),
                            "expires_at": observation.expires_at - timedelta(hours=1),
                        }
                    )
                )
                self.assertEqual(self.inspect()["proposed_action"], "hold_unknown")
        self.assertEqual(self.store.list_held_provider_target_reservations(), (self.original,))

    def test_unproved_or_foreign_image_does_not_abandon_unknown_execution(self) -> None:
        observed = self.fixture.provider.observation
        for changes in (
            {"outcome": "unknown"},
            {"deployment_id": "foreign-image"},
            {"deployment_status": "error"},
        ):
            with self.subTest(changes=changes):
                self.fixture.provider.observation = observed.model_copy(update=changes)
                self.assertEqual(self.inspect()["proposed_action"], "hold_unknown")
        self.assertEqual(self.store.list_held_provider_target_reservations(), (self.original,))

    def test_active_execution_lock_refuses_abandonment(self) -> None:
        from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name

        plan = self.inspect()
        with self.store.testing_replacement_execution_lock(
            testing_event_execution_lock_name(self.original.provider_target_key)
        ) as check:
            self.assertIsNotNone(check)
            status, _ = self.apply(plan["recovery_digest"])
        self.assertEqual(status, 409)
        self.assertEqual(self.store.list_held_provider_target_reservations(), (self.original,))

    def test_missing_or_unfinished_provider_execution_is_not_abandonable(self) -> None:
        receipt = execution_receipt(self.deployment)
        assert receipt is not None
        read = self.store.read_deployment_record
        for entries in (None, (receipt.entries[-1].model_copy(update={"kind": "schedule_run"}),)):
            with self.subTest(entries=entries):
                evidence = dict(self.deployment.post_deploy_update.evidence)
                if entries is None:
                    evidence.pop(EXECUTION_EVIDENCE_KEY)
                else:
                    evidence[EXECUTION_EVIDENCE_KEY] = receipt.model_copy(
                        update={"entries": entries}
                    ).model_dump_json()
                changed = self.deployment.model_copy(
                    update={
                        "post_deploy_update": self.deployment.post_deploy_update.model_copy(
                            update={"evidence": evidence}
                        )
                    }
                )
                with patch.object(
                    self.store,
                    "read_deployment_record",
                    side_effect=lambda record_id: (
                        changed if record_id == changed.record_id else read(record_id)
                    ),
                ):
                    self.assertEqual(self.inspect()["proposed_action"], "hold_unknown")
        self.assertEqual(self.store.list_held_provider_target_reservations(), (self.original,))

    def test_staff_hold_added_after_review_refuses_abandonment(self) -> None:
        plan = self.inspect()
        target = self.store.read_dokploy_target_record(
            context_name=self.deployment.context, instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={
                    "policies": target.policies.model_copy(
                        update={
                            "staff_testing_hold": DokployTargetStaffTestingHold(
                                reason="Staff are using testing", purpose="staff_testing"
                            )
                        }
                    )
                }
            )
        )
        self.assertEqual(self.inspect()["proposed_action"], "hold_unknown")
        status, _ = self.apply(plan["recovery_digest"])
        self.assertEqual(status, 409)
        self.assertEqual(self.store.list_held_provider_target_reservations(), (self.original,))


class TestingAbandonmentReconcileTests(unittest.TestCase):
    def test_normal_reconcile_qualifies_a_successor_with_the_captured_schedule_baseline(
        self,
    ) -> None:
        self.qualify_successor(abandon_during_reconcile=False)

    def test_abandonment_after_initial_read_starts_successor_in_the_same_pass(self) -> None:
        self.qualify_successor(abandon_during_reconcile=True)

    def qualify_successor(self, *, abandon_during_reconcile: bool) -> None:
        scenario = reconciliation.ProductReconcileGenericWebTestingTests()
        scenario.setUp()
        self.addCleanup(scenario.doCleanups)
        triggered = scenario.prepare_application_runtime_test()
        scenario.application_input_schedule()
        profile = scenario.store.read_product_profile_record("site")
        lane = profile.lanes[0].model_copy(
            update={
                "generic_web_extensions": profile.lanes[0].generic_web_extensions.model_copy(
                    update={"verification_extension": "verireel-testing"}
                )
            }
        )
        scenario.store.write_product_profile_record(
            profile.model_copy(update={"lanes": (lane, *profile.lanes[1:])})
        )
        calls: list[str] = []

        def execute_verireel_testing_post_deploy(*args: object) -> PostDeployUpdateEvidence:
            context = cast(GenericWebPostDeployContext, args[2])
            guard = cast(GenericWebPostDeployGuard, args[3])
            calls.append(context.deployment_record_id)
            guard.checkpoint_effect("post_deploy_fixture_signup")
            if len(calls) == 1:
                raise ProviderMutationUnknownError("The original fixture response was lost.")
            return cast(PostDeployUpdateEvidence, scenario._testing_qualification_evidence(*args))

        with (
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.execute_verireel_testing_post_deploy",
                execute_verireel_testing_post_deploy,
            ),
            patch(
                "control_plane.product_reconcile._current_testing_deployment_id", return_value=None
            ),
        ):
            scenario.request()
            first = scenario.run_once()
            self.assertEqual(first.state, "failed")
            (original,) = scenario.store.list_held_provider_target_reservations()
            before = scenario.store.read_deployment_record(calls[0])
            baseline = recorded_application_lifecycle(before)
            assert baseline is not None
            self.assertTrue(baseline.originals)
            disabled = dict(scenario.input_schedule)
            self.assertFalse(disabled["enabled"])
            snapshot = decode_generic_web_provider_reconciliation_target(
                original.reconciliation_key
            )
            app = _create_recovery_app(
                root=scenario.root,
                store=scenario.store,
                actions=("generic_web_deploy.execute",),
                contexts=("cm",),
                authz_policy=local_operator_policy(
                    actions=("generic_web_deploy.execute",), products=("site",), contexts=("cm",)
                ),
            )
            body = {
                "product": "site",
                "instance": "testing",
                "original_deploy": snapshot.original_event_deploy,
                "reason": "Abandon this fixture and let normal reconciliation continue.",
            }
            with (
                patch(
                    "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
                    return_value=scenario.application_inputs_provider,
                ),
                patch.object(
                    scenario.store,
                    "serialize_disposable_testing_resources",
                    side_effect=lambda: nullcontext(lambda: None),
                ),
            ):
                status, plan = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/admin/generic-web/deploy-recovery/dry-run",
                    authorization=f"Bearer {_OPERATOR_TOKEN}",
                    payload=body,
                    headers={"Idempotency-Key": original.idempotency_key},
                )
                self.assertEqual(status, 200, plan)
                self.assertEqual(plan["proposed_action"], "abandon_testing_original")

                def abandon() -> None:
                    status, applied = _invoke_app(
                        app,
                        method="POST",
                        path="/v1/admin/generic-web/deploy-recovery/apply",
                        authorization=f"Bearer {_OPERATOR_TOKEN}",
                        payload={**body, "expected_recovery_digest": plan["recovery_digest"]},
                        headers={"Idempotency-Key": original.idempotency_key},
                    )
                    self.assertEqual(status, 202, applied)
                    self.assertEqual(scenario.input_schedule, disabled)
                    self.assertEqual(
                        scenario.store.read_deployment_record(before.record_id), before
                    )

                lock = scenario.store.testing_replacement_execution_lock

                @contextmanager
                def abandon_before_lock(name: str) -> Iterator[Callable[[], None] | None]:
                    with patch.object(scenario.store, "testing_replacement_execution_lock", lock):
                        abandon()
                        with lock(name) as check:
                            yield check

                if not abandon_during_reconcile:
                    abandon()
                scenario.request()
                with patch.object(
                    scenario.store,
                    "testing_replacement_execution_lock",
                    abandon_before_lock if abandon_during_reconcile else lock,
                ):
                    successor = scenario.reconcile()
        self.assertNotEqual(successor["deploy_idempotency_key"], original.idempotency_key)
        self.assertEqual(successor["post_deploy_status"], "pass")
        self.assertEqual(len(triggered), len(calls))
        self.assertEqual(len(set(calls)), len(calls))
        after = scenario.store.read_deployment_record(str(successor["deployment_record_id"]))
        self.assertEqual(calls, [before.record_id, after.record_id])
        next_baseline = recorded_application_lifecycle(after)
        assert next_baseline is not None and baseline is not None
        self.assertEqual(next_baseline.originals, baseline.originals)
        self.assertEqual(next_baseline.managed_names, baseline.managed_names)
        self.assertEqual(next_baseline.previous_artifact_id, baseline.previous_artifact_id)
        self.assertTrue(scenario.input_schedule["enabled"])
