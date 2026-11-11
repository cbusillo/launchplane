from datetime import datetime, timezone
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.testing_candidate import (
    TestingCandidateAllocation,
    testing_candidate_namespace,
)


class CandidateRetentionPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        from control_plane import testing_candidate_retention as retention
        from control_plane.contracts.testing_candidate_retention import NativeTestingSweepRequest
        from control_plane.contracts.deploy_target import ProviderTargetRecord
        from tests.test_testing_replacement_resources import TestingReplacementResourceFixture
        from unittest.mock import patch

        self.retention = retention
        fixture = TestingReplacementResourceFixture(self)
        self.store, self.root = fixture.store, fixture.root
        self.request = NativeTestingSweepRequest(
            kind="native-testing",
            product=fixture.profile.product,
            context=fixture.context,
            operation_id=fixture.operation.operation_id,
        )
        original = fixture.operation.model_copy(
            update={
                "operation_id": "failed-original",
                "idempotency_key": "failed-key",
                "status": "fail",
                "phase": "failed",
                "finished_at": fixture.operation.updated_at,
                "error_message": "Fabricated restore failed before serving",
                "error_code": "candidate_preparation_failed",
                "request": fixture.operation.request.model_copy(
                    update={
                        "strategy": "replace-and-cutover",
                        "data_source_mode": "empty",
                        "allow_empty_data": True,
                        "artifact_id": "fixture-artifact",
                        "source_git_ref": "a" * 40,
                    }
                ),
            }
        )
        original = type(original).model_validate(original.model_dump())
        self.store.write_odoo_stable_target_replacement_operation_record(original)
        self.candidate = TestingCandidateAllocation(
            namespace=testing_candidate_namespace(
                original.product, original.context, original.operation_id
            ),
            product=original.product,
            context=original.context,
            operation_id=original.operation_id,
            intent_sha256="a" * 64,
            serving_target_id="compose-testing",
            server_id="host",
            project_id="project",
            environment_id="environment",
            compose_id="failed-copy",
            created_at=datetime.now(timezone.utc),
        )
        self.store_candidate(self.candidate)
        self.store.write_provider_target_record(
            ProviderTargetRecord(
                provider_id="dokploy",
                context=self.candidate.namespace,
                instance="testing-candidate",
                target_category="compose",
                target_id="failed-copy",
                display_name="Fabricated failed candidate",
                updated_at=original.updated_at,
            )
        )
        self.observation: dict[str, Any] = {
            "volumes": [
                {
                    "role": "ODOO_DB_VOLUME",
                    "name": self.candidate.namespace + "-db",
                    "status": "observed",
                    "reason": "",
                    "identity": "b" * 64,
                    "engine_identity": "c" * 64,
                    "empty": True,
                    "boot_id": "boot",
                    "monotonic_deadline": 1000000000,
                }
            ],
            "capacity": [],
            "capacity_refusal": "receiver_generation_unavailable",
        }
        from control_plane import testing_candidate_release as release

        self.enterContext(patch.object(release, "candidate_routed", return_value=False))
        self.enterContext(patch.object(release, "require_lane_releasable", return_value=""))
        self.inspect = self.enterContext(
            patch.object(
                retention, "inspect_candidate_resources", return_value=(self.observation, "")
            )
        )

    def plan(self) -> Any:
        request = self.request.model_copy(update={"candidates": (self.candidate.namespace,)})
        return self.retention.build_native_testing_sweep(self.store, self.root, request, "trace")[0]

    def store_candidate(self, candidate: TestingCandidateAllocation) -> None:
        from control_plane.storage.postgres import LaunchplaneTestingCandidateRow

        with self.store._session_factory() as session:
            session.merge(
                LaunchplaneTestingCandidateRow(
                    namespace=candidate.namespace, payload=self.store._payload_dict(candidate)
                )
            )
            session.commit()

    def with_application(self, *, prepared: bool) -> TestingCandidateAllocation:
        from control_plane.contracts.artifact_identity import (
            ArtifactIdentityManifest,
            ArtifactImageReference,
        )
        from control_plane.contracts.runtime_identity import RuntimeIdentity
        from control_plane.contracts.testing_candidate_application import (
            TestingCandidateApplication,
            TestingCandidateApplicationPlan,
            TestingCandidatePrepared,
        )

        digest = "sha256:" + "d" * 64
        identity = RuntimeIdentity(
            product=self.candidate.product,
            context=self.candidate.context,
            instance="testing",
            deployment_record_id="fixture-deployment",
            artifact_id="fixture-artifact",
            source_git_ref="a" * 40,
            image_reference="ghcr.io/example/app@" + digest,
            image_digest=digest,
        )
        plan = TestingCandidateApplicationPlan(
            product=self.candidate.product,
            context=self.candidate.context,
            operation_id=self.candidate.operation_id,
            namespace=self.candidate.namespace,
            target_id=self.candidate.compose_id,
            base_url="https://testing.example",
            artifact=ArtifactIdentityManifest(
                artifact_id="fixture-artifact",
                source_commit="a" * 40,
                enterprise_base_digest="sha256:" + "b" * 64,
                image=ArtifactImageReference(repository="ghcr.io/example/app", digest=digest),
            ),
            runtime_identity=identity,
            source="empty",
            authority_sha256="c" * 64,
            credential_bindings_sha256="f" * 64,
        )
        application = (
            TestingCandidateApplication(
                plan=plan,
                phase="prepared",
                deployment_id="fixture-deployment",
                prepared=TestingCandidatePrepared(
                    plan_sha256=plan.digest,
                    runtime_identity=identity,
                    private_endpoint="http://fixture-private",
                    deployment_id="fixture-deployment",
                    source_witness="fixture-witness",
                    integration_isolation_verified=True,
                    credentials_verified=True,
                    observed_at=datetime.now(timezone.utc),
                ),
            )
            if prepared
            else TestingCandidateApplication(
                plan=plan, phase="abandoned", abandonment_evidence="fixture-restore-failed"
            )
        )
        self.candidate = self.candidate.model_copy(
            update={"resources_binding_sha256": "e" * 64, "application": application}
        )
        self.candidate = TestingCandidateAllocation.model_validate(self.candidate.model_dump())
        self.store_candidate(self.candidate)
        return self.candidate

    def selected(self) -> Any:
        return next(
            item for item in self.plan().candidates if item.namespace == self.candidate.namespace
        )

    def test_history_references_completed_copy_and_missing_source_do_not_hold_failed_copy(
        self,
    ) -> None:
        self.with_application(prepared=True)
        later = self.candidate.model_copy(
            update={
                "namespace": testing_candidate_namespace(
                    self.candidate.product, self.candidate.context, "later"
                ),
                "operation_id": "later",
                "serving_target_id": self.candidate.compose_id,
                "compose_id": "later-target",
                "application": None,
                "resources_binding_sha256": "",
            }
        )
        self.store_candidate(later)
        operation = self.store.read_odoo_stable_target_replacement_operation_record(
            self.candidate.operation_id
        )
        self.store.write_odoo_stable_target_replacement_operation_record(
            operation.model_copy(update={"operation_id": "later"})
        )
        item = self.selected()
        self.assertEqual(item.status, "eligible")
        self.assertEqual(item.reasons, ())
        self.assertEqual(item.references, ())

    def test_unavailable_observation_is_reported_without_holding_the_copy(self) -> None:
        self.inspect.side_effect = OSError("unavailable inventory")
        item = self.selected()
        self.assertEqual(item.status, "eligible")
        self.assertTrue(all(row.status == "eligible" for row in item.resources))
        self.assertEqual([row.reason for row in item.references], ["physical_inventory_unknown"])

    def test_restore_receiver_is_excluded_even_if_it_failed(self) -> None:
        self.request = self.request.model_copy(
            update={"receiver_operation_id": self.candidate.operation_id}
        )
        item = self.selected()
        self.assertEqual(item.status, "held")
        self.assertIn("receiver_candidate", item.reasons)

    def test_foreign_or_production_original_cannot_enter_candidate_plan(self) -> None:
        operation = self.store.read_odoo_stable_target_replacement_operation_record(
            self.candidate.operation_id
        )
        with patch.object(
            self.store,
            "read_odoo_stable_target_replacement_operation_record",
            return_value=operation.model_copy(update={"instance": "prod"}),
        ):
            with self.assertRaises(ValueError):
                self.plan()
