"""The accepted-release adapter uses real custody, backup records and publication."""

from datetime import UTC, datetime, timedelta
from contextlib import redirect_stdout
import io
import json
import subprocess
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.durable_operation_authorization import DurableOperationAuthorization
from control_plane.contracts.production_backup_authority import ProductionBackupPolicyRecord
from control_plane.contracts.production_backup_gate import ProductionBackupGateRequest
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.secret_record import SecretRecord, SecretBinding
from control_plane.contracts.runtime_identity import runtime_identity_env
from control_plane.production_recipient import custody, runtime_values
from control_plane.production_recipient_cutover import (
    RecipientCutoverAdapter,
    RecipientCutoverRequest,
    cutover_intent,
    save_cutover_intent,
)
from control_plane.provider_operations import run_durable_provider_operation
from control_plane.runtime_environments import SiteRuntimeEnvironment
from control_plane.dokploy import api
from control_plane.dokploy.post_deploy import ODOO_POST_DEPLOY_SCHEDULE_TRIGGER
from control_plane.workflows.production_backup_gate import enqueue_production_backup_gate
from control_plane.workflows.production_backup_gate_operation_worker import (
    run_production_backup_gate_operation_worker_once,
)
from tests import test_production_recipient_publication as publication_tests
from tests.test_production_backup_provider import BackupHost, setup_memory_files
from tests.support.durable_operations import (
    durable_operation_authorization_payload,
    durable_operation_policy_record,
)


class RecipientCutoverTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        self.publication_case = publication_tests.RecipientPublicationTests()
        self.publication_case.database_url = self.database_url
        with patch.object(self.publication_case, "customize_preparation", side_effect=self.seed):
            self.publication_case.setUp()
        self.addCleanup(self.publication_case.doCleanups)
        self.fixture = self.publication_case.fixture
        f = self.fixture
        self.store = f.store
        self.record = self.publication_case.record
        self.accepted = True
        self.old_running = True
        self.new_running = False
        self.actions: list[tuple[str, bool]] = []
        self.deploys = 0
        setup_memory_files(self)
        auth = durable_operation_authorization_payload(
            action="production_backup_gate.execute",
            managed_rule_id="recipient-fixture-backup",
            product=f.product,
            context=f.context,
            instances=("prod",),
        )
        self.store.seed_authz_policy_if_absent(durable_operation_policy_record(auth))
        self.authorization = DurableOperationAuthorization.model_validate(auth)
        self.old_backup = self.capture(self.publication_case.policy, "recipient-old-backup")
        self.destination_backup = self.capture(
            self.destination_policy, "recipient-destination-backup"
        )
        self.request = RecipientCutoverRequest(
            context=f.context,
            preparation_plan_sha256=self.record.plan_sha256,
            health_urls={
                self.publication_case.monitor.endpoint_key: "http://192.168.200.20:19220/health"
            },
            readiness_url="http://192.168.200.20:19220/readyz",
            backup_sources={
                row.policy_id: "new-source-guest"
                for row in self.store.list_production_backup_policy_records(
                    product=f.product, status="active"
                )
            },
            writer_binding_id="fixture-writer",
            destination_backup_operation_id=self.destination_backup,
            reason="Review exact independent recipient publication",
        )
        intent = cutover_intent(f.root, self.store, self.record, self.request)
        save_cutover_intent(self.store, self.record, intent)
        current = custody(self.store, f.product, f.context)
        assert current is not None
        self.record = current
        self.adapter = RecipientCutoverAdapter(
            f.root,
            self.store,
            current,
            "client-release-fixture",
            self.old_backup,
            self.validate_release,
            validate_source=self.validate_release,
        )
        for replacement in (
            patch(
                "control_plane.production_recipient_cutover.inspect_resources",
                side_effect=f.inspect_resources,
            ),
            patch(
                "control_plane.production_recipient_cutover.writer_action", side_effect=self.writer
            ),
            patch(
                "control_plane.production_recipient_cutover.execute_dokploy_artifact_deploy",
                side_effect=self.deploy,
            ),
            patch(
                "control_plane.production_recipient_cutover._verify_health_evidence_with_identity",
                side_effect=self.health,
            ),
            patch.object(self.adapter, "verify_runtime"),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def seed(self) -> None:
        f = self.publication_case.fixture
        self.destination_policy = ProductionBackupPolicyRecord.model_validate(
            {
                **self.publication_case.policy.model_dump(mode="json"),
                "record_id": "",
                "policy_id": "",
                "policy_digest": "",
                "promotion_action": "recipient_destination_qualification",
                "fast_snapshot": {
                    **self.publication_case.policy.fast_snapshot.model_dump(mode="json"),
                    "source_target_id": "new-source-guest",
                },
                "independent_backup": {
                    **self.publication_case.policy.independent_backup.model_dump(mode="json"),
                    "source_target_id": "new-source-guest",
                },
            }
        )
        f.store.write_production_backup_policy_record(self.destination_policy)
        stamp = f.old.updated_at
        from control_plane.contracts.runtime_identity import RuntimeIdentity

        inventory = self.publication_case.inventory.model_copy(
            update={
                "runtime_identity": RuntimeIdentity(
                    product=f.product,
                    context=f.context,
                    instance="prod",
                    environment_kind="stable",
                    deployment_record_id="retained-deployment",
                    artifact_id="old-artifact",
                    source_git_ref="b" * 40,
                    image_reference="example@sha256:" + "c" * 64,
                )
            }
        )
        self.publication_case.inventory = inventory
        f.store.write_environment_inventory(inventory)
        secret = SecretRecord(
            secret_id="fixture-writer-secret",
            scope="context_instance",
            integration="runtime_environment",
            name="SYNC_API_TOKEN",
            context=f.context,
            instance="prod",
            current_version_id="fixture-api-version",
            created_at=stamp,
            updated_at=stamp,
        )
        f.store.write_secret_record(secret)
        f.store.write_secret_binding(
            SecretBinding(
                binding_id="fixture-writer",
                secret_id=secret.secret_id,
                integration=secret.integration,
                binding_key=secret.name,
                context=f.context,
                instance="prod",
                created_at=stamp,
                updated_at=stamp,
            )
        )
        f.values["SYNC_API_TOKEN"] = "managed-fixture-api-token"
        f.document["services"]["web"]["environment"]["SYNC_API_TOKEN"] = "${SYNC_API_TOKEN}"
        replacement = patch(
            "control_plane.production_recipient.runtime_environments.resolve_site_runtime_environment",
            side_effect=lambda **kwargs: SiteRuntimeEnvironment(
                values=dict(f.values), secret_keys=frozenset({"DB_PASSWORD", "SYNC_API_TOKEN"})
            ),
        )
        replacement.start()
        self.publication_case.addCleanup(replacement.stop)

    def capture(self, policy: ProductionBackupPolicyRecord, record_id: str) -> str:
        request = ProductionBackupGateRequest(
            product=self.fixture.product,
            context=self.fixture.context,
            instance="prod",
            promotion_action=policy.promotion_action,
            backup_record_id=record_id,
        )
        operation = enqueue_production_backup_gate(
            record_store=self.store,
            request=request,
            authorization=self.authorization,
            operation_key="fixture-capture|" + record_id,
        )
        backend = BackupHost()
        backend.source = self.store.list_production_backup_target_records(
            target_id=policy.fast_snapshot.source_target_id, status="active"
        )[0].destination  # type: ignore[assignment]
        with (
            patch(
                "control_plane.workflows.production_backup_gate_operation_worker._utc_now_timestamp",
                side_effect=lambda: datetime.now(UTC).isoformat(),
            ),
            patch(
                "control_plane.workflows.production_backup_provider.utc_now_timestamp",
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
                side_effect=backend.run,
            ),
        ):
            result = run_production_backup_gate_operation_worker_once(
                record_store=self.store,
                control_plane_root_path=self.fixture.root,
                lease_owner="fixture-backup",
            )
        self.assertTrue(result.terminal_write_committed)
        self.assertEqual(self.store.read_backup_gate_record(record_id).status, "pass")
        return operation.operation_id if policy == self.destination_policy else record_id

    def validate_release(self) -> None:
        if not self.accepted:
            raise ValueError("Client acceptance withdrawn")

    def writer(
        self,
        root: Any,
        record: Any,
        *,
        old: bool,
        action: str,
        checkpoint: Any,
        binding: str | None = None,
    ) -> None:
        from control_plane.production_recipient_cutover import WRITER_LIBRARY

        checkpoint(ODOO_POST_DEPLOY_SCHEDULE_TRIGGER)
        self.actions.append((action, old))
        project = "old-production" if old else record.app_name
        identity = (
            record.authority_snapshot["inventory"]["runtime_identity"]
            if old
            else record.execution.get("runtime_identity")
        )

        def docker(args: list[str], **kwargs: Any) -> Any:
            command = args[1]
            identifier = "old-writer" if old else "new-writer"
            if command == "ps":
                output = identifier if old or self.deploys else ""
            elif command == "inspect":
                output = json.dumps(
                    [
                        {
                            "Image": identifier + "-image",
                            "State": {"Running": self.old_running if old else self.new_running},
                            "Config": {
                                "Image": record.image_reference,
                                "Labels": {
                                    "com.docker.compose.project": project,
                                    "com.docker.compose.service": record.request.application_service,
                                },
                                "Env": [
                                    "LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + json.dumps(identity)
                                ],
                            },
                        }
                    ]
                )
            elif command in {"start", "stop"}:
                if old and command == "start" and getattr(self, "fail_old_start_once", False):
                    self.fail_old_start_once = False
                    raise TimeoutError("old writer start interrupted")
                self.assertEqual(args[-1], identifier)
                if old:
                    self.old_running = command == "start"
                else:
                    self.new_running = command == "start"
                output = identifier
            else:
                self.fail("Unexpected writer operation")
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

        scope: dict[str, Any] = {}
        with patch("subprocess.run", side_effect=docker), redirect_stdout(io.StringIO()):
            exec(WRITER_LIBRARY, scope)
            scope["coordinate_writer"](
                {
                    "project": project,
                    "service": record.request.application_service,
                    "binding": binding
                    or record.execution.get("writer_binding", record.plan_sha256),
                    "action": action,
                    "expected_identity": identity,
                    "image": record.image_reference,
                    "owned_replacement": not old,
                },
                base=self.fixture.root.resolve() / "writer-custody",
            )

    def deploy(self, **kwargs: Any) -> None:
        kwargs["before_provider_mutation"]("target_update")
        current = custody(self.store, self.fixture.product, self.fixture.context)
        assert current is not None
        self.fixture.provider[current.target_id]["env"] = api.serialize_dokploy_env_text(
            {
                **runtime_values(self.store, self.fixture.root, current),
                **runtime_identity_env(kwargs["runtime_identity"]),
            },
        )
        kwargs["before_provider_mutation"]("deploy_trigger")
        self.assertFalse(self.old_running)
        self.deploys += 1
        self.new_running = True

    def health(
        self, evidence: HealthcheckEvidence, *, expected_runtime_identity: Any
    ) -> HealthcheckEvidence:
        return evidence.model_copy(
            update={
                "status": "pass",
                "verified": True,
                "runtime_identity_status": "match",
                "observed_runtime_identity": expected_runtime_identity,
            }
        )

    def run_release(self) -> Any:
        return run_durable_provider_operation(
            store=self.store,
            scope="client-release",
            route_path="/fixture/recipient-release",
            idempotency_key="accepted-release",
            request_fingerprint="e" * 64,
            lease_owner="fixture-release",
            response_trace_id="launchplane_req_cutover",
            adapter=self.adapter,
        )

    def test_accepted_release_bootstraps_then_publishes_one_atomic_set(self) -> None:
        result = self.run_release()
        self.assertEqual(result.response_status_code, 202, result.response_payload)
        payload = result.response_payload["result"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["promotion_status"], "pass")
        current = custody(self.store, self.fixture.product, self.fixture.context)
        assert current is not None
        self.assertEqual(current.phase, "published")
        self.assertFalse(self.old_running)
        self.assertTrue(self.new_running)
        self.assertEqual(self.deploys, 1)
        self.assertEqual(
            self.store.read_dokploy_target_id_record(
                context_name=current.context, instance_name="prod"
            ).target_id,
            current.target_id,
        )
        inventory = self.store.read_environment_inventory(
            context_name=current.context, instance_name="prod"
        )
        self.assertEqual(inventory.runtime_identity.image_reference, current.image_reference)  # type: ignore[union-attr]
        self.assertEqual(
            self.store.read_deployment_record(inventory.deployment_record_id).runtime_identity,
            inventory.runtime_identity,
        )
        self.assertEqual(self.run_release().response_status_code, 202)
        self.assertEqual(self.deploys, 1)

    def test_unaccepted_release_cannot_pause_or_start_a_writer(self) -> None:
        self.accepted = False
        with self.assertRaises(ValueError):
            self.run_release()
        self.assertEqual(self.actions, [])
        self.assertEqual(self.deploys, 0)
        self.fixture.assert_old_preserved()

    def test_terminal_bad_health_stops_only_new_writer_and_restores_old(self) -> None:
        with patch(
            "control_plane.production_recipient_cutover._verify_health_evidence_with_identity",
            return_value=HealthcheckEvidence(status="fail"),
        ):
            result = self.run_release()
        self.assertEqual(
            result.response_payload["result"]["promotion_status"], "fail", result.response_payload
        )
        self.assertFalse(self.new_running)
        self.assertTrue(self.old_running)
        self.assertIn(("pause", False), self.actions)
        current = custody(self.store, self.fixture.product, self.fixture.context)
        assert current is not None
        self.assertEqual(current.phase, "rolled_back")
        self.fixture.assert_old_preserved()

    def test_unknown_deploy_keeps_original_fence_and_never_restarts_old(self) -> None:
        def unknown(**kwargs: Any) -> None:
            self.deploy(**kwargs)
            raise TimeoutError("provider reply lost")

        with patch(
            "control_plane.production_recipient_cutover.execute_dokploy_artifact_deploy",
            side_effect=unknown,
        ):
            result = self.run_release()
        self.assertEqual(result.response_status_code, 409)
        self.assertFalse(self.old_running)
        self.assertTrue(self.new_running)
        self.assertNotIn(("resume", True), self.actions)
        with patch.object(
            self.adapter,
            "terminal_deployment",
            return_value={"title": "original", "status": "done"},
        ):
            resumed = self.run_release()
        self.assertEqual(resumed.response_status_code, 202, resumed.response_payload)
        self.assertEqual(self.deploys, 1)

    def test_capture_only_or_wrong_source_cannot_arm_bootstrap(self) -> None:
        record = self.record.model_copy(update={"recovery_proof": {}})
        with self.assertRaises(ValueError):
            cutover_intent(self.fixture.root, self.store, record, self.request)
        wrong = self.request.model_copy(
            update={"destination_backup_operation_id": "missing-destination"}
        )
        with self.assertRaises(FileNotFoundError):
            cutover_intent(self.fixture.root, self.store, self.record, wrong)

    def test_drill_restores_retained_data_then_repromotes_without_reinitialization(self) -> None:
        from control_plane.production_recipient_inverse import run_recipient_inverse

        self.assertEqual(self.run_release().response_status_code, 202)
        published = custody(self.store, self.fixture.product, self.fixture.context)
        assert published is not None
        with (
            patch(
                "control_plane.production_recipient_inverse.writer_action", side_effect=self.writer
            ),
            patch(
                "control_plane.production_recipient_inverse._verify_health_evidence_with_identity",
                side_effect=self.health,
            ),
        ):
            operation = run_recipient_inverse(
                root=self.fixture.root,
                store=self.store,
                record=published,
                scope="client-release",
                route_path="/fixture/inverse",
                key="drill-rollback",
                trace=self.adapter.trace_id,
                validate_release=self.validate_release,
            )
        self.assertTrue(operation)
        current = custody(self.store, self.fixture.product, self.fixture.context)
        assert current is not None
        self.assertEqual(current.phase, "rolled_back")
        self.assertTrue(self.old_running)
        self.assertFalse(self.new_running)
        self.assertEqual(
            self.store.read_environment_inventory(
                context_name=current.context, instance_name="prod"
            ),
            self.publication_case.inventory,
        )
        policy = next(
            row
            for row in self.store.list_production_backup_policy_records(
                product=current.product, status="active"
            )
            if row.promotion_action == self.publication_case.policy.promotion_action
        )
        binding = next(
            row
            for row in self.store.list_secret_bindings()
            if row.binding_id == self.request.writer_binding_id
        )
        self.store.write_secret_binding(
            binding.model_copy(update={"updated_at": "2026-10-16T11:00:00Z"})
        )
        backup = self.capture(policy, "second-release-backup")
        adapter = RecipientCutoverAdapter(
            self.fixture.root,
            self.store,
            current,
            self.adapter.trace_id,
            backup,
            self.validate_release,
            validate_source=self.validate_release,
        )
        source_qualified = False

        def admit_source() -> None:
            if not source_qualified:
                raise ValueError("Testing is not qualified for this repromotion.")

        adapter.validate_source = admit_source

        def advance_after_pause(*args: Any, **kwargs: Any) -> None:
            nonlocal source_qualified
            self.writer(*args, **kwargs)
            if kwargs.get("old") and kwargs.get("action") == "pause":
                source_qualified = False

        def run_second() -> Any:
            return run_durable_provider_operation(
                store=self.store,
                scope="client-release",
                route_path="/fixture/recipient-release",
                idempotency_key="accepted-release-promote-2",
                request_fingerprint="f" * 64,
                lease_owner="fixture-release",
                response_trace_id="launchplane_req_second",
                adapter=adapter,
            )

        with patch.object(adapter, "verify_runtime"):
            self.accepted = False
            with self.assertRaises(ValueError):
                run_second()
            self.assertTrue(self.old_running)
            self.assertFalse(self.new_running)
            self.accepted = True
            # Generic pause/trigger facts from the original release cannot
            # admit this new attempt. Its own dispatch marker then survives B.
            with self.assertRaises(ValueError):
                run_second()
            source_qualified = True
            with patch(
                "control_plane.production_recipient_cutover.writer_action",
                side_effect=advance_after_pause,
            ):
                result = run_second()
        self.assertEqual(result.response_status_code, 202, result.response_payload)
        payload = result.response_payload["result"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["promotion_status"], "pass")
        self.assertFalse(self.old_running)
        self.assertTrue(self.new_running)
        self.assertEqual(self.deploys, 1)
        final = custody(self.store, current.product, current.context)
        assert final is not None
        self.assertEqual(final.phase, "published")
        self.assertTrue(final.publication["history"])

    def test_acceptance_revoked_at_publication_restores_old_without_partial_authority(self) -> None:
        from control_plane.production_recipient_publication import commit_publication

        def revoke(*args: Any, **kwargs: Any) -> Any:
            self.accepted = False
            return commit_publication(*args, **kwargs)

        with patch(
            "control_plane.production_recipient_cutover.commit_publication", side_effect=revoke
        ):
            result = self.run_release()
        self.assertEqual(result.response_payload["result"]["promotion_status"], "fail")
        self.assertFalse(self.new_running)
        self.assertTrue(self.old_running)
        self.fixture.assert_old_preserved()

    def test_existing_peer_operation_blocks_bootstrap_before_any_writer_effect(self) -> None:
        from control_plane.workflows.generic_web_deploy_provider import (
            generic_web_provider_target_key,
        )

        key = generic_web_provider_target_key(
            provider_id="dokploy",
            context=self.record.context,
            instance="prod",
            target_type="compose",
            target_id=self.record.retained_provider.target_id,
        )
        self.store.reserve_mutation(
            scope="peer",
            route_path="/fixture/restart",
            idempotency_key="peer",
            request_fingerprint="b" * 64,
            lease_owner="peer",
            provider_target_key=key,
            reconciliation_key="peer-restart",
        )
        result = self.run_release()
        self.assertEqual(result.response_status_code, 409)
        self.assertEqual(self.actions, [])
        self.assertEqual(self.deploys, 0)

    def test_reserved_peer_cannot_cross_effect_checkpoint_during_bootstrap(self) -> None:
        from control_plane.workflows.generic_web_deploy_provider import (
            generic_web_provider_target_key,
        )

        replacement = self.record.model_copy(update={"phase": "bootstrapping"})
        self.store.write_production_recipient(
            expected=self.record.model_dump(mode="json"),
            replacement=replacement.model_dump(mode="json"),
        )
        key = generic_web_provider_target_key(
            provider_id="dokploy",
            context=self.record.context,
            instance="prod",
            target_type="compose",
            target_id=self.record.retained_provider.target_id,
        )
        peer = self.store.reserve_mutation(
            scope="peer",
            route_path="/fixture/restart",
            idempotency_key="peer",
            request_fingerprint="b" * 64,
            lease_owner="peer",
            provider_target_key=key,
            reconciliation_key="peer-restart",
        ).record
        assert peer is not None
        with self.assertRaises(ValueError):
            self.store.checkpoint_mutation_provider_effect(reservation=peer, effect_phase="start")

    def test_unused_intent_can_refresh_destination_capture_and_retains_its_review(self) -> None:
        backup = self.capture(self.destination_policy, "refreshed-destination")
        request = self.request.model_copy(update={"destination_backup_operation_id": backup})
        intent = cutover_intent(self.fixture.root, self.store, self.record, request)
        save_cutover_intent(self.store, self.record, intent)
        current = custody(self.store, self.record.product, self.record.context)
        assert current is not None
        self.assertEqual(current.activation_history, [self.record.activation])
        self.assertEqual(current.activation, intent)

    def test_admitted_destination_capture_remains_historical_proof_during_reconciliation(
        self,
    ) -> None:
        self.run_release()
        record = custody(self.store, self.record.product, self.record.context)
        assert record is not None
        maximum = max(
            self.destination_policy.fast_snapshot.max_evidence_age_seconds,
            self.destination_policy.independent_backup.max_evidence_age_seconds,
        )
        future = max(
            datetime.now(UTC) + timedelta(seconds=maximum + 1),
            *(
                datetime.fromisoformat(row["review_after"].replace("Z", "+00:00"))
                + timedelta(seconds=1)
                for row in record.activation["source_records"]
            ),
        )

        class FutureDate(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> Any:
                return future

        with patch("control_plane.production_recipient_cutover.datetime", FutureDate):
            with self.assertRaises(ValueError):
                cutover_intent(self.fixture.root, self.store, record, self.request)
            self.assertEqual(
                cutover_intent(
                    self.fixture.root, self.store, record, self.request, check_freshness=False
                ),
                record.activation,
            )

    def test_lost_commit_reply_uses_published_custody_without_stopping_new_writer(self) -> None:
        from control_plane.production_recipient_publication import commit_publication

        def lost_reply(*args: Any, **kwargs: Any) -> Any:
            commit_publication(*args, **kwargs)
            raise TimeoutError("commit reply lost")

        with patch(
            "control_plane.production_recipient_cutover.commit_publication", side_effect=lost_reply
        ):
            result = self.run_release()
        self.assertEqual(result.response_payload["result"]["promotion_status"], "pass")
        self.assertTrue(self.new_running)
        self.assertFalse(self.old_running)
        self.assertNotIn(("pause", False), self.actions)

    def test_predispatch_pause_refusal_releases_budget_and_original_key_can_retry(self) -> None:
        def refuse(*args: Any, **kwargs: Any) -> None:
            if kwargs.get("old") and kwargs.get("action") == "pause":
                raise ValueError("pending provider deployment")
            self.writer(*args, **kwargs)

        with patch("control_plane.production_recipient_cutover.writer_action", side_effect=refuse):
            self.assertEqual(self.run_release().response_status_code, 409)
        current = custody(self.store, self.record.product, self.record.context)
        assert current is not None
        self.assertEqual(current.phase, "preflight_refused")
        self.assertTrue(self.old_running)
        self.assertEqual(self.run_release().response_status_code, 202)

    def test_missing_peer_target_does_not_block_an_unrelated_products_effect(self) -> None:
        record = self.record.model_copy(update={"phase": "bootstrapping"})
        self.store.write_production_recipient(
            expected=self.record.model_dump(mode="json"), replacement=record.model_dump(mode="json")
        )
        binding = next(
            row
            for row in self.store.list_secret_bindings()
            if row.binding_id == self.request.writer_binding_id
        )
        self.store.write_secret_binding(
            binding.model_copy(
                update={
                    "binding_id": "missing-peer",
                    "context": "another-context",
                    "instance": "testing",
                }
            )
        )
        peer = self.store.reserve_mutation(
            scope="another-product",
            route_path="/fixture/other-deploy",
            idempotency_key="original",
            request_fingerprint="a" * 64,
            lease_owner="other",
            provider_target_key="other-products-target",
            reconciliation_key="other-deploy",
        ).record
        assert peer is not None
        self.assertEqual(
            self.store.checkpoint_mutation_provider_effect(
                reservation=peer, effect_phase="deploy"
            ).status,
            "updated",
        )

    def test_binding_metadata_touch_does_not_block_the_exact_inverse(self) -> None:
        from control_plane.production_recipient_inverse import run_recipient_inverse

        self.run_release()
        current = custody(self.store, self.record.product, self.record.context)
        assert current is not None
        binding = next(
            row
            for row in self.store.list_secret_bindings()
            if row.binding_id == self.request.writer_binding_id
        )
        self.store.write_secret_binding(
            binding.model_copy(update={"updated_at": "2026-10-16T10:00:00Z"})
        )
        with (
            patch(
                "control_plane.production_recipient_inverse.writer_action", side_effect=self.writer
            ),
            patch(
                "control_plane.production_recipient_inverse._verify_health_evidence_with_identity",
                side_effect=self.health,
            ),
        ):
            run_recipient_inverse(
                root=self.fixture.root,
                store=self.store,
                record=current,
                scope="client-release",
                route_path="/fixture/inverse",
                key="inverse",
                trace=self.adapter.trace_id,
                validate_release=self.validate_release,
            )
        restored = custody(self.store, self.record.product, self.record.context)
        assert restored is not None
        self.assertEqual(restored.phase, "rolled_back")
        self.assertTrue(self.old_running)
        self.assertFalse(self.new_running)

    def test_unknown_host_pause_requires_a_terminal_witness_before_retry(self) -> None:
        pauses = []

        def unknown(*args: Any, **kwargs: Any) -> None:
            if kwargs.get("old") and kwargs.get("action") == "pause":
                pauses.append("pause")
                kwargs["checkpoint"](ODOO_POST_DEPLOY_SCHEDULE_TRIGGER)
                self.old_running = False
                raise TimeoutError("host pause response lost")
            if kwargs.get("action") in {"resume", "receipt_pause"}:
                raise ValueError("terminal host witness unavailable")
            self.writer(*args, **kwargs)

        with patch("control_plane.production_recipient_cutover.writer_action", side_effect=unknown):
            self.assertEqual(self.run_release().response_status_code, 409)
            self.assertEqual(self.run_release().response_status_code, 409)
        self.assertEqual(pauses, ["pause"])
        self.assertFalse(self.old_running)
        self.assertEqual(self.deploys, 0)
        current = custody(self.store, self.record.product, self.record.context)
        assert current is not None
        self.assertEqual(current.phase, "bootstrapping")

    def test_settled_failed_bootstrap_can_supersede_with_both_writer_hosts_fenced(self) -> None:
        from control_plane.production_recipient_continuation import (
            RecipientContinuationRequest,
            continuation_plan,
            supersede_recipient,
        )
        from control_plane.integration_allowances import canonical_sha256

        self.test_terminal_bad_health_stops_only_new_writer_and_restores_old()
        current = custody(self.store, self.record.product, self.record.context)
        assert current is not None
        request = RecipientContinuationRequest(
            context=current.context,
            original_key=current.original_key,
            expected_custody_sha256=canonical_sha256(current.model_dump(mode="json")),
            replacement=current.request.model_copy(
                update={"mode": "dry-run", "reviewed_plan_sha256": ""}
            ),
            reason="Retain failed bootstrap and requalify a new generation",
        )
        draft = continuation_plan(
            self.fixture.root, self.store, current.product, request, current.actor, current.scope
        )[2]
        with patch(
            "control_plane.production_recipient_continuation.run_recipient_program",
            return_value={"recipient_execution_fenced": current.plan_sha256},
        ) as fence:
            supersede_recipient(
                self.fixture.root,
                self.store,
                current.product,
                request.model_copy(
                    update={"mode": "apply", "reviewed_plan_sha256": draft["plan_sha256"]}
                ),
                current.actor,
                current.scope,
                lambda _phase: None,
            )
        replacement = custody(self.store, current.product, current.context)
        assert replacement is not None
        self.assertEqual(replacement.original_key, current.original_key)
        self.assertNotEqual(replacement.namespace, current.namespace)
        self.assertEqual(replacement.prior_attempts[-1]["execution"], current.execution)
        self.assertEqual(
            {call.args[1].target_id for call in fence.call_args_list},
            {current.target_id, current.retained_target_id.target_id},
        )
        self.assertFalse(replacement.execution)
        self.assertFalse(replacement.activation)

    def test_terminal_host_rollback_can_finish_its_journal_after_a_lost_write(self) -> None:
        original = self.store.write_production_recipient
        failed: list[bool] = []

        def lose_final_write(**kwargs: Any) -> None:
            if kwargs["replacement"]["phase"] == "rolled_back" and not failed:
                failed.append(True)
                self.store.write_product_profile_record(
                    self.fixture.profile.model_copy(update={"updated_at": "2026-10-16T11:00:00Z"})
                )
                raise RuntimeError("rollback journal reply lost")
            original(**kwargs)

        with (
            patch.object(self.store, "write_production_recipient", side_effect=lose_final_write),
            patch(
                "control_plane.production_recipient_cutover._verify_health_evidence_with_identity",
                return_value=HealthcheckEvidence(status="fail"),
            ),
        ):
            self.assertEqual(self.run_release().response_status_code, 409)
        self.assertTrue(self.old_running)
        self.assertFalse(self.new_running)
        result = self.run_release()
        self.assertEqual(result.response_status_code, 202, result.response_payload)
        self.assertEqual(result.response_payload["result"]["promotion_status"], "fail")
        self.assertEqual(self.deploys, 1)
        self.assertEqual(self.actions.count(("pause", True)), 1)
        current = custody(self.store, self.record.product, self.record.context)
        assert current is not None
        self.assertEqual(current.phase, "rolled_back")

    def test_inverse_preflight_peer_refusal_can_retry_under_the_same_key(self) -> None:
        from control_plane.production_recipient_inverse import RecipientInverseAdapter

        self.run_release()

        def inverse() -> Any:
            current = custody(self.store, self.record.product, self.record.context)
            assert current is not None
            adapter = RecipientInverseAdapter(
                self.fixture.root, self.store, current, self.adapter.trace_id, self.validate_release
            )
            return run_durable_provider_operation(
                store=self.store,
                scope="client-release",
                route_path="/fixture/inverse",
                idempotency_key="same-inverse",
                request_fingerprint="d" * 64,
                lease_owner="inverse",
                response_trace_id="launchplane_req_inverse",
                adapter=adapter,
            )

        def running_peer(root: Any, store: Any, record: Any, checkpoint: Any) -> None:
            checkpoint(ODOO_POST_DEPLOY_SCHEDULE_TRIGGER)
            raise ValueError("peer is running")

        with (
            patch(
                "control_plane.production_recipient_inverse.writer_action", side_effect=self.writer
            ),
            patch(
                "control_plane.production_recipient_inverse._verify_health_evidence_with_identity",
                side_effect=self.health,
            ),
        ):
            with patch(
                "control_plane.production_recipient_inverse.require_peers_parked",
                side_effect=running_peer,
            ):
                self.assertEqual(inverse().response_status_code, 409)
            current = custody(self.store, self.record.product, self.record.context)
            assert current is not None
            self.assertEqual(current.phase, "published")
            self.assertTrue(self.new_running)
            self.assertEqual(inverse().response_status_code, 202)
        self.assertTrue(self.old_running)
        self.assertFalse(self.new_running)

    def test_reviewed_native_inverse_works_without_minting_a_client_release_step(self) -> None:
        from control_plane.production_recipient_inverse import (
            RecipientInverseAdapter,
            RecipientInverseRequest,
            inverse_plan,
            INVERSE_ROUTE,
        )
        from control_plane.integration_allowances import canonical_sha256

        self.run_release()
        current = custody(self.store, self.record.product, self.record.context)
        assert current is not None
        request = RecipientInverseRequest(
            context=current.context,
            preparation_plan_sha256=current.plan_sha256,
            publication_sha256=current.publication["published_sha256"],
            reason="Restore only this publication's unchanged rows and retained writer",
        )
        plan = inverse_plan(self.store, current, request)
        request = request.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": plan.plan_sha256}
        )
        adapter = RecipientInverseAdapter(
            self.fixture.root,
            self.store,
            current,
            "native-inverse",
            self.validate_release,
            request=request,
        )
        with (
            patch(
                "control_plane.production_recipient_inverse.writer_action", side_effect=self.writer
            ),
            patch(
                "control_plane.production_recipient_inverse._verify_health_evidence_with_identity",
                side_effect=self.health,
            ),
        ):
            result = run_durable_provider_operation(
                store=self.store,
                scope="native-admin",
                route_path=INVERSE_ROUTE.format(product=current.product),
                idempotency_key="reviewed-inverse",
                request_fingerprint=canonical_sha256(request.model_dump(mode="json")),
                lease_owner="native-inverse",
                response_trace_id="launchplane_req_native_inverse",
                adapter=adapter,
            )
        self.assertEqual(result.response_status_code, 202, result.response_payload)
        payload = result.response_payload["result"]
        assert isinstance(payload, dict)
        self.assertTrue(payload["completed"])
        self.assertEqual(payload["plan_sha256"], plan.plan_sha256)
        self.assertTrue(self.old_running)
        self.assertFalse(self.new_running)

    def test_interrupted_old_writer_start_retries_original_rollback_without_redeploy(self) -> None:
        self.fail_old_start_once = True
        with patch(
            "control_plane.production_recipient_cutover._verify_health_evidence_with_identity",
            return_value=HealthcheckEvidence(status="fail"),
        ):
            self.assertEqual(self.run_release().response_status_code, 409)
        self.assertFalse(self.old_running)
        self.assertFalse(self.new_running)
        result = self.run_release()
        self.assertEqual(result.response_status_code, 202, result.response_payload)
        self.assertEqual(result.response_payload["result"]["promotion_status"], "fail")
        self.assertTrue(self.old_running)
        self.assertFalse(self.new_running)
        self.assertEqual(self.deploys, 1)
