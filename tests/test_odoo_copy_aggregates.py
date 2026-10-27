import base64
from contextlib import redirect_stdout
import hashlib
from io import BytesIO, StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from control_plane import odoo_copy_aggregates as reader
from control_plane._odoo_clean_snapshot_program import CLEAN_TESTING_SNAPSHOT_LIBRARY
from control_plane.clean_testing_snapshot import CleanTestingSnapshot
from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget
from control_plane.dokploy import source, api
from control_plane.http_routes.odoo_runtime import (
    OdooRuntimeReadDependencies,
    register_odoo_runtime_read_routes,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.odoo_testing_restore import generation_binding
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_odoo_import_diagnostics import DiagnosticFixture
from tests.test_odoo_testing_restore import generation
from tests.support.auth import _identity
from tests.support.http import request


COPY_ROWS = (
    b"-- Archive output\nCOPY public.product_template (active, first_mpn, manufacturer, personal) FROM stdin;\n"
    b"t\tABC\t1\tprivate@example.invalid\n"
    b"f\tabc\t1\tprivate@example.invalid\n"
    b"t\t ABC \t1\tprivate@example.invalid\n"
    b"t\tabc\t2\tprivate@example.invalid\n"
    b"t\t\\N\t1\tprivate@example.invalid\n"
    b"t\t\t1\tprivate@example.invalid\n"
    b"\\.\n-- Archive completed\n"
)
QUERY = reader.AggregateQuery(table="product_template", key="first_mpn", partition="manufacturer")


class ArchiveCountTests(unittest.TestCase):
    def test_archive_counts_group_without_exporting_fields_and_decode_copy_escapes(self) -> None:
        counts = reader.count_archive_rows(BytesIO(COPY_ROWS), QUERY.model_dump())
        self.assertEqual(
            counts, dict(active_rows=5, duplicate_groups=1, duplicate_rows=3, missing_key_rows=2)
        )
        self.assertNotIn("private", json.dumps(counts))
        for escaped, decoded in (
            (b"\\N", None),
            (b"\\\\N", r"\N"),
            (b"a\\tb", "a\tb"),
            (b"\\303\\251", "é"),
            (b"\\x41", "A"),
        ):
            with self.subTest(escaped=escaped):
                self.assertEqual(reader.decode_copy_field(escaped), decoded)

    def test_incomplete_foreign_duplicate_or_oversized_tables_never_produce_counts(self) -> None:
        for data in (
            COPY_ROWS.replace(b"product_template", b"res_users"),
            COPY_ROWS.replace(b"\\.\n", b""),
            COPY_ROWS + COPY_ROWS,
            COPY_ROWS.replace(b"t\tABC", b"notbool\tABC"),
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                reader.count_archive_rows(BytesIO(data), QUERY.model_dump())
        for limit in ("MAX_ROW_BYTES", "MAX_EXTRACTED_BYTES", "MAX_GROUP_BYTES", "MAX_ROWS"):
            with (
                self.subTest(limit=limit),
                patch.object(reader, limit, 1),
                self.assertRaises(ValueError),
            ):
                reader.count_archive_rows(BytesIO(COPY_ROWS), QUERY.model_dump())

    def test_query_and_output_accept_only_identifiers_and_bounded_integer_counts(self) -> None:
        for values in (
            {"table": "product_template;DELETE FROM res_users", "key": "first_mpn"},
            {**QUERY.model_dump(), "sql": "SELECT * FROM res_users"},
            {**QUERY.model_dump(), "database": "prod"},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                reader.AggregateQuery.model_validate(values)
        counts = dict(active_rows=10, duplicate_groups=2, duplicate_rows=5, missing_key_rows=1)
        for values in (
            {**counts, "rows": [{"email": "private@example.invalid"}]},
            {**counts, "active_rows": True},
            {**counts, "active_rows": -1},
            {**counts, "active_rows": 2**63},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                reader.AggregateCounts.model_validate(values)


class SnapshotReaderTests(unittest.TestCase):
    def test_verified_archive_uses_only_offline_decoder_and_refuses_tampering(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            library: dict[str, Any] = {}
            exec(CLEAN_TESTING_SNAPSHOT_LIBRARY, library)
            snapshot_id = "a" * 64
            with patch("pathlib.Path.home", return_value=root):
                path = library["clean_snapshot_root"](snapshot_id)
            path.mkdir()
            archive = path / "database.dump"
            archive.write_bytes(b"fixture custom archive")
            receipt = dict(
                snapshot_id=snapshot_id,
                database_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                filestore_sha256="b" * 64,
            )
            (path / "receipt.json").write_text(json.dumps(receipt))
            snapshot = CleanTestingSnapshot(
                product="example-odoo",
                context="example",
                server_id="fixture-server",
                captured_at="2026-10-26T00:00:00Z",
                storage_budget=OdooTestingDrillStorageBudget(
                    database_bytes=1024,
                    filestore_bytes=1024,
                    staging_bytes=1024,
                    reserve_bytes=1024,
                ),
                **receipt,
            )
            spec = dict(
                snapshot=snapshot.model_dump(mode="json"),
                query=QUERY.model_dump(),
                project="fixture-copy",
            )
            process = Mock(stdout=BytesIO(COPY_ROWS))
            process.wait.return_value = 0
            process.poll.return_value = 0
            files = {
                str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
            }
            with (
                patch("pathlib.Path.home", return_value=root),
                patch.object(
                    sys,
                    "argv",
                    ["read", base64.b64encode(json.dumps(spec).encode()).decode(), "FRAME:"],
                ),
                patch("signal.signal"),
                patch("signal.alarm"),
                patch(
                    "subprocess.check_output",
                    side_effect=lambda arguments, **kwargs: (
                        b"[]" if arguments[1] == "inspect" else b"a" * 64 + b"\n"
                    ),
                ),
                patch("subprocess.Popen", return_value=process) as decode,
            ):
                output = StringIO()
                with redirect_stdout(output):
                    exec(reader.SNAPSHOT_AGGREGATE_PROGRAM, {})
                frame = json.loads(output.getvalue().removeprefix("FRAME:"))
                self.assertEqual(frame["counts"]["duplicate_rows"], 3)
                arguments = decode.call_args.args[0]
                self.assertIn("--file=-", arguments)
                self.assertFalse(any(arg.startswith(("-d", "--dbname")) for arg in arguments))
                self.assertEqual(
                    files,
                    {
                        str(p.relative_to(root)): p.read_bytes()
                        for p in root.rglob("*")
                        if p.is_file()
                    },
                )
                decode.reset_mock()
                archive.write_bytes(b"tampered archive")
                output = StringIO()
                with redirect_stdout(output):
                    exec(reader.SNAPSHOT_AGGREGATE_PROGRAM, {})
                self.assertEqual(
                    json.loads(output.getvalue().removeprefix("FRAME:"))["unavailable"],
                    "snapshot_unavailable",
                )
                decode.assert_not_called()

    def test_matching_completed_provenance_required_before_any_provider_read(self) -> None:
        fixture = DiagnosticFixture()
        store = fixture.store
        current = generation().model_copy(
            update={
                "product": fixture.profile.product,
                "context": fixture.context,
                "completed_at": "2026-10-26T00:00:00Z",
            }
        )
        store.read_fresh_odoo_testing_generation.return_value = current
        store.list_dokploy_target_id_records.return_value = (
            store.read_dokploy_target_id_record.return_value,
        )
        snapshot = Mock(
            snapshot_id=generation_binding(current),
            server_id="fixture-server",
            database_sha256="b" * 64,
            captured_at="2026-10-26T00:00:00Z",
        )
        snapshot.model_dump.return_value = {}
        counts = dict(active_rows=12, duplicate_groups=1, duplicate_rows=3, missing_key_rows=2)

        def remote(**kwargs: Any) -> None:
            kwargs["receive"]({"type": "complete", "counts": counts})

        with (
            patch.object(reader, "preview_snapshot", return_value=snapshot),
            patch.object(
                source, "read_dokploy_config", return_value=("https://provider.invalid", "fixture")
            ),
            patch.object(
                api,
                "fetch_dokploy_target_payload",
                return_value={
                    "serverId": "fixture-server",
                    "name": fixture.store.read_dokploy_target_record.return_value.target_name,
                    "appName": "fixture-copy",
                },
            ) as provider,
            patch.object(reader, "_read_remote", side_effect=remote) as execute,
        ):
            result = reader.read_aggregates(
                store=store, product=fixture.profile.product, query=QUERY
            )
            self.assertEqual(result.counts.duplicate_rows, 3)
            self.assertEqual(result.snapshot_id, snapshot.snapshot_id)
            execute.reset_mock()
            provider.reset_mock()
            snapshot.snapshot_id = "c" * 64
            with self.assertRaises(ValueError):
                reader.read_aggregates(store=store, product=fixture.profile.product, query=QUERY)
            execute.assert_not_called()
            provider.assert_not_called()


class AggregateRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_product_read_scope_no_production_no_overrides_and_fail_closed(self) -> None:
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
                    error_response_model=reader.AggregateCounts,
                ),
                control_plane_root=Path("fixture"),
                database_url=None,
            ),
        )
        path = reader.ROUTE.format(product=fixture.profile.product, environment="testing")
        evidence = reader.AggregateEvidence(
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
            counts=reader.AggregateCounts(
                active_rows=12, duplicate_groups=1, duplicate_rows=3, missing_key_rows=2
            ),
        )
        with patch.object(reader, "read_aggregates", return_value=evidence) as read:
            response = await request(app, "POST", path, payload=QUERY.model_dump())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(allowed.call_args.kwargs["product"], fixture.profile.product)
            self.assertEqual(allowed.call_args.kwargs["target"].instances, ("testing",))
            read.reset_mock()
            for invalid in (path.replace("/testing/", "/prod/"), path + "?host=prod"):
                self.assertEqual(
                    (await request(app, "POST", invalid, payload=QUERY.model_dump())).status_code,
                    400,
                )
            self.assertEqual(
                (
                    await request(
                        app,
                        "POST",
                        path,
                        payload={**QUERY.model_dump(), "sql": "SELECT * FROM res_users"},
                    )
                ).status_code,
                422,
            )
            allowed.return_value = False
            self.assertEqual(
                (await request(app, "POST", path, payload=QUERY.model_dump())).status_code, 403
            )
            read.assert_not_called()
            allowed.return_value = True
            read.side_effect = ValueError("private database credentials")
            response = await request(app, "POST", path, payload=QUERY.model_dump())
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private", response.text)
