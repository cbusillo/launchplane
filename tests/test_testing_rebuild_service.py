"""The ordinary replacement action admits a native rebuild only on testing."""

import os
from typing import Any
import unittest
import yaml
from unittest.mock import Mock, patch
from control_plane import secrets
from control_plane.contracts.secret_record import SecretVersion
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_serving_copy import resolve_testing_maintenance_environment
from control_plane.testing_serving_copy import (
    render_maintained_testing_copy,
    require_maintained_local_credentials,
)
from control_plane.odoo_testing_restore import read_current_testing_generation
from tests import test_testing_publication as publication_tests
from control_plane.testing_serving_copy import published_testing_copy
from control_plane.contracts.testing_publication import TestingPublicationRecord

from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
    OdooStableTargetReplacementRequest,
)
from control_plane.workflows.odoo_stable_target_replacement import (
    build_odoo_stable_target_replacement_plan,
)
from control_plane.workflows import odoo_stable_target_replacement as maintenance
from control_plane.dokploy import api, compose, post_deploy
from control_plane.contracts.runtime_identity import COMPOSE_IMAGE_REFERENCE_ENV_KEYS
from control_plane.workflows.runtime_identity_health import HealthcheckPass
from control_plane.workflows.odoo_verification import (
    OdooVerificationEvidence,
    OdooVerificationResult,
)


def exercise_published_copy_maintenance(
    case: unittest.TestCase, fixture: publication_tests.TestingPublicationTests
) -> None:
    """Run ordinary replacement/post-deploy with mocked provider IO and build ordering."""
    publication = published_testing_copy(
        fixture.store, product=fixture.candidate.product, context=fixture.candidate.context
    )
    assert publication is not None
    app = fixture.fixture
    candidate = fixture.candidate
    artifact = app.artifact.model_copy(
        update={
            "artifact_id": "later-build",
            "source_commit": "e" * 40,
            "image": app.artifact.image.model_copy(update={"digest": "sha256:" + "c" * 64}),
        }
    )
    fixture.store.write_artifact_manifest(artifact)
    before = fixture.current()
    credentials = fixture.store.list_secret_records()
    provider = app.provider_state
    checkpoint = Mock()

    def fetch(**kwargs: Any) -> Any:
        case.assertEqual(kwargs["target_id"], candidate.compose_id)
        return dict(provider)

    def request(**kwargs: Any) -> Any:
        if kwargs["path"] == "/api/domain.byComposeId":
            case.assertEqual(kwargs["query"]["composeId"], candidate.compose_id)
            return []
        if kwargs["path"] == "/api/docker.getContainersByAppNameMatch":
            case.assertEqual(kwargs["query"]["appName"], candidate.namespace)
            return [
                {
                    "containerId": "web",
                    "name": candidate.namespace + "-web-1",
                    "state": "running",
                }
            ]
        if kwargs["path"] == "/api/docker.getConfig":
            return {
                "Config": {"Labels": {}},
                "NetworkSettings": {"Networks": {"dokploy-network": {}}},
            }
        case.fail("Unexpected provider operation")

    def sync(**kwargs: Any) -> Any:
        case.assertEqual(kwargs["compose_id"], candidate.compose_id)
        provider["composeFile"] = kwargs["compose_file"]
        return {"applied": "true"}

    def env(**kwargs: Any) -> None:
        case.assertEqual(kwargs["target_id"], candidate.compose_id)
        provider["env"] = kwargs["env_text"]

    def update(**kwargs: Any) -> Any:
        case.assertEqual(kwargs["target_definition"].target_id, candidate.compose_id)
        case.assertFalse(kwargs["run_destructive_restore"])
        case.assertIsNone(kwargs["fresh_testing_generation"])
        kwargs["before_provider_mutation"](post_deploy.ODOO_POST_DEPLOY_SCHEDULE_TRIGGER)
        return {
            "log_available": "true",
            **dict.fromkeys(post_deploy.ODOO_MODULE_UPDATE_REQUIRED_READBACK_MARKERS, "true"),
        }

    base_url = app.compiled.plan.base_url
    profile = fixture.store.read_product_profile_record(candidate.product)
    with (
        patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": fixture.store.database_url}),
        patch.object(api, "fetch_dokploy_target_payload", side_effect=fetch),
        patch.object(compose, "sync_dokploy_compose_raw_source", side_effect=sync),
        patch.object(
            compose,
            "fetch_dokploy_converted_compose_file",
            side_effect=lambda **_: provider["composeFile"],
        ),
        patch.object(api, "update_dokploy_target_env", side_effect=env),
        patch.object(api, "trigger_deployment"),
        patch.object(api, "wait_for_target_deployment"),
        patch.object(maintenance, "require_forward_lane_build"),
        patch.object(maintenance, "require_forward_build"),
        patch.object(post_deploy, "run_compose_post_deploy_update", side_effect=update),
        patch.object(
            maintenance,
            "verify_odoo_stable_readiness",
            return_value=OdooVerificationResult(
                health_status="pass",
                canonical_status="skipped",
                logo_status="skipped",
                evidence=OdooVerificationEvidence(
                    base_url=base_url, health_url=base_url + profile.health_path
                ),
            ),
        ),
        patch.object(
            maintenance,
            "wait_for_runtime_identity_healthcheck_with_retry",
            side_effect=lambda **kw: HealthcheckPass(
                payload={
                    "runtime_identity": kw["expected_runtime_identity"].model_dump(mode="json")
                }
            ),
        ),
    ):
        result = maintenance.execute_odoo_stable_target_replacement_apply(
            control_plane_root=app.root,
            record_store=fixture.store,
            request=OdooStableTargetReplacementApplyRequest(
                product=candidate.product,
                instance="testing",
                artifact_id=artifact.artifact_id,
                source_git_ref=artifact.source_commit,
                verify_canonical=False,
                verify_logo=False,
            ),
            dokploy_request=request,
            provider_effect_checkpoint=checkpoint,
        )
    case.assertEqual(result.deploy_status, "pass", result.error_code)
    checkpoint.assert_called()
    current = fixture.current()
    case.assertEqual(current.target_id, before.target_id)
    case.assertEqual(current.binding, before.binding)
    case.assertEqual(fixture.store.list_secret_records(), credentials)
    case.assertEqual(current.generation, before.generation)
    case.assertEqual(
        published_testing_copy(fixture.store, product=candidate.product, context=candidate.context),
        publication,
    )
    assert current.inventory is not None and current.inventory.artifact_identity is not None
    case.assertEqual(current.inventory.artifact_identity.artifact_id, artifact.artifact_id)
    delivered = api.parse_dokploy_env_text(str(provider["env"]))
    image = artifact.image.repository + "@" + artifact.image.digest
    case.assertTrue(all(delivered.get(key) == image for key in COMPOSE_IMAGE_REFERENCE_ENV_KEYS))
    web = yaml.safe_load(provider["composeFile"])["services"]["web"]
    case.assertFalse(web.get("ports"))
    case.assertTrue(
        any(
            candidate.namespace in network.get("aliases", [])
            for network in web["networks"].values()
            if isinstance(network, dict)
        )
    )


class NativeTestingRequestTests(unittest.TestCase):
    def test_existing_action_accepts_the_three_native_testing_sources(self) -> None:
        for source in ("empty", "production_restore", "upstream_restore"):
            with self.subTest(source=source):
                request = OdooStableTargetReplacementApplyRequest.model_validate(
                    {
                        "product": "example",
                        "instance": "testing",
                        "strategy": "replace-and-cutover",
                        "data_source_mode": source,
                        "allow_empty_data": source == "empty",
                        "artifact_id": "new-build",
                        "source_git_ref": "new-source",
                    }
                )
                self.assertEqual(request.data_source_mode, source)

    def test_native_strategy_cannot_reach_production_or_in_place_restore(self) -> None:
        for changes in (
            {"instance": "prod"},
            {"data_source_mode": "existing"},
            {"strategy": "recreate-in-place", "data_source_mode": "production_restore"},
            {"verify_health": False},
            {"artifact_id": "", "source_git_ref": ""},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                OdooStableTargetReplacementApplyRequest.model_validate(
                    {
                        "product": "example",
                        "instance": "testing",
                        "strategy": "replace-and-cutover",
                        "data_source_mode": "empty",
                        "allow_empty_data": True,
                        "artifact_id": "new-build",
                        "source_git_ref": "new-source",
                        **changes,
                    }
                )


class HistoricalPublishedTestingCopyTests(unittest.TestCase):
    def test_original_published_blank_receipt_supports_maintenance_only_with_exact_proof(
        self,
    ) -> None:
        from control_plane import testing_candidate_runtime as compiler
        from control_plane.odoo_testing_restore import (
            FreshOdooTestingGeneration,
            fresh_testing_mode_refusal,
        )

        fixture = publication_tests.TestingPublicationTests()
        fixture.source_mode = "upstream"

        def original_generation(**kwargs: Any) -> FreshOdooTestingGeneration:
            return FreshOdooTestingGeneration(**{**kwargs, "reconcile_requested_at": ""})

        with patch.object(compiler, "FreshOdooTestingGeneration", side_effect=original_generation):
            fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.prepare()
        publication = fixture.publish()
        generation = publication.plan.after.generation
        assert generation is not None
        self.assertFalse(generation.reconcile_requested_at)
        self.assertIsNotNone(fresh_testing_mode_refusal(generation, "existing"))
        self.assertIsNone(
            fresh_testing_mode_refusal(generation, "existing", publication=publication)
        )
        self.assertIsNotNone(
            fresh_testing_mode_refusal(generation, "upstream_restore", publication=publication)
        )
        for unproved in (
            generation.model_copy(update={"completed_at": ""}),
            generation.model_copy(update={"target_id": "another-target"}),
        ):
            self.assertIsNotNone(
                fresh_testing_mode_refusal(unproved, "existing", publication=publication)
            )
        exercise_published_copy_maintenance(self, fixture)


class PublishedTestingCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = publication_tests.TestingPublicationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.prepare()
        self.publication = self.fixture.publish()
        self.store = self.fixture.store

    def select(self) -> TestingPublicationRecord | None:
        return published_testing_copy(
            self.store,
            product=self.fixture.candidate.product,
            context=self.fixture.candidate.context,
        )

    def test_maintenance_reads_only_local_or_explicit_nonproduction_credentials(self) -> None:
        versions: dict[str, str] = {}
        for key, classification in (("SAFE_TEST_TOKEN", "testing"), ("LIVE_TOKEN", "prod_only")):
            result = secrets.write_secret_value(
                record_store=self.store,
                scope="context_instance",
                integration=secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                name=key.lower(),
                plaintext_value=key.lower() + "-fixture",
                binding_key=key,
                context_name=self.fixture.candidate.context,
                instance_name="testing",
                actor="fixture",
            )
            binding = next(
                item
                for item in self.store.list_secret_bindings()
                if item.secret_id == result["secret_id"]
            )
            self.store.write_secret_binding(
                binding.model_copy(
                    update={"declared_secret_class": classification, "value_source": "supplied"}
                )
            )
            versions[key] = self.store.read_secret_record(result["secret_id"]).current_version_id
        read = PostgresRecordStore.read_secret_version

        def scoped(store: PostgresRecordStore, version: str) -> SecretVersion:
            if version == versions["LIVE_TOKEN"]:
                raise AssertionError("Excluded credentials cannot be read")
            return read(store, version)

        with patch.object(
            PostgresRecordStore, "read_secret_version", autospec=True, side_effect=scoped
        ):
            environment = resolve_testing_maintenance_environment(
                self.fixture.fixture.root, self.store, self.publication
            )
        self.assertEqual(environment.values["SAFE_TEST_TOKEN"], "safe_test_token-fixture")
        self.assertNotIn("LIVE_TOKEN", environment.values)

    def test_changed_local_password_version_requires_a_new_private_copy(self) -> None:
        binding = next(
            item
            for item in self.publication.plan.secret_read_set.bindings
            if item.binding_key == "ODOO_DB_PASSWORD" and item.instance == "testing"
        )
        record = self.store.read_secret_record(binding.secret_id)
        self.store.write_secret_record(
            record.model_copy(update={"current_version_id": "new-version"})
        )
        with self.assertRaises(ValueError):
            require_maintained_local_credentials(self.store, self.publication)
        # Custody is still readable, so rebuilding can install the new credential.
        self.assertEqual(self.select(), self.publication)

    def test_restored_copy_uses_native_publication_instead_of_legacy_bootstrap_receipts(
        self,
    ) -> None:
        fixture = publication_tests.TestingPublicationTests()
        fixture.source_mode = "upstream"
        fixture.setUp()
        try:
            fixture.prepare()
            fixture.publish()
            self.assertEqual(
                read_current_testing_generation(
                    fixture.store,
                    product=fixture.candidate.product,
                    context=fixture.candidate.context,
                    instance="testing",
                ),
                fixture.current().generation,
            )
        finally:
            fixture.doCleanups()

    def test_later_build_keeps_private_alias_and_never_publishes_a_host_port(self) -> None:
        document = yaml.safe_load(
            render_maintained_testing_copy(
                self.publication,
                image_reference="example.invalid/new-build@sha256:" + "d" * 64,
                hold_web=True,
            )
        )
        web = document["services"]["web"]
        self.assertFalse(web.get("ports"))
        self.assertTrue(
            any(
                self.fixture.candidate.namespace in network.get("aliases", [])
                for network in web["networks"].values()
                if isinstance(network, dict)
            )
        )
        self.assertFalse(
            any(key.startswith("traefik.http.routers.") for key in web.get("labels", {}))
        )

    def test_actual_ordinary_deploy_plan_accepts_an_adopted_empty_copy(self) -> None:
        app = self.fixture.fixture
        plan = build_odoo_stable_target_replacement_plan(
            control_plane_root=app.root,
            record_store=self.store,
            request=OdooStableTargetReplacementRequest(
                product=self.fixture.candidate.product,
                instance="testing",
                artifact_id=app.artifact.artifact_id,
                source_git_ref=app.artifact.source_commit,
            ),
            dokploy_request=lambda **_: [],
        )
        self.assertEqual(plan.plan_status, "ready", plan.blocker_codes)
        self.assertEqual(plan.expected_domain_hosts, ())
        self.assertIsNone(self.fixture.current().generation)
        self.assertEqual(self.select(), self.publication)

    def test_next_ordinary_deploy_keeps_the_adopted_route_storage_and_credentials(self) -> None:
        exercise_published_copy_maintenance(self, self.fixture)

    def test_empty_copy_remains_selected_across_later_build_inventory(self) -> None:
        self.assertIsNone(self.fixture.current().generation)
        inventory = self.fixture.current().inventory
        assert inventory is not None and inventory.runtime_identity is not None
        self.store.write_environment_inventory(
            inventory.model_copy(
                update={
                    "deployment_record_id": "subsequent-build",
                    "runtime_identity": inventory.runtime_identity.model_copy(
                        update={
                            "deployment_record_id": "subsequent-build",
                            "artifact_id": "subsequent-artifact",
                        }
                    ),
                }
            )
        )
        self.assertEqual(self.select(), self.publication)

    def test_missing_provenance_cannot_silently_fall_back_to_legacy_deploy(self) -> None:
        binding = self.fixture.current().binding
        assert binding is not None
        self.store.write_route_binding_record(
            binding.model_copy(
                update={"source": binding.source.model_copy(update={"source_versions": {}})}
            )
        )
        with self.assertRaises(ValueError):
            self.select()

    def test_changed_physical_storage_cannot_keep_using_original_copy_authority(self) -> None:
        runtime = self.fixture.current().runtime
        assert runtime is not None
        self.store.write_runtime_environment_record(
            runtime.model_copy(
                update={"env": {**runtime.env, "ODOO_DATA_VOLUME": "unqualified-volume"}}
            )
        )
        with self.assertRaises(ValueError):
            self.select()
