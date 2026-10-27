from io import StringIO
import json
import unittest
from unittest.mock import Mock

from pydantic import ValidationError

from control_plane.odoo_copy_aggregates import AggregateQuery, AggregateCounts, read_aggregates


class CopyAggregateTests(unittest.TestCase):
    def test_query_accepts_identifiers_and_rejects_sql_and_overrides(self) -> None:
        query = AggregateQuery(table="product_template", key="first_mpn", partition="manufacturer")
        self.assertEqual(query.table, "product_template")
        for values in (
            {"table": "product_template;DELETE FROM res_users", "key": "first_mpn"},
            {"table": "product_template", "key": "first_mpn", "sql": "SELECT * FROM res_users"},
            {"table": "product_template", "key": "first_mpn", "database": "prod"},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                AggregateQuery.model_validate(values)

    def test_output_rejects_rows_negative_counts_booleans_and_overflow(self) -> None:
        counts = dict(active_rows=10, duplicate_groups=2, duplicate_rows=5, missing_key_rows=1)
        self.assertEqual(AggregateCounts(**counts).duplicate_rows, 5)
        for values in (
            {**counts, "rows": [{"email": "private@example.invalid"}]},
            {**counts, "active_rows": True},
            {**counts, "active_rows": -1},
            {**counts, "active_rows": 2**63},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                AggregateCounts.model_validate(values)

    def test_unqualified_copy_never_dispatches_a_query(self) -> None:
        store = Mock()
        store.read_product_profile_record.side_effect = ValueError("unknown custody")
        with self.assertRaises(ValueError):
            read_aggregates(
                store=store,
                product="example-odoo",
                query=AggregateQuery(table="product_template", key="first_mpn"),
            )
        store.read_clean_testing_snapshot.assert_not_called()


class AggregateProgramTests(unittest.TestCase):
    def test_query_uses_readonly_snapshot_and_refuses_unsanitized_or_remote_relations(self) -> None:
        import base64
        from contextlib import redirect_stdout
        import os
        import sys
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch
        from psycopg import sql
        from control_plane.odoo_copy_aggregates import AGGREGATE_QUERY_PROGRAM

        query = AggregateQuery(table="product_template", key="first_mpn", partition="manufacturer")
        payload = dict(
            aggregate_query=query.model_dump(),
            database_name="copy",
            witness_key="copy_witness",
            copy_binding_sha256="a" * 64,
            readback={},
        )
        for readonly, sanitized, relation in (
            (True, True, "r"),
            (False, True, "r"),
            (True, False, "r"),
            (True, True, "f"),
            (True, True, "v"),
        ):
            with self.subTest(readonly=readonly, sanitized=sanitized, relation=relation):
                cursor = MagicMock()
                cursor.__enter__.return_value = cursor
                cursor.fetchone.side_effect = [("on" if readonly else "off",), (10, 1), (2, 5)]
                cursor.fetchall.side_effect = [
                    [("a" * 64,)],
                    [(relation, False)],
                    [("active", "bool"), ("first_mpn", "text"), ("manufacturer", "int4")],
                ]
                connection = Mock()
                connection.cursor.return_value = cursor
                connect = Mock(return_value=connection)
                isolation = Mock(return_value=([] if sanitized else ["live_connection"], [], 1))
                output = StringIO()
                with (
                    patch.dict(
                        sys.modules, {"psycopg2": SimpleNamespace(connect=connect, sql=sql)}
                    ),
                    patch.dict(
                        os.environ,
                        {
                            "PLATFORM_INSTANCE": "testing",
                            "ODOO_DB_HOST": "database",
                            "ODOO_DB_USER": "copy",
                            "ODOO_DB_PASSWORD": "fixture",
                        },
                    ),
                    patch.object(
                        sys,
                        "argv",
                        [
                            "aggregate",
                            base64.b64encode(json.dumps(payload).encode()).decode(),
                        ],
                    ),
                    redirect_stdout(output),
                ):
                    if readonly and sanitized and relation == "r":
                        exec(AGGREGATE_QUERY_PROGRAM, {"read_integration_settings": isolation})
                        counts = AggregateCounts.model_validate_json(output.getvalue())
                        self.assertEqual(counts.active_rows, 10)
                        self.assertEqual(counts.duplicate_rows, 5)
                        statements = [
                            call.args[0].as_string()
                            for call in cursor.execute.call_args_list
                            if isinstance(call.args[0], sql.Composed)
                        ]
                        self.assertTrue(
                            all(
                                'FROM ONLY "public"."product_template"' in statement
                                for statement in statements
                            )
                        )
                    else:
                        with self.assertRaises(RuntimeError):
                            exec(AGGREGATE_QUERY_PROGRAM, {"read_integration_settings": isolation})
                        self.assertEqual(output.getvalue(), "")
                        self.assertFalse(
                            any(
                                isinstance(call.args[0], sql.Composed)
                                for call in cursor.execute.call_args_list
                            )
                        )
                connection.commit.assert_not_called()
                connection.rollback.assert_called_once()
                connection.close.assert_called_once()


class AggregateRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_product_read_scope_no_production_no_overrides_and_fail_closed(self) -> None:
        from pathlib import Path
        from typing import Any, cast
        from unittest.mock import patch
        from fastapi import FastAPI, HTTPException
        from control_plane.http_routes.odoo_runtime import (
            OdooRuntimeReadDependencies,
            register_odoo_runtime_read_routes,
        )
        from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
        from control_plane.odoo_copy_aggregates import AggregateEvidence, ROUTE
        from control_plane.storage.postgres import PostgresRecordStore
        from tests.test_odoo_import_diagnostics import DiagnosticFixture
        from tests.support.auth import _identity
        from tests.support.http import request

        fixture = DiagnosticFixture()
        store = Mock(spec=PostgresRecordStore)
        store.read_product_profile_record.return_value = fixture.profile
        store.list_product_profile_records.return_value = (fixture.profile,)
        allowed = Mock(return_value=True)

        def http_error(**kwargs: Any) -> HTTPException:
            return HTTPException(status_code=kwargs["status_code"], detail={"code": kwargs["code"]})

        app = FastAPI()
        register_odoo_runtime_read_routes(
            cast(ApiRouteRegistrar, app),
            dependencies=OdooRuntimeReadDependencies(
                common=ReadRouteDependencies(
                    read_identity=lambda: _identity(),
                    get_record_store=lambda: store,
                    next_trace_id=lambda: "fixture",
                    authorization_allows=allowed,
                    http_error=http_error,
                    error_response_model=AggregateCounts,
                ),
                control_plane_root=Path("fixture"),
                database_url=None,
            ),
        )
        path = ROUTE.format(product=fixture.profile.product, environment="testing")
        payload = dict(table="product_template", key="first_mpn", partition="manufacturer")
        evidence = AggregateEvidence(
            product=fixture.profile.product,
            context=fixture.context,
            generation_sha256="a" * 64,
            snapshot_id="a" * 64,
            snapshot_database_sha256="b" * 64,
            snapshot_captured_at="2026-10-25T00:00:00Z",
            source_mode="container-export",
            source_binding_sha256="c" * 64,
            operation_id="fixture-copy",
            observed_at="2026-10-26T00:00:00Z",
            counts=AggregateCounts(
                active_rows=12, duplicate_groups=1, duplicate_rows=3, missing_key_rows=2
            ),
        )
        with patch(
            "control_plane.odoo_copy_aggregates.read_aggregates", return_value=evidence
        ) as read:
            response = await request(app, "POST", path, payload=payload)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(allowed.call_args.kwargs["product"], fixture.profile.product)
            self.assertEqual(allowed.call_args.kwargs["target"].instances, ("testing",))
            read.reset_mock()
            for invalid in (path.replace("/testing/", "/prod/"), path + "?host=prod"):
                self.assertEqual(
                    (await request(app, "POST", invalid, payload=payload)).status_code, 400
                )
            self.assertEqual(
                (
                    await request(
                        app, "POST", path, payload={**payload, "sql": "SELECT * FROM res_users"}
                    )
                ).status_code,
                422,
            )
            allowed.return_value = False
            self.assertEqual((await request(app, "POST", path, payload=payload)).status_code, 403)
            read.assert_not_called()
            allowed.return_value = True
            read.side_effect = ValueError("private database credentials")
            response = await request(app, "POST", path, payload=payload)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private", response.text)


class AggregateProvenanceTests(unittest.TestCase):
    def test_current_copy_requires_matching_snapshot_and_unchanged_authority(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch
        from control_plane import odoo_copy_aggregates as reader
        from tests.test_odoo_import_diagnostics import DiagnosticFixture
        from tests.test_odoo_testing_restore import generation
        from control_plane.odoo_testing_restore import generation_binding
        from control_plane.dokploy import source, api

        fixture = DiagnosticFixture()
        lane = next(lane for lane in fixture.profile.lanes if lane.instance == "testing")
        qualified_lane = lane.model_copy(
            update={
                "odoo_data_policy": lane.odoo_data_policy.model_copy(
                    update={"data_authority": "resettable"}
                )
            }
        )
        profile = fixture.profile.model_copy(
            update={
                "lanes": tuple(
                    qualified_lane if item.instance == "testing" else item
                    for item in fixture.profile.lanes
                )
            }
        )
        store = fixture.store
        store.read_product_profile_record.return_value = profile
        current = generation().model_copy(
            update={
                "product": profile.product,
                "context": lane.context,
                "target_id": store.read_dokploy_target_id_record.return_value.target_id,
                "completed_at": "2026-10-26T00:00:00Z",
            }
        )
        snapshot = SimpleNamespace(
            snapshot_id=generation_binding(current),
            server_id="fixture-server",
            database_sha256="b" * 64,
            captured_at="2026-10-26T00:00:00Z",
        )
        counts = dict(active_rows=12, duplicate_groups=1, duplicate_rows=3, missing_key_rows=2)

        def receive(**kwargs: object) -> None:
            callback = kwargs["receive"]
            assert callable(callback)
            callback({"type": "complete", "counts": counts})

        with (
            patch.object(reader, "preview_snapshot", return_value=snapshot),
            patch.object(reader, "require_distinct_testing_credentials"),
            patch.object(
                reader, "read_current_testing_generation", return_value=current
            ) as read_generation,
            patch.object(
                reader, "select_stable_runtime", return_value=fixture.connection.selection
            ) as select,
            patch.object(reader, "assert_selection_current"),
            patch.object(
                source,
                "read_dokploy_config",
                return_value=("https://provider.invalid", "fixture"),
            ),
            patch.object(
                api,
                "fetch_dokploy_target_payload",
                return_value={"serverId": "fixture-server", "appName": current.namespace},
            ) as provider,
            patch.object(
                reader, "testing_drill_environment", return_value={"ODOO_DB_NAME": "fixture-copy"}
            ),
            patch.object(reader, "inspect_native_testing_receiver", side_effect=receive) as inspect,
        ):
            query = reader.AggregateQuery(table="product_template", key="first_mpn")
            result = reader.read_aggregates(store=store, product=profile.product, query=query)
            self.assertEqual(result.counts.duplicate_rows, 3)
            self.assertEqual(result.snapshot_id, snapshot.snapshot_id)
            self.assertEqual(select.call_args.args[2], "testing")
            inspect.reset_mock()
            provider.reset_mock()
            snapshot.snapshot_id = "c" * 64
            with self.assertRaises(ValueError):
                reader.read_aggregates(store=store, product=profile.product, query=query)
            inspect.assert_not_called()
            provider.assert_not_called()
            snapshot.snapshot_id = generation_binding(current)
            read_generation.side_effect = [current, None]
            with self.assertRaises(ValueError):
                reader.read_aggregates(store=store, product=profile.product, query=query)
