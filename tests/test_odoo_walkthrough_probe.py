import base64
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import sqlite3
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.odoo_walkthrough import (
    WALKTHROUGH_ACTIVE_STATE,
    WALKTHROUGH_LOGIN_PREFIX,
)
from control_plane.contracts.runtime_identity import (
    RuntimeIdentity,
    RUNTIME_IDENTITY_ENV_KEY,
    runtime_identity_env,
)
from control_plane.dokploy.odoo_walkthrough import (
    ACCOUNT_DATABASE_PROBE,
    ACCOUNT_JOURNAL_READ_PROGRAM,
    WALKTHROUGH_JOURNAL_DIRECTORY,
)
from control_plane.odoo_testing_restore import TESTING_COPY_COMPLETION_PARAMETER
from control_plane.odoo_connection_isolation import connection_isolation_library
from tests.test_odoo_connection_isolation import SqliteCursor, seed


class ProbeConnection:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.readonly = False
        self.closed = False
        self.free = True
        self.used = False
        self.copy = spec["copy_binding"]
        self.active = True
        self.statement = ""
        self.parameters: tuple[object, ...] = ()
        self.database = sqlite3.connect(":memory:")
        seed(self.database)
        self.sql = SqliteCursor(self.database)
        namespace: dict[str, Any] = {}
        exec(connection_isolation_library(), namespace)
        self.repair = namespace["connection_isolation"]
        self.repair(self.sql, apply=True)
        self.sql_result = False
        self.isolation_readable = True

    def set_session(self, *, readonly: bool, autocommit: bool) -> None:
        self.readonly = readonly and not autocommit
        if not self.isolation_readable:
            raise RuntimeError("Database read is unavailable")

    def cursor(self) -> "ProbeConnection":
        return self

    def __enter__(self) -> "ProbeConnection":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> None:
        if not self.readonly or not statement.strip().upper().startswith(("SELECT", "SET", "WITH")):
            raise AssertionError("Probe attempted a write")
        self.statement, self.parameters = statement, parameters
        self.sql_result = not any(
            marker in statement
            for marker in (
                "SET LOCAL",
                "pg_try_advisory_xact_lock",
                "res_users_log",
                "SELECT value FROM ir_config_parameter WHERE key=%s",
                "SELECT login,active FROM res_users",
            )
        )
        if self.sql_result:
            self.sql.execute(statement, parameters)

    def fetchone(self) -> tuple[object, ...]:
        if self.sql_result:
            return tuple(self.sql.fetchone())
        if "pg_try_advisory_xact_lock" in self.statement:
            return (self.free,)
        if "res_users_log" in self.statement:
            return (self.used,)
        if "ir_config_parameter" in self.statement:
            if self.parameters[0] == TESTING_COPY_COMPLETION_PARAMETER:
                return (self.copy,)
            return (
                json.dumps(
                    {
                        "binding": self.spec["binding"],
                        "account_id": 17,
                        "state": WALKTHROUGH_ACTIVE_STATE,
                    }
                ),
            )
        if "res_users" in self.statement:
            return (WALKTHROUGH_LOGIN_PREFIX + self.spec["task_id"], self.active)
        raise AssertionError("Unexpected database read")

    def fetchall(self) -> list[tuple[str]]:
        return list(self.sql.fetchall())

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class NativeProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.identity = RuntimeIdentity(
            product="example",
            context="example",
            instance="testing",
            deployment_record_id="deployment",
            artifact_id="artifact",
            source_git_ref="a" * 40,
            image_reference="example@sha256:" + "b" * 64,
        )
        self.spec: dict[str, Any] = {
            "mode": "quiescence",
            "dispatch_id": "d" * 64,
            "dispatch_deadline": int(time.time()) - 1,
            "copy_binding": "c" * 64,
            "binding": "e" * 64,
            "task_id": "f" * 32,
            "project": "testing-project",
            "database": "testing",
            "identity_key": RUNTIME_IDENTITY_ENV_KEY,
            "identity": self.identity.model_dump(mode="json"),
            "image": self.identity.image_reference,
            "usage_since": datetime.now(timezone.utc).isoformat(),
            "probe": ACCOUNT_DATABASE_PROBE,
        }
        self.connection = ProbeConnection(self.spec)
        self.addCleanup(self.connection.database.close)
        self.environment = {
            **runtime_identity_env(self.identity),
            "PLATFORM_INSTANCE": "testing",
            "ODOO_DB_HOST": "database",
            "ODOO_DB_NAME": "testing",
            "ODOO_DB_USER": "fixture",
            "ODOO_DB_PASSWORD": "private-database-fixture",
        }
        self.database_reads = 0

    def run_probe(self) -> bool:
        output = io.StringIO()
        original_path = Path

        def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            if arguments[:2] == ["docker", "ps"]:
                raw = b"fixture-runner\n"
            elif arguments[:2] == ["docker", "inspect"]:
                raw = json.dumps(
                    [
                        {
                            "Config": {
                                "Env": [
                                    key + "=" + value for key, value in self.environment.items()
                                ],
                                "Image": self.identity.image_reference,
                            }
                        }
                    ]
                ).encode()
            elif arguments[:2] == ["docker", "exec"]:
                self.database_reads += 1
                response = io.StringIO()
                with (
                    patch.dict("os.environ", self.environment),
                    patch.dict(
                        sys.modules,
                        {"psycopg2": SimpleNamespace(connect=lambda **kwargs: self.connection)},
                    ),
                    patch.object(sys, "stdin", io.StringIO(kwargs["input"].decode())),
                    redirect_stdout(response),
                ):
                    exec(compile(arguments[-1], "database-probe", "exec"), {})
                raw = response.getvalue().encode()
            else:
                raise AssertionError("Unexpected provider effect")
            return subprocess.CompletedProcess(arguments, 0, stdout=raw, stderr=b"")

        with (
            patch("subprocess.run", side_effect=run),
            patch.object(
                sys,
                "argv",
                ["probe", base64.b64encode(json.dumps(self.spec).encode()).decode(), "FRAME:"],
            ),
            redirect_stdout(output),
            patch(
                "pathlib.Path",
                side_effect=lambda path: (
                    original_path(self.temporary.name) / "journal"
                    if path == WALKTHROUGH_JOURNAL_DIRECTORY
                    else original_path(path)
                ),
            ),
        ):
            exec(compile(ACCOUNT_JOURNAL_READ_PROGRAM, "host-probe", "exec"), {})
        self.assertNotIn(self.environment["ODOO_DB_PASSWORD"], output.getvalue())
        return json.loads(output.getvalue().removeprefix("FRAME:"))["clear"] is True

    def test_absent_journal_settles_only_after_deadline_and_actual_transaction_quiescence(
        self,
    ) -> None:
        self.spec["dispatch_deadline"] = int(time.time()) + 60
        self.assertFalse(self.run_probe())
        self.assertEqual(self.database_reads, 0)
        self.spec["dispatch_deadline"] = int(time.time()) - 1
        self.connection.free = False
        self.assertFalse(self.run_probe())
        self.connection.free = True
        self.assertTrue(self.run_probe())
        self.assertTrue(self.connection.readonly)
        self.assertTrue(self.connection.closed)

    def test_wrong_copy_or_production_receiver_never_proves_quiescence(self) -> None:
        self.connection.copy = "another-copy"
        self.assertFalse(self.run_probe())
        self.environment["PLATFORM_INSTANCE"] = "prod"
        self.database_reads = 0
        self.assertFalse(self.run_probe())
        self.assertEqual(self.database_reads, 0)

    def test_each_usage_checkpoint_requires_its_task_and_no_other_recent_signins(self) -> None:
        self.spec["mode"] = "usage"
        self.assertTrue(self.run_probe())
        self.connection.used = True
        self.assertFalse(self.run_probe())
        self.connection.used = False
        self.connection.active = False
        self.assertFalse(self.run_probe())

    def test_isolation_is_current_and_fail_closed_at_every_usage_and_cleanup_probe(self) -> None:
        for mode in ("lane_usage", "usage", "isolation"):
            with self.subTest(mode=mode):
                self.spec["mode"] = mode
                self.connection.repair(self.connection.sql, apply=True)
                self.connection.isolation_readable = True
                self.assertTrue(self.run_probe())
                for mutation in (
                    "UPDATE res_company SET stock_move_sms_validation = true",
                    "UPDATE iap_account SET account_token = 'live-fixture-key'",
                    "UPDATE delivery_carrier SET prod_environment = true",
                ):
                    self.connection.database.execute(mutation)
                    self.assertFalse(self.run_probe())
                    self.connection.repair(self.connection.sql, apply=True)
                self.connection.isolation_readable = False
                self.assertFalse(self.run_probe())
