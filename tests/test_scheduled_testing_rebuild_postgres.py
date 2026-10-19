"""Focused native schedule/publication integration on isolated PostgreSQL databases."""

import unittest

from tests import test_scheduled_testing_rebuild as scheduled
from tests import test_odoo_testing_drill as drills
from tests.test_postgres_integration import _head_postgres_database


class ScheduledNativePostgresTests(unittest.TestCase):
    def fixture(self) -> scheduled.ScheduledNativeRebuildTests:
        fixture = scheduled.ScheduledNativeRebuildTests()
        fixture.database_url = self.enterContext(_head_postgres_database())
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_two_scheduled_copies_and_next_deploy_preserve_all_serving_authority(self) -> None:
        self.fixture().test_two_scheduled_native_restores_preserve_address_original_witnesses_and_production()

    def test_original_storage_recovery_precedes_fresh_capacity(self) -> None:
        self.fixture().test_interrupted_original_storage_is_read_before_capacity_without_reallocation()

    def test_changed_collected_generation_is_refused_by_locked_admission(self) -> None:
        fixture = drills.NativeTestingDrillTests()
        self.addCleanup(fixture.doCleanups)
        fixture.seed(self.enterContext(_head_postgres_database()), legacy_operations=False)
        fixture.test_locked_native_admission_refuses_a_generation_changed_after_collection()

    def test_attempt_scoped_down_admission_survives_expiry_but_new_positive_use_refuses(
        self,
    ) -> None:
        fixture = drills.NativeTestingDrillTests()
        self.addCleanup(fixture.doCleanups)
        fixture.seed(self.enterContext(_head_postgres_database()), legacy_operations=False)
        fixture.test_down_qualified_native_attempt_keeps_history_when_monitor_expires_but_new_use_wins()

    def test_reclaimed_native_attempt_collects_full_history_and_preserves_holds(self) -> None:
        fixture = drills.NativeTestingDrillTests()
        self.addCleanup(fixture.doCleanups)
        fixture.seed(self.enterContext(_head_postgres_database()), legacy_operations=False)
        fixture.test_native_attempt_retakes_full_history_on_reclaim_and_keeps_holds_live()

    def test_forward_read_after_preparing_prevents_new_login_cutover(self) -> None:
        self.fixture().test_new_login_after_route_preparing_is_checked_before_any_forward_write()
