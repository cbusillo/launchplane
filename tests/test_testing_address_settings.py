import base64
from contextlib import redirect_stdout
import io
import itertools
import os
import subprocess
import sys
import time
from types import SimpleNamespace
import json
import unittest
from typing import Any
from unittest.mock import patch

from control_plane import testing_address_settings as settings
from control_plane.testing_address_settings import canonical_address_payload
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.testing_address_settings import TestingAddressSettingsPlan
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.dokploy import api, source
from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable
from tests import test_testing_address as address_tests
from tests import test_odoo_testing_restore as restore_tests
from control_plane.testing_address_settings_program import CANONICAL_ADDRESS_DATABASE_READ
from control_plane.odoo_testing_restore import (
    FRESH_TESTING_RESTORE_HOST_PROGRAM,
    TESTING_COPY_COMPLETION_PARAMETER,
)


class CanonicalAddressPayloadTests(unittest.TestCase):
    def test_only_the_reviewed_canonical_values_change(self) -> None:
        original: dict[str, Any] = {
            "schema_version": 1,
            "context": "fixture",
            "instance": "testing",
            "config_parameters": [
                {
                    "key": "web.base.url",
                    "value": {"source": "literal", "value": "https://dated.example.invalid"},
                },
                {"key": "web.base.url.freeze", "value": {"source": "literal", "value": "True"}},
                {"key": "fixture.option", "value": {"source": "literal", "value": "preserve"}},
            ],
            "addon_settings": [],
            "website_bootstrap": {
                "name": "Fixture website",
                "canonical_url": "https://dated.example.invalid",
                "homepage_url": "/fixture",
            },
        }
        encoded = base64.b64encode(json.dumps(original).encode()).decode()
        updated = json.loads(
            base64.b64decode(
                canonical_address_payload(
                    encoded,
                    context="fixture",
                    before="https://dated.example.invalid",
                    after="https://stable.example.invalid",
                )
            )
        )
        expected = json.loads(json.dumps(original))
        expected["config_parameters"][0]["value"]["value"] = "https://stable.example.invalid"
        expected["website_bootstrap"]["canonical_url"] = "https://stable.example.invalid"
        self.assertEqual(updated, expected)
        for changed in (
            {**original, "instance": "prod"},
            {**original, "context": "another"},
            {**original, "config_parameters": original["config_parameters"] * 2},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                canonical_address_payload(
                    base64.b64encode(json.dumps(changed).encode()).decode(),
                    context="fixture",
                    before="https://dated.example.invalid",
                    after="https://stable.example.invalid",
                )


class CanonicalAddressRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.address = address_tests.TestingAddressTests()
        self.address.setUp()
        self.addCleanup(self.address.doCleanups)
        original_operation = next(
            record
            for record in self.address.store.list_odoo_stable_target_replacement_operation_records(
                product=self.address.product
            )
            if record.status == "reconciliation_required"
        )
        self.original_operation = original_operation
        self.address.store.write_odoo_stable_target_replacement_operation_record(
            original_operation.model_copy(
                update={
                    "status": "superseded",
                    "finished_at": self.address.now.isoformat(),
                    "disposable_supersession": self.address.operation.disposable_supersession,
                }
            )
        )
        _, self.run_address = self.address.native_address_execution()
        plan = self.address.plan
        original = {
            "context": plan.context,
            "instance": "testing",
            "schema_version": 1,
            "config_parameters": [
                {
                    "key": "web.base.url",
                    "value": {"source": "literal", "value": "https://" + plan.route_before.host},
                }
            ],
            "addon_settings": [],
            "website_bootstrap": {
                "name": "Fixture",
                "canonical_url": "https://" + plan.route_before.host,
            },
        }
        encoded = base64.b64encode(json.dumps(original).encode()).decode()
        self.environment = {"ODOO_DB_NAME": "fixture", settings.PAYLOAD_KEY: encoded}
        self.intent = TestingAddressSettingsPlan(
            original_payload=encoded,
            payload=canonical_address_payload(
                encoded,
                context=plan.context,
                before="https://" + plan.route_before.host,
                after="https://" + plan.route_after.host,
            ),
            provider_environment_sha256=settings.canonical_environment_sha256(self.environment),
            website_id=1,
            database_values_sha256=canonical_json_sha256({"database": "original"}),
        )
        self.address.plan = plan.model_copy(update={"canonical_settings": self.intent})
        self.events: list[str] = []
        self.lost = ""
        self.gate_released = False
        self.deployment_visible = False
        self.deployment_status = "done"
        self.reject_gate_once = False
        self.reject_preflight = False
        self.startup_observations = 0
        self.startup_held = False
        self.enterContext(
            patch.object(
                source, "read_dokploy_config", return_value=("https://provider.invalid", "fixture")
            )
        )
        self.enterContext(
            patch.object(api, "fetch_dokploy_target_payload", side_effect=self.provider)
        )
        self.enterContext(patch.object(api, "list_dokploy_schedules", return_value=()))
        self.enterContext(
            patch.object(
                api,
                "list_deployments_for_target",
                side_effect=lambda **_kwargs: [
                    {"deploymentId": "fixture", "status": self.deployment_status}
                ],
            )
        )
        self.enterContext(patch.object(api, "update_dokploy_target_env", side_effect=self.update))
        self.enterContext(patch.object(api, "trigger_deployment", side_effect=self.deploy))
        self.enterContext(
            patch.object(settings, "canonical_settings_frame", side_effect=self.frame)
        )

    def provider(self, **_kwargs: Any) -> dict[str, Any]:
        plan = self.address.plan
        return {
            "composeId": plan.before.target_id.target_id,
            "name": plan.before.target.target_name,
            "serverId": plan.server_id,
            "appName": plan.provider_project,
            "domains": [],
            "env": api.serialize_dokploy_env_text(self.environment),
            "composeType": "docker-compose",
        }

    def lose_reply(self, phase: str) -> None:
        if self.lost == phase:
            self.lost = ""
            raise NativeSourceUnavailable("Fixture lost reply")

    def update(self, **kwargs: Any) -> None:
        self.events.append("environment")
        self.assertEqual(kwargs["target_id"], self.address.plan.before.target_id.target_id)
        self.environment = api.parse_dokploy_env_text(kwargs["env_text"])
        self.lose_reply("environment")

    def deploy(self, **kwargs: Any) -> None:
        self.events.append("deployment")
        self.assertEqual(kwargs["target_id"], self.address.plan.before.target_id.target_id)
        self.lose_reply("deployment")
        self.deployment_visible = True

    def frame(self, **kwargs: Any) -> dict[str, object]:
        if kwargs["mode"] == "observe" and not self.deployment_visible:
            raise NativeSourceUnavailable("Fixture still has the original runtime payload")
        if kwargs["mode"] == "inspect" and self.reject_preflight:
            raise NativeSourceUnavailable("Fixture integration preflight refused")
        if kwargs["mode"] == "gate":

            def send() -> None:
                self.events.append("gate")
                if self.reject_gate_once:
                    self.reject_gate_once = False
                    raise NativeSourceUnavailable("Fixture gate refusal before its write")
                self.gate_released = True
                self.lose_reply("gate")

            kwargs["dispatch_guard"](send)
        ready = self.gate_released
        if kwargs["mode"] == "readback":
            if self.startup_observations:
                self.startup_observations -= 1
                ready = False
            ready = ready and not self.startup_held
        return {
            "website_id": self.intent.website_id,
            "database_values_sha256": self.intent.database_values_sha256,
            "canonical_settings_verified": ready,
            "canonical_gate_released": self.gate_released,
        }

    def test_canonical_phases_recover_lost_environment_reply_without_reapplying(self) -> None:
        self.lost = "environment"
        with self.assertRaises(NativeSourceUnavailable):
            self.run_address(False)
        result = self.run_address(False)
        self.assertEqual(result.status, "completed")
        evidence = result.response_payload["result"]
        self.assertIsInstance(evidence, dict)
        assert isinstance(evidence, dict)
        self.assertTrue(evidence["canonical_settings_verified"])
        self.assertEqual(self.events, ["environment", "deployment", "gate"])
        self.assertEqual(self.run_address(False).status, "replayed")
        self.assertEqual(self.events, ["environment", "deployment", "gate"])

    def lose_deployment_reply(self) -> None:
        self.lost = "deployment"
        with self.assertRaises(NativeSourceUnavailable):
            self.run_address(False)

    def test_unobserved_and_failed_deployments_require_positive_native_completion(self) -> None:
        self.lose_deployment_reply()
        for status in ("done", "failed"):
            self.deployment_status = status
            with self.assertRaises(NativeSourceUnavailable):
                self.run_address(False)
            self.assertEqual(self.events, ["environment", "deployment"])
        self.deployment_visible = True
        self.assertEqual(self.run_address(False).status, "completed")
        self.assertEqual(self.events, ["environment", "deployment", "gate"])

    def test_lost_gate_reply_is_read_back_without_another_writer(self) -> None:
        self.lost = "gate"
        with self.assertRaises(NativeSourceUnavailable):
            self.run_address(False)
        self.assertEqual(self.run_address(False).status, "completed")
        self.assertEqual(self.events, ["environment", "deployment", "gate"])

    def test_unrelated_unknown_operation_blocks_all_canonical_effects(self) -> None:
        self.address.store.write_odoo_stable_target_replacement_operation_record(
            self.original_operation.model_copy(
                update={
                    "operation_id": "fixture-unrelated-unknown",
                    "idempotency_key": "fixture-unrelated-key",
                }
            )
        )
        with self.assertRaises(ValueError):
            self.run_address(False)
        self.assertFalse(self.events)

    def test_gate_refusal_recovers_after_positive_absence_observation(self) -> None:
        self.reject_gate_once = True
        with self.assertRaises(NativeSourceUnavailable):
            self.run_address(False)
        self.assertFalse(self.gate_released)
        self.assertEqual(self.run_address(False).status, "completed")
        self.assertEqual(self.events, ["environment", "deployment", "gate", "gate"])

    def test_integration_refusal_precedes_route_and_runtime_effects(self) -> None:
        self.reject_preflight = True
        original = self.address.fixture.current()
        with self.assertRaises(NativeSourceUnavailable):
            self.run_address(False)
        self.assertFalse(self.events)
        self.assertEqual(self.address.fixture.current(), original)

    def test_startup_wait_and_timeout_resume_without_redeploy(self) -> None:
        self.startup_held = True
        with (
            patch.object(time, "monotonic", side_effect=itertools.count(0, 3600)),
            self.assertRaises(NativeSourceUnavailable),
        ):
            self.run_address(False)
        self.startup_held = False
        self.startup_observations = 2
        with patch.object(time, "sleep") as pause:
            self.assertEqual(self.run_address(False).status, "completed")
        self.assertTrue(pause.called)
        self.assertEqual(self.events, ["environment", "deployment", "gate"])

    def test_running_deployment_prevents_adoption_of_a_visible_runtime(self) -> None:
        self.lose_deployment_reply()
        self.deployment_visible = True
        self.deployment_status = "running"
        with self.assertRaises((NativeSourceUnavailable, ValueError)):
            self.run_address(False)
        self.assertEqual(self.events, ["environment", "deployment"])
        self.deployment_status = "done"
        self.assertEqual(self.run_address(False).status, "completed")

    def test_permanent_runtime_refusal_is_preserved_without_polling(self) -> None:
        refusal = ValueError("Fixture reviewed intent changed")
        original = settings.CanonicalAddressEffect.frame

        def frame(effect: Any, *args: Any, **kwargs: Any) -> Any:
            if effect.phase == "deployment":
                raise refusal
            return original(effect, *args, **kwargs)

        with (
            patch.object(settings.CanonicalAddressEffect, "frame", frame),
            patch.object(time, "monotonic", side_effect=itertools.count(0, 3600)),
            patch.object(time, "sleep") as pause,
            self.assertRaises(ValueError) as caught,
        ):
            self.run_address(False)
        self.assertIs(caught.exception, refusal)
        self.assertFalse(pause.called)
        self.assertEqual(self.events, ["environment", "deployment"])


class CanonicalAddressProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.native = restore_tests.FreshTestingHostProgramTests()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.parameters = {
            "web.base.url": "https://dated.example.invalid",
            "web.base.url.freeze": "True",
            TESTING_COPY_COMPLETION_PARAMETER: "fixture-copy",
        }
        self.websites = [(1, "dated.example.invalid")]
        self.gate = self.native.root / "canonical-gate"
        self.integration_ok = True
        self.foreign_network_member = False
        self.identity = {"product": "fixture", "context": "fixture", "instance": "testing"}
        self.native.spec.update(
            verification_only=True,
            inspection_only=False,
            web_gate_path=str(self.gate),
            data_workflow_lock_path=str(self.native.root / "workflow-lock"),
            copy_binding_sha256="fixture-copy",
            credential_program=base64.b64encode(
                b"print('candidate_credentials_verified=true')"
            ).decode(),
        )
        self.native.spec["canonical_settings"] = {
            "mode": "gate",
            "payload": "fixture-payload",
            "runtime_identity": self.identity,
            "canonical_url": "https://stable.example.invalid",
            "website_id": 1,
            "copy_witness_parameter": TESTING_COPY_COMPLETION_PARAMETER,
        }
        for service in ("web", "script-runner"):
            self.native.containers[service]["Config"]["Env"] += [
                "LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + json.dumps(self.identity),
                "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64=fixture-payload",
            ]
        self.environment = {
            "ODOO_DB_NAME": self.native.database,
            "ODOO_DB_USER": "fixture",
            "ODOO_DB_PASSWORD": "fixture",
            "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64": "fixture-payload",
        }
        self.native.spec["canonical_settings"]["database_values_sha256"] = self.database_read()[
            "database_values_sha256"
        ]

    def cursor(self) -> Any:
        fixture = self

        class Cursor:
            query = ""

            def __enter__(self) -> "Cursor":
                return self

            def __exit__(self, *_args: Any) -> None:
                return None

            def execute(self, query: str, _values: object = None) -> None:
                self.query = query

            def fetchall(self) -> list[Any]:
                return (
                    list(fixture.websites)
                    if "FROM WEBSITE" in self.query.upper()
                    else list(fixture.parameters.items())
                )

        return Cursor()

    def python(self, program: str, arguments: list[str], stdin: bytes = b"") -> bytes:
        output = io.StringIO()
        connection = SimpleNamespace(cursor=self.cursor, rollback=lambda: None, close=lambda: None)
        with (
            patch.dict(
                sys.modules, {"psycopg2": SimpleNamespace(connect=lambda **_kwargs: connection)}
            ),
            patch.dict(os.environ, self.environment),
            patch.object(
                sys,
                "argv",
                [
                    "fixture",
                    *[
                        str(self.gate)
                        if argument == self.native.spec["web_gate_path"]
                        else argument
                        for argument in arguments
                    ],
                ],
            ),
            patch.object(sys, "stdin", io.StringIO(stdin.decode())),
            redirect_stdout(output),
        ):
            exec(compile(program, "<canonical-container>", "exec"), {})
        return output.getvalue().encode()

    def database_read(self) -> dict[str, Any]:
        specification = {
            **self.native.spec["canonical_settings"],
            "copy_binding_sha256": self.native.spec["copy_binding_sha256"],
        }
        result = json.loads(
            self.python(CANONICAL_ADDRESS_DATABASE_READ, [], json.dumps(specification).encode())
        )
        assert isinstance(result, dict)
        return result

    def command(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if command[:3] == ["docker", "inspect", "testing-data"]:
            members: dict[str, dict[str, object]] = {
                item["Id"]: {} for item in self.native.containers.values()
            }
            if self.foreign_network_member:
                members["foreign"] = {}
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps([{"Internal": True, "Containers": members}]).encode(),
                stderr=b"",
            )
        if command[:2] == ["docker", "exec"] and "python3" in command:
            position = command.index("python3")
            arguments = command[position + 1 :]
            if arguments[0] == "-c":
                output = self.python(arguments[1], arguments[2:], kwargs.get("input") or b"")
            elif kwargs.get("input") == self.native.spec["readback_program"].encode():
                output = ("integration_readback_ok=" + str(self.integration_ok).lower()).encode()
            else:
                raise AssertionError("Canonical settings attempted an unrelated runner workflow")
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr=b"")
        return self.native.command(command, **kwargs)

    def run_program(self) -> tuple[int, dict[str, Any]]:
        output = io.StringIO()
        status = 0
        with (
            patch.object(
                sys,
                "argv",
                [
                    "canonical",
                    base64.b64encode(json.dumps(self.native.spec).encode()).decode(),
                    "fixture:",
                ],
            ),
            patch("subprocess.run", side_effect=self.command),
            patch("tempfile.gettempdir", return_value=str(self.native.tmp)),
            patch("pathlib.Path.home", return_value=self.native.root),
            redirect_stdout(output),
        ):
            try:
                exec(compile(FRESH_TESTING_RESTORE_HOST_PROGRAM, "<canonical-host>", "exec"), {})
            except SystemExit as error:
                status = int(error.code or 0)
        frames = [
            json.loads(line.removeprefix("fixture:"))
            for line in output.getvalue().splitlines()
            if line.startswith("fixture:")
        ]
        self.assertTrue(frames, output.getvalue())
        return status, frames[-1]

    def test_gate_release_does_not_claim_unapplied_database_values(self) -> None:
        original = dict(self.parameters)
        status, frame = self.run_program()
        self.assertEqual(status, 0, frame)
        self.assertTrue(frame["canonical_gate_released"])
        self.assertFalse(frame["canonical_settings_verified"])
        self.assertEqual(self.parameters, original)
        self.assertTrue(self.gate.exists())
        self.native.spec["inspection_only"] = True
        self.native.spec["canonical_settings"]["mode"] = "readback"
        status, frame = self.run_program()
        self.assertEqual(status, 0, frame)
        self.assertFalse(frame["canonical_settings_verified"])
        self.parameters["web.base.url"] = "https://stable.example.invalid"
        self.websites = [(1, "stable.example.invalid")]
        status, frame = self.run_program()
        self.assertEqual(status, 0, frame)
        self.assertTrue(frame["canonical_settings_verified"])

    def test_unqualified_copy_or_connections_never_release_the_gate(self) -> None:
        for refusal in ("network", "integration", "copy", "website", "read-route"):
            with self.subTest(refusal=refusal):
                self.foreign_network_member = refusal == "network"
                self.integration_ok = refusal != "integration"
                self.parameters[TESTING_COPY_COMPLETION_PARAMETER] = (
                    "other" if refusal == "copy" else "fixture-copy"
                )
                self.websites = (
                    [(1, "dated.example.invalid"), (2, "foreign.example.invalid")]
                    if refusal == "website"
                    else [(1, "dated.example.invalid")]
                )
                self.native.spec["inspection_only"] = refusal == "read-route"
                status, frame = self.run_program()
                self.assertNotEqual(status, 0, frame)
                self.assertFalse(self.gate.exists())

    def test_observer_proves_absence_while_http_is_held_and_preflight_checks_integrations(
        self,
    ) -> None:
        self.native.spec["credential_program"] = base64.b64encode(
            b"raise RuntimeError('HTTP is held')"
        ).decode()
        self.native.spec["inspection_only"] = True
        self.native.spec["canonical_settings"]["mode"] = "observe"
        status, frame = self.run_program()
        self.assertEqual(status, 0, frame)
        self.assertFalse(frame["canonical_gate_released"])
        self.assertFalse(self.gate.exists())
        self.native.spec["credential_program"] = base64.b64encode(
            b"print('candidate_credentials_verified=true')"
        ).decode()
        self.native.spec["canonical_settings"]["mode"] = "inspect"
        self.integration_ok = False
        status, frame = self.run_program()
        self.assertNotEqual(status, 0, frame)
        self.assertFalse(self.gate.exists())

    def test_real_service_spec_reaches_the_qualified_native_gate(self) -> None:
        identity = RuntimeIdentity(
            product=self.native.gen.product,
            context=self.native.gen.context,
            instance="testing",
            artifact_id="fixture",
            deployment_record_id="fixture",
            source_git_ref="fixture",
            image_reference=self.native.spec["image_reference"],
        )
        self.identity = identity.model_dump(mode="json", exclude={"deployed_at"})
        for service in ("web", "script-runner"):
            values = self.native.containers[service]["Config"]["Env"]
            self.native.containers[service]["Config"]["Env"] = [
                value
                for value in values
                if not value.startswith("LAUNCHPLANE_RUNTIME_IDENTITY_JSON=")
            ] + ["LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + json.dumps(self.identity)]
        plan: Any = SimpleNamespace(
            context=self.native.gen.context,
            before=SimpleNamespace(
                generation=self.native.gen,
                inventory=SimpleNamespace(runtime_identity=identity),
                target_id=SimpleNamespace(target_id="fixture-target"),
                target=SimpleNamespace(target_name=self.native.gen.namespace),
            ),
            provider_project=self.native.spec["compose_project"],
            server_id="fixture-server",
            route_before=SimpleNamespace(host="dated.example.invalid"),
            route_after=SimpleNamespace(host="stable.example.invalid"),
            profile=SimpleNamespace(runtime_port=8069),
        )
        publication: Any = SimpleNamespace(
            plan=SimpleNamespace(
                candidate=SimpleNamespace(
                    namespace=self.native.gen.namespace,
                    resources_binding_sha256=self.native.gen.resources_binding_sha256,
                )
            )
        )
        environment = {
            "ODOO_DB_NAME": self.native.database,
            settings.PAYLOAD_KEY: base64.b64encode(
                json.dumps(
                    {
                        "schema_version": 1,
                        "context": plan.context,
                        "instance": "testing",
                        "config_parameters": [
                            {
                                "key": "web.base.url",
                                "value": {
                                    "source": "literal",
                                    "value": "https://" + plan.route_before.host,
                                },
                            }
                        ],
                        "website_bootstrap": {
                            "name": "Fixture",
                            "canonical_url": "https://" + plan.route_before.host,
                        },
                    }
                ).encode()
            ).decode(),
        }
        provider = {
            "composeId": "fixture-target",
            "name": self.native.gen.namespace,
            "serverId": "fixture-server",
            "appName": self.native.spec["compose_project"],
            "domains": [],
            "env": api.serialize_dokploy_env_text(environment),
        }

        def remote(**kwargs: Any) -> None:
            self.native.spec = kwargs["payload"]
            self.parameters[TESTING_COPY_COMPLETION_PARAMETER] = self.native.spec[
                "copy_binding_sha256"
            ]
            status, native_frame = self.run_program()
            self.assertEqual(status, 0, native_frame)
            kwargs["receive"](native_frame)

        def runtime_payload(payload: str) -> None:
            environment[settings.PAYLOAD_KEY] = payload
            provider["env"] = api.serialize_dokploy_env_text(environment)
            self.environment[settings.PAYLOAD_KEY] = payload
            for runtime_service in ("web", "script-runner"):
                container_environment = self.native.containers[runtime_service]["Config"]["Env"]
                self.native.containers[runtime_service]["Config"]["Env"] = [
                    value
                    for value in container_environment
                    if not value.startswith(settings.PAYLOAD_KEY + "=")
                ] + [settings.PAYLOAD_KEY + "=" + payload]

        with (
            patch.object(api, "list_dokploy_schedules", return_value=()),
            patch.object(api, "list_deployments_for_target", return_value=[]),
            patch.dict(
                settings.__dict__,
                {
                    "run_managed_server_program": remote,
                    "CANDIDATE_CONTAINER_PROBE": "print('candidate_credentials_verified=true')",
                },
            ),
        ):
            runtime_payload(environment[settings.PAYLOAD_KEY])
            intent = settings.plan_canonical_settings(
                host="https://provider.invalid",
                token="fixture",
                plan=plan,
                publication=publication,
                provider=provider,
            )
            self.assertNotEqual(intent.original_payload, intent.payload)
            for mode in ("inspect", "gate", "observe"):
                runtime_payload(intent.original_payload if mode == "inspect" else intent.payload)
                frame = settings.canonical_settings_frame(
                    host="https://provider.invalid",
                    token="fixture",
                    plan=plan,
                    publication=publication,
                    settings=intent,
                    provider=provider,
                    mode=mode,
                )
        self.assertTrue(frame["canonical_gate_released"])
        self.assertFalse(frame["canonical_settings_verified"])
