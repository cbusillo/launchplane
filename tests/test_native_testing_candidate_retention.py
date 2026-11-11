from datetime import datetime, timezone
from typing import Any
import unittest
from unittest.mock import Mock

from control_plane.contracts.testing_candidate import (
    TestingCandidateAllocation,
    testing_candidate_namespace,
)
from control_plane.testing_candidate_retention import candidate_record_refusals


class StagedSourceInputsTests(unittest.TestCase):
    def test_native_inspection_keeps_simultaneous_web_port_source_and_mount_holds(self) -> None:
        import ast
        import hashlib
        import json
        import os
        from pathlib import Path
        import re
        from tempfile import TemporaryDirectory
        import time
        from control_plane.testing_candidate_retention_host import (
            NATIVE_TESTING_RETENTION_INSPECTION,
        )

        resources = {"namespace": "lp-testing-" + "a" * 32, "instance": "testing"}
        digest = hashlib.sha256(
            json.dumps(resources, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            volumes = {}
            for role, suffix in (
                ("ODOO_DB_VOLUME", "db"),
                ("ODOO_DATA_VOLUME", "data"),
                ("ODOO_LOG_VOLUME", "logs"),
            ):
                name = resources["namespace"] + "-" + suffix
                path = root / "volumes" / name / "_data"
                path.mkdir(parents=True)
                (path / "retained").write_text("fixture")
                volumes[name] = {
                    "Name": name,
                    "Driver": "local",
                    "Options": {},
                    "Mountpoint": str(path),
                    "Labels": {
                        "io.launchplane.testing-resources": digest,
                        "io.launchplane.testing-resource-role": role,
                    },
                }
            containers = {
                "web": {
                    "Config": {
                        "Labels": {
                            "com.docker.compose.project": "project",
                            "com.docker.compose.service": "web",
                        }
                    },
                    "State": {"Running": True},
                    "HostConfig": {"PortBindings": {"80/tcp": [{}]}},
                    "Mounts": [{"Name": name} for name in volumes],
                }
            }
            namespace: dict[str, Any] = {
                "spec": {"resources": resources, "project": "project"},
                "hashlib": hashlib,
                "json": json,
                "os": os,
                "re": re,
                "time": time,
                "Path": Path,
                "WEB_GATE_PATH": "/volumes/data/gate",
                "load": lambda *_args: {"ID": "engine", "DockerRootDir": str(root)},
                "identifiers": lambda *args: list(volumes if args[0] == "volume" else containers),
                "_batched_docker_inspect": lambda kind, *_args: (
                    volumes if kind == "volume" else containers
                ),
                "failed_candidate_own_web": lambda *_args: False,
                "staged_source_inputs": lambda _path: True,
                "web_gate_unexpected": lambda *_args: True,
                "storage_backing_pools": lambda: {},
                "retained_volume_identity": lambda *_args, **_kwargs: "identity",
                "retained_volume_engine_identity": lambda *_args: "engine-identity",
            }
            # Execute the shipped function against an anonymous filesystem and Docker inventory.
            tree = ast.parse(NATIVE_TESTING_RETENTION_INSPECTION)
            function = next(
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "inspect_candidate"
            )
            exec(
                compile(ast.Module(body=[function], type_ignores=[]), "native-inspection", "exec"),
                namespace,
            )
            from unittest.mock import patch

            with patch.object(Path, "read_text", return_value="boot"):
                result = namespace["inspect_candidate"]()
        for row in result["volumes"]:
            self.assertEqual(
                set(row.get("reasons", ())),
                {
                    "candidate_web_active_or_unknown",
                    "candidate_published_ports",
                    "source_inputs_preserved",
                    "container_reference",
                    "completed_or_unknown_web_gate",
                },
            )

    def test_only_canonical_empty_markers_are_not_preserved_inputs(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from control_plane.testing_candidate_retention_host import staged_source_inputs

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            marker = root / (".lp-native-" + "a" * 32)
            marker.mkdir()
            self.assertFalse(staged_source_inputs(root))
            for name in ("database.artifact", "filestore.artifact.partial", "unknown"):
                with self.subTest(name=name):
                    child = marker / name
                    child.write_bytes(b"original or interrupted source")
                    self.assertTrue(staged_source_inputs(root))
                    child.unlink()
            with patch("os.scandir", side_effect=PermissionError):
                self.assertTrue(staged_source_inputs(root))
            marker.rmdir()
            marker.write_bytes(b"")
            self.assertTrue(staged_source_inputs(root))
            marker.unlink()
            other = root / "empty"
            other.mkdir()
            marker.symlink_to(other, target_is_directory=True)
            self.assertTrue(staged_source_inputs(root))
            marker.unlink()
            marker.symlink_to(root / "missing", target_is_directory=True)
            self.assertTrue(staged_source_inputs(root))
            marker.unlink()
            alias = root / ".lp-native-unknown"
            alias.mkdir()
            self.assertTrue(staged_source_inputs(root))


class CandidateRetentionGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = TestingCandidateAllocation(
            namespace=testing_candidate_namespace("odoo", "site", "failed-original"),
            product="odoo",
            context="site",
            operation_id="failed-original",
            intent_sha256="a" * 64,
            serving_target_id="selected",
            server_id="host",
            project_id="project",
            environment_id="environment",
            compose_id="failed-copy",
            created_at=datetime.now(timezone.utc),
        )
        self.operation: Any = Mock(
            product="odoo",
            context="site",
            instance="testing",
            status="fail",
            lease_owner="",
            lease_expires_at="",
            is_native_testing_rebuild=True,
        )

    def refusal(self, **kwargs: Any) -> tuple[str, ...]:
        return candidate_record_refusals(
            self.candidate,
            self.operation,
            product="odoo",
            context="site",
            receiver_operation_id="pending-original",
            selected_targets=(),
            **kwargs,
        )

    def test_active_provider_snapshot_reports_other_holds_without_host_inspection(self) -> None:
        from pathlib import Path
        from unittest.mock import patch
        from control_plane import testing_candidate_retention as retention
        from control_plane.dokploy import api, native_copy_stream, source

        payload = {
            "composeId": self.candidate.compose_id,
            "environmentId": self.candidate.environment_id,
            "serverId": self.candidate.server_id,
            "composeType": "docker-compose",
            "appName": self.candidate.namespace,
            "composeStatus": "running",
            "autoDeploy": True,
        }
        with (
            patch.object(source, "read_dokploy_config", return_value=("host", "token")),
            patch.object(api, "fetch_dokploy_target_payload", return_value=payload),
            patch.object(api, "list_deployments_for_target", side_effect=ValueError),
            patch.object(api, "list_dokploy_schedules", return_value=[{"enabled": True}]),
            patch.object(native_copy_stream, "run_managed_server_program") as host,
        ):
            observed, _reason = retention.inspect_candidate_resources(
                Path("."), Mock(), self.candidate
            )
        self.assertEqual(observed["volumes"], [])
        self.assertTrue(
            {
                "provider_job_active_or_unknown",
                "provider_automatic_or_public_use",
                "provider_job_inventory_unavailable",
                "provider_schedule_reference",
            }.issubset(observed["reasons"])
        )
        host.assert_not_called()

    def test_active_original_foreign_instance_and_selected_copy_each_refuse(self) -> None:
        for field, value, reason in (
            ("status", "pending", "original_not_terminal"),
            ("status", "reconciliation_required", "original_not_terminal"),
            ("instance", "prod", "foreign_original"),
            ("product", "foreign", "foreign_original"),
            ("lease_expires_at", "unknown", "original_lease_unknown"),
        ):
            with self.subTest(field=field):
                original = getattr(self.operation, field)
                setattr(self.operation, field, value)
                self.assertIn(reason, self.refusal())
                setattr(self.operation, field, original)
        reasons = candidate_record_refusals(
            self.candidate,
            self.operation,
            product="odoo",
            context="site",
            receiver_operation_id="failed-original",
            selected_targets=("failed-copy",),
        )
        self.assertIn("receiver_candidate", reasons)
        self.assertIn("selected_target", reasons)


class CandidateRetentionPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        from control_plane import testing_candidate_retention as retention
        from control_plane.contracts.testing_candidate_retention import NativeTestingSweepRequest
        from control_plane.contracts.deploy_target import ProviderTargetRecord
        from tests.test_testing_replacement_resources import TestingReplacementResourceFixture
        from unittest.mock import patch

        self.retention = retention
        fixture = TestingReplacementResourceFixture(self)
        self.store, self.root = fixture.store, fixture.root
        self.request = NativeTestingSweepRequest(
            kind="native-testing",
            product=fixture.profile.product,
            context=fixture.context,
            operation_id=fixture.operation.operation_id,
        )
        original = fixture.operation.model_copy(
            update={
                "operation_id": "failed-original",
                "idempotency_key": "failed-key",
                "status": "fail",
                "phase": "failed",
                "finished_at": fixture.operation.updated_at,
                "error_message": "Fabricated restore failed before serving",
                "error_code": "candidate_preparation_failed",
                "request": fixture.operation.request.model_copy(
                    update={
                        "strategy": "replace-and-cutover",
                        "data_source_mode": "empty",
                        "allow_empty_data": True,
                        "artifact_id": "fixture-artifact",
                        "source_git_ref": "a" * 40,
                    }
                ),
            }
        )
        original = type(original).model_validate(original.model_dump())
        self.store.write_odoo_stable_target_replacement_operation_record(original)
        self.candidate = TestingCandidateAllocation(
            namespace=testing_candidate_namespace(
                original.product, original.context, original.operation_id
            ),
            product=original.product,
            context=original.context,
            operation_id=original.operation_id,
            intent_sha256="a" * 64,
            serving_target_id="compose-testing",
            server_id="host",
            project_id="project",
            environment_id="environment",
            compose_id="failed-copy",
            created_at=datetime.now(timezone.utc),
        )
        self.store_candidate(self.candidate)
        self.store.write_provider_target_record(
            ProviderTargetRecord(
                provider_id="dokploy",
                context=self.candidate.namespace,
                instance="testing-candidate",
                target_category="compose",
                target_id="failed-copy",
                display_name="Fabricated failed candidate",
                updated_at=original.updated_at,
            )
        )
        self.observation: dict[str, Any] = {
            "volumes": [
                {
                    "role": "ODOO_DB_VOLUME",
                    "name": self.candidate.namespace + "-db",
                    "status": "observed",
                    "reason": "",
                    "identity": "b" * 64,
                    "engine_identity": "c" * 64,
                    "empty": True,
                    "boot_id": "boot",
                    "monotonic_deadline": 1000000000,
                }
            ],
            "capacity": [],
            "capacity_refusal": "receiver_generation_unavailable",
        }
        self.inspect = self.enterContext(
            patch.object(
                retention, "inspect_candidate_resources", return_value=(self.observation, "")
            )
        )
        self.enterContext(patch.object(retention, "_inventory_clear", return_value=True))
        self.host = self.enterContext(
            patch.object(retention, "_host_result", return_value=("eligible", "qualified"))
        )

    def plan(self) -> Any:
        return self.retention.build_native_testing_sweep(
            self.store, self.root, self.request, "trace"
        )[0]

    def store_candidate(self, candidate: TestingCandidateAllocation) -> None:
        from control_plane.storage.postgres import LaunchplaneTestingCandidateRow

        with self.store._session_factory() as session:
            session.merge(
                LaunchplaneTestingCandidateRow(
                    namespace=candidate.namespace, payload=self.store._payload_dict(candidate)
                )
            )
            session.commit()

    def with_application(self, *, prepared: bool) -> TestingCandidateAllocation:
        from control_plane.contracts.artifact_identity import (
            ArtifactIdentityManifest,
            ArtifactImageReference,
        )
        from control_plane.contracts.runtime_identity import RuntimeIdentity
        from control_plane.contracts.testing_candidate_application import (
            TestingCandidateApplication,
            TestingCandidateApplicationPlan,
            TestingCandidatePrepared,
        )

        digest = "sha256:" + "d" * 64
        identity = RuntimeIdentity(
            product=self.candidate.product,
            context=self.candidate.context,
            instance="testing",
            deployment_record_id="fixture-deployment",
            artifact_id="fixture-artifact",
            source_git_ref="a" * 40,
            image_reference="ghcr.io/example/app@" + digest,
            image_digest=digest,
        )
        plan = TestingCandidateApplicationPlan(
            product=self.candidate.product,
            context=self.candidate.context,
            operation_id=self.candidate.operation_id,
            namespace=self.candidate.namespace,
            target_id=self.candidate.compose_id,
            base_url="https://testing.example",
            artifact=ArtifactIdentityManifest(
                artifact_id="fixture-artifact",
                source_commit="a" * 40,
                enterprise_base_digest="sha256:" + "b" * 64,
                image=ArtifactImageReference(repository="ghcr.io/example/app", digest=digest),
            ),
            runtime_identity=identity,
            source="empty",
            authority_sha256="c" * 64,
            credential_bindings_sha256="f" * 64,
        )
        application = (
            TestingCandidateApplication(
                plan=plan,
                phase="prepared",
                deployment_id="fixture-deployment",
                prepared=TestingCandidatePrepared(
                    plan_sha256=plan.digest,
                    runtime_identity=identity,
                    private_endpoint="http://fixture-private",
                    deployment_id="fixture-deployment",
                    source_witness="fixture-witness",
                    integration_isolation_verified=True,
                    credentials_verified=True,
                    observed_at=datetime.now(timezone.utc),
                ),
            )
            if prepared
            else TestingCandidateApplication(
                plan=plan, phase="abandoned", abandonment_evidence="fixture-restore-failed"
            )
        )
        self.candidate = self.candidate.model_copy(
            update={"resources_binding_sha256": "e" * 64, "application": application}
        )
        self.candidate = TestingCandidateAllocation.model_validate(self.candidate.model_dump())
        self.store_candidate(self.candidate)
        return self.candidate

    def selected(self) -> Any:
        return next(
            item for item in self.plan().candidates if item.namespace == self.candidate.namespace
        )

    def test_failed_copy_is_not_held_by_its_own_stored_records(self) -> None:
        self.with_application(prepared=False)
        candidate = self.selected()
        self.assertEqual(candidate.status, "eligible")
        self.assertEqual(candidate.references, ())
        self.assertNotIn("references", candidate.model_dump(mode="json"))

    def test_another_record_naming_the_copy_holds_it_and_names_its_owner(self) -> None:
        self.with_application(prepared=False)
        reviewed = self.selected()
        later = TestingCandidateAllocation(
            namespace=testing_candidate_namespace(
                self.candidate.product, self.candidate.context, "later-original"
            ),
            product=self.candidate.product,
            context=self.candidate.context,
            operation_id="later-original",
            intent_sha256="a" * 64,
            serving_target_id=self.candidate.compose_id,
            server_id="host",
            created_at=datetime.now(timezone.utc),
        )
        self.store_candidate(later)
        candidate = self.selected()
        self.assertNotEqual(candidate.ownership_sha256, reviewed.ownership_sha256)
        self.assertEqual(candidate.status, "held")
        self.assertIn("needed_copy_or_evidence_reference", candidate.reasons)
        self.assertEqual(
            [(row.kind, row.record_id) for row in candidate.references],
            [("testing_candidate", later.namespace)],
        )

    def test_completed_copy_releases_only_with_its_preserved_source(self) -> None:
        from unittest.mock import patch

        self.with_application(prepared=True)
        self.observation["volumes"][0].update(empty=False, reason="container_reference")
        self.observation["project"] = self.candidate.namespace
        lifecycle = {"payload": {"container_ids": ["1" * 64], "container_snapshot": "s"}}
        custody = self.enterContext(
            patch.object(self.retention, "candidate_lifecycle_custody", return_value=lifecycle)
        )
        self.assertEqual(self.selected().status, "eligible")
        self.assertTrue(custody.called)

        with patch.object(self.retention, "_source_preserved", return_value=False):
            held = self.selected()
        self.assertEqual(held.status, "held")
        self.assertIn("completed_copy_requires_disposition", held.reasons)
        self.assertFalse(held.lifecycle_snapshot_sha256)
        self.request = self.request.model_copy(
            update={"receiver_operation_id": self.candidate.operation_id}
        )
        receiver = self.selected()
        self.assertIn("receiver_candidate", receiver.reasons)
        self.assertEqual(receiver.status, "held")
        self.assertFalse(receiver.lifecycle_snapshot_sha256)

    def test_source_readback_distinguishes_skipped_unavailable_and_invalid_files(self) -> None:
        from unittest.mock import patch
        from control_plane.copy_readback_diagnostics import ReadbackPredicate
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable

        self.with_application(prepared=True)
        for error, status in (
            (FileNotFoundError(), "unavailable"),
            (NativeSourceUnavailable("Private capture mount is unavailable"), "unavailable"),
            (ValueError(), "fail"),
        ):
            with (
                self.subTest(status=status),
                patch.object(self.retention, "_source_preserved", side_effect=error),
            ):
                item = self.selected()
                self.assertEqual(item.readback[ReadbackPredicate.FILES_READ], status)
                self.assertEqual(item.status, "held")
        self.request = self.request.model_copy(
            update={"receiver_operation_id": self.candidate.operation_id}
        )
        with patch.object(self.retention, "_source_preserved", return_value=True):
            item = self.selected()
        self.assertEqual(item.readback[ReadbackPredicate.FILES_READ], "pass")
        self.assertEqual(item.resources[0].readback[ReadbackPredicate.STORAGE_CONSUMERS], "pass")
        self.assertEqual(item.status, "held")

    def test_lifecycle_readback_keeps_acquisition_distinct_from_later_inventory_hold(self) -> None:
        from unittest.mock import patch
        from control_plane.copy_readback_diagnostics import ReadbackPredicate
        from control_plane.dokploy.native_copy_stream import NativeProgramFailed

        self.with_application(prepared=True)
        self.observation["volumes"][0].update(empty=False, reason="container_reference")
        self.observation["project"] = self.candidate.namespace
        failure = NativeProgramFailed(
            "fixture refusal", predicate=ReadbackPredicate.MOUNTS, kind="refused"
        )
        with patch.object(self.retention, "candidate_lifecycle_custody", side_effect=failure):
            held = self.selected()
        self.assertEqual(held.readback[ReadbackPredicate.CUSTODY], "fail")
        self.assertEqual(held.readback[ReadbackPredicate.MOUNTS], "fail")
        self.assertNotIn("candidate_lifecycle_custody_incomplete", held.reasons)
        self.assertEqual(held.resources[0].status, "held")
        lifecycle = {"payload": {"container_ids": ["1" * 64], "container_snapshot": "s"}}

        def unavailable(*_args: Any, **kwargs: Any) -> bool:
            kwargs["references"].append(
                {
                    "reason": "provider_inventory_unavailable",
                    "kind": "project",
                    "record_id": "unreadable-project",
                }
            )
            return False

        with (
            patch.object(self.retention, "candidate_lifecycle_custody", return_value=lifecycle),
            patch.object(self.retention, "_inventory_clear", side_effect=unavailable),
        ):
            invalidated = self.selected()
        self.assertEqual(invalidated.readback[ReadbackPredicate.CUSTODY], "pass")
        self.assertIn("candidate_lifecycle_custody_incomplete", invalidated.reasons)
        self.assertFalse(invalidated.lifecycle_snapshot_sha256)
        self.assertEqual(
            invalidated.resources[0].readback[ReadbackPredicate.STORAGE_CONSUMERS], "unavailable"
        )
        self.assertIn(
            "unreadable-project", {reference.record_id for reference in invalidated.references}
        )
        self.assertEqual(invalidated.status, "held")
        self.assertEqual(invalidated.resources[0].readback[ReadbackPredicate.RESOURCES], "pass")

    def test_inventory_exception_keeps_independent_physical_holds_and_later_volumes(self) -> None:
        from unittest.mock import patch
        from control_plane.copy_readback_diagnostics import ReadbackPredicate

        row = self.observation["volumes"][0]
        row.update(source_inputs=False, referenced=False)
        self.observation["volumes"].append(
            {**row, "role": "ODOO_DATA_VOLUME", "name": self.candidate.namespace + "-data"}
        )
        self.host.return_value = ("held", "service_reference")
        with patch.object(self.retention, "_inventory_clear", side_effect=OSError):
            held = self.selected()
        self.assertEqual(held.status, "held")
        self.assertEqual(len(held.resources), 2)
        self.assertEqual(
            held.resources[0].readback[ReadbackPredicate.STORAGE_CONSUMERS], "unavailable"
        )
        self.assertEqual(
            held.resources[1].readback[ReadbackPredicate.STORAGE_CONSUMERS], "unavailable"
        )
        for resource in held.resources:
            self.assertEqual(resource.status, "held")
            self.assertEqual(resource.readback[ReadbackPredicate.NATIVE_READ], "pass")
            self.assertEqual(resource.readback[ReadbackPredicate.RESOURCES], "fail")
        self.assertIn("service_reference", {reference.reason for reference in held.references})

    def test_staged_source_and_container_reference_both_survive_the_primary_hold(self) -> None:
        row = self.observation["volumes"][0]
        row.update(source_inputs=True, referenced=True, reason="source_inputs_preserved")
        item = self.selected()
        self.assertEqual(item.status, "held")
        self.assertEqual(
            {(reference.reason, reference.record_id) for reference in item.references},
            {("source_inputs_preserved", row["name"]), ("container_reference", row["name"])},
        )
        self.assertEqual(self.host.call_args.args[2], "dry-run")

    def test_independent_holds_are_reported_without_making_the_copy_eligible(self) -> None:
        from unittest.mock import patch
        from control_plane.copy_readback_diagnostics import ReadbackPredicate

        self.with_application(prepared=True)
        self.observation["project"] = self.candidate.namespace
        row = self.observation["volumes"][0]
        row.update(empty=False, referenced=True, reason="container_reference")

        def unavailable(*_args: Any, **kwargs: Any) -> bool:
            kwargs["references"].append(
                {
                    "reason": "provider_inventory_unavailable",
                    "kind": "project",
                    "record_id": "unknown-project",
                }
            )
            return False

        with (
            patch.object(self.retention, "_source_preserved", return_value=False),
            patch.object(self.retention, "candidate_lifecycle_custody", side_effect=ValueError),
            patch.object(self.retention, "_inventory_clear", side_effect=unavailable),
        ):
            item = self.selected()
        self.assertEqual(item.status, "held")
        self.assertIn("completed_copy_requires_disposition", item.reasons)
        self.assertEqual(item.readback[ReadbackPredicate.CUSTODY], "unavailable")
        self.assertEqual(
            item.resources[0].readback[ReadbackPredicate.STORAGE_CONSUMERS], "unavailable"
        )
        self.assertIn(
            "provider_inventory_unavailable", {reference.reason for reference in item.references}
        )
        self.assertIn("container_reference", {reference.reason for reference in item.references})
        self.assertFalse(item.lifecycle_snapshot_sha256)

    def test_source_hold_cannot_hide_inventory_failure_from_lifecycle_invalidation(self) -> None:
        from unittest.mock import patch

        self.with_application(prepared=True)
        self.observation["project"] = self.candidate.namespace
        row = self.observation["volumes"][0]
        self.observation["volumes"].append(
            {**row, "role": "ODOO_LOG_VOLUME", "name": self.candidate.namespace + "-logs"}
        )
        row.update(reason="source_inputs_preserved", source_inputs=True, referenced=True)
        lifecycle = {"payload": {"container_ids": ["1" * 64], "container_snapshot": "s"}}
        with (
            patch.object(self.retention, "candidate_lifecycle_custody", return_value=lifecycle),
            patch.object(self.retention, "_inventory_clear", side_effect=[False, True]),
        ):
            item = self.selected()
        self.assertFalse(item.lifecycle_snapshot_sha256)
        self.assertTrue(all(resource.status == "held" for resource in item.resources))

    def test_completed_copy_lifecycle_is_bound_to_its_serving_web(self) -> None:
        from unittest.mock import patch
        from control_plane.dokploy import source, testing_retained_copy

        proof = {
            "container_ids": [],
            "container_snapshot": "s",
            "resource_binding": "r",
            "physical_binding": "p",
        }
        sent: list[dict[str, Any]] = []

        def operation(**kwargs: Any) -> dict[str, Any]:
            sent.append(kwargs["payload"])
            return proof

        self.enterContext(
            patch.object(testing_retained_copy, "retained_copy_operation", side_effect=operation)
        )
        self.enterContext(patch.object(source, "read_dokploy_config", return_value=("h", "t")))
        for prepared in (True, False):
            self.with_application(prepared=prepared)
            self.retention.candidate_lifecycle_custody(
                self.root, self.store, self.candidate, self.candidate.namespace
            )
            self.assertIs(sent[-1]["failed_candidate"].get("completed", False), prepared)
        configured = self.candidate.application.model_copy(  # type: ignore[union-attr]
            update={"phase": "configured", "abandonment_evidence": ""}
        )
        with self.assertRaisesRegex(ValueError, "terminal"):
            self.retention.candidate_lifecycle_custody(
                self.root,
                self.store,
                self.candidate.model_copy(update={"application": configured}),
                self.candidate.namespace,
            )

    def test_original_records_survive_and_a_selected_target_loses_eligibility(self) -> None:
        from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord

        before = self.store.read_testing_candidate(self.candidate.namespace)
        self.assertEqual(self.plan().candidates[0].status, "eligible")
        self.assertEqual(self.store.read_testing_candidate(self.candidate.namespace), before)
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=self.request.context,
                instance="testing",
                target_id=self.candidate.compose_id,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        held = self.plan().candidates[0]
        self.assertEqual(held.status, "held")
        self.assertIn("selected_target", held.reasons)

    def test_foreign_selection_refuses_without_reading_a_provider(self) -> None:
        self.request = self.request.model_copy(update={"candidates": ("lp-testing-" + "f" * 32,)})
        with self.assertRaisesRegex(ValueError, "foreign or unknown"):
            self.plan()
        self.inspect.assert_not_called()

    def test_physical_sources_and_unqualified_nonempty_copy_are_never_removed(self) -> None:
        self.observation["volumes"][0].update(empty=False, reason="source_inputs_preserved")
        resource = self.selected().resources[0]
        self.assertEqual(resource.reason, "source_inputs_preserved")
        self.assertEqual(resource.status, "held")
        self.assertEqual(self.host.call_args.args[2], "dry-run")

    def test_nonempty_unrecorded_copy_is_held(self) -> None:
        self.observation["volumes"][0]["empty"] = False
        self.assertEqual(
            self.plan().candidates[0].resources[0].reason, "only_needed_copy_unqualified"
        )
        self.assertEqual(self.selected().status, "held")
        self.assertEqual(self.host.call_args.args[2], "dry-run")

    def test_free_space_changes_do_not_invalidate_reviewed_resource_ownership(self) -> None:
        self.request = self.request.model_copy(update={"candidates": (self.candidate.namespace,)})
        self.observation["volumes"][0]["free_bytes"] = 100
        before = self.plan()
        self.observation["volumes"][0]["free_bytes"] = 90
        after = self.plan()
        self.assertEqual(before.plan_sha256, after.plan_sha256)
        self.assertNotEqual(before.candidates[0].resources, after.candidates[0].resources)
        self.observation["volumes"][0]["identity"] = "d" * 64
        self.assertNotEqual(after.plan_sha256, self.plan().plan_sha256)

    def test_provider_stop_transition_preserves_custody_but_new_work_refuses(self) -> None:
        payload = {"composeId": self.candidate.compose_id, "composeStatus": "error"}
        before = self.retention.candidate_provider_snapshot(payload)
        payload["composeStatus"] = "idle"
        self.assertEqual(before, self.retention.candidate_provider_snapshot(payload))
        payload["composeId"] = "foreign"
        self.assertNotEqual(before, self.retention.candidate_provider_snapshot(payload))
        for status in ("running", "unknown", ""):
            payload["composeStatus"] = status
            with self.assertRaisesRegex(ValueError, "active or unknown"):
                self.retention.candidate_provider_snapshot(payload)

    def test_provider_read_error_holds_one_candidate_without_a_mutation_claim(self) -> None:
        from control_plane.dokploy.api import DokployRequestFailed

        self.inspect.side_effect = DokployRequestFailed(
            method="GET", path="/api/compose.one", detail="missing", status_code=404
        )
        candidate = self.plan().candidates[0]
        self.assertEqual(candidate.status, "held")
        self.host.assert_not_called()
