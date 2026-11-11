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

    def test_current_observation_not_frozen_archive_or_expected_counts(self) -> None:
        first = self.read()
        self.observed["facts"]["parts"] = 3
        second = self.read()
        self.assertEqual((first.facts.parts, second.facts.parts), (2, 3))  # type: ignore[union-attr]
        self.assertIsNone(second.facts.historical_fields_copied)  # type: ignore[union-attr]
        self.assertEqual(second.evidence.identity, self.fixture.identity)  # type: ignore[union-attr]
        self.assertIsNotNone(second.observed_at)
        self.assertFalse(second.applied)
        self.assertEqual(second.purchaser.first_signin, "not_observed")  # type: ignore[union-attr]
        self.assertNotIn(PRIVATE, second.model_dump_json())
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
            cast(ApiRouteRegistrar, app),
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
        self.part_ids = [101, 102]
        self.suppliers = [(11, "Example Supplier", "V-51", "8.25", 1, 1)]
        self.user = (19, True, "buyer@example.invalid", "Fictional Purchaser")

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.commands.append((sql, params))
        if sql.startswith("SET TRANSACTION"):
            self.result = []
        elif "information_schema.columns" in sql:
            self.result = [(column,) for column in self.schema.get(params[0], set())]
        elif "FROM res_company" in sql:
            self.result = [(1,)]
        elif sql.startswith("SELECT DISTINCT t.id"):
            self.result = [(value,) for value in self.part_ids]
        elif sql.startswith("SELECT t.id"):
            self.result = (
                [(101, "Fictional hinge", "PART-51", "0", "none", True)]
                if params == ("51",)
                else []
            )
        elif "e.resource='product'" in sql:
            self.result = [(0,)]
        elif "count(DISTINCT p.id)" in sql:
            self.result = [(3,)]
        elif "SELECT count(*) FROM product_template" in sql:
            self.result = [(0 if "list_price" in sql else 1,)]
        elif "FROM cm_product_import_conflict" in sql:
            self.result = [(1,)]
        elif "FROM stock_quant" in sql:
            self.result = [("5.25" if len(params[1]) > 1 else "2.50",)]
        elif "FROM stock_move m" in sql:
            self.result = [(9, 8)]
        elif sql.startswith("SELECT to_jsonb(p)"):
            self.result = [("PART-51", "9.45", "PART-51")]
        elif sql.startswith("SELECT p.partner_id"):
            self.result = self.suppliers
        elif sql.startswith("SELECT state FROM ir_module_module"):
            self.result = [("installed",)]
        elif sql.startswith("SELECT u.id"):
            self.result = [self.user]
        elif sql.startswith("SELECT DISTINCT d.module"):
            self.result = [("base.group_user",), ("purchase.group_purchase_user",)]
        elif sql.startswith("SELECT count(DISTINCT g.gid)"):
            self.result = [(0,)]
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

    def test_current_rows_are_read_and_missing_metadata_does_not_gate_them(self) -> None:
        self.cursor.schema = {
            "external_id": {"system_id", "resource", "res_model", "res_id", "external_id"},
            "external_system": {"code"},
            "product_template": {"id", "active"},
        }
        first = self.run_program()
        self.cursor.part_ids.append(103)
        second = self.run_program()
        self.assertEqual((first["facts"]["parts"], second["facts"]["parts"]), (2, 3))
        self.assertEqual(second["parts"][0]["name"], "Fictional hinge")
        self.assertEqual(second["parts"][1]["status"], "missing")
        self.assertIsNone(second["facts"]["stockable_parts"])
        self.assertIsNone(second["facts"]["supplier_cost_rows"])

    def test_persisted_cost_stock_and_seed_fields_are_bounded_and_company_scoped(self) -> None:
        self.cursor.schema = {
            "external_id": {"system_id", "resource", "res_model", "res_id", "external_id"},
            "external_system": {"code"},
            "product_template": {
                "id",
                "active",
                "list_price",
                "is_storable",
                "tracking",
                "fishbowl_upc",
                "fishbowl_packaging",
                "fishbowl_extra_details",
            },
            "product_product": {"id", "product_tmpl_id"},
            "product_supplierinfo": {
                "id",
                "company_id",
                "product_tmpl_id",
                "partner_id",
                "price",
                "currency_id",
                "uom_id",
                "product_code",
            },
            "cm_product_import_conflict": {"product_id", "source", "company_id"},
            "stock_quant": {"product_id", "location_id", "company_id", "quantity"},
            "stock_location": {"id", "usage"},
            "stock_move": {"product_id", "state", "company_id"},
            "ir_model_data": {"module", "name", "model", "res_id"},
            "ir_module_module": {"name", "state"},
            "res_users": {"id", "active", "login", "partner_id"},
            "res_partner": {"id", "name"},
            "res_groups_users_rel": {"uid", "gid"},
        }
        self.cursor.suppliers *= 11
        result = reader.InventoryReadResult.model_validate(self.run_program())
        assert result.facts is not None and result.purchaser is not None
        self.assertEqual(result.facts.supplier_cost_rows, 3)
        self.assertEqual(result.facts.internal_quantity, "5.25")
        self.assertEqual((result.facts.stock_move_rows, result.facts.done_stock_move_rows), (9, 8))
        self.assertIsNone(result.facts.historical_fields_copied)
        part = result.parts[0]
        self.assertEqual(
            (part.name, part.part_number, part.barcode, part.cost, part.sale_price),
            ("Fictional hinge", "PART-51", "PART-51", "9.45", "0"),
        )
        self.assertEqual(part.internal_quantity, "2.50")
        self.assertTrue(part.suppliers_truncated)
        self.assertEqual(len(part.supplier_costs or ()), 10)
        self.assertEqual(result.purchaser.login, self.cursor.user[2])
        self.assertEqual(result.purchaser.module_state, "installed")
        self.assertEqual(
            result.purchaser.group_xmlids, ("base.group_user", "purchase.group_purchase_user")
        )
        for statement, parameters in self.cursor.commands:
            if "company_id=%s" in statement:
                self.assertIn(self.spec["company_id"], parameters)
            self.assertNotIn("SELECT *", statement)
            self.assertNotIn("password", statement)

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
        self.assertEqual(self.run_program()["failure_stage"], "applied_parameters")
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

        def docker(arguments: list[str], **kwargs: Any) -> SimpleNamespace:
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
