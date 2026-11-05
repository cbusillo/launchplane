from io import BytesIO
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from control_plane import odoo_copy_aggregates as reader
from control_plane import odoo_instance_overrides
from control_plane.odoo_runtime_reads import OdooRuntimeReadError
from control_plane.workflows.odoo_preview_runtime import OdooPreviewApplyInputsResult


MODULE_ROWS = (
    b"-- archive\nCOPY public.ir_module_module (id, name, state, latest_version) FROM stdin;\n"
    b"1\tmail\tinstalled\t20.0.1.0\n"
    b"2\tbase\tinstalled\t19.0.1.3\n\\.\n-- done\n"
)


class OrdinaryPreviewSourceVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        from control_plane import odoo_preview_apply_http as preview
        from control_plane.clean_testing_snapshot import CleanTestingSnapshot
        from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget
        from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
        from tests.support.profiles import _odoo_preview_profile_payload
        from tests.test_odoo_preview_apply_execution import _refresh_payload, _issued_plan

        self.preview = preview
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_preview_profile_payload()
        )
        self.profile.preview.clean_testing_snapshot = True
        budget = OdooTestingDrillStorageBudget(
            database_bytes=1024, filestore_bytes=1024, staging_bytes=1024, reserve_bytes=1024
        )
        self.profile.lanes[0].odoo_testing_drill_storage = budget
        self.snapshot = CleanTestingSnapshot(
            product=self.profile.product,
            context=self.profile.preview.context,
            snapshot_id="a" * 64,
            server_id="fixture-server",
            database_sha256="b" * 64,
            filestore_sha256="c" * 64,
            captured_at=datetime.now(timezone.utc).isoformat(),
            storage_budget=budget,
        )
        self.envelope = preview.OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
        self.issued = _issued_plan(self.envelope)
        self.store = Mock(spec=["read_clean_testing_snapshot"])
        self.store.read_clean_testing_snapshot.return_value = self.snapshot
        stack = self.enterContext(ExitStack())
        stack.enter_context(
            patch.object(preview, "build_odoo_preview_apply_inputs", return_value=self.issued)
        )
        stack.enter_context(
            patch.object(
                odoo_instance_overrides,
                "resolve_preview_website_bootstrap",
                return_value=(None, ""),
            )
        )
        stack.enter_context(
            patch.object(preview, "preview_has_completed_restore", return_value=False)
        )
        stack.enter_context(
            patch.object(
                preview,
                "_odoo_preview_service_environment",
                return_value=preview._OdooPreviewServiceEnvironment(values={}),
            )
        )
        from control_plane import odoo_snapshot_version as version

        self.archive = stack.enter_context(patch.object(version, "read_snapshot_archive"))

    def plan(self, source: str, target: str) -> OdooPreviewApplyInputsResult:
        from control_plane.contracts.artifact_identity import ArtifactBuildFlags

        assert self.issued.plan_request.manifest is not None
        self.issued.plan_request.manifest.build_flags = ArtifactBuildFlags(
            values={"odoo_version": target}
        )
        self.archive.return_value = [
            {
                "type": "complete",
                "metadata": {"recorded_base_version": source, "preparation_stage": None},
            }
        ]
        return self.preview._build_odoo_preview_apply_inputs(
            control_plane_root=Path("/synthetic-unused"),
            record_store=self.store,
            profile=self.profile,
            request=self.issued.plan_request,
            database_url=None,
        )

    def test_cross_major_snapshot_refuses_from_the_selected_archive(self) -> None:
        with self.assertRaises(self.preview.OdooPreviewPlanProvenanceError) as caught:
            self.plan("19.0.1.3", "20.0")
        self.assertEqual(caught.exception.code, "odoo_preview_source_major_mismatch")
        self.archive.assert_called_once()
        self.assertEqual(self.archive.call_args.kwargs["snapshot"], self.snapshot)

    def test_compatible_snapshot_records_actual_source_version(self) -> None:
        for major in (19, 20):
            with self.subTest(major=major):
                version = f"{major}.0.1.3"
                result = self.plan(version, f"{major}.0")
                self.assertEqual(result.status, "ready")
                self.assertEqual(result.clean_snapshot_base_version, version)
                self.assertEqual(result.dry_run_plan.clean_snapshot, self.snapshot)

    def test_unavailable_or_unknown_version_refuses_without_claiming_compatibility(self) -> None:
        for source, target in (("19.0.1.3", ""), ("unknown", "20.0")):
            with self.subTest(source=source, target=target):
                with self.assertRaises(self.preview.OdooPreviewPlanProvenanceError) as caught:
                    self.plan(source, target)
                self.assertEqual(caught.exception.code, "odoo_preview_source_version_unavailable")

    def test_archive_unavailable_refuses_and_explicit_upstream_route_stays_separate(self) -> None:
        self.archive.side_effect = OdooRuntimeReadError(
            "archive_tool_unavailable", "unavailable", 503
        )
        with self.assertRaises(self.preview.OdooPreviewPlanProvenanceError) as caught:
            self.plan("19.0", "20.0")
        self.assertEqual(caught.exception.code, "odoo_preview_source_version_unavailable")
        self.archive.reset_mock()
        self.profile.preview.clean_testing_snapshot = False
        self.profile.preview.data_transport_mode = "driver"
        result = self.plan("19.0", "20.0")
        self.assertEqual(result.status, "ready")
        self.archive.assert_not_called()
        self.store.read_clean_testing_snapshot.assert_called_once()

    def test_apply_reuses_only_fingerprinted_same_snapshot_version(self) -> None:
        result = self.plan("19.0.1.3", "19.0")
        issued = self.preview.issue_odoo_preview_apply_plan(result=result, plan_id="version-plan")
        envelope = self.envelope.model_copy(
            update={
                "apply": self.envelope.apply.model_copy(
                    update={
                        "dry_run_plan": issued.dry_run_plan,
                        "manifest": issued.plan_request.manifest,
                    }
                )
            }
        )
        self.preview.refresh_odoo_preview_issued_plan(
            control_plane_root=Path("/synthetic-unused"),
            record_store=self.store,
            profile=self.profile,
            request=envelope,
            issued_plan=issued,
            database_url=None,
        )
        self.archive.assert_called_once()
        tampered = issued.model_copy(update={"clean_snapshot_base_version": "20.0"})
        with self.assertRaises(self.preview.OdooPreviewPlanProvenanceError) as caught:
            self.preview.refresh_odoo_preview_issued_plan(
                control_plane_root=Path("/synthetic-unused"),
                record_store=self.store,
                profile=self.profile,
                request=envelope,
                issued_plan=tampered,
                database_url=None,
            )
        self.assertEqual(caught.exception.code, "odoo_preview_plan_mismatch")
        self.archive.assert_called_once()
        self.store.read_clean_testing_snapshot.return_value = self.snapshot.model_copy(
            update={"snapshot_id": "d" * 64, "database_sha256": "e" * 64}
        )
        self.archive.return_value = [
            {
                "type": "complete",
                "metadata": {"recorded_base_version": "20.0", "preparation_stage": None},
            }
        ]
        with self.assertRaises(self.preview.OdooPreviewPlanProvenanceError) as caught:
            self.preview.refresh_odoo_preview_issued_plan(
                control_plane_root=Path("/synthetic-unused"),
                record_store=self.store,
                profile=self.profile,
                request=envelope,
                issued_plan=issued,
                database_url=None,
            )
        self.assertEqual(caught.exception.code, "odoo_preview_source_major_mismatch")
        self.assertEqual(self.archive.call_count, 2)


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

        for changes, status, code in (
            (
                {"original_reservation_id": "mutation-reservation-" + "d" * 64},
                404,
                "snapshot_original_unavailable",
            ),
            ({"expected_snapshot_id": "d" * 64}, 409, "snapshot_binding_mismatch"),
            ({"expected_database_sha256": "d" * 64}, 409, "snapshot_binding_mismatch"),
        ):
            with (
                self.subTest(changes=changes),
                patch.object(version, "read_snapshot_archive") as archive,
            ):
                with self.assertRaises(OdooRuntimeReadError) as raised:
                    version.read_snapshot_version(
                        store=self.store,
                        product=self.profile.product,
                        query=self.query.model_copy(update=changes),
                    )
                self.assertEqual(
                    (raised.exception.status_code, raised.exception.code), (status, code)
                )
                archive.assert_not_called()
        changed = self.profile.model_copy(update={"product": "foreign-product"})
        self.store.write_product_profile_record(changed)
        with (
            patch.object(version, "read_snapshot_archive") as archive,
            self.assertRaises(OdooRuntimeReadError) as raised,
        ):
            version.read_snapshot_version(
                store=self.store, product=changed.product, query=self.query
            )
        self.assertEqual(
            (raised.exception.status_code, raised.exception.code),
            (409, "snapshot_binding_mismatch"),
        )
        archive.assert_not_called()

    def test_archive_failure_stage_is_retained_without_provider_detail(self) -> None:
        from unittest.mock import patch
        from control_plane import odoo_snapshot_version as version
        from control_plane.odoo_runtime_reads import OdooRuntimeReadError

        for reason in reader.ARCHIVE_UNAVAILABLE_CODES:
            with (
                self.subTest(reason=reason),
                patch.object(
                    version,
                    "read_snapshot_archive",
                    return_value=[{"type": "complete", "unavailable": reason}],
                ),
            ):
                with self.assertRaises(OdooRuntimeReadError) as raised:
                    version.read_snapshot_version(
                        store=self.store, product=self.profile.product, query=self.query
                    )
                self.assertEqual(raised.exception.code, reason)

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
            with self.assertRaises(OdooRuntimeReadError) as raised:
                version.read_snapshot_version(
                    store=self.store, product=self.profile.product, query=self.query
                )
            self.assertEqual(
                (raised.exception.status_code, raised.exception.code),
                (409, "snapshot_binding_mismatch"),
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
            self.assertCountEqual(
                [call.kwargs["action"] for call in allowed.call_args_list],
                ["preview.read", "operations.read"],
            )
            scoped = next(
                call.kwargs
                for call in allowed.call_args_list
                if call.kwargs["action"] == "operations.read"
            )
            self.assertEqual(scoped["target"].instances, ("testing",))
            self.assertEqual(scoped["context"], self.profile.preview.context)
            self.assertEqual(scoped["product"], self.profile.product)
            archive.reset_mock()
            allowed.return_value = False
            self.assertEqual(asyncio.run(request(app, "GET", path)).status_code, 403)
            archive.assert_not_called()
            allowed.return_value = True
            allowed.side_effect = lambda **kwargs: kwargs["action"] != "operations.read"
            self.assertEqual(asyncio.run(request(app, "GET", path)).status_code, 403)
            archive.assert_not_called()
            allowed.side_effect = None
            for field in ("host", "sql", "table", "database", "server_id"):
                self.assertEqual(
                    asyncio.run(request(app, "GET", path + "&" + field + "=private")).status_code,
                    422,
                )
            archive.assert_not_called()
            import click

            for failure, status, code in (
                (FileNotFoundError("private missing path"), 404, "not_found"),
                (
                    click.ClickException("private provider token"),
                    503,
                    "snapshot_version_unavailable",
                ),
                (
                    OdooRuntimeReadError("snapshot_binding_mismatch", "private binding", 409),
                    409,
                    "snapshot_binding_mismatch",
                ),
                (
                    OdooRuntimeReadError("archive_tool_unavailable", "private utility", 503),
                    503,
                    "archive_tool_unavailable",
                ),
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

        for limit in ("MAX_EXTRACTED_BYTES", "MAX_ROWS"):
            with (
                self.subTest(limit=limit),
                patch.object(reader, limit, 1),
                self.assertRaises(ValueError),
            ):
                reader.read_base_module_version(BytesIO(MODULE_ROWS))
        header = next(
            line for line in MODULE_ROWS.splitlines(keepends=True) if line.startswith(b"COPY ")
        )
        with patch.object(reader, "MAX_ROW_BYTES", len(header) - 1), self.assertRaises(ValueError):
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

    def test_missing_or_cross_mode_flags_are_usage_errors_before_transport(self) -> None:
        from contextlib import redirect_stderr
        from io import StringIO
        from pathlib import Path
        import runpy
        import sys
        from unittest.mock import patch

        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/read-copy-aggregates.py"),
            run_name="snapshot_usage_test",
        )
        for arguments in (
            ["--original-reservation-id", "", "--key", "name"],
            ["--table", "", "--key", "name"],
            ["--table", "product_template", "--key", "name", "--active", ""],
            ["--table", "product_template", "--key", "name", "--expected-snapshot-id", ""],
            ["--table", "product_template"],
            ["--table", "product_template", "--key", "name", "--expected-snapshot-id", "a" * 64],
            ["--original-reservation-id", "mutation-reservation-" + "a" * 64],
            [
                "--original-reservation-id",
                "mutation-reservation-" + "a" * 64,
                "--expected-snapshot-id",
                "a" * 64,
                "--expected-database-sha256",
                "b" * 64,
                "--active=enabled",
            ],
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(runpy, "run_path") as transport,
                patch.object(
                    sys,
                    "argv",
                    [
                        "read",
                        "--operator-helper",
                        "/unused/launchplane-write-action.py",
                        "--product",
                        "example-odoo",
                        *arguments,
                    ],
                ),
                redirect_stderr(StringIO()),
            ):
                with self.assertRaises(SystemExit) as raised:
                    client["main"]()
                self.assertEqual(raised.exception.code, 2)
                transport.assert_not_called()
