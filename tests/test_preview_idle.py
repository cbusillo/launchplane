import ast
from collections.abc import Iterator
import base64
from copy import deepcopy
from contextlib import redirect_stdout, nullcontext, contextmanager
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.runtime_identity import RuntimeIdentity, runtime_identity_env
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.disposable_lane import DisposableLaneSafety
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    parse_launchplane_mutation_timestamp,
)
from control_plane.disposable_lanes import DisposableLaneSafetyUnknown
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
    preview_backend_alias,
)
from control_plane.preview_idle import (
    PreviewIdleAdapter,
    PreviewIdleScheduler,
    idle_action,
)
from control_plane.preview_idle_runtime import PREVIEW_IDLE_RUNTIME_PROGRAM
from control_plane.provider_operations import (
    DurableProviderMutationAdapter,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    run_durable_provider_operation,
)
from control_plane.preview_reservation_recovery import (
    classify_preview_reservation,
    recover_held_preview,
)
from control_plane.preview_idle import observe_idle_reservation
from control_plane.odoo_preview_apply_execution import (
    _original_supersession_is_quiescent,
    prepare_odoo_preview_apply_operation,
)
from control_plane.workflows.odoo_preview_runtime import ODOO_PREVIEW_SUPERSESSION_GRACE_SECONDS
from control_plane.legacy_preview_reconciliation import _require_no_held_odoo_work
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.profiles import _odoo_preview_profile_payload
from control_plane.workflows.odoo_verification import DEFAULT_ODOO_RUNTIME_HEALTH_PATH
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
        baseline = yaml.safe_load(
            render_odoo_raw_compose_file(
                image_reference=identity.image_reference,
                hold_web_until_integration_readback=True,
                domain_hosts=("preview.example.invalid",),
                publish_host_ports=False,
                mount_workflow_ssh=False,
            )
        )["services"]["web"]
        self.assertEqual(gateway["labels"], baseline["labels"])
        web_labels = dict(label.split("=", 1) for label in services["web"]["labels"])
        self.assertEqual(web_labels.get("traefik.enable"), "false")
        self.assertEqual(set(gateway["networks"]), set(services["web"]["networks"]))
        self.assertEqual(
            services["web"]["networks"]["default"]["aliases"],
            [
                preview_backend_alias(
                    runtime_identity_env(identity)["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"]
                )
            ],
        )


class PreviewIdleOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{self.root / 'records.sqlite3'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.enterContext(
            patch.object(self.store, "serialize_existing_preview_refresh", side_effect=nullcontext)
        )
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
                repo=self.profile.repository,
                pr_number=1,
                head_sha=self.identity.source_git_ref,
                pr_url=self.preview.anchor_pr_url,
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
                "control_plane.preview_idle.source.read_dokploy_config",
                return_value=("https://provider.invalid", "fixture"),
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
        self.refuse_sleep = False

    def transport(self, **kwargs: Any) -> None:
        payload = kwargs["payload"]
        kwargs["dispatch_guard"](lambda: None)
        if payload["action"] not in {"observe", "poll"}:
            control = payload["control"]
            self.effects.append(deepcopy(control))
            if control["action"] == "sleep" and self.refuse_sleep:
                kwargs["receive"](
                    {"type": "complete", "result": {**deepcopy(self.value), "accepted": False}}
                )
                return
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
                if control["action"] == "awake":
                    self.value["gateway"].update(mode="asleep", requested=True)
                    for item in self.value["services"].values():
                        item.update(running=False, healthy=False)
                self.value["services"]["database"].update(running=True, healthy=True)
                raise ProviderMutationUnknownError("Interrupted fixture stop")
        kwargs["receive"](
            {
                "type": "complete",
                "result": {
                    **deepcopy(self.value),
                    **({"accepted": True} if payload["action"] not in {"observe", "poll"} else {}),
                },
            }
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

    def test_capacity_refusal_finishes_without_a_fence_and_next_attempt_can_wake(self) -> None:
        self.run_preview()
        self.value["gateway"]["requested"] = True
        self.capacity = False
        self.run_preview()
        original = self.effects[-1]["record_id"]
        held = self.store.read_idempotency_record_by_id(original)
        assert held is not None
        self.assertEqual(held.state, "completed")
        self.assertFalse(held.response_payload["accepted"])
        self.capacity = True
        self.run_preview()
        self.assertNotEqual(self.effects[-1]["record_id"], original)
        self.assertEqual(self.value["gateway"]["mode"], "awake")
        completed = self.store.read_idempotency_record_by_id(original)
        assert completed is not None
        self.assertEqual(completed.state, "completed")

    def test_directory_without_source_connection_does_not_prevent_sleep(self) -> None:
        self.provider["env"] += "\nDATA_WORKFLOW_SSH_DIR=/var/lib/unused-reader"
        self.run_preview()
        self.assertEqual(self.value["gateway"]["mode"], "asleep")

    def test_source_connections_still_prevent_sleep(self) -> None:
        original = self.provider["env"]
        for key in ("ODOO_UPSTREAM_HOST", "DATA_WORKFLOW_SSH_KEY", "DATA_WORKFLOW_SSH_USER"):
            with self.subTest(key=key):
                self.provider["env"] = original + f"\n{key}=fixture"
                self.run_preview()
                self.assertFalse(self.effects)

    def test_inventory_observes_sleep_and_reports_coverage_gaps_without_effects(self) -> None:
        from control_plane.preview_sleep_evidence import collect_preview_sleep_observations

        self.enterContext(
            patch("control_plane.preview_sleep_evidence._read_remote", side_effect=self.transport)
        )
        self.enterContext(
            patch(
                "control_plane.preview_sleep_evidence.api.dokploy_request",
                side_effect=lambda **_: deepcopy(self.provider),
            )
        )

        def read() -> Any:
            return collect_preview_sleep_observations(
                store=self.store,
                profile=self.profile,
                host="fixture",
                token="fixture",
                inventory=(("env", "compose-fixture", self.identity.instance),),
                prefix="fixture",
            )[0]

        self.assertEqual(read().state, "awake")
        self.store.write_preview_record(
            self.preview.model_copy(
                update={"preview_id": "ended-preview", "state": "teardown_pending"}
            )
        )
        self.assertEqual(read().state, "awake")
        with patch.object(
            self.store, "list_held_provider_target_reservations", return_value=(Mock(),)
        ):
            self.assertEqual(read().state, "cannot_sleep")
        with patch(
            "control_plane.preview_sleep_evidence._read_remote",
            side_effect=RuntimeError("fixture secret"),
        ):
            unavailable = read()
        self.assertEqual(unavailable.state, "unknown")
        self.assertNotIn("fixture secret", unavailable.reason)
        self.value = observation(mode="asleep", running=False)
        self.assertEqual(read().state, "asleep")
        self.provider["env"] += "\nODOO_UPSTREAM_HOST=private-source"
        blocked = read()
        self.assertEqual(blocked.state, "cannot_sleep")
        self.assertTrue(blocked.reason)
        self.assertNotIn("private-source", blocked.reason)
        self.provider["composeFile"] = "services: {web: {}}"
        self.assertEqual(read().state, "cannot_sleep")
        self.provider["composeFile"] = "services: {preview-router: {}}"
        self.store.write_preview_record(self.preview.model_copy(update={"state": "destroyed"}))
        self.assertEqual(read().state, "cannot_sleep")
        self.assertFalse(self.effects)
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())

    def test_inventory_polls_without_host_lock_and_stops_at_its_read_budget(self) -> None:
        from control_plane.preview_sleep_evidence import (
            collect_preview_sleep_observations,
            PREVIEW_SLEEP_READ_BUDGET_SECONDS,
        )
        from control_plane.preview_lifecycle_cleanup_routes import PreviewLifecycleSweepCancelled

        clock = [0.0]
        cancelled = [False]

        def native_poll(**kwargs: Any) -> None:
            namespace: dict[str, Any] = {}
            tree = ast.parse(kwargs["program"])
            entry = tree.body[-2:]
            tree.body = tree.body[:-2]
            exec(compile(tree, "<sleep-poll>", "exec"), namespace)
            namespace["runtime"] = lambda: ({}, "router")
            namespace["control"] = lambda *_: self.value["gateway"]
            namespace["application_state"] = lambda *_: self.value["services"]
            encoded = base64.b64encode(json.dumps(kwargs["payload"]).encode()).decode()
            output = io.StringIO()
            with (
                patch("sys.argv", ["poll", encoded, "frame:"]),
                patch("fcntl.flock", side_effect=AssertionError("deploy owns host lock")),
                redirect_stdout(output),
            ):
                with self.assertRaises(SystemExit):
                    exec(
                        compile(
                            ast.Module(body=entry, type_ignores=[]), "<sleep-poll-entry>", "exec"
                        ),
                        namespace,
                    )
            kwargs["receive"](json.loads(output.getvalue().removeprefix("frame:")))
            clock[0] = PREVIEW_SLEEP_READ_BUDGET_SECONDS + 1

        with (
            patch("control_plane.preview_sleep_evidence.monotonic", side_effect=lambda: clock[0]),
            patch(
                "control_plane.preview_sleep_evidence._read_remote", side_effect=native_poll
            ) as remote,
            patch(
                "control_plane.preview_sleep_evidence.api.dokploy_request",
                return_value=self.provider,
            ) as provider,
            patch.object(
                self.store, "list_preview_records", wraps=self.store.list_preview_records
            ) as records,
        ):
            arguments: dict[str, Any] = dict(
                store=self.store,
                profile=self.profile,
                host="fixture",
                token="fixture",
                inventory=(
                    ("env", "compose-fixture", self.identity.instance),
                    ("env", "second", "fixture-pr-2"),
                ),
                prefix="fixture",
            )
            states = collect_preview_sleep_observations(**arguments)
            self.assertEqual([item.state for item in states], ["awake", "unknown"])
            self.assertEqual(remote.call_count, 1)
            self.assertEqual(provider.call_count, 1)
            records.assert_called_once()
            cancelled[0] = True
            with self.assertRaises(PreviewLifecycleSweepCancelled):
                collect_preview_sleep_observations(**arguments, should_stop=lambda: cancelled[0])
        self.assertFalse(self.effects)

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

    def test_inventory_budget_covers_slower_polls_and_rotates_overflow_after_restart(self) -> None:
        from control_plane.preview_lifecycle_scheduler import PreviewInventoryScheduler
        from control_plane.preview_sleep_evidence import PREVIEW_SLEEP_READ_BUDGET_SECONDS
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable

        inventory = tuple(
            ("env", f"compose-{number}", f"fixture-pr-{number}") for number in range(1, 4)
        )
        for number in range(2, 4):
            self.store.write_preview_record(
                self.preview.model_copy(
                    update={"preview_id": f"preview-{number}", "anchor_pr_number": number}
                )
            )
        clock = [0.0]
        poll_seconds = 6.0
        cost = [poll_seconds]
        failed = [False]
        attempted: list[str] = []

        def binding(
            _store: Any, _profile: Any, preview_id: str, *_args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            return {
                "server_id": "shared-host",
                "spec": {"preview_id": preview_id},
                "context": self.profile.preview.context,
                "compose_name": kwargs["payload"]["name"],
            }

        def provider(**kwargs: Any) -> dict[str, Any]:
            number = kwargs["query"]["composeId"].removeprefix("compose-")
            return {"name": f"fixture-pr-{number}", "composeFile": "services: {preview-router: {}}"}

        def remote(**kwargs: Any) -> None:
            preview_id = kwargs["payload"]["preview_id"]
            attempted.append(preview_id)
            clock[0] += cost[0]
            if (
                failed[0]
                and preview_id == self.preview.preview_id
                or kwargs["timeout_seconds"] < min(cost[0], poll_seconds)
            ):
                raise NativeSourceUnavailable("fixture read failure")
            kwargs["receive"]({"type": "complete", "result": observation()})

        with (
            patch(
                "control_plane.preview_inventory_collection._complete_preview_compose_inventory",
                return_value=inventory,
            ),
            patch("control_plane.preview_sleep_evidence.monotonic", side_effect=lambda: clock[0]),
            patch(
                "control_plane.preview_sleep_evidence.require_preview_idle_binding",
                side_effect=binding,
            ),
            patch("control_plane.preview_sleep_evidence.api.dokploy_request", side_effect=provider),
            patch("control_plane.preview_sleep_evidence._read_remote", side_effect=remote),
        ):

            def scan(slot: int) -> Any:
                clock[0] = 0
                scheduler = PreviewInventoryScheduler(
                    self.store,
                    control_plane_root=self.root,
                    clock=lambda: slot * PreviewInventoryScheduler.interval,
                )
                scheduler._run_sweep()
                return self.store.list_preview_inventory_scan_records()[0].sleep_observations

            self.assertTrue(all(item.state == "awake" for item in scan(0)))
            failed[0] = True
            states = {item.slug: item.state for item in scan(0)}
            self.assertEqual(states["pr-1"], "unknown")
            self.assertEqual(states["pr-2"], "awake")
            self.assertEqual(states["pr-3"], "awake")
            failed[0] = False
            cost[0] = PREVIEW_SLEEP_READ_BUDGET_SECONDS + 1
            attempted.clear()
            for slot in range(len(inventory)):
                states = scan(slot)
                self.assertEqual(sum(item.state == "awake" for item in states), 1)
            self.assertEqual(set(attempted), {self.preview.preview_id, "preview-2", "preview-3"})
        self.assertFalse(self.effects)

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
            self.profile.model_copy(
                update={"preview": self.profile.preview.model_copy(update={"enabled": False})}
            )
        )
        self.run_preview()
        self.assertFalse(self.effects)

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

    def protective_report(self, **values: Any) -> DisposableLaneSafety:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return DisposableLaneSafety(
            product=self.profile.product,
            context=self.profile.preview.context,
            instance="pr-1",
            observed_by="fixture",
            observer_kind="fixture",
            binding_sha256="a" * 64,
            observed_at=now,
            expires_at=now + timedelta(minutes=5),
            usage_evidence="fixture",
            data_evidence="fixture",
            isolation_evidence="fixture",
            **values,
        )

    def test_expired_protected_use_data_and_shared_resources_still_block_idle_effects(self) -> None:
        for values in (
            {"usage": "in_use"},
            {"data": "only_copy"},
            {"production_resources": "shared"},
            {"production_resources": "separation_ready"},
        ):
            with (
                self.subTest(values=values),
                patch.object(
                    self.store,
                    "read_disposable_lane_safety",
                    return_value=self.protective_report(**values),
                ),
            ):
                with self.assertRaises(DisposableLaneSafetyUnknown):
                    self.run_preview()
        self.assertFalse(self.effects)

    def test_protection_added_at_dispatch_refuses_before_a_checkpoint_or_effect(self) -> None:
        protected = [False]

        @contextmanager
        def admission() -> Iterator[None]:
            protected[0] = True
            yield

        with (
            patch.object(self.store, "serialize_existing_preview_refresh", side_effect=admission),
            patch.object(
                self.store,
                "read_disposable_lane_safety",
                side_effect=lambda *_: (
                    self.protective_report(usage="in_use") if protected[0] else None
                ),
            ),
        ):
            with self.assertRaises(DisposableLaneSafetyUnknown):
                self.run_preview()
        self.assertFalse(self.effects)
        self.assertFalse(self.store.list_held_provider_target_reservations())

    def test_quiet_poll_does_not_take_the_preview_executor(self) -> None:
        self.value["gateway"]["idle_seconds"] = 0
        with patch.object(self.store, "testing_replacement_execution_lock") as lock:
            self.run_preview()
        lock.assert_not_called()
        self.assertFalse(self.effects)

    def test_transient_sleep_refusal_does_not_poison_future_idle_attempts(self) -> None:
        self.refuse_sleep = True
        self.run_preview()
        first = self.effects[-1]["record_id"]
        self.refuse_sleep = False
        self.run_preview()
        self.assertNotEqual(first, self.effects[-1]["record_id"])
        self.assertEqual(self.value["gateway"]["mode"], "asleep")

    def test_mutable_capacity_policy_does_not_change_original_target_custody(self) -> None:
        floor = [100]
        with patch(
            "control_plane.preview_idle.preview_host_memory_floor", side_effect=lambda **_: floor[0]
        ):
            self.interrupt = True
            self.run_preview()
            original_id = self.effects[-1]["record_id"]
            held = self.store.read_idempotency_record_by_id(original_id)
            assert held is not None
            intent = self.store.read_original_mutation_intent(held)
            self.assertNotIn("memory_floor", intent["target"]["spec"])
            self.assertNotIn("disk_reserve", intent["target"]["spec"])
            floor[0] = 200
            self.interrupt = False
            self.run_preview()
        self.assertEqual(self.effects[-1]["record_id"], original_id)

    def interrupted_stop(self) -> LaunchplaneIdempotencyRecord:
        self.interrupt = True
        self.run_preview()
        self.interrupt = False
        record = self.store.read_idempotency_record_by_id(self.effects[-1]["record_id"])
        assert record is not None
        return record

    def test_idle_hold_is_listed_and_original_observation_is_read_only(self) -> None:
        held = self.interrupted_stop()
        self.assertEqual(
            self.store.list_held_odoo_preview_reservations(product=self.profile.product), (held,)
        )
        classified = classify_preview_reservation(held, profile=self.profile, store=self.store)
        self.assertTrue(classified["original_action_available"])
        self.assertEqual(classified["target_pull_request_number"], self.preview.anchor_pr_number)
        effects = len(self.effects)
        result = recover_held_preview(
            store=self.store, reservation=held, profile=self.profile, root=self.root, observe=Mock()
        )
        self.assertEqual(result["settlement"], "reconcile_required")
        self.assertEqual(len(self.effects), effects)

    def test_completed_original_can_settle_after_preview_admission_is_disabled(self) -> None:
        held = self.interrupted_stop()
        for item in self.value["services"].values():
            item.update(running=False, healthy=False)
        self.store.write_product_profile_record(
            self.profile.model_copy(
                update={"preview": self.profile.preview.model_copy(update={"enabled": False})}
            )
        )
        effects = len(self.effects)
        self.assertEqual(
            observe_idle_reservation(self.store, held, self.profile.product, self.root), "adopted"
        )
        self.assertEqual(len(self.effects), effects)

    def test_refresh_can_qualify_idle_supersession_without_replaying_its_effect(self) -> None:
        held = self.interrupted_stop()
        effects = len(self.effects)
        request = Mock()
        request.apply.dry_run_plan.compose_ref = self.provider["composeId"]
        self.assertTrue(
            _original_supersession_is_quiescent(
                reservation=held,
                profile=self.profile,
                request=request,
                root=self.root,
                database_url=self.store.database_url,
                check=Mock(),
                store=self.store,
            )
        )
        self.assertEqual(len(self.effects), effects)

    def supersede_then_abort_refresh(self, held: LaunchplaneIdempotencyRecord) -> None:
        request = Mock()
        request.apply.dry_run_plan.compose_ref = self.provider["composeId"]
        with patch(
            "control_plane.odoo_preview_apply_execution.build_odoo_preview_runtime_identity",
            return_value=self.identity,
        ):
            operation = prepare_odoo_preview_apply_operation(
                record_store=self.store,
                control_plane_root=self.root,
                profile=self.profile,
                apply_request=request,
                issued_plan=Mock(operation="refresh"),
                reservation_scope="fixture-refresh",
                idempotency_key="aborted-refresh",
                request_fingerprint="refresh",
                trace_id="aborted-refresh",
            )
        policy = operation.target_supersession
        adapter = Mock(spec=DurableProviderMutationAdapter)
        adapter.target_key.return_value = held.provider_target_key
        adapter.reconciliation_key.return_value = held.reconciliation_key
        adapter.apply.side_effect = ProviderMutationRejectedError(ValueError("refresh aborted"))
        expired = parse_launchplane_mutation_timestamp(
            held.lease_expires_at, field_name="lease_expires_at"
        ) + timedelta(seconds=ODOO_PREVIEW_SUPERSESSION_GRACE_SECONDS + 1)
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value=expired.isoformat()
        ):
            with self.assertRaisesRegex(ValueError, "refresh aborted"):
                run_durable_provider_operation(
                    store=self.store,
                    scope=operation.reservation_scope,
                    route_path=operation.route_path,
                    idempotency_key=operation.idempotency_key,
                    request_fingerprint=operation.request_fingerprint,
                    lease_owner="refresh",
                    response_trace_id=operation.trace_id,
                    adapter=adapter,
                    target_supersession=policy,
                )
        original = self.store.read_idempotency_record_by_id(held.record_id)
        assert original is not None
        self.assertEqual(original.state, "completed")
        self.assertEqual(original.response_payload, policy.response_payload)
        self.assertFalse(self.store.list_held_provider_target_reservations())

    def test_readonly_adopted_sleep_can_wake_without_apply_response_fields(self) -> None:
        held = self.interrupted_stop()
        for item in self.value["services"].values():
            item.update(running=False, healthy=False)
        result = recover_held_preview(
            store=self.store, reservation=held, profile=self.profile, root=self.root, observe=Mock()
        )
        self.assertEqual(result["settlement"], "adopted")
        record = self.store.read_idempotency_record_by_id(held.record_id)
        assert record is not None
        self.assertNotIn("accepted", record.response_payload)
        self.value["gateway"]["requested"] = True
        self.run_preview()
        self.assertEqual(self.value["gateway"]["mode"], "awake")

    def test_superseded_sleep_can_wake_after_refresh_aborts(self) -> None:
        held = self.interrupted_stop()
        for item in self.value["services"].values():
            item.update(running=False, healthy=False)
        self.supersede_then_abort_refresh(held)
        self.value["gateway"]["requested"] = True
        self.run_preview()
        self.assertEqual(self.value["gateway"]["mode"], "awake")

    def test_superseded_partial_wake_resumes_exact_gateway_receipt_after_refresh_aborts(
        self,
    ) -> None:
        self.run_preview()
        self.value["gateway"]["requested"] = True
        self.interrupt = True
        self.run_preview()
        self.interrupt = False
        original_id = self.effects[-1]["record_id"]
        held = self.store.read_idempotency_record_by_id(original_id)
        assert held is not None
        self.supersede_then_abort_refresh(held)
        self.run_preview()
        self.assertEqual(self.effects[-1]["record_id"], original_id)
        self.assertEqual(self.value["gateway"]["mode"], "awake")

    def test_resumed_gateway_wake_is_adopted_after_its_completion_response_is_lost(self) -> None:
        self.run_preview()
        self.value["gateway"]["requested"] = True
        self.interrupt = True
        self.run_preview()
        self.interrupt = False
        original_id = self.effects[-1]["record_id"]
        held = self.store.read_idempotency_record_by_id(original_id)
        assert held is not None
        self.supersede_then_abort_refresh(held)

        def lose_response(**kwargs: Any) -> None:
            if kwargs["payload"]["action"] == "awake":

                def lose(_frame: object) -> None:
                    raise ProviderMutationUnknownError("fixture completion response lost")

                kwargs["receive"] = lose
            self.transport(**kwargs)

        with patch.object(self.remote, "side_effect", lose_response):
            self.run_preview()
        self.assertEqual(self.value["gateway"]["wake_record_id"], original_id)
        recovery = self.store.list_held_provider_target_reservations()[0]
        self.assertNotEqual(recovery.record_id, original_id)
        effects = len(self.effects)
        self.run_preview()
        completed = self.store.read_idempotency_record_by_id(recovery.record_id)
        assert completed is not None
        self.assertEqual(completed.state, "completed")
        self.assertEqual(len(self.effects), effects)

    def exhaust_partial_effects(self) -> None:
        self.interrupt = True
        with self.assertLogs("control_plane.preview_idle", level="WARNING") as warnings:
            for _ in range(10):
                self.run_preview()
        self.assertEqual(len(warnings.records), 1)
        held = self.store.list_held_provider_target_reservations()[0]
        effects = len(self.effects)
        self.run_preview()
        self.assertEqual(self.store.read_idempotency_record_by_id(held.record_id), held)
        self.assertEqual(len(self.effects), effects)
        self.interrupt = False
        self.supersede_then_abort_refresh(held)

    def test_persistent_partial_sleep_stops_retrying_without_renewing_the_lease(self) -> None:
        self.exhaust_partial_effects()

    def test_persistent_partial_wake_stops_retrying_without_renewing_the_lease(self) -> None:
        self.run_preview()
        self.value["gateway"]["requested"] = True
        self.exhaust_partial_effects()

    def test_unrecognized_wake_cursor_cannot_replace_an_original_receipt(self) -> None:
        self.run_preview()
        self.value["gateway"].update(requested=True, wake_record_id="unknown")
        effects = len(self.effects)
        self.run_preview()
        self.assertEqual(len(self.effects), effects)

    def test_partial_wake_keeps_its_checkpoint_when_retry_admission_refuses(self) -> None:
        self.run_preview()
        self.value["gateway"]["requested"] = True
        self.interrupt = True
        self.run_preview()
        held = self.store.list_held_provider_target_reservations()[0]
        self.interrupt = False
        with patch.object(
            self.store,
            "serialize_existing_preview_refresh",
            side_effect=ValueError("fixture admission changed"),
        ):
            self.run_preview()
        current = self.store.read_idempotency_record_by_id(held.record_id)
        assert current is not None
        self.assertEqual(current.state, "reconcile_required")
        self.assertEqual(current.provider_effect_started_at, held.provider_effect_started_at)

    def test_idle_hold_on_another_preview_does_not_block_legacy_reconciliation(self) -> None:
        self.interrupted_stop()
        other = self.preview.model_copy(
            update={
                "preview_id": "another-preview",
                "anchor_pr_number": 2,
                "canonical_url": "https://another-preview.example.invalid",
            }
        )
        previews = (self.preview, other)
        _require_no_held_odoo_work(self.store, self.profile, "pr-2", previews)
        with self.assertRaises(ValueError):
            _require_no_held_odoo_work(self.store, self.profile, "pr-1", previews)


class PreviewIdleRuntimeTests(unittest.TestCase):
    def test_native_inspection_allows_only_an_unmounted_directory_without_connection(self) -> None:
        spec = {
            "generation": {"instance": "preview", "namespace": "fixture"},
            "compose_project": "fixture",
            "database_name": "fixture_db",
            "preview_volumes": {
                "data": "fixture-data",
                "logs": "fixture-logs",
                "database": "fixture-db",
            },
            "database_data_directory": "/database",
            "image_reference": "fixture-image",
            "usage_only": True,
        }
        containers: dict[str, Any] = {
            name: {
                "Id": name,
                "Config": {
                    "Image": "fixture-image",
                    "Labels": {
                        "com.docker.compose.project": "fixture",
                        "com.docker.compose.service": name,
                    },
                    "Env": [
                        "PLATFORM_INSTANCE=preview",
                        "ODOO_DB_HOST=database",
                        "ODOO_DB_NAME=fixture_db",
                        "ODOO_MAX_CRON_THREADS=0",
                        "DATA_WORKFLOW_SSH_DIR=/unused",
                    ],
                },
                "Mounts": [
                    {"Type": "volume", "Name": volume, "Destination": "/database"}
                    for volume in (
                        ["fixture-db"] if name == "database" else ["fixture-data", "fixture-logs"]
                    )
                ],
                "NetworkSettings": {
                    "Networks": {
                        "fixture-net": {},
                        **({"dokploy-network": {}} if name == "web" else {}),
                    }
                },
            }
            for name in ("web", "script-runner", "database")
        }

        def docker(*arguments: str) -> bytes:
            if arguments[0] == "ps":
                return "\n".join(containers).encode()
            return json.dumps([{"Labels": {"com.docker.compose.project": "fixture"}}]).encode()

        self.namespace["docker"] = docker
        self.namespace["one_inspection"] = lambda name: (
            containers[name]
            if name in containers
            else {
                "Labels": {"com.docker.compose.project": "fixture"},
                "Containers": {name: {} for name in containers},
            }
        )
        self.namespace["inspect_volume_backings"] = Mock()
        inspect_runtime = self.namespace["inspect_copy_runtime"]
        self.assertEqual(set(inspect_runtime(spec, expected_instance="preview")), set(containers))
        containers["web"]["Config"]["Env"].append("ODOO_UPSTREAM_HOST=fixture-source")
        with self.assertRaises(self.namespace["CopyReadbackRefused"]):
            inspect_runtime(spec, expected_instance="preview")
        containers["web"]["Config"]["Env"].pop()
        containers["web"]["Mounts"].append(
            {"Type": "bind", "Source": "/unused", "Destination": "/root/.ssh"}
        )
        with self.assertRaises(self.namespace["CopyReadbackRefused"]):
            inspect_runtime(spec, expected_instance="preview")

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
            "health_path": DEFAULT_ODOO_RUNTIME_HEALTH_PATH,
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
            for name in arguments[1:] if arguments[0] == "start" else [arguments[-1]]:
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
            self.value["gateway"]["mode"] = (
                "asleep" if request["action"] in {"sleep", "waking"} else "awake"
            )
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
            [name for call in self.calls if call[0] == "start" for name in call[1:]],
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

    def test_admitted_partial_wake_does_not_charge_its_memory_twice(self) -> None:
        self.wake()
        self.value["gateway"]["wake_record_id"] = "wake-fixture"
        self.states["database"] = {"running": True, "healthy": True}
        self.namespace["observe_memory_headroom"] = Mock(
            side_effect=AssertionError("Do not re-admit this original wake")
        )
        self.namespace["execute"]()
        self.assertEqual(self.calls[-1], ("control", "awake"))

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
