"""Retirement settles only an idle held execute and never invents recovery copies."""

import unittest
from typing import Any, cast
from unittest.mock import patch

from control_plane.odoo_upgrade_rehearsal import OdooRehearsalAdapter, RehearsalRefused
from control_plane.testing_local_credentials import prepare_rehearsal_local_credentials
from control_plane.contracts.odoo_upgrade_rehearsal import ODOO_UPGRADE_REHEARSAL_ROUTE
from tests import test_odoo_upgrade_rehearsal as rehearsal


def native_fixture(record: Any, key: str, digest: str, disposed: bool = False) -> dict[str, object]:
    from control_plane.contracts.odoo_upgrade_rehearsal import IncompleteRehearsalHostResult
    from control_plane.contracts.canonical_json import canonical_json_sha256

    return IncompleteRehearsalHostResult(
        qualified=True,
        disposed=disposed,
        binding_sha256=digest,
        intent_sha256=record.intent_sha256,
        attempt_sha256=canonical_json_sha256({"operation_key": key}),
        containers_absent=disposed,
        volumes_absent=disposed,
        network_absent=disposed,
        raw_source_absent=disposed,
        public_route=False,
        business_host_frozen=False,
    ).model_dump(mode="json")


class IncompleteRehearsalRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = rehearsal.RehearsalScenario()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.prepared = self.fixture.record.model_copy(
            update={
                "state": "prepared",
                "compose_id": "own-compose",
                "project_id": "own-project",
                "environment_id": "own-environment",
            }
        )
        self.store.compare_and_write_odoo_upgrade_rehearsal(self.fixture.record, self.prepared)
        prepare_rehearsal_local_credentials(
            self.store, self.fixture.identifier, actor=self.fixture.principal
        )
        self.execute = self.fixture.adapter("execute")
        self.key = "original-held-execute"
        with patch.object(OdooRehearsalAdapter, "host", side_effect=TimeoutError("held")):
            with self.assertRaises(TimeoutError):
                self.fixture.run_operation(self.execute, self.key)

    def retirement(self, key: str = "") -> Any:
        from control_plane.odoo_incomplete_rehearsal_retirement import IncompleteRehearsalRetirement

        return IncompleteRehearsalRetirement(
            self.fixture.adapter("retire-incomplete"), key or self.key
        )

    def run_retirement(self, operation: Any) -> Any:
        from control_plane.provider_operations import run_durable_provider_operation
        from control_plane.http_routes.mutation_support import request_fingerprint

        return run_durable_provider_operation(
            store=self.store,
            scope=self.fixture.principal + "|incomplete-retirement",
            route_path=ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=self.fixture.product),
            idempotency_key=operation.original_key,
            request_fingerprint=request_fingerprint(
                {
                    "product": self.fixture.product,
                    **operation.adapter.request.model_dump(mode="json"),
                }
            ),
            lease_owner="retirement-worker",
            response_trace_id="retirement-trace",
            adapter=operation,
        )

    def test_held_incomplete_operation_retires_and_replays_with_readback(self) -> None:
        from control_plane.storage.postgres import LaunchplaneOdooUpgradeRehearsalRow

        with self.store._session_factory() as session:
            row = session.get(LaunchplaneOdooUpgradeRehearsalRow, self.prepared.rehearsal_id)
            assert row is not None
            row.payload = {
                key: value for key, value in row.payload.items() if key != "incomplete_retirement"
            }
            session.commit()
        operation = self.retirement()
        native = native_fixture(self.prepared, self.key, "a" * 64)
        with (
            patch.object(
                type(operation),
                "host",
                side_effect=lambda mode, check: {
                    **native,
                    **{
                        key: mode != "dry-run"
                        for key in (
                            "disposed",
                            "containers_absent",
                            "volumes_absent",
                            "network_absent",
                            "raw_source_absent",
                        )
                    },
                },
            ) as host,
            patch.object(type(operation), "placement", return_value=True),
        ):
            plan = operation.plan()
            operation.adapter.request = operation.adapter.request.model_copy(
                update={"reviewed_plan_sha256": plan["plan_sha256"]}
            )
            outcome = self.run_retirement(operation)
            self.assertEqual(outcome.status, "completed")
            result = outcome.response_payload["result"]
            self.assertTrue(result["disposed"])
            self.assertEqual(result["state"], "retired")
            self.assertEqual(result["copies"], [])
            self.assertTrue(result["original_operation_released"])
            count = host.call_count
            self.assertEqual(operation.readback()["state"], "retired")
            replay = self.run_retirement(operation)
            self.assertEqual(replay.status, "replayed")
            self.assertEqual(replay.response_payload["result"], result)
            with self.assertRaises(RehearsalRefused):
                self.run_retirement(self.retirement("different-key-after-retirement"))
            self.assertEqual(host.call_count, count)
        original = self.store.read_idempotency_record(
            scope=self.fixture.principal,
            route_path=ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=self.fixture.product),
            idempotency_key=self.key,
        )
        assert original is not None
        self.assertEqual(original.state, "completed")
        self.assertFalse(self.store.list_held_provider_target_reservations())

    def test_failed_publication_keeps_original_hold_record_and_target_registration(self) -> None:
        operation = self.retirement()
        native = native_fixture(self.prepared, self.key, "b" * 64)
        owned = self.store.read_provider_target_record(
            context_name=self.fixture.identifier, instance_name="rehearsal"
        )
        with (
            patch.object(
                type(operation),
                "host",
                side_effect=lambda mode, check: {
                    **native,
                    **{
                        key: mode != "dry-run"
                        for key in (
                            "disposed",
                            "containers_absent",
                            "volumes_absent",
                            "network_absent",
                            "raw_source_absent",
                        )
                    },
                },
            ),
            patch.object(type(operation), "placement", return_value=True),
        ):
            operation.adapter.request = operation.adapter.request.model_copy(
                update={"reviewed_plan_sha256": operation.plan()["plan_sha256"]}
            )
            with (
                patch.object(
                    self.store,
                    "_sync_idempotency_row",
                    side_effect=RuntimeError("publication failed"),
                ),
                self.assertRaises(RuntimeError),
            ):
                operation.run(
                    type(
                        "Lease",
                        (),
                        {
                            "assert_current": lambda self: None,
                            "checkpoint_effect": lambda self, phase: None,
                        },
                    )()
                )
        current = self.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
        assert current is not None
        self.assertEqual(current.state, self.prepared.state)
        self.assertEqual(current.binding_sha256, self.prepared.binding_sha256)
        self.assertIsNotNone(current.incomplete_retirement)
        self.assertEqual(
            self.store.read_provider_target_record(
                context_name=self.fixture.identifier, instance_name="rehearsal"
            ),
            owned,
        )
        self.assertEqual(operation.original(self.prepared).state, "reconcile_required")

    def test_same_key_recovers_each_interrupted_retirement_phase(self) -> None:
        for phase in (
            "precheck",
            "partial_host",
            "lost_host_reply",
            "lost_compose_reply",
            "publication",
        ):
            with self.subTest(phase=phase):
                scenario = IncompleteRehearsalRetirementTests()
                scenario.setUp()
                self.addCleanup(scenario.doCleanups)
                operation = scenario.retirement()
                flags = {
                    "failed": False,
                    "disposed": False,
                    "compose_absent": False,
                    "project_absent": False,
                    "planned": False,
                }

                def host(mode: str, check: Any) -> dict[str, object]:
                    if flags["compose_absent"]:
                        raise AssertionError("Host transport cannot use a removed compose")
                    if mode == "dry-run":
                        if flags["planned"] and phase == "precheck" and not flags["failed"]:
                            flags["failed"] = True
                            raise RuntimeError("Transient precheck failure")
                        return native_fixture(
                            scenario.prepared, scenario.key, "e" * 64, flags["disposed"]
                        )
                    if phase in {"partial_host", "lost_host_reply"} and not flags["failed"]:
                        flags["failed"] = True
                        flags["disposed"] = phase == "lost_host_reply"
                        raise RuntimeError("Host response lost after native progress")
                    flags["disposed"] = True
                    return native_fixture(scenario.prepared, scenario.key, "e" * 64, True)

                def placement(apply: bool = False, check: Any = None) -> bool:
                    if apply:
                        flags["compose_absent"] = True
                        if phase == "lost_compose_reply" and not flags["failed"]:
                            flags["failed"] = True
                            raise RuntimeError("Compose removal acknowledged by provider only")
                        flags["project_absent"] = True
                    return flags["project_absent"]

                publish = scenario.store.complete_incomplete_rehearsal_retirement

                def publication(**kwargs: Any) -> None:
                    if phase == "publication" and not flags["failed"]:
                        flags["failed"] = True
                        raise RuntimeError("Transient publication failure")
                    publish(**kwargs)

                with (
                    patch.object(type(operation), "host", side_effect=host),
                    patch.object(type(operation), "placement", side_effect=placement),
                    patch.object(
                        scenario.store,
                        "complete_incomplete_rehearsal_retirement",
                        side_effect=publication,
                    ),
                ):
                    plan = operation.plan()
                    flags["planned"] = True
                    operation.adapter.request = operation.adapter.request.model_copy(
                        update={"reviewed_plan_sha256": plan["plan_sha256"]}
                    )
                    with self.assertRaises(RuntimeError):
                        scenario.run_retirement(operation)
                    self.assertEqual(
                        operation.original(operation.record()).state, "reconcile_required"
                    )
                    resumed = scenario.run_retirement(operation)
                    self.assertIn(resumed.status, {"completed", "adopted"})
                    self.assertTrue(resumed.response_payload["result"]["disposed"])
                    self.assertFalse(scenario.store.list_held_provider_target_reservations())

    def test_verified_or_running_operation_is_refused_before_host_effects(self) -> None:
        operation = self.retirement()
        with patch.object(type(operation), "host") as host:
            self.assertEqual(
                operation.observe(
                    "fixture-operation",
                    "dispose_incomplete_rehearsal",
                    operation.reconciliation_key(),
                ).outcome,
                "unknown",
            )
            verified = self.prepared.model_copy(update={"state": "verified"})
            self.store.compare_and_write_odoo_upgrade_rehearsal(self.prepared, verified)
            with self.assertRaises(RehearsalRefused):
                operation.plan()
            self.store.compare_and_write_odoo_upgrade_rehearsal(verified, self.prepared)
            original = operation.original(self.prepared)
            self.store.retry_reconciled_mutation(reservation=original, lease_owner="still-running")
            with self.assertRaises(RehearsalRefused):
                operation.plan()
            host.assert_not_called()

    def test_placement_retirement_refuses_public_or_populated_projects_and_reads_absence(
        self,
    ) -> None:
        import copy
        from control_plane.dokploy.sandbox_placement import REHEARSAL_PROJECT_DESCRIPTION

        operation = self.retirement()
        environment = {
            "name": self.prepared.rehearsal_id,
            "environmentId": self.prepared.environment_id,
            **{
                key: []
                for key in (
                    "applications",
                    "postgres",
                    "mysql",
                    "mariadb",
                    "mongo",
                    "redis",
                    "libsql",
                )
            },
            "compose": [
                {"name": self.prepared.rehearsal_id, "composeId": self.prepared.compose_id}
            ],
        }
        project = {
            "name": self.prepared.rehearsal_id,
            "projectId": self.prepared.project_id,
            "description": REHEARSAL_PROJECT_DESCRIPTION,
            "environments": [environment],
        }
        protected = {"name": "production", "projectId": "production-project", "environments": []}
        projects = [project, protected]
        untouched = copy.deepcopy(protected)
        payload = {
            "serverId": self.prepared.server_id,
            "environmentId": self.prepared.environment_id,
            "appName": self.prepared.rehearsal_id,
            "env": "",
            "composeFile": "",
        }
        deletes: list[dict[str, Any]] = []

        def provider(**kwargs: Any) -> dict[str, object]:
            deletes.append(kwargs["payload"])
            if kwargs["path"] == "/api/compose.delete":
                self.assertEqual(kwargs["payload"]["composeId"], self.prepared.compose_id)
                self.assertFalse(kwargs["payload"]["deleteVolumes"])
                environment["compose"] = []
            elif kwargs["path"] == "/api/project.remove":
                self.assertEqual(kwargs["payload"]["projectId"], self.prepared.project_id)
                projects.remove(project)
            else:
                raise AssertionError("Unexpected provider effect")
            return {}

        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_projects",
                side_effect=lambda **kwargs: tuple(projects),
            ),
            patch("control_plane.dokploy.api.fetch_dokploy_target_payload", return_value=payload),
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_compose_domains_for_target_setup",
                return_value=[],
            ) as domains,
            patch("control_plane.dokploy.api.dokploy_request", side_effect=provider),
        ):
            self.assertFalse(operation.placement())
            domains.return_value = [{"domainId": "protected-public-route"}]
            with self.assertRaises(RehearsalRefused):
                operation.placement(True)
            domains.return_value = []
            environment["libsql"] = [{"libsqlId": "protected-store"}]
            with self.assertRaises((ValueError, RehearsalRefused)):
                operation.placement(True)
            self.assertFalse(deletes)
            environment["libsql"] = []
            self.assertTrue(operation.placement(True))
            self.assertTrue(operation.placement())
            self.assertEqual(projects, [untouched])
        self.assertEqual(operation.original(self.prepared).state, "reconcile_required")

    def test_production_alias_and_another_key_are_refused(self) -> None:
        operation = self.retirement()
        from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord

        prod = next(lane for lane in self.fixture.profile.lanes if lane.instance == "prod")
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=prod.context,
                instance="prod",
                target_id=self.prepared.compose_id,
                updated_at=self.prepared.updated_at.isoformat(),
            )
        )
        with patch.object(type(operation), "host") as host:
            with self.assertRaises(RehearsalRefused):
                operation.plan()
            host.assert_not_called()
        with self.assertRaises(RehearsalRefused):
            self.retirement("replacement-key").plan()


class IncompleteRetirementHostTests(unittest.TestCase):
    def setUp(self) -> None:
        import hashlib
        from pathlib import Path

        self.fixture = rehearsal.RehearsalHostSafetyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.spec, self.root, _ = self.fixture.storage_fixture(
            {"database_bytes": 1, "filestore_bytes": 1, "staging_bytes": 1, "reserve_bytes": 1}
        )
        self.spec.update(
            action="retire-incomplete",
            mode="dry-run",
            resources_binding_sha256=self.fixture.spec["generation"]["resources_binding_sha256"],
            database_data_directory=self.fixture.spec["database_data_directory"],
        )
        self.spec["record"].update(state="prepared", source_odoo_version="fixture")
        self.spec["source_image"] = self.spec["target_image"]
        (self.root / ".execution.lock").touch()
        (self.root / "execute.started").touch()
        raw = self.root / "raw"
        raw.mkdir()
        (raw / "database.dump").write_bytes(b"disposable raw capture")
        (raw / "filestore.tar").write_bytes(b"disposable raw files")
        self.outside = self.fixture.fixture.root / "protected-production"
        self.outside.mkdir()
        (self.outside / "keep").write_bytes(b"production fixture")
        self.fixture.fixture.containers["web"]["State"]["Running"] = False
        self.calls: list[tuple[str, ...]] = []
        self.network = True
        self.original_docker = self.fixture.host["docker"]
        self.network_id = hashlib.sha256(b"isolated fixture network").hexdigest()
        self.fixture.host["docker"] = self.docker
        self.fixture.host["one_inspection"] = lambda name: __import__("json").loads(
            self.docker("inspect", name)
        )[0]
        # The fixture uses actual local backing directories with unique inodes.
        self.backings = {
            name: Path(volume["Mountpoint"])
            for name, volume in self.fixture.fixture.volumes.items()
        }

    def docker(self, *args: str, stdin: bytes | None = None) -> bytes:
        import json

        self.calls.append(args)
        containers = self.fixture.fixture.containers
        if args[:2] == ("inspect", "testing-data"):
            return json.dumps(
                [
                    {
                        "Id": self.network_id,
                        "Internal": True,
                        "Containers": {item["Id"]: {} for item in containers.values()},
                    }
                ]
            ).encode()
        if args[:2] == ("volume", "ls"):
            return "\n".join(self.fixture.fixture.volumes).encode()
        if args[:2] == ("network", "ls"):
            return b"testing-data" if self.network else b""
        if args[0] == "stop":
            for identifier in args[1:]:
                next(item for item in containers.values() if item["Id"] == identifier)["State"][
                    "Running"
                ] = False
            return b""
        if args[0] == "rm":
            for identifier in args[1:]:
                name = next(name for name, item in containers.items() if item["Id"] == identifier)
                del containers[name]
            return b""
        if args[:2] == ("volume", "rm"):
            del self.fixture.fixture.volumes[args[2]]
            return b""
        if args[:2] == ("network", "rm"):
            self.assertEqual(args[2], self.network_id)
            self.network = False
            return b""
        return cast(bytes, self.original_docker(*args, stdin=stdin))

    def test_host_dry_run_apply_and_replay_dispose_only_its_own_custody(self) -> None:
        plan = self.fixture.run_host(self.spec)
        self.assertFalse(plan["disposed"])
        self.assertTrue((self.root / "raw/database.dump").exists())
        self.assertFalse(
            any(
                call[0] in {"stop", "rm"} or call[:2] in {("volume", "rm"), ("network", "rm")}
                for call in self.calls
            )
        )
        self.spec["mode"] = "apply"
        applied = self.fixture.run_host(self.spec)
        self.assertTrue(applied["disposed"])
        self.assertEqual(applied["binding_sha256"], plan["binding_sha256"])
        self.assertFalse(self.fixture.fixture.containers)
        self.assertFalse(self.fixture.fixture.volumes)
        self.assertFalse((self.root / "raw").exists())
        self.assertEqual((self.outside / "keep").read_bytes(), b"production fixture")
        self.spec["mode"] = "apply"
        self.assertTrue(self.fixture.run_host(self.spec)["disposed"])

    def test_host_resumes_its_manifest_after_a_lost_volume_removal_reply(self) -> None:
        plan = self.fixture.run_host(self.spec)
        self.spec["mode"] = "apply"
        docker = self.fixture.host["docker"]
        failed = False

        def lose_reply(*args: str, stdin: bytes | None = None) -> bytes:
            nonlocal failed
            result = cast(bytes, docker(*args, stdin=stdin))
            if args[:2] == ("volume", "rm") and not failed:
                failed = True
                raise RuntimeError("Volume removed; reply lost")
            return result

        self.fixture.host["docker"] = lose_reply
        with self.assertRaises(RuntimeError):
            self.fixture.run_host(self.spec)
        self.assertTrue((self.root / "raw/database.dump").exists())
        self.spec["mode"] = "dry-run"
        resumed_plan = self.fixture.run_host(self.spec)
        self.assertEqual(resumed_plan["binding_sha256"], plan["binding_sha256"])
        self.spec["mode"] = "apply"
        self.assertTrue(self.fixture.run_host(self.spec)["disposed"])
        self.assertEqual((self.outside / "keep").read_bytes(), b"production fixture")

    def test_host_refuses_running_execute_and_shared_storage_before_disposal(self) -> None:
        import fcntl
        import copy

        with (self.root / ".execution.lock").open("r") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.spec["mode"] = "apply"
            with self.assertRaises(BlockingIOError):
                self.fixture.run_host(self.spec)
        other = copy.deepcopy(next(iter(self.fixture.fixture.containers.values())))
        other["Id"] = "protected-production-container"
        other["Config"]["Labels"]["com.docker.compose.project"] = "production"
        self.fixture.fixture.containers[other["Id"]] = other
        with self.assertRaises(RuntimeError):
            self.fixture.run_host(self.spec)
        self.assertTrue((self.root / "raw/database.dump").exists())
        self.assertFalse(any(call[0] in {"stop", "rm"} for call in self.calls))


class IncompleteRetirementHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_director_route_plans_applies_replays_and_reads_original_retirement(
        self,
    ) -> None:
        from unittest.mock import PropertyMock
        from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
        from control_plane.service_auth import (
            configured_local_operator_identity,
            LaunchplaneAuthzPolicy,
        )
        from control_plane.http_routes.mutation_support import idempotency_scope
        from control_plane.storage.postgres import PostgresRecordStore
        from control_plane.odoo_incomplete_rehearsal_retirement import IncompleteRehearsalRetirement
        from tests.http_app_test_support import _local_operator_bearer_config
        from tests.support.http import request

        fixture = rehearsal.RehearsalScenario()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        identity = configured_local_operator_identity(
            _local_operator_bearer_config(token_label="local-owner-write")
        )
        assert identity is not None
        fixture.principal = idempotency_scope(identity)
        prepared = fixture.record.model_copy(
            update={
                "principal": fixture.principal,
                "state": "prepared",
                "compose_id": "own-compose",
                "project_id": "own-project",
                "environment_id": "own-environment",
            }
        )
        fixture.store.compare_and_write_odoo_upgrade_rehearsal(fixture.record, prepared)
        prepare_rehearsal_local_credentials(
            fixture.store, fixture.identifier, actor=fixture.principal
        )
        fixture.store.seed_authz_policy_if_absent(
            LaunchplaneAuthzPolicyRecord(
                record_id="retirement-policy",
                source="test",
                updated_at=prepared.updated_at.isoformat(),
                policy=LaunchplaneAuthzPolicy(schema_version=2),
            )
        )
        with patch.object(OdooRehearsalAdapter, "host", side_effect=TimeoutError("held execute")):
            with self.assertRaises(TimeoutError):
                fixture.run_operation(fixture.adapter("execute"), "original-execute")
        before = fixture.authority()
        native = native_fixture(prepared, "original-execute", "c" * 64)
        headers = {
            "Authorization": "Bearer local-operator-token",
            "Idempotency-Key": "original-execute",
        }
        payload = fixture.request.model_copy(update={"action": "retire-incomplete"}).model_dump(
            mode="json"
        )
        route = ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=fixture.product)
        with (
            patch.object(
                IncompleteRehearsalRetirement,
                "host",
                side_effect=lambda mode, check: {
                    **native,
                    **{
                        key: mode != "dry-run"
                        for key in (
                            "disposed",
                            "containers_absent",
                            "volumes_absent",
                            "network_absent",
                            "raw_source_absent",
                        )
                    },
                },
            ) as host,
            patch.object(IncompleteRehearsalRetirement, "placement", return_value=True),
        ):
            plan = await request(fixture.app, "POST", route, payload=payload, headers=headers)
            self.assertEqual(plan.status_code, 202, plan.text)
            payload.update(mode="apply", reviewed_plan_sha256=plan.json()["result"]["plan_sha256"])
            from control_plane.provider_operations import run_durable_provider_operation
            from control_plane.http_routes.mutation_support import request_fingerprint

            operation = IncompleteRehearsalRetirement(
                fixture.adapter("retire-incomplete"), "original-execute"
            )
            operation.adapter.request = operation.adapter.request.model_copy(
                update={"reviewed_plan_sha256": payload["reviewed_plan_sha256"]}
            )

            def run() -> Any:
                return run_durable_provider_operation(
                    store=fixture.store,
                    scope=fixture.principal + "|incomplete-retirement",
                    route_path=route,
                    idempotency_key="original-execute",
                    request_fingerprint=request_fingerprint(
                        {"product": fixture.product, **payload}
                    ),
                    lease_owner="retirement-http-worker",
                    response_trace_id="retirement-http-trace",
                    adapter=operation,
                )

            outcome, replay_outcome = run(), run()
            # The real local journal/adapter ran above. Bypass only HTTP's
            # PostgreSQL admission while mapping those completed outcomes.
            with (
                patch.object(
                    PostgresRecordStore,
                    "database_dialect_name",
                    new_callable=PropertyMock,
                    return_value="postgresql",
                ),
                patch(
                    "control_plane.http_app.run_durable_provider_operation",
                    side_effect=[outcome, replay_outcome],
                ) as route_runner,
            ):
                applied = await request(
                    fixture.app, "POST", route, payload=payload, headers=headers
                )
                self.assertEqual(applied.status_code, 202, applied.text)
                self.assertTrue(applied.json()["result"]["original_operation_released"])
                replay = await request(fixture.app, "POST", route, payload=payload, headers=headers)
                self.assertEqual(replay.status_code, 202, replay.text)
                self.assertTrue(replay.json()["replayed"])
                routed = route_runner.call_args.kwargs
                self.assertEqual(routed["scope"], fixture.principal + "|incomplete-retirement")
                self.assertEqual(routed["idempotency_key"], headers["Idempotency-Key"])
                self.assertNotEqual(routed["adapter"].target_key(), fixture.identifier)
            calls = host.call_count
            payload.update(mode="readback", reviewed_plan_sha256="")
            readback = await request(fixture.app, "POST", route, payload=payload, headers=headers)
            self.assertEqual(readback.status_code, 202, readback.text)
            self.assertEqual(readback.json()["result"]["state"], "retired")
            self.assertEqual(readback.json()["result"]["copies"], [])
            self.assertEqual(host.call_count, calls)
        self.assertEqual(fixture.authority(), before)
        self.assertFalse(fixture.store.list_held_provider_target_reservations())
