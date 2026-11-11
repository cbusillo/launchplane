from datetime import datetime, timezone
from typing import Any
import unittest
from unittest.mock import Mock

from control_plane.contracts.testing_candidate import (
    TestingCandidateAllocation,
    testing_candidate_namespace,
)
from control_plane.testing_candidate_retention import candidate_record_refusals


class CandidateRetentionGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = TestingCandidateAllocation(
            namespace=testing_candidate_namespace("odoo", "site", "failed-original"),
            product="odoo",
            context="site",
            operation_id="failed-original",
            intent_sha256="a" * 64,
            serving_target_id="selected",
            server_id="host",
            project_id="project",
            environment_id="environment",
            compose_id="failed-copy",
            created_at=datetime.now(timezone.utc),
        )
        self.operation: Any = Mock(
            product="odoo",
            context="site",
            instance="testing",
            status="fail",
            lease_owner="",
            lease_expires_at="",
            is_native_testing_rebuild=True,
        )

    def refusal(self, **kwargs: Any) -> tuple[str, ...]:
        return candidate_record_refusals(
            self.candidate,
            self.operation,
            product="odoo",
            context="site",
            receiver_operation_id="pending-original",
            selected_targets=(),
            **kwargs,
        )

    def test_active_original_foreign_instance_and_selected_copy_each_refuse(self) -> None:
        for field, value, reason in (
            ("status", "pending", "original_not_terminal"),
            ("status", "reconciliation_required", "original_not_terminal"),
            ("instance", "prod", "foreign_original"),
            ("product", "foreign", "foreign_original"),
            ("lease_expires_at", "unknown", "original_lease_unknown"),
        ):
            with self.subTest(field=field):
                original = getattr(self.operation, field)
                setattr(self.operation, field, value)
                self.assertIn(reason, self.refusal())
                setattr(self.operation, field, original)
        reasons = candidate_record_refusals(
            self.candidate,
            self.operation,
            product="odoo",
            context="site",
            receiver_operation_id="failed-original",
            selected_targets=("failed-copy",),
        )
        self.assertIn("receiver_candidate", reasons)
        self.assertIn("selected_target", reasons)


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
        self.inspect = self.enterContext(
            patch.object(
                retention, "inspect_candidate_resources", return_value=(self.observation, "")
            )
        )
        self.enterContext(patch.object(retention, "_inventory_clear", return_value=True))
        self.host = self.enterContext(
            patch.object(retention, "_host_result", return_value=("eligible", "qualified"))
        )

    def plan(self) -> Any:
        return self.retention.build_native_testing_sweep(
            self.store, self.root, self.request, "trace"
        )[0]

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

    def test_failed_copy_is_not_held_by_its_own_stored_records(self) -> None:
        self.with_application(prepared=False)
        candidate = self.selected()
        self.assertEqual(candidate.status, "eligible")
        self.assertEqual(candidate.references, ())
        self.assertNotIn("references", candidate.model_dump(mode="json"))

    def test_another_record_naming_the_copy_holds_it_and_names_its_owner(self) -> None:
        self.with_application(prepared=False)
        reviewed = self.selected()
        later = TestingCandidateAllocation(
            namespace=testing_candidate_namespace(
                self.candidate.product, self.candidate.context, "later-original"
            ),
            product=self.candidate.product,
            context=self.candidate.context,
            operation_id="later-original",
            intent_sha256="a" * 64,
            serving_target_id=self.candidate.compose_id,
            server_id="host",
            created_at=datetime.now(timezone.utc),
        )
        self.store_candidate(later)
        candidate = self.selected()
        self.assertNotEqual(candidate.ownership_sha256, reviewed.ownership_sha256)
        self.assertEqual(candidate.status, "held")
        self.assertIn("needed_copy_or_evidence_reference", candidate.reasons)
        self.assertEqual(
            [(row.kind, row.record_id) for row in candidate.references],
            [("testing_candidate", later.namespace)],
        )

    def test_completed_copy_releases_only_with_its_preserved_source(self) -> None:
        from unittest.mock import patch

        self.with_application(prepared=True)
        self.observation["volumes"][0].update(empty=False, reason="container_reference")
        self.observation["project"] = self.candidate.namespace
        lifecycle = {"payload": {"container_ids": ["1" * 64], "container_snapshot": "s"}}
        custody = self.enterContext(
            patch.object(self.retention, "candidate_lifecycle_custody", return_value=lifecycle)
        )
        self.assertEqual(self.selected().status, "eligible")
        custody.assert_called_once()
        with patch.object(self.retention, "_source_preserved", return_value=False):
            held = self.selected()
        self.assertEqual(held.status, "held")
        self.assertEqual(held.reasons, ("completed_copy_requires_disposition",))
        self.request = self.request.model_copy(
            update={"receiver_operation_id": self.candidate.operation_id}
        )
        receiver = self.selected()
        self.assertEqual(
            receiver.reasons, ("receiver_candidate", "completed_copy_requires_disposition")
        )
        custody.assert_called_once()

    def test_completed_copy_lifecycle_is_bound_to_its_serving_web(self) -> None:
        from unittest.mock import patch
        from control_plane.dokploy import source, testing_retained_copy

        proof = {
            "container_ids": [],
            "container_snapshot": "s",
            "resource_binding": "r",
            "physical_binding": "p",
        }
        sent: list[dict[str, Any]] = []

        def operation(**kwargs: Any) -> dict[str, Any]:
            sent.append(kwargs["payload"])
            return proof

        self.enterContext(
            patch.object(testing_retained_copy, "retained_copy_operation", side_effect=operation)
        )
        self.enterContext(patch.object(source, "read_dokploy_config", return_value=("h", "t")))
        for prepared in (True, False):
            self.with_application(prepared=prepared)
            self.retention.candidate_lifecycle_custody(
                self.root, self.store, self.candidate, self.candidate.namespace
            )
            self.assertIs(sent[-1]["failed_candidate"].get("completed", False), prepared)
        configured = self.candidate.application.model_copy(  # type: ignore[union-attr]
            update={"phase": "configured", "abandonment_evidence": ""}
        )
        with self.assertRaisesRegex(ValueError, "terminal"):
            self.retention.candidate_lifecycle_custody(
                self.root,
                self.store,
                self.candidate.model_copy(update={"application": configured}),
                self.candidate.namespace,
            )

    def test_original_records_survive_and_a_selected_target_loses_eligibility(self) -> None:
        from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord

        before = self.store.read_testing_candidate(self.candidate.namespace)
        self.assertEqual(self.plan().candidates[0].status, "eligible")
        self.assertEqual(self.store.read_testing_candidate(self.candidate.namespace), before)
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=self.request.context,
                instance="testing",
                target_id=self.candidate.compose_id,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        held = self.plan().candidates[0]
        self.assertEqual(held.status, "held")
        self.assertIn("selected_target", held.reasons)

    def test_foreign_selection_refuses_without_reading_a_provider(self) -> None:
        self.request = self.request.model_copy(update={"candidates": ("lp-testing-" + "f" * 32,)})
        with self.assertRaisesRegex(ValueError, "foreign or unknown"):
            self.plan()
        self.inspect.assert_not_called()

    def test_physical_sources_and_unqualified_nonempty_copy_are_never_removed(self) -> None:
        self.observation["volumes"][0].update(empty=False, reason="source_inputs_preserved")
        self.assertEqual(self.plan().candidates[0].resources[0].reason, "source_inputs_preserved")
        self.host.assert_not_called()

    def test_nonempty_unrecorded_copy_is_held(self) -> None:
        self.observation["volumes"][0]["empty"] = False
        self.assertEqual(
            self.plan().candidates[0].resources[0].reason, "only_needed_copy_unqualified"
        )
        self.host.assert_not_called()

    def test_free_space_changes_do_not_invalidate_reviewed_resource_ownership(self) -> None:
        self.request = self.request.model_copy(update={"candidates": (self.candidate.namespace,)})
        self.observation["volumes"][0]["free_bytes"] = 100
        before = self.plan()
        self.observation["volumes"][0]["free_bytes"] = 90
        after = self.plan()
        self.assertEqual(before.plan_sha256, after.plan_sha256)
        self.assertNotEqual(before.candidates[0].resources, after.candidates[0].resources)
        self.observation["volumes"][0]["identity"] = "d" * 64
        self.assertNotEqual(after.plan_sha256, self.plan().plan_sha256)

    def test_provider_stop_transition_preserves_custody_but_new_work_refuses(self) -> None:
        payload = {"composeId": self.candidate.compose_id, "composeStatus": "error"}
        before = self.retention.candidate_provider_snapshot(payload)
        payload["composeStatus"] = "idle"
        self.assertEqual(before, self.retention.candidate_provider_snapshot(payload))
        payload["composeId"] = "foreign"
        self.assertNotEqual(before, self.retention.candidate_provider_snapshot(payload))
        for status in ("running", "unknown", ""):
            payload["composeStatus"] = status
            with self.assertRaisesRegex(ValueError, "active or unknown"):
                self.retention.candidate_provider_snapshot(payload)

    def test_provider_read_error_holds_one_candidate_without_a_mutation_claim(self) -> None:
        from control_plane.dokploy.api import DokployRequestFailed

        self.inspect.side_effect = DokployRequestFailed(
            method="GET", path="/api/compose.one", detail="missing", status_code=404
        )
        candidate = self.plan().candidates[0]
        self.assertEqual(candidate.status, "held")
        self.host.assert_not_called()
