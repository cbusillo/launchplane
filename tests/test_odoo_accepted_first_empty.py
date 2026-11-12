import json
import unittest
from pathlib import Path
from unittest.mock import patch
from typing import Any
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
from control_plane.release_review import build_release_review, checklist_digest
from tests.test_odoo_first_empty_inputs import FirstEmptyInputsFixture
from tests.test_release_review import github_read


class AcceptedFirstEmptyTests(FirstEmptyInputsFixture):
    def setUp(self) -> None:
        super().setUp()
        self.profile = self.profile.model_copy(
            update={"owner": ProductOwnerProfile(github_id="9001", github_login="site-owner")}
        )
        self.store.write_product_profile_record(self.profile)
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

    def read(self, path: str) -> Any:
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
        self.assertTrue(review.required)
        self.assertFalse(review.approved)
        self.assertTrue(review.checklist_complete, review.blockers)
        assert review.checklist is not None
        production = review.checklist.production.model_dump(mode="json")
        self.assertEqual(production["state"], "absent")
        self.assertNotIn("artifact_id", production)
        self.assertEqual(review.checklist.candidate.artifact_id, self.artifact.artifact_id)
        self.probe.assert_not_called()
        self.assertEqual(self.provider_payload["env"], "")
        before = checklist_digest(review.checklist)
        self.store.write_runtime_environment_record(
            self.runtime.model_copy(update={"env": {**self.runtime.env, "ODOO_WORKERS": "3"}})
        )
        changed = build_release_review(store=self.store, profile=self.profile, read=self.read)
        assert changed.checklist is not None
        self.assertNotEqual(checklist_digest(changed.checklist), before)

    def accept(self) -> Any:
        from control_plane.contracts.release_review import ReleaseReviewDecisionRecord
        from control_plane.client_release import release_start_for_acceptance
        from control_plane.timestamps import utc_now_timestamp

        self.profile = self.profile.model_copy(update={"release_on_acceptance": "promote"})
        self.store.write_product_profile_record(self.profile)
        review = build_release_review(store=self.store, profile=self.profile, read=self.read)
        assert review.checklist is not None
        decision = ReleaseReviewDecisionRecord(
            record_id="fictional-first-acceptance",
            product=self.profile.product,
            checklist=review.checklist,
            checklist_digest=review.checklist_digest,
            decision="accepted",
            actor_github_id=self.profile.owner.github_id,
            actor_github_login=self.profile.owner.github_login,
            release_issue_url="https://github.com/example/product/issues/1",
            decided_at=utc_now_timestamp(),
            release_start=release_start_for_acceptance(store=self.store, profile=self.profile),
        )
        self.store.write_release_review_decision_record(decision)
        return decision

    def test_missing_recorded_production_artifact_is_not_original_absence(self) -> None:
        from control_plane.release_review import release_version, ReleaseEvidenceUnavailable

        self.accept()
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

    def provider_replacement(self, **kwargs: Any) -> Any:
        from control_plane.contracts.odoo_stable_target_replacement import (
            OdooStableTargetReplacementApplyResult,
        )
        from control_plane.contracts.promotion_record import HealthcheckEvidence
        from control_plane.workflows.inventory import build_environment_inventory
        from control_plane.workflows.odoo_stable_target_replacement import (
            TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE,
        )
        from control_plane.timestamps import utc_now_timestamp
        from tests.test_client_release import _deployment
        import hashlib

        request = kwargs["request"]
        self.modes.append(request.data_source_mode)
        self.assertEqual(kwargs["release_decision"].record_id, self.accepted.record_id)
        self.assertTrue(kwargs["operation_id"])
        self.assertTrue(kwargs["operation_lease_owner"])
        kwargs["provider_effect_checkpoint"](TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE)
        self.provider_payload["env"] = "\n".join(
            f"{key}={value}" for key, value in self.runtime.env.items()
        )
        self.provider_payload["composeFile"] = "services: {}"
        deployment = _deployment(
            f"fictional-deployment-{len(self.modes)}", self.artifact.artifact_id
        ).model_copy(
            update={
                "context": self.context,
                "source_git_ref": self.artifact.source_commit,
                "destination_health": HealthcheckEvidence(
                    status="pass", urls=("https://example.test/web/health",), timeout_seconds=1
                ),
                "runtime_source": {
                    "first_empty_release_decision": self.accepted.record_id,
                    "first_empty_release_operation": kwargs["operation_id"],
                    "first_empty_provider_env_sha256": hashlib.sha256(
                        str(self.provider_payload["env"]).encode()
                    ).hexdigest(),
                    "first_empty_provider_compose_sha256": hashlib.sha256(
                        str(self.provider_payload["composeFile"]).encode()
                    ).hexdigest(),
                },
            }
        )
        self.store.write_deployment_record(deployment)
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
        )
        if prior is not None and request.data_source_mode == "existing":
            self.docker_state.volumes = prior.volumes
        failed = getattr(self, "fail_deployment", False)
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

    def test_accepted_executor_delivers_initial_credentials_without_runtime_sync(self) -> None:
        from functools import partial
        from datetime import UTC, datetime, timedelta
        from control_plane import runtime_environments
        from control_plane.dokploy import api, compose
        from control_plane.product_config import apply_product_config_bundle
        from control_plane.runtime_environments import resolve_site_runtime_environment_from_store
        from control_plane.workflows.odoo_prod_promotion_run import execute_odoo_prod_promotion_run
        from control_plane.workflows import odoo_stable_target_replacement as replacement
        from tests.test_odoo_first_empty_lifecycle import ReachedDeployment
        from control_plane.contracts.odoo_stable_target_replacement import (
            LAUNCHPLANE_REQUIRED_ODOO_MODULES,
        )

        self.runtime = self.runtime.model_copy(
            update={"env": {**self.runtime.env, "ODOO_DB_USER": "example"}}
        )
        self.store.write_runtime_environment_record(self.runtime)
        keys = ("ODOO_ADMIN_PASSWORD", "ODOO_DB_PASSWORD", "ODOO_MASTER_PASSWORD")
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
                    for key in keys
                ],
            },
            mode="apply",
            actor="fixture",
            source_label="fixture",
        )
        self.target = self.target.model_copy(update={"domains": ("example.test",)})
        self.store.write_dokploy_target_record(self.target)
        self.artifact = self.artifact.model_copy(
            update={"odoo_install_modules": LAUNCHPLANE_REQUIRED_ODOO_MODULES}
        )
        self.store.write_artifact_manifest(self.artifact)
        accepted = self.accept()
        operation = self.queue_promotion(accepted)
        now = datetime.now(UTC)
        current = self.store.claim_next_odoo_prod_promotion_operation_record(
            lease_owner="fictional-worker",
            claimed_at=now.isoformat(),
            lease_expires_at=(now + timedelta(minutes=5)).isoformat(),
        )
        assert current is not None
        self.assertEqual(current.operation_id, operation.operation_id)

        def source_write(**kwargs: Any) -> dict[str, str]:
            self.provider_payload["composeFile"] = kwargs["compose_file"]
            self.provider_payload["sourceType"] = "raw"
            return {}

        def environment_write(**kwargs: Any) -> None:
            self.provider_payload["env"] = kwargs["env_text"]

        def provider_request(*, path: str, **_: Any) -> Any:
            return (
                [{"host": "example.test", "domainId": "fictional-domain"}]
                if path == "/api/domain.byComposeId"
                else []
            )

        with (
            patch(
                "control_plane.workflows.odoo_prod_promotion.execute_odoo_stable_target_replacement_apply",
                side_effect=partial(
                    replacement.execute_odoo_stable_target_replacement_apply,
                    dokploy_request=provider_request,
                ),
            ),
            patch.object(
                replacement,
                "build_odoo_stable_target_replacement_plan",
                wraps=replacement.build_odoo_stable_target_replacement_plan,
            ) as plan,
            patch.object(api, "dokploy_request", side_effect=provider_request),
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
            patch.object(api, "latest_deployment_for_target", return_value=None),
            patch.object(api, "trigger_deployment", side_effect=ReachedDeployment),
            patch.object(replacement, "require_forward_lane_build"),
        ):
            with self.assertRaises(ReachedDeployment):
                result = execute_odoo_prod_promotion_run(
                    control_plane_root=Path("."),
                    state_dir=Path("."),
                    database_url=self.store.database_url,
                    record_store=self.store,
                    request=current.request,
                    release_decision=accepted,
                    operation_id=current.operation_id,
                    operation_lease_owner=current.lease_owner,
                    provider_effect_checkpoint=lambda _: None,
                )
                self.fail(result.error_message)
        self.assertTrue(plan.called)
        delivered = api.parse_dokploy_env_text(str(self.provider_payload["env"]))
        self.assertTrue(set(self.runtime.env) | set(keys) <= delivered.keys())
        for key in keys:
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

        if program == first_release_park_program():
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
            super().observe(payload=payload, receive=receive, **kwargs)

    def test_first_drill_retains_data_then_takes_real_dump_and_repromotes(self) -> None:
        from control_plane.client_release import _queue_rollback, ClientReleaseStep
        from control_plane.timestamps import utc_now_timestamp
        from control_plane.release_review import release_version
        from control_plane.odoo_first_release import FIRST_RELEASE_ABSENCE_SOURCE
        from tests.test_odoo_prod_backup_gate import _passing_backup_capture_evidence

        self.accepted = self.accept()
        first = self.queue_promotion(self.accepted)
        self.modes: list[str] = []
        with patch(
            "control_plane.workflows.odoo_prod_promotion.execute_odoo_stable_target_replacement_apply",
            side_effect=self.provider_replacement,
        ):
            self.assertTrue(self.run_worker().terminal_write_committed)
            forward = self.store.read_odoo_prod_promotion_operation_record(first.operation_id)
            self.assertEqual(forward.status, "pass", forward.error_message)
            assert forward.result is not None
            gate = self.store.read_backup_gate_record(forward.result.backup_record_id)
            self.assertEqual(gate.source, FIRST_RELEASE_ABSENCE_SOURCE)
            self.assertNotIn("database_dump_sha256", gate.evidence)
            retained = dict(self.docker_state.volumes)
            rollback_id = _queue_rollback(
                self.store,
                Path("."),
                self.profile,
                self.accepted,
                ClientReleaseStep("rollback", 1),
                self.context,
                utc_now_timestamp(),
            )
            self.assertTrue(self.run_worker().terminal_write_committed)
            inverse = self.store.read_odoo_prod_rollback_operation_record(rollback_id)
            self.assertEqual(inverse.status, "pass", inverse.error_message)
            self.assertEqual(self.docker_state.volumes, retained)
            self.assertFalse(self.docker_state.rows["web-id"]["State"]["Running"])
            self.assertEqual(
                release_version(store=self.store, profile=self.profile, instance="prod"),
                self.accepted.checklist.production,
            )
            second = self.queue_promotion(self.accepted, 2)

            def capture(**kwargs: Any) -> dict[str, Any]:
                self.assertFalse(self.docker_state.rows["web-id"]["State"]["Running"])
                self.assertEqual(kwargs["expected_web_state"], "exited")
                return {
                    **_passing_backup_capture_evidence(),
                    "backup_nonce": kwargs["backup_nonce"],
                    "backup_record_id": kwargs["backup_record_id"],
                    "database_name": kwargs["database_name"],
                }

            with patch(
                "control_plane.dokploy.post_deploy.run_compose_odoo_backup_gate",
                side_effect=capture,
            ) as dump:
                self.assertTrue(self.run_worker().terminal_write_committed)
            repeated = self.store.read_odoo_prod_promotion_operation_record(second.operation_id)
            self.assertEqual(repeated.status, "pass", repeated.error_message)
            dump.assert_called_once()
            self.assertEqual(self.modes, ["empty", "existing"])
            self.assertEqual(
                release_version(store=self.store, profile=self.profile, instance="prod"),
                self.accepted.checklist.candidate,
            )
            self.assertEqual(self.docker_state.volumes, retained)

    def test_failed_first_deploy_queues_same_acceptance_recovery_without_deleting_data(
        self,
    ) -> None:
        self.accepted = self.accept()
        first = self.queue_promotion(self.accepted)
        self.modes = []
        self.fail_deployment = True
        with patch(
            "control_plane.workflows.odoo_prod_promotion.execute_odoo_stable_target_replacement_apply",
            side_effect=self.provider_replacement,
        ):
            self.assertTrue(self.run_worker().terminal_write_committed)
        failed = self.store.read_odoo_prod_promotion_operation_record(first.operation_id)
        self.assertEqual(failed.status, "fail")
        retained = dict(self.docker_state.volumes)
        recoveries = self.store.list_odoo_prod_rollback_operation_records()
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(
            recoveries[0].authorization.release_decision_record_id, self.accepted.record_id
        )
        self.assertTrue(self.run_worker().terminal_write_committed)
        recovery = self.store.read_odoo_prod_rollback_operation_record(recoveries[0].operation_id)
        self.assertEqual(recovery.status, "pass", recovery.error_message)
        self.assertEqual(self.docker_state.volumes, retained)
        self.assertFalse(self.docker_state.rows["web-id"]["State"]["Running"])

    def test_foreign_backup_prevents_initial_provider_reads_or_writes(self) -> None:
        from control_plane.contracts.backup_gate_record import BackupGateRecord
        from control_plane.timestamps import utc_now_timestamp

        accepted = self.accept()
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

    def test_lost_parking_response_holds_original_operation_without_reexecution(self) -> None:
        from control_plane.client_release import _queue_rollback, ClientReleaseStep
        from control_plane.timestamps import utc_now_timestamp
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable
        from control_plane.odoo_first_release import first_release_park_program

        self.accepted = self.accept()
        self.queue_promotion(self.accepted)
        self.modes = []
        with patch(
            "control_plane.workflows.odoo_prod_promotion.execute_odoo_stable_target_replacement_apply",
            side_effect=self.provider_replacement,
        ):
            self.run_worker()
        inverse_id = _queue_rollback(
            self.store,
            Path("."),
            self.profile,
            self.accepted,
            ClientReleaseStep("rollback", 1),
            self.context,
            utc_now_timestamp(),
        )

        def lost_response(**kwargs: Any) -> None:
            self.observe(**kwargs)
            if kwargs["program"] == first_release_park_program():
                raise NativeSourceUnavailable("fictional response loss")

        self.probe.side_effect = lost_response
        retained = dict(self.docker_state.volumes)
        self.run_worker()
        operation = self.store.read_odoo_prod_rollback_operation_record(inverse_id)
        self.assertEqual(operation.status, "reconciliation_required")
        self.assertEqual(self.docker_state.volumes, retained)
        observed = self.probe.call_count
        self.run_worker()
        self.assertEqual(self.probe.call_count, observed)
        self.assertEqual(
            self.store.read_odoo_prod_rollback_operation_record(inverse_id).operation_id,
            operation.operation_id,
        )

    def test_parking_publication_cannot_overwrite_concurrent_inventory(self) -> None:
        from control_plane.client_release import _queue_rollback, ClientReleaseStep
        from control_plane.timestamps import utc_now_timestamp
        from control_plane.odoo_first_release import first_release_park_program, STOPPED_RELEASE_KEY

        self.accepted = self.accept()
        self.queue_promotion(self.accepted)
        self.modes = []
        with patch(
            "control_plane.workflows.odoo_prod_promotion.execute_odoo_stable_target_replacement_apply",
            side_effect=self.provider_replacement,
        ):
            self.run_worker()
        inverse_id = _queue_rollback(
            self.store,
            Path("."),
            self.profile,
            self.accepted,
            ClientReleaseStep("rollback", 1),
            self.context,
            utc_now_timestamp(),
        )
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
        self.assertNotIn(STOPPED_RELEASE_KEY, current.post_deploy_update.evidence)


class FakeReleaseDocker:
    def __init__(self, artifact: str, deployment: str, volumes: tuple[str, ...]) -> None:
        self.volumes = {name: {"retained-business-data": object()} for name in volumes}
        self.writes: list[tuple[str, ...]] = []
        self.rows: dict[str, dict[str, Any]] = {
            "web-id": {
                "Id": "web-id",
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": "example-prod",
                        "com.docker.compose.service": "web",
                    },
                    "Env": [
                        f"LAUNCHPLANE_ARTIFACT_ID={artifact}",
                        f"LAUNCHPLANE_DEPLOYMENT_RECORD_ID={deployment}",
                    ],
                },
                "Mounts": [{"Type": "volume", "Name": volumes[1]}],
                "State": {"Running": True},
                "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
            },
        }

    def __call__(self, *args: str) -> str:
        if args[0] == "info":
            return json.dumps({"LocalNodeState": "inactive"})
        if args[0] == "ps":
            return "\n".join(self.rows)
        if args[:2] == ("volume", "ls"):
            return "\n".join(self.volumes)
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
        for name in ("database", "script-runner"):
            docker.rows[name] = {
                **docker.rows["web-id"],
                "Id": name,
                "State": {"Running": True},
                "Config": {
                    **docker.rows["web-id"]["Config"],
                    "Cmd": ["tail", "-f", "/dev/null"],
                    "Labels": {
                        "com.docker.compose.project": "example-prod",
                        "com.docker.compose.service": name,
                    },
                },
            }
        spec = {
            "project": "example-prod",
            "volumes": list(docker.volumes),
            "artifact": "artifact",
            "deployments": ["deployment"],
        }
        retained = dict(docker.volumes)

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
        self.assertEqual(docker.volumes, retained)

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
                {
                    "project": "example-prod",
                    "volumes": list(docker.volumes),
                    "artifact": "artifact",
                    "deployments": ["deployment"],
                },
            )
        self.assertEqual(docker.writes, [])
        self.assertTrue(docker.rows["web-id"]["State"]["Running"])
