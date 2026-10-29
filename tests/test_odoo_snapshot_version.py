from io import BytesIO
import unittest

from control_plane import odoo_copy_aggregates as reader


MODULE_ROWS = (
    b"-- archive\nCOPY public.ir_module_module (id, name, state, latest_version) FROM stdin;\n"
    b"1\tmail\tinstalled\t20.0.1.0\n"
    b"2\tbase\tinstalled\t19.0.1.3\n\\.\n-- done\n"
)


class SnapshotVersionParserTests(unittest.TestCase):
    def test_reports_only_recorded_base_version_without_claiming_preparation(self) -> None:
        self.assertEqual(
            reader.read_base_module_version(BytesIO(MODULE_ROWS)),
            {"recorded_base_version": "19.0.1.3", "preparation_stage": None},
        )

    def test_missing_ambiguous_malformed_or_incomplete_metadata_refuses(self) -> None:
        for rows in (
            MODULE_ROWS.replace(b"\tbase\t", b"\tother\t"),
            MODULE_ROWS.replace(b"\\.\n", b"3\tbase\tinstalled\t20.0.1.0\n\\.\n"),
            MODULE_ROWS.replace(b"19.0.1.3", b"unknown"),
            MODULE_ROWS.replace(b"base\tinstalled", b"base\tto upgrade"),
            MODULE_ROWS.replace(b"latest_version", b"private"),
            MODULE_ROWS.replace(b"\\.\n", b""),
            MODULE_ROWS + MODULE_ROWS,
            MODULE_ROWS.replace(b"ir_module_module", b"res_users"),
        ):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                reader.read_base_module_version(BytesIO(rows))


class SnapshotVersionBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from control_plane.clean_testing_snapshot import CleanTestingSnapshot
        from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget
        from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
        from control_plane.odoo_preview_apply_execution import prepare_odoo_preview_apply_operation
        from control_plane.odoo_preview_apply_http import (
            OdooPreviewApplyEnvelope,
            ODOO_PREVIEW_APPLY_ROUTE,
        )
        from control_plane.storage.postgres import PostgresRecordStore
        from tests.support.profiles import _odoo_preview_profile_payload
        from tests.support.stores import _sqlite_database_url
        from tests.test_odoo_preview_apply_execution import _refresh_payload, _issued_plan
        from control_plane import odoo_snapshot_version as version

        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.store = PostgresRecordStore(database_url=_sqlite_database_url(root / "state.sqlite"))
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_preview_profile_payload()
        )
        self.store.write_product_profile_record(self.profile)
        self.snapshot = CleanTestingSnapshot(
            product=self.profile.product,
            context=self.profile.preview.context,
            snapshot_id="a" * 64,
            server_id="fixture-server",
            database_sha256="b" * 64,
            filestore_sha256="c" * 64,
            captured_at="2026-10-20T00:00:00Z",
            storage_budget=OdooTestingDrillStorageBudget(
                database_bytes=1024,
                filestore_bytes=1024,
                staging_bytes=1024,
                reserve_bytes=1024,
            ),
        )
        request = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
        request.apply.dry_run_plan.clean_snapshot = self.snapshot
        self.issued = _issued_plan(request)
        assert self.issued.plan_provenance is not None
        operation = prepare_odoo_preview_apply_operation(
            control_plane_root=root,
            record_store=self.store,
            profile=self.profile,
            apply_request=request,
            issued_plan=self.issued,
            reservation_scope="fixture-preview",
            idempotency_key=self.issued.plan_provenance.plan_id,
            request_fingerprint="fixture",
            trace_id="fixture",
        )
        self.reservation = self.store.reserve_mutation(
            scope=operation.reservation_scope,
            route_path=ODOO_PREVIEW_APPLY_ROUTE,
            idempotency_key=operation.idempotency_key,
            request_fingerprint="fixture",
            lease_owner="fixture",
            reconciliation_key=operation.adapter.reconciliation_key(),
            provider_target_key=operation.adapter.target_key(),
            original_intent=operation.adapter.original_intent(),
        ).record
        assert self.reservation is not None
        self.query = version.SnapshotVersionQuery(
            original_reservation_id=self.reservation.record_id,
            expected_snapshot_id=self.snapshot.snapshot_id,
            expected_database_sha256=self.snapshot.database_sha256,
        )

    def test_original_snapshot_is_read_without_current_publication_or_mutating_history(
        self,
    ) -> None:
        from unittest.mock import patch
        from control_plane import odoo_snapshot_version as version

        before = self.store.read_original_mutation_intent(self.reservation)
        with patch.object(
            version,
            "read_snapshot_archive",
            return_value=[
                {
                    "type": "complete",
                    "metadata": reader.read_base_module_version(BytesIO(MODULE_ROWS)),
                }
            ],
        ) as archive:
            result = version.read_snapshot_version(
                store=self.store, product=self.profile.product, query=self.query
            )
        self.assertEqual(result.snapshot_id, self.snapshot.snapshot_id)
        self.assertEqual(archive.call_args.kwargs["snapshot"], self.snapshot)
        self.assertEqual(archive.call_args.kwargs["query"], {"table": "ir_module_module"})
        self.assertIsNone(
            self.store.read_clean_testing_snapshot(
                self.profile.product, self.profile.preview.context
            )
        )
        self.assertEqual(self.store.read_original_mutation_intent(self.reservation), before)
        self.assertEqual(
            self.store.read_idempotency_record_by_id(self.reservation.record_id), self.reservation
        )

    def test_wrong_original_snapshot_hash_or_foreign_product_refuses_before_provider(self) -> None:
        from unittest.mock import patch
        from control_plane import odoo_snapshot_version as version

        for changes in (
            {"original_reservation_id": "mutation-reservation-" + "d" * 64},
            {"expected_snapshot_id": "d" * 64},
            {"expected_database_sha256": "d" * 64},
        ):
            with (
                self.subTest(changes=changes),
                patch.object(version, "read_snapshot_archive") as archive,
            ):
                with self.assertRaises(ValueError):
                    version.read_snapshot_version(
                        store=self.store,
                        product=self.profile.product,
                        query=self.query.model_copy(update=changes),
                    )
                archive.assert_not_called()
        changed = self.profile.model_copy(update={"product": "foreign-product"})
        self.store.write_product_profile_record(changed)
        with (
            patch.object(version, "read_snapshot_archive") as archive,
            self.assertRaises(ValueError),
        ):
            version.read_snapshot_version(
                store=self.store, product=changed.product, query=self.query
            )
        archive.assert_not_called()

    def test_invalid_retained_intent_or_malformed_decoder_result_never_qualifies(self) -> None:
        from unittest.mock import patch
        from control_plane import odoo_snapshot_version as version
        from control_plane.odoo_runtime_reads import OdooRuntimeReadError

        for frames in (
            [],
            [{"type": "complete", "unavailable": "archive_unreadable"}],
            [
                {
                    "type": "complete",
                    "metadata": {"recorded_base_version": "20.0", "preparation_stage": "complete"},
                }
            ],
        ):
            with (
                self.subTest(frames=frames),
                patch.object(version, "read_snapshot_archive", return_value=frames),
            ):
                with self.assertRaises((ValueError, OdooRuntimeReadError)):
                    version.read_snapshot_version(
                        store=self.store, product=self.profile.product, query=self.query
                    )
        with (
            patch.object(self.store, "read_original_mutation_intent", return_value={}),
            patch.object(version, "read_snapshot_archive") as archive,
        ):
            with self.assertRaises(ValueError):
                version.read_snapshot_version(
                    store=self.store, product=self.profile.product, query=self.query
                )
            archive.assert_not_called()

    def test_service_reuses_preview_read_scope_and_refuses_overrides_without_leaking_errors(
        self,
    ) -> None:
        import asyncio
        from typing import Any, cast
        from unittest.mock import Mock, patch
        from urllib.parse import urlencode
        from fastapi import FastAPI, HTTPException
        from control_plane import odoo_snapshot_version as version
        from control_plane.http_routes.preview_reservations import (
            register_preview_reservation_read_routes,
        )
        from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
        from control_plane.odoo_runtime_reads import OdooRuntimeReadError
        from tests.support.auth import _identity
        from tests.support.http import request

        allowed = Mock(return_value=True)

        def http_error(**kwargs: Any) -> HTTPException:
            return HTTPException(status_code=kwargs["status_code"], detail={"code": kwargs["code"]})

        app = FastAPI()
        register_preview_reservation_read_routes(
            cast(ApiRouteRegistrar, app),
            dependencies=ReadRouteDependencies(
                read_identity=lambda: _identity(),
                get_record_store=lambda: self.store,
                next_trace_id=lambda: "fixture",
                authorization_allows=allowed,
                http_error=http_error,
                error_response_model=version.SnapshotVersionResponse,
            ),
        )
        path = (
            version.ROUTE.format(product=self.profile.product)
            + "?"
            + urlencode(self.query.model_dump())
        )
        frames = [
            {"type": "complete", "metadata": reader.read_base_module_version(BytesIO(MODULE_ROWS))}
        ]
        with patch.object(version, "read_snapshot_archive", return_value=frames) as archive:
            response = asyncio.run(request(app, "GET", path))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json()["result"]["metadata"]["recorded_base_version"], "19.0.1.3"
            )
            self.assertIsNone(response.json()["result"]["metadata"]["preparation_stage"])
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(allowed.call_args.kwargs["action"], "preview.read")
            self.assertEqual(allowed.call_args.kwargs["context"], self.profile.preview.context)
            self.assertEqual(allowed.call_args.kwargs["product"], self.profile.product)
            archive.reset_mock()
            allowed.return_value = False
            self.assertEqual(asyncio.run(request(app, "GET", path)).status_code, 403)
            archive.assert_not_called()
            allowed.return_value = True
            for field in ("host", "sql", "table", "database", "server_id"):
                self.assertEqual(
                    asyncio.run(request(app, "GET", path + "&" + field + "=private")).status_code,
                    422,
                )
            archive.assert_not_called()
            for failure, status, code in (
                (ValueError("private token row"), 503, "snapshot_version_unavailable"),
                (
                    OdooRuntimeReadError("authorization_denied", "private token", 403),
                    403,
                    "authorization_denied",
                ),
            ):
                archive.side_effect = failure
                response = asyncio.run(request(app, "GET", path))
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["detail"]["code"], code)
                self.assertNotIn("private", response.text)

    def test_parser_bounds_apply_before_any_version_result(self) -> None:
        from unittest.mock import patch

        for limit in ("MAX_ROW_BYTES", "MAX_EXTRACTED_BYTES", "MAX_ROWS"):
            with (
                self.subTest(limit=limit),
                patch.object(reader, limit, 1),
                self.assertRaises(ValueError),
            ):
                reader.read_base_module_version(BytesIO(MODULE_ROWS))


class SnapshotVersionClientTests(unittest.TestCase):
    def test_client_selects_and_verifies_exact_original_without_another_credential(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO
        from pathlib import Path
        from tempfile import TemporaryDirectory
        import runpy
        import sys
        from unittest.mock import Mock, patch
        from control_plane import odoo_snapshot_version as version

        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/read-copy-aggregates.py"),
            run_name="snapshot_version_client_test",
        )
        query = version.SnapshotVersionQuery(
            original_reservation_id="mutation-reservation-" + "a" * 64,
            expected_snapshot_id="b" * 64,
            expected_database_sha256="c" * 64,
        )
        evidence = version.SnapshotVersionEvidence(
            product="example-odoo",
            context="example",
            original_reservation_id=query.original_reservation_id,
            snapshot_id=query.expected_snapshot_id,
            snapshot_database_sha256=query.expected_database_sha256,
            snapshot_captured_at="2026-10-20T00:00:00Z",
            observed_at="2026-10-29T00:00:00Z",
            metadata=version.SnapshotVersionMetadata(recorded_base_version="18.0.1.0"),
        )
        with TemporaryDirectory() as temporary:
            helper = Path(temporary) / "launchplane-write-action.py"
            helper.touch()
            read = Mock()
            transport = {
                "resolve_settings": Mock(
                    return_value={"service_url": "https://service.invalid", "token": "fixture"}
                ),
                "validate_service_url": Mock(),
                "request_launchplane_read": read,
                "public_trace_id": lambda value: value,
            }
            for mismatch in (False, True):
                response = version.SnapshotVersionResponse(
                    trace_id="fixture", result=evidence
                ).model_dump(mode="json")
                if mismatch:
                    response["result"]["snapshot_database_sha256"] = "d" * 64
                read.return_value = response
                output = StringIO()
                from contextlib import redirect_stderr

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
                            evidence.product,
                            "--original-reservation-id",
                            query.original_reservation_id,
                            "--expected-snapshot-id",
                            query.expected_snapshot_id,
                            "--expected-database-sha256",
                            query.expected_database_sha256,
                        ],
                    ),
                    redirect_stdout(output),
                    redirect_stderr(StringIO()),
                ):
                    self.assertEqual(client["main"](), 1 if mismatch else 0)
                if mismatch:
                    self.assertEqual(output.getvalue(), "")
                else:
                    self.assertIn(evidence.metadata.recorded_base_version, output.getvalue())
                self.assertEqual(
                    read.call_args.kwargs["query"],
                    query.model_dump(),
                )
