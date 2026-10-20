"""A completed fixture consumer must release its store before the next test runs."""

from importlib import import_module
from pathlib import Path
import unittest
from unittest.mock import patch

from sqlalchemy.engine import make_url

from control_plane.storage.postgres import PostgresRecordStore
from tests import test_testing_replacement_resources as resources


class FixtureCleanupTests(unittest.TestCase):
    def test_resource_consumers_close_their_stores(self) -> None:
        targets = (
            "tests.test_testing_path_check.TestingPathCheckHttpTests.test_default_testing_read_preserves_deploy_scope_without_copy_probes",
            "tests.test_testing_candidate_allocation.TestingCandidateTests.test_independent_storage_never_publishes_candidate_as_serving",
            "tests.test_testing_initial_generation.InitialTestingGenerationHttpTests.test_successful_legacy_lane_plans_without_changing_history_or_authority",
            "tests.test_testing_replacement_bootstrap.TestingBootstrapHttpRefusalTests.test_wrong_context_operation_production_and_client_never_reach_provider",
            "tests.test_testing_path_check.TestingResourceResumePathTests.test_stranded_replacement_does_not_require_initial_generation_use",
            "tests.test_odoo_upgrade_rehearsal.RehearsalScenario.test_new_record_and_source_inputs_leave_canonical_lane_authority_unchanged",
        )
        original_close = PostgresRecordStore.close
        for target in targets:
            with self.subTest(target=target):
                module, case, method = target.rsplit(".", 2)
                consumer = getattr(import_module(module), case)(method)
                stores: list[PostgresRecordStore] = []
                roots: list[Path] = []
                closed: list[PostgresRecordStore] = []

                def create_store(*, database_url: str) -> PostgresRecordStore:
                    store = PostgresRecordStore(database_url=database_url)
                    stores.append(store)
                    database = make_url(database_url).database
                    assert database is not None
                    roots.append(Path(database).parent)
                    return store

                def close(store: PostgresRecordStore) -> None:
                    closed.append(store)
                    original_close(store)

                with (
                    patch.object(PostgresRecordStore, "close", close),
                    patch.object(resources, "PostgresRecordStore", side_effect=create_store),
                ):
                    result = unittest.TestResult()
                    consumer.run(result)
                self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
                self.assertTrue(stores)
                for store in stores:
                    self.assertIn(store, closed)
                for root in roots:
                    self.assertFalse(root.exists(), root)
