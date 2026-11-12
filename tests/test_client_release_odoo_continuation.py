from contextlib import ExitStack
from functools import partial
from typing import Any, cast
from datetime import UTC, datetime
from collections.abc import Callable
from fastapi import FastAPI
from httpx2 import Response
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.contracts.odoo_prod_promotion_operation import OdooProdPromotionCheckpoint
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.workflows.odoo_stable_target_replacement import (
    TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE,
)
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyResult,
)
from control_plane.service_deploy_drain import prepare as prepare_drain
from tests.test_client_release import _deployment
from tests.test_release_review import decision
import unittest
from unittest.mock import patch

from control_plane.client_release import read_client_release_run
from control_plane import client_release_odoo_continuation
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import BearerIdentityConfig, LaunchplaneAuthzPolicy
from tests.http_app_test_support import _RejectingVerifier
from tests.support.http import request
from control_plane.workflows.odoo_stable_target_replacement import OdooStableTargetRuntimeSnapshot
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.workflows.odoo_prod_backup_gate import (
    BACKUP_GATE_SOURCE,
    OdooProdBackupGateResult,
)
from control_plane.workflows.odoo_stable_operation_worker import (
    run_odoo_stable_operation_worker_once,
)
from control_plane.workflows.odoo_stable_target_replacement import (
    OdooStableTargetReplacementPlan,
    execute_odoo_stable_target_replacement_apply,
)
from control_plane.workflows.production_backup_gate_operation_worker import (
    run_production_backup_gate_operation_worker_once,
)
from tests.test_client_release import ClientReleaseFixture, PRODUCT, CONTEXT, _backup_binding
from tests.test_production_backup_provider import BackupHost, setup_memory_files
from tests.test_release_review import HEAD
from control_plane.release_review import build_release_review


class OdooAcceptedContinuationTests(ClientReleaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        setup_memory_files(self)
        baseline = self.store.read_deployment_record("deployment-prod-1")
        baseline.resolved_target = ResolvedTargetEvidence(
            target_id="fixture-target", target_type="compose", target_name="example-prod"
        )
        self.store.write_deployment_record(
            baseline.model_validate(baseline.model_dump(mode="json"))
        )
        self.switch("promote")
        self.accepted = self.accept()
        (self.backup_id,) = self.advance()
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
            capture = run_production_backup_gate_operation_worker_once(
                record_store=self.store,
                control_plane_root_path=self.root,
                lease_owner="fixture-backup",
            )
        self.assertTrue(capture.terminal_write_committed)
        (self.operation_id,) = self.advance()
        self.original = self.store.read_odoo_prod_promotion_operation_record(self.operation_id)
        self.plan = OdooStableTargetReplacementPlan(
            plan_status="blocked",
            product=PRODUCT,
            context=CONTEXT,
            instance="prod",
            strategy="recreate-in-place",
            target_record_found=True,
            target_id_record_found=True,
            inventory_found=True,
            expected_next_target_name="example-prod",
            expected_artifact_id="artifact-testing",
            blockers=("Provider-only fixture setting is not recorded.",),
            blocker_codes=("provider_keys_unrecorded",),
            blocker_keys={"provider_keys_unrecorded": ("EXAMPLE_SETTING",)},
        )

    def providers(self) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(
            patch(
                "control_plane.workflows.production_promotion_backup.resolve_production_backup_binding",
                side_effect=_backup_binding,
            )
        )
        self.addCleanup(stack.close)
        stack.enter_context(
            patch(
                "control_plane.release_review.current_release_review",
                side_effect=lambda **kwargs: build_release_review(
                    store=cast(Any, self.store),
                    profile=kwargs["profile"],
                    read=self.review_read,
                ),
            )
        )
        stack.enter_context(
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.resolve_odoo_prod_promotion_inputs",
                side_effect=lambda **kwargs: self._admission(**kwargs).inputs_result.model_copy(
                    update={
                        "backup_record_id": "fixture-logical-backup",
                        "source_git_ref": HEAD,
                    }
                ),
            )
        )

        def logical(**_kwargs: Any) -> OdooProdBackupGateResult:
            self.store.write_backup_gate_record(
                BackupGateRecord(
                    record_id="fixture-logical-backup",
                    context=CONTEXT,
                    instance="prod",
                    source=BACKUP_GATE_SOURCE,
                    status="pass",
                    created_at=self.original.updated_at,
                    evidence={"snapshot": "fixture-snapshot"},
                )
            )
            return OdooProdBackupGateResult(
                context=CONTEXT,
                instance="prod",
                backup_record_id="fixture-logical-backup",
                backup_status="pass",
            )

        stack.enter_context(
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.execute_odoo_prod_backup_gate",
                side_effect=logical,
            )
        )
        stack.enter_context(
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.build_odoo_stable_target_replacement_plan",
                side_effect=lambda **_kwargs: self.plan,
            )
        )
        stack.enter_context(
            patch.object(
                client_release_odoo_continuation,
                "build_odoo_stable_target_replacement_plan",
                side_effect=lambda **_kwargs: self.plan,
            )
        )
        stack.enter_context(
            patch(
                "control_plane.workflows.odoo_prod_promotion.execute_odoo_stable_target_replacement_apply",
                side_effect=partial(
                    execute_odoo_stable_target_replacement_apply,
                    dokploy_request=lambda *_args, **_kwargs: self.fail("unexpected provider call"),
                ),
            )
        )
        return stack

    def refuse_original(self) -> None:
        with self.providers():
            result = run_odoo_stable_operation_worker_once(
                record_store=self.store,
                control_plane_root_path=self.root,
                lease_owner="fixture-release",
            )
        self.assertTrue(result.terminal_write_committed)
        self.failed = self.store.read_odoo_prod_promotion_operation_record(self.operation_id)
        self.assertEqual(self.failed.status, "fail", self.failed.error_message)
        self.assertIsNotNone(self.failed.result)
        assert self.failed.result is not None
        self.promotion = self.store.read_promotion_record(self.failed.result.promotion_record_id)
        self.assertEqual(
            self.promotion.backup_gate.evidence.get("infrastructure_provider_effects_status"),
            "not_started",
        )
        self.assertFalse(self.promotion.deployment_record_id)

    def ready_plan(self) -> None:
        self.plan = self.plan.model_copy(
            update={
                "plan_status": "ready",
                "blockers": (),
                "blocker_codes": (),
                "blocker_keys": {},
                "expected_source_git_ref": HEAD,
                "current_target": OdooStableTargetRuntimeSnapshot(
                    target_id="fixture-target",
                    target_type="compose",
                    target_name="example-prod",
                    runtime_identity_present=True,
                    runtime_identity_deployment_record_id="deployment-prod-1",
                ),
            }
        )

    def app(self) -> FastAPI:
        return create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            bearer_identity_config=BearerIdentityConfig(
                terminal_agent_token="fictional-director-token",
                terminal_agent_subject="fictional-director",
                terminal_agent_token_label="fictional-director",
            ),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )

    async def continuation(
        self, body: dict[str, object], *, authenticated: bool = True
    ) -> Response:
        return await request(
            self.app(),
            "POST",
            f"/v1/products/{PRODUCT}/releases/{self.accepted.record_id}/backup-continuation",
            headers={"Authorization": "Bearer fictional-director-token"} if authenticated else {},
            payload=body,
        )

    async def reviewed_body(self) -> dict[str, object]:
        self.ready_plan()
        body: dict[str, object] = {"reason": "Fictional original admission refusal was repaired"}
        response = await self.continuation(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "ready")
        self.assertEqual(
            self.store.read_odoo_prod_promotion_operation_record(self.operation_id), self.failed
        )
        return {**body, "apply": True, "expected_digest": response.json()["review_digest"]}

    def deploy(self, **kwargs: Any) -> OdooStableTargetReplacementApplyResult:
        kwargs["provider_effect_checkpoint"](TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE)
        deployment = _deployment("fixture-continued-deployment", "artifact-testing")
        deployment.destination_health = HealthcheckEvidence(
            verified=True,
            status="pass",
            urls=("https://fixture.invalid/health",),
            timeout_seconds=30,
        )
        self.store.write_deployment_record(deployment)
        return OdooStableTargetReplacementApplyResult(
            product=PRODUCT,
            context=CONTEXT,
            instance="prod",
            strategy="recreate-in-place",
            artifact_id="artifact-testing",
            deployment_record_id=deployment.record_id,
            deploy_status="pass",
            post_deploy_status="pass",
            health_status="pass",
        )

    async def test_proved_original_pre_dispatch_refusal_can_continue_the_same_acceptance(
        self,
    ) -> None:
        self.refuse_original()
        run = read_client_release_run(
            store=self.store,
            profile=self.store.read_product_profile_record(PRODUCT),
            decision=self.accepted,
        )
        assert run is not None
        self.assertEqual(run.state, "stopped")
        self.assertEqual(self.advance(), ())
        with self.providers():
            body = await self.reviewed_body()
            denied = await self.continuation(body, authenticated=False)
            self.assertIn(denied.status_code, (401, 403))
            applied = await self.continuation(body)
            self.assertEqual(applied.status_code, 200, applied.text)
            self.assertEqual(applied.json()["status"], "queued")
            pending = self.store.read_odoo_prod_promotion_operation_record(self.operation_id)
            self.assertEqual(pending.status, "pending")
            self.assertEqual(pending.previous_attempt, self.failed)
            self.assertEqual(pending.previous_promotion, self.promotion)
            self.assertEqual(pending.request, self.original.request)
            self.assertEqual(pending.authorization, self.original.authorization)
            self.assertEqual(pending.idempotency_key, self.original.idempotency_key)
            self.assertEqual(
                self.store.read_promotion_record(self.promotion.record_id), self.promotion
            )
            with patch(
                "control_plane.workflows.odoo_prod_promotion.execute_odoo_stable_target_replacement_apply",
                side_effect=self.deploy,
            ) as dispatch:
                progressed = run_odoo_stable_operation_worker_once(
                    record_store=self.store,
                    control_plane_root_path=self.root,
                    lease_owner="fixture-continuation",
                )
                self.assertTrue(progressed.terminal_write_committed)
                self.assertEqual(self.advance(), ())
                again = run_odoo_stable_operation_worker_once(
                    record_store=self.store,
                    control_plane_root_path=self.root,
                    lease_owner="fixture-replay",
                )
                self.assertFalse(again.operation_id)
                self.assertEqual(dispatch.call_count, 1)
            completed = self.store.read_odoo_prod_promotion_operation_record(self.operation_id)
            self.assertEqual(completed.status, "pass", completed.error_message)
            self.assertEqual(completed.previous_attempt, self.failed)
            self.assertEqual(
                self.store.read_promotion_record(self.promotion.record_id), self.promotion
            )
            run = read_client_release_run(
                store=self.store,
                profile=self.store.read_product_profile_record(PRODUCT),
                decision=self.accepted,
            )
            assert run is not None
            self.assertEqual(run.state, "passed")
            replayed = await self.continuation(body)
            self.assertEqual(replayed.status_code, 200, replayed.text)
            self.assertEqual(replayed.json()["status"], "replayed")
            self.assertEqual(
                self.store.read_odoo_prod_promotion_operation_record(self.operation_id), completed
            )

    async def test_changed_current_release_or_review_inputs_refuse_without_requeue(self) -> None:
        self.refuse_original()
        profile = self.store.read_product_profile_record(PRODUCT)
        testing = self.store.read_release_tuple_record(context_name=CONTEXT, channel_name="testing")
        with self.providers():
            body = await self.reviewed_body()
            ready = self.plan
            ready_target = ready.current_target
            assert ready_target is not None
            foreign_target = ready_target.model_copy(update={"target_id": "foreign-target"})
            mutations: dict[str, Callable[[], object]] = {
                "hold": lambda: self.switch("held"),
                "client": lambda: self.store.write_product_profile_record(
                    profile.model_copy(
                        update={"owner": profile.owner.model_copy(update={"github_id": "9002"})}
                    )
                ),
                "candidate": lambda: self.store.write_release_tuple_record(
                    testing.model_copy(update={"artifact_id": "artifact-prod"})
                ),
                "production": lambda: self.set_prod("artifact-testing"),
                "target": lambda: setattr(
                    self,
                    "plan",
                    ready.model_copy(update={"current_target": foreign_target}),
                ),
                "readiness": lambda: setattr(
                    self, "plan", ready.model_copy(update={"plan_status": "blocked"})
                ),
                "key": lambda: self.store.write_odoo_prod_promotion_operation_record(
                    self.failed.model_copy(update={"idempotency_key": "unrelated-key"})
                ),
                "request": lambda: self.store.write_odoo_prod_promotion_operation_record(
                    self.failed.model_copy(update={"request_fingerprint": "0" * 64})
                ),
            }
            for name, mutate in mutations.items():
                with self.subTest(change=name):
                    mutate()
                    changed = self.store.read_odoo_prod_promotion_operation_record(
                        self.operation_id
                    )
                    refused = await self.continuation({"reason": body["reason"]})
                    self.assertEqual(refused.status_code, 409, refused.text)
                    expected = {
                        "hold": "release_acceptance_no_longer_current",
                        "client": "release_acceptance_no_longer_current",
                        "candidate": "backup_continuation_held",
                        "production": "backup_continuation_held",
                        "target": "odoo_current_target_or_plan_changed",
                        "readiness": "odoo_current_target_or_plan_changed",
                        "key": "odoo_original_effects_not_proven_absent",
                        "request": "odoo_original_effects_not_proven_absent",
                    }
                    self.assertEqual(refused.json()["error"]["code"], expected[name])
                    self.assertEqual(
                        self.store.read_odoo_prod_promotion_operation_record(self.operation_id),
                        changed,
                    )
                    self.store.write_product_profile_record(profile)
                    self.store.write_release_tuple_record(testing)
                    self.set_prod("artifact-prod")
                    self.store.write_odoo_prod_promotion_operation_record(self.failed)
                    self.plan = ready

    async def test_missing_unknown_partial_and_wrong_family_receipts_refuse(self) -> None:
        self.refuse_original()
        with self.providers():
            body = await self.reviewed_body()
            for value in (None, "unknown_after_failure"):
                promotion = self.promotion.model_copy(deep=True)
                if value is None:
                    promotion.backup_gate.evidence.pop("infrastructure_provider_effects_status")
                else:
                    promotion.backup_gate.evidence["infrastructure_provider_effects_status"] = value
                self.store.write_promotion_record(promotion)
                refused = await self.continuation({"reason": body["reason"]})
                self.assertEqual(refused.status_code, 409, refused.text)
                self.assertEqual(refused.json()["error"]["code"], "odoo_original_receipt_mismatch")
            self.store.write_promotion_record(self.promotion)
            assert self.failed.result is not None
            for index, changed in enumerate(
                (
                    self.failed.model_copy(
                        update={
                            "status": "reconciliation_required",
                            "finished_at": "",
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                            "result": None,
                        }
                    ),
                    self.failed.model_copy(
                        update={
                            "checkpoints": (
                                *self.failed.checkpoints,
                                OdooProdPromotionCheckpoint(
                                    phase="promotion_started",
                                    recorded_at=self.failed.finished_at,
                                    evidence={"production_write_started": "true"},
                                ),
                            )
                        }
                    ),
                    self.failed.model_copy(
                        update={
                            "result": self.failed.result.model_copy(
                                update={
                                    "promotion_record_id": "replacement-operation-from-wrong-family"
                                }
                            )
                        }
                    ),
                )
            ):
                self.store.write_odoo_prod_promotion_operation_record(changed)
                refused = await self.continuation({"reason": body["reason"]})
                self.assertEqual(refused.status_code, 409, refused.text)
                self.assertEqual(
                    refused.json()["error"]["code"],
                    "backup_continuation_held"
                    if index == 2
                    else "odoo_original_effects_not_proven_absent",
                )
                self.assertEqual(
                    self.store.read_odoo_prod_promotion_operation_record(self.operation_id), changed
                )
            self.store.write_odoo_prod_promotion_operation_record(self.failed)

    async def test_newer_acceptance_never_continues_the_old_decision(self) -> None:
        self.refuse_original()
        with self.providers():
            body = await self.reviewed_body()
            newer = decision(self.store, date="2026-09-24T01:00:00Z").model_copy(
                update={"release_start": "promote"}
            )
            self.store.write_release_review_decision_record(newer)
            refused = await self.continuation(body)
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(
                self.store.read_odoo_prod_promotion_operation_record(self.operation_id), self.failed
            )

    async def test_interrupted_apply_response_replays_the_original_after_restart(self) -> None:
        self.refuse_original()
        with self.providers():
            body = await self.reviewed_body()
            commit = self.store.continue_pre_effect_odoo_promotion_operation

            def lost_response(**kwargs: Any) -> bool:
                self.assertTrue(commit(**kwargs))
                raise ConnectionResetError("Fictional response was lost after commit")

            with patch.object(
                self.store,
                "continue_pre_effect_odoo_promotion_operation",
                side_effect=lost_response,
            ):
                with self.assertRaises(ConnectionResetError):
                    await self.continuation(body)
            url = self.store.database_url
            self.store.close()
            self.store = PostgresRecordStore(database_url=url)
            self.addCleanup(self.store.close)
            replayed = await self.continuation(body)
            self.assertEqual(replayed.status_code, 200, replayed.text)
            self.assertEqual(replayed.json()["status"], "replayed")
            pending = self.store.read_odoo_prod_promotion_operation_record(self.operation_id)
            self.assertEqual(pending.status, "pending")
            self.assertEqual(pending.previous_attempt, self.failed)

    async def test_apply_rechecks_changed_original_inside_its_atomic_boundary(self) -> None:
        self.refuse_original()
        with self.providers():
            body = await self.reviewed_body()
            commit = self.store.continue_pre_effect_odoo_promotion_operation
            changed = self.failed.model_copy(update={"error_message": "A newer original outcome"})

            def change_then_commit(**kwargs: Any) -> bool:
                self.store.write_odoo_prod_promotion_operation_record(changed)
                return commit(**kwargs)

            with patch.object(
                self.store,
                "continue_pre_effect_odoo_promotion_operation",
                side_effect=change_then_commit,
            ):
                refused = await self.continuation(body)
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(
                self.store.read_odoo_prod_promotion_operation_record(self.operation_id), changed
            )

    async def test_active_original_lane_and_deploy_drain_keep_the_original_stopped(self) -> None:
        self.refuse_original()
        with self.providers():
            body = await self.reviewed_body()
            other = self.original.model_copy(
                update={
                    "operation_id": "fictional-overlapping-operation",
                    "created_at": datetime.now(UTC).isoformat(),
                }
            )
            self.store.write_odoo_prod_promotion_operation_record(other)
            refused = await self.continuation(body)
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(
                self.store.read_odoo_prod_promotion_operation_record(self.operation_id), self.failed
            )
            self.store.write_odoo_prod_promotion_operation_record(
                other.model_copy(
                    update={
                        "status": "fail",
                        "phase": "failed",
                        "finished_at": datetime.now(UTC).isoformat(),
                        "error_message": "Fictional operation ended",
                    }
                )
            )
            prepare_drain(
                self.store,
                request_fingerprint="fixture-drain",
                target_type="compose",
                target_id="fixture-control",
                image_reference="fixture.invalid/control@sha256:" + "a" * 64,
                deployment_marker="fixture-drain",
            )
            refused = await self.continuation(body)
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(
                self.store.read_odoo_prod_promotion_operation_record(self.operation_id), self.failed
            )

    async def test_expired_changed_or_consumed_backup_never_resumes(self) -> None:
        from tests.test_generic_web_client_release import expired_backup_clock

        self.refuse_original()
        backup = self.store.read_backup_gate_record(
            self.original.request.infrastructure_backup_record_id
        )
        logical = self.store.read_backup_gate_record("fixture-logical-backup")
        with self.providers():
            body = await self.reviewed_body()
            with expired_backup_clock(self.store, self.backup_id):
                refused = await self.continuation({"reason": body["reason"]})
                self.assertEqual(refused.status_code, 409, refused.text)
            for record in (
                logical.model_copy(update={"status": "fail"}),
                backup.model_copy(
                    update={"evidence": {**backup.evidence, "snapshot_name": "different-snapshot"}}
                ),
            ):
                self.store.write_backup_gate_record(record)
                refused = await self.continuation({"reason": body["reason"]})
                self.assertEqual(refused.status_code, 409, refused.text)
                self.store.write_backup_gate_record(logical)
                self.store.write_backup_gate_record(backup)
            other = self.promotion.model_copy(
                deep=True, update={"record_id": "fictional-other-consumer"}
            )
            other.backup_gate.evidence.pop("infrastructure_provider_effects_status")
            self.store.write_promotion_record(other)
            refused = await self.continuation({"reason": body["reason"]})
            self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(
            self.store.read_odoo_prod_promotion_operation_record(self.operation_id), self.failed
        )

    async def test_target_changed_after_apply_refuses_before_the_workers_first_write(self) -> None:
        self.refuse_original()
        before = self.store.list_deployment_records()
        with self.providers():
            body = await self.reviewed_body()
            applied = await self.continuation(body)
            self.assertEqual(applied.status_code, 200, applied.text)
            target = self.plan.current_target
            assert target is not None
            self.plan = self.plan.model_copy(
                update={
                    "current_target": target.model_copy(
                        update={"target_id": "different-provider-target"}
                    )
                }
            )
            with patch(
                "control_plane.workflows.odoo_prod_promotion.execute_odoo_stable_target_replacement_apply",
                side_effect=self.deploy,
            ):
                result = run_odoo_stable_operation_worker_once(
                    record_store=self.store,
                    control_plane_root_path=self.root,
                    lease_owner="fixture-current-target",
                )
        self.assertTrue(result.terminal_write_committed)
        current = self.store.read_odoo_prod_promotion_operation_record(self.operation_id)
        self.assertEqual(current.status, "fail")
        self.assertEqual(current.previous_attempt, self.failed)
        assert current.result is not None
        refused_promotion = self.store.read_promotion_record(current.result.promotion_record_id)
        self.assertEqual(
            refused_promotion.backup_gate.evidence.get("infrastructure_provider_effects_status"),
            "not_started",
        )
        self.ready_plan()
        with self.providers():
            reviewed = await self.continuation({"reason": body["reason"]})
            self.assertEqual(reviewed.status_code, 200, reviewed.text)
            self.assertEqual(
                self.store.read_odoo_prod_promotion_operation_record(self.operation_id), current
            )
        self.assertEqual(self.store.list_deployment_records(), before)
        self.assertEqual(self.store.read_promotion_record(self.promotion.record_id), self.promotion)

    async def test_lost_response_after_a_continued_write_retains_unknown_effects_without_redispatch(
        self,
    ) -> None:
        self.refuse_original()
        effects: list[str] = []
        with self.providers():
            body = await self.reviewed_body()
            applied = await self.continuation(body)
            self.assertEqual(applied.status_code, 200, applied.text)

            def uncertain(**kwargs: Any) -> OdooStableTargetReplacementApplyResult:
                kwargs["provider_effect_checkpoint"](TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE)
                effects.append("fixture-dispatch")
                raise TimeoutError("Fictional provider response lost")

            with patch(
                "control_plane.workflows.odoo_prod_promotion.execute_odoo_stable_target_replacement_apply",
                side_effect=uncertain,
            ):
                with self.assertLogs(level="ERROR"):
                    result = run_odoo_stable_operation_worker_once(
                        record_store=self.store,
                        control_plane_root_path=self.root,
                        lease_owner="fixture-uncertain",
                    )
                self.assertTrue(result.terminal_write_committed)
                unknown = self.store.read_odoo_prod_promotion_operation_record(self.operation_id)
                self.assertEqual(unknown.status, "reconciliation_required")
                replayed = await self.continuation(body)
                self.assertEqual(replayed.status_code, 200, replayed.text)
                self.assertEqual(replayed.json()["status"], "replayed")
                self.assertEqual(self.advance(), ())
                run_odoo_stable_operation_worker_once(
                    record_store=self.store,
                    control_plane_root_path=self.root,
                    lease_owner="fixture-no-retry",
                )
        self.assertEqual(effects, ["fixture-dispatch"])
        self.assertEqual(
            self.store.read_odoo_prod_promotion_operation_record(self.operation_id), unknown
        )

    async def test_plan_drift_invalidates_an_old_review_but_allows_fresh_qualification(
        self,
    ) -> None:
        self.refuse_original()
        with self.providers():
            body = await self.reviewed_body()
            self.plan = self.plan.model_copy(
                update={"delivered_runtime_keys": ("NEW_RECORDED_SETTING",)}
            )
            refused = await self.continuation(body)
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(refused.json()["error"]["code"], "backup_continuation_review_changed")
            reviewed = await self.continuation({"reason": body["reason"]})
            self.assertEqual(reviewed.status_code, 200, reviewed.text)
            self.assertEqual(
                self.store.read_odoo_prod_promotion_operation_record(self.operation_id), self.failed
            )

    def test_direct_failed_promotion_still_reports_attention_and_consumes_its_capture(self) -> None:
        from control_plane.production_delivery_monitor import _work_events
        from control_plane.storage.delivery_evidence import DeliveryEvidenceWork
        from control_plane.workflows.production_promotion_backup import (
            require_production_promotion_backup,
            ODOO_PROMOTION_BACKUP_ACTION,
        )
        import click

        self.refuse_original()
        with self.store.delivery_evidence_index() as index:
            events = _work_events(
                self.store,
                self.store.read_product_profile_record(PRODUCT),
                index,
                DeliveryEvidenceWork(
                    key="fixture-direct-promotion",
                    kind="promotion",
                    source_id=self.promotion.record_id,
                    version="fixture-version",
                    baseline=False,
                    processed={},
                ),
                datetime.now(UTC).isoformat(),
            )
        self.assertEqual([event[0] for event in events], [self.promotion.record_id])
        with self.providers():
            with self.assertRaises(click.ClickException):
                require_production_promotion_backup(
                    record_store=self.store,
                    product=PRODUCT,
                    context=CONTEXT,
                    instance="prod",
                    promotion_action=ODOO_PROMOTION_BACKUP_ACTION,
                    backup_record_id=self.original.request.infrastructure_backup_record_id,
                )
        self.assertFalse(
            client_release_odoo_continuation.continuation_left_capture_unused(
                self.store, self.promotion
            )
        )
