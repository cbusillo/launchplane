from __future__ import annotations

import contextlib
import base64
import io
import json
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch
from collections.abc import Callable
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException

from control_plane import odoo_inventory_readback as reader, odoo_runtime_reads as reads
from control_plane import odoo_inventory_readback_program as programs
from control_plane.contracts.runtime_identity import RUNTIME_IDENTITY_ENV_KEY
from control_plane.http_routes.odoo_runtime import (
    OdooRuntimeReadDependencies,
    register_odoo_runtime_read_routes,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.odoo_source_prerequisite_programs import HOST_INSPECT_LIBRARY
from tests.support.auth import _identity
from tests.support.http import request
from tests.test_odoo_import_diagnostics import DiagnosticFixture, PRIVATE


def query(fixture: DiagnosticFixture) -> reader.InventoryReadQuery:
    identity = fixture.identity
    return reader.InventoryReadQuery(
        context=identity.context,
        deployment_record_id=identity.deployment_record_id,
        source_git_ref=identity.source_git_ref,
        artifact_id=identity.artifact_id,
        image_digest=identity.image_reference.rpartition("@")[2],
        target_id=fixture.connection.evidence.target_id,
        company_id=7,
        part_ids=("51", "52"),
        purchaser_xmlid="cm_custom.user_gunneet",
    )


def observation() -> dict[str, Any]:
    return {
        "runtime": "verified",
        "company_id": 7,
        "database_snapshot": "10:10:",
        "facts": {name: None for name in reader.StoredInventoryFacts.model_fields} | {"parts": 2},
        "parts": [
            {"part_id": "51", "status": "present", "name": "Fictional hinge"},
            {"part_id": "52", "status": "missing"},
        ],
        "purchaser": {
            "xmlid": "cm_custom.user_gunneet",
            "status": "present",
            "user_id": 19,
            "login": "buyer@example.invalid",
            "active": True,
        },
    }


class InventoryReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = DiagnosticFixture()
        self.query = query(self.fixture)
        self.observed = observation()
        self.connector = self.enterContext(
            patch.object(reads, "connect_runtime", return_value=self.fixture.connection)
        )
        self.enterContext(
            patch.object(
                reads,
                "inspect_runtime_container",
                return_value={RUNTIME_IDENTITY_ENV_KEY: self.fixture.identity.model_dump_json()},
            )
        )
        self.remote = self.enterContext(
            patch.object(reader, "_read_remote", side_effect=self.observe)
        )

    def observe(self, **kwargs: Any) -> None:
        kwargs["receive"]({"type": "complete", "result": self.observed})

    def read(self) -> reader.InventoryReadResult:
        return reader.read_inventory(
            store=cast(reads.OdooRuntimeReadStore, self.fixture.store),
            product=self.fixture.profile.product,
            query=self.query,
            control_plane_root=Path("fixture"),
            database_url=None,
        )

    def test_observation_is_bound_without_diagnostic_signin_or_store_writes(self) -> None:
        first = self.read()
        self.observed["facts"]["parts"] = 3
        second = self.read()
        self.assertEqual((first.facts.parts, second.facts.parts), (2, 3))  # type: ignore[union-attr]
        self.assertIsNone(second.facts.historical_fields_copied)  # type: ignore[union-attr]
        self.assertEqual(second.evidence.identity, self.fixture.identity)  # type: ignore[union-attr]
        self.assertIsNotNone(second.observed_at)
        self.fixture.store.read_secret_version.assert_not_called()
        self.assertTrue(
            all(call[0].startswith(("read_", "list_")) for call in self.fixture.store.mock_calls)
        )
        self.assertFalse(self.connector.call_args.kwargs.get("diagnostics", False))

    def test_expected_source_target_or_deployment_drift_never_reads_data(self) -> None:
        original = self.query
        for field, value in (
            ("context", "foreign"),
            ("deployment_record_id", "old"),
            ("source_git_ref", "f" * 40),
            ("artifact_id", "old"),
            ("image_digest", "sha256:" + "f" * 64),
            ("target_id", "foreign"),
        ):
            with self.subTest(field=field):
                self.query = original.model_copy(update={field: value})
                self.assertEqual(self.read().runtime, "unknown")
                self.remote.assert_not_called()

    def test_replacement_during_read_discards_all_private_observations(self) -> None:
        changed = replace(
            self.fixture.connection,
            evidence=self.fixture.connection.evidence.model_copy(
                update={"container_id": "different"}
            ),
        )
        self.connector.side_effect = [self.fixture.connection, changed]
        result = self.read()
        self.assertEqual(result.failure_stage, "drift")
        self.assertIsNone(result.facts)
        self.assertIsNone(result.purchaser)
        self.assertEqual(result.parts, ())

    def test_record_generation_changes_during_read_discard_data(self) -> None:
        self.fixture.store.read_environment_inventory.side_effect = [
            self.fixture.inventory,
            self.fixture.inventory.model_copy(update={"runtime_identity": None}),
        ]
        self.assertEqual(self.read().failure_stage, "drift")

    def test_transport_failure_and_undeclared_or_unbound_fields_are_not_returned(self) -> None:
        mutations: tuple[Callable[[dict[str, Any]], None], ...] = (
            lambda value: value.update(password=PRIVATE),
            lambda value: value["facts"].update(historical_unmapped_lines=0),
            lambda value: value["parts"][0].update(part_id="999"),
            lambda value: value["purchaser"].update(xmlid="base.user_admin"),
            lambda value: value["purchaser"].update(password=PRIVATE),
            lambda value: value.update(company_id=8),
            lambda value: value.update(observed_at="stale"),
            lambda value: value["facts"].update(parts=True),
        )
        for mutate in mutations:
            self.observed = observation()
            mutate(self.observed)
            result = self.read()
            self.assertEqual(result.failure_stage, "result_validation")
            self.assertNotIn(PRIVATE, result.model_dump_json())
        self.remote.side_effect = RuntimeError(PRIVATE)
        self.assertEqual(self.read().failure_stage, "transport")
        self.assertNotIn(PRIVATE, self.read().model_dump_json())


class InventoryRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_read_scope_and_exact_bounded_selectors(self) -> None:
        fixture = DiagnosticFixture()
        allowed = MagicMock(return_value=True)
        app = FastAPI()
        common = ReadRouteDependencies(
            read_identity=lambda: _identity(),
            get_record_store=lambda: fixture.store,
            next_trace_id=lambda: "fixture-trace",
            authorization_allows=allowed,
            http_error=lambda **kwargs: HTTPException(
                status_code=kwargs["status_code"], detail=kwargs["code"]
            ),
            error_response_model=reader.InventoryReadResult,
        )
        register_odoo_runtime_read_routes(
            cast(ApiRouteRegistrar, cast(object, app)),
            dependencies=OdooRuntimeReadDependencies(
                common=common, control_plane_root=Path("fixture"), database_url=None
            ),
        )
        base = reader.ROUTE.format(product=fixture.profile.product, environment="testing")
        values = query(fixture).model_dump()
        path = base + "?" + urlencode(values, doseq=True)
        with patch.object(
            reader, "read_inventory", return_value=reader.InventoryReadResult()
        ) as execute:
            response = await request(app, "GET", path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(allowed.call_args.kwargs["action"], "operations.read")
            self.assertEqual(allowed.call_args.kwargs["product"], fixture.profile.product)
            self.assertEqual(allowed.call_args.kwargs["target"].instances, ("testing",))
            execute.reset_mock()
            for override in (
                {"sql": "select *"},
                {"model": "res.users"},
                {"method": "write"},
                {"part_ids": tuple(str(i) for i in range(1, 12))},
                {"part_ids": ("51", "51")},
                {"part_ids": ("1 OR 1=1",)},
                {"purchaser_xmlid": "base.user_admin"},
            ):
                self.assertEqual(
                    (
                        await request(
                            app, "GET", base + "?" + urlencode(values | override, doseq=True)
                        )
                    ).status_code,
                    422,
                )
            self.assertEqual(
                (await request(app, "GET", path.replace("/testing/", "/prod/"))).status_code, 400
            )
            allowed.return_value = False
            self.assertEqual((await request(app, "GET", path)).status_code, 403)
            execute.assert_not_called()


class StoredCursor:
    def __init__(self) -> None:
        self.commands: list[tuple[str, tuple[Any, ...]]] = []
        self.schema: dict[str, set[str]] = {}
        self.result: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.commands.append((sql, params))
        if sql.startswith("SET TRANSACTION"):
            self.result = []
        elif "information_schema.columns" in sql:
            self.result = [(column,) for column in self.schema.get(params[0], set())]
        elif "FROM res_company" in sql:
            self.result = [(1,)]
        elif sql == "SELECT pg_current_snapshot()::text":
            self.result = [("10:10:",)]
        else:
            raise AssertionError("Unexpected statement")

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.result

    def __enter__(self) -> StoredCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class InventoryProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = DiagnosticFixture()
        self.cursor = StoredCursor()
        self.connection = MagicMock()
        self.connection.cursor.return_value = self.cursor
        self.connect = MagicMock(return_value=self.connection)
        self.spec = {
            "identity": self.fixture.identity.model_dump(mode="json"),
            "company_id": 7,
            "part_ids": ["51", "52"],
            "purchaser_xmlid": "cm_custom.user_gunneet",
            "database_address": "192.0.2.7",
        }
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "odoo.conf"
        self.config.write_text(
            "[options]\ndb_name=fictional\ndb_host=database\ndb_user=local\ndb_password="
            + PRIVATE
            + "\n"
        )

    def run_program(self) -> dict[str, Any]:
        program = programs.CONTAINER_PROGRAM.replace(
            "__IDENTITY_KEY__", repr(RUNTIME_IDENTITY_ENV_KEY)
        ).replace("__CONFIG_PATH__", repr(str(self.config)))
        output = io.StringIO()
        with (
            patch.dict(sys.modules, {"psycopg2": SimpleNamespace(connect=self.connect)}),
            patch.dict(
                os.environ,
                {
                    "PLATFORM_INSTANCE": "testing",
                    "ODOO_DB_NAME": "fictional",
                    RUNTIME_IDENTITY_ENV_KEY: json.dumps(self.spec["identity"]),
                },
            ),
            patch.object(sys, "stdin", io.StringIO(json.dumps(self.spec))),
            contextlib.redirect_stdout(output),
        ):
            exec(compile(program, "<fixed-inventory-program>", "exec"), {})
        self.assertNotIn(PRIVATE, output.getvalue())
        return cast(dict[str, Any], json.loads(output.getvalue()))

    def test_absent_schema_is_unavailable_and_database_is_read_only_then_rolled_back(self) -> None:
        result = reader.InventoryReadResult.model_validate(self.run_program())
        self.assertEqual(result.runtime, "verified")
        self.assertIsNone(result.facts.parts)  # type: ignore[union-attr]
        self.assertEqual([part.status for part in result.parts], ["unavailable", "unavailable"])
        self.assertEqual(result.purchaser.status, "unavailable")  # type: ignore[union-attr]
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once()
        self.connection.close.assert_called_once()
        options = self.connect.call_args.kwargs
        self.assertIn("default_transaction_read_only=on", options["options"])
        self.assertEqual(options["hostaddr"], self.spec["database_address"])
        self.assertTrue(
            all(
                sql.startswith(
                    ("SELECT ", "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                )
                for sql, _ in self.cursor.commands
            )
        )

    def test_external_database_configuration_and_query_failure_cannot_write_or_leak(self) -> None:
        original = self.config.read_text()
        for field, value in (
            ("db_host=database", "db_host=foreign.invalid"),
            ("db_name=fictional", "db_name=foreign"),
        ):
            self.config.write_text(original.replace(field, value))
            self.assertEqual(self.run_program()["runtime"], "unknown")
            self.connect.assert_not_called()
        self.config.write_text(original)
        self.connection.cursor.side_effect = RuntimeError(PRIVATE)
        self.assertEqual(self.run_program()["runtime"], "unknown")
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once()
        self.connection.close.assert_called_once()

    def test_configuration_drift_discards_read_result(self) -> None:
        def drift() -> None:
            self.config.write_text(self.config.read_text() + "db_port=6000\n")

        self.connection.rollback.side_effect = drift
        result = self.run_program()
        self.assertEqual(result, {"runtime": "unknown", "failure_stage": "drift"})


class InventoryHostTests(unittest.TestCase):
    def test_database_peer_must_share_the_selected_internal_compose_network(self) -> None:
        project = "fictional-project"
        database: dict[str, Any] = {
            "Id": "d" * 64,
            "Config": {
                "Labels": {
                    "com.docker.compose.project": project,
                    "com.docker.compose.service": "database",
                }
            },
            "State": {"Running": True},
            "NetworkSettings": {"Networks": {"fictional-private": {"IPAddress": "192.0.2.7"}}},
        }
        network = {"Internal": True, "Labels": {"com.docker.compose.project": project}}
        web: dict[str, Any] = {
            "NetworkSettings": {"Networks": {"fictional-private": {}, "dokploy-network": {}}}
        }

        def docker(arguments: list[str], **_kwargs: Any) -> SimpleNamespace:
            if arguments[1] == "ps":
                output = database["Id"]
            elif arguments[1] == "inspect":
                output = json.dumps([database])
            elif arguments[1:3] == ["network", "inspect"]:
                output = json.dumps([network])
            else:
                raise AssertionError("No other host command is allowed")
            return SimpleNamespace(stdout=output.encode())

        namespace: dict[str, Any] = {}
        encoded = base64.b64encode(json.dumps({"project": project}).encode()).decode()
        with (
            patch.object(sys, "argv", ["program", encoded, "prefix"]),
            patch("subprocess.run", side_effect=docker),
        ):
            exec(HOST_INSPECT_LIBRARY, namespace)
            inspect_peer = namespace["inspect_database_peer"]
            self.assertEqual(inspect_peer(web)["address"], "192.0.2.7")
            network["Internal"] = False
            with self.assertRaises(ValueError):
                inspect_peer(web)
            network["Internal"] = True
            database["Config"]["Labels"]["com.docker.compose.project"] = "foreign"
            with self.assertRaises(ValueError):
                inspect_peer(web)

    def test_host_uses_fixed_program_and_rejects_changed_database_peer(self) -> None:
        spec = {
            "company_id": 7,
            "part_ids": ["51"],
            "purchaser_xmlid": "cm_custom.user_gunneet",
            "identity": {},
            "container_program": "fixed-program",
        }
        output = io.StringIO()
        inspect_web = MagicMock(return_value={"Id": "a" * 64})
        inspect_db = MagicMock(side_effect=[{"address": "192.0.2.7"}, {"address": "192.0.2.8"}])
        child = MagicMock(
            return_value=SimpleNamespace(returncode=0, stdout=json.dumps(observation()))
        )
        program = programs.HOST_PROGRAM[len(HOST_INSPECT_LIBRARY) :]
        with contextlib.redirect_stdout(output):
            exec(
                program,
                {
                    "spec": spec,
                    "prefix": "",
                    "json": json,
                    "subprocess": SimpleNamespace(run=child, PIPE=-1, DEVNULL=-3),
                    "inspect": inspect_web,
                    "inspect_database_peer": inspect_db,
                },
            )
        self.assertEqual(
            json.loads(output.getvalue())["result"],
            {"runtime": "unknown", "failure_stage": "drift"},
        )
        self.assertEqual(child.call_args.args[0][-1], spec["container_program"])
        self.assertEqual(
            json.loads(child.call_args.kwargs["input"])["database_address"], "192.0.2.7"
        )


class InventoryPostgresTests(unittest.TestCase):
    def test_fixed_queries_read_current_company_scoped_rows_and_preserve_unavailable(self) -> None:
        import psycopg
        from tests.test_postgres_integration import _isolated_postgres_database

        namespace: dict[str, Any] = {}
        exec(programs.INVENTORY_LIBRARY, namespace)
        spec = {
            "company_id": 7,
            "part_ids": ["51", "52", "53"],
            "purchaser_xmlid": "cm_custom.user_gunneet",
        }
        with _isolated_postgres_database() as database_url:
            with psycopg.connect(
                database_url.replace("postgresql+psycopg:", "postgresql:")
            ) as database:
                database.execute(
                    (Path(__file__).parent / "fixtures/odoo_inventory_readback.sql").read_bytes()
                )
                database.commit()

                def read() -> reader.InventoryReadResult:
                    with database.cursor() as cursor:
                        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                        result = reader.InventoryReadResult.model_validate(
                            namespace["read_inventory"](cursor, spec)
                        )
                    database.rollback()
                    return result

                first = read()
                assert first.facts is not None and first.purchaser is not None
                self.assertEqual(first.facts.parts, 2)
                self.assertEqual(first.facts.supplier_cost_rows, 12)
                self.assertEqual(first.facts.current_conflicts, 2)
                self.assertEqual(first.facts.listing_bindings, 0)
                self.assertEqual(first.facts.nonzero_sale_price_parts, 0)
                self.assertEqual(first.facts.internal_quantity, "5.25")
                self.assertEqual(
                    (first.facts.stock_move_rows, first.facts.done_stock_move_rows), (2, 1)
                )
                self.assertEqual(first.parts[0].cost, "9.45")
                self.assertEqual(first.parts[0].internal_quantity, "2.50")
                self.assertEqual(first.parts[0].part_number, first.parts[0].barcode)
                self.assertTrue(first.parts[0].suppliers_truncated)
                self.assertEqual(len(first.parts[0].supplier_costs or ()), 10)
                self.assertEqual(first.parts[2].status, "missing")
                self.assertEqual(first.purchaser.login, "buyer@example.invalid")
                self.assertEqual(
                    first.purchaser.group_xmlids,
                    ("base.group_user", "purchase.group_purchase_user"),
                )
                self.assertEqual(first.purchaser.groups_without_xmlid, 1)
                self.assertIsNotNone(first.database_uuid)
                self.assertIsNotNone(first.database_snapshot)
                self.assertNotIn("private-fixture", first.model_dump_json())

                database.execute(
                    "INSERT INTO external_id VALUES (1,'part','product.template',999,'53',true)"
                )
                database.execute("UPDATE product_template SET list_price=5 WHERE id=101")
                database.execute("ALTER TABLE product_template DROP COLUMN fishbowl_packaging")
                database.execute("ALTER TABLE product_supplierinfo DROP COLUMN uom_id")
                database.commit()
                current = read()
                assert current.facts is not None
                self.assertEqual(current.facts.parts, 3)
                self.assertEqual(current.facts.nonzero_sale_price_parts, 2)
                self.assertEqual(current.parts[2].status, "present")
                self.assertNotEqual(current.database_snapshot, first.database_snapshot)
                self.assertIsNone(current.facts.parts_with_packaging)
                self.assertIsNone(current.facts.historical_fields_copied)
                self.assertEqual(current.facts.supplier_cost_rows, first.facts.supplier_cost_rows)
                self.assertIsNone(current.parts[0].supplier_costs)

                database.execute("DELETE FROM external_system WHERE code='fishbowl'")
                database.commit()
                absent = read()
                assert absent.facts is not None
                self.assertIsNone(absent.facts.parts)
                self.assertIsNone(absent.facts.listing_bindings)
                self.assertTrue(all(part.status == "unavailable" for part in absent.parts))
