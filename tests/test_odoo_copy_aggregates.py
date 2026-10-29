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
from urllib.parse import urlencode
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
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                reader.AggregateCounts.model_validate(values)


class SnapshotReaderTests(unittest.TestCase):
    def test_verified_archive_uses_only_offline_decoder_and_refuses_tampering(self) -> None:
        self.verify_offline_archive(
            reader.SNAPSHOT_AGGREGATE_PROGRAM,
            COPY_ROWS,
            "counts",
            reader.count_archive_rows(BytesIO(COPY_ROWS), QUERY.model_dump()),
        )

    def test_version_metadata_uses_the_same_verified_offline_archive_and_tamper_refusals(
        self,
    ) -> None:
        from tests.test_odoo_snapshot_version import MODULE_ROWS

        self.verify_offline_archive(
            reader.SNAPSHOT_VERSION_PROGRAM,
            MODULE_ROWS,
            "metadata",
            reader.read_base_module_version(BytesIO(MODULE_ROWS)),
        )

    def verify_offline_archive(
        self,
        program: str,
        rows: bytes,
        field: str,
        expected: object,
    ) -> None:
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
                query=QUERY.model_dump() if field == "counts" else {"table": "ir_module_module"},
                project="fixture-copy",
            )
            process = Mock(stdout=BytesIO(rows))
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
                    exec(program, {})
                frame = json.loads(output.getvalue().removeprefix("FRAME:"))
                self.assertEqual(frame[field], expected)
                arguments = decode.call_args.args[0]
                self.assertIn("--file=-", arguments)
                environment = arguments.index("env")
                decoder = arguments.index("pg_restore")
                self.assertLess(environment, decoder)
                self.assertEqual(arguments[environment + 1], "-i")
                self.assertTrue(
                    all(
                        argument.startswith("PATH=")
                        for argument in arguments[environment + 2 : decoder]
                    )
                )
                self.assertEqual(
                    [argument for argument in arguments if argument.startswith("--table=")],
                    ["--table=" + (QUERY.table if field == "counts" else "ir_module_module")],
                )
                self.assertIs(decode.call_args.kwargs["stdin"].closed, True)
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
                    exec(program, {})
                self.assertEqual(
                    json.loads(output.getvalue().removeprefix("FRAME:"))["unavailable"],
                    "snapshot_unavailable",
                )
                decode.assert_not_called()
                archive.write_bytes(b"fixture custom archive")
                process.stdout = BytesIO(rows)
                digest = hashlib.file_digest

                def change_after_hash(stream: Any, algorithm: str) -> Any:
                    calculated = digest(stream, algorithm)
                    archive.write_bytes(b"changed after digest")
                    return calculated

                output = StringIO()
                with (
                    patch.object(hashlib, "file_digest", side_effect=change_after_hash),
                    redirect_stdout(output),
                ):
                    exec(program, {})
                self.assertNotIn(field, json.loads(output.getvalue().removeprefix("FRAME:")))

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
        path += "?" + urlencode(QUERY.model_dump(exclude_none=True))
        evidence = reader.AggregateEvidence(
            product=fixture.profile.product,
            context=fixture.context,
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
            response = await request(app, "GET", path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(allowed.call_args.kwargs["product"], fixture.profile.product)
            self.assertEqual(allowed.call_args.kwargs["target"].instances, ("testing",))
            read.reset_mock()
            self.assertEqual(
                (await request(app, "GET", path.replace("/testing/", "/prod/"))).status_code,
                400,
            )
            self.assertEqual((await request(app, "GET", path + "&host=prod")).status_code, 422)
            self.assertEqual(
                (
                    await request(
                        app,
                        "GET",
                        path + "&" + urlencode({"sql": "SELECT * FROM res_users"}),
                    )
                ).status_code,
                422,
            )
            allowed.return_value = False
            self.assertEqual((await request(app, "GET", path)).status_code, 403)
            read.assert_not_called()
            allowed.return_value = True
            without_testing = fixture.profile.model_copy(
                update={
                    "lanes": tuple(
                        lane for lane in fixture.profile.lanes if lane.instance != "testing"
                    )
                }
            )
            store.read_product_profile_record.return_value = without_testing
            store.list_product_profile_records.return_value = (without_testing,)
            self.assertEqual((await request(app, "GET", path)).status_code, 400)
            read.assert_not_called()
            store.read_product_profile_record.return_value = fixture.profile
            store.list_product_profile_records.return_value = (fixture.profile,)
            read.side_effect = ValueError("private database credentials")
            response = await request(app, "GET", path)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private", response.text)


class AggregateClientTests(unittest.TestCase):
    def test_error_codes_are_bounded_and_messages_never_leave_the_client(self) -> None:
        from contextlib import redirect_stderr
        from email.message import Message
        import runpy
        from urllib.error import HTTPError

        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/read-copy-aggregates.py"),
            run_name="aggregate_client_test",
        )
        with TemporaryDirectory() as temporary:
            helper = Path(temporary) / "launchplane-write-action.py"
            helper.write_text("")
            for service_code, expected in (
                ("snapshot_unavailable", "snapshot_unavailable"),
                ("private_secret", "aggregate_unavailable"),
            ):
                failure = HTTPError(
                    "https://service.invalid/read",
                    503,
                    "private message",
                    Message(),
                    BytesIO(
                        json.dumps(
                            {
                                "trace_id": "fixture-trace",
                                "error": {"code": service_code, "message": "private row detail"},
                            }
                        ).encode()
                    ),
                )
                transport = {
                    "resolve_settings": Mock(
                        return_value={
                            "service_url": "https://service.invalid",
                            "token": "fixture-token",
                        }
                    ),
                    "validate_service_url": Mock(),
                    "request_launchplane_read": Mock(side_effect=failure),
                    "public_trace_id": lambda value: value,
                }
                output = StringIO()
                with (
                    patch.object(runpy, "run_path", return_value=transport),
                    patch.object(
                        sys,
                        "argv",
                        [
                            "read",
                            "--operator-helper",
                            str(helper),
                            "--product",
                            "example-odoo",
                            "--table",
                            "product_template",
                            "--key",
                            "first_mpn",
                        ],
                    ),
                    redirect_stderr(output),
                ):
                    self.assertEqual(client["main"](), 1)
                result = json.loads(output.getvalue())
                self.assertEqual(result["error_code"], expected)
                self.assertEqual(result["trace_id"], "fixture-trace")
                self.assertNotIn("private", output.getvalue())
                self.assertNotIn("fixture-token", output.getvalue())
