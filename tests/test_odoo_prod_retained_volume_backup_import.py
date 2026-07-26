import base64
import json
import subprocess
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import patch

from control_plane.contracts.artifact_identity import (
    ArtifactImageReference,
    ArtifactIdentityManifest,
)
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.odoo_prod_retained_volume_backup_import import (
    ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_CONFIRMATION,
    OdooProdRetainedVolumeBackupImportApplyRequest,
    OdooProdRetainedVolumeBackupImportPlan,
    OdooProdRetainedVolumeBackupImportRequest,
    build_odoo_prod_retained_volume_backup_import_plan_fingerprint,
)
from control_plane.contracts.odoo_prod_retained_volume_backup_import_operation import (
    OdooProdRetainedVolumeBackupImportOperationRecord,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneHealthMonitoringPolicy,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity, runtime_identity_env
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import post_deploy as dokploy_post_deploy
from control_plane.dokploy.source import DokployTargetDefinition
from control_plane.workflows.odoo_prod_backup_gate import (
    RETAINED_VOLUME_BACKUP_IMPORT_SOURCE,
)
from control_plane.workflows.odoo_prod_retained_volume_backup_import import (
    build_odoo_prod_retained_volume_backup_import_plan,
    execute_odoo_prod_retained_volume_backup_import_apply,
)
from tests.support.durable_operations import durable_operation_authorization_payload


PRODUCT = "example-odoo-product"
CONTEXT = "example-context"
BACKUP_RECORD_ID = "retained-import-20260726"
ARTIFACT_ID = "artifact-prod-current"
SOURCE_COMMIT = "abc1234"
IMAGE_REFERENCE = "ghcr.io/example/odoo@sha256:" + "a" * 64
ACTIVE_DB_VOLUME = "example_prod_db"
ACTIVE_DATA_VOLUME = "example_prod_data"
ACTIVE_LOG_VOLUME = "example_prod_logs"
SOURCE_DB_VOLUME = "example_retained_db"
SOURCE_DATA_VOLUME = "example_retained_data"
STAGING_CLONE_VOLUME = "example_retained_db_clone"
SOURCE_DATABASE_NAME = "example_legacy"
DESTINATION_DATABASE_NAME = "example_prod"
DATABASE_USER = "odoo"
SOURCE_COMPOSE_PROJECT = "example-retained"
ACTIVE_COMPOSE_PROJECT = "example-prod-app"
BACKUP_ROOT = "/volumes/data/backups/launchplane"
FILESTORE_PATH = "/volumes/data/filestore"
PG_CONTROL_SHA256 = "b" * 64
POSTGRES_IMAGE_ID = "sha256:" + "c" * 64
SCRIPT_RUNNER_IMAGE_ID = "sha256:" + "d" * 64


def _request() -> OdooProdRetainedVolumeBackupImportRequest:
    return OdooProdRetainedVolumeBackupImportRequest(
        product=PRODUCT,
        context=CONTEXT,
        backup_record_id=BACKUP_RECORD_ID,
        expected_current_artifact_id=ARTIFACT_ID,
        expected_active_db_volume=ACTIVE_DB_VOLUME,
        expected_active_data_volume=ACTIVE_DATA_VOLUME,
        expected_active_log_volume=ACTIVE_LOG_VOLUME,
        source_db_volume=SOURCE_DB_VOLUME,
        source_data_volume=SOURCE_DATA_VOLUME,
        source_database_name=SOURCE_DATABASE_NAME,
        expected_destination_database_name=DESTINATION_DATABASE_NAME,
        expected_database_user=DATABASE_USER,
        staging_clone_volume=STAGING_CLONE_VOLUME,
        expected_source_compose_project=SOURCE_COMPOSE_PROJECT,
    )


def _inspection_payload(
    *,
    inspection_nonce: str = "e" * 64,
    deployment_id: str = "schedule-inspect-1",
    source_pg_cluster_state: str = "shut down",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "inspection_nonce": inspection_nonce,
        "backup_record_id": BACKUP_RECORD_ID,
        "active_db_volume": ACTIVE_DB_VOLUME,
        "active_data_volume": ACTIVE_DATA_VOLUME,
        "active_log_volume": ACTIVE_LOG_VOLUME,
        "source_db_volume": SOURCE_DB_VOLUME,
        "source_data_volume": SOURCE_DATA_VOLUME,
        "staging_clone_volume": STAGING_CLONE_VOLUME,
        "source_database_name": SOURCE_DATABASE_NAME,
        "destination_database_name": DESTINATION_DATABASE_NAME,
        "database_user": DATABASE_USER,
        "source_db_project_label": SOURCE_COMPOSE_PROJECT,
        "source_db_role_label": "odoo_db",
        "source_data_project_label": SOURCE_COMPOSE_PROJECT,
        "source_data_role_label": "odoo_data",
        "source_pg_version": "17",
        "source_pg_control_sha256": PG_CONTROL_SHA256,
        "source_pg_control_size": 8192,
        "source_pg_system_identifier": "7654321098765432109",
        "source_pg_cluster_state": source_pg_cluster_state,
        "source_pg_checkpoint_location": "0/4000028",
        "source_pg_checkpoint_redo_location": "0/4000028",
        "source_pg_checkpoint_timeline_id": "1",
        "source_pg_checkpoint_time": "2026-07-20 12:00:00 UTC",
        "source_db_volume_used_bytes": 1_000_000_000,
        "source_filestore_file_count": 42,
        "source_filestore_size_bytes": 100_000_000,
        "active_data_free_bytes": 5_000_000_000,
        "staging_clone_volume_absent": True,
        "backup_destination_absent": True,
        "postgres_image_id": POSTGRES_IMAGE_ID,
        "script_runner_image_id": SCRIPT_RUNNER_IMAGE_ID,
        "inspection_deployment_id": deployment_id,
    }


class _Store:
    def __init__(self) -> None:
        self.runtime_identity = RuntimeIdentity(
            product=PRODUCT,
            context=CONTEXT,
            instance="prod",
            deployment_record_id="deployment-current",
            artifact_id=ARTIFACT_ID,
            source_git_ref=SOURCE_COMMIT,
            image_reference=IMAGE_REFERENCE,
        )
        self.profile = LaunchplaneProductProfileRecord(
            product=PRODUCT,
            display_name="Example Odoo",
            repository="example/odoo",
            driver_id="odoo",
            image=ProductImageProfile(repository="ghcr.io/example/odoo"),
            runtime_port=8069,
            health_path="/web/health",
            lanes=(
                ProductLaneProfile(
                    instance="prod",
                    context=CONTEXT,
                    base_url="https://prod.example.test",
                    health_url="https://prod.example.test/launchplane/health",
                    health_monitoring=ProductLaneHealthMonitoringPolicy(checks=()),
                ),
            ),
            preview=ProductPreviewProfile(enabled=False),
            updated_at="2026-07-26T00:00:00Z",
            source="test",
        )
        self.target = DokployTargetRecord(
            context=CONTEXT,
            instance="prod",
            project_name="odoo",
            target_type="compose",
            target_name="example-prod",
            domains=("prod.example.test",),
            updated_at="2026-07-26T00:00:00Z",
        )
        self.target_id = DokployTargetIdRecord(
            context=CONTEXT,
            instance="prod",
            target_id="compose-example-prod",
            updated_at="2026-07-26T00:00:00Z",
        )
        self.inventory = EnvironmentInventory(
            context=CONTEXT,
            instance="prod",
            artifact_identity=ArtifactIdentityReference(artifact_id=ARTIFACT_ID),
            source_git_ref=SOURCE_COMMIT,
            deploy=DeploymentEvidence(
                status="pass",
                target_type="compose",
                target_name="example-prod",
                deploy_mode="dokploy-compose-api",
            ),
            runtime_identity=self.runtime_identity,
            updated_at="2026-07-26T00:00:00Z",
            deployment_record_id="deployment-current",
        )
        self.artifact = ArtifactIdentityManifest(
            artifact_id=ARTIFACT_ID,
            source_commit=SOURCE_COMMIT,
            enterprise_base_digest="sha256:" + "f" * 64,
            image=ArtifactImageReference(
                repository="ghcr.io/example/odoo",
                digest="sha256:" + "a" * 64,
                tags=("prod",),
            ),
        )
        self.runtime_records = (
            RuntimeEnvironmentRecord(
                scope="instance",
                context=CONTEXT,
                instance="prod",
                env={
                    "ODOO_DB_NAME": DESTINATION_DATABASE_NAME,
                    "ODOO_DB_USER": DATABASE_USER,
                    "ODOO_DB_VOLUME": ACTIVE_DB_VOLUME,
                    "ODOO_DATA_VOLUME": ACTIVE_DATA_VOLUME,
                    "ODOO_LOG_VOLUME": ACTIVE_LOG_VOLUME,
                    "ODOO_BACKUP_ROOT": BACKUP_ROOT,
                    "ODOO_FILESTORE_PATH": FILESTORE_PATH,
                },
                updated_at="2026-07-26T00:00:00Z",
                source_label="test",
            ),
        )
        self.backup_records: dict[str, BackupGateRecord] = {}
        self.deployment_records: dict[str, DeploymentRecord] = {}

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != PRODUCT:
            raise FileNotFoundError(product)
        return self.profile

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord:
        if (context_name, instance_name) != (CONTEXT, "prod"):
            raise FileNotFoundError
        return self.target

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord:
        if (context_name, instance_name) != (CONTEXT, "prod"):
            raise FileNotFoundError
        return self.target_id

    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory:
        if (context_name, instance_name) != (CONTEXT, "prod"):
            raise FileNotFoundError
        return self.inventory

    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest:
        if artifact_id != ARTIFACT_ID:
            raise FileNotFoundError(artifact_id)
        return self.artifact

    def read_deployment_record(self, record_id: str) -> DeploymentRecord:
        try:
            return self.deployment_records[record_id]
        except KeyError as error:
            raise FileNotFoundError(record_id) from error

    def read_backup_gate_record(self, record_id: str) -> BackupGateRecord:
        try:
            return self.backup_records[record_id]
        except KeyError as error:
            raise FileNotFoundError(record_id) from error

    def write_backup_gate_record(self, record: BackupGateRecord) -> None:
        self.backup_records[record.record_id] = record

    def list_runtime_environment_records(
        self, *, context_name: str = "", instance_name: str = ""
    ) -> tuple[RuntimeEnvironmentRecord, ...]:
        return tuple(
            record
            for record in self.runtime_records
            if (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
        )


def _target_payload(
    store: _Store,
    *,
    runtime_identity: RuntimeIdentity | None = None,
) -> dict[str, object]:
    resolved_runtime_identity = runtime_identity or store.runtime_identity
    live_env = {
        "ODOO_DB_NAME": DESTINATION_DATABASE_NAME,
        "ODOO_DB_USER": DATABASE_USER,
        "ODOO_DB_VOLUME": ACTIVE_DB_VOLUME,
        "ODOO_DATA_VOLUME": ACTIVE_DATA_VOLUME,
        "ODOO_LOG_VOLUME": ACTIVE_LOG_VOLUME,
        **runtime_identity_env(resolved_runtime_identity),
    }
    return {
        "name": "example-prod",
        "appName": ACTIVE_COMPOSE_PROJECT,
        "serverId": "server-example",
        "env": "\n".join(f"{key}={value}" for key, value in live_env.items()),
    }


def _failed_deployment_record(runtime_identity: RuntimeIdentity) -> DeploymentRecord:
    return DeploymentRecord(
        record_id=runtime_identity.deployment_record_id,
        artifact_identity=ArtifactIdentityReference(artifact_id=runtime_identity.artifact_id),
        context=runtime_identity.context,
        instance=runtime_identity.instance,
        source_git_ref=runtime_identity.source_git_ref,
        resolved_target=ResolvedTargetEvidence(
            target_type="compose",
            target_id="compose-example-prod",
            target_name="example-prod",
        ),
        runtime_identity=runtime_identity,
        deploy=DeploymentEvidence(
            target_name="example-prod",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            status="fail",
        ),
    )


def _build_plan(store: _Store) -> OdooProdRetainedVolumeBackupImportPlan:
    def inspect(**kwargs: object) -> dict[str, object]:
        callback = kwargs.get("before_provider_mutation")
        if callable(callback):
            callback("schedule_upsert")
            callback("schedule_trigger")
        return _inspection_payload(inspection_nonce=str(kwargs["inspection_nonce"]))

    with (
        patch(
            "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_source.read_dokploy_config",
            return_value=("https://dokploy.example.test", "token"),
        ),
        patch(
            "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_api.fetch_dokploy_target_payload",
            return_value=_target_payload(store),
        ),
        patch(
            "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_post_deploy.run_compose_odoo_retained_volume_backup_import_inspection",
            side_effect=inspect,
        ),
    ):
        return build_odoo_prod_retained_volume_backup_import_plan(
            control_plane_root=Path("."),
            record_store=store,
            operation_id="plan-operation-1",
            request=_request(),
            phase_checkpoint=lambda _phase, _evidence: None,
            provider_effect_checkpoint=lambda _phase, _effect: None,
        )


def _operation(
    *,
    operation_kind: str = "plan",
    operation_id: str = "retained-import-operation-1",
) -> OdooProdRetainedVolumeBackupImportOperationRecord:
    request = _request()
    plan = _build_plan(_Store())
    authorization_action = (
        "odoo_prod_retained_volume_backup_import_plan.execute"
        if operation_kind == "plan"
        else "odoo_prod_retained_volume_backup_import_apply.execute"
    )
    authorization = durable_operation_authorization_payload(
        action=authorization_action,
        managed_rule_id=f"retained-import-{operation_kind}",
        product=PRODUCT,
        context=CONTEXT,
        instances=("prod",),
    )
    operation_request: object = request
    operation_plan: OdooProdRetainedVolumeBackupImportPlan | None = None
    if operation_kind == "apply":
        operation_request = OdooProdRetainedVolumeBackupImportApplyRequest(
            **request.model_dump(),
            plan_operation_id="retained-import-plan-operation-1",
            plan_fingerprint=plan.plan_fingerprint,
            confirmation=ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_CONFIRMATION,
        )
        operation_plan = plan
    return OdooProdRetainedVolumeBackupImportOperationRecord.model_validate(
        {
            "operation_id": operation_id,
            "operation_kind": operation_kind,
            "product": PRODUCT,
            "context": CONTEXT,
            "instance": "prod",
            "idempotency_key": f"idempotency-{operation_kind}",
            "idempotency_scope": "github-actions:example",
            "request_fingerprint": "f" * 64,
            "request": operation_request,
            "plan": operation_plan,
            "authorization": authorization,
            "created_at": "2026-07-26T00:00:00Z",
            "updated_at": "2026-07-26T00:00:00Z",
        }
    )


class OdooProdRetainedVolumeBackupImportTests(unittest.TestCase):
    def test_request_rejects_source_or_staging_volume_aliases(self) -> None:
        payload = _request().model_dump()
        payload["source_db_volume"] = ACTIVE_DB_VOLUME
        with self.assertRaisesRegex(ValueError, "must not be active"):
            OdooProdRetainedVolumeBackupImportRequest.model_validate(payload)

        payload = _request().model_dump()
        payload["staging_clone_volume"] = SOURCE_DATA_VOLUME
        with self.assertRaisesRegex(ValueError, "fresh and distinct"):
            OdooProdRetainedVolumeBackupImportRequest.model_validate(payload)

    def test_plan_binds_runtime_and_read_only_provider_evidence(self) -> None:
        store = _Store()
        provider_effects: list[str] = []
        phases: list[str] = []

        def inspect(**kwargs: object) -> dict[str, object]:
            callback = cast(Callable[[str], None], kwargs["before_provider_mutation"])
            callback("schedule_upsert")
            callback("schedule_trigger")
            return _inspection_payload(
                inspection_nonce=str(kwargs["inspection_nonce"]),
                deployment_id="schedule-inspect-first",
            )

        with (
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.test", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_api.fetch_dokploy_target_payload",
                return_value=_target_payload(store),
            ),
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_post_deploy.run_compose_odoo_retained_volume_backup_import_inspection",
                side_effect=inspect,
            ),
        ):
            first_plan = build_odoo_prod_retained_volume_backup_import_plan(
                control_plane_root=Path("."),
                record_store=store,
                operation_id="plan-operation-1",
                request=_request(),
                phase_checkpoint=lambda phase, _evidence: phases.append(phase),
                provider_effect_checkpoint=lambda _phase, effect: provider_effects.append(effect),
            )
            second_plan = build_odoo_prod_retained_volume_backup_import_plan(
                control_plane_root=Path("."),
                record_store=store,
                operation_id="plan-operation-2",
                request=_request(),
                phase_checkpoint=lambda _phase, _evidence: None,
                provider_effect_checkpoint=lambda _phase, _effect: None,
            )

        self.assertEqual(first_plan.plan_status, "ready")
        self.assertEqual(first_plan.runtime_identity, store.runtime_identity)
        self.assertEqual(first_plan.source_pg_version, "17")
        self.assertEqual(first_plan.source_pg_control_sha256, PG_CONTROL_SHA256)
        self.assertEqual(first_plan.source_db_role_label, "odoo_db")
        self.assertEqual(first_plan.source_data_role_label, "odoo_data")
        self.assertTrue(first_plan.staging_clone_volume_absent)
        self.assertTrue(first_plan.backup_destination_absent)
        self.assertGreaterEqual(
            first_plan.active_data_free_bytes,
            first_plan.active_data_required_bytes,
        )
        self.assertEqual(provider_effects, ["schedule_upsert", "schedule_trigger"])
        self.assertIn("inspected", phases)
        self.assertIn("planned", phases)
        self.assertEqual(first_plan.plan_fingerprint, second_plan.plan_fingerprint)
        self.assertNotEqual(first_plan.inspection_nonce, second_plan.inspection_nonce)

    def test_plan_fails_closed_before_provider_inspection_on_runtime_drift(self) -> None:
        store = _Store()
        store.runtime_records = (
            store.runtime_records[0].model_copy(
                update={
                    "env": {
                        **store.runtime_records[0].env,
                        "ODOO_DB_VOLUME": "drifted-active-db",
                    }
                }
            ),
        )
        with (
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.test", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_api.fetch_dokploy_target_payload",
                return_value=_target_payload(store),
            ),
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_post_deploy.run_compose_odoo_retained_volume_backup_import_inspection"
            ) as inspect_mock,
        ):
            plan = build_odoo_prod_retained_volume_backup_import_plan(
                control_plane_root=Path("."),
                record_store=store,
                operation_id="plan-operation-1",
                request=_request(),
                phase_checkpoint=lambda _phase, _evidence: None,
                provider_effect_checkpoint=lambda _phase, _effect: None,
            )

        self.assertEqual(plan.plan_status, "blocked")
        self.assertEqual(plan.plan_fingerprint, "")
        self.assertTrue(any("ODOO_DB_VOLUME" in blocker for blocker in plan.blockers))
        inspect_mock.assert_not_called()

    def test_plan_accepts_recorded_failed_deployment_identity_for_repair(self) -> None:
        store = _Store()
        live_runtime_identity = store.runtime_identity.model_copy(
            update={"deployment_record_id": "deployment-failed-replacement"}
        )
        store.deployment_records[live_runtime_identity.deployment_record_id] = (
            _failed_deployment_record(live_runtime_identity)
        )

        def inspect(**kwargs: object) -> dict[str, object]:
            return _inspection_payload(inspection_nonce=str(kwargs["inspection_nonce"]))

        with (
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.test", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_api.fetch_dokploy_target_payload",
                return_value=_target_payload(
                    store,
                    runtime_identity=live_runtime_identity,
                ),
            ),
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_post_deploy.run_compose_odoo_retained_volume_backup_import_inspection",
                side_effect=inspect,
            ),
        ):
            plan = build_odoo_prod_retained_volume_backup_import_plan(
                control_plane_root=Path("."),
                record_store=store,
                operation_id="plan-operation-1",
                request=_request(),
                phase_checkpoint=lambda _phase, _evidence: None,
                provider_effect_checkpoint=lambda _phase, _effect: None,
            )

        self.assertEqual(plan.plan_status, "ready", plan.blockers)
        self.assertEqual(plan.runtime_identity, live_runtime_identity)
        self.assertTrue(any("recorded failed deployment" in warning for warning in plan.warnings))

    def test_plan_rejects_unrecorded_or_changed_failed_deployment_identity(self) -> None:
        authority_changes: tuple[dict[str, object] | None, ...] = (
            None,
            {"schema_version": 2},
            {"product": "product-drifted"},
            {"context": "context-drifted"},
            {"instance": "testing"},
            {"environment_kind": "preview"},
            {"artifact_id": "artifact-drifted"},
            {"source_git_ref": "source-drifted"},
            {"image_reference": "ghcr.io/example/odoo@sha256:" + "9" * 64},
            {"release_tuple_id": "release-drifted"},
            {"preview_id": "preview-drifted"},
            {"preview_generation_id": "generation-drifted"},
            {"deployed_at": "2026-07-26T07:00:00Z"},
        )
        for authority_change in authority_changes:
            with self.subTest(authority_change=authority_change):
                store = _Store()
                live_runtime_identity = store.runtime_identity.model_copy(
                    update={
                        "deployment_record_id": "deployment-failed-replacement",
                        **(authority_change or {}),
                    }
                )
                if authority_change is not None:
                    store.deployment_records[live_runtime_identity.deployment_record_id] = (
                        _failed_deployment_record(live_runtime_identity)
                    )
                with (
                    patch(
                        "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_source.read_dokploy_config",
                        return_value=("https://dokploy.example.test", "token"),
                    ),
                    patch(
                        "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_api.fetch_dokploy_target_payload",
                        return_value=_target_payload(
                            store,
                            runtime_identity=live_runtime_identity,
                        ),
                    ),
                    patch(
                        "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_post_deploy.run_compose_odoo_retained_volume_backup_import_inspection"
                    ) as inspect_mock,
                ):
                    plan = build_odoo_prod_retained_volume_backup_import_plan(
                        control_plane_root=Path("."),
                        record_store=store,
                        operation_id="plan-operation-1",
                        request=_request(),
                        phase_checkpoint=lambda _phase, _evidence: None,
                        provider_effect_checkpoint=lambda _phase, _effect: None,
                    )

                self.assertEqual(plan.plan_status, "blocked")
                self.assertTrue(
                    any("runtime identity drifted" in blocker for blocker in plan.blockers)
                )
                inspect_mock.assert_not_called()

    def test_plan_rejects_nonfailed_deployment_identity_provenance(self) -> None:
        store = _Store()
        live_runtime_identity = store.runtime_identity.model_copy(
            update={"deployment_record_id": "deployment-not-failed"}
        )
        deployment_record = _failed_deployment_record(live_runtime_identity)
        store.deployment_records[live_runtime_identity.deployment_record_id] = (
            deployment_record.model_copy(
                update={"deploy": deployment_record.deploy.model_copy(update={"status": "pass"})}
            )
        )
        with (
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.test", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_api.fetch_dokploy_target_payload",
                return_value=_target_payload(
                    store,
                    runtime_identity=live_runtime_identity,
                ),
            ),
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_post_deploy.run_compose_odoo_retained_volume_backup_import_inspection"
            ) as inspect_mock,
        ):
            plan = build_odoo_prod_retained_volume_backup_import_plan(
                control_plane_root=Path("."),
                record_store=store,
                operation_id="plan-operation-1",
                request=_request(),
                phase_checkpoint=lambda _phase, _evidence: None,
                provider_effect_checkpoint=lambda _phase, _effect: None,
            )

        self.assertEqual(plan.plan_status, "blocked")
        inspect_mock.assert_not_called()

    def test_plan_rejects_unbound_inspection_nonce(self) -> None:
        store = _Store()
        with (
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.test", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_api.fetch_dokploy_target_payload",
                return_value=_target_payload(store),
            ),
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_post_deploy.run_compose_odoo_retained_volume_backup_import_inspection",
                return_value=_inspection_payload(inspection_nonce="f" * 64),
            ),
        ):
            plan = build_odoo_prod_retained_volume_backup_import_plan(
                control_plane_root=Path("."),
                record_store=store,
                operation_id="plan-operation-1",
                request=_request(),
                phase_checkpoint=lambda _phase, _evidence: None,
                provider_effect_checkpoint=lambda _phase, _effect: None,
            )

        self.assertEqual(plan.plan_status, "blocked")
        self.assertTrue(any("exact reviewed request" in blocker for blocker in plan.blockers))

    def test_plan_requires_cleanly_shut_down_source_cluster(self) -> None:
        store = _Store()

        def inspect(**kwargs: object) -> dict[str, object]:
            return _inspection_payload(
                inspection_nonce=str(kwargs["inspection_nonce"]),
                source_pg_cluster_state="in production",
            )

        with (
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.test", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_api.fetch_dokploy_target_payload",
                return_value=_target_payload(store),
            ),
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_post_deploy.run_compose_odoo_retained_volume_backup_import_inspection",
                side_effect=inspect,
            ),
        ):
            plan = build_odoo_prod_retained_volume_backup_import_plan(
                control_plane_root=Path("."),
                record_store=store,
                operation_id="plan-operation-1",
                request=_request(),
                phase_checkpoint=lambda _phase, _evidence: None,
                provider_effect_checkpoint=lambda _phase, _effect: None,
            )

        self.assertEqual(plan.plan_status, "blocked")
        self.assertTrue(any("cleanly shut down" in blocker for blocker in plan.blockers))

    def test_plan_requires_explicit_filestore_and_compose_project_authority(self) -> None:
        for missing_authority in ("filestore_path", "compose_project"):
            with self.subTest(missing_authority=missing_authority):
                store = _Store()
                if missing_authority == "filestore_path":
                    runtime_record = store.runtime_records[0]
                    store.runtime_records = (
                        runtime_record.model_copy(
                            update={
                                "env": {
                                    key: value
                                    for key, value in runtime_record.env.items()
                                    if key != "ODOO_FILESTORE_PATH"
                                }
                            }
                        ),
                    )
                target_payload = _target_payload(store)
                if missing_authority == "compose_project":
                    target_payload.pop("appName")
                with (
                    patch(
                        "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_source.read_dokploy_config",
                        return_value=("https://dokploy.example.test", "token"),
                    ),
                    patch(
                        "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_api.fetch_dokploy_target_payload",
                        return_value=target_payload,
                    ),
                    patch(
                        "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_post_deploy.run_compose_odoo_retained_volume_backup_import_inspection"
                    ) as inspect_mock,
                ):
                    plan = build_odoo_prod_retained_volume_backup_import_plan(
                        control_plane_root=Path("."),
                        record_store=store,
                        operation_id="plan-operation-1",
                        request=_request(),
                        phase_checkpoint=lambda _phase, _evidence: None,
                        provider_effect_checkpoint=lambda _phase, _effect: None,
                    )

                self.assertEqual(plan.plan_status, "blocked")
                inspect_mock.assert_not_called()

    def test_apply_writes_distinct_standard_backup_gate_without_cutover(self) -> None:
        store = _Store()
        reviewed_plan = _build_plan(store)
        apply_request = OdooProdRetainedVolumeBackupImportApplyRequest(
            **_request().model_dump(),
            plan_operation_id="retained-import-plan-operation-1",
            plan_fingerprint=reviewed_plan.plan_fingerprint,
            confirmation=ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_CONFIRMATION,
        )
        provider_effects: list[str] = []

        def inspect(**kwargs: object) -> dict[str, object]:
            callback = cast(Callable[[str], None], kwargs["before_provider_mutation"])
            callback("schedule_upsert")
            callback("schedule_trigger")
            return _inspection_payload(inspection_nonce=str(kwargs["inspection_nonce"]))

        def apply_provider(**kwargs: object) -> dict[str, object]:
            callback = cast(Callable[[str], None], kwargs["before_provider_mutation"])
            callback("schedule_upsert")
            callback("schedule_trigger")
            return {
                "schema_version": 1,
                "import_nonce": kwargs["import_nonce"],
                "operation_id": kwargs["operation_id"],
                "plan_fingerprint": reviewed_plan.plan_fingerprint,
                "backup_record_id": BACKUP_RECORD_ID,
                "source_db_volume": SOURCE_DB_VOLUME,
                "source_data_volume": SOURCE_DATA_VOLUME,
                "staging_clone_volume": STAGING_CLONE_VOLUME,
                "source_database_name": SOURCE_DATABASE_NAME,
                "destination_database_name": DESTINATION_DATABASE_NAME,
                "database_user": DATABASE_USER,
                "source_pg_control_sha256": PG_CONTROL_SHA256,
                "clone_pg_control_sha256": PG_CONTROL_SHA256,
                "database_dump_sha256": "1" * 64,
                "filestore_archive_sha256": "2" * 64,
                "database_dump_size": 1234,
                "filestore_archive_size": 5678,
                "source_filestore_file_count": 42,
                "source_filestore_size_bytes": 100_000_000,
                "schedule_deployment_id": "schedule-apply-1",
            }

        with (
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.test", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_api.fetch_dokploy_target_payload",
                return_value=_target_payload(store),
            ),
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_post_deploy.run_compose_odoo_retained_volume_backup_import_inspection",
                side_effect=inspect,
            ),
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_post_deploy.run_compose_odoo_retained_volume_backup_import_apply",
                side_effect=apply_provider,
            ),
        ):
            result = execute_odoo_prod_retained_volume_backup_import_apply(
                control_plane_root=Path("."),
                record_store=store,
                operation_id="retained-import-apply-1",
                reviewed_plan=reviewed_plan,
                request=apply_request,
                phase_checkpoint=lambda _phase, _evidence: None,
                provider_effect_checkpoint=lambda _phase, effect: provider_effects.append(effect),
            )

        backup_record = store.backup_records[BACKUP_RECORD_ID]
        self.assertEqual(result.import_status, "pass")
        self.assertEqual(result.backup_status, "pass")
        self.assertEqual(result.schedule_deployment_id, "schedule-apply-1")
        self.assertEqual(backup_record.source, RETAINED_VOLUME_BACKUP_IMPORT_SOURCE)
        self.assertEqual(backup_record.status, "pass")
        self.assertEqual(backup_record.evidence["database_name"], DESTINATION_DATABASE_NAME)
        self.assertTrue(
            backup_record.evidence["database_dump_path"].endswith(
                f"/{DESTINATION_DATABASE_NAME}.dump"
            )
        )
        self.assertEqual(
            provider_effects,
            ["schedule_upsert", "schedule_trigger", "schedule_upsert", "schedule_trigger"],
        )

    def test_plan_fingerprint_changes_when_pg_control_changes(self) -> None:
        plan = _build_plan(_Store())
        changed = plan.model_copy(update={"source_pg_control_sha256": "9" * 64})
        self.assertNotEqual(
            build_odoo_prod_retained_volume_backup_import_plan_fingerprint(plan),
            build_odoo_prod_retained_volume_backup_import_plan_fingerprint(changed),
        )

    def test_plan_fingerprint_binds_live_failed_deployment_identity(self) -> None:
        plan = _build_plan(_Store())
        assert plan.runtime_identity is not None
        changed = plan.model_copy(
            update={
                "runtime_identity": plan.runtime_identity.model_copy(
                    update={"deployment_record_id": "deployment-changed-after-review"}
                )
            }
        )
        self.assertNotEqual(
            build_odoo_prod_retained_volume_backup_import_plan_fingerprint(plan),
            build_odoo_prod_retained_volume_backup_import_plan_fingerprint(changed),
        )

    def test_same_apply_operation_can_recover_after_pending_record_write(self) -> None:
        store = _Store()
        operation_id = "retained-import-apply-1"
        store.backup_records[BACKUP_RECORD_ID] = BackupGateRecord(
            record_id=BACKUP_RECORD_ID,
            context=CONTEXT,
            instance="prod",
            created_at="2026-07-26T00:00:00Z",
            source=RETAINED_VOLUME_BACKUP_IMPORT_SOURCE,
            required=True,
            status="pending",
            evidence={"operation_id": operation_id},
        )

        def inspect(**kwargs: object) -> dict[str, object]:
            return _inspection_payload(inspection_nonce=str(kwargs["inspection_nonce"]))

        with (
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.test", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_api.fetch_dokploy_target_payload",
                return_value=_target_payload(store),
            ),
            patch(
                "control_plane.workflows.odoo_prod_retained_volume_backup_import.dokploy_post_deploy.run_compose_odoo_retained_volume_backup_import_inspection",
                side_effect=inspect,
            ),
        ):
            plan = build_odoo_prod_retained_volume_backup_import_plan(
                control_plane_root=Path("."),
                record_store=store,
                operation_id=operation_id,
                request=_request(),
                phase_checkpoint=lambda _phase, _evidence: None,
                provider_effect_checkpoint=lambda _phase, _effect: None,
            )

        self.assertEqual(plan.plan_status, "ready")

    def test_provider_scripts_mount_sources_read_only_and_preserve_clone(self) -> None:
        inspection_script = (
            dokploy_post_deploy._build_dokploy_odoo_retained_volume_backup_import_inspection_script(
                compose_app_name=ACTIVE_COMPOSE_PROJECT,
                inspection_nonce="e" * 64,
                backup_record_id=BACKUP_RECORD_ID,
                active_db_volume=ACTIVE_DB_VOLUME,
                active_data_volume=ACTIVE_DATA_VOLUME,
                active_log_volume=ACTIVE_LOG_VOLUME,
                source_db_volume=SOURCE_DB_VOLUME,
                source_data_volume=SOURCE_DATA_VOLUME,
                staging_clone_volume=STAGING_CLONE_VOLUME,
                source_database_name=SOURCE_DATABASE_NAME,
                destination_database_name=DESTINATION_DATABASE_NAME,
                database_user=DATABASE_USER,
                expected_source_compose_project=SOURCE_COMPOSE_PROJECT,
                filestore_relative_path="filestore",
                backup_dir_relative_path=(
                    f"backups/launchplane/{DESTINATION_DATABASE_NAME}/{BACKUP_RECORD_ID}"
                ),
            )
        )
        apply_script = (
            dokploy_post_deploy._build_dokploy_odoo_retained_volume_backup_import_apply_script(
                compose_app_name=ACTIVE_COMPOSE_PROJECT,
                import_nonce="6" * 64,
                operation_id="retained-import-apply-1",
                plan_fingerprint="7" * 64,
                backup_record_id=BACKUP_RECORD_ID,
                active_db_volume=ACTIVE_DB_VOLUME,
                active_data_volume=ACTIVE_DATA_VOLUME,
                active_log_volume=ACTIVE_LOG_VOLUME,
                source_db_volume=SOURCE_DB_VOLUME,
                source_data_volume=SOURCE_DATA_VOLUME,
                staging_clone_volume=STAGING_CLONE_VOLUME,
                source_database_name=SOURCE_DATABASE_NAME,
                destination_database_name=DESTINATION_DATABASE_NAME,
                database_user=DATABASE_USER,
                expected_source_compose_project=SOURCE_COMPOSE_PROJECT,
                expected_source_pg_control_sha256=PG_CONTROL_SHA256,
                expected_source_pg_version="17",
                expected_postgres_image_id=POSTGRES_IMAGE_ID,
                expected_script_runner_image_id=SCRIPT_RUNNER_IMAGE_ID,
                expected_source_filestore_file_count=42,
                expected_source_filestore_size_bytes=100_000_000,
                expected_active_data_required_bytes=2_300_000_000,
                filestore_relative_path="filestore",
                backup_dir_relative_path=(
                    f"backups/launchplane/{DESTINATION_DATABASE_NAME}/{BACKUP_RECORD_ID}"
                ),
                database_dump_relative_path=(
                    f"backups/launchplane/{DESTINATION_DATABASE_NAME}/{BACKUP_RECORD_ID}/"
                    f"{DESTINATION_DATABASE_NAME}.dump"
                ),
                filestore_archive_relative_path=(
                    f"backups/launchplane/{DESTINATION_DATABASE_NAME}/{BACKUP_RECORD_ID}/"
                    f"{DESTINATION_DATABASE_NAME}-filestore.tar.gz"
                ),
                manifest_relative_path=(
                    f"backups/launchplane/{DESTINATION_DATABASE_NAME}/{BACKUP_RECORD_ID}/"
                    "manifest.json"
                ),
            )
        )

        combined = inspection_script + apply_script
        self.assertNotIn("pg_resetwal", combined)
        self.assertNotIn("update_dokploy_target_env", combined)
        self.assertNotIn("trigger_deployment", combined)
        source_mount_lines = tuple(
            line
            for line in combined.splitlines()
            if "src=${source_db_volume}" in line or "src=${source_data_volume}" in line
        )
        self.assertTrue(source_mount_lines)
        self.assertTrue(all("readonly" in line for line in source_mount_lines))
        self.assertIn("cp -a /source/. /clone/", apply_script)
        self.assertIn("io.launchplane.recovery=true", apply_script)
        self.assertIn("docker network create --internal", apply_script)
        self.assertIn("clone_container_created=0", apply_script)
        self.assertIn('if [ "${clone_container_created}" = "1" ]', apply_script)
        self.assertIn("docker create", apply_script)
        self.assertIn('docker start "${clone_container}"', apply_script)
        self.assertIn("container_mounts_volume", combined)
        self.assertIn("active_destination_database_name", combined)
        self.assertIn('"postgres (PostgreSQL) 17."*', combined)
        self.assertEqual(
            combined.count("resolve_single_container_any_state database"),
            2,
        )
        self.assertEqual(combined.count("resolve_single_container script-runner"), 2)
        self.assertNotIn("resolve_single_container_any_state script-runner", combined)
        self.assertEqual(combined.count("docker ps -aq"), 2)
        self.assertIn("created_staging_clone_volume", apply_script)
        self.assertIn('mkdir -m 700 "$backup_dir"', apply_script)
        self.assertIn("pg_dump --host /var/run/postgresql", apply_script)
        self.assertIn("tarfile.open", apply_script)
        self.assertIn("schema_version", apply_script)
        self.assertIn("trap cleanup EXIT", apply_script)
        self.assertNotIn('docker volume rm "${staging_clone_volume}"', apply_script)
        for script in (inspection_script, apply_script):
            syntax_check = subprocess.run(
                ["bash", "-n"],
                input=script,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(syntax_check.returncode, 0, msg=syntax_check.stderr)

    def test_provider_inspection_binds_exact_schedule_deployment(self) -> None:
        target = DokployTargetDefinition(
            context=CONTEXT,
            instance="prod",
            target_id="compose-example-prod",
            target_name="example-prod",
        )
        marker_payload = _inspection_payload(
            inspection_nonce="e" * 64,
            deployment_id="unused-in-marker",
        )
        marker_payload.pop("inspection_deployment_id")
        encoded = base64.b64encode(
            json.dumps(marker_payload, sort_keys=True).encode("utf-8")
        ).decode("ascii")
        with (
            patch(
                "control_plane.dokploy.post_deploy.api.fetch_dokploy_target_payload",
                return_value={"appName": ACTIVE_COMPOSE_PROJECT, "serverId": "server-1"},
            ),
            patch(
                "control_plane.dokploy.post_deploy.api.upsert_dokploy_schedule",
                return_value={"scheduleId": "schedule-1"},
            ),
            patch(
                "control_plane.dokploy.post_deploy.api.latest_deployment_for_schedule",
                return_value={"deploymentId": "before"},
            ),
            patch(
                "control_plane.dokploy.post_deploy.api.dokploy_request",
                return_value={"ok": True},
            ),
            patch(
                "control_plane.dokploy.post_deploy.api.wait_for_dokploy_schedule_deployment",
                return_value="deployment=exact-deployment status=done",
            ),
            patch(
                "control_plane.dokploy.post_deploy.api.fetch_dokploy_deployment_logs",
                return_value=(
                    f"{dokploy_post_deploy.ODOO_RETAINED_VOLUME_BACKUP_IMPORT_INSPECT_RESULT_MARKER}={encoded}",
                ),
            ) as logs_mock,
        ):
            result = dokploy_post_deploy.run_compose_odoo_retained_volume_backup_import_inspection(
                host="https://dokploy.example.test",
                token="token",
                target_definition=target,
                expected_compose_app_name=ACTIVE_COMPOSE_PROJECT,
                inspection_nonce="e" * 64,
                backup_record_id=BACKUP_RECORD_ID,
                active_db_volume=ACTIVE_DB_VOLUME,
                active_data_volume=ACTIVE_DATA_VOLUME,
                active_log_volume=ACTIVE_LOG_VOLUME,
                source_db_volume=SOURCE_DB_VOLUME,
                source_data_volume=SOURCE_DATA_VOLUME,
                staging_clone_volume=STAGING_CLONE_VOLUME,
                source_database_name=SOURCE_DATABASE_NAME,
                destination_database_name=DESTINATION_DATABASE_NAME,
                database_user=DATABASE_USER,
                expected_source_compose_project=SOURCE_COMPOSE_PROJECT,
                filestore_relative_path="filestore",
                backup_dir_relative_path="backups/example",
            )

        self.assertEqual(result["inspection_deployment_id"], "exact-deployment")
        logs_mock.assert_called_once_with(
            host="https://dokploy.example.test",
            token="token",
            deployment_id="exact-deployment",
            line_count=dokploy_api.MAX_DOKPLOY_LOG_LINE_COUNT,
        )


if __name__ == "__main__":
    unittest.main()
