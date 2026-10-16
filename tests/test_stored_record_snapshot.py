import asyncio
from datetime import datetime, timedelta, timezone
import json
from itertools import chain, repeat
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from sqlalchemy import insert, select, text, update

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

    def test_lifecycle_schedules_and_preview_suffix_remain_readable_after_scrubbing(self) -> None:
        from control_plane.stored_record_snapshot_codec import iter_snapshot_rows

        payload = product_profile_payload()
        suffix = "-private-preview"
        expression = "17 4 * * 2"
        schedule = {
            "name": "private-maintenance",
            "command": "private-command --secret-option",
            "cron_expression": expression,
        }
        preview = payload["preview"]
        lanes = payload["lanes"]
        assert isinstance(preview, dict) and isinstance(lanes, list)
        preview["app_name_suffix"] = suffix
        for target in (*lanes, preview):
            target["generic_web_extensions"] = {"schedules": [schedule]}
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(payload)
        )
        for version in (1, 2):
            with self.subTest(format_version=version):
                snapshot = self.store.export_stored_record_snapshot(format_version=version)
                raw = next(
                    row["payload"]
                    for family, row in iter_snapshot_rows(snapshot)
                    if family == "launchplane_product_profiles"
                )
                profile = LaunchplaneProductProfileRecord.model_validate(raw)
                encoded = json.dumps(raw)
                for private in (suffix, expression, schedule["name"], schedule["command"]):
                    self.assertNotIn(private, encoded)
                self.assertTrue(profile.preview.app_name_suffix.startswith("-"))
                self.assertEqual(
                    profile.preview.generic_web_extensions.schedules,
                    profile.lanes[0].generic_web_extensions.schedules,
                )

    def test_invalid_historical_schedule_and_suffix_still_fail_after_scrubbing(self) -> None:
        from control_plane.stored_record_snapshot import _Scrubber

        payload = product_profile_payload()
        preview = payload["preview"]
        lanes = payload["lanes"]
        assert isinstance(preview, dict) and isinstance(lanes, list)
        preview["app_name_suffix"] = "invalid-suffix"
        lanes[0]["generic_web_extensions"] = {
            "schedules": [{"name": "old", "command": "old", "cron_expression": "invalid"}]
        }
        schema = LaunchplaneProductProfileRecord.model_json_schema()
        scrubbed = _Scrubber().scrub(payload, schema, schema)
        with self.assertRaises(ValidationError) as result:
            LaunchplaneProductProfileRecord.model_validate(scrubbed)
        locations = {tuple(item["loc"]) for item in result.exception.errors()}
        self.assertIn(("preview", "app_name_suffix"), locations)
        self.assertIn(("lanes", 0, "generic_web_extensions", "schedules", 0), locations)

    def test_aggregate_product_scopes_include_history_without_current_profiles(self) -> None:
        from sqlalchemy import insert
        from control_plane.storage.postgres import LaunchplaneOwnerAcceptanceSubjectSequenceRow

        with self.store._engine.begin() as connection:
            connection.execute(
                insert(LaunchplaneOwnerAcceptanceSubjectSequenceRow).values(
                    repository_id="historical-repository",
                    pr_number=1,
                    product="historical-product",
                    system="runtime",
                    owner_action="review",
                    environment="testing",
                    last_sequence=1,
                )
            )
        profile = LaunchplaneProductProfileRecord.model_validate(product_profile_payload())
        self.store.write_product_profile_record(profile)
        self.assertTrue(
            {profile.product, "historical-product"}.issubset(
                self.store.list_recorded_product_scopes()
            )
        )

    def test_role_export_authorizes_legacy_profiles_using_the_store_reader(self) -> None:
        from control_plane.contracts.product_health_monitoring_migration import (
            downgrade_product_profile_health_monitoring_payload,
        )
        from control_plane.storage.postgres import LaunchplaneProductProfileRow

        profile = LaunchplaneProductProfileRecord.model_validate(product_profile_payload())
        self.store.write_product_profile_record(profile)
        legacy = downgrade_product_profile_health_monitoring_payload(
            profile.model_dump(mode="json")
        )
        with self.store._engine.begin() as connection:
            connection.execute(update(LaunchplaneProductProfileRow).values(payload=legacy))
        expected = self.store.read_product_profile_record(profile.product)
        observed: list[LaunchplaneProductProfileRecord] = []

        def authorize(
            profiles: tuple[LaunchplaneProductProfileRecord, ...], _inventory: tuple[object, ...]
        ) -> bool:
            observed.extend(profiles)
            return profiles == (expected,)

        snapshot = self.store.export_stored_record_snapshot(authorize_records=authorize)
        self.assertEqual(observed, [expected])
        self.assertEqual(self.store.read_product_profile_record(profile.product), expected)
        raw = snapshot.tables[LaunchplaneProductProfileRow.__tablename__][0]["payload"]
        self.assertIn("public_ingress_monitoring", raw["lanes"][0])

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
        with self.store._engine.connect() as connection:
            source_sizes = [
                len(json.dumps(dict(row)).encode())
                for row in connection.execute(
                    select(LaunchplaneAuthzPolicyRow.__table__)
                ).mappings()
            ]
        byte_bound = max(source_sizes) + 1024
        self.assertGreater(sum(source_sizes), byte_bound)
        # Each row fits, and the scrubbed export fits, but cumulative raw data does not.
        with patch("control_plane.stored_record_snapshot.SNAPSHOT_MAX_BYTES", byte_bound):
            snapshot = self.store.export_stored_record_snapshot()
        rows = snapshot.tables[LaunchplaneAuthzPolicyRow.__tablename__]
        self.assertEqual({row["status"] for row in rows}, {"active", "superseded"})
        self.assertEqual(len(rows), 2)
        self.assertLess(len(snapshot.model_dump_json().encode()), byte_bound)
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
        # Use the same synthetic salt when measuring and exercising the bound;
        # random numeric pseudonym widths must not change between these exports.
        with patch(
            "control_plane.stored_record_snapshot.secrets.token_bytes", return_value=b"test"
        ):
            snapshot = self.store.export_stored_record_snapshot()
            row = snapshot.tables[LaunchplaneAuthzPolicyRow.__tablename__][0]
            byte_bound = len(json.dumps(row, separators=(",", ":")).encode()) - 1
            with patch("control_plane.stored_record_snapshot.SNAPSHOT_MAX_BYTES", byte_bound):
                with self.assertRaises(ValueError) as raised:
                    self.store.export_stored_record_snapshot()
        diagnostics = snapshot_failure_diagnostics(raised.exception)
        self.assertEqual(diagnostics["reason"], "snapshot_byte_limit")
        self.assertLess(diagnostics["source_row_bytes"], byte_bound)
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

        with self.assertRaises(ValidationError) as raised:
            LaunchplaneAuthzPolicyRecord.model_validate(
                {"unexpected-private-key": "plaintext-sentinel"}
            )
        diagnostics = json.dumps(snapshot_failure_diagnostics(raised.exception))
        self.assertNotIn("plaintext-sentinel", diagnostics)
        self.assertNotIn("unexpected-private-key", diagnostics)

    def test_expired_export_budget_reports_time_without_record_inputs(self) -> None:
        from control_plane.stored_record_snapshot import snapshot_failure_diagnostics

        with patch(
            "control_plane.stored_record_snapshot.monotonic",
            side_effect=chain([0], repeat(1_000_000)),
        ):
            with self.assertRaises(ValueError) as raised:
                self.store.export_stored_record_snapshot()
        diagnostics = snapshot_failure_diagnostics(raised.exception)
        self.assertEqual(diagnostics["reason"], "snapshot_time_limit")
        self.assertEqual(diagnostics["rows_exported"], 0)
        self.assertNotIn("plaintext-sentinel", json.dumps(diagnostics))

    def test_database_statement_timeout_reports_reason_without_sql_or_inputs(self) -> None:
        from psycopg.errors import QueryCanceled
        from sqlalchemy.exc import OperationalError
        from control_plane.stored_record_snapshot import snapshot_failure_diagnostics

        error = OperationalError(
            "private-sql", {"private-key": "plaintext-sentinel"}, QueryCanceled("private-value")
        )
        diagnostics = snapshot_failure_diagnostics(error)
        self.assertEqual(diagnostics["reason"], "snapshot_statement_canceled")
        self.assertNotIn("private", json.dumps(diagnostics))
        self.assertNotIn("plaintext-sentinel", json.dumps(diagnostics))

    def test_subsecond_statement_budget_is_preserved_and_never_disabled(self) -> None:
        from control_plane.stored_record_snapshot import _statement_timeout_ms

        for remaining in (1, 20, 999):
            with self.subTest(remaining=remaining):
                self.assertEqual(_statement_timeout_ms(remaining), remaining)
        for remaining in (1000, 1999, 20_000):
            with self.subTest(remaining=remaining):
                self.assertGreater(_statement_timeout_ms(remaining), 0)
                self.assertLessEqual(_statement_timeout_ms(remaining), remaining)

    def test_numeric_identity_cache_does_not_retain_large_source_strings(self) -> None:
        import gc
        import tracemalloc
        from control_plane.stored_record_snapshot import _Scrubber

        gc.collect()
        already_tracing = tracemalloc.is_tracing()
        if not already_tracing:
            tracemalloc.start()
        try:
            baseline = tracemalloc.get_traced_memory()[0]
            scrubber = _Scrubber()
            source_value_size = 64 * 1024
            for index in range(64):
                value = str(index) + "0" * source_value_size
                self.assertEqual(scrubber.number(value), scrubber.number(value))
            del value
            retained = tracemalloc.get_traced_memory()[0] - baseline
            self.assertLess(retained, source_value_size * 4)
        finally:
            if not already_tracing:
                tracemalloc.stop()

    def test_compressed_history_exceeds_legacy_bound_without_losing_any_row(self) -> None:
        from control_plane.stored_record_snapshot_codec import iter_snapshot_rows

        with self.store._engine.begin() as connection:
            original = dict(
                connection.execute(select(LaunchplaneAuthzPolicyRow.__table__)).mappings().one()
            )
            for revision in range(2, 14):
                historical = dict(original)
                historical.update(
                    record_id=f"history-{revision}", revision=revision, status="superseded"
                )
                historical["payload"] = dict(
                    original["payload"], record_id=historical["record_id"], status="superseded"
                )
                # Field absence in just one old revision must reach the reader.
                historical["payload"]["policy"] = json.loads(
                    json.dumps(original["payload"]["policy"])
                )
                if revision == 5:
                    del historical["payload"]["policy"]["github_actions"][0]["instances"]
                connection.execute(insert(LaunchplaneAuthzPolicyRow).values(**historical))
        before = self.store.list_authz_policy_records()
        legacy = self.store.export_stored_record_snapshot()
        bound = len(legacy.model_dump_json().encode()) // 2
        with (
            patch("control_plane.stored_record_snapshot.SNAPSHOT_MAX_BYTES", bound),
            patch("control_plane.stored_record_snapshot_codec.SNAPSHOT_MAX_BYTES", bound),
        ):
            with self.assertRaisesRegex(ValueError, "snapshot_byte_limit"):
                self.store.export_stored_record_snapshot()
            compact = self.store.export_stored_record_snapshot(format_version=2)
        self.assertLess(len(compact.model_dump_json().encode()), bound)
        rows = list(iter_snapshot_rows(compact))
        self.assertEqual(sorted(row["revision"] for _, row in rows), list(range(1, 14)))
        old = next(row for _, row in rows if row["revision"] == 5)
        self.assertNotIn("instances", old["payload"]["policy"]["github_actions"][0])
        restored = PostgresRecordStore(database_url=_sqlite_database_url(self.root / "compact.db"))
        self.addCleanup(restored.close)
        restored.ensure_schema()
        with restored._engine.begin() as connection:
            for table, row in rows:
                self.assertEqual(table, LaunchplaneAuthzPolicyRow.__tablename__)
                connection.execute(insert(LaunchplaneAuthzPolicyRow).values(**row))
        check_restored_records(restored, state_dir=self.root)
        with patch(
            "control_plane.service_auth.exclusively_instance_scoped_authz_actions",
            return_value=frozenset({"live_target_runtime.plan"}),
        ):
            with self.assertRaises(ValidationError):
                check_restored_records(restored, state_dir=self.root)
        self.assertEqual(before, self.store.list_authz_policy_records())
        self.assertNotIn("plaintext-sentinel", json.dumps(rows))

    def test_compressed_route_negotiates_format_and_keeps_read_authority(self) -> None:
        from control_plane.contracts.stored_record_snapshot import StoredRecordSnapshotResponse
        from control_plane.stored_record_snapshot_codec import iter_snapshot_rows

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
                "/v1/service/stored-record-snapshot?format_version=2",
                headers={"Authorization": "Bearer valid-token"},
            )
        )
        self.assertEqual(response.status_code, 200, response.text)
        snapshot = StoredRecordSnapshotResponse.model_validate(response.json()).snapshot
        self.assertEqual(
            len(list(iter_snapshot_rows(snapshot))), len(self.store.list_authz_policy_records())
        )
        legacy = asyncio.run(
            request(
                app,
                "GET",
                "/v1/service/stored-record-snapshot",
                headers={"Authorization": "Bearer valid-token"},
            )
        )
        self.assertIn("tables", legacy.json()["snapshot"])
        denied = create_launchplane_fastapi_app(
            verifier=StubVerifier(identity(repository="other/repo")),
            authz_policy=self.policy,
            record_store_factory=lambda: self.store,
        )
        refused = asyncio.run(
            request(
                denied,
                "GET",
                "/v1/service/stored-record-snapshot?format_version=2",
                headers={"Authorization": "Bearer valid-token"},
            )
        )
        self.assertEqual(refused.status_code, 403)

    def test_compressed_truncation_counts_and_decoded_budgets_refuse(self) -> None:
        import base64
        from control_plane.stored_record_snapshot_codec import iter_snapshot_rows

        snapshot = self.store.export_stored_record_snapshot(format_version=2)
        validate_snapshot(snapshot, now=datetime.now(timezone.utc))
        invalid_count = snapshot.model_copy(
            update={"table_counts": dict(snapshot.table_counts, launchplane_authz_policies=2)}
        )
        with self.assertRaisesRegex(ValueError, "snapshot_incomplete_records"):
            list(iter_snapshot_rows(invalid_count))
        from sqlalchemy import create_engine, inspect
        from control_plane.stored_record_preflight import restore_snapshot

        empty_engine = create_engine(_sqlite_database_url(self.root / "invalid-compact.db"))
        self.addCleanup(empty_engine.dispose)
        with self.assertRaisesRegex(ValueError, "snapshot_incomplete_records"):
            restore_snapshot(empty_engine, invalid_count)
        self.assertEqual(inspect(empty_engine).get_table_names(), [])
        truncated = snapshot.model_copy(
            update={
                "rows_gzip_base64": base64.b64encode(
                    base64.b64decode(snapshot.rows_gzip_base64)[:-4]
                ).decode()
            }
        )
        with self.assertRaises(EOFError):
            list(iter_snapshot_rows(truncated))
        with patch(
            "control_plane.stored_record_snapshot_codec.SNAPSHOT_MAX_EXPANDED_BYTES",
            snapshot.expanded_bytes // 2,
        ):
            with self.assertRaisesRegex(ValueError, "snapshot_expanded_byte_limit"):
                list(iter_snapshot_rows(snapshot))
            with self.assertRaisesRegex(ValueError, "snapshot_expanded_byte_limit"):
                self.store.export_stored_record_snapshot(format_version=2)
        row_bytes = len(next(iter_snapshot_rows(snapshot))[1]["payload"]["policy_sha256"])
        with patch("control_plane.stored_record_snapshot_codec.SNAPSHOT_MAX_BYTES", row_bytes):
            with self.assertRaises(ValueError):
                self.store.export_stored_record_snapshot(format_version=2)

    def test_train_run_history_samples_latest_without_deleting_source_records(self) -> None:
        earlier = _merge_train_run_record()
        later = _merge_train_run_record(
            recorded_at=(
                datetime.fromisoformat(earlier.recorded_at.replace("Z", "+00:00"))
                + timedelta(days=1)
            ).isoformat()
        )
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
