import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from sqlalchemy import insert, text, update

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.stored_record_snapshot import SNAPSHOT_MAX_AGE_SECONDS
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import (
    GitHubActionsPolicyRule,
    GitHubHumanPolicyRule,
    LaunchplaneAuthzPolicy,
)
from control_plane.storage.postgres import LaunchplaneAuthzPolicyRow, PostgresRecordStore
from control_plane.stored_record_preflight import check_restored_records, validate_snapshot
from tests.support.auth import StubVerifier, identity
from tests.support.http import request
from tests.support.profiles import product_profile_payload
from tests.support.stores import _sqlite_database_url
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.test_postgres_store import _inventory_record, _merge_train_run_record


class StoredRecordSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(self.root / "snapshot.db")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            github_actions=(
                GitHubActionsPolicyRule(
                    repository="private/example",
                    workflow_refs=("private/example/.github/workflows/deploy.yml@refs/heads/main",),
                    products=("launchplane",),
                    contexts=("launchplane",),
                    actions=("launchplane_service.read", "live_target_runtime.plan"),
                ),
            ),
            github_humans=(GitHubHumanPolicyRule(github_ids=(987654321,), roles=("admin",)),),
        )
        self.store.seed_authz_policy_if_absent(
            LaunchplaneAuthzPolicyRecord(
                record_id="policy",
                source="private",
                updated_at=datetime.now(timezone.utc).isoformat(),
                policy=self.policy,
                audit={"token": "plaintext-sentinel", "secret_number": 987654321},
            )
        )
        with self.store._engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            connection.execute(text("INSERT INTO alembic_version VALUES ('test-revision')"))

    def test_export_preserves_historical_shape_scrubs_values_and_does_not_write(self) -> None:
        payload = product_profile_payload()
        preview = payload["preview"]
        assert isinstance(preview, dict)
        preview["override_env"] = {
            "TOKEN": "plaintext-sentinel",
            "actions": "plaintext-sentinel",
            "driver_id": "plaintext-sentinel",
        }
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(payload)
        )
        before = self.store.list_authz_policy_records()[0].model_dump_json()
        snapshot = self.store.export_stored_record_snapshot()
        encoded = snapshot.model_dump_json()
        self.assertNotIn("plaintext-sentinel", encoded)
        self.assertNotIn("private/example", encoded)
        self.assertNotIn(str(payload["lanes"][0]["base_url"]), encoded)  # type: ignore[index]
        self.assertNotIn("987654321", encoded)
        raw = snapshot.tables[LaunchplaneAuthzPolicyRow.__tablename__][0]["payload"]
        self.assertIn("live_target_runtime.plan", raw["policy"]["github_actions"][0]["actions"])
        LaunchplaneAuthzPolicyRecord.model_validate(raw)
        profile = snapshot.tables["launchplane_product_profiles"][0]["payload"]
        LaunchplaneProductProfileRecord.model_validate(profile)
        self.assertEqual(before, self.store.list_authz_policy_records()[0].model_dump_json())

    def test_raw_omitted_instances_survive_export_and_new_validation_fails_rehearsal(self) -> None:
        with self.store._engine.begin() as connection:
            payload = self.store.list_authz_policy_records()[0].model_dump(mode="json")
            del payload["policy"]["github_actions"][0]["instances"]
            connection.execute(update(LaunchplaneAuthzPolicyRow).values(payload=payload))
        snapshot = self.store.export_stored_record_snapshot()
        rule = snapshot.tables["launchplane_authz_policies"][0]["payload"]["policy"][
            "github_actions"
        ][0]
        self.assertNotIn("instances", rule)
        restored = PostgresRecordStore(database_url=_sqlite_database_url(self.root / "restored.db"))
        self.addCleanup(restored.close)
        restored.ensure_schema()
        from control_plane.stored_record_snapshot import SNAPSHOT_RECORD_TYPES
        from sqlalchemy import insert

        with restored._engine.begin() as connection:
            for row_type, _ in SNAPSHOT_RECORD_TYPES:
                rows = snapshot.tables[row_type.__tablename__]
                if rows:
                    connection.execute(insert(row_type), rows)
        check_restored_records(restored, state_dir=self.root)
        with patch(
            "control_plane.service_auth.exclusively_instance_scoped_authz_actions",
            return_value=frozenset({"live_target_runtime.plan"}),
        ):
            with self.assertRaises(ValidationError):
                check_restored_records(restored, state_dir=self.root)

    def test_missing_active_policy_and_stale_or_partial_snapshots_refuse(self) -> None:
        snapshot = self.store.export_stored_record_snapshot()
        now = datetime.now(timezone.utc)
        validate_snapshot(snapshot, now=now)
        with self.assertRaises(ValueError):
            validate_snapshot(snapshot, now=now + timedelta(seconds=SNAPSHOT_MAX_AGE_SECONDS + 1))
        snapshot.tables.pop("launchplane_inventory")
        with self.assertRaises(ValueError):
            validate_snapshot(snapshot, now=now)
        with self.store._engine.begin() as connection:
            connection.execute(update(LaunchplaneAuthzPolicyRow).values(status="superseded"))
        with self.assertRaises(ValueError):
            self.store.export_stored_record_snapshot()

    def test_route_reuses_service_read_authority_and_returns_only_scrubbed_data(self) -> None:
        caller = identity(
            repository="private/example",
            workflow_ref="private/example/.github/workflows/deploy.yml@refs/heads/main",
        )
        app = create_launchplane_fastapi_app(
            verifier=StubVerifier(caller),
            authz_policy=self.policy,
            record_store_factory=lambda: self.store,
        )
        response = asyncio.run(
            request(
                app,
                "GET",
                "/v1/service/stored-record-snapshot",
                headers={"Authorization": "Bearer valid-token"},
            )
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("plaintext-sentinel", json.dumps(response.json()))
        denied = create_launchplane_fastapi_app(
            verifier=StubVerifier(identity(repository="other/repo")),
            authz_policy=self.policy,
            record_store_factory=lambda: self.store,
        )
        response = asyncio.run(
            request(
                denied,
                "GET",
                "/v1/service/stored-record-snapshot",
                headers={"Authorization": "Bearer valid-token"},
            )
        )
        self.assertEqual(response.status_code, 403)

    def test_candidate_constructs_real_application_with_current_stored_records(self) -> None:
        self.assertGreater(check_restored_records(self.store, state_dir=self.root), 0)

    def test_lane_and_train_payloads_remain_loadable_after_scrubbing(self) -> None:
        self.store.write_environment_inventory(_inventory_record())
        self.store.write_merge_train_policy_record(build_test_merge_train_policy_record())
        self.store.write_merge_train_run_record(_merge_train_run_record())
        snapshot = self.store.export_stored_record_snapshot()
        from control_plane.stored_record_snapshot import SNAPSHOT_RECORD_TYPES

        for row_type, model_type in SNAPSHOT_RECORD_TYPES:
            for row in snapshot.tables[row_type.__tablename__]:
                model_type.model_validate(row["payload"])

    def test_export_bounds_refuse_instead_of_truncating(self) -> None:
        with patch("control_plane.stored_record_snapshot.SNAPSHOT_MAX_ROWS", 0):
            with self.assertRaises(ValueError):
                self.store.export_stored_record_snapshot()

    def test_large_source_history_exports_every_row_within_scrubbed_bound(self) -> None:
        from control_plane.contracts.stored_record_snapshot import SNAPSHOT_MAX_BYTES

        with self.store._engine.begin() as connection:
            payload = self.store.list_authz_policy_records()[0].model_dump(mode="json")
            payload["audit"] = {"private_note": "plaintext-sentinel" * 1000}
            connection.execute(update(LaunchplaneAuthzPolicyRow).values(payload=payload))
            historical = dict(
                payload, record_id="historical-policy", status="superseded", revision=2
            )
            connection.execute(
                insert(LaunchplaneAuthzPolicyRow).values(
                    record_id=historical["record_id"],
                    status=historical["status"],
                    source=historical["source"],
                    revision=historical["revision"],
                    updated_at=historical["updated_at"],
                    policy_sha256=historical["policy_sha256"],
                    payload=historical,
                )
            )
        # Each row fits, and the scrubbed export fits, but cumulative raw data does not.
        with patch("control_plane.stored_record_snapshot.SNAPSHOT_MAX_BYTES", 25_000):
            snapshot = self.store.export_stored_record_snapshot()
        rows = snapshot.tables[LaunchplaneAuthzPolicyRow.__tablename__]
        self.assertEqual({row["status"] for row in rows}, {"active", "superseded"})
        self.assertEqual(len(rows), 2)
        self.assertLess(len(snapshot.model_dump_json().encode()), SNAPSHOT_MAX_BYTES)
        self.assertNotIn("plaintext-sentinel", snapshot.model_dump_json())

    def test_oversized_source_row_refuses_with_safe_family_and_budget_diagnostics(self) -> None:
        from control_plane.stored_record_snapshot import snapshot_failure_diagnostics

        with patch("control_plane.stored_record_snapshot.SNAPSHOT_MAX_BYTES", 1):
            with self.assertRaises(ValueError) as raised:
                self.store.export_stored_record_snapshot()
        diagnostics = snapshot_failure_diagnostics(raised.exception)
        self.assertEqual(diagnostics["reason"], "snapshot_source_row_byte_limit")
        self.assertEqual(diagnostics["record_family"], LaunchplaneAuthzPolicyRow.__tablename__)
        self.assertGreater(diagnostics["source_row_bytes"], 1)
        self.assertEqual(diagnostics["rows_exported"], 0)
        self.assertNotIn("plaintext-sentinel", json.dumps(diagnostics))

    def test_scrubbed_output_growth_refuses_before_accumulating_more_rows(self) -> None:
        from control_plane.stored_record_snapshot import snapshot_failure_diagnostics

        with self.store._engine.begin() as connection:
            payload = self.store.list_authz_policy_records()[0].model_dump(mode="json")
            payload["audit"] = {"private_numbers": [1] * 500}
            connection.execute(update(LaunchplaneAuthzPolicyRow).values(payload=payload))
        with patch("control_plane.stored_record_snapshot.SNAPSHOT_MAX_BYTES", 4_000):
            with self.assertRaises(ValueError) as raised:
                self.store.export_stored_record_snapshot()
        diagnostics = snapshot_failure_diagnostics(raised.exception)
        self.assertEqual(diagnostics["reason"], "snapshot_byte_limit")
        self.assertLess(diagnostics["source_row_bytes"], 4_000)
        self.assertEqual(diagnostics["rows_exported"], 0)

    def test_legacy_dictionary_keys_and_policy_key_relationship_survive(self) -> None:
        from control_plane.stored_record_snapshot import _Scrubber

        original = {
            "public_ingress_monitoring": {"enabled": False, "require_runtime_identity": True}
        }
        scrubbed = _Scrubber().scrub(original, {}, {})
        self.assertEqual(scrubbed, original)
        self.store.write_merge_train_run_record(_merge_train_run_record())
        snapshot = self.store.export_stored_record_snapshot()
        run = snapshot.tables["launchplane_merge_train_runs"][0]["payload"]
        self.assertEqual(run["policy_key"], f"{run['repository']}:{run['base_branch']}")
        self.assertEqual(run["status"], _merge_train_run_record().status)

    def test_startup_with_active_reader_failure_cannot_pass(self) -> None:
        original_reader = self.store.list_authz_policy_records

        def reader(**kwargs: object) -> object:
            if kwargs.get("status") == "active":
                return (LaunchplaneAuthzPolicyRecord.model_validate({}),)
            return original_reader()

        with patch.object(self.store, "list_authz_policy_records", side_effect=reader):
            with self.assertRaises(ValueError):
                check_restored_records(self.store, state_dir=self.root)

    def test_failure_diagnostics_exclude_validation_inputs(self) -> None:
        from control_plane.stored_record_snapshot import snapshot_failure_diagnostics

        try:
            LaunchplaneAuthzPolicyRecord.model_validate(
                {"unexpected-private-key": "plaintext-sentinel"}
            )
        except ValidationError as error:
            diagnostics = json.dumps(snapshot_failure_diagnostics(error))
        self.assertNotIn("plaintext-sentinel", diagnostics)
        self.assertNotIn("unexpected-private-key", diagnostics)

    def test_train_run_history_samples_latest_without_deleting_source_records(self) -> None:
        earlier = _merge_train_run_record(recorded_at="2026-05-09T02:05:00Z")
        later = _merge_train_run_record(recorded_at="2026-05-10T02:05:00Z")
        later = later.model_copy(update={"run_id": "later"})
        self.store.write_merge_train_run_record(earlier)
        self.store.write_merge_train_run_record(later)
        snapshot = self.store.export_stored_record_snapshot()
        runs = snapshot.tables["launchplane_merge_train_runs"]
        self.assertEqual([row["recorded_at"] for row in runs], [later.recorded_at])
        self.assertEqual(len(self.store.list_merge_train_run_records()), 2)
        with patch("control_plane.stored_record_snapshot.SNAPSHOT_MAX_BYTES", 1):
            with self.assertRaises(ValueError):
                self.store.export_stored_record_snapshot()


if __name__ == "__main__":
    unittest.main()
