"""An independent rehearsal preserves lane authority, recovery copies and unknown effects."""

from datetime import datetime, timezone
import hashlib
import os
import re
from pathlib import Path
from typing import Any, cast
from collections.abc import Callable
import unittest
from unittest.mock import Mock, PropertyMock, patch

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


def python_heredoc(script: str) -> str:
    match = re.search(r"python3\s+-\s*<<'(?P<delimiter>\w+)'\s*\n", script)
    if match is None:
        raise AssertionError("Host delivery must put its Python program on stdin")
    body, delimiter, _ = script[match.end() :].partition("\n" + match["delimiter"] + "\n")
    if not delimiter:
        raise AssertionError("Host Python heredoc is unterminated")
    return body


class RehearsalScenario(unittest.TestCase):
    database_url = ""

    def test_host_accepts_only_content_free_mismatch_evidence_for_its_exact_operation(self) -> None:
        import json
        from control_plane.contracts.canonical_json import canonical_json_sha256

        record = self.record.model_copy(update={"state": "prepared", "compose_id": "own-compose"})
        adapter = self.adapter("execute")
        operation = "original-provider-operation"
        evidence: dict[str, Any] = {
            "intent_sha256": record.intent_sha256,
            "record_binding_sha256": record.binding_sha256,
            "attempt_sha256": canonical_json_sha256({"operation_key": operation}),
            "attachment": RehearsalHostSafetyTests.attachment_mismatch_fixture(),
        }
        result = {
            "intent_sha256": record.intent_sha256,
            "state": record.state,
            "copies": [],
            "elapsed_seconds": {},
            "resources_preserved": True,
            "public_route": False,
            "business_host_frozen": False,
            "completed": False,
            "capture_attachment_validation": evidence["attachment"]["reason"],
            "attachment_mismatch": evidence,
        }
        with patch.object(
            adapter,
            "run_script",
            side_effect=lambda *a: {"rehearsal_result": json.dumps(result).encode().hex()},
        ):
            read = adapter.host(
                record, "observe", lambda: None, operation_key=operation, inspect_capture=True
            )
            self.assertEqual(read["attachment_mismatch"], evidence)
            for key in ("intent_sha256", "record_binding_sha256", "attempt_sha256"):
                original = evidence[key]
                evidence[key] = hashlib.sha256(key.encode()).hexdigest()
                with self.subTest(binding=key), self.assertRaises(RehearsalRefused):
                    adapter.host(
                        record,
                        "observe",
                        lambda: None,
                        operation_key=operation,
                        inspect_capture=True,
                    )
                evidence[key] = original
            for field in ("content", "datas", "db_datas", "raw"):
                evidence["attachment"][field] = "private contents"
                with self.subTest(content=field), self.assertRaises(RehearsalRefused):
                    adapter.host(
                        record,
                        "observe",
                        lambda: None,
                        operation_key=operation,
                        inspect_capture=True,
                    )
                del evidence["attachment"][field]
            result["completed"] = True
            with self.assertRaises(RehearsalRefused):
                adapter.host(
                    record, "observe", lambda: None, operation_key=operation, inspect_capture=True
                )

    def setUp(self) -> None:
        fixture = resources.TestingReplacementResourceHttpTests()
        fixture.database_url = self.database_url
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.store = fixture.store
        self.app = fixture.app
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
        from control_plane.dokploy import post_deploy

        comparison_limit = cast(int, spec["capture_comparison_timeout_seconds"])
        self.assertGreater(comparison_limit, 0)
        self.assertLess(
            comparison_limit, post_deploy.DEFAULT_ODOO_UPSTREAM_RESTORE_EXECUTION_TIMEOUT_SECONDS
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
        import subprocess
        import sys
        from contextlib import redirect_stdout
        from control_plane.dokploy.post_deploy import extract_odoo_post_deploy_readback_markers

        record = self.record.model_copy(update={"compose_id": "own-compose"})
        adapter = self.adapter("prepare")
        project_containers = b""

        def scheduler(
            record: OdooUpgradeRehearsalRecord, script: str, check: Callable[[], None]
        ) -> dict[str, str]:
            program = python_heredoc(script)
            output = io.StringIO()
            with (
                patch.object(sys, "argv", ["host"]),
                patch.object(Path, "home", return_value=self.root.resolve()),
                patch.object(
                    subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [], 0, stdout=project_containers, stderr=b""
                    ),
                ),
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
        self.assertTrue(result["prepare_not_started"])
        project_containers = b"existing-rehearsal-container\n"
        with patch.object(adapter, "run_script", side_effect=scheduler):
            self.assertFalse(adapter.host(record, "observe", lambda: None)["prepare_not_started"])
        project_containers = b""
        marker = (
            self.root
            / ".local/state/launchplane-rehearsals"
            / record.rehearsal_id
            / "prepare.started"
        )
        marker.parent.mkdir(parents=True)
        marker.touch()
        with patch.object(adapter, "run_script", side_effect=scheduler):
            self.assertFalse(adapter.host(record, "observe", lambda: None)["prepare_not_started"])
        marker.unlink()
        receipt = marker.with_name("prepare.json")
        receipt.mkdir()
        with patch.object(adapter, "run_script", side_effect=scheduler):
            self.assertFalse(adapter.host(record, "observe", lambda: None)["prepare_not_started"])
        with patch.object(adapter, "run_script", return_value={}):
            with self.assertRaises(RehearsalRefused):
                adapter.host(record, "observe", lambda: None)

    def test_large_manifest_survives_provider_script_materialization_and_host_readback(
        self,
    ) -> None:
        import io
        import base64
        import subprocess
        import sys
        from contextlib import redirect_stdout
        from control_plane.dokploy.post_deploy import extract_odoo_post_deploy_readback_markers
        from control_plane.dokploy.api import validate_dokploy_schedule_script

        artifact = self.record.target_artifact
        artifact = artifact.model_copy(
            update={
                "build_flags": artifact.build_flags.model_copy(
                    update={
                        "values": {
                            **artifact.build_flags.values,
                            "fixture_large_provenance": hashlib.shake_256(
                                b"large artifact"
                            ).hexdigest(350_000),
                        }
                    }
                )
            }
        )
        record = self.record.model_copy(
            update={"compose_id": "own-compose", "target_artifact": artifact}
        )
        adapter = self.adapter("prepare")

        def scheduler(
            record: OdooUpgradeRehearsalRecord, script: str, check: Callable[[], None]
        ) -> dict[str, str]:
            # Enforce the Linux materializer bound independently of the test OS.
            validate_dokploy_schedule_script({"script": script, "appName": record.rehearsal_id})
            encoded = base64.b64encode(script.encode()).decode()
            materialized = base64.b64decode(encoded, validate=True).decode()
            body = python_heredoc(materialized)
            output = io.StringIO()
            with (
                patch.object(sys, "argv", ["host"]),
                patch.object(Path, "home", return_value=self.root.resolve()),
                patch.object(
                    subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, stdout=b"", stderr=b""),
                ),
                redirect_stdout(output),
            ):
                exec(compile(body, "<provider-host>", "exec"), {})
            return extract_odoo_post_deploy_readback_markers({"logs": output.getvalue()})

        with patch.object(adapter, "run_script", side_effect=scheduler):
            result = adapter.host(record, "observe", lambda: None)
        self.assertEqual(result["intent_sha256"], record.intent_sha256)
        self.assertFalse(result["completed"])

        prepare_rehearsal_local_credentials(self.store, self.identifier, actor=self.principal)
        apply_script = adapter.host_script(record, "apply", operation_key="fixture-operation")
        validate_dokploy_schedule_script({"script": apply_script, "appName": record.rehearsal_id})
        compile(python_heredoc(apply_script), "<apply-transport>", "exec")

    def test_oversize_host_script_is_refused_before_dispatch_checks(self) -> None:
        import click
        from control_plane.dokploy.api import DOKPLOY_SCHEDULE_MATERIALIZATION_LIMIT_BYTES

        adapter = self.adapter("prepare")
        checkpoint = Mock()
        with self.assertRaises(click.ClickException):
            adapter.run_script(
                self.record, "x" * DOKPLOY_SCHEDULE_MATERIALIZATION_LIMIT_BYTES, checkpoint
            )
        checkpoint.assert_not_called()

    def test_oversize_apply_payload_releases_fresh_action_without_provider_dispatch(self) -> None:
        from control_plane.dokploy import api

        retained = self.record.model_copy(update={"state": "switched", "compose_id": "own-compose"})
        self.store.compare_and_write_odoo_upgrade_rehearsal(self.record, retained)
        adapter = self.adapter("verify")
        render = rehearsal_host_spec

        def oversized(*args: Any, **kwargs: Any) -> dict[str, Any]:
            spec = render(*args, **kwargs)
            if args[2] == "apply":
                spec["oversize_fixture"] = hashlib.shake_256(b"apply payload").hexdigest(250_000)
            return spec

        with (
            patch(
                "control_plane.odoo_upgrade_rehearsal.rehearsal_host_spec", side_effect=oversized
            ),
            patch.object(api, "dokploy_request") as provider,
        ):
            with self.assertRaises(RehearsalRefused) as refusal:
                self.run_operation(adapter, "oversize-apply")
        self.assertEqual(refusal.exception.code, RehearsalPrerequisite.PRECHECK_REFUSED)
        self.assertEqual(refusal.exception.prerequisite, RehearsalPrerequisite.STATE_UNVERIFIED)
        provider.assert_not_called()
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())

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

    def test_missing_creation_receipt_requires_recovery_without_starting_a_copy(self) -> None:
        from control_plane.provider_operations import ProviderOperationLease

        before = self.authority()
        with (
            patch(
                "control_plane.dokploy.sandbox_placement.created_placement",
                return_value=("", "", ""),
            ),
            patch("control_plane.dokploy.api.dokploy_request", return_value={}) as create,
            patch.object(OdooRehearsalAdapter, "run_script") as storage,
            self.assertRaises(RehearsalRefused) as refused,
        ):
            self.adapter("prepare").apply("original-operation", Mock(spec=ProviderOperationLease))
        self.assertEqual(refused.exception.code, RehearsalPrerequisite.RECOVERY_REQUIRED)
        create.assert_called_once()
        storage.assert_not_called()
        self.assertEqual(self.authority(), before)

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
            *,
            operation_key: str,
            prepared_script: str | None = None,
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
        prepare_rehearsal_local_credentials(self.store, self.identifier, actor=self.principal)
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

    def test_input_refusal_before_any_effect_releases_only_its_fresh_reservation(self) -> None:
        adapter = self.adapter("prepare")
        with (
            patch.object(
                OdooRehearsalAdapter,
                "inputs",
                side_effect=RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED),
            ),
            patch.object(OdooRehearsalAdapter, "host") as host,
            self.assertRaises(RehearsalRefused) as refusal,
        ):
            self.run_operation(adapter, "fresh-refusal")
        self.assertEqual(refusal.exception.prerequisite, RehearsalPrerequisite.STATE_UNVERIFIED)
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())
        host.assert_not_called()
        self.assertEqual(self.store.read_odoo_upgrade_rehearsal(self.identifier), self.record)

    def test_fresh_lock_and_existing_placement_refusals_release_without_dispatch(self) -> None:
        from contextlib import contextmanager, ExitStack
        from control_plane.dokploy import api

        @contextmanager
        def busy(*args: Any) -> Any:
            yield None

        for cause in ("lock", "placement"):
            with ExitStack() as stack:
                if cause == "lock":
                    stack.enter_context(
                        patch.object(
                            self.store, "testing_replacement_execution_lock", side_effect=busy
                        )
                    )
                else:
                    stack.enter_context(
                        patch(
                            "control_plane.dokploy.sandbox_placement.created_placement",
                            return_value=("owned", "", ""),
                        )
                    )
                provider = stack.enter_context(patch.object(api, "dokploy_request"))
                with self.assertRaises(RehearsalRefused) as refusal:
                    self.run_operation(self.adapter("prepare"), "fresh-" + cause)
            self.assertEqual(refusal.exception.code, RehearsalPrerequisite.PRECHECK_REFUSED)
            self.assertEqual(
                refusal.exception.prerequisite, RehearsalPrerequisite.RECOVERY_REQUIRED
            )
            self.assertEqual(self.store.list_held_provider_target_reservations(), ())
            self.assertEqual(self.store.read_odoo_upgrade_rehearsal(self.identifier), self.record)
            provider.assert_not_called()

    def test_partial_execute_journal_is_bound_to_its_original_key_without_writes(self) -> None:
        prepare_rehearsal_local_credentials(self.store, self.identifier, actor=self.principal)
        prepared = self.record.model_copy(update={"state": "prepared", "compose_id": "own-compose"})
        self.store.compare_and_write_odoo_upgrade_rehearsal(self.record, prepared)
        adapter = self.adapter("execute")
        with patch.object(OdooRehearsalAdapter, "host", side_effect=TimeoutError("private detail")):
            with self.assertRaises(TimeoutError):
                self.run_operation(adapter, "original-execute")
        before = self.store.list_held_provider_target_reservations()
        journal = adapter.operation_readback(prepared, "original-execute")
        self.assertEqual(journal["operation_state"], before[0].state)
        self.assertEqual(journal["provider_effect_phase"], before[0].provider_effect_phase)
        self.assertEqual(
            journal["provider_effect_started_at"], before[0].provider_effect_started_at
        )
        with self.assertRaises(RehearsalRefused):
            adapter.operation_readback(prepared, "different-key")
        from dataclasses import replace

        changed = replace(adapter, request=adapter.request.model_copy(update={"reason": "changed"}))
        with self.assertRaises(RehearsalRefused):
            changed.operation_readback(prepared, "original-execute")
        with self.assertRaises(RehearsalRefused):
            adapter.operation_readback(
                prepared.model_copy(update={"state": "upgraded"}), "original-execute"
            )
        self.assertEqual(self.store.list_held_provider_target_reservations(), before)
        self.assertEqual(self.store.read_odoo_upgrade_rehearsal(self.identifier), prepared)

    def test_failure_events_keep_process_deaths_and_project_actual_host_tracebacks(
        self,
    ) -> None:
        from control_plane.dokploy import api

        record = self.record.model_copy(update={"compose_id": "own-compose"})
        checkpoint = datetime.now(timezone.utc)
        current = {"deploymentId": "failed-source-event", "createdAt": checkpoint.isoformat()}
        older = {"deploymentId": "older-event", "createdAt": "2000-01-01T00:00:00Z"}
        import io
        import traceback

        host_fixture = RehearsalHostSafetyTests()
        host_fixture.setUp()
        self.addCleanup(host_fixture.doCleanups)
        process = Mock(stdout=io.BytesIO(b"fixture capture"))
        process.wait.return_value = 1
        process.poll.return_value = 0
        try:
            with patch("subprocess.Popen", return_value=process):
                host_fixture.host["bounded_capture"](
                    ["fixture-command"], host_fixture.fixture.root / "partial.dump", 100
                )
        except RuntimeError as error:
            log = traceback.format_exc().splitlines()
            from control_plane.odoo_upgrade_rehearsal import REHEARSAL_HOST_FILENAME

            locations = [
                {"function": frame.name, "line": frame.lineno}
                for frame in traceback.extract_tb(error.__traceback__)
                if frame.filename == REHEARSAL_HOST_FILENAME
            ]
        else:
            self.fail("An incomplete source process must refuse capture")
        log += ["PRIVATE_VALUE=do-not-export"]
        try:
            host_fixture.host["inspect_copy_runtime"](
                host_fixture.spec, expected_instance="invalid"
            )
        except RuntimeError as error:
            library_log = traceback.format_exc().splitlines()
            library_locations = [
                {"function": frame.name, "line": frame.lineno}
                for frame in traceback.extract_tb(error.__traceback__)
                if frame.filename == REHEARSAL_HOST_FILENAME
            ]
        else:
            self.fail("An unqualified runtime inspection must refuse")
        current["status"] = "error"
        killed = {**current, "deploymentId": "killed-event"}
        library = {**current, "deploymentId": "library-event"}
        with (
            patch.object(
                api,
                "fetch_dokploy_target_payload",
                return_value={"serverId": record.server_id, "appName": record.rehearsal_id},
            ),
            patch(
                "control_plane.dokploy.post_deploy._resolve_dokploy_schedule_runtime",
                return_value=("server", record.server_id, "", record.server_id),
            ),
            patch.object(
                api, "find_matching_dokploy_schedule", return_value={"scheduleId": "owned"}
            ),
            patch.object(
                api, "dokploy_request", return_value=[older] * 40 + [current, killed, library]
            ),
            patch.object(
                api, "fetch_dokploy_deployment_logs", side_effect=[log, ["Killed"], library_log]
            ) as logs,
        ):
            events, complete = self.adapter("execute").failure_events(
                record, checkpoint.isoformat()
            )
        self.assertTrue(complete)
        self.assertEqual(logs.call_count, len(events))
        self.assertEqual(events[0]["deployment_id"], current["deploymentId"])
        frames = events[0]["failure_frames"]
        assert isinstance(frames, list)
        self.assertIn(host_fixture.host["bounded_capture"].__name__, frames)
        self.assertTrue(events[0]["traceback_present"])
        self.assertEqual(events[0]["failure_locations"], locations)
        self.assertEqual(events[1]["provider_status"], killed["status"])
        self.assertFalse(events[1]["traceback_present"])
        self.assertEqual(events[2]["failure_locations"], library_locations)
        self.assertNotIn("PRIVATE_VALUE", str(events))
        self.assertNotIn("do-not-export", str(events))

    def test_failure_events_preserve_oldest_metadata_when_one_log_is_unavailable(self) -> None:
        from control_plane.dokploy import api
        from datetime import timedelta

        record = self.record.model_copy(update={"compose_id": "own-compose"})
        checkpoint = datetime.now(timezone.utc)
        candidates = [
            {
                "deploymentId": f"event-{index}",
                "createdAt": (checkpoint + timedelta(seconds=index)).isoformat(),
                "status": "error",
            }
            for index in range(2)
        ]
        unavailable = api.DokployRequestFailed(
            method="GET", path="/api/deployment.logs", detail="private", status_code=503
        )
        with (
            patch.object(
                api,
                "fetch_dokploy_target_payload",
                return_value={"serverId": record.server_id, "appName": record.rehearsal_id},
            ),
            patch(
                "control_plane.dokploy.post_deploy._resolve_dokploy_schedule_runtime",
                return_value=("server", record.server_id, "", record.server_id),
            ),
            patch.object(
                api, "find_matching_dokploy_schedule", return_value={"scheduleId": "owned"}
            ),
            patch.object(api, "dokploy_request", return_value=list(reversed(candidates))),
            patch.object(
                api,
                "fetch_dokploy_deployment_logs",
                side_effect=lambda **kwargs: (
                    (_ for _ in ()).throw(unavailable)
                    if kwargs["deployment_id"] == candidates[0]["deploymentId"]
                    else ()
                ),
            ),
        ):
            events, complete = self.adapter("execute").failure_events(
                record, checkpoint.isoformat()
            )
        self.assertFalse(complete)
        self.assertEqual(events[0]["deployment_id"], candidates[0]["deploymentId"])
        self.assertFalse(events[0]["log_available"])
        self.assertEqual(events[0]["provider_status"], candidates[0]["status"])
        self.assertTrue(events[1]["log_available"])
        self.assertGreater(len(events), 1)

    def test_native_readback_preserves_journal_and_events_when_host_is_busy(self) -> None:
        adapter = self.adapter("execute")
        journal = {
            "operation_state": "reconcile_required",
            "provider_effect_phase": "execute",
            "provider_effect_started_at": "2026-10-19T00:00:00Z",
            "_provider_operation_key": "private-key",
            "_stored_refusal_code": "",
        }
        event = {"deployment_id": "original", "provider_status": "running"}
        with (
            patch.object(OdooRehearsalAdapter, "operation_readback", return_value=dict(journal)),
            patch.object(OdooRehearsalAdapter, "failure_events", return_value=([event], True)),
            patch.object(
                OdooRehearsalAdapter,
                "host",
                return_value={
                    "completed": False,
                    "host_observation_unavailable": True,
                    "host_observation_reason": "host_busy",
                },
            ),
        ):
            result = adapter.native_readback(self.record, "original", lambda: None)
        self.assertEqual(result["failure_events_after_checkpoint"], [event])
        self.assertEqual(result["operation_state"], journal["operation_state"])
        self.assertTrue(result["host_observation_unavailable"])
        self.assertTrue(result["original_key_required"])
        for field in ("state", "copies", "custody", "raw_source_retained", "_stored_refusal_code"):
            self.assertNotIn(field, result)

        def lost() -> None:
            raise RuntimeError("fixture exclusion lost")

        with (
            patch.object(OdooRehearsalAdapter, "operation_readback", return_value=dict(journal)),
            patch.object(OdooRehearsalAdapter, "failure_events", return_value=([event], True)),
            patch.object(
                OdooRehearsalAdapter,
                "host",
                side_effect=lambda record, mode, checkpoint, **kwargs: checkpoint(),
            ),
            self.assertRaises(RuntimeError),
        ):
            adapter.native_readback(self.record, "original", lost)

    def test_prepare_qualification_availability_preserves_evidence_but_integrity_refuses(
        self,
    ) -> None:
        record = self.record.model_copy(
            update={
                "project_id": "own-project",
                "environment_id": "own-environment",
                "compose_id": "own-compose",
                "credential_bindings_sha256": "a" * 64,
            }
        )
        journal = {
            "operation_state": "reconcile_required",
            "provider_effect_phase": "prepare",
            "provider_effect_started_at": "2026-10-19T00:00:00Z",
            "_provider_operation_key": "private-key",
            "_stored_refusal_code": "",
        }
        observation: dict[str, object] = {
            "state": record.state,
            "copies": [],
            "completed": False,
            "raw_source_retained": False,
            "prepare_not_started": True,
        }
        for code in (
            RehearsalPrerequisite.ARTIFACT_UNAVAILABLE,
            RehearsalPrerequisite.CREDENTIALS_UNAVAILABLE,
            RehearsalPrerequisite.STATE_UNVERIFIED,
            RehearsalPrerequisite.PLACEMENT_UNAVAILABLE,
        ):
            with (
                patch.object(
                    OdooRehearsalAdapter, "operation_readback", return_value=dict(journal)
                ),
                patch.object(OdooRehearsalAdapter, "failure_events", return_value=([], True)),
                patch.object(OdooRehearsalAdapter, "host", return_value=dict(observation)),
                patch.object(OdooRehearsalAdapter, "inputs", side_effect=RehearsalRefused(code)),
            ):
                if code in {
                    RehearsalPrerequisite.STATE_UNVERIFIED,
                    RehearsalPrerequisite.PLACEMENT_UNAVAILABLE,
                }:
                    with self.assertRaises(RehearsalRefused):
                        self.adapter("prepare").native_readback(record, "original", lambda: None)
                else:
                    result = self.adapter("prepare").native_readback(
                        record, "original", lambda: None
                    )
                    self.assertEqual(result["recovery_reason"], code)
                    self.assertEqual(result["recovery_disposition"], "hold_unknown")
                    self.assertEqual(
                        result["provider_effect_phase"], journal["provider_effect_phase"]
                    )
                    self.assertTrue(result["original_key_required"])

    def test_host_busy_result_is_typed_and_bound_without_asserting_isolation(self) -> None:
        import json

        adapter = self.adapter("execute")
        record = self.record.model_copy(update={"compose_id": "own-compose"})
        for intent in (record.intent_sha256, "f" * 64):
            evidence = {
                "rehearsal_result": json.dumps({"intent_sha256": intent, "host_busy": True})
                .encode()
                .hex()
            }
            with patch.object(OdooRehearsalAdapter, "run_script", return_value=evidence):
                if intent != self.record.intent_sha256:
                    with self.assertRaises(RehearsalRefused):
                        adapter.host(record, "observe", lambda: None)
                else:
                    result = adapter.host(record, "observe", lambda: None)
                    self.assertTrue(result["host_observation_unavailable"])
                    for field in ("public_route", "resources_preserved", "business_host_frozen"):
                        self.assertNotIn(field, result)

    def test_native_readback_distinguishes_missing_unbound_and_no_checkpoint(self) -> None:
        adapter = self.adapter("execute")
        bound = {
            "operation_state": "reconcile_required",
            "provider_effect_phase": "",
            "provider_effect_started_at": "",
            "_provider_operation_key": "private-key",
            "_stored_refusal_code": "",
        }
        for value, reason in (
            (
                RehearsalRefused(RehearsalPrerequisite.OPERATION_UNAVAILABLE),
                "operation_journal_missing",
            ),
            (RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED), "operation_journal_unbound"),
            (bound, "no_effect_checkpoint"),
        ):
            with (
                self.subTest(reason=reason),
                patch.object(
                    OdooRehearsalAdapter,
                    "operation_readback",
                    side_effect=value if isinstance(value, Exception) else None,
                    return_value=dict(bound),
                ),
                patch.object(OdooRehearsalAdapter, "failure_events") as events,
                patch.object(OdooRehearsalAdapter, "host", return_value={"completed": False}),
            ):
                result = adapter.native_readback(self.record, "original", lambda: None)
            self.assertEqual(result["recovery_reason"], reason)
            self.assertEqual(result["recovery_disposition"], "hold_unknown")
            self.assertTrue(result["original_key_required"])
            events.assert_not_called()

        with (
            patch.object(
                OdooRehearsalAdapter,
                "operation_readback",
                side_effect=RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED),
            ),
            patch.object(OdooRehearsalAdapter, "host", return_value={"completed": True}),
        ):
            result = adapter.native_readback(self.record, "original", lambda: None)
        self.assertTrue(result["completed"])
        self.assertNotIn("operation_journal_status", result)
        self.assertNotIn("operation_state", result)

    def run_operation(
        self, adapter: OdooRehearsalAdapter, key: str
    ) -> DurableProviderOperationResult:
        from control_plane.contracts.odoo_upgrade_rehearsal import ODOO_UPGRADE_REHEARSAL_ROUTE
        from control_plane.http_routes.mutation_support import request_fingerprint

        return run_durable_provider_operation(
            store=self.store,
            scope=self.principal,
            route_path=ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=self.product),
            idempotency_key=key,
            request_fingerprint=request_fingerprint(
                {
                    "product": self.product,
                    **adapter.request.model_dump(mode="json"),
                }
            ),
            lease_owner="fixture-worker",
            response_trace_id="fixture-trace",
            adapter=adapter,
        )

    def test_prepare_partial_project_continues_only_original_effect_without_duplicate_create(
        self,
    ) -> None:
        from control_plane.dokploy import api
        from control_plane.dokploy.sandbox_placement import created_placement

        before = self.authority()
        projects: list[dict[str, Any]] = []
        calls: list[str] = []

        def provider(**kwargs: Any) -> dict[str, object]:
            path = kwargs["path"]
            calls.append(path)
            if path == "/api/project.create":
                projects.append(
                    {
                        "name": self.identifier,
                        "description": kwargs["payload"]["description"],
                        "projectId": "partial-project",
                        "environments": [
                            {
                                "name": "production",
                                "environmentId": "default-environment",
                                "isDefault": True,
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
                                        "compose",
                                    )
                                },
                            }
                        ],
                    }
                )
                # A successful provider effect whose identity was not decoded.
                return {}
            if path == "/api/environment.create":
                self.assertEqual(kwargs["payload"]["projectId"], "partial-project")
                projects[0]["environments"].append(
                    {"name": self.identifier, "environmentId": "own-environment", "compose": []}
                )
                return {"environmentId": "own-environment"}
            self.assertEqual(path, "/api/compose.create")
            return {"composeId": "own-compose"}

        adapter = self.adapter("prepare")
        with (
            patch.object(api, "fetch_dokploy_projects", side_effect=lambda **_: tuple(projects)),
            patch.object(api, "dokploy_request", side_effect=provider),
            patch.object(
                OdooRehearsalAdapter,
                "run_script",
                side_effect=lambda record, *_: {
                    "testing_resources_ok": "true",
                    "testing_resources_binding_sha256": rehearsal_resource(record).binding_sha256,
                },
            ),
            patch.object(
                OdooRehearsalAdapter,
                "host",
                return_value={
                    "state": "prepared",
                    "copies": [],
                    "elapsed_seconds": {},
                },
            ) as host,
        ):
            with self.assertRaises(RehearsalRefused):
                self.run_operation(adapter, "original-prepare")
            partial = self.store.read_odoo_upgrade_rehearsal(self.identifier)
            assert partial is not None
            self.assertFalse(partial.project_id)
            self.assertEqual(
                created_placement(self.root, self.identifier), ("partial-project", "", "")
            )
            credentials = rehearsal_credentials(self.store, self.identifier)
            host.assert_not_called()
            journal_before = self.store.list_held_provider_target_reservations()
            observed = adapter.prepare_readback(partial, "original-prepare")
            self.assertEqual(observed["recovery_disposition"], "continue_original_prepare")
            self.assertEqual(observed["provider_effect_phase"], "project_id")
            self.assertFalse(observed["completed"])
            self.assertEqual(self.store.list_held_provider_target_reservations(), journal_before)
            self.assertEqual(self.store.read_odoo_upgrade_rehearsal(self.identifier), partial)
            host.assert_not_called()
            with self.assertRaises(RehearsalRefused):
                adapter.prepare_readback(partial, "replacement-key")
            with patch.object(
                api,
                "fetch_dokploy_projects",
                side_effect=api.DokployRequestFailed(
                    method="GET",
                    path="/api/project.all",
                    detail="private failure",
                    status_code=503,
                ),
            ):
                unavailable = adapter.prepare_readback(partial, "original-prepare")
                self.assertEqual(unavailable["recovery_disposition"], "hold_unknown")
                self.assertEqual(unavailable["recovery_reason"], "observation_unavailable")
                self.assertNotIn("private failure", str(unavailable))
            with (
                patch.object(
                    api,
                    "fetch_dokploy_projects",
                    side_effect=api.DokployRequestFailed(
                        method="GET",
                        path="/api/project.all",
                        detail="private denial",
                        status_code=403,
                    ),
                ),
                self.assertRaises(api.DokployRequestFailed),
            ):
                adapter.prepare_readback(partial, "original-prepare")
            original_project = projects[0]
            for inventory in ([], [original_project, original_project]):
                projects[:] = inventory
                self.assertEqual(
                    self.run_operation(adapter, "original-prepare").status, "reconcile_required"
                )
                self.assertEqual(calls, ["/api/project.create"])
            projects[:] = [original_project]
            with patch(
                "control_plane.odoo_upgrade_rehearsal.rehearsal_credential_bindings",
                return_value="f" * 64,
            ):
                self.assertEqual(
                    self.run_operation(adapter, "original-prepare").status, "reconcile_required"
                )
                self.assertEqual(calls, ["/api/project.create"])
            with self.assertRaises(RehearsalRefused):
                adapter.partial_prepare_project(partial, "environment_id")
            default_environment = projects[0]["environments"][0]
            for broken in (
                {"applications": [{"applicationId": "foreign"}]},
                {"libsql": [{"libsqlId": "foreign"}]},
                {"compose": [{"composeId": "foreign"}]},
                {"postgres": None},
                {"isDefault": False},
            ):
                with self.subTest(broken=broken):
                    projects[0]["environments"][0] = {**default_environment, **broken}
                    held = self.run_operation(adapter, "original-prepare")
                    self.assertEqual(held.status, "reconcile_required")
                    observed = adapter.prepare_readback(partial, "original-prepare")
                    self.assertEqual(observed["recovery_reason"], "placement_not_qualified")
                    self.assertEqual(calls, ["/api/project.create"])
                    self.assertEqual(
                        self.store.read_odoo_upgrade_rehearsal(self.identifier), partial
                    )
            projects[0]["environments"][0] = default_environment
            # A different operation cannot adopt or duplicate this effect.
            other = self.run_operation(adapter, "replacement-key")
            self.assertEqual(other.status, "target_busy")
            result = self.run_operation(adapter, "original-prepare")
            self.assertEqual((result.status, result.response_status_code), ("completed", 202))
            self.assertEqual(calls.count("/api/project.create"), 1)
            self.assertEqual(calls.count("/api/environment.create"), 1)
            self.assertEqual(calls.count("/api/compose.create"), 1)
            self.assertEqual(rehearsal_credentials(self.store, self.identifier), credentials)
            self.assertEqual(self.authority(), before)
            prepared = self.store.read_odoo_upgrade_rehearsal(self.identifier)
            assert prepared is not None
            self.assertEqual(prepared.state, "prepared")

    def test_terminal_refusal_releases_the_operation_target_for_a_new_key(self) -> None:
        retained = self.record.model_copy(
            update={
                "state": "switched",
                "compose_id": "own-compose",
                "copies": (self.copy("source"), self.copy("final_source")),
            }
        )
        self.store.compare_and_write_odoo_upgrade_rehearsal(self.record, retained)
        adapter = self.adapter("verify")
        refused: dict[str, object] = {
            "refusal_code": "rehearsal_storage_capacity_insufficient",
            "completed": False,
        }
        completed: dict[str, object] = {
            "state": "verified",
            "copies": [copy.model_dump(mode="json") for copy in retained.copies]
            + [self.copy("final_upgrade").model_dump(mode="json")],
            "elapsed_seconds": {"verify": 0.1},
        }
        with (
            patch.object(adapter, "host", side_effect=[refused, completed]) as host,
            patch.object(adapter, "publish", wraps=adapter.publish) as publish,
        ):
            first = self.run_operation(adapter, "first-review")
            self.assertEqual((first.status, first.response_status_code), ("completed", 409))
            self.assertEqual(
                first.response_payload["error"], adapter.refusal_response(refused)["error"]
            )
            publish.assert_not_called()
            self.assertEqual(self.store.read_odoo_upgrade_rehearsal(self.identifier), retained)
            replay = self.run_operation(adapter, "first-review")
            self.assertEqual(replay.response_status_code, 409)
            host.assert_called_once()
            second = self.run_operation(adapter, "new-review")
            self.assertEqual((second.status, second.response_status_code), ("completed", 202))
            publish.assert_called_once()
            self.assertTrue(all(call.args[1] == "apply" for call in host.call_args_list))
            self.assertNotEqual(
                host.call_args_list[0].kwargs["operation_key"],
                host.call_args_list[1].kwargs["operation_key"],
            )

    def test_prepare_host_continuation_requires_native_not_started_proof(self) -> None:
        from control_plane.dokploy import api
        from control_plane.odoo_testing_resources import OdooRehearsalResources

        adapter = self.adapter("prepare")
        before = self.authority()
        creates = iter(
            (
                {"projectId": "own-project"},
                {"environmentId": "own-environment"},
                {"composeId": "own-compose"},
            )
        )
        modes: list[str] = []
        script = OdooRehearsalResources.script

        def resource_script(resource: OdooRehearsalResources, mode: Any) -> str:
            modes.append(mode)
            return script(resource, mode)

        def placement(*_: Any) -> tuple[str, str, str]:
            record = self.store.read_odoo_upgrade_rehearsal(self.identifier)
            assert record is not None
            return record.project_id, record.environment_id, record.compose_id

        def storage(record: OdooUpgradeRehearsalRecord, *_: Any) -> dict[str, str]:
            return {
                "testing_resources_ok": "true",
                "testing_resources_binding_sha256": rehearsal_resource(record).binding_sha256,
            }

        incomplete = {
            "completed": False,
            "state": "planned",
            "copies": [],
            "raw_source_retained": False,
        }
        with (
            patch(
                "control_plane.dokploy.sandbox_placement.created_placement", side_effect=placement
            ),
            patch.object(api, "dokploy_request", side_effect=lambda **_: next(creates)) as create,
            patch.object(
                OdooRehearsalResources, "script", autospec=True, side_effect=resource_script
            ),
            patch.object(adapter, "run_script", side_effect=storage),
            patch.object(
                adapter,
                "host",
                side_effect=api.DokployRequestFailed(
                    method="POST",
                    path="/api/schedule.update",
                    status_code=500,
                    detail="fixture materialization failure",
                ),
            ) as host,
        ):
            with self.assertRaises(api.DokployRequestFailed):
                self.run_operation(adapter, "original-prepare")
            credentials = rehearsal_credentials(self.store, self.identifier)
            host.side_effect = None
            host.return_value = incomplete
            self.assertEqual(
                self.run_operation(adapter, "original-prepare").status, "reconcile_required"
            )
            self.assertEqual(modes, ["prepare"])
            original = self.store.list_held_provider_target_reservations()[0]
            host.return_value = {**incomplete, "prepare_not_started": True}
            render = rehearsal_host_spec
            renders = 0

            def oversized(*args: Any, **kwargs: Any) -> dict[str, Any]:
                nonlocal renders
                spec = render(*args, **kwargs)
                if args[2] == "apply":
                    renders += 1
                    if renders >= refuse_on_render:
                        spec["oversize_fixture"] = hashlib.shake_256(b"continuation").hexdigest(
                            250_000
                        )
                return spec

            for refuse_on_render in (1, 2):
                renders = 0
                with patch(
                    "control_plane.odoo_upgrade_rehearsal.rehearsal_host_spec",
                    side_effect=oversized,
                ):
                    self.assertEqual(
                        self.run_operation(adapter, "original-prepare").status, "reconcile_required"
                    )
                retained = self.store.list_held_provider_target_reservations()[0]
                self.assertEqual(retained.provider_effect_phase, original.provider_effect_phase)
                self.assertEqual(
                    retained.provider_effect_started_at, original.provider_effect_started_at
                )
                if refuse_on_render == 1:
                    self.assertEqual(retained, original)
                else:
                    self.assertGreater(retained.attempt, original.attempt)
            with patch.object(
                adapter,
                "run_script",
                side_effect=RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED),
            ):
                with self.assertRaises(RehearsalRefused):
                    self.run_operation(adapter, "original-prepare")
            retained = self.store.list_held_provider_target_reservations()[0]
            self.assertEqual(retained.provider_effect_phase, original.provider_effect_phase)
            self.assertEqual(
                retained.provider_effect_started_at, original.provider_effect_started_at
            )
            self.assertEqual(self.run_operation(adapter, "replacement-key").status, "target_busy")
            host.side_effect = [
                {**incomplete, "prepare_not_started": True},
                {"state": "prepared", "copies": [], "elapsed_seconds": {}},
            ]
            result = self.run_operation(adapter, "original-prepare")
            self.assertEqual((result.status, result.response_status_code), ("completed", 202))
            self.assertEqual(modes, ["prepare", "readback", "readback", "readback"])
            self.assertEqual(create.call_count, 3)
            self.assertEqual(rehearsal_credentials(self.store, self.identifier), credentials)
            self.assertEqual(self.authority(), before)

    def assert_prepare_size_refusal_retains_previous_phase(self, stage: str) -> None:
        from control_plane.dokploy import api
        from control_plane.odoo_testing_resources import OdooRehearsalResources

        adapter = self.adapter("prepare")
        creates = iter(
            (
                {"projectId": "own-project"},
                {"environmentId": "own-environment"},
                {"composeId": "own-compose"},
            )
        )
        storage_script = OdooRehearsalResources.script
        render = rehearsal_host_spec
        observed: dict[str, Any] = {}
        write_checkpoint = self.store.checkpoint_mutation_provider_effect

        def track_checkpoint(**kwargs: Any) -> Any:
            result = write_checkpoint(**kwargs)
            observed[kwargs["effect_phase"]] = result.record
            return result

        def resource_script(resource: OdooRehearsalResources, mode: Any) -> str:
            return (
                "x" * api.DOKPLOY_SCHEDULE_MATERIALIZATION_LIMIT_BYTES
                if stage == "storage"
                else storage_script(resource, mode)
            )

        def host_spec(*args: Any, **kwargs: Any) -> dict[str, Any]:
            spec = render(*args, **kwargs)
            spec["oversize_fixture"] = hashlib.shake_256(b"prepare apply").hexdigest(250_000)
            return spec

        def storage(record: OdooUpgradeRehearsalRecord, *_: Any) -> dict[str, str]:
            return {
                "testing_resources_ok": "true",
                "testing_resources_binding_sha256": rehearsal_resource(record).binding_sha256,
            }

        with (
            patch(
                "control_plane.dokploy.sandbox_placement.created_placement",
                return_value=("", "", ""),
            ),
            patch.object(api, "dokploy_request", side_effect=lambda **_: next(creates)) as create,
            patch.object(
                OdooRehearsalResources, "script", autospec=True, side_effect=resource_script
            ),
            patch(
                "control_plane.odoo_upgrade_rehearsal.rehearsal_host_spec", side_effect=host_spec
            ),
            patch.object(adapter, "run_script", side_effect=storage) as dispatch,
            patch.object(
                self.store, "checkpoint_mutation_provider_effect", side_effect=track_checkpoint
            ),
        ):
            self.assertEqual(
                self.run_operation(adapter, "original-prepare").status, "reconcile_required"
            )
        retained = self.store.list_held_provider_target_reservations()[0]
        expected_phase = "compose_id" if stage == "storage" else "independent_storage"
        self.assertEqual(retained.provider_effect_phase, expected_phase)
        self.assertEqual(
            retained.provider_effect_started_at, observed[expected_phase].provider_effect_started_at
        )
        self.assertEqual(dispatch.call_count, 0 if stage == "storage" else 1)
        self.assertEqual(create.call_count, 3)

    def test_storage_size_refusal_retains_placement_checkpoint(self) -> None:
        self.assert_prepare_size_refusal_retains_previous_phase("storage")

    def test_host_size_refusal_retains_storage_checkpoint(self) -> None:
        self.assert_prepare_size_refusal_retains_previous_phase("host")

    def test_lost_refusal_response_reconciles_only_the_same_operation_key(self) -> None:
        retained = self.record.model_copy(update={"state": "switched", "compose_id": "own-compose"})
        self.store.compare_and_write_odoo_upgrade_rehearsal(self.record, retained)
        adapter = self.adapter("verify")
        original = ""

        def host(
            record: OdooUpgradeRehearsalRecord,
            mode: str,
            checkpoint: Callable[[], None],
            *,
            operation_key: str,
            prepared_script: str | None = None,
        ) -> dict[str, object]:
            nonlocal original
            checkpoint()
            if mode == "apply":
                original = operation_key
                raise TimeoutError("Refusal response was lost")
            self.assertEqual(operation_key, original)
            return {"refusal_code": "rehearsal_precheck_refused", "completed": False}

        with (
            patch.object(adapter, "host", side_effect=host) as transport,
            patch.object(adapter, "publish") as publish,
        ):
            with self.assertRaises(TimeoutError):
                self.run_operation(adapter, "lost-review")
            result = self.run_operation(adapter, "lost-review")
            self.assertEqual((result.status, result.response_status_code), ("adopted", 409))
            self.assertEqual(
                [call.args[1] for call in transport.call_args_list], ["apply", "observe"]
            )
            publish.assert_not_called()
        self.assertEqual(self.store.read_odoo_upgrade_rehearsal(self.identifier), retained)


class RehearsalHostSafetyTests(unittest.TestCase):
    def test_failed_child_preserves_exit_and_redacted_error_for_readback(self) -> None:
        import subprocess

        spec, root, _ = self.storage_fixture(
            {"database_bytes": 40, "filestore_bytes": 80, "staging_bytes": 20, "reserve_bytes": 10}
        )
        (root / "verify.started").touch()
        (root / ".execution.lock").touch()
        error_text = "psycopg2.errors.UndefinedColumn: column legacy_code does not exist"
        secret = "fixture-password-do-not-export"
        with patch.object(
            self.host["subprocess"],
            "run",
            side_effect=[
                subprocess.CompletedProcess([], 1, b"", b"transient startup error"),
                subprocess.CompletedProcess(
                    [], 7, b"initialization stopped", (error_text + "\nPASSWORD=" + secret).encode()
                ),
                subprocess.CompletedProcess([], 9, b"", b"cleanup also failed"),
            ],
        ):
            with self.assertRaises(self.host["RehearsalCommandFailure"]) as failure:
                with self.host["command_failure_scope"](spec, root):
                    try:
                        self.host["command"](["transient-child"])
                    except self.host["RehearsalCommandFailure"]:
                        pass
                    try:
                        self.host["command"](["fixture-child"])
                    finally:
                        self.host["command"](["cleanup-child"])
        self.assertEqual(failure.exception.diagnostic["exit_status"], 9)
        diagnostic = self.host["read_command_failure"](spec, root)
        self.assertEqual(
            diagnostic["step"],
            self.test_failed_child_preserves_exit_and_redacted_error_for_readback.__name__,
        )
        self.assertEqual(diagnostic["exit_status"], 7)
        self.assertIn(error_text, diagnostic["error_text"])
        self.assertIn("initialization stopped", diagnostic["error_text"])
        self.assertNotIn("transient startup error", diagnostic["error_text"])
        self.assertNotIn(secret, str(diagnostic))
        spec.update(mode="observe", inspect_capture=True)
        observed = self.run_host(spec)
        self.assertEqual(observed["child_failure"]["exit_status"], diagnostic["exit_status"])
        self.assertEqual(observed["child_failure"]["error_text"], diagnostic["error_text"])
        self.assertEqual(observed["child_failure"]["attempt_sha256"], spec["attempt_sha256"])
        self.assertFalse(observed["completed"])
        original = spec["attempt_sha256"]
        spec["attempt_sha256"] = hashlib.sha256(b"another operation").hexdigest()
        with self.assertRaises(RuntimeError):
            self.run_host(spec)
        spec["attempt_sha256"] = original
        path = self.host["command_failure_path"](spec, root)
        path.rename(root / "aliased-diagnostic")
        path.symlink_to(root / "aliased-diagnostic")
        with self.assertRaises(OSError):
            self.run_host(spec)

    def test_optional_log_failures_preserve_child_error_and_custody(self) -> None:
        import json

        spec, root, containers = self.storage_fixture(
            {"database_bytes": 40, "filestore_bytes": 80, "staging_bytes": 20, "reserve_bytes": 10}
        )
        spec.update(mode="observe", action="execute", inspect_capture=True)
        spec["record"]["state"] = "prepared"
        (root / "execute.started").touch()
        (root / ".execution.lock").touch()
        source = self.host["database_names"](spec)["source"]
        (root / source).mkdir()
        (root / source / "receipt.json").write_text(json.dumps({"database": source}))
        self.host["validate_snapshots"] = Mock()
        self.host["retain_command_failure"](
            spec,
            root,
            {"step": "clone", "exit_status": 1, "error_text": "clone could not complete"},
        )
        containers["web"][1]["State"]["Running"] = False
        for failure in ("inspection", "writer", "logs"):
            with self.subTest(failure=failure):
                self.host["inspection"] = Mock(
                    return_value=containers,
                    side_effect=RuntimeError("source runtime is still selected")
                    if failure == "inspection"
                    else None,
                )
                containers["web"][1]["State"]["Running"] = failure == "writer"
                self.host["retained_upgrade_logs"] = Mock(side_effect=ValueError("log unavailable"))
                observed = self.run_host(spec)
                self.assertEqual(
                    observed["child_failure"]["error_text"], "clone could not complete"
                )
                self.assertTrue(observed["custody"]["effect_started"])
                self.assertTrue(observed["retained_upgrade_logs_unavailable"])
                self.assertFalse(observed["completed"])

    def test_diagnostic_bounds_redact_known_secrets_and_credential_forms(self) -> None:
        from control_plane._odoo_rehearsal_diagnostics import REHEARSAL_DIAGNOSTIC_TEXT_LIMIT

        error = "ValueError: missing legacy field"
        text = (
            "x" * (REHEARSAL_DIAGNOSTIC_TEXT_LIMIT * 2)
            + "\n--db_password cli-value\nPASSWORD='quoted value'\nBearer bearer-value"
            + "\npostgresql://user:uri-value@database/name\nstandalone-known-secret\n"
            + error
        )
        redacted = self.host["diagnostic_text"](text, ["standalone-known-secret"])
        self.assertLessEqual(len(redacted), REHEARSAL_DIAGNOSTIC_TEXT_LIMIT)
        self.assertIn(error, redacted)
        for secret in (
            "cli-value",
            "quoted value",
            "bearer-value",
            "uri-value",
            "standalone-known-secret",
        ):
            self.assertNotIn(secret, redacted)
        unicode_text = self.host["diagnostic_text"]("🙂" * (REHEARSAL_DIAGNOSTIC_TEXT_LIMIT * 2))
        self.assertLessEqual(len(unicode_text.encode()), REHEARSAL_DIAGNOSTIC_TEXT_LIMIT)
        self.assertEqual(
            self.host["diagnostic_text"]("password authentication failed"),
            "password authentication failed",
        )
        self.assertNotIn(
            "client@example.invalid",
            self.host["diagnostic_text"](
                "DETAIL: Key (login)=(client@example.invalid) already exists"
            ),
        )

    def test_retained_logs_are_bounded_read_only_and_exclude_symlinks(self) -> None:
        from control_plane._odoo_rehearsal_diagnostics import REHEARSAL_DIAGNOSTIC_TEXT_LIMIT

        spec, root, containers = self.storage_fixture(
            {"database_bytes": 40, "filestore_bytes": 80, "staging_bytes": 20, "reserve_bytes": 10}
        )
        (root / "verify.started").touch()
        mount = next(
            item
            for item in containers["script-runner"][1]["Mounts"]
            if item["Name"].endswith("-logs")
        )
        logs = Path(self.fixture.volumes[mount["Name"]]["Mountpoint"])
        mount["Source"] = str(logs)
        secret = "known-runtime-password"
        containers["script-runner"][1]["Config"]["Env"].append("ODOO_DB_PASSWORD=" + secret)
        path = logs / "odoo.log"
        content = (
            b"old output\n" * REHEARSAL_DIAGNOSTIC_TEXT_LIMIT
            + ("RuntimeError: unknown upgrade module\n" + secret).encode()
        )
        path.write_bytes(content)
        before = path.stat()
        private = self.fixture.root / "private.log"
        private.write_text("must never be read")
        (logs / "alias.log").symlink_to(private)
        with patch.dict(
            self.host,
            diagnostic_process_output=Mock(return_value=(True, b"retained runtime error")),
        ):
            result = self.host["retained_upgrade_logs"](spec, containers, root)
        self.assertTrue(all(log["available"] for log in result["runtime"]))
        self.assertEqual(len(result["files"]), 1)
        self.assertIn("RuntimeError: unknown upgrade module", result["files"][0]["tail"])
        self.assertNotIn(secret, str(result))
        self.assertNotIn("must never be read", str(result))
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertLessEqual(len(result["files"][0]["tail"]), REHEARSAL_DIAGNOSTIC_TEXT_LIMIT)

    def test_native_subprocess_readers_bound_and_preserve_failure(self) -> None:
        import sys

        read = self.host["diagnostic_process_output"]
        self.assertEqual(
            read([sys.executable, "-c", "print('retained error')"]), (True, b"retained error\n")
        )
        available, _ = read([sys.executable, "-c", "raise SystemExit(7)"])
        self.assertFalse(available)
        available, _ = read(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x'*(2*1024*1024))"]
        )
        self.assertFalse(available)
        spec, root, _ = self.storage_fixture(
            {"database_bytes": 40, "filestore_bytes": 80, "staging_bytes": 20, "reserve_bytes": 10}
        )
        (root / "verify.started").touch()
        with self.assertRaises(self.host["RehearsalCommandFailure"]):
            with self.host["command_failure_scope"](spec, root):
                self.host["bounded_capture"](
                    [
                        sys.executable,
                        "-c",
                        "import sys;sys.stdout.write('private capture bytes');sys.stderr.write('capture child error');sys.exit(7)",
                    ],
                    root / "partial.dump",
                    1024,
                )
        diagnostic = self.host["read_command_failure"](spec, root)
        self.assertEqual(diagnostic["exit_status"], 7)
        self.assertIn("capture child error", diagnostic["error_text"])
        self.assertNotIn("private capture bytes", str(diagnostic))

    @staticmethod
    def attachment_mismatch_fixture() -> dict[str, Any]:
        checksum = hashlib.sha1(b"expected attachment").hexdigest()
        return {
            "reason": "attachment_size_mismatch",
            "store_fname": checksum[:2] + "/" + checksum,
            "name": "Service invoice.pdf",
            "res_model": "account.move",
            "res_id": 42,
            "mimetype": "application/pdf",
            "database_size": len(b"expected attachment"),
            "database_checksum": checksum,
            "file_size": len(b"private contents"),
            "file_checksum": hashlib.sha1(b"private contents").hexdigest(),
            "file_status": "measured",
            "truncated_fields": [],
        }

    def test_failing_capture_names_its_attachment_without_content_or_another_scan(self) -> None:
        from control_plane._odoo_rehearsal_attachment_validation import (
            ATTACHMENT_METADATA_LIMITS,
            ATTACHMENT_MISMATCH_RECEIPT_NAME,
        )
        import io
        import json
        import tarfile

        spec, root, containers = self.storage_fixture(
            {
                "database_bytes": 4096,
                "filestore_bytes": 20480,
                "staging_bytes": 20,
                "reserve_bytes": 10,
            }
        )
        spec.update(
            action="execute",
            source={"ODOO_UPSTREAM_DB_USER": "fixture", "ODOO_UPSTREAM_DB_NAME": "fixture"},
        )
        spec["record"].update(
            state="prepared",
            source_odoo_version="18.0",
            source_binding_sha256=hashlib.sha256(b"source").hexdigest(),
        )
        self.host["rehearsal_source_reader"] = Mock(return_value=([], "fixture", "/fixture"))
        self.host["sanitize_capture"] = Mock()
        self.host["sql"] = Mock(return_value="18.0")
        self.host["docker"] = Mock()
        mismatch = self.attachment_mismatch_fixture()
        for field, limit in ATTACHMENT_METADATA_LIMITS.items():
            mismatch[field] = "\U0001f680" * limit

        def capture_file(arguments: list[str], path: Path, bound: int) -> None:
            if path.suffix == ".dump":
                path.write_bytes(b"private database contents")
            else:
                with tarfile.open(path, "w") as archive:
                    member = tarfile.TarInfo(mismatch["store_fname"])
                    member.size = len(b"private contents")
                    archive.addfile(member, io.BytesIO(b"private contents"))

        self.host["bounded_capture"] = capture_file
        probe = Mock(return_value={"reason": mismatch["reason"], "mismatch": mismatch})
        self.host["attachment_validation_payload"] = probe
        (root / "execute.started").touch(mode=0o600)
        (root / ".execution.lock").touch(mode=0o600)
        with self.assertRaises(RuntimeError):
            self.host["capture"](spec, root, containers, self.fixture.database)
        saved = root / ATTACHMENT_MISMATCH_RECEIPT_NAME
        evidence = json.loads(saved.read_text())
        self.assertEqual(evidence["attachment"], mismatch)
        self.assertEqual(evidence["attempt_sha256"], spec["attempt_sha256"])
        self.assertEqual(evidence["record_binding_sha256"], spec["record_binding_sha256"])
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("private contents", saved.read_text())
        self.assertNotIn("private database contents", saved.read_text())
        probe.assert_called_once()
        spec.update(mode="observe", inspect_capture=True)
        self.host["capture_attachment_observation"] = Mock(side_effect=AssertionError("extra scan"))
        self.host["inspection"] = Mock(side_effect=AssertionError("runtime lookup"))
        saved_before_readback = saved.read_bytes()
        observed = self.run_host(spec)
        self.assertEqual(observed["attachment_mismatch"], evidence)
        self.assertFalse(observed["completed"])
        self.assertEqual(observed["copies"], [])
        self.assertEqual(saved.read_bytes(), saved_before_readback)
        for field in ("attempt_sha256", "record_binding_sha256"):
            with self.subTest(binding=field):
                changed = {**spec, field: hashlib.sha256(field.encode()).hexdigest()}
                with self.assertRaises(RuntimeError):
                    self.run_host(changed)
        clean_root = root / "clean-fixture"
        clean_root.mkdir()
        probe.return_value = {"reason": "verified"}
        self.assertEqual(
            self.host["capture"](spec, clean_root, containers, self.fixture.database),
            clean_root / "raw-source",
        )
        self.assertFalse((clean_root / ATTACHMENT_MISMATCH_RECEIPT_NAME).exists())
        self.assertTrue((clean_root / "capture.json").is_file())
        self.assertEqual(
            (clean_root / "raw-source/filestore" / mismatch["store_fname"]).read_bytes(),
            b"private contents",
        )

    def test_validator_reports_compared_facts_for_size_and_checksum_mismatches(self) -> None:
        from control_plane.contracts.odoo_upgrade_rehearsal import RehearsalAttachmentFacts
        from contextlib import nullcontext, redirect_stdout
        import io
        import json
        import sys
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from control_plane._odoo_rehearsal_attachment_validation import (
            ATTACHMENT_METADATA_LIMITS,
            ATTACHMENT_VALIDATION_PROGRAM,
        )

        program: dict[str, Any] = {"__name__": "fixture"}
        exec(compile(ATTACHMENT_VALIDATION_PROGRAM, "fixture", "exec"), program)
        expected = self.attachment_mismatch_fixture()
        root = self.fixture.root / "attachment-evidence"
        path = root / expected["store_fname"]
        path.parent.mkdir(parents=True)
        row = (
            expected["store_fname"],
            expected["database_checksum"],
            expected["database_size"],
            expected["name"],
            expected["res_model"],
            expected["res_id"],
            expected["mimetype"],
        )
        for content, reason in (
            (b"private contents", "attachment_size_mismatch"),
            (b"x" * expected["database_size"], "attachment_hash_mismatch"),
        ):
            with self.subTest(reason=reason):
                path.write_bytes(content)
                mismatch: dict[str, Any] = {}
                self.assertEqual(
                    program["validate_attachment_files"]([row], root, None, mismatch=mismatch),
                    reason,
                )
                self.assertEqual(
                    mismatch,
                    {
                        **expected,
                        "reason": reason,
                        "file_size": len(content),
                        "file_checksum": hashlib.sha1(content).hexdigest(),
                    },
                )
                self.assertNotIn(content.decode(), json.dumps(mismatch))
                self.assertEqual(path.read_bytes(), content)
        path.write_bytes(b"expected attachment")
        clean: dict[str, Any] = {}
        self.assertEqual(
            program["validate_attachment_files"]([row], root, None, mismatch=clean), "verified"
        )
        self.assertEqual(clean, {})
        path.write_bytes(b"private contents")
        for record_id in (-7, None):
            mismatch = {}
            signed_row = (*row[:5], record_id, row[6])
            self.assertEqual(
                program["validate_attachment_files"]([signed_row], root, None, mismatch=mismatch),
                expected["reason"],
            )
            self.assertEqual(mismatch["res_id"], record_id)
            self.assertEqual(
                RehearsalAttachmentFacts.model_validate(mismatch).model_dump(), mismatch
            )
        for field in ("name", "res_model", "mimetype"):
            long_value = "\U0001f680" * (ATTACHMENT_METADATA_LIMITS[field] + 1)
            details = {**expected, field: long_value}
            long_row = (
                details["store_fname"],
                details["database_checksum"],
                details["database_size"],
                details["name"],
                details["res_model"],
                details["res_id"],
                details["mimetype"],
            )
            path.write_bytes(b"private contents")
            mismatch = {}
            self.assertEqual(
                program["validate_attachment_files"]([long_row], root, None, mismatch=mismatch),
                expected["reason"],
            )
            self.assertTrue(long_value.startswith(mismatch[field]))
            self.assertLess(len(mismatch[field]), len(long_value))
            self.assertEqual(mismatch["truncated_fields"], [field])
            self.assertEqual(
                RehearsalAttachmentFacts.model_validate(mismatch).model_dump(), mismatch
            )
        metadata = path.stat()
        changed = SimpleNamespace(
            st_size=metadata.st_size,
            st_mtime_ns=metadata.st_mtime_ns,
            st_ctime_ns=metadata.st_ctime_ns + 1,
        )
        real_fstat = program["os"].fstat
        for status in ("changed", "unreadable"):
            mismatch = {}
            reads = 0

            def observed_metadata(descriptor: int) -> Any:
                nonlocal reads
                actual = real_fstat(descriptor)
                if actual.st_ino != metadata.st_ino:
                    return actual
                reads += 1
                return metadata if reads == 1 else changed

            with (
                patch.object(
                    program["os"],
                    "fstat",
                    side_effect=observed_metadata if status == "changed" else real_fstat,
                ),
                patch.object(
                    program["os"], "fdopen", side_effect=OSError("private filesystem error")
                )
                if status == "unreadable"
                else nullcontext(),
            ):
                self.assertEqual(
                    program["validate_attachment_files"]([row], root, None, mismatch=mismatch),
                    expected["reason"],
                )
            self.assertEqual(mismatch["file_status"], status)
            self.assertIsNone(mismatch["file_checksum"])
            self.assertEqual(mismatch["name"], expected["name"])
            self.assertEqual(
                RehearsalAttachmentFacts.model_validate(mismatch).model_dump(), mismatch
            )
        # Exercise the emitted CLI too: its normal validate call must carry
        # the failed row into the payload, not only the lower-level helper.
        path.write_bytes(b"private contents")
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value.__iter__.side_effect = lambda: iter(
            [row]
        )
        output = io.StringIO()
        with (
            patch.dict(sys.modules, {"psycopg2": SimpleNamespace(connect=lambda **_: connection)}),
            patch.dict(
                os.environ,
                {
                    "ODOO_DB_NAME": root.name,
                    "ODOO_DB_USER": "fixture",
                    "ODOO_DB_PASSWORD": "private login",
                },
            ),
            patch.object(sys, "argv", ["validator", "validate"]),
            patch("pathlib.Path", side_effect=lambda _: root.parent),
            redirect_stdout(output),
        ):
            exec(
                compile(ATTACHMENT_VALIDATION_PROGRAM, "fixture-cli", "exec"),
                {"__name__": "__main__"},
            )
        payload = json.loads(bytes.fromhex(output.getvalue().strip().split("=", 1)[1]))
        self.assertEqual(payload, {"reason": expected["reason"], "mismatch": expected})
        self.assertNotIn("private contents", str(payload))
        self.assertNotIn("private login", str(payload))
        self.assertTrue(connection.set_session.call_args.kwargs["readonly"])
        self.assertEqual(
            sum(bool(call.kwargs.get("name")) for call in connection.cursor.call_args_list), 1
        )

    def test_captured_table_decoder_bounds_output_and_closes_its_process(self) -> None:
        import subprocess
        import sys
        import time

        _, root, _ = self.storage_fixture(
            {
                "database_bytes": 4096,
                "filestore_bytes": 10240,
                "staging_bytes": 20,
                "reserve_bytes": 10,
            }
        )
        dump = root / "dump-fixture"
        dump.write_bytes(b"fixture stdin")
        original_popen = subprocess.Popen
        processes = []
        output = b"COPY fixture\nrow\n"
        exit_code = 0
        stall = False
        output_fixture = root / "decoder-output-fixture"

        def decoder_process(arguments: list[str], **kwargs: Any) -> Any:
            self.assertFalse(any(arg.startswith(("-d", "--dbname")) for arg in arguments))
            output_fixture.write_bytes(output)
            program = (
                "import sys,time; from pathlib import Path; assert sys.stdin.buffer.read()==b'fixture stdin'; "
                f"sys.stdout.buffer.write(Path({str(output_fixture)!r}).read_bytes()); "
                f"time.sleep(5 if {stall!r} else 0); sys.exit({exit_code})"
            )
            process = original_popen([sys.executable, "-c", program], **kwargs)
            processes.append(process)
            return process

        decode = self.host["captured_attachment_table_chunks"]
        with patch.object(self.host["subprocess"], "Popen", side_effect=decoder_process):
            with dump.open("rb") as stream:
                self.assertEqual(
                    b"".join(decode(stream, self.inspect(), time.monotonic() + 10, len(output))),
                    output,
                )
            with dump.open("rb") as stream, self.assertRaises(ValueError):
                list(decode(stream, self.inspect(), time.monotonic() + 10, len(output) - 1))
            exit_code = 1
            with dump.open("rb") as stream, self.assertRaises(ValueError):
                list(decode(stream, self.inspect(), time.monotonic() + 10, len(output)))
            exit_code = 124  # GNU timeout reports its expired child this way.
            with dump.open("rb") as stream, self.assertRaises(TimeoutError):
                list(decode(stream, self.inspect(), time.monotonic() + 10, len(output)))
            with dump.open("rb") as stream, self.assertRaises(TimeoutError):
                list(decode(stream, self.inspect(), time.monotonic() - 1, len(output)))
            exit_code = 137  # An unexplained hard kill is not proof of timeout.
            with dump.open("rb") as stream, self.assertRaises(ValueError):
                list(decode(stream, self.inspect(), time.monotonic() + 10, len(output)))
            stall = True
            with dump.open("rb") as stream, self.assertRaises(TimeoutError):
                list(decode(stream, self.inspect(), time.monotonic() + 0.1, len(output)))
            stall = False
            exit_code = 0
            checksum = hashlib.sha1(b"attachment fixture").hexdigest()
            name = checksum[:2] + "/" + checksum
            reference = {"name": name, "checksum": checksum, "size": 19}
            output = (
                b"COPY public.ir_attachment (name, store_fname, checksum, file_size) FROM stdin;\n"
                + b"x" * (2 * 1024**2)
                + b"\t\\N\t\\N\t0\n"
                + ("target\t" + name + "\t" + checksum + "\t19\n\\.\n").encode()
            )
            with dump.open("rb") as stream:
                self.assertTrue(
                    self.host["captured_attachment_reference_present"](
                        decode(stream, self.inspect(), time.monotonic() + 10, len(output)),
                        reference,
                    )
                )
        self.assertTrue(all(process.poll() is not None for process in processes))
        self.assertEqual(dump.read_bytes(), b"fixture stdin")

    def test_captured_attachment_reference_requires_verified_original_dump(self) -> None:
        import json

        spec, root, _ = self.storage_fixture(
            {
                "database_bytes": 4096,
                "filestore_bytes": 10240,
                "staging_bytes": 20,
                "reserve_bytes": 10,
            }
        )
        binding = hashlib.sha256(b"source fixture").hexdigest()
        spec["record"]["source_binding_sha256"] = binding
        raw = root / "raw-source"
        raw.mkdir()
        dump = raw / "database.dump"
        dump.write_bytes(b"custom dump fixture")
        checksum = hashlib.sha1(b"original attachment").hexdigest()
        name = checksum[:2] + "/" + checksum
        reference = {
            "name": name,
            "checksum": checksum,
            "size": 19,
            "copied_size": 3,
            "copied_sha1": hashlib.sha1(b"bad").hexdigest(),
        }
        receipt = {
            "source_binding_sha256": binding,
            "database_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
            "filestore_sha256": hashlib.sha256(b"tar fixture").hexdigest(),
        }
        (root / "capture.json").write_text(json.dumps(receipt))
        header = (
            b"COPY public.ir_attachment (id, name, file_size, checksum, store_fname) FROM stdin;\n"
        )
        row = ("42\tprivate\\tfixture\\nname\t19\t" + checksum + "\t" + name + "\n").encode()
        lines = [b"-- dump fixture\n", header, row, b"\\.\n"]
        decoder = Mock(return_value=iter(lines))
        compare = self.host["compare_captured_database_reference"]
        before = {p: p.read_bytes() for p in (root / "capture.json", dump)}
        with patch.dict(self.host, {"captured_attachment_table_chunks": decoder}):
            self.assertEqual(compare(spec, root, self.inspect(), reference), "reference_present")
            decoder.return_value = iter([header, row.replace(b"\t19\t", b"\t20\t"), b"\\.\n"])
            self.assertEqual(compare(spec, root, self.inspect(), reference), "reference_not_found")
            large_row = b"41\t" + b"x" * (2 * 1024**2) + b"\t0\t\\N\t\\N\n"
            sql_output = header + large_row + row + b"\\.\n"
            self.assertTrue(
                self.host["captured_attachment_reference_present"](
                    (sql_output[i : i + 65536] for i in range(0, len(sql_output), 65536)), reference
                )
            )
            self.assertTrue(
                self.host["captured_attachment_reference_present"](
                    (bytes([value]) for value in header + row + b"\\.\n"), reference
                )
            )
            decoder.return_value = iter([header, row])
            self.assertEqual(compare(spec, root, self.inspect(), reference), "unavailable")
            decoder.return_value = iter([header, b"too\tfew\n", b"\\.\n"])
            self.assertEqual(compare(spec, root, self.inspect(), reference), "unavailable")
            decoder.side_effect = TimeoutError("private reference must not escape")
            self.assertEqual(compare(spec, root, self.inspect(), reference), "incomplete")
            decoder.side_effect = OSError("private transport detail")
            self.assertEqual(compare(spec, root, self.inspect(), reference), "unavailable")
            decoder.reset_mock(side_effect=True)
            dump.write_bytes(b"changed dump")
            self.assertEqual(compare(spec, root, self.inspect(), reference), "unavailable")
            decoder.assert_not_called()
        dump.write_bytes(before[dump])
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_retained_archive_comparison_preserves_custody_and_requires_its_receipt(self) -> None:
        import io
        import json
        import tarfile

        spec, root, _ = self.storage_fixture(
            {
                "database_bytes": 40,
                "filestore_bytes": 102400,
                "staging_bytes": 20,
                "reserve_bytes": 10,
            }
        )
        binding = hashlib.sha256(b"source fixture").hexdigest()
        spec["record"]["source_binding_sha256"] = binding
        raw = root / "raw-source"
        raw.mkdir()
        content = b"original attachment fixture"
        checksum = hashlib.sha1(content).hexdigest()
        name = checksum[:2] + "/" + checksum
        reference = {
            "name": name,
            "checksum": checksum,
            "size": len(content),
            "copied_size": 3,
            "copied_sha1": hashlib.sha1(b"bad").hexdigest(),
        }

        def archive(rows: list[tuple[str, bytes]], *, extended: bool = False) -> None:
            with tarfile.open(
                raw / "filestore.tar",
                "w",
                format=tarfile.PAX_FORMAT if extended else tarfile.USTAR_FORMAT,
            ) as output:
                for member_name, data in rows:
                    member = tarfile.TarInfo(member_name)
                    member.size = len(data)
                    if extended:
                        member.pax_headers = {"comment": "fixture metadata"}
                    output.addfile(member, io.BytesIO(data))
            (root / "capture.json").write_text(
                json.dumps(
                    {
                        "source_binding_sha256": binding,
                        "database_sha256": hashlib.sha256(b"dump fixture").hexdigest(),
                        "filestore_sha256": hashlib.sha256(
                            (raw / "filestore.tar").read_bytes()
                        ).hexdigest(),
                    }
                )
            )

        compare = self.host["compare_retained_attachment"]
        limit = spec["capture_comparison_timeout_seconds"]
        budget = self.host["capture_comparison_budget"]
        self.assertGreater(budget(100 * 1024**3, 10**9), budget(1024, 10**9))
        self.assertLessEqual(budget(100 * 1024**3, limit), limit)
        for data, expected in (
            (content, "capture_attachment_matches_reference_copy_differs"),
            (b"bad", "capture_attachment_matches_copy_reference_differs"),
            (b"different damaged bytes", "capture_attachment_differs_from_copy_and_reference"),
        ):
            archive([("./" + name, data)])
            before = {p: p.read_bytes() for p in (root / "capture.json", raw / "filestore.tar")}
            self.assertEqual(compare(spec, root, reference), expected)
            self.assertEqual({p: p.read_bytes() for p in before}, before)
        archive([("./" + name, content)])
        archive([("./" + name, content)], extended=True)
        self.assertEqual(
            compare(spec, root, reference), "capture_attachment_matches_reference_copy_differs"
        )
        archive([(name, content)])
        truncated = (raw / "filestore.tar").read_bytes()[: tarfile.BLOCKSIZE * 3]
        (raw / "filestore.tar").write_bytes(truncated)
        receipt_value = json.loads((root / "capture.json").read_text())
        receipt_value["filestore_sha256"] = hashlib.sha256(truncated).hexdigest()
        (root / "capture.json").write_text(json.dumps(receipt_value))
        self.assertEqual(compare(spec, root, reference), "capture_comparison_unavailable")
        archive([(name, content)])
        stale = {**reference, "size": len(content) + 1}
        self.assertEqual(
            compare(spec, root, stale), "capture_attachment_checksum_matches_size_metadata_differs"
        )
        archive([("./" + name, content), (name, content)])
        self.assertEqual(compare(spec, root, reference), "capture_comparison_unavailable")
        archive([("../" + name, content)])
        self.assertEqual(compare(spec, root, reference), "capture_comparison_unavailable")
        archive([("unrelated", b"fixture")])
        self.assertEqual(compare(spec, root, reference), "capture_attachment_not_present")
        first_archive = (raw / "filestore.tar").read_bytes()
        archive([(name, content)])
        concatenated = first_archive + (raw / "filestore.tar").read_bytes()
        (raw / "filestore.tar").write_bytes(concatenated)
        concatenated_receipt = json.loads((root / "capture.json").read_text())
        concatenated_receipt["filestore_sha256"] = hashlib.sha256(concatenated).hexdigest()
        (root / "capture.json").write_text(json.dumps(concatenated_receipt))
        self.assertEqual(compare(spec, root, reference), "capture_comparison_unavailable")
        archive([(name, content)])
        (raw / "filestore.tar").write_bytes((raw / "filestore.tar").read_bytes() + b"changed")
        self.assertEqual(compare(spec, root, reference), "capture_comparison_unavailable")
        archive([(name, content)])
        tampered_archive = bytearray((raw / "filestore.tar").read_bytes())
        tampered_archive[tampered_archive.index(content)] ^= 1
        (raw / "filestore.tar").write_bytes(tampered_archive)
        self.assertEqual(compare(spec, root, reference), "capture_comparison_unavailable")
        archive([(name, content)])
        expired = budget((raw / "filestore.tar").stat().st_size, limit) + 1
        ticks = iter([0])
        with patch.object(self.host["time"], "monotonic", side_effect=lambda: next(ticks, expired)):
            self.assertEqual(compare(spec, root, reference), "capture_comparison_incomplete")
        receipt = root / "capture.json"
        receipt.rename(root / "saved-capture.json")
        receipt.symlink_to(root / "saved-capture.json")
        self.assertEqual(compare(spec, root, reference), "capture_comparison_unavailable")
        receipt.unlink()
        (root / "saved-capture.json").rename(receipt)
        with self.assertRaises(ValueError):
            with self.host["capture_comparison_file"](raw / "filestore.tar") as (_, metadata):
                os.utime(raw / "filestore.tar", ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1))
        archive([(name, content)])
        # Exercise the real private runner-result parser and original-bound
        # observe path: no attachment reference may reach public host output.
        containers = self.inspect()
        containers["web"][1]["State"]["Running"] = False
        spec.update(mode="observe", action="execute", inspect_capture=True)
        spec["record"]["state"] = "prepared"
        spec["source_image"] = self.spec["image_reference"]
        self.host["inspection"] = Mock(return_value=containers)
        private = {"reason": "attachment_size_and_hash_mismatch", "reference": reference}
        self.host["docker"] = Mock(
            return_value=("attachment_validation=" + json.dumps(private).encode().hex()).encode()
        )
        (raw / "database.dump").write_bytes(b"dump fixture")
        (raw / "filestore").mkdir()
        (root / "execute.started").touch()
        (root / ".execution.lock").touch()
        observed = self.run_host(spec)
        from control_plane.contracts.odoo_upgrade_rehearsal import RehearsalHostResult

        RehearsalHostResult.model_validate(observed)
        self.assertEqual(
            observed["capture_attachment_validation"],
            "capture_attachment_matches_reference_copy_differs",
        )
        self.assertFalse(observed["completed"])
        self.assertTrue(observed["raw_source_retained"])
        for value in (name, checksum, reference["copied_sha1"]):
            self.assertNotIn(value, json.dumps(observed))
        archive([(name, b"bad")])
        dump_before = (raw / "database.dump").read_bytes()
        table = [
            b"COPY public.ir_attachment (store_fname, checksum, file_size) FROM stdin;\n",
            (name + "\t" + checksum + "\t" + str(reference["size"]) + "\n").encode(),
            b"\\.\n",
        ]
        with patch.dict(
            self.host, {"captured_attachment_table_chunks": Mock(return_value=iter(table))}
        ):
            observed = self.run_host(spec)
        RehearsalHostResult.model_validate(observed)
        self.assertEqual(
            observed["capture_attachment_validation"],
            "capture_attachment_matches_copy_reference_differs",
        )
        self.assertEqual(observed["capture_database_reference_validation"], "reference_present")
        self.assertFalse(observed["completed"])
        self.assertEqual((raw / "database.dump").read_bytes(), dump_before)
        for value in (name, checksum, reference["copied_sha1"]):
            self.assertNotIn(value, json.dumps(observed))
        private["reference"] = {**reference, "name": "../private-fixture"}
        self.host["docker"].return_value = (
            "attachment_validation=" + json.dumps(private).encode().hex()
        ).encode()
        archive_comparison = Mock(wraps=compare)
        with patch.dict(self.host, {"compare_retained_attachment": archive_comparison}):
            with self.assertRaises(RuntimeError):
                self.run_host(spec)
        archive_comparison.assert_not_called()
        archive([(name, content)])
        spec["record"]["source_binding_sha256"] = hashlib.sha256(b"other fixture").hexdigest()
        self.assertEqual(compare(spec, root, reference), "capture_comparison_unavailable")

    def test_capture_attachment_probe_refuses_a_running_writer_before_exec(self) -> None:
        import json
        from control_plane._odoo_rehearsal_attachment_validation import (
            ATTACHMENT_VALIDATION_PROGRAM,
        )

        spec, root, containers = self.storage_fixture(
            {"database_bytes": 40, "filestore_bytes": 80, "staging_bytes": 20, "reserve_bytes": 10}
        )
        spec["source_image"] = self.spec["image_reference"]
        self.host["inspection"] = Mock(return_value=containers)
        self.host["docker"] = Mock(
            return_value=(
                "attachment_validation="
                + json.dumps({"reason": "database_unavailable"}).encode().hex()
            ).encode()
        )
        containers["web"][1]["State"]["Running"] = True
        self.assertEqual(self.host["capture_attachment_observation"](spec, root), "writer_active")
        self.host["docker"].assert_not_called()
        containers["web"][1]["State"]["Running"] = False
        self.assertEqual(
            self.host["capture_attachment_observation"](spec, root), "database_unavailable"
        )
        self.assertEqual(
            self.host["docker"].call_args.kwargs["stdin"], ATTACHMENT_VALIDATION_PROGRAM.encode()
        )

    def test_emitted_attachment_validator_classifies_runtime_and_read_only_database_failures(
        self,
    ) -> None:
        import os
        import sys
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from control_plane._odoo_rehearsal_attachment_validation import (
            ATTACHMENT_VALIDATION_PROGRAM,
        )

        program: dict[str, Any] = {"__name__": "attachment_fixture"}
        exec(compile(ATTACHMENT_VALIDATION_PROGRAM, "attachment_fixture", "exec"), program)
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        driver = SimpleNamespace(connect=Mock(return_value=connection))
        environment = {
            "ODOO_DB_NAME": "fixture",
            "ODOO_DB_USER": "fixture",
            "ODOO_DB_PASSWORD": "fixture-private",
        }
        with (
            patch.dict(sys.modules, {"psycopg2": driver}),
            patch.dict(os.environ, environment, clear=True),
            patch.object(sys, "argv", ["validator", "observe"]),
        ):
            # The emitted entry function uses an explicitly read-only session;
            # file cases below execute its real file validator separately.
            program["validate_attachment_files"] = Mock(return_value="verified")
            self.assertEqual(program["attachment_validation_main"](), "verified")
            connection.set_session.assert_called_once_with(
                readonly=True, isolation_level="REPEATABLE READ"
            )
            connection.rollback.assert_called_once()
            connection.close.assert_called_once()
            self.assertGreater(driver.connect.call_args.kwargs["connect_timeout"], 0)
            cursor.execute.side_effect = [None, RuntimeError("private SQL detail")]
            self.assertEqual(
                program["attachment_validation_main"](), "attachment_query_unavailable"
            )
            driver.connect.side_effect = RuntimeError("private login detail")
            self.assertEqual(program["attachment_validation_main"](), "database_unavailable")
            driver.connect.side_effect = None
            cursor.execute.side_effect = None
            connection.rollback.side_effect = RuntimeError("private connection loss")
            connection.close.side_effect = RuntimeError("private connection loss")
            self.assertEqual(program["attachment_validation_main"](), "verified")
        with patch.dict(sys.modules, {"psycopg2": None}):
            self.assertEqual(program["attachment_validation_main"](), "dependency_unavailable")
        with patch.dict(sys.modules, {"psycopg2": driver}), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(program["attachment_validation_main"](), "environment_unavailable")

    def test_emitted_attachment_validator_preserves_bytes_and_distinguishes_integrity_failures(
        self,
    ) -> None:
        from types import SimpleNamespace
        import sys
        from unittest.mock import MagicMock
        from control_plane._odoo_rehearsal_attachment_validation import (
            ATTACHMENT_VALIDATION_PROGRAM,
        )

        program: dict[str, Any] = {"__name__": "attachment_fixture"}
        exec(compile(ATTACHMENT_VALIDATION_PROGRAM, "attachment_fixture", "exec"), program)
        root = (self.fixture.root / "attachments").resolve()
        root.mkdir()
        content = b"captured attachment fixture"
        checksum = hashlib.sha1(content).hexdigest()
        name = checksum[:2] + "/" + checksum
        path = root / name
        path.parent.mkdir()
        path.write_bytes(content)
        validate = program["validate_attachment_files"]
        connection = MagicMock()
        rows = [(name, checksum, len(content))]
        connection.cursor.return_value.__enter__.return_value.__iter__.side_effect = lambda: iter(
            rows
        )
        program["Path"] = lambda _: root.parent
        with (
            patch.dict(sys.modules, {"psycopg2": SimpleNamespace(connect=lambda **_: connection)}),
            patch.dict(
                os.environ,
                {
                    "ODOO_DB_NAME": root.name,
                    "ODOO_DB_USER": "fixture",
                    "ODOO_DB_PASSWORD": "fixture",
                },
                clear=True,
            ),
            patch.object(sys, "argv", ["validator", "observe"]),
        ):
            self.assertEqual(program["attachment_validation_main"](), "verified")
            # A source-sized scan can legitimately outlast a short polling
            # interval while remaining inside the existing host job budget.
            ticks = iter([0])
            program["time"] = SimpleNamespace(monotonic=lambda: next(ticks, 30))
            self.assertEqual(program["attachment_validation_main"](), "verified")
            rows[0] = (name, checksum, len(content) + 1)
            self.assertEqual(
                program["attachment_validation_main"](),
                "attachment_content_matches_checksum_size_mismatch",
            )
            with patch.object(sys, "argv", ["validator", "validate"]):
                self.assertEqual(
                    program["attachment_validation_main"](), "attachment_size_mismatch"
                )
            path.write_bytes(b"bad")
            private_row: dict[str, object] = {}
            with patch.object(sys, "argv", ["validator", "compare"]):
                self.assertEqual(
                    program["attachment_validation_main"](failure_reference=private_row),
                    "attachment_size_and_hash_mismatch",
                )
            self.assertEqual(private_row["name"], name)
            self.assertEqual(private_row["copied_sha1"], hashlib.sha1(b"bad").hexdigest())
            path.write_bytes(content)
            rows[0] = (name, checksum, len(content))
            missing = hashlib.sha1(b"missing fixture").hexdigest()
            rows.append((missing[:2] + "/" + missing, missing, 1))
            # A slow earlier content scan must not hide a later missing file.
            with patch.object(
                program["hashlib"],
                "sha1",
                side_effect=AssertionError("content scan before metadata"),
            ):
                self.assertEqual(program["attachment_validation_main"](), "attachment_missing")
        for case_rows, expected in (
            ([(name, checksum, len(content))], "verified"),
            ([(name, None, len(content))], "attachment_metadata_invalid"),
            ([("../private", checksum, len(content))], "attachment_metadata_invalid"),
            ([(name, checksum, len(content) + 1)], "attachment_size_mismatch"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(validate(case_rows, root, None), expected)
                self.assertEqual(path.read_bytes(), content)
        # Readback can distinguish stale size metadata from changed content
        # without granting capture/apply permission to accept either one.
        mismatch = [(name, checksum, len(content) + 1)]
        self.assertEqual(
            validate(mismatch, root, None, classify_size=True),
            "attachment_content_matches_checksum_size_mismatch",
        )
        self.assertEqual(validate(mismatch, root, None), "attachment_size_mismatch")
        self.assertEqual(path.read_bytes(), content)
        program["time"] = SimpleNamespace(monotonic=Mock(side_effect=[0, 1, 2]))
        self.assertEqual(validate(mismatch, root, 2, classify_size=True), "incomplete")
        before = path.stat()
        changed = SimpleNamespace(
            st_size=before.st_size,
            st_mtime_ns=before.st_mtime_ns,
            st_ctime_ns=before.st_ctime_ns + 1,
        )
        with patch.object(program["os"], "fstat", side_effect=[before, changed]):
            self.assertEqual(
                validate(mismatch, root, None, classify_size=True), "attachment_unreadable"
            )
        self.assertEqual(path.read_bytes(), content)
        path.write_bytes(b"damaged copy fixture")
        private_reference: dict[str, object] = {}
        self.assertEqual(
            validate(mismatch, root, None, classify_size=True, failure_reference=private_reference),
            "attachment_size_and_hash_mismatch",
        )
        self.assertEqual(
            private_reference,
            {
                "name": name,
                "checksum": checksum,
                "size": len(content) + 1,
                "copied_size": len(b"damaged copy fixture"),
                "copied_sha1": hashlib.sha1(b"damaged copy fixture").hexdigest(),
            },
        )
        self.assertEqual(path.read_bytes(), b"damaged copy fixture")
        path.write_bytes(content)
        program["time"] = SimpleNamespace(monotonic=lambda: 2)
        self.assertEqual(validate([(name, checksum, len(content))], root, 1), "incomplete")
        path.write_bytes(b"x" * len(content))
        self.assertEqual(
            validate([(name, checksum, len(content))], root, None), "attachment_hash_mismatch"
        )
        self.assertEqual(path.read_bytes(), b"x" * len(content))
        path.unlink()
        self.assertEqual(
            validate([(name, checksum, len(content))], root, None), "attachment_missing"
        )
        target = root / "private-fixture"
        target.write_bytes(content)
        path.symlink_to(target)
        self.assertEqual(
            validate([(name, checksum, len(content))], root, None), "attachment_path_unsafe"
        )
        self.assertEqual(target.read_bytes(), content)
        path.unlink()
        path.write_bytes(content)
        with patch.object(program["os"], "open", side_effect=PermissionError("private path")):
            self.assertEqual(
                validate([(name, checksum, len(content))], root, None), "attachment_unreadable"
            )
        self.assertEqual(path.read_bytes(), content)

    def setUp(self) -> None:
        from tests.test_odoo_testing_restore import FreshTestingHostProgramTests
        from control_plane.odoo_upgrade_rehearsal import rehearsal_host_program
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
        program = rehearsal_host_program()
        tree = ast.parse(program)
        tree.body.pop()  # Load the real emitted functions without starting its CLI.
        self.host: dict[str, Any] = {}
        from control_plane.odoo_upgrade_rehearsal import REHEARSAL_HOST_FILENAME

        exec(compile(tree, REHEARSAL_HOST_FILENAME, "exec"), self.host)

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

    def storage_fixture(
        self, budget: dict[str, int]
    ) -> tuple[dict[str, Any], Path, dict[str, Any]]:
        root = self.fixture.root / ".local/state/launchplane-rehearsals" / self.identifier
        root.mkdir(parents=True)
        containers = self.inspect()
        for role in ("database", "script-runner"):
            upper = self.fixture.root / (role + "-upper")
            upper.mkdir()
            containers[role][1]["GraphDriver"] = {"Data": {"UpperDir": str(upper)}}
        spec = {
            "record": {
                "rehearsal_id": self.identifier,
                "storage": budget,
                "state": "switched",
                "intent_sha256": hashlib.sha256(b"fixture").hexdigest(),
                "copies": [],
                "elapsed_seconds": {},
            },
            "attempt_sha256": hashlib.sha256(b"fixture-attempt").hexdigest(),
            "record_binding_sha256": hashlib.sha256(b"fixture-record").hexdigest(),
            "mode": "apply",
            "action": "verify",
            "capture_comparison_timeout_seconds": 1200,
            "target_image": self.spec["image_reference"],
        }
        return spec, root, containers

    def run_host(self, spec: dict[str, Any]) -> dict[str, Any]:
        import base64
        import io
        import json
        import sys
        from contextlib import redirect_stdout

        output = io.StringIO()
        encoded = base64.b64encode(json.dumps(spec).encode()).decode()
        with (
            patch.object(sys, "argv", ["rehearsal", encoded]),
            patch.object(Path, "home", return_value=self.fixture.root),
            redirect_stdout(output),
        ):
            self.host["main"]()
        return cast(
            dict[str, Any], json.loads(bytes.fromhex(output.getvalue().strip().split("=", 1)[1]))
        )

    def test_partial_prepare_and_capture_report_custody_without_replaying(self) -> None:
        spec, root, _ = self.storage_fixture(
            {"database_bytes": 40, "filestore_bytes": 80, "staging_bytes": 20, "reserve_bytes": 10}
        )
        spec.update(mode="observe", action="prepare")
        spec["record"]["state"] = "planned"
        (root / "prepare.started").touch()
        self.host["docker"] = Mock(
            side_effect=AssertionError("Custody observation must not execute")
        )
        observed = self.run_host(spec)
        self.assertEqual(
            observed["custody"],
            {
                "effect_started": True,
                "raw_database_bytes": None,
                "raw_filestore_bytes": None,
                "raw_filestore_extracted": False,
                "capture_receipt_present": False,
                "capture_pending_present": False,
                "source_snapshot_directory_present": False,
            },
        )
        self.assertFalse(observed["completed"])
        self.assertFalse(observed["prepare_not_started"])
        spec.update(action="execute")
        spec["record"]["state"] = "prepared"
        (root / "execute.started").touch()
        raw = root / "raw-source"
        raw.mkdir()
        (raw / "database.dump").write_bytes(b"partial fixture")
        before = (raw / "database.dump").read_bytes()
        observed = self.run_host(spec)
        self.assertTrue(observed["custody"]["effect_started"])
        self.assertEqual(observed["custody"]["raw_database_bytes"], len(before))
        self.assertIsNone(observed["custody"]["raw_filestore_bytes"])
        self.assertTrue(observed["raw_source_retained"])
        self.assertFalse(observed["completed"])
        self.assertEqual((raw / "database.dump").read_bytes(), before)
        self.host["docker"].assert_not_called()
        # A complete retained capture can be inspected without restoring,
        # sanitizing, recapturing or releasing its original effect.
        (root / "capture.json").write_text("{}")
        (raw / "filestore.tar").write_bytes(b"archive fixture")
        (raw / "filestore").mkdir()
        spec["inspect_capture"] = True
        self.host["capture_attachment_observation"] = Mock(return_value="database_unavailable")
        self.assertNotIn("capture_attachment_validation", self.run_host(spec))
        self.host["capture_attachment_observation"].assert_not_called()
        (root / ".execution.lock").touch()
        observed = self.run_host(spec)
        self.assertEqual(observed["capture_attachment_validation"], "database_unavailable")
        self.assertFalse(observed["completed"])
        self.assertEqual((raw / "database.dump").read_bytes(), before)
        self.host["docker"].assert_not_called()
        self.host["capture_attachment_observation"].reset_mock()
        spec.update(action="prepare")
        spec["record"]["state"] = "planned"
        partial_prepare = self.run_host(spec)
        self.assertNotIn("capture_attachment_validation", partial_prepare)
        self.assertFalse(partial_prepare["completed"])
        self.host["capture_attachment_observation"].assert_not_called()
        spec.update(action="execute")
        spec["record"]["state"] = "prepared"
        from subprocess import CompletedProcess

        with (
            patch.object(
                self.host["subprocess"], "run", return_value=CompletedProcess([], 1, b"", b"")
            ),
            self.assertRaises(RuntimeError) as failure,
        ):
            self.host["command"](["docker", "exec", "fixture"])
        self.host["capture_attachment_observation"].side_effect = failure.exception
        unavailable = self.run_host(spec)
        self.assertEqual(unavailable["capture_attachment_validation"], "runtime_unavailable")
        self.assertEqual(unavailable["custody"], observed["custody"])
        self.assertFalse(unavailable["completed"])
        self.host["capture_attachment_observation"].side_effect = RuntimeError(
            "isolation contradiction"
        )
        with self.assertRaises(RuntimeError):
            self.run_host(spec)
        (raw / "filestore").rmdir()
        import fcntl

        with (root / ".execution.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            busy = self.run_host(spec)
            self.assertTrue(busy["host_busy"])
            self.assertNotIn("custody", busy)
        self.assertEqual((raw / "database.dump").read_bytes(), before)
        (raw / "filestore").symlink_to(self.fixture.root, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.run_host(spec)

    def test_historical_capture_fits_without_recharging_completed_copies(self) -> None:
        from types import SimpleNamespace

        # Synthetic storage limits around the diagnosed source sizes. These
        # are fixture budgets, not requested changes to a live host's quota.
        database, filestore = 1_075_328_691, 106_516_687_222
        available = 933_105_549_312
        spec, root, containers = self.storage_fixture(
            {
                "database_bytes": database,
                "filestore_bytes": filestore,
                "staging_bytes": 1024**3,
                "reserve_bytes": 10 * 1024**3,
            }
        )
        retained_copy_bytes = database + filestore
        # After execute, two working copies and one recovery snapshot exist;
        # after simulate-final there are four working copies and two snapshots.
        for phase, retained in (("execute", 0), ("simulate-final", 3), ("verify", 6)):
            free = available - retained * retained_copy_bytes
            with (
                self.subTest(phase=phase),
                patch.object(
                    os, "statvfs", return_value=SimpleNamespace(f_bavail=free, f_frsize=1)
                ),
            ):
                self.assertTrue(
                    self.host["rehearsal_storage_capacity"](spec, root, containers, phase)
                )

    def test_capacity_groups_devices_and_reserves_nonroot_available_bytes_once(self) -> None:
        from types import SimpleNamespace

        spec, root, containers = self.storage_fixture(
            {
                "database_bytes": 40,
                "filestore_bytes": 80,
                "staging_bytes": 20,
                "reserve_bytes": 10,
            }
        )
        volumes = self.fixture.volumes
        data = Path(volumes[self.identifier + "-data"]["Mountpoint"])
        database = Path(volumes[self.identifier + "-db"]["Mountpoint"])
        runner = Path(containers["script-runner"][1]["GraphDriver"]["Data"]["UpperDir"])
        database_upper = Path(containers["database"][1]["GraphDriver"]["Data"]["UpperDir"])
        devices = {data: 1, database: 1, runner: 2, root: 3, database_upper: 4}
        # Two new working copies share device 1. The runner dump and host
        # raw/snapshot custody use their own devices, each with one reserve.
        free = {1: 250, 2: 50, 3: 350, 4: 50}
        original_stat = Path.stat

        def stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
            values = list(original_stat(path, *args, **kwargs))
            if path in devices:
                values[2] = devices[path]
            return os.stat_result(values)

        def statvfs(path: Path) -> SimpleNamespace:
            return SimpleNamespace(f_bavail=free[devices[path]] // 2, f_bfree=10_000, f_frsize=2)

        with patch.object(Path, "stat", stat), patch.object(os, "statvfs", statvfs):
            self.assertTrue(
                self.host["rehearsal_storage_capacity"](spec, root, containers, "execute")
            )
            for device in (1, 2, 3):
                with self.subTest(device=device):
                    free[device] -= 2
                    self.assertFalse(
                        self.host["rehearsal_storage_capacity"](spec, root, containers, "execute")
                    )
                    free[device] += 2
            # Final restoration's temporary dump belongs to the database
            # writable layer. It must not borrow runner or host free space.
            free[2] = 0
            free[3] = 230
            free[4] -= 2
            self.assertFalse(
                self.host["rehearsal_storage_capacity"](spec, root, containers, "simulate-final")
            )
            free[4] += 2
            self.assertTrue(
                self.host["rehearsal_storage_capacity"](spec, root, containers, "simulate-final")
            )
            free[1] = 0
            free[3] = 150
            free[4] = 0
            self.assertTrue(
                self.host["rehearsal_storage_capacity"](spec, root, containers, "verify")
            )

    def test_unknown_runner_or_database_staging_backing_refuses(self) -> None:
        from types import SimpleNamespace

        spec, root, containers = self.storage_fixture(
            {
                "database_bytes": 40,
                "filestore_bytes": 80,
                "staging_bytes": 20,
                "reserve_bytes": 10,
            }
        )
        with patch.object(os, "statvfs", return_value=SimpleNamespace(f_bavail=10_000, f_frsize=1)):
            for role, phase in (("script-runner", "execute"), ("database", "simulate-final")):
                with self.subTest(role=role):
                    driver = containers[role][1].pop("GraphDriver")
                    with self.assertRaisesRegex(RuntimeError, "staging backing is unavailable"):
                        self.host["rehearsal_storage_capacity"](spec, root, containers, phase)
                    containers[role][1]["GraphDriver"] = driver

    def test_capacity_refusal_is_terminal_per_attempt_and_new_attempt_can_start(self) -> None:
        from types import SimpleNamespace

        spec, root, containers = self.storage_fixture(
            {
                "database_bytes": 40,
                "filestore_bytes": 80,
                "staging_bytes": 20,
                "reserve_bytes": 10,
            }
        )
        self.host["inspection"] = Mock(return_value=containers)
        self.host["validate_snapshots"] = Mock()
        self.host["snapshot"] = Mock(return_value={"database": "fixture-new-snapshot"})
        self.host["private_health"] = Mock()
        self.host["docker"] = Mock(wraps=self.host["docker"])
        with patch.object(os, "statvfs", return_value=SimpleNamespace(f_bavail=149, f_frsize=1)):
            result = self.run_host(spec)
        self.assertFalse(result["completed"])
        self.assertEqual(result["refusal_code"], "rehearsal_storage_capacity_insufficient")
        self.assertEqual(result["attempt_sha256"], spec["attempt_sha256"])
        receipt = self.host["refusal_receipt"](spec, root, result["refusal_code"])
        self.assertEqual(receipt.read_bytes(), b"")
        self.assertEqual(self.host["read_refusal"](spec, root), result)
        self.host["snapshot"].assert_not_called()
        self.host["private_health"].assert_not_called()
        self.assertFalse((root / "verify.started").exists())
        # Capacity may inspect volume backing, but must not stop a container
        # or run SQL before refusing the new recovery allocation.
        self.assertTrue(
            all(
                call.args[:2] == ("volume", "inspect")
                for call in self.host["docker"].call_args_list
            )
        )
        self.host["inspection"].reset_mock()
        with patch.object(os, "statvfs", return_value=SimpleNamespace(f_bavail=150, f_frsize=1)):
            self.assertEqual(self.run_host(spec), result)
            spec["mode"] = "observe"
            self.assertEqual(self.run_host(spec), result)
            self.host["inspection"].assert_not_called()
            spec["attempt_sha256"] = hashlib.sha256(b"new-attempt").hexdigest()
            unknown = self.run_host(spec)
            self.assertFalse(unknown["completed"])
            self.assertNotIn("refusal_code", unknown)
            spec["mode"] = "apply"
            completed = self.run_host(spec)
        self.assertEqual(completed["state"], "verified")
        self.host["snapshot"].assert_called_once()
        self.assertTrue((root / "verify.started").is_file())
        self.assertTrue((root / "verify.json").is_file())
        self.assertEqual(receipt.read_bytes(), b"")

    def test_read_only_precheck_failures_refuse_before_the_started_marker(self) -> None:
        spec, root, containers = self.storage_fixture(
            {"database_bytes": 40, "filestore_bytes": 80, "staging_bytes": 20, "reserve_bytes": 10}
        )
        self.host["docker"] = Mock(side_effect=AssertionError("No runtime write may start"))
        self.host["snapshot"] = Mock()
        for failure, error in (
            ("inspection", RuntimeError("Unverified fixture prerequisite")),
            ("inspection", TypeError("Malformed fixture observation")),
            ("inspection", AttributeError("Missing fixture observation")),
            ("validate_snapshots", OSError("Unavailable fixture snapshot")),
            ("retire-presence", None),
        ):
            with self.subTest(failure=failure, error=type(error).__name__):
                spec["attempt_sha256"] = hashlib.sha256(
                    (failure + type(error).__name__).encode()
                ).hexdigest()
                spec["action"] = "retire" if failure == "retire-presence" else "verify"
                self.host["inspection"] = Mock(return_value=containers)
                self.host["validate_snapshots"] = Mock()
                if failure != "retire-presence":
                    self.host[failure].side_effect = error
                result = self.run_host(spec)
                self.assertFalse(result["completed"])
                self.assertEqual(result["refusal_code"], "rehearsal_precheck_refused")
                self.assertFalse((root / (spec["action"] + ".started")).exists())
        self.host["docker"].assert_not_called()
        self.host["snapshot"].assert_not_called()

    def test_started_custody_cannot_be_reconciled_as_an_earlier_refusal(self) -> None:
        spec, root, _ = self.storage_fixture(
            {"database_bytes": 40, "filestore_bytes": 80, "staging_bytes": 20, "reserve_bytes": 10}
        )
        self.host["inspection"] = Mock(side_effect=RuntimeError("Unknown backing"))
        refused = self.run_host(spec)
        receipt = self.host["refusal_receipt"](spec, root, refused["refusal_code"])
        started = root / "verify.started"
        started.touch()
        spec["mode"] = "observe"
        observed = self.run_host(spec)
        self.assertFalse(observed["completed"])
        self.assertNotIn("refusal_code", observed)
        spec["mode"] = "apply"
        with self.assertRaisesRegex(RuntimeError, "Uncertain original effect"):
            self.run_host(spec)
        self.assertTrue(started.is_file())
        self.assertEqual(receipt.read_bytes(), b"")

    def test_interrupted_refusal_marker_is_complete_and_bound_to_attempt_and_record(self) -> None:
        spec, root, _ = self.storage_fixture(
            {"database_bytes": 40, "filestore_bytes": 80, "staging_bytes": 20, "reserve_bytes": 10}
        )
        self.host["inspection"] = Mock(side_effect=RuntimeError("Unknown backing"))
        self.host["docker"] = Mock(side_effect=AssertionError("No runtime effect may start"))
        with patch.object(os, "fsync", side_effect=RuntimeError("Interrupted after creation")):
            with self.assertRaisesRegex(RuntimeError, "Interrupted after creation"):
                self.run_host(spec)
        self.assertFalse((root / "verify.started").exists())
        spec["mode"] = "observe"
        result = self.run_host(spec)
        self.assertEqual(result["refusal_code"], "rehearsal_precheck_refused")
        self.assertFalse(result["completed"])
        self.host["docker"].assert_not_called()
        for field in ("attempt_sha256", "record_binding_sha256"):
            with self.subTest(field=field):
                previous = spec[field]
                spec[field] = hashlib.sha256(field.encode()).hexdigest()
                unknown = self.run_host(spec)
                self.assertFalse(unknown["completed"])
                self.assertNotIn("refusal_code", unknown)
                spec[field] = previous

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

    def test_read_only_consumer_cannot_see_private_raw_source_custody(self) -> None:
        self.fixture.containers["foreign"] = {
            "Id": "foreign-container",
            "Config": {"Labels": {}},
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(Path.home()),
                    "Destination": "/host-home",
                    "RW": False,
                }
            ],
        }
        # The foreign container is not part of the internal network or volume
        # inventory. Its read-only ancestor bind alone must block capture.
        docker = self.host["docker"]

        def network(*arguments: str, **kwargs: Any) -> bytes:
            if arguments == ("inspect", "testing-data"):
                import json

                return json.dumps(
                    [
                        {
                            "Internal": True,
                            "Containers": {
                                item["Id"]: {}
                                for name, item in self.fixture.containers.items()
                                if name != "foreign"
                            },
                        }
                    ]
                ).encode()
            return cast(bytes, docker(*arguments, **kwargs))

        self.host["docker"] = network
        self.host["one_inspection"] = lambda name: __import__("json").loads(
            network("inspect", name)
        )[0]
        with self.assertRaisesRegex(RuntimeError, "state is exposed"):
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

    async def test_terminal_http_outcomes_preserve_fixed_refusal_codes_through_the_client(
        self,
    ) -> None:
        from control_plane.contracts.odoo_upgrade_rehearsal import (
            ODOO_UPGRADE_REHEARSAL_ROUTE,
            REHEARSAL_PUBLIC_FAILURE_MESSAGES,
        )
        from control_plane.http_routes.mutation_support import idempotency_scope
        from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
        from control_plane.service_auth import (
            configured_local_operator_identity,
            LaunchplaneAuthzPolicy,
        )
        from control_plane.storage.postgres import PostgresRecordStore
        from tests.http_app_test_support import _local_operator_bearer_config
        from tests.support.http import request
        from tests.test_odoo_upgrade_rehearsal_client import RehearsalClientTests

        identity = configured_local_operator_identity(
            _local_operator_bearer_config(token_label="local-owner-write")
        )
        assert identity is not None
        self.fixture.principal = idempotency_scope(identity)
        retained = self.fixture.record.model_copy(
            update={
                "principal": self.fixture.principal,
                "state": "switched",
                "compose_id": "own-compose",
            }
        )
        self.fixture.store.compare_and_write_odoo_upgrade_rehearsal(self.fixture.record, retained)
        self.fixture.store.seed_authz_policy_if_absent(
            LaunchplaneAuthzPolicyRecord(
                record_id="fixture-http-policy",
                source="test",
                updated_at=retained.updated_at.isoformat(),
                policy=LaunchplaneAuthzPolicy(schema_version=2),
            )
        )
        adapter = self.fixture.adapter("verify")
        payload = adapter.request.model_dump(mode="json")
        client = RehearsalClientTests()
        self.addCleanup(client.doCleanups)
        private_message = "private-provider-detail-must-stay-private"
        for code in (*REHEARSAL_PUBLIC_FAILURE_MESSAGES, "unsupported_provider_code"):
            refusal: dict[str, object] = {"refusal_code": code, "completed": False}
            failure = {
                "trace_id": "fixture-trace",
                "error": {"code": code, "message": private_message},
            }
            with patch.object(OdooRehearsalAdapter, "refusal_response", return_value=failure):
                with patch.object(OdooRehearsalAdapter, "host", return_value=refusal):
                    fresh = self.fixture.run_operation(adapter, code)
                    replay = self.fixture.run_operation(adapter, code)
                with patch.object(
                    OdooRehearsalAdapter,
                    "host",
                    side_effect=[TimeoutError("Lost refusal response"), refusal],
                ):
                    with self.assertRaises(TimeoutError):
                        self.fixture.run_operation(adapter, code + "-lost")
                    adopted = self.fixture.run_operation(adapter, code + "-lost")
            self.assertEqual(
                (fresh.status, adopted.status, replay.status), ("completed", "adopted", "replayed")
            )
            for outcome in (fresh, adopted, replay):
                with (
                    self.subTest(code=code, outcome=outcome.status),
                    # Only bypass the production-storage admission for this
                    # HTTP mapping proof. Journal outcomes above use real
                    # SQLite storage and make no PostgreSQL-locking claim.
                    patch.object(
                        PostgresRecordStore,
                        "database_dialect_name",
                        new_callable=PropertyMock,
                        return_value="postgresql",
                    ),
                    patch(
                        "control_plane.http_app.run_durable_provider_operation",
                        return_value=outcome,
                    ),
                ):
                    response = await request(
                        self.fixture.app,
                        "POST",
                        ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=self.fixture.product),
                        payload=payload,
                        headers={
                            "Authorization": "Bearer local-operator-token",
                            "Idempotency-Key": code,
                        },
                    )
                self.assertEqual(response.status_code, 409, response.text)
                public = response.json()
                terminal = code in REHEARSAL_PUBLIC_FAILURE_MESSAGES
                expected_code = code if terminal else "provider_mutation_failed"
                self.assertEqual(public["error"]["code"], expected_code)
                if terminal:
                    self.assertEqual(
                        public["error"]["message"], REHEARSAL_PUBLIC_FAILURE_MESSAGES[code]
                    )
                self.assertNotIn(private_message, response.text)
                client_code, result, post = client.call(
                    "apply", failure=(response.status_code, public)
                )
                self.assertNotEqual(client_code, 0)
                self.assertEqual(result["error_code"], expected_code)
                self.assertEqual(result["original_key_required"], not terminal)
                self.assertEqual(
                    result["status"], "unavailable" if terminal else "reconciliation_required"
                )
                post.assert_called_once()
        self.assertEqual(
            self.fixture.store.read_odoo_upgrade_rehearsal(self.fixture.identifier), retained
        )

    async def test_execute_readback_merges_only_original_journal_and_keeps_unavailable_logs_unknown(
        self,
    ) -> None:
        from control_plane.contracts.canonical_json import canonical_json_sha256
        from control_plane.dokploy import api
        from control_plane.contracts.odoo_upgrade_rehearsal import ODOO_UPGRADE_REHEARSAL_ROUTE
        from tests.support.http import request

        app = self.read_only_app()
        fixture = self.fixture
        prepare_rehearsal_local_credentials(
            fixture.store, fixture.identifier, actor=fixture.principal
        )
        prepared = fixture.record.model_copy(
            update={"state": "prepared", "compose_id": "own-compose"}
        )
        fixture.store.compare_and_write_odoo_upgrade_rehearsal(fixture.record, prepared)
        with patch.object(
            OdooRehearsalAdapter, "host", side_effect=TimeoutError("private failure")
        ):
            with self.assertRaises(TimeoutError):
                fixture.run_operation(fixture.adapter("execute"), "original-execute")
        before = fixture.store.list_held_provider_target_reservations()
        custody = {"effect_started": True, "raw_database_bytes": 12}
        observed: dict[str, object] = {
            "state": prepared.state,
            "copies": [],
            "completed": False,
            "raw_source_retained": True,
            "custody": custody,
            "capture_attachment_validation": "verified",
            "child_failure": {
                "intent_sha256": prepared.intent_sha256,
                "record_binding_sha256": prepared.binding_sha256,
                "attempt_sha256": canonical_json_sha256({"operation_key": "fixture-operation"}),
                "step": "copy_runner",
                "exit_status": 7,
                "error_text": "UndefinedColumn: legacy_code",
            },
            "retained_upgrade_logs": {
                "files": [],
                "runtime": [],
                "since": datetime.now(timezone.utc).isoformat(),
                "truncated": False,
            },
        }
        evidence: dict[str, Any] = {
            "intent_sha256": prepared.intent_sha256,
            "record_binding_sha256": prepared.binding_sha256,
            "attempt_sha256": canonical_json_sha256({"operation_key": "fixture-operation"}),
            "attachment": RehearsalHostSafetyTests.attachment_mismatch_fixture(),
        }
        payload = fixture.request.model_copy(
            update={"mode": "readback", "action": "execute"}
        ).model_dump(mode="json")
        for status, attachment_reason in (
            (None, "verified"),
            (None, "attachment_size_mismatch"),
            (503, "attachment_content_matches_checksum_size_mismatch"),
            (None, "attachment_size_and_hash_mismatch"),
            (None, "capture_attachment_matches_reference_copy_differs"),
            (None, "capture_attachment_matches_copy_reference_differs"),
            (None, "capture_comparison_incomplete"),
            (403, "verified"),
        ):
            with self.subTest(log_status=status, attachment_reason=attachment_reason):
                observed["capture_attachment_validation"] = attachment_reason
                observed.pop("attachment_mismatch", None)
                if attachment_reason == evidence["attachment"]["reason"]:
                    observed["attachment_mismatch"] = evidence
                observed.pop("capture_database_reference_validation", None)
                if attachment_reason == "capture_attachment_matches_copy_reference_differs":
                    observed["capture_database_reference_validation"] = "reference_present"
                error = api.DokployRequestFailed(
                    method="GET",
                    path="/api/deployment.allByType",
                    detail="private provider detail",
                    status_code=status or 503,
                )
                with (
                    patch.object(
                        OdooRehearsalAdapter, "host", side_effect=lambda *a, **k: dict(observed)
                    ) as host,
                    patch.object(
                        OdooRehearsalAdapter,
                        "failure_events",
                        return_value=([], True),
                        side_effect=error if status else None,
                    ),
                ):
                    response = await request(
                        app,
                        "POST",
                        ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=fixture.product),
                        payload=payload,
                        headers={"Idempotency-Key": "original-execute"},
                    )
                self.assertEqual(response.status_code, 403 if status == 403 else 202, response.text)
                self.assertNotIn("private provider detail", response.text)
                if status == 403:
                    host.assert_not_called()
                    continue
                result = response.json()["result"]
                self.assertEqual(result["custody"], custody)
                self.assertEqual(result["child_failure"], observed["child_failure"])
                self.assertEqual(result["retained_upgrade_logs"], observed["retained_upgrade_logs"])
                self.assertEqual(result["operation_state"], before[0].state)
                self.assertEqual(result["provider_effect_phase"], before[0].provider_effect_phase)
                self.assertEqual(result.get("failure_events_unavailable", False), status == 503)
                self.assertTrue(result["original_key_required"])
                self.assertNotIn("_provider_operation_key", result)
                self.assertTrue(host.call_args.kwargs["operation_key"])
                self.assertTrue(host.call_args.kwargs["inspect_capture"])
                self.assertEqual(result["capture_attachment_validation"], attachment_reason)
                self.assertEqual(
                    result.get("attachment_mismatch"), observed.get("attachment_mismatch")
                )
                if "attachment_mismatch" in result:
                    from tests.test_odoo_upgrade_rehearsal_client import RehearsalClientTests

                    client = RehearsalClientTests()
                    self.addCleanup(client.doCleanups)
                    client_result_input = {
                        key: value
                        for key, value in result.items()
                        if key not in {"product", "rehearsal_id"}
                    }
                    code, client_result, _ = client.call(
                        "readback", action="execute", response_result=client_result_input
                    )
                    self.assertEqual(code, 1)
                    self.assertEqual(client_result["attachment_mismatch"], evidence)
                    self.assertNotIn("private contents", str(client_result))
                self.assertEqual(
                    result.get("capture_database_reference_validation"),
                    observed.get("capture_database_reference_validation"),
                )
                self.assertEqual(result["recovery_disposition"], "hold_unknown")
        with patch.object(OdooRehearsalAdapter, "host", return_value=observed) as host:
            response = await request(
                app,
                "POST",
                ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=fixture.product),
                payload=payload,
                headers={"Idempotency-Key": "unbound-key"},
            )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertFalse(host.call_args.kwargs["inspect_capture"])
        self.assertEqual(response.json()["result"]["recovery_disposition"], "hold_unknown")
        self.assertEqual(fixture.store.list_held_provider_target_reservations(), before)
        self.assertEqual(fixture.store.read_odoo_upgrade_rehearsal(fixture.identifier), prepared)

        for observation in (
            {
                "completed": False,
                "host_observation_unavailable": True,
                "host_observation_reason": "host_busy",
            },
            RehearsalRefused(RehearsalPrerequisite.PLACEMENT_UNAVAILABLE),
            RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED),
        ):
            with (
                patch.object(OdooRehearsalAdapter, "failure_events", return_value=([], True)),
                patch.object(
                    OdooRehearsalAdapter,
                    "host",
                    side_effect=observation if isinstance(observation, Exception) else None,
                    return_value=observation,
                ),
            ):
                response = await request(
                    app,
                    "POST",
                    ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=fixture.product),
                    payload=payload,
                    headers={"Idempotency-Key": "original-execute"},
                )
            if isinstance(observation, Exception):
                self.assertEqual(response.status_code, 409)
            else:
                self.assertEqual(response.status_code, 202)
                result = response.json()["result"]
                self.assertTrue(result["original_key_required"])
                self.assertEqual(result["operation_state"], before[0].state)
                for field in (
                    "public_route",
                    "resources_preserved",
                    "business_host_frozen",
                    "copies",
                    "custody",
                ):
                    self.assertNotIn(field, result)

        refusal_code = RehearsalPrerequisite.PRECHECK_REFUSED
        adopted = fixture.store.adopt_reconciled_mutation(
            reservation=before[0],
            response_status_code=409,
            response_trace_id="fixture-refusal",
            response_payload={"error": {"code": refusal_code}},
        )
        self.assertEqual(adopted.status, "adopted")
        with (
            patch.object(
                OdooRehearsalAdapter,
                "host",
                return_value={**observed, "refusal_code": refusal_code},
            ),
            patch.object(OdooRehearsalAdapter, "failure_events") as events,
        ):
            response = await request(
                app,
                "POST",
                ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=fixture.product),
                payload=payload,
                headers={"Idempotency-Key": "original-execute"},
            )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["result"]["recovery_disposition"], "completed_refusal")
        self.assertFalse(response.json()["result"]["original_key_required"])
        events.assert_not_called()

        with patch.object(
            OdooRehearsalAdapter, "host", return_value={**observed, "refusal_code": "different"}
        ):
            response = await request(
                app,
                "POST",
                ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=fixture.product),
                payload=payload,
                headers={"Idempotency-Key": "original-execute"},
            )
        self.assertEqual(response.json()["result"]["recovery_disposition"], "hold_unknown")
        self.assertTrue(response.json()["result"]["original_key_required"])

    def read_only_app(self) -> Any:
        from fastapi import FastAPI, HTTPException
        from pydantic import BaseModel
        from control_plane.http_routes.odoo_upgrade_rehearsal import (
            register_odoo_upgrade_rehearsal_routes,
        )
        from control_plane.http_routes.support import ReadRouteDependencies, ApiRouteRegistrar
        from control_plane.service_auth import LocalOperatorIdentity

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
        return app

    async def test_read_role_can_plan_without_changing_any_lane_and_cannot_apply(self) -> None:
        from tests.support.http import request
        from control_plane.contracts.odoo_upgrade_rehearsal import ODOO_UPGRADE_REHEARSAL_ROUTE

        app = self.read_only_app()
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

        # A failed prepare has no compose on which to schedule host readback.
        # The read role can inspect the exact journal without adopting custody.
        with (
            patch(
                "control_plane.dokploy.sandbox_placement.created_placement",
                return_value=("", "", ""),
            ),
            patch("control_plane.dokploy.api.dokploy_request", return_value={}),
            self.assertRaises(RehearsalRefused),
        ):
            self.fixture.run_operation(self.fixture.adapter("prepare"), "original-prepare")
        partial = self.fixture.store.read_odoo_upgrade_rehearsal(self.fixture.identifier)
        assert partial is not None
        with (
            patch(
                "control_plane.dokploy.sandbox_placement.observe_empty_isolated_project",
                return_value="partial-project",
            ),
            patch.object(OdooRehearsalAdapter, "host") as host,
        ):
            read = await request(
                app,
                "POST",
                ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=self.fixture.product),
                payload=self.fixture.request.model_copy(update={"mode": "readback"}).model_dump(
                    mode="json"
                ),
                headers={"Idempotency-Key": "original-prepare"},
            )
            self.assertEqual(read.status_code, 202)
            self.assertEqual(
                read.json()["result"]["recovery_disposition"], "continue_original_prepare"
            )
            self.assertFalse(read.json()["result"]["completed"])
            host.assert_not_called()
        self.assertEqual(
            self.fixture.store.read_odoo_upgrade_rehearsal(self.fixture.identifier), partial
        )
        store = self.fixture.store
        reservation = store.read_idempotency_record(
            scope=self.fixture.principal,
            route_path=ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=self.fixture.product),
            idempotency_key="original-prepare",
        )
        assert reservation is not None
        acquired = store.retry_reconciled_mutation(
            reservation=reservation, lease_owner="fixture-host", lease_seconds=300
        )
        assert acquired.record is not None
        checkpoint = store.checkpoint_mutation_provider_effect(
            reservation=acquired.record, effect_phase="prepare", lease_seconds=300
        )
        assert checkpoint.record is not None
        store.mark_mutation_reconcile_required(
            reservation=checkpoint.record, reconciliation_key=checkpoint.record.reconciliation_key
        )
        placed = partial.model_copy(
            update={
                "project_id": "own-project",
                "environment_id": "own-environment",
                "compose_id": "own-compose",
            }
        )
        store.compare_and_write_odoo_upgrade_rehearsal(partial, placed)
        journal = store.list_held_provider_target_reservations()
        for not_started in (False, True):
            with (
                patch.object(OdooRehearsalAdapter, "failure_events", return_value=([], False)),
                patch.object(
                    OdooRehearsalAdapter,
                    "host",
                    return_value={
                        "state": "planned",
                        "copies": [],
                        "completed": False,
                        "raw_source_retained": False,
                        "prepare_not_started": not_started,
                    },
                ),
            ):
                read = await request(
                    app,
                    "POST",
                    ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=self.fixture.product),
                    payload=self.fixture.request.model_copy(update={"mode": "readback"}).model_dump(
                        mode="json"
                    ),
                    headers={"Idempotency-Key": "original-prepare"},
                )
            self.assertEqual(read.status_code, 202)
            self.assertEqual(
                read.json()["result"]["recovery_disposition"],
                "continue_original_prepare" if not_started else "hold_unknown",
            )
            self.assertEqual(read.json()["result"]["provider_effect_phase"], "prepare")
            self.assertEqual(store.read_odoo_upgrade_rehearsal(self.fixture.identifier), placed)
            self.assertEqual(store.list_held_provider_target_reservations(), journal)
        self.fixture.record = placed
        for key in ("", "different-key"):
            with patch.object(
                OdooRehearsalAdapter,
                "host",
                return_value={
                    "state": "planned",
                    "copies": [],
                    "completed": False,
                    "raw_source_retained": False,
                    "prepare_not_started": True,
                },
            ):
                read = await request(
                    app,
                    "POST",
                    ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=self.fixture.product),
                    payload=self.fixture.request.model_copy(update={"mode": "readback"}).model_dump(
                        mode="json"
                    ),
                    headers={"Idempotency-Key": key} if key else {},
                )
            self.assertEqual(read.status_code, 202)
            self.assertTrue(read.json()["result"]["original_key_required"])
            self.assertEqual(read.json()["result"]["recovery_disposition"], "hold_unknown")
            self.assertEqual(
                read.json()["result"]["operation_journal_status"],
                "missing" if key else "not_requested",
            )
            self.assertEqual(store.list_held_provider_target_reservations(), journal)
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
