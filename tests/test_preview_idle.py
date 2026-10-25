import ast
import base64
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.runtime_identity import RuntimeIdentity, runtime_identity_env
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewPullRequestSummary,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.dokploy.compose import render_odoo_raw_compose_file
from control_plane.preview_gateway import (
    PREVIEW_GATEWAY_PROGRAM,
    PREVIEW_GATEWAY_SERVICE,
    PREVIEW_IDLE_SECONDS,
)
from control_plane.preview_idle import PreviewIdleAdapter, PreviewIdleScheduler, idle_action
from control_plane.preview_idle_runtime import PREVIEW_IDLE_RUNTIME_PROGRAM
from control_plane.provider_operations import ProviderMutationUnknownError
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.profiles import _odoo_preview_profile_payload
import yaml


def runtime_identity() -> RuntimeIdentity:
    return RuntimeIdentity(
        product="fixture",
        context="fixture-preview",
        instance="fixture-pr-1",
        environment_kind="preview",
        deployment_record_id="deploy-fixture",
        artifact_id="artifact-fixture",
        source_git_ref="a" * 40,
        image_reference="registry.invalid/odoo@sha256:" + "b" * 64,
        preview_id="preview-fixture",
        preview_generation_id="generation-fixture",
    )


def observation(*, mode: str = "awake", running: bool = True) -> dict[str, Any]:
    return {
        "gateway": {
            "mode": mode,
            "requested": False,
            "active": 0,
            "idle_seconds": PREVIEW_IDLE_SECONDS,
            "boot_id": "boot",
            "sequence": 1,
            "sleep_record_id": "sleep-fixture",
            "wake_record_id": "",
        },
        "services": {
            name: {"running": running, "healthy": running}
            for name in ("web", "database", "script-runner")
        },
    }


class PreviewIdleTests(unittest.TestCase):
    def test_only_idle_healthy_preview_sleeps_and_visit_requests_wake(self) -> None:
        value = observation()
        self.assertEqual(idle_action(value), "sleep")
        for field, changed in (
            ("active", 1),
            ("requested", True),
            ("idle_seconds", PREVIEW_IDLE_SECONDS - 1),
        ):
            changed_value = deepcopy(value)
            changed_value["gateway"][field] = changed
            self.assertEqual(idle_action(changed_value), "")
        value["services"]["web"]["healthy"] = False
        self.assertEqual(idle_action(value), "")
        value = observation(mode="asleep", running=False)
        self.assertEqual(idle_action(value), "")
        value["gateway"]["requested"] = True
        self.assertEqual(idle_action(value), "awake")

    def test_capacity_wait_keeps_the_original_wake_operation_held(self) -> None:
        adapter = PreviewIdleAdapter(
            host="fixture",
            token="fixture",
            target={},
            intent={"action": "awake"},
            record_id="wake-fixture",
            check=lambda: None,
        )
        with patch.object(adapter, "read", return_value={"accepted": False, "capacity": False}):
            with self.assertRaises(ProviderMutationUnknownError):
                adapter.apply("original-operation", Mock())

    def test_interrupted_partial_stop_resumes_only_its_own_receipt(self) -> None:
        target = {"context": "fixture", "compose_name": "fixture-pr-1"}
        adapter = PreviewIdleAdapter(
            host="fixture",
            token="fixture",
            target=target,
            intent={"action": "sleep", "control": {"boot_id": "boot", "sequence": 1}},
            record_id="sleep-fixture",
            check=lambda: None,
        )
        value = observation(mode="asleep", running=False)
        value["services"]["database"]["running"] = True
        with patch.object(adapter, "read", return_value=value):
            result = adapter.observe("original", "preview_idle_sleep", adapter.reconciliation_key())
            self.assertEqual(result.outcome, "absent")
            self.assertTrue(result.retry_safe)
            value["gateway"]["sleep_record_id"] = "another-owner"
            self.assertEqual(
                adapter.observe(
                    "original", "preview_idle_sleep", adapter.reconciliation_key()
                ).outcome,
                "unknown",
            )

    def test_changed_visit_cancels_old_sleep_instead_of_stopping_it(self) -> None:
        adapter = PreviewIdleAdapter(
            host="fixture",
            token="fixture",
            target={"context": "fixture", "compose_name": "fixture-pr-1"},
            intent={"action": "sleep", "control": {"boot_id": "boot", "sequence": 1}},
            record_id="sleep-fixture",
            check=lambda: None,
        )
        value = observation()
        value["gateway"]["sequence"] += 1
        with patch.object(adapter, "read", return_value=value):
            result = adapter.observe("original", "preview_idle_sleep", adapter.reconciliation_key())
        self.assertEqual(result.outcome, "present")
        self.assertFalse(result.response_payload["accepted"])

    def test_transport_effect_is_checkpointed_before_dispatch(self) -> None:
        adapter = PreviewIdleAdapter(
            host="fixture",
            token="fixture",
            target={"server_id": "server-fixture", "spec": {}},
            intent={"action": "sleep"},
            record_id="sleep-fixture",
            check=Mock(),
        )
        events: list[str] = []
        lease = Mock()
        lease.checkpoint_effect.side_effect = lambda _phase: events.append("checkpoint")

        def remote(**kwargs: Any) -> None:
            kwargs["dispatch_guard"](lambda: events.append("dispatch"))
            kwargs["receive"]({"type": "complete", "result": {"accepted": False}})

        with patch("control_plane.preview_idle._read_remote", side_effect=remote):
            adapter.apply("original", lease)
        self.assertEqual(events, ["checkpoint", "dispatch"])

    def test_preview_routing_moves_to_router_without_app_data_or_secrets(self) -> None:
        identity = runtime_identity()
        rendered = render_odoo_raw_compose_file(
            image_reference=identity.image_reference,
            hold_web_until_integration_readback=True,
            domain_hosts=("preview.example.invalid",),
            publish_host_ports=False,
            mount_workflow_ssh=False,
            preview_identity=identity,
        )
        services = yaml.safe_load(rendered)["services"]
        gateway = services[PREVIEW_GATEWAY_SERVICE]
        self.assertEqual(gateway["image"], services["web"]["image"])
        self.assertEqual(
            gateway["environment"],
            {
                "LAUNCHPLANE_RUNTIME_IDENTITY_JSON": runtime_identity_env(identity)[
                    "LAUNCHPLANE_RUNTIME_IDENTITY_JSON"
                ]
            },
        )
        self.assertFalse(gateway.get("volumes"))
        self.assertFalse(gateway.get("env_file"))
        self.assertTrue(
            any("Host(`preview.example.invalid`)" in label for label in gateway["labels"])
        )
        self.assertFalse(any("Host(" in label for label in services["web"]["labels"]))
        self.assertEqual(gateway["networks"], services["web"]["networks"])


class PreviewIdleOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{self.root / 'records.sqlite3'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        payload = _odoo_preview_profile_payload("fixture")
        payload["preview"] = {
            "enabled": True,
            "context": "fixture-preview",
            "app_name_prefix": "fixture",
            "slug_template": "pr-{number}",
        }
        self.profile = LaunchplaneProductProfileRecord.model_validate(payload)
        self.store.write_product_profile_record(self.profile)
        self.identity = runtime_identity()
        self.preview = PreviewRecord(
            preview_id=self.identity.preview_id,
            context=self.profile.preview.context,
            anchor_repo="fixture",
            anchor_pr_number=1,
            anchor_pr_url="https://example.invalid/pr/1",
            preview_label="preview",
            canonical_url="https://preview.example.invalid",
            state="active",
            created_at=self.profile.updated_at,
            updated_at=self.profile.updated_at,
            eligible_at=self.profile.updated_at,
        )
        self.store.write_preview_record(self.preview)
        self.generation = PreviewGenerationRecord(
            generation_id=self.identity.preview_generation_id,
            preview_id=self.preview.preview_id,
            sequence=1,
            state="ready",
            requested_reason="fixture",
            requested_at=self.profile.updated_at,
            resolved_manifest_fingerprint="fixture",
            verify_status="pass",
            runtime_identity=self.identity,
            anchor_summary=PreviewPullRequestSummary(
                **{
                    "repo": self.profile.repository,
                    "pr_number": 1,
                    "head_sha": self.identity.source_git_ref,
                    "pr_url": self.preview.anchor_pr_url,
                }
            ),
        )
        self.store.write_preview_generation_record(self.generation)
        self.preview = self.preview.model_copy(
            update={
                "serving_generation_id": self.generation.generation_id,
                "active_generation_id": self.generation.generation_id,
            }
        )
        self.store.write_preview_record(self.preview)
        raw_identity = runtime_identity_env(self.identity)["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"]
        self.provider = {
            "name": self.identity.instance,
            "composeId": "compose-fixture",
            "appName": "fixture-project",
            "sourceType": "raw",
            "serverId": "server-fixture",
            "composeFile": "services: {preview-router: {}}",
            "env": "\n".join(
                (
                    "LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + raw_identity,
                    "ODOO_DATA_VOLUME=fixture-data",
                    "ODOO_DB_VOLUME=fixture-db",
                    "ODOO_LOG_VOLUME=fixture-logs",
                    "ODOO_DB_NAME=fixture_db",
                )
            ),
        }
        self.enterContext(
            patch(
                "control_plane.preview_idle.complete_preview_compose_inventory",
                return_value=(("env", "compose-fixture", self.identity.instance),),
            )
        )
        self.enterContext(
            patch(
                "control_plane.preview_idle.api.fetch_dokploy_target_payload",
                side_effect=lambda **_: deepcopy(self.provider),
            )
        )
        self.remote = self.enterContext(
            patch("control_plane.preview_idle._read_remote", side_effect=self.transport)
        )
        self.scheduler = PreviewIdleScheduler(self.store, control_plane_root=self.root)
        self.value = observation()
        self.effects: list[dict[str, Any]] = []
        self.capacity = True
        self.interrupt = False

    def transport(self, **kwargs: Any) -> None:
        payload = kwargs["payload"]
        kwargs["dispatch_guard"](lambda: None)
        if payload["action"] != "observe":
            control = payload["control"]
            self.effects.append(deepcopy(control))
            if control["action"] == "awake" and not self.capacity:
                kwargs["receive"](
                    {
                        "type": "complete",
                        "result": {**deepcopy(self.value), "capacity": False, "accepted": False},
                    }
                )
                return
            mode = "asleep" if control["action"] == "sleep" else "awake"
            self.value["gateway"].update(mode=mode, requested=False)
            self.value["gateway"]["sleep_record_id" if mode == "asleep" else "wake_record_id"] = (
                control["record_id"]
            )
            for item in self.value["services"].values():
                item.update(running=mode == "awake", healthy=mode == "awake")
            if self.interrupt:
                self.value["services"]["database"].update(running=True, healthy=True)
                raise ProviderMutationUnknownError("Interrupted fixture stop")
        kwargs["receive"](
            {"type": "complete", "result": {**deepcopy(self.value), "accepted": True}}
        )

    def run_preview(self) -> None:
        self.scheduler._run_preview(
            self.profile, self.preview.preview_id, "https://provider.invalid", "fixture"
        )

    def test_sleep_then_visit_wake_has_durable_generation_bound_receipts(self) -> None:
        self.run_preview()
        sleep_id = self.value["gateway"]["sleep_record_id"]
        record = self.store.read_idempotency_record_by_id(sleep_id)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.state, "completed")
        self.assertEqual(
            self.store.read_original_mutation_intent(record)["target"]["compose_id"],
            self.provider["composeId"],
        )
        self.value["gateway"]["requested"] = True
        self.run_preview()
        self.assertEqual([effect["action"] for effect in self.effects], ["sleep", "awake"])
        self.assertEqual(self.effects[1]["sleep_record_id"], sleep_id)

    def test_capacity_recovers_using_the_same_wake_reservation(self) -> None:
        self.run_preview()
        self.value["gateway"]["requested"] = True
        self.capacity = False
        self.run_preview()
        original = self.effects[-1]["record_id"]
        held = self.store.read_idempotency_record_by_id(original)
        assert held is not None
        self.assertEqual(held.state, "reconcile_required")
        self.capacity = True
        self.run_preview()
        self.assertEqual(self.effects[-1]["record_id"], original)
        self.assertEqual(self.value["gateway"]["mode"], "awake")
        completed = self.store.read_idempotency_record_by_id(original)
        assert completed is not None
        self.assertEqual(completed.state, "completed")

    def test_interrupted_stop_resumes_original_intent_before_wake(self) -> None:
        self.interrupt = True
        self.run_preview()
        original = self.effects[-1]["record_id"]
        self.interrupt = False
        self.value["gateway"]["requested"] = True
        self.run_preview()
        self.assertEqual(self.effects[-1]["record_id"], original)
        self.assertEqual(self.effects[-1]["action"], "sleep")
        self.value["gateway"]["requested"] = True
        self.run_preview()
        self.assertEqual(self.effects[-1]["action"], "awake")

    def test_unrecognized_sleep_cursor_cannot_authorize_a_start(self) -> None:
        self.value["gateway"].update(mode="asleep", requested=True, sleep_record_id="foreign")
        self.run_preview()
        self.assertFalse(self.effects)

    def test_changed_generation_and_inactive_product_cannot_start_or_stop(self) -> None:
        self.store.write_preview_generation_record(
            self.generation.model_copy(update={"state": "deploying"})
        )
        self.run_preview()
        self.remote.assert_not_called()
        self.store.write_preview_generation_record(self.generation)
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"lifecycle_state": "retired"})
        )
        self.run_preview()
        self.remote.assert_not_called()

    def test_another_provider_operation_is_preserved_without_an_idle_effect(self) -> None:
        target = self.scheduler._bind(self.profile, self.preview.preview_id, "fixture", "fixture")
        assert target is not None
        adapter = PreviewIdleAdapter(
            host="fixture",
            token="fixture",
            target=target,
            intent={},
            record_id="",
            check=lambda: None,
        )
        original = self.store.reserve_mutation(
            scope="fixture-refresh",
            route_path="service:refresh",
            idempotency_key="original",
            request_fingerprint="fixture",
            lease_owner="fixture",
            provider_target_key=adapter.target_key(),
            reconciliation_key=adapter.reconciliation_key(),
            original_intent={"refresh": True},
        )
        self.run_preview()
        self.remote.assert_not_called()
        self.assertEqual(
            self.store.read_idempotency_record_by_id(original.record.record_id), original.record
        )


class PreviewIdleRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.namespace: dict[str, Any] = {}
        module = ast.parse(PREVIEW_IDLE_RUNTIME_PROGRAM)
        module.body = module.body[:-2]
        exec(compile(module, "<idle-fixture>", "exec"), self.namespace)
        self.value = observation()
        self.states = self.value["services"]
        self.calls: list[tuple[str, ...]] = []
        self.identity = runtime_identity_env(runtime_identity())[
            "LAUNCHPLANE_RUNTIME_IDENTITY_JSON"
        ]
        self.namespace["spec"] = {
            "action": "sleep",
            "identity": self.identity,
            "control": {"action": "sleep", "record_id": "sleep-fixture"},
            "memory_floor": 100,
            "disk_reserve": 100,
            "preview_volumes": {"data": "data-fixture"},
        }
        self.namespace["runtime"] = self.runtime
        self.namespace["docker"] = self.docker
        self.namespace["one_inspection"] = self.inspect
        self.namespace["control"] = self.control
        self.namespace["observe_memory_headroom"] = lambda *_: 100
        self.namespace["wait_healthy"] = lambda name: self.calls.append(("healthy", name))
        self.enterContext(patch("os.statvfs", return_value=Mock(f_bavail=100, f_frsize=1)))

    def runtime(self) -> tuple[dict[str, Any], str]:
        return {
            name: (
                name,
                {
                    "State": {
                        "Running": state["running"],
                        "Health": {"Status": "healthy" if state["healthy"] else "unhealthy"},
                    },
                    "ExecIDs": [],
                },
            )
            for name, state in self.states.items()
        }, "router"

    def inspect(self, identifier: str) -> dict[str, Any]:
        if identifier == "router":
            return {"Config": {"Cmd": [PREVIEW_GATEWAY_PROGRAM, "8069"]}}
        return dict(self.runtime()[0][identifier][1])

    def docker(self, *arguments: str) -> bytes:
        self.calls.append(arguments)
        if arguments[0] in {"stop", "start"}:
            name = arguments[-1]
            self.states[name] = {
                "running": arguments[0] == "start",
                "healthy": arguments[0] == "start",
            }
        if arguments[:2] == ("volume", "inspect"):
            return json.dumps([{"Mountpoint": "/fixture"}]).encode()
        if arguments[0] == "exec":
            return json.dumps({"runtime_identity": json.loads(self.identity)}).encode()
        return b""

    def control(self, router: str, request: dict[str, Any]) -> dict[str, Any]:
        if request["action"] != "status":
            self.calls.append(("control", request["action"]))
            self.value["gateway"]["mode"] = "asleep" if request["action"] == "sleep" else "awake"
            self.value["gateway"][
                "sleep_record_id" if request["action"] == "sleep" else "wake_record_id"
            ] = request["record_id"]
        return {**self.value["gateway"], "accepted": True}

    def test_sleep_freezes_front_door_before_stopping_all_three_services(self) -> None:
        result = self.namespace["execute"]()
        self.assertEqual(self.calls[0], ("control", "sleep"))
        self.assertEqual(
            [call[-1] for call in self.calls if call[0] == "stop"],
            ["web", "script-runner", "database"],
        )
        self.assertTrue(all(not item["running"] for item in result["services"].values()))

    def wake(self) -> None:
        self.value["gateway"]["mode"] = "asleep"
        self.namespace["spec"].update(
            action="awake",
            control={
                "action": "awake",
                "record_id": "wake-fixture",
                "sleep_record_id": "sleep-fixture",
            },
        )
        for name in self.states:
            self.states[name] = {"running": False, "healthy": False}

    def test_memory_and_disk_refuse_before_any_start(self) -> None:
        self.wake()
        self.namespace["observe_memory_headroom"] = lambda *_: 99
        self.assertFalse(self.namespace["execute"]()["capacity"])
        self.assertFalse(self.calls)
        self.namespace["observe_memory_headroom"] = lambda *_: 100
        self.namespace["spec"]["disk_reserve"] = 101
        self.assertFalse(self.namespace["execute"]()["capacity"])
        self.assertFalse(any(call[0] == "start" for call in self.calls))

    def test_wake_starts_database_first_and_opens_only_after_health_identity(self) -> None:
        self.wake()
        self.namespace["execute"]()
        self.assertEqual(
            [call[-1] for call in self.calls if call[0] == "start"],
            ["database", "script-runner", "web"],
        )
        self.assertEqual(self.calls[-1], ("control", "awake"))

    def test_failed_health_never_opens_gateway(self) -> None:
        self.wake()
        self.namespace["wait_healthy"] = Mock(side_effect=RuntimeError("fixture health failure"))
        with self.assertRaises(RuntimeError):
            self.namespace["execute"]()
        self.assertFalse(any(call == ("control", "awake") for call in self.calls))

    def test_foreign_sleep_receipt_never_starts_containers(self) -> None:
        self.wake()
        self.namespace["spec"]["control"]["sleep_record_id"] = "foreign"
        with self.assertRaises(RuntimeError):
            self.namespace["execute"]()
        self.assertFalse(self.calls)

    def test_native_entrypoint_runs_with_its_own_imports_and_project_lock(self) -> None:
        self.namespace["spec"]["compose_project"] = "fixture-project"
        self.namespace["spec"]["action"] = "observe"
        entry = ast.parse(PREVIEW_IDLE_RUNTIME_PROGRAM)
        entry.body = entry.body[-2:]
        encoded = base64.b64encode(json.dumps(self.namespace["spec"]).encode()).decode()
        output = io.StringIO()
        with TemporaryDirectory() as directory:
            with (
                patch("pathlib.Path.home", return_value=Path(directory).resolve()),
                patch("sys.argv", ["fixture", encoded, "frame:"]),
                redirect_stdout(output),
            ):
                exec(compile(entry, "<idle-entrypoint-fixture>", "exec"), self.namespace)
        result = json.loads(output.getvalue().removeprefix("frame:"))
        self.assertEqual(result["type"], "complete")
        self.assertEqual(result["result"]["gateway"]["mode"], "awake")
