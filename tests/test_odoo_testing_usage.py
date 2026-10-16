import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from typing import Any
import unittest
from unittest.mock import MagicMock, Mock, patch

from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
from control_plane.dokploy.api import DokployRequestFailed
from control_plane.dokploy.compose import ODOO_WEB_CONFIG_PATH, ODOO_WEB_START_COMMAND
from control_plane.odoo_testing_usage import (
    ODOO_TESTING_USAGE_CONTAINER_PROGRAM,
    ODOO_TESTING_USAGE_HOST_PROGRAM,
    read_odoo_testing_usage,
)


class SessionInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = self.enterContext(TemporaryDirectory())
        self.path = Path(self.directory)
        self.driver = ModuleType("psycopg2")
        self.namespace: dict[str, Any] = {"__name__": "fixture"}
        with patch.dict("sys.modules", {"psycopg2": self.driver}):
            exec(ODOO_TESTING_USAGE_CONTAINER_PROGRAM, self.namespace)

    def sessions(self) -> bool:
        return bool(self.namespace["read_sessions"](self.path, "testing_db"))

    def test_empty_or_anonymous_store_is_idle_but_old_authenticated_sessions_protect(self) -> None:
        self.assertFalse(self.sessions())
        (self.path / "anonymous").write_text(json.dumps({"db": "testing_db", "uid": None}))
        self.assertFalse(self.sessions())
        shard = self.path / "ab"
        shard.mkdir()
        (shard / "session").write_text(json.dumps({"db": "testing_db", "uid": 2}))
        import os

        os.utime(shard / "session", (1, 1))
        self.assertTrue(self.sessions())

    def test_partial_authentication_counts_and_other_databases_are_excluded(self) -> None:
        session = self.path / "session"
        session.write_text(json.dumps({"db": "another_db", "uid": 2}))
        self.assertFalse(self.sessions())
        session.write_text(json.dumps({"db": "testing_db", "pre_uid": 2}))
        self.assertTrue(self.sessions())

    def test_missing_corrupt_aliased_and_oversized_stores_cannot_mean_idle(self) -> None:
        for mode in ("missing", "corrupt", "alias", "oversized"):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                path = Path(directory) / "sessions"
                if mode != "missing":
                    path.mkdir()
                    file = path / "session"
                    if mode == "corrupt":
                        file.write_text("unfinished JSON")
                    elif mode == "alias":
                        file.symlink_to(self.path)
                    else:
                        file.write_bytes(b" " * (1024 * 1024 + 1))
                with self.assertRaises((ValueError, OSError)):
                    self.namespace["read_sessions"](path, "testing_db")

    def test_probe_uses_read_only_connection_and_rolls_back_without_signing_in(self) -> None:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (False,)
        connect = Mock(return_value=connection)
        self.driver.connect = connect  # type: ignore[attr-defined]
        config = MagicMock()
        config.read.return_value = [ODOO_WEB_CONFIG_PATH]
        config.__getitem__ = Mock(
            return_value={
                "db_name": "testing_db",
                "db_host": "database",
                "data_dir": self.directory,
                "db_user": "fixture",
                "db_password": "private-fixture",
            }
        )
        process = SimpleNamespace(read_bytes=lambda: f"odoo\0-c\0{ODOO_WEB_CONFIG_PATH}\0".encode())
        env = {
            "PLATFORM_INSTANCE": "testing",
            "ODOO_DB_NAME": "testing_db",
            "ODOO_DB_USER": "fixture",
            "ODOO_DB_PASSWORD": "private-fixture",
        }
        (self.path / "sessions").mkdir()
        with (
            patch.dict("os.environ", env, clear=True),
            patch("configparser.ConfigParser", return_value=config),
            patch.object(Path, "glob", return_value=[process]),
        ):
            for recent in (False, True):
                cursor.fetchone.return_value = (recent,)
                result = self.namespace["probe"]()
                self.assertEqual(result["recent_signins"], recent)
                self.assertFalse(result["authenticated_sessions"])
        connection.rollback.assert_called()
        connection.commit.assert_not_called()
        connection.close.assert_called()
        self.assertNotIn("private-fixture", json.dumps(result))

    def test_startup_wrapper_alone_cannot_qualify_session_location(self) -> None:
        config = MagicMock()
        config.read.return_value = [ODOO_WEB_CONFIG_PATH]
        config.__getitem__.return_value = {
            "db_name": "testing_db",
            "db_host": "database",
            "data_dir": self.directory,
        }
        process = SimpleNamespace(
            read_bytes=lambda: (
                f"python3\0/volumes/scripts/run_odoo_startup.py\0-c\0{ODOO_WEB_CONFIG_PATH}\0".encode()
            )
        )
        with (
            patch.dict(
                "os.environ", {"PLATFORM_INSTANCE": "testing", "ODOO_DB_NAME": "testing_db"}
            ),
            patch("configparser.ConfigParser", return_value=config),
            patch.object(Path, "glob", return_value=[process]),
            self.assertRaises(ValueError),
        ):
            self.namespace["probe"]()


class UsageReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = DokployTargetRecord(
            context="fixture",
            instance="testing",
            target_name="fixture-testing",
            updated_at="2026-10-16T00:00:00Z",
        )
        self.target_id = DokployTargetIdRecord(
            context="fixture",
            instance="testing",
            target_id="fixture-target",
            updated_at=self.target.updated_at,
        )
        module = "control_plane.odoo_testing_usage."
        self.enterContext(
            patch(
                module + "source.read_dokploy_config",
                return_value=(
                    "https://provider.invalid",
                    "private-provider-token",
                ),
            )
        )
        self.provider = self.enterContext(patch(module + "api.fetch_dokploy_target_payload"))
        self.provider.return_value = {
            "name": self.target.target_name,
            "appName": "fixture-project",
            "serverId": "remote",
        }
        self.remote = self.enterContext(patch(module + "_read_remote"))

    def read(self) -> Any:
        return read_odoo_testing_usage(
            target=self.target,
            target_id=self.target_id,
            control_plane_root=Path("/fixture"),
            database_url="sqlite+pysqlite://",
        )

    def test_unused_requires_both_complete_false_facts_and_never_excludes_admin(self) -> None:
        for recent, sessions in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(recent=recent, sessions=sessions):
                self.remote.side_effect = lambda **kw: kw["receive"](
                    {
                        "type": "complete",
                        "recent_signins": recent,
                        "authenticated_sessions": sessions,
                    }
                )
                result = self.read()
                self.assertEqual(result.usage, "in_use" if recent or sessions else "unused")
                self.assertNotIn("private-provider-token", repr(result))

    def test_incomplete_invalid_or_failed_frames_are_unknown(self) -> None:
        for frame in (
            None,
            {"type": "complete"},
            {"type": "complete", "unavailable": True},
            {"type": "complete", "recent_signins": False, "authenticated_sessions": 0},
            {"type": "failed", "recent_signins": False, "authenticated_sessions": False},
        ):
            with self.subTest(frame=frame):
                self.remote.side_effect = lambda **kw: kw["receive"](frame) if frame else None
                self.assertEqual(self.read().usage, "unknown")
        self.remote.side_effect = TimeoutError("private-provider-error")
        self.assertEqual(self.read().usage, "unknown")

    def test_denial_is_not_swallowed_or_retried(self) -> None:
        self.remote.side_effect = NativeSourceAuthorizationDenied("fixture denial")
        with self.assertRaises(NativeSourceAuthorizationDenied):
            self.read()
        self.assertEqual(self.remote.call_count, 1)

        self.provider.side_effect = DokployRequestFailed(
            method="GET",
            path="/fixture",
            detail="private-provider-error",
            status_code=403,
        )
        with self.assertRaises(NativeSourceAuthorizationDenied) as caught:
            self.read()
        self.assertNotIn("private-provider-error", str(caught.exception))

    def test_production_and_changed_placement_cannot_qualify(self) -> None:
        self.target = self.target.model_copy(update={"instance": "prod"})
        self.assertEqual(self.read().usage, "unknown")
        self.remote.assert_not_called()
        self.target = self.target.model_copy(update={"instance": "testing"})
        first = dict(self.provider.return_value)
        self.provider.side_effect = [first, {**first, "serverId": "another-server"}]
        self.remote.side_effect = lambda **kw: kw["receive"](
            {
                "type": "complete",
                "recent_signins": False,
                "authenticated_sessions": False,
            }
        )
        self.assertEqual(self.read().usage, "unknown")


class HostProbeTests(unittest.TestCase):
    def run_probe(
        self,
        *,
        duplicate: bool = False,
        changed: bool = False,
        wrong_context: bool = False,
        custom_command: bool = False,
    ) -> dict[str, Any]:
        def container(service: str) -> dict[str, Any]:
            return {
                "Id": service,
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": "fixture",
                        "com.docker.compose.service": service,
                    },
                    "Env": [
                        "PLATFORM_CONTEXT=fixture",
                        "PLATFORM_INSTANCE=testing",
                        "ODOO_DB_HOST=database",
                    ],
                    "Cmd": [
                        "/bin/sh",
                        "-lc",
                        ODOO_WEB_START_COMMAND,
                    ],
                },
                "State": {"Running": True, "StartedAt": "start"},
                "Mounts": [],
                "NetworkSettings": {"Networks": {"private": {}}},
            }

        web, database = container("web"), container("database")
        if wrong_context:
            web["Config"]["Env"][0] = "PLATFORM_CONTEXT=other"
        if custom_command:
            web["Config"]["Cmd"] = ["odoo --data-dir=/another-store"]
        calls: list[list[str]] = []

        def run(command: list[str], **kwargs: Any) -> Any:
            calls.append(command)
            if command[1] == "ps":
                output = b"web database web2" if duplicate else b"web database"
            elif command[1] == "inspect":
                values = [web, database, web] if duplicate else [web, database]
                if changed and len(calls) > 3:
                    web["State"]["StartedAt"] = "restarted"
                output = json.dumps(values).encode()
            elif command[1] == "exec":
                output = json.dumps(
                    {"type": "complete", "recent_signins": False, "authenticated_sessions": False}
                ).encode()
            else:
                raise AssertionError("Unexpected provider mutation")
            return SimpleNamespace(stdout=output)

        payload = base64.b64encode(
            json.dumps(
                {
                    "project": "fixture",
                    "context": "fixture",
                    "container_program": ODOO_TESTING_USAGE_CONTAINER_PROGRAM,
                }
            ).encode()
        ).decode()
        output = io.StringIO()
        with (
            patch("sys.argv", ["probe", payload, "frame:"]),
            patch("subprocess.run", side_effect=run),
            redirect_stdout(output),
        ):
            exec(ODOO_TESTING_USAGE_HOST_PROGRAM, {"__name__": "__main__"})
        result = json.loads(output.getvalue().removeprefix("frame:"))
        assert isinstance(result, dict)
        return result

    def test_fixed_host_read_collects_without_provider_mutations(self) -> None:
        self.assertFalse(self.run_probe()["authenticated_sessions"])

    def test_duplicate_services_and_container_restarts_leave_usage_unknown(self) -> None:
        self.assertTrue(self.run_probe(duplicate=True)["unavailable"])
        self.assertTrue(self.run_probe(changed=True)["unavailable"])
        self.assertTrue(self.run_probe(wrong_context=True)["unavailable"])
        self.assertTrue(self.run_probe(custom_command=True)["unavailable"])
