"""Run the service-owned release with real storage, backup gates and fake providers."""

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import Mock, patch

import click

from control_plane.client_release import (
    advance_client_releases,
    StandingReleaseReviewBackoff,
    read_client_release_run,
    release_start_for_acceptance,
)
from control_plane.contracts.product_profile_record import ProductOwnerProfile
from control_plane.contracts.production_backup_authority import ProductionBackupPolicyRecord
from control_plane.contracts.release_review import (
    ReleaseReviewDecisionRecord,
    ReleaseReviewStatus,
    ReleaseDecision,
)
from control_plane.contracts.production_backup_gate import ProductionBackupGateRequest
from control_plane.workflows.runtime_identity_health import HealthcheckPass
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.workflows.generic_web_deploy_provider import GenericWebResolvedDeployTarget
from control_plane.client_release import CLIENT_RELEASE_IDEMPOTENCY_SCOPE
from control_plane.generic_web_promotion_http import GENERIC_WEB_PROD_PROMOTION_ROUTE
from datetime import timedelta
from control_plane.product_owner_setting import ProductOwnerIdentity, updated_product_owner_profile
from control_plane.release_review import build_release_review
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.production_promotion_backup import GENERIC_WEB_PROMOTION_BACKUP_ACTION
from control_plane.workflows.verireel_prod_backup_gate_operation_worker import (
    run_verireel_prod_backup_gate_operation_worker_once,
)
from tests.test_generic_web_promotion import (
    _ProductionProvider,
    _store_with_production,
    _PREVIOUS_ARTIFACT,
)
from tests.test_production_backup_provider import BackupHost, _binding, setup_memory_files
from tests.test_release_review import BASE, HEAD, github_read


class GenericWebClientReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{self.root / 'state.db'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        source = _store_with_production()
        self.profile = source.profile.model_copy(
            update={
                "production_use": "live",
                "release_on_acceptance": "promote",
                "owner": ProductOwnerProfile(github_id="9001", github_login="example-client"),
            }
        )
        self.store.write_product_profile_record(self.profile)
        for inventory in source.inventories.values():
            sha = BASE if inventory.instance == "prod" else HEAD
            deployment = next(iter(source.deployments.values()))
            identity = deployment.runtime_identity
            assert identity is not None and inventory.artifact_identity is not None
            identity = identity.model_copy(
                update={
                    "instance": inventory.instance,
                    "artifact_id": inventory.artifact_identity.artifact_id,
                    "source_git_ref": sha,
                    "deployment_record_id": inventory.deployment_record_id,
                }
            )
            if inventory.instance == "testing":
                candidate_artifact = self.profile.image.repository + "@sha256:" + "b" * 64
                identity = identity.model_copy(
                    update={"artifact_id": candidate_artifact, "image_reference": ""}
                )
                inventory = inventory.model_copy(
                    update={
                        "artifact_identity": inventory.artifact_identity.model_copy(
                            update={"artifact_id": candidate_artifact}
                        )
                    }
                )
            self.store.write_environment_inventory(
                inventory.model_copy(
                    update={
                        "source_git_ref": sha,
                        "runtime_identity": identity,
                    }
                )
            )
            if inventory.instance == "prod":
                self.store.write_deployment_record(
                    deployment.model_copy(
                        update={
                            "source_git_ref": BASE,
                            "runtime_identity": identity,
                        }
                    )
                )
        original = _binding()
        policy = original.policy.model_dump()
        policy.update(
            product=self.profile.product,
            context=self.context,
            promotion_action=GENERIC_WEB_PROMOTION_BACKUP_ACTION,
            record_id="",
            policy_id="",
            policy_digest="",
        )
        self.store.write_production_backup_target_record(original.source_target)
        self.store.write_production_backup_target_record(original.destination_target)
        self.store.write_production_backup_policy_record(
            ProductionBackupPolicyRecord.model_validate(policy)
        )
        setup_memory_files(self)
        self.provider = _ProductionProvider()
        self.target = ProviderTargetRecord(
            context=self.context,
            instance="prod",
            provider_id="dokploy",
            target_category="application",
            target_id="app-123",
            provider_target_type="application",
            display_name="syo-prod-app",
            updated_at=datetime.now(UTC).isoformat(),
            source_label="test",
        )
        self.store.write_provider_target_record(self.target)
        resolver = self.provider.resolve_deploy_target

        def resolve(**kwargs: Any) -> GenericWebResolvedDeployTarget:
            target = resolver(**kwargs)
            return target.model_copy(
                update={"deployed_target": self.target.to_deployed_target_reference()}
            )

        self.enterContext(patch.object(self.provider, "resolve_deploy_target", side_effect=resolve))
        self.fail_health = False
        self.raw_read = github_read
        transport = Mock()
        transport.get_json.return_value = {
            "status": "ahead",
            "base_commit": {"sha": BASE},
            "merge_base_commit": {"sha": BASE},
        }
        self.enterContext(
            patch(
                "control_plane.product_reconcile.resolve_build_provenance_transport",
                return_value=transport,
            )
        )
        self.publish = self.enterContext(
            patch(
                "control_plane.client_release.publish_release_decision",
                return_value="https://github.com/example/site/issues/99",
            )
        )
        self.enterContext(
            patch("control_plane.client_release.current_release_review", side_effect=self.review)
        )
        self.enterContext(
            patch("control_plane.release_review.current_release_review", side_effect=self.review)
        )
        self.enterContext(
            patch(
                "control_plane.generic_web_promotion_provider_adapter.default_generic_web_deploy_provider",
                return_value=self.provider,
            )
        )
        self.enterContext(
            patch("control_plane.workflows.generic_web_promotion._wait_for_healthcheck")
        )
        self.enterContext(
            patch(
                "control_plane.workflows.generic_web_promotion.wait_for_runtime_identity_healthcheck_with_retry",
                side_effect=self.healthcheck,
            )
        )

    @property
    def context(self) -> str:
        return self.profile.lanes[-1].context

    def review(self, **kwargs: object) -> ReleaseReviewStatus:
        def read(path: str) -> object:
            payload = self.raw_read(path)
            if "/compare/" in path:
                return payload
            payload = cast(list[dict[str, object]], payload)
            return [
                {**item, "base": {"repo": {"full_name": self.profile.repository}}}
                for item in payload
            ]

        return build_release_review(
            store=self.store,
            profile=self.store.read_product_profile_record(self.profile.product),
            read=read,
        )

    def healthcheck(self, **kwargs: object) -> HealthcheckPass:
        identity = self.provider.running
        assert identity is not None
        if self.fail_health and identity.artifact_id != _PREVIOUS_ARTIFACT:
            raise click.ClickException("Candidate returned 503")
        return HealthcheckPass(payload={"runtime_identity": identity.model_dump(mode="json")})

    def switch(self, mode: str) -> None:
        profile = self.store.read_product_profile_record(self.profile.product)
        self.store.write_product_profile_record(
            profile.model_copy(update={"release_on_acceptance": mode})
        )

    def accept(self, outcome: ReleaseDecision = "accepted") -> ReleaseReviewDecisionRecord:
        review = self.review()
        assert review.checklist is not None
        record = ReleaseReviewDecisionRecord(
            record_id=f"decision-{outcome}",
            product=self.profile.product,
            checklist=review.checklist,
            checklist_digest=review.checklist_digest,
            decision=outcome,
            reason="Client decision",
            actor_github_id=self.profile.owner.github_id,
            actor_github_login=self.profile.owner.github_login,
            decided_at=datetime.now(UTC).isoformat(),
            release_issue_url="https://github.com/example/site/issues/99",
            release_start=release_start_for_acceptance(
                store=self.store,
                profile=self.store.read_product_profile_record(self.profile.product),
            )
            if outcome == "accepted"
            else "",
        )
        self.store.write_release_review_decision_record(record)
        return record

    def advance(self) -> tuple[str, ...]:
        return advance_client_releases(store=self.store, control_plane_root=self.root)

    def capture(self) -> None:
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
                record_store=self.store, control_plane_root_path=self.root, lease_owner="test"
            )
        self.assertTrue(result.terminal_write_committed)
        operation = self.store.list_verireel_prod_backup_gate_operation_records()[0]
        self.assertEqual(operation.status, "pass")
        self.assertEqual(
            self.store.read_backup_gate_record(operation.backup_record_id).status, "pass"
        )

    def test_client_acceptance_runs_verified_backup_and_promotion_once(self) -> None:
        accepted = self.accept()
        (backup_id,) = self.advance()
        self.assertEqual(self.advance(), ())
        self.assertEqual(self.provider.deployed_artifacts, [])
        backup = self.store.read_verireel_prod_backup_gate_operation_record(backup_id)
        assert isinstance(backup.request, ProductionBackupGateRequest)
        self.assertEqual(backup.request.promotion_action, GENERIC_WEB_PROMOTION_BACKUP_ACTION)
        self.capture()
        self.assertEqual(len(self.advance()), 1)
        run = read_client_release_run(store=self.store, profile=self.profile, decision=accepted)
        assert run is not None
        self.assertEqual(run.state, "passed")
        self.assertEqual(self.advance(), ())
        self.assertEqual(
            self.provider.deployed_artifacts, [accepted.checklist.candidate.artifact_id]
        )
        promotions = self.store.list_promotion_records()
        self.assertEqual(promotions[0].backup_record_id, backup.backup_record_id)
        self.assertEqual(promotions[0].destination_health.status, "pass")

    def test_github_outage_before_deploy_retries_accepted_release_without_rollback(self) -> None:
        from control_plane.lane_movement import LaneMovementRefused

        accepted = self.accept()
        self.advance()
        self.capture()
        with patch(
            "control_plane.workflows.generic_web_deploy.require_forward_lane_build",
            side_effect=LaneMovementRefused("source_order_unavailable"),
        ):
            for _ in range(4):
                self.assertEqual(self.advance(), ())
                run = read_client_release_run(
                    store=self.store, profile=self.profile, decision=accepted
                )
                assert run is not None
                self.assertNotEqual(run.state, "stopped")
                self.assertEqual(self.provider.deployed_artifacts, [])
        self.assertEqual(len(self.advance()), 1)
        self.assertEqual(
            self.provider.deployed_artifacts, [accepted.checklist.candidate.artifact_id]
        )

    def test_failed_health_rolls_back_and_stops_without_repeating_release(self) -> None:
        self.fail_health = True
        accepted = self.accept()
        self.advance()
        self.capture()
        self.advance()
        self.assertEqual(
            self.provider.deployed_artifacts,
            [accepted.checklist.candidate.artifact_id, accepted.checklist.production.artifact_id],
        )
        production = self.store.read_environment_inventory(
            context_name=self.context, instance_name="prod"
        )
        assert production.runtime_identity is not None
        self.assertEqual(
            production.runtime_identity.artifact_id, accepted.checklist.production.artifact_id
        )
        promotion = self.store.list_promotion_records()[0]
        self.assertEqual(promotion.rollback.status, "pass")
        run = read_client_release_run(store=self.store, profile=self.profile, decision=accepted)
        assert run is not None
        self.assertEqual(run.state, "stopped")
        self.assertEqual(self.advance(), ())

    def test_standing_acceptance_records_and_publishes_exact_candidate(self) -> None:
        self.switch("director_standing")
        self.advance()
        decision = self.store.list_release_review_decision_records(product=self.profile.product)[0]
        self.assertEqual(decision.acceptance_source, "director_standing")
        self.assertEqual(decision.actor_github_id, self.profile.owner.github_id)
        self.assertEqual(decision.checklist.candidate.source_commit, HEAD)
        self.publish.assert_called_once()
        self.assertEqual(self.advance(), ())
        self.publish.assert_called_once()
        self.capture()
        self.advance()
        self.assertEqual(
            self.provider.deployed_artifacts, [decision.checklist.candidate.artifact_id]
        )

    def test_late_standing_writer_keeps_publication_and_admitted_backup_authorized(self) -> None:
        self.switch("director_standing")
        create = self.store.create_release_review_decision_record_if_absent
        winner: ReleaseReviewDecisionRecord | None = None

        def race(contender: ReleaseReviewDecisionRecord) -> ReleaseReviewDecisionRecord:
            nonlocal winner
            earlier = contender.model_copy(
                update={
                    "decided_at": (
                        datetime.fromisoformat(contender.decided_at) - timedelta(seconds=1)
                    ).isoformat(),
                    "actor_github_login": "client-before-rename",
                }
            )
            create(earlier)
            winner = self.store.record_release_review_decision_publication(
                record_id=earlier.record_id,
                release_issue_url="https://github.com/example/site/issues/98",
            )
            # The other worker has published and admitted its backup before this
            # stale contender reaches the insertion point.
            (backup_id,) = self.advance()
            backup = self.store.read_verireel_prod_backup_gate_operation_record(backup_id)
            assert backup.authorization is not None
            self.assertEqual(backup.authorization.release_decision_record_id, winner.record_id)
            recovered = create(contender)
            self.assertEqual(recovered, winner)
            self.assertEqual(
                self.store.list_release_review_decision_records(product=self.profile.product)[0],
                winner,
            )
            # Verify before the late creator returns: the real backup worker
            # must retain its authorization throughout the contention window.
            self.capture()
            return recovered

        with patch.object(
            self.store, "create_release_review_decision_record_if_absent", side_effect=race
        ):
            self.advance()
        self.publish.assert_not_called()
        self.assertEqual(
            self.store.list_release_review_decision_records(product=self.profile.product), (winner,)
        )
        self.assertIsNotNone(winner)
        assert winner is not None
        self.assertEqual(self.provider.deployed_artifacts, [winner.checklist.candidate.artifact_id])

    def test_missing_acceptance_hold_override_or_changes_requested_starts_nothing(self) -> None:
        self.assertEqual(self.advance(), ())
        for outcome in ("overridden", "changes_requested"):
            self.accept(outcome)
            self.assertEqual(self.advance(), ())
            self.switch("director_standing")
            self.assertEqual(self.advance(), ())
        self.switch("held")
        self.accept()
        self.assertEqual(self.advance(), ())
        self.assertEqual(self.provider.deployed_artifacts, [])

    def test_unpublished_standing_acceptance_retries_publication_without_effects(self) -> None:
        self.switch("director_standing")
        self.publish.side_effect = ValueError("unavailable")
        self.assertEqual(self.advance(), ())
        saved = self.store.list_release_review_decision_records(product=self.profile.product)[0]
        self.assertEqual(saved.release_issue_url, "")
        self.assertEqual(self.store.list_verireel_prod_backup_gate_operation_records(), ())
        self.publish.side_effect = None
        self.advance()
        retry = self.store.list_release_review_decision_records(product=self.profile.product)[0]
        self.assertEqual(saved.record_id, retry.record_id)
        self.assertEqual(self.provider.deployed_artifacts, [])

    def test_unpublished_acceptance_backs_off_then_reuses_its_saved_record(self) -> None:
        self.switch("director_standing")
        backoff = StandingReleaseReviewBackoff()
        self.publish.side_effect = ValueError("Issues unavailable")
        with (
            patch("control_plane.client_release.monotonic", return_value=0) as clock,
            patch(
                "control_plane.client_release.current_release_review", side_effect=self.review
            ) as read,
        ):
            advance_client_releases(
                store=self.store, control_plane_root=self.root, standing_review_backoff=backoff
            )
            saved = self.store.list_release_review_decision_records(product=self.profile.product)[0]
            advance_client_releases(
                store=self.store, control_plane_root=self.root, standing_review_backoff=backoff
            )
            read.assert_called_once()
            self.publish.assert_called_once()
            self.assertEqual(self.store.list_verireel_prod_backup_gate_operation_records(), ())
            clock.return_value = backoff.blocked[self.profile.product][1] + 1
            self.publish.side_effect = None
            advance_client_releases(
                store=self.store, control_plane_root=self.root, standing_review_backoff=backoff
            )
            retry = self.store.list_release_review_decision_records(product=self.profile.product)[0]
            self.assertEqual(retry.record_id, saved.record_id)
            self.assertNotIn(self.profile.product, backoff.blocked)
            self.assertEqual(len(self.store.list_verireel_prod_backup_gate_operation_records()), 1)

    def test_returning_to_a_previously_decided_candidate_backs_off_reads(self) -> None:
        self.switch("director_standing")
        self.advance()
        prior_inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        assert prior_inventory.runtime_identity is not None
        self.store.write_environment_inventory(
            prior_inventory.model_copy(
                update={
                    "runtime_identity": prior_inventory.runtime_identity.model_copy(
                        update={"source_git_ref": "d" * 40}
                    )
                }
            )
        )
        self.accept()
        self.store.write_environment_inventory(prior_inventory)
        backoff = StandingReleaseReviewBackoff()
        with patch(
            "control_plane.client_release.current_release_review", side_effect=self.review
        ) as read:
            advance_client_releases(
                store=self.store, control_plane_root=self.root, standing_review_backoff=backoff
            )
            advance_client_releases(
                store=self.store, control_plane_root=self.root, standing_review_backoff=backoff
            )
        read.assert_called_once()
        self.assertEqual(self.provider.deployed_artifacts, [])

    @patch(
        "control_plane.workflows.verireel_prod_backup_gate_operation_worker._utc_now_timestamp",
        side_effect=lambda: datetime.now(UTC).isoformat(),
    )
    def test_changed_notes_while_waiting_get_fresh_acceptance_and_backup(self, _clock: Any) -> None:
        self.switch("director_standing")
        self.advance()
        prior = self.store.list_release_review_decision_records(product=self.profile.product)[0]
        self.capture()
        original_read = self.raw_read

        def changed_read(path: str) -> object:
            result = original_read(path)
            if isinstance(result, list):
                return [
                    {**item, "body": "## Client test notes\nCheck the updated behavior."}
                    for item in result
                ]
            return result

        self.raw_read = changed_read
        self.advance()
        latest = self.store.list_release_review_decision_records(product=self.profile.product)[0]
        self.assertNotEqual(latest.record_id, prior.record_id)
        self.assertNotEqual(latest.checklist_digest, prior.checklist_digest)
        self.assertEqual(latest.checklist.candidate, prior.checklist.candidate)
        self.assertEqual(len(self.store.list_verireel_prod_backup_gate_operation_records()), 2)
        self.assertEqual(self.provider.deployed_artifacts, [])
        self.capture()
        self.advance()
        self.assertEqual(self.provider.deployed_artifacts, [latest.checklist.candidate.artifact_id])

    def test_stale_candidate_or_hold_after_backup_never_deploys(self) -> None:
        self.accept()
        self.advance()
        self.capture()
        self.switch("held")
        self.assertEqual(self.advance(), ())
        self.switch("promote")
        inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        assert inventory.runtime_identity is not None
        self.store.write_environment_inventory(
            inventory.model_copy(
                update={
                    "runtime_identity": inventory.runtime_identity.model_copy(
                        update={"source_git_ref": "d" * 40}
                    )
                }
            )
        )
        self.assertEqual(self.advance(), ())
        self.assertEqual(self.provider.deployed_artifacts, [])

    def test_missing_or_failed_backup_evidence_prevents_provider_mutation(self) -> None:
        self.accept()
        self.advance()
        self.capture()
        operation = self.store.list_verireel_prod_backup_gate_operation_records()[0]
        record = self.store.read_backup_gate_record(operation.backup_record_id)
        self.store.write_backup_gate_record(record.model_copy(update={"status": "fail"}))
        self.assertEqual(len(self.advance()), 1)
        self.assertEqual(self.advance(), ())
        self.assertEqual(self.provider.deployed_artifacts, [])

    def test_changing_client_revokes_standing_acceptance(self) -> None:
        self.switch("director_standing")
        current = self.store.read_product_profile_record(self.profile.product)
        renamed = updated_product_owner_profile(
            profile=current,
            resolved_owner=ProductOwnerIdentity(
                github_id=current.owner.github_id, github_login="renamed"
            ),
            updated_at=datetime.now(UTC).isoformat(),
        )
        self.assertEqual(renamed.release_on_acceptance, "director_standing")
        changed = updated_product_owner_profile(
            profile=current,
            resolved_owner=ProductOwnerIdentity(github_id="9999", github_login="another-client"),
            updated_at=datetime.now(UTC).isoformat(),
        )
        self.assertEqual(changed.release_on_acceptance, "held")
        self.store.write_product_profile_record(changed)
        self.assertEqual(self.advance(), ())

    def test_new_candidate_after_requested_changes_gets_standing_acceptance(self) -> None:
        self.switch("director_standing")
        previous = self.accept("changes_requested")
        self.assertEqual(self.advance(), ())
        inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        assert inventory.runtime_identity is not None
        self.store.write_environment_inventory(
            inventory.model_copy(
                update={
                    "runtime_identity": inventory.runtime_identity.model_copy(
                        update={"source_git_ref": "d" * 40}
                    ),
                }
            )
        )
        self.assertEqual(len(self.advance()), 1)
        accepted = self.store.list_release_review_decision_records(product=self.profile.product)[0]
        self.assertEqual(accepted.decision, "accepted")
        self.assertNotEqual(accepted.checklist_digest, previous.checklist_digest)
        self.assertEqual(accepted.checklist.candidate.source_commit, "d" * 40)

    def test_profile_metadata_change_does_not_retry_failed_standing_release(self) -> None:
        self.switch("director_standing")
        self.advance()
        self.capture()
        with patch(
            "control_plane.workflows.generic_web_promotion._wait_for_healthcheck",
            side_effect=click.ClickException("Testing is unhealthy"),
        ):
            self.advance()
        profile = self.store.read_product_profile_record(self.profile.product)
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "updated_at": datetime.now(UTC).isoformat(),
                    "source_label": "metadata update",
                }
            )
        )
        self.assertEqual(self.advance(), ())
        self.assertEqual(
            len(self.store.list_release_review_decision_records(product=self.profile.product)), 1
        )
        self.assertEqual(len(self.store.list_promotion_records()), 1)
        self.assertEqual(self.provider.deployed_artifacts, [])

    def test_settled_standing_decision_does_not_read_github_again(self) -> None:
        self.switch("director_standing")
        self.advance()
        with patch("control_plane.client_release.current_release_review") as read:
            self.assertEqual(self.advance(), ())
        read.assert_not_called()

    def test_incomplete_standing_review_backs_off_until_lane_changes(self) -> None:
        self.switch("director_standing")
        incomplete = self.review()
        assert incomplete.checklist is not None
        incomplete = incomplete.model_copy(
            update={
                "checklist": incomplete.checklist.model_copy(update={"untracked_commits": (HEAD,)})
            }
        )
        backoff = StandingReleaseReviewBackoff()
        with patch(
            "control_plane.client_release.current_release_review", return_value=incomplete
        ) as read:
            advance_client_releases(
                store=self.store, control_plane_root=self.root, standing_review_backoff=backoff
            )
            advance_client_releases(
                store=self.store, control_plane_root=self.root, standing_review_backoff=backoff
            )
            read.assert_called_once()
            inventory = self.store.read_environment_inventory(
                context_name=self.context, instance_name="testing"
            )
            assert inventory.runtime_identity is not None
            self.store.write_environment_inventory(
                inventory.model_copy(
                    update={
                        "runtime_identity": inventory.runtime_identity.model_copy(
                            update={"source_git_ref": "d" * 40}
                        )
                    }
                )
            )
            read.reset_mock()
            advance_client_releases(
                store=self.store, control_plane_root=self.root, standing_review_backoff=backoff
            )
            read.assert_called_once()
            self.assertEqual(
                self.store.list_release_review_decision_records(product=self.profile.product), ()
            )

    def test_standing_reacceptance_keeps_prior_audit_and_gets_new_operations(self) -> None:
        self.switch("director_standing")
        self.advance()
        prior = self.store.list_release_review_decision_records(product=self.profile.product)[0]
        self.switch("held")
        held = self.accept()
        self.assertEqual(held.release_start, "")
        self.switch("director_standing")
        self.advance()
        latest = self.store.list_release_review_decision_records(product=self.profile.product)[0]
        self.assertNotEqual(latest.record_id, prior.record_id)
        recorded = self.store.list_release_review_decision_records(product=self.profile.product)
        self.assertEqual(
            next(record for record in recorded if record.record_id == prior.record_id), prior
        )
        operations = self.store.list_verireel_prod_backup_gate_operation_records()
        self.assertEqual(len(operations), 2)
        self.assertNotEqual(operations[0].operation_id, operations[1].operation_id)

    def test_hold_after_a_failed_deploy_still_allows_automatic_rollback(self) -> None:
        from typing import Any

        accepted = self.accept()
        self.advance()
        self.capture()
        self.provider.fail_artifact = accepted.checklist.candidate.artifact_id
        original = self.provider.execute_artifact_deploy

        def execute(**kwargs: Any) -> None:
            try:
                original(**kwargs)
            finally:
                self.switch("held")

        with patch.object(self.provider, "execute_artifact_deploy", side_effect=execute):
            self.advance()
        self.assertEqual(
            self.provider.deployed_artifacts,
            [accepted.checklist.candidate.artifact_id, accepted.checklist.production.artifact_id],
        )
        self.assertEqual(self.store.list_promotion_records()[0].rollback.status, "pass")

    def test_failed_source_health_is_terminal_and_never_auto_retries(self) -> None:
        accepted = self.accept()
        self.advance()
        self.capture()
        with patch(
            "control_plane.workflows.generic_web_promotion._wait_for_healthcheck",
            side_effect=click.ClickException("Testing is unhealthy"),
        ):
            self.advance()
        run = read_client_release_run(store=self.store, profile=self.profile, decision=accepted)
        assert run is not None
        self.assertEqual(run.state, "stopped")
        self.assertEqual(self.advance(), ())
        self.assertEqual(len(self.store.list_promotion_records()), 1)
        self.assertEqual(self.provider.deployed_artifacts, [])

    def test_target_change_after_resolution_is_refused_before_provider(self) -> None:
        accepted = self.accept()
        self.advance()
        self.capture()
        original = self.provider.resolve_deploy_target

        def retarget(**kwargs: Any) -> GenericWebResolvedDeployTarget:
            target = original(**kwargs)
            self.store.write_provider_target_record(
                self.target.model_copy(update={"target_id": "replacement"})
            )
            return target

        with patch.object(self.provider, "resolve_deploy_target", side_effect=retarget):
            self.advance()
        run = read_client_release_run(store=self.store, profile=self.profile, decision=accepted)
        assert run is not None
        self.assertEqual(run.state, "stopped")
        self.assertEqual(self.provider.deployed_artifacts, [])

    def test_second_review_read_failure_stops_release_without_a_silent_retry(self) -> None:
        accepted = self.accept()
        self.advance()
        self.capture()
        calls = 0

        def review(**kwargs: object) -> ReleaseReviewStatus:
            nonlocal calls
            calls += 1
            if calls > 1:
                return ReleaseReviewStatus(unavailable_reason="github_read_failed")
            return self.review(**kwargs)

        with patch("control_plane.client_release.current_release_review", side_effect=review):
            self.advance()
        run = read_client_release_run(store=self.store, profile=self.profile, decision=accepted)
        assert run is not None
        self.assertEqual(run.state, "stopped")
        self.assertEqual(self.advance(), ())
        self.assertEqual(self.provider.deployed_artifacts, [])

    def test_expired_running_promotion_reports_reconciliation(self) -> None:
        accepted = self.accept()
        self.advance()
        self.capture()
        self.store.reserve_mutation(
            scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
            idempotency_key=f"{accepted.record_id}:promote-1",
            request_fingerprint="f" * 64,
            lease_owner="crashed-worker",
            lease_seconds=300,
        )
        # Rehearsal DB clock advances past the acquired lease.
        with patch("control_plane.client_release.datetime") as clock:
            clock.now.return_value = datetime.now(UTC) + timedelta(seconds=600)
            run = read_client_release_run(store=self.store, profile=self.profile, decision=accepted)
        assert run is not None
        self.assertEqual(run.state, "stopped")
        self.assertEqual(run.steps[-1].status, "reconciliation_required")


if __name__ == "__main__":
    unittest.main()
