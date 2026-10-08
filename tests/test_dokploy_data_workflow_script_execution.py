"""Execute the rendered Dokploy data-workflow script against a fake ``docker``.

These tests run the real bash the schedule would run. The fake ``docker`` on
PATH keeps container state on disk, runs the Shopify guard's Python program
against a fake ``psycopg2``, and reproduces Docker's stdin behaviour: without
``-i`` the container gets no stdin, so ``python3 -`` runs an empty program.
"""

from __future__ import annotations

import json
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
from control_plane.dokploy.compose import render_odoo_raw_compose_file
from control_plane.integration_readback import (
    INTEGRATION_FAMILIES,
    INTEGRATION_READBACK_PASSED_PATH,
    integration_readback_policy,
)
from control_plane.contracts.dokploy_target_record import (
    DokployTargetIntegrationAllowance,
    DokployTargetPolicies,
    DokployTargetShopifyPolicy,
)

PROTECTED_STORE_KEY = "example-production-store"
DEV_STORE_KEY = "example-dev-store"
WEB_CONTAINER_ID = "web-id"
PRODUCTION_VALUE = "production-secret-value"

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
            status = path.read_text().strip() if path.exists() else "running"
            if container_id == "web-id":
                after_stop = (state_dir / "web-stopped").exists()
                after_start = (state_dir / "web-started").exists()
                log(f"inspect web {status}")
                if after_stop and not after_start and os.environ.get("FAKE_INITIAL_INSPECT_FAILURE") == "1":
                    return 42
                if after_stop and os.environ.get("FAKE_RECOVERY_INSPECT_FAILURE") == "1":
                    return 42
                if after_start and os.environ.get("FAKE_FINAL_INSPECT_FAILURE") == "1":
                    return 42
            print(status)
        else:
            print("sha256:same-image")
        return 0
    if command in ("start", "stop"):
        container_id = argv[1]
        log(f"{command} {container_id}")
        status = "running" if command == "start" else "exited"
        if container_id == "web-id":
            (state_dir / f"web-{'started' if command == 'start' else 'stopped'}").touch()
            if command == "start":
                if os.environ.get("FAKE_WEB_START_FAILURE") == "1":
                    return 41
                status = os.environ.get("FAKE_WEB_START_STATUS", "running")
        state_path(container_id).write_text(status)
        return 0
    if command != "exec":
        print(f"fake docker: unsupported command {argv!r}", file=sys.stderr)
        return 99

    interactive = False
    exec_environment = {}
    index = 1
    while argv[index].startswith("-"):
        if argv[index] == "-i":
            interactive = True
            index += 1
        elif argv[index] == "-e":
            key, _, value = argv[index + 1].partition("=")
            exec_environment[key] = value
            index += 2
        elif argv[index] == "-u":
            index += 2
        else:
            print(f"fake docker: unsupported exec option {argv[index]!r}", file=sys.stderr)
            return 99
    program = argv[index + 1:]
    passed_path = state_dir / "readback-passed"
    if program[:1] == ["id"]:
        print("1000")
        return 0
    if program[:1] == ["rm"]:
        if program[-1].endswith("integration_readback_passed"):
            passed_path.unlink(missing_ok=True)
            log("rm readback-passed")
        return 0
    if program[:1] == ["/bin/bash"]:
        return 0
    if program[:2] == ["sh", "-c"] and "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64" in program[2]:
        log("read overrides payload")
        if "FAKE_PAYLOAD_READ_EXIT" in os.environ:
            print("partial-payload", end="")
            return int(os.environ["FAKE_PAYLOAD_READ_EXIT"])
        # The container's own environment comes from the compose .env file.
        container_environment = {"PATH": os.environ["PATH"]}
        if "FAKE_CONTAINER_PAYLOAD" in os.environ:
            container_environment["ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"] = os.environ["FAKE_CONTAINER_PAYLOAD"]
        return subprocess.run(program, env={**container_environment, **exec_environment}).returncode
    if program[:2] == ["sh", "-c"] and program[-1].endswith("integration_readback_passed"):
        if os.environ.get("FAKE_PASS_WRITE_FAILURE") == "1":
            log("write readback-passed failed")
            return 43
        passed_path.write_text(program[-2])
        log("write readback-passed")
        return 0
    if program[:2] == ["sh", "-c"] and "psql" in program[2]:
        log("probe database")
        answer = os.environ.get("FAKE_DATABASE_PRESENT", "1")
        if answer == "error":
            print("psql: error: connection refused", file=sys.stderr)
            return 2
        print(answer)
        return 0
    if program[:2] == ["python3", "-u"]:
        log("exec workflow")
        log("workflow arguments " + " ".join(program[3:]))
        kept_key = "ODOO_RESTORE_KEPT_INTEGRATIONS"
        log("workflow kept integrations " + exec_environment.get(kept_key, "<unset>"))
        print(os.environ.get("FAKE_WORKFLOW_OUTPUT", "workflow ran"))
        return int(os.environ.get("FAKE_WORKFLOW_EXIT", "0"))
    if program[:2] == ["python3", "-"]:
        log("exec readback" + (" -i" if interactive else ""))
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(state_dir / "modules")
        # Docker attaches the caller's stdin only with -i.
        stdin = None if interactive else subprocess.DEVNULL
        if program[2:] == ["/volumes/scripts/odoo_website_bootstrap.py"]:
            program = [*program[:2], str(state_dir / "bootstrap.py")]
        return subprocess.run(
            [sys.executable, *program[1:]], stdin=stdin, env=environment
        ).returncode
    print(f"fake docker: unsupported exec program {program!r}", file=sys.stderr)
    return 99


sys.exit(main(sys.argv[1:]))
"""

# Stands in for PostgreSQL: FAKE_ODOO_DB describes ir_config_parameter values and,
# per table, whether any row matches the catalog's "configured" predicate. A table
# missing from "tables" does not exist. The real SQL is proved separately against
# PostgreSQL; this fake only has to answer the program's four query shapes.
_FAKE_PSYCOPG2 = """\
import json
import os


def _store_handle(value):
    handle = value.strip().lower()
    if "://" in handle:
        handle = handle.split("://", 1)[1]
    handle = handle.split("/", 1)[0].rstrip(".")
    if handle.endswith(".myshopify.com"):
        handle = handle[: -len(".myshopify.com")]
    return handle


class _Cursor:
    def __init__(self, database):
        self.database = database
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, query, params=None):
        config = self.database.get("config", {})
        tables = self.database.get("tables", {})
        if query.startswith("SELECT key FROM ir_config_parameter"):
            self.rows = [(key,) for key in params[0] if str(config.get(key) or "").strip()]
        elif query.startswith("SELECT to_regclass"):
            self.rows = [(params[0] in tables,)]
        elif "key = 'shopify.shop_url_key'" in query:
            value = str(config.get("shopify.shop_url_key") or "")
            self.rows = [(bool(value) and _store_handle(value) in params[0],)]
        elif query.startswith("SELECT EXISTS (SELECT 1 FROM "):
            table = query[len("SELECT EXISTS (SELECT 1 FROM "):].split(" ", 1)[0]
            self.rows = [(bool(tables[table]),)]
        else:
            raise AssertionError("unexpected query: " + query)

    def fetchone(self):
        return self.rows[0]

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self, database):
        self.database = database

    def cursor(self):
        return _Cursor(self.database)

    def close(self):
        pass


def connect(**_kwargs):
    if os.environ.get("FAKE_DB_UNREACHABLE"):
        raise RuntimeError("database unreachable")
    return _Connection(json.loads(os.environ.get("FAKE_ODOO_DB") or "{}"))
"""


def _fake_database(
    config: dict[str, str] | None = None, tables: dict[str, bool] | None = None
) -> str:
    return json.dumps({"config": config or {}, "tables": tables or {}})


@dataclass(frozen=True)
class ScriptRun:
    returncode: int
    stdout: str
    stderr: str
    docker_log: tuple[str, ...]

    @property
    def web_restarted(self) -> bool:
        return f"start {WEB_CONTAINER_ID}" in self.docker_log

    @property
    def readback_ran(self) -> bool:
        return "exec readback -i" in self.docker_log


def _modern_bash() -> str | None:
    bash = shutil.which("bash")
    if bash is None:
        return None
    probe = subprocess.run(
        [bash, "-c", 'echo "${BASH_VERSINFO[0]}"'], capture_output=True, text=True
    )
    return bash if probe.stdout.strip().isdigit() and int(probe.stdout) >= 4 else None


BASH = _modern_bash()


def _policies(
    *, protected_store_keys: tuple[str, ...] = (), allowed: tuple[str, ...] = ()
) -> DokployTargetPolicies:
    return DokployTargetPolicies(
        shopify=DokployTargetShopifyPolicy(protected_store_keys=protected_store_keys),
        integration_allowances=tuple(
            DokployTargetIntegrationAllowance(
                integration=integration, kind="dev_store", reason="Test allowance."
            )
            for integration in allowed
        ),
    )


def _render_script(
    policies: DokployTargetPolicies | None = None, *, instance: str = "testing"
) -> str:
    """Render the schedule script through the stable bootstrap entrypoint."""
    schedule_payloads: list[dict[str, object]] = []

    def capture_schedule_payload(**kwargs: object) -> dict[str, str]:
        schedule_payloads.append(cast("dict[str, object]", kwargs["schedule_payload"]))
        return {"scheduleId": "schedule-123"}

    target_definition = control_plane_dokploy.DokployTargetDefinition(
        context="example",
        instance=instance,
        target_id="compose-123",
        target_name=f"example-{instance}",
        policies=policies or _policies(),
    )
    with (
        patch(
            "control_plane.dokploy.api.fetch_dokploy_target_payload",
            return_value={
                "name": f"example-{instance}",
                "env": textwrap.dedent(
                    """\
                    ODOO_DB_NAME=example_testing
                    ODOO_ADDONS_PATH=/opt/project/addons,/opt/launchplane/addons,/odoo/addons,/opt/enterprise
                    ODOO_INSTALL_MODULES=base
                    """
                ),
                "appName": f"example-{instance}-app",
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
                        "integration_readback_ok=true",
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
        )
    if len(schedule_payloads) != 1:
        raise AssertionError(f"expected one schedule upsert, got {len(schedule_payloads)}")
    return cast(str, schedule_payloads[0]["script"])


def _render_restore_script(policies: DokployTargetPolicies | None = None) -> str:
    """Render the schedule script through the destructive-restore post-deploy entrypoint."""
    return _render_post_deploy_script(policies, run_destructive_restore=True)


def _render_preview_script() -> str:
    """Render the schedule script the way an Odoo preview refresh runs it."""
    return _render_post_deploy_script(None, bootstrap_missing_database=True)


def _render_post_deploy_script(
    policies: DokployTargetPolicies | None,
    *,
    run_destructive_restore: bool = False,
    bootstrap_missing_database: bool = False,
    instance: str = "testing",
    workflow_environment_overrides: dict[str, str] | None = None,
) -> str:
    schedule_payloads: list[dict[str, object]] = []

    def capture_schedule_payload(**kwargs: object) -> dict[str, str]:
        schedule_payloads.append(cast("dict[str, object]", kwargs["schedule_payload"]))
        return {"scheduleId": "schedule-123"}

    target_definition = control_plane_dokploy.DokployTargetDefinition(
        context="example",
        instance=instance,
        target_id="compose-123",
        target_name="example-testing",
        policies=policies or _policies(),
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
                {
                    "deploymentId": "schedule-after",
                    "logs": [
                        "odoo_restore_completed=true",
                        "odoo_module_update_image_match=true",
                        "odoo_module_update_modules_configured=true",
                        "odoo_module_update_completed=true",
                        "integration_readback_ok=true",
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
        control_plane_dokploy.run_compose_post_deploy_update(
            host="https://dokploy.example.com",
            token="secret-token",
            target_definition=target_definition,
            env_file=None,
            run_destructive_restore=run_destructive_restore,
            bootstrap_missing_database=bootstrap_missing_database,
            workflow_environment_overrides=workflow_environment_overrides,
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

    def _run(
        self, script: str, *, web_status: str = "running", **fake_environment: str
    ) -> ScriptRun:
        log_path = self.root / "docker.log"
        log_path.unlink(missing_ok=True)
        for state_file in self.root.glob("*.state"):
            state_file.unlink()
        for marker in ("web-stopped", "web-started"):
            (self.root / marker).unlink(missing_ok=True)
        (self.root / f"{WEB_CONTAINER_ID}.state").write_text(web_status)
        script_path = self.root / "script.sh"
        script_path.write_text(script, encoding="utf-8")
        completed = subprocess.run(
            [self.bash, str(script_path)],
            capture_output=True,
            text=True,
            env={**self.base_environment, **fake_environment},
            timeout=60,
        )
        docker_log = tuple(log_path.read_text().splitlines()) if log_path.exists() else ()
        return ScriptRun(completed.returncode, completed.stdout, completed.stderr, docker_log)

    def test_sender_contract_is_probed_from_the_running_artifact(self) -> None:
        script = dokploy_post_deploy._build_dokploy_data_workflow_script(
            compose_app_name="example-prod-app",
            database_name="example",
            filestore_path="/volumes/data/filestore",
            clear_stale_lock=False,
            data_workflow_lock_path="/volumes/data/.workflow-lock",
            required_update_modules="base,website",
            readback_policy=integration_readback_policy(
                instance_name="prod", policies=_policies(), workflow_mode="maintenance"
            ),
            hold_web_until_integration_readback=False,
            probe_company_email_contract=True,
        )
        marker = dokploy_post_deploy.ODOO_COMPANY_EMAIL_MATCH_MARKER
        bootstrap = self.root / "bootstrap.py"
        for source, sender, expected_status in (
            ("def apply_website_bootstrap(env, payload):\n    pass\n", "", "pass"),
            (
                f'def apply_website_bootstrap(env, payload):\n    print("{marker}=true")\n',
                "",
                "fail",
            ),
            (
                f'def apply_website_bootstrap(env, payload):\n    print("{marker}=true")\n',
                f"{marker}=true\n",
                "pass",
            ),
        ):
            with self.subTest(source=source, sender=sender):
                bootstrap.write_text(source)
                run = self._run(
                    script,
                    FAKE_WORKFLOW_OUTPUT=sender
                    + "\n".join(
                        f"{key}=true"
                        for key in dokploy_post_deploy.ODOO_WEBSITE_BOOTSTRAP_REQUIRED_READBACK_MARKERS
                    ),
                )
                self.assertEqual(run.returncode, 0, run.stderr)
                evidence = dokploy_post_deploy.extract_odoo_post_deploy_readback_markers(
                    {"logs": run.stdout}
                )
                evidence["log_available"] = "true"
                if expected_status == "pass":
                    dokploy_post_deploy.require_odoo_company_email_readback_evidence(evidence)
                    if not sender:
                        self.assertEqual(
                            evidence["website_bootstrap_company_email_skip_reason"],
                            dokploy_post_deploy.ODOO_COMPANY_EMAIL_SKIP_REASON,
                        )
                else:
                    with self.assertRaises(dokploy_post_deploy.OdooPostDeployReadbackFailure):
                        dokploy_post_deploy.require_odoo_company_email_readback_evidence(evidence)
        for unreadable_source in ("", "def broken(", "def unrelated():\n    pass\n", None):
            with self.subTest(unrecognized=unreadable_source):
                if unreadable_source is None:
                    bootstrap.unlink(missing_ok=True)
                else:
                    bootstrap.write_text(unreadable_source)
                run = self._run(script)
                self.assertNotEqual(run.returncode, 0)
                evidence = dokploy_post_deploy.extract_odoo_post_deploy_readback_markers(
                    {"logs": run.stdout}
                )
                self.assertNotIn(dokploy_post_deploy.ODOO_COMPANY_EMAIL_CONTRACT_MARKER, evidence)

    def test_restore_runner_keeps_only_real_account_allowances(self) -> None:
        policies = DokployTargetPolicies(
            integration_allowances=(
                DokployTargetIntegrationAllowance(
                    integration="repairshopr", kind="pre_live", reason="Working instance."
                ),
                DokployTargetIntegrationAllowance(
                    integration="fishbowl",
                    kind="read_only_source",
                    reason="Import source.",
                    evidence="Read-only grant verified.",
                ),
                DokployTargetIntegrationAllowance(
                    integration="cm_data", kind="pre_live", reason="Working instance."
                ),
                DokployTargetIntegrationAllowance(
                    integration="shopify", kind="dev_store", reason="Development store."
                ),
            )
        )
        for instance, expected in (
            ("testing", "cm_data,fishbowl,repairshopr"),
            ("dev", "cm_data,fishbowl,repairshopr"),
            ("prod", ""),
            ("production", ""),
            ("preview", ""),
            ("pr-42", ""),
        ):
            with self.subTest(instance=instance):
                run = self._run(
                    _render_post_deploy_script(
                        policies,
                        run_destructive_restore=True,
                        instance=instance,
                        workflow_environment_overrides={
                            "ODOO_RESTORE_KEPT_INTEGRATIONS": "payment"
                        },
                    )
                )
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertIn("workflow kept integrations " + expected, run.docker_log)

    def test_restore_without_real_account_allowances_keeps_nothing(self) -> None:
        for policies in (_policies(), _policies(allowed=("shopify",))):
            with self.subTest(policies=policies):
                run = self._run(_render_restore_script(policies))
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertIn("workflow kept integrations ", run.docker_log)

    def test_maintenance_does_not_send_restore_allowances(self) -> None:
        policies = DokployTargetPolicies(
            integration_allowances=(
                DokployTargetIntegrationAllowance(
                    integration="fishbowl", kind="pre_live", reason="Working instance."
                ),
            )
        )
        run = self._run(_render_post_deploy_script(policies))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("workflow kept integrations <unset>", run.docker_log)

    def assert_refused_and_web_stopped(self, run: ScriptRun, *entries: str) -> None:
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue(run.readback_ran, run.docker_log)
        self.assertIn(f"stop {WEB_CONTAINER_ID}", run.docker_log)
        self.assertFalse(run.web_restarted, run.docker_log)
        self.assertIn("integration_readback_ok=false", run.stdout.splitlines())
        for entry in entries:
            integration, _, setting = entry.partition("/")
            self.assertIn(
                f"Integration read-back refused: integration={integration} setting={setting}",
                run.stderr,
            )
        self.assertIn(f"Leaving web container {WEB_CONTAINER_ID} stopped", run.stderr)
        self.assertNotIn(PRODUCTION_VALUE, run.stdout + run.stderr)

    def _held_web_starts(
        self, *, overrides_payload: str, database_name: str = "example_testing"
    ) -> bool:
        """Run the held web command from the rendered compose file, as the container would."""
        compose_file = render_odoo_raw_compose_file(
            image_reference="ghcr.io/example/odoo@sha256:" + "a" * 64,
            hold_web_until_integration_readback=True,
        )
        rendered = next(
            line.strip()[2:]
            for line in compose_file.splitlines()
            if line.strip().startswith('- "expected=')
        )
        command = (
            json.loads(rendered)
            .replace("$$", "$")
            .replace(INTEGRATION_READBACK_PASSED_PATH, str(self.root / "readback-passed"))
        )
        # Compose resolves the unset ODOO_WEB_COMMAND to the startup script.
        command = command[: command.index("exec ${ODOO_WEB_COMMAND")] + "echo web-started"
        try:
            completed = subprocess.run(
                ["/bin/sh", "-c", command],
                capture_output=True,
                text=True,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "ODOO_DB_NAME": database_name,
                    "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64": overrides_payload,
                },
                timeout=3,
            )
        except subprocess.TimeoutExpired:
            return False
        return "web-started" in completed.stdout

    def test_passing_readback_releases_web_for_the_checked_payload_only(self) -> None:
        run = self._run(
            _render_script(), FAKE_CONTAINER_PAYLOAD="payload-a", FAKE_ODOO_DB=_fake_database()
        )

        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertLess(
            run.docker_log.index("rm readback-passed"), run.docker_log.index("exec workflow")
        )
        self.assertLess(
            run.docker_log.index("write readback-passed"),
            run.docker_log.index(f"start {WEB_CONTAINER_ID}"),
        )
        self.assertTrue(self._held_web_starts(overrides_payload="payload-a"))
        # A provider deploy that changes the payload or the database starts web before
        # any read-back of it.
        self.assertFalse(self._held_web_starts(overrides_payload="payload-b"))
        self.assertFalse(
            self._held_web_starts(overrides_payload="payload-a", database_name="other_db")
        )

    def test_unset_or_empty_payload_releases_web_after_a_pass(self) -> None:
        for environment in ({}, {"FAKE_CONTAINER_PAYLOAD": ""}):
            with self.subTest(environment=environment):
                run = self._run(
                    _render_restore_script(), FAKE_ODOO_DB=_fake_database(), **environment
                )
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertIn("write readback-passed", run.docker_log)
                self.assertIn("odoo_restore_completed=true", run.stdout.splitlines())
                self.assertTrue(self._held_web_starts(overrides_payload=""))

    def test_failed_payload_read_keeps_web_held_and_restore_unsuccessful(self) -> None:
        for exit_status in ("1", "125"):
            with self.subTest(exit_status=exit_status):
                # An earlier pass must also be cleared before the failed read.
                (self.root / "readback-passed").write_text("previous-pass")
                run = self._run(
                    _render_restore_script(),
                    FAKE_CONTAINER_PAYLOAD="payload-a",
                    FAKE_PAYLOAD_READ_EXIT=exit_status,
                    FAKE_ODOO_DB=_fake_database(),
                )
                self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertTrue(run.readback_ran, run.docker_log)
                self.assertNotIn("write readback-passed", run.docker_log)
                self.assertNotIn("integration_readback_passed_recorded=true", run.stdout)
                self.assertNotIn("odoo_restore_completed=true", run.stdout)
                self.assertFalse(run.web_restarted, run.docker_log)
                self.assertFalse(self._held_web_starts(overrides_payload=""))
                self.assertFalse(self._held_web_starts(overrides_payload="payload-a"))

    def test_new_preview_bootstraps_its_missing_database_then_releases_web(self) -> None:
        run = self._run(
            _render_preview_script(),
            FAKE_DATABASE_PRESENT="0",
            FAKE_CONTAINER_PAYLOAD="payload-a",
            FAKE_ODOO_DB=_fake_database(),
        )

        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("workflow arguments --bootstrap", run.docker_log)
        self.assertIn("odoo_module_update_completed=true", run.stdout.splitlines())
        self.assertTrue(run.readback_ran, run.docker_log)
        self.assertTrue(self._held_web_starts(overrides_payload="payload-a"))

    def test_preview_with_a_database_runs_maintenance(self) -> None:
        run = self._run(
            _render_preview_script(),
            FAKE_DATABASE_PRESENT="1",
            FAKE_CONTAINER_PAYLOAD="payload-a",
            FAKE_ODOO_DB=_fake_database(),
        )

        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("workflow arguments --post-deploy-maintenance", run.docker_log)

    def test_unanswered_database_probe_stops_before_touching_web(self) -> None:
        run = self._run(_render_preview_script(), FAKE_DATABASE_PRESENT="error")

        self.assertNotEqual(run.returncode, 0)
        self.assertNotIn("exec workflow", run.docker_log)
        self.assertNotIn(f"stop {WEB_CONTAINER_ID}", run.docker_log)

    def test_stable_lanes_never_probe_or_bootstrap_on_deploy(self) -> None:
        run = self._run(
            _render_restore_script(),
            FAKE_DATABASE_PRESENT="0",
            FAKE_ODOO_DB=_fake_database(),
        )

        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotIn("probe database", run.docker_log)
        self.assertNotIn("workflow arguments --bootstrap", run.docker_log)

    def test_failed_workflow_never_releases_held_web(self) -> None:
        # The workflow may fail before it applies a new payload; the clean database
        # then says nothing about the payload web would apply when it starts.
        run = self._run(
            _render_script(),
            FAKE_CONTAINER_PAYLOAD="payload-a",
            FAKE_WORKFLOW_EXIT="7",
            FAKE_ODOO_DB=_fake_database(),
        )

        self.assertEqual(run.returncode, 7)
        self.assertIn("integration_readback_ok=true", run.stdout.splitlines())
        self.assertNotIn("write readback-passed", run.docker_log)
        self.assertFalse(self._held_web_starts(overrides_payload="payload-a"))

    def test_refusal_clears_an_earlier_pass_so_restarted_web_stays_held(self) -> None:
        passed = self._run(
            _render_script(), FAKE_CONTAINER_PAYLOAD="payload-a", FAKE_ODOO_DB=_fake_database()
        )
        refused = self._run(
            _render_script(),
            FAKE_CONTAINER_PAYLOAD="payload-a",
            FAKE_ODOO_DB=_fake_database(config={"printnode.api_key": PRODUCTION_VALUE}),
        )

        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assert_refused_and_web_stopped(refused, "printnode/printnode.api_key")
        self.assertNotIn("write readback-passed", refused.docker_log)
        self.assertFalse(self._held_web_starts(overrides_payload="payload-a"))

    def test_production_lane_never_touches_the_readback_pass(self) -> None:
        run = self._run(_render_script(instance="prod"), FAKE_CONTAINER_PAYLOAD="payload-a")

        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotIn("rm readback-passed", run.docker_log)
        self.assertNotIn("write readback-passed", run.docker_log)

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

    def test_each_family_setting_fails_and_leaves_web_stopped(self) -> None:
        # A restored production copy on a testing lane with no allowance. This also
        # fails if the read-back's docker exec loses -i (#2557): the empty program
        # would exit 0 and web would restart.
        script = _render_restore_script()
        for family in INTEGRATION_FAMILIES:
            settings = [
                *(
                    (f"{family.integration}/{key}", _fake_database(config={key: PRODUCTION_VALUE}))
                    for key in family.config_parameters
                ),
                *(
                    (
                        f"{family.integration}/{table.table}",
                        _fake_database(tables={table.table: True}),
                    )
                    for table in family.tables
                ),
            ]
            self.assertTrue(settings, family.integration)
            for entry, database in settings:
                with self.subTest(setting=entry):
                    run = self._run(script, FAKE_ODOO_DB=database)

                    self.assert_refused_and_web_stopped(run, entry)
                    self.assertIn(f"integration_readback_refused={entry}", run.stdout)

    def test_allowance_passes_the_matching_integration_only(self) -> None:
        database = _fake_database(
            config={"printnode.api_key": PRODUCTION_VALUE, "shopify.api_token": PRODUCTION_VALUE}
        )

        allowed = self._run(
            _render_script(_policies(allowed=("printnode", "shopify"))), FAKE_ODOO_DB=database
        )
        partly_allowed = self._run(
            _render_script(_policies(allowed=("printnode",))), FAKE_ODOO_DB=database
        )

        self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)
        self.assertIn("integration_readback_ok=true", allowed.stdout.splitlines())
        self.assertIn(
            "integration_readback_allowed=shopify/shopify.api_token,printnode/printnode.api_key",
            allowed.stdout.splitlines(),
        )
        self.assertTrue(allowed.web_restarted, allowed.docker_log)
        self.assert_refused_and_web_stopped(partly_allowed, "shopify/shopify.api_token")
        self.assertNotIn("printnode.api_key setting", partly_allowed.stderr)

    def test_empty_settings_pass_and_restart_web(self) -> None:
        run = self._run(
            _render_script(),
            FAKE_ODOO_DB=_fake_database(
                config={"printnode.api_key": "  ", "shopify.api_version": "2026-07"},
                tables={"ir_mail_server": False, "payment_provider": False},
            ),
        )

        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("integration_readback_ok=true", run.stdout.splitlines())
        self.assertTrue(run.web_restarted, run.docker_log)
        self.assertLess(
            run.docker_log.index("exec readback -i"),
            run.docker_log.index(f"start {WEB_CONTAINER_ID}"),
        )

    def test_protected_store_key_fails_even_with_a_shopify_allowance(self) -> None:
        run = self._run(
            _render_script(
                _policies(protected_store_keys=(PROTECTED_STORE_KEY,), allowed=("shopify",))
            ),
            FAKE_ODOO_DB=_fake_database(
                config={"shopify.shop_url_key": f"{PROTECTED_STORE_KEY.upper()}.myshopify.com"}
            ),
        )

        self.assert_refused_and_web_stopped(run, "shopify/shopify.shop_url_key:protected")

    def test_web_left_stopped_by_a_refusal_starts_once_the_readback_passes(self) -> None:
        cleared = self._run(_render_script(), web_status="exited", FAKE_ODOO_DB=_fake_database())
        still_refused = self._run(
            _render_script(),
            web_status="exited",
            FAKE_ODOO_DB=_fake_database(config={"printnode.api_key": PRODUCTION_VALUE}),
        )

        self.assertEqual(cleared.returncode, 0, cleared.stdout + cleared.stderr)
        self.assertTrue(cleared.web_restarted, cleared.docker_log)
        self.assertLess(
            cleared.docker_log.index("exec readback -i"),
            cleared.docker_log.index(f"start {WEB_CONTAINER_ID}"),
        )
        self.assertNotEqual(still_refused.returncode, 0)
        self.assertFalse(still_refused.web_restarted, still_refused.docker_log)

    def test_stopped_web_without_a_readback_still_fails(self) -> None:
        run = self._run(_render_script(instance="prod"), web_status="exited")

        self.assertNotEqual(run.returncode, 0)
        self.assertIn("Expected a running web container", run.stderr)
        self.assertFalse(run.web_restarted, run.docker_log)

    def test_dev_store_with_shopify_allowance_passes(self) -> None:
        run = self._run(
            _render_script(
                _policies(protected_store_keys=(PROTECTED_STORE_KEY,), allowed=("shopify",))
            ),
            FAKE_ODOO_DB=_fake_database(config={"shopify.shop_url_key": DEV_STORE_KEY}),
        )

        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue(run.web_restarted, run.docker_log)

    def test_unreadable_database_fails_closed_and_leaves_web_stopped(self) -> None:
        run = self._run(_render_script(), FAKE_DB_UNREACHABLE="1")

        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(run.web_restarted, run.docker_log)
        self.assertIn("integration_readback_ok=false", run.stdout.splitlines())

    def test_workflow_failure_with_refused_setting_leaves_web_stopped(self) -> None:
        run = self._run(
            _render_script(),
            FAKE_WORKFLOW_EXIT="7",
            FAKE_ODOO_DB=_fake_database(config={"printnode.api_key": PRODUCTION_VALUE}),
        )

        self.assertEqual(run.returncode, 7)
        self.assertTrue(run.readback_ran, run.docker_log)
        self.assertFalse(run.web_restarted, run.docker_log)

    def test_workflow_failure_with_clean_database_restarts_web(self) -> None:
        run = self._run(_render_script(), FAKE_WORKFLOW_EXIT="7", FAKE_ODOO_DB=_fake_database())

        self.assertEqual(run.returncode, 7)
        self.assertIn("integration_readback_ok=true", run.stdout.splitlines())
        self.assertTrue(run.web_restarted, run.docker_log)

    def test_production_lane_without_protected_keys_skips_the_readback(self) -> None:
        run = self._run(
            _render_script(instance="prod"),
            FAKE_WORKFLOW_EXIT="7",
            FAKE_ODOO_DB=_fake_database(config={"printnode.api_key": PRODUCTION_VALUE}),
        )

        self.assertEqual(run.returncode, 7)
        self.assertFalse(run.readback_ran, run.docker_log)
        self.assertTrue(run.web_restarted, run.docker_log)

    def test_successful_restore_prints_the_completion_marker(self) -> None:
        run = self._run(
            _render_restore_script(),
            FAKE_WORKFLOW_OUTPUT="Upstream overwrite completed successfully.",
        )

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("odoo_restore_completed=true", run.stdout.splitlines())
        self.assertNotIn("odoo_restore_completed=false", run.stdout)

    def test_restore_fails_when_web_recovery_fails(self) -> None:
        script = _render_restore_script()
        for failure in (
            {"FAKE_WEB_START_FAILURE": "1"},
            {"FAKE_WEB_START_STATUS": "exited"},
            {"FAKE_FINAL_INSPECT_FAILURE": "1"},
            {"FAKE_RECOVERY_INSPECT_FAILURE": "1"},
            {"FAKE_PASS_WRITE_FAILURE": "1"},
        ):
            with self.subTest(failure=failure):
                run = self._run(script, **failure)

                self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertIn("exec workflow", run.docker_log)
                self.assertIn("odoo_restore_completed=false", run.stdout.splitlines())
                self.assertNotIn("odoo_restore_completed=true", run.stdout.splitlines())
                if "FAKE_PASS_WRITE_FAILURE" in failure:
                    self.assertFalse(run.web_restarted, run.docker_log)
                else:
                    self.assertTrue(run.web_restarted, run.docker_log)

    def test_restore_failure_keeps_its_exit_status_when_recovery_also_fails(self) -> None:
        script = _render_restore_script()
        for failure in (
            {},
            {"FAKE_WEB_START_FAILURE": "1"},
            {"FAKE_WEB_START_STATUS": "exited"},
            {"FAKE_FINAL_INSPECT_FAILURE": "1"},
        ):
            with self.subTest(failure=failure):
                run = self._run(script, FAKE_WORKFLOW_EXIT="40", **failure)

                self.assertEqual(run.returncode, 40, run.stdout + run.stderr)
                self.assertTrue(run.web_restarted, run.docker_log)
                self.assertIn("odoo_restore_completed=false", run.stdout.splitlines())
                self.assertNotIn("odoo_restore_completed=true", run.stdout.splitlines())

    def test_recovery_retries_start_after_an_initial_status_read_failure(self) -> None:
        # Fail only the read before start; a fresh read after start must succeed.
        run = self._run(_render_restore_script(), FAKE_INITIAL_INSPECT_FAILURE="1")

        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue(run.web_restarted, run.docker_log)
        start_index = run.docker_log.index(f"start {WEB_CONTAINER_ID}")
        self.assertIn("inspect web running", run.docker_log[start_index + 1 :])

    def test_maintenance_and_bootstrap_fail_when_web_cannot_restart(self) -> None:
        for script in (_render_post_deploy_script(None), _render_script()):
            with self.subTest(script=script.splitlines()[1:4]):
                run = self._run(script, FAKE_WEB_START_FAILURE="1")

                self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertTrue(run.web_restarted, run.docker_log)
                self.assertNotIn("odoo_restore_completed", run.stdout)

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
        run = self._run(_render_script(), FAKE_WORKFLOW_OUTPUT="Upstream restore failed (x).")

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("odoo_restore_completed", run.stdout)
        self.assertNotIn("odoo_restore_failure_logged", run.stdout)


if __name__ == "__main__":
    unittest.main()
