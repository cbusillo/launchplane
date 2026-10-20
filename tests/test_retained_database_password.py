import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.live_target_runtime import (
    LiveTargetRuntimeError,
    LiveTargetRuntimeApplyEnvelope,
    apply_live_target_runtime_environment,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.stores import _seed_tracked_target_records, _sqlite_database_url


class RetainedDatabasePasswordSyncTests(unittest.TestCase):
    def test_literal_qualification_preserves_separation_authority_boundary(self) -> None:
        payload = dict(
            mode="dry-run",
            product="example",
            context="example",
            instance="testing",
            requalify_literal_keys=["SMTP_PASSWORD", "APP", "APP"],
        )
        envelope = LiveTargetRuntimeApplyEnvelope.model_validate(payload)
        self.assertEqual(envelope.requalify_literal_keys, ("APP", "SMTP_PASSWORD"))
        with self.assertRaises(ValueError):
            LiveTargetRuntimeApplyEnvelope.model_validate({**payload, "separation_only": True})
        with self.assertRaises(ValueError):
            LiveTargetRuntimeApplyEnvelope.model_validate(
                {**payload, "requalify_literal_keys": ["APP=value"]}
            )

    # 2026-10-17: OPW testing's local passwords were regenerated while its
    # database volume, created with the old password, stayed in place.
    def _sync(
        self,
        *,
        live_password: str,
        desired_password: str = "generated-local-password",
        literal_keys: tuple[str, ...] = (),
    ) -> tuple[list[object], Mock]:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_url = _sqlite_database_url(root / "records.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            self.addCleanup(store.close)
            store.write_runtime_environment_record(
                RuntimeEnvironmentRecord(
                    scope="instance",
                    context="example-site",
                    instance="testing",
                    env={"ODOO_DB_PASSWORD": desired_password},
                    updated_at="2026-10-17T00:00:00Z",
                    source_label="test",
                )
            )
            _seed_tracked_target_records(
                database_url=database_url,
                context="example-site",
                instance="testing",
                target_id="test-compose",
                target_type="compose",
                target_name="example-site-testing",
            )
            updates: list[object] = []
            deploy = Mock()
            with (
                patch(
                    "control_plane.live_target_runtime.dokploy_source.read_dokploy_config",
                    return_value=("host", "token"),
                ),
                patch(
                    "control_plane.live_target_runtime.dokploy_api.fetch_dokploy_target_payload",
                    return_value={
                        "name": "example-site-testing",
                        "env": f"ODOO_DB_PASSWORD={live_password}",
                    },
                ),
                patch(
                    "control_plane.live_target_runtime.dokploy_api.update_dokploy_target_env",
                    side_effect=lambda **kwargs: updates.append(kwargs),
                ),
            ):
                apply_live_target_runtime_environment(
                    control_plane_root=root,
                    database_url=database_url,
                    context_name="example-site",
                    instance_name="testing",
                    apply_changes=True,
                    deploy=True,
                    no_cache=False,
                    deploy_timeout_seconds=None,
                    deploy_trigger=deploy,
                    requalify_literal_keys=literal_keys,
                )
            return updates, deploy

    def test_sync_keeps_a_new_database_password_off_a_running_testing_database(self) -> None:
        with self.assertRaises(LiveTargetRuntimeError) as refusal:
            self._sync(live_password="retained-password")

        self.assertEqual(refusal.exception.code, "retained_database_credential_changed")
        self.assertNotIn("generated-local-password", str(refusal.exception))
        self.assertNotIn("retained-password", str(refusal.exception))

    def test_sync_delivers_a_testing_database_password_that_already_matches(self) -> None:
        updates, deploy = self._sync(live_password="generated-local-password")

        self.assertEqual(updates, [])
        deploy.assert_called_once()

    def test_equal_first_stage_password_cannot_be_reinterpreted_for_retained_database(self) -> None:
        with self.assertRaises(LiveTargetRuntimeError) as refusal:
            self._sync(
                live_password="password$NAME",
                desired_password="password$NAME",
                literal_keys=("ODOO_DB_PASSWORD",),
            )
        self.assertEqual(refusal.exception.code, "retained_database_credential_changed")


if __name__ == "__main__":
    unittest.main()
