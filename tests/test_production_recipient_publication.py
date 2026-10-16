"""Publication commits all authority or none; inverse preserves other work."""

from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.production_backup_authority import (
    ProductionBackupPolicyRecord,
    ProductionBackupTargetRecord,
)
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.promotion_record import HealthcheckEvidence, DeploymentEvidence
from control_plane.production_recipient import custody, authority_snapshot
from control_plane.production_recipient_publication import (
    RecipientPublication,
    commit_publication,
    rollback_publication,
)
from tests.test_production_backup_authority import _source_target, _destination_target, _policy
from tests import test_production_recipient as preparation_tests


class RecipientPublicationTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        self.fixture = preparation_tests.ProductionRecipientTests()
        self.fixture.database_url = self.database_url
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.store = f.store
        stamp = f.old.updated_at
        self.inventory = EnvironmentInventory(
            context=f.context,
            instance="prod",
            source_git_ref="b" * 40,
            deploy=DeploymentEvidence(
                status="pass",
                target_name=f.old.target_name,
                target_type="compose",
                deploy_mode="compose",
            ),
            updated_at=stamp,
            deployment_record_id="retained-deployment",
        )
        self.store.write_environment_inventory(self.inventory)
        self.monitor = PrivateHealthEndpointRecord(
            endpoint_key="private-runtime",
            product=f.product,
            context=f.context,
            instance="prod",
            url="http://192.168.200.10:8090/health",
            updated_at=stamp,
        )
        self.store.write_private_health_endpoint_record(self.monitor)
        self.store.write_production_backup_target_record(_source_target())
        self.store.write_production_backup_target_record(_destination_target())
        new_source = ProductionBackupTargetRecord.model_validate(
            {
                **_source_target().model_dump(mode="json"),
                "target_id": "new-source-guest",
                "record_id": "",
                "target_digest": "",
                "destination": {
                    **_source_target().destination.model_dump(mode="json"),
                    "guest_id": "102",
                },
            }
        )
        self.store.write_production_backup_target_record(new_source)
        self.policy = ProductionBackupPolicyRecord.model_validate(
            {
                **_policy().model_dump(mode="json"),
                "product": f.product,
                "context": f.context,
                "record_id": "",
                "policy_id": "",
                "policy_digest": "",
            }
        )
        self.store.write_production_backup_policy_record(self.policy)
        self.customize_preparation()
        f.body = f.body.model_copy(update={"mode": "dry-run", "reviewed_plan_sha256": ""})
        f.adapter.request = f.body
        record = f.adapter.current()
        f.adapter.request = f.body.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": record.plan_sha256}
        )
        self.assertEqual(f.run_operation().response_status_code, 202)
        saved = custody(self.store, f.product, f.context)
        assert saved is not None
        record = saved
        assert record is not None
        recovered = record.model_copy(update={"recovery_proof": {"binding": "fixture-recovery"}})
        self.store.write_production_recipient(
            expected=record.model_dump(mode="json"), replacement=recovered.model_dump(mode="json")
        )
        self.record = recovered
        target = record.retained_target.model_copy(
            update={"target_name": record.namespace, "updated_at": stamp}
        )
        target_id = record.retained_target_id.model_copy(update={"target_id": record.target_id})
        self.next_policy = ProductionBackupPolicyRecord.model_validate(
            {
                **self.policy.model_dump(mode="json"),
                "record_id": "",
                "policy_digest": "",
                "policy_revision": 2,
                "supersedes_record_id": self.policy.record_id,
                "fast_snapshot": {
                    **self.policy.fast_snapshot.model_dump(mode="json"),
                    "source_target_id": new_source.target_id,
                },
                "independent_backup": {
                    **self.policy.independent_backup.model_dump(mode="json"),
                    "source_target_id": new_source.target_id,
                },
            }
        )
        self.publication = RecipientPublication(
            target=target,
            target_id=target_id,
            provider=ProviderTargetRecord.from_dokploy_records(
                target_record=target, target_id_record=target_id
            ),
            inventory=self.inventory.model_copy(
                update={
                    "source_git_ref": record.request.source_commit,
                    "runtime_identity": RuntimeIdentity(
                        product=f.product,
                        context=f.context,
                        instance="prod",
                        environment_kind="stable",
                        deployment_record_id="recipient-deployment",
                        artifact_id=record.image_reference,
                        source_git_ref=record.request.source_commit,
                        image_reference=record.image_reference,
                    ),
                    "destination_health": HealthcheckEvidence(status="pass"),
                    "deployment_record_id": "recipient-deployment",
                }
            ),
            health_endpoints=(
                self.monitor.model_copy(update={"url": "http://192.168.200.20:19220/health"}),
            ),
            backup_policies=(self.next_policy,),
        )
        # model_copy deliberately does not recursively validate dictionary updates.
        self.publication = RecipientPublication.model_validate(
            self.publication.model_dump(mode="json")
        )

    def customize_preparation(self) -> None:
        pass

    def publish(self) -> Any:
        return commit_publication(
            self.store, self.record, self.publication, validate_release=lambda: None
        )

    def test_revoked_execution_lease_cannot_publish_or_update_custody(self) -> None:
        reservation = self.store.reserve_mutation(
            scope="fixture",
            route_path="/publication",
            idempotency_key="original",
            request_fingerprint="a" * 64,
            lease_owner="original-worker",
            reconciliation_key="original-effect",
        ).record
        assert reservation is not None
        self.store.mark_mutation_reconcile_required(
            reservation=reservation, reconciliation_key=reservation.reconciliation_key
        )
        with self.assertRaises(ValueError):
            commit_publication(
                self.store,
                self.record,
                self.publication,
                validate_release=lambda: None,
                reservation=reservation,
            )
        with self.assertRaises(ValueError):
            self.store.write_production_recipient(
                expected=self.record.model_dump(mode="json"),
                replacement=self.record.model_copy(update={"phase": "stale-worker"}).model_dump(
                    mode="json"
                ),
                reservation=reservation,
            )
        self.assertEqual(custody(self.store, self.record.product, self.record.context), self.record)
        self.assertEqual(
            self.store.read_environment_inventory(
                context_name=self.record.context, instance_name="prod"
            ),
            self.inventory,
        )

    def test_atomic_publication_and_scoped_inverse_retain_history_and_unrelated_rows(self) -> None:
        published = self.publish()
        self.assertEqual(
            self.store.read_dokploy_target_id_record(
                context_name=self.record.context, instance_name="prod"
            ).target_id,
            self.record.target_id,
        )
        extra = self.monitor.model_copy(
            update={"endpoint_key": "later-monitor", "url": "http://192.168.200.30/health"}
        )
        self.store.write_private_health_endpoint_record(extra)
        stops = []
        result = rollback_publication(
            self.store, published, stop_replacement=lambda: stops.append(self.record.target_id)
        )
        self.assertEqual(stops, [self.record.target_id])
        self.assertEqual(result.phase, "inverse_published")
        self.assertEqual(self.store.read_private_health_endpoint_record(extra.endpoint_key), extra)
        self.assertEqual(
            self.store.read_private_health_endpoint_record(self.monitor.endpoint_key), self.monitor
        )
        self.assertEqual(
            self.store.read_environment_inventory(
                context_name=self.record.context, instance_name="prod"
            ),
            self.inventory,
        )
        history = self.store.list_production_backup_policy_records(product=self.record.product)
        current = next(r for r in history if r.status == "active")
        self.assertEqual(current.fast_snapshot, self.policy.fast_snapshot)
        self.assertEqual(current.policy_revision, self.next_policy.policy_revision + 1)
        self.assertEqual(len(history), 3)

    def test_failure_before_commit_rolls_back_target_monitor_backup_and_custody(self) -> None:
        before = authority_snapshot(self.store, self.record.product, self.record.context)
        with patch.object(
            self.store,
            "_after_product_authority_bundle_step",
            side_effect=RuntimeError("injected crash"),
        ):
            with self.assertRaises(RuntimeError):
                self.publish()
        self.assertEqual(
            authority_snapshot(self.store, self.record.product, self.record.context), before
        )
        self.assertEqual(custody(self.store, self.record.product, self.record.context), self.record)

    def test_revoked_release_and_authority_drift_refuse_publication(self) -> None:
        def revoked() -> None:
            raise ValueError("acceptance withdrawn")

        with self.assertRaises(ValueError):
            commit_publication(self.store, self.record, self.publication, validate_release=revoked)
        self.store.write_private_health_endpoint_record(
            self.monitor.model_copy(update={"url": "http://192.168.200.40/health"})
        )
        with self.assertRaises(ValueError):
            self.publish()
        self.fixture.assert_old_preserved()

    def test_changed_owned_row_is_preserved_by_inverse(self) -> None:
        published = self.publish()
        changed = self.publication.health_endpoints[0].model_copy(
            update={"url": "http://192.168.200.50/health"}
        )
        self.store.write_private_health_endpoint_record(changed)
        with self.assertRaises(ValueError):
            rollback_publication(self.store, published, stop_replacement=lambda: None)
        self.assertEqual(
            self.store.read_private_health_endpoint_record(changed.endpoint_key), changed
        )
        self.assertEqual(custody(self.store, self.record.product, self.record.context), published)
