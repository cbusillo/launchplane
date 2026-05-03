import unittest

import click

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    PromotionRecord,
)
from control_plane.workflows.evidence_ingestion import (
    apply_deployment_evidence,
    apply_promotion_evidence,
)


class _FakeEvidenceStore:
    def __init__(self) -> None:
        self.deployments: dict[str, DeploymentRecord] = {}
        self.promotions: dict[str, PromotionRecord] = {}
        self.inventories: dict[tuple[str, str], EnvironmentInventory] = {}

    def write_deployment_record(self, record: DeploymentRecord) -> None:
        self.deployments[record.record_id] = record

    def read_deployment_record(self, deployment_record_id: str) -> DeploymentRecord:
        return self.deployments[deployment_record_id]

    def write_promotion_record(self, record: PromotionRecord) -> None:
        self.promotions[record.record_id] = record

    def write_environment_inventory(self, inventory: EnvironmentInventory) -> None:
        self.inventories[(inventory.context, inventory.instance)] = inventory


def _deployment_record(
    *,
    record_id: str = "deployment-site-prod",
    context: str = "site",
    instance: str = "prod",
    artifact_id: str = "artifact-site-123",
) -> DeploymentRecord:
    return DeploymentRecord(
        record_id=record_id,
        artifact_identity=ArtifactIdentityReference(artifact_id=artifact_id),
        context=context,
        instance=instance,
        source_git_ref="abc123",
        deploy=DeploymentEvidence(
            target_name=f"{context}-{instance}",
            target_type="application",
            deploy_mode="dokploy-application-api",
            status="pass",
        ),
    )


def _promotion_record(
    *,
    record_id: str = "promotion-site-testing-to-prod",
    deployment_record_id: str = "deployment-site-prod",
    context: str = "site",
    to_instance: str = "prod",
    artifact_id: str = "artifact-site-123",
) -> PromotionRecord:
    return PromotionRecord(
        record_id=record_id,
        artifact_identity=ArtifactIdentityReference(artifact_id=artifact_id),
        deployment_record_id=deployment_record_id,
        backup_record_id="backup-site-prod-1",
        context=context,
        from_instance="testing",
        to_instance=to_instance,
        deploy=DeploymentEvidence(
            target_name=f"{context}-{to_instance}",
            target_type="application",
            deploy_mode="dokploy-application-api",
            status="pass",
        ),
    )


class EvidenceIngestionTests(unittest.TestCase):
    def test_apply_deployment_evidence_uses_structural_store_boundary(self) -> None:
        store = _FakeEvidenceStore()
        deployment = _deployment_record()

        result = apply_deployment_evidence(record_store=store, deployment_record=deployment)

        self.assertEqual(result["deployment_record_id"], "deployment-site-prod")
        self.assertEqual(result["inventory_record_id"], "site-prod")
        self.assertEqual(store.deployments["deployment-site-prod"], deployment)
        inventory = store.inventories[("site", "prod")]
        self.assertEqual(inventory.deployment_record_id, "deployment-site-prod")
        self.assertEqual(inventory.artifact_identity, deployment.artifact_identity)

    def test_apply_promotion_evidence_writes_inventory_from_linked_deployment(self) -> None:
        store = _FakeEvidenceStore()
        deployment = _deployment_record()
        promotion = _promotion_record()
        store.write_deployment_record(deployment)

        result = apply_promotion_evidence(record_store=store, promotion_record=promotion)

        self.assertEqual(result["promotion_record_id"], "promotion-site-testing-to-prod")
        self.assertEqual(result["inventory_record_id"], "site-prod")
        self.assertEqual(store.promotions["promotion-site-testing-to-prod"], promotion)
        inventory = store.inventories[("site", "prod")]
        self.assertEqual(inventory.promotion_record_id, "promotion-site-testing-to-prod")
        self.assertEqual(inventory.promoted_from_instance, "testing")

    def test_apply_promotion_evidence_rejects_mismatched_deployment_context(self) -> None:
        store = _FakeEvidenceStore()
        store.write_deployment_record(_deployment_record(context="other"))

        with self.assertRaises(click.ClickException):
            apply_promotion_evidence(record_store=store, promotion_record=_promotion_record())

    def test_apply_promotion_evidence_rejects_mismatched_artifact(self) -> None:
        store = _FakeEvidenceStore()
        store.write_deployment_record(_deployment_record(artifact_id="artifact-site-old"))

        with self.assertRaises(click.ClickException):
            apply_promotion_evidence(record_store=store, promotion_record=_promotion_record())


if __name__ == "__main__":
    unittest.main()
