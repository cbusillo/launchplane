from contextlib import closing
import unittest

from sqlalchemy import text, update
from sqlalchemy.exc import OperationalError

from control_plane.storage.postgres import (
    LaunchplaneOdooStableTargetReplacementOperationRow,
    PostgresRecordStore,
)
from tests.test_postgres_integration import _head_postgres_database
from tests.test_testing_replacement_resources import TestingReplacementResourceFixture


class NativeTestingRetentionPostgresTests(unittest.TestCase):
    def test_cleanup_excludes_original_resume_and_an_existing_executor(self) -> None:
        with _head_postgres_database() as url:
            fixture = TestingReplacementResourceFixture(self, database_url=url)
            store = fixture.store
            operation = fixture.operation
            with closing(PostgresRecordStore(database_url=url)) as second:
                with store.testing_candidate_retention_guard(operation.operation_id) as check:
                    check()
                    with second._session_factory() as session:
                        session.execute(text("SET LOCAL lock_timeout = '250ms'"))
                        with self.assertRaises(OperationalError) as blocked:
                            session.execute(
                                update(LaunchplaneOdooStableTargetReplacementOperationRow)
                                .where(
                                    LaunchplaneOdooStableTargetReplacementOperationRow.operation_id
                                    == operation.operation_id
                                )
                                .values(payload=operation.model_dump(mode="json"))
                            )
                        self.assertEqual(getattr(blocked.exception.orig, "sqlstate", ""), "55P03")
                with second.testing_replacement_execution_lock(operation.operation_id) as executor:
                    self.assertIsNotNone(executor)
                    with self.assertRaisesRegex(ValueError, "still has an executor"):
                        with store.testing_candidate_retention_guard(operation.operation_id):
                            self.fail("Cleanup entered an executing original")
                with store.testing_candidate_retention_guard(operation.operation_id) as check:
                    check()
