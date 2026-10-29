from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator, Any, cast
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
from control_plane.workflows.production_backup_gate_operation_worker import (
    run_production_backup_gate_operation_worker_once,
)
from tests.support.durable_operations import (
    durable_operation_authorization_payload,
    durable_operation_policy_record,
)
from tests.test_generic_web_promotion import _profile
from tests.test_odoo_prod_promotion_run import _inputs_result
from tests.test_production_backup_provider import BackupHost, _binding, setup_memory_files
from tests.test_generic_web_client_release import expired_backup_clock
from control_plane.generic_web_source_qualification import GenericWebPromotionAdmissionRefused


class ProductionPromotionBackupTests(unittest.TestCase):
    def test_only_generic_web_expiry_is_a_temporary_admission_refusal(self) -> None:
        for action in (ODOO_PROMOTION_BACKUP_ACTION, GENERIC_WEB_PROMOTION_BACKUP_ACTION):
            with self.subTest(action=action):
                store = self.capture(action)
                operation = store.list_verireel_prod_backup_gate_operation_records()[0]
                pending = self.pending(store, action)
                with self.assertRaises(click.ClickException):
                    with production_promotion_backup_guard(
                        record_store=store,
                        product=operation.product,
                        context=operation.context,
                        instance=operation.instance,
                        promotion_action=action,
                        backup_record_id=operation.backup_record_id,
                        pending_promotion=pending,
                    ) as checkpoint:
                        with expired_backup_clock(store, operation.operation_id):
                            checkpoint("first_effect")
                refused = store.read_promotion_record(pending.record_id)
                temporary = (
                    refused.failure is not None
                    and refused.failure.code == GenericWebPromotionAdmissionRefused.failure_code
                )
                self.assertEqual(temporary, action == GENERIC_WEB_PROMOTION_BACKUP_ACTION)

    def test_interrupted_cutover_resumes_through_real_backup_guard_without_new_capture(
        self,
    ) -> None:
        self.run_cutover_worker("operator")

    def test_begun_client_cutover_finishes_when_testing_advances_during_maintenance(self) -> None:
        self.run_cutover_worker("testing")

    def test_begun_client_cutover_finishes_when_releases_are_held_during_maintenance(self) -> None:
        self.run_cutover_worker("held")

    def test_client_cutover_restart_reuses_admission_after_testing_advances_and_release_hold(
        self,
    ) -> None:
        self.run_cutover_worker("restart")

    def test_begun_settings_cutover_preserves_original_authority_while_settings_are_pending(
        self,
    ) -> None:
        self.run_cutover_worker("settings-testing")

    def test_settings_cutover_restart_preserves_original_authority_after_lost_maintenance_response(
        self,
    ) -> None:
        self.run_cutover_worker("settings-restart")

    def test_deterministic_native_preparation_failure_has_a_configured_attempt_bound(self) -> None:
        self.run_cutover_worker("prepare-failure")

    def test_definite_native_maintenance_failure_does_not_dispatch_another_upgrade(self) -> None:
        self.run_cutover_worker("maintenance-failure")

    def test_native_entry_refusal_before_record_creation_releases_the_lane(self) -> None:
        self.run_cutover_worker("entry-refusal")

    def run_cutover_worker(self, scenario: str) -> None:
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
        website_settings = scenario.startswith("settings-")
        scenario = scenario.removeprefix("settings-")
        client = scenario != "operator"
        provider = IsolatedCutoverProvider(self.root / "cutover", database_changing=client)
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
        profile["production_use"] = "live" if client else "prelaunch"
        if client:
            profile["repository"] = "example/site"
            profile["owner"] = {"github_id": "9001", "github_login": "site-owner"}
            profile["release_on_acceptance"] = "promote"
        profile["preview"] = {"enabled": False}
        lanes = [
            {
                "instance": "prod",
                "context": "example-product",
                "base_url": "https://fixture.invalid"
                if website_settings
                else "http://fixture.invalid",
                "health_url": "http://fixture.invalid/launchplane/health",
                "odoo_cutover": config.model_dump(mode="json"),
            }
        ]
        if client:
            lanes.append(
                {
                    "instance": "testing",
                    "context": "example-product",
                    "base_url": "https://testing.fixture.invalid",
                }
            )
        profile["lanes"] = lanes
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
        if client:
            from control_plane.contracts.deployment_record import DeploymentRecord
            from control_plane.contracts.release_review import ReleaseReviewDecisionRecord
            from control_plane.release_review import build_release_review
            from tests.test_release_review import github_read

            if website_settings:
                from control_plane.contracts.odoo_instance_override_record import (
                    OdooInstanceOverrideRecord,
                    OdooOverrideApplyResult,
                )
                from tests.test_odoo_cutover_website_settings import snapshot

                original_settings = snapshot(pair, production=True)
                candidate_settings = snapshot(pair)
                store.write_odoo_instance_override_record(
                    OdooInstanceOverrideRecord(
                        context="example-product",
                        instance="prod",
                        website_settings=candidate_settings,
                        last_apply=OdooOverrideApplyResult(
                            attempted=True,
                            status="pass",
                            applied_at=datetime.now(UTC).isoformat(),
                            website_settings=original_settings,
                            website_settings_verified=True,
                        ),
                        updated_at=datetime.now(UTC).isoformat(),
                    )
                )

            current_profile = store.read_product_profile_record("example-product")
            review = build_release_review(store=store, profile=current_profile, read=github_read)
            assert review.checklist is not None
            accepted = ReleaseReviewDecisionRecord(
                record_id="accepted-cutover-decision",
                product=current_profile.product,
                checklist=review.checklist,
                checklist_digest=review.checklist_digest,
                decision="accepted",
                actor_github_id=current_profile.owner.github_id,
                actor_github_login=current_profile.owner.github_login,
                decided_at=datetime.now(UTC).isoformat(),
                release_start="promote",
                release_issue_url="https://github.com/example/site/issues/99",
            )
            store.write_release_review_decision_record(accepted)
            if website_settings:
                self.assertEqual(accepted.checklist.production_website_settings, original_settings)
                self.assertEqual(accepted.checklist.website_settings, candidate_settings)
                self.assertNotEqual(
                    accepted.checklist.production.website_settings_sha256,
                    accepted.checklist.candidate.website_settings_sha256,
                )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id=old_identity.deployment_record_id,
                    context="example-product",
                    instance="prod",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id=pair.production.artifact_id
                    ),
                    runtime_identity=old_identity,
                    source_git_ref=pair.production.source_commit,
                    deploy=DeploymentEvidence(
                        status="pass",
                        deploy_mode="atomic-two-slot",
                        target_type="compose",
                        target_name="old",
                    ),
                )
            )
        request = OdooProdPromotionRunRequest(
            context="example-product",
            product="example-product",
            request_id="accepted-cutover",
            infrastructure_backup_record_id="backup-example",
            expected_artifact_id=pair.candidate.artifact_id if client else "",
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
        if website_settings:
            from control_plane.odoo_cutover_plan import decision_bound_cutover_plan

            plan = decision_bound_cutover_plan(plan, accepted)
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

        def current_inputs(**kwargs: object) -> Any:
            testing = store.read_release_tuple_record(
                context_name=request.context, channel_name="testing"
            )
            manifest = store.read_artifact_manifest(testing.artifact_id)
            return inputs.model_copy(
                update={
                    "artifact_id": manifest.artifact_id,
                    "source_git_ref": manifest.source_commit,
                    "image_repository": manifest.image.repository,
                    "image_digest": manifest.image.digest,
                    "release_tuple_id": testing.tuple_id,
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
                side_effect=current_inputs,
            ) as resolve_inputs,
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.execute_odoo_prod_backup_gate",
                side_effect=capture_logical,
            ) as capture,
            patch(
                "control_plane.release_review.current_release_review",
                side_effect=lambda **kwargs: build_release_review(
                    store=store, profile=kwargs["profile"], read=github_read
                ),
            )
            if client
            else nullcontext(),
        ):
            from control_plane.workflows.odoo_stable_operation_worker import (
                _execute_prod_promotion_operation,
            )
            from control_plane.durable_operation_authorization import (
                capture_durable_operation_authorization,
            )
            from control_plane.contracts.odoo_prod_promotion_operation import (
                OdooProdPromotionCheckpoint,
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
            authorization = capture_durable_operation_authorization(
                identity=_github_human_identity(),
                action=ODOO_PROMOTION_BACKUP_ACTION,
                product=request.product,
                context=request.context,
                instances=("prod",),
                policy_record=policy,
                authorized_at=datetime.now(UTC).isoformat(),
            )
            checkpoints = original.checkpoints
            scope = original.idempotency_scope
            if client:
                from control_plane.client_release import client_release_grant
                from control_plane.odoo_release_recovery import pin_odoo_release_recovery_target

                authorization = client_release_grant(
                    decision=accepted,
                    action=ODOO_PROMOTION_BACKUP_ACTION,
                    context=request.context,
                    authorized_at=datetime.now(UTC).isoformat(),
                )
                scope = "client-release"
                checkpoints = (
                    OdooProdPromotionCheckpoint(
                        phase="created",
                        recorded_at=original.created_at,
                        evidence=pin_odoo_release_recovery_target(store, accepted, request.context),
                    ),
                )
            operation = type(original).model_validate(
                original.model_copy(
                    update={
                        "product": request.product,
                        "context": request.context,
                        "request": request,
                        "idempotency_scope": scope,
                        "checkpoints": checkpoints,
                        "operation_id": build_odoo_prod_promotion_operation_id(
                            product=request.product,
                            context=request.context,
                            idempotency_key=original.idempotency_key,
                            idempotency_scope=scope,
                        ),
                        "request_fingerprint": odoo_prod_promotion_request_fingerprint(request),
                        "authorization": authorization,
                    }
                ).model_dump()
            )
            store.create_odoo_prod_promotion_operation_record_if_no_active_lane(operation)

            def claim(
                *, reconcile: bool = False, owner: str = "cutover-worker"
            ) -> OdooProdPromotionOperationRecord | None:
                at = datetime.now(UTC)
                if reconcile:
                    from control_plane.contracts.odoo_prod_promotion_operation import (
                        ODOO_CUTOVER_RECONCILIATION_DELAY,
                    )

                    current = store.read_odoo_prod_promotion_operation_record(
                        operation.operation_id
                    )
                    at = max(
                        at,
                        datetime.fromisoformat(current.updated_at.replace("Z", "+00:00"))
                        + ODOO_CUTOVER_RECONCILIATION_DELAY,
                    )
                arguments: dict[str, Any] = dict(
                    lease_owner=owner,
                    lease_expires_at=(at + timedelta(minutes=5)).isoformat(),
                    claimed_at=at.isoformat(),
                )
                if reconcile:
                    return store.claim_odoo_cutover_reconciliation_record(
                        **arguments,
                    )
                return store.claim_next_odoo_prod_promotion_operation_record(**arguments)

            def work(
                current: OdooProdPromotionOperationRecord | None,
                denial: str = "",
                *,
                reconciling: bool = False,
            ) -> bool:
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
                        lease_owner=current.lease_owner,
                        lease_seconds=300,
                        heartbeat_seconds=5,
                        cutover_provider_factory=lambda plan: provider,
                        reconciling=reconciling,
                    )

            if scenario in {"prepare-failure", "maintenance-failure"}:
                from control_plane.contracts.odoo_prod_promotion_operation import (
                    ODOO_CUTOVER_RECONCILIATION_DELAY,
                )
                from control_plane.workflows.odoo_stable_operation_worker import (
                    DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS,
                    run_odoo_stable_operation_worker_once,
                )

                method = "prepare_candidate" if scenario == "prepare-failure" else "maintenance"
                with (
                    patch.object(
                        provider, method, side_effect=ValueError("deterministic native refusal")
                    ) as effects,
                    patch(
                        "control_plane.workflows.odoo_stable_operation_worker.read_active_authz_policy_record",
                        return_value=policy,
                    ),
                ):
                    for _ in range(DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS + 2):
                        current = store.read_odoo_prod_promotion_operation_record(
                            operation.operation_id
                        )
                        at = max(
                            datetime.now(UTC),
                            datetime.fromisoformat(current.updated_at.replace("Z", "+00:00"))
                            + ODOO_CUTOVER_RECONCILIATION_DELAY,
                        )
                        with patch(
                            "control_plane.workflows.odoo_stable_operation_worker._utc_now_timestamp",
                            return_value=at.isoformat(),
                        ):
                            result = run_odoo_stable_operation_worker_once(
                                record_store=store,
                                control_plane_root_path=self.root,
                                lease_owner="bounded-native-worker",
                                max_attempts=DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS,
                                cutover_provider_factory=lambda current_plan: provider,
                            )
                        if result.status == "worked":
                            self.assertTrue(result.terminal_write_committed)
                    expected = (
                        DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS
                        if method == "prepare_candidate"
                        else 1
                    )
                    self.assertEqual(effects.call_count, expected)
                held = store.read_odoo_prod_promotion_operation_record(operation.operation_id)
                self.assertEqual(held.status, "reconciliation_required")
                self.assertEqual(held.attempt, expected)
                deployment = store.read_deployment_record(
                    plan.new.runtime_identity.deployment_record_id
                )
                assert deployment.odoo_cutover is not None
                self.assertEqual(
                    deployment.odoo_cutover.step,
                    "preparing" if method == "prepare_candidate" else "maintaining",
                )
                self.assertEqual(capture.call_count, 1)
                self.assertEqual(store.list_odoo_prod_rollback_operation_records(), ())
                return

            if scenario == "entry-refusal":
                from types import SimpleNamespace
                from control_plane.dokploy.cutover_host import BoundOdooCutoverHost
                from control_plane.dokploy.cutover_execution import require_cutover_execution
                from control_plane.odoo_cutover_recovery import operation_cutover
                from control_plane.odoo_release_recovery import odoo_release_wrote_production

                def entry_guard() -> None:
                    profile = store.read_product_profile_record(request.product)
                    store.write_product_profile_record(
                        profile.model_copy(update={"release_on_acceptance": "held"})
                    )
                    require_cutover_execution(plan)

                host = cast(
                    Any,
                    SimpleNamespace(
                        plan=plan, admitted=False, guard=entry_guard, record=lambda: None
                    ),
                )
                host.enter_execution = lambda current_plan: getattr(
                    BoundOdooCutoverHost, "enter_execution"
                )(host, current_plan)
                before = list(provider.actions)
                with (
                    patch.object(
                        provider,
                        "exclusive",
                        side_effect=lambda current_plan: BoundOdooCutoverHost.exclusive_state(
                            host, current_plan
                        ),
                    ),
                    patch.object(provider, "maintenance") as entry_maintenance,
                ):
                    self.assertTrue(work(claim()))
                finished = store.read_odoo_prod_promotion_operation_record(operation.operation_id)
                self.assertEqual(finished.status, "fail", finished.error_message)
                self.assertTrue(odoo_release_wrote_production(finished))
                self.assertIsNone(operation_cutover(store, finished))
                from control_plane.odoo_cutover_plan import cutover_record_id

                self.assertEqual(
                    store.read_promotion_record(
                        "promotion-cutover-" + cutover_record_id(plan)
                    ).deploy.status,
                    "fail",
                )
                self.assertEqual(provider.actions, before)
                entry_maintenance.assert_not_called()
                self.assertEqual(store.list_odoo_prod_rollback_operation_records(), ())
                with store.odoo_synchronous_lane_reservation(
                    product=request.product, context=request.context, instance="prod"
                ) as owner:
                    self.assertIsNone(owner)
                return

            if client:
                from control_plane.client_release import client_release_grant_allows
                from control_plane.dokploy.cutover_execution import require_cutover_execution
                from control_plane.odoo_cutover_recovery import BackupVerificationRejected
                from control_plane.odoo_release_recovery import PRODUCTION_WRITE_KEY

                guarded_snapshots: list[tuple[Any, ...]] = []
                self.assertTrue(client_release_grant_allows(store, authorization))

                def observe_dispatch() -> None:
                    before = store.read_odoo_prod_promotion_operation_record(operation.operation_id)
                    for _ in range(8):
                        require_cutover_execution(plan)
                    after = store.read_odoo_prod_promotion_operation_record(operation.operation_id)
                    self.assertEqual(after.checkpoints, before.checkpoints)
                    guarded_snapshots.append(after.checkpoints)
                    if website_settings:
                        from control_plane.client_release import (
                            ClientReleaseStepView,
                            read_client_release_run,
                        )
                        from control_plane.contracts.odoo_business_hours import BusinessHoursWindow

                        views = (
                            ClientReleaseStepView(
                                step="promote-1",
                                kind="promote",
                                status="running",
                                operation_id=operation.operation_id,
                            ),
                        )
                        with (
                            patch(
                                "control_plane.client_release.read_client_release_step_views",
                                return_value=views,
                            ),
                            patch(
                                "control_plane.client_release.release_window",
                                return_value=BusinessHoursWindow(
                                    applies=True, blocked_reason="fresh window closed"
                                ),
                            ) as window,
                            patch(
                                "control_plane.service_deploy_drain.read_status",
                                return_value={"admission_paused": True},
                            ),
                        ):
                            view = read_client_release_run(
                                store=store,
                                profile=store.read_product_profile_record(request.product),
                                decision=accepted,
                                control_plane_root=self.root,
                            )
                        assert view is not None
                        self.assertEqual(view.state, "running")
                        self.assertEqual(view.blocked_reason, "")
                        self.assertEqual(view.next_release_at, "")
                        window.assert_not_called()

                def drift() -> None:
                    if scenario in {"testing", "restart"}:
                        newer = plan.candidate.model_copy(update={"artifact_id": "newer-testing"})
                        store.write_artifact_manifest(newer)
                        testing = store.read_release_tuple_record(
                            context_name=request.context, channel_name="testing"
                        )
                        store.write_release_tuple_record(
                            testing.model_copy(update={"artifact_id": newer.artifact_id})
                        )
                    if scenario in {"held", "restart"}:
                        current_profile = store.read_product_profile_record(request.product)
                        store.write_product_profile_record(
                            current_profile.model_copy(update={"release_on_acceptance": "held"})
                        )
                    self.assertFalse(client_release_grant_allows(store, authorization))

                original_maintenance = provider.maintenance
                response_lost = False

                def maintenance(current: Any) -> Any:
                    nonlocal response_lost
                    receipt = original_maintenance(current)
                    if website_settings:
                        from control_plane.contracts.odoo_cutover import OdooWebsiteSettingsReceipt

                        pending_settings = store.read_odoo_instance_override_record(
                            context_name=request.context, instance_name="prod"
                        )
                        self.assertFalse(pending_settings.last_apply.website_settings_verified)
                        assert current.website_settings is not None
                        digest = current.website_settings.settings.digest
                        receipt = receipt.model_copy(
                            update={
                                "website_settings": OdooWebsiteSettingsReceipt(
                                    binding=current.new.binding,
                                    image=current.new.image,
                                    settings_sha256=digest,
                                    startup_settings_sha256=digest,
                                    retained_startup_settings_sha256=digest,
                                    committed=True,
                                )
                            }
                        )
                        provider.maintenance_receipt = receipt
                    drift()
                    observe_dispatch()
                    if scenario == "restart" and not response_lost:
                        response_lost = True
                        raise TimeoutError("native maintenance response lost")
                    return receipt

                def reject_backup(current: Any) -> Any:
                    observe_dispatch()
                    raise BackupVerificationRejected("verification rejected before maintenance")

                original_handoff = provider.handoff_writers

                def handoff(current: Any, slot: Any) -> None:
                    observe_dispatch()
                    original_handoff(current, slot)

                with (
                    patch.object(provider, "maintenance", side_effect=maintenance),
                    patch.object(provider, "handoff_writers", side_effect=handoff),
                    patch.object(provider, "backup", side_effect=reject_backup)
                    if scenario == "abort"
                    else patch.object(
                        provider, "backup", wraps=provider.backup
                    ) as coherent_capture,
                ):
                    pending = store.read_odoo_prod_promotion_operation_record(
                        operation.operation_id
                    )
                    self.assertEqual(pending.status, "pending")
                    self.assertFalse(
                        any(
                            item.evidence.get(PRODUCTION_WRITE_KEY) == "true"
                            for item in pending.checkpoints
                        )
                    )
                    self.assertRaises(
                        FileNotFoundError,
                        store.read_deployment_record,
                        plan.new.runtime_identity.deployment_record_id,
                    )
                    self.assertTrue(work(claim()))
                    if scenario == "restart":
                        held = store.read_odoo_prod_promotion_operation_record(
                            operation.operation_id
                        )
                        self.assertEqual(held.status, "reconciliation_required", held.error_message)
                        deployment = store.read_deployment_record(
                            plan.new.runtime_identity.deployment_record_id
                        )
                        assert deployment.odoo_cutover is not None
                        self.assertEqual(deployment.odoo_cutover.step, "maintaining")
                        self.assertEqual(current_inputs().artifact_id, "newer-testing")
                        previous_input_reads = resolve_inputs.call_count
                        resumed = claim(reconcile=True, owner="replacement-worker")
                        self.assertIsNotNone(
                            resumed,
                            "lost maintenance response must permit original-operation reconciliation",
                        )
                        self.assertTrue(work(resumed, reconciling=True))
                        self.assertEqual(resolve_inputs.call_count, previous_input_reads)

                completed = store.read_odoo_prod_promotion_operation_record(operation.operation_id)
                deployment = store.read_deployment_record(
                    plan.new.runtime_identity.deployment_record_id
                )
                assert deployment.odoo_cutover is not None
                self.assertEqual(
                    completed.status,
                    "fail" if scenario == "abort" else "pass",
                    completed.error_message,
                )
                self.assertEqual(
                    deployment.odoo_cutover.step, "aborted" if scenario == "abort" else "complete"
                )
                self.assertEqual(provider.target, plan.old if scenario == "abort" else plan.new)
                self.assertEqual(provider.maintenance_count, 0 if scenario == "abort" else 1)
                self.assertEqual(coherent_capture.call_count, 1)
                self.assertEqual(capture.call_count, 1)
                self.assertTrue(guarded_snapshots)
                self.assertEqual(
                    sum(
                        item.evidence.get(PRODUCTION_WRITE_KEY) == "true"
                        for item in completed.checkpoints
                    ),
                    1,
                )
                self.assertEqual(store.list_odoo_prod_rollback_operation_records(), ())
                with store.odoo_synchronous_lane_reservation(
                    product=operation.product,
                    context=operation.context,
                    instance=operation.instance,
                ) as owner:
                    self.assertIsNone(owner)
                if scenario == "abort":
                    self.assertFalse(provider.fenced)
                    self.assertEqual(set(provider.owners.values()), {plan.old.slot_id})
                return

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
                self.assertTrue(work(claim(reconcile=True), denial, reconciling=True))
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
                self.assertTrue(work(resumed, reconciling=True))
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
            audit_before = list(provider.actions)
            store.write_odoo_prod_promotion_operation_record(
                completed.model_copy(
                    update={
                        "status": "reconciliation_required",
                        "finished_at": "",
                        "lease_owner": "",
                        "lease_expires_at": "",
                        "result": None,
                        "error_code": "operation_reconciliation_required",
                        "error_message": "completion audit reply lost",
                        "heartbeat_at": "",
                    }
                )
            )
            with (
                patch(
                    "control_plane.workflows.odoo_prod_promotion_run.require_production_promotion_backup",
                    side_effect=AssertionError("completed audit cannot require a new backup"),
                ),
                patch.object(provider, "readiness", side_effect=AssertionError("already serving")),
                patch.object(provider, "switch", side_effect=AssertionError("already switched")),
                patch.object(
                    provider, "handoff_writers", side_effect=AssertionError("already running")
                ),
            ):
                self.assertTrue(work(claim(reconcile=True), reconciling=True))
            audited = store.read_odoo_prod_promotion_operation_record(operation.operation_id)
            self.assertEqual(audited.status, "pass", audited.error_message)
            self.assertEqual(audited.attempt, completed.attempt)
            self.assertEqual(audited.checkpoints, completed.checkpoints)
            self.assertEqual(provider.actions, audit_before)
            self.assertEqual(capture.call_count, 1)
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
            rollback_actions = list(provider.actions)
            from control_plane.odoo_cutover_recovery import rollback_cutover_deployment

            self.assertIsNone(
                rollback_cutover_deployment(store, OdooProdRollbackRequest(context=request.context))
            )
            original_inverse = rollback_cutover_deployment(
                store,
                OdooProdRollbackRequest(context=request.context),
                include_terminal_rollback=True,
            )
            assert original_inverse is not None
            self.assertEqual(
                original_inverse.record_id, plan.new.runtime_identity.deployment_record_id
            )
            with (
                patch.object(provider, "readiness", side_effect=AssertionError("already serving")),
                patch.object(provider, "switch", side_effect=AssertionError("already switched")),
                patch.object(
                    provider, "handoff_writers", side_effect=AssertionError("already running")
                ),
            ):
                rollback_audit = execute_odoo_prod_rollback(
                    control_plane_root=self.root,
                    record_store=store,
                    product=request.product,
                    request=OdooProdRollbackRequest(context=request.context),
                    cutover_provider=provider,
                    cutover_reconciliation=True,
                )
            self.assertEqual(rollback_audit.rollback_status, "pass")
            self.assertEqual(rollback_audit.deployment_record_id, rolled.deployment_record_id)
            self.assertEqual(provider.actions, rollback_actions)
            from tests.test_odoo_prod_promotion_operation import _rollback_operation
            from control_plane.contracts.odoo_prod_rollback_operation import (
                OdooProdRollbackCheckpoint,
                build_odoo_prod_rollback_operation_id,
                odoo_prod_rollback_request_fingerprint,
            )
            from control_plane.odoo_release_recovery import PRODUCTION_WRITE_KEY
            from control_plane.workflows.odoo_stable_operation_worker import (
                _execute_prod_rollback_operation,
            )

            rollback_request = OdooProdRollbackRequest(
                context=request.context, promotion_record_id=completed.result.promotion_record_id
            )
            rollback_operation = _rollback_operation()
            rollback_operation = type(rollback_operation).model_validate(
                {
                    **rollback_operation.model_dump(),
                    "operation_id": build_odoo_prod_rollback_operation_id(
                        product=request.product,
                        context=request.context,
                        idempotency_key=rollback_operation.idempotency_key,
                        idempotency_scope=rollback_operation.idempotency_scope,
                    ),
                    "product": request.product,
                    "context": request.context,
                    "request": rollback_request,
                    "request_fingerprint": odoo_prod_rollback_request_fingerprint(
                        product=request.product, request=rollback_request
                    ),
                    "authorization": rollback_operation.authorization.model_copy(
                        update={"product": request.product, "context": request.context}
                    ),
                    "target": target,
                    "status": "reconciliation_required",
                    "phase": "rollback_started",
                    "attempt": 1,
                    "error_code": "operation_reconciliation_required",
                    "error_message": "original rollback completion audit lost",
                    "checkpoints": (
                        OdooProdRollbackCheckpoint(
                            phase="rollback_started",
                            recorded_at=datetime.now(UTC).isoformat(),
                            evidence={PRODUCTION_WRITE_KEY: "true"},
                        ),
                    ),
                }
            )
            store.create_odoo_prod_rollback_operation_record_if_no_active_lane(rollback_operation)
            at = datetime.now(UTC)
            reclaimed_rollback = store.claim_odoo_cutover_rollback_reconciliation_record(
                lease_owner="rollback-audit",
                lease_expires_at=(at + timedelta(minutes=5)).isoformat(),
                claimed_at=at.isoformat(),
            )
            self.assertIsNotNone(reclaimed_rollback)
            assert reclaimed_rollback is not None
            with (
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker.read_active_authz_policy_record",
                    return_value=policy,
                ),
                patch.object(provider, "readiness", side_effect=AssertionError("already serving")),
                patch.object(provider, "switch", side_effect=AssertionError("already switched")),
                patch.object(
                    provider, "handoff_writers", side_effect=AssertionError("already running")
                ),
            ):
                self.assertTrue(
                    _execute_prod_rollback_operation(
                        record_store=store,
                        control_plane_root_path=self.root,
                        operation=reclaimed_rollback,
                        lease_owner="rollback-audit",
                        lease_seconds=300,
                        heartbeat_seconds=5,
                        cutover_provider_factory=lambda plan: provider,
                        reconciling=True,
                    )
                )
            rollback_completed = store.read_odoo_prod_rollback_operation_record(
                rollback_operation.operation_id
            )
            self.assertEqual(rollback_completed.status, "pass", rollback_completed.error_message)
            self.assertEqual(rollback_completed.checkpoints, rollback_operation.checkpoints)
            self.assertEqual(rollback_completed.attempt, rollback_operation.attempt)
            self.assertEqual(provider.actions, rollback_actions)
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
                "control_plane.workflows.production_backup_gate.resolve_guest_capture_values",
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
            result = run_production_backup_gate_operation_worker_once(
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
            patch(
                "control_plane.workflows.odoo_prod_promotion.require_release_approval",
                return_value=None,
            ),
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

    def test_only_source_admission_refusals_before_effects_leave_backup_available(self) -> None:
        from control_plane.lane_movement import LaneMovementRefused
        from control_plane.generic_web_source_qualification import GenericWebPromotionSourceRefused

        for refusal in (
            LaneMovementRefused("source_order_unavailable"),
            GenericWebPromotionSourceRefused("Testing must qualify before admission."),
        ):
            for effects_started in (False, True):
                with self.subTest(refusal=type(refusal).__name__, effects_started=effects_started):
                    store = self.capture()
                    pending = self.pending(store)
                    with self.assertRaises(type(refusal)):
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
                            raise refusal
                    if effects_started:
                        with self.assertRaises(click.ClickException):
                            self.require(store)
                    else:
                        self.assertEqual(self.require(store).status, "pass")

    def test_generic_web_cannot_opt_out(self) -> None:
        with self.assertRaises(ValidationError):
            GenericWebProdPromotionRequest.model_validate(
                {"product": "example", "backup_required": False}
            )
