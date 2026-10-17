"""An independent rehearsal preserves lane authority, recovery copies and unknown effects."""

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
from typing import Any, cast
from collections.abc import Callable
import unittest
from unittest.mock import Mock, patch

from control_plane import secrets
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.odoo_upgrade_rehearsal import (
    OdooUpgradeRehearsalRequest,
    RehearsalCopyReceipt,
    RehearsalPrerequisite,
    rehearsal_identity,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.odoo_upgrade_rehearsal import (
    OdooRehearsalAdapter,
    RehearsalRefused,
    rehearsal_credentials,
    rehearsal_host_spec,
    rehearsal_inputs,
    rehearsal_resource,
    require_unaliased_rehearsal,
)
from control_plane.provider_operations import (
    run_durable_provider_operation,
    DurableProviderOperationResult,
)
from control_plane.testing_local_credentials import prepare_rehearsal_local_credentials
from tests import test_testing_replacement_resources as resources
from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget
from control_plane.contracts.odoo_upgrade_rehearsal import OdooUpgradeRehearsalRecord


class RehearsalScenario(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        fixture = resources.TestingReplacementResourceHttpTests()
        fixture.database_url = self.database_url
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.store = fixture.store
        self.root = fixture.root
        self.product = fixture.profile.product
        self.profile = fixture.profile
        self.context = fixture.context
        self.principal = "rehearsal-fixture-agent"
        self.identifier = rehearsal_identity(self.product, self.principal, "fixture-original-key")
        self.enterContext(
            patch.dict(
                os.environ, {secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "rehearsal-fixture-key"}
            )
        )
        source: dict[str, str | int | float | bool] = {
            "ODOO_UPSTREAM_HOST": "source.invalid",
            "ODOO_UPSTREAM_USER": "reader",
            "ODOO_UPSTREAM_DB_USER": "postgres",
            "ODOO_UPSTREAM_DB_NAME": "source_db",
            "ODOO_UPSTREAM_FILESTORE_PATH": "/source/filestore/source_db",
            "OPENUPGRADE_ENABLED": "true",
            "OPENUPGRADE_TARGET_VERSION": "next",
            "OPENUPGRADE_SCRIPTS_PATH": "/artifact/migrations",
            "OPENUPGRADE_SKIP_UPDATE_ADDONS": "false",
        }
        prod = next(lane for lane in self.profile.lanes if lane.instance == "prod")
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context=prod.context,
                instance="prod",
                env={
                    "ODOO_WORKERS": "4",
                    "POSTGRES_PASSWORD": "private-source-key",
                    "ODOO_SMTP_PASSWORD": "private-mail-key",
                },
                updated_at=self.profile.updated_at,
            )
        )
        self.manifests = []
        for sequence in range(1, 3):
            manifest = ArtifactIdentityManifest.model_validate(
                {
                    "artifact_id": f"fixture-artifact-{sequence}",
                    "source_commit": str(sequence) * 40,
                    "enterprise_base_digest": "sha256:" + str(sequence) * 64,
                    "odoo_install_modules": ["base", "web", "launchplane_runtime_health"],
                    "build_flags": {
                        "values": {
                            "odoo_version": str(sequence) + ".0",
                            "sql_copy_source_versions": "1.0",
                        }
                    },
                    "image": {
                        "repository": "registry.invalid/fixture",
                        "digest": "sha256:" + str(sequence) * 64,
                    },
                    "source_build": {
                        "repository": self.profile.repository,
                        "repository_id": "fixture-repository",
                        "workflow_path": ".github/workflows/build.yml",
                        "event": "push",
                        "purpose": "release",
                        "run_id": sequence,
                        "run_attempt": 1,
                        "github_artifact_id": sequence,
                        "manifest_artifact_id": "manifest",
                    },
                }
            )
            self.store.write_artifact_manifest(manifest)
            self.manifests.append(manifest)
        source["OPENUPGRADE_TARGET_VERSION"] = self.manifests[-1].build_flags.values["odoo_version"]
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context=self.context,
                instance="testing",
                env=source,
                updated_at=self.profile.updated_at,
            )
        )
        self.request = OdooUpgradeRehearsalRequest(
            rehearsal_id=self.identifier,
            source_context=self.context,
            source_odoo_version=self.manifests[0].build_flags.values["odoo_version"],
            target_artifact_id=self.manifests[1].artifact_id,
            storage=OdooTestingDrillStorageBudget.model_validate(
                {
                    "database_bytes": 1024,
                    "filestore_bytes": 1024,
                    "staging_bytes": 8192,
                    "reserve_bytes": 1024,
                }
            ),
            reason="isolated fixture",
        )
        self.enterContext(
            patch(
                "control_plane.dokploy.source.read_dokploy_config",
                return_value=("https://provider.invalid", "fixture-provider-token"),
            )
        )
        self.enterContext(
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                return_value={"serverId": "fixture-server"},
            )
        )
        self.record, self.source, self.shape = rehearsal_inputs(
            self.root, self.store, self.product, self.request, self.principal
        )
        self.store.compare_and_write_odoo_upgrade_rehearsal(None, self.record)

    def authority(self) -> tuple[object, ...]:
        return (
            self.store.list_product_profile_records(),
            self.store.list_dokploy_target_id_records(),
            tuple(
                item
                for item in self.store.list_physical_provider_target_records()
                if item.context != self.identifier
            ),
            self.store.list_dokploy_target_records(),
            tuple(
                item
                for item in self.store.list_runtime_environment_records()
                if item.context != self.identifier
            ),
            tuple(
                item
                for item in self.store.list_secret_records(limit=None)
                if item.context != self.identifier
            ),
        )

    def copy(self, role: str) -> RehearsalCopyReceipt:
        database = self.identifier.replace("-", "_") + "_" + role
        digest = hashlib.sha256(role.encode()).hexdigest()
        return RehearsalCopyReceipt(
            database=database,
            witness_sha256=hashlib.sha256(
                (self.record.intent_sha256 + "\n" + database).encode()
            ).hexdigest(),
            database_sha256=digest,
            filestore_sha256=hashlib.sha256((role + "files").encode()).hexdigest(),
            source_capture_sha256=hashlib.sha256(b"fixture capture").hexdigest(),
            sanitized=True,
            odoo_version=self.request.source_odoo_version
            if role.endswith("source")
            else self.manifests[-1].build_flags.values["odoo_version"],
            captured_at=datetime.now(timezone.utc),
        )

    def adapter(self, action: str) -> OdooRehearsalAdapter:
        record = self.store.read_odoo_upgrade_rehearsal(self.identifier)
        assert record is not None
        return OdooRehearsalAdapter(
            self.root,
            self.store,
            self.product,
            self.request.model_copy(
                update={
                    "mode": "apply",
                    "action": action,
                    "reviewed_plan_sha256": record.binding_sha256,
                }
            ),
            self.principal,
            "fixture-trace",
        )

    def test_new_record_and_source_inputs_leave_canonical_lane_authority_unchanged(self) -> None:
        before = self.authority()
        planned, _, _ = rehearsal_inputs(
            self.root, self.store, self.product, self.request, self.principal
        )
        self.assertEqual(planned, self.record)
        self.assertEqual(self.authority(), before)
        spec = rehearsal_host_spec(
            self.record.model_copy(update={"compose_id": "own-compose"}),
            "prepare",
            "apply",
            self.source,
            self.shape,
            {},
        )
        self.assertNotIn("private-source-key", repr(spec))
        self.assertNotIn("private-mail-key", repr(spec))
        self.assertEqual(
            cast(dict[str, str], spec["environment"])["ODOO_WORKERS"], self.shape["ODOO_WORKERS"]
        )

    def test_artifact_from_another_product_cannot_receive_copied_data(self) -> None:
        assert self.manifests[1].source_build is not None
        changed = self.manifests[1].model_copy(
            update={
                "source_build": self.manifests[1].source_build.model_copy(
                    update={"repository": "another/product"}
                )
            }
        )
        self.store.write_artifact_manifest(changed)
        with self.assertRaises(RehearsalRefused) as refusal:
            rehearsal_inputs(self.root, self.store, self.product, self.request, self.principal)
        self.assertEqual(refusal.exception.code, RehearsalPrerequisite.ARTIFACT_UNAVAILABLE)

    def test_artifact_without_source_major_qualification_refuses_before_placement(self) -> None:
        manifest = self.manifests[-1]
        values = dict(manifest.build_flags.values)
        values.pop("sql_copy_source_versions")
        candidate = manifest.model_copy(
            update={"build_flags": manifest.build_flags.model_copy(update={"values": values})}
        )
        with (
            patch.object(self.store, "read_artifact_manifest", return_value=candidate),
            patch("control_plane.dokploy.api.fetch_dokploy_target_payload") as provider,
        ):
            with self.assertRaises(RehearsalRefused) as refused:
                rehearsal_inputs(self.root, self.store, self.product, self.request, self.principal)
            self.assertEqual(refused.exception.code, RehearsalPrerequisite.ARTIFACT_UNAVAILABLE)
            provider.assert_not_called()

    def test_placement_uses_existing_testing_reader_target(self) -> None:
        target = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="testing"
        )
        with patch(
            "control_plane.dokploy.api.fetch_dokploy_target_payload",
            return_value={"serverId": self.record.server_id},
        ) as provider:
            rehearsal_inputs(self.root, self.store, self.product, self.request, self.principal)
        self.assertEqual(provider.call_args.kwargs["target_id"], target.target_id)

    def test_target_audit_recognizes_only_a_matching_owned_rehearsal_reservation(self) -> None:
        from control_plane.workflows.provider_target_audit import audit_provider_targets

        owned = self.record.model_copy(update={"compose_id": "owned-compose"})
        self.store.compare_and_write_odoo_upgrade_rehearsal(self.record, owned)
        result = audit_provider_targets(self.store, context_name=self.identifier)
        self.assertTrue(result.ok)
        with patch.object(self.store, "read_odoo_upgrade_rehearsal", return_value=None):
            self.assertFalse(audit_provider_targets(self.store, context_name=self.identifier).ok)

    def test_native_registry_keeps_a_rehearsal_target_out_of_canonical_lanes(self) -> None:
        current = self.store.read_odoo_upgrade_rehearsal(self.identifier)
        assert current is not None
        bound = current.model_copy(update={"compose_id": "own-reserved-compose"})
        self.store.compare_and_write_odoo_upgrade_rehearsal(current, bound)
        before = self.authority()
        owned = self.store.read_provider_target_record(
            context_name=self.identifier, instance_name="rehearsal"
        )
        prod = next(lane for lane in self.profile.lanes if lane.instance == "prod")
        with self.assertRaises(ValueError):
            self.store.write_provider_target_record(
                owned.model_copy(update={"context": prod.context, "instance": prod.instance})
            )
        self.assertEqual(self.authority(), before)

    def test_partial_custody_does_not_clear_an_uncertain_operation(self) -> None:
        prepared = self.record.model_copy(
            update={"state": "prepared", "compose_id": "own-partial-compose"}
        )
        self.store.compare_and_write_odoo_upgrade_rehearsal(self.record, prepared)
        adapter = self.adapter("execute")
        with patch.object(
            OdooRehearsalAdapter, "host", return_value={"completed": False, "state": "prepared"}
        ):
            observation = adapter.observe("original", "execute", "original-reconciliation")
        self.assertEqual(observation.outcome, "unknown")
        self.assertEqual(self.store.read_odoo_upgrade_rehearsal(self.identifier), prepared)

    def test_equal_artifact_versions_are_not_an_upgrade_rehearsal(self) -> None:
        draft = self.request.model_copy(
            update={"target_artifact_id": self.manifests[0].artifact_id}
        )
        with self.assertRaises(RehearsalRefused):
            rehearsal_inputs(self.root, self.store, self.product, draft, self.principal)

    def test_alias_to_existing_target_is_refused(self) -> None:
        identifier = self.store.list_dokploy_target_id_records()[0].target_id
        with self.assertRaises(RehearsalRefused):
            require_unaliased_rehearsal(
                self.store, self.record.model_copy(update={"compose_id": identifier})
            )

    def test_local_password_primitive_never_changes_canonical_keys_and_is_stable(self) -> None:
        before = self.authority()
        prepare_rehearsal_local_credentials(self.store, self.identifier, actor=self.principal)
        first = rehearsal_credentials(self.store, self.identifier)
        prepare_rehearsal_local_credentials(self.store, self.identifier, actor=self.principal)
        self.assertEqual(rehearsal_credentials(self.store, self.identifier), first)
        self.assertEqual(self.authority(), before)
        self.assertNotIn("private-source-key", first.values())
        self.assertEqual(
            {item.context for item in self.store.list_secret_records(limit=None)}, {self.identifier}
        )

    def test_compare_and_write_preserves_the_winner(self) -> None:
        updated = self.record.model_copy(update={"project_id": "owned-project"})
        self.store.compare_and_write_odoo_upgrade_rehearsal(self.record, updated)
        with self.assertRaises(ValueError):
            self.store.compare_and_write_odoo_upgrade_rehearsal(
                self.record, self.record.model_copy(update={"project_id": "loser"})
            )
        self.assertEqual(self.store.read_odoo_upgrade_rehearsal(self.identifier), updated)

    def test_actual_host_stdout_survives_the_shared_scheduler_marker_parser(self) -> None:
        import io
        import shlex
        import sys
        from contextlib import redirect_stdout
        from control_plane.dokploy.post_deploy import extract_odoo_post_deploy_readback_markers

        record = self.record.model_copy(update={"compose_id": "own-compose"})
        adapter = self.adapter("prepare")

        def scheduler(
            record: OdooUpgradeRehearsalRecord, script: str, check: Callable[[], None]
        ) -> dict[str, str]:
            lines = script.splitlines()
            encoded = shlex.split(lines[2])[2]
            program = "\n".join(lines[3:-1])
            output = io.StringIO()
            with (
                patch.object(sys, "argv", ["host", encoded]),
                patch.object(Path, "home", return_value=self.root.resolve()),
                redirect_stdout(output),
            ):
                exec(compile(program, "<actual-host>", "exec"), {})
            # This is the parser used by run_manual_data_schedule, including
            # its value lowercasing and allowlist.
            return extract_odoo_post_deploy_readback_markers({"logs": output.getvalue()})

        with patch.object(adapter, "run_script", side_effect=scheduler):
            result = adapter.host(record, "observe", lambda: None)
        self.assertEqual(result["intent_sha256"], record.intent_sha256)
        self.assertFalse(result["completed"])
        with patch.object(adapter, "run_script", return_value={}):
            with self.assertRaises(RehearsalRefused):
                adapter.host(record, "observe", lambda: None)

    def test_retirement_uses_retained_intent_without_source_or_credential_reads(self) -> None:
        retained = self.record.model_copy(update={"state": "verified", "compose_id": "own-compose"})
        self.store.compare_and_write_odoo_upgrade_rehearsal(self.record, retained)
        adapter = self.adapter("retire")
        with (
            patch(
                "control_plane.odoo_upgrade_rehearsal._source_values",
                side_effect=AssertionError("Retirement must not depend on the source"),
            ),
            patch(
                "control_plane.odoo_upgrade_rehearsal.rehearsal_credentials",
                side_effect=AssertionError("Retirement must not require managed credentials"),
            ),
        ):
            record, upgrade, shape = adapter.inputs()
            self.assertEqual(record, retained)
            self.assertEqual(upgrade, retained.upgrade_environment)
            self.assertEqual(shape, retained.shape_environment)
            with patch.object(adapter, "run_script", side_effect=RuntimeError("host reached")):
                with self.assertRaisesRegex(RuntimeError, "host reached"):
                    adapter.host(record, "apply", lambda: None)

    def test_lifecycle_preserves_both_recovery_states_and_all_original_lanes(self) -> None:
        before = self.authority()
        copies = []
        creates = iter(
            (
                {"projectId": "own-project"},
                {"environmentId": "own-environment"},
                {"composeId": "own-compose"},
            )
        )

        def schedule(
            record: OdooUpgradeRehearsalRecord, script: str, checkpoint: Callable[[], None]
        ) -> dict[str, str]:
            checkpoint()
            resource = rehearsal_resource(record)
            return {
                "testing_resources_ok": "true",
                "testing_resources_binding_sha256": resource.binding_sha256,
            }

        def host(
            adapter: OdooRehearsalAdapter,
            record: OdooUpgradeRehearsalRecord,
            mode: str,
            checkpoint: Callable[[], None],
        ) -> dict[str, object]:
            checkpoint()
            action = adapter.request.action
            if action == "execute":
                copies.append(self.copy("source"))
            if action == "simulate-final":
                copies.append(self.copy("final_source"))
            if action == "verify":
                copies.append(self.copy("final_upgrade"))
            state = {
                "prepare": "prepared",
                "execute": "upgraded",
                "simulate-final": "switched",
                "verify": "verified",
                "retire": "retired",
            }[action]
            return {
                "intent_sha256": record.intent_sha256,
                "state": state,
                "copies": [copy.model_dump(mode="json") for copy in copies],
                "elapsed_seconds": {**record.elapsed_seconds, action: 0.1},
                "resources_preserved": True,
                "public_route": False,
                "business_host_frozen": False,
            }

        with (
            patch(
                "control_plane.dokploy.sandbox_placement.created_placement",
                return_value=("", "", ""),
            ),
            patch(
                "control_plane.dokploy.api.dokploy_request",
                side_effect=lambda **kwargs: next(creates),
            ),
            patch.object(OdooRehearsalAdapter, "run_script", side_effect=schedule),
            patch.object(OdooRehearsalAdapter, "host", autospec=True, side_effect=host),
        ):
            for action in ("prepare", "execute", "simulate-final", "verify", "retire"):
                from control_plane.provider_operations import ProviderOperationLease

                result = self.adapter(action).apply(
                    "original-operation", Mock(spec=ProviderOperationLease)
                )
                self.assertTrue(
                    cast(dict[str, object], result.response_payload["result"])[
                        "resources_preserved"
                    ]
                )
                self.assertEqual(self.authority(), before)
        retired = self.store.read_odoo_upgrade_rehearsal(self.identifier)
        assert retired is not None
        self.assertEqual(retired.state, "retired")
        self.assertEqual(retired.copies, tuple(copies))

    def test_unknown_provider_effect_is_never_blindly_replayed(self) -> None:
        prepared = self.record.model_copy(update={"state": "prepared", "compose_id": "own-compose"})
        self.store.compare_and_write_odoo_upgrade_rehearsal(self.record, prepared)
        adapter = self.adapter("execute")

        def run() -> DurableProviderOperationResult:
            return run_durable_provider_operation(
                store=self.store,
                scope=self.principal,
                route_path="fixture-route",
                idempotency_key="original-execute",
                request_fingerprint="fixture-intent",
                lease_owner="fixture-worker",
                response_trace_id="fixture-trace",
                adapter=adapter,
            )

        with patch.object(
            OdooRehearsalAdapter, "host", side_effect=TimeoutError("private host detail")
        ) as host:
            with self.assertRaises(TimeoutError):
                run()
            calls = host.call_count
            second = run()
            self.assertNotEqual(second.status, "completed")
            self.assertEqual(self.store.read_odoo_upgrade_rehearsal(self.identifier), prepared)
            # Any later call is observation only; the original apply cannot run.
            self.assertTrue(all(call.args[1] == "observe" for call in host.call_args_list[calls:]))


class RehearsalHostSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.test_odoo_testing_restore import FreshTestingHostProgramTests
        from control_plane._odoo_copy_runtime_guard import COPY_RUNTIME_INSPECTION_LIBRARY
        from control_plane._odoo_testing_storage_program import STORAGE_MAINTENANCE_LIBRARY
        from control_plane._odoo_upgrade_rehearsal_program import ODOO_UPGRADE_REHEARSAL_PROGRAM
        import ast

        self.fixture = FreshTestingHostProgramTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.identifier = rehearsal_identity("fixture", "agent", "host-key")
        previous = self.fixture.gen.namespace
        self.fixture.database = self.identifier.replace("-", "_") + "_source"
        self.spec = {
            **self.fixture.spec,
            "generation": {
                **self.fixture.spec["generation"],
                "namespace": self.identifier,
                "instance": "rehearsal",
            },
            "compose_project": self.identifier,
            "database_name": self.fixture.database,
            "storage_maintenance": True,
        }
        renamed = {}
        for name, volume in self.fixture.volumes.items():
            new_name = name.replace(previous, self.identifier)
            path = Path(volume["Mountpoint"])
            new = path.with_name(new_name)
            path.rename(new)
            volume["Mountpoint"] = str(new)
            renamed[new_name] = volume
        self.fixture.volumes = renamed
        for container in self.fixture.containers.values():
            container["Config"]["Labels"]["com.docker.compose.project"] = self.identifier
            container["Config"]["Env"] = [
                "PLATFORM_INSTANCE=rehearsal",
                "ODOO_DB_HOST=database",
                "ODOO_DB_NAME=" + self.fixture.database,
                "ODOO_MAX_CRON_THREADS=0",
            ]
            container["NetworkSettings"]["Networks"] = {"testing-data": {}}
            for mount in container["Mounts"]:
                mount["Name"] = mount["Name"].replace(previous, self.identifier)
            container["State"] = {"Running": True}
        program = (
            COPY_RUNTIME_INSPECTION_LIBRARY
            + STORAGE_MAINTENANCE_LIBRARY
            + ODOO_UPGRADE_REHEARSAL_PROGRAM
        )
        tree = ast.parse(program)
        tree.body.pop()  # Load the real emitted functions without starting its CLI.
        self.host: dict[str, Any] = {}
        exec(compile(tree, "<rehearsal-host>", "exec"), self.host)

        def docker(*arguments: str, stdin: bytes | None = None) -> bytes:
            if arguments[:2] == ("inspect", "testing-data"):
                import json

                return json.dumps(
                    [
                        {
                            "Internal": True,
                            "Containers": {
                                item["Id"]: {} for item in self.fixture.containers.values()
                            },
                        }
                    ]
                ).encode()
            return self.fixture.command(["docker", *arguments], input=stdin).stdout

        self.host["docker"] = docker
        self.host["one_inspection"] = lambda name: __import__("json").loads(
            docker("inspect", name)
        )[0]

    def inspect(self) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.host["inspect_copy_runtime"](self.spec, expected_instance="rehearsal"),
        )

    def test_runtime_guard_accepts_only_the_private_standalone_network(self) -> None:
        qualified = self.inspect()
        self.assertEqual(set(qualified), set(self.fixture.containers))
        self.fixture.containers["web"]["NetworkSettings"]["Networks"]["dokploy-network"] = {}
        with self.assertRaises(RuntimeError):
            self.inspect()

    def test_another_container_on_the_private_network_is_refused(self) -> None:
        original = self.host["one_inspection"]

        def inspect(name: str) -> dict[str, Any]:
            result = cast(dict[str, Any], original(name))
            if name == "testing-data":
                result["Containers"]["foreign-container"] = {}
            return result

        self.host["one_inspection"] = inspect
        with self.assertRaises(RuntimeError):
            self.inspect()

    def test_host_bind_and_published_ports_refuse_before_data_work(self) -> None:
        web = self.fixture.containers["web"]
        web["HostConfig"]["PortBindings"] = {"8069/tcp": [{"HostPort": "8069"}]}
        with self.assertRaises(RuntimeError):
            self.inspect()
        web["HostConfig"]["PortBindings"] = {}
        web["Mounts"].append(
            {"Type": "bind", "Source": "/private/identity", "Destination": "/root/.ssh"}
        )
        with self.assertRaises(RuntimeError):
            self.inspect()

    def test_streamed_capture_stops_at_its_reviewed_byte_bound(self) -> None:
        import sys

        destination = self.fixture.root / "captured.dump"
        with self.assertRaises(RuntimeError):
            self.host["bounded_capture"](
                [sys.executable, "-c", 'import sys;sys.stdout.buffer.write(b"fixture"*10000)'],
                destination,
                1024,
            )
        self.assertLessEqual(destination.stat().st_size, 1024)

    def test_failed_restore_cleans_only_container_dump_and_preserves_host_capture(self) -> None:
        raw = self.fixture.root / "raw-source"
        raw.mkdir()
        dump = raw / "database.dump"
        dump.write_bytes(b"retained synthetic source")
        copied = False

        def docker(*arguments: str, **kwargs: Any) -> bytes:
            nonlocal copied
            if arguments[0] == "cp":
                copied = True
            if arguments[-2:] in {("id", "-u"), ("id", "-g")}:
                return b"1000\n"
            if "rm" in arguments:
                copied = False
            return b""

        self.host["docker"] = docker
        self.host["copy_runner"] = Mock(side_effect=RuntimeError("restore failed"))
        with self.assertRaisesRegex(RuntimeError, "restore failed"):
            self.host["sanitize_capture"](
                {}, {"script-runner": ("runner", {})}, self.fixture.database, raw
            )
        self.assertFalse(copied)
        self.assertEqual(dump.read_bytes(), b"retained synthetic source")

    def test_source_sql_phase_receives_the_source_version_without_running_upgrade(self) -> None:
        source_version, target_version = "1.0", "2.0"
        spec = {
            "record": {"source_odoo_version": source_version},
            "workflow": {},
            "source_image": "candidate",
            "target_image": "candidate",
            "versions": {"candidate": target_version},
            "sanitize_runner": "source-sql",
            "upgrade_runner": "upgrade",
            "readback_spec": "readback",
            "readback_program": "readback-program",
        }
        self.host["witness"] = lambda *args: "witness"

        def docker(*arguments: str, **kwargs: Any) -> bytes:
            environment = dict(argument.split("=", 1) for argument in arguments if "=" in argument)
            self.assertEqual(environment["ODOO_VERSION"], source_version)
            self.assertNotIn(target_version, environment.values())
            return b"testing_copy_sanitized=true\ntesting_copy_pre_registry_sanitized=true\n"

        self.host["docker"] = docker
        self.host["copy_runner"](
            spec,
            {"script-runner": ("runner", {})},
            self.fixture.database,
            fresh=True,
            sanitize_only=True,
        )

    def test_tampered_recovery_file_refuses_without_database_or_container_writes(self) -> None:
        import json

        spec = {
            "record": {
                "rehearsal_id": self.identifier,
                "intent_sha256": hashlib.sha256(b"host-intent").hexdigest(),
                "source_binding_sha256": hashlib.sha256(b"source-binding").hexdigest(),
            }
        }
        root = self.fixture.root / "custody"
        root.mkdir()
        (root / "capture.json").write_text(
            json.dumps({"source_binding_sha256": spec["record"]["source_binding_sha256"]})
        )
        database = self.host["database_names"](spec)["source"]
        directory = root / database
        directory.mkdir()
        for name in ("database.dump", "filestore.tar"):
            (directory / name).write_bytes(name.encode())
        receipt = {
            "database": database,
            "witness_sha256": self.host["witness"](spec, database),
            "source_capture_sha256": self.host["digest"](root / "capture.json"),
            "database_sha256": self.host["digest"](directory / "database.dump"),
            "filestore_sha256": self.host["digest"](directory / "filestore.tar"),
        }
        sql = Mock(return_value=receipt["witness_sha256"])
        self.host["sql"] = sql
        self.host["validate_snapshots"](spec, root, {}, [receipt])
        (directory / "database.dump").write_bytes(b"changed")
        sql.reset_mock()
        with self.assertRaises(RuntimeError):
            self.host["validate_snapshots"](spec, root, {}, [receipt])
        sql.assert_not_called()

    def test_retirement_and_independent_readback_keep_both_recovery_snapshots(self) -> None:
        import base64
        import io
        import json
        import sys
        from contextlib import redirect_stdout

        record: dict[str, Any] = {
            "rehearsal_id": self.identifier,
            "product": "fixture",
            "state": "verified",
            "intent_sha256": hashlib.sha256(b"host-retirement").hexdigest(),
            "source_binding_sha256": hashlib.sha256(b"source").hexdigest(),
            "elapsed_seconds": {},
            "storage": {
                "database_bytes": 1,
                "filestore_bytes": 1,
                "staging_bytes": 1,
                "reserve_bytes": 1,
            },
        }
        spec = {
            "record": record,
            "mode": "apply",
            "action": "retire",
            "target_image": self.spec["image_reference"],
        }
        root = self.fixture.root / ".local/state/launchplane-rehearsals" / self.identifier
        root.mkdir(parents=True)
        (root / "capture.json").write_text(
            json.dumps({"source_binding_sha256": record["source_binding_sha256"]})
        )
        copies = []
        paths = []
        for role in ("final_source", "final_upgrade"):
            database = self.host["database_names"](spec)[role]
            directory = root / database
            directory.mkdir()
            for name in ("database.dump", "filestore.tar"):
                path = directory / name
                path.write_bytes((role + name).encode())
                paths.append(path)
            copies.append(
                {
                    "database": database,
                    "witness_sha256": self.host["witness"](spec, database),
                    "source_capture_sha256": self.host["digest"](root / "capture.json"),
                    "database_sha256": self.host["digest"](directory / "database.dump"),
                    "filestore_sha256": self.host["digest"](directory / "filestore.tar"),
                    "sanitized": True,
                    "odoo_version": "fixture-version",
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        record["copies"] = copies
        containers = {name: (name, value) for name, value in self.fixture.containers.items()}
        self.host["inspection"] = lambda *args: containers
        self.host["sql"] = lambda containers, statement, database: self.host["witness"](
            spec, database
        )

        def stop(*arguments: str, **kwargs: Any) -> bytes:
            self.assertEqual(arguments[0], "stop")
            for name in arguments[1:]:
                self.fixture.containers[name]["State"]["Running"] = False
            return b""

        self.host["docker"] = stop

        def run() -> str:
            output = io.StringIO()
            encoded = base64.b64encode(json.dumps(spec).encode()).decode()
            with (
                patch.object(sys, "argv", ["rehearsal", encoded]),
                patch.object(Path, "home", return_value=self.fixture.root),
                redirect_stdout(output),
            ):
                self.host["main"]()
            return output.getvalue()

        run()
        self.assertTrue(all(path.is_file() for path in paths))

        spec["mode"] = "observe"
        self.host["sql"] = Mock(side_effect=AssertionError("retired database must stay stopped"))
        output = run()
        result = json.loads(bytes.fromhex(output.strip().split("=", 1)[1]))
        self.assertEqual(result["state"], "retired")
        self.assertEqual(result["copies"], copies)
        self.assertTrue(all(path.is_file() for path in paths))

        # An older completed receipt remains inspectable after retirement,
        # using current container identity and files, without restarting SQL.
        spec["record"]["state"] = "retired"
        spec["action"] = "verify"
        prior = {**result, "state": "verified"}
        (root / "verify.json").write_text(json.dumps(prior))
        historical = json.loads(bytes.fromhex(run().strip().split("=", 1)[1]))
        self.assertEqual(historical["state"], "verified")
        spec["action"] = "retire"

        (root / "retire.json").unlink()
        for receipt in copies:
            (root / receipt["database"] / "receipt.json").write_text(json.dumps(receipt))
        self.host["docker"] = Mock(
            side_effect=AssertionError(
                "Partial custody inspection must not mutate or start a runtime"
            )
        )
        partial = json.loads(bytes.fromhex(run().strip().split("=", 1)[1]))
        self.assertFalse(partial["completed"])
        self.assertEqual(partial["copies"], copies)
        self.assertTrue(all(path.is_file() for path in paths))


class RehearsalHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = RehearsalScenario()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    async def test_read_role_can_plan_without_changing_any_lane_and_cannot_apply(self) -> None:
        from fastapi import FastAPI, HTTPException
        from pydantic import BaseModel
        from control_plane.http_routes.odoo_upgrade_rehearsal import (
            register_odoo_upgrade_rehearsal_routes,
        )
        from control_plane.http_routes.support import ReadRouteDependencies, ApiRouteRegistrar
        from control_plane.service_auth import LocalOperatorIdentity
        from tests.support.http import request
        from control_plane.contracts.odoo_upgrade_rehearsal import ODOO_UPGRADE_REHEARSAL_ROUTE

        app = FastAPI()
        identity = LocalOperatorIdentity(subject="fixture-reader", token_label="fixture-read")

        async def run_mutation(**kwargs: Any) -> Any:
            raise AssertionError("A read role must never reach a provider mutation")

        def error(**kwargs: Any) -> HTTPException:
            return HTTPException(
                status_code=kwargs["status_code"], detail={"error": {"code": kwargs["code"]}}
            )

        register_odoo_upgrade_rehearsal_routes(
            cast(ApiRouteRegistrar, app),
            dependencies=ReadRouteDependencies(
                read_identity=lambda: identity,
                get_record_store=lambda: self.fixture.store,
                next_trace_id=lambda: "fixture-trace",
                authorization_allows=lambda **kwargs: True,
                http_error=error,
                error_response_model=BaseModel,
            ),
            mutation_identity=lambda: identity,
            root=self.fixture.root,
            run_provider_mutation=run_mutation,
        )
        before = self.fixture.authority()
        response = await request(
            app,
            "POST",
            ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=self.fixture.product),
            payload=self.fixture.request.model_dump(mode="json"),
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["rehearsal_id"], self.fixture.identifier)
        self.assertEqual(self.fixture.authority(), before)
        denied = await request(
            app,
            "POST",
            ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=self.fixture.product),
            payload=self.fixture.request.model_copy(
                update={"mode": "apply", "reviewed_plan_sha256": self.fixture.record.binding_sha256}
            ).model_dump(mode="json"),
            headers={"Idempotency-Key": "fixture-prepare"},
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.fixture.authority(), before)
        from contextlib import contextmanager

        @contextmanager
        def busy(*args: Any) -> Any:
            yield None

        prepared = self.fixture.record.model_copy(
            update={"compose_id": "own-compose", "state": "prepared"}
        )
        self.fixture.store.compare_and_write_odoo_upgrade_rehearsal(self.fixture.record, prepared)
        readback = self.fixture.request.model_copy(update={"mode": "readback"})
        with (
            patch.object(
                self.fixture.store, "testing_replacement_execution_lock", side_effect=busy
            ),
            patch.object(OdooRehearsalAdapter, "host") as host,
        ):
            response = await request(
                app,
                "POST",
                ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=self.fixture.product),
                payload=readback.model_dump(mode="json"),
            )
            self.assertEqual(response.status_code, 409)
            host.assert_not_called()
        with patch.object(OdooRehearsalAdapter, "host") as host:
            response = await request(
                app,
                "POST",
                ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=self.fixture.product),
                payload=readback.model_copy(
                    update={"source_context": "another-context"}
                ).model_dump(mode="json"),
            )
            self.assertEqual(response.status_code, 409)
            host.assert_not_called()
