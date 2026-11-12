import json
import unittest
from pathlib import Path
from unittest.mock import patch
from typing import Any, Self
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

from control_plane.contracts.artifact_identity import (
    ArtifactBaseImageProvenance,
    ArtifactBuildProvenance,
    ArtifactBuildToolProvenance,
    ArtifactIdentityManifest,
    ArtifactImageReference,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.product_profile_record import ProductOwnerProfile
from control_plane.contracts.release_review import ReleaseDecision
from control_plane.release_review import build_release_review, checklist_digest
from tests.test_odoo_first_empty_inputs import FirstEmptyInputsFixture
from tests.test_release_review import github_read


class AcceptedFirstEmptyTests(FirstEmptyInputsFixture):
    def setUp(self) -> None:
        super().setUp()
        from tempfile import TemporaryDirectory

        cache = TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        self.cached_root = Path(cache.name).resolve()
        from control_plane.contracts.odoo_online_backup import DEFAULT_ODOO_BACKUP_ROOT

        self.runtime = self.runtime.model_copy(
            update={"env": {**self.runtime.env, "ODOO_BACKUP_ROOT": DEFAULT_ODOO_BACKUP_ROOT}}
        )
        self.store.write_runtime_environment_record(self.runtime)
        self.profile = self.profile.model_copy(
            update={"owner": ProductOwnerProfile(github_id="9001", github_login="site-owner")}
        )
        self.store.write_product_profile_record(self.profile)
        from control_plane.product_config import apply_product_config_bundle
        from control_plane.contracts.product_profile_record import (
            FRESH_TESTING_LOCAL_CREDENTIAL_KEYS,
        )

        apply_product_config_bundle(
            record_store=self.store,
            payload={
                "product": self.profile.product,
                "context": self.context,
                "instance": "prod",
                "secrets": [
                    {
                        "binding_key": key,
                        "value": f"fictional-{key.lower()}",
                        "secret_class": "prod_only",
                    }
                    for key in sorted(FRESH_TESTING_LOCAL_CREDENTIAL_KEYS)
                ],
            },
            mode="apply",
            actor="fixture",
            source_label="fixture",
        )
        self.artifact = ArtifactIdentityManifest(
            artifact_id="fictional-candidate",
            source_commit="b" * 40,
            enterprise_base_digest="sha256:base",
            image=ArtifactImageReference(
                repository=self.profile.image.repository, digest="sha256:" + "d" * 64
            ),
            build_provenance=ArtifactBuildProvenance(
                base_images=tuple(
                    ArtifactBaseImageProvenance(
                        role=role,
                        image=ArtifactImageReference(
                            repository=f"example/{role}", digest="sha256:base"
                        ),
                        source_repository=f"example/{role}",
                        source_ref="a" * 40,
                    )
                    for role in ("runtime", "devtools")
                ),
                build_tools=(
                    ArtifactBuildToolProvenance(
                        name="builder", source_repository="example/builder", source_ref="a" * 40
                    ),
                ),
            ),
        )
        self.store.write_artifact_manifest(self.artifact)
        self.store.write_release_tuple_record(
            ReleaseTupleRecord(
                tuple_id="fictional-testing",
                context=self.context,
                channel="testing",
                artifact_id=self.artifact.artifact_id,
                repo_shas={self.profile.repository: self.artifact.source_commit},
                provenance="ship",
                minted_at=self.target.updated_at,
            )
        )

    @staticmethod
    def read(path: str) -> Any:
        if "/commits?" in path:
            return [{"sha": parse_qs(urlsplit(path).query)["sha"][0]}]
        result = github_read(path)
        if isinstance(result, list):
            for pull in result:
                pull["base"]["repo"]["full_name"] = path.split("/commits/")[0].removeprefix(
                    "/repos/"
                )
                pull["merge_commit_sha"] = path.split("/commits/")[1].split("/")[0]
        return result

    def test_review_binds_actual_absence_and_initial_runtime_without_exemption(self) -> None:
        review = build_release_review(store=self.store, profile=self.profile, read=self.read)
        self.assertFalse(review.checklist_complete)
        review = self.prepare_initial_review()
        self.assertTrue(review.required)
        self.assertFalse(review.approved)
        self.assertTrue(review.checklist_complete, review.blockers)
        assert review.checklist is not None
        production = review.checklist.production.model_dump(mode="json")
        self.assertEqual(production["state"], "absent")
        self.assertNotIn("artifact_id", production)
        self.assertEqual(review.checklist.candidate.artifact_id, self.artifact.artifact_id)
        self.probe.assert_not_called()
        before = checklist_digest(review.checklist)
        self.store.write_runtime_environment_record(
            self.runtime.model_copy(update={"env": {**self.runtime.env, "ODOO_WORKERS": "3"}})
        )
        changed = build_release_review(store=self.store, profile=self.profile, read=self.read)
        assert changed.checklist is not None
        self.assertNotEqual(checklist_digest(changed.checklist), before)

    def prepare_initial_review(self) -> Any:
        from control_plane.release_request import record_release_request

        review = build_release_review(store=self.store, profile=self.profile, read=self.read)
        self.profile = record_release_request(
            store=self.store,
            profile=self.profile,
            review=review,
            source="director_request",
            reason="Initial version is ready for review",
            decision_reference="https://github.com/example/product/issues/1#issuecomment-1",
            actor="fixture",
            control_plane_root=Path("."),
            initial_version_test_notes="Open the initial site, add a fictional contact and verify the follow-up reminder.",
        )
        return build_release_review(store=self.store, profile=self.profile, read=self.read)

    def test_initial_notes_bind_whole_version_without_requiring_ancestor_change_notes(self) -> None:
        from control_plane.artifact_release_inputs import verified_release_inputs

        with patch(
            "control_plane.release_review.read_release_changes",
            side_effect=AssertionError("No historical comparison"),
        ):
            incomplete = build_release_review(
                store=self.store, profile=self.profile, read=self.read
            )
            self.assertFalse(incomplete.checklist_complete)
            complete = self.prepare_initial_review()
        self.assertTrue(complete.checklist_complete, complete.blockers)
        initial = complete.checklist.initial_version
        self.assertIsNotNone(initial)
        self.assertEqual(initial.runtime_values, self.runtime.env)
        inputs, unknown = verified_release_inputs(self.artifact)
        self.assertFalse(unknown)
        self.assertTrue(
            {(source.name, source.repository, source.commit) for source in inputs.values()}
            <= {(source.name, source.repository, source.commit) for source in initial.sources}
        )
        self.store.write_artifact_manifest(
            self.artifact.model_copy(update={"odoo_install_modules": ("website",)})
        )
        changed = build_release_review(store=self.store, profile=self.profile, read=self.read)
        self.assertFalse(changed.checklist_complete)
        assert changed.checklist is not None and changed.checklist.initial_version is not None
        self.assertFalse(changed.checklist.initial_version.client_test_notes)
        self.assertNotEqual(changed.checklist_digest, complete.checklist_digest)

    def test_http_initial_notes_prepare_review_then_only_exact_human_acceptance_starts_release(
        self,
    ) -> None:
        import asyncio
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
        from tests.http_app_test_support import (
            _github_oauth_config,
            _browser_mutation_headers,
            _RejectingVerifier,
            _local_operator_bearer_config,
        )
        from control_plane.service_auth import LaunchplaneAuthzPolicy
        from tests.support.http import request
        from tests.test_product_review import _human

        self.profile = self.profile.model_copy(update={"release_on_acceptance": "promote"})
        self.store.write_product_profile_record(self.profile)
        sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            record_store_factory=lambda: self.store,
            bearer_identity_config=_local_operator_bearer_config(),
            human_session_manager=sessions,
            authz_policy=LaunchplaneAuthzPolicy(),
        )

        def browser_headers() -> dict[str, str]:
            session = sessions.issue(
                _human(
                    login=self.profile.owner.github_login,
                    github_id=int(self.profile.owner.github_id),
                )
            )
            return _browser_mutation_headers(sessions, session)

        async def exercise() -> None:
            initial = build_release_review(store=self.store, profile=self.profile, read=self.read)
            payload = {
                "product": self.profile.product,
                "checklist_digest": initial.checklist_digest,
                "decision": "accepted",
            }
            refused = await request(
                app,
                "POST",
                "/v1/release-review/decisions",
                headers=browser_headers(),
                payload=payload,
            )
            self.assertEqual(refused.status_code, 409)
            self.assertFalse(
                self.store.list_release_review_decision_records(product=self.profile.product)
            )
            preparation = {
                "product": self.profile.product,
                "checklist_digest": initial.checklist_digest,
                "reason": "The complete first version is ready",
                "decision_reference": "https://github.com/example/product/issues/1#issuecomment-1",
                "initial_version_test_notes": "Open the initial site and verify a fictional contact and follow-up.",
            }
            publication.side_effect = [False, True]
            prepared = await request(
                app,
                "POST",
                "/v1/release-review/requests",
                headers={"Authorization": "Bearer local-operator-token"},
                payload=preparation,
            )
            self.assertEqual(prepared.status_code, 409, prepared.text)
            readback = await request(
                app,
                "GET",
                f"/v1/release-review?product={self.profile.product}",
                headers={"Authorization": "Bearer local-operator-token"},
            )
            self.assertEqual(readback.status_code, 200, readback.text)
            ready = readback.json()["review"]
            self.assertTrue(ready["checklist_complete"])
            self.assertNotEqual(ready["checklist_digest"], initial.checklist_digest)
            self.assertFalse(
                self.store.list_release_review_decision_records(product=self.profile.product)
            )
            replay = await request(
                app,
                "POST",
                "/v1/release-review/requests",
                headers={"Authorization": "Bearer local-operator-token"},
                payload=preparation,
            )
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(publication.call_count, 2)
            self.assertEqual(replay.json()["review"]["checklist_digest"], ready["checklist_digest"])
            altered = await request(
                app,
                "POST",
                "/v1/release-review/requests",
                headers={"Authorization": "Bearer local-operator-token"},
                payload={**preparation, "initial_version_test_notes": "Changed notes"},
            )
            self.assertEqual(altered.status_code, 409)
            stale = await request(
                app,
                "POST",
                "/v1/release-review/decisions",
                headers=browser_headers(),
                payload=payload,
            )
            self.assertEqual(stale.status_code, 409)
            from control_plane.release_review import ReleaseEvidenceUnavailable

            with patch(
                "control_plane.client_release.release_version",
                side_effect=ReleaseEvidenceUnavailable("production_settings_unavailable"),
            ):
                accepted = await request(
                    app,
                    "POST",
                    "/v1/release-review/decisions",
                    headers=browser_headers(),
                    payload={**payload, "checklist_digest": ready["checklist_digest"]},
                )
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertEqual(
                accepted.json()["review"]["latest_decision"]["release_start"],
                "promote_with_rollback_drill",
            )

        with (
            patch(
                "control_plane.http_app.current_release_review",
                side_effect=lambda **kwargs: build_release_review(
                    store=self.store, profile=kwargs["profile"], read=self.read
                ),
            ),
            patch(
                "control_plane.release_request.publish_release_invitation", return_value=True
            ) as publication,
            patch(
                "control_plane.http_app.publish_release_decision",
                return_value="https://github.com/example/product/issues/1",
            ),
        ):
            asyncio.run(exercise())

    def decide(self, kind: ReleaseDecision = "accepted") -> Any:
        from control_plane.contracts.release_review import ReleaseReviewDecisionRecord
        from control_plane.client_release import release_start_for_acceptance
        from control_plane.timestamps import utc_now_timestamp

        self.profile = self.profile.model_copy(update={"release_on_acceptance": "promote"})
        self.store.write_product_profile_record(self.profile)
        review = build_release_review(store=self.store, profile=self.profile, read=self.read)
        if not review.checklist_complete:
            review = self.prepare_initial_review()
        assert review.checklist is not None
        decision = ReleaseReviewDecisionRecord(
            record_id=f"fictional-acceptance-{len(self.store.list_release_review_decision_records(product=self.profile.product)) + 1}",
            product=self.profile.product,
            checklist=review.checklist,
            checklist_digest=review.checklist_digest,
            decision=kind,
            reason="Revise the initial version" if kind == "changes_requested" else "",
            actor_github_id=self.profile.owner.github_id,
            actor_github_login=self.profile.owner.github_login,
            release_issue_url="https://github.com/example/product/issues/1",
            decided_at=utc_now_timestamp(),
            release_start=release_start_for_acceptance(
                store=self.store, profile=self.profile, checklist=review.checklist
            )
            if kind == "accepted"
            else "",
        )
        self.store.write_release_review_decision_record(decision)
        return decision

    def test_missing_recorded_production_artifact_is_not_original_absence(self) -> None:
        from control_plane.release_review import release_version, ReleaseEvidenceUnavailable

        self.decide()
        self.store.write_release_tuple_record(
            ReleaseTupleRecord(
                tuple_id="fictional-prod",
                context=self.context,
                channel="prod",
                artifact_id=self.artifact.artifact_id,
                repo_shas={self.profile.repository: self.artifact.source_commit},
                provenance="ship",
                minted_at=self.target.updated_at,
            )
        )
        with patch.object(self.store, "read_artifact_manifest", side_effect=FileNotFoundError):
            with self.assertRaises(ReleaseEvidenceUnavailable):
                release_version(store=self.store, profile=self.profile, instance="prod")

    def test_initial_review_separates_managed_keys_from_plain_setup(self) -> None:
        from control_plane.product_config import apply_product_config_bundle

        apply_product_config_bundle(
            record_store=self.store,
            payload={
                "product": self.profile.product,
                "context": self.context,
                "instance": "prod",
                "secrets": [
                    {"binding_key": "ODOO_WORKERS", "value": "2", "secret_class": "prod_only"}
                ],
            },
            mode="apply",
            actor="fixture",
            source_label="fixture",
        )
        accepted = self.decide()
        initial = accepted.checklist.initial_version
        assert initial is not None
        self.assertIn("ODOO_WORKERS", initial.managed_secret_keys)
        self.assertNotIn("ODOO_WORKERS", initial.runtime_values)

    def test_rejected_first_candidate_can_be_reviewed_again(self) -> None:
        self.decide("changes_requested")
        review = build_release_review(store=self.store, profile=self.profile, read=self.read)
        self.assertIsNotNone(review.checklist)
        self.assertFalse(review.approved)
        from control_plane.release_request import record_release_request

        self.profile = record_release_request(
            store=self.store,
            profile=self.profile,
            review=review,
            source="director_request",
            reason="The initial test notes were corrected",
            decision_reference="https://github.com/example/product/issues/1#issuecomment-2",
            actor="fixture",
            control_plane_root=Path("."),
            initial_version_test_notes="Verify the corrected initial contact and follow-up workflow.",
        )
        changed = build_release_review(store=self.store, profile=self.profile, read=self.read)
        self.assertTrue(changed.checklist_complete, changed.blockers)
        self.assertIsNone(changed.latest_decision)
        self.assertNotEqual(changed.checklist_digest, review.checklist_digest)

    def test_standing_acceptance_waits_for_complete_initial_notes_and_binds_the_prepared_version(
        self,
    ) -> None:
        from control_plane.client_release import _record_standing_acceptance

        self.profile = self.profile.model_copy(
            update={"release_on_acceptance": "director_standing"}
        )
        self.store.write_product_profile_record(self.profile)
        with patch(
            "control_plane.client_release.current_release_review",
            side_effect=lambda **_: build_release_review(
                store=self.store, profile=self.profile, read=self.read
            ),
        ):
            _record_standing_acceptance(self.store, Path("."), self.profile)
            self.assertFalse(
                self.store.list_release_review_decision_records(product=self.profile.product)
            )
            prepared = self.prepare_initial_review()
            _record_standing_acceptance(self.store, Path("."), self.profile)
        (accepted,) = self.store.list_release_review_decision_records(product=self.profile.product)
        self.assertEqual(accepted.checklist_digest, prepared.checklist_digest)
        self.assertEqual(accepted.acceptance_source, "director_standing")
        self.assertEqual(accepted.release_start, "promote_with_rollback_drill")
        self.probe.assert_not_called()

    def test_initial_review_requires_recorded_recovery_root_and_bounds_invalid_inputs(self) -> None:
        from control_plane.client_release import release_start_for_acceptance
        from control_plane.release_review import release_version, ReleaseEvidenceUnavailable

        self.profile = self.profile.model_copy(update={"release_on_acceptance": "promote"})
        self.store.write_product_profile_record(self.profile)
        for missing in ("ODOO_BACKUP_ROOT", "ODOO_DB_NAME"):
            with self.subTest(missing=missing):
                self.store.write_runtime_environment_record(
                    self.runtime.model_copy(
                        update={
                            "env": {
                                key: value
                                for key, value in self.runtime.env.items()
                                if key != missing
                            }
                        }
                    )
                )
                with self.assertRaises(ReleaseEvidenceUnavailable):
                    release_version(store=self.store, profile=self.profile, instance="prod")
                self.assertEqual(
                    release_start_for_acceptance(store=self.store, profile=self.profile),
                    "promote_with_rollback_drill",
                )

    def queue_promotion(self, decision: Any, attempt: int = 1) -> Any:
        from control_plane.client_release import _queue_promotion, ClientReleaseStep
        from control_plane.timestamps import utc_now_timestamp
        from tests.support.promotion_backup import stub_verified_promotion_backup

        for module in ("odoo_prod_promotion_run", "odoo_prod_promotion"):
            stub_verified_promotion_backup(self, "control_plane.workflows." + module)
        for target in (
            "control_plane.client_release.current_release_review",
            "control_plane.release_review.current_release_review",
        ):
            self.enterContext(
                patch(
                    target,
                    side_effect=lambda **_: build_release_review(
                        store=self.store, profile=self.profile, read=self.read
                    ),
                )
            )
        operation_id = _queue_promotion(
            self.store,
            Path("."),
            self.profile,
            decision,
            ClientReleaseStep("promote", attempt),
            self.context,
            f"fictional-infrastructure-{attempt}",
            utc_now_timestamp(),
        )
        self.assertTrue(operation_id)
        return self.store.read_odoo_prod_promotion_operation_record(operation_id)

    def write_rendered_configuration(self) -> dict[str, str]:
        import hashlib

        code = self.cached_root / str(self.provider_payload["appName"]) / "code"
        code.mkdir(parents=True, exist_ok=True)
        rendered_env = f"APP_NAME={self.provider_payload['appName']}\nCOMPOSE_PROJECT_NAME={self.provider_payload['appName']}\n{self.provider_payload['env']}\nDOCKER_CONFIG=/root/.docker"
        sources = {"env": rendered_env, "compose": str(self.provider_payload["composeFile"])}
        for name, content in ((".env", rendered_env), ("docker-compose.yml", sources["compose"])):
            (code / name).write_text(content)
        return {
            f"first_empty_rendered_{key}_sha256": hashlib.sha256(value.encode()).hexdigest()
            for key, value in sources.items()
        }

    def provider_replacement(self, **kwargs: Any) -> Any:
        from control_plane.contracts.odoo_stable_target_replacement import (
            OdooStableTargetReplacementApplyResult,
        )
        from control_plane.contracts.promotion_record import HealthcheckEvidence
        from control_plane.contracts.runtime_identity import RuntimeIdentity
        from control_plane.workflows.inventory import build_environment_inventory
        from control_plane.workflows.odoo_stable_target_replacement import (
            TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE,
        )
        from control_plane.timestamps import utc_now_timestamp
        from tests.test_client_release import _deployment
        import hashlib

        request = kwargs["request"]
        failed = getattr(self, "fail_deployment", False)
        self.modes.append(request.data_source_mode)
        self.deployment_counter = getattr(self, "deployment_counter", 0) + 1
        self.assertEqual(kwargs["release_decision"].record_id, self.accepted.record_id)
        self.assertTrue(kwargs["operation_id"])
        self.assertTrue(kwargs["operation_lease_owner"])
        kwargs["provider_effect_checkpoint"](TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE)
        self.provider_payload["env"] = "\n".join(
            f"{key}={value}" for key, value in self.runtime.env.items()
        )
        self.provider_payload["composeFile"] = "services: {}"
        rendered = self.write_rendered_configuration()
        deployment = _deployment(
            f"fictional-deployment-{self.deployment_counter}", self.artifact.artifact_id
        ).model_copy(
            update={
                "context": self.context,
                "source_git_ref": self.artifact.source_commit,
                "destination_health": HealthcheckEvidence(
                    status="pass", urls=("https://example.test/web/health",), timeout_seconds=1
                ),
                "runtime_source": {
                    **rendered,
                    "first_empty_release_decision": self.accepted.record_id,
                    "first_empty_release_operation": kwargs["operation_id"],
                    "first_empty_provider_deployment_id": f"fictional-provider-{self.deployment_counter}",
                    "first_empty_provider_env_sha256": hashlib.sha256(
                        str(self.provider_payload["env"]).encode()
                    ).hexdigest(),
                    "first_empty_provider_compose_sha256": hashlib.sha256(
                        str(self.provider_payload["composeFile"]).encode()
                    ).hexdigest(),
                },
                "runtime_identity": RuntimeIdentity(
                    product=self.profile.product,
                    context=self.context,
                    instance="prod",
                    artifact_id=self.artifact.artifact_id,
                    deployment_record_id=f"fictional-deployment-{self.deployment_counter}",
                    source_git_ref=self.artifact.source_commit,
                    image_reference=f"{self.artifact.image.repository}@{self.artifact.image.digest}",
                ),
            }
        )
        if failed:
            deployment = deployment.model_copy(
                update={"deploy": deployment.deploy.model_copy(update={"status": "fail"})}
            )
        self.store.write_deployment_record(deployment)
        if not failed:
            self.store.write_environment_inventory(
                build_environment_inventory(
                    deployment_record=deployment, updated_at=utc_now_timestamp()
                )
            )
        prior = getattr(self, "docker_state", None)
        self.docker_state = FakeReleaseDocker(
            self.artifact.artifact_id,
            deployment.record_id,
            ("example-db", "example-data", "example-logs", "example-data-filestore"),
            image=f"{self.artifact.image.repository}@{self.artifact.image.digest}",
        )
        if prior is not None and request.data_source_mode == "existing":
            self.docker_state.volumes = prior.volumes
        if failed and getattr(self, "no_initialized_stack", False):
            self.docker_state.rows.clear()
            self.docker_state.volumes.clear()
        return OdooStableTargetReplacementApplyResult(
            product=self.profile.product,
            context=self.context,
            instance="prod",
            artifact_id=self.artifact.artifact_id,
            image_reference=f"{self.artifact.image.repository}@{self.artifact.image.digest}",
            target_id=self.target_id.target_id,
            target_name=self.target.target_name,
            strategy="recreate-in-place",
            deployment_record_id=deployment.record_id,
            deploy_status="fail" if failed else "pass",
            post_deploy_status="fail" if failed else "pass",
            health_status="fail" if failed else "pass",
        )

    def run_worker(self) -> Any:
        from control_plane.workflows.odoo_stable_operation_worker import (
            run_odoo_stable_operation_worker_once,
        )

        return run_odoo_stable_operation_worker_once(
            record_store=self.store,
            control_plane_root_path=Path("."),
            lease_owner="fictional-worker",
        )

    def test_accepted_executor_delivers_initial_credentials_and_completes_real_executor_drill(
        self,
    ) -> None:
        from control_plane import runtime_environments
        from control_plane.dokploy import api, compose, source, post_deploy
        from control_plane.runtime_environments import resolve_site_runtime_environment_from_store
        from control_plane.workflows import odoo_stable_target_replacement as replacement
        from control_plane.contracts.odoo_stable_target_replacement import (
            LAUNCHPLANE_REQUIRED_ODOO_MODULES,
        )
        from control_plane.contracts.runtime_identity import RUNTIME_IDENTITY_ENV_KEY
        from control_plane.workflows.runtime_identity_health import HealthcheckPass
        from tests.test_odoo_post_deploy import _module_update_evidence
        from tests.test_odoo_stable_target_replacement import _verification_result

        self.runtime = self.runtime.model_copy(
            update={"env": {**self.runtime.env, "ODOO_DB_USER": "example"}}
        )
        self.store.write_runtime_environment_record(self.runtime)
        self.target = self.target.model_copy(
            update={"domains": ("example.test",), "public_hosts": ("example.test",)}
        )
        self.store.write_dokploy_target_record(self.target)
        from tests.test_product_topology_read_model import _route_binding

        self.store.write_route_binding_record(
            _route_binding(
                product=self.profile.product, target_name=self.target.target_name
            ).model_copy(update={"context": self.context})
        )
        self.artifact = self.artifact.model_copy(
            update={"odoo_install_modules": LAUNCHPLANE_REQUIRED_ODOO_MODULES}
        )
        self.store.write_artifact_manifest(self.artifact)
        history: list[dict[str, str]] = []

        def source_write(**kwargs: Any) -> dict[str, str]:
            self.provider_payload["composeFile"] = kwargs["compose_file"]
            self.provider_payload["sourceType"] = "raw"
            return {}

        def environment_write(**kwargs: Any) -> None:
            self.provider_payload["env"] = kwargs["env_text"]

        def trigger(**kwargs: Any) -> None:
            env = api.parse_dokploy_env_text(str(self.provider_payload["env"]))
            identity = json.loads(env[RUNTIME_IDENTITY_ENV_KEY])
            prior = getattr(self, "docker_state", None)
            self.docker_state = FakeReleaseDocker(
                identity["artifact_id"],
                identity["deployment_record_id"],
                (env["ODOO_DB_VOLUME"], env["ODOO_DATA_VOLUME"], env["ODOO_LOG_VOLUME"]),
                image=identity["image_reference"],
            )
            for name in ("web-id", "script-runner"):
                self.docker_state.rows[name]["Config"]["Env"] = [
                    f"{key}={value}" for key, value in env.items()
                ]
            if prior is not None:
                self.docker_state.volumes = prior.volumes
            self.write_rendered_configuration()
            history.append(
                {
                    "deploymentId": f"provider-{len(history) + 1}",
                    "status": "done",
                    "title": kwargs["title"],
                }
            )

        def provider_request(*, path: str, **_: Any) -> Any:
            return (
                [{"host": "example.test", "domainId": "fictional-domain"}]
                if path == "/api/domain.byComposeId"
                else []
            )

        def execute_real(**kwargs: Any) -> Any:
            self.modes.append(kwargs["request"].data_source_mode)
            return replacement.execute_odoo_stable_target_replacement_apply(
                **kwargs, dokploy_request=provider_request
            )

        def post_deploy_execution(**kwargs: Any) -> dict[str, str]:
            kwargs["before_provider_mutation"]("post_deploy_target_update")
            return _module_update_evidence(website_indexing_production_use="prelaunch")

        def native_indexing(**kwargs: Any) -> dict[str, Any]:
            request = kwargs["request"]
            return {
                "status": "complete",
                "native": {
                    **request["indexing"],
                    "website_id": 1,
                    "field": "custom_code_head",
                    "before_sha256": "a" * 64,
                    "after_sha256": "b" * 64,
                    "changed": True,
                    "managed_noindex_before": False,
                    "managed_noindex_after": True,
                    "committed": False,
                    "database_uuid": "fictional-database",
                },
            }

        def health_read(**_: Any) -> HealthcheckPass:
            env = dict(
                value.split("=", 1) for value in self.docker_state.rows["web-id"]["Config"]["Env"]
            )
            return HealthcheckPass(
                payload={"runtime_identity": json.loads(env[RUNTIME_IDENTITY_ENV_KEY])}
            )

        with (
            patch.object(
                replacement,
                "build_odoo_stable_target_replacement_plan",
                wraps=replacement.build_odoo_stable_target_replacement_plan,
            ) as plan,
            patch.object(api, "dokploy_request", side_effect=provider_request),
            patch.object(api, "list_deployments_for_target", side_effect=lambda **_: list(history)),
            patch.object(
                api,
                "latest_deployment_for_target",
                side_effect=lambda **_: history[-1] if history else None,
            ),
            patch.object(
                runtime_environments,
                "resolve_site_runtime_environment",
                side_effect=lambda **_: resolve_site_runtime_environment_from_store(
                    record_store=self.store,
                    context_name=self.context,
                    instance_name="prod",
                    target_environment={},
                ),
            ),
            patch.object(compose, "sync_dokploy_compose_raw_source", side_effect=source_write),
            patch.object(compose, "ensure_compose_web_domain_route"),
            patch.object(
                compose,
                "fetch_dokploy_converted_compose_file",
                side_effect=lambda **_: str(self.provider_payload["composeFile"]),
            ),
            patch.object(api, "update_dokploy_target_env", side_effect=environment_write),
            patch.object(api, "trigger_deployment", side_effect=trigger),
            patch.object(api, "wait_for_target_deployment", return_value="done"),
            patch.object(
                source,
                "read_control_plane_dokploy_source_of_truth",
                side_effect=lambda **_: source.load_optional_dokploy_source_of_truth_from_store(
                    record_store=self.store
                ),
            ),
            patch.object(
                post_deploy, "run_compose_post_deploy_update", side_effect=post_deploy_execution
            ) as updates,
            patch(
                "control_plane.odoo_website_indexing.run_native_indexing",
                side_effect=native_indexing,
            ),
            patch.object(
                replacement, "verify_odoo_stable_readiness", return_value=_verification_result()
            ),
            patch.object(
                replacement,
                "wait_for_runtime_identity_healthcheck_with_retry",
                side_effect=health_read,
            ),
            patch.object(replacement, "require_forward_lane_build"),
        ):
            self.complete_drill(expected_first_mode="empty", replacement=execute_real)
        self.assertTrue(plan.called)
        self.assertEqual(updates.call_count, 2)
        delivered = api.parse_dokploy_env_text(str(self.provider_payload["env"]))
        from control_plane.contracts.product_profile_record import (
            FRESH_TESTING_LOCAL_CREDENTIAL_KEYS,
        )

        self.assertTrue(
            set(self.runtime.env) | FRESH_TESTING_LOCAL_CREDENTIAL_KEYS <= delivered.keys()
        )
        for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS:
            self.assertEqual(delivered[key], f"fictional-{key.lower()}")
        self.assertFalse(
            self.store.read_product_profile_record(
                self.profile.product
            ).prelaunch_bootstrap_authorized
        )

    def observe(
        self, *, program: str = "", payload: dict[str, object], receive: Any, **kwargs: Any
    ) -> None:
        from control_plane.odoo_first_release import (
            first_release_park_program,
            park_first_release_web,
        )

        from control_plane.dokploy.compose_inventory import (
            CACHED_COMPOSE_PROGRAM,
            read_cached_files,
        )

        if program == CACHED_COMPOSE_PROGRAM:
            receive({"type": "complete", "result": read_cached_files(self.cached_root, payload)})
        elif program == first_release_park_program():
            if kwargs.get("dispatch_guard") is not None:
                kwargs["dispatch_guard"](lambda: None)
            result = park_first_release_web(self.docker_state, payload)
            from control_plane.contracts.canonical_json import (
                nonvalidating_compact_sorted_json_sha256,
            )

            receive(
                {
                    "type": "complete",
                    "binding": nonvalidating_compact_sorted_json_sha256(payload),
                    "result": result,
                }
            )
        else:
            fixture = getattr(self, "empty_storage_fixture", None)
            if fixture is None:
                super().observe(payload=payload, receive=receive, **kwargs)
            else:
                from control_plane.odoo_first_empty_inputs import first_empty_storage_snapshot
                from control_plane.contracts.canonical_json import (
                    nonvalidating_compact_sorted_json_sha256,
                )

                storage = first_empty_storage_snapshot(
                    fixture.docker,
                    payload,
                    compose_root=fixture.root / "compose",
                    read_swarm=lambda _: [],
                )
                receive(
                    {
                        "type": "complete",
                        "binding": nonvalidating_compact_sorted_json_sha256(payload),
                        "storage": nonvalidating_compact_sorted_json_sha256(storage),
                    }
                )

    def test_first_drill_retains_data_then_takes_real_dump_and_repromotes(self) -> None:
        self.complete_drill(expected_first_mode="empty")

    def capture_stopped_backup(self, **kwargs: Any) -> dict[str, Any]:
        from tests.test_odoo_prod_backup_gate import _passing_backup_capture_evidence

        self.assertFalse(self.docker_state.rows["web-id"]["State"]["Running"])
        self.assertEqual(kwargs["expected_web_state"], "exited")
        return {
            **_passing_backup_capture_evidence(),
            "backup_nonce": kwargs["backup_nonce"],
            "backup_record_id": kwargs["backup_record_id"],
            "database_name": kwargs["database_name"],
        }

    def complete_drill(self, *, expected_first_mode: str, replacement: Any = None) -> None:
        from datetime import UTC, datetime, timedelta
        from control_plane.client_release import (
            advance_client_releases,
            client_release_steps,
            read_client_release_run,
        )
        from control_plane.release_review import release_version
        from control_plane.odoo_first_release import FIRST_RELEASE_ABSENCE_SOURCE
        from control_plane.contracts.production_backup_authority import ProductionBackupPolicyRecord
        from control_plane.workflows.production_promotion_backup import ODOO_PROMOTION_BACKUP_ACTION
        from control_plane.workflows.production_backup_gate_operation_worker import (
            run_production_backup_gate_operation_worker_once,
        )
        from tests.test_production_backup_provider import _binding, BackupHost, setup_memory_files

        binding = _binding()
        payload = binding.policy.model_dump(mode="json")
        for derived in ("policy_id", "record_id", "policy_digest"):
            payload.pop(derived)
        payload.update(
            product=self.profile.product,
            context=self.context,
            promotion_action=ODOO_PROMOTION_BACKUP_ACTION,
        )
        self.store.write_production_backup_target_record(binding.source_target)
        self.store.write_production_backup_target_record(binding.destination_target)
        self.store.write_production_backup_policy_record(
            ProductionBackupPolicyRecord.model_validate(payload)
        )
        setup_memory_files(self)
        host = BackupHost()
        capture_time = datetime.now(UTC)

        class CaptureClock(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> Self:
                return cls.fromtimestamp(capture_time.timestamp(), tz)

        self.enterContext(
            patch("control_plane.workflows.production_promotion_backup.datetime", CaptureClock)
        )
        self.enterContext(
            patch(
                "control_plane.workflows.production_backup_provider.utc_now_timestamp",
                side_effect=lambda: capture_time.isoformat(),
            )
        )
        self.enterContext(
            patch(
                "control_plane.workflows.production_backup_gate_operation_worker._utc_now_timestamp",
                side_effect=lambda: capture_time.isoformat(),
            )
        )
        dump = self.enterContext(
            patch(
                "control_plane.dokploy.post_deploy.run_compose_odoo_backup_gate",
                side_effect=self.capture_stopped_backup,
            )
        )
        self.enterContext(
            patch(
                "control_plane.workflows.production_backup_gate.resolve_guest_capture_values",
                return_value={
                    "PRODUCTION_BACKUP_SSH_PRIVATE_KEY": "fictional-private",
                    "PRODUCTION_BACKUP_SSH_KNOWN_HOSTS": "fictional-hosts",
                },
            )
        )
        self.enterContext(
            patch(
                "control_plane.workflows.production_backup_provider.subprocess.run",
                side_effect=host.run,
            )
        )
        for target in (
            "control_plane.client_release.current_release_review",
            "control_plane.release_review.current_release_review",
        ):
            self.enterContext(
                patch(
                    target,
                    side_effect=lambda **_: build_release_review(
                        store=self.store, profile=self.profile, read=self.read
                    ),
                )
            )
        self.accepted = self.decide()
        self.modes: list[str] = []
        with patch(
            "control_plane.workflows.odoo_prod_promotion.execute_odoo_stable_target_replacement_apply",
            side_effect=replacement or self.provider_replacement,
        ):
            for step in client_release_steps(self.accepted.release_start):
                queued = advance_client_releases(store=self.store, control_plane_root=Path("."))
                self.assertEqual(
                    len(queued),
                    1,
                    (
                        step.name,
                        read_client_release_run(
                            store=self.store,
                            profile=self.profile,
                            decision=self.accepted,
                            control_plane_root=Path("."),
                        ),
                    ),
                )
                (operation_id,) = queued
                self.assertFalse(
                    advance_client_releases(store=self.store, control_plane_root=Path("."))
                )
                operation: Any
                if step.kind == "backup":
                    worked = run_production_backup_gate_operation_worker_once(
                        record_store=self.store,
                        control_plane_root_path=Path("."),
                        lease_owner="fictional-backup-worker",
                    )
                    operation = self.store.read_verireel_prod_backup_gate_operation_record(
                        operation_id
                    )
                    capture_time = datetime.fromisoformat(operation.finished_at) + timedelta(
                        seconds=1
                    )
                else:
                    worked = self.run_worker()
                    operation = (
                        self.store.read_odoo_prod_promotion_operation_record(operation_id)
                        if step.kind == "promote"
                        else self.store.read_odoo_prod_rollback_operation_record(operation_id)
                    )
                self.assertTrue(worked.terminal_write_committed)
                self.assertEqual(operation.status, "pass", operation.error_message)
                if step.kind == "promote" and step.attempt == 1:
                    assert operation.result is not None
                    gate = self.store.read_backup_gate_record(operation.result.backup_record_id)
                    if expected_first_mode == "empty":
                        self.assertEqual(gate.source, FIRST_RELEASE_ABSENCE_SOURCE)
                        self.assertNotIn("database_dump_sha256", gate.evidence)
                    else:
                        self.assertIn("database_dump_sha256", gate.evidence)
                if step.kind == "rollback":
                    from control_plane.client_release import (
                        client_release_grant,
                        client_release_grant_allows,
                    )
                    from control_plane.contracts.odoo_prod_promotion_operation import (
                        ODOO_PROD_PROMOTION_RUN_ACTION,
                    )

                    grant = client_release_grant(
                        decision=self.accepted,
                        action=ODOO_PROD_PROMOTION_RUN_ACTION,
                        context=self.context,
                        authorized_at=self.accepted.decided_at,
                    )
                    self.assertTrue(client_release_grant_allows(self.store, grant))
                    self.assert_parked_web_only()
                    self.assertFalse(self.docker_state.rows["web-id"]["State"]["Running"])
                    self.assertEqual(
                        release_version(store=self.store, profile=self.profile, instance="prod"),
                        self.accepted.checklist.production,
                    )
        self.assertFalse(advance_client_releases(store=self.store, control_plane_root=Path(".")))
        run = read_client_release_run(
            store=self.store, profile=self.profile, decision=self.accepted
        )
        assert run is not None
        self.assertEqual(run.state, "passed")
        self.assertEqual(dump.call_count, 1 if expected_first_mode == "empty" else 2)
        self.assertEqual(sum(command[0] == "vzdump" for command in host.commands), 2)
        self.assertEqual(self.modes, [expected_first_mode, "existing"])
        self.assertEqual(
            release_version(store=self.store, profile=self.profile, instance="prod"),
            self.accepted.checklist.candidate,
        )

    def assert_parked_web_only(self) -> None:
        self.assertEqual([command[0] for command in self.docker_state.writes], ["update", "stop"])
        self.assertTrue(all(command[-1] == "web-id" for command in self.docker_state.writes))
        self.assertTrue(self.docker_state.rows["database"]["State"]["Running"])
        self.assertTrue(self.docker_state.rows["script-runner"]["State"]["Running"])

    def test_failure_recovery_stops_owned_web_after_runtime_and_secret_edits(self) -> None:
        from control_plane.product_config import apply_product_config_bundle

        def edit_workers() -> None:
            self.runtime = self.runtime.model_copy(
                update={"env": {**self.runtime.env, "ODOO_WORKERS": "3"}}
            )
            self.store.write_runtime_environment_record(self.runtime)

        def rotate_secret() -> None:
            apply_product_config_bundle(
                record_store=self.store,
                payload={
                    "product": self.profile.product,
                    "context": self.context,
                    "instance": "prod",
                    "secrets": [
                        {
                            "binding_key": "ODOO_DB_PASSWORD",
                            "value": "fictional-rotated",
                            "secret_class": "prod_only",
                        }
                    ],
                },
                mode="apply",
                actor="fixture",
                source_label="fixture",
            )

        self.fail_and_recover(before_terminal=edit_workers, before_recovery=rotate_secret)

    def test_newer_acceptance_cannot_deploy_until_original_recovery_releases_the_lane(self) -> None:
        from control_plane.odoo_stable_lane import OdooStableLaneOperationConflictError

        later = None

        def newer_acceptance() -> None:
            nonlocal later
            later = self.decide()
            with self.assertRaises(OdooStableLaneOperationConflictError):
                self.queue_promotion(later)
            self.assertFalse(
                any(
                    record.authorization.release_decision_record_id == later.record_id
                    for record in self.store.list_odoo_prod_promotion_operation_records()
                )
            )

        self.fail_and_recover(before_recovery=newer_acceptance)
        assert later is not None
        self.accepted = later
        self.fail_deployment = False
        operation = self.queue_promotion(later)
        with patch(
            "control_plane.workflows.odoo_prod_promotion.execute_odoo_stable_target_replacement_apply",
            side_effect=self.provider_replacement,
        ):
            self.assertTrue(self.run_worker().terminal_write_committed)
        finished = self.store.read_odoo_prod_promotion_operation_record(operation.operation_id)
        self.assertEqual(finished.status, "pass", finished.error_message)
        self.assertEqual(self.modes, ["empty", "existing"])

    def test_inverse_refuses_changed_original_storage_before_native_effects(self) -> None:
        inverse_id = self.promote_and_queue_inverse()
        self.store.write_runtime_environment_record(
            self.runtime.model_copy(
                update={"env": {**self.runtime.env, "ODOO_DB_VOLUME": "another-original-db"}}
            )
        )
        self.probe.reset_mock()
        self.run_worker()
        self.assertEqual(
            self.store.read_odoo_prod_rollback_operation_record(inverse_id).status, "fail"
        )
        self.probe.assert_not_called()
        self.assertFalse(self.docker_state.writes)
        self.assertTrue(self.docker_state.rows["web-id"]["State"]["Running"])

    def test_drill_inverse_can_stop_after_settings_edit_but_cannot_repromote_changed_inputs(
        self,
    ) -> None:
        from control_plane.client_release import client_release_grant, client_release_grant_allows
        from control_plane.contracts.odoo_prod_promotion_operation import (
            ODOO_PROD_PROMOTION_RUN_ACTION,
        )
        from control_plane.release_review import release_version

        inverse_id = self.promote_and_queue_inverse()
        self.store.write_runtime_environment_record(
            self.runtime.model_copy(update={"env": {**self.runtime.env, "ODOO_WORKERS": "3"}})
        )
        self.assertTrue(self.run_worker().terminal_write_committed)
        inverse = self.store.read_odoo_prod_rollback_operation_record(inverse_id)
        self.assertEqual(inverse.status, "pass", inverse.error_message)
        self.assert_parked_web_only()
        self.assertNotEqual(
            release_version(store=self.store, profile=self.profile, instance="prod"),
            self.accepted.checklist.production,
        )
        grant = client_release_grant(
            decision=self.accepted,
            action=ODOO_PROD_PROMOTION_RUN_ACTION,
            context=self.context,
            authorized_at=self.accepted.decided_at,
        )
        self.assertFalse(client_release_grant_allows(self.store, grant))
        self.assertEqual(self.modes, ["empty"])

    def test_failed_first_deploy_queues_same_acceptance_recovery_without_deleting_data(
        self,
    ) -> None:
        self.fail_and_recover()

    def fail_and_recover(
        self,
        *,
        initialized: bool = True,
        before_terminal: Callable[[], None] | None = None,
        before_recovery: Callable[[], None] | None = None,
    ) -> None:
        self.enterContext(
            patch(
                "control_plane.dokploy.post_deploy.run_compose_odoo_backup_gate",
                side_effect=self.capture_stopped_backup,
            )
        )
        self.accepted = self.decide()
        first = self.queue_promotion(self.accepted)
        self.modes = []
        self.fail_deployment = True
        self.no_initialized_stack = not initialized

        def replace(**kwargs: Any) -> Any:
            result = self.provider_replacement(**kwargs)
            if before_terminal is not None:
                before_terminal()
            return result

        with patch(
            "control_plane.workflows.odoo_prod_promotion.execute_odoo_stable_target_replacement_apply",
            side_effect=replace,
        ):
            self.assertTrue(self.run_worker().terminal_write_committed)
        failed = self.store.read_odoo_prod_promotion_operation_record(first.operation_id)
        self.assertEqual(failed.status, "fail")
        recoveries = tuple(
            record
            for record in self.store.list_odoo_prod_rollback_operation_records()
            if record.authorization.release_decision_record_id == self.accepted.record_id
        )
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(
            recoveries[0].authorization.release_decision_record_id, self.accepted.record_id
        )
        if before_recovery is not None:
            before_recovery()
        self.assertTrue(self.run_worker().terminal_write_committed)
        recovery = self.store.read_odoo_prod_rollback_operation_record(recoveries[0].operation_id)
        self.assertEqual(recovery.status, "pass", recovery.error_message)
        if initialized:
            self.assert_parked_web_only()
            self.assertFalse(self.docker_state.rows["web-id"]["State"]["Running"])
        else:
            self.assertFalse(self.docker_state.rows)

    def test_conclusive_failure_without_a_stack_requalifies_owned_configuration_and_absence(
        self,
    ) -> None:
        from tests.test_odoo_first_empty_inputs import FirstEmptyStorageTests
        from control_plane.dokploy import api

        fixture = FirstEmptyStorageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.empty_storage_fixture = fixture
        self.cached_root = fixture.root / "compose"
        self.fail_and_recover(initialized=False)
        code = self.cached_root / "example-prod" / "code"
        self.assertNotEqual((code / ".env").read_text(), self.provider_payload["env"])
        self.enterContext(
            patch.object(
                api,
                "list_deployments_for_target",
                return_value=[{"deploymentId": "fictional-provider-1", "status": "error"}],
            )
        )
        self.fail_deployment = False
        self.complete_drill(expected_first_mode="empty")

    def test_foreign_backup_prevents_initial_provider_reads_or_writes(self) -> None:
        from control_plane.contracts.backup_gate_record import BackupGateRecord
        from control_plane.timestamps import utc_now_timestamp

        accepted = self.decide()
        operation = self.queue_promotion(accepted)
        self.store.write_backup_gate_record(
            BackupGateRecord(
                record_id="foreign-backup",
                context=self.context,
                instance="prod",
                created_at=utc_now_timestamp(),
                source="fictional",
                status="pass",
                evidence={"held": "yes"},
            )
        )
        self.probe.reset_mock()
        with patch(
            "control_plane.workflows.odoo_prod_promotion.execute_odoo_stable_target_replacement_apply"
        ) as deploy:
            self.run_worker()
        deploy.assert_not_called()
        self.probe.assert_not_called()
        self.assertNotEqual(
            self.store.read_odoo_prod_promotion_operation_record(operation.operation_id).status,
            "pass",
        )

    def test_recovered_initial_releases_can_accept_again_and_finish_with_retained_data(
        self,
    ) -> None:
        self.fail_and_recover()
        first = self.accepted
        self.fail_and_recover()
        self.assertNotEqual(self.accepted.record_id, first.record_id)
        self.artifact = self.artifact.model_copy(
            update={"artifact_id": "fictional-corrected", "source_commit": "c" * 40}
        )
        self.store.write_artifact_manifest(self.artifact)
        self.store.write_release_tuple_record(
            ReleaseTupleRecord(
                tuple_id="fictional-corrected-testing",
                context=self.context,
                channel="testing",
                artifact_id=self.artifact.artifact_id,
                repo_shas={self.profile.repository: self.artifact.source_commit},
                provenance="ship",
                minted_at="2026-11-12T16:00:00Z",
            )
        )
        self.fail_deployment = False
        self.complete_drill(expected_first_mode="existing")

    def promote_and_queue_inverse(self) -> str:
        from control_plane.client_release import _queue_rollback, ClientReleaseStep
        from control_plane.timestamps import utc_now_timestamp

        self.accepted = self.decide()
        self.queue_promotion(self.accepted)
        self.modes = []
        with patch(
            "control_plane.workflows.odoo_prod_promotion.execute_odoo_stable_target_replacement_apply",
            side_effect=self.provider_replacement,
        ):
            self.run_worker()
        return _queue_rollback(
            self.store,
            Path("."),
            self.profile,
            self.accepted,
            ClientReleaseStep("rollback", 1),
            self.context,
            utc_now_timestamp(),
        )

    def test_lost_parking_response_holds_original_operation_without_repeating_effects(self) -> None:
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable
        from control_plane.odoo_first_release import first_release_park_program

        inverse_id = self.promote_and_queue_inverse()

        def lost_response(**kwargs: Any) -> None:
            self.observe(**kwargs)
            if kwargs["program"] == first_release_park_program():
                raise NativeSourceUnavailable("fictional response loss")

        self.probe.side_effect = lost_response
        self.run_worker()
        operation = self.store.read_odoo_prod_rollback_operation_record(inverse_id)
        self.assertEqual(operation.status, "reconciliation_required")
        self.assert_parked_web_only()
        observed = self.probe.call_count
        self.run_worker()
        self.assertEqual(self.probe.call_count, observed)
        self.assertEqual(
            self.store.read_odoo_prod_rollback_operation_record(inverse_id).operation_id,
            operation.operation_id,
        )

    def test_parking_publication_cannot_overwrite_concurrent_inventory(self) -> None:
        from control_plane.odoo_first_release import (
            first_release_park_program,
            FIRST_RELEASE_STOPPED_KEY,
        )

        inverse_id = self.promote_and_queue_inverse()
        original = self.store.read_environment_inventory(
            context_name=self.context, instance_name="prod"
        )
        from datetime import datetime, timedelta

        replacement = original.model_copy(
            update={
                "updated_at": (
                    datetime.fromisoformat(original.updated_at) + timedelta(microseconds=1)
                ).isoformat()
            }
        )
        self.assertNotEqual(replacement, original)

        def race(**kwargs: Any) -> None:
            self.observe(**kwargs)
            if kwargs["program"] == first_release_park_program():
                self.store.write_environment_inventory(replacement)

        self.probe.side_effect = race
        self.run_worker()
        self.assertEqual(
            self.store.read_odoo_prod_rollback_operation_record(inverse_id).status,
            "reconciliation_required",
        )
        current = self.store.read_environment_inventory(
            context_name=self.context, instance_name="prod"
        )
        self.assertEqual(current, replacement)
        self.assertNotIn(FIRST_RELEASE_STOPPED_KEY, current.post_deploy_update.evidence)


class FakeReleaseDocker:
    def __init__(
        self,
        artifact: str,
        deployment: str,
        volumes: tuple[str, ...],
        *,
        image: str = "example/odoo@sha256:" + "a" * 64,
    ) -> None:
        self.volumes = {name: {"retained-business-data": object()} for name in volumes}
        self.writes: list[tuple[str, ...]] = []
        from control_plane.dokploy.compose import render_odoo_raw_compose_file
        import yaml

        compose = yaml.safe_load(
            render_odoo_raw_compose_file(
                image_reference=image,
                hold_web_until_integration_readback=True,
            )
        )
        names = dict(zip(("odoo_db", "odoo_data", "odoo_logs"), volumes[:3], strict=True))
        self.workflow_ssh_source = ""
        self.artifact, self.deployment, self.image = artifact, deployment, image
        self.rows: dict[str, dict[str, Any]] = {}
        for service, definition in compose["services"].items():
            identifier = "web-id" if service == "web" else service
            mounts = []
            for mount in definition.get("volumes", []):
                if mount.startswith("${DATA_WORKFLOW_SSH_DIR"):
                    source, destination, mode = mount.rsplit(":", 2)
                    self.workflow_ssh_source = source.split(":-", 1)[1].removesuffix("}")
                    mounts.append(
                        {
                            "Type": "bind",
                            "Source": self.workflow_ssh_source,
                            "Destination": destination,
                            "RW": mode != "ro",
                        }
                    )
                else:
                    name, destination = mount.split(":", 1)
                    mounts.append(
                        {
                            "Type": "volume",
                            "Name": names[name],
                            "Destination": destination,
                            "Source": f"/fictional/volumes/{names[name]}/_data",
                        }
                    )
            self.rows[identifier] = {
                "Id": identifier,
                "Config": {
                    "Image": definition["image"],
                    "Labels": {
                        "com.docker.compose.project": "example-prod",
                        "com.docker.compose.service": service,
                    },
                    "Env": [
                        f"LAUNCHPLANE_ARTIFACT_ID={artifact}",
                        f"LAUNCHPLANE_DEPLOYMENT_RECORD_ID={deployment}",
                    ],
                    "Cmd": definition.get("command", "").split()
                    if isinstance(definition.get("command", ""), str)
                    else definition["command"],
                },
                "Mounts": mounts,
                "State": {"Running": True},
                "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
            }

    def park_spec(self) -> dict[str, Any]:
        return {
            "project": "example-prod",
            "volumes": list(self.volumes),
            "workflow_ssh_source": self.workflow_ssh_source,
            "deployments": {self.deployment: {"artifact": self.artifact, "image": self.image}},
        }

    def __call__(self, *args: str) -> str:
        if args[0] == "info":
            return json.dumps({"LocalNodeState": "inactive"})
        if args[0] == "ps":
            return "\n".join(self.rows)
        if args[:2] == ("volume", "ls"):
            return "\n".join(self.volumes)
        if args[:2] == ("volume", "inspect"):
            return json.dumps(
                [
                    {
                        "Name": name,
                        "Driver": "local",
                        "Options": {},
                        "Mountpoint": f"/fictional/volumes/{name}/_data",
                    }
                    for name in args[2:]
                ]
            )
        if args[0] == "inspect":
            return json.dumps([self.rows[key] for key in args[1:]])
        self.writes.append(args)
        if args[0] == "update":
            self.rows[args[-1]]["HostConfig"]["RestartPolicy"]["Name"] = "no"
        elif args[0] == "stop":
            self.rows[args[-1]]["State"]["Running"] = False
        else:
            raise AssertionError(args)
        return ""


class FirstReleaseParkingTests(unittest.TestCase):
    def test_native_program_stops_only_web_and_keeps_runtime_storage(self) -> None:
        import base64
        from contextlib import redirect_stdout
        import io
        import sys
        from types import SimpleNamespace
        from control_plane.odoo_first_release import first_release_park_program
        from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256

        docker = FakeReleaseDocker("artifact", "deployment", ("db", "data", "logs"))
        spec = docker.park_spec()

        def run(arguments: list[str], **_: Any) -> Any:
            return SimpleNamespace(returncode=0, stdout=docker(*arguments[3:]).encode())

        output = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                ["program", base64.b64encode(json.dumps(spec).encode()).decode(), "FRAME:"],
            ),
            patch("subprocess.run", side_effect=run),
            redirect_stdout(output),
        ):
            exec(first_release_park_program(), {"__name__": "__main__"})
        frame = json.loads(output.getvalue().removeprefix("FRAME:"))
        self.assertEqual(frame["binding"], nonvalidating_compact_sorted_json_sha256(spec))
        self.assertTrue(frame["result"]["web_stopped"])
        self.assertFalse(docker.rows["web-id"]["State"]["Running"])
        self.assertTrue(docker.rows["database"]["State"]["Running"])
        self.assertTrue(docker.rows["script-runner"]["State"]["Running"])
        self.assertEqual([command[0] for command in docker.writes], ["update", "stop"])
        self.assertTrue(all(command[-1] == "web-id" for command in docker.writes))

    def test_foreign_container_retaining_original_storage_blocks_before_writes(self) -> None:
        from control_plane.odoo_first_release import park_first_release_web

        docker = FakeReleaseDocker("artifact", "deployment", ("db", "data", "logs"))
        docker.rows["foreign"] = {
            "Id": "foreign",
            "Config": {"Labels": {}},
            "Mounts": [{"Type": "volume", "Name": "data"}],
        }
        with self.assertRaises(ValueError):
            park_first_release_web(
                docker,
                docker.park_spec(),
            )
        self.assertEqual(docker.writes, [])
        self.assertTrue(docker.rows["web-id"]["State"]["Running"])
