import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import click

from control_plane.contracts.artifact_identity import (
    ArtifactImageReference,
    ArtifactIdentityManifest,
)
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.odoo_prod_backup_restore import (
    ODOO_PROD_BACKUP_RESTORE_CONFIRMATION,
    OdooProdBackupRestoreApplyRequest,
    OdooProdBackupRestoreRequest,
)
from control_plane.contracts.odoo_prod_backup_restore_operation import (
    OdooProdBackupRestoreOperationRecord,
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
    HealthcheckEvidence,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_key_safety_policy import RuntimeKeySafetyPolicyRecord
from control_plane.contracts.secret_record import SecretBinding
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import post_deploy as dokploy_post_deploy
from control_plane.workflows.odoo_post_deploy import OdooPostDeployResult
from control_plane.workflows.odoo_prod_backup_gate import (
    BACKUP_GATE_SOURCE,
    BACKUP_VERIFICATION_SOURCE,
    RETAINED_VOLUME_BACKUP_IMPORT_SOURCE,
)
from control_plane.workflows.odoo_prod_backup_restore import (
    build_odoo_prod_backup_restore_plan,
    execute_odoo_prod_backup_restore_apply,
    execute_odoo_prod_backup_restore_verification_replay,
)
from control_plane.workflows.odoo_verification import (
    OdooVerificationEvidence,
    OdooVerificationResult,
)
from tests.support.durable_operations import durable_operation_authorization_payload


PRODUCT = "example-odoo-product"
CONTEXT = "example-context"
BACKUP_RECORD_ID = "backup-20260614"
VERIFICATION_RECORD_ID = "verification-backup-20260614"
ARTIFACT_ID = "artifact-prod-current"
SOURCE_COMMIT = "abc1234"
IMAGE_REFERENCE = "ghcr.io/example/odoo@sha256:artifact"
OLD_DB_VOLUME = "example_prod_db_corrupt"
NEW_DB_VOLUME = "example_prod_db_restore"
DATA_VOLUME = "example_prod_data"
LOG_VOLUME = "example_prod_logs"
DATABASE_NAME = "example_prod"
BASE_URL = "https://prod.example.test"
HEALTH_URL = f"{BASE_URL}/launchplane/health"
BACKUP_ROOT = "/volumes/data/backups/launchplane"
FILESTORE_PATH = "/volumes/data/filestore"
DATABASE_DUMP_SHA256 = "a" * 64
FILESTORE_ARCHIVE_SHA256 = "b" * 64


class _Store:
    def __init__(self) -> None:
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
                    base_url=BASE_URL,
                    health_url=HEALTH_URL,
                    health_monitoring=ProductLaneHealthMonitoringPolicy(checks=()),
                ),
            ),
            preview=ProductPreviewProfile(enabled=False),
            updated_at="2026-07-25T00:00:00Z",
            source="test",
        )
        self.target_record = DokployTargetRecord(
            context=CONTEXT,
            instance="prod",
            project_name="odoo",
            target_type="compose",
            target_name="example-prod",
            domains=("prod.example.test",),
            updated_at="2026-07-25T00:00:00Z",
        )
        self.target_id_record = DokployTargetIdRecord(
            context=CONTEXT,
            instance="prod",
            target_id="compose-example-prod",
            updated_at="2026-07-25T00:00:00Z",
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
            updated_at="2026-07-25T00:00:00Z",
            deployment_record_id="deployment-current",
        )
        self.artifact_manifest = ArtifactIdentityManifest(
            artifact_id=ARTIFACT_ID,
            source_commit=SOURCE_COMMIT,
            enterprise_base_digest="sha256:enterprise",
            image=ArtifactImageReference(
                repository="ghcr.io/example/odoo",
                digest="sha256:artifact",
                tags=("prod",),
            ),
        )
        backup_dir = f"{BACKUP_ROOT}/{DATABASE_NAME}/{BACKUP_RECORD_ID}"
        backup_paths = {
            "database_name": DATABASE_NAME,
            "filestore_path": FILESTORE_PATH,
            "backup_root": BACKUP_ROOT,
            "backup_dir": backup_dir,
            "database_dump_path": f"{backup_dir}/{DATABASE_NAME}.dump",
            "filestore_archive_path": f"{backup_dir}/{DATABASE_NAME}-filestore.tar.gz",
            "manifest_path": f"{backup_dir}/manifest.json",
        }
        self.backup_records = {
            BACKUP_RECORD_ID: BackupGateRecord(
                record_id=BACKUP_RECORD_ID,
                context=CONTEXT,
                instance="prod",
                created_at="2026-06-14T12:00:00Z",
                source=BACKUP_GATE_SOURCE,
                required=True,
                status="pass",
                evidence=backup_paths,
            ),
            VERIFICATION_RECORD_ID: BackupGateRecord(
                record_id=VERIFICATION_RECORD_ID,
                context=CONTEXT,
                instance="prod",
                created_at="2026-07-25T01:00:00Z",
                source=BACKUP_VERIFICATION_SOURCE,
                required=True,
                status="pass",
                evidence={
                    "backup_record_id": BACKUP_RECORD_ID,
                    "backup_record_created_at": "2026-06-14T12:00:00Z",
                    **backup_paths,
                    "schema_version": "1",
                    "verification_nonce": "c" * 64,
                    "verification_status": "pass",
                    "manifest_status": "pass",
                    "sha256_status": "pass",
                    "pg_restore_status": "pass",
                    "tar_status": "pass",
                    "staging_space_status": "pass",
                    "database_dump_sha256": DATABASE_DUMP_SHA256,
                    "filestore_archive_sha256": FILESTORE_ARCHIVE_SHA256,
                    "database_dump_size": "1024",
                    "filestore_archive_size": "2048",
                    "pg_restore_entry_count": "200",
                    "filestore_member_count": "300",
                    "filestore_unpacked_size": "4096",
                    "data_volume_free_bytes": "8192",
                    "staging_required_bytes": "4096",
                    "failure_code": "",
                },
            ),
        }
        self.runtime_records = (
            RuntimeEnvironmentRecord(
                scope="instance",
                context=CONTEXT,
                instance="prod",
                env={
                    "ODOO_DB_NAME": DATABASE_NAME,
                    "ODOO_BACKUP_ROOT": BACKUP_ROOT,
                    "ODOO_FILESTORE_PATH": FILESTORE_PATH,
                    "ODOO_DB_VOLUME": NEW_DB_VOLUME,
                    "ODOO_DATA_VOLUME": DATA_VOLUME,
                    "ODOO_LOG_VOLUME": LOG_VOLUME,
                },
                updated_at="2026-07-25T00:00:00Z",
                source_label="test",
            ),
        )
        self.deployment_records: list[DeploymentRecord] = []
        self.environment_inventories: list[EnvironmentInventory] = []
        self.release_tuples: list[object] = []

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != PRODUCT:
            raise FileNotFoundError(product)
        return self.profile

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord:
        if (context_name, instance_name) != (CONTEXT, "prod"):
            raise FileNotFoundError
        return self.target_record

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord:
        if (context_name, instance_name) != (CONTEXT, "prod"):
            raise FileNotFoundError
        return self.target_id_record

    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory:
        if (context_name, instance_name) != (CONTEXT, "prod"):
            raise FileNotFoundError
        return self.inventory

    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest:
        if artifact_id != ARTIFACT_ID:
            raise FileNotFoundError(artifact_id)
        return self.artifact_manifest

    def read_backup_gate_record(self, record_id: str) -> BackupGateRecord:
        try:
            return self.backup_records[record_id]
        except KeyError as error:
            raise FileNotFoundError(record_id) from error

    def list_runtime_environment_records(
        self, *, context_name: str = "", instance_name: str = ""
    ) -> tuple[RuntimeEnvironmentRecord, ...]:
        return tuple(
            record
            for record in self.runtime_records
            if (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
        )

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]:
        del integration, context_name, instance_name, limit
        return ()

    def list_runtime_key_safety_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RuntimeKeySafetyPolicyRecord, ...]:
        del status, limit
        return ()

    def write_deployment_record(self, record: DeploymentRecord) -> None:
        self.deployment_records.append(record)

    def read_deployment_record(self, record_id: str) -> DeploymentRecord:
        for record in reversed(self.deployment_records):
            if record.record_id == record_id:
                return record
        raise FileNotFoundError(record_id)

    def write_environment_inventory(self, record: EnvironmentInventory) -> None:
        self.environment_inventories.append(record)

    def write_release_tuple_record(self, record: object) -> None:
        self.release_tuples.append(record)


def _request() -> OdooProdBackupRestoreRequest:
    return OdooProdBackupRestoreRequest(
        product=PRODUCT,
        context=CONTEXT,
        backup_record_id=BACKUP_RECORD_ID,
        verification_record_id=VERIFICATION_RECORD_ID,
        expected_current_artifact_id=ARTIFACT_ID,
        expected_old_db_volume=OLD_DB_VOLUME,
        expected_new_db_volume=NEW_DB_VOLUME,
        expected_data_volume=DATA_VOLUME,
        expected_log_volume=LOG_VOLUME,
    )


def _live_env(*, data_volume: str = DATA_VOLUME, db_volume: str = OLD_DB_VOLUME) -> dict[str, str]:
    return {
        "ODOO_DB_VOLUME": db_volume,
        "ODOO_DATA_VOLUME": data_volume,
        "ODOO_LOG_VOLUME": LOG_VOLUME,
        "DOCKER_IMAGE_REFERENCE": IMAGE_REFERENCE,
    }


class OdooProdBackupRestorePlanTests(unittest.TestCase):
    def test_ready_plan_binds_historical_backup_hashes_and_volume_drift(self) -> None:
        store = _Store()
        with (
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.fetch_dokploy_target_payload",
                return_value={
                    "name": "example-prod",
                    "env": dokploy_api.serialize_dokploy_env_text(_live_env()),
                },
            ),
        ):
            plan = build_odoo_prod_backup_restore_plan(
                control_plane_root=Path("."),
                record_store=store,
                request=_request(),
            )

        self.assertEqual(plan.plan_status, "ready")
        self.assertRegex(plan.plan_fingerprint, r"^[0-9a-f]{64}$")
        self.assertEqual(plan.backup_record_created_at, "2026-06-14T12:00:00Z")
        self.assertEqual(plan.database_dump_sha256, DATABASE_DUMP_SHA256)
        self.assertEqual(plan.filestore_archive_sha256, FILESTORE_ARCHIVE_SHA256)
        self.assertEqual(plan.verification_nonce, "c" * 64)
        self.assertEqual(plan.verification_status, "pass")
        self.assertGreaterEqual(plan.data_volume_free_bytes, plan.staging_required_bytes)
        self.assertEqual(plan.old_db_volume, OLD_DB_VOLUME)
        self.assertEqual(plan.new_db_volume, NEW_DB_VOLUME)
        self.assertEqual(plan.data_volume, DATA_VOLUME)
        self.assertIn(".quarantine-", plan.filestore_quarantine_path)

    def test_ready_plan_accepts_verified_retained_volume_backup_source(self) -> None:
        store = _Store()
        store.backup_records[BACKUP_RECORD_ID] = store.backup_records[BACKUP_RECORD_ID].model_copy(
            update={"source": RETAINED_VOLUME_BACKUP_IMPORT_SOURCE}
        )
        with (
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.fetch_dokploy_target_payload",
                return_value={
                    "name": "example-prod",
                    "env": dokploy_api.serialize_dokploy_env_text(_live_env()),
                },
            ),
        ):
            plan = build_odoo_prod_backup_restore_plan(
                control_plane_root=Path("."),
                record_store=store,
                request=_request(),
            )

        self.assertEqual(plan.plan_status, "ready")

    def test_plan_blocks_data_volume_drift(self) -> None:
        store = _Store()
        with (
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.fetch_dokploy_target_payload",
                return_value={
                    "name": "example-prod",
                    "env": dokploy_api.serialize_dokploy_env_text(
                        _live_env(data_volume="unexpected-data-volume")
                    ),
                },
            ),
        ):
            plan = build_odoo_prod_backup_restore_plan(
                control_plane_root=Path("."),
                record_store=store,
                request=_request(),
            )

        self.assertEqual(plan.plan_status, "blocked")
        self.assertEqual(plan.plan_fingerprint, "")
        self.assertTrue(any("ODOO_DATA_VOLUME" in blocker for blocker in plan.blockers))


class OdooProdBackupRestoreApplyTests(unittest.TestCase):
    def test_apply_runs_durable_phases_and_switches_only_database_volume(self) -> None:
        store = _Store()
        live_env = _live_env()

        def fetch_target_payload(**_: object) -> dict[str, str]:
            return {
                "name": "example-prod",
                "env": dokploy_api.serialize_dokploy_env_text(live_env),
            }

        def update_target_env(*, env_text: str, **_: object) -> None:
            live_env.clear()
            live_env.update(dokploy_api.parse_dokploy_env_text(env_text))

        def provider_phase(
            phase: str,
            evidence: dict[str, str],
        ) -> Callable[..., dict[str, str]]:
            def run(
                *,
                before_provider_mutation: Callable[[str], None],
                **_: object,
            ) -> dict[str, str]:
                before_provider_mutation(f"{phase}_schedule_upsert")
                before_provider_mutation(f"{phase}_schedule_trigger")
                return evidence

            return run

        phase_checkpoints: list[tuple[str, dict[str, str]]] = []
        provider_effects: list[tuple[str, str]] = []
        with (
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.fetch_dokploy_target_payload",
                side_effect=fetch_target_payload,
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.update_dokploy_target_env",
                side_effect=update_target_env,
            ),
        ):
            plan = build_odoo_prod_backup_restore_plan(
                control_plane_root=Path("."),
                record_store=store,
                request=_request(),
            )
        apply_request = OdooProdBackupRestoreApplyRequest(
            **_request().model_dump(),
            plan_fingerprint=plan.plan_fingerprint,
            confirmation=ODOO_PROD_BACKUP_RESTORE_CONFIRMATION,
        )

        def post_deploy(*, request, provider_effect_checkpoint, **_: object):  # type: ignore[no-untyped-def]
            self.assertEqual(request.phase, "deploy")
            provider_effect_checkpoint("post_deploy_schedule_upsert")
            provider_effect_checkpoint("post_deploy_schedule_trigger")
            return OdooPostDeployResult(
                context=CONTEXT,
                instance="prod",
                phase="deploy",
                post_deploy_status="pass",
            )

        verification = OdooVerificationResult(
            health_status="pass",
            canonical_status="pass",
            logo_status="pass",
            evidence=OdooVerificationEvidence(
                base_url=BASE_URL,
                health_url=HEALTH_URL,
                canonical_url=BASE_URL,
                logo_urls=(f"{BASE_URL}/web/image/website/1/logo",),
            ),
        )
        with (
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.fetch_dokploy_target_payload",
                side_effect=fetch_target_payload,
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.update_dokploy_target_env",
                side_effect=update_target_env,
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.latest_deployment_for_target",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.trigger_deployment"
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.wait_for_target_deployment",
                return_value="provider-deployment-1",
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_post_deploy.run_compose_odoo_backup_restore_database",
                side_effect=provider_phase(
                    "database_restore",
                    {"new_db_volume": NEW_DB_VOLUME},
                ),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_post_deploy.run_compose_odoo_backup_restore_filestore_stage",
                side_effect=provider_phase(
                    "filestore_stage",
                    {"filestore_staging_path": "/volumes/data/filestore/staging"},
                ),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_post_deploy.run_compose_odoo_backup_restore_web_quiesce",
                side_effect=provider_phase(
                    "web_quiesce",
                    {"web_was_running": "true"},
                ),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_post_deploy.run_compose_odoo_backup_restore_filestore_activate",
                side_effect=provider_phase(
                    "filestore_activate",
                    {"filestore_quarantine_path": plan.filestore_quarantine_path},
                ),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.control_plane_live_target_runtime.require_product_profile_runtime_secret_keys",
                return_value=frozenset(),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.control_plane_live_target_runtime.evaluate_runtime_key_safety_for_live_target_sync",
                return_value={"required": False, "status": "not_required"},
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.execute_odoo_post_deploy",
                side_effect=post_deploy,
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.verify_odoo_stable_readiness",
                return_value=verification,
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore._verify_runtime_identity",
                return_value=HealthcheckEvidence(
                    verified=True,
                    urls=(HEALTH_URL,),
                    timeout_seconds=60,
                    status="pass",
                    runtime_identity_status="match",
                ),
            ),
        ):
            result = execute_odoo_prod_backup_restore_apply(
                control_plane_root=Path("."),
                record_store=store,
                operation_id="restore-operation-1",
                request=apply_request,
                phase_checkpoint=lambda phase, evidence: phase_checkpoints.append(
                    (phase, evidence)
                ),
                provider_effect_checkpoint=lambda phase, effect: provider_effects.append(
                    (phase, effect)
                ),
            )

        self.assertEqual(result.restore_status, "pass")
        self.assertEqual(live_env["ODOO_DB_VOLUME"], NEW_DB_VOLUME)
        self.assertEqual(live_env["ODOO_DATA_VOLUME"], DATA_VOLUME)
        self.assertEqual(live_env["ODOO_LOG_VOLUME"], LOG_VOLUME)
        self.assertIn(("target_env_update_started", "target_env_update"), provider_effects)
        self.assertIn(("deployment_started", "deploy_trigger"), provider_effects)
        self.assertEqual(phase_checkpoints[0][0], "validated")
        self.assertIn("filestore_activated", [phase for phase, _ in phase_checkpoints])
        self.assertIn("verification_started", [phase for phase, _ in phase_checkpoints])
        self.assertTrue(store.environment_inventories)

    def test_verification_replay_reruns_only_post_deploy_and_checks(self) -> None:
        store = _Store()
        live_env = _live_env()

        def fetch_target_payload(**_: object) -> dict[str, str]:
            return {
                "name": "example-prod",
                "env": dokploy_api.serialize_dokploy_env_text(live_env),
            }

        def update_target_env(*, env_text: str, **_: object) -> None:
            live_env.clear()
            live_env.update(dokploy_api.parse_dokploy_env_text(env_text))

        def provider_phase(
            phase: str,
            evidence: dict[str, str],
        ) -> Callable[..., dict[str, str]]:
            def run(
                *,
                before_provider_mutation: Callable[[str], None],
                **_: object,
            ) -> dict[str, str]:
                before_provider_mutation(f"{phase}_schedule_upsert")
                before_provider_mutation(f"{phase}_schedule_trigger")
                return evidence

            return run

        with (
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.fetch_dokploy_target_payload",
                side_effect=fetch_target_payload,
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.update_dokploy_target_env",
                side_effect=update_target_env,
            ),
        ):
            plan = build_odoo_prod_backup_restore_plan(
                control_plane_root=Path("."),
                record_store=store,
                request=_request(),
            )
        apply_request = OdooProdBackupRestoreApplyRequest(
            **_request().model_dump(),
            plan_fingerprint=plan.plan_fingerprint,
            confirmation=ODOO_PROD_BACKUP_RESTORE_CONFIRMATION,
        )
        first_phase_checkpoints: list[tuple[str, dict[str, str]]] = []

        def post_deploy(*, request, provider_effect_checkpoint, **_: object):  # type: ignore[no-untyped-def]
            self.assertEqual(request.phase, "deploy")
            provider_effect_checkpoint("post_deploy_schedule_upsert")
            provider_effect_checkpoint("post_deploy_schedule_trigger")
            return OdooPostDeployResult(
                context=CONTEXT,
                instance="prod",
                phase="deploy",
                post_deploy_status="pass",
                override_evidence={"website_bootstrap_included": "true"},
            )

        canonical_failure = OdooVerificationResult(
            health_status="pass",
            canonical_status="fail",
            logo_status="pass",
            evidence=OdooVerificationEvidence(
                base_url=BASE_URL,
                health_url=HEALTH_URL,
                canonical_url="https://wrong.example.test",
                logo_urls=(f"{BASE_URL}/web/image/website/1/logo",),
            ),
            error_message="Odoo canonical verification failed.",
        )
        with (
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.fetch_dokploy_target_payload",
                side_effect=fetch_target_payload,
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.update_dokploy_target_env",
                side_effect=update_target_env,
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.latest_deployment_for_target",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.trigger_deployment"
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.wait_for_target_deployment",
                return_value="provider-deployment-1",
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_post_deploy.run_compose_odoo_backup_restore_database",
                side_effect=provider_phase("database_restore", {"new_db_volume": NEW_DB_VOLUME}),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_post_deploy.run_compose_odoo_backup_restore_filestore_stage",
                side_effect=provider_phase(
                    "filestore_stage",
                    {"filestore_staging_path": plan.filestore_staging_path},
                ),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_post_deploy.run_compose_odoo_backup_restore_web_quiesce",
                side_effect=provider_phase("web_quiesce", {"web_was_running": "true"}),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_post_deploy.run_compose_odoo_backup_restore_filestore_activate",
                side_effect=provider_phase(
                    "filestore_activate",
                    {"filestore_quarantine_path": plan.filestore_quarantine_path},
                ),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.control_plane_live_target_runtime.require_product_profile_runtime_secret_keys",
                return_value=frozenset(),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.control_plane_live_target_runtime.evaluate_runtime_key_safety_for_live_target_sync",
                return_value={"required": False, "status": "not_required"},
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.execute_odoo_post_deploy",
                side_effect=post_deploy,
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.verify_odoo_stable_readiness",
                return_value=canonical_failure,
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore._verify_runtime_identity",
                side_effect=AssertionError("runtime identity check should wait for readiness pass"),
            ),
        ):
            failed_result = execute_odoo_prod_backup_restore_apply(
                control_plane_root=Path("."),
                record_store=store,
                operation_id="restore-operation-1",
                request=apply_request,
                phase_checkpoint=lambda phase, evidence: first_phase_checkpoints.append(
                    (phase, evidence)
                ),
                provider_effect_checkpoint=lambda _phase, _effect: None,
            )

        failed_deployment = store.read_deployment_record(failed_result.deployment_record_id)
        self.assertEqual(failed_result.restore_status, "fail")
        self.assertEqual(failed_result.canonical_status, "fail")
        self.assertEqual(failed_deployment.deploy.status, "fail")
        replay_operation = OdooProdBackupRestoreOperationRecord.model_validate(
            {
                "operation_id": "restore-operation-1",
                "product": PRODUCT,
                "context": CONTEXT,
                "instance": "prod",
                "idempotency_key": "restore-request-1",
                "idempotency_scope": "github-actions|example|restore.yml|subject-a",
                "request_fingerprint": "restore-fingerprint",
                "request": apply_request.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json"),
                "authorization": durable_operation_authorization_payload(
                    action="odoo_prod_backup_restore_apply.execute",
                    managed_rule_id="example-prod-backup-restore",
                    product=PRODUCT,
                    context=CONTEXT,
                    instances=("prod",),
                ),
                "status": "fail",
                "phase": "failed",
                "checkpoints": tuple(
                    {
                        "phase": phase,
                        "recorded_at": f"2026-07-25T00:{index:02d}:00Z",
                        "evidence": evidence,
                    }
                    for index, (phase, evidence) in enumerate(first_phase_checkpoints, start=1)
                ),
                "deployment_record_id": failed_result.deployment_record_id,
                "created_at": "2026-07-25T00:00:00Z",
                "updated_at": "2026-07-25T00:30:00Z",
                "started_at": "2026-07-25T00:01:00Z",
                "finished_at": "2026-07-25T00:30:00Z",
                "attempt": 1,
                "result": failed_result.model_dump(mode="json"),
                "error_message": failed_result.error_message,
            }
        ).model_copy(update={"status": "running", "phase": "running", "attempt": 2})
        replay_phase_checkpoints: list[tuple[str, dict[str, str]]] = []
        replay_provider_effects: list[tuple[str, str]] = []
        replay_verification = OdooVerificationResult(
            health_status="pass",
            canonical_status="pass",
            logo_status="pass",
            evidence=OdooVerificationEvidence(
                base_url=BASE_URL,
                health_url=HEALTH_URL,
                canonical_url=BASE_URL,
                logo_urls=(f"{BASE_URL}/web/image/website/1/logo",),
            ),
        )

        valid_live_env = dict(live_env)
        sensitive_runtime_payload = "super-secret-runtime-identity-payload"
        live_env["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"] = sensitive_runtime_payload
        with (
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.fetch_dokploy_target_payload",
                side_effect=fetch_target_payload,
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.execute_odoo_post_deploy",
                side_effect=AssertionError("invalid live identity must block post-deploy"),
            ),
        ):
            malformed_identity_result = execute_odoo_prod_backup_restore_verification_replay(
                control_plane_root=Path("."),
                record_store=store,
                operation=replay_operation,
                phase_checkpoint=lambda _phase, _evidence: None,
                provider_effect_checkpoint=lambda _phase, _effect: None,
            )
        malformed_identity_deployment = store.read_deployment_record(
            failed_result.deployment_record_id
        )
        self.assertEqual(malformed_identity_result.restore_status, "fail")
        self.assertEqual(
            malformed_identity_result.error_message,
            "Odoo production restore replay failed: live_target_authority_changed.",
        )
        self.assertNotIn(sensitive_runtime_payload, malformed_identity_result.model_dump_json())
        self.assertNotIn(sensitive_runtime_payload, malformed_identity_deployment.model_dump_json())
        live_env.clear()
        live_env.update(valid_live_env)

        sensitive_provider_error = "private provider response body"
        with (
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.fetch_dokploy_target_payload",
                side_effect=click.ClickException(sensitive_provider_error),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.execute_odoo_post_deploy",
                side_effect=AssertionError("provider read failure must block post-deploy"),
            ),
        ):
            provider_error_result = execute_odoo_prod_backup_restore_verification_replay(
                control_plane_root=Path("."),
                record_store=store,
                operation=replay_operation,
                phase_checkpoint=lambda _phase, _evidence: None,
                provider_effect_checkpoint=lambda _phase, _effect: None,
            )
        provider_error_deployment = store.read_deployment_record(failed_result.deployment_record_id)
        self.assertEqual(
            provider_error_result.error_message,
            "Odoo production restore replay failed: live_target_read_failed.",
        )
        self.assertNotIn(sensitive_provider_error, provider_error_result.model_dump_json())
        self.assertNotIn(sensitive_provider_error, provider_error_deployment.model_dump_json())

        with (
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.fetch_dokploy_target_payload",
                side_effect=fetch_target_payload,
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.update_dokploy_target_env",
                side_effect=AssertionError("replay must not update target env"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.latest_deployment_for_target",
                side_effect=AssertionError("replay must not inspect deploy baseline"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.trigger_deployment",
                side_effect=AssertionError("replay must not trigger deploy"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_api.wait_for_target_deployment",
                side_effect=AssertionError("replay must not wait for deploy"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_post_deploy.run_compose_odoo_backup_restore_database",
                side_effect=AssertionError("replay must not restore database"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_post_deploy.run_compose_odoo_backup_restore_filestore_stage",
                side_effect=AssertionError("replay must not stage filestore"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_post_deploy.run_compose_odoo_backup_restore_web_quiesce",
                side_effect=AssertionError("replay must not quiesce web"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.dokploy_post_deploy.run_compose_odoo_backup_restore_filestore_activate",
                side_effect=AssertionError("replay must not activate filestore"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.execute_odoo_post_deploy",
                side_effect=post_deploy,
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore.verify_odoo_stable_readiness",
                return_value=replay_verification,
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_restore._verify_runtime_identity",
                return_value=HealthcheckEvidence(
                    verified=True,
                    urls=(HEALTH_URL,),
                    timeout_seconds=60,
                    status="pass",
                    runtime_identity_status="match",
                ),
            ),
        ):
            replay_result = execute_odoo_prod_backup_restore_verification_replay(
                control_plane_root=Path("."),
                record_store=store,
                operation=replay_operation,
                phase_checkpoint=lambda phase, evidence: replay_phase_checkpoints.append(
                    (phase, evidence)
                ),
                provider_effect_checkpoint=lambda phase, effect: replay_provider_effects.append(
                    (phase, effect)
                ),
            )

        self.assertEqual(replay_result.restore_status, "pass")
        self.assertEqual(replay_result.deployment_record_id, failed_result.deployment_record_id)
        self.assertEqual(replay_result.database_restore_status, "pass")
        self.assertEqual(replay_result.filestore_stage_status, "pass")
        self.assertEqual(replay_result.web_quiesce_status, "pass")
        self.assertEqual(replay_result.filestore_activation_status, "pass")
        self.assertEqual(replay_result.deployment_status, "pass")
        self.assertEqual(replay_result.canonical_status, "pass")
        self.assertEqual(replay_result.runtime_identity_status, "match")
        self.assertEqual(
            replay_provider_effects,
            [
                ("post_deploy_started", "post_deploy_schedule_upsert"),
                ("post_deploy_started", "post_deploy_schedule_trigger"),
            ],
        )
        self.assertEqual(
            [phase for phase, _ in replay_phase_checkpoints],
            ["post_deploy_started", "post_deploy_completed", "verification_started"],
        )
        passed_deployment = store.read_deployment_record(failed_result.deployment_record_id)
        self.assertEqual(passed_deployment.deploy.status, "pass")
        self.assertNotIn("restore_error", passed_deployment.runtime_source)
        self.assertIsNotNone(passed_deployment.runtime_identity)
        assert passed_deployment.runtime_identity is not None
        assert failed_deployment.runtime_identity is not None
        self.assertEqual(
            passed_deployment.runtime_identity.deployment_record_id,
            failed_deployment.runtime_identity.deployment_record_id,
        )
        self.assertTrue(store.environment_inventories)
        self.assertTrue(store.release_tuples)


class OdooProdBackupRestoreScriptTests(unittest.TestCase):
    def test_restore_scripts_are_bounded_and_never_reset_wal(self) -> None:
        database_script = dokploy_post_deploy._build_dokploy_odoo_backup_restore_database_script(
            compose_app_name="example",
            operation_id="restore-operation-1",
            database_name=DATABASE_NAME,
            database_dump_path="/volumes/data/backup.dump",
            database_dump_sha256=DATABASE_DUMP_SHA256,
            old_db_volume=OLD_DB_VOLUME,
            new_db_volume=NEW_DB_VOLUME,
            data_volume=DATA_VOLUME,
            log_volume=LOG_VOLUME,
        )
        filestore_script = (
            dokploy_post_deploy._build_dokploy_odoo_backup_restore_filestore_stage_script(
                compose_app_name="example",
                operation_id="restore-operation-1",
                database_name=DATABASE_NAME,
                filestore_path=FILESTORE_PATH,
                filestore_archive_path="/volumes/data/filestore.tar.gz",
                filestore_archive_sha256=FILESTORE_ARCHIVE_SHA256,
                filestore_member_count=10,
                filestore_unpacked_size=100,
                filestore_staging_path="/volumes/data/filestore/.restore",
                filestore_quarantine_path="/volumes/data/filestore/quarantine",
                data_volume=DATA_VOLUME,
                log_volume=LOG_VOLUME,
            )
        )
        activation_script = (
            dokploy_post_deploy._build_dokploy_odoo_backup_restore_filestore_activate_script(
                compose_app_name="example",
                operation_id="restore-operation-1",
                database_name=DATABASE_NAME,
                filestore_path=FILESTORE_PATH,
                filestore_staging_path="/volumes/data/filestore/.restore",
                filestore_quarantine_path="/volumes/data/filestore/quarantine",
                data_volume=DATA_VOLUME,
                log_volume=LOG_VOLUME,
            )
        )

        combined = database_script + filestore_script + activation_script
        self.assertNotIn("pg_resetwal", combined)
        self.assertIn("pg_restore", database_script)
        self.assertIn("--exit-on-error", database_script)
        self.assertIn("docker inspect -f '{{.Image}}'", database_script)
        self.assertIn("postgres (PostgreSQL) 17", database_script)
        self.assertIn("launchplane.restore.operation", database_script)
        self.assertIn("volume_label", database_script)
        self.assertIn("not member.isfile() and not member.isdir()", filestore_script)
        self.assertIn("member_path.parts[0] != database_name", filestore_script)
        self.assertIn(
            '"${expected_filestore_archive_sha256}" '
            '"${expected_filestore_member_count}" '
            '"${expected_filestore_unpacked_size}"',
            filestore_script,
        )
        self.assertNotIn('"${filestore_archive_sha256}"', filestore_script)
        self.assertNotIn('"${filestore_member_count}"', filestore_script)
        self.assertNotIn('"${filestore_unpacked_size}"', filestore_script)
        self.assertIn("filestore_quarantine_path", activation_script)
        self.assertIn("docker exec -u root -i", activation_script)
        self.assertIn("live_path.rename(quarantine_path)", activation_script)
        self.assertNotIn('mv "${current_filestore_path}"', activation_script)

    def test_restore_result_failure_identifies_exact_phase_with_bounded_code(self) -> None:
        with self.assertRaises(click.ClickException) as raised:
            dokploy_post_deploy.extract_odoo_backup_restore_result(
                {"logs": ["untrusted provider error text"]},
                operation_id="restore-operation-1",
                phase="filestore_stage",
            )
        self.assertEqual(
            str(raised.exception),
            "Dokploy Odoo backup restore filestore_stage failed (provider_result_missing).",
        )


if __name__ == "__main__":
    unittest.main()
