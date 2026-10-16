"""Execute fixed destination programs against isolated files and bounded Docker doubles."""

import base64
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import Mock, patch

from control_plane.dokploy.post_deploy import extract_odoo_post_deploy_readback_markers
from control_plane.dokploy.api import JsonObject
from control_plane.production_recipient_host import (
    RECOVERY_LIBRARY,
    RecipientProbeCleanupError,
    run_recipient_program,
    upstream_program,
)
from control_plane.production_recipient_resources import RESOURCE_LIBRARY
from tests import test_production_recipient as preparation_tests


class UpstreamProgramTests(unittest.TestCase):
    def test_actual_grants_refuse_writers_roles_unknowns_and_keep_values_private(self) -> None:
        for grants, passed in (
            ("GRANT SELECT ON `source`.* TO 'reader'@'%'", True),
            ("GRANT ALL PRIVILEGES ON *.* TO 'reader'@'%'", False),
            ("GRANT 'writer_role' TO 'reader'@'%'", False),
            ("unavailable", False),
        ):
            with self.subTest(grants=grants):
                namespace = "lp-prod-recipient-" + "a" * 32
                spec: dict[str, Any] = {
                    "image": "mariadb@sha256:" + "b" * 64,
                    "project": namespace,
                    "binding": "c" * 64,
                    "connections": [
                        {
                            "settings": {
                                "host": "source.invalid",
                                "port": "3306",
                                "user": "reader",
                                "database": "source",
                                "password": "private-fixture-password",
                            }
                        }
                    ],
                }
                network = {
                    "Driver": "bridge",
                    "EnableIPv6": False,
                    "Options": {},
                    "Containers": {},
                    "Labels": {
                        "com.docker.compose.project": namespace,
                        "com.docker.compose.network": "default",
                        "io.launchplane.production-recipient": spec["binding"],
                    },
                }
                containers: dict[str, dict[str, Any]] = {}

                def docker(args: list[str], **kwargs: Any) -> Any:
                    call = args[1:]
                    if call[:2] == ["network", "ls"]:
                        result = ""
                    elif call[:2] == ["network", "create"]:
                        result = "network"
                    elif call[:2] == ["network", "inspect"]:
                        result = json.dumps([network])
                    elif call[0] == "create":
                        name = call[call.index("--name") + 1]
                        containers[name] = {
                            "State": {"ExitCode": 0},
                            "Config": {"Labels": {"io.launchplane.upstream-probe": name}},
                        }
                        self.assertNotIn(spec["connections"][0]["settings"]["password"], args)
                        result = name
                    elif call[0] == "start":
                        result = grants
                    elif call[0] == "inspect":
                        result = json.dumps([containers[call[1]]])
                    elif call[0] == "ps":
                        result = "\n".join(containers)
                    elif call[0] == "rm":
                        containers.pop(call[-1])
                        result = ""
                    else:
                        self.fail(f"Unexpected operation {call}")
                    return subprocess.CompletedProcess(args, 0, stdout=result, stderr="")

                output = io.StringIO()
                code = 0
                with (
                    patch.object(
                        sys, "argv", ["probe", base64.b64encode(json.dumps(spec).encode()).decode()]
                    ),
                    patch("subprocess.run", side_effect=docker),
                    redirect_stdout(output),
                ):
                    try:
                        exec(compile(upstream_program(), "<upstream-probe>", "exec"), {})
                    except SystemExit as error:
                        code = error.code if isinstance(error.code, int) else 1
                self.assertEqual(code == 0, passed)
                self.assertFalse(containers)
                self.assertNotIn("private-fixture-password", output.getvalue())
                self.assertNotIn("source.invalid", output.getvalue())
                evidence = extract_odoo_post_deploy_readback_markers(
                    cast(JsonObject, {"logs": output.getvalue().splitlines()})
                )
                self.assertEqual(evidence.get("recipient_upstreams_ok"), str(passed).lower())
                if passed:
                    self.assertEqual(evidence["recipient_upstreams_binding"], spec["binding"])


class HostTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = preparation_tests.ProductionRecipientTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root = fixture.root
        self.record = fixture.adapter.current().model_copy(
            update={"target_id": "prepared", "app_name": "prepared-recipient"}
        )

    def test_writer_actions_keep_distinct_slots_and_retirement_scrubs_only_this_namespace(
        self,
    ) -> None:
        from control_plane.production_recipient_fence import RETIRE_EXECUTION_PROGRAM

        slots: dict[tuple[str, str], dict[str, Any]] = {}

        def run(**kwargs: Any) -> dict[str, str]:
            payload = kwargs["schedule_payload"]
            key = (payload["name"], payload["appName"])
            slots[key] = {**payload, "scheduleId": str(len(slots))}
            return {"recipient_execution_fenced": self.record.plan_sha256}

        def upsert(**kwargs: Any) -> Any:
            key = (kwargs["schedule_name"], kwargs["app_name"])
            slots[key] = {
                **kwargs["schedule_payload"],
                "scheduleId": slots.get(key, {}).get("scheduleId", "unexpected-new-slot"),
            }
            return slots[key]

        with (
            patch(
                "control_plane.production_recipient_host.fenced_program",
                side_effect=lambda program, *args: program,
            ),
            patch(
                "control_plane.production_recipient_host.api.fetch_dokploy_target_payload",
                return_value={"serverId": self.record.request.destination_server_id},
            ),
            patch(
                "control_plane.production_recipient_host.post_deploy._resolve_dokploy_schedule_runtime",
                return_value=("server", "destination", "project", "destination"),
            ),
            patch(
                "control_plane.production_recipient_host.post_deploy.run_manual_data_schedule",
                side_effect=run,
            ),
            patch(
                "control_plane.production_recipient_host.api.list_dokploy_schedules",
                side_effect=lambda **kwargs: tuple(slots.values()),
            ),
            patch(
                "control_plane.production_recipient_host.api.upsert_dokploy_schedule",
                side_effect=upsert,
            ),
        ):
            for action in ("pause", "resume"):
                run_recipient_program(
                    self.root,
                    self.record,
                    {"action": action, "binding": self.record.plan_sha256},
                    "print('retained private fixture program')",
                    lambda _phase: None,
                )
            original = dict(slots)
            self.assertEqual(len(original), 2)
            foreign = ("another product", "other-namespace-job")
            slots[foreign] = {
                "name": foreign[0],
                "appName": foreign[1],
                "script": "preserve another owner",
                "enabled": True,
                "scheduleId": "foreign",
            }
            preserved = dict(slots[foreign])
            with patch(
                "control_plane.production_recipient_host.api.fetch_dokploy_target_payload",
                side_effect=FileNotFoundError("disposable target removed"),
            ):
                run_recipient_program(
                    self.root,
                    self.record,
                    {"namespace": self.record.namespace, "binding": self.record.plan_sha256},
                    RETIRE_EXECUTION_PROGRAM,
                    lambda _phase: None,
                    retire_execution=True,
                )
        self.assertEqual(slots[foreign], preserved)
        for key in original:
            self.assertFalse(slots[key]["enabled"])
            result = subprocess.run(
                ["bash"], input=slots[key]["script"], text=True, capture_output=True
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(result.stdout)

    def test_custodied_host_read_refuses_any_writer_mutation_or_arbitrary_program(self) -> None:
        from control_plane.production_recipient_cutover import (
            WRITER_PROGRAM,
            WRITER_RECEIPT_PROGRAM,
        )

        for program, spec in (
            (WRITER_PROGRAM, {"inspect": True, "action": "resume"}),
            (WRITER_RECEIPT_PROGRAM, {"inspect": False, "action": "receipt_pause"}),
            ("print('unowned')", {"inspect": True, "action": "parked"}),
        ):
            with (
                self.subTest(spec=spec),
                patch(
                    "control_plane.production_recipient_host.source.read_dokploy_config"
                ) as config,
            ):
                with self.assertRaises(ValueError):
                    run_recipient_program(
                        self.root,
                        self.record,
                        spec,
                        program,
                        lambda _phase: None,
                        custodied_read=True,
                    )
                config.assert_not_called()

    def test_private_spec_is_not_in_process_arguments_and_cleanup_ignores_authority_drift(
        self,
    ) -> None:
        password = "private-fixture-never-process-argv"
        spec = {"connections": [{"settings": {"password": password}}]}
        guard = Mock()

        def checkpoint(phase: str) -> None:
            if phase == "scrub_upstream_probe":
                raise ValueError("authority changed during probe")

        def run(**kwargs: Any) -> dict[str, str]:
            completed = subprocess.run(
                ["bash"],
                input=kwargs["schedule_payload"]["script"],
                text=True,
                capture_output=True,
                check=True,
                env={
                    **os.environ,
                    "PATH": str(Path(sys.executable).parent)
                    + os.pathsep
                    + os.environ.get("PATH", ""),
                },
            )
            self.assertNotIn(password, completed.stdout)
            self.assertNotIn(base64.b64encode(json.dumps(spec).encode()).decode(), completed.stdout)
            return {"recipient_upstreams_ok": "true"}

        from control_plane.production_recipient_fence import fenced_program

        with (
            patch(
                "control_plane.production_recipient_host.fenced_program",
                side_effect=lambda program, namespace, binding: fenced_program(
                    program, namespace, binding, base=str(self.root.resolve() / "execution")
                ),
            ),
            patch(
                "control_plane.production_recipient_host.api.fetch_dokploy_target_payload",
                return_value={"serverId": self.record.request.destination_server_id},
            ),
            patch(
                "control_plane.production_recipient_host.post_deploy._resolve_dokploy_schedule_runtime",
                return_value=("server", "destination", "project", "destination"),
            ),
            patch(
                "control_plane.production_recipient_host.post_deploy.run_manual_data_schedule",
                side_effect=run,
            ),
            patch(
                "control_plane.production_recipient_host.api.upsert_dokploy_schedule",
                side_effect=lambda **kwargs: kwargs["schedule_payload"],
            ) as scrub,
        ):
            result = run_recipient_program(
                self.root,
                self.record,
                spec,
                "import json; print(json.dumps(sys.orig_argv))",
                checkpoint,
                scrub_guard=guard,
            )
        self.assertEqual(result["recipient_upstreams_ok"], "true")
        guard.assert_called_once()
        self.assertNotIn(password, str(scrub.call_args))
        self.assertNotIn(base64.b64encode(json.dumps(spec).encode()).decode(), str(scrub.call_args))

    def test_probe_failure_still_scrubs_and_failed_scrub_is_explicit(self) -> None:
        spec = {"connections": [{"settings": {"password": "private-fixture"}}]}
        guard = Mock()
        with (
            patch(
                "control_plane.production_recipient_host.api.fetch_dokploy_target_payload",
                return_value={"serverId": self.record.request.destination_server_id},
            ),
            patch(
                "control_plane.production_recipient_host.post_deploy._resolve_dokploy_schedule_runtime",
                return_value=("server", "destination", "project", "destination"),
            ),
            patch(
                "control_plane.production_recipient_host.post_deploy.run_manual_data_schedule",
                side_effect=TimeoutError("unknown probe"),
            ),
            patch(
                "control_plane.production_recipient_host.api.upsert_dokploy_schedule",
                side_effect=lambda **kwargs: kwargs["schedule_payload"],
            ) as scrub,
        ):
            with self.assertRaises(TimeoutError):
                run_recipient_program(
                    self.root, self.record, spec, "", lambda phase: None, scrub_guard=guard
                )
            scrub.assert_called_once()
            scrub.side_effect = lambda **kwargs: {
                "script": "stale private script",
                "enabled": False,
            }
            with self.assertRaises(RecipientProbeCleanupError):
                run_recipient_program(
                    self.root, self.record, spec, "", lambda phase: None, scrub_guard=guard
                )
            scrub.side_effect = OSError("provider unavailable")
            with self.assertRaises(RecipientProbeCleanupError):
                run_recipient_program(
                    self.root, self.record, spec, "", lambda phase: None, scrub_guard=guard
                )

    def test_readback_and_apply_cannot_replace_each_others_provider_script(self) -> None:
        with (
            patch(
                "control_plane.production_recipient_host.api.fetch_dokploy_target_payload",
                return_value={"serverId": self.record.request.destination_server_id},
            ),
            patch(
                "control_plane.production_recipient_host.post_deploy._resolve_dokploy_schedule_runtime",
                return_value=("server", "destination", "project", "destination"),
            ),
            patch(
                "control_plane.production_recipient_host.post_deploy.run_manual_data_schedule",
                return_value={},
            ) as dispatch,
        ):
            run_recipient_program(self.root, self.record, {}, "", lambda phase: None)
            run_recipient_program(self.root, self.record, {"inspect": True}, "", lambda phase: None)
        apply, readback = [call.kwargs for call in dispatch.call_args_list]
        self.assertNotEqual(apply["schedule_name"], readback["schedule_name"])
        self.assertNotEqual(apply["schedule_app_name"], readback["schedule_app_name"])

    def test_foreign_destination_refuses_before_host_dispatch(self) -> None:
        with (
            patch(
                "control_plane.production_recipient_host.api.fetch_dokploy_target_payload",
                return_value={"serverId": "another-host"},
            ),
            patch(
                "control_plane.production_recipient_host.post_deploy.run_manual_data_schedule"
            ) as dispatch,
        ):
            with self.assertRaises(ValueError):
                run_recipient_program(self.root, self.record, {}, "", lambda phase: None)
        dispatch.assert_not_called()


class RecoveryProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.paths = [self.root / role for role in ("database", "config")]
        for path in self.paths:
            path.mkdir()
        (self.paths[0] / "mysql").mkdir()
        (self.paths[0] / "table.data").write_bytes(b"needed database rows")
        (self.paths[1] / "configuration").write_bytes(b"needed config")
        self.namespace: dict[str, Any] = {}
        exec(
            compile(RESOURCE_LIBRARY + RECOVERY_LIBRARY, "<isolated-recovery>", "exec"),
            self.namespace,
        )
        self.namespace["main"] = lambda: self.paths
        self.spec = {"recovery_binding": "a" * 64, "database_image": "mariadb@sha256:" + "b" * 64}
        self.containers: dict[str, dict[str, Any]] = {}
        self.network = "none"
        self.fail_check = False
        self.check_output = ""
        self.calls: list[tuple[str, ...]] = []
        self.namespace["docker"] = self.docker

    def docker(self, *args: str) -> str:
        self.calls.append(args)
        if args[0] == "create":
            name = args[args.index("--name") + 1]
            self.containers[name] = {
                "Config": {
                    "Env": [],
                    "Labels": {"io.launchplane.recipient-recovery": self.spec["recovery_binding"]},
                },
                "HostConfig": {"NetworkMode": self.network},
            }
            return name
        if args[0] == "start":
            return ""
        if args[0] == "exec":
            if args[2] == "mariadb":
                return "1"
            if self.fail_check:
                raise ValueError("corrupt recovery")
            return self.check_output
        if args[:2] == ("image", "inspect"):
            return json.dumps([{"Config": {"Env": []}}])
        if args[0] == "inspect":
            return json.dumps([self.containers[args[1]]])
        if args[0] == "rm":
            self.containers.pop(args[-1])
            return ""
        raise AssertionError(args)

    def recover(self) -> str:
        output = io.StringIO()
        with (
            patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)),
            redirect_stdout(output),
        ):
            self.namespace["recover"](self.paths, self.spec, base=self.root / "captures")
        return output.getvalue()

    def test_capture_restores_exact_data_verifies_database_and_retains_archive(self) -> None:
        before = [self.namespace["manifest"](path) for path in self.paths]
        evidence = extract_odoo_post_deploy_readback_markers(
            cast(JsonObject, {"logs": self.recover().splitlines()})
        )
        self.assertEqual(evidence["recipient_recovery_ok"], "true")
        self.assertEqual(evidence["recipient_recovery_binding"], self.spec["recovery_binding"])
        self.assertEqual(before, [self.namespace["manifest"](path) for path in self.paths])
        destination = self.root / "captures" / self.spec["recovery_binding"]
        receipt = json.loads((destination / "outcome.json").read_text())
        self.assertTrue((destination / receipt["archive"]).exists())
        self.assertEqual(receipt["state"], "passed")
        self.assertFalse(self.containers)
        before_calls = list(self.calls)
        self.assertEqual(
            self.recover().splitlines(),
            list(evidence_key + "=" + value for evidence_key, value in evidence.items()),
        )
        self.assertEqual(self.calls, before_calls)

    def test_live_network_corrupt_database_or_symlink_never_produces_a_receipt(self) -> None:
        self.network = "bridge"
        with self.assertRaises(ValueError):
            self.recover()
        self.assertFalse(
            json.loads(
                (
                    self.root / "captures" / self.spec["recovery_binding"] / "outcome.json"
                ).read_text()
            )["state"]
            == "passed"
        )
        self.assertFalse(self.containers)
        (self.paths[1] / "alias").symlink_to(self.paths[0] / "table.data")
        with self.assertRaises(ValueError):
            self.namespace["manifest"](self.paths[1])

    def test_uncertain_integrity_preserves_capture_and_never_writes_source(self) -> None:
        self.fail_check = True
        before = (self.paths[0] / "table.data").read_bytes()
        with self.assertRaises(ValueError):
            self.recover()
        self.assertEqual((self.paths[0] / "table.data").read_bytes(), before)
        destination = self.root / "captures" / self.spec["recovery_binding"]
        self.assertTrue(list(destination.glob("attempt-*/capture.tar")))
        self.assertFalse(self.containers)

    def test_zero_exit_corrupt_table_refuses_and_original_key_can_retry_after_cleanup(self) -> None:
        self.check_output = "fixture.items\nError : Incorrect file format\nerror : Corrupt\n"
        with self.assertRaises(ValueError):
            self.recover()
        destination = self.root / "captures" / self.spec["recovery_binding"]
        self.assertEqual(
            json.loads((destination / "outcome.json").read_text())["state"], "failed_safe"
        )
        self.assertFalse(self.containers)
        self.check_output = ""
        self.assertIn("recipient_recovery_ok=true", self.recover())
        self.assertEqual(json.loads((destination / "outcome.json").read_text())["state"], "passed")

    def test_inspection_adopts_completed_witness_and_rejects_changed_capture(self) -> None:
        self.recover()
        output = io.StringIO()
        with redirect_stdout(output):
            self.namespace["inspect_recovery"](self.spec, base=self.root / "captures")
        self.assertIn("recipient_recovery_ok=true", output.getvalue())
        destination = self.root / "captures" / self.spec["recovery_binding"]
        receipt = json.loads((destination / "outcome.json").read_text())
        (destination / receipt["archive"]).write_bytes(b"wrong-capture")
        with self.assertRaises(ValueError):
            self.namespace["inspect_recovery"](self.spec, base=self.root / "captures")

    def test_insufficient_disk_refuses_before_capture_and_allows_only_witnessed_retry(self) -> None:
        with patch("shutil.disk_usage", return_value=Mock(free=0)):
            with self.assertRaises(ValueError):
                self.recover()
        self.assertFalse(self.calls)
        destination = self.root / "captures" / self.spec["recovery_binding"]
        self.assertFalse(list(destination.glob("attempt-*/capture.tar")))
        output = io.StringIO()
        with redirect_stdout(output):
            self.namespace["inspect_recovery"](self.spec, base=self.root / "captures")
        self.assertIn("recipient_recovery_retry_safe=true", output.getvalue())
        self.assertIn("recipient_recovery_ok=true", self.recover())

    def test_active_or_interrupted_attempt_never_inherits_an_old_retry_witness(self) -> None:
        with self.namespace["recovery_directory"](self.spec, base=self.root / "captures"):
            with self.assertRaises(BlockingIOError):
                self.namespace["inspect_recovery"](self.spec, base=self.root / "captures")
        self.fail_check = True
        with self.assertRaises(ValueError):
            self.recover()
        original = self.docker

        def unknown_create(*args: str) -> str:
            if args[0] == "create":
                raise TimeoutError("unknown create effect")
            return original(*args)

        self.namespace["docker"] = unknown_create
        with self.assertRaises(TimeoutError):
            self.recover()
        destination = self.root / "captures" / self.spec["recovery_binding"]
        self.assertEqual(json.loads((destination / "outcome.json").read_text())["state"], "running")
        with self.assertRaises(ValueError):
            self.namespace["inspect_recovery"](self.spec, base=self.root / "captures")
