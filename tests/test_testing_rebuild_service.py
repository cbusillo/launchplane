"""The ordinary replacement action admits a native rebuild only on testing."""

import unittest
import yaml
from unittest.mock import patch
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
