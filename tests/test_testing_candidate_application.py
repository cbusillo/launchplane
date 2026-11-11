"""A private candidate uses the current build and only testing-local passwords."""

import os
import hashlib
import base64
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import Mock
import unittest
from unittest.mock import patch

from control_plane.copy_readback_diagnostics import ReadbackPredicate
from control_plane import secrets
from control_plane import testing_candidate_application as application
from control_plane.dokploy import api, source as provider_source, post_deploy
from control_plane.contracts.testing_candidate_application import TestingCandidatePrepared
from control_plane.provider_operations import ProviderOperationLease
from control_plane.contracts.artifact_identity import (
    ArtifactBuildFlags,
    ArtifactIdentityManifest,
    ArtifactImageReference,
)
from control_plane.contracts.odoo_stable_target_replacement import LAUNCHPLANE_REQUIRED_ODOO_MODULES
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.product_profile_record import ProductRuntimeConfigRequirement
from control_plane.contracts.testing_candidate import TestingCandidateAllocation
from control_plane.contracts.testing_candidate_application import TestingCandidateApplication
from control_plane.contracts.odoo_testing_source import (
    NativeOdooTestingSource,
    NativeOdooSourceAuthority,
    NativeTestingCopyCapture,
    MigratedOdooTestingSource,
)
from control_plane import testing_candidate_runtime as compiler
from control_plane.odoo_testing_restore import FRESH_TESTING_LOCAL_CREDENTIAL_KEYS
from control_plane.testing_local_credentials import generation_bundle
from control_plane.testing_candidate_runtime import (
    CandidateSource,
    TestingCandidateRuntimeInputs,
    candidate_application_intent,
    candidate_runtime_inputs,
)
from tests import test_testing_candidate_allocation as allocation_tests
from tests.support.stores import _write_runtime_key_safety_policy
from tests.test_dokploy_compose_environment import (
    VALUES,
    ROOT,
    consumer_commands_available,
    consume_compose_environment,
)


class CandidateApplicationTests(unittest.TestCase):
    candidate_build = False

    def setUp(self) -> None:
        self.fixture = allocation_tests.TestingCandidateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store, self.root = self.fixture.store, self.fixture.root
        self.enterContext(
            patch.dict(
                os.environ, {secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "candidate-fixture-key"}
            )
        )
        _write_runtime_key_safety_policy(database_url=self.store.database_url)
        self.profile = self.store.read_product_profile_record(self.fixture.product)
        bundle = generation_bundle(
            self.store,
            self.profile,
            self.fixture.context,
            actor="fixture",
            generation="original",
            only_keys=FRESH_TESTING_LOCAL_CREDENTIAL_KEYS,
        )
        self.store.write_product_authority_bundle(bundle)
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context=self.fixture.context,
                instance="testing",
                env={
                    "ODOO_DB_NAME": "example",
                    "ODOO_DB_USER": "odoo",
                    "ODOO_ADMIN_LOGIN": "tester",
                    "ODOO_UPSTREAM_HOST": "upstream.invalid",
                    "ODOO_UPSTREAM_USER": "reader",
                    "ODOO_UPSTREAM_DB_USER": "postgres",
                    "ODOO_UPSTREAM_DB_NAME": "source",
                    "ODOO_UPSTREAM_FILESTORE_PATH": "/source/filestore/source",
                },
                updated_at=self.profile.updated_at,
            )
        )
        self.artifact = ArtifactIdentityManifest(
            artifact_id="candidate-build",
            source_commit="e" * 40,
            enterprise_base_digest="sha256:" + "b" * 64,
            odoo_install_modules=LAUNCHPLANE_REQUIRED_ODOO_MODULES,
            image=ArtifactImageReference(
                repository=self.profile.image.repository, digest="sha256:" + "a" * 64
            ),
        )
        if self.candidate_build:
            from control_plane.contracts.artifact_identity import ArtifactSourceBuild

            self.artifact = self.artifact.model_copy(
                update={
                    "artifact_id": f"artifact-{self.fixture.context}-run-7-1",
                    "source_build": ArtifactSourceBuild(
                        repository=self.profile.repository,
                        repository_id="101",
                        workflow_path=".github/workflows/build.yml",
                        event="pull_request",
                        purpose="preview",
                        pull_request_number=9,
                        run_id=7,
                        run_attempt=1,
                        github_artifact_id=70,
                        manifest_artifact_id="producer-candidate",
                    ),
                }
            )
        else:
            self.store.write_artifact_manifest(self.artifact)
        self.fixture.before = self.fixture.authority()
        self.credentials = self.store.list_secret_records(
            context_name=self.fixture.context, instance_name="testing"
        )

    def test_candidate_admission_refuses_provenance_drift_and_runtime_uses_admitted_manifest(
        self,
    ) -> None:
        from control_plane import testing_rebuild_service as worker
        from control_plane.build_provenance import BuildProvenanceError, VerifiedBuildArtifact
        from control_plane.contracts.odoo_stable_target_replacement_operation import (
            OdooStableTargetReplacementOperationRecord,
        )
        from control_plane.contracts.odoo_stable_target_replacement import (
            OdooStableTargetReplacementApplyRequest,
        )

        self.doCleanups()
        self.candidate_build = True
        self.setUp()
        self.execution("migrated")
        allocation = self.allocation
        migrated = self.compiled.plan.native_source
        assert isinstance(migrated, MigratedOdooTestingSource)
        operation = OdooStableTargetReplacementOperationRecord(
            operation_id=allocation.operation_id,
            product=allocation.product,
            context=allocation.context,
            instance="testing",
            idempotency_key="candidate-original",
            request_fingerprint="candidate-request",
            request=OdooStableTargetReplacementApplyRequest(
                product=allocation.product,
                instance="testing",
                strategy="replace-and-cutover",
                data_source_mode="migrated_restore",
                migrated_source=migrated,
                artifact_id=self.artifact.artifact_id,
                source_git_ref=self.artifact.source_commit,
            ),
            testing_candidate_manifest=self.artifact,
            created_at=allocation.created_at.isoformat(),
            updated_at=allocation.created_at.isoformat(),
        )
        execution = worker.NativeTestingRebuildExecution(
            Mock(
                root=self.root,
                store=self.store,
                operation=operation,
                intent=Mock(profile=self.profile),
            ),
            Mock(),
            Mock(),
        )
        with (
            patch(
                "control_plane.product_reconcile.verified_testing_rebuild_artifact",
                return_value=VerifiedBuildArtifact(manifest=self.artifact),
            ) as verify,
            patch.object(
                self.store, "read_artifact_manifest", side_effect=FileNotFoundError
            ) as release_read,
        ):
            execution.require_candidate_build()
            inputs = execution.current_candidate_inputs(allocation)
            self.assertEqual(inputs.plan.artifact, operation.testing_candidate_manifest)
            self.assertEqual(inputs.plan.native_source, migrated)
            self.assertEqual(
                inputs.plan.runtime_identity.source_git_ref, operation.request.source_git_ref
            )
            release_read.assert_not_called()
            verify.side_effect = BuildProvenanceError(
                "Changed PR head", code="pull_request_head_mismatch"
            )
            with self.assertRaises(worker.NativeTestingRebuildAdmissionChanged):
                execution.require_candidate_build()
            verify.side_effect = None
            changed = self.artifact.model_copy(
                update={
                    "image": self.artifact.image.model_copy(update={"digest": "sha256:" + "f" * 64})
                }
            )
            verify.return_value = VerifiedBuildArtifact(manifest=changed)
            with self.assertRaises(worker.NativeTestingRebuildAdmissionChanged):
                execution.require_candidate_build()
            with self.assertRaises(ValueError):
                execution.build_plan(
                    allocation, inputs.plan.model_copy(update={"artifact": changed})
                )
        for changes in (
            {
                "instance": "prod",
                "request": operation.request.model_copy(update={"instance": "prod"}),
            },
            {"request": operation.request.model_copy(update={"source_git_ref": "f" * 40})},
            {
                "request": operation.request.model_copy(
                    update={
                        "data_source_mode": "empty",
                        "allow_empty_data": True,
                        "migrated_source": None,
                    }
                )
            },
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                OdooStableTargetReplacementOperationRecord.model_validate(
                    {**operation.model_dump(), **changes}
                )

    def allocate(
        self,
        source: CandidateSource = "empty",
        migrated_source: MigratedOdooTestingSource | None = None,
    ) -> TestingCandidateAllocation:
        self.fixture.allocation = self.fixture.allocation.model_copy(
            update={
                "intent_sha256": candidate_application_intent(
                    product=self.fixture.product,
                    context=self.fixture.context,
                    artifact_id=self.artifact.artifact_id,
                    source=source,
                    migrated_source=migrated_source,
                )
            }
        )
        return self.fixture.prepare()

    def inputs(
        self,
        allocation: TestingCandidateAllocation,
        source: CandidateSource = "empty",
        migrated_source: MigratedOdooTestingSource | None = None,
    ) -> TestingCandidateRuntimeInputs:
        return candidate_runtime_inputs(
            root=self.root,
            store=self.store,
            allocation=allocation,
            artifact_id=self.artifact.artifact_id,
            source=source,
            migrated_source=migrated_source,
            admitted_artifact=self.artifact if self.candidate_build else None,
        )

    def test_migrated_pair_binds_candidate_image_and_receipt_before_restore(self) -> None:
        from tests.test_migrated_testing_source import MigratedTestingRequestTests
        from control_plane.odoo_testing_restore import generation_binding

        original = MigratedTestingRequestTests().request().migrated_source
        assert original is not None
        migrated = original.model_copy(
            update={
                "product": self.fixture.product,
                "context": self.fixture.context,
                "target_image": self.artifact.image.digest,
            }
        )
        from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget

        budget = OdooTestingDrillStorageBudget(
            database_bytes=1024, filestore_bytes=1, staging_bytes=1024, reserve_bytes=1024
        )
        self.profile = self.profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(update={"odoo_testing_drill_storage": budget})
                    if lane.instance == "testing"
                    else lane
                    for lane in self.profile.lanes
                )
            }
        )
        self.store.write_product_profile_record(self.profile)
        self.fixture.before = self.fixture.authority()
        allocation = self.allocate("migrated", migrated)
        inputs = self.inputs(allocation, "migrated", migrated)
        with patch.object(self.store, "read_artifact_manifest", side_effect=FileNotFoundError):
            admitted = candidate_runtime_inputs(
                root=self.root,
                store=self.store,
                allocation=allocation,
                artifact_id=self.artifact.artifact_id,
                source="migrated",
                migrated_source=migrated,
                admitted_artifact=self.artifact,
            )
        self.assertEqual(admitted.plan, inputs.plan)
        self.assertEqual(admitted.compose_file, inputs.compose_file)
        app = TestingCandidateApplication(plan=inputs.plan)
        recorded = allocation.model_copy(update={"application": app})
        self.store.compare_and_write_testing_candidate(allocation, recorded)
        generation = inputs.plan.generation
        assert generation is not None and generation.drill_storage_budget is not None
        self.assertEqual(
            generation.drill_storage_budget.filestore_bytes, migrated.filestore_expanded_bytes
        )
        self.assertEqual(generation.drill_storage_budget.reserve_bytes, budget.reserve_bytes)
        capture = NativeTestingCopyCapture(
            product=migrated.product,
            context=migrated.context,
            generation_binding_sha256=generation_binding(generation),
            source_binding_sha256=migrated.binding_sha256,
            capture_id="f" * 32,
            database=migrated.database,
            filestore=migrated.filestore,
            captured_at=datetime.now(UTC).isoformat(),
            source_observation=migrated,
        )
        self.store.write_native_testing_copy_capture(
            capture, candidate_namespace=recorded.namespace
        )
        self.assertEqual(
            self.store.read_native_testing_copy_capture(generation_binding(generation)), capture
        )
        changed = capture.model_copy(
            update={
                "database": capture.database.model_copy(update={"size": capture.database.size + 1})
            }
        )
        with self.assertRaises(ValueError):
            self.store.write_native_testing_copy_capture(
                changed, candidate_namespace=recorded.namespace
            )
        with self.assertRaises(compiler.CandidateRuntimeInputsChanged):
            self.inputs(
                recorded,
                "migrated",
                migrated.model_copy(update={"target_image": "sha256:" + "c" * 64}),
            )
        self.fixture.assert_serving_unchanged()

    def test_private_candidate_uses_the_managed_application_port(self) -> None:
        self.profile = self.profile.model_copy(update={"runtime_port": 8072})
        self.store.write_product_profile_record(self.profile)
        self.fixture.before = self.fixture.authority()
        inputs = self.inputs(self.allocate())
        self.assertEqual(
            inputs.runtime_environment["ODOO_HTTP_PORT"], str(self.profile.runtime_port)
        )
        self.fixture.assert_serving_unchanged()

    def test_candidate_runtime_version_follows_its_artifact_and_preserves_legacy_inputs(
        self,
    ) -> None:
        allocation = self.allocate("upstream")
        runtime = next(
            record
            for record in self.store.list_runtime_environment_records()
            if (record.scope, record.context, record.instance)
            == ("instance", self.fixture.context, "testing")
        )
        for index, (artifact_version, lane_version, declared_instance) in enumerate(
            (
                ("20.0", "19.0", ""),
                ("19.0", "19.0", ""),
                ("20.0", "20.0", ""),
                ("18.0", "19.0", ""),
                ("", "19.0", ""),
                ("20.0", "", ""),
                ("", "", ""),
                ("20.0", "", "testing"),
                ("20.0", "", "prod"),
                ("", "", "testing"),
            )
        ):
            with self.subTest(
                artifact=artifact_version, lane=lane_version, declared=declared_instance
            ):
                expected_config = self.profile.expected_config
                if declared_instance:
                    expected_config = expected_config.model_copy(
                        update={
                            "runtime_environment_keys": (
                                *expected_config.runtime_environment_keys,
                                ProductRuntimeConfigRequirement(
                                    key="ODOO_VERSION",
                                    context=self.fixture.context,
                                    instance=declared_instance,
                                ),
                            )
                        }
                    )
                self.store.write_product_profile_record(
                    self.profile.model_copy(update={"expected_config": expected_config})
                )
                artifact = self.artifact.model_copy(
                    update={
                        "artifact_id": f"versioned-candidate-{index}",
                        "build_flags": ArtifactBuildFlags(
                            values={"odoo_version": artifact_version}
                        ),
                    }
                )
                self.store.write_artifact_manifest(artifact)
                environment = dict(runtime.env)
                if lane_version:
                    environment["ODOO_VERSION"] = lane_version
                self.store.write_runtime_environment_record(
                    runtime.model_copy(update={"env": environment})
                )
                self.fixture.before = self.fixture.authority()
                inputs = candidate_runtime_inputs(
                    root=self.root,
                    store=self.store,
                    allocation=allocation.model_copy(
                        update={
                            "intent_sha256": candidate_application_intent(
                                product=self.fixture.product,
                                context=self.fixture.context,
                                artifact_id=artifact.artifact_id,
                                source="upstream",
                            )
                        }
                    ),
                    artifact_id=artifact.artifact_id,
                    source="upstream",
                )
                expected_version = lane_version
                if artifact_version and (lane_version or declared_instance == "testing"):
                    expected_version = artifact.build_flags.values["odoo_version"]
                if expected_version:
                    self.assertEqual(
                        inputs.runtime_environment["ODOO_VERSION"],
                        expected_version,
                    )
                else:
                    self.assertNotIn("ODOO_VERSION", inputs.runtime_environment)
                self.assertEqual(inputs.plan.artifact, artifact)
                self.assertEqual(
                    inputs.plan.runtime_identity.source_git_ref, artifact.source_commit
                )
                self.assertEqual(inputs.plan.runtime_identity.artifact_id, artifact.artifact_id)
                self.assertEqual(
                    inputs.runtime_environment["ODOO_DB_NAME"], runtime.env["ODOO_DB_NAME"]
                )
                self.assertEqual(
                    inputs.plan.upstream_source["ODOO_UPSTREAM_HOST"],
                    runtime.env["ODOO_UPSTREAM_HOST"],
                )
                self.assertNotIn("ODOO_UPSTREAM_HOST", inputs.runtime_environment)
                self.fixture.assert_serving_unchanged()

    def test_managed_port_reaches_private_probe_and_recorded_endpoint(self) -> None:
        real_readiness = application.candidate_private_readiness
        self.profile = self.profile.model_copy(update={"runtime_port": 8072})
        self.store.write_product_profile_record(self.profile)
        self.fixture.before = self.fixture.authority()
        self.execution()
        self.provider_state["appName"] = self.allocation.namespace + "-abc123"

        def remote(**kwargs: Any) -> None:
            self.assertEqual(kwargs["payload"]["http_port"], self.profile.runtime_port)
            self.assertEqual(kwargs["payload"]["compose_project"], self.provider_state["appName"])
            kwargs["receive"]({"type": "complete", "private_readiness_verified": True})

        with (
            patch.object(application, "_read_remote", side_effect=remote),
            patch.object(application, "candidate_private_readiness", side_effect=real_readiness),
        ):
            result = self.prepare_application()
        assert result.application is not None and result.application.prepared is not None
        self.assertEqual(
            result.application.prepared.compose_project, self.provider_state["appName"]
        )
        self.assertEqual(
            result.application.prepared.private_endpoint,
            f"http://{result.namespace}:{self.profile.runtime_port}",
        )
        self.fixture.assert_serving_unchanged()

    def test_private_project_must_belong_to_the_original_namespace_before_probe(self) -> None:
        real_readiness = application.candidate_private_readiness
        self.execution()
        allocation = self.prepare_application()
        assert allocation.application is not None
        self.provider_state["appName"] = "foreign-project"
        with patch.object(application, "_read_remote") as remote:
            with self.assertRaises(ValueError):
                real_readiness(
                    host="https://provider.invalid",
                    token="fixture",
                    allocation=allocation,
                    inputs=self.inputs(allocation),
                    deployment_id=allocation.application.deployment_id,
                )
            remote.assert_not_called()

    def test_prepared_project_preserves_legacy_wire_and_rejects_foreign_custody(self) -> None:
        self.execution()
        allocation = self.prepare_application()
        assert allocation.application is not None
        legacy = allocation.application.model_dump(mode="json")
        assert legacy["prepared"] is not None
        legacy["prepared"].pop("compose_project", None)
        self.assertEqual(
            TestingCandidateApplication.model_validate(legacy).model_dump(mode="json"), legacy
        )
        assert legacy["prepared"] is not None
        for project in ("foreign-project", allocation.namespace + "-too-long"):
            with self.subTest(project=project):
                legacy["prepared"]["compose_project"] = project
                with self.assertRaises(ValueError):
                    TestingCandidateApplication.model_validate(legacy)

    def test_prepared_project_cannot_be_replaced_by_a_fresh_readiness_probe(self) -> None:
        real_readiness = application.candidate_private_readiness
        self.execution()
        allocation = self.prepare_application()
        assert allocation.application is not None
        self.provider_state["appName"] = allocation.namespace + "-def456"
        with patch.object(application, "_read_remote") as remote:
            remote.side_effect = lambda **kwargs: kwargs["receive"](
                {"type": "complete", "private_readiness_verified": True}
            )
            with self.assertRaises(application.CandidateObservationUnavailable):
                real_readiness(
                    host="https://provider.invalid",
                    token="fixture",
                    allocation=allocation,
                    inputs=self.inputs(allocation),
                    deployment_id=allocation.application.deployment_id,
                )
            remote.assert_not_called()

    def test_private_probe_distinguishes_returned_failure_from_transport_or_denial(self) -> None:
        from control_plane.dokploy.native_copy_stream import (
            NativeProgramFailed,
            NativeSourceUnavailable,
            NativeSourceAuthorizationDenied,
        )

        real_readiness = application.candidate_private_readiness
        self.execution()
        allocation = self.prepare_application()
        assert allocation.application is not None
        inputs = self.inputs(allocation)
        for error, expected in (
            (
                NativeProgramFailed("fixture failed frame"),
                application.CandidatePrivateReadinessFailed,
            ),
            (NativeSourceUnavailable("fixture lost connection"), NativeSourceUnavailable),
            (
                NativeSourceAuthorizationDenied("fixture provider refused"),
                NativeSourceAuthorizationDenied,
            ),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(application, "_read_remote", side_effect=error),
            ):
                with self.assertRaises(expected) as raised:
                    real_readiness(
                        host="https://provider.invalid",
                        token="fixture",
                        allocation=allocation,
                        inputs=inputs,
                        deployment_id=allocation.application.deployment_id,
                    )
                if isinstance(error, NativeProgramFailed):
                    assert isinstance(raised.exception, application.CandidatePrivateReadinessFailed)
                    self.assertEqual(raised.exception.evidence, error.evidence)

    def test_private_build_preserves_the_serving_address_and_generated_passwords(self) -> None:
        allocation = self.allocate()
        inputs = self.inputs(allocation)
        lane = next(lane for lane in self.profile.lanes if lane.instance == "testing")
        self.assertEqual(inputs.plan.base_url, lane.base_url.rstrip("/"))
        self.assertEqual(inputs.plan.runtime_identity.artifact_id, self.artifact.artifact_id)
        self.assertEqual(
            inputs.runtime_environment["DOCKER_IMAGE_REFERENCE"], inputs.plan.image_reference
        )
        self.assertTrue(
            all(inputs.runtime_environment.get(key) for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS)
        )
        self.assertFalse(inputs.target.domains)
        self.assertFalse(inputs.target.env)
        self.assertFalse(set(inputs.plan.upstream_source) & inputs.runtime_environment.keys())
        self.assertNotIn("ODOO_UPSTREAM_HOST", inputs.runtime_environment)
        self.fixture.assert_serving_unchanged()
        self.assertEqual(
            self.store.list_secret_records(
                context_name=self.fixture.context, instance_name="testing"
            ),
            self.credentials,
        )

    def test_upstream_copy_keeps_original_address_without_delivering_source_coordinates(
        self,
    ) -> None:
        allocation = self.allocate("upstream")
        inputs = self.inputs(allocation, "upstream")
        self.assertEqual(inputs.plan.upstream_source["ODOO_UPSTREAM_HOST"], "upstream.invalid")
        assert inputs.plan.generation is not None
        self.assertEqual(inputs.plan.generation.domain_host, inputs.plan.base_url.split("//")[1])
        self.assertFalse(inputs.runtime_environment.keys() & inputs.plan.upstream_source.keys())
        self.fixture.assert_serving_unchanged()

    def test_changed_build_or_source_refuses_without_mutation(self) -> None:
        allocation = self.allocate()
        with self.assertRaises(ValueError):
            self.inputs(allocation, "upstream")
        self.fixture.assert_serving_unchanged()

    def test_snapshot_hold_without_budget_refuses_before_source_probe(self) -> None:
        self.profile.preview.clean_testing_snapshot = True
        self.store.write_product_profile_record(self.profile)
        self.fixture.before = self.fixture.authority()
        allocation = self.allocate("production")
        with patch.object(compiler, "resolve_native_testing_source") as probe:
            with self.assertRaises(compiler.CandidateRuntimeInputsChanged):
                self.inputs(allocation, "production")
        probe.assert_not_called()
        self.fixture.assert_serving_unchanged()

    def execution(self, source: CandidateSource = "empty") -> None:
        migrated = None
        if source == "migrated":
            from tests.test_migrated_testing_source import MigratedTestingRequestTests
            from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget
            from control_plane.odoo_testing_restore import generation_binding

            self.enterContext(
                patch(
                    "control_plane.odoo_testing_native_source.remove_completed_native_capture_cache"
                )
            )

            original = MigratedTestingRequestTests().request().migrated_source
            assert original is not None
            migrated = original.model_copy(
                update={
                    "product": self.fixture.product,
                    "context": self.fixture.context,
                    "target_image": self.artifact.image.digest,
                }
            )
            self.profile = self.profile.model_copy(
                update={
                    "lanes": tuple(
                        lane.model_copy(
                            update={
                                "odoo_testing_drill_storage": OdooTestingDrillStorageBudget(
                                    database_bytes=1024,
                                    filestore_bytes=1024,
                                    staging_bytes=1024,
                                    reserve_bytes=1024,
                                )
                            }
                        )
                        if lane.instance == "testing"
                        else lane
                        for lane in self.profile.lanes
                    )
                }
            )
            self.store.write_product_profile_record(self.profile)
            self.fixture.before = self.fixture.authority()
        self.allocation = self.allocate(source, migrated)
        self.compiled = self.inputs(self.allocation, source, migrated)
        if migrated is not None:
            recorded = self.allocation.model_copy(
                update={"application": TestingCandidateApplication(plan=self.compiled.plan)}
            )
            self.store.compare_and_write_testing_candidate(self.allocation, recorded)
            self.allocation = recorded
            assert self.compiled.plan.generation is not None
            self.store.write_native_testing_copy_capture(
                NativeTestingCopyCapture(
                    product=migrated.product,
                    context=migrated.context,
                    generation_binding_sha256=generation_binding(self.compiled.plan.generation),
                    source_binding_sha256=migrated.binding_sha256,
                    capture_id="f" * 32,
                    database=migrated.database,
                    filestore=migrated.filestore,
                    captured_at=datetime.now(UTC).isoformat(),
                    source_observation=migrated,
                ),
                candidate_namespace=recorded.namespace,
            )
        self.provider_state: dict[str, Any] = {
            "composeId": self.allocation.compose_id,
            "environmentId": self.allocation.environment_id,
            "serverId": self.allocation.server_id,
            "name": self.allocation.namespace,
            "sourceType": "raw",
            "composeFile": "",
            "env": "",
            "autoDeploy": False,
            "appName": self.allocation.namespace + "-abc123",
        }
        self.deployments: list[dict[str, Any]] = []
        self.restore_deployments: list[dict[str, Any]] = []
        self.enterContext(
            patch.object(
                api,
                "find_matching_dokploy_schedule",
                return_value={"scheduleId": "candidate-schedule", "deployments": []},
            )
        )
        self.effects: list[str] = []
        self.lose = ""
        self.restored = False
        self.enterContext(
            patch.object(
                provider_source, "read_dokploy_config", return_value=("provider.invalid", "fixture")
            )
        )
        self.enterContext(
            patch.object(
                application, "fetch_dokploy_compose_domains_for_target_setup", return_value=()
            )
        )
        self.enterContext(
            patch.object(
                api,
                "fetch_dokploy_target_payload",
                side_effect=lambda **_: dict(self.provider_state),
            )
        )
        self.enterContext(
            patch.object(
                api, "list_deployments_for_target", side_effect=lambda **_: list(self.deployments)
            )
        )
        self.fixture.request.side_effect = self.configure
        self.enterContext(patch.object(api, "trigger_deployment", side_effect=self.deploy))
        self.enterContext(
            patch.object(api, "wait_for_target_deployment", return_value="candidate-deployment")
        )
        self.enterContext(
            patch.object(post_deploy, "run_compose_post_deploy_update", side_effect=self.restore)
        )
        self.enterContext(
            patch.object(application, "candidate_private_readiness", side_effect=self.readiness)
        )
        self.execution_lease = Mock(spec=ProviderOperationLease)

    def configure(self, **kwargs: Any) -> Any:
        if kwargs["path"] == "/api/deployment.allByType":
            self.assertEqual(kwargs["query"], {"id": "candidate-schedule", "type": "schedule"})
            return list(self.restore_deployments)
        self.fixture.assert_serving_unchanged()
        self.assertEqual(kwargs["path"], "/api/compose.update")
        self.effects.append("configure")
        self.provider_state.update(kwargs["payload"])
        self.lost("configure")
        return {}

    def lost(self, phase: str) -> None:
        if self.lose == phase:
            self.lose = ""
            raise TimeoutError("fixture lost reply")

    def deploy(self, **kwargs: Any) -> None:
        self.fixture.assert_serving_unchanged()
        self.effects.append("deploy")
        self.deployments.append(
            {"deploymentId": "candidate-deployment", "title": kwargs["title"], "status": "done"}
        )
        self.lost("deploy")

    def restore(self, **kwargs: Any) -> dict[str, str]:
        self.fixture.assert_serving_unchanged()
        self.assertEqual(kwargs["target_definition"].target_id, self.allocation.compose_id)
        self.assertEqual(kwargs["bootstrap_missing_database"], self.compiled.plan.source == "empty")
        self.assertEqual(kwargs["run_destructive_restore"], self.compiled.plan.source != "empty")
        self.assertEqual(kwargs["fresh_testing_generation"], self.compiled.plan.generation)
        self.assertEqual(kwargs["testing_candidate_namespace"], self.allocation.namespace)
        kwargs["before_provider_mutation"](post_deploy.ODOO_POST_DEPLOY_SCHEDULE_UPSERT)
        kwargs["before_provider_mutation"](post_deploy.ODOO_POST_DEPLOY_SCHEDULE_TRIGGER)
        self.effects.append("restore")
        self.restore_deployments.append({"deploymentId": "original-restore", "status": "done"})
        self.restored = True
        self.lost("restore")
        return {"integration_readback_ok": "true"}

    def readiness(self, **kwargs: Any) -> TestingCandidatePrepared:
        if not self.restored:
            raise ValueError("fixture not healthy")
        return TestingCandidatePrepared(
            plan_sha256=self.compiled.plan.digest,
            runtime_identity=self.compiled.plan.runtime_identity,
            private_endpoint="http://" + self.allocation.namespace + ":8069",
            deployment_id="candidate-deployment",
            compose_project=str(self.provider_state["appName"]),
            source_witness="fixture-restored",
            integration_isolation_verified=True,
            credentials_verified=True,
            observed_at=datetime.now(UTC),
        )

    def prepare_application(self) -> TestingCandidateAllocation:
        return application.prepare_candidate_application(
            root=self.root,
            store=self.store,
            allocation=self.allocation,
            inputs=self.compiled,
            lease=self.execution_lease,
            guard=self.fixture.guard,
        )

    def test_candidate_deploys_before_prepared_receipt_without_publishing_serving_records(
        self,
    ) -> None:
        self.execution()
        result = self.prepare_application()
        assert result.application is not None
        self.assertEqual(result.application.phase, "prepared")
        self.assertEqual(self.effects, ["configure", "deploy", "restore"])
        self.fixture.assert_serving_unchanged()
        self.prepare_application()
        self.assertEqual(self.effects, ["configure", "deploy", "restore"])

    def test_candidate_environment_reaches_final_consumer(self) -> None:
        self.execution()
        self.compiled.runtime_environment.update(VALUES)
        result = self.prepare_application()
        self.assertIsNotNone(result.application)
        wire = self.provider_state["env"]
        self.assertEqual(api.parse_dokploy_env_text(wire), self.compiled.runtime_environment)
        unqualified_values = {**self.compiled.runtime_environment, "APP": "hello $NAME"}
        for key in VALUES:
            unqualified_values.pop(key, None)
        with self.assertRaises(ValueError):
            application.require_candidate_configuration(
                self.allocation,
                replace(self.compiled, runtime_environment=unqualified_values),
                {
                    **self.provider_state,
                    "env": api.serialize_dokploy_env_text({}, target_type="compose")
                    + "\n"
                    + api.serialize_dokploy_env_text(unqualified_values),
                },
            )
        legacy = api.serialize_dokploy_env_text({"APP": '"literal" $NAME'})
        encoded = api.serialize_dokploy_env_text({"APP": '"literal" $NAME'}, target_type="compose")
        self.assertNotEqual(
            application.candidate_configuration_sha256({"env": legacy}),
            application.candidate_configuration_sha256({"env": encoded}),
        )
        saved = (ROOT / "tests/fixtures/dokploy-compose/legacy-literal.env").read_text()
        with self.assertRaises(ValueError):
            application.require_candidate_configuration(
                self.allocation,
                replace(self.compiled, runtime_environment=api.parse_dokploy_env_text(saved)),
                {**self.provider_state, "env": saved},
            )
        self.assertNotEqual(
            application.candidate_configuration_sha256({"env": saved}),
            application.candidate_configuration_sha256({"env": "# provider comment\n" + saved}),
        )
        if consumer_commands_available():
            consumed, _ = consume_compose_environment(wire, tuple(VALUES))
            for service in consumed.values():
                self.assertEqual(
                    service, {key: value.replace("$", "$$") for key, value in VALUES.items()}
                )
        self.fixture.assert_serving_unchanged()

    def test_invalid_candidate_environment_refuses_before_configuration_checkpoint(self) -> None:
        self.execution()
        self.compiled.runtime_environment["APP"] = "invalid\x00value"
        with self.assertRaises(api.DokployEnvironmentEncodingError):
            self.prepare_application()
        self.assertEqual(self.effects, [])
        self.execution_lease.checkpoint_effect.assert_not_called()

    def test_lost_effect_reply_reconciles_without_duplicate_dispatch(self) -> None:
        for phase in ("configure", "deploy", "restore"):
            with self.subTest(phase=phase):
                # Separate operation/store per scenario.
                scenario = CandidateApplicationTests()
                scenario.setUp()
                self.addCleanup(scenario.doCleanups)
                scenario.execution()
                scenario.lose = phase
                with self.assertRaises(TimeoutError):
                    scenario.prepare_application()
                result = scenario.prepare_application()
                assert result.application is not None
                self.assertEqual(result.application.phase, "prepared")
                self.assertEqual(scenario.effects, ["configure", "deploy", "restore"])
                scenario.fixture.assert_serving_unchanged()
                scenario.doCleanups()

    def test_unknown_deployment_is_not_redispatched_and_old_lane_keeps_serving(self) -> None:
        self.execution()
        self.lose = "deploy"
        with self.assertRaises(TimeoutError):
            self.prepare_application()
        self.deployments.clear()
        with self.assertRaises(ValueError):
            self.prepare_application()
        self.assertEqual(self.effects, ["configure", "deploy"])
        self.fixture.assert_serving_unchanged()

    def test_denial_propagates_before_candidate_configuration_without_alternate_action(
        self,
    ) -> None:
        self.execution()
        self.execution_lease.assert_current.side_effect = PermissionError("fixture refusal")
        with self.assertRaises(PermissionError):
            self.prepare_application()
        self.assertFalse(self.effects)
        self.fixture.assert_serving_unchanged()

    def test_failed_restore_can_be_abandoned_without_erasing_custody_or_old_lane(self) -> None:
        self.execution()
        self.lose = "restore"
        with self.assertRaises(TimeoutError):
            self.prepare_application()
        self.restore_deployments[0]["status"] = "error"
        restoring = self.store.read_testing_candidate(self.allocation.namespace)
        assert restoring is not None
        for predicate in (
            ReadbackPredicate.ATTACHMENTS,
            ReadbackPredicate.INTEGRATIONS,
            ReadbackPredicate.ISOLATION,
        ):
            for kind in ("refused", "unavailable"):
                with self.subTest(predicate=predicate, kind=kind):
                    self.restore_deployments[0]["logs"] = [
                        f"readback_failed_predicate={predicate}",
                        f"readback_failure_kind={kind}",
                        "private attachment detail",
                    ]
                    evidence = application.candidate_restore_terminal(
                        host="fixture",
                        token="fixture",
                        record=restoring,
                        require_success=False,
                    )
                    self.assertIn(f":{predicate}_{kind}:", evidence)
                    self.assertTrue(evidence.endswith(":" + self.restore_deployments[0]["status"]))
                    self.assertNotIn("private", evidence)
        for invalid_predicate, invalid_kind in (
            ("private-value", "refused"),
            (ReadbackPredicate.ATTACHMENTS, "private-value"),
        ):
            self.restore_deployments[0]["logs"] = [
                f"readback_failed_predicate={invalid_predicate}",
                f"readback_failure_kind={invalid_kind}",
            ]
            generic = application.candidate_restore_terminal(
                host="fixture", token="fixture", record=restoring, require_success=False
            )
            self.assertNotIn("private", generic)
            self.assertNotIn("refused", generic)
        self.restore_deployments[0]["logs"] = []
        with patch.object(
            api,
            "fetch_dokploy_deployment_logs",
            side_effect=post_deploy.OdooPostDeployReadbackFailure(
                "private unavailable", evidence={}
            ),
        ):
            self.assertEqual(
                application.candidate_restore_terminal(
                    host="fixture", token="fixture", record=restoring, require_success=False
                ),
                generic,
            )
        self.restore_deployments[0]["logs"] = [
            f"readback_failed_predicate={ReadbackPredicate.ATTACHMENTS}",
            "readback_failure_kind=refused",
        ]
        with self.assertRaises(ValueError):
            self.prepare_application()
        abandoned = application.abandon_candidate_application(
            root=self.root,
            store=self.store,
            namespace=self.allocation.namespace,
            lease=self.execution_lease,
            guard=self.fixture.guard,
        )
        assert abandoned.application is not None
        self.assertEqual(abandoned.application.phase, "abandoned")
        self.assertIn(
            f":{ReadbackPredicate.ATTACHMENTS}_refused:", abandoned.application.abandonment_evidence
        )
        self.assertEqual(abandoned.compose_id, self.allocation.compose_id)
        self.assertEqual(
            abandoned.resources_binding_sha256, self.allocation.resources_binding_sha256
        )
        with self.assertRaises(ValueError):
            self.prepare_application()
        self.fixture.assert_serving_unchanged()
        self.assertEqual(self.effects, ["configure", "deploy", "restore"])

    def test_running_or_unknown_restore_cannot_publish_or_abandon(self) -> None:
        self.execution()
        self.lose = "restore"
        with self.assertRaises(TimeoutError):
            self.prepare_application()
        for history in ([{"deploymentId": "original-restore", "status": "running"}], []):
            self.restore_deployments[:] = history
            with self.assertRaises(ValueError):
                self.prepare_application()
            with self.assertRaises(ValueError):
                application.abandon_candidate_application(
                    root=self.root,
                    store=self.store,
                    namespace=self.allocation.namespace,
                    lease=self.execution_lease,
                    guard=self.fixture.guard,
                )
        self.assertEqual(self.effects, ["configure", "deploy", "restore"])
        self.fixture.assert_serving_unchanged()

    def test_existing_restore_helper_uses_candidate_schedule_without_replacing_lane_schedule(
        self,
    ) -> None:
        allocation = self.allocate()
        inputs = self.inputs(allocation)
        evidence = {
            "log_available": "true",
            "odoo_module_update_completed": "true",
            "odoo_module_update_image_match": "true",
            "odoo_module_update_modules_configured": "true",
            "integration_readback_ok": "true",
            "integration_readback_checked": "1",
        }
        with (
            patch.object(
                api,
                "fetch_dokploy_target_payload",
                return_value={
                    "composeId": allocation.compose_id,
                    "env": api.serialize_dokploy_env_text(inputs.runtime_environment),
                    "appName": allocation.namespace,
                    "serverId": allocation.server_id,
                },
            ),
            patch.object(api, "find_matching_dokploy_schedule", return_value=None) as find_schedule,
            patch.object(
                post_deploy, "run_manual_data_schedule", return_value=evidence
            ) as run_schedule,
        ):
            post_deploy.run_compose_post_deploy_update(
                host="provider.invalid",
                token="fixture",
                target_definition=inputs.target,
                env_file=None,
                workflow_environment_overrides=inputs.workflow_environment,
                required_workflow_environment_keys=inputs.required_workflow_keys,
                bootstrap_missing_database=True,
                testing_candidate_namespace=allocation.namespace,
            )
        self.assertEqual(find_schedule.call_args.kwargs["app_name"], allocation.namespace)
        self.assertEqual(run_schedule.call_args.kwargs["schedule_app_name"], allocation.namespace)
        self.assertNotEqual(
            run_schedule.call_args.kwargs["schedule_app_name"],
            post_deploy._build_dokploy_data_workflow_schedule_app_name(
                context_name=allocation.context, instance_name="testing"
            ),
        )
        self.fixture.assert_serving_unchanged()

    def test_production_source_binds_readonly_native_authority_at_the_existing_address(
        self,
    ) -> None:
        allocation = self.allocate("production")
        prod_lane = next(lane for lane in self.profile.lanes if lane.instance == "prod")
        descriptor = NativeOdooTestingSource(
            product=self.profile.product,
            context=prod_lane.context,
            target_id="production-compose",
            database_name="production_db",
            filestore_path="/production/filestore",
            settings_sha256="b" * 64,
            server_id="production-server",
            compose_project="production-project",
            database_container="c" * 64,
            web_container="d" * 64,
            database_image="sha256:" + "e" * 64,
            web_image="sha256:" + "f" * 64,
        )
        authority = NativeOdooSourceAuthority.model_validate(
            descriptor.model_dump(include=set(NativeOdooSourceAuthority.model_fields))
        )
        with (
            patch.object(compiler, "native_testing_source_authority", return_value=authority),
            patch.object(compiler, "resolve_native_testing_source", return_value=descriptor),
        ):
            inputs = self.inputs(allocation, "production")
        assert inputs.plan.generation is not None
        self.assertEqual(inputs.plan.generation.source_binding_sha256, authority.binding_sha256)
        self.assertEqual(inputs.native_source, descriptor)
        self.assertEqual(
            inputs.plan.base_url,
            next(
                lane.base_url.rstrip("/")
                for lane in self.profile.lanes
                if lane.instance == "testing"
            ),
        )
        self.fixture.assert_serving_unchanged()
        # Input changes cannot silently rebind a persisted candidate application.
        persisted = allocation.model_copy(
            update={"application": TestingCandidateApplication(plan=inputs.plan)}
        )
        self.store.compare_and_write_testing_candidate(allocation, persisted)
        changed = self.artifact.model_copy(update={"source_commit": "changed-commit"})
        self.store.write_artifact_manifest(changed)
        with (
            patch.object(compiler, "native_testing_source_authority", return_value=authority),
            patch.object(compiler, "resolve_native_testing_source", return_value=descriptor),
            self.assertRaises(ValueError),
        ):
            self.inputs(persisted, "production")
        self.fixture.assert_serving_unchanged()

    def test_prepared_native_copy_keeps_source_proof_through_prepared_resume(self) -> None:
        from control_plane import odoo_testing_native_source as native
        from control_plane.odoo_testing_restore import generation_binding
        from tests.test_odoo_testing_native_source import descriptor

        source = descriptor().model_copy(
            update={
                "product": self.profile.product,
                "context": next(
                    lane.context for lane in self.profile.lanes if lane.instance == "prod"
                ),
            }
        )
        authority = NativeOdooSourceAuthority.model_validate(
            source.model_dump(include=set(NativeOdooSourceAuthority.model_fields))
        )
        with (
            patch.object(compiler, "native_testing_source_authority", return_value=authority),
            patch.object(compiler, "resolve_native_testing_source", return_value=source),
        ):
            self.execution("production")
        generation = self.compiled.plan.generation
        assert generation is not None
        serving_generation = self.store.read_fresh_odoo_testing_generation(
            generation.product, generation.context
        )
        cache = (self.root / "source-cache").resolve()
        cache.mkdir()
        captures: list[NativeTestingCopyCapture] = []
        restore = self.restore

        def stream(**kwargs: Any) -> None:
            receipts = {}
            for part in ("database", "filestore"):
                data = f"private {part} fixture".encode()
                kwargs["receive"](
                    {"type": "data", "part": part, "data": base64.b64encode(data).decode()}
                )
                receipts[part] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            kwargs["receive"]({"type": "complete", **receipts})

        def restore_with_capture(**kwargs: Any) -> dict[str, str]:
            captures.append(
                native.capture_native_testing_source(
                    self.store, root=self.root, generation=generation, descriptor=source
                )
            )
            for context, instance in (
                (self.allocation.namespace, "testing-candidate"),
                (self.allocation.context, "testing"),
            ):
                original = self.store.read_provider_target_record(
                    context_name=context, instance_name=instance
                )
                self.store.write_provider_target_record(
                    original.model_copy(update={"target_id": "foreign-custody-fixture"})
                )
                try:
                    with self.assertRaises(ValueError):
                        self.store.write_native_testing_copy_capture(
                            captures[0], candidate_namespace=self.allocation.namespace
                        )
                finally:
                    self.store.write_provider_target_record(original)
            for namespace, receipt in (
                ("foreign-candidate", captures[0]),
                (
                    self.allocation.namespace,
                    captures[0].model_copy(update={"generation_binding_sha256": "0" * 64}),
                ),
                (
                    self.allocation.namespace,
                    captures[0].model_copy(update={"capture_id": "0" * 32}),
                ),
            ):
                with self.assertRaises(ValueError):
                    self.store.write_native_testing_copy_capture(
                        receipt, candidate_namespace=namespace
                    )
            return restore(**kwargs)

        with (
            patch.object(native, "_cache_directory", return_value=cache),
            patch.object(native, "read_native_source", side_effect=stream),
            patch.object(
                post_deploy, "run_compose_post_deploy_update", side_effect=restore_with_capture
            ),
        ):
            self.prepare_application()
            capture = captures[0]
            directory = cache / capture.capture_id
            self.assertTrue(directory.exists())
            prepared = self.store.read_testing_candidate(self.allocation.namespace)
            assert prepared is not None and prepared.application is not None
            self.assertEqual(prepared.application.phase, "prepared")
            with patch.object(
                compiler,
                "resolve_native_testing_source",
                side_effect=AssertionError("A prepared copy reuses its original capture receipt"),
            ):
                self.compiled = self.inputs(prepared, "production")
                self.assertEqual(self.prepare_application(), prepared)
            from tests.test_testing_rebuild import rebuild_plan, TestingProvider
            from control_plane.contracts.testing_rebuild import TestingRebuildRecord
            from control_plane.testing_rebuild_storage import RecordTestingRebuildStore
            from control_plane.workflows.testing_rebuild import execute_testing_rebuild

            plan = rebuild_plan().model_copy(
                update={
                    "product": prepared.product,
                    "context": prepared.context,
                    "operation_id": prepared.operation_id,
                }
            )
            plan = plan.model_copy(
                update={
                    "new": plan.new.model_copy(
                        update={
                            "copy_id": prepared.namespace,
                            "target_id": prepared.compose_id,
                        }
                    )
                }
            )
            plan = plan.model_copy(
                update={
                    name: getattr(plan, name).model_copy(
                        update={
                            "runtime_identity": getattr(plan, name).runtime_identity.model_copy(
                                update={
                                    "product": prepared.product,
                                    "context": prepared.context,
                                }
                            )
                        }
                    )
                    for name in ("old", "new")
                }
            )
            provider = TestingProvider(plan)
            checkpoints = RecordTestingRebuildStore(self.store)
            checkpoints.save(
                TestingRebuildRecord(plan=plan, began_at=provider.now, updated_at=provider.now)
            )
            application.remove_prepared_candidate_source_cache(self.store, prepared)
            self.assertTrue(directory.exists())
            completed = execute_testing_rebuild(
                plan,
                checkpoints,
                provider,
                clock=lambda: provider.now,
            )
            self.assertEqual(completed.step, "complete")
            with patch.object(
                RecordTestingRebuildStore,
                "load_operation",
                return_value=completed.model_copy(
                    update={"error": "candidate_public_health_failed"}
                ),
            ):
                application.remove_prepared_candidate_source_cache(self.store, prepared)
            self.assertTrue(directory.exists())
            from control_plane import testing_rebuild_service as service
            from control_plane.provider_operations import ProviderEvidenceLease

            adapter = service.NativeTestingRebuildAdapter(
                self.root,
                self.store,
                Mock(
                    product=prepared.product,
                    context=prepared.context,
                    operation_id=prepared.operation_id,
                ),
                Mock(),
                Mock(),
            )
            executor = Mock(allocation=prepared)
            executor.run.return_value = completed
            lease = cast(ProviderOperationLease, cast(object, Mock(spec=ProviderEvidenceLease)))
            remove = shutil.rmtree

            def lost_cleanup(path: Any) -> None:
                remove(path)
                raise TimeoutError("Fixture lost cleanup reply")

            with (
                patch.object(service, "NativeTestingRebuildExecution", return_value=executor),
                patch.object(shutil, "rmtree", side_effect=lost_cleanup),
            ):
                result = adapter.apply("completed-original", lease)
                response = result.response_payload["result"]
                assert isinstance(response, dict)
                self.assertEqual(response["deploy_status"], "pass")
            with patch.object(service, "NativeTestingRebuildExecution", return_value=executor):
                adapter.apply("completed-original", lease)
            self.assertFalse(directory.exists())

        self.assertEqual(
            self.store.read_native_testing_copy_capture(generation_binding(generation)), capture
        )
        self.assertEqual(captures, [capture])
        self.assertEqual(
            self.store.read_fresh_odoo_testing_generation(generation.product, generation.context),
            serving_generation,
        )
        self.assertEqual(self.effects, ["configure", "deploy", "restore"])
        self.fixture.assert_serving_unchanged()

    def test_undelivered_global_change_does_not_rebind_or_block_candidate(self) -> None:
        allocation = self.allocate()
        original = self.inputs(allocation)
        persisted = allocation.model_copy(
            update={"application": TestingCandidateApplication(plan=original.plan)}
        )
        self.store.compare_and_write_testing_candidate(allocation, persisted)
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="global", env={"ODOO_WORKERS": "3"}, updated_at=self.profile.updated_at
            )
        )
        resumed = self.inputs(persisted)
        self.assertEqual(resumed.plan, original.plan)
        self.assertEqual(resumed.runtime_environment, original.runtime_environment)

    def test_delivered_context_change_cannot_rebind_an_original_candidate(self) -> None:
        allocation = self.allocate()
        original = self.inputs(allocation)
        persisted = allocation.model_copy(
            update={"application": TestingCandidateApplication(plan=original.plan)}
        )
        self.store.compare_and_write_testing_candidate(allocation, persisted)
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="context",
                context=self.fixture.context,
                env={"ODOO_WORKERS": "3"},
                updated_at=self.profile.updated_at,
            )
        )
        self.fixture.before = self.fixture.authority()
        with self.assertRaises(ValueError):
            self.inputs(persisted)
        self.fixture.assert_serving_unchanged()

    def test_crash_after_restore_intent_before_any_effect_resumes_the_original_workflow(
        self,
    ) -> None:
        self.execution()
        write = self.store.compare_and_write_testing_candidate

        def crash(
            previous: TestingCandidateAllocation | None, updated: TestingCandidateAllocation
        ) -> None:
            write(previous, updated)
            if (
                previous is not None
                and previous.application is not None
                and previous.application.phase == "deployed"
                and updated.application is not None
                and updated.application.phase == "restoring"
            ):
                raise TimeoutError("fixture ended after restore intent")

        with (
            patch.object(self.store, "compare_and_write_testing_candidate", side_effect=crash),
            self.assertRaises(TimeoutError),
        ):
            self.prepare_application()
        self.assertEqual(self.effects, ["configure", "deploy"])
        resumed = self.prepare_application()
        assert resumed.application is not None
        self.assertEqual(resumed.application.phase, "prepared")
        self.assertEqual(self.effects, ["configure", "deploy", "restore"])
        self.fixture.assert_serving_unchanged()

    def test_lost_lease_before_restore_dispatch_can_resume_original_setup(self) -> None:
        self.execution()

        def checkpoint(phase: str) -> None:
            if phase == post_deploy.ODOO_POST_DEPLOY_SCHEDULE_TRIGGER:
                raise RuntimeError("fixture lease lost before restore dispatch")

        self.execution_lease.checkpoint_effect.side_effect = checkpoint
        with self.assertRaises(RuntimeError):
            self.prepare_application()
        record = self.store.read_testing_candidate(self.allocation.namespace)
        assert record is not None and record.application is not None
        self.assertIs(record.application.restore_workflow_started, False)
        self.assertFalse(record.application.restore_schedule_id)
        self.assertEqual(self.effects, ["configure", "deploy"])
        self.execution_lease.checkpoint_effect.side_effect = None
        resumed = self.prepare_application()
        assert resumed.application is not None
        self.assertEqual(resumed.application.phase, "prepared")
        self.assertEqual(self.effects, ["configure", "deploy", "restore"])
        self.fixture.assert_serving_unchanged()

    def test_refused_schedule_setup_can_be_abandoned_without_a_restore_job(self) -> None:
        self.execution()
        with (
            patch.object(api, "find_matching_dokploy_schedule", return_value=None),
            self.assertRaises(ValueError),
        ):
            self.prepare_application()
        abandoned = application.abandon_candidate_application(
            root=self.root,
            store=self.store,
            namespace=self.allocation.namespace,
            lease=self.execution_lease,
            guard=self.fixture.guard,
        )
        assert abandoned.application is not None
        self.assertEqual(abandoned.application.phase, "abandoned")
        self.assertEqual(abandoned.compose_id, self.allocation.compose_id)
        self.assertEqual(self.effects, ["configure", "deploy"])
        self.fixture.assert_serving_unchanged()

    def test_failed_original_build_can_be_abandoned_without_running_a_restore(self) -> None:
        self.execution()
        self.lose = "deploy"
        with self.assertRaises(TimeoutError):
            self.prepare_application()
        self.deployments[0]["status"] = "error"
        with self.assertRaises(api.DokployDeploymentFailed):
            self.prepare_application()
        abandoned = application.abandon_candidate_application(
            root=self.root,
            store=self.store,
            namespace=self.allocation.namespace,
            lease=self.execution_lease,
            guard=self.fixture.guard,
        )
        assert abandoned.application is not None
        self.assertEqual(abandoned.application.phase, "abandoned")
        self.assertEqual(self.effects, ["configure", "deploy"])
        self.assertEqual(abandoned.compose_id, self.allocation.compose_id)
        self.fixture.assert_serving_unchanged()

    def test_older_restore_intent_without_dispatch_evidence_stays_unknown(self) -> None:
        self.execution()
        write = self.store.compare_and_write_testing_candidate

        def legacy_crash(
            previous: TestingCandidateAllocation | None, updated: TestingCandidateAllocation
        ) -> None:
            if (
                previous is not None
                and previous.application is not None
                and previous.application.phase == "deployed"
                and updated.application is not None
                and updated.application.phase == "restoring"
            ):
                legacy = updated.model_copy(
                    update={
                        "application": updated.application.model_copy(
                            update={"restore_workflow_started": None}
                        )
                    }
                )
                write(previous, legacy)
                raise TimeoutError("fixture older writer has no dispatch marker")
            write(previous, updated)

        with (
            patch.object(
                self.store, "compare_and_write_testing_candidate", side_effect=legacy_crash
            ),
            self.assertRaises(TimeoutError),
        ):
            self.prepare_application()
        with self.assertRaises(application.CandidateObservationUnavailable):
            self.prepare_application()
        current = self.store.read_testing_candidate(self.allocation.namespace)
        assert current is not None and current.application is not None
        with self.assertRaises(ValueError):
            self.store.compare_and_write_testing_candidate(
                current,
                current.model_copy(
                    update={
                        "application": current.application.model_copy(
                            update={"restore_workflow_started": False}
                        )
                    }
                ),
            )
        self.assertEqual(self.effects, ["configure", "deploy"])
        self.fixture.assert_serving_unchanged()
