"""Reconciliation retains an existing decision's admitted step and original key."""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
import unittest
from unittest.mock import patch
import click
from control_plane import client_release as releases
from control_plane.contracts.production_recipient import ProductionRecipientRecord
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.release_review import (
    ReleaseChecklist,
    ReleaseReviewDecisionRecord,
    ReleaseVersion,
)
from control_plane.production_recipient_release_recovery import (
    RecipientReleaseRecoveryRequest,
    recover_release_step,
)
from control_plane.generic_web_promotion_http import GENERIC_WEB_PROD_PROMOTION_ROUTE
from tests import test_production_recipient_cutover as fixtures
from tests.support.profiles import _generic_site_profile_payload
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductOwnerProfile,
)
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    PostDeployUpdateEvidence,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.production_recipient import custody
from control_plane.production_recipient_cutover import RecipientCutoverAdapter
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.generic_web_source_qualification import GenericWebPromotionSourceRefused
from control_plane.provider_operations import (
    build_provider_operation_key,
    ProviderObservation,
    ProviderMutationOutcome,
)
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.storage.postgres import LaunchplaneVeriReelProdBackupGateOperationRow
from control_plane.integration_allowances import canonical_sha256


class RecipientReleaseRecoveryTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        self.fixture = fixtures.RecipientCutoverTests()
        self.fixture.database_url = self.database_url
        payload = _generic_site_profile_payload()
        payload.update(
            production_use="live",
            release_on_acceptance="promote_with_rollback_drill",
            owner={"github_id": "123", "github_login": "fixture"},
        )
        with patch(
            "tests.test_production_recipient._generic_site_profile_payload", return_value=payload
        ):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        checklist = ReleaseChecklist(
            product=f.record.product,
            repository=f.fixture.profile.repository,
            owner_github_id="123",
            testing_url="",
            production=ReleaseVersion(artifact_id="old-artifact", source_commit="b" * 40),
            candidate=ReleaseVersion(
                artifact_id=f.record.image_reference, source_commit=f.record.request.source_commit
            ),
            items=(),
        )
        self.decision = ReleaseReviewDecisionRecord(
            record_id="accepted-recipient",
            product=f.record.product,
            checklist_digest="d" * 64,
            checklist=checklist,
            decision="accepted",
            actor_github_id="123",
            actor_github_login="fixture",
            decided_at="2026-10-16T08:00:00Z",
            release_issue_url="https://github.com/example/site/issues/1",
            release_start="promote_with_rollback_drill",
        )
        self.request = RecipientReleaseRecoveryRequest(
            context=f.record.context,
            preparation_plan_sha256=f.record.plan_sha256,
            decision_record_id=self.decision.record_id,
            kind="promote",
            reason="Reconcile original outcome",
        )
        f.store.write_release_review_decision_record(self.decision)
        self.key = releases._step_key(self.decision, releases.ClientReleaseStep("promote", 1))

    def continue_recipient(self, *, source_commit: str = "") -> ProductionRecipientRecord:
        from control_plane.production_recipient_continuation import (
            RecipientContinuationRequest,
            continuation_plan,
            supersede_recipient,
        )

        f = self.fixture
        current = custody(f.store, f.record.product, f.record.context)
        assert current is not None
        request = RecipientContinuationRequest(
            context=current.context,
            original_key=current.original_key,
            expected_custody_sha256=canonical_sha256(current.model_dump(mode="json")),
            replacement=current.request.model_copy(
                update={
                    "mode": "dry-run",
                    "reviewed_plan_sha256": "",
                    "source_commit": source_commit or current.request.source_commit,
                }
            ),
            reason="Continue the retained preparation before its accepted release starts.",
        )
        draft = continuation_plan(
            f.fixture.root, f.store, current.product, request, current.actor, current.scope
        )[2]
        with patch(
            "control_plane.production_recipient_continuation.run_recipient_program",
            return_value={"recipient_execution_fenced": current.plan_sha256},
        ):
            supersede_recipient(
                f.fixture.root,
                f.store,
                current.product,
                request.model_copy(
                    update={"mode": "apply", "reviewed_plan_sha256": draft["plan_sha256"]}
                ),
                current.actor,
                current.scope,
                lambda _phase: None,
            )
        replacement = custody(f.store, current.product, current.context)
        assert replacement is not None
        self.assertFalse(replacement.activation)
        self.assertNotEqual(replacement.plan_sha256, current.plan_sha256)
        return replacement

    def test_unarmed_recipient_never_uses_the_ordinary_production_adapter(self) -> None:
        f = self.fixture
        profile, _testing = self.qualified_testing_source()
        self.continue_recipient()
        with (
            patch("control_plane.client_release._require_current_release"),
            patch(
                "control_plane.client_release.GenericWebProdPromotionProviderMutationAdapter",
                side_effect=AssertionError("Unarmed recipient reached the ordinary adapter."),
            ) as ordinary,
        ):
            with self.assertRaises(releases.ClientReleaseNotReady):
                releases._run_generic_web_promotion(
                    f.store,
                    f.fixture.root,
                    profile,
                    self.decision,
                    releases.ClientReleaseStep("promote", 1),
                    f.old_backup,
                )
            ordinary.assert_not_called()
        self.assertEqual(f.actions, [])

    def seed_retained_deployment(self) -> None:
        f = self.fixture
        inventory = f.store.read_environment_inventory(
            context_name=f.record.context, instance_name="prod"
        )
        retained = f.record.retained_provider
        identity = inventory.runtime_identity
        assert identity is not None
        artifact = ArtifactIdentityReference(
            artifact_id=f.record.image_reference.split("@", 1)[0]
            + "@"
            + identity.image_reference.split("@", 1)[1]
        )
        identity = identity.model_copy(update={"artifact_id": artifact.artifact_id})
        inventory = inventory.model_copy(
            update={"artifact_identity": artifact, "runtime_identity": identity}
        )
        f.store.write_environment_inventory(inventory)
        f.store.write_deployment_record(
            DeploymentRecord(
                record_id=inventory.deployment_record_id,
                context=inventory.context,
                instance=inventory.instance,
                artifact_identity=artifact,
                source_git_ref=inventory.source_git_ref,
                runtime_identity=identity,
                deploy=inventory.deploy,
                destination_health=inventory.destination_health,
                resolved_target=ResolvedTargetEvidence(
                    target_type="compose",
                    target_id=retained.target_id,
                    target_name=retained.display_name,
                ),
                deployed_target=retained.to_deployed_target_reference(),
            )
        )
        self.decision = self.decision.model_copy(
            update={
                "checklist": self.decision.checklist.model_copy(
                    update={
                        "production": self.decision.checklist.production.model_copy(
                            update={"artifact_id": artifact.artifact_id}
                        ),
                    }
                )
            }
        )
        f.store.write_release_review_decision_record(self.decision)

    def capture_accepted_backup(self, profile: LaunchplaneProductProfileRecord) -> None:
        f = self.fixture
        operation_id = releases._queue_backup(
            f.store,
            profile,
            self.decision,
            releases.ClientReleaseStep("backup", 1),
            f.record.context,
            self.decision.decided_at,
        )
        operation = f.store.read_verireel_prod_backup_gate_operation_record(operation_id)
        with patch(
            "tests.test_production_recipient_cutover.enqueue_production_backup_gate",
            return_value=operation,
        ):
            f.capture(f.publication_case.policy, operation.backup_record_id)
        f.old_backup = operation.backup_record_id

    def test_native_release_reports_the_same_unarmed_custody_refusal_as_worker(self) -> None:
        f = self.fixture
        profile, _testing = self.qualified_testing_source()
        self.seed_retained_deployment()
        self.capture_accepted_backup(profile)
        before = releases.read_client_release_run(
            store=f.store, profile=profile, decision=self.decision
        )
        assert before is not None
        self.assertFalse(before.blocked_reason)
        self.continue_recipient()
        run = releases.read_client_release_run(
            store=f.store, profile=profile, decision=self.decision
        )
        assert run is not None
        self.assertEqual(run.state, "waiting")
        self.assertTrue(run.blocked_reason)
        with self.assertRaises(releases.ClientReleaseNotReady) as held:
            releases._run_generic_web_promotion(
                f.store,
                f.fixture.root,
                profile,
                self.decision,
                releases.ClientReleaseStep("promote", 1),
                f.old_backup,
            )
        self.assertEqual(str(held.exception), run.blocked_reason)
        self.assertEqual(f.actions, [])
        self.assertEqual(f.store.list_held_provider_target_reservations(), ())

    def test_unarmed_different_candidate_remains_on_recipient_path(self) -> None:
        f = self.fixture
        profile, _testing = self.qualified_testing_source()
        self.seed_retained_deployment()
        self.capture_accepted_backup(profile)
        self.continue_recipient(source_commit="c" * 40)
        run = releases.read_client_release_run(
            store=f.store, profile=profile, decision=self.decision
        )
        assert run is not None
        self.assertTrue(run.blocked_reason)
        with self.assertRaises(releases.ClientReleaseNotReady) as held:
            releases._run_generic_web_promotion(
                f.store,
                f.fixture.root,
                profile,
                self.decision,
                releases.ClientReleaseStep("promote", 1),
                f.old_backup,
            )
        self.assertEqual(str(held.exception), run.blocked_reason)
        self.assertEqual(f.actions, [])
        self.assertEqual(f.store.list_held_provider_target_reservations(), ())

    def test_published_other_version_and_retired_custody_allow_ordinary_selection(self) -> None:
        f = self.fixture
        profile, _testing = self.qualified_testing_source()
        for phase in ("published", "retired"):
            with (
                self.subTest(phase=phase),
                patch(
                    "control_plane.production_recipient.custody",
                    return_value=f.record.model_copy(
                        update={
                            "phase": phase,
                            "request": f.record.request.model_copy(
                                update={"source_commit": "c" * 40}
                            ),
                        }
                    ),
                ),
                patch(
                    "control_plane.client_release.GenericWebProdPromotionProviderMutationAdapter",
                    side_effect=AssertionError("Ordinary update selected."),
                ) as ordinary,
            ):
                with self.assertRaises(AssertionError):
                    releases._run_generic_web_promotion(
                        f.store,
                        f.fixture.root,
                        profile,
                        self.decision,
                        releases.ClientReleaseStep("promote", 1),
                        f.old_backup,
                    )
                ordinary.assert_called_once()
        self.assertEqual(f.actions, [])

    def test_superseded_preflight_retry_releases_without_touching_replacement(self) -> None:
        f = self.fixture
        with patch.object(
            f.adapter, "verify_upstreams", side_effect=ValueError("Upstream preflight unavailable")
        ):
            with self.assertRaises(ValueError):
                f.run_release()
        previous = custody(f.store, f.record.product, f.record.context)
        assert previous is not None
        self.assertEqual(previous.phase, "preflight_refused")
        self.assertTrue(previous.execution)
        replacement = self.continue_recipient()
        actions = list(f.actions)
        with self.assertRaises(ValueError):
            f.run_release()
        self.assertEqual(custody(f.store, replacement.product, replacement.context), replacement)
        self.assertEqual(f.actions, actions)
        self.assertEqual(f.store.list_held_provider_target_reservations(), ())
        self.assertIsNone(
            f.store.read_idempotency_record(
                scope="client-release",
                route_path="/fixture/recipient-release",
                idempotency_key="accepted-release",
            )
        )

    def ordinary_adapter(self) -> tuple[Any, Any]:
        from control_plane.generic_web_promotion_provider_adapter import (
            GenericWebProdPromotionProviderMutationAdapter,
        )
        from control_plane.generic_web_promotion_http import GenericWebProdPromotionEnvelope
        from tests.test_generic_web_promotion import _ProductionProvider

        f = self.fixture
        profile, _testing = self.qualified_testing_source()
        self.seed_retained_deployment()
        profile = profile.model_copy(
            update={
                "image": profile.image.model_copy(
                    update={
                        "repository": f.record.image_reference.split("@", 1)[0],
                    }
                )
            }
        )
        f.store.write_product_profile_record(profile)
        provider = _ProductionProvider()
        resolve = provider.resolve_deploy_target
        retained = f.record.retained_provider

        def old_target(**kwargs: Any) -> Any:
            target = resolve(**kwargs)
            return target.model_copy(
                update={
                    "ship_request": target.ship_request.model_copy(
                        update={
                            "target_name": retained.display_name,
                            "target_type": "compose",
                            "target_category": "compose",
                            "provider_target_type": "compose",
                        }
                    ),
                    "resolved_target": ResolvedTargetEvidence(
                        target_type="compose",
                        target_id=retained.target_id,
                        target_name=retained.display_name,
                    ),
                    "deployed_target": retained.to_deployed_target_reference(),
                }
            )

        self.enterContext(
            patch(
                "control_plane.workflows.generic_web_promotion.require_release_approval",
                return_value=self.decision,
            )
        )
        self.enterContext(patch.object(provider, "resolve_deploy_target", side_effect=old_target))
        from tests.support.lane_builds import LaneBuildGitHub

        inventory = f.store.read_environment_inventory(
            context_name=f.record.context, instance_name="prod"
        )
        assert inventory.runtime_identity is not None
        history = LaneBuildGitHub(
            profile.repository,
            profile.image.repository,
            {
                inventory.runtime_identity.source_git_ref: inventory.runtime_identity.artifact_id.split(
                    "@", 1
                )[1],
                self.decision.checklist.candidate.source_commit: self.decision.checklist.candidate.artifact_id.split(
                    "@", 1
                )[1],
            },
        )
        history.run_started_at[2] = (
            datetime.fromisoformat(inventory.updated_at.replace("Z", "+00:00"))
            + timedelta(seconds=1)
        ).isoformat()
        self.enterContext(
            patch(
                "control_plane.product_reconcile.resolve_build_provenance_transport",
                return_value=history,
            )
        )
        self.enterContext(
            patch("control_plane.workflows.generic_web_promotion._wait_for_healthcheck")
        )
        adapter = GenericWebProdPromotionProviderMutationAdapter(
            control_plane_root=f.fixture.root,
            record_store=f.store,
            promotion_request=GenericWebProdPromotionEnvelope(
                product=profile.product,
                promotion=releases.client_release_promotion_request(
                    profile, self.decision, f.old_backup
                ),
            ),
            profile=profile,
            lane=next(lane for lane in profile.lanes if lane.instance == "prod"),
            trace_id="recipient-race",
            validate_before_effect=lambda _target: None,
            deploy_provider=provider,
            settle_pre_effect_failure=True,
        )
        return adapter, provider

    def ordinary_custody_after_effect(self, *, fail_candidate: bool) -> None:
        from control_plane.production_recipient import authority_snapshot
        from control_plane.storage.postgres import LaunchplaneProductionRecipientRow

        f = self.fixture
        adapter, provider = self.ordinary_adapter()
        saved = custody(f.store, f.record.product, f.record.context)
        assert saved is not None
        with f.store._session_factory() as session, session.begin():
            row = session.get(LaunchplaneProductionRecipientRow, (saved.product, saved.context))
            assert row is not None
            session.delete(row)
        candidate = self.decision.checklist.candidate.artifact_id
        provider.healthy_artifact = (
            self.decision.checklist.production.artifact_id if fail_candidate else candidate
        )
        provider.fail_artifact = candidate if fail_candidate else ""
        checkpoint = f.store.checkpoint_mutation_provider_effect
        inserted = False

        def create_after_admission(**kwargs: Any) -> Any:
            nonlocal inserted
            admitted = checkpoint(**kwargs)
            if not inserted:
                f.store.write_production_recipient(
                    expected=None,
                    replacement=saved.model_copy(
                        update={
                            "authority_snapshot": authority_snapshot(
                                f.store, saved.product, saved.context
                            ),
                        }
                    ).model_dump(mode="json"),
                )
                inserted = True
            return admitted

        with patch.object(
            f.store, "checkpoint_mutation_provider_effect", side_effect=create_after_admission
        ):
            result = self.run_ordinary(adapter)
        self.assertTrue(inserted)
        self.assertEqual(result.status, "completed")
        self.assertEqual(
            provider.deployed_artifacts,
            [candidate, self.decision.checklist.production.artifact_id]
            if fail_candidate
            else [candidate],
        )
        self.assertEqual(
            result.response_payload["result"]["promotion_status"],
            "fail" if fail_candidate else "pass",
        )
        if fail_candidate:
            self.assertEqual(result.response_payload["result"]["rollback_status"], "pass")
        self.assertEqual(f.actions, [])
        self.assertEqual(f.store.list_held_provider_target_reservations(), ())

    def test_admitted_ordinary_promotion_finishes_after_custody_creation(self) -> None:
        self.ordinary_custody_after_effect(fail_candidate=False)

    def test_ordinary_automatic_rollback_survives_custody_creation(self) -> None:
        self.ordinary_custody_after_effect(fail_candidate=True)

    def run_ordinary(self, adapter: Any) -> Any:
        from control_plane.provider_operations import run_durable_provider_operation

        f = self.fixture
        return run_durable_provider_operation(
            store=f.store,
            scope=releases.CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
            idempotency_key=self.key,
            request_fingerprint="f" * 64,
            lease_owner="ordinary-worker",
            response_trace_id="recipient-race",
            adapter=adapter,
        )

    def test_ordinary_promotion_rechecks_new_custody_at_its_atomic_first_effect(self) -> None:
        from control_plane.production_recipient import authority_snapshot
        from control_plane.storage.postgres import LaunchplaneProductionRecipientRow
        from control_plane.generic_web_promotion_provider_adapter import PromotionTargetChanged

        f = self.fixture
        adapter, provider = self.ordinary_adapter()
        saved = custody(f.store, f.record.product, f.record.context)
        assert saved is not None
        with f.store._session_factory() as session, session.begin():
            row = session.get(LaunchplaneProductionRecipientRow, (saved.product, saved.context))
            assert row is not None
            session.delete(row)
        checkpoint = f.store.checkpoint_mutation_provider_effect
        inserted = False

        def create_custody(**kwargs: Any) -> Any:
            nonlocal inserted
            if not inserted:
                f.store.write_production_recipient(
                    expected=None,
                    replacement=saved.model_copy(
                        update={
                            "authority_snapshot": authority_snapshot(
                                f.store, saved.product, saved.context
                            ),
                        }
                    ).model_dump(mode="json"),
                )
                inserted = True
            return checkpoint(**kwargs)

        with patch.object(
            f.store, "checkpoint_mutation_provider_effect", side_effect=create_custody
        ):
            with self.assertRaises(PromotionTargetChanged):
                self.run_ordinary(adapter)
        self.assertTrue(inserted)
        self.assertEqual(provider.deployed_artifacts, [])
        self.assertEqual(f.actions, [])
        self.assertIsNone(
            f.store.read_idempotency_record(
                scope=releases.CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
                route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
                idempotency_key=self.key,
            )
        )

    def recover(self, request: Any = None, key: str | None = None) -> Any:
        f = self.fixture
        return recover_release_step(
            f.fixture.root,
            f.store,
            f.record.product,
            request or self.request,
            self.key if key is None else key,
        )

    def reserve(self) -> None:
        self.fixture.store.reserve_mutation(
            scope=releases.CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
            idempotency_key=self.key,
            request_fingerprint="e" * 64,
            lease_owner="original-release",
            reconciliation_key=self.fixture.adapter.reconciliation_key(),
        )

    def test_no_recovery_without_original_step_or_with_an_alternate_key(self) -> None:
        with self.assertRaises(ValueError):
            self.recover()
        self.reserve()
        with self.assertRaises(ValueError):
            self.recover(key="replacement")
        with self.assertRaises(ValueError):
            self.recover(self.request.model_copy(update={"kind": "rollback", "attempt": 2}))

    def interrupted_recipient(
        self, *, fenced: bool = False, mark_held: bool = True
    ) -> tuple[LaunchplaneProductProfileRecord, LaunchplaneIdempotencyRecord]:
        f = self.fixture
        profile, _testing = self.qualified_testing_source()
        backup = next(
            operation
            for operation in f.store.list_verireel_prod_backup_gate_operation_records()
            if operation.backup_record_id == f.old_backup
        )
        admitted = backup.model_copy(
            update={
                "operation_id": releases.client_release_step_operation_id(
                    profile=profile,
                    decision=self.decision,
                    step=releases.ClientReleaseStep("backup", 1),
                )
            }
        )
        with f.store._session_factory() as session, session.begin():
            row = session.get(LaunchplaneVeriReelProdBackupGateOperationRow, backup.operation_id)
            assert row is not None
            row.operation_id = admitted.operation_id
            row.payload = admitted.model_dump(mode="json")
        request = releases.client_release_promotion_request(profile, self.decision, f.old_backup)
        reserved = f.store.reserve_mutation(
            scope=releases.CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
            idempotency_key=self.key,
            request_fingerprint=hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
            lease_owner="interrupted-release",
            reconciliation_key=f.adapter.reconciliation_key(),
            provider_target_key=f.adapter.target_key(),
            provider_target_origin=f.adapter.provider_target_origin(),
        )
        assert reserved.record is not None
        if not mark_held:
            return profile, reserved.record
        held = f.store.mark_mutation_reconcile_required(
            reservation=reserved.record, reconciliation_key=reserved.record.reconciliation_key
        )
        assert held.record is not None
        if not fenced:
            return profile, held.record
        operation_key = build_provider_operation_key(
            scope=held.record.scope,
            route_path=held.record.route_path,
            idempotency_key=held.record.idempotency_key,
            request_fingerprint=held.record.request_fingerprint,
            reconciliation_key=held.record.reconciliation_key,
        )
        f.store.write_production_recipient(
            expected=f.record.model_dump(mode="json"),
            replacement=f.record.model_copy(
                update={
                    "phase": "preflight_refused",
                    "execution": {canonical_sha256(operation_key) + ":old_pause_fenced": "proved"},
                }
            ).model_dump(mode="json"),
        )
        return profile, held.record

    def advance_interrupted(
        self, *, paused: bool = False, backoff: releases.ClientReleaseBackoff | None = None
    ) -> tuple[str, ...]:
        f = self.fixture
        from control_plane.service_deploy_drain import prepare, read_status

        if paused and not read_status(f.store).get("admission_paused"):
            prepare(
                f.store,
                request_fingerprint="fixture-service-replacement",
                target_type="compose",
                target_id="fixture-control-plane",
                image_reference=f.record.image_reference,
                deployment_marker="fixture-replacement",
            )
        return releases.advance_client_releases(
            store=f.store, control_plane_root=f.fixture.root, release_backoff=backoff
        )

    def test_release_worker_adopts_fenced_failure_while_draining_without_writer_effect(
        self,
    ) -> None:
        f = self.fixture
        _profile, held = self.interrupted_recipient(fenced=True)
        self.advance_interrupted(paused=True)
        self.advance_interrupted(paused=True)
        settled = f.store.read_idempotency_record(
            scope=held.scope, route_path=held.route_path, idempotency_key=self.key
        )
        assert settled is not None
        self.assertEqual(settled.state, "completed")
        self.assertEqual(settled.record_id, held.record_id)
        self.assertEqual(settled.response_payload["result"]["promotion_status"], "fail")
        self.assertEqual(settled.response_payload["result"]["writer_effect"], "none")
        self.assertEqual(f.actions, [])
        self.assertEqual(f.deploys, 0)
        self.assertTrue(f.old_running)
        from control_plane.service_deploy_drain import read_status

        drain = read_status(f.store)
        self.assertTrue(drain["admission_paused"])
        blockers = drain["running_operation_ids"]
        assert isinstance(blockers, (tuple, list))
        self.assertNotIn(held.record_id, blockers)

    def test_unknown_recipient_is_observed_on_bounded_cadence_without_retry(self) -> None:
        f = self.fixture
        self.interrupted_recipient()
        backoff = releases.ClientReleaseBackoff()
        with (
            patch.object(
                RecipientCutoverAdapter,
                "observe",
                autospec=True,
                side_effect=RecipientCutoverAdapter.observe,
            ) as observe,
            patch("control_plane.client_release.monotonic", return_value=10.0) as clock,
        ):
            self.advance_interrupted(backoff=backoff)
            self.advance_interrupted(backoff=backoff)
            self.assertEqual(observe.call_count, 1)
            clock.return_value += releases.CLIENT_RELEASE_RECONCILIATION_SECONDS + 1
            self.advance_interrupted(backoff=backoff)
            self.assertEqual(observe.call_count, 2)
        self.assertEqual(self.recover().state, "reconcile_required")
        self.assertEqual(f.actions, [])
        self.assertEqual(f.deploys, 0)

    def test_release_hold_prevents_original_reconciliation(self) -> None:
        f = self.fixture
        profile, held = self.interrupted_recipient(fenced=True)
        f.store.write_product_profile_record(
            profile.model_copy(update={"release_on_acceptance": "held"})
        )
        with patch.object(RecipientCutoverAdapter, "observe") as observe:
            self.assertEqual(self.advance_interrupted(), ())
        observe.assert_not_called()
        self.assertEqual(self.recover().state, held.state)

    def test_active_original_lease_is_not_observed_or_retried(self) -> None:
        self.interrupted_recipient(mark_held=False)
        with patch.object(RecipientCutoverAdapter, "observe") as observe:
            self.assertEqual(self.advance_interrupted(), ())
        observe.assert_not_called()
        self.assertEqual(self.recover().state, "running")

    def test_proved_absence_retries_original_only_after_drain_admits_effects(self) -> None:
        from datetime import timedelta
        from control_plane.contracts.idempotency_record import parse_launchplane_mutation_timestamp

        f = self.fixture
        _profile, held = self.interrupted_recipient()
        future = (
            parse_launchplane_mutation_timestamp(
                held.lease_expires_at, field_name="lease_expires_at"
            )
            + timedelta(seconds=1)
        ).isoformat()
        with (
            patch.object(
                RecipientCutoverAdapter,
                "observe",
                return_value=ProviderObservation("absent", retry_safe=True),
            ),
            patch.object(
                RecipientCutoverAdapter,
                "apply",
                return_value=ProviderMutationOutcome(
                    response_status_code=202,
                    response_payload={"result": {"promotion_status": "fail"}},
                    provider_effect_performed=False,
                ),
            ) as apply,
            patch.object(f.store, "_database_mutation_timestamp", return_value=future) as clock,
        ):
            self.advance_interrupted(paused=True)
            apply.assert_not_called()
            clock.return_value = (
                parse_launchplane_mutation_timestamp(future, field_name="now")
                + timedelta(minutes=3)
            ).isoformat()
            self.advance_interrupted()
        apply.assert_called_once()
        settled = f.store.read_idempotency_record(
            scope=held.scope, route_path=held.route_path, idempotency_key=self.key
        )
        assert settled is not None
        self.assertEqual(settled.record_id, held.record_id)
        self.assertEqual(settled.idempotency_key, held.idempotency_key)
        self.assertEqual(settled.attempt, held.attempt + 1)
        self.assertEqual(settled.state, "completed")

    def test_original_retry_still_refuses_unqualified_testing_before_writer_effect(self) -> None:
        from control_plane.contracts.idempotency_record import parse_launchplane_mutation_timestamp

        f = self.fixture
        profile, held = self.interrupted_recipient()
        context = next(lane.context for lane in profile.lanes if lane.instance == "testing")
        inventory = f.store.read_environment_inventory(
            context_name=context, instance_name="testing"
        )
        testing = f.store.read_deployment_record(inventory.deployment_record_id)
        f.store.write_deployment_record(
            testing.model_copy(
                update={
                    "post_deploy_update": PostDeployUpdateEvidence(attempted=True, status="fail")
                }
            )
        )
        future = (
            parse_launchplane_mutation_timestamp(
                held.lease_expires_at, field_name="lease_expires_at"
            )
            + timedelta(seconds=1)
        ).isoformat()
        with (
            patch(
                "control_plane.client_release._require_qualified_generic_web_release_source",
                wraps=releases._require_qualified_generic_web_release_source,
            ) as source,
            patch.object(
                RecipientCutoverAdapter,
                "observe",
                return_value=ProviderObservation("absent", retry_safe=True),
            ),
            patch.object(f.store, "_database_mutation_timestamp", return_value=future),
        ):
            self.assertEqual(self.advance_interrupted(), ())
        source.assert_called()
        self.assertEqual(f.actions, [])
        self.assertEqual(f.deploys, 0)

    def test_original_retry_still_requires_fresh_backup_before_writer_effect(self) -> None:
        from control_plane.contracts.idempotency_record import parse_launchplane_mutation_timestamp
        from control_plane.workflows.production_promotion_backup import (
            require_production_promotion_backup,
        )

        f = self.fixture
        _profile, held = self.interrupted_recipient()
        backup = next(
            operation
            for operation in f.store.list_verireel_prod_backup_gate_operation_records()
            if operation.backup_record_id == f.old_backup
        )
        assert backup.binding is not None
        expired = datetime.now(UTC) + timedelta(
            seconds=backup.binding.policy.fast_snapshot.max_evidence_age_seconds + 1
        )
        future = (
            parse_launchplane_mutation_timestamp(
                held.lease_expires_at, field_name="lease_expires_at"
            )
            + timedelta(seconds=1)
        ).isoformat()
        with (
            patch.object(
                RecipientCutoverAdapter,
                "observe",
                return_value=ProviderObservation("absent", retry_safe=True),
            ),
            patch.object(f.store, "_database_mutation_timestamp", return_value=future),
            patch("control_plane.client_release._require_current_release"),
            patch(
                "control_plane.workflows.production_promotion_backup.datetime", wraps=datetime
            ) as clock,
            patch(
                "control_plane.production_recipient_cutover.require_production_promotion_backup",
                wraps=require_production_promotion_backup,
            ) as gate,
        ):
            clock.now.return_value = expired
            self.advance_interrupted()
        gate.assert_called()
        self.assertEqual(f.actions, [])
        self.assertEqual(f.deploys, 0)

    def test_saved_review_resumes_only_the_existing_operation(self) -> None:
        self.reserve()
        draft = self.recover(key="")
        self.assertEqual(draft.original_key, self.key)
        with patch(
            "control_plane.production_recipient_release_recovery.releases._run_generic_web_promotion"
        ) as run:
            with self.assertRaises(ValueError):
                self.recover(
                    self.request.model_copy(
                        update={"mode": "apply", "reviewed_plan_sha256": "f" * 64}
                    )
                )
            run.assert_not_called()
            result = self.recover(
                self.request.model_copy(
                    update={"mode": "apply", "reviewed_plan_sha256": draft.plan_sha256}
                )
            )
            self.assertEqual(result.operation_id, draft.operation_id)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[4], releases.ClientReleaseStep("promote", 1))

    def test_temporary_hotfix_grant_cannot_start_the_fresh_production_cutover(self) -> None:
        from control_plane.contracts.product_profile_record import ProductOwnerProfile

        f = self.fixture
        profile = f.fixture.profile.model_copy(
            update={"owner": ProductOwnerProfile(github_id="123", github_login="fixture")}
        )
        hotfix = self.decision.model_copy(update={"decision": "temporary_hotfix"})
        with (
            patch("control_plane.client_release._require_current_release"),
        ):
            with self.assertRaises(click.ClickException):
                releases._run_generic_web_promotion(
                    f.store,
                    f.fixture.root,
                    profile,
                    hotfix,
                    releases.ClientReleaseStep("promote", 1),
                    f.old_backup,
                )
        self.assertEqual(f.actions, [])
        self.assertEqual(f.deploys, 0)

    def qualified_testing_source(self) -> tuple[LaunchplaneProductProfileRecord, DeploymentRecord]:
        f = self.fixture
        profile = f.fixture.profile.model_copy(
            update={
                "owner": ProductOwnerProfile(github_id="123", github_login="fixture"),
            }
        )
        testing_context = next(lane.context for lane in profile.lanes if lane.instance == "testing")
        testing = DeploymentRecord(
            record_id="qualified-recipient-testing",
            context=testing_context,
            instance="testing",
            artifact_identity=ArtifactIdentityReference(
                artifact_id=self.decision.checklist.candidate.artifact_id
            ),
            source_git_ref=self.decision.checklist.candidate.source_commit,
            runtime_identity=RuntimeIdentity(
                product=profile.product,
                context=testing_context,
                instance="testing",
                deployment_record_id="qualified-recipient-testing",
                artifact_id=self.decision.checklist.candidate.artifact_id,
                source_git_ref=self.decision.checklist.candidate.source_commit,
            ),
            deploy=DeploymentEvidence(
                target_name="testing",
                target_type="application",
                deploy_mode="fixture",
                status="pass",
            ),
            resolved_target=ResolvedTargetEvidence(
                target_type="application", target_id="testing", target_name="testing"
            ),
        )
        assert testing.deployed_target is not None
        f.store.write_provider_target_record(
            ProviderTargetRecord(
                context=testing_context,
                instance="testing",
                provider_id=testing.deployed_target.provider_id,
                target_category=testing.deployed_target.target_category,
                provider_target_type=testing.deployed_target.provider_target_type,
                target_id=testing.deployed_target.target_id,
                display_name=testing.deployed_target.display_name,
                updated_at=profile.updated_at,
            )
        )
        f.store.write_deployment_record(testing)
        f.store.write_environment_inventory(
            build_environment_inventory(deployment_record=testing, updated_at=profile.updated_at)
        )
        return profile, testing

    def advance_testing(self, testing: DeploymentRecord) -> None:
        assert testing.runtime_identity is not None and testing.artifact_identity is not None
        record_id = testing.record_id + "-next"
        artifact_id = self.fixture.fixture.profile.image.repository + "@sha256:" + "e" * 64
        later = testing.model_copy(
            update={
                "record_id": record_id,
                "source_git_ref": "e" * 40,
                "artifact_identity": testing.artifact_identity.model_copy(
                    update={"artifact_id": artifact_id}
                ),
                "runtime_identity": testing.runtime_identity.model_copy(
                    update={
                        "deployment_record_id": record_id,
                        "artifact_id": artifact_id,
                        "source_git_ref": "e" * 40,
                    }
                ),
            }
        )
        self.fixture.store.write_deployment_record(later)
        self.fixture.store.write_environment_inventory(
            build_environment_inventory(
                deployment_record=later, updated_at=self.fixture.fixture.profile.updated_at
            )
        )

    def test_resumed_recipient_rechecks_testing_before_forward_bootstrap(self) -> None:
        f = self.fixture
        profile, testing = self.qualified_testing_source()

        def run() -> None:
            releases._run_generic_web_promotion(
                f.store,
                f.fixture.root,
                profile,
                self.decision,
                releases.ClientReleaseStep("promote", 1),
                f.old_backup,
            )

        with (
            patch("control_plane.client_release._require_current_release"),
            patch.object(
                RecipientCutoverAdapter,
                "verify_upstreams",
                side_effect=ValueError("interrupt before provider dispatch"),
            ) as upstreams,
        ):
            with self.assertRaises(ValueError):
                run()
            upstreams.assert_called_once()
            saved = custody(f.store, f.record.product, f.record.context)
            assert saved is not None
            self.assertTrue(saved.execution)
            f.store.write_deployment_record(
                testing.model_copy(
                    update={
                        "post_deploy_update": PostDeployUpdateEvidence(
                            attempted=True, status="fail"
                        )
                    }
                )
            )
            upstreams.reset_mock()
            with patch(
                "control_plane.client_release._require_current_release",
                side_effect=AssertionError(
                    "Resumption must not compare the changed production baseline"
                ),
            ):
                with self.assertRaises(GenericWebPromotionSourceRefused):
                    run()
            upstreams.assert_not_called()
            f.store.write_deployment_record(testing)
            with self.assertRaises(ValueError):
                run()
            upstreams.assert_called_once()
        self.assertEqual(f.actions, [])
        self.assertEqual(f.deploys, 0)

    def test_fresh_source_refusal_preserves_type_and_original_step_retry(self) -> None:
        f = self.fixture
        profile, testing = self.qualified_testing_source()
        f.store.write_deployment_record(
            testing.model_copy(
                update={
                    "post_deploy_update": PostDeployUpdateEvidence(attempted=True, status="fail")
                }
            )
        )
        with (
            patch("control_plane.client_release._require_current_release"),
            patch.object(RecipientCutoverAdapter, "verify_runtime"),
        ):
            for _ in range(2):
                with self.assertRaises(GenericWebPromotionSourceRefused):
                    releases._run_generic_web_promotion(
                        f.store,
                        f.fixture.root,
                        profile,
                        self.decision,
                        releases.ClientReleaseStep("promote", 1),
                        f.old_backup,
                    )
                self.assertIsNone(
                    f.store.read_idempotency_record(
                        scope=releases.CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
                        route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
                        idempotency_key=self.key,
                    )
                )
                self.assertEqual(f.actions, [])
                self.assertEqual(f.deploys, 0)
            f.store.write_deployment_record(testing)
            self.assertTrue(
                releases._run_generic_web_promotion(
                    f.store,
                    f.fixture.root,
                    profile,
                    self.decision,
                    releases.ClientReleaseStep("promote", 1),
                    f.old_backup,
                )
            )
        saved = custody(f.store, f.record.product, f.record.context)
        assert saved is not None
        self.assertEqual(saved.phase, "published")

    def test_source_change_after_resource_effect_does_not_abort_admitted_recipient(self) -> None:
        f = self.fixture
        profile, testing = self.qualified_testing_source()

        def upstreams(_record: Any, checkpoint: Any, _lease: Any) -> None:
            checkpoint("resource_schedule_upsert")
            f.store.write_deployment_record(
                testing.model_copy(
                    update={
                        "post_deploy_update": PostDeployUpdateEvidence(
                            attempted=True, status="fail"
                        )
                    }
                )
            )
            checkpoint("resource_schedule_trigger")

        with (
            patch("control_plane.client_release._require_current_release"),
            patch.object(RecipientCutoverAdapter, "verify_runtime"),
            patch.object(RecipientCutoverAdapter, "verify_upstreams", side_effect=upstreams),
        ):
            self.assertTrue(
                releases._run_generic_web_promotion(
                    f.store,
                    f.fixture.root,
                    profile,
                    self.decision,
                    releases.ClientReleaseStep("promote", 1),
                    f.old_backup,
                )
            )
        reservation = f.store.read_idempotency_record(
            scope=releases.CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
            idempotency_key=self.key,
        )
        assert reservation is not None
        self.assertEqual(reservation.state, "completed")
        saved = custody(f.store, f.record.product, f.record.context)
        assert saved is not None
        self.assertEqual(saved.phase, "published")
        self.assertTrue(f.new_running)
        self.assertFalse(f.old_running)

    def test_started_recipient_finishes_accepted_artifact_after_testing_advances(self) -> None:
        self.started_recipient_with_new_testing(resume=False)

    def test_resumed_started_recipient_keeps_original_source_admission(self) -> None:
        self.started_recipient_with_new_testing(resume=True)

    def test_inverse_original_key_recovery_uses_real_authority_without_testing(self) -> None:
        from control_plane.generic_web_rollback_http import GENERIC_WEB_ROLLBACK_ROUTE

        self.started_recipient_with_new_testing(resume=False)
        f = self.fixture
        profile = f.store.read_product_profile_record(f.record.product)
        inventory = f.store.read_environment_inventory(
            context_name=next(lane.context for lane in profile.lanes if lane.instance == "testing"),
            instance_name="testing",
        )
        testing = f.store.read_deployment_record(inventory.deployment_record_id)
        f.store.write_deployment_record(
            testing.model_copy(
                update={
                    "post_deploy_update": PostDeployUpdateEvidence(attempted=True, status="fail")
                }
            )
        )
        step = releases.ClientReleaseStep("rollback", 1)
        key = releases._step_key(self.decision, step)
        grant = releases.client_release_grant(
            decision=self.decision,
            action="generic_web_rollback.execute",
            context=f.record.context,
            authorized_at=self.decision.decided_at,
        )
        self.assertFalse(releases.client_release_grant_allows(f.store, grant))

        def lose_pause_reply(*args: Any, **kwargs: Any) -> None:
            f.writer(*args, **kwargs)
            if not kwargs.get("old") and kwargs.get("action") == "pause":
                raise TimeoutError("replacement writer pause reply lost")

        with (
            patch(
                "control_plane.production_recipient_inverse.writer_action",
                side_effect=lose_pause_reply,
            ),
            patch(
                "control_plane.production_recipient_inverse._verify_health_evidence_with_identity",
                side_effect=f.health,
            ),
        ):
            self.assertTrue(
                releases._queue_recipient_inverse(
                    f.store,
                    f.fixture.root,
                    profile,
                    self.decision,
                    step,
                    f.record.context,
                    self.decision.decided_at,
                )
            )
        original = f.store.read_idempotency_record(
            scope=releases.CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_ROLLBACK_ROUTE,
            idempotency_key=key,
        )
        assert original is not None
        self.assertEqual(original.state, "reconcile_required")
        request = self.request.model_copy(update={"kind": "rollback"})
        plan = self.recover(request, key="")
        with (
            patch("control_plane.production_recipient_inverse.writer_action", side_effect=f.writer),
            patch(
                "control_plane.production_recipient_inverse._verify_health_evidence_with_identity",
                side_effect=f.health,
            ),
        ):
            result = self.recover(
                request.model_copy(
                    update={
                        "mode": "apply",
                        "reviewed_plan_sha256": plan.plan_sha256,
                    }
                ),
                key=key,
            )
        self.assertEqual(result.operation_id, original.record_id)
        self.assertEqual(result.original_key, key)
        self.assertEqual(result.state, "completed")
        current = custody(f.store, f.record.product, f.record.context)
        assert current is not None
        self.assertEqual(current.phase, "rolled_back")
        self.assertTrue(f.old_running)
        self.assertFalse(f.new_running)
        self.assertEqual(
            f.store.read_environment_inventory(context_name=f.record.context, instance_name="prod"),
            f.publication_case.inventory,
        )

    def test_native_inverse_is_owed_without_a_passing_historical_deployment(self) -> None:
        f = self.fixture
        profile, _testing = self.qualified_testing_source()
        operation_id = releases._queue_backup(
            f.store,
            profile,
            self.decision,
            releases.ClientReleaseStep("backup", 1),
            f.record.context,
            self.decision.decided_at,
        )
        backup_operation = f.store.read_verireel_prod_backup_gate_operation_record(operation_id)
        # Reuse the fixture's real backup worker for the already queued Client
        # operation rather than enqueueing its separate fixture-capture key.
        with patch(
            "tests.test_production_recipient_cutover.enqueue_production_backup_gate",
            return_value=backup_operation,
        ):
            f.capture(f.publication_case.policy, backup_operation.backup_record_id)
        f.old_backup = backup_operation.backup_record_id
        self.started_recipient_with_new_testing(resume=False)
        baseline = f.publication_case.inventory
        current = f.store.read_environment_inventory(
            context_name=f.record.context, instance_name="prod"
        )
        published = f.store.read_deployment_record(current.deployment_record_id)
        retained = published.model_copy(
            update={
                "record_id": baseline.deployment_record_id,
                "artifact_identity": baseline.artifact_identity,
                "runtime_identity": baseline.runtime_identity,
                "source_git_ref": baseline.source_git_ref,
                "deploy": published.deploy.model_copy(update={"status": "fail"}),
            }
        )
        f.store.write_deployment_record(retained)
        self.assertEqual(f.store.list_held_provider_target_reservations(), ())
        with self.assertRaises(releases.ClientReleaseNotReady):
            releases._require_standing_release_available(f.store, profile.product)
        with self.assertRaises(releases.ClientReleaseNotReady):
            releases._generic_web_drill_target(f.store, profile, self.decision)
        run = releases.read_client_release_run(
            store=f.store, profile=profile, decision=self.decision
        )
        assert run is not None
        self.assertFalse(run.blocked_reason)
        owned = f.store.read_provider_target_record(
            context_name=f.record.context, instance_name="prod"
        )
        f.store.write_provider_target_record(
            owned.model_copy(update={"display_name": "operator changed the owned target"})
        )
        blocked = releases.read_client_release_run(
            store=f.store, profile=profile, decision=self.decision
        )
        assert blocked is not None
        self.assertTrue(blocked.blocked_reason)
        actions = list(f.actions)
        with patch("control_plane.client_release._generic_web_drill_target") as fallback:
            with self.assertRaises(releases.ClientReleaseNotReady):
                releases._queue_rollback(
                    f.store,
                    f.fixture.root,
                    profile,
                    self.decision,
                    releases.ClientReleaseStep("rollback", 1),
                    f.record.context,
                    self.decision.decided_at,
                )
            fallback.assert_not_called()
        self.assertEqual(f.actions, actions)
        # A local owned-row refusal is distinct from a transient provider wait.
        releases._require_standing_release_available(f.store, profile.product)
        f.store.write_provider_target_record(owned)
        with (
            patch("control_plane.production_recipient_inverse.writer_action", side_effect=f.writer),
            patch(
                "control_plane.production_recipient_inverse._verify_health_evidence_with_identity",
                side_effect=f.health,
            ),
        ):
            self.assertTrue(
                releases._queue_recipient_inverse(
                    f.store,
                    f.fixture.root,
                    profile,
                    self.decision,
                    releases.ClientReleaseStep("rollback", 1),
                    f.record.context,
                    self.decision.decided_at,
                )
            )
        self.assertTrue(f.old_running)
        self.assertFalse(f.new_running)

    def test_admitted_inverse_restores_baseline_after_hold(self) -> None:
        self.inverse_revocation_after_pause(new_decision=False, abort=False)

    def test_admitted_inverse_restores_baseline_after_new_human_decision(self) -> None:
        self.inverse_revocation_after_pause(new_decision=True, abort=False)

    def test_inverse_abort_restores_replacement_after_hold(self) -> None:
        self.inverse_revocation_after_pause(new_decision=False, abort=True)

    def inverse_revocation_after_pause(self, *, new_decision: bool, abort: bool) -> None:
        from datetime import UTC, datetime, timedelta
        from control_plane.generic_web_rollback_http import GENERIC_WEB_ROLLBACK_ROUTE
        from control_plane.production_recipient_publication import RecipientOwnedAuthorityChanged

        self.started_recipient_with_new_testing(resume=False)
        f = self.fixture
        profile = f.store.read_product_profile_record(f.record.product)

        def revoke_after_pause(*args: Any, **kwargs: Any) -> None:
            f.writer(*args, **kwargs)
            if not kwargs.get("old") and kwargs.get("action") == "pause":
                if new_decision:
                    f.store.write_release_review_decision_record(
                        self.decision.model_copy(
                            update={
                                "record_id": self.decision.record_id + "-new-human-decision",
                                "decided_at": (
                                    datetime.now(UTC) + timedelta(seconds=1)
                                ).isoformat(),
                            }
                        )
                    )
                else:
                    f.store.write_product_profile_record(
                        profile.model_copy(update={"release_on_acceptance": "held"})
                    )

        step = releases.ClientReleaseStep("rollback", 1)
        key = releases._step_key(self.decision, step)
        with (
            patch(
                "control_plane.production_recipient_inverse.writer_action",
                side_effect=revoke_after_pause,
            ),
            patch(
                "control_plane.production_recipient_inverse._verify_health_evidence_with_identity",
                side_effect=f.health,
            ),
        ):
            if abort:
                with patch(
                    "control_plane.production_recipient_inverse.rollback_publication",
                    side_effect=RecipientOwnedAuthorityChanged("changed owned row"),
                ):
                    self.assertTrue(
                        releases._queue_recipient_inverse(
                            f.store,
                            f.fixture.root,
                            profile,
                            self.decision,
                            step,
                            f.record.context,
                            self.decision.decided_at,
                        )
                    )
            else:
                self.assertTrue(
                    releases._queue_recipient_inverse(
                        f.store,
                        f.fixture.root,
                        profile,
                        self.decision,
                        step,
                        f.record.context,
                        self.decision.decided_at,
                    )
                )
        reservation = f.store.read_idempotency_record(
            scope=releases.CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_ROLLBACK_ROUTE,
            idempotency_key=key,
        )
        assert reservation is not None
        self.assertEqual(reservation.state, "completed")
        saved = custody(f.store, f.record.product, f.record.context)
        assert saved is not None
        self.assertEqual(saved.phase, "published" if abort else "rolled_back")
        self.assertEqual(f.old_running, not abort)
        self.assertEqual(f.new_running, abort)

    def test_inverse_hold_before_pause_starts_no_writer_effect(self) -> None:
        from control_plane.generic_web_rollback_http import GENERIC_WEB_ROLLBACK_ROUTE

        self.started_recipient_with_new_testing(resume=False)
        f = self.fixture
        profile = f.store.read_product_profile_record(f.record.product)
        f.store.write_product_profile_record(
            profile.model_copy(update={"release_on_acceptance": "held"})
        )
        actions = list(f.actions)
        step = releases.ClientReleaseStep("rollback", 1)
        authority = releases._generic_web_release_authority_allows
        refusals: list[bool] = []

        def check_authority(*args: Any, **kwargs: Any) -> bool:
            allowed = authority(*args, **kwargs)
            refusals.append(not allowed)
            return allowed

        with patch(
            "control_plane.client_release._generic_web_release_authority_allows",
            side_effect=check_authority,
        ):
            with self.assertRaises(ValueError):
                releases._queue_recipient_inverse(
                    f.store,
                    f.fixture.root,
                    profile,
                    self.decision,
                    step,
                    f.record.context,
                    self.decision.decided_at,
                )
        self.assertIn(True, refusals)
        self.assertEqual(f.actions, actions)
        self.assertFalse(f.old_running)
        self.assertTrue(f.new_running)
        self.assertIsNone(
            f.store.read_idempotency_record(
                scope=releases.CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
                route_path=GENERIC_WEB_ROLLBACK_ROUTE,
                idempotency_key=releases._step_key(self.decision, step),
            )
        )

    def started_recipient_with_new_testing(self, *, resume: bool) -> None:
        f = self.fixture
        profile, testing = self.qualified_testing_source()

        def writer(*args: Any, **kwargs: Any) -> None:
            f.writer(*args, **kwargs)
            if kwargs.get("old") and kwargs.get("action") == "pause":
                self.advance_testing(testing)

        def deploy(**kwargs: Any) -> None:
            f.deploy(**kwargs)
            if resume:
                raise TimeoutError("original provider reply lost")

        def run() -> None:
            releases._run_generic_web_promotion(
                f.store,
                f.fixture.root,
                profile,
                self.decision,
                releases.ClientReleaseStep("promote", 1),
                f.old_backup,
            )

        with (
            patch("control_plane.client_release._require_current_release"),
            patch.object(RecipientCutoverAdapter, "verify_runtime"),
            patch("control_plane.production_recipient_cutover.writer_action", side_effect=writer),
            patch(
                "control_plane.production_recipient_cutover.execute_dokploy_artifact_deploy",
                side_effect=deploy,
            ),
            patch.object(
                RecipientCutoverAdapter,
                "terminal_deployment",
                return_value=None,
            ) as terminal,
        ):
            run()
            if resume:
                saved = custody(f.store, f.record.product, f.record.context)
                assert saved is not None
                self.assertNotEqual(saved.phase, "published")
                terminal.return_value = {
                    "title": saved.execution["runtime_identity"]["deployment_record_id"],
                    "status": "done",
                }
                with patch(
                    "control_plane.client_release._require_current_release",
                    side_effect=AssertionError("Started cutover cannot chase latest testing"),
                ):
                    run()
        saved = custody(f.store, f.record.product, f.record.context)
        assert saved is not None
        self.assertEqual(saved.phase, "published")
        self.assertEqual(f.deploys, 1)
        self.assertTrue(f.new_running)
        self.assertFalse(f.old_running)
        production = f.store.read_environment_inventory(
            context_name=f.record.context, instance_name="prod"
        )
        assert production.artifact_identity is not None
        self.assertEqual(
            production.artifact_identity.artifact_id, self.decision.checklist.candidate.artifact_id
        )
        self.assertNotIn(("resume", True), f.actions)
