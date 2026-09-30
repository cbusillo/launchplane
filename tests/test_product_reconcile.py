from datetime import datetime, timedelta, timezone, tzinfo
import io
import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from control_plane.build_provenance import BUILD_WORKFLOW_PATH
from control_plane.contracts.dokploy_target_record import (
    DokployTargetPolicies,
    DokployTargetRecord,
    DokployTargetStaffTestingHold,
)
from control_plane.contracts.odoo_preview_runtime_plan import OdooPreviewRuntimePlan
from control_plane.contracts.odoo_stable_target_replacement import (
    LAUNCHPLANE_REQUIRED_ODOO_MODULES,
    OdooStableTargetReplacementApplyRequest,
    OdooStableTargetReplacementApplyResult,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewPullRequestSummary,
)
from control_plane.contracts.preview_record import PreviewRecord, PreviewState
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_reconcile import (
    ProductReconcileLeaseLostError,
    ProductReconcileRequestRecord,
    ProductReconcileTarget,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
)
from control_plane.launchplane_reconcile_authorization import (
    build_launchplane_reconcile_authorization,
)
from control_plane.odoo_preview_apply_http import ODOO_PREVIEW_APPLY_ROUTE
from control_plane.contracts.merge_train_policy import MergeTrainPolicy, MergeTrainPolicyRecord
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.product_reconcile import (
    PreviewProviderHooks,
    ProductReconcileError,
    request_product_reconcile_sweep,
    resolve_build_provenance_transport,
    run_product_reconcile_once,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_lane_hold import (
    STAFF_TESTING_HOLD_CANCELLATION_REASON,
    TestingHoldApplyRequest,
    apply_testing_hold_plan,
    build_testing_hold_plan,
)
from control_plane.workflows.odoo_preview_runtime import (
    OdooPreviewApplyInputsRequest,
    OdooPreviewApplyInputsResult,
    OdooPreviewDokployDryRunPlan,
)
from control_plane.workflows.odoo_stable_operation_worker import (
    OdooStableOperationWorkerResult,
    OdooStableOperationWorkerStore,
    run_odoo_stable_operation_worker_loop,
    run_odoo_stable_operation_worker_once,
)
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.support.durable_operations import durable_operation_authorization_payload
from tests.support.artifact_manifests import artifact_manifest_v2
from tests.support.profiles import _odoo_preview_profile_payload
from tests.support.stores import sqlite_database_url

REPOSITORY = "example/site"
REPOSITORY_ID = "101"
IMAGE_REPOSITORY = "ghcr.io/example/site"
NEWEST = "3" * 40
DEPLOYABLE = "2" * 40
OLDER = "1" * 40
OFF_HISTORY = "9" * 40
PR_HEAD = "a" * 40
LABEL = "launchplane-preview"


def _digest(commit: str) -> str:
    return f"sha256:{commit[0] * 64}"


class FakeGitHub:
    """GitHub's view of example/site: runs by id, a first-parent chain, and one PR."""

    def __init__(self) -> None:
        self.runs: dict[int, dict[str, object]] = {}
        self.first_parents = {NEWEST: DEPLOYABLE, DEPLOYABLE: OLDER, OLDER: ""}
        self.pull_request: dict[str, object] = {
            "state": "open",
            "labels": [{"name": LABEL}],
            "head": {"sha": PR_HEAD},
        }
        self.pull_request_reads = 0
        # (read count, change): the PR changes right after that many reads of it.
        self.pull_request_move: tuple[int, dict[str, object]] | None = None

    def add_run(self, run_id: int, commit: str, *, event: str = "push") -> None:
        self.runs[run_id] = {
            "id": run_id,
            "run_attempt": 1,
            "event": event,
            "head_branch": "main" if event == "push" else "feature",
            "head_sha": commit,
            "path": BUILD_WORKFLOW_PATH,
            "status": "completed",
            "conclusion": "success",
            "repository": {"id": int(REPOSITORY_ID)},
            "head_repository": {"id": int(REPOSITORY_ID)},
        }

    def get_json(self, path: str) -> object:
        if path == f"/repos/{REPOSITORY}":
            return {"id": int(REPOSITORY_ID), "default_branch": "main"}
        if path.startswith(f"/repos/{REPOSITORY}/actions/workflows/build.yml/runs?"):
            runs = [run for run in self.runs.values() if run["event"] == "push"]
            return {"workflow_runs": sorted(runs, key=lambda run: -cast(int, run["id"]))}
        if path.startswith(f"/repos/{REPOSITORY}/actions/runs?"):
            return {
                "workflow_runs": [
                    run
                    for run in self.runs.values()
                    if f"head_sha={run['head_sha']}" in path and f"event={run['event']}" in path
                ]
            }
        if path.startswith(f"/repos/{REPOSITORY}/commits?"):
            if "page=1" not in path:
                return []
            return [
                {"sha": sha, "parents": [{"sha": parent}] if parent else []}
                for sha, parent in self.first_parents.items()
            ]
        if path.startswith(f"/repos/{REPOSITORY}/pulls/"):
            current = dict(self.pull_request)
            self.pull_request_reads += 1
            if self.pull_request_move and self.pull_request_move[0] == self.pull_request_reads:
                self.pull_request.update(self.pull_request_move[1])
            return current
        if "/artifacts?" in path:
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            return {
                "artifacts": [{"id": run_id * 10, "name": "artifact-manifest-1", "expired": False}]
            }
        raise AssertionError(f"unexpected GitHub read {path}")

    def get_bytes(self, path: str) -> bytes:
        run_id = int(path.split("/actions/artifacts/")[1].split("/")[0]) // 10
        commit = cast(str, self.runs[run_id]["head_sha"])
        manifest = artifact_manifest_v2(
            image_repository=IMAGE_REPOSITORY,
            tenant_source_repository=REPOSITORY,
            odoo_install_modules=LAUNCHPLANE_REQUIRED_ODOO_MODULES,
        ).model_dump(mode="json")
        manifest["source_commit"] = commit
        for lock in manifest["dependency_provenance"]["uv_locks"]:
            if lock["scope"] == "tenant":
                lock["source_ref"] = commit
        manifest["image"] = {"repository": IMAGE_REPOSITORY, "digest": _digest(commit)}
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zip_file:
            zip_file.writestr("artifact-manifest.json", json.dumps(manifest))
        return archive.getvalue()


def _profile(product: str = "site", *, repository_id: str = "") -> dict[str, object]:
    """A profile that, by default, stores no repository ids: the inventory is the authority."""
    payload = _odoo_preview_profile_payload(product)
    payload.update(
        repository=REPOSITORY if product == "site" else f"example/{product}",
        image={"repository": IMAGE_REPOSITORY},
        repository_id=repository_id,
        repository_owner_id="1" if repository_id else "",
        preview={**cast(dict[str, object], payload["preview"]), "enable_label": LABEL},
    )
    return payload


def _inventory(
    repository: str = REPOSITORY, repository_id: str = REPOSITORY_ID
) -> RepositoryInventoryRecord:
    return RepositoryInventoryRecord(
        repository_id=repository_id,
        repository_owner_id="1",
        repository=repository,
        inventory_state="tracked",
        inventory_revision=1,
        recorded_at="2026-09-29T09:00:00Z",
        source="test",
        reason="Track the product repository.",
    )


class FakePreviewProvider:
    """Plans and 'runs' preview provider changes; Launchplane's lifecycle records are real."""

    def __init__(self) -> None:
        self.applied: list[tuple[str, int]] = []

    def hooks(self) -> PreviewProviderHooks:
        return PreviewProviderHooks(
            build_inputs=self.build_inputs,
            execute_apply=self.execute_apply,
            observe_apply=self.observe_apply,
        )

    def build_inputs(
        self,
        *,
        profile: LaunchplaneProductProfileRecord,
        request: OdooPreviewApplyInputsRequest,
        **_kwargs: object,
    ) -> dict[str, object]:
        slug = f"pr-{request.pr_number}"
        url = f"https://{slug}.example.test"
        return OdooPreviewApplyInputsResult(
            status="ready",
            product=profile.product,
            context=profile.preview.context,
            template_instance=profile.preview.template_instance,
            operation=request.operation,
            preview_slug=slug,
            preview_url=url,
            repository=profile.repository,
            plan_request=request,
            runtime_plan=OdooPreviewRuntimePlan(
                status="ready",
                operation=request.operation,
                product=profile.product,
                repository=profile.repository,
                pr_number=request.pr_number,
                preview_slug=slug,
                preview_url=url,
                strategy="isolated_dokploy_compose",
                summary="ready",
            ),
            dry_run_plan=OdooPreviewDokployDryRunPlan(
                status="ready",
                operation=request.operation,
                product=profile.product,
                repository=profile.repository,
                preview_slug=slug,
                preview_url=url,
                compose_ref=f"cm-odoo-preview-{slug}",
                compose_name=f"cm-odoo-preview-{slug}",
                summary="ready",
            ),
            source=request.source,
        ).model_dump(mode="json")

    def execute_apply(
        self, *, issued_plan: OdooPreviewApplyInputsResult, **_kwargs: object
    ) -> dict[str, object]:
        self.applied.append((issued_plan.operation, issued_plan.plan_request.pr_number))
        dry_run_plan = issued_plan.dry_run_plan
        return {
            "status": "pass",
            "operation": issued_plan.operation,
            "product": issued_plan.product,
            "repository": issued_plan.repository,
            "preview_slug": dry_run_plan.preview_slug,
            "preview_url": dry_run_plan.preview_url,
            "domain_host": dry_run_plan.preview_url.removeprefix("https://"),
            "compose_name": dry_run_plan.compose_name,
        }

    def observe_apply(self, **_kwargs: object) -> tuple[str, None, bool]:
        raise AssertionError("a fresh preview operation is never observed")


class ProductReconcileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(temporary_directory.name) / "lp.sqlite3")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.store.write_repository_inventory_record(_inventory())
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_profile())
        )
        self.github = FakeGitHub()
        self.provider = FakePreviewProvider()
        self.root = Path(temporary_directory.name)

    def request(self, target_kind: str = "testing", number: int | None = None) -> str:
        target = ProductReconcileTarget.model_validate(
            {"product": "site", "target_kind": target_kind, "pull_request_number": number}
        )
        return self.store.request_product_reconcile(target, "2026-09-29T12:00:00Z").target_key

    def run_once(self) -> ProductReconcileRequestRecord:
        completed = run_product_reconcile_once(
            record_store=self.store,
            lease_owner="worker-a",
            transport_factory=lambda _store, _profile: self.github,
            control_plane_root=self.root,
            preview_hooks=self.provider.hooks(),
        )
        assert completed is not None
        return completed

    def reconcile(self) -> dict[str, object]:
        completed = self.run_once()
        self.assertEqual(completed.state, "done", completed.last_error)
        return dict(completed.last_plan)

    def write_preview(self, *, number: int = 5, state: PreviewState = "active") -> None:
        preview_id = f"preview-cm-site-pr-{number}"
        generation_id = f"{preview_id}-generation-0001"
        self.store.write_preview_record(
            PreviewRecord(
                preview_id=preview_id,
                context="cm",
                anchor_repo="site",
                anchor_pr_number=number,
                anchor_pr_url=f"https://github.com/{REPOSITORY}/pull/{number}",
                preview_label=LABEL,
                canonical_url=f"https://pr-{number}.example.test",
                state=state,
                created_at="2026-09-29T10:00:00Z",
                updated_at="2026-09-29T10:00:00Z",
                eligible_at="2026-09-29T10:00:00Z",
                serving_generation_id=generation_id,
            )
        )
        self.store.write_preview_generation_record(
            PreviewGenerationRecord(
                generation_id=generation_id,
                preview_id=preview_id,
                sequence=1,
                state="ready",
                requested_reason="test",
                requested_at="2026-09-29T10:00:00Z",
                resolved_manifest_fingerprint="fingerprint",
                anchor_summary=PreviewPullRequestSummary(
                    repo="site",
                    pr_number=number,
                    head_sha=PR_HEAD,
                    pr_url=f"https://github.com/{REPOSITORY}/pull/{number}",
                ),
                runtime_identity=RuntimeIdentity(
                    context="cm",
                    instance=f"pr-{number}",
                    deployment_record_id="deployment-preview",
                    artifact_id="preview-artifact",
                    source_git_ref=PR_HEAD,
                    image_reference=f"{IMAGE_REPOSITORY}@{_digest(PR_HEAD)}",
                ),
            )
        )

    def hold_testing(self) -> None:
        self.store.write_dokploy_target_record(
            DokployTargetRecord(
                context="cm",
                instance="testing",
                policies=DokployTargetPolicies(
                    staff_testing_hold=DokployTargetStaffTestingHold(
                        reason="Staff are testing checkout.",
                        recorded_by="site-operator",
                        recorded_at="2026-09-30T09:00:00Z",
                    )
                ),
                updated_at="2026-09-30T09:00:00Z",
            )
        )

    def lift_testing_hold(self) -> None:
        """Lift the hold the way the operator route does; it requests the testing reconcile."""
        payload: dict[str, object] = {
            "product": "site",
            "context": "cm",
            "instance": "testing",
            "hold": False,
            "reason": "Staff testing finished.",
        }
        plan, _ = build_testing_hold_plan(
            record_store=self.store,
            request=TestingHoldApplyRequest.model_validate(payload),
            actor="site-operator",
        )
        lifted = apply_testing_hold_plan(
            record_store=self.store,
            request=TestingHoldApplyRequest.model_validate(
                {**payload, "mode": "apply", "reviewed_plan_sha256": plan.plan_sha256}
            ),
            actor="site-operator",
        )
        self.assertTrue(lifted.reconcile_requested)

    def snapshot(self) -> tuple[object, ...]:
        return (
            self.store.list_artifact_manifests(),
            self.store.list_release_tuple_records(),
            self.store.list_preview_records(),
            self.store.list_preview_generation_records(),
            self.store.list_odoo_stable_target_replacement_operation_records(),
        )


class ProductReconcileStoreTests(ProductReconcileTestCase):
    def test_claims_oldest_pending_and_completes_with_plan(self) -> None:
        self.request()
        self.store.request_product_reconcile(
            ProductReconcileTarget(product="site", target_kind="preview", pull_request_number=5),
            "2026-09-29T12:01:00Z",
        )

        claimed = self.store.claim_next_product_reconcile_request("worker-a", 60)
        assert claimed is not None
        self.assertEqual(
            (claimed.target_key, claimed.state, claimed.attempt, claimed.lease_owner),
            ("site:testing", "running", 1, "worker-a"),
        )
        with self.assertRaises(ProductReconcileLeaseLostError):
            self.store.complete_product_reconcile_request("site:testing", "worker-b", "done", {})

        failed = self.store.complete_product_reconcile_request(
            "site:testing", "worker-a", "failed", {"target": "testing"}, "boom"
        )
        self.assertEqual((failed.state, failed.last_error), ("failed", "boom"))
        self.assertEqual(failed.last_plan, {"target": "testing"})
        self.assertEqual(failed.lease_owner, "")
        next_claim = self.store.claim_next_product_reconcile_request("worker-a", 60)
        assert next_claim is not None
        self.assertEqual(next_claim.target_key, "site:preview:5")
        self.assertIsNone(self.store.claim_next_product_reconcile_request("worker-a", 60))

    def test_expired_lease_is_reclaimed_and_old_owner_cannot_complete(self) -> None:
        self.request()
        self.store.claim_next_product_reconcile_request("worker-a", 60, now="2026-09-29T12:00:00Z")

        self.assertIsNone(
            self.store.claim_next_product_reconcile_request(
                "worker-b", 60, now="2026-09-29T12:00:30Z"
            )
        )
        reclaimed = self.store.claim_next_product_reconcile_request(
            "worker-b", 60, now="2026-09-29T12:02:00Z"
        )
        assert reclaimed is not None
        self.assertEqual((reclaimed.lease_owner, reclaimed.attempt), ("worker-b", 2))
        with self.assertRaises(ProductReconcileLeaseLostError):
            self.store.complete_product_reconcile_request("site:testing", "worker-a", "done", {})

    def test_request_during_run_returns_it_to_pending_with_the_plan(self) -> None:
        self.request()
        self.store.claim_next_product_reconcile_request("worker-a", 60)
        self.request()

        completed = self.store.complete_product_reconcile_request(
            "site:testing", "worker-a", "done", {"action": "none"}
        )

        self.assertEqual(completed.state, "pending")
        self.assertEqual(completed.last_plan, {"action": "none"})
        reclaimed = self.store.claim_next_product_reconcile_request("worker-a", 60)
        assert reclaimed is not None
        self.assertFalse(reclaimed.rerequested_while_running)
        done = self.store.complete_product_reconcile_request(
            "site:testing", "worker-a", "done", {"action": "none"}
        )
        self.assertEqual(done.state, "done")


class ProductReconcileTestingTests(ProductReconcileTestCase):
    def test_testing_deploy_queues_one_target_replacement_on_the_reconcile_grant(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.github.add_run(30, OLDER)
        self.github.add_run(40, OFF_HISTORY)
        self.request()

        plan = self.reconcile()

        # The newest first-parent build wins over a later build of an older commit.
        self.assertEqual(plan["action"], "deploy")
        self.assertEqual(plan["desired_commit"], DEPLOYABLE)
        self.assertEqual(plan["desired_artifact_id"], "artifact-cm-run-20-1")
        self.assertFalse(plan["held"])
        (operation,) = self.store.list_odoo_stable_target_replacement_operation_records()
        self.assertEqual(plan["queued_operation_id"], operation.operation_id)
        self.assertEqual(
            (operation.product, operation.context, operation.instance, operation.status),
            ("site", "cm", "testing", "pending"),
        )
        request = operation.request
        self.assertEqual(
            (
                request.strategy,
                request.allow_empty_data,
                request.data_source_mode,
                request.artifact_id,
                request.source_git_ref,
            ),
            ("recreate-in-place", True, "existing", "artifact-cm-run-20-1", DEPLOYABLE),
        )
        authorization = operation.authorization
        assert authorization is not None
        self.assertEqual(authorization.grant, "launchplane_reconcile")
        self.assertEqual(authorization.caller.identity_type, "launchplane_reconcile")
        self.assertEqual(operation.idempotency_scope, "launchplane-reconcile:site")
        recorded = self.store.read_artifact_manifest("artifact-cm-run-20-1")
        self.assertEqual(recorded.image.digest, _digest(DEPLOYABLE))

        self.request()
        repeated = self.reconcile()

        self.assertEqual(repeated["queued_operation_id"], operation.operation_id)
        self.assertEqual(len(self.store.list_odoo_stable_target_replacement_operation_records()), 1)

    def test_busy_testing_lane_leaves_the_reconcile_pending(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        busy = OdooStableTargetReplacementOperationRecord.model_validate(
            {
                "schema_version": 2,
                "operation_id": "operation-site-testing-busy",
                "product": "site",
                "context": "cm",
                "instance": "testing",
                "idempotency_key": "someone-else",
                "idempotency_scope": "operator",
                "request_fingerprint": "fingerprint",
                "request": {"product": "site", "instance": "testing"},
                "authorization": durable_operation_authorization_payload(
                    action="odoo_target_replacement_apply.execute",
                    managed_rule_id="site-testing",
                    product="site",
                ),
                "status": "running",
                "phase": "created",
                "created_at": "2026-09-29T11:00:00Z",
                "updated_at": "2026-09-29T11:00:00Z",
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(busy)
        self.request()

        completed = self.run_once()

        self.assertEqual(completed.state, "pending")
        self.assertEqual(completed.last_plan["deferred"], "lane_busy")
        self.assertEqual(completed.last_plan["active_operation_id"], busy.operation_id)
        self.assertEqual(
            self.store.list_odoo_stable_target_replacement_operation_records(), (busy,)
        )

    def finish(self, operation_id: str, status: str) -> None:
        operation = self.store.read_odoo_stable_target_replacement_operation_record(operation_id)
        changes: dict[str, object] = {"status": status, "phase": "failed"}
        if status == "pass":
            changes.update(phase="completed", finished_at="2026-09-30T12:00:00Z")
        elif status == "fail":
            changes.update(finished_at="2026-09-30T12:00:00Z", error_message="deploy failed")
        else:
            changes.update(error_code="provider_outcome_unknown", error_message="unknown")
        self.store.write_odoo_stable_target_replacement_operation_record(
            OdooStableTargetReplacementOperationRecord.model_validate(
                {**operation.model_dump(mode="json"), **changes}
            )
        )

    def test_failed_testing_deploy_is_retried_once_per_reconcile(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        first = cast(str, self.reconcile()["queued_operation_id"])
        self.finish(first, "fail")

        self.request()
        retried = self.reconcile()
        self.request()
        repeated = self.reconcile()

        second = cast(str, retried["queued_operation_id"])
        self.assertNotEqual(second, first)
        self.assertEqual(retried["last_failed_operation_id"], first)
        self.assertEqual(repeated["queued_operation_id"], second)
        operations = self.store.list_odoo_stable_target_replacement_operation_records()
        self.assertEqual(len(operations), 2)
        (retry,) = (operation for operation in operations if operation.operation_id == second)
        self.assertTrue(retry.idempotency_key.endswith(f":after-{first}"))

    def test_testing_is_redeployed_after_a_rollback(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        first = cast(str, self.reconcile()["queued_operation_id"])
        # It passed, but testing no longer runs it (the release still names another build).
        self.finish(first, "pass")

        self.request()
        redeployed = self.reconcile()

        self.assertNotEqual(redeployed["queued_operation_id"], first)
        self.assertNotIn("last_failed_operation_id", redeployed)

    def test_a_deploy_that_finishes_after_the_plan_read_is_not_queued_again(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        first = cast(str, self.reconcile()["queued_operation_id"])
        self.request()
        read_release = self.store.read_release_tuple_record
        reads = 0

        def publish_after_the_plan_read(**kwargs: str) -> ReleaseTupleRecord:
            # The plan reads no release; the worker then publishes it and completes.
            nonlocal reads
            reads += 1
            try:
                return read_release(**kwargs)
            finally:
                if reads == 1:
                    self.store.write_release_tuple_record(
                        ReleaseTupleRecord(
                            tuple_id="cm-testing-deployed",
                            context="cm",
                            channel="testing",
                            artifact_id="artifact-cm-run-20-1",
                            repo_shas={"site": DEPLOYABLE},
                            image_repository=IMAGE_REPOSITORY,
                            image_digest=_digest(DEPLOYABLE),
                            provenance="ship",
                            minted_at="2026-09-30T12:00:00Z",
                        )
                    )
                    self.finish(first, "pass")

        with patch.object(self.store, "read_release_tuple_record", publish_after_the_plan_read):
            plan = self.reconcile()

        self.assertEqual(reads, 2)
        self.assertEqual((plan["action"], plan["reason"]), ("none", "already_deployed"))
        self.assertEqual(plan["deployed_operation_id"], first)
        self.assertEqual(len(self.store.list_odoo_stable_target_replacement_operation_records()), 1)

    def test_uncertain_testing_deploy_is_not_bypassed(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        first = cast(str, self.reconcile()["queued_operation_id"])
        self.finish(first, "reconciliation_required")

        self.request()
        plan = self.reconcile()

        self.assertEqual(
            (plan["queued_operation_id"], plan["queued_operation_status"]),
            (first, "reconciliation_required"),
        )
        self.assertEqual(len(self.store.list_odoo_stable_target_replacement_operation_records()), 1)

    def test_testing_deploy_stops_after_three_failed_attempts(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        for _attempt in range(3):
            self.request()
            self.finish(cast(str, self.reconcile()["queued_operation_id"]), "fail")
        self.request()

        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            completed = self.run_once()

        self.assertEqual(completed.state, "failed")
        self.assertIn("failed 3 times", completed.last_error)
        self.assertEqual(len(self.store.list_odoo_stable_target_replacement_operation_records()), 3)

    def test_testing_is_left_alone_when_the_release_already_has_that_digest(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.store.write_release_tuple_record(
            ReleaseTupleRecord(
                tuple_id="cm-testing-legacy",
                context="cm",
                channel="testing",
                artifact_id="artifact-legacy-id",
                repo_shas={"site": DEPLOYABLE},
                image_repository=IMAGE_REPOSITORY,
                image_digest=_digest(DEPLOYABLE),
                provenance="ship",
                minted_at="2026-09-29T09:00:00Z",
            )
        )
        self.request()

        plan = self.reconcile()

        self.assertEqual((plan["action"], plan["held"]), ("none", False))
        self.assertEqual(plan["current_artifact_id"], "artifact-legacy-id")
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), ())


class ProductReconcileStaffTestingHoldTests(ProductReconcileTestCase):
    def test_held_testing_lane_waits_and_lifting_the_hold_deploys(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.hold_testing()
        self.request()

        held = self.reconcile()

        self.assertEqual(
            (held["action"], held["held"], held["reason"]), ("wait", True, "staff_testing")
        )
        self.assertEqual(held["desired_artifact_id"], "artifact-cm-run-20-1")
        self.assertEqual(
            (held["hold_reason"], held["hold_recorded_by"]),
            ("Staff are testing checkout.", "site-operator"),
        )
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), ())
        self.assertEqual(self.store.list_artifact_manifests(), ())

        self.lift_testing_hold()
        deployed = self.reconcile()

        self.assertEqual((deployed["action"], deployed["held"]), ("deploy", False))
        (operation,) = self.store.list_odoo_stable_target_replacement_operation_records()
        self.assertEqual(deployed["queued_operation_id"], operation.operation_id)
        self.assertEqual(operation.request.artifact_id, "artifact-cm-run-20-1")

    def test_hold_leaves_a_testing_lane_that_already_runs_the_build_alone(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.store.write_release_tuple_record(
            ReleaseTupleRecord(
                tuple_id="cm-testing-current",
                context="cm",
                channel="testing",
                artifact_id="artifact-cm-run-20-1",
                repo_shas={"site": DEPLOYABLE},
                image_repository=IMAGE_REPOSITORY,
                image_digest=_digest(DEPLOYABLE),
                provenance="ship",
                minted_at="2026-09-29T09:00:00Z",
            )
        )
        self.hold_testing()
        self.request()

        plan = self.reconcile()

        self.assertEqual(
            (plan["action"], plan["reason"], plan["held"]), ("none", "already_deployed", False)
        )

    def test_deploy_cancelled_by_the_hold_is_not_a_failed_attempt(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        first = cast(str, self.reconcile()["queued_operation_id"])
        # Staff start testing after the deploy was queued but before the worker ran it.
        self.hold_testing()
        with patch(
            "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_target_replacement_apply"
        ) as execute:
            run_odoo_stable_operation_worker_once(
                record_store=self.store, control_plane_root_path=self.root, lease_owner="worker-a"
            )
        execute.assert_not_called()
        cancelled = self.store.read_odoo_stable_target_replacement_operation_record(first)
        self.assertEqual(cancelled.status, "cancelled")

        self.lift_testing_hold()
        redeployed = self.reconcile()

        second = cast(str, redeployed["queued_operation_id"])
        self.assertNotEqual(second, first)
        self.assertNotIn("last_failed_operation_id", redeployed)
        retry = self.store.read_odoo_stable_target_replacement_operation_record(second)
        self.assertTrue(retry.idempotency_key.endswith(f":after-{first}"))


class _MinuteClock(datetime):
    """Each plan is issued a minute after the last; lifecycle order is by issue time."""

    ticks = 0

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> "_MinuteClock":
        cls.ticks += 1
        return cls(2026, 9, 30, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=cls.ticks)


class ProductReconcilePreviewTests(ProductReconcileTestCase):
    def setUp(self) -> None:
        super().setUp()
        clock = patch("control_plane.odoo_preview_apply_http.datetime", _MinuteClock)
        clock.start()
        self.addCleanup(clock.stop)

    def test_preview_is_applied_kept_destroyed_and_applied_again(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)

        applied = self.reconcile()

        self.assertEqual(
            (applied["action"], applied["held"], applied["preview_result_status"]),
            ("apply", False, "pass"),
        )
        self.assertEqual(self.provider.applied, [("refresh", 5)])
        reservation = self.store.read_idempotency_record(
            scope="launchplane-reconcile:site",
            route_path=ODOO_PREVIEW_APPLY_ROUTE,
            idempotency_key=cast(str, applied["preview_plan_id"]),
        )
        assert reservation is not None
        self.assertEqual(reservation.state, "completed")
        (preview,) = self.store.list_preview_records()
        self.assertEqual((preview.anchor_pr_number, preview.state), (5, "active"))

        self.request("preview", 5)
        self.assertEqual(self.reconcile()["reason"], "already_serving")

        self.github.pull_request["labels"] = []
        self.request("preview", 5)
        destroyed = self.reconcile()

        self.assertEqual(
            (destroyed["action"], destroyed["preview_result_status"]), ("destroy", "pass")
        )
        self.assertEqual(self.store.list_preview_records()[0].state, "destroyed")

        # The same build asked for again after a destroy is a new operation, not a replay.
        self.github.pull_request["labels"] = [{"name": LABEL}]
        self.request("preview", 5)
        self.assertEqual(self.reconcile()["action"], "apply")
        self.assertEqual(self.provider.applied, [("refresh", 5), ("destroy", 5), ("refresh", 5)])
        self.assertEqual(self.store.list_preview_records()[0].state, "active")

    def test_preview_is_not_changed_while_it_waits_or_has_nothing_to_do(self) -> None:
        cases: tuple[tuple[str, dict[str, object], bool, bool, str], ...] = (
            ("none when serving the build", {}, True, True, "none"),
            ("wait for the build", {}, False, False, "wait"),
            ("none when closed and absent", {"state": "closed"}, False, False, "none"),
        )
        for index, (name, pull_request, built, live, action) in enumerate(cases, start=1):
            with self.subTest(name):
                self.github.runs.clear()
                self.github.pull_request.update(
                    {"state": "open", "labels": [{"name": LABEL}], **pull_request}
                )
                if built:
                    self.github.add_run(50, PR_HEAD, event="pull_request")
                if live:
                    self.write_preview(number=index)
                self.request("preview", index)
                before = self.snapshot()

                plan = self.reconcile()

                self.assertEqual((plan["action"], plan["held"]), (action, False))
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.provider.applied, [])

    def test_a_pr_that_moves_before_the_provider_change_is_reconciled_again(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        # Reads: the plan, the build verification, then the check just before applying.
        self.github.pull_request_move = (2, {"head": {"sha": "b" * 40}})
        self.request("preview", 5)

        completed = self.run_once()

        self.assertEqual(completed.state, "pending")
        self.assertEqual(completed.last_plan["deferred"], "pull_request_moved")
        self.assertEqual(self.provider.applied, [])
        self.assertEqual(self.store.list_preview_records(), ())


class ProductReconcilePreviewRaceTests(ProductReconcileTestCase):
    def test_a_pr_closed_while_the_plan_is_prepared_never_reaches_the_provider(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        build_inputs = self.provider.build_inputs

        def close_while_preparing(**kwargs: object) -> dict[str, object]:
            self.github.pull_request["state"] = "closed"
            return build_inputs(**kwargs)  # type: ignore[arg-type]

        self.provider.build_inputs = close_while_preparing  # type: ignore[method-assign]
        self.request("preview", 5)

        completed = self.run_once()

        self.assertEqual(completed.state, "pending")
        self.assertEqual(completed.last_plan["deferred"], "pull_request_moved")
        self.assertEqual(self.provider.applied, [])
        reservation = self.store.read_idempotency_record(
            scope="launchplane-reconcile:site",
            route_path=ODOO_PREVIEW_APPLY_ROUTE,
            idempotency_key=cast(str, completed.last_plan["preview_plan_id"]),
        )
        self.assertTrue(reservation is None or reservation.state != "completed")
        self.assertEqual(self.store.list_preview_records(), ())

        # Next time round the closed PR has no preview to make.
        self.assertEqual(self.reconcile()["action"], "none")


class ProductReconcileGrantTests(ProductReconcileTestCase):
    """The worker runs a reconcile-granted operation only on the product's own testing lane."""

    def setUp(self) -> None:
        super().setUp()
        profile = _profile()
        profile["lanes"] = (
            *cast(tuple[dict[str, object], ...], profile["lanes"]),
            {"instance": "prod", "context": "cm", "base_url": "https://cm.example.com"},
        )
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile)
        )

    def replacement(
        self, *, product: str = "site", context: str = "cm", instance: str = "testing"
    ) -> OdooStableTargetReplacementOperationRecord:
        return OdooStableTargetReplacementOperationRecord(
            schema_version=2,
            operation_id=f"operation-{product}-{context}-{instance}",
            product=product,
            context=context,
            instance=instance,
            idempotency_key=f"key-{product}-{context}-{instance}",
            idempotency_scope="launchplane-reconcile:site",
            request_fingerprint="fingerprint",
            request=OdooStableTargetReplacementApplyRequest(
                product=product, instance=instance, allow_empty_data=True
            ),
            # A forged or stale record: the builder itself only ever names testing.
            authorization=build_launchplane_reconcile_authorization(
                product=product, context=context, authorized_at="2026-09-30T00:00:00Z"
            ).model_copy(update={"instances": (instance,)}),
            status="pending",
            phase="created",
            created_at="2026-09-30T00:00:00Z",
            updated_at="2026-09-30T00:00:00Z",
        )

    def run_replacement(
        self, operation: OdooStableTargetReplacementOperationRecord
    ) -> OdooStableTargetReplacementOperationRecord:
        self.store.write_odoo_stable_target_replacement_operation_record(operation)
        result = OdooStableTargetReplacementApplyResult(
            product=operation.product,
            context=operation.context,
            instance=operation.instance,
            strategy="recreate-in-place",
            deployment_record_id="deployment-site-testing",
            deploy_status="pass",
            post_deploy_status="pass",
            health_status="pass",
            canonical_status="pass",
            logo_status="pass",
            runtime_identity_injected=True,
        )
        with patch(
            "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_target_replacement_apply",
            return_value=result,
        ) as execute:
            run_odoo_stable_operation_worker_once(
                record_store=self.store,
                control_plane_root_path=self.root,
                lease_owner="worker-a",
            )
        finished = self.store.read_odoo_stable_target_replacement_operation_record(
            operation.operation_id
        )
        self.assertEqual(execute.called, finished.status == "pass")
        return finished

    def test_reconcile_grant_runs_the_products_own_testing_replacement(self) -> None:
        finished = self.run_replacement(self.replacement())

        self.assertEqual((finished.status, finished.error_code), ("pass", ""))

    def test_reconcile_grant_replacement_is_cancelled_while_testing_is_held(self) -> None:
        self.hold_testing()

        finished = self.run_replacement(self.replacement())

        self.assertEqual((finished.status, finished.phase), ("cancelled", "cancelled"))
        assert finished.cancellation is not None
        self.assertEqual(finished.cancellation.reason, STAFF_TESTING_HOLD_CANCELLATION_REASON)
        self.assertEqual(finished.cancellation.caller.identity_type, "launchplane_reconcile")
        self.assertIsNone(finished.result)

    def test_reconcile_grant_is_refused_for_other_destinations(self) -> None:
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_profile("other"))
        )
        self.store.write_repository_inventory_record(_inventory("example/stale", "202"))
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_profile("stale", repository_id="999"))
        )
        cases = {
            "prod instance": self.replacement(instance="prod"),
            "another context": self.replacement(context="elsewhere"),
            "product not in the repository inventory": self.replacement(product="other"),
            "product whose stored ids disagree with the inventory": self.replacement(
                product="stale"
            ),
            "unknown product": self.replacement(product="missing"),
        }
        for name, operation in cases.items():
            with self.subTest(name), self.assertLogs(level="WARNING"):
                finished = self.run_replacement(operation)

                self.assertEqual(
                    (finished.status, finished.error_code),
                    ("fail", "operation_authorization_reconcile_refused"),
                )

    def test_reconcile_grant_is_refused_for_other_operation_kinds(self) -> None:
        forged = DurableOperationAuthorization(
            grant="launchplane_reconcile",
            action="odoo_stable_bootstrap.execute",
            product="site",
            context="cm",
            instances=("testing",),
            authorized_at="2026-09-30T00:00:00Z",
            caller=build_launchplane_reconcile_authorization(
                product="site", context="cm", authorized_at="2026-09-30T00:00:00Z"
            ).caller,
        )
        self.store.write_odoo_stable_bootstrap_operation_record(
            OdooStableBootstrapOperationRecord.model_validate(
                {
                    "schema_version": 2,
                    "operation_id": "operation-site-bootstrap",
                    "product": "site",
                    "context": "cm",
                    "instance": "testing",
                    "idempotency_key": "bootstrap",
                    "request_fingerprint": "fingerprint",
                    "request": {
                        "product": "site",
                        "context": "cm",
                        "instance": "testing",
                        "confirmation": "bootstrap cm testing",
                    },
                    "authorization": forged.model_dump(mode="json"),
                    "status": "pending",
                    "phase": "created",
                    "created_at": "2026-09-30T00:00:00Z",
                    "updated_at": "2026-09-30T00:00:00Z",
                }
            )
        )

        with (
            patch(
                "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_bootstrap"
            ) as execute,
            self.assertLogs(level="WARNING"),
        ):
            run_odoo_stable_operation_worker_once(
                record_store=self.store, control_plane_root_path=self.root, lease_owner="worker-a"
            )

        execute.assert_not_called()
        finished = self.store.read_odoo_stable_bootstrap_operation_record(
            "operation-site-bootstrap"
        )
        self.assertEqual(finished.error_code, "operation_authorization_reconcile_refused")


class ProductReconcileFailureTests(ProductReconcileTestCase):
    def test_missing_merge_train_app_fails_without_a_token(self) -> None:
        self.request()
        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            failed = run_product_reconcile_once(record_store=self.store, lease_owner="worker-a")
        assert failed is not None
        self.assertEqual(failed.state, "failed")
        self.assertIn("active merge train policy record is missing", failed.last_error)

        self.store.write_merge_train_policy_record(
            build_test_merge_train_policy_record(repository=REPOSITORY)
        )
        self.request()
        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            failed = run_product_reconcile_once(record_store=self.store, lease_owner="worker-a")
        assert failed is not None
        self.assertEqual(failed.state, "failed")
        self.assertIn("has no GitHub App", failed.last_error)

    def write_merge_train_app(self, *, app_repository_id: int) -> None:
        record = build_test_merge_train_policy_record(repository=REPOSITORY)
        policy = record.policy.model_dump(mode="json")
        policy["policies"][0]["merge_identity"] = {"kind": "github_app", "name": "site-merge"}
        policy["policies"][0]["github_token"] = {
            "github_app": {
                "app_id": 77,
                "repository_id": app_repository_id,
                "private_key_context": "site-merge-train",
            }
        }
        self.store.write_merge_train_policy_record(
            MergeTrainPolicyRecord(
                record_id=record.record_id,
                source="test",
                updated_at=record.updated_at,
                policy=MergeTrainPolicy.model_validate(policy),
            )
        )

    def test_build_provenance_token_is_minted_for_the_inventory_repository_id(self) -> None:
        self.write_merge_train_app(app_repository_id=int(REPOSITORY_ID))
        profile = self.store.read_product_profile_record("site")
        self.assertEqual(profile.repository_id, "")
        token = GitHubAppInstallationToken(
            token="read-only",
            app_id=77,
            installation_id=5,
            repository_id=int(REPOSITORY_ID),
            repository=REPOSITORY,
            expires_at="2026-09-30T13:00:00Z",
        )

        with (
            patch(
                "control_plane.product_reconcile.secrets.resolve_context_secret_value",
                return_value="private-key",
            ),
            patch(
                "control_plane.product_reconcile.mint_build_provenance_installation_token",
                return_value=token,
            ) as mint,
        ):
            resolve_build_provenance_transport(self.store, profile)

        self.assertEqual(mint.call_args.kwargs["repository_id"], REPOSITORY_ID)
        self.assertEqual(mint.call_args.kwargs["repository"], REPOSITORY)

    def test_build_provenance_token_is_refused_for_another_repository_id(self) -> None:
        self.write_merge_train_app(app_repository_id=int(REPOSITORY_ID) + 1)

        with (
            patch(
                "control_plane.product_reconcile.mint_build_provenance_installation_token"
            ) as mint,
            self.assertRaisesRegex(ProductReconcileError, "repository inventory"),
        ):
            resolve_build_provenance_transport(
                self.store, self.store.read_product_profile_record("site")
            )

        mint.assert_not_called()

    def test_reconcile_fails_closed_without_a_repository_inventory_identity(self) -> None:
        cases = {
            "not in the inventory": _profile() | {"repository": "example/untracked"},
            "stored ids disagree": _profile(repository_id="999"),
        }
        for name, profile in cases.items():
            with self.subTest(name):
                self.store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(profile)
                )
                self.request()
                with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                    failed = self.run_once()

                self.assertEqual(failed.state, "failed")
                self.assertIn("repository inventory", failed.last_error)
                self.assertEqual(self.snapshot(), ((), (), (), (), ()))

    def test_reconcile_fails_closed_when_another_active_profile_names_the_repository(
        self,
    ) -> None:
        site = self.store.read_product_profile_record("site")
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(
                _profile("copy") | {"repository": site.repository}
            )
        )
        self.request()
        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            failed = self.run_once()

        self.assertEqual(failed.state, "failed")
        self.assertIn("also named by active product profile copy", failed.last_error)
        self.assertEqual(self.snapshot(), ((), (), (), (), ()))
        self.assertEqual(request_product_reconcile_sweep(self.store, "2026-09-29T12:00:00Z"), ())


class ProductReconcileSweepTests(ProductReconcileTestCase):
    def test_sweep_requests_mapped_testing_targets_and_live_previews(self) -> None:
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_profile("unmapped"))
        )
        self.write_preview(number=5)
        self.write_preview(number=6, state="destroyed")

        requested = request_product_reconcile_sweep(self.store, "2026-09-29T12:00:00Z")

        self.assertEqual(set(requested), {"site:testing", "site:preview:5"})
        self.assertEqual(
            {request.target_key for request in self.store.list_product_reconcile_requests()},
            {"site:testing", "site:preview:5"},
        )


class _ClaimOrderStore:
    """Answers every worker claim with nothing and remembers the order it was asked."""

    def __init__(self) -> None:
        self.claims: list[str] = []

    def __getattr__(self, name: str) -> object:
        if name.startswith("recover_expired_"):
            return lambda **_kwargs: ()
        if name.startswith("claim_next_"):

            def claim(*_args: object, **_kwargs: object) -> None:
                self.claims.append(name)

            return claim
        raise AttributeError(name)


class ProductReconcileWorkerTests(ProductReconcileTestCase):
    def test_worker_tries_reconcile_only_after_every_operation_kind(self) -> None:
        store = _ClaimOrderStore()

        result = run_odoo_stable_operation_worker_once(
            record_store=cast(OdooStableOperationWorkerStore, store),
            control_plane_root_path=Path("."),
            lease_owner="worker-a",
        )

        self.assertEqual(result.status, "idle")
        self.assertEqual(store.claims[-1], "claim_next_product_reconcile_request")
        self.assertEqual(len(store.claims), 5)

    def test_worker_reports_a_reconcile_it_ran(self) -> None:
        self.request()

        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            result = run_odoo_stable_operation_worker_once(
                record_store=self.store,
                control_plane_root_path=Path("."),
                lease_owner="worker-a",
            )

        self.assertEqual(
            (result.status, result.operation_kind, result.operation_id),
            ("worked", "product_reconcile", "site:testing"),
        )
        self.assertEqual(self.store.read_product_reconcile_request("site:testing").state, "failed")

    def test_worker_loop_sweeps_at_most_every_thirty_minutes(self) -> None:
        clock = [0.0]

        def advance(_result: OdooStableOperationWorkerResult) -> None:
            clock[0] += 900

        with (
            patch(
                "control_plane.workflows.odoo_stable_operation_worker.run_odoo_stable_operation_worker_once",
                return_value=OdooStableOperationWorkerResult(status="worked"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_operation_worker.request_product_reconcile_sweep"
            ) as sweep,
        ):
            run_odoo_stable_operation_worker_loop(
                record_store=self.store,
                control_plane_root_path=Path("."),
                lease_owner="worker-a",
                max_iterations=4,
                iteration_callback=advance,
                monotonic=lambda: clock[0],
            )

        self.assertEqual(sweep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
