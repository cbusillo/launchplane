import unittest

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord


class DeployTargetContractTests(unittest.TestCase):
    def test_provider_target_record_normalizes_identity_and_evidence(self) -> None:
        record = ProviderTargetRecord(
            context=" syo ",
            instance=" prod ",
            provider_id=" Fake-Cloud ",
            target_category="service",
            target_id=" svc-123 ",
            display_name=" SYO Prod ",
            provider_target_type=" Managed-Service ",
            provider_evidence={" region ": " us-east-1 "},
            updated_at=" 2026-06-01T20:00:00Z ",
            source_label=" service:syo ",
        )

        self.assertEqual(record.context, "syo")
        self.assertEqual(record.instance, "prod")
        self.assertEqual(record.provider_id, "fake-cloud")
        self.assertEqual(record.target_id, "svc-123")
        self.assertEqual(record.display_name, "SYO Prod")
        self.assertEqual(record.provider_target_type, "managed-service")
        self.assertEqual(record.provider_evidence, {"region": "us-east-1"})

    def test_provider_target_record_rejects_empty_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires target_id"):
            ProviderTargetRecord(
                context="syo",
                instance="prod",
                provider_id="fake-cloud",
                target_category="service",
                target_id="",
                display_name="SYO Prod",
                provider_target_type="managed-service",
                updated_at="2026-06-01T20:00:00Z",
            )

    def test_provider_target_record_rejects_empty_evidence_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "evidence keys"):
            ProviderTargetRecord(
                context="syo",
                instance="prod",
                provider_id="fake-cloud",
                target_category="service",
                target_id="svc-123",
                display_name="SYO Prod",
                provider_target_type="managed-service",
                provider_evidence={" ": "us-east-1"},
                updated_at="2026-06-01T20:00:00Z",
            )

    def test_provider_target_record_from_dokploy_records(self) -> None:
        target_record = DokployTargetRecord(
            context="syo",
            instance="prod",
            project_name="SYO",
            target_type="application",
            target_name="syo-prod",
            updated_at="2026-06-01T20:00:00Z",
            source_label="service:syo",
        )
        target_id_record = DokployTargetIdRecord(
            context="syo",
            instance="prod",
            target_id="app-syo-prod",
            updated_at="2026-06-01T20:00:01Z",
            source_label="target-id:syo",
        )

        provider_record = ProviderTargetRecord.from_dokploy_records(
            target_record=target_record,
            target_id_record=target_id_record,
        )

        self.assertEqual(provider_record.provider_id, "dokploy")
        self.assertEqual(provider_record.target_category, "application")
        self.assertEqual(provider_record.target_id, "app-syo-prod")
        self.assertEqual(provider_record.display_name, "syo-prod")
        self.assertEqual(provider_record.provider_target_type, "application")
        self.assertEqual(provider_record.provider_evidence, {"project_name": "SYO"})
        self.assertEqual(provider_record.updated_at, "2026-06-01T20:00:01Z")
        self.assertEqual(provider_record.source_label, "service:syo")

    def test_provider_target_record_from_dokploy_records_uses_fallbacks(self) -> None:
        target_record = DokployTargetRecord(
            context="syo",
            instance="prod",
            target_type="application",
            target_name="",
            updated_at="2026-06-01T20:00:00Z",
            source_label=" ",
        )
        target_id_record = DokployTargetIdRecord(
            context="syo",
            instance="prod",
            target_id="app-syo-prod",
            updated_at="2026-06-01T20:00:01Z",
            source_label="target-id:syo",
        )

        provider_record = ProviderTargetRecord.from_dokploy_records(
            target_record=target_record,
            target_id_record=target_id_record,
        )

        self.assertEqual(provider_record.display_name, "prod")
        self.assertEqual(provider_record.provider_evidence, {})
        self.assertEqual(provider_record.source_label, "target-id:syo")

    def test_provider_target_record_rejects_mismatched_dokploy_context(self) -> None:
        target_record = DokployTargetRecord(
            context="syo-testing",
            instance="prod",
            target_type="application",
            target_name="syo-prod",
            updated_at="2026-06-01T20:00:00Z",
        )
        target_id_record = DokployTargetIdRecord(
            context="syo",
            instance="prod",
            target_id="app-syo-prod",
            updated_at="2026-06-01T20:00:01Z",
        )

        with self.assertRaisesRegex(ValueError, "context must match"):
            ProviderTargetRecord.from_dokploy_records(
                target_record=target_record,
                target_id_record=target_id_record,
            )

    def test_provider_target_record_rejects_mismatched_dokploy_instance(self) -> None:
        target_record = DokployTargetRecord(
            context="syo",
            instance="testing",
            target_type="application",
            target_name="syo-testing",
            updated_at="2026-06-01T20:00:00Z",
        )
        target_id_record = DokployTargetIdRecord(
            context="syo",
            instance="prod",
            target_id="app-syo-prod",
            updated_at="2026-06-01T20:00:01Z",
        )

        with self.assertRaisesRegex(ValueError, "instance must match"):
            ProviderTargetRecord.from_dokploy_records(
                target_record=target_record,
                target_id_record=target_id_record,
            )

    def test_provider_target_record_exports_deployed_target_reference(self) -> None:
        provider_record = ProviderTargetRecord(
            context="verireel",
            instance="prod",
            provider_id="dokploy",
            target_category="application",
            target_id="app-ver-prod",
            display_name="verireel-prod",
            provider_target_type="application",
            provider_evidence={"project_name": "VeriReel"},
            updated_at="2026-06-01T20:00:00Z",
        )

        deployed_target = provider_record.to_deployed_target_reference()

        self.assertEqual(deployed_target.provider_id, "dokploy")
        self.assertEqual(deployed_target.target_category, "application")
        self.assertEqual(deployed_target.target_id, "app-ver-prod")
        self.assertEqual(deployed_target.display_name, "verireel-prod")
        self.assertEqual(deployed_target.provider_evidence, {"project_name": "VeriReel"})


if __name__ == "__main__":
    unittest.main()
