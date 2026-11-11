from dataclasses import replace
from datetime import UTC, datetime, timedelta
import asyncio
import socket
from typing import Any, cast
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
)
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
from control_plane.contracts.testing_candidate import (
    testing_candidate_namespace,
    TestingCandidateAllocation,
)
from control_plane.contracts.testing_candidate_application import (
    TestingCandidateApplication,
    TestingCandidateApplicationPlan,
)
from control_plane.contracts.artifact_identity import (
    ArtifactIdentityManifest,
    ArtifactImageReference,
)
from control_plane.dokploy.testing_route import testing_route_id
from control_plane.dokploy.testing_route_execution import (
    TestingRouteExecutionIntent,
    TestingRouteExecutionCustody,
)
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
    read_native_testing_execution_binding,
    _route_read,
)
from control_plane.contracts.native_testing_rebuild_evidence import (
    NativeTestingRebuildEvidence,
    NativeTestingExecutionBinding,
)
from control_plane.http_routes.drivers import (
    ODOO_STABLE_TARGET_REPLACEMENT_OPERATION_STATUS_ROUTE,
    register_operation_status_read_routes,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import LocalOperatorIdentity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_rebuild_authorization import (
    capture_director_testing_rebuild_authorization,
)
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
            update={
                "route_id": testing_route_id(self.plan.product, self.plan.context),
                "new": self.plan.new.model_copy(
                    update={
                        "copy_id": namespace,
                        "resource_binding": "d" * 64,
                        "runtime_identity": self.plan.new.runtime_identity.model_copy(
                            update={
                                "source_git_ref": "e" * 40,
                                "image_reference": "ghcr.io/example/app@sha256:" + "a" * 64,
                            }
                        ),
                    }
                ),
            }
        )
        self.intent: dict[str, Any] = {
            "operation_id": self.plan.operation_id,
            "old": self.plan.old.model_dump(mode="json"),
            "private": "fixture-private-topology-and-credential-material",
            "server_id": "fixture-execution-server",
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
        self.identity = LocalOperatorIdentity(subject="fixture", token_label="fixture")
        self.store = Mock(spec=PostgresRecordStore)
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
        self.store.read_testing_candidate.return_value = None
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

    def allocation(self) -> TestingCandidateAllocation:
        plan = self.plan
        identity = plan.new.runtime_identity
        return TestingCandidateAllocation(
            namespace=plan.new.copy_id,
            product=plan.product,
            context=plan.context,
            operation_id=plan.operation_id,
            intent_sha256=plan.authority_digest,
            serving_target_id=plan.old.target_id,
            server_id=self.intent["server_id"],
            project_id="private-project",
            environment_id="private-environment",
            compose_id=plan.new.target_id,
            resources_binding_sha256=plan.new.resource_binding,
            created_at=self.checkpoint.began_at,
            application=TestingCandidateApplication(
                plan=TestingCandidateApplicationPlan(
                    product=plan.product,
                    context=plan.context,
                    operation_id=plan.operation_id,
                    namespace=plan.new.copy_id,
                    target_id=plan.new.target_id,
                    base_url=plan.base_url,
                    artifact=ArtifactIdentityManifest(
                        artifact_id=identity.artifact_id,
                        source_commit=identity.source_git_ref,
                        enterprise_base_digest="sha256:" + "b" * 64,
                        image=ArtifactImageReference(
                            repository="ghcr.io/example/app", digest=identity.image_digest
                        ),
                    ),
                    runtime_identity=identity,
                    source="empty",
                    authority_sha256="c" * 64,
                    credential_bindings_sha256="f" * 64,
                )
            ),
        )

    def private_read(self) -> NativeTestingExecutionBinding:
        return read_native_testing_execution_binding(self.store, self.operation)

    def test_private_allocation_requires_original_resources_and_application_identity(self) -> None:
        allocation = self.allocation()
        assert allocation.application is not None
        self.store.read_testing_candidate.return_value = allocation
        binding = self.private_read()
        self.assertEqual(binding.candidate.binding, "matched")
        assert binding.candidate.selector is not None
        self.assertEqual(binding.candidate.selector.compose_id, self.plan.new.target_id)
        self.assertEqual(binding.candidate.selector.server_id, self.intent["server_id"])
        self.assertEqual(binding.intent_sha256, self.reservation.reconciliation_key)
        self.assertEqual(binding.checkpoint_plan_sha256, self.plan.digest)
        for changes in (
            {"operation_id": "foreign"},
            {"server_id": "foreign"},
            {"intent_sha256": "a" * 64},
            {"compose_id": "foreign"},
            {"resources_binding_sha256": "a" * 64},
            {
                "application": allocation.application.model_copy(
                    update={
                        "plan": allocation.application.plan.model_copy(
                            update={"runtime_identity": self.plan.old.runtime_identity}
                        )
                    }
                )
            },
        ):
            with self.subTest(changes=changes):
                self.store.read_testing_candidate.return_value = allocation.model_copy(
                    update=changes
                )
                result = self.private_read().candidate
                self.assertEqual(result.availability, "unbound")
                self.assertIsNone(result.selector)

    def test_missing_and_unavailable_allocation_are_distinct_from_unbound(self) -> None:
        self.assertEqual(self.private_read().candidate.availability, "missing")
        self.store.read_testing_candidate.side_effect = OSError("private failure")
        result = self.private_read().candidate
        self.assertEqual(result.availability, "unavailable")
        self.assertIsNone(result.selector)

    def test_foreign_original_does_not_read_candidate_or_route_intents(self) -> None:
        self.store.read_original_mutation_intent.return_value = {
            **self.intent,
            "operation_id": "foreign",
        }
        result = self.private_read()
        self.assertEqual(result.native_evidence.original_intent.binding, "mismatched")
        self.store.read_testing_candidate.assert_not_called()
        self.assertEqual(self.store.read_original_mutation_intent.call_count, 1)

    def route_execution(
        self, *, inverse: bool = False
    ) -> tuple[LaunchplaneIdempotencyRecord, TestingRouteExecutionIntent]:
        desired = self.plan.old if inverse else self.plan.new
        phase = "route:" + desired.copy_id
        key = self.operation.operation_id + ":" + phase
        fingerprint = canonical_json_sha256({"intent": self.intent_digest, "phase": phase})
        receipt = self.row(REBUILD_SCOPE, REBUILD_ROUTE + ":effect", key, fingerprint).model_copy(
            update={
                "reconciliation_key": fingerprint,
                "provider_target_key": self.reservation.provider_target_key + ":" + phase,
            }
        )
        self.rows[(receipt.scope, receipt.route_path, key)] = receipt
        execution = TestingRouteExecutionIntent(
            operation_id=self.plan.operation_id,
            server_id=self.intent["server_id"],
            route_id=self.plan.route_id,
            route_digest="a" * 64,
            document_sha256="b" * 64,
            backend=desired.private_url,
            custody=TestingRouteExecutionCustody(mount_source="/private/route", filesystem="1:2"),
        )
        self.store.read_original_mutation_intent.side_effect = lambda row: (
            {"route_execution": execution.model_dump(mode="json")}
            if row.record_id == receipt.record_id
            else self.intent
        )
        return receipt, execution

    def test_route_intent_returns_bound_desired_backend_without_claiming_current_backend(
        self,
    ) -> None:
        receipt, execution = self.route_execution()
        result = self.private_read().forward_route
        self.assertEqual(result.availability, "present")
        assert result.selector is not None
        self.assertEqual(result.selector.backend, execution.backend)
        self.assertEqual(result.attempt, receipt.attempt)
        self.assertIsNone(result.route_digest_matches)
        self.assertEqual(result.binding, "unverified")
        self.assertIsNone(result.observed_backend_matches)
        self.assertIsNone(result.backend_observed_at)

    def test_foreign_route_intent_cannot_select_a_private_read_target(self) -> None:
        receipt, execution = self.route_execution()
        for changes in (
            {"operation_id": "foreign"},
            {"server_id": "foreign"},
            {"backend": "http://foreign:8069"},
        ):
            with self.subTest(changes=changes):
                self.store.read_original_mutation_intent.side_effect = lambda row: (
                    {
                        "route_execution": execution.model_copy(update=changes).model_dump(
                            mode="json"
                        )
                    }
                    if row.record_id == receipt.record_id
                    else self.intent
                )
                result = self.private_read().forward_route
                self.assertEqual(result.availability, "unbound")
                self.assertIsNone(result.selector)

    def test_legacy_route_without_execution_intent_is_not_a_missing_receipt(self) -> None:
        receipt, _ = self.route_execution()
        self.store.read_original_mutation_intent.side_effect = lambda row: (
            {} if row.record_id == receipt.record_id else self.intent
        )
        result = self.private_read()
        self.assertEqual(result.native_evidence.forward_route.availability, "present")
        self.assertEqual(result.forward_route.availability, "not_retained")

    def test_route_digest_requires_the_retained_original_route_plan(self) -> None:
        from tests.test_testing_publication import TestingPublicationTests

        fixture = TestingPublicationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        publication = fixture.prepare()
        receipt, execution = self.route_execution()
        evidence = self.read().forward_route
        route = publication.plan.route
        plan = self.plan.model_copy(
            update={
                "product": route.product,
                "context": route.context,
                "base_url": "https://" + route.host,
                "route_id": route.route_id,
                "old": self.plan.old.model_copy(update={"private_url": route.old_backend}),
                "new": self.plan.new.model_copy(update={"private_url": route.new_backend}),
            }
        )
        checkpoint = self.checkpoint.model_copy(update={"plan": plan, "route": None})
        for digest, expected in ((route.digest, "matched"), ("0" * 64, "mismatched")):
            with self.subTest(digest=digest):
                self.store.read_original_mutation_intent.side_effect = None
                self.store.read_original_mutation_intent.return_value = {
                    "route_execution": execution.model_copy(
                        update={
                            "route_digest": digest,
                            "route_id": route.route_id,
                            "backend": route.new_backend,
                        }
                    ).model_dump(mode="json")
                }
                result = _route_read(
                    cast(NativeTestingEvidenceStore, cast(object, self.store)),
                    receipt,
                    evidence,
                    checkpoint,
                    self.intent,
                    inverse=False,
                    publication=publication,
                )
                self.assertEqual(result.binding, expected)
                self.assertEqual(result.route_digest_matches, expected == "matched")
                self.assertEqual(result.selector is not None, expected == "matched")

    def test_readiness_returns_retained_success_without_borrowing_public_probe_attempt(
        self,
    ) -> None:
        readiness = self.checkpoint.readiness
        assert readiness is not None
        evidence = self.read().candidate_readiness
        self.assertEqual(evidence.state, "pass")
        self.assertTrue(evidence.runtime_identity_matches)
        self.assertEqual(evidence.observed_at, readiness.observed_at)
        self.assertIsNone(evidence.attempt)
        self.assertIsNone(evidence.verified)

    def test_private_binding_preserves_existing_full_record_authority(self) -> None:
        path = ODOO_STABLE_TARGET_REPLACEMENT_OPERATION_STATUS_ROUTE.format(
            operation_id=self.operation.operation_id
        )
        for read_only in (False, True):
            with self.subTest(read_only=read_only):
                self.store.reset_mock()
                response = asyncio.run(
                    request(
                        self.app(read_only=read_only),
                        "GET",
                        path + "?include_native_execution_binding=true",
                    )
                )
                if read_only:
                    self.assertEqual(response.status_code, 403)
                    self.store.read_idempotency_record.assert_not_called()
                    self.store.read_original_mutation_intent.assert_not_called()
                    self.store.read_testing_candidate.assert_not_called()
                    continue
                self.assertEqual(response.status_code, 200, response.text)
                binding = response.json()["native_execution_binding"]
                self.assertEqual(binding["operation_id"], self.operation.operation_id)
                self.assertEqual(binding["native_evidence"]["candidate_readiness"]["state"], "pass")
                self.assertEqual(binding["failure_reference"]["availability"], "not_retained")
                self.assertNotIn("native_evidence", response.json())

    def test_private_binding_accepts_only_the_recorded_native_director_owner_without_grants(
        self,
    ) -> None:
        owner = replace(self.identity, director_agent=True)
        profile = LaunchplaneProductProfileRecord(
            product=self.operation.product,
            display_name="Fixture",
            repository="example/fixture",
            driver_id="odoo",
            image=ProductImageProfile(repository="ghcr.io/example/fixture"),
            runtime_port=8069,
            health_path="/web/health",
            lanes=(ProductLaneProfile(context=self.operation.context, instance="testing"),),
            updated_at=self.operation.updated_at,
            source="fixture",
        )
        self.store.read_product_profile_record.return_value = profile
        self.store.list_product_profile_records.return_value = (profile,)
        self.operation.authorization = capture_director_testing_rebuild_authorization(
            identity=owner,
            request=self.operation.request,
            context=self.operation.context,
            store=self.store,
            authorized_at=self.operation.created_at,
        )
        path = ODOO_STABLE_TARGET_REPLACEMENT_OPERATION_STATUS_ROUTE.format(
            operation_id=self.operation.operation_id
        )
        for caller, expected in (
            (owner, 200),
            (replace(owner, subject="another-operator"), 403),
            (replace(owner, token_label="another-token"), 403),
            (replace(owner, director_agent=False), 403),
        ):
            with self.subTest(caller=caller):
                self.identity = caller
                self.store.reset_mock()
                response = asyncio.run(
                    request(
                        self.app(authorized=False),
                        "GET",
                        path + "?include_native_execution_binding=true",
                    )
                )
                self.assertEqual(response.status_code, expected, response.text)
                if expected == 200:
                    binding = response.json()["native_execution_binding"]
                    self.assertEqual(binding["operation_id"], self.operation.operation_id)
                    self.assertEqual(binding["execution_server_id"], self.intent["server_id"])
                    self.assertFalse(response.json()["recovery"]["safe_to_release"])
                else:
                    self.store.read_original_mutation_intent.assert_not_called()
                    self.store.read_testing_candidate.assert_not_called()
                self.store.compare_and_write_testing_rebuild_checkpoint.assert_not_called()

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
            return self.identity

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
                self.assertNotIn("native_execution_binding", response.json())
                self.assertEqual(response.json()["operation"]["attempt"], self.operation.attempt)
                self.assertFalse(response.json()["recovery"]["safe_to_release"])
        self.store.read_idempotency_record.assert_not_called()
        self.store.read_original_mutation_intent.assert_not_called()
        self.store.read_testing_candidate.assert_not_called()

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
        for query in ("include_native_evidence", "include_native_execution_binding"):
            response = asyncio.run(
                request(self.app(authorized=False), "GET", path + "?" + query + "=true")
            )
            self.assertEqual(response.status_code, 403)
        self.store.read_idempotency_record.assert_not_called()
        self.store.read_original_mutation_intent.assert_not_called()
        self.store.read_testing_candidate.assert_not_called()

    def test_private_read_omits_raw_material_and_never_calls_transports_or_writers(self) -> None:
        self.store.read_testing_candidate.return_value = self.allocation()
        self.route_execution(inverse=True)
        with patch.object(socket.socket, "connect", side_effect=AssertionError("provider IO")):
            result = self.private_read()
        self.assertEqual(result.execution_server_id, self.intent["server_id"])
        self.assertTrue(result.inverse_route.observed_backend_matches)
        assert self.checkpoint.route is not None
        self.assertEqual(
            result.inverse_route.backend_observed_at, self.checkpoint.route.observed_at
        )
        payload = result.model_dump_json()
        for private in (
            self.intent["private"],
            "/private/route",
            self.reservation.response_trace_id,
            self.operation.idempotency_key,
        ):
            self.assertNotIn(private, payload)
        self.store.compare_and_write_testing_rebuild_checkpoint.assert_not_called()
        self.store.reset_mock()
        public = self.read().model_dump_json()
        self.assertNotIn(self.intent["server_id"], public)
        self.assertNotIn(self.plan.new.target_id, public)
        self.store.read_testing_candidate.assert_not_called()

    def test_failure_reference_uses_only_bound_checkpoint_markers_without_attribution(self) -> None:
        key = next(key for key in self.rows if key[0] == REBUILD_CHECKPOINT_SCOPE)
        checkpoint = self.checkpoint.model_copy(
            update={
                "error": "candidate_private_readiness_failed",
                "readiness": None,
                "rollback_started": False,
                "route": None,
                "candidate_probe": None,
                "private_readiness_failed_at": self.checkpoint.updated_at,
                "preparation_failure_evidence": "private-text:copy_failure_stage=host_preparation:copy_failure_kind=command_failed:copy_failure_exit_status=1:copy_failure_evidence_id="
                + "d" * 32,
            }
        )
        self.rows[key] = self.rows[key].model_copy(
            update={"response_payload": checkpoint.model_dump(mode="json")}
        )
        result = self.private_read()
        self.assertEqual(result.failure_reference.evidence_id, "d" * 32)
        self.assertEqual(result.failure_reference.stage, "host_preparation")
        self.assertIsNone(result.failure_reference.attempt)
        self.assertIsNone(result.failure_reference.observed_at)
        readiness = result.native_evidence.candidate_readiness
        self.assertEqual(readiness.state, "fail")
        self.assertEqual(readiness.observed_at, checkpoint.private_readiness_failed_at)
        self.assertIsNone(readiness.runtime_identity_matches)
        self.assertIsNone(readiness.attempt)
        self.assertNotIn("private-text", result.model_dump_json())

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
