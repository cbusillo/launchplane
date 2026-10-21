"""Plain fixtures release resources through their running unittest owner."""

import unittest
from unittest.mock import patch

from control_plane.storage.postgres import PostgresRecordStore
from tests import test_director_bounded_writes as bounded
from tests import test_testing_replacement_resources as resources


class FixtureCleanupTests(unittest.TestCase):
    def test_plain_fixtures_release_stores_and_directories_for_sync_and_async_owners(self) -> None:
        for fixture_type in (
            resources.TestingReplacementResourceFixture,
            bounded.DirectorBoundedWritesFixture,
        ):

            class Owner(unittest.TestCase):
                def runTest(self) -> None:
                    self.fixture = fixture_type(self)

            class AsyncOwner(unittest.IsolatedAsyncioTestCase, Owner):
                pass

            for owner in (Owner(), AsyncOwner()):
                with self.subTest(fixture=fixture_type.__name__, owner=type(owner).__name__):
                    closed: list[PostgresRecordStore] = []
                    original_close = PostgresRecordStore.close

                    def close(store: PostgresRecordStore) -> None:
                        closed.append(store)
                        original_close(store)

                    with patch.object(PostgresRecordStore, "close", close):
                        result = unittest.TestResult()
                        owner.run(result)
                    self.assertTrue(result.wasSuccessful(), (result.errors, result.failures))
                    self.assertIn(owner.fixture.store, closed)
                    self.assertFalse(owner.fixture.root.exists())
