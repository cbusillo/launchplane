import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from sqlalchemy import text, update

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.stored_record_snapshot import SNAPSHOT_MAX_AGE_SECONDS
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import GitHubActionsPolicyRule, LaunchplaneAuthzPolicy
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
        )
        self.store.seed_authz_policy_if_absent(
            LaunchplaneAuthzPolicyRecord(
                record_id="policy",
                source="private",
                updated_at=datetime.now(timezone.utc).isoformat(),
                policy=self.policy,
                audit={"token": "plaintext-sentinel"},
            )
        )
        with self.store._engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            connection.execute(text("INSERT INTO alembic_version VALUES ('test-revision')"))

    def test_export_preserves_historical_shape_scrubs_values_and_does_not_write(self) -> None:
        payload = product_profile_payload()
        payload["preview"]["override_env"] = {"TOKEN": "plaintext-sentinel"}  # type: ignore[index]
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(payload)
        )
        before = self.store.list_authz_policy_records()[0].model_dump_json()
        snapshot = self.store.export_stored_record_snapshot()
        encoded = snapshot.model_dump_json()
        self.assertNotIn("plaintext-sentinel", encoded)
        self.assertNotIn("private/example", encoded)
        self.assertNotIn("sellyouroutboard.com", encoded)
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
        with patch(
            "control_plane.service_auth.exclusively_instance_scoped_authz_actions",
            return_value=frozenset({"live_target_runtime.plan"}),
        ):
            with self.assertRaises(ValidationError):
                check_restored_records(self.store, state_dir=self.root)

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
        with patch("control_plane.stored_record_snapshot.SNAPSHOT_MAX_BYTES", 1):
            with self.assertRaises(ValueError):
                self.store.export_stored_record_snapshot()


if __name__ == "__main__":
    unittest.main()
