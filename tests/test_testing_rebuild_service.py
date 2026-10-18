"""The ordinary replacement action admits a native rebuild only on testing."""

import unittest
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
