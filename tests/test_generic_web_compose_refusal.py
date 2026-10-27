from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from typing import Any
from datetime import datetime, timedelta, timezone

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.generic_web_deploy_http import GenericWebDeployEnvelope
from control_plane.generic_web_deploy_provider_adapter import (
    GenericWebDeployProviderMutationAdapter,
)
from control_plane.lane_movement import LaneMovementRefused
from control_plane.contracts.promotion_record import RecordFailure
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.provider_operations import (
    DurableProviderOperationResult,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
)
from control_plane.provider_operations import ProviderOperationLease, run_durable_provider_operation
from control_plane.http_app import idempotency_request_fingerprint
from control_plane.generic_web_deploy_http import execute_generic_web_deploy_result
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.profiles import product_profile_payload
from tests.support.stores import sqlite_database_url
from tests.support.auth import local_operator_policy
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    build_authz_policy_record_id,
    authz_policy_sha256,
)
from tests.test_generic_web_deploy_recovery import (
    _generic_web_recovery_original_deploy,
    _generic_web_recovery_target,
    _write_recovery_target_authority,
    _create_recovery_app,
    _invoke_recovery,
    _invoke_recovery_apply,
)
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebProviderDeploymentObservation,
    GenericWebDeployProvider,
)


class ComposeRefusalTests(unittest.TestCase):
    def database_url(self) -> str:
        return sqlite_database_url(self.root / "state.db")

    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = PostgresRecordStore(database_url=self.database_url())
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.profile = LaunchplaneProductProfileRecord.model_validate(product_profile_payload())
        self.store.write_product_profile_record(self.profile)
        policy = local_operator_policy(
            actions=("generic_web_deploy.execute",),
            products=(self.profile.product,),
            contexts=("sellyouroutboard-testing",),
        )
        self.store.seed_authz_policy_if_absent(
            LaunchplaneAuthzPolicyRecord(
                record_id=build_authz_policy_record_id(
                    revision=1, policy_sha256=authz_policy_sha256(policy)
                ),
                revision=1,
                status="active",
                source="test:compose-refusal",
                updated_at=self.profile.updated_at,
                policy=policy,
            )
        )
        self.request = GenericWebDeployEnvelope.model_validate(
            _generic_web_recovery_original_deploy()
        )
        target = _generic_web_recovery_target()
        self.target = target.model_copy(
            update={
                "ship_request": target.ship_request.model_copy(
                    update={
                        "target_type": "compose",
                        "target_category": "compose",
                        "provider_target_type": "compose",
                        "deploy_mode": "dokploy-compose-api",
                        "provider_deploy_mode": "dokploy-compose-api",
                    }
                ),
                "resolved_target": target.resolved_target.model_copy(
                    update={"target_type": "compose"}
                ),
            }
        )
        self.provider = Mock(spec=GenericWebDeployProvider)
        self.provider.provider_id = "dokploy"
        self.provider.delegated_executor = "control-plane.dokploy"
        self.provider.observe_artifact_deploy.return_value = (
            GenericWebProviderDeploymentObservation(outcome="absent")
        )
        self.provider.resolve_deploy_target.return_value = self.target
        self.adapter = GenericWebDeployProviderMutationAdapter(
            control_plane_root=self.root,
            record_store=self.store,
            deploy_request=self.request,
            profile=self.profile,
            lane=next(lane for lane in self.profile.lanes if lane.instance == "testing"),
            trace_id="compose-refusal",
            deploy_provider=self.provider,
        )
        self.adapter._resolved_deploy_target = self.target
        _write_recovery_target_authority(self.store)
        target_record = self.store.read_dokploy_target_record(
            context_name=self.target.ship_request.context, instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            target_record.model_copy(update={"target_type": "compose"})
        )
        provider_record = self.store.read_provider_target_record(
            context_name=self.target.ship_request.context, instance_name="testing"
        )
        self.store.write_provider_target_record(
            provider_record.model_copy(
                update={"target_category": "compose", "provider_target_type": "compose"}
            )
        )

    def test_pre_dispatch_refusal_publishes_failure_without_effect_checkpoint(self) -> None:
        lease = Mock(spec=ProviderOperationLease)
        with patch(
            "control_plane.workflows.generic_web_deploy.require_forward_lane_build",
            side_effect=LaneMovementRefused("ancestor_build"),
        ):
            outcome = self.adapter.apply("original-operation", lease)
        self.assertFalse(outcome.provider_effect_performed)
        self.assertFalse(outcome.durable)
        lease.checkpoint_effect.assert_not_called()
        lease.assert_current.assert_called()
        records = self.store.list_deployment_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].failure, LaneMovementRefused("ancestor_build").record_failure())
        self.assertEqual(records[0].deploy.status, "fail")
        self.assertEqual(self.store.list_environment_inventory(), ())
        result = self.run_original()
        self.assertEqual(result.status, "unstored")
        self.assertIsNone(result.record)
        self.provider.execute_artifact_deploy.assert_not_called()

    def run_original(
        self, *, historical_failure_publication: bool = False
    ) -> DurableProviderOperationResult:
        def old_failure_publication(**kwargs: Any) -> Any:
            if historical_failure_publication:
                kwargs["before_failure_records_commit"] = kwargs["before_records_commit"]
            return execute_generic_web_deploy_result(**kwargs)

        original_time = (
            (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(microsecond=0).isoformat()
        )
        with (
            patch.object(self.store, "_database_mutation_timestamp", return_value=original_time),
            patch(
                "control_plane.workflows.generic_web_deploy.utc_now_timestamp",
                return_value=original_time,
            ),
            patch(
                "control_plane.workflows.generic_web_deploy.require_forward_lane_build",
                side_effect=LaneMovementRefused("ancestor_build"),
            ),
            patch(
                "control_plane.generic_web_deploy_provider_adapter.execute_generic_web_deploy_result",
                side_effect=old_failure_publication,
            ),
        ):
            result = run_durable_provider_operation(
                store=self.store,
                scope="legacy/github-actions/scope",
                route_path="/v1/drivers/generic-web/deploy",
                idempotency_key="original-compose-refusal",
                request_fingerprint=idempotency_request_fingerprint(
                    route_path="/v1/drivers/generic-web/deploy",
                    payload=_generic_web_recovery_original_deploy(),
                ),
                lease_owner="original-worker",
                response_trace_id="original-trace",
                adapter=self.adapter,
            )
        return result

    def hold_original(self) -> None:
        result = self.run_original(historical_failure_publication=True)
        assert result.record is not None
        self.assertEqual(result.record.state, "reconcile_required")

    def test_original_refusal_closes_without_retry_or_lane_publication(self) -> None:
        self.hold_original()
        before = self.store.list_deployment_records()
        app = _create_recovery_app(root=self.root, store=self.store)
        with (
            patch(
                "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
                return_value=self.provider,
            ),
            patch.object(
                GenericWebDeployProviderMutationAdapter,
                "apply",
                side_effect=AssertionError("provider retry"),
            ),
        ):
            status, plan = _invoke_recovery(
                app,
                original_deploy=_generic_web_recovery_original_deploy(),
                idempotency_key="original-compose-refusal",
                reason="Settle the original proved refusal.",
            )
            self.assertEqual(status, 200, plan)
            self.assertEqual(plan["proposed_action"], "close_out_observed")
            status, applied = _invoke_recovery_apply(
                app,
                original_deploy=_generic_web_recovery_original_deploy(),
                idempotency_key="original-compose-refusal",
                reason="Settle the original proved refusal.",
                expected_recovery_digest=plan["recovery_digest"],
            )
        self.assertEqual(status, 202, applied)
        self.assertEqual(applied["reservation_state"], "completed")
        self.assertFalse(applied["retry_safe"])
        self.assertEqual(self.store.list_deployment_records(), before)
        self.assertEqual(self.store.list_environment_inventory(), ())
        self.provider.execute_artifact_deploy.assert_not_called()
        saved = self.store.lookup_existing_mutation_reservation(
            route_path="/v1/drivers/generic-web/deploy",
            idempotency_key="original-compose-refusal",
            request_fingerprint=idempotency_request_fingerprint(
                route_path="/v1/drivers/generic-web/deploy",
                payload=_generic_web_recovery_original_deploy(),
            ),
        ).record
        assert saved is not None
        self.assertEqual(saved.response_status_code, 409)
        self.assertEqual(saved.response_payload["result"]["deploy_status"], "fail")

    def test_ambiguous_or_effected_record_keeps_original_fence(self) -> None:
        self.hold_original()
        record = self.store.list_deployment_records()[0]
        identity = RuntimeIdentity(
            product=self.profile.product,
            context=record.context,
            instance="testing",
            deployment_record_id=record.record_id,
            artifact_id=self.request.deploy.artifact_id,
            source_git_ref=record.source_git_ref,
        )
        assert record.artifact_identity is not None
        assert record.resolved_target is not None
        changes: dict[str, dict[str, Any]] = {
            "dispatch": {
                "post_deploy_update": record.post_deploy_update.model_copy(
                    update={"evidence": {"provider_dispatch": "{}"}}
                )
            },
            "deployment": {
                "deploy": record.deploy.model_copy(update={"deployment_id": "provider-deployment"})
            },
            "identity": {"runtime_identity": identity},
            "bootstrap": {
                "bootstrap": record.bootstrap.model_copy(
                    update={"attempted": True, "run_status": "fail", "readiness_status": "fail"}
                )
            },
            "failure": {
                "failure": RecordFailure(code="different_failure", description="Different failure.")
            },
            "artifact": {
                "artifact_identity": record.artifact_identity.model_copy(
                    update={"artifact_id": "different-artifact"}
                )
            },
            "target": {
                "resolved_target": record.resolved_target.model_copy(
                    update={"target_id": "different-target"}
                )
            },
        }
        app = _create_recovery_app(root=self.root, store=self.store)
        with patch(
            "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
            return_value=self.provider,
        ):
            for name, changed in changes.items():
                with self.subTest(name=name):
                    self.store.write_deployment_record(record.model_copy(update=changed))
                    status, plan = _invoke_recovery(
                        app,
                        original_deploy=_generic_web_recovery_original_deploy(),
                        idempotency_key="original-compose-refusal",
                        reason="Inspect uncertain record.",
                    )
                    self.assertEqual(status, 200, plan)
                    self.assertEqual(plan["proposed_action"], "hold_unknown")
            self.store.write_deployment_record(record)

    def test_closeout_rejects_deployment_change_at_atomic_adoption(self) -> None:
        self.hold_original()
        app = _create_recovery_app(root=self.root, store=self.store)
        adopt = self.store.adopt_reconciled_mutation

        def change_before_adoption(**kwargs: Any) -> Any:
            record = self.store.list_deployment_records()[0]
            self.store.write_deployment_record(
                record.model_copy(update={"source_git_ref": "changed"})
            )
            return adopt(**kwargs)

        with patch(
            "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
            return_value=self.provider,
        ):
            status, plan = _invoke_recovery(
                app,
                original_deploy=_generic_web_recovery_original_deploy(),
                idempotency_key="original-compose-refusal",
                reason="Inspect original refusal.",
            )
            self.assertEqual(status, 200, plan)
            with patch.object(
                self.store, "adopt_reconciled_mutation", side_effect=change_before_adoption
            ):
                status, applied = _invoke_recovery_apply(
                    app,
                    original_deploy=_generic_web_recovery_original_deploy(),
                    idempotency_key="original-compose-refusal",
                    reason="Inspect original refusal.",
                    expected_recovery_digest=plan["recovery_digest"],
                )
        self.assertEqual(status, 409, applied)
        self.assertEqual(applied["error"]["code"], "reservation_changed")
        self.provider.execute_artifact_deploy.assert_not_called()

    def test_active_retried_or_other_phase_original_stays_fenced(self) -> None:
        self.hold_original()
        lookup = self.store.lookup_existing_mutation_reservation(
            route_path="/v1/drivers/generic-web/deploy",
            idempotency_key="original-compose-refusal",
            request_fingerprint=idempotency_request_fingerprint(
                route_path="/v1/drivers/generic-web/deploy",
                payload=_generic_web_recovery_original_deploy(),
            ),
        )
        assert lookup.record is not None
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        app = _create_recovery_app(root=self.root, store=self.store)
        with patch(
            "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
            return_value=self.provider,
        ):
            for change in (
                {"attempt": 2},
                {"provider_effect_phase": "deploy_trigger"},
                {"lease_expires_at": future},
            ):
                with (
                    self.subTest(change=change),
                    patch.object(
                        self.store,
                        "lookup_existing_mutation_reservation",
                        return_value=lookup._replace(
                            record=lookup.record.model_copy(update=change)
                        ),
                    ),
                ):
                    status, plan = _invoke_recovery(
                        app,
                        original_deploy=_generic_web_recovery_original_deploy(),
                        idempotency_key="original-compose-refusal",
                        reason="Inspect uncertain original.",
                    )
                    self.assertEqual(status, 200, plan)
                    self.assertEqual(plan["proposed_action"], "hold_unknown")

    def test_lost_lease_cannot_publish_failure(self) -> None:
        lease = Mock(spec=ProviderOperationLease)
        lease.assert_current.side_effect = RuntimeError("Lease lost.")
        with patch(
            "control_plane.workflows.generic_web_deploy.require_forward_lane_build",
            side_effect=LaneMovementRefused("ancestor_build"),
        ):
            with self.assertRaises(RuntimeError):
                self.adapter.apply("stale-worker", lease)
        self.assertEqual(self.store.list_deployment_records(), ())

    def test_typed_refusal_before_effect_is_rejected_but_effected_attempt_stays_unknown(
        self,
    ) -> None:
        lease = Mock(spec=ProviderOperationLease)
        with patch(
            "control_plane.generic_web_deploy_provider_adapter.execute_generic_web_deploy_result",
            side_effect=LaneMovementRefused("ancestor_build"),
        ):
            with self.assertRaises(ProviderMutationRejectedError):
                self.adapter.apply("unstarted-cutover", lease)

        def refused_after_checkpoint(**kwargs: Any) -> Any:
            kwargs["provider_effect_checkpoint"]("deploy_trigger")
            raise LaneMovementRefused("ancestor_build")

        with patch(
            "control_plane.generic_web_deploy_provider_adapter.execute_generic_web_deploy_result",
            side_effect=refused_after_checkpoint,
        ):
            with self.assertRaises(ProviderMutationUnknownError):
                self.adapter.apply("effected-operation", lease)
        lease.checkpoint_effect.assert_called_once_with("deploy_trigger")
