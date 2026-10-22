"""Run the service-owned release with real storage, backup gates and fake providers."""

from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import patch

import click

from control_plane.client_release import (
    ClientReleaseStep,
    advance_client_releases,
    StandingReleaseReviewBackoff,
    read_client_release_run,
    release_start_for_acceptance,
    client_release_grant,
    client_release_grant_allows,
    _generic_web_release_authority_allows,
)
from control_plane.contracts.product_profile_record import ProductOwnerProfile
from control_plane.contracts.production_backup_authority import ProductionBackupPolicyRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.deployment_record import DeploymentRecord
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
from control_plane.release_review import ReleaseEvidenceUnavailable
from control_plane.release_review import checklist_digest
from control_plane.contracts.promotion_record import PostDeployUpdateEvidence
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.production_promotion_backup import GENERIC_WEB_PROMOTION_BACKUP_ACTION
from control_plane.workflows.production_backup_gate_operation_worker import (
    run_production_backup_gate_operation_worker_once,
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
        for key, binding in source.provider_targets.items():
            source.provider_targets[key] = binding.model_copy(update={"target_id": "testing-app"})
            self.store.write_provider_target_record(source.provider_targets[key])
        testing = source.read_environment_inventory(
            context_name=self.profile.lanes[0].context, instance_name="testing"
        )
        prior = source.read_deployment_record(testing.deployment_record_id)
        assert prior.deployed_target is not None and prior.resolved_target is not None
        source.write_deployment_record(
            prior.model_copy(
                update={
                    "deployed_target": prior.deployed_target.model_copy(
                        update={"target_id": "testing-app"}
                    ),
                    "resolved_target": prior.resolved_target.model_copy(
                        update={"target_id": "testing-app"}
                    ),
                }
            )
        )
        for inventory in source.inventories.values():
            sha = BASE if inventory.instance == "prod" else HEAD
            deployment = source.deployments[inventory.deployment_record_id]
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
            self.store.write_deployment_record(
                deployment.model_copy(
                    update={
                        "artifact_identity": inventory.artifact_identity,
                        "source_git_ref": sha,
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
        from tests.support.lane_builds import LaneBuildGitHub

        transport = LaneBuildGitHub(
            self.profile.repository,
            self.profile.image.repository,
            {BASE: "sha256:" + "f" * 64, HEAD: "sha256:" + "b" * 64},
        )
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

    def accept(
        self, outcome: ReleaseDecision = "accepted", *, suffix: str = ""
    ) -> ReleaseReviewDecisionRecord:
        review = self.review()
        assert review.checklist is not None
        record = ReleaseReviewDecisionRecord(
            record_id=f"decision-{outcome}{suffix}",
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

    def write_qualified_testing_inventory(self, inventory: EnvironmentInventory) -> None:
        """Represent a complete testing deployment when moving this fixture's candidate."""
        identity = inventory.runtime_identity
        assert identity is not None and inventory.artifact_identity is not None
        artifact = inventory.artifact_identity.model_copy(
            update={"artifact_id": identity.artifact_id}
        )
        record = self.store.read_deployment_record(inventory.deployment_record_id)
        self.store.write_deployment_record(
            record.model_copy(
                update={
                    "artifact_identity": artifact,
                    "source_git_ref": identity.source_git_ref,
                    "runtime_identity": identity,
                }
            )
        )
        self.store.write_environment_inventory(
            inventory.model_copy(
                update={
                    "artifact_identity": artifact,
                    "source_git_ref": identity.source_git_ref,
                }
            )
        )

    def capture(self) -> None:
        with (
            # Real backup captures take longer than a second. Preserve ordering
            # for the two fast stub captures without a wall-clock sleep.
            patch(
                "control_plane.workflows.production_backup_gate_operation_worker._utc_now_timestamp",
                side_effect=lambda: datetime.now(UTC).isoformat(),
            ),
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
            result = run_production_backup_gate_operation_worker_once(
                record_store=self.store, control_plane_root_path=self.root, lease_owner="test"
            )
        self.assertTrue(result.terminal_write_committed)
        operation = self.store.list_verireel_prod_backup_gate_operation_records()[0]
        self.assertEqual(operation.status, "pass")
        self.assertEqual(
            self.store.read_backup_gate_record(operation.backup_record_id).status, "pass"
        )

    def _qualification_lost_after_acceptance(self, *, backup_completed: bool) -> None:
        decision = self.accept()
        if backup_completed:
            self.assertTrue(self.advance())
            self.capture()
        inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        record = self.store.read_deployment_record(inventory.deployment_record_id)
        self.store.write_deployment_record(
            record.model_copy(
                update={
                    "deploy": record.deploy.model_copy(update={"status": "fail"}),
                    "post_deploy_update": PostDeployUpdateEvidence(attempted=True, status="fail"),
                    "runtime_identity": None,
                }
            )
        )
        with self.assertRaises(ReleaseEvidenceUnavailable):
            self.review()
        self.assertEqual(self.advance(), ())
        self.assertEqual(self.provider.deployed_artifacts, [])
        self.assertEqual(
            self.store.read_release_review_decision_record(
                product=self.profile.product, record_id=decision.record_id
            ),
            decision,
        )

        # Another qualified deployment of the accepted artifact restores its
        # engineering evidence without asking the Client to accept it again.
        assert record.runtime_identity is not None
        retest_id = record.record_id + "-retest"
        identity = record.runtime_identity.model_copy(update={"deployment_record_id": retest_id})
        self.store.write_deployment_record(
            record.model_copy(update={"record_id": retest_id, "runtime_identity": identity})
        )
        self.store.write_environment_inventory(
            inventory.model_copy(
                update={"deployment_record_id": retest_id, "runtime_identity": identity}
            )
        )
        self.assertTrue(self.review().approved)
        self.assertEqual(self.review().checklist_digest, decision.checklist_digest)
        if not backup_completed:
            self.assertTrue(self.advance())
            self.capture()
        self.assertTrue(self.advance())
        self.assertEqual(
            self.provider.deployed_artifacts, [decision.checklist.candidate.artifact_id]
        )

    def test_failed_testing_record_blocks_accepted_release_until_same_artifact_retest(self) -> None:
        self._qualification_lost_after_acceptance(backup_completed=False)

    def test_failed_testing_record_after_backup_blocks_production_until_retest(self) -> None:
        self._qualification_lost_after_acceptance(backup_completed=True)

    def test_qualification_lost_during_source_health_wait_never_dispatches(self) -> None:
        self._source_changed_during_health_wait("post_deploy_failure")

    def test_pre_effect_source_refusal_resumes_same_accepted_artifact_after_retest(self) -> None:
        self.pre_effect_source_refusal_retest("post_deploy_failure")

    def test_new_testing_during_wait_can_return_to_the_accepted_artifact(self) -> None:
        self.pre_effect_source_refusal_retest("new_testing")

    def test_rebound_testing_during_wait_can_return_to_the_accepted_lane(self) -> None:
        self.pre_effect_source_refusal_retest("context_rebound")

    def pre_effect_source_refusal_retest(self, change: str) -> None:
        from control_plane import client_release as releases

        profile = self.store.read_product_profile_record(self.profile.product)
        inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        testing = self.store.read_deployment_record(inventory.deployment_record_id)
        self._source_changed_during_health_wait(change)
        decision = self.store.list_release_review_decision_records(product=self.profile.product)[0]
        for _ in range(2):
            reservation = self.store.read_idempotency_record(
                scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
                route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
                idempotency_key=releases._step_key(
                    decision, releases.ClientReleaseStep("promote", 1)
                ),
            )
            self.assertIsNone(reservation)
            self.store.write_product_profile_record(profile)
            self.store.write_deployment_record(testing)
            self.store.write_environment_inventory(inventory)
            with patch(
                "control_plane.workflows.generic_web_promotion._wait_for_healthcheck",
                side_effect=lambda **_kwargs: self._lose_testing_source(change, inventory, testing),
            ):
                self.assertEqual(self.advance(), ())
            self.assertEqual(self.provider.deployed_artifacts, [])
        self.store.write_product_profile_record(profile)
        assert testing.runtime_identity is not None
        retest_id = testing.record_id + "-retest"
        identity = testing.runtime_identity.model_copy(update={"deployment_record_id": retest_id})
        self.store.write_deployment_record(
            testing.model_copy(
                update={
                    "record_id": retest_id,
                    "runtime_identity": identity,
                }
            )
        )
        self.store.write_environment_inventory(
            inventory.model_copy(
                update={
                    "deployment_record_id": retest_id,
                    "runtime_identity": identity,
                }
            )
        )
        self.assertEqual(self.review().checklist_digest, decision.checklist_digest)
        self.assertTrue(self.advance())
        self.assertEqual(
            self.provider.deployed_artifacts, [decision.checklist.candidate.artifact_id]
        )
        self.assertEqual(
            self.store.read_release_review_decision_record(
                product=self.profile.product, record_id=decision.record_id
            ),
            decision,
        )

    def test_testing_context_rebound_during_source_health_wait_never_dispatches(self) -> None:
        self._source_changed_during_health_wait("context_rebound")

    def test_testing_lane_removed_during_source_health_wait_never_dispatches(self) -> None:
        self._source_changed_during_health_wait("lane_removed")

    def test_accepted_release_finishes_after_new_qualified_testing_arrives(self) -> None:
        decision = self.accept()
        self.advance()
        self.capture()
        execute = self.provider.execute_artifact_deploy

        def advance_testing_after_image(**kwargs: Any) -> None:
            execute(**kwargs)
            inventory = self.store.read_environment_inventory(
                context_name=self.context, instance_name="testing"
            )
            record = self.store.read_deployment_record(inventory.deployment_record_id)
            assert (
                inventory.runtime_identity is not None and inventory.artifact_identity is not None
            )
            next_id = record.record_id + "-next"
            artifact = inventory.artifact_identity.model_copy(
                update={"artifact_id": self.profile.image.repository + "@sha256:" + "d" * 64}
            )
            identity = inventory.runtime_identity.model_copy(
                update={
                    "artifact_id": artifact.artifact_id,
                    "source_git_ref": "d" * 40,
                    "deployment_record_id": next_id,
                }
            )
            self.store.write_deployment_record(
                record.model_copy(
                    update={
                        "record_id": next_id,
                        "artifact_identity": artifact,
                        "source_git_ref": identity.source_git_ref,
                        "runtime_identity": identity,
                    }
                )
            )
            self.store.write_environment_inventory(
                inventory.model_copy(
                    update={
                        "deployment_record_id": next_id,
                        "artifact_identity": artifact,
                        "source_git_ref": identity.source_git_ref,
                        "runtime_identity": identity,
                    }
                )
            )
            kwargs["before_provider_mutation"]("post_image_maintenance")

        with patch.object(
            self.provider, "execute_artifact_deploy", side_effect=advance_testing_after_image
        ):
            self.advance()
        self.assertEqual(
            self.provider.deployed_artifacts, [decision.checklist.candidate.artifact_id]
        )
        promotion = self.store.list_promotion_records()[0]
        self.assertEqual(promotion.deploy.status, "pass")
        self.assertEqual(promotion.rollback.status, "skipped")
        self.assertEqual(
            self.store.read_release_review_decision_record(
                product=self.profile.product, record_id=decision.record_id
            ),
            decision,
        )

    def test_default_forward_grant_still_requires_testing_but_attempt_authority_is_separate(
        self,
    ) -> None:
        decision = self.accept()
        grant = client_release_grant(
            decision=decision,
            action="generic_web_prod_promotion.execute",
            context=self.context,
            authorized_at=decision.decided_at,
        )
        self.assertTrue(client_release_grant_allows(self.store, grant))
        inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        record = self.store.read_deployment_record(inventory.deployment_record_id)
        self.store.write_deployment_record(
            record.model_copy(
                update={"deploy": record.deploy.model_copy(update={"status": "fail"})}
            )
        )
        self.assertFalse(client_release_grant_allows(self.store, grant))
        self.assertTrue(_generic_web_release_authority_allows(self.store, grant))
        self.store.write_deployment_record(record)
        assert inventory.runtime_identity is not None
        self.write_qualified_testing_inventory(
            inventory.model_copy(
                update={
                    "runtime_identity": inventory.runtime_identity.model_copy(
                        update={"source_git_ref": "d" * 40}
                    )
                }
            )
        )
        self.assertFalse(client_release_grant_allows(self.store, grant))
        self.assertTrue(_generic_web_release_authority_allows(self.store, grant))
        for changed in (
            grant.model_copy(update={"context": "another-context"}),
            grant.model_copy(update={"release_decision_record_id": "another-decision"}),
            grant.model_copy(
                update={"caller": grant.caller.model_copy(update={"github_id": 12345})}
            ),
        ):
            self.assertFalse(_generic_web_release_authority_allows(self.store, changed))
        self.switch("held")
        self.assertFalse(_generic_web_release_authority_allows(self.store, grant))

    def test_rejection_rollback_ignores_failed_testing_qualification(self) -> None:
        self.rejection_rollback_without_testing(missing=False)

    def test_rejection_rollback_ignores_missing_testing_record(self) -> None:
        self.rejection_rollback_without_testing(missing=True)

    def rejection_rollback_without_testing(self, *, missing: bool) -> None:
        candidate = self.accept()
        baseline_checklist = candidate.checklist.model_copy(
            update={"candidate": candidate.checklist.production, "database_compatibility": None}
        )
        self.store.write_release_review_decision_record(
            candidate.model_copy(
                update={
                    "checklist": baseline_checklist,
                    "checklist_digest": checklist_digest(baseline_checklist),
                    "release_start": "",
                    "decided_at": (datetime.now(UTC) - timedelta(seconds=2)).isoformat(),
                }
            )
        )
        hotfix = candidate.model_copy(
            update={
                "record_id": "director-hotfix",
                "decision": "temporary_hotfix",
                "actor_github_id": "123",
                "actor_github_login": "director",
                "release_start": "promote",
                "decided_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            }
        )
        rejection = candidate.model_copy(
            update={
                "record_id": "client-hotfix-rejection",
                "decision": "changes_requested",
                "release_start": "rollback",
                "decided_at": datetime.now(UTC).isoformat(),
            }
        )
        self.store.write_release_review_decision_record(hotfix)
        self.store.write_release_review_decision_record(rejection)
        testing = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        original = self.store.read_deployment_record(testing.deployment_record_id)
        previous = self.store.read_deployment_record(
            self.store.read_environment_inventory(
                context_name=self.context, instance_name="prod"
            ).deployment_record_id
        )
        assert original.runtime_identity is not None
        identity = original.runtime_identity.model_copy(
            update={"instance": "prod", "deployment_record_id": "live-hotfix"}
        )
        hotfix_deployment = original.model_copy(
            update={
                "record_id": "live-hotfix",
                "instance": "prod",
                "runtime_identity": identity,
                "deploy": previous.deploy,
                "resolved_target": previous.resolved_target,
                "deployed_target": previous.deployed_target,
            }
        )
        self.store.write_deployment_record(hotfix_deployment)
        self.store.write_environment_inventory(
            build_environment_inventory(
                deployment_record=hotfix_deployment, updated_at=rejection.decided_at
            )
        )
        self.provider.running = identity
        if not missing:
            self.store.write_deployment_record(
                original.model_copy(
                    update={"deploy": original.deploy.model_copy(update={"status": "fail"})}
                )
            )
        read = self.store.read_deployment_record

        def read_record(record_id: str) -> Any:
            if missing and record_id == testing.deployment_record_id:
                raise FileNotFoundError(record_id)
            return read(record_id)

        grant = client_release_grant(
            decision=rejection,
            action="generic_web_rollback.execute",
            context=self.context,
            authorized_at=rejection.decided_at,
        )
        with patch.object(self.store, "read_deployment_record", side_effect=read_record):
            self.assertTrue(client_release_grant_allows(self.store, grant))
            self.assertTrue(self.advance())
        self.assertEqual(
            self.provider.deployed_artifacts, [candidate.checklist.production.artifact_id]
        )
        production = self.store.read_environment_inventory(
            context_name=self.context, instance_name="prod"
        )
        assert production.runtime_identity is not None
        self.assertEqual(
            production.runtime_identity.artifact_id,
            candidate.checklist.production.artifact_id,
        )

    def _source_changed_during_health_wait(self, change: str) -> None:
        self.accept()
        self.advance()
        self.capture()
        inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        record = self.store.read_deployment_record(inventory.deployment_record_id)

        def lose_qualification(**_kwargs: object) -> None:
            self._lose_testing_source(change, inventory, record)

        with patch(
            "control_plane.workflows.generic_web_promotion._wait_for_healthcheck",
            side_effect=lose_qualification,
        ):
            self.advance()
        self.assertEqual(self.provider.deployed_artifacts, [])

    def _lose_testing_source(
        self, change: str, inventory: EnvironmentInventory, record: DeploymentRecord
    ) -> None:
        if change == "post_deploy_failure":
            self.store.write_deployment_record(
                record.model_copy(
                    update={
                        "post_deploy_update": PostDeployUpdateEvidence(
                            attempted=True, status="fail"
                        ),
                    }
                )
            )
        elif change == "new_testing":
            assert inventory.runtime_identity is not None
            identity = inventory.runtime_identity.model_copy(
                update={
                    "artifact_id": self.profile.image.repository + "@sha256:" + "e" * 64,
                    "source_git_ref": "e" * 40,
                }
            )
            self.write_qualified_testing_inventory(
                inventory.model_copy(update={"runtime_identity": identity})
            )
        else:
            profile = self.store.read_product_profile_record(self.profile.product)
            self.store.write_product_profile_record(
                profile.model_copy(
                    update={
                        "lanes": tuple(
                            lane.model_copy(update={"context": lane.context + "-replacement"})
                            if lane.instance == "testing"
                            else lane
                            for lane in profile.lanes
                            if not (change == "lane_removed" and lane.instance == "testing")
                        ),
                    }
                )
            )

    def test_held_and_changed_candidate_progress_uses_local_records_only(self) -> None:
        accepted = self.accept()
        self.switch("held")
        with patch(
            "control_plane.client_release.current_release_review",
            side_effect=AssertionError("Progress must not call source control"),
        ):
            held = read_client_release_run(
                store=self.store,
                profile=self.store.read_product_profile_record(self.profile.product),
                decision=accepted,
            )
            assert held is not None
            self.assertEqual(held.state, "waiting")
            self.assertIn("hold", held.blocked_reason)
            self.switch("promote")
            ready = read_client_release_run(
                store=self.store,
                profile=self.store.read_product_profile_record(self.profile.product),
                decision=accepted,
            )
            assert ready is not None
            self.assertEqual(ready.blocked_reason, "")
            testing = self.store.read_environment_inventory(
                context_name=self.context, instance_name="testing"
            )
            assert testing.runtime_identity is not None
            self.write_qualified_testing_inventory(
                testing.model_copy(
                    update={
                        "runtime_identity": testing.runtime_identity.model_copy(
                            update={"source_git_ref": BASE}
                        ),
                    }
                )
            )
            stale = read_client_release_run(
                store=self.store,
                profile=self.store.read_product_profile_record(self.profile.product),
                decision=accepted,
            )
            assert stale is not None
            self.assertTrue(stale.blocked_reason)
            self.assertEqual(stale.steps, held.steps)
            self.assertEqual(self.advance(), ())
            self.assertEqual(self.provider.deployed_artifacts, [])
            self.assertEqual(self.store.list_verireel_prod_backup_gate_operation_records(), ())
            self.write_qualified_testing_inventory(testing)
            restored = read_client_release_run(
                store=self.store,
                profile=self.store.read_product_profile_record(self.profile.product),
                decision=accepted,
            )
            assert restored is not None
            self.assertEqual(restored.blocked_reason, "")
            self.assertEqual(restored.steps, stale.steps)

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
        self.assertEqual(run.state, "passed", run.model_dump())
        self.assertEqual(self.advance(), ())
        self.assertEqual(
            self.provider.deployed_artifacts, [accepted.checklist.candidate.artifact_id]
        )
        promotions = self.store.list_promotion_records()
        self.assertEqual(promotions[0].backup_record_id, backup.backup_record_id)
        self.assertEqual(promotions[0].destination_health.status, "pass")
        completed_review = self.review()
        self.assertTrue(completed_review.no_new_release)
        self.assertFalse(completed_review.required)
        self.assertFalse(completed_review.approved)
        self.assertEqual(completed_review.blockers, ())
        self.assertEqual(
            self.store.list_release_review_decision_records(product=self.profile.product),
            (accepted,),
        )

    def test_generic_web_drills_once_with_two_backups_and_the_reviewed_passing_target(self) -> None:
        from control_plane.generic_web_rollback_http import GENERIC_WEB_ROLLBACK_ROUTE

        self.switch("promote_with_rollback_drill")
        accepted = self.accept()
        self.assertEqual(accepted.release_start, "promote_with_rollback_drill")
        baseline = self.store.list_deployment_records(
            context_name=self.context, instance_name="prod"
        )[0]
        assert baseline.artifact_identity is not None
        assert baseline.runtime_identity is not None
        baseline = baseline.model_copy(
            update={
                "record_id": "newest-passing-reviewed-version",
                "runtime_identity": baseline.runtime_identity.model_copy(
                    update={"deployment_record_id": "newest-passing-reviewed-version"}
                ),
                "deploy": baseline.deploy.model_copy(
                    update={"finished_at": (datetime.now(UTC) - timedelta(seconds=2)).isoformat()}
                ),
            }
        )
        self.store.write_deployment_record(baseline)
        assert baseline.artifact_identity is not None
        # The newest passing deployment may be a different artifact; a newer
        # failed record of the reviewed artifact must not be selected either.
        for record_id, artifact_id, status in (
            ("newer-other-artifact", accepted.checklist.candidate.artifact_id, "pass"),
            ("newer-failed-baseline", accepted.checklist.production.artifact_id, "fail"),
        ):
            self.store.write_deployment_record(
                baseline.model_copy(
                    update={
                        "record_id": record_id,
                        "artifact_identity": baseline.artifact_identity.model_copy(
                            update={"artifact_id": artifact_id}
                        ),
                        "deploy": baseline.deploy.model_copy(
                            update={"status": status, "finished_at": datetime.now(UTC).isoformat()}
                        ),
                    }
                )
            )
        (backup_id,) = self.advance()
        self.capture()
        self.assertEqual(len(self.advance()), 1)
        self.assertEqual(
            self.provider.deployed_artifacts, [accepted.checklist.candidate.artifact_id]
        )
        (rollback_id,) = self.advance()
        reservation = self.store.read_idempotency_record(
            scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_ROLLBACK_ROUTE,
            idempotency_key=f"{accepted.record_id}:rollback-1",
        )
        assert reservation is not None
        self.assertEqual(reservation.record_id, rollback_id)
        self.assertEqual(
            reservation.response_payload["result"]["rollback_target_deployment_record_id"],
            baseline.record_id,
        )
        self.assertEqual(reservation.response_payload["result"]["rollback_health_status"], "pass")
        self.assertEqual(self.review().checklist_digest, accepted.checklist_digest)
        assert self.provider.running is not None
        self.assertEqual(
            self.provider.running.artifact_id, accepted.checklist.production.artifact_id
        )
        (second_backup_id,) = self.advance()
        self.assertNotEqual(second_backup_id, backup_id)
        self.assertEqual(self.advance(), ())
        self.capture()
        self.assertEqual(len(self.advance()), 1)
        run = read_client_release_run(store=self.store, profile=self.profile, decision=accepted)
        assert run is not None
        self.assertEqual(run.state, "passed", run.model_dump())
        self.assertTrue(all(step.status == "pass" for step in run.steps))
        self.assertTrue(self.review().no_new_release)
        self.assertEqual(self.advance(), ())
        self.assertEqual(
            self.provider.deployed_artifacts,
            [
                accepted.checklist.candidate.artifact_id,
                accepted.checklist.production.artifact_id,
                accepted.checklist.candidate.artifact_id,
            ],
        )
        promotions = self.store.list_promotion_records()
        self.assertEqual(len({record.backup_record_id for record in promotions}), 2)
        later = self.accept(suffix="-later")
        self.assertEqual(later.release_start, "promote")

    def test_drill_restores_baseline_without_testing_then_requalifies_repromotion(self) -> None:
        self.switch("promote_with_rollback_drill")
        decision = self.accept()
        self.advance()
        self.capture()
        self.advance()
        inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        testing = self.store.read_deployment_record(inventory.deployment_record_id)
        self.store.write_deployment_record(
            testing.model_copy(
                update={
                    "post_deploy_update": PostDeployUpdateEvidence(attempted=True, status="fail")
                }
            )
        )
        run = read_client_release_run(store=self.store, profile=self.profile, decision=decision)
        assert run is not None
        self.assertEqual(run.blocked_reason, "")
        self.assertTrue(self.advance())
        assert self.provider.running is not None
        self.assertEqual(
            self.provider.running.artifact_id, decision.checklist.production.artifact_id
        )
        self.assertEqual(self.advance(), ())
        self.assertEqual(len(self.store.list_verireel_prod_backup_gate_operation_records()), 1)
        # Requalification of the same accepted source resumes the fresh second
        # backup and promotion under the original decision.
        self.store.write_deployment_record(testing)
        self.assertTrue(self.advance())
        self.capture()
        self.assertTrue(self.advance())
        run = read_client_release_run(store=self.store, profile=self.profile, decision=decision)
        assert run is not None
        self.assertEqual(run.state, "passed")
        self.assertEqual(
            self.store.read_release_review_decision_record(
                product=self.profile.product, record_id=decision.record_id
            ),
            decision,
        )

    def test_testing_failure_mid_drill_does_not_interrupt_baseline_restore(self) -> None:
        self.switch("promote_with_rollback_drill")
        decision = self.accept()
        self.advance()
        self.capture()
        self.advance()
        execute = self.provider.execute_artifact_deploy

        def fail_testing_after_rollback_image(**kwargs: Any) -> None:
            execute(**kwargs)
            inventory = self.store.read_environment_inventory(
                context_name=self.context, instance_name="testing"
            )
            testing = self.store.read_deployment_record(inventory.deployment_record_id)
            self.store.write_deployment_record(
                testing.model_copy(
                    update={
                        "post_deploy_update": PostDeployUpdateEvidence(
                            attempted=True, status="fail"
                        )
                    }
                )
            )
            kwargs["before_provider_mutation"]("rollback_maintenance")

        with patch.object(
            self.provider, "execute_artifact_deploy", side_effect=fail_testing_after_rollback_image
        ):
            self.assertTrue(self.advance())
        run = read_client_release_run(store=self.store, profile=self.profile, decision=decision)
        assert run is not None
        self.assertEqual(
            next(
                step for step in run.steps if step.step == ClientReleaseStep("rollback", 1).name
            ).status,
            "pass",
        )
        self.assertEqual(
            next(
                step for step in run.steps if step.step == ClientReleaseStep("backup", 2).name
            ).status,
            "not_started",
        )
        assert self.provider.running is not None
        self.assertEqual(
            self.provider.running.artifact_id, decision.checklist.production.artifact_id
        )

    def test_started_inverse_finishes_lifecycle_after_authority_changes(self) -> None:
        for rejection, hold in ((False, True), (False, False), (True, False)):
            with self.subTest(rejection=rejection, hold=hold):
                case = GenericWebClientReleaseTests()
                case.setUp()
                try:
                    case.inverse_authority_changes_after_image(rejection=rejection, hold=hold)
                finally:
                    case.doCleanups()

    def inverse_authority_changes_after_image(self, *, rejection: bool, hold: bool) -> None:
        from control_plane import client_release as releases
        from control_plane.contracts.generic_web_extensions import (
            GenericWebApplicationSchedule,
            GenericWebLaneExtensions,
            GenericWebMaintenanceCommand,
        )
        from control_plane.workflows.generic_web_application_lifecycle import schedule_payload
        from control_plane.workflows.generic_web_deploy_provider import (
            DokployGenericWebDeployProvider,
        )

        case = self
        changed = False

        class Provider(_ProductionProvider, DokployGenericWebDeployProvider):
            def execute_artifact_deploy(self, **kwargs: Any) -> None:
                nonlocal changed
                super().execute_artifact_deploy(**kwargs)
                if kwargs["runtime_identity"].artifact_id != _PREVIOUS_ARTIFACT or changed:
                    return
                changed = True
                if hold:
                    case.switch("held")
                else:
                    # Human acceptance can supersede A without the standing guard.
                    case.accept(suffix="-new-human")

        self.provider = Provider()
        resolve = self.provider.resolve_deploy_target
        self.enterContext(
            patch.object(
                self.provider,
                "resolve_deploy_target",
                side_effect=lambda **kwargs: resolve(**kwargs).model_copy(
                    update={"deployed_target": self.target.to_deployed_target_reference()}
                ),
            )
        )
        self.enterContext(
            patch(
                "control_plane.generic_web_promotion_provider_adapter.default_generic_web_deploy_provider",
                return_value=self.provider,
            )
        )
        extensions = GenericWebLaneExtensions(
            maintenance=(GenericWebMaintenanceCommand(name="migrate", command="./migration"),),
            schedules=(
                GenericWebApplicationSchedule(
                    name="billing", command="./new-billing", cron_expression="*/15 * * * *"
                ),
            ),
        )
        self.profile = self.profile.model_copy(
            update={
                "release_on_acceptance": "promote_with_rollback_drill",
                "lanes": tuple(
                    lane.model_copy(update={"generic_web_extensions": extensions})
                    if lane.instance == "prod"
                    else lane
                    for lane in self.profile.lanes
                ),
            }
        )
        self.store.write_product_profile_record(self.profile)
        original = schedule_payload(
            GenericWebApplicationSchedule(
                name="billing", command="./old-billing", cron_expression="*/20 * * * *"
            ),
            "app-123",
            enabled=True,
        )
        schedules = {"billing": dict(original)}
        commands: list[str] = []

        def upsert(**kwargs: Any) -> dict[str, Any]:
            guard = kwargs.get("provider_mutation_guard")
            with guard() if callable(guard) else nullcontext():
                payload = kwargs["schedule_payload"]
                schedules[str(payload["name"])] = dict(payload)
                return {**payload, "scheduleId": payload["name"]}

        with (
            patch(
                "control_plane.dokploy.source.read_dokploy_config",
                return_value=("https://provider.invalid", "synthetic"),
            ),
            patch(
                "control_plane.dokploy.api.read_dokploy_application_schedule",
                side_effect=lambda **kwargs: schedules.get(str(kwargs["schedule_name"])),
            ),
            patch(
                "control_plane.dokploy.api.upsert_dokploy_application_schedule", side_effect=upsert
            ),
            patch(
                "control_plane.dokploy.api.run_dokploy_schedule",
                side_effect=lambda **kwargs: commands.append(
                    str(schedules[str(kwargs["schedule_id"])]["command"])
                ),
            ),
        ):
            decision = self.accept()
            self.advance()
            self.capture()
            self.advance()
            if rejection:
                baseline = decision.checklist.model_copy(
                    update={
                        "candidate": decision.checklist.production,
                        "database_compatibility": None,
                    }
                )
                self.store.write_release_review_decision_record(
                    decision.model_copy(
                        update={
                            "checklist": baseline,
                            "checklist_digest": checklist_digest(baseline),
                            "release_start": "",
                            "decided_at": (datetime.now(UTC) - timedelta(seconds=2)).isoformat(),
                        }
                    )
                )
                self.store.write_release_review_decision_record(
                    decision.model_copy(
                        update={
                            "record_id": "director-hotfix",
                            "decision": "temporary_hotfix",
                            "actor_github_id": "123",
                            "actor_github_login": "director",
                            "release_start": "promote",
                            "decided_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                        }
                    )
                )
                decision = decision.model_copy(
                    update={
                        "record_id": "client-rejection",
                        "decision": "changes_requested",
                        "release_start": "rollback",
                        "decided_at": datetime.now(UTC).isoformat(),
                    }
                )
                self.store.write_release_review_decision_record(decision)
            commands.clear()
            self.assertTrue(self.advance())
        self.assertTrue(changed)
        self.assertEqual(schedules["billing"], original)
        self.assertIn("./old-billing", commands)
        from control_plane.generic_web_rollback_http import GENERIC_WEB_ROLLBACK_ROUTE

        reservation = self.store.read_idempotency_record(
            scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_ROLLBACK_ROUTE,
            idempotency_key=releases._step_key(decision, releases.ClientReleaseStep("rollback", 1)),
        )
        assert reservation is not None
        self.assertEqual(reservation.state, "completed")
        self.assertEqual(reservation.response_payload["result"]["rollback_status"], "pass")

    def test_standing_failed_testing_cannot_starve_admitted_drill_inverse(self) -> None:
        self.standing_testing_change_after_admission(new_candidate=False)

    def test_standing_new_candidate_waits_for_admitted_drill_inverse(self) -> None:
        self.standing_testing_change_after_admission(new_candidate=True)

    def test_supported_baseline_restore_does_not_pin_next_standing_candidate(self) -> None:
        self.standing_testing_change_after_admission(new_candidate=True, production_changed=True)

    def test_missing_old_target_does_not_pin_independently_qualified_next_drill(self) -> None:
        self.standing_testing_change_after_admission(new_candidate=True, prior_target_missing=True)

    def standing_testing_change_after_admission(
        self,
        *,
        new_candidate: bool,
        production_changed: bool = False,
        prior_target_missing: bool = False,
    ) -> None:
        from tests.support.lane_builds import LaneBuildGitHub

        next_commit = "d" * 40
        next_digest = "sha256:" + "c" * 64
        self.enterContext(
            patch(
                "control_plane.product_reconcile.resolve_build_provenance_transport",
                return_value=LaneBuildGitHub(
                    self.profile.repository,
                    self.profile.image.repository,
                    {
                        BASE: "sha256:" + "f" * 64,
                        HEAD: "sha256:" + "b" * 64,
                        next_commit: next_digest,
                    },
                    tags={HEAD: ("first-candidate",), next_commit: ("next-candidate",)},
                ),
            )
        )
        baseline = self.store.read_environment_inventory(
            context_name=self.context, instance_name="prod"
        )
        if prior_target_missing:
            initial_testing = self.store.read_environment_inventory(
                context_name=self.context, instance_name="testing"
            )
            assert initial_testing.runtime_identity is not None
            self.write_qualified_testing_inventory(
                initial_testing.model_copy(
                    update={
                        "runtime_identity": initial_testing.runtime_identity.model_copy(
                            update={
                                "image_reference": self.profile.image.repository
                                + ":first-candidate"
                            }
                        )
                    }
                )
            )
        self.switch("director_standing")
        self.advance()
        self.capture()
        self.advance()
        decision = self.store.list_release_review_decision_records(product=self.profile.product)[0]
        if production_changed:
            from control_plane.workflows.generic_web_rollback import (
                execute_generic_web_rollback,
            )
            from control_plane.contracts.generic_web_rollback import GenericWebRollbackPlanRequest

            restored = execute_generic_web_rollback(
                control_plane_root=self.root,
                record_store=self.store,
                request=GenericWebRollbackPlanRequest(
                    product=self.profile.product,
                    rollback_deployment_record_id=baseline.deployment_record_id,
                ),
                deploy_provider=self.provider,
            )
            self.assertEqual(restored.rollback_status, "pass")
            self.assertEqual(self.store.list_held_provider_target_reservations(), ())
        if prior_target_missing:
            previous = self.store.read_deployment_record(baseline.deployment_record_id)
            self.store.write_deployment_record(
                previous.model_copy(
                    update={"deploy": previous.deploy.model_copy(update={"status": "fail"})}
                )
            )
        inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        testing = self.store.read_deployment_record(inventory.deployment_record_id)
        if new_candidate:
            assert inventory.runtime_identity is not None
            self.write_qualified_testing_inventory(
                inventory.model_copy(
                    update={
                        "runtime_identity": inventory.runtime_identity.model_copy(
                            update={
                                "artifact_id": self.profile.image.repository + "@" + next_digest,
                                "source_git_ref": next_commit,
                                "image_reference": self.profile.image.repository
                                + ":next-candidate",
                            }
                        )
                    }
                )
            )
        else:
            self.store.write_deployment_record(
                testing.model_copy(
                    update={
                        "post_deploy_update": PostDeployUpdateEvidence(
                            attempted=True, status="fail"
                        )
                    }
                )
            )
        if production_changed or prior_target_missing:
            self.assertTrue(self.advance())
            next_decision = self.store.list_release_review_decision_records(
                product=self.profile.product
            )[0]
            self.assertNotEqual(next_decision.record_id, decision.record_id)
            self.assertEqual(next_decision.checklist.candidate.source_commit, next_commit)
            self.capture()
            self.assertTrue(self.advance())
            self.assertTrue(self.advance())
            self.assertTrue(self.advance())
            self.capture()
            self.assertTrue(self.advance())
            run = read_client_release_run(
                store=self.store, profile=self.profile, decision=next_decision
            )
            assert run is not None
            self.assertEqual(run.state, "passed")
            return
        self.assertTrue(self.advance())
        self.assertEqual(
            self.store.list_release_review_decision_records(product=self.profile.product),
            (decision,),
        )
        assert self.provider.running is not None
        self.assertEqual(
            self.provider.running.artifact_id, decision.checklist.production.artifact_id
        )
        run = read_client_release_run(store=self.store, profile=self.profile, decision=decision)
        assert run is not None
        self.assertEqual(
            next(
                step for step in run.steps if step.step == ClientReleaseStep("rollback", 1).name
            ).status,
            "pass",
        )
        if new_candidate:
            # The settled inverse no longer pins an obsolete A's fresh second attempt.
            self.assertTrue(self.advance())
            latest = self.store.list_release_review_decision_records(product=self.profile.product)[
                0
            ]
            self.assertNotEqual(latest.record_id, decision.record_id)
            self.assertEqual(latest.checklist.candidate.source_commit, next_commit)
        else:
            self.assertEqual(self.advance(), ())
            self.store.write_deployment_record(testing)
            self.assertTrue(self.advance())

    def test_each_drill_failure_stops_remaining_steps_and_does_not_count_as_passed(self) -> None:
        for failure_step in ("backup-1", "promote-1", "rollback-1", "backup-2", "promote-2"):
            with self.subTest(failure_step=failure_step):
                self.setUp()
                self.switch("promote_with_rollback_drill")
                accepted = self.accept()
                (backup_id,) = self.advance()
                if failure_step == "backup-1":
                    backup = self.store.read_verireel_prod_backup_gate_operation_record(backup_id)
                    self.store.write_verireel_prod_backup_gate_operation_record(
                        backup.model_copy(
                            update={
                                "status": "fail",
                                "error_message": "Backup capture failed.",
                                "finished_at": datetime.now(UTC).isoformat(),
                            }
                        )
                    )
                else:
                    self.capture()
                    if failure_step == "promote-1":
                        self.provider.fail_artifact = accepted.checklist.candidate.artifact_id
                    self.advance()
                    if failure_step != "promote-1":
                        if failure_step == "rollback-1":
                            self.provider.fail_artifact = accepted.checklist.production.artifact_id
                        self.advance()
                        if failure_step != "rollback-1":
                            (second_backup_id,) = self.advance()
                            if failure_step == "backup-2":
                                backup = self.store.read_verireel_prod_backup_gate_operation_record(
                                    second_backup_id
                                )
                                self.store.write_verireel_prod_backup_gate_operation_record(
                                    backup.model_copy(
                                        update={
                                            "status": "fail",
                                            "error_message": "Backup capture failed.",
                                            "finished_at": datetime.now(UTC).isoformat(),
                                        }
                                    )
                                )
                            else:
                                self.capture()
                                self.provider.fail_artifact = (
                                    accepted.checklist.candidate.artifact_id
                                )
                                self.advance()
                run = read_client_release_run(
                    store=self.store, profile=self.profile, decision=accepted
                )
                assert run is not None
                self.assertEqual(run.state, "stopped")
                failed_index = next(
                    index for index, step in enumerate(run.steps) if step.step == failure_step
                )
                self.assertEqual(run.steps[failed_index].status, "fail")
                self.assertTrue(
                    all(step.status == "not_started" for step in run.steps[failed_index + 1 :])
                )
                deployed = list(self.provider.deployed_artifacts)
                self.assertEqual(self.advance(), ())
                self.assertEqual(self.provider.deployed_artifacts, deployed)
                self.assertEqual(
                    release_start_for_acceptance(
                        store=self.store,
                        profile=self.store.read_product_profile_record(self.profile.product),
                    ),
                    "promote_with_rollback_drill",
                )
                if failure_step in {"promote-1", "promote-2"}:
                    self.assertTrue(
                        any(
                            record.rollback.status == "pass"
                            for record in self.store.list_promotion_records()
                        )
                    )
                    assert self.provider.running is not None
                    self.assertEqual(
                        self.provider.running.artifact_id, accepted.checklist.production.artifact_id
                    )

    def test_hold_before_drill_provider_write_stops_forward_effects(self) -> None:
        self.switch("promote_with_rollback_drill")
        accepted = self.accept()
        self.advance()
        self.capture()
        self.advance()
        resolve = self.provider.resolve_deploy_target

        def revoke(**kwargs: Any) -> GenericWebResolvedDeployTarget:
            target = resolve(**kwargs)
            self.switch("held")
            return target

        with patch.object(self.provider, "resolve_deploy_target", side_effect=revoke):
            self.advance()
        self.assertEqual(
            self.provider.deployed_artifacts, [accepted.checklist.candidate.artifact_id]
        )
        run = read_client_release_run(store=self.store, profile=self.profile, decision=accepted)
        assert run is not None
        self.assertEqual(run.steps[2].status, "fail")
        self.assertEqual(run.steps[3].status, "not_started")

    def test_missing_drill_target_blocks_before_backup_and_is_visible(self) -> None:
        self.switch("promote_with_rollback_drill")
        accepted = self.accept()
        baseline = self.store.list_deployment_records(
            context_name=self.context, instance_name="prod"
        )[0]
        self.store.write_deployment_record(
            baseline.model_copy(
                update={"deploy": baseline.deploy.model_copy(update={"status": "fail"})}
            )
        )
        self.assertEqual(self.advance(), ())
        self.assertEqual(self.store.list_verireel_prod_backup_gate_operation_records(), ())
        self.assertEqual(self.provider.deployed_artifacts, [])
        run = read_client_release_run(store=self.store, profile=self.profile, decision=accepted)
        assert run is not None
        self.assertIn("rollback target", run.blocked_reason)

    def test_same_digest_with_another_source_ref_is_not_the_drill_target(self) -> None:
        self.switch("promote_with_rollback_drill")
        accepted = self.accept()
        baseline = self.store.list_deployment_records(
            context_name=self.context, instance_name="prod"
        )[0]
        self.store.write_deployment_record(
            baseline.model_copy(
                update={
                    "record_id": "wrong-source-ref",
                    "source_git_ref": "c" * 40,
                    "deploy": baseline.deploy.model_copy(
                        update={"finished_at": datetime.now(UTC).isoformat()}
                    ),
                }
            )
        )
        self.advance()
        self.capture()
        self.advance()
        self.advance()
        self.assertEqual(self.review().checklist_digest, accepted.checklist_digest)
        inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="prod"
        )
        self.assertEqual(inventory.promotion_record_id, "promotion-previous-release")
        self.assertEqual(inventory.promoted_from_instance, "testing")

    def test_drill_health_failure_and_unknown_effect_stop_repromotion(self) -> None:
        for outcome in ("unhealthy", "unknown"):
            with self.subTest(outcome=outcome):
                self.setUp()
                self.switch("promote_with_rollback_drill")
                accepted = self.accept()
                self.advance()
                self.capture()
                self.advance()
                execute = self.provider.execute_artifact_deploy

                def disconnect(**kwargs: Any) -> None:
                    execute(**kwargs)
                    raise TimeoutError("Rollback may still be running.")

                if outcome == "unhealthy":
                    with patch(
                        "control_plane.workflows.generic_web_promotion.wait_for_runtime_identity_healthcheck_with_retry",
                        side_effect=click.ClickException("Rollback health failed"),
                    ):
                        self.advance()
                else:
                    with patch.object(
                        self.provider, "execute_artifact_deploy", side_effect=disconnect
                    ):
                        self.advance()
                run = read_client_release_run(
                    store=self.store, profile=self.profile, decision=accepted
                )
                assert run is not None
                self.assertEqual(run.state, "stopped")
                self.assertEqual(
                    run.steps[2].status,
                    "fail" if outcome == "unhealthy" else "reconciliation_required",
                )
                self.assertEqual(run.steps[3].status, "not_started")
                deployed = list(self.provider.deployed_artifacts)
                self.assertEqual(self.advance(), ())
                self.assertEqual(self.provider.deployed_artifacts, deployed)

    def test_repromotion_failure_can_recover_after_releases_are_held(self) -> None:
        self.switch("promote_with_rollback_drill")
        accepted = self.accept()
        self.advance()
        self.capture()
        self.advance()
        self.advance()
        self.advance()
        self.capture()
        self.provider.fail_artifact = accepted.checklist.candidate.artifact_id
        execute = self.provider.execute_artifact_deploy

        def hold_after_effect(**kwargs: Any) -> None:
            try:
                execute(**kwargs)
            finally:
                self.switch("held")

        with patch.object(self.provider, "execute_artifact_deploy", side_effect=hold_after_effect):
            self.advance()
        assert self.provider.running is not None
        self.assertEqual(
            self.provider.running.artifact_id, accepted.checklist.production.artifact_id
        )
        self.assertTrue(
            any(record.rollback.status == "pass" for record in self.store.list_promotion_records())
        )
        run = read_client_release_run(store=self.store, profile=self.profile, decision=accepted)
        assert run is not None
        self.assertEqual(run.steps[-1].status, "fail")

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
        failure = run.steps[1].failure
        assert failure is not None
        self.assertTrue(failure.trace_id)
        assert promotion.failure is not None
        self.assertEqual(failure.code, promotion.failure.code)
        self.assertIn("health", failure.reason.lower())
        self.assertEqual(failure.record_id, promotion.deployment_record_id)
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

    def test_standing_release_drills_once_then_promotes_the_next_candidate(self) -> None:
        from tests.support.lane_builds import LaneBuildGitHub

        next_commit = "d" * 40
        next_digest = "sha256:" + "c" * 64
        first_tag = "first-candidate"
        next_tag = "next-candidate"
        transport = LaneBuildGitHub(
            self.profile.repository,
            self.profile.image.repository,
            {BASE: "sha256:" + "f" * 64, HEAD: "sha256:" + "b" * 64, next_commit: next_digest},
            tags={HEAD: (first_tag,), next_commit: (next_tag,)},
        )
        self.enterContext(
            patch(
                "control_plane.product_reconcile.resolve_build_provenance_transport",
                return_value=transport,
            )
        )
        # Application targets preserve a declared immutable Build tag for rollback.
        inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        assert inventory.runtime_identity is not None
        self.write_qualified_testing_inventory(
            inventory.model_copy(
                update={
                    "runtime_identity": inventory.runtime_identity.model_copy(
                        update={"image_reference": self.profile.image.repository + ":" + first_tag}
                    )
                }
            )
        )
        self.switch("director_standing")
        (backup_id,) = self.advance()
        first = self.store.list_release_review_decision_records(product=self.profile.product)[0]
        self.assertEqual(first.release_start, "promote_with_rollback_drill")
        self.assertEqual(
            release_start_for_acceptance(
                store=self.store,
                profile=self.store.read_product_profile_record(self.profile.product),
            ),
            "promote_with_rollback_drill",
        )
        self.capture()
        self.advance()
        # The first promotion is not proof of a completed drill.
        self.assertEqual(
            release_start_for_acceptance(
                store=self.store,
                profile=self.store.read_product_profile_record(self.profile.product),
            ),
            "promote_with_rollback_drill",
        )
        self.advance()
        (second_backup_id,) = self.advance()
        self.assertNotEqual(second_backup_id, backup_id)
        self.capture()
        self.advance()
        run = read_client_release_run(store=self.store, profile=self.profile, decision=first)
        assert run is not None
        self.assertEqual(run.state, "passed", run.model_dump())
        self.assertTrue(all(step.status == "pass" for step in run.steps))
        self.assertEqual(
            self.provider.deployed_artifacts,
            [
                first.checklist.candidate.artifact_id,
                first.checklist.production.artifact_id,
                first.checklist.candidate.artifact_id,
            ],
        )
        self.assertEqual(self.advance(), ())

        inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        assert inventory.runtime_identity is not None and inventory.artifact_identity is not None
        next_artifact = self.profile.image.repository + "@" + next_digest
        self.write_qualified_testing_inventory(
            inventory.model_copy(
                update={
                    "source_git_ref": next_commit,
                    "artifact_identity": inventory.artifact_identity.model_copy(
                        update={"artifact_id": next_artifact}
                    ),
                    "runtime_identity": inventory.runtime_identity.model_copy(
                        update={
                            "source_git_ref": next_commit,
                            "artifact_id": next_artifact,
                            "image_reference": self.profile.image.repository + ":" + next_tag,
                        }
                    ),
                }
            )
        )
        self.assertEqual(len(self.advance()), 1)
        later = self.store.list_release_review_decision_records(product=self.profile.product)[0]
        self.assertEqual(later.acceptance_source, "director_standing")
        self.assertEqual(later.release_start, "promote")
        self.assertEqual(later.checklist.candidate.source_commit, next_commit)
        self.capture()
        self.advance()
        later_run = read_client_release_run(store=self.store, profile=self.profile, decision=later)
        assert later_run is not None
        self.assertEqual(later_run.state, "passed", later_run.model_dump())
        self.assertFalse(later_run.rollback_drill)
        self.assertEqual(
            self.provider.deployed_artifacts,
            [
                first.checklist.candidate.artifact_id,
                first.checklist.production.artifact_id,
                first.checklist.candidate.artifact_id,
                next_artifact,
            ],
        )
        self.assertEqual(self.advance(), ())

    def test_failed_standing_repromotion_stops_and_a_new_candidate_still_drills(self) -> None:
        self.switch("director_standing")
        self.advance()
        first = self.store.list_release_review_decision_records(product=self.profile.product)[0]
        self.capture()
        self.advance()
        self.advance()
        self.advance()
        self.capture()
        self.provider.fail_artifact = first.checklist.candidate.artifact_id
        self.advance()
        run = read_client_release_run(store=self.store, profile=self.profile, decision=first)
        assert run is not None
        self.assertEqual(run.state, "stopped", run.model_dump())
        failed = next(
            step for step in run.steps if step.step == ClientReleaseStep("promote", 2).name
        )
        self.assertEqual(failed.status, "fail")
        deployed = list(self.provider.deployed_artifacts)
        self.assertEqual(self.advance(), ())
        self.assertEqual(self.provider.deployed_artifacts, deployed)
        self.assertEqual(
            self.store.list_release_review_decision_records(product=self.profile.product), (first,)
        )
        self.assertEqual(
            release_start_for_acceptance(
                store=self.store,
                profile=self.store.read_product_profile_record(self.profile.product),
            ),
            "promote_with_rollback_drill",
        )
        inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        assert inventory.runtime_identity is not None
        next_commit = "d" * 40
        self.write_qualified_testing_inventory(
            inventory.model_copy(
                update={
                    "source_git_ref": next_commit,
                    "runtime_identity": inventory.runtime_identity.model_copy(
                        update={"source_git_ref": next_commit}
                    ),
                }
            )
        )
        self.assertEqual(len(self.advance()), 1)
        latest = self.store.list_release_review_decision_records(product=self.profile.product)[0]
        self.assertNotEqual(latest.record_id, first.record_id)
        self.assertEqual(latest.checklist.candidate.source_commit, next_commit)
        self.assertEqual(latest.release_start, "promote_with_rollback_drill")

    def test_late_standing_writer_keeps_publication_and_admitted_backup_authorized(self) -> None:
        self.switch("director_standing")
        create = self.store.create_release_review_decision_record_if_absent
        winner: ReleaseReviewDecisionRecord | None = None

        def race(
            contender: ReleaseReviewDecisionRecord, **_options: Any
        ) -> ReleaseReviewDecisionRecord:
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
        self.write_qualified_testing_inventory(
            prior_inventory.model_copy(
                update={
                    "runtime_identity": prior_inventory.runtime_identity.model_copy(
                        update={"source_git_ref": "d" * 40}
                    )
                }
            )
        )
        self.accept()
        self.write_qualified_testing_inventory(prior_inventory)
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
        "control_plane.workflows.production_backup_gate_operation_worker._utc_now_timestamp",
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
        self.write_qualified_testing_inventory(
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
        self.write_qualified_testing_inventory(
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
                    "source": "metadata update",
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
            self.write_qualified_testing_inventory(
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

    def hold_testing_source(self) -> None:
        from control_plane.contracts.provider_target_origin import ProviderTargetOrigin
        from control_plane.generic_web_source_qualification import generic_web_source_target_key

        target = self.store.read_provider_target_record(
            context_name=self.context, instance_name="testing"
        )
        inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        deployment = self.store.read_deployment_record(inventory.deployment_record_id)
        reservation = self.store.reserve_mutation(
            scope="testing-source",
            route_path="/v1/drivers/generic-web/deploy",
            idempotency_key="uncertain-original",
            request_fingerprint="a" * 64,
            lease_owner="testing-worker",
            reconciliation_key="testing-source-original",
            provider_target_key=generic_web_source_target_key(
                target, deployment.deploy.target_type
            ),
            provider_target_origin=ProviderTargetOrigin(
                product=self.profile.product,
                context=self.context,
                instance="testing",
                provider_id=target.provider_id,
                target_type=deployment.deploy.target_type,
                target_id=target.target_id,
            ),
        )
        self.assertEqual(reservation.status, "acquired")

    def test_default_input_promotion_rechecks_the_selected_build_at_final_checkpoint(self) -> None:
        from control_plane.generic_web_promotion_provider_adapter import (
            GenericWebProdPromotionProviderMutationAdapter,
        )
        from control_plane.generic_web_promotion_http import GenericWebProdPromotionEnvelope
        from control_plane.workflows.generic_web_promotion import GenericWebProdPromotionRequest
        from control_plane.provider_operations import run_durable_provider_operation
        from control_plane.generic_web_source_qualification import GenericWebPromotionSourceRefused

        self.accept()
        self.advance()
        self.capture()
        backup = self.store.list_verireel_prod_backup_gate_operation_records()[0].backup_record_id
        original = self.store.checkpoint_mutation_provider_effect
        inserted = False

        def checkpoint(**kwargs: Any) -> Any:
            nonlocal inserted
            if not inserted:
                inventory = self.store.read_environment_inventory(
                    context_name=self.context, instance_name="testing"
                )
                assert inventory.runtime_identity is not None
                self.write_qualified_testing_inventory(
                    inventory.model_copy(
                        update={
                            "runtime_identity": inventory.runtime_identity.model_copy(
                                update={
                                    "artifact_id": self.profile.image.repository
                                    + "@sha256:"
                                    + "d" * 64,
                                    "source_git_ref": "d" * 40,
                                }
                            ),
                        }
                    )
                )
                inserted = True
            return original(**kwargs)

        adapter = GenericWebProdPromotionProviderMutationAdapter(
            control_plane_root=self.root,
            record_store=self.store,
            promotion_request=GenericWebProdPromotionEnvelope(
                product=self.profile.product,
                promotion=GenericWebProdPromotionRequest(
                    product=self.profile.product, backup_record_id=backup
                ),
            ),
            profile=self.profile,
            lane=next(lane for lane in self.profile.lanes if lane.instance == "prod"),
            trace_id="default-input-race",
            validate_before_effect=lambda _: None,
            deploy_provider=self.provider,
        )
        with patch.object(
            self.store, "checkpoint_mutation_provider_effect", side_effect=checkpoint
        ):
            with self.assertRaises(GenericWebPromotionSourceRefused):
                run_durable_provider_operation(
                    store=self.store,
                    scope="default-input-promotion",
                    route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
                    idempotency_key="selected-build",
                    request_fingerprint="f" * 64,
                    lease_owner="default-worker",
                    response_trace_id="default-input-race",
                    adapter=adapter,
                )
        self.assertTrue(inserted)
        self.assertEqual(self.provider.deployed_artifacts, [])

    def test_testing_hold_after_cached_admission_refuses_at_first_effect(self) -> None:
        accepted = self.accept()
        self.advance()
        self.capture()
        original = self.store.checkpoint_mutation_provider_effect
        inserted = False

        def checkpoint(**kwargs: Any) -> Any:
            nonlocal inserted
            if not inserted:
                self.hold_testing_source()
                inserted = True
            return original(**kwargs)

        with patch.object(
            self.store, "checkpoint_mutation_provider_effect", side_effect=checkpoint
        ):
            self.assertEqual(self.advance(), ())
        self.assertTrue(inserted)
        self.assertEqual(self.provider.deployed_artifacts, [])
        self.assertIsNone(
            self.store.read_idempotency_record(
                scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
                route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
                idempotency_key=f"{accepted.record_id}:promote-1",
            )
        )
        self.assertEqual(
            self.store.list_release_review_decision_records(product=self.profile.product),
            (accepted,),
        )

    def test_testing_hold_after_first_effect_keeps_forward_and_inverse_usable(self) -> None:
        accepted = self.accept()
        self.advance()
        self.capture()
        self.provider.fail_artifact = accepted.checklist.candidate.artifact_id
        original = self.provider.execute_artifact_deploy

        def execute(**kwargs: Any) -> None:
            try:
                original(**kwargs)
            finally:
                if len(self.provider.deployed_artifacts) == 1:
                    self.hold_testing_source()

        with patch.object(self.provider, "execute_artifact_deploy", side_effect=execute):
            self.advance()
        self.assertEqual(
            self.provider.deployed_artifacts,
            [accepted.checklist.candidate.artifact_id, accepted.checklist.production.artifact_id],
        )
        self.assertEqual(self.store.list_promotion_records()[0].rollback.status, "pass")

    def test_source_loss_during_target_resolution_keeps_accepted_step_retryable(self) -> None:
        self.source_loss_before_effect(during_review=False)

    def test_source_loss_during_review_keeps_accepted_step_retryable(self) -> None:
        self.source_loss_before_effect(during_review=True)

    def source_loss_before_effect(self, *, during_review: bool) -> None:
        accepted = self.accept()
        self.advance()
        self.capture()
        inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        testing = self.store.read_deployment_record(inventory.deployment_record_id)
        original = self.provider.resolve_deploy_target
        reviews = 0

        def fail_source() -> None:
            self.store.write_deployment_record(
                testing.model_copy(
                    update={
                        "post_deploy_update": PostDeployUpdateEvidence(
                            attempted=True, status="fail"
                        )
                    }
                )
            )

        def lose_source(**kwargs: Any) -> GenericWebResolvedDeployTarget:
            target = original(**kwargs)
            if not during_review:
                fail_source()
            return target

        def review(**kwargs: object) -> ReleaseReviewStatus:
            nonlocal reviews
            reviews += 1
            if during_review and reviews == 2:
                fail_source()
                return ReleaseReviewStatus(unavailable_reason="candidate_identity_missing")
            return self.review(**kwargs)

        with (
            patch.object(self.provider, "resolve_deploy_target", side_effect=lose_source),
            patch("control_plane.client_release.current_release_review", side_effect=review),
            patch(
                "control_plane.production_delivery_monitor.record_production_delivery_failure"
            ) as failure,
        ):
            self.assertEqual(self.advance(), ())
            failure.assert_not_called()
        run = read_client_release_run(store=self.store, profile=self.profile, decision=accepted)
        assert run is not None
        promotion = next(
            step for step in run.steps if step.step == ClientReleaseStep("promote", 1).name
        )
        self.assertEqual(promotion.status, "not_started")
        self.assertEqual(self.provider.deployed_artifacts, [])
        self.store.write_deployment_record(testing)
        self.assertTrue(self.advance())
        self.assertEqual(
            self.store.list_release_review_decision_records(product=self.profile.product),
            (accepted,),
        )
        self.assertEqual(
            self.provider.deployed_artifacts, [accepted.checklist.candidate.artifact_id]
        )

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
        failure = run.steps[1].failure
        assert failure is not None
        reservation = self.store.read_idempotency_record(
            scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
            idempotency_key=f"{accepted.record_id}:promote-1",
        )
        assert reservation is not None
        self.assertEqual(failure.code, reservation.response_payload["result"]["error_code"])
        self.assertIn("before deployment", failure.reason)
        self.assertEqual(failure.trace_id, reservation.response_trace_id)
        self.assertEqual(failure.record_id, reservation.record_id)
        self.assertEqual(self.advance(), ())
        self.assertEqual(self.provider.deployed_artifacts, [])

    def test_busy_drill_target_does_not_report_another_operation_as_queued(self) -> None:
        self.switch("promote_with_rollback_drill")
        accepted = self.accept()
        self.advance()
        self.capture()
        self.advance()
        promotion = self.store.read_idempotency_record(
            scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
            idempotency_key=f"{accepted.record_id}:promote-1",
        )
        assert promotion is not None
        origin = self.store.read_provider_target_origin(promotion)
        assert origin is not None
        competing = self.store.reserve_mutation(
            scope="manual-deploy",
            route_path="/v1/admin/generic-web/deploy",
            idempotency_key="manual-deploy",
            request_fingerprint="f" * 64,
            lease_owner="admin-worker",
            reconciliation_key=promotion.reconciliation_key,
            provider_target_key=promotion.provider_target_key,
            provider_target_origin=origin.origin,
        )
        self.assertEqual(competing.status, "acquired")
        effects = list(self.provider.deployed_artifacts)
        self.assertEqual(self.advance(), ())
        self.assertEqual(self.provider.deployed_artifacts, effects)
        run = read_client_release_run(store=self.store, profile=self.profile, decision=accepted)
        assert run is not None
        self.assertEqual(run.steps[2].status, "not_started")

    def test_delivery_monitor_rechecks_a_running_step_when_its_lease_expires(self) -> None:
        from unittest.mock import Mock
        from control_plane.production_delivery_monitor import monitor_production_delivery_once

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

        def monitor(now: datetime) -> None:
            monitor_production_delivery_once(
                self.store,
                observed_at=now.isoformat(),
                transport_factory=lambda _store, _profile: Mock(
                    get_json=Mock(
                        side_effect=lambda path: (
                            {"sha": HEAD} if "/commits/" in path else {"status": "behind"}
                        )
                    )
                ),
            )

        now = datetime.now(UTC)
        monitor(now)
        self.assertFalse(self.store.list_public_ingress_incident_records(status="open"))
        future = now + timedelta(seconds=600)
        with patch("control_plane.client_release.datetime") as clock:
            clock.now.return_value = future
            monitor(future)
            run = read_client_release_run(store=self.store, profile=self.profile, decision=accepted)
        assert run is not None
        incidents = self.store.list_public_ingress_incident_records(status="open")
        self.assertEqual(len(incidents), 1)
        self.assertIn(run.steps[-1].status, incidents[0].summary)

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
