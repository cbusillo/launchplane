"""Recover public verification using real records and fixed fake provider reads."""

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch
import unittest

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    PostDeployUpdateEvidence,
    HealthcheckEvidence,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyResult,
)
from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold
from control_plane.odoo_testing_restore import generation_binding
from control_plane.storage.postgres import DbOnlyMutationRequest
from control_plane.testing_verification import (
    COPY_MARKERS,
    ROUTE,
    TestingVerification,
    TestingVerificationRequest,
)
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.workflows.odoo_verification import OdooVerificationResult
from tests.test_testing_public_ingress import TestingPublicIngressScenario
from tests.test_odoo_stable_target_replacement import _artifact_manifest
from tests.support.http import request
from tests.support.auth import scoped_agent_transport


class TestingVerificationScenario(TestingPublicIngressScenario):
    def setUp(self) -> None:
        super().setUp()
        moment = datetime.now(timezone.utc) - timedelta(minutes=10)
        self.started = moment.isoformat()
        self.finished = (moment + timedelta(minutes=1)).isoformat()
        self.store.write_odoo_stable_target_replacement_operation_record(
            self.fixture.operation.model_copy(
                update={"status": "fail", "phase": "failed", "finished_at": self.finished}
            )
        )
        self.artifact = _artifact_manifest(
            artifact_id="restored-build",
            source_commit="a" * 40,
            digest="sha256:" + "b" * 64,
            image_repository=self.profile.image.repository,
        )
        self.store.write_artifact_manifest(self.artifact)
        self.identity = RuntimeIdentity(
            product=self.profile.product,
            context=self.fixture.context,
            instance="testing",
            deployment_record_id="restored-deployment",
            artifact_id=self.artifact.artifact_id,
            source_git_ref=self.artifact.source_commit,
            image_reference=f"{self.artifact.image.repository}@{self.artifact.image.digest}",
        )
        self.deployment = DeploymentRecord(
            record_id=self.identity.deployment_record_id,
            context=self.fixture.context,
            instance="testing",
            source_git_ref=self.identity.source_git_ref,
            artifact_identity=ArtifactIdentityReference(artifact_id=self.artifact.artifact_id),
            resolved_target=ResolvedTargetEvidence(
                target_type="compose",
                target_id=self.resources.target_id,
                target_name=self.resources.namespace,
            ),
            runtime_identity=self.identity,
            deploy=DeploymentEvidence(
                deploy_mode="dokploy-compose-api",
                target_name=self.resources.namespace,
                target_type="compose",
                status="fail",
                started_at=self.started,
                finished_at=self.finished,
            ),
            post_deploy_update=PostDeployUpdateEvidence(
                attempted=True,
                status="pass",
                evidence={"post_deploy_readback_" + key: "true" for key in COPY_MARKERS},
            ),
            destination_health=HealthcheckEvidence(status="fail"),
        )
        self.store.write_deployment_record(self.deployment)
        self.store.write_environment_inventory(
            build_environment_inventory(
                deployment_record=self.deployment.model_copy(
                    update={
                        "artifact_identity": ArtifactIdentityReference(artifact_id="previous-build")
                    }
                ),
                updated_at=self.finished,
            )
        )
        operation = self.fixture.operation.model_copy(
            update={
                "operation_id": "terminal-installed-deploy",
                "status": "fail",
                "phase": "failed",
                "created_at": self.started,
                "started_at": self.started,
                "finished_at": self.finished,
                "deployment_record_id": self.deployment.record_id,
                "request": self.fixture.operation.request.model_copy(
                    update={
                        "artifact_id": self.artifact.artifact_id,
                        "source_git_ref": self.artifact.source_commit,
                    }
                ),
                "result": OdooStableTargetReplacementApplyResult(
                    product=self.profile.product,
                    context=self.fixture.context,
                    instance="testing",
                    strategy="recreate-in-place",
                    deploy_status="fail",
                    post_deploy_status="pass",
                    deployment_record_id=self.deployment.record_id,
                    target_id=self.resources.target_id,
                ),
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(operation)
        self.terminal = operation
        self.verification_request = TestingVerificationRequest(
            context=self.fixture.context,
            operation_id=self.fixture.operation.operation_id,
            deployment_record_id=self.deployment.record_id,
            reason="Complete public verification after repaired TLS",
        )
        self.enterContext(
            patch(
                "control_plane.testing_verification.source.read_dokploy_config",
                return_value=("provider.invalid", "fixture-token"),
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_verification.api.fetch_dokploy_target_payload",
                return_value={
                    "composeId": self.resources.target_id,
                    "serverId": "fixture-server",
                    "appName": self.resources.namespace,
                    "env": "ODOO_DB_NAME=fixture",
                },
            )
        )
        self.receiver = self.enterContext(
            patch(
                "control_plane.testing_verification.inspect_native_testing_receiver",
                side_effect=lambda **kw: kw["receive"]({"type": "complete", "copy_verified": True}),
            )
        )
        self.readiness = self.enterContext(
            patch(
                "control_plane.testing_verification.verify_odoo_stable_readiness",
                return_value=OdooVerificationResult(
                    health_status="pass", canonical_status="pass", logo_status="pass"
                ),
            )
        )
        self.runtime = self.enterContext(
            patch(
                "control_plane.testing_verification._verify_required_runtime_identity_evidence",
                return_value=HealthcheckEvidence(
                    verified=True,
                    urls=(self.request.domain_host,),
                    timeout_seconds=30,
                    status="pass",
                    runtime_identity_status="match",
                    observed_runtime_identity=self.identity,
                ),
            )
        )

    def verification(self) -> TestingVerification:
        return TestingVerification(
            self.fixture.root, self.store, self.profile.product, self.verification_request
        )

    def review_verification(self) -> None:
        digest = self.verification().plan()["plan_sha256"]
        self.verification_request = self.verification_request.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": digest}
        )

    def publish_verification(self, *, checkpoint: Any = lambda: None) -> Any:
        verification = self.verification()
        expected = verification.snapshot()
        completed = verification.verify()
        return self.store.publish_testing_verification(
            verification=verification,
            expected=expected,
            completed=completed,
            mutation=DbOnlyMutationRequest(
                scope="testing-fixture",
                route_path=ROUTE,
                idempotency_key="original-verification-key",
                request_fingerprint=canonical_json_sha256(
                    self.verification_request.model_dump(mode="json")
                ),
                lease_owner="fixture",
                response_status_code=202,
                response_trace_id="fixture",
                response_payload={
                    "result": verification.receipt(
                        self.verification_request.reviewed_plan_sha256, applied=True
                    )
                },
            ),
            checkpoint=checkpoint,
        )


class TestingVerificationTests(TestingVerificationScenario):
    def test_publication_completes_exact_generation_and_preserves_original_failure_and_operations(
        self,
    ) -> None:
        before = self.store.read_fresh_odoo_testing_generation(
            self.profile.product, self.fixture.context
        )
        self.review_verification()
        status, _ = self.publish_verification()
        self.assertEqual(status, "written")
        generation = self.store.read_fresh_odoo_testing_generation(
            self.profile.product, self.fixture.context
        )
        assert generation is not None and before is not None
        self.assertEqual(generation_binding(generation), generation_binding(before))
        self.assertTrue(generation.completed_at)
        published = self.store.read_deployment_record(self.deployment.record_id)
        inventory = self.store.read_environment_inventory(
            context_name=self.fixture.context, instance_name="testing"
        )
        self.assertEqual(inventory.runtime_identity, published.runtime_identity)
        self.assertEqual(inventory.artifact_identity, self.deployment.artifact_identity)
        self.assertEqual(inventory.destination_health.runtime_identity_status, "match")
        archived = self.store.read_deployment_record(
            published.runtime_source["testing_verification_previous_record_id"]
        )
        self.assertEqual(
            archived.model_copy(update={"record_id": self.deployment.record_id}), self.deployment
        )
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(
                self.terminal.operation_id
            ),
            self.terminal,
        )
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.fixture.context, instance_name="prod"
            ),
            self.production_before,
        )
        release_tuple = self.store.read_release_tuple_record(
            context_name=self.fixture.context, channel_name="testing"
        )
        self.assertEqual(release_tuple.deployment_record_id, published.record_id)
        self.assertEqual(release_tuple.artifact_id, self.artifact.artifact_id)
        self.assertTrue(self.verification().readback()["public_health_verified"])
        self.assertEqual(
            self.store.read_native_testing_copy_capture(generation_binding(before)),
            self.copy_witness,
        )

    def test_wrong_artifact_copy_evidence_or_active_terminal_operation_refuses(self) -> None:
        for change in ("artifact", "copy", "operation"):
            with self.subTest(change=change):
                if change == "artifact":
                    self.store.write_deployment_record(
                        self.deployment.model_copy(
                            update={
                                "runtime_identity": self.identity.model_copy(
                                    update={"artifact_id": "wrong-build"}
                                )
                            }
                        )
                    )
                elif change == "copy":
                    self.store.write_deployment_record(
                        self.deployment.model_copy(
                            update={
                                "post_deploy_update": self.deployment.post_deploy_update.model_copy(
                                    update={"evidence": {}}
                                )
                            }
                        )
                    )
                else:
                    self.store.write_odoo_stable_target_replacement_operation_record(
                        self.terminal.model_copy(
                            update={"status": "running", "phase": "running", "finished_at": ""}
                        )
                    )
                with self.assertRaises(ValueError):
                    self.verification().plan()
                self.store.write_deployment_record(self.deployment)

    def test_hold_refuses(self) -> None:
        target = self.store.read_dokploy_target_record(
            context_name=self.fixture.context, instance_name="testing"
        )
        held = target.model_copy(
            update={
                "policies": target.policies.model_copy(
                    update={
                        "staff_testing_hold": DokployTargetStaffTestingHold(
                            reason="Staff testing", recorded_at=self.finished
                        )
                    }
                )
            }
        )
        self.store.write_dokploy_target_record(held)
        with self.assertRaises(ValueError):
            self.verification().plan()

    def test_reassigned_target_refuses(self) -> None:
        target = self.store.read_dokploy_target_id_record(
            context_name=self.fixture.context, instance_name="testing"
        )
        self.store.write_dokploy_target_id_record(
            target.model_copy(update={"target_id": "another-target"})
        )
        with self.assertRaises(ValueError):
            self.verification().plan()

    def test_authority_change_between_review_and_commit_does_not_publish(self) -> None:
        self.review_verification()
        profile = self.store.read_product_profile_record(self.profile.product)
        self.store.write_product_profile_record(
            profile.model_copy(update={"display_name": "Changed"})
        )
        with self.assertRaises(ValueError):
            self.publish_verification()
        self.assertEqual(
            self.store.read_deployment_record(self.deployment.record_id), self.deployment
        )

    def test_missing_live_witness_public_health_or_runtime_match_never_publishes(self) -> None:
        self.review_verification()
        for failure in ("copy", "public", "identity"):
            with self.subTest(failure=failure):
                with (
                    patch(
                        "control_plane.testing_verification.inspect_native_testing_receiver",
                        side_effect=lambda **kw: kw["receive"](
                            {"type": "complete", "copy_verified": failure != "copy"}
                        ),
                    ),
                    patch(
                        "control_plane.testing_verification.verify_odoo_stable_readiness",
                        return_value=OdooVerificationResult(
                            health_status="fail" if failure == "public" else "pass",
                            canonical_status="pass",
                            logo_status="pass",
                        ),
                    ),
                    patch(
                        "control_plane.testing_verification._verify_required_runtime_identity_evidence",
                        return_value=HealthcheckEvidence(
                            status="pass",
                            runtime_identity_status="mismatch"
                            if failure == "identity"
                            else "match",
                        ),
                    ),
                ):
                    with self.assertRaises(ValueError):
                        self.publish_verification()
                self.assertEqual(
                    self.store.read_deployment_record(self.deployment.record_id), self.deployment
                )

    def test_commit_failure_rolls_back_generation_inventory_archive_and_receipt(self) -> None:
        self.review_verification()
        before = self.verification().snapshot()

        def lost() -> None:
            raise RuntimeError("fixture lost executor")

        with self.assertRaises(RuntimeError):
            self.publish_verification(checkpoint=lost)
        self.assertEqual(self.verification().snapshot(), before)
        status, _ = self.publish_verification()
        self.assertEqual(status, "written")


class TestingVerificationHttpTests(TestingVerificationScenario, unittest.IsolatedAsyncioTestCase):
    async def test_lost_executor_connection_keeps_bounded_refusal_code(self) -> None:
        from sqlalchemy.exc import SQLAlchemyError

        def lost() -> None:
            raise SQLAlchemyError("fixture executor database transport")

        @contextmanager
        def executor(_: str) -> Iterator[Any]:
            yield lost

        with patch.object(self.store, "testing_replacement_execution_lock", side_effect=executor):
            result = await request(
                self.fixture.app,
                "POST",
                ROUTE.format(product=self.profile.product),
                headers={"Authorization": "Bearer local-operator-token"},
                payload=self.verification_request.model_dump(mode="json"),
            )
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()["error"]["code"], "testing_verification_unverified")
        self.assertEqual(
            self.store.read_deployment_record(self.deployment.record_id), self.deployment
        )

    async def test_provider_denial_preserves_authorization_refusal(self) -> None:
        from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied

        with patch(
            "control_plane.testing_verification.inspect_native_testing_receiver",
            side_effect=NativeSourceAuthorizationDenied("fixture denied"),
        ):
            result = await request(
                self.fixture.app,
                "POST",
                ROUTE.format(product=self.profile.product),
                headers={"Authorization": "Bearer local-operator-token"},
                payload=self.verification_request.model_dump(mode="json"),
            )
        self.assertEqual(result.status_code, 403, result.text)
        self.assertEqual(result.json()["error"]["code"], "authorization_denied")
        self.assertEqual(
            self.store.read_deployment_record(self.deployment.record_id), self.deployment
        )

    async def test_active_original_executor_reports_in_progress_before_reads(self) -> None:
        @contextmanager
        def busy(_: str) -> Iterator[None]:
            yield None

        with patch.object(self.store, "testing_replacement_execution_lock", side_effect=busy):
            result = await request(
                self.fixture.app,
                "POST",
                ROUTE.format(product=self.profile.product),
                headers={"Authorization": "Bearer local-operator-token"},
                payload=self.verification_request.model_dump(mode="json"),
            )
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()["error"]["code"], "testing_verification_in_progress")
        self.receiver.assert_not_called()

    async def test_lost_postgres_lane_reservation_refuses_publication(self) -> None:
        if self.store.database_dialect_name != "postgresql":
            self.skipTest("PostgreSQL is required for connection-loss fencing")
        from sqlalchemy import text
        from control_plane.storage import postgres

        original = postgres._try_odoo_synchronous_lane_lock
        backend: list[int] = []

        def reserve(session: Any, **kwargs: Any) -> bool:
            backend.append(int(session.scalar(text("SELECT pg_backend_pid()"))))
            return original(session, **kwargs)

        def lose_reservation(**kwargs: Any) -> None:
            with self.store._engine.connect() as observer:
                observer.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": backend[0]})
            kwargs["receive"]({"type": "complete", "copy_verified": True})

        self.review_verification()
        before = self.verification().snapshot()
        with (
            patch(
                "control_plane.storage.postgres._try_odoo_synchronous_lane_lock",
                side_effect=reserve,
            ),
            patch(
                "control_plane.testing_verification.inspect_native_testing_receiver",
                side_effect=lose_reservation,
            ),
        ):
            result = await request(
                self.fixture.app,
                "POST",
                ROUTE.format(product=self.profile.product),
                headers={
                    "Authorization": "Bearer local-operator-token",
                    "Idempotency-Key": "original-verification-key",
                },
                payload=self.verification_request.model_dump(mode="json"),
            )
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(self.verification().snapshot(), before)

    async def test_postgres_apply_replay_and_independent_readback_preserve_original_operation(
        self,
    ) -> None:
        if self.store.database_dialect_name != "postgresql":
            self.skipTest("PostgreSQL is required for live publication fencing")
        payload = self.verification_request.model_dump(mode="json")

        async def post(body: dict[str, object]) -> Any:
            return await request(
                self.fixture.app,
                "POST",
                ROUTE.format(product=self.profile.product),
                headers={
                    "Authorization": "Bearer local-operator-token",
                    "Idempotency-Key": "original-verification-key",
                },
                payload=body,
            )

        plan = await post(payload)
        self.assertEqual(plan.status_code, 202, plan.text)
        applied_body = {
            **payload,
            "mode": "apply",
            "reviewed_plan_sha256": plan.json()["result"]["plan_sha256"],
        }
        applied = await post(applied_body)
        self.assertEqual(applied.status_code, 202, applied.text)
        calls = self.receiver.call_count
        replay = await post(applied_body)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["result"], applied.json()["result"])
        self.assertEqual(self.receiver.call_count, calls)
        readback = await post({**applied_body, "mode": "readback"})
        self.assertEqual(readback.status_code, 202, readback.text)
        self.assertTrue(readback.json()["result"]["public_health_verified"])
        self.assertGreater(self.receiver.call_count, calls)
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(
                self.terminal.operation_id
            ),
            self.terminal,
        )

    async def test_only_director_testing_request_and_no_active_fence_reach_reads(self) -> None:
        async def post(payload: dict[str, object]) -> Any:
            return await request(
                self.fixture.app,
                "POST",
                ROUTE.format(product=self.profile.product),
                headers={"Authorization": "Bearer local-operator-token"},
                payload=payload,
            )

        payload = self.verification_request.model_dump(mode="json")
        response = await post(payload)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(response.json()["result"]["public_health_verified"])
        with scoped_agent_transport():
            denied = await post(payload)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual((await post({**payload, "instance": "prod"})).status_code, 400)
        self.store.write_odoo_stable_target_replacement_operation_record(
            self.terminal.model_copy(
                update={
                    "operation_id": "other-active",
                    "deployment_record_id": "another-pending-deployment",
                    "status": "pending",
                    "phase": "created",
                    "finished_at": "",
                }
            )
        )
        calls = self.receiver.call_count
        blocked = await post(payload)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(self.receiver.call_count, calls)
