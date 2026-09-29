"""Execute the rendered Dokploy data-workflow script against a fake ``docker``.

These tests run the real bash the schedule would run. The fake ``docker`` on
PATH keeps container state on disk, runs the Shopify guard's Python program
against a fake ``psycopg2``, and reproduces Docker's stdin behaviour: without
``-i`` the container gets no stdin, so ``python3 -`` runs an empty program.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest.mock import patch

from control_plane import dokploy as control_plane_dokploy
from control_plane.dokploy import post_deploy as dokploy_post_deploy
from control_plane.contracts.dokploy_target_record import (
    DokployTargetPolicies,
    DokployTargetShopifyPolicy,
)

PROTECTED_STORE_KEY = "example-production-store"
DEV_STORE_KEY = "example-dev-store"
WEB_CONTAINER_ID = "web-id"

_FAKE_DOCKER = """\
import os
import subprocess
import sys
from pathlib import Path

state_dir = Path(os.environ["FAKE_DOCKER_STATE"])
log_path = state_dir / "docker.log"


def log(line):
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\\n")


def state_path(container_id):
    return state_dir / f"{container_id}.state"


def main(argv):
    command = argv[0]
    if command == "ps":
        for argument in argv[1:]:
            prefix = "label=com.docker.compose.service="
            if argument.startswith(prefix):
                print(argument[len(prefix):] + "-id")
        return 0
    if command == "inspect":
        template, container_id = argv[2], argv[3]
        if ".State.Status" in template:
            path = state_path(container_id)
            print(path.read_text().strip() if path.exists() else "running")
        else:
            print("sha256:same-image")
        return 0
    if command in ("start", "stop"):
        container_id = argv[1]
        state_path(container_id).write_text("running" if command == "start" else "exited")
        log(f"{command} {container_id}")
        return 0
    if command != "exec":
        print(f"fake docker: unsupported command {argv!r}", file=sys.stderr)
        return 99

    interactive = False
    index = 1
    while argv[index].startswith("-"):
        if argv[index] == "-i":
            interactive = True
            index += 1
        elif argv[index] in ("-e", "-u"):
            index += 2
        else:
            print(f"fake docker: unsupported exec option {argv[index]!r}", file=sys.stderr)
            return 99
    program = argv[index + 1:]
    if program[:1] == ["id"]:
        print("1000")
        return 0
    if program[:1] in (["rm"], ["/bin/bash"]):
        return 0
    if program[:2] == ["python3", "-u"]:
        log("exec workflow")
        print(os.environ.get("FAKE_WORKFLOW_OUTPUT", "workflow ran"))
        return int(os.environ.get("FAKE_WORKFLOW_EXIT", "0"))
    if program[:2] == ["python3", "-"]:
        log("exec guard" + (" -i" if interactive else ""))
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(state_dir / "modules")
        # Docker attaches the caller's stdin only with -i.
        stdin = None if interactive else subprocess.DEVNULL
        return subprocess.run(
            [sys.executable, *program[1:]], stdin=stdin, env=environment
        ).returncode
    print(f"fake docker: unsupported exec program {program!r}", file=sys.stderr)
    return 99


sys.exit(main(sys.argv[1:]))
"""

_FAKE_PSYCOPG2 = """\
import os


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, query, params):
        self.key = params[0]

    def fetchone(self):
        if self.key != "shopify.shop_url_key":
            return None
        value = os.environ.get("FAKE_SHOPIFY_STORE_KEY")
        return None if value is None else (value,)


class _Connection:
    def cursor(self):
        return _Cursor()

    def close(self):
        pass


def connect(**_kwargs):
    if os.environ.get("FAKE_DB_UNREACHABLE"):
        raise RuntimeError("database unreachable")
    return _Connection()
"""


@dataclass(frozen=True)
class ScriptRun:
    returncode: int
    stdout: str
    stderr: str
    docker_log: tuple[str, ...]

    @property
    def web_restarted(self) -> bool:
        return f"start {WEB_CONTAINER_ID}" in self.docker_log


def _modern_bash() -> str | None:
    bash = shutil.which("bash")
    if bash is None:
        return None
    probe = subprocess.run(
        [bash, "-c", 'echo "${BASH_VERSINFO[0]}"'], capture_output=True, text=True
    )
    return bash if probe.stdout.strip().isdigit() and int(probe.stdout) >= 4 else None


BASH = _modern_bash()


def _render_script(protected_store_keys: tuple[str, ...]) -> str:
    """Render the schedule script through the stable bootstrap entrypoint."""
    schedule_payloads: list[dict[str, object]] = []

    def capture_schedule_payload(**kwargs: object) -> dict[str, str]:
        schedule_payloads.append(cast("dict[str, object]", kwargs["schedule_payload"]))
        return {"scheduleId": "schedule-123"}

    target_definition = control_plane_dokploy.DokployTargetDefinition(
        context="example",
        instance="testing",
        target_id="compose-123",
        target_name="example-testing",
        policies=DokployTargetPolicies(
            shopify=DokployTargetShopifyPolicy(protected_store_keys=protected_store_keys)
        ),
    )
    with (
        patch(
            "control_plane.dokploy.api.fetch_dokploy_target_payload",
            return_value={
                "name": "example-testing",
                "env": textwrap.dedent(
                    """\
                    ODOO_DB_NAME=example_testing
                    ODOO_ADDONS_PATH=/opt/project/addons,/opt/launchplane/addons,/odoo/addons,/opt/enterprise
                    ODOO_INSTALL_MODULES=base
                    """
                ),
                "appName": "example-testing-app",
                "serverId": "server-123",
            },
        ),
        patch("control_plane.dokploy.api.find_matching_dokploy_schedule", return_value=None),
        patch(
            "control_plane.dokploy.api.upsert_dokploy_schedule",
            side_effect=capture_schedule_payload,
        ),
        patch(
            "control_plane.dokploy.api.latest_deployment_for_schedule",
            side_effect=(
                {"deploymentId": "schedule-before"},
                {
                    "deploymentId": "schedule-after",
                    "logs": [
                        "odoo_module_update_image_match=true",
                        "odoo_module_update_modules_configured=true",
                        "odoo_module_update_completed=true",
                    ],
                },
            ),
        ),
        patch(
            "control_plane.dokploy.api.wait_for_dokploy_schedule_deployment",
            return_value="deployment=schedule-after status=done",
        ),
        patch("control_plane.dokploy.api.dokploy_request", return_value={"ok": True}),
    ):
        control_plane_dokploy.run_compose_odoo_stable_bootstrap(
            host="https://dokploy.example.com",
            token="secret-token",
            target_definition=target_definition,
            env_file=None,
            protected_shopify_store_keys=protected_store_keys,
        )
    if len(schedule_payloads) != 1:
        raise AssertionError(f"expected one schedule upsert, got {len(schedule_payloads)}")
    return cast(str, schedule_payloads[0]["script"])


def _render_restore_script() -> str:
    """Render the schedule script through the destructive-restore post-deploy entrypoint."""
    schedule_payloads: list[dict[str, object]] = []

    def capture_schedule_payload(**kwargs: object) -> dict[str, str]:
        schedule_payloads.append(cast("dict[str, object]", kwargs["schedule_payload"]))
        return {"scheduleId": "schedule-123"}

    target_definition = control_plane_dokploy.DokployTargetDefinition(
        context="example",
        instance="testing",
        target_id="compose-123",
        target_name="example-testing",
    )
    with (
        patch(
            "control_plane.dokploy.api.fetch_dokploy_target_payload",
            return_value={
                "name": "example-testing",
                "env": textwrap.dedent(
                    """\
                    ODOO_DB_NAME=example_testing
                    ODOO_INSTALL_MODULES=base
                    ODOO_UPSTREAM_HOST=source.example.com
                    ODOO_UPSTREAM_USER=backup
                    ODOO_UPSTREAM_DB_NAME=source
                    ODOO_UPSTREAM_DB_USER=odoo
                    ODOO_UPSTREAM_FILESTORE_PATH=/source/filestore
                    """
                ),
                "appName": "example-testing-app",
                "serverId": "server-123",
            },
        ),
        patch("control_plane.dokploy.api.update_dokploy_target_env"),
        patch("control_plane.dokploy.api.trigger_deployment"),
        patch("control_plane.dokploy.api.wait_for_target_deployment"),
        patch("control_plane.dokploy.api.latest_deployment_for_target", return_value=None),
        patch("control_plane.dokploy.api.find_matching_dokploy_schedule", return_value=None),
        patch(
            "control_plane.dokploy.api.upsert_dokploy_schedule",
            side_effect=capture_schedule_payload,
        ),
        patch(
            "control_plane.dokploy.api.latest_deployment_for_schedule",
            side_effect=(
                {"deploymentId": "schedule-before"},
                {"deploymentId": "schedule-after", "logs": ["odoo_restore_completed=true"]},
            ),
        ),
        patch(
            "control_plane.dokploy.api.wait_for_dokploy_schedule_deployment",
            return_value="deployment=schedule-after status=done",
        ),
        patch("control_plane.dokploy.api.dokploy_request", return_value={"ok": True}),
    ):
        control_plane_dokploy.run_compose_post_deploy_update(
            host="https://dokploy.example.com",
            token="secret-token",
            target_definition=target_definition,
            env_file=None,
            run_destructive_restore=True,
        )
    if len(schedule_payloads) != 1:
        raise AssertionError(f"expected one schedule upsert, got {len(schedule_payloads)}")
    return cast(str, schedule_payloads[0]["script"])


@unittest.skipIf(BASH is None, "bash 4 or newer is required to run the rendered script")
class DataWorkflowScriptExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(f"#!{sys.executable}\n{_FAKE_DOCKER}", encoding="utf-8")
        docker.chmod(0o755)
        modules = self.root / "modules"
        modules.mkdir()
        (modules / "psycopg2.py").write_text(_FAKE_PSYCOPG2, encoding="utf-8")
        assert BASH is not None
        self.bash = BASH
        self.base_environment = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_DOCKER_STATE": str(self.root),
            "HOME": str(self.root),
        }

    def _run(self, script: str, **fake_environment: str) -> ScriptRun:
        script_path = self.root / "script.sh"
        script_path.write_text(script, encoding="utf-8")
        completed = subprocess.run(
            [self.bash, str(script_path)],
            capture_output=True,
            text=True,
            env={**self.base_environment, **fake_environment},
            timeout=60,
        )
        log_path = self.root / "docker.log"
        docker_log = tuple(log_path.read_text().splitlines()) if log_path.exists() else ()
        return ScriptRun(completed.returncode, completed.stdout, completed.stderr, docker_log)

    def test_fake_docker_runs_an_empty_program_without_dash_i(self) -> None:
        # Mirrors the reproduction on the testing script-runner: a stdin
        # program that exits 3 returns 0 without -i and 3 with it.
        environment = {**self.base_environment}
        program = "raise SystemExit(3)\n"
        without_i = subprocess.run(
            ["docker", "exec", "runner-id", "python3", "-"],
            input=program,
            text=True,
            env=environment,
        )
        with_i = subprocess.run(
            ["docker", "exec", "-i", "runner-id", "python3", "-"],
            input=program,
            text=True,
            env=environment,
        )
        self.assertEqual(without_i.returncode, 0)
        self.assertEqual(with_i.returncode, 3)

    def test_protected_store_key_fails_and_leaves_web_stopped(self) -> None:
        # Regression for #2557: this fails if the guard's docker exec loses -i,
        # because the empty program would exit 0 and web would restart.
        run = self._run(
            _render_script((PROTECTED_STORE_KEY,)),
            FAKE_SHOPIFY_STORE_KEY=PROTECTED_STORE_KEY.upper(),
        )

        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("exec guard -i", run.docker_log)
        self.assertIn(f"stop {WEB_CONTAINER_ID}", run.docker_log)
        self.assertFalse(run.web_restarted, run.docker_log)
        self.assertIn("Protected Shopify store key is not allowed", run.stderr)
        self.assertIn("shopify_store_key_guard_refused", run.stdout)
        self.assertNotIn("shopify_store_key_guard_pass", run.stdout)
        self.assertIn(f"Leaving web container {WEB_CONTAINER_ID} stopped", run.stderr)

    def test_unprotected_store_key_passes_and_restarts_web(self) -> None:
        run = self._run(
            _render_script((PROTECTED_STORE_KEY,)),
            FAKE_SHOPIFY_STORE_KEY=DEV_STORE_KEY,
        )

        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn(
            f"shopify_store_key_guard_pass db=example_testing value={DEV_STORE_KEY}", run.stdout
        )
        self.assertTrue(run.web_restarted, run.docker_log)
        self.assertLess(
            run.docker_log.index("exec guard -i"), run.docker_log.index(f"start {WEB_CONTAINER_ID}")
        )

    def test_unreadable_database_fails_closed_and_leaves_web_stopped(self) -> None:
        run = self._run(_render_script((PROTECTED_STORE_KEY,)), FAKE_DB_UNREACHABLE="1")

        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(run.web_restarted, run.docker_log)
        self.assertIn("shopify_store_key_guard_refused", run.stdout)

    def test_workflow_failure_with_protected_key_leaves_web_stopped(self) -> None:
        run = self._run(
            _render_script((PROTECTED_STORE_KEY,)),
            FAKE_WORKFLOW_EXIT="7",
            FAKE_SHOPIFY_STORE_KEY=PROTECTED_STORE_KEY,
        )

        self.assertEqual(run.returncode, 7)
        self.assertIn("exec guard -i", run.docker_log)
        self.assertFalse(run.web_restarted, run.docker_log)

    def test_workflow_failure_with_unprotected_key_restarts_web(self) -> None:
        run = self._run(
            _render_script((PROTECTED_STORE_KEY,)),
            FAKE_WORKFLOW_EXIT="7",
            FAKE_SHOPIFY_STORE_KEY=DEV_STORE_KEY,
        )

        self.assertEqual(run.returncode, 7)
        self.assertIn("shopify_store_key_guard_pass", run.stdout)
        self.assertTrue(run.web_restarted, run.docker_log)

    def test_lane_without_protected_keys_keeps_restart_on_failure(self) -> None:
        run = self._run(
            _render_script(()),
            FAKE_WORKFLOW_EXIT="7",
            FAKE_SHOPIFY_STORE_KEY=PROTECTED_STORE_KEY,
        )

        self.assertEqual(run.returncode, 7)
        self.assertNotIn("exec guard -i", run.docker_log)
        self.assertTrue(run.web_restarted, run.docker_log)

    def test_successful_restore_prints_the_completion_marker(self) -> None:
        run = self._run(
            _render_restore_script(),
            FAKE_WORKFLOW_OUTPUT="Upstream overwrite completed successfully.",
        )

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("odoo_restore_completed=true", run.stdout.splitlines())
        self.assertNotIn("odoo_restore_completed=false", run.stdout)

    def test_restore_that_exits_non_zero_is_not_marked_complete(self) -> None:
        run = self._run(
            _render_restore_script(),
            FAKE_WORKFLOW_OUTPUT="pg_restore: error: could not execute query",
            FAKE_WORKFLOW_EXIT="40",
        )

        self.assertEqual(run.returncode, 40)
        self.assertIn("odoo_restore_completed=false", run.stdout.splitlines())
        self.assertNotIn("odoo_restore_completed=true", run.stdout)

    def test_restore_that_logs_a_failure_but_exits_zero_is_failed(self) -> None:
        for failure_line in dokploy_post_deploy.ODOO_RESTORE_FAILURE_LOG_PATTERNS:
            with self.subTest(failure_line=failure_line):
                run = self._run(
                    _render_restore_script(),
                    FAKE_WORKFLOW_OUTPUT=f"ERROR {failure_line} (host key verification failed).",
                )

                self.assertEqual(run.returncode, 1)
                lines = run.stdout.splitlines()
                self.assertIn("odoo_restore_failure_logged=true", lines)
                self.assertIn("odoo_restore_completed=false", lines)
                self.assertNotIn("odoo_restore_completed=true", lines)

    def test_maintenance_does_not_print_restore_markers(self) -> None:
        run = self._run(_render_script(()), FAKE_WORKFLOW_OUTPUT="Upstream restore failed (x).")

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("odoo_restore_completed", run.stdout)
        self.assertNotIn("odoo_restore_failure_logged", run.stdout)


if __name__ == "__main__":
    unittest.main()
