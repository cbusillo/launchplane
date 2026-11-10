from datetime import UTC, datetime, timedelta
import asyncio
import socket
from typing import Any, cast
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation_id,
)
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.testing_candidate import testing_candidate_namespace
from control_plane.contracts.testing_rebuild import (
    REBUILD_SCOPE,
    REBUILD_ROUTE,
    TestingRebuildPlan,
    TestingRebuildRecord,
    TestingRouteObservation,
    TestingPublicHealthEvidence,
)
from control_plane.native_testing_rebuild_evidence import (
    NativeTestingEvidenceStore,
    read_native_testing_rebuild_evidence,
)
from control_plane.contracts.native_testing_rebuild_evidence import NativeTestingRebuildEvidence
from control_plane.http_routes.drivers import (
    ODOO_STABLE_TARGET_REPLACEMENT_OPERATION_STATUS_ROUTE,
    register_operation_status_read_routes,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import LocalOperatorIdentity
from control_plane.testing_rebuild_storage import (
    REBUILD_CHECKPOINT_SCOPE,
    RebuildRecordStore,
    RecordTestingRebuildStore,
)
from control_plane.workflows.testing_rebuild import execute_testing_rebuild
from tests.test_testing_rebuild import rebuild_plan, TestingProvider
from tests.support.http import request


class NativeTestingEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = rebuild_plan("empty")
        namespace = testing_candidate_namespace(
            self.plan.product, self.plan.context, self.plan.operation_id
        )
        self.plan = self.plan.model_copy(
            update={"new": self.plan.new.model_copy(update={"copy_id": namespace})}
        )
        self.intent = {
            "operation_id": self.plan.operation_id,
            "old": self.plan.old.model_dump(mode="json"),
            "private": "fixture-private-topology-and-credential-material",
        }
        self.intent_digest = canonical_json_sha256(self.intent)
        self.plan = self.plan.model_copy(update={"authority_digest": self.intent_digest})
        now = datetime.now(UTC).isoformat()
        self.operation = OdooStableTargetReplacementOperationRecord(
            operation_id=self.plan.operation_id,
            product=self.plan.product,
            context=self.plan.context,
            instance="testing",
            idempotency_key="fixture-private-key",
            request_fingerprint="fixture-original-request",
            request=OdooStableTargetReplacementApplyRequest(
                product=self.plan.product,
                instance="testing",
                strategy="replace-and-cutover",
                data_source_mode="empty",
                allow_empty_data=True,
                artifact_id=self.plan.new.runtime_identity.artifact_id,
                source_git_ref=self.plan.new.runtime_identity.source_git_ref,
            ),
            created_at=now,
            updated_at=now,
            attempt=12,
        )
        self.rows: dict[tuple[str, str, str], LaunchplaneIdempotencyRecord] = {}
        self.store = Mock(
            spec=[
                "read_idempotency_record",
                "read_original_mutation_intent",
                "compare_and_write_testing_rebuild_checkpoint",
                "read_deployment_record",
                "read_odoo_stable_target_replacement_operation_record",
            ]
        )
        self.store.read_idempotency_record.side_effect = (
            lambda *, scope, route_path, idempotency_key: self.rows.get(
                (scope, route_path, idempotency_key)
            )
        )
        self.store.read_original_mutation_intent.return_value = self.intent
        self.store.compare_and_write_testing_rebuild_checkpoint.side_effect = (
            lambda expected, replacement: self.rows.__setitem__(
                (replacement.scope, replacement.route_path, replacement.idempotency_key),
                replacement,
            )
        )
        self.store.read_deployment_record.side_effect = FileNotFoundError("fixture")
        self.store.read_odoo_stable_target_replacement_operation_record.return_value = (
            self.operation
        )
        self.reservation = self.row(
            REBUILD_SCOPE,
            REBUILD_ROUTE,
            self.operation.operation_id,
            self.operation.request_fingerprint,
        )
        self.rows[(REBUILD_SCOPE, REBUILD_ROUTE, self.operation.operation_id)] = self.reservation
        provider = TestingProvider(self.plan)
        provider.bad_public = True
        original_switch = provider.switch

        def switch(
            plan: TestingRebuildPlan, previous: Any, desired: Any, *, reconcile_only: bool
        ) -> None:
            if desired == plan.old:
                provider.now += timedelta(seconds=1)
            original_switch(plan, previous, desired, reconcile_only=reconcile_only)

        self.enterContext(patch.object(provider, "switch", side_effect=switch))
        self.checkpoint = execute_testing_rebuild(
            self.plan,
            RecordTestingRebuildStore(cast(RebuildRecordStore, cast(object, self.store))),
            provider,
            clock=lambda: provider.now,
            reservation=self.reservation,
            operation_attempt=self.operation.attempt,
        )
        self.store.reset_mock()

    def row(
        self,
        scope: str,
        route: str,
        key: str,
        fingerprint: str,
        payload: dict[str, Any] | None = None,
    ) -> LaunchplaneIdempotencyRecord:
        now = datetime.now(UTC).isoformat()
        return LaunchplaneIdempotencyRecord(
            record_id=build_launchplane_mutation_reservation_id(
                scope=scope, route_path=route, idempotency_key=key
            ),
            scope=scope,
            route_path=route,
            idempotency_key=key,
            request_fingerprint=fingerprint,
            reconciliation_key=self.intent_digest,
            provider_target_key="fixture-private-provider-target",
            attempt=3,
            created_at=now,
            updated_at=now,
            recorded_at=now,
            response_status_code=200,
            response_trace_id="fixture-private-trace",
            response_payload=payload or {},
        )

    def read(self) -> NativeTestingRebuildEvidence:
        return read_native_testing_rebuild_evidence(self.store, self.operation)

    def test_failed_candidate_probe_survives_inverse_and_terminal_checkpoint(self) -> None:
        evidence = self.read().terminal_probe
        self.assertEqual(self.checkpoint.step, "failed")
        assert self.checkpoint.route is not None
        self.assertEqual(self.checkpoint.route.slot_id, self.plan.old.copy_id)
        self.assertEqual(evidence.availability, "present")
        self.assertEqual(evidence.state, "fail")
        self.assertTrue(evidence.verified)
        assert self.checkpoint.candidate_probe is not None
        self.assertEqual(evidence.observed_at, self.checkpoint.candidate_probe.observed_at)
        assert evidence.observed_at is not None
        self.assertLess(evidence.observed_at, self.checkpoint.route.observed_at)

    def test_retained_probe_stays_bound_after_worker_reclaim(self) -> None:
        self.operation = self.operation.model_copy(update={"attempt": self.operation.attempt + 1})
        self.reservation = self.reservation.model_copy(
            update={"attempt": self.reservation.attempt + 1}
        )
        self.rows[(REBUILD_SCOPE, REBUILD_ROUTE, self.operation.operation_id)] = self.reservation
        evidence = self.read().terminal_probe
        self.assertEqual(evidence.availability, "present")
        self.assertEqual(evidence.binding, "matched")
        self.assertEqual(evidence.state, "fail")
        assert self.checkpoint.candidate_probe is not None
        self.assertEqual(evidence.attempt, self.checkpoint.candidate_probe.reservation_attempt)

    def test_candidate_probe_binding_is_independent_of_the_inverse_route(self) -> None:
        probe = self.checkpoint.candidate_probe
        assert probe is not None
        key = next(key for key in self.rows if key[0] == REBUILD_CHECKPOINT_SCOPE)
        original = self.rows[key]
        for changes in (
            {"reservation_attempt": self.reservation.attempt + 1},
            {"operation_attempt": self.operation.attempt + 1},
        ):
            with self.subTest(changes=changes):
                checkpoint = self.checkpoint.model_copy(
                    update={"candidate_probe": probe.model_copy(update=changes)}
                )
                self.rows[key] = original.model_copy(
                    update={"response_payload": checkpoint.model_dump(mode="json")}
                )
                evidence = self.read().terminal_probe
                self.assertEqual(evidence.availability, "unbound")
                self.assertIsNone(evidence.state)
                self.assertIsNone(evidence.observed_at)

    def test_candidate_http_cause_is_projected_without_private_probe_material(self) -> None:
        key = next(key for key in self.rows if key[0] == REBUILD_CHECKPOINT_SCOPE)
        self.rows.pop(key)
        provider = TestingProvider(self.plan)
        provider.bad_public = True
        original_route = provider.current_route

        def route(plan: TestingRebuildPlan) -> TestingRouteObservation:
            observation = original_route(plan)
            if observation.slot_id == plan.new.copy_id:
                assert observation.public_health is not None
                health = TestingPublicHealthEvidence.model_validate(
                    {
                        **observation.public_health.model_dump(),
                        "http_status": 503,
                        "urls": (plan.base_url + "/health?token=private-query",),
                        "runtime_identity_detail": "private-body-or-error",
                    }
                )
                return observation.model_copy(update={"public_health": health})
            return observation

        with patch.object(provider, "current_route", side_effect=route):
            checkpoint = execute_testing_rebuild(
                self.plan,
                RecordTestingRebuildStore(cast(RebuildRecordStore, cast(object, self.store))),
                provider,
                clock=lambda: provider.now,
                reservation=self.reservation,
                operation_attempt=self.operation.attempt,
            )
        assert checkpoint.candidate_probe is not None
        self.assertNotIn("private-", checkpoint.candidate_probe.model_dump_json())
        key = next(key for key in self.rows if key[0] == REBUILD_CHECKPOINT_SCOPE)
        self.rows[key] = self.rows[key].model_copy(
            update={"response_payload": checkpoint.model_dump(mode="json")}
        )
        evidence = self.read()
        self.assertEqual(evidence.terminal_probe.http_status, 503)
        self.assertEqual(evidence.terminal_probe.attempt, self.reservation.attempt)
        self.assertNotIn("private-", evidence.model_dump_json())

    def test_failed_checkpoint_reports_probe_loss_without_claiming_recovery(self) -> None:
        key = next(key for key in self.rows if key[0] == REBUILD_CHECKPOINT_SCOPE)
        payload = self.rows[key].response_payload.copy()
        payload.pop("candidate_probe", None)
        self.rows[key] = self.rows[key].model_copy(update={"response_payload": payload})
        evidence = self.read()
        self.assertEqual(evidence.reservation.availability, "present")
        self.assertEqual(evidence.reservation.binding, "matched")
        self.assertEqual(evidence.reservation.attempt, self.reservation.attempt)
        self.assertEqual(evidence.operation_attempt, self.operation.attempt)
        self.assertEqual(evidence.checkpoint.availability, "present")
        self.assertEqual(evidence.terminal_probe.availability, "not_retained")
        self.assertIsNone(evidence.terminal_probe.observed_at)
        self.assertIsNone(evidence.terminal_probe.state)
        self.assertTrue(evidence.rollback_started)
        self.assertEqual(evidence.candidate_preparation.availability, "present")
        self.assertEqual(evidence.candidate_readiness.availability, "present")
        self.assertEqual(evidence.predecessor_snapshot, "identity_recorded")
        self.assertEqual(evidence.inverse_route.availability, "missing")
        self.assertEqual(evidence.deployment.availability, "missing")

    def test_foreign_reservation_stops_before_checkpoint_or_private_intent(self) -> None:
        self.rows[(REBUILD_SCOPE, REBUILD_ROUTE, self.operation.operation_id)] = (
            self.reservation.model_copy(update={"request_fingerprint": "foreign"})
        )
        evidence = self.read()
        self.assertEqual(evidence.reservation.availability, "unbound")
        self.assertEqual(evidence.checkpoint.availability, "unsupported")
        self.store.read_original_mutation_intent.assert_not_called()
        self.assertEqual(self.store.read_idempotency_record.call_count, 1)

    def test_changed_original_intent_or_checkpoint_does_not_publish_receipts(self) -> None:
        key = next(key for key in self.rows if key[0] == REBUILD_CHECKPOINT_SCOPE)
        original_row = self.rows[key]
        for changed in ("intent", "checkpoint", "candidate", "artifact"):
            with self.subTest(changed=changed):
                self.rows[key] = original_row
                self.store.read_original_mutation_intent.return_value = self.intent
                if changed == "intent":
                    self.store.read_original_mutation_intent.return_value = {
                        **self.intent,
                        "operation_id": "foreign",
                    }
                else:
                    payload = self.checkpoint.model_dump(mode="json")
                    if changed == "checkpoint":
                        payload["plan"]["operation_id"] = "foreign"
                    elif changed == "candidate":
                        payload["plan"]["new"]["copy_id"] = "foreign"
                    else:
                        payload["plan"]["new"]["runtime_identity"]["artifact_id"] = "foreign"
                    payload["preparation"]["plan_digest"] = canonical_json_sha256(payload["plan"])
                    payload["preparation"]["copy_id"] = payload["plan"]["new"]["copy_id"]
                    payload["readiness"]["copy_id"] = payload["plan"]["new"]["copy_id"]
                    payload["readiness"]["runtime_identity"] = payload["plan"]["new"][
                        "runtime_identity"
                    ]
                    self.rows[key] = original_row.model_copy(
                        update={
                            "response_payload": payload,
                            "request_fingerprint": canonical_json_sha256(payload["plan"]),
                        }
                    )
                evidence = self.read()
                self.assertEqual(evidence.checkpoint.availability, "unbound")
                self.assertEqual(evidence.candidate_preparation.availability, "unsupported")

    def test_missing_unsupported_and_unavailable_records_stay_distinct(self) -> None:
        self.assertEqual(
            read_native_testing_rebuild_evidence(object(), self.operation).reservation.availability,
            "unsupported",
        )
        self.store.read_idempotency_record.side_effect = OSError("fixture-private-detail")
        self.assertEqual(self.read().reservation.availability, "unavailable")
        self.store.read_idempotency_record.side_effect = None
        self.store.read_idempotency_record.return_value = None
        self.assertEqual(self.read().reservation.availability, "missing")

    def test_projection_omits_private_coordinates_and_never_writes(self) -> None:
        readonly = Mock(spec=NativeTestingEvidenceStore)
        readonly.read_idempotency_record = self.store.read_idempotency_record
        readonly.read_original_mutation_intent = self.store.read_original_mutation_intent
        readonly.read_deployment_record = self.store.read_deployment_record
        with patch.object(socket.socket, "connect", side_effect=AssertionError("provider IO")):
            result = read_native_testing_rebuild_evidence(readonly, self.operation)
        self.assertEqual(result.checkpoint.availability, "present")
        evidence = result.model_dump_json()
        for private in (
            self.operation.idempotency_key,
            self.intent["private"],
            self.plan.base_url,
            self.plan.old.target_id,
            self.plan.new.target_id,
            self.reservation.provider_target_key,
            self.reservation.response_trace_id,
        ):
            self.assertNotIn(private, evidence)
        self.store.compare_and_write_testing_rebuild_checkpoint.assert_not_called()

    def app(self, *, read_only: bool = False, authorized: bool = True) -> FastAPI:
        app = FastAPI()

        def identity() -> LocalOperatorIdentity:
            return LocalOperatorIdentity(subject="fixture", token_label="fixture")

        def error(**kwargs: Any) -> HTTPException:
            return HTTPException(kwargs["status_code"], detail=kwargs["code"])

        allowed = Mock(
            side_effect=lambda **kw: (
                authorized and (not read_only or kw["action"] == "operations.read")
            )
        )
        register_operation_status_read_routes(
            cast(ApiRouteRegistrar, cast(object, app)),
            dependencies=ReadRouteDependencies(
                read_identity=identity,
                get_record_store=lambda: self.store,
                next_trace_id=lambda: "launchplane_req_fixture",
                authorization_allows=allowed,
                http_error=error,
                error_response_model=BaseModel,
            ),
        )
        return app

    def test_default_get_keeps_existing_consumer_shape_and_does_not_read_native_records(
        self,
    ) -> None:
        path = ODOO_STABLE_TARGET_REPLACEMENT_OPERATION_STATUS_ROUTE.format(
            operation_id=self.operation.operation_id
        )
        for read_only in (False, True):
            with self.subTest(read_only=read_only):
                response = asyncio.run(request(self.app(read_only=read_only), "GET", path))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn("native_evidence", response.json())
                self.assertEqual(response.json()["operation"]["attempt"], self.operation.attempt)
                self.assertFalse(response.json()["recovery"]["safe_to_release"])
        self.store.read_idempotency_record.assert_not_called()
        self.store.read_original_mutation_intent.assert_not_called()

    def test_opt_in_uses_same_scoped_read_authority_and_does_not_change_status_or_recovery(
        self,
    ) -> None:
        path = ODOO_STABLE_TARGET_REPLACEMENT_OPERATION_STATUS_ROUTE.format(
            operation_id=self.operation.operation_id
        )
        for read_only in (False, True):
            with self.subTest(read_only=read_only):
                app = self.app(read_only=read_only)
                ordinary = asyncio.run(request(app, "GET", path)).json()
                opted = asyncio.run(request(app, "GET", path + "?include_native_evidence=true"))
                self.assertEqual(opted.status_code, 200, opted.text)
                payload = opted.json()
                evidence = NativeTestingRebuildEvidence.model_validate(
                    payload.pop("native_evidence")
                )
                self.assertEqual(payload, ordinary)
                self.assertEqual(evidence.checkpoint.binding, "matched")
                self.assertFalse(payload["recovery"]["safe_to_release"])

    def test_opt_in_denial_does_not_read_additional_private_records(self) -> None:
        path = ODOO_STABLE_TARGET_REPLACEMENT_OPERATION_STATUS_ROUTE.format(
            operation_id=self.operation.operation_id
        )
        response = asyncio.run(
            request(self.app(authorized=False), "GET", path + "?include_native_evidence=true")
        )
        self.assertEqual(response.status_code, 403)
        self.store.read_idempotency_record.assert_not_called()
        self.store.read_original_mutation_intent.assert_not_called()

    def test_foreign_effect_receipt_is_unbound_and_missing_is_not_no_effect(self) -> None:
        phase = "route:" + self.plan.old.copy_id
        key = self.operation.operation_id + ":" + phase
        self.rows[(REBUILD_SCOPE, REBUILD_ROUTE + ":effect", key)] = self.row(
            REBUILD_SCOPE, REBUILD_ROUTE + ":effect", key, "foreign"
        )
        evidence = self.read()
        self.assertEqual(evidence.inverse_route.availability, "unbound")
        self.assertEqual(evidence.forward_route.availability, "missing")

    def test_bound_effect_receipt_reports_its_attempt_without_claiming_a_provider_outcome(
        self,
    ) -> None:
        phase = "route:" + self.plan.old.copy_id
        key = self.operation.operation_id + ":" + phase
        fingerprint = canonical_json_sha256({"intent": self.intent_digest, "phase": phase})
        self.rows[(REBUILD_SCOPE, REBUILD_ROUTE + ":effect", key)] = self.row(
            REBUILD_SCOPE, REBUILD_ROUTE + ":effect", key, fingerprint
        ).model_copy(
            update={
                "reconciliation_key": fingerprint,
                "provider_target_key": self.reservation.provider_target_key + ":" + phase,
            }
        )
        evidence = self.read()
        self.assertEqual(evidence.inverse_route.availability, "present")
        self.assertEqual(evidence.inverse_route.binding, "matched")
        self.assertEqual(evidence.inverse_route.state, "completed")
        self.assertEqual(evidence.inverse_route.attempt, self.reservation.attempt)

    def test_retained_candidate_probe_uses_its_recorded_time_and_preserves_null(self) -> None:
        provider = TestingProvider(self.plan)
        provider.serving = self.plan.new
        provider.bad_public = True
        observation = provider.current_route(self.plan)
        key = next(key for key in self.rows if key[0] == REBUILD_CHECKPOINT_SCOPE)
        original = self.rows[key]
        for probe in (
            observation,
            observation.model_copy(update={"public_health": None}),
            observation.model_copy(update={"host": "foreign.invalid"}),
        ):
            with self.subTest(has_health=probe.public_health is not None):
                checkpoint = self.checkpoint.model_copy(
                    update={"route": probe, "candidate_probe": None}
                )
                self.rows[key] = original.model_copy(
                    update={"response_payload": checkpoint.model_dump(mode="json")}
                )
                evidence = self.read().terminal_probe
                if probe.host != self.plan.host:
                    self.assertEqual(evidence.availability, "unbound")
                    self.assertIsNone(evidence.observed_at)
                    self.assertIsNone(evidence.state)
                elif probe.public_health is None:
                    self.assertEqual(evidence.availability, "null")
                    self.assertIsNone(evidence.verified)
                    self.assertIsNone(evidence.state)
                else:
                    self.assertEqual(evidence.availability, "present")
                    self.assertEqual(evidence.state, probe.public_health.status)
                    self.assertEqual(evidence.observed_at, probe.observed_at)
                    self.assertTrue(evidence.runtime_identity_matches)

    def test_completed_route_receipt_preserves_explicit_non_dispatch_markers(self) -> None:
        phase = "route:" + self.plan.new.copy_id
        key = self.operation.operation_id + ":" + phase
        fingerprint = canonical_json_sha256({"intent": self.intent_digest, "phase": phase})
        self.rows[(REBUILD_SCOPE, REBUILD_ROUTE + ":effect", key)] = self.row(
            REBUILD_SCOPE,
            REBUILD_ROUTE + ":effect",
            key,
            fingerprint,
            {"route_write_not_started": True, "not_dispatched": False},
        ).model_copy(
            update={
                "reconciliation_key": fingerprint,
                "provider_target_key": self.reservation.provider_target_key + ":" + phase,
            }
        )
        receipt = self.read().forward_route
        self.assertEqual(receipt.state, "completed")
        self.assertTrue(receipt.route_write_not_started)
        self.assertFalse(receipt.not_dispatched)

    def test_generic_web_intent_is_unsupported_without_a_native_checkpoint_lookup(self) -> None:
        intent = {**self.intent, "state": {}}
        self.store.read_original_mutation_intent.return_value = intent
        self.rows[(REBUILD_SCOPE, REBUILD_ROUTE, self.operation.operation_id)] = (
            self.reservation.model_copy(
                update={"reconciliation_key": canonical_json_sha256(intent)}
            )
        )
        for key in list(self.rows):
            if key[0] == REBUILD_CHECKPOINT_SCOPE:
                del self.rows[key]
        evidence = self.read()
        self.assertEqual(evidence.checkpoint.availability, "unsupported")
        self.assertEqual(self.store.read_idempotency_record.call_count, 1)

    def test_undecodable_checkpoint_is_unavailable_rather_than_a_foreign_binding(self) -> None:
        key = next(key for key in self.rows if key[0] == REBUILD_CHECKPOINT_SCOPE)
        self.rows[key] = self.rows[key].model_copy(update={"response_payload": {}})
        evidence = self.read()
        self.assertEqual(evidence.checkpoint.availability, "unavailable")
        self.assertEqual(evidence.checkpoint.binding, "unverified")

    def test_unsupported_native_source_mode_returns_an_item_instead_of_raising(self) -> None:
        self.operation = self.operation.model_copy(
            update={
                "request": self.operation.request.model_copy(
                    update={"data_source_mode": "existing"}
                )
            }
        )
        self.assertEqual(self.read().checkpoint.availability, "unbound")

    def test_predecessor_snapshot_keeps_recorded_absence_separate_from_receipt_presence(
        self,
    ) -> None:
        for availability in ("stopped", "absent"):
            with self.subTest(availability=availability):
                payload = self.plan.model_dump(mode="json")
                payload["old"] = {
                    "copy_id": self.plan.old.copy_id,
                    "target_id": self.plan.old.target_id,
                    "availability": availability,
                    "observation_sha256": "c" * 64,
                    "route_applied": False,
                }
                intent = {**self.intent, "old": payload["old"]}
                digest = canonical_json_sha256(intent)
                payload["authority_digest"] = digest
                record = TestingRebuildRecord.model_validate(
                    {
                        "plan": payload,
                        "began_at": self.checkpoint.began_at,
                        "updated_at": self.checkpoint.updated_at,
                    }
                )
                key = next(key for key in self.rows if key[0] == REBUILD_CHECKPOINT_SCOPE)
                self.rows[key] = self.rows[key].model_copy(
                    update={
                        "response_payload": record.model_dump(mode="json"),
                        "request_fingerprint": record.plan.digest,
                    }
                )
                self.store.read_original_mutation_intent.return_value = intent
                self.rows[(REBUILD_SCOPE, REBUILD_ROUTE, self.operation.operation_id)] = (
                    self.reservation.model_copy(update={"reconciliation_key": digest})
                )
                evidence = self.read()
                self.assertEqual(evidence.predecessor_snapshot, availability)
                self.assertEqual(evidence.candidate_preparation.availability, "null")
                self.assertEqual(evidence.candidate_readiness.availability, "null")
                self.assertFalse(evidence.rollback_started)


if __name__ == "__main__":
    unittest.main()
