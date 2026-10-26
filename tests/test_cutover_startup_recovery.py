from datetime import UTC, datetime
from collections.abc import Callable
from control_plane.contracts.odoo_prod_promotion_operation import OdooProdPromotionOperationRecord
from control_plane.contracts.odoo_prod_rollback_operation import OdooProdRollbackOperationRecord
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.odoo_cutover import OdooCutoverPlan, OdooCutoverRecord
from control_plane.contracts.prepared_public_site import PublicPauseRecord, WriterObservation
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.dokploy.slot_maintenance import maintenance_payload, payload_digest
from control_plane.odoo_cutover_plan import cutover_identity
from control_plane.odoo_cutover_recovery import startup_recovery_pause, publication_recovery_pause
from control_plane.prepared_public_site import prepare_public_site
from tests.test_odoo_cutover import IsolatedCutoverProvider
from tests.test_odoo_cutover_website_settings import snapshot
from tests.test_odoo_prod_promotion_operation import _operation, _rollback_operation, _store


class CutoverStartupRecoveryTests(unittest.TestCase):
    def test_only_original_held_operation_can_reclaim_proven_interrupted_startup(self) -> None:
        for rollback in (False, True):
            with self.subTest(rollback=rollback), TemporaryDirectory() as temporary:
                store = _store(temporary)
                self.addCleanup(store.close)
                provider = IsolatedCutoverProvider(Path(temporary) / "fixture")
                self.addCleanup(provider.engine.dispose)
                operation = _rollback_operation() if rollback else _operation()
                release = "original-release" if rollback else _operation().request.request_id
                pair = provider.cutover
                slots = [
                    slot.model_copy(
                        update={
                            "binding": slot.binding.model_copy(
                                update={
                                    "product_id": operation.product,
                                    "environment_id": operation.instance,
                                    "release_id": release,
                                }
                            ),
                            "runtime_identity": slot.runtime_identity.model_copy(
                                update={
                                    "product": operation.product,
                                    "context": operation.context,
                                    "instance": operation.instance,
                                    "release_id": release if slot == pair.new else "previous",
                                }
                            ),
                        }
                    )
                    for slot in (pair.old, pair.new)
                ]
                pair = OdooCutoverPlan.model_validate(
                    {**pair.model_dump(), "release_id": release, "old": slots[0], "new": slots[1]}
                )
                pair = OdooCutoverPlan.model_validate(
                    {**pair.model_dump(), "website_settings": snapshot(pair)}
                )
                provider.plan = provider.plan.model_copy(update={"binding": pair.old.binding})
                provider.binding = pair.old.binding
                site = prepare_public_site(provider.plan, provider)
                receipt = provider.maintenance(pair).model_copy(
                    update={"evidence_id": payload_digest(maintenance_payload(pair))}
                )
                backup = provider.backup(pair)
                identity = cutover_identity(
                    operation.product, operation.context, operation.instance, release
                )
                pause_id = identity + "-rollback" if rollback else identity
                now = datetime.now(UTC)
                pause = PublicPauseRecord(
                    pause_id=pause_id,
                    binding=pair.old.binding,
                    content_digest=site.content_digest,
                    began_at=now,
                    drained_at=now,
                    state="drained",
                    served_mode="prepared-public",
                    writers=tuple(
                        WriterObservation(
                            binding=pair.old.binding,
                            pause_id=pause_id,
                            writer=writer,
                            fenced=True,
                            active_jobs=0,
                            evidence_id="native-observation",
                        )
                        for writer in pair.writers
                    ),
                )
                progress = OdooCutoverRecord(
                    record_id=identity,
                    plan=pair,
                    began_at=now,
                    updated_at=now,
                    step="rolling_back" if rollback else "maintaining",
                    maintenance=receipt,
                    backup=backup,
                    public_site=site,
                    website_settings_attempted=True,
                    rollback_pause=pause if rollback else None,
                    pause=None if rollback else pause,
                )
                self.assertEqual(startup_recovery_pause(progress), pause)
                for step in (
                    ("rolling_back",)
                    if rollback
                    else ("maintaining", "verifying", "switching", "resuming")
                ):
                    self.assertEqual(
                        publication_recovery_pause(progress.model_copy(update={"step": step})),
                        pause,
                    )
                held = operation.model_copy(
                    update={
                        "status": "reconciliation_required",
                        "phase": "rollback_started" if rollback else "promotion_started",
                        "attempt": 1,
                        "error_code": "operation_reconciliation_required",
                        "error_message": "Original startup completion was lost.",
                    }
                )
                claim: Callable[
                    ..., OdooProdPromotionOperationRecord | OdooProdRollbackOperationRecord | None
                ]
                if isinstance(held, OdooProdRollbackOperationRecord):
                    held = held.model_copy(
                        update={
                            "target": _rollback_operation().target.model_copy(
                                update={"artifact_id": pair.production.artifact_id}
                            )
                        }
                    )
                    store.create_odoo_prod_rollback_operation_record_if_no_active_lane(held)
                    claim = store.claim_odoo_cutover_rollback_reconciliation_record
                else:
                    store.create_odoo_prod_promotion_operation_record_if_no_active_lane(held)
                    claim = store.claim_odoo_cutover_reconciliation_record
                deployment = DeploymentRecord(
                    record_id="deployment-cutover-" + identity,
                    context=operation.context,
                    instance=operation.instance,
                    source_git_ref=pair.candidate.source_commit,
                    deploy=DeploymentEvidence(
                        deploy_mode="atomic-two-slot", target_type="compose", target_name="isolated"
                    ),
                    odoo_cutover=progress,
                )
                for invalid in (
                    progress.model_copy(update={"maintenance": None}),
                    progress.model_copy(update={"backup": None}),
                    progress.model_copy(
                        update={
                            "rollback_pause" if rollback else "pause": pause.model_copy(
                                update={"ended_at": now}
                            )
                        }
                    ),
                    progress.model_copy(
                        update={
                            "rollback_pause" if rollback else "pause": pause.model_copy(
                                update={"writers": ()}
                            )
                        }
                    ),
                ):
                    self.assertIsNone(startup_recovery_pause(invalid))
                    self.assertIsNone(publication_recovery_pause(invalid))
                    store.write_deployment_record(
                        deployment.model_copy(update={"odoo_cutover": invalid})
                    )
                    self.assertIsNone(
                        claim(
                            lease_owner="worker",
                            lease_expires_at="2026-10-27T00:05:00Z",
                            claimed_at="2026-10-27T00:00:00Z",
                            max_attempts=3,
                        )
                    )
                store.write_deployment_record(deployment)
                claimed = claim(
                    lease_owner="worker",
                    lease_expires_at="2026-10-27T00:05:00Z",
                    claimed_at="2026-10-27T00:00:00Z",
                    max_attempts=3,
                )
                assert claimed is not None
                self.assertEqual(claimed.operation_id, operation.operation_id)
                self.assertEqual(claimed.idempotency_key, operation.idempotency_key)
                self.assertEqual(claimed.attempt, 2)
                self.assertIsNone(
                    claim(
                        lease_owner="other",
                        lease_expires_at="2026-10-27T00:05:00Z",
                        claimed_at="2026-10-27T00:00:00Z",
                        max_attempts=3,
                    )
                )
