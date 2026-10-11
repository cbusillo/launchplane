from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator
import unittest
from unittest.mock import Mock, patch

import click
from pydantic import ValidationError

from control_plane.contracts.durable_operation_authorization import DurableOperationAuthorization
from control_plane.contracts.production_backup_authority import ProductionBackupPolicyRecord
from control_plane.contracts.production_backup_gate import ProductionBackupGateWorkerRequest
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    BackupGateEvidence,
    DeploymentEvidence,
    PromotionRecord,
)
from control_plane.workflows.promote import generate_promotion_record_id
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.production_backup_gate import enqueue_production_backup_gate
from control_plane.workflows.production_promotion_backup import (
    GENERIC_WEB_PROMOTION_BACKUP_ACTION,
    ODOO_PROMOTION_BACKUP_ACTION,
    production_promotion_backup_guard,
    require_production_promotion_backup,
)
from control_plane.workflows.generic_web_promotion import (
    GenericWebProdPromotionRequest,
    execute_generic_web_prod_promotion,
)
from control_plane.workflows.odoo_prod_promotion import (
    _checkpoint_chain,
    OdooProdPromotionRequest,
    execute_odoo_prod_promotion,
)
from control_plane.workflows.odoo_prod_promotion_run import (
    OdooProdPromotionRunRequest,
    execute_odoo_prod_promotion_run,
)
from control_plane.workflows.verireel_prod_backup_gate_operation_worker import (
    run_verireel_prod_backup_gate_operation_worker_once,
)
from tests.support.durable_operations import (
    durable_operation_authorization_payload,
    durable_operation_policy_record,
)
from tests.test_generic_web_promotion import _profile
from tests.test_odoo_prod_promotion_run import _inputs_result
from tests.test_production_backup_provider import BackupHost, _binding, setup_memory_files


class ProductionPromotionBackupTests(unittest.TestCase):
    def test_interrupted_cutover_resumes_through_real_backup_guard_without_new_capture(
        self,
    ) -> None:
        from control_plane.contracts.backup_gate_record import BackupGateRecord
        from control_plane.contracts.environment_inventory import EnvironmentInventory
        from control_plane.contracts.odoo_cutover_lane import OdooCutoverLane, OdooSlotCoordinates
        from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
        from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
        from control_plane.odoo_cutover_plan import resolve_profile_cutover_plan
        from control_plane.workflows.odoo_prod_backup_gate import (
            BACKUP_GATE_SOURCE,
            OdooProdBackupGateResult,
        )
        from tests.test_odoo_cutover import IsolatedCutoverProvider
        from tests.support.profiles import _odoo_preview_profile_payload

        store = self.capture()
        provider = IsolatedCutoverProvider(self.root / "cutover")
        self.addCleanup(provider.engine.dispose)
        pair = provider.cutover
        old_identity = pair.old.runtime_identity.model_copy(
            update={"product": "example-product", "context": "example-product", "instance": "prod"}
        )
        config = OdooCutoverLane(
            route_id=pair.route_id,
            host=pair.host,
            database=pair.old.binding.database,
            database_uuid=old_identity.database_uuid,
            prepared_origin="http://prepared:8080",
            slots=(
                OdooSlotCoordinates(
                    **pair.old.model_dump(include={"slot_id", "compose_id", "private_url"})
                ),
                OdooSlotCoordinates(
                    **pair.new.model_dump(include={"slot_id", "compose_id", "private_url"})
                ),
            ),
            writers=pair.writers,
        )
        profile = _odoo_preview_profile_payload("example-product")
        profile["production_use"] = "prelaunch"
        profile["preview"] = {"enabled": False}
        profile["lanes"] = [
            {
                "instance": "prod",
                "context": "example-product",
                "base_url": "http://fixture.invalid",
                "health_url": "http://fixture.invalid/launchplane/health",
                "odoo_cutover": config.model_dump(mode="json"),
            }
        ]
        store.write_product_profile_record(LaunchplaneProductProfileRecord.model_validate(profile))
        for manifest in (pair.production, pair.candidate):
            store.write_artifact_manifest(manifest)
        for instance, manifest in (("prod", pair.production), ("testing", pair.candidate)):
            store.write_release_tuple_record(
                ReleaseTupleRecord(
                    tuple_id=instance,
                    context="example-product",
                    channel=instance,
                    artifact_id=manifest.artifact_id,
                    repo_shas={"example/site": manifest.source_commit},
                    provenance="ship",
                    minted_at=datetime.now(UTC).isoformat(),
                )
            )
        store.write_environment_inventory(
            EnvironmentInventory(
                context="example-product",
                instance="prod",
                source_git_ref=pair.production.source_commit,
                runtime_identity=old_identity,
                deploy=DeploymentEvidence(
                    deploy_mode="atomic-two-slot",
                    target_type="compose",
                    target_name="old",
                    status="pass",
                ),
                deployment_record_id="old",
                updated_at=datetime.now(UTC).isoformat(),
            )
        )
        request = OdooProdPromotionRunRequest(
            context="example-product",
            product="example-product",
            request_id="accepted-cutover",
            infrastructure_backup_record_id="backup-example",
        )
        plan = resolve_profile_cutover_plan(
            store,
            product=request.product,
            context=request.context,
            instance="prod",
            artifact_id=pair.candidate.artifact_id,
            release_id=request.request_id,
        )
        assert plan is not None
        provider.cutover = plan
        provider.target = plan.old
        provider.binding = plan.old.binding
        provider.plan = provider.plan.model_copy(update={"binding": plan.old.binding})
        inputs = _inputs_result().model_copy(
            update={
                "context": request.context,
                "artifact_id": plan.candidate.artifact_id,
                "source_git_ref": plan.candidate.source_commit,
                "request_id": request.request_id,
                "backup_record_id": "logical-cutover",
                "release_tuple_id": "testing",
                "image_repository": plan.candidate.image.repository,
                "image_digest": plan.candidate.image.digest,
            }
        )

        def capture_logical(**kwargs: object) -> OdooProdBackupGateResult:
            store.write_backup_gate_record(
                BackupGateRecord(
                    record_id="logical-cutover",
                    context=request.context,
                    instance="prod",
                    created_at=datetime.now(UTC).isoformat(),
                    source=BACKUP_GATE_SOURCE,
                    status="pass",
                    evidence={"coherent_fixture_capture": "verified"},
                )
            )
            return OdooProdBackupGateResult(
                context=request.context,
                instance="prod",
                backup_record_id="logical-cutover",
                backup_status="pass",
            )

        with (
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.resolve_odoo_prod_promotion_inputs",
                return_value=inputs,
            ),
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.execute_odoo_prod_backup_gate",
                side_effect=capture_logical,
            ) as capture,
        ):
            from control_plane.workflows.odoo_stable_operation_worker import (
                _execute_prod_promotion_operation,
                DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS,
            )
            from control_plane.durable_operation_authorization import (
                capture_durable_operation_authorization,
            )
            from control_plane.contracts.odoo_prod_promotion_operation import (
                OdooProdPromotionOperationRecord,
                build_odoo_prod_promotion_operation_id,
                odoo_prod_promotion_request_fingerprint,
            )
            from tests.test_odoo_prod_promotion_operation import (
                _operation,
                _policy_record,
                _ADMINISTRATOR_POLICY,
            )
            from tests.http_app_test_support import _github_human_identity

            policy = _policy_record(_ADMINISTRATOR_POLICY)
            original = _operation()
            operation = type(original).model_validate(
                original.model_copy(
                    update={
                        "product": request.product,
                        "context": request.context,
                        "request": request,
                        "operation_id": build_odoo_prod_promotion_operation_id(
                            product=request.product,
                            context=request.context,
                            idempotency_key=original.idempotency_key,
                            idempotency_scope=original.idempotency_scope,
                        ),
                        "request_fingerprint": odoo_prod_promotion_request_fingerprint(request),
                        "authorization": capture_durable_operation_authorization(
                            identity=_github_human_identity(),
                            action=ODOO_PROMOTION_BACKUP_ACTION,
                            product=request.product,
                            context=request.context,
                            instances=("prod",),
                            policy_record=policy,
                            authorized_at=datetime.now(UTC).isoformat(),
                        ),
                    }
                ).model_dump()
            )
            store.create_odoo_prod_promotion_operation_record_if_no_active_lane(operation)

            def claim(*, reconcile: bool = False) -> OdooProdPromotionOperationRecord | None:
                arguments = dict(
                    lease_owner="cutover-worker",
                    lease_expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    claimed_at=datetime.now(UTC).isoformat(),
                )
                if reconcile:
                    return store.claim_odoo_cutover_reconciliation_record(
                        **arguments, max_attempts=DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS
                    )
                return store.claim_next_odoo_prod_promotion_operation_record(**arguments)

            def work(current: OdooProdPromotionOperationRecord | None, denial: str = "") -> bool:
                assert current is not None
                from control_plane.durable_operation_authorization import (
                    DurableOperationAuthorizationGuard,
                )
                from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord

                boundary = ["start"]
                original_check = DurableOperationAuthorizationGuard.recheck_provider_effect

                def recheck(guard: DurableOperationAuthorizationGuard, effect: str) -> None:
                    boundary[0] = effect
                    original_check(guard, effect)

                def read_policy(_store: object) -> LaunchplaneAuthzPolicyRecord:
                    if boundary[0] == denial:
                        raise RuntimeError("fixture policy read unavailable")
                    return policy

                with (
                    patch.object(
                        DurableOperationAuthorizationGuard, "recheck_provider_effect", recheck
                    ),
                    patch(
                        "control_plane.workflows.odoo_stable_operation_worker.read_active_authz_policy_record",
                        side_effect=read_policy,
                    ),
                ):
                    return _execute_prod_promotion_operation(
                        record_store=store,
                        control_plane_root_path=self.root,
                        operation=current,
                        lease_owner="cutover-worker",
                        lease_seconds=300,
                        heartbeat_seconds=5,
                        cutover_provider_factory=lambda plan: provider,
                    )

            provider.uncertain_switch = True
            self.assertTrue(work(claim()))
            held = store.read_odoo_prod_promotion_operation_record(operation.operation_id)
            self.assertEqual(held.status, "reconciliation_required")
            deployment = store.read_deployment_record(
                plan.new.runtime_identity.deployment_record_id
            )
            assert deployment.odoo_cutover is not None
            self.assertEqual(deployment.odoo_cutover.step, "switching")
            from control_plane.workflows.odoo_prod_promotion_run import (
                ODOO_LOGICAL_BACKUP_EFFECT,
                ODOO_PROD_DEPLOY_EFFECT,
            )
            from control_plane.workflows.odoo_prod_rollback import (
                resolve_odoo_prod_rollback_target,
                execute_odoo_prod_rollback,
                OdooProdRollbackRequest,
            )

            target = resolve_odoo_prod_rollback_target(
                record_store=store, request=OdooProdRollbackRequest(context=request.context)
            )
            self.assertEqual(target.artifact_id, plan.production.artifact_id)
            for denial in (
                "start",
                ODOO_LOGICAL_BACKUP_EFFECT,
                ODOO_PROD_DEPLOY_EFFECT,
                "start",
                ODOO_PROD_DEPLOY_EFFECT,
            ):
                before = list(provider.actions)
                self.assertTrue(work(claim(reconcile=True), denial))
                self.assertEqual(
                    store.read_odoo_prod_promotion_operation_record(operation.operation_id).status,
                    "reconciliation_required",
                )
                self.assertEqual(provider.actions, before)
                self.assertEqual(capture.call_count, 1)
                self.assertEqual(
                    store.read_odoo_prod_promotion_operation_record(operation.operation_id).attempt,
                    held.attempt,
                )
            resumed = claim(reconcile=True)
            self.assertIsNotNone(resumed)
            with patch(
                "control_plane.workflows.production_promotion_backup.datetime", wraps=datetime
            ) as clock:
                clock.now.return_value = datetime.now(UTC) + timedelta(days=2)
                self.assertTrue(work(resumed))
            completed = store.read_odoo_prod_promotion_operation_record(operation.operation_id)
            self.assertEqual(completed.status, "pass")
            assert completed.result is not None
            self.assertEqual(completed.result.run_status, "pass")
            self.assertEqual(provider.maintenance_count, 1)
            self.assertEqual(capture.call_count, 1)
            self.assertEqual(
                store.read_environment_inventory(
                    context_name=request.context, instance_name="prod"
                ).runtime_identity,
                plan.new.runtime_identity,
            )
            rolled = execute_odoo_prod_rollback(
                control_plane_root=self.root,
                record_store=store,
                product=request.product,
                request=OdooProdRollbackRequest(context=request.context),
                cutover_provider=provider,
            )
            self.assertEqual(rolled.rollback_status, "pass")
            self.assertEqual(
                store.read_release_tuple_record(
                    context_name=request.context, channel_name="prod"
                ).artifact_id,
                plan.production.artifact_id,
            )
            switched = store.read_deployment_record(rolled.deployment_record_id)
            self.assertTrue(switched.deploy.started_at and switched.deploy.finished_at)
            next_plan = resolve_profile_cutover_plan(
                store,
                product=request.product,
                context=request.context,
                instance="prod",
                artifact_id=plan.candidate.artifact_id,
                release_id="next-accepted-release",
            )
            assert next_plan is not None
            self.assertEqual(next_plan.old.runtime_identity, plan.old.runtime_identity)

    def setUp(self) -> None:
        setup_memory_files(self)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store_count = 0

    def capture(self, action: str = ODOO_PROMOTION_BACKUP_ACTION) -> PostgresRecordStore:
        self.store_count += 1
        store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{self.root / str(self.store_count)}.sqlite3"
        )
        self.addCleanup(store.close)
        store.ensure_schema()
        original = _binding()
        policy = ProductionBackupPolicyRecord.model_validate(
            {
                **original.policy.model_dump(),
                "policy_id": "",
                "record_id": "",
                "policy_digest": "",
                "promotion_action": action,
            }
        )
        binding = ProductionBackupGateWorkerRequest(
            request=original.request.model_copy(update={"promotion_action": action}),
            policy=policy,
            source_target=original.source_target,
            destination_target=original.destination_target,
        )
        store.write_production_backup_target_record(binding.source_target)
        store.write_production_backup_target_record(binding.destination_target)
        store.write_production_backup_policy_record(policy)
        payload = durable_operation_authorization_payload(
            action="production_backup_gate.execute",
            managed_rule_id="example-backup",
            product="example-product",
            context="example-product",
            instances=("prod",),
        )
        store.seed_authz_policy_if_absent(durable_operation_policy_record(payload))
        enqueue_production_backup_gate(
            record_store=store,
            request=binding.request,
            authorization=DurableOperationAuthorization.model_validate(payload),
            operation_key="example-caller|capture",
        )
        with (
            patch(
                "control_plane.workflows.production_backup_gate.control_plane_secrets.resolve_lane_worker_secret_values",
                return_value={
                    "PRODUCTION_BACKUP_SSH_PRIVATE_KEY": "synthetic",
                    "PRODUCTION_BACKUP_SSH_KNOWN_HOSTS": "synthetic",
                },
            ),
            patch(
                "control_plane.workflows.production_backup_provider.subprocess.run",
                side_effect=BackupHost().run,
            ),
        ):
            result = run_verireel_prod_backup_gate_operation_worker_once(
                record_store=store, control_plane_root_path=self.root, lease_owner="test-worker"
            )
        self.assertTrue(result.terminal_write_committed)
        self.assertEqual(store.read_backup_gate_record("backup-example").status, "pass")
        return store

    @staticmethod
    def require(
        store: PostgresRecordStore, action: str = ODOO_PROMOTION_BACKUP_ACTION
    ) -> BackupGateEvidence:
        return require_production_promotion_backup(
            record_store=store,
            product="example-product",
            context="example-product",
            instance="prod",
            promotion_action=action,
            backup_record_id="backup-example",
        )

    def pending(
        self, store: PostgresRecordStore, action: str = ODOO_PROMOTION_BACKUP_ACTION
    ) -> PromotionRecord:
        return PromotionRecord(
            record_id=generate_promotion_record_id(
                context_name="example-product",
                from_instance_name="testing",
                to_instance_name="prod",
            ),
            context="example-product",
            from_instance="testing",
            to_instance="prod",
            artifact_identity=ArtifactIdentityReference(artifact_id="example-artifact"),
            backup_record_id="backup-example",
            backup_gate=self.require(store, action),
            deploy=DeploymentEvidence(
                target_name="example-prod", target_type="compose", deploy_mode="dokploy-compose-api"
            ),
        )

    def test_worker_capture_binds_exact_policy_and_operation(self) -> None:
        store = self.capture()
        evidence = self.require(store)
        operation = store.list_verireel_prod_backup_gate_operation_records()[0]
        self.assertEqual(evidence.evidence["backup_operation_id"], operation.operation_id)
        self.assertEqual(evidence.evidence["backup_record_id"], operation.backup_record_id)
        self.assertIsNotNone(operation.binding)
        assert operation.binding is not None
        self.assertEqual(evidence.evidence["policy_digest"], operation.binding.policy.policy_digest)
        self.assertTrue(evidence.required)

    def test_missing_stale_partial_mismatched_and_failed_evidence_blocks_all_entrypoints(
        self,
    ) -> None:
        for action in (ODOO_PROMOTION_BACKUP_ACTION, GENERIC_WEB_PROMOTION_BACKUP_ACTION):
            store = self.capture(action)
            original = store.read_backup_gate_record("backup-example")
            operation = store.list_verireel_prod_backup_gate_operation_records()[0]
            assert operation.result is not None
            for case, change in (
                ("partial", {"capture_status": "partial"}),
                ("missing snapshot", {"snapshot_name": ""}),
                ("missing PBS", {"independent_backup_id": ""}),
                ("wrong policy", {"policy_digest": "0" * 64}),
                ("wrong target", {"source_target_digest": "0" * 64}),
                ("invalid time", {"snapshot_finished_at": "invalid"}),
                ("stale", {"snapshot_finished_at": "2020-01-01T00:00:00Z"}),
                ("future", {"independent_backup_finished_at": "2099-01-01T00:00:00Z"}),
                ("failed", {}),
            ):
                with self.subTest(action=action, case=case):
                    evidence = {**original.evidence, **change}
                    store.write_backup_gate_record(
                        original.model_copy(
                            update={
                                "evidence": evidence,
                                "status": "fail" if case == "failed" else "pass",
                            }
                        )
                    )
                    store.write_verireel_prod_backup_gate_operation_record(
                        operation.model_copy(
                            update={
                                "result": operation.result.model_copy(update={"evidence": evidence})
                            }
                        )
                    )
                    with self.assertRaises(click.ClickException):
                        self.require(store, action)
                    self.assert_entrypoints_block(store, action)
            store.write_backup_gate_record(original)
            store.write_verireel_prod_backup_gate_operation_record(operation)
            with self.subTest(action=action, case="missing"):
                self.assert_entrypoints_block(store, action, backup_record_id="missing-backup")

    def assert_entrypoints_block(
        self, store: PostgresRecordStore, action: str, *, backup_record_id: str = "backup-example"
    ) -> None:
        if action == GENERIC_WEB_PROMOTION_BACKUP_ACTION:
            profile = _profile().model_copy(update={"product": "example-product"})
            lanes = tuple(
                lane.model_copy(update={"context": "example-product"}) for lane in profile.lanes
            )
            request = GenericWebProdPromotionRequest(
                product="example-product",
                artifact_id="example-artifact",
                backup_record_id=backup_record_id,
            )
            with (
                patch(
                    "control_plane.workflows.generic_web_promotion.resolve_generic_web_promotion_lanes",
                    return_value=(profile, *lanes),
                ),
                patch(
                    "control_plane.workflows.generic_web_promotion._resolve_source_inventory_inputs",
                    return_value=request,
                ),
                patch("control_plane.workflows.generic_web_promotion.require_release_approval"),
                patch(
                    "control_plane.workflows.generic_web_promotion.execute_generic_web_deploy"
                ) as deploy,
                self.assertRaises(click.ClickException),
            ):
                execute_generic_web_prod_promotion(
                    control_plane_root=self.root, record_store=store, request=request
                )
            deploy.assert_not_called()
            return
        with (
            patch("control_plane.workflows.odoo_prod_promotion.require_release_approval"),
            patch(
                "control_plane.workflows.odoo_prod_promotion.execute_odoo_stable_target_replacement_apply"
            ) as replacement,
        ):
            direct = execute_odoo_prod_promotion(
                control_plane_root=self.root,
                state_dir=self.root,
                database_url=store.database_url,
                record_store=store,
                request=OdooProdPromotionRequest(
                    product="example-product",
                    context="example-product",
                    artifact_id="example-artifact",
                    backup_record_id="logical-backup",
                    infrastructure_backup_record_id=backup_record_id,
                ),
            )
        self.assertEqual(direct.promotion_status, "fail")
        replacement.assert_not_called()
        with (
            patch("control_plane.workflows.odoo_prod_promotion_run.require_release_approval"),
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.resolve_odoo_prod_promotion_inputs",
                return_value=_inputs_result(),
            ),
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.execute_odoo_prod_backup_gate"
            ) as logical_backup,
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.execute_odoo_prod_promotion"
            ) as promote,
        ):
            run = execute_odoo_prod_promotion_run(
                control_plane_root=self.root,
                state_dir=self.root,
                database_url=store.database_url,
                record_store=store,
                request=OdooProdPromotionRunRequest(
                    product="example-product",
                    context="example-product",
                    request_id="example-run",
                    infrastructure_backup_record_id=backup_record_id,
                ),
            )
        self.assertEqual(run.run_status, "blocked")
        logical_backup.assert_not_called()
        promote.assert_not_called()

    def test_changed_policy_blocks_before_effect(self) -> None:
        store = self.capture()
        policy = store.list_production_backup_policy_records()[0]
        replacement = ProductionBackupPolicyRecord.model_validate(
            {
                **policy.model_dump(),
                "record_id": "",
                "policy_revision": 2,
                "policy_digest": "",
                "supersedes_record_id": policy.record_id,
            }
        )
        store.write_production_backup_policy_record(replacement)
        with self.assertRaisesRegex(click.ClickException, "changed"):
            self.require(store)
        self.assert_entrypoints_block(store, ODOO_PROMOTION_BACKUP_ACTION)

    def test_later_capture_invalidates_possibly_pruned_snapshot(self) -> None:
        store = self.capture()
        first = store.list_verireel_prod_backup_gate_operation_records()[0]
        assert first.binding is not None
        later_request = first.binding.request.model_copy(
            update={"backup_record_id": "later-backup"}
        )
        later = first.model_copy(
            update={
                "operation_id": "later-operation",
                "backup_record_id": "later-backup",
                "request": later_request,
                "binding": first.binding.model_copy(update={"request": later_request}),
                "started_at": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
            }
        )
        store.write_verireel_prod_backup_gate_operation_record(later)
        with self.assertRaisesRegex(click.ClickException, "superseded"):
            self.require(store)

    def test_guard_refuses_lock_loss_before_first_effect(self) -> None:
        store = self.capture()
        check = Mock()

        @contextmanager
        def lock(_key: str) -> Iterator[Mock]:
            yield check

        with patch.object(store, "production_backup_source_lock", side_effect=lock):
            with production_promotion_backup_guard(
                record_store=store,
                product="example-product",
                context="example-product",
                instance="prod",
                promotion_action=ODOO_PROMOTION_BACKUP_ACTION,
                backup_record_id="backup-example",
                pending_promotion=self.pending(store),
            ) as checkpoint:
                check.side_effect = RuntimeError("lost connection")
                with self.assertRaisesRegex(click.ClickException, "lock was lost"):
                    checkpoint("first_effect")
                check.side_effect = None

    def test_admitted_promotion_finishes_after_policy_change_and_refused_capture(self) -> None:
        store = self.capture()
        original = store.list_verireel_prod_backup_gate_operation_records()[0]
        assert original.binding is not None and original.result is not None
        with production_promotion_backup_guard(
            record_store=store,
            product="example-product",
            context="example-product",
            instance="prod",
            promotion_action=ODOO_PROMOTION_BACKUP_ACTION,
            backup_record_id="backup-example",
            pending_promotion=self.pending(store),
        ) as checkpoint:
            checkpoint("target_update")
            request = original.binding.request.model_copy(
                update={"backup_record_id": "refused-backup"}
            )
            refused = original.model_copy(
                update={
                    "operation_id": "refused-operation",
                    "backup_record_id": "refused-backup",
                    "request": request,
                    "binding": original.binding.model_copy(update={"request": request}),
                    "status": "fail",
                    "error_code": "backup_source_busy",
                    "error_message": "busy",
                    "progress_evidence": {},
                    "result": original.result.model_copy(
                        update={"backup_status": "fail", "evidence": {}}
                    ),
                }
            )
            store.write_verireel_prod_backup_gate_operation_record(refused)
            with self.assertRaisesRegex(click.ClickException, "already used"):
                self.require(store)
            policy = original.binding.policy
            store.write_production_backup_policy_record(
                ProductionBackupPolicyRecord.model_validate(
                    {
                        **policy.model_dump(),
                        "record_id": "",
                        "policy_revision": 2,
                        "policy_digest": "",
                        "supersedes_record_id": policy.record_id,
                    }
                )
            )
            checkpoint("odoo_module_update")
        with self.assertRaisesRegex(click.ClickException, "changed"):
            self.require(store)

    def test_guard_rechecks_evidence_immediately_before_first_effect(self) -> None:
        store = self.capture()
        with production_promotion_backup_guard(
            record_store=store,
            product="example-product",
            context="example-product",
            instance="prod",
            promotion_action=ODOO_PROMOTION_BACKUP_ACTION,
            backup_record_id="backup-example",
            pending_promotion=self.pending(store),
        ) as checkpoint:
            record = store.read_backup_gate_record("backup-example")
            store.write_backup_gate_record(record.model_copy(update={"status": "fail"}))
            with self.assertRaisesRegex(click.ClickException, "incomplete or mismatched"):
                checkpoint("target_update")

    def test_guard_records_lock_loss_without_interrupting_admitted_deployment(self) -> None:
        store = self.capture()
        check = Mock()

        @contextmanager
        def lock(_key: str) -> Iterator[Mock]:
            yield check

        with (
            patch.object(store, "production_backup_source_lock", side_effect=lock),
            self.assertLogs(level="WARNING"),
        ):
            with production_promotion_backup_guard(
                record_store=store,
                product="example-product",
                context="example-product",
                instance="prod",
                promotion_action=ODOO_PROMOTION_BACKUP_ACTION,
                backup_record_id="backup-example",
                pending_promotion=self.pending(store),
            ) as checkpoint:
                checkpoint("target_update")
                check.side_effect = RuntimeError("lost connection")
                checkpoint("post_deploy")
        self.assertEqual(checkpoint.evidence["source_lock_status"], "lost_after_effect")
        self.assertIn("source_lock_lost_at", checkpoint.evidence)

        check.side_effect = None
        store = self.capture()
        with (
            patch.object(store, "production_backup_source_lock", side_effect=lock),
            self.assertLogs(level="WARNING"),
        ):
            with production_promotion_backup_guard(
                record_store=store,
                product="example-product",
                context="example-product",
                instance="prod",
                promotion_action=ODOO_PROMOTION_BACKUP_ACTION,
                backup_record_id="backup-example",
                pending_promotion=self.pending(store),
            ) as completed:
                completed("target_update")
                check.side_effect = RuntimeError("lost connection on completion")
        self.assertEqual(completed.evidence["source_lock_status"], "lost_after_effect")

    def test_admitted_or_crashed_promotion_cannot_reuse_a_capture(self) -> None:
        for action in (ODOO_PROMOTION_BACKUP_ACTION, GENERIC_WEB_PROMOTION_BACKUP_ACTION):
            with self.subTest(action=action):
                store = self.capture(action)
                pending = self.pending(store, action)
                with production_promotion_backup_guard(
                    record_store=store,
                    product="example-product",
                    context="example-product",
                    instance="prod",
                    promotion_action=action,
                    backup_record_id="backup-example",
                    pending_promotion=pending,
                ) as checkpoint:
                    checkpoint("target_update")
                self.assertEqual(store.read_promotion_record(pending.record_id), pending)
                with self.assertRaisesRegex(click.ClickException, "already used"):
                    self.require(store, action)
                self.assert_entrypoints_block(store, action)
                self.assertEqual(store.read_promotion_record(pending.record_id), pending)

    def test_same_second_promotions_keep_distinct_admission_records(self) -> None:
        store = self.capture()
        with patch("control_plane.workflows.promote.datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 26, tzinfo=UTC)
            first = self.pending(store)
            second = self.pending(store)
        store.write_promotion_record(first)
        store.write_promotion_record(second)
        self.assertEqual(len(store.list_promotion_records()), 2)

    def test_admitted_exception_records_failed_attempt_and_preserves_uncertainty(self) -> None:
        for start_effects in (False, True):
            with self.subTest(start_effects=start_effects):
                store = self.capture()
                pending = self.pending(store)
                interrupted_provider = Mock(
                    side_effect=click.ClickException("provider interrupted")
                )
                with self.assertRaisesRegex(click.ClickException, "provider interrupted"):
                    with production_promotion_backup_guard(
                        record_store=store,
                        product="example-product",
                        context="example-product",
                        instance="prod",
                        promotion_action=ODOO_PROMOTION_BACKUP_ACTION,
                        backup_record_id="backup-example",
                        pending_promotion=pending,
                    ) as checkpoint:
                        if start_effects:
                            checkpoint("target_update")
                        interrupted_provider()
                recorded = store.read_promotion_record(pending.record_id)
                self.assertEqual(recorded.deploy.status, "fail")
                self.assertEqual(recorded.backup_gate.status, "pass")
                self.assertEqual(
                    recorded.backup_gate.evidence["provider_effects_status"],
                    "unknown_after_failure" if start_effects else "not_started",
                )
                with self.assertRaisesRegex(click.ClickException, "already used"):
                    self.require(store)

    def test_worker_refusal_before_provider_write_records_no_effect(self) -> None:
        store = self.capture()
        pending = self.pending(store)
        provider_write = Mock()
        worker_checkpoint = Mock(side_effect=RuntimeError("Worker lost its lease before writing."))
        with self.assertRaisesRegex(RuntimeError, "lost its lease"):
            with production_promotion_backup_guard(
                record_store=store,
                product="example-product",
                context="example-product",
                instance="prod",
                promotion_action=ODOO_PROMOTION_BACKUP_ACTION,
                backup_record_id="backup-example",
                pending_promotion=pending,
            ) as guard:
                _checkpoint_chain(worker_checkpoint, guard)("target_update")
                provider_write()
        provider_write.assert_not_called()
        worker_checkpoint.assert_called_once()
        recorded = store.read_promotion_record(pending.record_id)
        self.assertEqual(recorded.deploy.status, "fail")
        self.assertEqual(recorded.backup_gate.evidence["provider_effects_status"], "not_started")

    def test_only_a_source_read_refusal_before_effects_leaves_backup_available(self) -> None:
        from control_plane.lane_movement import LaneMovementRefused

        for effects_started in (False, True):
            with self.subTest(effects_started=effects_started):
                store = self.capture()
                pending = self.pending(store)
                with self.assertRaises(LaneMovementRefused):
                    with production_promotion_backup_guard(
                        record_store=store,
                        product="example-product",
                        context="example-product",
                        instance="prod",
                        promotion_action=ODOO_PROMOTION_BACKUP_ACTION,
                        backup_record_id="backup-example",
                        pending_promotion=pending,
                    ) as guard:
                        if effects_started:
                            guard("target_update")
                        raise LaneMovementRefused("source_order_unavailable")
                if effects_started:
                    with self.assertRaisesRegex(click.ClickException, "already used"):
                        self.require(store)
                else:
                    self.assertEqual(self.require(store).status, "pass")

    def test_generic_web_cannot_opt_out(self) -> None:
        with self.assertRaises(ValidationError):
            GenericWebProdPromotionRequest.model_validate(
                {"product": "example", "backup_required": False}
            )
