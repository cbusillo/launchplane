import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import sqlalchemy as sa

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    BackupGateEvidence,
    DeploymentEvidence,
    HealthcheckEvidence,
    PromotionRecord,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.storage import verireel_prod_inventory_backfill as backfill
from control_plane.storage.postgres import PostgresRecordStore

DEPLOY = DeploymentEvidence(
    target_name="ver-prod-app", target_type="application", deploy_mode="test", status="pass"
)


def deployment(**updates: object) -> DeploymentRecord:
    record = DeploymentRecord(
        record_id=backfill.DEPLOYMENT_RECORD_ID,
        artifact_identity=ArtifactIdentityReference(artifact_id=backfill.ARTIFACT_ID),
        context="verireel",
        instance="prod",
        source_git_ref=backfill.SOURCE_GIT_REF,
        deploy=DEPLOY,
        runtime_identity=RuntimeIdentity(
            product="verireel",
            context="verireel",
            instance="prod",
            deployment_record_id=backfill.DEPLOYMENT_RECORD_ID,
            artifact_id=backfill.ARTIFACT_ID,
            source_git_ref=backfill.SOURCE_GIT_REF,
        ),
    )
    return record.model_copy(update=updates)


def promotion() -> PromotionRecord:
    return PromotionRecord(
        record_id=backfill.PROMOTION_RECORD_ID,
        artifact_identity=ArtifactIdentityReference(artifact_id=backfill.ARTIFACT_ID),
        deployment_record_id=backfill.DEPLOYMENT_RECORD_ID,
        backup_record_id="backup-gate",
        context="verireel",
        from_instance="testing",
        to_instance="prod",
        source_health=HealthcheckEvidence(status="pass"),
        backup_gate=BackupGateEvidence(status="pass"),
        deploy=DEPLOY,
        destination_health=HealthcheckEvidence(status="pass"),
    )


STALE = EnvironmentInventory(
    context="verireel",
    instance="prod",
    artifact_identity=ArtifactIdentityReference(artifact_id="ghcr.io/example/app:sha-old"),
    source_git_ref="0" * 40,
    deploy=DEPLOY,
    updated_at="2026-05-01T11:35:20Z",
    deployment_record_id="deployment-old",
)


class VeriReelProdInventoryBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database_url = f"sqlite+pysqlite:///{Path(directory.name) / 'lp.sqlite3'}"
        self.store = PostgresRecordStore(database_url=self.database_url)
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.store.write_environment_inventory(STALE)

    def run_backfill(self) -> str:
        engine = sa.create_engine(self.database_url)
        try:
            with engine.begin() as connection:
                return backfill.backfill_verireel_prod_inventory(
                    connection, updated_at="2026-09-28T22:00:00Z"
                )
        finally:
            engine.dispose()

    def prod(self) -> EnvironmentInventory:
        return self.store.read_environment_inventory(context_name="verireel", instance_name="prod")

    def test_rebuilds_prod_record_once_from_matching_promotion(self) -> None:
        self.store.write_deployment_record(deployment())
        self.store.write_promotion_record(promotion())

        self.assertEqual(self.run_backfill(), "written")
        rebuilt = self.prod()
        assert rebuilt.runtime_identity is not None
        self.assertEqual(rebuilt.runtime_identity.source_git_ref, backfill.SOURCE_GIT_REF)
        self.assertEqual(rebuilt.promotion_record_id, backfill.PROMOTION_RECORD_ID)
        self.assertEqual(rebuilt.deployment_record_id, backfill.DEPLOYMENT_RECORD_ID)

        self.assertEqual(self.run_backfill(), "current")
        self.assertEqual(self.prod(), rebuilt)

    def test_refuses_when_deployment_does_not_match(self) -> None:
        self.store.write_promotion_record(promotion())
        for mismatch in (
            {"source_git_ref": "1" * 40},
            {"context": "other"},
            {"instance": "testing"},
            {"artifact_identity": ArtifactIdentityReference(artifact_id="ghcr.io/other@sha256:1")},
            {"runtime_identity": None},
        ):
            with self.subTest(mismatch=mismatch):
                self.store.write_deployment_record(deployment(**mismatch))
                self.assertEqual(self.run_backfill(), "refused")
                self.assertEqual(self.prod(), STALE)

    def test_does_nothing_without_source_records(self) -> None:
        self.assertEqual(self.run_backfill(), "absent")
        self.assertEqual(self.prod(), STALE)


if __name__ == "__main__":
    unittest.main()
