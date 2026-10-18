"""A private candidate uses the current build and only testing-local passwords."""

import os
from datetime import UTC, datetime
from typing import Any
from unittest.mock import Mock
import unittest
from unittest.mock import patch

from control_plane import secrets
from control_plane import testing_candidate_application as application
from control_plane.dokploy import api, source as provider_source, post_deploy
from control_plane.contracts.testing_candidate_application import TestingCandidatePrepared
from control_plane.provider_operations import ProviderOperationLease
from control_plane.contracts.artifact_identity import (
    ArtifactIdentityManifest,
    ArtifactImageReference,
)
from control_plane.contracts.odoo_stable_target_replacement import LAUNCHPLANE_REQUIRED_ODOO_MODULES
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.testing_candidate import TestingCandidateAllocation
from control_plane.contracts.testing_candidate_application import TestingCandidateApplication
from control_plane.contracts.odoo_testing_source import (
    NativeOdooTestingSource,
    NativeOdooSourceAuthority,
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


class CandidateApplicationTests(unittest.TestCase):
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
            source_commit="new-source",
            enterprise_base_digest="sha256:" + "b" * 64,
            odoo_install_modules=LAUNCHPLANE_REQUIRED_ODOO_MODULES,
            image=ArtifactImageReference(
                repository=self.profile.image.repository, digest="sha256:" + "a" * 64
            ),
        )
        self.store.write_artifact_manifest(self.artifact)
        self.fixture.before = self.fixture.authority()
        self.credentials = self.store.list_secret_records(
            context_name=self.fixture.context, instance_name="testing"
        )

    def allocate(self, source: CandidateSource = "empty") -> TestingCandidateAllocation:
        self.fixture.allocation = self.fixture.allocation.model_copy(
            update={
                "intent_sha256": candidate_application_intent(
                    product=self.fixture.product,
                    context=self.fixture.context,
                    artifact_id=self.artifact.artifact_id,
                    source=source,
                )
            }
        )
        return self.fixture.prepare()

    def inputs(
        self, allocation: TestingCandidateAllocation, source: CandidateSource = "empty"
    ) -> TestingCandidateRuntimeInputs:
        return candidate_runtime_inputs(
            root=self.root,
            store=self.store,
            allocation=allocation,
            artifact_id=self.artifact.artifact_id,
            source=source,
        )

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

    def execution(self, source: CandidateSource = "empty") -> None:
        self.allocation = self.allocate(source)
        self.compiled = self.inputs(self.allocation, source)
        self.provider_state: dict[str, Any] = {
            "composeId": self.allocation.compose_id,
            "environmentId": self.allocation.environment_id,
            "serverId": self.allocation.server_id,
            "name": self.allocation.namespace,
            "sourceType": "raw",
            "composeFile": "",
            "env": "",
            "autoDeploy": False,
            "appName": self.allocation.namespace,
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
