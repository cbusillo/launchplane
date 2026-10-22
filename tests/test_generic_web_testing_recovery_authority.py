from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, cast
import unittest
from unittest.mock import patch

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.http_routes.mutation_support import idempotency_scope
from control_plane.service_auth import LaunchplaneAuthzPolicy, LocalOperatorIdentity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebProviderDeploymentObservation,
)
from tests.support.auth import scoped_agent_transport
from tests.support.disposable_lanes import record_safety
from tests.support.profiles import product_profile_payload, _product_profile_payload_with_prod
from tests.support.stores import sqlite_database_url
from tests.test_generic_web_deploy_recovery import (
    _create_recovery_app,
    _generic_web_recovery_original_deploy,
    _generic_web_recovery_reservation,
    _invoke_recovery,
    _invoke_recovery_apply,
    _RecoveryObservationProvider,
    _write_generic_web_recovery_reservation,
    _write_recovery_target_authority,
    _write_successful_retry_evidence,
)
from tests.test_service import _invoke_app


class GenericWebTestingRecoveryAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = PostgresRecordStore(database_url=sqlite_database_url(self.root / "state.db"))
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.profile = LaunchplaneProductProfileRecord.model_validate(product_profile_payload())
        self.store.write_product_profile_record(self.profile)
        self.lane = self.profile.lanes[0]
        _write_recovery_target_authority(self.store)
        self.identity = LocalOperatorIdentity(
            subject="local-owner-agent", token_label="local-owner-write", director_agent=True
        )
        self.original = _generic_web_recovery_original_deploy()
        self.app = _create_recovery_app(
            root=self.root, store=self.store, authz_policy=LaunchplaneAuthzPolicy()
        )
        self.reason = "Recover the exact original testing operation."
        self.patches = self.enterContext(ExitStack())

    def reserve(
        self,
        *,
        own: bool = True,
        outcome: Literal["present", "absent", "unknown"] = "present",
        state: Literal["running", "reconcile_required"] = "reconcile_required",
    ) -> None:
        reservation = _generic_web_recovery_reservation(
            original_deploy=self.original, idempotency_key="own-testing-original", state=state
        )
        if own:
            reservation = reservation.model_copy(update={"scope": idempotency_scope(self.identity)})
        self.reservation = _write_generic_web_recovery_reservation(self.store, reservation)
        self.provider = _RecoveryObservationProvider(
            GenericWebProviderDeploymentObservation(
                outcome=outcome,
                deployment_status="success" if outcome == "present" else "",
                deployment_id="original-deployment" if outcome == "present" else "",
                started_at=self.reservation.created_at if outcome == "present" else "",
                finished_at=self.reservation.updated_at if outcome == "present" else "",
            ),
            reservation=self.reservation,
        )
        self.patches.enter_context(
            patch(
                "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
                return_value=self.provider,
            )
        )
        self.executor = self.patches.enter_context(
            patch(
                "control_plane.generic_web_deploy_provider_adapter.execute_generic_web_deploy_result",
                side_effect=_write_successful_retry_evidence,
            )
        )

    def inspect(self) -> dict[str, Any]:
        status, payload = _invoke_recovery(
            self.app,
            original_deploy=self.original,
            idempotency_key=self.reservation.idempotency_key,
            reason=self.reason,
        )
        self.assertEqual(status, 200, payload)
        return payload

    def apply(self, digest: str) -> tuple[int, dict[str, Any]]:
        return _invoke_recovery_apply(
            self.app,
            original_deploy=self.original,
            idempotency_key=self.reservation.idempotency_key,
            reason=self.reason,
            expected_recovery_digest=digest,
        )

    def test_own_observed_original_adopts_and_replays_without_disposable_facts(self) -> None:
        self.reserve()
        reviewed = self.inspect()
        self.assertEqual(reviewed["proposed_action"], "adopt_observed")
        status, payload = self.apply(reviewed["recovery_digest"])
        self.assertEqual(status, 202, payload)
        record_safety(
            self.store,
            self.profile.product,
            self.lane.context,
            self.lane.instance,
            data="only_copy",
        )
        status, replay = self.apply(reviewed["recovery_digest"])
        self.assertEqual(status, 202, replay)
        self.assertEqual(payload["recovery_digest"], replay["recovery_digest"])
        self.executor.assert_not_called()

    def test_foreign_original_remains_readable_but_cannot_be_applied(self) -> None:
        self.reserve(own=False)
        reviewed = self.inspect()
        status, payload = self.apply(reviewed["recovery_digest"])
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.executor.assert_not_called()

    def test_scoped_identity_cannot_apply_an_original_with_matching_coordinates(self) -> None:
        self.reserve()
        reviewed = self.inspect()
        with scoped_agent_transport():
            status, payload = self.apply(reviewed["recovery_digest"])
        self.assertEqual(status, 403, payload)
        self.executor.assert_not_called()

    def test_own_retry_requires_current_safety_before_forward_work(self) -> None:
        self.reserve(outcome="absent")
        reviewed = self.inspect()
        self.assertEqual(reviewed["proposed_action"], "retry_original_operation")
        status, payload = self.apply(reviewed["recovery_digest"])
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "testing_deploy_safety_unknown")
        self.executor.assert_not_called()
        self.assertEqual(self.saved(), self.reservation)

    def saved(self) -> LaunchplaneIdempotencyRecord | None:
        return self.store.read_idempotency_record(
            scope=self.reservation.scope,
            route_path=self.reservation.route_path,
            idempotency_key=self.reservation.idempotency_key,
        )

    def test_protected_facts_block_own_retry_without_changing_the_original(self) -> None:
        self.reserve(outcome="absent")
        for changes in (
            {"usage": "in_use"},
            {"data": "only_copy"},
            {"data": "unknown"},
            {"production_resources": "shared"},
            {"production_resources": "unknown"},
        ):
            with self.subTest(changes=changes):
                record_safety(
                    self.store,
                    self.profile.product,
                    self.lane.context,
                    self.lane.instance,
                    **changes,
                )
                reviewed = self.inspect()
                status, payload = self.apply(reviewed["recovery_digest"])
                self.assertEqual(status, 409, payload)
                self.assertEqual(payload["error"]["code"], "testing_deploy_safety_unknown")
                self.assertEqual(self.saved(), self.reservation)
        self.executor.assert_not_called()

    def test_safety_is_rechecked_at_the_first_forward_checkpoint(self) -> None:
        self.reserve(outcome="absent")
        record_safety(self.store, self.profile.product, self.lane.context, self.lane.instance)
        reviewed = self.inspect()
        effects: list[str] = []

        def execute(**arguments: Any) -> Any:
            record_safety(
                self.store,
                self.profile.product,
                self.lane.context,
                self.lane.instance,
                data="only_copy",
            )
            arguments["provider_effect_checkpoint"]("target_update")
            effects.append("dispatched")
            return _write_successful_retry_evidence(**arguments)

        self.executor.side_effect = execute
        status, payload = self.apply(reviewed["recovery_digest"])
        self.assertEqual(status, 409, payload)
        self.assertEqual(effects, [])

    def test_unknown_effects_keep_the_original_fenced(self) -> None:
        self.reserve(outcome="unknown")
        reviewed = self.inspect()
        self.assertEqual(reviewed["proposed_action"], "hold_unknown")
        status, payload = self.apply(reviewed["recovery_digest"])
        self.assertEqual(status, 409, payload)
        self.assertEqual(self.saved(), self.reservation)
        self.executor.assert_not_called()

    def test_active_original_cannot_be_recovered_over_its_worker(self) -> None:
        self.reserve(state="running")
        reviewed = self.inspect()
        self.assertEqual(reviewed["proposed_action"], "wait_for_active_lease")
        status, payload = self.apply(reviewed["recovery_digest"])
        self.assertEqual(status, 409, payload)
        self.assertEqual(self.saved(), self.reservation)
        self.assertEqual(self.provider.observation_calls, 0)
        self.executor.assert_not_called()

    def test_changed_provider_evidence_stales_an_own_review(self) -> None:
        self.reserve()
        reviewed = self.inspect()
        self.provider.observation = GenericWebProviderDeploymentObservation(outcome="unknown")
        status, payload = self.apply(reviewed["recovery_digest"])
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "stale_recovery_digest")
        self.assertEqual(self.saved(), self.reservation)
        self.executor.assert_not_called()

    def test_changed_original_payload_cannot_recover_the_reserved_intent(self) -> None:
        self.reserve()
        reviewed = self.inspect()
        cast(dict[str, object], self.original["deploy"])["source_git_ref"] = "different-commit"
        status, payload = self.apply(reviewed["recovery_digest"])
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "reservation_conflict")
        self.assertEqual(self.saved(), self.reservation)
        self.executor.assert_not_called()

    def test_changed_owned_context_cannot_recover_the_old_original(self) -> None:
        self.reserve()
        reviewed = self.inspect()
        self.store.write_product_profile_record(
            self.profile.model_copy(
                update={"lanes": (self.lane.model_copy(update={"context": "replacement-testing"}),)}
            )
        )
        status, payload = self.apply(reviewed["recovery_digest"])
        self.assertEqual(status, 403, payload)
        self.assertEqual(self.saved(), self.reservation)
        self.executor.assert_not_called()

    def test_own_retry_cannot_dispatch_to_a_rebound_target(self) -> None:
        self.reserve(outcome="absent")
        reviewed = self.inspect()
        target = self.store.read_provider_target_record(
            context_name=self.lane.context, instance_name=self.lane.instance
        )
        self.store.write_provider_target_record(
            target.model_copy(update={"target_id": "other-target"})
        )
        record_safety(self.store, self.profile.product, self.lane.context, self.lane.instance)
        status, payload = self.apply(reviewed["recovery_digest"])
        self.assertEqual(status, 409, payload)
        self.assertEqual(self.saved(), self.reservation)
        self.executor.assert_not_called()

    def test_production_apply_requires_explicit_execute_authority(self) -> None:
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
        )
        original = _generic_web_recovery_original_deploy()
        cast(dict[str, object], original["deploy"])["instance"] = "prod"
        with patch.object(
            self.store, "lookup_existing_mutation_reservation", side_effect=AssertionError("lookup")
        ):
            status, payload = _invoke_app(
                self.app,
                method="POST",
                path="/v1/admin/generic-web/deploy-recovery/apply",
                authorization="Bearer local-operator-token",
                headers={"Idempotency-Key": "production-original"},
                payload={
                    "product": self.profile.product,
                    "instance": "prod",
                    "original_deploy": original,
                    "reason": self.reason,
                    "expected_recovery_digest": "0" * 64,
                },
            )
        self.assertEqual(status, 403, payload)

    def test_qualified_own_retry_preserves_the_original_and_replays_once(self) -> None:
        self.reserve(outcome="absent")
        record_safety(self.store, self.profile.product, self.lane.context, self.lane.instance)
        reviewed = self.inspect()
        status, payload = self.apply(reviewed["recovery_digest"])
        self.assertEqual(status, 202, payload)
        status, replay = self.apply(reviewed["recovery_digest"])
        self.assertEqual(status, 202, replay)
        self.executor.assert_called_once()
        saved = self.store.read_idempotency_record(
            scope=self.reservation.scope,
            route_path=self.reservation.route_path,
            idempotency_key=self.reservation.idempotency_key,
        )
        assert saved is not None
        self.assertEqual(saved.request_fingerprint, self.reservation.request_fingerprint)
        self.assertEqual(saved.reconciliation_key, self.reservation.reconciliation_key)
        self.assertEqual(saved.state, "completed")
