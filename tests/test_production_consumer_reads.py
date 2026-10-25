import base64
import contextlib
import hashlib
import io
import json
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pymysql

from control_plane import production_consumer_reads as reader, odoo_runtime_reads as reads
from control_plane.odoo_runtime_source_prerequisites import SOURCE_ADDONS, PAYLOAD_ADDON
from control_plane.contracts.production_consumer_reads import (
    ConsumerTableRole,
    ConsumerReadEvidence,
    ProductionConsumerReview,
    ReviewedOdooConsumer,
)
from control_plane.contracts.runtime_identity import RUNTIME_IDENTITY_ENV_KEY
from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
from control_plane.production_consumer_programs import (
    CONSUMER_CONTAINER_PROGRAM,
    CONSUMER_TABLE_ROLES,
)
from tests.test_odoo_import_diagnostics import PRIVATE
from tests.test_odoo_runtime_source_prerequisites import PrerequisiteFixture


class ConsumerProgramFixture(PrerequisiteFixture):
    def setUp(self) -> None:
        super().setUp()
        self.env["PLATFORM_INSTANCE"] = "prod"
        self.fixture.identity = self.fixture.identity.model_copy(update={"instance": "prod"})
        self.env[RUNTIME_IDENTITY_ENV_KEY] = self.fixture.identity.model_dump_json()
        self.client_path = self.producer("repairshopr").with_name("repairshopr_sync_client.py")
        self.tables = {
            role: "synthetic_" + str(index)
            for index, role in enumerate(sorted(CONSUMER_TABLE_ROLES))
        }
        self.child_table = self.tables["INVOICE_LINE_ITEM_TABLE"]
        self.client_path.write_text(
            "\n".join(name + " = " + repr(table) for name, table in self.tables.items())
            + """
import pymysql
from odoo.addons.external_ids.utils.ssl_context import build_ssl_context
class RepairshoprSyncClient:
    def _open_connection(self):
        secure_context=build_ssl_context(self._settings.use_ssl,self._settings.ssl_verify)
        return pymysql.connect(host=self._settings.host,user=self._settings.user,password=self._settings.password,
            database=self._settings.database,port=self._settings.port,charset='utf8mb4',autocommit=True,ssl=secure_context)
"""
        )
        self.update_producer(
            "repairshopr",
            """
from dataclasses import dataclass
PARAMETER_KEYS=tuple('repairshopr.sync_db.'+key for key in ('host','user','password','name','port','use_ssl','ssl_verify','batch_size'))
OPTION_PARAMETER_KEYS=PARAMETER_KEYS[4:]
@dataclass(frozen=True)
class Settings:
    host:str
    user:str
    password:str
    database:str
    port:int
    use_ssl:bool
    ssl_verify:bool
    batch_size:int

def resolve_connection_options(values):
    return resolve_connection_settings(values)

def resolve_connection_settings(values):
    print(values)
    return Settings(*(values.get('repairshopr.sync_db.'+key,'') for key in ('host','user','password','name')),
        int(values.get('repairshopr.sync_db.port','3306')),values.get('repairshopr.sync_db.use_ssl','true').lower()=='true',
        values.get('repairshopr.sync_db.ssl_verify','true').lower()=='true',int(values.get('repairshopr.sync_db.batch_size','50')))
""",
        )
        self.values["repairshopr.sync_db.name"] = "synthetic_database"
        self.set_overrides({"repairshopr.sync_db.name": "synthetic_database"})
        self.tls_path = self.tenant / "addons/shared/external_ids/utils/ssl_context.py"
        self.tls_path.parent.mkdir(parents=True)
        self.tls_path.write_text(
            """
import ssl
def build_ssl_context(use_ssl,verify_ssl):
    print("""
            + repr(PRIVATE)
            + """)
    if not use_ssl:
        return None
    context=ssl.create_default_context()
    if not verify_ssl:
        context.check_hostname=False
        context.verify_mode=ssl.CERT_NONE
    return context
"""
        )
        self.mysql = MagicMock()
        self.mysql_cursor = self.mysql.cursor.return_value.__enter__.return_value
        self.mysql_cursor.execute.side_effect = self.mysql_execute
        self.mysql_cursor.fetchone.side_effect = self.mysql_one
        self.mysql_cursor.fetchmany.side_effect = self.mysql_many
        self.installed["external_ids"] = "installed"
        self.sql = ""
        self.mysql_queries: list[str] = []
        self.empty = False
        self.denied = False
        self.grants = "SELECT"
        self.wrong_database = False
        self.mysql_kwargs: dict[str, Any] = {}

    def mysql_execute(self, query: str) -> None:
        self.sql = query
        self.mysql_queries.append(query)
        if query.startswith("SELECT SLEEP"):
            if self.drift == "parameter":
                self.values["repairshopr.sync_db.host"] = "changed"
            if self.drift == "file":
                self.client_path.write_text("changed")
        if self.denied and query.startswith("SELECT 1 FROM") and self.child_table + "`" in query:
            raise pymysql.err.ProgrammingError(1142, PRIVATE)

    def mysql_one(self) -> Any:
        if self.sql == "SELECT DATABASE(), CONNECTION_ID()":
            return ("wrong" if self.wrong_database else "synthetic_database", 719)
        if self.sql.startswith("SELECT SLEEP"):
            return (0, "a" * 32)
        raise AssertionError(self.sql)

    def mysql_many(self, limit: int) -> Any:
        if self.sql == "SHOW GRANTS":
            return [("GRANT " + self.grants + " ON `synthetic_database`.* TO 'reader'@'%'",)]
        return () if self.empty else ((1,),)

    def mysql_connect(self, **kwargs: Any) -> Any:
        self.mysql_kwargs = kwargs
        return self.mysql

    def run_container(self, **changes: Any) -> list[dict[str, Any]]:
        files = {
            name: {
                str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (self.producer(name).parents[1]).rglob("*.py")
            }
            for name in SOURCE_ADDONS
        }
        payload_root = self.tenant / "addons/shared"
        files["payload"] = {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (payload_root / PAYLOAD_ADDON).rglob("*.py")
        }
        files["tls"] = {str(self.tls_path): hashlib.sha256(self.tls_path.read_bytes()).hexdigest()}
        desired_payload = json.loads(
            base64.b64decode(self.env["ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"])
        )
        desired_keys = {
            item["value"]["environment_variable"]
            for item in desired_payload["config_parameters"]
            if item["value"].get("environment_variable")
        }
        spec = {
            "identity": self.fixture.identity.model_dump(mode="json"),
            "files": files,
            "addons": {
                **{name: value[0] for name, value in SOURCE_ADDONS.items()},
                "tls": "external_ids",
            },
            "prefixes": {"repairshopr": "repairshopr.sync_db"},
            "option_fields": ["port", "use_ssl", "ssl_verify", "batch_size"],
            "payload_addon": PAYLOAD_ADDON,
            "payload_root": str(payload_root),
            "mounts": [],
            "nonce": "a" * 32,
            "desired_payload": desired_payload,
            "required_populated_roles": sorted(CONSUMER_TABLE_ROLES),
            "desired_env_sha256": {
                key: __import__("hmac")
                .new(bytes.fromhex("a" * 32), value.encode(), hashlib.sha256)
                .hexdigest()
                for key, value in self.env.items()
                if key in desired_keys
            },
            **changes,
        }
        self.assertNotIn(PRIVATE, json.dumps(spec))
        program = CONSUMER_CONTAINER_PROGRAM.replace(
            "__CONFIG_PATH__", repr(str(self.config))
        ).replace("__IDENTITY_KEY__", repr(RUNTIME_IDENTITY_ENV_KEY))
        with (
            patch.object(sys, "stdin", io.StringIO(json.dumps(spec))),
            patch.dict(os.environ, self.env, clear=True),
            patch("pathlib.Path", side_effect=self.path),
            patch.dict(sys.modules, {"psycopg2": SimpleNamespace(connect=self.pg_connect)}),
            patch.object(pymysql, "connect", side_effect=self.mysql_connect),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            exec(compile(program, "<consumer>", "exec"), {"__name__": "__main__"})
        self.assertNotIn(PRIVATE, out.getvalue())
        return [json.loads(line) for line in out.getvalue().splitlines()]


class ConsumerProgramTests(ConsumerProgramFixture):
    def test_actual_required_table_queries_close_and_rollback_with_applied_settings(self) -> None:
        frames = self.run_container()
        self.assertEqual(frames[-1]["result"], {"query": "verified", "populated": "verified"})
        self.assertEqual(frames[0], {"type": "connection", "connection_id": 719})
        self.assertTrue(any(self.child_table + "`" in sql for sql in self.mysql_queries))
        self.assertEqual(self.mysql_kwargs["database"], "synthetic_database")
        self.assertEqual(self.mysql_kwargs["password"], PRIVATE)
        self.assertFalse(self.mysql_kwargs["local_infile"])
        self.mysql.rollback.assert_called_once()
        self.mysql.close.assert_called_once()

    def test_denied_child_table_wrong_database_and_write_grants_never_qualify(self) -> None:
        for condition in ("denied", "wrong_database", "grants"):
            with self.subTest(condition=condition):
                setattr(self, condition, "ALL PRIVILEGES" if condition == "grants" else True)
                frames = self.run_container()
                self.assertEqual(frames[-1]["result"]["query"], "failed")
                self.assertEqual(len(frames), 1)
                setattr(self, condition, "SELECT" if condition == "grants" else False)
        self.assertEqual(self.mysql.close.call_count, self.mysql.rollback.call_count)

    def test_empty_tables_prove_query_capability_but_never_populated_data(self) -> None:
        self.empty = True
        self.assertEqual(
            self.run_container()[-1]["result"], {"query": "verified", "populated": "failed"}
        )

    def test_installed_tls_semantics_control_the_actual_connection(self) -> None:
        import ssl

        for use_ssl, verify in ((False, False), (True, False), (True, True)):
            with self.subTest(use_ssl=use_ssl, verify=verify):
                options = {
                    "repairshopr.sync_db.name": "synthetic_database",
                    "repairshopr.sync_db.use_ssl": str(use_ssl),
                    "repairshopr.sync_db.ssl_verify": str(verify),
                }
                self.values.update(options)
                self.set_overrides(options)
                self.assertEqual(self.run_container()[-1]["result"]["query"], "verified")
                context = self.mysql_kwargs["ssl"]
                if use_ssl:
                    self.assertEqual(
                        context.verify_mode, ssl.CERT_REQUIRED if verify else ssl.CERT_NONE
                    )
                    self.assertEqual(context.check_hostname, verify)
                else:
                    self.assertIsNone(context)

    def test_shadowed_tls_binding_and_transient_driver_failure_stay_unknown(self) -> None:
        original = self.client_path.read_text()
        self.client_path.write_text(original + "\nbuild_ssl_context=lambda *args:None\n")
        self.assertEqual(self.run_container()[-1]["result"]["query"], "unknown")
        self.client_path.write_text(original)
        self.mysql_cursor.execute.side_effect = pymysql.err.OperationalError(2013, PRIVATE)
        self.assertEqual(self.run_container()[-1]["result"]["query"], "unknown")
        self.mysql.close.assert_called_once()

    def test_missing_or_extra_digest_coverage_never_uses_stale_container_values(self) -> None:
        self.assertEqual(
            self.run_container(desired_env_sha256={})[-1]["result"]["query"], "unknown"
        )
        self.assertEqual(
            self.run_container(desired_env_sha256={"unknown": "f" * 64})[-1]["result"]["query"],
            "unknown",
        )
        self.mysql.close.assert_not_called()

    def test_connection_time_access_errors_are_failed_and_transport_is_unknown(self) -> None:
        for code, expected in ((1045, "failed"), (1049, "failed"), (2013, "unknown")):
            with (
                self.subTest(code=code),
                patch.object(
                    self, "mysql_connect", side_effect=pymysql.err.OperationalError(code, PRIVATE)
                ),
            ):
                self.assertEqual(self.run_container()[-1]["result"]["query"], expected)
        self.mysql.close.assert_not_called()

    def test_only_reviewed_population_roles_require_rows(self) -> None:
        self.empty = True
        self.assertEqual(
            self.run_container(required_populated_roles=[])[-1]["result"],
            {"query": "verified", "populated": "unknown"},
        )
        self.assertEqual(
            self.run_container(required_populated_roles=[ConsumerTableRole.INVOICE_LINE.value])[-1][
                "result"
            ],
            {"query": "verified", "populated": "failed"},
        )
        original = self.mysql_cursor.fetchmany.side_effect

        def rows(limit: int) -> Any:
            if self.child_table + "`" in self.sql:
                return ((1,),)
            return original(limit)

        self.mysql_cursor.fetchmany.side_effect = rows
        self.assertEqual(
            self.run_container(required_populated_roles=[ConsumerTableRole.INVOICE_LINE.value])[-1][
                "result"
            ],
            {"query": "verified", "populated": "verified"},
        )

    def test_managed_delivered_applied_mismatch_never_connects(self) -> None:
        for layer in ("applied", "managed"):
            with self.subTest(layer=layer):
                if layer == "applied":
                    self.values["repairshopr.sync_db.host"] = "different"
                    frames = self.run_container()
                    self.values["repairshopr.sync_db.host"] = "synthetic-host"
                else:
                    frames = self.run_container(desired_env_sha256={"missing": "f" * 64})
                self.assertEqual(frames[-1]["result"]["query"], "unknown")
        self.mysql.close.assert_not_called()

    def test_applied_settings_and_installed_source_drift_invalidate_success(self) -> None:
        original = self.client_path.read_bytes()
        for drift in ("parameter", "file"):
            with self.subTest(drift=drift):
                self.drift = drift
                self.assertEqual(self.run_container()[-1]["result"]["query"], "unknown")
                self.values["repairshopr.sync_db.host"] = "synthetic-host"
                self.client_path.write_bytes(original)
                self.drift = ""


class CoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.first = ReviewedOdooConsumer(product="consumer", environment="prod")
        self.second = ReviewedOdooConsumer(product="consumer", preview_id="deleted-preview")
        self.current = reader.PublishedConsumerSource(
            "snapshot",
            "b" * 64,
            "observation",
            ProductionConsumerReview(
                consumers=(self.first, self.second),
                coordination_reference="review-record",
                required_populated_roles=(ConsumerTableRole.INVOICE_LINE,),
            ),
            {},
            "server",
        )
        self.store = MagicMock()
        review_patch = patch.object(reader, "reviewed_consumers", return_value=self.current.review)
        review_patch.start()
        self.addCleanup(review_patch.stop)
        self.selection = MagicMock()
        self.selection.identity.context = "consumer-context"
        self.selection.identity.instance = "prod"

    def qualify(self, **kwargs: Any) -> Any:
        return reader.qualify_consumer_reads(
            store=self.store,
            root=Path("/synthetic"),
            product="source",
            context="source",
            can_read=kwargs.get("can_read", lambda *args: True),
        )

    def test_deleted_or_inaccessible_reviewed_member_stays_in_denominator(self) -> None:
        verified = ConsumerReadEvidence(
            consumer=self.first, status="verified", query="verified", destination="verified"
        )
        with (
            patch.object(reader, "current_source", return_value=self.current),
            patch.object(
                reader, "select_consumer", side_effect=[self.selection, FileNotFoundError()]
            ),
            patch.object(reader, "read_consumer", return_value=verified),
        ):
            result = self.qualify()
        self.assertEqual([item.consumer for item in result.consumers], [self.first, self.second])
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.consumers[1].status, "unknown")

    def test_no_authority_means_no_io_and_no_missing_members(self) -> None:
        with (
            patch.object(reader, "current_source", return_value=self.current),
            patch.object(reader, "select_consumer", return_value=self.selection),
            patch.object(reader, "read_consumer") as read,
        ):
            result = self.qualify(can_read=lambda *args: False)
        read.assert_not_called()
        self.assertEqual(len(result.consumers), len(self.current.review.consumers))
        self.assertEqual(result.status, "unknown")

    def test_missing_source_proof_does_not_drop_reviewed_consumers(self) -> None:
        with patch.object(reader, "current_source", side_effect=ValueError("source unknown")):
            result = self.qualify()
        self.assertEqual(
            tuple(item.consumer for item in result.consumers), self.current.review.consumers
        )
        self.assertEqual(result.status, "unknown")

    def test_busy_read_preserves_every_reviewed_member(self) -> None:
        with patch.object(reader, "_READ_SLOTS") as slots:
            slots.acquire.return_value = False
            result = self.qualify()
        self.assertTrue(result.busy)
        self.assertEqual(
            tuple(item.consumer for item in result.consumers), self.current.review.consumers
        )

    def test_consumers_run_concurrently_on_their_authorized_selection(self) -> None:
        import threading

        barrier = threading.Barrier(len(self.current.review.consumers))

        def read(store: Any, root: Path, member: Any, current: Any, selection: Any) -> Any:
            self.assertIs(selection, self.selection)
            barrier.wait(timeout=5)
            return ConsumerReadEvidence(
                consumer=member,
                status="verified",
                query="verified",
                populated="verified",
                destination="verified",
            )

        with (
            patch.object(reader, "current_source", return_value=self.current),
            patch.object(reader, "select_consumer", return_value=self.selection),
            patch.object(reader, "read_consumer", side_effect=read),
        ):
            result = self.qualify()
        self.assertEqual(result.status, "verified")

    def test_publication_drift_discards_every_consumer_proof(self) -> None:
        with (
            patch.object(
                reader,
                "current_source",
                side_effect=[self.current, replace(self.current, snapshot="changed")],
            ),
            patch.object(reader, "select_consumer", return_value=self.selection),
            patch.object(
                reader,
                "read_consumer",
                side_effect=lambda store, root, member, current, selection: ConsumerReadEvidence(
                    consumer=member, status="verified"
                ),
            ),
        ):
            result = self.qualify()
        self.assertTrue(all(item.status == "unknown" for item in result.consumers))

    def test_provider_denial_stops_instead_of_trying_another_reader(self) -> None:
        with (
            patch.object(reader, "current_source", return_value=self.current),
            patch.object(reader, "select_consumer", return_value=self.selection),
            patch.object(
                reader, "read_consumer", side_effect=NativeSourceAuthorizationDenied("denied")
            ) as read,
        ):
            with self.assertRaises(NativeSourceAuthorizationDenied):
                self.qualify()
        self.assertEqual(
            len(read.call_args_list), len({call.args[2] for call in read.call_args_list})
        )


class SourceCycleTests(unittest.TestCase):
    def setUp(self) -> None:
        from datetime import datetime, timedelta, timezone
        from tests.test_odoo_import_diagnostics import DiagnosticFixture
        from control_plane.contracts.sync_cycle_health import SyncCycleHealth

        self.now = datetime.now(timezone.utc)
        self.identity = DiagnosticFixture().identity
        self.review = ProductionConsumerReview(
            consumers=(ReviewedOdooConsumer(product="consumer", environment="prod"),),
            coordination_reference="review",
        )
        self.record = MagicMock()
        self.record.phase = "published"
        self.record.activation = {
            "request": {"consumer_review": self.review.model_dump(mode="json")}
        }
        self.record.publication = {
            "published_at": (self.now - timedelta(minutes=2)).isoformat(),
            "published_sha256": "b" * 64,
        }
        self.record.request.database_service = "database"
        self.record.request.application_service = "sync"
        self.record.model_dump.return_value = {"record": "source"}
        cycle = SyncCycleHealth(
            status="success",
            mode="incremental",
            cycle_id="a" * 32,
            cycle_started_at=(self.now - timedelta(minutes=1)).isoformat(),
            cycle_finished_at=(self.now - timedelta(seconds=30)).isoformat(),
            is_stale=False,
            next_cycle_overdue=False,
            freshness_status="fresh",
            stale_threshold_seconds=300,
        )
        self.target = SimpleNamespace(
            status="pass",
            runtime_identity_status="match",
            observed_runtime_identity=self.identity,
            sync=SimpleNamespace(cycle=cycle),
        )
        self.observation = SimpleNamespace(
            record_id="source-probe",
            observed_at=self.now.isoformat(),
            status="pass",
            purpose="probe",
            check_kind="private_http",
            check_name="source-check",
            expected_runtime_identity=self.identity,
            targets=(self.target,),
            model_dump=lambda **kwargs: {"probe": "source"},
        )
        self.store = MagicMock()
        self.store.list_public_ingress_observation_records.return_value = (self.observation,)
        self.store.read_product_profile_record.return_value = SimpleNamespace(
            lanes=(
                SimpleNamespace(
                    context="source",
                    instance="prod",
                    health_monitoring=SimpleNamespace(
                        checks=(
                            SimpleNamespace(
                                enabled=True,
                                kind="private_http",
                                require_runtime_identity=True,
                                private_endpoint_key="health",
                                name="source-check",
                            ),
                        )
                    ),
                ),
            )
        )
        self.patches = [
            patch.object(reader, "custody", return_value=self.record),
            patch.object(reader, "require_owned_publication_rows"),
            patch.object(
                reader,
                "authority_snapshot",
                return_value={
                    "inventory": {"runtime_identity": self.identity.model_dump(mode="json")},
                    "health_endpoints": [{"endpoint_key": "health"}],
                },
            ),
            patch.object(reader, "ProductionRecipientAdapter"),
            patch.object(reader, "runtime_values", return_value={}),
            patch.object(
                reader,
                "qualified_render",
                return_value={
                    "services": {
                        "database": {
                            "environment": {"MARIADB_DATABASE": "synthetic_database"},
                            "image": "database-image",
                        }
                    }
                },
            ),
            patch.object(reader, "published_mounts", return_value={}),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def current(self) -> Any:
        return reader.current_source(self.store, Path("/synthetic"), "source", "source")

    def test_current_successful_incremental_cycle_is_bound_to_publication(self) -> None:
        result = self.current()
        self.assertEqual(result.observation, self.observation.record_id)
        self.assertEqual(result.review, self.review)
        self.observation.record_id = "new-observation"
        refreshed = self.current()
        self.assertEqual(result.snapshot, refreshed.snapshot)
        self.assertNotEqual(result.observation, refreshed.observation)

    def test_running_successor_and_new_success_renew_without_accepting_failure(self) -> None:
        from datetime import timedelta

        before = self.current()
        cycle = self.target.sync.cycle
        self.target.sync.cycle = cycle.model_copy(
            update={
                "status": "running",
                "cycle_id": "b" * 32,
                "cycle_started_at": (self.now - timedelta(seconds=15)).isoformat(),
                "cycle_finished_at": None,
            }
        )
        after = reader.current_source(
            self.store, Path("/synthetic"), "source", "source", before.cycle
        )
        self.assertEqual(before.snapshot, after.snapshot)
        self.assertEqual(before.cycle, after.cycle)
        self.target.sync.cycle = cycle.model_copy(
            update={
                "cycle_id": "b" * 32,
                "cycle_started_at": (self.now - timedelta(seconds=15)).isoformat(),
                "cycle_finished_at": (self.now - timedelta(seconds=5)).isoformat(),
            }
        )
        after = reader.current_source(
            self.store, Path("/synthetic"), "source", "source", before.cycle
        )
        self.assertEqual(before.snapshot, after.snapshot)
        assert before.cycle is not None and after.cycle is not None
        self.assertGreater(after.cycle.finished, before.cycle.finished)
        self.target.sync.cycle = cycle.model_copy(
            update={"status": "failed", "freshness_status": "failed"}
        )
        with self.assertRaises(ValueError):
            reader.current_source(self.store, Path("/synthetic"), "source", "source", before.cycle)

    def test_irrelevant_newer_observations_do_not_displace_the_source_probe(self) -> None:
        unrelated = SimpleNamespace(
            purpose="probe", check_kind="provider", check_name="source-check"
        )
        reconciliation = SimpleNamespace(
            purpose="reconciliation", check_kind="private_http", check_name="source-check"
        )
        self.store.list_public_ingress_observation_records.return_value = (
            unrelated,
            reconciliation,
            self.observation,
        )
        self.assertEqual(self.current().observation, self.observation.record_id)

    def test_replayed_bootstrap_stale_future_failed_and_foreign_cycles_are_refused(self) -> None:
        from datetime import timedelta

        cycle = self.target.sync.cycle
        for updates in (
            {"mode": "full"},
            {"freshness_status": "overdue"},
            {"status": "failed"},
            {"cycle_started_at": (self.now - timedelta(days=1)).isoformat()},
            {"cycle_finished_at": (self.now + timedelta(minutes=1)).isoformat()},
        ):
            with self.subTest(updates=updates):
                self.target.sync.cycle = cycle.model_copy(update=updates)
                with self.assertRaises(ValueError):
                    self.current()
        self.target.sync.cycle = cycle
        self.observation.observed_at = (self.now - timedelta(hours=1)).isoformat()
        with self.assertRaises(ValueError):
            self.current()
        self.observation.observed_at = self.now.isoformat()
        self.target.observed_runtime_identity = self.identity.model_copy(
            update={"artifact_id": "foreign"}
        )
        with self.assertRaises(ValueError):
            self.current()


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_MARIADB_IMAGE"), "explicit local MariaDB image required"
)
class RealConsumerQueryTests(ConsumerProgramFixture):
    def docker(self, *arguments: str) -> str:
        import subprocess

        return subprocess.run(
            ["docker", *arguments], capture_output=True, text=True, check=True, timeout=60
        ).stdout.strip()

    def setUp(self) -> None:
        import time
        import uuid

        super().setUp()
        self.name = "lp-consumer-read-" + uuid.uuid4().hex
        self.identifier = self.docker(
            "run",
            "-d",
            "--rm",
            "--name",
            self.name,
            "--memory=512m",
            "--cpus=1",
            "--tmpfs",
            "/var/lib/mysql",
            "-p",
            "127.0.0.1::3306",
            "-e",
            "MARIADB_ROOT_PASSWORD=synthetic-root",
            "-e",
            "MARIADB_DATABASE=synthetic_database",
            os.environ["LAUNCHPLANE_TEST_MARIADB_IMAGE"],
            "--innodb-buffer-pool-size=64M",
        )
        self.addCleanup(self.docker, "rm", "-f", self.identifier)
        for attempt in range(45):
            try:
                self.root_sql("SELECT 1")
                break
            except Exception:
                if attempt == 44:
                    raise
                time.sleep(1)
        port = json.loads(self.docker("inspect", self.identifier))[0]["NetworkSettings"]["Ports"][
            "3306/tcp"
        ][0]["HostPort"]
        sql = "CREATE USER 'reader'@'%' IDENTIFIED BY 'synthetic-reader'; GRANT SELECT ON synthetic_database.* TO 'reader'@'%';"
        for table in self.tables.values():
            sql += (
                "CREATE TABLE synthetic_database."
                + table
                + " (id INTEGER PRIMARY KEY); INSERT INTO synthetic_database."
                + table
                + " VALUES (1);"
            )
        self.root_sql(sql)
        options = {
            "repairshopr.sync_db.name": "synthetic_database",
            "repairshopr.sync_db.port": port,
            "repairshopr.sync_db.use_ssl": "false",
            "repairshopr.sync_db.ssl_verify": "false",
        }
        self.values.update(
            {
                "repairshopr.sync_db.host": "127.0.0.1",
                "repairshopr.sync_db.user": "reader",
                "repairshopr.sync_db.password": "synthetic-reader",
                **options,
            }
        )
        from control_plane.odoo_instance_overrides import config_parameter_secret_env_key

        self.env[config_parameter_secret_env_key("repairshopr.sync_db.password")] = (
            "synthetic-reader"
        )
        self.set_overrides(options)
        self.real_connect = pymysql.connect
        self.env["PATH"] = os.environ["PATH"]

    def root_sql(self, query: str) -> str:
        return self.docker(
            "exec",
            "-e",
            "MYSQL_PWD=synthetic-root",
            self.identifier,
            "mariadb",
            "--no-defaults",
            "--protocol=socket",
            "--user=root",
            "--batch",
            "--skip-column-names",
            "--execute=" + query,
        )

    def mysql_connect(self, **kwargs: Any) -> Any:
        return self.real_connect(**kwargs)

    def test_real_selects_and_live_nonce_witness_refuse_wrong_destination_and_denied_child(
        self,
    ) -> None:
        import threading
        import time
        from control_plane.production_consumer_programs import SOURCE_WITNESS_PROGRAM

        results = []
        errors = []

        def read() -> None:
            try:
                results.extend(self.run_container())
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=read)
        worker.start()
        self.addCleanup(worker.join, 45)
        identifier = ""
        for attempt in range(40):
            identifier = self.root_sql(
                "SELECT ID FROM information_schema.PROCESSLIST WHERE INFO LIKE 'SELECT SLEEP(30),%' AND USER='reader'"
            )
            if identifier:
                break
            time.sleep(0.25)
        self.assertTrue(identifier, (errors, results))
        before, tail = SOURCE_WITNESS_PROGRAM.split("result={'verified':False}", 1)
        spec = {
            "connection_id": int(identifier),
            "nonce": "a" * 32,
            "database": "synthetic_database",
        }

        def witness(**changes: Any) -> Any:
            inputs = {**spec, **changes}
            with (
                patch.object(
                    sys,
                    "argv",
                    ["witness", base64.b64encode(json.dumps(inputs).encode()).decode(), "FRAME:"],
                ),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                namespace: dict[str, Any] = {}
                exec(compile(before, "<source-library>", "exec"), namespace)
                namespace["published_source"] = lambda _: ({}, self.identifier, "physical-source")
                exec(
                    compile("result={'verified':False}" + tail, "<source-witness>", "exec"),
                    namespace,
                )
            return json.loads(out.getvalue().removeprefix("FRAME:"))["result"]["verified"]

        self.assertTrue(witness())
        self.assertFalse(witness(nonce="b" * 32))
        self.assertFalse(witness(database="other_database"))
        worker.join(40)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertEqual(results[-1]["result"]["query"], "verified")
        grants = "REVOKE SELECT ON synthetic_database.* FROM 'reader'@'%';"
        for table in self.tables.values():
            if table != self.child_table:
                grants += "GRANT SELECT ON synthetic_database." + table + " TO 'reader'@'%';"
        self.root_sql(grants)
        self.assertEqual(self.run_container()[-1]["result"]["query"], "failed")
        self.root_sql(
            "GRANT SELECT ON synthetic_database.* TO 'reader'@'%'; GRANT INSERT ON synthetic_database.* TO PUBLIC"
        )
        self.assertEqual(self.run_container()[-1]["result"]["query"], "unknown")


class GrantClassificationTests(unittest.TestCase):
    def test_mariadb_authentication_is_opt_in_and_preserves_unsafe_or_unknown_grants(self) -> None:
        from control_plane.odoo_source_probe import classify_grants

        usage = "GRANT USAGE ON *.* TO 'reader'@'%' IDENTIFIED BY PASSWORD '*" + "A" * 40 + "'"
        select = "GRANT SELECT ON `source`.* TO 'reader'@'%'"
        rows = [(usage,), (select,)]
        self.assertEqual(classify_grants(rows), "unknown")
        self.assertEqual(classify_grants(rows, mariadb_authentication=True), "read_only")
        self.assertEqual(
            classify_grants(
                [(usage + " WITH GRANT OPTION",), (select,)], mariadb_authentication=True
            ),
            "unsafe",
        )
        self.assertEqual(
            classify_grants([(usage + " UNRECOGNIZED",), (select,)], mariadb_authentication=True),
            "unknown",
        )


class ConsumerReadIntegrationTests(unittest.TestCase):
    def test_payload_excludes_managed_values_and_empty_data_cannot_qualify(self) -> None:
        from tests.test_odoo_import_diagnostics import DiagnosticFixture

        fixture = DiagnosticFixture()
        member = ReviewedOdooConsumer(product=fixture.profile.product, environment="testing")
        source = reader.PublishedConsumerSource(
            "source",
            "b" * 64,
            "probe",
            ProductionConsumerReview(
                consumers=(member,),
                coordination_reference="review",
                required_populated_roles=(ConsumerTableRole.INVOICE_LINE,),
            ),
            {},
            "source-server",
        )
        populated = "verified"

        def remote(**kwargs: Any) -> None:
            self.assertNotIn(PRIVATE, json.dumps(kwargs["payload"]))
            kwargs["receive"]({"type": "connection", "connection_id": 17})
            kwargs["receive"](
                {"type": "complete", "result": {"query": "verified", "populated": populated}}
            )

        with (
            patch.object(reads, "connect_runtime", return_value=fixture.connection),
            patch.object(
                reader,
                "consumer_authority",
                return_value=(
                    {"desired_payload": {}, "desired_env": {"password": PRIVATE}},
                    "authority",
                ),
            ),
            patch.object(reader, "consumer_files", return_value={}),
            patch.object(reads, "inspect_runtime_container", return_value={}),
            patch.object(reads, "assert_selection_current"),
            patch.object(reader, "witness_source", return_value=True),
            patch.object(reader, "_read_remote", side_effect=remote),
        ):
            result = reader.read_consumer(
                fixture.store, Path("/synthetic"), member, source, fixture.connection.selection
            )
            self.assertEqual(result.status, "verified")
            populated = "failed"
            result = reader.read_consumer(
                fixture.store, Path("/synthetic"), member, source, fixture.connection.selection
            )
            self.assertEqual(result.query, "verified")
            self.assertEqual(result.status, "failed")
