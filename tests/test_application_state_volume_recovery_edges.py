from unittest.mock import patch

from control_plane.application_state_volume_retention import (
    checkpoint_state_volume_progress,
    read_state_volume_progress,
)
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.dokploy_target_adoption import PROJECT_CREATE_PATH
from tests import test_application_state_volume as volume_fixtures


class ApplicationStateVolumeRecoveryEdgeTests(volume_fixtures._ApplicationStateVolumeFixture):
    async def test_progress_accepts_heartbeat_renewal_but_fences_identity_and_expiry(self) -> None:
        fixture = self
        plan = await fixture.plan()
        persisted_phase = ""

        def checkpoint(
            store: PostgresRecordStore,
            *,
            reservation: LaunchplaneIdempotencyRecord,
            plan_id: str,
            phase: str,
        ) -> None:
            nonlocal persisted_phase
            if not persisted_phase:
                renewed = store.renew_mutation_reservation(
                    reservation=reservation, lease_seconds=600
                )
                self.assertEqual(renewed.status, "updated")
                self.assertIsNotNone(renewed.record)
                assert renewed.record is not None
                self.assertNotEqual(renewed.record.lease_expires_at, reservation.lease_expires_at)
                for changes in (
                    {"lease_owner": "another-executor"},
                    {"attempt": reservation.attempt + 1},
                    {"provider_target_key": "another-target"},
                    {"reconciliation_key": "another-plan"},
                ):
                    with self.subTest(changes=changes), self.assertRaises(ValueError):
                        checkpoint_state_volume_progress(
                            store,
                            reservation=reservation.model_copy(update=changes),
                            plan_id=plan_id,
                            phase=phase,
                        )
                with (
                    patch.object(
                        store,
                        "_database_mutation_timestamp",
                        return_value=renewed.record.lease_expires_at,
                    ),
                    self.assertRaises(ValueError),
                ):
                    checkpoint_state_volume_progress(
                        store, reservation=reservation, plan_id=plan_id, phase=phase
                    )
            checkpoint_state_volume_progress(
                store, reservation=reservation, plan_id=plan_id, phase=phase
            )
            persisted_phase = phase
            # Stop at the storage boundary: this injected renewal has no runner
            # heartbeat to receive its new snapshot and continue dispatching.
            raise OSError("Fixture: interrupted after persisting renewed progress")

        with patch(
            "control_plane.application_state_volume_retention.checkpoint_state_volume_progress",
            side_effect=checkpoint,
        ):
            result = await fixture.post()
        self.assertEqual(result.status_code, 409, result.text)
        self.assertTrue(persisted_phase)
        row = fixture.store.list_held_provider_target_reservations()[0]
        self.assertEqual(
            read_state_volume_progress(fixture.store, row.scope, plan["state_volume_plan_id"]),
            persisted_phase,
        )
        fixture.provider.assert_not_called()
        fixture.assert_production_unchanged()

    async def test_readback_without_application_identity_is_unavailable(self) -> None:
        fixture = self
        await fixture.plan()
        fixture.fail_before = PROJECT_CREATE_PATH
        interrupted = await fixture.post()
        self.assertEqual(interrupted.status_code, 409, interrupted.text)
        calls = fixture.provider.call_count
        with patch(
            "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup"
        ) as fetch:
            readback = await fixture.post(mode="readback")
        self.assertEqual(readback.status_code, 202, readback.text)
        self.assertEqual(readback.json()["result"]["configuration_evidence"], "unavailable")
        self.assertFalse(readback.json()["result"]["configuration_matches"])
        fetch.assert_not_called()
        self.assertEqual(fixture.provider.call_count, calls)
        fixture.assert_production_unchanged()

    async def test_changed_provider_host_is_unavailable_without_contacting_it(self) -> None:
        fixture = self
        await fixture.plan()
        result = await fixture.post()
        self.assertEqual(result.status_code, 202, result.text)
        calls = fixture.provider.call_count
        with (
            patch(
                "control_plane.application_state_volume.source.read_dokploy_config",
                return_value=("https://another-provider.example.invalid", "fixture-token"),
            ),
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup"
            ) as fetch,
        ):
            readback = await fixture.post(mode="readback")
        self.assertEqual(readback.status_code, 202, readback.text)
        self.assertEqual(readback.json()["result"]["configuration_evidence"], "unavailable")
        self.assertFalse(readback.json()["result"]["configuration_matches"])
        fetch.assert_not_called()
        self.assertEqual(fixture.provider.call_count, calls)
        fixture.assert_production_unchanged()
