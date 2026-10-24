from contextlib import contextmanager
from collections.abc import Iterator, Sequence
from pathlib import Path
import os
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.odoo_connection_isolation import CONNECTION_ISOLATION_PARAMETERS
from control_plane.odoo_preview_restore import preview_restore_program
from control_plane import odoo_preview_restore
from tests.test_odoo_connection_isolation import SqliteCursor, seed


class ReadOnlyCursor(SqliteCursor):
    def execute(self, query: str, params: Any = ()) -> None:
        if query != "SET TRANSACTION READ ONLY":
            super().execute(query, params)


class PreviewConnection:
    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path)

    @contextmanager
    def cursor(self) -> Iterator[ReadOnlyCursor]:
        yield ReadOnlyCursor(self.connection)

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.connection.close()


class PreviewRestoreTests(unittest.TestCase):
    def test_source_process_refuses_root_capabilities_or_privilege_escalation(self) -> None:
        for uid, root_access, privileges, capabilities in (
            (1000, False, 1, 0),
            (0, True, 1, 0),
            (1000, True, 1, 0),
            (1000, False, 0, 0),
            (1000, False, 1, 1),
        ):
            status = f"NoNewPrivs:\t{privileges}\nCapEff:\t{capabilities:x}\nCapPrm:\t{capabilities:x}\nCapAmb:\t{capabilities:x}\n"
            with (
                self.subTest(
                    uid=uid,
                    root_access=root_access,
                    privileges=privileges,
                    capabilities=capabilities,
                ),
                patch("os.geteuid", return_value=uid),
                patch("os.access", return_value=root_access),
                patch.object(Path, "read_text", return_value=status),
            ):
                program = odoo_preview_restore.preview_source_process_program()
                if uid == 1000 and not root_access and privileges == 1 and capabilities == 0:
                    exec(program, {})
                else:
                    with self.assertRaises(SystemExit):
                        exec(program, {})

    def test_capture_cleanup_removes_only_current_preview_capture_and_temporary_identity(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / ".preview-upstream-restore"
            capture.mkdir()
            (capture / "database.dump").write_text("synthetic")
            identity = root / "identity"
            identity.mkdir()
            sentinel = root / "other-preview-data"
            sentinel.write_text("preserved")
            with (
                patch.object(odoo_preview_restore, "PREVIEW_WORKFLOW_SSH_DIRECTORY", identity),
                patch.dict(
                    os.environ,
                    {
                        "ODOO_DB_NAME": "preview",
                        "ODOO_DATA_WORKFLOW_LOCK_FILE": str(root / ".lock"),
                    },
                ),
            ):
                exec(odoo_preview_restore.preview_capture_cleanup_program(), {})
            self.assertFalse(capture.exists())
            self.assertFalse(identity.exists())
            self.assertTrue(sentinel.exists())

    def test_connections_are_scrubbed_and_dump_removed_before_addon_code(self) -> None:
        self._exercise(False)

    def test_failed_credential_boundary_removes_unsanitized_recovery_dump(self) -> None:
        self._exercise(True)

    def _exercise(self, fail_boundary: bool) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "database.sqlite"
            connection = sqlite3.connect(database)
            seed(connection)
            connection.close()
            dump_directory = root / ".preview-upstream-restore"
            dump_directory.mkdir()
            dump = dump_directory / "database.dump"
            dump.write_text("synthetic unsanitized capture")
            settings = SimpleNamespace(
                host="database",
                platform_instance="preview",
                db_name="preview",
                data_workflow_lock_file=root / ".workflow-lock",
            )
            identity_directory = root / "identity"
            identity_directory.mkdir()
            (identity_directory / "synthetic-identity").write_text("synthetic")
            boundary_calls: list[bool] = []
            hooks: list[str] = []
            case = self

            class Runner:
                def __init__(self, local: Any, _upstream: object, _environment: object) -> None:
                    self.local = local
                    self.connection = PreviewConnection(database)
                    self.os_env = dict(os.environ)

                def connect_to_db(self) -> PreviewConnection:
                    return self.connection

                def _connect_with_retry(self, _database: str) -> PreviewConnection:
                    return PreviewConnection(database)

                def prepare_credentials_before_registry(
                    self, *, restored_copy: bool = False, do_sanitize: bool = False
                ) -> None:
                    boundary_calls.append(restored_copy and do_sanitize)
                    if fail_boundary:
                        raise RuntimeError("credential boundary refused")

                def _prepare_restored_database(
                    self, target_owner: str | None, *, do_sanitize: bool
                ) -> None:
                    case.assertFalse(identity_directory.exists())
                    for environment in (self.os_env, os.environ):
                        case.assertFalse(
                            any(
                                key.startswith(("ODOO_UPSTREAM_", "DATA_WORKFLOW_SSH_"))
                                for key in environment
                            )
                        )
                    hooks.append("addon code")

            workflow = ModuleType("run_odoo_data_workflows")
            workflow.LocalServerSettings = lambda: settings  # type: ignore[attr-defined]
            workflow.OdooDataWorkflowRunner = Runner  # type: ignore[attr-defined]

            def main(_arguments: Sequence[str]) -> int:
                runner = workflow.OdooDataWorkflowRunner(settings, None, None)
                try:
                    runner.prepare_credentials_before_registry(restored_copy=True, do_sanitize=True)
                    case.assertFalse(dump.exists())
                    observer = sqlite3.connect(database)
                    try:
                        for key, value in CONNECTION_ISOLATION_PARAMETERS.items():
                            case.assertEqual(
                                observer.execute(
                                    "SELECT value FROM ir_config_parameter WHERE key = ?", (key,)
                                ).fetchone(),
                                (value,),
                            )
                        case.assertEqual(
                            observer.execute(
                                "SELECT stock_move_sms_validation FROM res_company"
                            ).fetchone(),
                            (0,),
                        )
                        case.assertFalse(
                            any(
                                value
                                for row in observer.execute(
                                    "SELECT usps_api_key,ups_client_secret FROM delivery_carrier"
                                )
                                for value in row
                            )
                        )
                    finally:
                        observer.close()
                    runner._prepare_restored_database(None, do_sanitize=True)
                    return 0
                finally:
                    runner.connection.close()

            workflow.main = main  # type: ignore[attr-defined]
            with (
                patch.object(
                    odoo_preview_restore, "preview_source_process_program", return_value=""
                ),
                patch.object(
                    odoo_preview_restore, "PREVIEW_WORKFLOW_SSH_DIRECTORY", identity_directory
                ),
                patch.object(sys, "path", list(sys.path)),
                patch.dict(sys.modules, {workflow.__name__: workflow}),
                patch.dict(
                    os.environ,
                    {
                        "PLATFORM_INSTANCE": "preview",
                        "ENV_OVERRIDE_DISABLE_CRON": "false",
                        "NO_SANITIZE": "true",
                        "BOOTSTRAP": "true",
                        "DATA_WORKFLOW_SSH_DIR": str(identity_directory),
                        "DATA_WORKFLOW_SSH_KEY": str(identity_directory / "synthetic-identity"),
                        "ODOO_UPSTREAM_HOST": "source.example.test",
                    },
                ),
            ):
                if fail_boundary:
                    with self.assertRaisesRegex(RuntimeError, "credential boundary refused"):
                        exec(preview_restore_program(), {})
                else:
                    with self.assertRaises(SystemExit) as completed:
                        exec(preview_restore_program(), {})
                    self.assertEqual(completed.exception.code, 0)
                self.assertEqual(os.environ["ENV_OVERRIDE_DISABLE_CRON"], "true")
                self.assertEqual(os.environ["NO_SANITIZE"], "false")
                self.assertEqual(os.environ["BOOTSTRAP"], "false")
            self.assertEqual(boundary_calls, [True])
            self.assertEqual(bool(hooks), not fail_boundary)
            self.assertFalse(dump_directory.exists())
