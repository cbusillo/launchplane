from control_plane.contracts.odoo_target_replacement_failures import TESTING_RESTORE_REQUIRED_CODE
from control_plane.odoo_testing_restore import FreshOdooTestingGeneration
from control_plane.contracts.generic_web_extensions import GenericWebLaneExtensions
from control_plane.dokploy.api import DokployRequestFailed, DokployScheduleExecutionFailed
from control_plane.contracts.record_failures import record_failure_summary
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone, tzinfo
from dataclasses import replace
from control_plane.dokploy.application_runtime import (
    ApplicationRuntimeObservation,
    ApplicationTaskObservation,
)
from email.message import Message
import io
import json
import os
import unittest
from contextlib import closing, contextmanager, nullcontext
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, current_thread, main_thread
from typing import Any, Literal, cast
from unittest.mock import patch
from urllib.error import HTTPError

import click
from cryptography.fernet import Fernet

from control_plane.build_provenance import (
    BUILD_WORKFLOW_PATH,
    BuildProvenanceError,
    GitHubBuildProvenanceTransport,
    verify_build_artifact,
)
from control_plane.contracts.dokploy_target_record import (
    DokployTargetPolicies,
    DokployTargetRecord,
    DokployTargetStaffTestingHold,
)
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.promotion_record import PostDeployUpdateEvidence
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.contracts.deploy_target import ProviderTargetRecord
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
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductPreviewProfile,
)
from control_plane.contracts.product_reconcile import (
    GitHubAppWebhookDeliveryRecord,
    ProductReconcileLeaseLostError,
    ProductReconcileRequestRecord,
    ProductReconcileTarget,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.deploy_reference import provider_image_reference
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretSafetyRule,
)
from control_plane.contracts.secret_record import SecretBinding, SecretRecord
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
)
from control_plane.launchplane_reconcile_authorization import (
    build_launchplane_reconcile_authorization,
    launchplane_reconcile_generic_web_testing_allowed,
    launchplane_reconcile_preview_destination_allowed,
)
from control_plane.odoo_preview_apply_http import (
    ODOO_PREVIEW_APPLY_ROUTE,
    OdooPreviewApplyConfigError,
    OdooPreviewApplyEnvelope,
    build_odoo_preview_apply_inputs_result,
    observe_odoo_preview_apply_result,
)
from control_plane.product_reconcile_read import product_reconcile_request_view
from control_plane.product_review_status import OwnerReviewStatus, owner_review_reference_url
from control_plane.product_review import resolve_serving_preview
from control_plane.contracts.merge_train_policy import MergeTrainPolicy, MergeTrainPolicyRecord
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.product_reconcile import (
    PLAN_BLOCKER_DESCRIPTIONS,
    _fingerprint,
    PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS,
    PREVIEW_FEEDBACK_RECOVERY_MAX_FAILED_ATTEMPTS,
    TESTING_FAILURE_DESCRIPTIONS,
    TESTING_DEPLOY_MAX_FAILED_ATTEMPTS,
    RECONCILE_SOURCE,
    PreviewProviderHooks,
    ProductReconcileError,
    TestingProviderHooks,
    _plan_testing_target,
    request_product_reconcile_sweep,
    reconcile_reservation_scope,
    resolve_build_provenance_transport,
    resolve_pull_request_feedback_token,
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
    execute_odoo_preview_dokploy_apply,
)
from control_plane.workflows.odoo_stable_operation_worker import (
    OdooStableOperationWorkerResult,
    OdooStableOperationWorkerStore,
    OdooProdPromotionOperationWorkerStore,
    OdooProdRollbackOperationWorkerStore,
    run_odoo_stable_operation_worker_loop,
    run_odoo_stable_operation_worker_once,
)
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.support.durable_operations import durable_operation_authorization_payload
from tests.support.artifact_manifests import artifact_manifest_v2
from control_plane.workflows.generic_web_deploy_provider import (
    DokployGenericWebDeployProvider,
    GenericWebProviderDeploymentObservation,
)
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewDestroyRequest,
    GenericWebPreviewDestroyResult,
    GenericWebPreviewRefreshRequest,
    GenericWebPreviewRefreshResult,
    _build_preview_runtime_identity,
)
from tests.support.profiles import _odoo_preview_profile_payload, product_profile_payload
from tests.test_generic_web_deploy import _FakeGenericWebDeployProvider
from tests.support.application_runtime import converged_application_runtime
from control_plane.generic_web_deploy_http import GENERIC_WEB_DEPLOY_ROUTE
from control_plane.drivers.generic_web_preview_dispatch import GenericWebPreviewVerificationRequest
from control_plane.generic_web_verification_http import (
    GenericWebPreviewVerificationEnvelope,
    apply_generic_web_preview_verification_result,
)
from tests.support.stores import (
    _write_odoo_preview_template_runtime_environment,
    sqlite_database_url,
)

REPOSITORY = "example/site"
REPOSITORY_ID = "101"
IMAGE_REPOSITORY = "ghcr.io/example/site"
NEWEST = "3" * 40
DEPLOYABLE = "2" * 40
OLDER = "1" * 40
OFF_HISTORY = "9" * 40
PR_HEAD = "a" * 40


def _digest(commit: str) -> str:
    return f"sha256:{commit[0] * 64}"


class FakeGitHub:
    """GitHub's view of example/site: runs by id, a first-parent chain, and one PR."""

    def __init__(self) -> None:
        self.runs: dict[int, dict[str, object]] = {}
        self.image_digests: dict[str, str] = {}
        self.first_parents = {NEWEST: DEPLOYABLE, DEPLOYABLE: OLDER, OLDER: ""}
        self.pull_request: dict[str, object] = {
            "state": "open",
            "draft": False,
            "body": "## Client test notes\nNothing for the Client to test: Build plumbing only.",
            "labels": [],
            "head": {"sha": PR_HEAD},
        }
        self.pull_request_reads = 0
        # The open PR list the sweep reads.
        self.open_pulls: list[dict[str, object]] = []
        # (read count, change): the PR changes right after that many reads of it.
        self.pull_request_move: tuple[int, dict[str, object]] | None = None

    def add_run(self, run_id: int, commit: str, *, event: str = "push") -> None:
        if event == "pull_request" and commit not in self.first_parents:
            earlier = [run for run in self.runs.values() if run["event"] == "pull_request"]
            self.first_parents[commit] = str(earlier[-1]["head_sha"]) if earlier else ""
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
        if "/compare/" in path:
            base, head = path.split("/compare/")[1].split("...")
            chain: set[str] = set()
            cursor = head
            while cursor and cursor not in chain:
                chain.add(cursor)
                cursor = self.first_parents.get(cursor, "")
            status = "ahead" if base in chain else "behind"
            return {
                "status": status,
                "base_commit": {"sha": base},
                "merge_base_commit": {"sha": base if status == "ahead" else head},
            }
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
        if path.startswith(f"/repos/{REPOSITORY}/pulls?state=open"):
            return self.open_pulls if "page=1" in path else []
        if path.startswith(f"/repos/{REPOSITORY}/pulls/"):
            current = dict(self.pull_request)
            self.pull_request_reads += 1
            if self.pull_request_move and self.pull_request_move[0] == self.pull_request_reads:
                self.pull_request.update(self.pull_request_move[1])
            return current
        if "/artifacts?" in path:
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            return {
                "artifacts": [
                    {
                        "id": run_id * 10,
                        "name": f"artifact-manifest-{self.runs[run_id]['run_attempt']}",
                        "expired": False,
                    }
                ]
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
        manifest["image"] = {
            "repository": IMAGE_REPOSITORY,
            "digest": self.image_digests.get(commit, _digest(commit)),
        }
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zip_file:
            zip_file.writestr("artifact-manifest.json", json.dumps(manifest))
        return archive.getvalue()


def _profile(product: str = "site", *, repository_id: str = "") -> dict[str, object]:
    """A profile that, by default, stores no repository ids: the inventory is the authority."""
    payload = _odoo_preview_profile_payload(product)
    payload.update(
        repository=REPOSITORY if product == "site" else f"example/{product}",
        image={"repository": IMAGE_REPOSITORY, "scan_enabled": True},
        repository_id=repository_id,
        repository_owner_id="1" if repository_id else "",
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


class FakeGitHubComments:
    """example/site's PR comments and merged PRs, as the feedback token sees them."""

    def __init__(self) -> None:
        self.comments: dict[int, list[dict[str, object]]] = {}
        self.writes: list[tuple[str, int]] = []
        self.merged: dict[str, int] = {}
        self.tokens: list[str] = []
        self.fail_writes = False
        self._next_id = 900

    def on(self, number: int) -> list[dict[str, object]]:
        return self.comments.get(number, [])

    def request(
        self,
        *,
        path: str,
        token: str,
        method: str = "GET",
        body: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> object:
        self.tokens.append(token)
        prefix = f"/repos/{REPOSITORY}"
        if method == "GET" and path.startswith(f"{prefix}/commits/") and path.endswith("/pulls"):
            commit = path.split("/commits/")[1].split("/")[0]
            number = self.merged.get(commit)
            if number is None:
                return []
            return [
                {"number": number, "merged_at": "2026-09-30T10:00:00Z", "merge_commit_sha": commit}
            ]
        if method != "GET" and self.fail_writes:
            raise click.ClickException(f"GitHub API request failed for {path}: HTTP Error 502")
        if path.startswith(f"{prefix}/issues/comments/") and method == "DELETE":
            comment_id = int(path.rsplit("/", 1)[1])
            for number, comments in self.comments.items():
                for comment in comments:
                    if comment["id"] == comment_id:
                        comments.remove(comment)
                        self.writes.append(("DELETE", number))
                        return None
        if path.startswith(f"{prefix}/issues/comments/") and method == "PATCH":
            comment_id = int(path.rsplit("/", 1)[1])
            for number, comments in self.comments.items():
                for comment in comments:
                    if comment["id"] == comment_id:
                        assert body is not None
                        comment["body"] = body["body"]
                        self.writes.append(("PATCH", number))
                        return dict(comment)
        path, _, query = path.partition("?")
        if path.startswith(f"{prefix}/issues/") and path.endswith("/comments"):
            number = int(path.split("/issues/")[1].split("/")[0])
            if method == "GET":
                parameters = dict(item.split("=", 1) for item in query.split("&") if item)
                size, page = int(parameters["per_page"]), int(parameters["page"])
                comments = self.on(number)[(page - 1) * size : page * size]
                return [dict(comment) for comment in comments]
            if method == "POST":
                assert body is not None
                self._next_id += 1
                comment = {"id": self._next_id, "body": body["body"]}
                self.comments.setdefault(number, []).append(comment)
                self.writes.append(("POST", number))
                return dict(comment)
        raise AssertionError(f"unexpected GitHub {method} {path}")


class FakePreviewProvider:
    """Plans and 'runs' preview provider changes; Launchplane's lifecycle records are real."""

    def __init__(self) -> None:
        self.applied: list[tuple[str, int]] = []
        self.omitted_integration_credential_keys: tuple[str, ...] = ()

    def hooks(self) -> PreviewProviderHooks:
        return PreviewProviderHooks(
            build_inputs=self.build_inputs,
            execute_apply=self.execute_apply,
            observe_apply=self.observe_apply,
            quiescence_check=lambda **_: True,
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
            omitted_integration_credential_keys=self.omitted_integration_credential_keys,
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


# Real shapes of provider, script and exception text a redactor misses.
PROVIDER_FAILURE_TEXT = (
    "Dokploy compose 'site-prod-app' (compose-A1b2c3D4e5) has no appName; "
    'FATAL: database "cm_test" does not exist; ECONNREFUSED 203.0.113.42:22; '
    "ENOTFOUND database"
)
PROVIDER_FAILURE_FRAGMENTS = (
    "site-prod-app",
    "compose-A1b2c3D4e5",
    "cm_test",
    "203.0.113.42",
    "ENOTFOUND",
    "ECONNREFUSED",
    "appName",
)


class ProductReconcileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=getattr(self, "database_url", "")
            or sqlite_database_url(Path(temporary_directory.name) / "lp.sqlite3")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.store.write_repository_inventory_record(_inventory())
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_profile())
        )
        self.github = FakeGitHub()
        transport = patch(
            "control_plane.product_reconcile.resolve_build_provenance_transport",
            side_effect=lambda _store, _profile: self.github,
        )
        transport.start()
        self.addCleanup(transport.stop)
        self.provider = FakePreviewProvider()
        self.root = Path(temporary_directory.name)
        self.comments = FakeGitHubComments()
        self.public_origin = "https://launchplane.example.test"
        self.testing_hooks = TestingProviderHooks()
        from control_plane.artifact_scan import ArtifactScanResult

        scanner_patch = patch(
            "control_plane.artifact_scan.scan_product_artifact",
            side_effect=lambda image: ArtifactScanResult(
                image_reference=image,
                status="pass",
                reason="clean",
                scanned_at="2026-10-16T12:00:00Z",
            ),
        )
        self.artifact_scanner = scanner_patch.start()
        self.addCleanup(scanner_patch.stop)
        # The Owner-review status writes with the preview context's credential, not
        # the reconciler's App; tests that need a written status replace this.
        self.owner_review_status: OwnerReviewStatus | None = None
        self.owner_review_status_calls: list[tuple[str, int]] = []
        for module in (
            "control_plane.workflows.launchplane",
            "control_plane.product_reconcile_feedback",
        ):
            github_api = patch(f"{module}.github_api_request", side_effect=self.comments.request)
            github_api.start()
            self.addCleanup(github_api.stop)

    def request(self, target_kind: str = "testing", number: int | None = None) -> str:
        target = ProductReconcileTarget.model_validate(
            {"product": "site", "target_kind": target_kind, "pull_request_number": number}
        )
        return self.store.request_product_reconcile(target, "2026-09-29T12:00:00Z").target_key

    def run_once(self) -> ProductReconcileRequestRecord:
        # The fake provider's previews are unused, reproducible and isolated.
        from tests.support.disposable_lanes import record_safety

        profile = self.store.read_product_profile_record("site")
        for preview in self.store.list_preview_records():
            record_safety(
                self.store,
                profile.product,
                preview.context,
                profile.preview.slug_template.format(number=preview.anchor_pr_number),
                preview=preview,
            )
        if getattr(self, "advance_preview_recovery_grace", False):
            from control_plane.storage.postgres import LaunchplaneIdempotencyRow
            from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord

            # Retry-budget cases run only after the safety grace; other tests
            # exercise active executors, active grace and provider quiescence.
            for reservation in self.store.list_held_odoo_preview_reservations(product="site"):
                expired = datetime.now(timezone.utc) - timedelta(hours=1)
                aged = LaunchplaneIdempotencyRecord.model_validate(
                    {
                        **reservation.model_dump(),
                        "lease_expires_at": expired.isoformat(),
                        "updated_at": (expired - timedelta(minutes=1)).isoformat(),
                    }
                )
                with self.store._session_factory() as session:
                    row = session.get(LaunchplaneIdempotencyRow, reservation.record_id)
                    assert row is not None
                    self.store._sync_idempotency_row(row, aged)
                    session.commit()
        completed = run_product_reconcile_once(
            record_store=self.store,
            lease_owner="worker-a",
            transport_factory=lambda _store, _profile: self.github,
            control_plane_root=self.root,
            preview_hooks=self.provider.hooks(),
            testing_hooks=self.testing_hooks,
            feedback_token=lambda _store, _profile: "feedback-token",
            public_origin=lambda: self.public_origin,
            owner_review_status=self.write_owner_review_status,
        )
        assert completed is not None
        return completed

    def write_owner_review_status(
        self, profile: LaunchplaneProductProfileRecord, pull_request_number: int
    ) -> OwnerReviewStatus | None:
        self.owner_review_status_calls.append((profile.product, pull_request_number))
        return self.owner_review_status

    def reconcile(self) -> dict[str, object]:
        completed = self.run_once()
        self.assertEqual(completed.state, "done", completed.last_error)
        return dict(completed.last_plan)

    def _running_release(self, run_id: int, commit: str) -> None:
        self.github.add_run(run_id, commit)
        verified = verify_build_artifact(
            transport=self.github,
            repository=REPOSITORY,
            repository_id=REPOSITORY_ID,
            commit=commit,
            purpose="release",
            context="cm",
            image_repository=IMAGE_REPOSITORY,
        )
        self.store.write_artifact_manifest(verified.manifest)
        self.store.write_release_tuple_record(
            ReleaseTupleRecord(
                tuple_id="running-testing",
                context="cm",
                channel="testing",
                artifact_id=verified.manifest.artifact_id,
                repo_shas={"site": commit},
                image_repository=IMAGE_REPOSITORY,
                image_digest=_digest(commit),
                provenance="ship",
                minted_at="2026-09-30T12:00:00Z",
            )
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
                preview_label=f"cm/site/pr-{number}",
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
        try:
            previous = self.store.read_dokploy_target_record(
                context_name="cm", instance_name="testing"
            )
        except FileNotFoundError:
            previous = DokployTargetRecord(
                context="cm", instance="testing", updated_at="2026-09-30T09:00:00Z"
            )
        self.store.write_dokploy_target_record(
            previous.model_copy(
                update={
                    "policies": DokployTargetPolicies(
                        staff_testing_hold=DokployTargetStaffTestingHold(
                            reason="Staff are testing checkout.",
                            recorded_by="site-operator",
                            recorded_at="2026-09-30T09:00:00Z",
                        )
                    ),
                    "updated_at": "2026-09-30T09:00:00Z",
                }
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
    def test_requeued_work_yields_to_waiting_products_without_losing_requests(self) -> None:
        outcomes: tuple[Literal["done", "failed"], ...] = ("done", "failed")
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                busy = ProductReconcileTarget(product=f"busy-{outcome}", target_kind="testing")
                waiting = ProductReconcileTarget(
                    product=f"waiting-{outcome}", target_kind="testing"
                )
                self.store.request_product_reconcile(busy, "2026-09-29T12:00:00Z")
                claimed = self.store.claim_next_product_reconcile_request(
                    "worker-a", 60, now="2026-09-29T12:00:00Z"
                )
                assert claimed is not None
                self.assertEqual(claimed.target_key, busy.target_key)
                self.store.request_product_reconcile(waiting, "2026-09-29T12:00:10Z")
                rerequested = self.store.request_product_reconcile(busy, "2026-09-29T12:00:20Z")
                plan: dict[str, object] = {"deferred": "lane_busy"}
                error = "lane unavailable" if outcome == "failed" else ""
                completed = self.store.complete_product_reconcile_request(
                    busy.target_key,
                    "worker-a",
                    outcome,
                    plan,
                    error,
                    now="2026-09-29T12:00:30Z",
                )
                self.assertEqual(completed.state, "pending")
                self.assertEqual(completed.request_count, rerequested.request_count)
                self.assertEqual(completed.last_plan, plan)
                self.assertEqual(completed.last_error, error)

                next_claim = self.store.claim_next_product_reconcile_request(
                    "worker-a", 60, now="2026-09-29T12:00:30Z"
                )
                assert next_claim is not None
                self.assertEqual(next_claim.target_key, waiting.target_key)
                self.store.complete_product_reconcile_request(
                    waiting.target_key, "worker-a", "done", {}, now="2026-09-29T12:00:40Z"
                )
                retry = self.store.claim_next_product_reconcile_request(
                    "worker-a", 60, now="2026-09-29T12:00:40Z"
                )
                assert retry is not None
                self.assertEqual(retry.target_key, busy.target_key)
                self.assertEqual(retry.request_count, rerequested.request_count)
                self.store.complete_product_reconcile_request(
                    busy.target_key, "worker-a", "done", {}, now="2026-09-29T12:00:50Z"
                )

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

    def test_a_renewed_lease_is_not_reclaimed_and_only_its_holder_renews_it(self) -> None:
        self.request()
        self.store.claim_next_product_reconcile_request("worker-a", 60, now="2026-09-29T12:00:00Z")

        self.assertTrue(
            self.store.renew_product_reconcile_lease(
                "site:testing", "worker-a", 60, now="2026-09-29T12:00:50Z"
            )
        )
        self.assertFalse(
            self.store.renew_product_reconcile_lease(
                "site:testing", "worker-b", 60, now="2026-09-29T12:00:55Z"
            )
        )
        self.assertIsNone(
            self.store.claim_next_product_reconcile_request(
                "worker-b", 60, now="2026-09-29T12:01:30Z"
            )
        )
        reclaimed = self.store.claim_next_product_reconcile_request(
            "worker-b", 60, now="2026-09-29T12:02:00Z"
        )
        assert reclaimed is not None
        self.assertFalse(
            self.store.renew_product_reconcile_lease(
                "site:testing", "worker-a", 60, now="2026-09-29T12:02:10Z"
            )
        )

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
    def test_failed_restore_holds_new_builds_until_this_target_has_restore_proof(self) -> None:
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context="cm",
                instance="testing",
                target_id="testing-target",
                updated_at="2026-09-29T12:00:00Z",
            )
        )
        restore = DeploymentRecord(
            record_id="failed-restore",
            context="cm",
            instance="testing",
            source_git_ref=OLDER,
            resolved_target=ResolvedTargetEvidence(
                target_type="compose", target_id="testing-target", target_name="fixture"
            ),
            deploy=DeploymentEvidence(
                deploy_mode="recreate-in-place",
                status="fail",
                target_name="fixture",
                target_type="compose",
                started_at="2026-09-29T11:00:00Z",
                finished_at="2026-09-29T11:01:00Z",
            ),
            post_deploy_update=PostDeployUpdateEvidence(
                attempted=True,
                status="fail",
                evidence={
                    "workflow_intent": "restore",
                    "post_deploy_readback_odoo_restore_completed": "false",
                },
            ),
        )
        self.store.write_deployment_record(restore)
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        plan = self.reconcile()
        self.assertEqual(plan["reason"], TESTING_RESTORE_REQUIRED_CODE)
        self.assertEqual(plan["failed_restore_deployment_record_id"], restore.record_id)
        self.assertFalse(self.store.list_odoo_stable_target_replacement_operation_records())
        # A later ordinary failure does not supersede the data prerequisite.
        maintenance = restore.model_copy(
            update={
                "record_id": "failed-maintenance",
                "deploy": restore.deploy.model_copy(
                    update={
                        "started_at": "2026-09-29T12:00:00Z",
                        "finished_at": "2026-09-29T12:01:00Z",
                    }
                ),
                "post_deploy_update": restore.post_deploy_update.model_copy(
                    update={"evidence": {"workflow_intent": "deploy"}}
                ),
            }
        )
        self.store.write_deployment_record(maintenance)
        self.github.add_run(21, DEPLOYABLE)
        self.request()
        self.assertEqual(self.reconcile()["reason"], TESTING_RESTORE_REQUIRED_CODE)
        from control_plane.testing_restore_prerequisite import incomplete_testing_restore

        # A retained target's failure cannot fence an independent replacement target.
        target = self.store.read_dokploy_target_id_record(
            context_name="cm", instance_name="testing"
        )
        self.store.write_dokploy_target_id_record(
            target.model_copy(update={"target_id": "independent-target"})
        )
        self.assertIsNone(incomplete_testing_restore(self.store, context="cm", instance="testing"))
        self.store.write_dokploy_target_id_record(target)
        # The supported governed restore provides completion proof; no hold is cleared by hand.
        self.store.write_deployment_record(
            restore.model_copy(
                update={
                    "record_id": "completed-restore",
                    "deploy": restore.deploy.model_copy(
                        update={
                            "started_at": "2026-09-29T13:00:00Z",
                            "finished_at": "2026-09-29T13:01:00Z",
                        }
                    ),
                    "post_deploy_update": restore.post_deploy_update.model_copy(
                        update={
                            "status": "pass",
                            "evidence": {
                                "workflow_intent": "restore",
                                "post_deploy_readback_odoo_restore_completed": "true",
                            },
                        }
                    ),
                }
            )
        )
        self.request()
        queued = self.reconcile()["queued_operation_id"]
        self.store.write_deployment_record(
            restore.model_copy(
                update={
                    "record_id": "interrupted-restore-after-queue",
                    "runtime_source": {"data_source_mode": "upstream_restore"},
                    "deploy": restore.deploy.model_copy(
                        update={"started_at": "2026-09-29T14:00:00Z", "finished_at": ""}
                    ),
                    "post_deploy_update": PostDeployUpdateEvidence(),
                }
            )
        )
        self.request()
        # The prerequisite must not hide an already-active attempt or its fence.
        self.assertEqual(self.reconcile()["queued_operation_id"], queued)

    def test_unverified_fresh_generation_holds_without_queueing(self) -> None:
        for error in (ValueError, FileNotFoundError, click.ClickException):
            with self.subTest(error=error):
                self.github.add_run(20, DEPLOYABLE)
                self.request()
                with patch(
                    "control_plane.odoo_testing_restore.read_current_testing_generation",
                    side_effect=error("private credential comparison detail"),
                ):
                    outcome = self.reconcile()
                self.assertEqual(outcome["action"], "wait")
                self.assertTrue(outcome["held"])
                self.assertEqual(outcome["reason"], "fresh_testing_authority_unverified")
                self.assertFalse(self.store.list_odoo_stable_target_replacement_operation_records())
                self.assertNotIn("private credential", json.dumps(outcome))

    def test_incomplete_run_listing_cannot_downgrade_testing(self) -> None:
        self._running_release(20, DEPLOYABLE)
        self.github.add_run(10, OLDER)
        get_json = self.github.get_json

        def incomplete(path: str) -> object:
            if "/actions/workflows/build.yml/runs?" in path:
                return {"total_count": 2, "workflow_runs": [self.github.runs[10]]}
            return get_json(path)

        self.request()
        with patch.object(self.github, "get_json", incomplete):
            plan = self.reconcile()
        self.assertEqual(plan["reason"], "incomplete_build_runs")
        self.assertEqual(plan["action"], "none")
        self.assertEqual(plan["build_runs_seen"], 1)
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), ())

    def test_truncated_first_parent_history_cannot_downgrade_testing(self) -> None:
        self._running_release(20, DEPLOYABLE)
        self.github.add_run(10, OLDER)
        self.github.first_parents = {NEWEST: DEPLOYABLE, OLDER: ""}
        self.request()
        plan = self.reconcile()
        self.assertEqual(plan["reason"], "incomplete_commit_history")
        self.assertFalse(plan["current_commit_seen"])
        self.assertEqual(plan["action"], "none")
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), ())

    def test_held_testing_build_is_found_beyond_first_run_and_commit_pages(self) -> None:
        self._running_release(1, OLDER)
        for run_id in range(2, 52):
            self.github.add_run(run_id, DEPLOYABLE)
        intermediate = [f"{index:040x}" for index in range(100, 450)]
        chain = [DEPLOYABLE, *intermediate, OLDER]
        self.github.first_parents = dict(zip(chain, [*chain[1:], ""], strict=True))
        get_json = self.github.get_json

        def paged(path: str) -> object:
            page = int(path.rsplit("page=", 1)[1]) if "&page=" in path else 1
            if "/actions/workflows/build.yml/runs?" in path:
                runs = sorted(self.github.runs.values(), key=lambda run: -int(str(run["id"])))
                return {
                    "total_count": len(runs),
                    "workflow_runs": runs[(page - 1) * 50 : page * 50],
                }
            if "/commits?" in path:
                return [
                    {
                        "sha": sha,
                        "parents": [{"sha": self.github.first_parents[sha]}]
                        if self.github.first_parents[sha]
                        else [],
                    }
                    for sha in chain[(page - 1) * 100 : page * 100]
                ]
            return get_json(path)

        self.request()
        with patch.object(self.github, "get_json", paged):
            plan = self.reconcile()
        self.assertEqual(plan["action"], "deploy")
        self.assertEqual(plan["desired_commit"], DEPLOYABLE)
        self.assertTrue(plan["current_commit_seen"])
        self.assertGreater(cast(int, plan["commit_history_seen"]), 300)
        self.assertGreater(cast(int, plan["build_runs_seen"]), 50)

    def test_failed_verification_of_current_build_cannot_fall_back_to_its_ancestor(self) -> None:
        self._running_release(20, DEPLOYABLE)
        self.github.add_run(10, OLDER)
        download = self.github.get_bytes

        def unavailable(path: str) -> bytes:
            if "/artifacts/200/" in path:
                raise BuildProvenanceError(
                    "Manifest temporarily unavailable", code="github_read_failed"
                )
            return download(path)

        self.request()
        with patch.object(self.github, "get_bytes", unavailable):
            plan = self.reconcile()
        self.assertEqual(plan["reason"], "ancestor_build")
        self.assertEqual(plan["action"], "none")
        self.assertEqual(
            plan["rejected_builds"],
            [
                {
                    "commit": DEPLOYABLE,
                    "run_id": 20,
                    "code": "github_read_failed",
                    "error": "Manifest temporarily unavailable",
                }
            ],
        )
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), ())

    def test_native_rebuild_cannot_select_ancestor_when_current_manifest_is_unavailable(
        self,
    ) -> None:
        from control_plane.product_reconcile import latest_verified_testing_build

        self._running_release(20, DEPLOYABLE)
        self.github.add_run(10, OLDER)
        download = self.github.get_bytes

        def unavailable(path: str) -> bytes:
            if "/artifacts/200/" in path:
                raise BuildProvenanceError(
                    "Fixture current manifest unavailable", code="github_read_failed"
                )
            return download(path)

        profile = self.store.read_product_profile_record("site")
        lane = next(item for item in profile.lanes if item.instance == "testing")
        with (
            patch.object(self.github, "get_bytes", unavailable),
            self.assertRaises(ProductReconcileError) as refused,
        ):
            latest_verified_testing_build(self.store, profile, lane)
        self.assertEqual(refused.exception.code, "ancestor_build")
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), ())

    def test_rejected_newer_manifest_records_reason_and_run_before_falling_back(self) -> None:
        self._running_release(20, DEPLOYABLE)
        self.github.add_run(30, NEWEST)
        self.github.add_run(40, NEWEST)
        download = self.github.get_bytes

        def wrong_commit(path: str) -> bytes:
            if "/artifacts/400/" in path:
                return download(path.replace("/artifacts/400/", "/artifacts/200/"))
            return download(path)

        self.request()
        with patch.object(self.github, "get_bytes", wrong_commit):
            plan = self.reconcile()
        self.assertEqual((plan["action"], plan["reason"]), ("none", "already_deployed"))
        self.assertEqual(
            plan["rejected_builds"],
            [
                {
                    "commit": NEWEST,
                    "run_id": 40,
                    "code": "manifest_commit_mismatch",
                    "error": "The build manifest names a different source commit.",
                }
            ],
        )
        request = self.store.read_product_reconcile_request("site:testing")
        self.assertEqual(request.last_plan["rejected_builds"], plan["rejected_builds"])

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

        self.artifact_scanner.side_effect = AssertionError(
            "An active attempt must not be rescanned"
        )
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
        incidents = self.store.list_public_ingress_incident_records(status="open")
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].instance, "testing")
        self.assertTrue(
            any(
                operation.operation_id in incidents[0].summary
                for operation in self.store.list_odoo_stable_target_replacement_operation_records()
            )
        )

    def test_fresh_generation_keeps_history_and_gets_its_own_bounded_attempts(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        with patch("control_plane.product_reconcile._utc_now", return_value="2026-09-29T12:00:00Z"):
            for _ in range(TESTING_DEPLOY_MAX_FAILED_ATTEMPTS):
                self.request()
                old_operation_id = cast(str, self.reconcile()["queued_operation_id"])
                self.finish(old_operation_id, "fail")
        retained = self.store.list_odoo_stable_target_replacement_operation_records()
        generation = FreshOdooTestingGeneration(
            product="site",
            context="cm",
            operation_id="initial-testing-fixture",
            target_id="independent-testing-target",
            namespace="lp-testing-" + "a" * 32,
            domain_host="fresh.example.invalid",
            resources_binding_sha256="b" * 64,
            bootstrap_plan_sha256="c" * 64,
            request_sha256="d" * 64,
            prepared_at="2026-09-30T12:00:00Z",
            reconcile_requested_at="2026-09-30T12:01:00Z",
        )
        with (
            patch(
                "control_plane.odoo_testing_restore.read_current_testing_generation",
                return_value=generation,
            ),
            patch("control_plane.product_reconcile._utc_now", return_value=generation.prepared_at),
        ):
            for _ in range(TESTING_DEPLOY_MAX_FAILED_ATTEMPTS):
                self.request()
                queued = cast(str, self.reconcile()["queued_operation_id"])
                operation = self.store.read_odoo_stable_target_replacement_operation_record(queued)
                self.assertEqual(operation.request.data_source_mode, "upstream_restore")
                self.request()
                self.assertEqual(self.reconcile()["queued_operation_id"], queued)
                self.finish(queued, "fail")
            self.request()
            with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                exhausted = self.run_once()
        self.assertEqual(exhausted.state, "failed")
        self.assertNotIn("queued_operation_id", exhausted.last_plan)
        for operation in retained:
            self.assertEqual(
                self.store.read_odoo_stable_target_replacement_operation_record(
                    operation.operation_id
                ),
                operation,
            )

    def fail_with_provider_text(self, operation_id: str) -> None:
        operation = self.store.read_odoo_stable_target_replacement_operation_record(operation_id)
        self.store.write_odoo_stable_target_replacement_operation_record(
            operation.model_copy(
                update={
                    "status": "fail",
                    "phase": "failed",
                    "finished_at": "2026-09-30T12:00:00Z",
                    "attempt": 1,
                    "result": OdooStableTargetReplacementApplyResult(
                        product="site",
                        context="cm",
                        instance="testing",
                        strategy="recreate-in-place",
                        deploy_status="pass",
                        post_deploy_status="pass",
                        health_status="fail",
                        target_id="compose-A1b2c3D4e5",
                        target_name="site-prod-app",
                    ),
                    "error_message": PROVIDER_FAILURE_TEXT,
                }
            )
        )

    def test_failed_testing_deploy_reason_is_structured_with_no_provider_text(self) -> None:
        # The operation's own status read needs the grant that starts a deploy, so
        # the product read must say why; provider text is never copied there.
        self.github.add_run(20, DEPLOYABLE)
        for _attempt in range(3):
            self.request()
            self.fail_with_provider_text(cast(str, self.reconcile()["queued_operation_id"]))
        self.request()

        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            completed = self.run_once()

        view = product_reconcile_request_view(completed)
        self.assertEqual(view.last_plan["last_failed_error_code"], "health_check_failed")
        self.assertEqual(
            view.last_plan["last_failed_error_summary"],
            "The health check did not pass. Steps: deploy pass, post-deploy pass, "
            "setting overrides skipped, health fail, canonical skipped, logo skipped. "
            "Worker attempt 1.",
        )
        self.assertIn("health_check_failed: The health check did not pass.", view.last_error)
        read = view.model_dump_json()
        for leaked in PROVIDER_FAILURE_FRAGMENTS:
            self.assertNotIn(leaked, read)

    def test_an_operation_error_code_keeps_its_fixed_description(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        first = cast(str, self.reconcile()["queued_operation_id"])
        operation = self.store.read_odoo_stable_target_replacement_operation_record(first)
        self.store.write_odoo_stable_target_replacement_operation_record(
            operation.model_copy(
                update={
                    "status": "fail",
                    "phase": "failed",
                    "finished_at": "2026-09-30T12:00:00Z",
                    "error_code": "operation_authorization_revoked",
                    "error_message": "Revoked for site-prod-app on 203.0.113.42.",
                }
            )
        )
        self.request()

        plan = self.reconcile()

        self.assertEqual(plan["last_failed_error_code"], "operation_authorization_revoked")
        self.assertEqual(
            plan["last_failed_error_summary"],
            "The deploy's authorization was removed or narrowed before it ran.",
        )

    def test_a_deploy_check_code_keeps_its_fixed_description_and_keys(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        first = cast(str, self.reconcile()["queued_operation_id"])
        operation = self.store.read_odoo_stable_target_replacement_operation_record(first)
        self.store.write_odoo_stable_target_replacement_operation_record(
            operation.model_copy(
                update={
                    "status": "fail",
                    "phase": "failed",
                    "finished_at": "2026-09-30T12:00:00Z",
                    "error_code": "deploy_blocked.compose_keys_missing",
                    "error_message": "Missing ODOO_DB_HOST on site-prod-app at 203.0.113.42.",
                    "error_detail_keys": ("ODOO_DB_HOST",),
                }
            )
        )
        self.request()

        plan = self.reconcile()

        self.assertEqual(plan["last_failed_error_code"], "deploy_blocked.compose_keys_missing")
        self.assertEqual(
            plan["last_failed_error_summary"],
            "The compose template requires settings that neither the site's records nor the "
            "target provide. Keys: ODOO_DB_HOST.",
        )

    def test_a_blocked_plan_code_describes_its_blocker_without_message_text(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        operation_id = cast(str, self.reconcile()["queued_operation_id"])
        expected = (
            (
                "plan_not_ready.volume_authority_drift",
                "The replacement plan was blocked before the deploy started. Blocker: "
                "The current target's Odoo volume settings do not match Launchplane's "
                "stored settings.",
            ),
            (
                "plan_not_ready.some_future_blocker",
                "The replacement plan was blocked before the deploy started. Blocker: "
                "Launchplane does not describe this blocker.",
            ),
        )
        for error_code, summary in expected:
            with self.subTest(error_code=error_code):
                operation = self.store.read_odoo_stable_target_replacement_operation_record(
                    operation_id
                )
                self.store.write_odoo_stable_target_replacement_operation_record(
                    operation.model_copy(
                        update={
                            "status": "fail",
                            "phase": "failed",
                            "finished_at": "2026-09-30T12:00:00Z",
                            "error_code": error_code,
                            "error_message": "Volume drift on site-prod-app at 203.0.113.42.",
                        }
                    )
                )
                self.request()

                plan = self.reconcile()

                self.assertEqual(plan["last_failed_error_code"], error_code)
                self.assertEqual(plan["last_failed_error_summary"], summary)
                operation_id = cast(str, plan["queued_operation_id"])

    def test_a_key_list_failure_names_the_keys_without_message_text(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        operation_id = cast(str, self.reconcile()["queued_operation_id"])
        operation = self.store.read_odoo_stable_target_replacement_operation_record(operation_id)
        self.store.write_odoo_stable_target_replacement_operation_record(
            operation.model_copy(
                update={
                    "status": "fail",
                    "phase": "failed",
                    "finished_at": "2026-09-30T12:00:00Z",
                    "error_code": "plan_not_ready.provider_keys_unrecorded",
                    "error_message": "Unrecorded ODOO_WEB_HOST_PORT on 203.0.113.42.",
                    # Stored unvalidated here; the read drops what is not a key name.
                    "error_detail_keys": ("ODOO_WEB_HOST_PORT", "203.0.113.42", "ODOO_DB_NAME"),
                }
            )
        )
        self.request()

        plan = self.reconcile()

        self.assertEqual(plan["last_failed_error_code"], "plan_not_ready.provider_keys_unrecorded")
        self.assertEqual(
            plan["last_failed_error_summary"],
            f"{TESTING_FAILURE_DESCRIPTIONS['plan_not_ready']} Blocker: "
            f"{PLAN_BLOCKER_DESCRIPTIONS['provider_keys_unrecorded']} "
            "Keys: ODOO_DB_NAME, ODOO_WEB_HOST_PORT.",
        )
        self.assertNotIn("203.0.113.42", cast(str, plan["last_failed_error_summary"]))

    def test_an_operation_stored_before_detail_keys_still_loads(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        operation_id = cast(str, self.reconcile()["queued_operation_id"])
        payload = self.store.read_odoo_stable_target_replacement_operation_record(
            operation_id
        ).model_dump(mode="json")
        payload.pop("error_detail_keys")

        operation = OdooStableTargetReplacementOperationRecord.model_validate(payload)

        self.assertEqual(operation.error_detail_keys, ())

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
    def test_replaced_testing_target_gets_a_fresh_attempt_without_failure_budget(self) -> None:
        from control_plane.contracts.disposable_lane import DisposableTestingSupersession

        self.github.add_run(20, DEPLOYABLE)
        self.request()
        first = cast(str, self.reconcile()["queued_operation_id"])
        operation = self.store.read_odoo_stable_target_replacement_operation_record(first)
        self.store.write_odoo_stable_target_replacement_operation_record(
            OdooStableTargetReplacementOperationRecord.model_validate(
                {
                    **operation.model_dump(mode="json"),
                    "status": "superseded",
                    "phase": "superseded",
                    "finished_at": operation.updated_at,
                    "disposable_supersession": DisposableTestingSupersession(
                        binding_sha256="a" * 64,
                        retained_target_id="retained-fixture",
                        replacement_target_id="replacement-fixture",
                        evidence_reference="fixture:isolated-replacement",
                        recorded_at=datetime.now(timezone.utc),
                    ).model_dump(mode="json"),
                }
            )
        )

        self.request()
        redeployed = self.reconcile()

        second = cast(str, redeployed["queued_operation_id"])
        self.assertNotEqual(second, first)
        self.assertNotIn("last_failed_operation_id", redeployed)
        retry = self.store.read_odoo_stable_target_replacement_operation_record(second)
        self.assertNotEqual(retry.idempotency_key, operation.idempotency_key)

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


class FakeGenericWebGitHub(FakeGitHub):
    """example/site as a generic-web product: its build uploads the generic-web manifest."""

    def get_json(self, path: str) -> object:
        if "/attempts/" in path:
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            return self.runs[run_id]
        payload = super().get_json(path)
        if "/actions/workflows/build.yml/runs?" in path:
            assert isinstance(payload, dict)
            payload["total_count"] = len(cast(list[object], payload["workflow_runs"]))
        return payload

    def get_bytes(self, path: str) -> bytes:
        run_id = int(path.split("/actions/artifacts/")[1].split("/")[0]) // 10
        commit = cast(str, self.runs[run_id]["head_sha"])
        manifest = {
            "schema_version": 1,
            "kind": "generic-web",
            "source_commit": commit,
            "image": {
                "repository": IMAGE_REPOSITORY,
                "digest": self.image_digests.get(commit, _digest(commit)),
            },
        }
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zip_file:
            zip_file.writestr("artifact-manifest.json", json.dumps(manifest))
        return archive.getvalue()


def _generic_web_profile() -> LaunchplaneProductProfileRecord:
    payload = product_profile_payload("site")
    payload.update(
        repository=REPOSITORY,
        image={"repository": IMAGE_REPOSITORY, "scan_enabled": True},
        lanes=(
            {"instance": "testing", "context": "cm", "base_url": "https://testing.example.test"},
            {"instance": "prod", "context": "cm", "base_url": "https://example.test"},
        ),
        preview={"enabled": False},
    )
    return LaunchplaneProductProfileRecord.model_validate(payload)


class ProductReconcileGenericWebTestingTests(ProductReconcileTestCase):
    def settle_terminal_testing_original(self) -> DeploymentRecord:
        from tests.test_generic_web_deploy_recovery import _create_recovery_app, _OPERATOR_TOKEN
        from tests.support.auth import local_operator_policy
        from tests.test_service import _invoke_app
        from control_plane.workflows.generic_web_deploy_provider import (
            decode_generic_web_provider_reconciliation_target,
        )
        from control_plane.provider_operations import build_provider_operation_key
        from control_plane.workflows.generic_web_application_snapshot import (
            generic_web_provider_deployment_record_id,
        )

        (original,) = self.store.list_held_provider_target_reservations()
        snapshot = decode_generic_web_provider_reconciliation_target(original.reconciliation_key)
        app = _create_recovery_app(
            root=self.root,
            store=self.store,
            authz_policy=local_operator_policy(
                actions=("generic_web_deploy.execute",),
                products=("site",),
                contexts=("cm",),
            ),
        )
        body = {
            "schema_version": 1,
            "product": "site",
            "instance": "testing",
            "original_deploy": snapshot.original_event_deploy,
            "reason": "Settle original terminal qualification without repeating effects.",
        }
        provider = getattr(self, "application_inputs_provider", self.deploys)
        with patch(
            "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
            return_value=provider,
        ):
            status, dry = _invoke_app(
                app,
                method="POST",
                path="/v1/admin/generic-web/deploy-recovery/dry-run",
                authorization=f"Bearer {_OPERATOR_TOKEN}",
                payload=body,
                headers={"Idempotency-Key": original.idempotency_key},
            )
            self.assertEqual(status, 200, dry)
            self.assertEqual(dry["proposed_action"], "settle_testing_failure", dry)
            status, settled = _invoke_app(
                app,
                method="POST",
                path="/v1/admin/generic-web/deploy-recovery/apply",
                authorization=f"Bearer {_OPERATOR_TOKEN}",
                payload={**body, "expected_recovery_digest": dry["recovery_digest"]},
                headers={"Idempotency-Key": original.idempotency_key},
            )
            self.assertEqual(
                (status, settled.get("reservation_state")), (202, "completed"), settled
            )
        operation = build_provider_operation_key(
            scope=original.scope,
            route_path=original.route_path,
            idempotency_key=original.idempotency_key,
            request_fingerprint=original.request_fingerprint,
            reconciliation_key=original.reconciliation_key,
        )
        return self.store.read_deployment_record(
            generic_web_provider_deployment_record_id(operation, context="cm", instance="testing")
        )

    def application_input_provider(self) -> tuple[dict[str, object], list[str]]:
        self.store.write_dokploy_target_record(
            DokployTargetRecord(
                context="cm",
                instance="testing",
                target_type="application",
                target_name="site-testing-app",
                updated_at="2026-10-19T00:00:00Z",
            )
        )
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context="cm",
                instance="testing",
                target_id="target-123",
                updated_at="2026-10-19T00:00:00Z",
            )
        )
        self.store.write_provider_target_record(
            ProviderTargetRecord(
                context="cm",
                instance="testing",
                provider_id="dokploy",
                target_category="application",
                provider_target_type="application",
                target_id="target-123",
                display_name="site-testing-app",
                updated_at="2026-10-19T00:00:00Z",
            )
        )
        provider = DokployGenericWebDeployProvider()
        self.application_inputs_provider = provider
        self.testing_hooks = TestingProviderHooks(generic_web_deploy_provider=lambda: provider)
        payload: dict[str, object] = {"env": "PROVIDER_ONLY=keep"}
        triggered: list[str] = []

        def update(**kwargs: object) -> None:
            payload["env"] = kwargs["env_text"]

        def runtime(**_kwargs: object) -> ApplicationRuntimeObservation:
            from control_plane.dokploy.api import parse_dokploy_env_text

            values = parse_dokploy_env_text(str(payload["env"]))
            source = values.get("LAUNCHPLANE_SOURCE_GIT_REF", DEPLOYABLE)
            artifact = values.get(
                "LAUNCHPLANE_ARTIFACT_ID", f"{IMAGE_REPOSITORY}@{_digest(source)}"
            )
            record_id = values.get("LAUNCHPLANE_DEPLOYMENT_RECORD_ID", "fixture-no-deployment")
            return ApplicationRuntimeObservation(
                application_id="target-123",
                app_name="site-testing-app",
                service_id="fixture-service",
                service_image=provider_image_reference(artifact_id=artifact),
                service_deployment_record_id=record_id,
                desired_replicas=1,
                rollout_state="completed",
                rollout_started_at="2026-10-19T00:00:30Z",
                running_container_images=((artifact,),),
                running_container_deployment_record_ids=(record_id,),
                running_container_health=("",),
                pending_task_count=0,
            )

        self.application_runtime = runtime

        for patcher in (
            patch.object(provider, "_read_provider_config", return_value=("fixture", "unused")),
            patch.object(provider, "observe_testing_application_runtime", side_effect=runtime),
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                side_effect=lambda **_: dict(payload),
            ),
            patch("control_plane.dokploy.api.latest_deployment_for_target", return_value=None),
            patch("control_plane.dokploy.api.update_dokploy_target_env", side_effect=update),
            patch("control_plane.dokploy.api.dokploy_request", return_value={}),
            patch(
                "control_plane.dokploy.api.trigger_deployment",
                side_effect=lambda **kwargs: triggered.append(str(kwargs.get("title"))),
            ),
            patch("control_plane.dokploy.api.wait_for_target_deployment", return_value=None),
            patch.object(
                provider,
                "observe_artifact_deploy",
                return_value=GenericWebProviderDeploymentObservation(
                    outcome="present",
                    deployment_status="success",
                    deployment_id="provider-input-deploy",
                    started_at="2026-10-19T00:00:00Z",
                    finished_at="2026-10-19T00:01:00Z",
                ),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return payload, triggered

    def test_inert_inventory_correction_gets_one_reconcile_successor(self) -> None:
        self.check_inert_inventory_correction()

    def test_inert_inventory_correction_preserves_lifecycle_before_successor(self) -> None:
        self.check_inert_inventory_correction(lifecycle=True)

    def test_inert_inventory_completion_failure_keeps_original_fence(self) -> None:
        self.check_inert_inventory_correction(completion_failure=True)

    def test_unknown_inventory_successor_stays_fenced_after_another_correction(self) -> None:
        self.check_inert_inventory_correction(unknown_successor=True)

    def check_inert_inventory_correction(
        self,
        *,
        lifecycle: bool = False,
        completion_failure: bool = False,
        unknown_successor: bool = False,
    ) -> None:
        from control_plane.dokploy import api
        from control_plane.generic_web_testing_settlement import testing_attempt_was_settled
        from control_plane.provider_operations import ProviderMutationUnknownError
        from control_plane.workflows.generic_web_deploy import GenericWebDeployResult
        from control_plane.workflows.generic_web_execution import evidence_digest, execution_receipt

        payload, triggered = self.application_input_provider()
        schedule = self.application_input_schedule() if lifecycle else None
        self.github.add_run(10, OLDER)
        self.request()
        self.reconcile()
        previous_schedule_calls = (
            list(cast(Any, api.upsert_dokploy_application_schedule).call_args_list)
            if lifecycle
            else []
        )
        inventory = self.store.read_environment_inventory(
            context_name="cm", instance_name="testing"
        )
        corrected = inventory.model_copy(
            update={
                "post_deploy_update": inventory.post_deploy_update.model_copy(
                    update={"detail": "Independently corrected inventory evidence"}
                )
            }
        )
        previous_ids = {record.record_id for record in self.store.list_deployment_records()}
        previous_payload = dict(payload)
        previous_triggers = list(triggered)
        previous_provider_calls = list(cast(Any, api.dokploy_request).call_args_list)
        checkpoint = self.store.checkpoint_mutation_provider_effect

        def correct_before_checkpoint(**arguments: Any) -> Any:
            with closing(
                PostgresRecordStore(
                    database_url=getattr(self, "database_url", "")
                    or sqlite_database_url(self.root / "lp.sqlite3")
                )
            ) as writer:
                writer.write_environment_inventory(corrected)
            return checkpoint(**arguments)

        self.github.add_run(20, DEPLOYABLE)
        self.request()
        with (
            patch.object(
                self.store,
                "checkpoint_mutation_provider_effect",
                side_effect=correct_before_checkpoint,
            ),
            (
                patch.object(
                    self.store,
                    "complete_mutation_reservation",
                    side_effect=RuntimeError("Completion unavailable"),
                )
                if completion_failure
                else nullcontext()
            ),
        ):
            refused = self.run_once()
        self.assertEqual(refused.state, "failed", refused.last_plan)
        self.assertEqual(payload, previous_payload)
        self.assertEqual(triggered, previous_triggers)
        self.assertEqual(cast(Any, api.dokploy_request).call_args_list, previous_provider_calls)
        (original,) = (
            record
            for record in self.store.list_deployment_records()
            if record.record_id not in previous_ids
        )
        receipt = execution_receipt(original)
        assert receipt is not None
        self.assertEqual(receipt.entries, ())
        self.assertEqual(
            receipt.inventory_baseline_digest, evidence_digest(inventory.model_dump(mode="json"))
        )
        self.assertEqual(
            self.store.read_environment_inventory(context_name="cm", instance_name="testing"),
            corrected,
        )
        if schedule is not None:
            self.assertEqual(self.input_schedule, schedule)
            self.assertEqual(
                cast(Any, api.upsert_dokploy_application_schedule).call_args_list,
                previous_schedule_calls,
            )
        if completion_failure:
            (held_original,) = self.store.list_held_provider_target_reservations()
            original_key = held_original.idempotency_key
        else:
            original_key = cast(str, refused.last_plan["deploy_idempotency_key"])
        completed = self.store.read_idempotency_record(
            scope=reconcile_reservation_scope("site"),
            route_path=GENERIC_WEB_DEPLOY_ROUTE,
            idempotency_key=original_key,
        )
        assert completed is not None
        if completion_failure:
            self.assertFalse(testing_attempt_was_settled(completed))
            self.assertEqual(
                [row.record_id for row in self.store.list_held_provider_target_reservations()],
                [completed.record_id],
            )
            for _ in range(2):
                self.request()
                repeated = self.run_once()
                self.assertNotEqual(repeated.state, "done")
                self.assertEqual(
                    [row.record_id for row in self.store.list_held_provider_target_reservations()],
                    [completed.record_id],
                )
                self.assertEqual(triggered, previous_triggers)
                self.assertEqual(payload, previous_payload)
                self.assertEqual(
                    cast(Any, api.dokploy_request).call_args_list, previous_provider_calls
                )
                self.assertEqual(
                    execution_receipt(self.store.read_deployment_record(original.record_id)),
                    receipt,
                )
                self.assertEqual(
                    self.store.read_environment_inventory(
                        context_name="cm", instance_name="testing"
                    ),
                    corrected,
                )
            return
        self.assertTrue(testing_attempt_was_settled(completed))
        result = GenericWebDeployResult.model_validate(completed.response_payload["result"])
        self.assertEqual(result.deployment_record_id, original.record_id)
        self.assertEqual(result.deploy_status, "fail")
        self.assertFalse(result.provider_effect_attempted)
        self.assertFalse(self.store.list_held_provider_target_reservations())
        self.request()
        if unknown_successor:
            with patch.object(
                api,
                "wait_for_target_deployment",
                side_effect=ProviderMutationUnknownError("Lost successor dispatch outcome"),
            ):
                self.assertEqual(self.run_once().state, "failed")
            (held,) = self.store.list_held_provider_target_reservations()
            self.assertNotEqual(held.idempotency_key, completed.idempotency_key)
            self.assertTrue(held.provider_effect_started_at)
            self.assertEqual(len(triggered), len(previous_triggers) + 1)
            unknown_payload = dict(payload)
            unknown_calls = list(cast(Any, api.dokploy_request).call_args_list)
            later_correction = corrected.model_copy(
                update={
                    "post_deploy_update": corrected.post_deploy_update.model_copy(
                        update={"detail": "Another independent evidence correction"}
                    )
                }
            )
            self.store.write_environment_inventory(later_correction)
            self.request()
            with patch.object(
                self.application_inputs_provider,
                "observe_artifact_deploy",
                return_value=GenericWebProviderDeploymentObservation(outcome="unknown"),
            ):
                self.assertNotEqual(self.run_once().state, "done")
            (retained,) = self.store.list_held_provider_target_reservations()
            self.assertEqual(retained.record_id, held.record_id)
            self.assertEqual(retained.idempotency_key, held.idempotency_key)
            self.assertEqual(retained.provider_effect_phase, held.provider_effect_phase)
            self.assertEqual(len(triggered), len(previous_triggers) + 1)
            self.assertEqual(payload, unknown_payload)
            self.assertEqual(cast(Any, api.dokploy_request).call_args_list, unknown_calls)
            self.assertEqual(
                execution_receipt(self.store.read_deployment_record(original.record_id)), receipt
            )
            return
        successor = self.run_once()
        self.assertEqual(successor.state, "done", successor.last_error)
        self.assertNotEqual(
            successor.last_plan["deploy_idempotency_key"],
            refused.last_plan["deploy_idempotency_key"],
        )
        self.assertEqual(len(triggered), len(previous_triggers) + 1)
        successor_record = self.store.read_deployment_record(
            cast(str, successor.last_plan["deployment_record_id"])
        )
        successor_receipt = execution_receipt(successor_record)
        assert successor_receipt is not None
        self.assertEqual(
            successor_receipt.inventory_baseline_digest,
            evidence_digest(corrected.model_dump(mode="json")),
        )
        self.assertEqual(
            execution_receipt(self.store.read_deployment_record(original.record_id)), receipt
        )
        self.assertEqual(
            self.store.read_environment_inventory(
                context_name="cm", instance_name="testing"
            ).source_git_ref,
            DEPLOYABLE,
        )
        successful_provider_calls = list(cast(Any, api.dokploy_request).call_args_list)
        successful_payload = dict(payload)
        self.request()
        self.assertEqual(self.reconcile()["reason"], "already_deployed")
        self.assertEqual(len(triggered), len(previous_triggers) + 1)
        self.assertEqual(payload, successful_payload)
        self.assertEqual(cast(Any, api.dokploy_request).call_args_list, successful_provider_calls)

    def prepare_application_runtime_test(self) -> list[str]:
        _payload, triggered = self.application_input_provider()
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="cm",
                instance="testing",
                env={"APP_SETTING": "fixture"},
                updated_at="2026-10-19T00:00:00Z",
            )
        )
        self.github.add_run(20, DEPLOYABLE)
        return triggered

    def rolled_back_application_runtime(self) -> ApplicationRuntimeObservation:
        return replace(
            self.application_runtime(),
            rollout_state="rollback_completed",
            service_image=f"{IMAGE_REPOSITORY}:sha-{OLDER}",
            service_deployment_record_id="old-deployment",
            running_container_images=(),
            running_container_deployment_record_ids=(),
            running_container_health=(),
            pending_task_count=1,
        )

    def test_provider_job_success_after_swarm_rollback_never_credits_testing(self) -> None:
        triggered = self.prepare_application_runtime_test()
        self.request()
        with patch.object(
            self.application_inputs_provider,
            "observe_testing_application_runtime",
            side_effect=lambda **_: self.rolled_back_application_runtime(),
        ):
            first = self.run_once()
            self.request()
            repeated = self.run_once()
        self.assertEqual(first.last_plan["deploy_status"], "fail")
        self.assertEqual(repeated.last_plan["deploy_operation_status"], "replayed")
        self.assertEqual(len(triggered), 1)
        records = self.store.list_deployment_records(context_name="cm", instance_name="testing")
        self.assertTrue(records)
        self.assertTrue(
            all(
                record.deploy.status == "fail" and record.runtime_identity is None
                for record in records
            )
        )
        with self.assertRaises(FileNotFoundError):
            self.store.read_environment_inventory(context_name="cm", instance_name="testing")

    def test_unreadable_rollout_at_deadline_retains_original_without_delivery_credit(self) -> None:
        triggered = self.prepare_application_runtime_test()
        with (
            patch.object(
                self.application_inputs_provider,
                "observe_testing_application_runtime",
                side_effect=lambda **_: replace(
                    self.application_runtime(),
                    task_evidence_complete=False,
                    running_container_images=(),
                    running_container_deployment_record_ids=(),
                    running_container_health=(),
                ),
            ),
            patch("control_plane.workflows.generic_web_deploy.monotonic", return_value=0),
            patch(
                "control_plane.workflows.generic_web_deploy_provider.monotonic", return_value=1e30
            ),
        ):
            self.request()
            original = self.run_once()
        self.assertEqual(original.state, "failed")
        (held,) = self.store.list_held_provider_target_reservations()
        self.assertEqual(len(triggered), 1)
        with self.assertRaises(FileNotFoundError):
            self.store.read_environment_inventory(context_name="cm", instance_name="testing")
        self.request()
        repeated = self.run_once()
        self.assertEqual(repeated.state, "failed")
        (retained,) = self.store.list_held_provider_target_reservations()
        self.assertEqual(retained.record_id, held.record_id)
        self.assertEqual(len(triggered), 1)
        with self.assertRaises(FileNotFoundError):
            self.store.read_environment_inventory(context_name="cm", instance_name="testing")

    def test_application_without_managed_inputs_repairs_runtime_drift_once(self) -> None:
        from control_plane.generic_web_testing_inputs import testing_input_authority

        _payload, triggered = self.application_input_provider()
        profile = self.store.read_product_profile_record("site")
        lane = next(lane for lane in profile.lanes if lane.instance == "testing")
        self.assertFalse(testing_input_authority(self.store, profile, lane))
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        self.reconcile()

        def runtime(**_kwargs: object) -> ApplicationRuntimeObservation:
            return (
                replace(self.rolled_back_application_runtime(), task_evidence_complete=False)
                if len(triggered) == 1
                else self.application_runtime()
            )

        with patch.object(
            self.application_inputs_provider,
            "observe_testing_application_runtime",
            side_effect=runtime,
        ):
            self.request()
            repaired = self.reconcile()
            self.request()
            unchanged = self.reconcile()
        self.assertEqual(repaired["reason"], "testing_runtime_drift")
        self.assertEqual(unchanged["reason"], "already_deployed")
        self.assertEqual(len(triggered), 2)

    def test_actual_runtime_drift_repairs_once_without_inventory_rewrite(self) -> None:
        triggered = self.prepare_application_runtime_test()
        self.request()
        initial = self.reconcile()
        old_inventory = self.store.read_environment_inventory(
            context_name="cm", instance_name="testing"
        )

        def runtime(**_kwargs: object) -> ApplicationRuntimeObservation:
            return (
                self.rolled_back_application_runtime()
                if len(triggered) == 1
                else self.application_runtime()
            )

        with patch.object(
            self.application_inputs_provider,
            "observe_testing_application_runtime",
            side_effect=runtime,
        ):
            self.request()
            repaired = self.reconcile()
            self.request()
            repeated = self.reconcile()
        self.assertEqual(repaired["reason"], "testing_runtime_drift")
        self.assertNotEqual(initial["deploy_idempotency_key"], repaired["deploy_idempotency_key"])
        self.assertNotEqual(old_inventory.deployment_record_id, repaired["deployment_record_id"])
        self.assertEqual(repeated["reason"], "already_deployed")
        self.assertEqual(len(triggered), 2)

        with patch.object(
            self.application_inputs_provider,
            "observe_testing_application_runtime",
            side_effect=lambda **_: (
                self.rolled_back_application_runtime()
                if len(triggered) == 2
                else self.application_runtime()
            ),
        ):
            self.request()
            later_repair = self.reconcile()
        self.assertEqual(later_repair["reason"], "testing_runtime_drift")
        self.assertNotEqual(
            later_repair["deploy_idempotency_key"], repaired["deploy_idempotency_key"]
        )
        self.assertEqual(len(triggered), 3)

    def test_pending_runtime_before_skip_waits_without_new_deployment(self) -> None:
        triggered = self.prepare_application_runtime_test()
        self.request()
        self.reconcile()
        runtime = replace(self.application_runtime(), rollout_state="updating")
        with patch.object(
            self.application_inputs_provider,
            "observe_testing_application_runtime",
            return_value=runtime,
        ):
            self.request()
            pending = self.run_once()
        self.assertEqual(pending.last_plan["reason"], "testing_runtime_unverified")
        self.assertEqual(pending.state, "done")
        self.assertEqual(len(triggered), 1)

    def test_failed_runtime_repair_replays_even_when_its_service_spec_remains(self) -> None:
        self.assert_failed_runtime_repair_replays("completed")

    def test_paused_failed_runtime_repair_replays_without_another_deploy(self) -> None:
        self.assert_failed_runtime_repair_replays("paused")

    def test_paused_rollback_repair_replays_without_another_deploy(self) -> None:
        self.assert_failed_runtime_repair_replays("rollback_paused")

    def assert_failed_runtime_repair_replays(self, rollout_state: str) -> None:
        triggered = self.prepare_application_runtime_test()
        self.request()
        self.reconcile()
        baseline = self.store.read_environment_inventory(context_name="cm", instance_name="testing")

        def runtime(**_kwargs: object) -> ApplicationRuntimeObservation:
            if len(triggered) == 1:
                return self.rolled_back_application_runtime()
            current = self.application_runtime()
            return replace(
                current,
                rollout_state=rollout_state,
                running_container_images=(),
                running_container_deployment_record_ids=(),
                running_container_health=(),
                tasks=(
                    ApplicationTaskObservation(
                        image=current.running_container_images[0][0],
                        deployment_record_id=current.service_deployment_record_id,
                        state="failed",
                    ),
                ),
            )

        with (
            patch.object(
                self.application_inputs_provider,
                "observe_testing_application_runtime",
                side_effect=runtime,
            ),
            patch(
                "control_plane.workflows.generic_web_deploy_provider.monotonic",
                return_value=1e30,
            ),
        ):
            self.request()
            failed = self.run_once()
            for _ in range(2):
                self.request()
                repeated = self.run_once()
                self.assertEqual(repeated.last_plan["deploy_operation_status"], "replayed")
                self.assertEqual(
                    repeated.last_plan["deploy_idempotency_key"],
                    failed.last_plan["deploy_idempotency_key"],
                )
        self.assertEqual(failed.last_plan["deploy_status"], "fail")
        self.assertEqual(len(triggered), 2)
        self.assertEqual(
            self.store.read_environment_inventory(context_name="cm", instance_name="testing"),
            baseline,
        )

    def test_known_application_without_target_configuration_cannot_skip_runtime_proof(self) -> None:
        triggered = self.prepare_application_runtime_test()
        self.request()
        self.reconcile()
        target = self.store.read_dokploy_target_record(context_name="cm", instance_name="testing")
        self.store.delete_dokploy_target_record(expected_record=target)
        with patch.object(
            self.application_inputs_provider, "observe_testing_application_runtime"
        ) as observe:
            self.request()
            pending = self.run_once()
        self.assertEqual(pending.last_plan["reason"], "testing_runtime_unverified")
        self.assertEqual(pending.last_plan["action"], "wait")
        self.assertEqual(pending.state, "done")
        observe.assert_not_called()
        self.assertEqual(len(triggered), 1)

    def test_interrupted_job_success_cannot_resurrect_rolled_back_runtime(self) -> None:
        from control_plane.provider_operations import ProviderMutationUnknownError

        triggered = self.prepare_application_runtime_test()
        self.request()
        with patch(
            "control_plane.dokploy.api.wait_for_target_deployment",
            side_effect=ProviderMutationUnknownError("lost reply"),
        ):
            interrupted = self.run_once()
        self.assertEqual(interrupted.state, "failed")
        self.assertTrue(self.store.list_held_provider_target_reservations())
        with patch.object(
            self.application_inputs_provider,
            "observe_testing_application_runtime",
            side_effect=lambda **_: self.rolled_back_application_runtime(),
        ):
            self.request()
            recovered = self.run_once()
        self.assertEqual(recovered.state, "failed")
        self.assertTrue(self.store.list_held_provider_target_reservations())
        self.assertEqual(len(triggered), 1)
        with self.assertRaises(FileNotFoundError):
            self.store.read_environment_inventory(context_name="cm", instance_name="testing")

    def test_same_image_missing_managed_inputs_is_repaired_once_and_rotation_converges(
        self,
    ) -> None:
        from control_plane.generic_web_testing_inputs import DELIVERY_RECEIPT_KEY
        from control_plane.dokploy.api import parse_dokploy_env_text

        payload, triggered = self.application_input_provider()
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        legacy = self.reconcile()
        self.assertNotIn(
            DELIVERY_RECEIPT_KEY,
            self.store.read_deployment_record(str(legacy["deployment_record_id"])).runtime_source,
        )
        settings = RuntimeEnvironmentRecord(
            scope="instance",
            context="cm",
            instance="testing",
            env={"APP_SETTING": "first"},
            updated_at="2026-10-19T00:00:00Z",
        )
        for value in ("first", "second"):
            self.store.write_runtime_environment_record(
                settings.model_copy(update={"env": {"APP_SETTING": value}})
            )
            self.request()
            repaired = self.reconcile()
            self.assertEqual(repaired["action"], "deploy")
            self.assertEqual(parse_dokploy_env_text(str(payload["env"]))["APP_SETTING"], value)
            record = self.store.read_deployment_record(str(repaired["deployment_record_id"]))
            self.assertTrue(record.runtime_source[DELIVERY_RECEIPT_KEY])
            count = len(triggered)
            self.request()
            self.assertEqual(self.reconcile()["reason"], "already_deployed")
            self.assertEqual(len(triggered), count)
        self.assertEqual(legacy["desired_image_digest"], repaired["desired_image_digest"])

    def test_application_testing_delivers_multiline_literal_inputs_then_converges(self) -> None:
        from control_plane.generic_web_testing_inputs import DELIVERY_RECEIPT_KEY
        from control_plane.dokploy.api import parse_dokploy_env_text

        payload, triggered = self.application_input_provider()
        payload["env"] = "PROVIDER_ONLY='keep#literal\nsecond line'"
        toml = "\n".join(
            (
                "[server]",
                'host = "127.0.0.1"',
                "port = 8099",
                "",
                "[display]",
                'label = "Synthetic fixture"',
                'theme = "plain"',
                "",
                "[features]",
                "enabled = true",
                "count = 2",
                'mode = "testing"',
            )
        )
        desired = {
            "APP_RUNTIME_CONFIG": toml,
            "APP_SETTING": "first#second",
            "APP_QUOTED": '"literal"',
            "APP_DOLLARS": "$NAME ${NAME} $$ $(literal)",
        }
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="cm",
                instance="testing",
                env={key: value for key, value in desired.items()},
                updated_at="2026-10-19T00:00:00Z",
            )
        )
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        deployed = self.reconcile()
        delivered = parse_dokploy_env_text(str(payload["env"]))
        for key, value in desired.items():
            self.assertEqual(delivered[key], value)
        self.assertEqual(delivered["PROVIDER_ONLY"], "keep#literal\nsecond line")
        self.assertNotIn("host", delivered)
        record = self.store.read_deployment_record(str(deployed["deployment_record_id"]))
        self.assertTrue(record.runtime_source[DELIVERY_RECEIPT_KEY])
        self.assertEqual(len(triggered), 1)
        self.request()
        self.assertEqual(self.reconcile()["reason"], "already_deployed")
        self.assertEqual(len(triggered), 1)

    def test_unsupported_literal_input_refuses_before_effects_and_unchanged_retry_runs(
        self,
    ) -> None:
        from control_plane.workflows.dokploy_deploy import (
            require_dokploy_testing_environment_representable,
        )

        payload, triggered = self.application_input_provider()
        original_schedule = self.application_input_schedule()
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="cm",
                instance="testing",
                env={"APP_SETTING": "${{PROVIDER_ONLY}}"},
                updated_at="2026-10-19T00:00:00Z",
            )
        )
        self.github.add_run(20, DEPLOYABLE)
        keys = []
        for _ in range(2):
            self.request()
            with (
                patch(
                    "control_plane.workflows.generic_web_deploy_provider."
                    "require_dokploy_testing_environment_representable",
                    wraps=require_dokploy_testing_environment_representable,
                ) as prepare,
                patch("control_plane.dokploy.api.update_dokploy_target_env") as env_write,
                patch("control_plane.dokploy.api.dokploy_request") as image_write,
                patch("control_plane.dokploy.api.upsert_dokploy_application_schedule") as schedule,
            ):
                result = self.run_once()
            self.assertEqual(result.state, "failed")
            self.assertTrue(prepare.called)
            env_write.assert_not_called()
            image_write.assert_not_called()
            schedule.assert_not_called()
            keys.append(result.last_plan["deploy_idempotency_key"])
        self.assertEqual(keys[0], keys[1])
        self.assertFalse(triggered)
        self.assertFalse(self.store.list_held_provider_target_reservations())
        self.assertEqual(self.input_schedule, original_schedule)
        self.assertEqual(payload["env"], "PROVIDER_ONLY=keep")

    def test_same_image_input_repair_obeys_testing_hold(self) -> None:
        _payload, triggered = self.application_input_provider()
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        self.reconcile()
        self.hold_testing()
        with patch.object(
            self.application_inputs_provider,
            "observe_testing_application_runtime",
            wraps=self.application_runtime,
        ) as observe:
            self.request()
            unchanged = self.reconcile()
        self.assertEqual(unchanged["reason"], "already_deployed")
        observe.assert_called_once()
        self.assertEqual(len(triggered), 1)
        with patch.object(
            self.application_inputs_provider,
            "observe_testing_application_runtime",
            side_effect=lambda **_: self.rolled_back_application_runtime(),
        ):
            self.request()
            drift_held = self.reconcile()
        self.assertEqual(drift_held["reason"], "staff_testing")
        self.assertEqual(len(triggered), 1)
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="cm",
                instance="testing",
                env={"APP_SETTING": "new"},
                updated_at="2026-10-19T00:00:00Z",
            )
        )
        target = self.store.read_dokploy_target_record(context_name="cm", instance_name="testing")
        self.hold_testing()
        held_target = self.store.read_dokploy_target_record(
            context_name="cm", instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            target.model_copy(update={"policies": held_target.policies})
        )
        self.request()
        held = self.reconcile()
        self.assertEqual(held["reason"], "staff_testing")
        self.assertEqual(len(triggered), 1)

    def test_changed_inputs_cannot_bypass_unknown_original_testing_deploy(self) -> None:
        from control_plane.generic_web_testing_inputs import DELIVERY_RECEIPT_KEY
        from control_plane.provider_operations import ProviderMutationUnknownError

        _payload, triggered = self.application_input_provider()
        settings = RuntimeEnvironmentRecord(
            scope="instance",
            context="cm",
            instance="testing",
            env={"APP_SETTING": "first"},
            updated_at="2026-10-19T00:00:00Z",
        )
        self.store.write_runtime_environment_record(settings)
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        with patch(
            "control_plane.dokploy.api.wait_for_target_deployment",
            side_effect=ProviderMutationUnknownError("lost provider outcome"),
        ):
            interrupted = self.run_once()
        self.assertEqual(interrupted.state, "failed")
        held = self.store.list_held_provider_target_reservations()
        self.assertTrue(held)
        pending = self.store.list_deployment_records(context_name="cm", instance_name="testing")[0]
        original_digest = pending.runtime_source[DELIVERY_RECEIPT_KEY]
        self.store.write_runtime_environment_record(
            settings.model_copy(update={"env": {"APP_SETTING": "second"}})
        )
        self.request()
        with patch.object(
            self.application_inputs_provider,
            "observe_artifact_deploy",
            return_value=GenericWebProviderDeploymentObservation(outcome="unknown"),
        ):
            refused = self.run_once()
        self.assertEqual(refused.state, "failed")
        self.assertEqual(len(triggered), 1)
        self.assertEqual(
            self.store.read_deployment_record(pending.record_id).runtime_source[
                DELIVERY_RECEIPT_KEY
            ],
            original_digest,
        )
        self.assertEqual(
            tuple(item.record_id for item in self.store.list_held_provider_target_reservations()),
            tuple(item.record_id for item in held),
        )

    def application_input_schedule(self) -> dict[str, Any]:
        from control_plane.contracts.generic_web_extensions import (
            GenericWebApplicationSchedule,
            GenericWebLaneExtensions,
        )
        from control_plane.workflows.generic_web_application_lifecycle import (
            DokployApplicationLifecycle,
            schedule_payload,
        )

        profile = self.store.read_product_profile_record("site")
        schedule = GenericWebApplicationSchedule(
            name="cleanup", command="./cleanup", cron_expression="0 * * * *"
        )
        lane = profile.lanes[0].model_copy(
            update={"generic_web_extensions": GenericWebLaneExtensions(schedules=(schedule,))}
        )
        self.store.write_product_profile_record(
            profile.model_copy(update={"lanes": (lane, *profile.lanes[1:])})
        )
        original = schedule_payload(schedule, "target-123", enabled=True)
        self.input_schedule = dict(original)

        def lifecycle(**kwargs: Any) -> DokployApplicationLifecycle:
            return DokployApplicationLifecycle(
                host="fixture",
                token="unused",
                application_id="target-123",
                snapshot=kwargs["snapshot"],
                checkpoint=kwargs["checkpoint"],
                effect_started=kwargs["effect_started"],
            )

        def upsert(**kwargs: Any) -> Any:
            guard = kwargs.get("provider_mutation_guard")
            with guard() if callable(guard) else nullcontext():
                self.input_schedule = dict(kwargs["schedule_payload"])
                return {**self.input_schedule, "scheduleId": "cleanup"}

        for patcher in (
            patch.object(
                self.application_inputs_provider, "application_lifecycle", side_effect=lifecycle
            ),
            patch(
                "control_plane.dokploy.api.read_dokploy_application_schedule",
                side_effect=lambda **_: dict(self.input_schedule),
            ),
            patch(
                "control_plane.dokploy.api.upsert_dokploy_application_schedule", side_effect=upsert
            ),
            patch("control_plane.dokploy.api.run_dokploy_schedule"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return original

    def prepare_interrupted_testing_inputs(
        self,
        *,
        lifecycle: bool = False,
        interrupt: bool = True,
        phase: str = "runtime_inputs_verified",
    ) -> tuple[RuntimeEnvironmentRecord, Any, Any, list[str]]:
        from control_plane.provider_operations import ProviderMutationUnknownError

        _payload, triggered = self.application_input_provider()
        original_schedule = self.application_input_schedule() if lifecycle else None
        settings = RuntimeEnvironmentRecord(
            scope="instance",
            context="cm",
            instance="testing",
            env={"APP_SETTING": "first"},
            updated_at="2026-10-19T00:00:00Z",
        )
        self.store.write_runtime_environment_record(settings)
        execute = self.application_inputs_provider.execute_artifact_deploy

        def apply(**kwargs: Any) -> Any:
            original = kwargs["before_provider_mutation"]

            def checkpoint(current_phase: str) -> None:
                original(current_phase)
                if current_phase == phase:
                    if interrupt:
                        raise ProviderMutationUnknownError("interrupted before image mutation")
                    self.store.write_runtime_environment_record(
                        settings.model_copy(update={"env": {"APP_SETTING": "second"}})
                    )

            kwargs["before_provider_mutation"] = checkpoint
            return execute(**kwargs)

        self.github.add_run(20, DEPLOYABLE)
        self.request()
        with patch.object(
            self.application_inputs_provider, "execute_artifact_deploy", side_effect=apply
        ):
            self.assertEqual(self.run_once().state, "failed")
        self.assertFalse(triggered)
        (reservation,) = self.store.list_held_provider_target_reservations()
        return settings, reservation, original_schedule, triggered

    def recover_testing_inputs(self, original: Any) -> dict[str, object]:
        from control_plane.provider_operations import (
            build_provider_operation_key,
            provider_operation_title,
        )

        old_title = provider_operation_title(
            build_provider_operation_key(
                scope=original.scope,
                route_path=original.route_path,
                idempotency_key=original.idempotency_key,
                request_fingerprint=original.request_fingerprint,
                reconciliation_key=original.reconciliation_key,
            )
        )
        success = GenericWebProviderDeploymentObservation(
            outcome="present",
            deployment_status="success",
            deployment_id="fresh-input-deploy",
            started_at="2026-10-19T00:00:00Z",
            finished_at="2026-10-19T00:01:00Z",
        )
        self.request()
        with patch.object(
            self.application_inputs_provider,
            "observe_artifact_deploy",
            side_effect=lambda **kwargs: (
                GenericWebProviderDeploymentObservation(outcome="absent")
                if kwargs["deployment_title"] == old_title
                else success
            ),
        ):
            return self.reconcile()

    def assert_obsolete_testing_input_settlement(
        self, *, lifecycle: str = "", newer_image: bool = False, changed_inputs: bool = True
    ) -> None:
        from control_plane.workflows.generic_web_application_snapshot import (
            recorded_application_lifecycle,
        )
        from control_plane.generic_web_testing_settlement import testing_attempt_was_settled

        settings, original, schedule, triggered = self.prepare_interrupted_testing_inputs(
            lifecycle=bool(lifecycle), interrupt=lifecycle != "restored"
        )
        before = self.store.list_deployment_records(context_name="cm", instance_name="testing")[0]
        baseline = recorded_application_lifecycle(before)
        if changed_inputs:
            self.store.write_runtime_environment_record(
                settings.model_copy(update={"env": {"APP_SETTING": "second"}})
            )
        if newer_image:
            self.github.add_run(30, NEWEST)
        result = self.recover_testing_inputs(original)
        self.assertEqual(result["action"], "deploy")
        self.assertFalse(self.store.list_held_provider_target_reservations())
        self.assertEqual(len(triggered), 1)
        completed = self.store.read_idempotency_record(
            scope=original.scope,
            route_path=original.route_path,
            idempotency_key=original.idempotency_key,
        )
        assert completed is not None
        self.assertTrue(testing_attempt_was_settled(completed))
        retained = self.store.read_deployment_record(before.record_id)
        self.assertEqual(retained.deploy.status, "fail")
        self.assertEqual(retained.runtime_source, before.runtime_source)
        if baseline is not None:
            restored = recorded_application_lifecycle(retained)
            assert restored is not None
            self.assertEqual(restored.originals, baseline.originals)
            self.assertEqual(restored.state, "restored")
            self.assertEqual(self.input_schedule, schedule)
        deployed = self.store.read_deployment_record(str(result["deployment_record_id"]))
        self.assertNotEqual(deployed.record_id, before.record_id)
        self.assertEqual(deployed.source_git_ref, NEWEST if newer_image else DEPLOYABLE)
        self.request()
        self.assertEqual(self.reconcile()["reason"], "already_deployed")
        self.assertEqual(len(triggered), 1)

    def test_changed_inputs_settle_environment_only_attempt_before_new_delivery(self) -> None:
        self.assert_obsolete_testing_input_settlement()

    def test_changed_inputs_settle_prepared_lifecycle_with_original_baseline(self) -> None:
        self.assert_obsolete_testing_input_settlement(lifecycle="prepared")

    def test_changed_inputs_settle_restored_lifecycle_with_original_baseline(self) -> None:
        self.assert_obsolete_testing_input_settlement(lifecycle="restored")

    def prepare_real_inherited_testing_hold(
        self, *, refuse: bool
    ) -> tuple[RuntimeEnvironmentRecord, Any, DeploymentRecord, Any, list[str]]:
        from control_plane.generic_web_testing_inputs import TestingInputSafetyRefused
        from control_plane.provider_operations import (
            ProviderMutationUnknownError,
            build_provider_operation_key,
        )
        from control_plane.workflows.generic_web_application_snapshot import (
            generic_web_provider_deployment_record_id,
            recorded_application_lifecycle,
        )

        _payload, triggered = self.application_input_provider()
        schedule = self.application_input_schedule()
        settings = RuntimeEnvironmentRecord(
            scope="instance",
            context="cm",
            instance="testing",
            env={"APP_SETTING": "first"},
            updated_at="2026-10-19T00:00:00Z",
        )
        self.store.write_runtime_environment_record(settings)
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        with patch(
            "control_plane.dokploy.api.run_dokploy_schedule",
            side_effect=click.ClickException("qualification failed"),
        ):
            self.assertEqual(self.run_once().state, "failed")
        (prior,) = self.store.list_deployment_records(context_name="cm", instance_name="testing")
        held = recorded_application_lifecycle(prior)
        assert held is not None
        self.assertEqual(prior.deploy.status, "pass")
        self.assertEqual(held.state, "held")
        self.assertFalse(self.input_schedule["enabled"])
        self.assertEqual(len(triggered), 1)
        execute = self.application_inputs_provider.execute_artifact_deploy

        def interrupt(**kwargs: Any) -> Any:
            original = kwargs["before_provider_mutation"]

            def checkpoint(phase: str) -> None:
                original(phase)
                if phase == "runtime_inputs_verified":
                    if refuse:
                        raise TestingInputSafetyRefused("transient pre-image refusal")
                    raise ProviderMutationUnknownError("interrupted before image mutation")

            kwargs["before_provider_mutation"] = checkpoint
            return execute(**kwargs)

        self.github.add_run(30, NEWEST)
        self.request()
        with patch.object(
            self.application_inputs_provider, "execute_artifact_deploy", side_effect=interrupt
        ):
            self.assertEqual(self.run_once().state, "failed")
        (reservation,) = self.store.list_held_provider_target_reservations()
        operation_key = build_provider_operation_key(
            scope=reservation.scope,
            route_path=reservation.route_path,
            idempotency_key=reservation.idempotency_key,
            request_fingerprint=reservation.request_fingerprint,
            reconciliation_key=reservation.reconciliation_key,
        )
        current = self.store.read_deployment_record(
            generic_web_provider_deployment_record_id(
                operation_key, context="cm", instance="testing"
            )
        )
        receipt = recorded_application_lifecycle(current)
        assert receipt is not None
        self.assertTrue(receipt.resume_from_hold)
        self.assertEqual(receipt.state, "held" if refuse else "prepared")
        self.assertEqual(receipt.originals["cleanup"], schedule)
        self.assertFalse(self.input_schedule["enabled"])
        self.assertEqual(len(triggered), 1)
        return settings, reservation, current, receipt, triggered

    def assert_inherited_testing_hold_survives_recovery(
        self, *, refuse: bool, settle: bool
    ) -> None:
        from control_plane.generic_web_testing_inputs import TestingInputSafetyRefused
        from control_plane.testing_event_deploy_recovery import settle_obsolete_testing_event_deploy
        from control_plane.workflows.generic_web_application_snapshot import (
            recorded_application_lifecycle,
        )

        settings, reservation, before, baseline, triggered = (
            self.prepare_real_inherited_testing_hold(refuse=refuse)
        )
        with patch.object(
            self.application_inputs_provider,
            "observe_artifact_deploy",
            return_value=GenericWebProviderDeploymentObservation(outcome="absent"),
        ):
            if settle:
                self.store.write_runtime_environment_record(
                    settings.model_copy(update={"env": {"APP_SETTING": "second"}})
                )
                result = settle_obsolete_testing_event_deploy(
                    store=self.store,
                    reservation=reservation,
                    profile=self.store.read_product_profile_record("site"),
                    context="cm",
                    current_target_key=reservation.provider_target_key,
                    desired_fingerprint=reservation.request_fingerprint,
                    provider=self.application_inputs_provider,
                    root=self.root,
                    trace_id="inherited-hold-settlement",
                    check_execution_lock=lambda: None,
                )
                assert result is not None
                self.assertEqual(result.status, "completed")
                self.assertFalse(self.store.list_held_provider_target_reservations())
            else:
                self.request()
                with patch.object(
                    self.application_inputs_provider,
                    "execute_artifact_deploy",
                    side_effect=TestingInputSafetyRefused("another pre-image refusal"),
                ):
                    self.assertEqual(self.run_once().state, "failed")
        retained = recorded_application_lifecycle(
            self.store.read_deployment_record(before.record_id)
        )
        assert retained is not None
        self.assertTrue(retained.resume_from_hold)
        self.assertEqual(retained.state, "held")
        self.assertEqual(retained.originals, baseline.originals)
        self.assertFalse(self.input_schedule["enabled"])
        self.assertEqual(len(triggered), 1)

    def test_held_pre_image_settlement_preserves_prior_unqualified_image_hold(self) -> None:
        self.assert_inherited_testing_hold_survives_recovery(refuse=True, settle=True)

    def test_prepared_pre_image_settlement_preserves_prior_unqualified_image_hold(self) -> None:
        self.assert_inherited_testing_hold_survives_recovery(refuse=False, settle=True)

    def test_held_pre_image_retry_failure_preserves_inherited_hold(self) -> None:
        self.assert_inherited_testing_hold_survives_recovery(refuse=True, settle=False)

    def test_prepared_pre_image_retry_failure_preserves_inherited_hold(self) -> None:
        self.assert_inherited_testing_hold_survives_recovery(refuse=False, settle=False)

    def test_new_image_and_inputs_settle_prepared_original_attempt(self) -> None:
        self.assert_obsolete_testing_input_settlement(lifecycle="prepared", newer_image=True)

    def test_ambiguous_image_update_stays_fenced_after_inputs_change(self) -> None:
        settings, original, _schedule, triggered = self.prepare_interrupted_testing_inputs(
            lifecycle=True, phase="target_update"
        )
        self.store.write_runtime_environment_record(
            settings.model_copy(update={"env": {"APP_SETTING": "second"}})
        )
        self.request()
        with patch.object(
            self.application_inputs_provider,
            "observe_artifact_deploy",
            return_value=GenericWebProviderDeploymentObservation(outcome="absent"),
        ):
            self.assertEqual(self.run_once().state, "failed")
        self.assertFalse(triggered)
        self.assertEqual(self.store.list_held_provider_target_reservations(), (original,))
        self.assertFalse(self.input_schedule["enabled"])

    def test_input_edit_after_image_save_finishes_captured_inputs_then_repairs_once(self) -> None:
        from control_plane.generic_web_testing_inputs import (
            DELIVERY_RECEIPT_KEY,
            testing_input_authority,
        )
        from control_plane.dokploy.api import parse_dokploy_env_text

        payload, triggered = self.application_input_provider()
        settings = RuntimeEnvironmentRecord(
            scope="instance",
            context="cm",
            instance="testing",
            env={"APP_SETTING": "first"},
            updated_at="2026-10-19T00:00:00Z",
        )
        self.store.write_runtime_environment_record(settings)
        profile = self.store.read_product_profile_record("site")
        captured_digest = testing_input_authority(self.store, profile, profile.lanes[0])

        def save(**kwargs: Any) -> dict[str, object]:
            if kwargs["path"] == "/api/application.saveDockerProvider":
                self.store.write_runtime_environment_record(
                    settings.model_copy(update={"env": {"APP_SETTING": "second"}})
                )
            return {}

        self.github.add_run(20, DEPLOYABLE)
        self.request()
        with patch("control_plane.dokploy.api.dokploy_request", side_effect=save):
            first = self.run_once()
        self.assertEqual(first.state, "failed")
        self.assertEqual(len(triggered), 1)
        record = self.settle_terminal_testing_original()
        self.assertEqual(record.runtime_source[DELIVERY_RECEIPT_KEY], captured_digest)
        self.assertEqual(parse_dokploy_env_text(str(payload["env"]))["APP_SETTING"], "first")
        self.assertFalse(self.store.list_held_provider_target_reservations())
        self.request()
        repaired = self.reconcile()
        self.assertEqual(len(triggered), 2)
        self.assertEqual(parse_dokploy_env_text(str(payload["env"]))["APP_SETTING"], "second")
        current = self.store.read_deployment_record(str(repaired["deployment_record_id"]))
        self.assertEqual(
            current.runtime_source[DELIVERY_RECEIPT_KEY],
            testing_input_authority(self.store, profile, profile.lanes[0]),
        )
        self.assertNotEqual(current.runtime_source[DELIVERY_RECEIPT_KEY], captured_digest)
        self.request()
        self.assertEqual(self.reconcile()["reason"], "already_deployed")
        self.assertEqual(len(triggered), 2)

    def assert_post_image_safety_stops_dispatch(
        self, change: str, *, cleanup_failure: bool = False, publication_failure: bool = False
    ) -> tuple[RuntimeEnvironmentRecord, Any, dict[str, Any], list[str]]:
        from control_plane.generic_web_testing_inputs import TESTING_DISPATCH_REFUSED_PHASE
        from control_plane.dokploy import api
        from tests.test_generic_web_deploy import (
            _runtime_secret_record,
            _runtime_secret_version,
            _runtime_secret_binding,
        )

        _payload, triggered = self.application_input_provider()
        schedule = self.application_input_schedule()
        settings = RuntimeEnvironmentRecord(
            scope="instance",
            context="cm",
            instance="testing",
            env={"APP_SETTING": "first"},
            updated_at="2026-10-19T00:00:00Z",
        )
        self.store.write_runtime_environment_record(settings)
        binding = None
        secret = None
        if change in {
            "policy",
            "binding",
            "binding_deleted",
            "binding_replaced",
            "binding_repointed",
        } or change.startswith("secret_"):
            secret = _runtime_secret_record(
                secret_id="site-secret",
                scope="context_instance" if change.startswith("secret_") else "context",
                name="api-token",
                version_id="site-version",
                context="cm",
                instance="testing" if change.startswith("secret_") else "",
            )
            self.store.write_secret_record(secret)
            self.store.write_secret_version(
                _runtime_secret_version(
                    secret_id="site-secret",
                    version_id="site-version",
                    plaintext_value="fixture token",
                )
            )
            binding = _runtime_secret_binding(
                secret_id="site-secret",
                binding_key="APP_TOKEN",
                context="cm",
                instance="testing" if change.startswith("secret_") else "",
            )
            self.store.write_secret_binding(binding)
            self.store.write_runtime_key_safety_policy_record(
                RuntimeKeySafetyPolicyRecord(
                    record_id="site-policy",
                    source="test",
                    updated_at="2026-10-19T00:00:00Z",
                    rules=(
                        RuntimeSecretSafetyRule(binding_key="APP_TOKEN", secret_class="testing"),
                    ),
                )
            )
        revoked = False
        policies = self.store.list_runtime_key_safety_policy_records
        saved: list[str] = []
        schedule_write = cast(Any, api.upsert_dokploy_application_schedule).side_effect
        publish = self.store.publish_generic_web_testing_result
        cleanup_writes: list[str] = []

        def upsert(**kwargs: Any) -> Any:
            if saved:
                cleanup_writes.append("hold")
                if cleanup_failure:
                    guard = kwargs.get("provider_mutation_guard")

                    @contextmanager
                    def lost_reply() -> Iterator[None]:
                        with guard() if callable(guard) else nullcontext():
                            yield
                            raise click.ClickException("Lost held-schedule reply")

                    kwargs["provider_mutation_guard"] = lost_reply
            return schedule_write(**kwargs)

        def write(**kwargs: Any) -> Any:
            record = kwargs["deployment"]
            if saved and publication_failure and record.deploy.status == "fail":
                raise RuntimeError("Failed refusal publication")
            return publish(**kwargs)

        def save(**kwargs: Any) -> dict[str, object]:
            nonlocal revoked
            if kwargs["path"] == "/api/application.saveDockerProvider":
                saved.append(str(kwargs["payload"]["dockerImage"]))
                if cleanup_failure:
                    self.input_schedule["enabled"] = True
                if change == "target":
                    current = self.store.read_dokploy_target_id_record(
                        context_name="cm", instance_name="testing"
                    )
                    self.store.write_dokploy_target_id_record(
                        current.model_copy(update={"target_id": "repointed-fixture"})
                    )
                elif change == "inactive":
                    profile = self.store.read_product_profile_record("site")
                    self.store.write_product_profile_record(
                        profile.model_copy(update={"lifecycle_state": "retiring"})
                    )
                elif change == "policy":
                    revoked = True
                elif change == "binding":
                    assert binding is not None
                    self.store.write_secret_binding(
                        binding.model_copy(update={"status": "disabled"})
                    )
                elif change == "binding_repointed":
                    assert binding is not None
                    self.store.write_secret_binding(
                        binding.model_copy(update={"secret_id": "different-secret"})
                    )
                elif change.startswith("secret_"):
                    assert secret is not None
                    changes = {
                        "secret_status": {"status": "disabled"},
                        "secret_scope": {"scope": "global", "context": "", "instance": ""},
                        "secret_context": {"context": "other-context"},
                        "secret_instance": {"instance": "other-lane"},
                        "secret_integration": {"integration": "other-integration"},
                    }
                    self.store.write_secret_record(secret.model_copy(update=changes[change]))
                else:
                    from control_plane.storage.postgres import LaunchplaneSecretBindingRow

                    assert binding is not None
                    with self.store._session_factory() as session:
                        row = session.get(LaunchplaneSecretBindingRow, binding.binding_id)
                        assert row is not None
                        session.delete(row)
                        session.commit()
                    if change == "binding_replaced":
                        self.store.write_secret_binding(
                            binding.model_copy(update={"binding_id": "replacement-binding"})
                        )
            return {}

        self.github.add_run(20, DEPLOYABLE)
        self.request()
        with (
            patch("control_plane.dokploy.api.dokploy_request", side_effect=save),
            patch(
                "control_plane.dokploy.api.upsert_dokploy_application_schedule", side_effect=upsert
            ),
            patch.object(self.store, "publish_generic_web_testing_result", side_effect=write),
            patch.object(
                self.store,
                "list_runtime_key_safety_policy_records",
                side_effect=lambda **kwargs: () if revoked else policies(**kwargs),
            ),
        ):
            self.assertEqual(self.run_once().state, "failed")
        self.assertEqual(len(saved), 1)
        self.assertFalse(triggered)
        (held,) = self.store.list_held_provider_target_reservations()
        self.assertEqual(held.provider_effect_phase, TESTING_DISPATCH_REFUSED_PHASE)
        if change in {"target", "inactive"}:
            self.assertFalse(cleanup_writes)
        return settings, held, schedule, triggered

    def test_target_drift_after_image_save_still_refuses_trigger(self) -> None:
        self.assert_post_image_safety_stops_dispatch("target")

    def test_product_retirement_after_image_save_still_refuses_trigger(self) -> None:
        self.assert_post_image_safety_stops_dispatch("inactive")

    def test_policy_revocation_after_image_save_still_refuses_trigger(self) -> None:
        self.assert_post_image_safety_stops_dispatch("policy")

    def test_captured_binding_revocation_after_image_save_still_refuses_trigger(self) -> None:
        self.assert_post_image_safety_stops_dispatch("binding")

    def test_captured_secret_status_revocation_after_image_save_refuses_trigger(self) -> None:
        self.assert_post_image_safety_stops_dispatch("secret_status")

    def test_captured_secret_scope_change_after_image_save_refuses_trigger(self) -> None:
        self.assert_post_image_safety_stops_dispatch("secret_scope")

    def test_captured_secret_context_change_after_image_save_refuses_trigger(self) -> None:
        self.assert_post_image_safety_stops_dispatch("secret_context")

    def test_captured_secret_instance_change_after_image_save_refuses_trigger(self) -> None:
        self.assert_post_image_safety_stops_dispatch("secret_instance")

    def test_captured_secret_integration_change_after_image_save_refuses_trigger(self) -> None:
        self.assert_post_image_safety_stops_dispatch("secret_integration")

    def test_deleted_captured_binding_after_image_save_still_refuses_trigger(self) -> None:
        self.assert_post_image_safety_stops_dispatch("binding_deleted")

    def test_recreated_captured_binding_cannot_authorize_old_payload(self) -> None:
        self.assert_post_image_safety_stops_dispatch("binding_replaced")

    def test_repointed_captured_binding_cannot_authorize_old_payload(self) -> None:
        self.assert_post_image_safety_stops_dispatch("binding_repointed")

    def create_testing_input_secret(
        self, secret_id: str, *, scope: Literal["global", "context", "context_instance"]
    ) -> tuple[SecretRecord, SecretBinding]:
        from tests.test_generic_web_deploy import (
            _runtime_secret_record,
            _runtime_secret_version,
            _runtime_secret_binding,
        )

        context = "" if scope == "global" else "cm"
        instance = "testing" if scope == "context_instance" else ""
        secret = _runtime_secret_record(
            secret_id=secret_id,
            scope=scope,
            name=secret_id,
            version_id=f"{secret_id}-version",
            context=context,
            instance=instance,
        )
        binding = _runtime_secret_binding(
            secret_id=secret_id, binding_key="APP_TOKEN", context=context, instance=instance
        )
        self.store.write_secret_record(secret)
        self.store.write_secret_version(
            _runtime_secret_version(
                secret_id=secret_id,
                version_id=secret.current_version_id,
                plaintext_value=f"fixture-{secret_id}",
            )
        )
        self.store.write_secret_binding(binding)
        return secret, binding

    def test_deleting_unused_global_and_shadowed_sources_after_save_preserves_dispatch(
        self,
    ) -> None:
        from control_plane.dokploy.api import parse_dokploy_env_text
        from control_plane.generic_web_testing_inputs import (
            DELIVERY_RECEIPT_KEY,
            testing_input_authority,
        )
        from control_plane.runtime_environments import site_delivered_secret_bindings_from_store
        from control_plane.storage.postgres import LaunchplaneSecretBindingRow

        payload, triggered = self.application_input_provider()
        self.application_input_schedule()
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="cm",
                instance="testing",
                env={"APP_SETTING": "first"},
                updated_at="2026-10-19T00:00:00Z",
            )
        )
        unused = (
            self.create_testing_input_secret("global-secret", scope="global")[1],
            self.create_testing_input_secret("context-secret", scope="context")[1],
        )
        _secret, selected = self.create_testing_input_secret(
            "selected-secret", scope="context_instance"
        )
        self.store.write_runtime_key_safety_policy_record(
            RuntimeKeySafetyPolicyRecord(
                record_id="site-policy",
                source="test",
                updated_at="2026-10-19T00:00:00Z",
                rules=(RuntimeSecretSafetyRule(binding_key="APP_TOKEN", secret_class="testing"),),
            )
        )
        profile = self.store.read_product_profile_record("site")
        captured = testing_input_authority(self.store, profile, profile.lanes[0])

        def save(**kwargs: Any) -> dict[str, object]:
            if kwargs["path"] == "/api/application.saveDockerProvider":
                with self.store._session_factory() as session:
                    for binding in unused:
                        row = session.get(LaunchplaneSecretBindingRow, binding.binding_id)
                        assert row is not None
                        session.delete(row)
                    session.commit()
            return {}

        self.github.add_run(20, DEPLOYABLE)
        self.request()
        with patch("control_plane.dokploy.api.dokploy_request", side_effect=save):
            first = self.reconcile()
        self.assertEqual(len(triggered), 1)
        self.assertEqual(
            parse_dokploy_env_text(str(payload["env"]))["APP_TOKEN"], "fixture-selected-secret"
        )
        self.assertEqual(
            site_delivered_secret_bindings_from_store(
                record_store=self.store, context_name="cm", instance_name="testing"
            )["APP_TOKEN"].binding_id,
            selected.binding_id,
        )
        record = self.store.read_deployment_record(str(first["deployment_record_id"]))
        self.assertEqual(record.runtime_source[DELIVERY_RECEIPT_KEY], captured)
        self.assertFalse(self.store.list_held_provider_target_reservations())

    def assert_policy_uses_captured_supplier(
        self, *, after_save: bool, policy_only: bool = False
    ) -> None:
        from control_plane.generic_web_testing_inputs import TESTING_DISPATCH_REFUSED_PHASE
        from control_plane.dokploy.api import parse_dokploy_env_text

        payload, triggered = self.application_input_provider()
        self.application_input_schedule()
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="cm",
                instance="testing",
                env={
                    "APP_SETTING": "first",
                    **({"APP_TOKEN": "plain fixture"} if policy_only else {}),
                },
                updated_at="2026-10-19T00:00:00Z",
            )
        )
        self.create_testing_input_secret(
            "selected-secret", scope="global" if policy_only else "context"
        )
        if not policy_only:
            unused, binding = self.create_testing_input_secret(
                "disabled-secret", scope="context_instance"
            )
            self.store.write_secret_record(unused.model_copy(update={"status": "disabled"}))
            self.store.write_secret_binding(
                binding.model_copy(
                    update={"declared_secret_class": "testing", "value_source": "supplied"}
                )
            )

        def write_policy(secret_class: Literal["testing", "prod_only"]) -> None:
            self.store.write_runtime_key_safety_policy_record(
                RuntimeKeySafetyPolicyRecord(
                    record_id="site-policy",
                    source="test",
                    updated_at="2026-10-19T00:00:00Z",
                    rules=(
                        RuntimeSecretSafetyRule(binding_key="APP_TOKEN", secret_class=secret_class),
                    ),
                )
            )

        write_policy("testing" if after_save else "prod_only")
        saved: list[str] = []

        def save(**kwargs: Any) -> dict[str, object]:
            if kwargs["path"] == "/api/application.saveDockerProvider":
                saved.append(str(kwargs["payload"]["dockerImage"]))
                write_policy("prod_only")
            return {}

        self.github.add_run(20, DEPLOYABLE)
        self.request()
        with patch("control_plane.dokploy.api.dokploy_request", side_effect=save):
            self.assertEqual(self.run_once().state, "failed")
        self.assertFalse(triggered)
        if after_save:
            self.assertEqual(len(saved), 1)
            (held,) = self.store.list_held_provider_target_reservations()
            self.assertEqual(held.provider_effect_phase, TESTING_DISPATCH_REFUSED_PHASE)
            self.assertFalse(self.input_schedule["enabled"])
            self.assertEqual(
                parse_dokploy_env_text(str(payload["env"]))["APP_TOKEN"],
                "plain fixture" if policy_only else "fixture-selected-secret",
            )
        else:
            self.assertFalse(saved)
            self.assertFalse(self.store.list_held_provider_target_reservations())
            self.assertTrue(self.input_schedule["enabled"])

    def test_preparation_policy_checks_actual_selected_supplier(self) -> None:
        self.assert_policy_uses_captured_supplier(after_save=False)

    def test_post_save_policy_revocation_checks_actual_selected_supplier(self) -> None:
        self.assert_policy_uses_captured_supplier(after_save=True)

    def test_post_save_policy_still_checks_frozen_keys_without_delivered_secrets(self) -> None:
        self.assert_policy_uses_captured_supplier(after_save=True, policy_only=True)

    def test_retirement_after_save_finishes_captured_secret_then_retires_provider_key(self) -> None:
        from control_plane.dokploy.api import parse_dokploy_env_text
        from control_plane.generic_web_testing_inputs import (
            DELIVERY_RECEIPT_KEY,
            testing_input_authority,
        )

        payload, triggered = self.application_input_provider()
        self.create_testing_input_secret("selected-secret", scope="context_instance")
        settings = RuntimeEnvironmentRecord(
            scope="instance",
            context="cm",
            instance="testing",
            env={"APP_SETTING": "first"},
            updated_at="2026-10-19T00:00:00Z",
        )
        self.store.write_runtime_environment_record(settings)
        self.store.write_runtime_key_safety_policy_record(
            RuntimeKeySafetyPolicyRecord(
                record_id="site-policy",
                source="test",
                updated_at="2026-10-19T00:00:00Z",
                rules=(RuntimeSecretSafetyRule(binding_key="APP_TOKEN", secret_class="testing"),),
            )
        )
        profile = self.store.read_product_profile_record("site")
        captured = testing_input_authority(self.store, profile, profile.lanes[0])

        def save(**kwargs: Any) -> dict[str, object]:
            if kwargs["path"] == "/api/application.saveDockerProvider":
                self.store.write_runtime_environment_record(
                    settings.model_copy(
                        update={"schema_version": 2, "retired_provider_keys": ("APP_TOKEN",)}
                    )
                )
            return {}

        self.github.add_run(20, DEPLOYABLE)
        self.request()
        with patch("control_plane.dokploy.api.dokploy_request", side_effect=save):
            first = self.run_once()
        self.assertEqual(first.state, "failed")
        self.assertEqual(len(triggered), 1)
        self.assertEqual(
            parse_dokploy_env_text(str(payload["env"]))["APP_TOKEN"], "fixture-selected-secret"
        )
        record = self.settle_terminal_testing_original()
        self.assertEqual(record.runtime_source[DELIVERY_RECEIPT_KEY], captured)
        self.request()
        self.reconcile()
        self.assertEqual(len(triggered), 2)
        self.assertNotIn("APP_TOKEN", parse_dokploy_env_text(str(payload["env"])))
        self.request()
        self.assertEqual(self.reconcile()["reason"], "already_deployed")
        self.assertEqual(len(triggered), 2)

    def settle_refused_testing_dispatch(self, reservation: Any) -> Any:
        from control_plane.testing_event_deploy_recovery import settle_obsolete_testing_event_deploy

        with patch.object(
            self.application_inputs_provider,
            "observe_artifact_deploy",
            return_value=GenericWebProviderDeploymentObservation(outcome="absent"),
        ):
            return settle_obsolete_testing_event_deploy(
                store=self.store,
                reservation=reservation,
                profile=self.store.read_product_profile_record("site"),
                context="cm",
                current_target_key=reservation.provider_target_key,
                desired_fingerprint=reservation.request_fingerprint,
                provider=self.application_inputs_provider,
                root=self.root,
                trace_id="refused-dispatch-settlement",
                check_execution_lock=lambda: None,
            )

    def assert_refused_dispatch_settles_held(
        self,
        *,
        change: str = "binding_deleted",
        cleanup_failure: bool = False,
        publication_failure: bool = False,
    ) -> None:
        from control_plane.generic_web_testing_settlement import TESTING_DISPATCH_REFUSED_SETTLED
        from control_plane.workflows.generic_web_application_snapshot import (
            recorded_application_lifecycle,
        )

        settings, original, schedule, triggered = self.assert_post_image_safety_stops_dispatch(
            change, cleanup_failure=cleanup_failure, publication_failure=publication_failure
        )
        with patch.object(self.application_inputs_provider, "execute_artifact_deploy") as execute:
            result = self.settle_refused_testing_dispatch(original)
            if cleanup_failure:
                self.assertIsNone(result)
                self.assertEqual(
                    self.store.list_held_provider_target_reservations()[0].record_id,
                    original.record_id,
                )
                execute.assert_not_called()
                return
            assert result is not None
            self.assertEqual(result.status, "completed")
            self.assertEqual(
                result.response_payload["result"]["error_code"], TESTING_DISPATCH_REFUSED_SETTLED
            )
            execute.assert_not_called()
        self.assertFalse(self.store.list_held_provider_target_reservations())
        record_id = result.response_payload["records"]["deployment_record_id"]
        record = self.store.read_deployment_record(record_id)
        receipt = recorded_application_lifecycle(record)
        assert receipt is not None
        self.assertEqual(receipt.state, "held")
        self.assertEqual(receipt.originals["cleanup"], schedule)
        self.assertFalse(self.input_schedule["enabled"])
        self.assertFalse(triggered)
        # Existing explicit retirement removes the revoked value already saved
        # on the provider. Recovery never recreates its binding or secret grant.
        self.store.write_runtime_environment_record(
            settings.model_copy(
                update={"schema_version": 2, "retired_provider_keys": ("APP_TOKEN",)}
            )
        )
        self.request()
        repaired = self.reconcile()
        self.assertNotEqual(repaired["deploy_idempotency_key"], original.idempotency_key)
        self.assertEqual(len(triggered), 1)
        self.assertFalse(
            any(binding.binding_key == "APP_TOKEN" for binding in self.store.list_secret_bindings())
        )

    def assert_new_secret_requirement_after_save_finishes_captured_inputs(
        self, *, declaration: bool, rotation: bool = False
    ) -> None:
        from control_plane.contracts.product_profile_record import ProductSecretConfigRequirement
        from control_plane.generic_web_testing_inputs import (
            DELIVERY_RECEIPT_KEY,
            testing_input_authority,
        )
        from control_plane.dokploy.api import parse_dokploy_env_text
        from tests.test_generic_web_deploy import (
            _runtime_secret_record,
            _runtime_secret_version,
            _runtime_secret_binding,
        )

        payload, triggered = self.application_input_provider()
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="cm",
                instance="testing",
                env={"APP_SETTING": "first"},
                updated_at="2026-10-19T00:00:00Z",
            )
        )
        secret = _runtime_secret_record(
            secret_id="new-secret",
            scope="context",
            name="new-token",
            version_id="new-version",
            context="cm",
        )
        self.store.write_secret_record(secret)
        self.store.write_secret_version(
            _runtime_secret_version(
                secret_id="new-secret",
                version_id="new-version",
                plaintext_value="fixture new value",
            )
        )
        key = "APP_TOKEN" if rotation else ("NEW_TOKEN" if declaration else "APP_SETTING")
        binding = _runtime_secret_binding(secret_id="new-secret", binding_key=key, context="cm")
        if rotation:
            self.store.write_secret_binding(binding)
        self.store.write_runtime_key_safety_policy_record(
            RuntimeKeySafetyPolicyRecord(
                record_id="site-policy",
                source="test",
                updated_at="2026-10-19T00:00:00Z",
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key=key if rotation else "UNRELATED_TOKEN", secret_class="testing"
                    ),
                ),
            )
        )
        profile = self.store.read_product_profile_record("site")
        captured = testing_input_authority(self.store, profile, profile.lanes[0])

        def save(**kwargs: Any) -> dict[str, object]:
            if kwargs["path"] == "/api/application.saveDockerProvider":
                if rotation:
                    self.store.write_secret_version(
                        _runtime_secret_version(
                            secret_id="new-secret",
                            version_id="rotated-version",
                            plaintext_value="fixture rotated value",
                        )
                    )
                    self.store.write_secret_record(
                        secret.model_copy(update={"current_version_id": "rotated-version"})
                    )
                elif declaration:
                    current = self.store.read_product_profile_record("site")
                    expected = current.expected_config.model_copy(
                        update={
                            "managed_secret_bindings": (
                                ProductSecretConfigRequirement(
                                    binding_key=key, context="cm", instance="testing"
                                ),
                            )
                        }
                    )
                    self.store.write_product_profile_record(
                        current.model_copy(update={"expected_config": expected})
                    )
                else:
                    self.store.write_secret_binding(binding)
            return {}

        self.github.add_run(20, DEPLOYABLE)
        self.request()
        with patch("control_plane.dokploy.api.dokploy_request", side_effect=save):
            first = self.run_once()
        self.assertEqual(first.state, "failed")
        record = self.settle_terminal_testing_original()
        self.assertEqual(record.runtime_source[DELIVERY_RECEIPT_KEY], captured)
        self.assertEqual(parse_dokploy_env_text(str(payload["env"]))["APP_SETTING"], "first")
        self.assertEqual(len(triggered), 1)
        if rotation:
            self.assertEqual(parse_dokploy_env_text(str(payload["env"]))[key], "fixture new value")
        self.store.write_secret_binding(binding)
        self.store.write_runtime_key_safety_policy_record(
            RuntimeKeySafetyPolicyRecord(
                record_id="site-policy",
                source="test",
                updated_at="2026-10-19T00:00:00Z",
                rules=(RuntimeSecretSafetyRule(binding_key=key, secret_class="testing"),),
            )
        )
        self.request()
        repaired = self.reconcile()
        self.assertEqual(len(triggered), 2)
        self.assertEqual(
            parse_dokploy_env_text(str(payload["env"]))[key],
            "fixture rotated value" if rotation else "fixture new value",
        )
        current = self.store.read_product_profile_record("site")
        record = self.store.read_deployment_record(str(repaired["deployment_record_id"]))
        self.assertEqual(
            record.runtime_source[DELIVERY_RECEIPT_KEY],
            testing_input_authority(self.store, current, current.lanes[0]),
        )
        self.request()
        self.assertEqual(self.reconcile()["reason"], "already_deployed")
        self.assertEqual(len(triggered), 2)

    def test_new_required_secret_after_save_finishes_captured_inputs_then_repairs(self) -> None:
        self.assert_new_secret_requirement_after_save_finishes_captured_inputs(declaration=True)

    def test_secret_rotation_after_save_finishes_captured_version_then_repairs(self) -> None:
        self.assert_new_secret_requirement_after_save_finishes_captured_inputs(
            declaration=False, rotation=True
        )

    def test_uncertain_image_save_cannot_acquire_known_dispatch_refusal_proof(self) -> None:
        from control_plane.generic_web_testing_inputs import TestingInputSafetyRefused

        _payload, triggered = self.application_input_provider()
        self.application_input_schedule()
        settings = RuntimeEnvironmentRecord(
            scope="instance",
            context="cm",
            instance="testing",
            env={"APP_SETTING": "first"},
            updated_at="2026-10-19T00:00:00Z",
        )
        self.store.write_runtime_environment_record(settings)
        self.github.add_run(20, DEPLOYABLE)
        self.request()

        def save(**kwargs: Any) -> dict[str, object]:
            if kwargs["path"] == "/api/application.saveDockerProvider":
                raise TestingInputSafetyRefused("Image save reply is uncertain")
            return {}

        with patch("control_plane.dokploy.api.dokploy_request", side_effect=save):
            self.assertEqual(self.run_once().state, "failed")
        (original,) = self.store.list_held_provider_target_reservations()
        self.assertEqual(original.provider_effect_phase, "target_update")
        self.store.write_runtime_environment_record(
            settings.model_copy(update={"env": {"APP_SETTING": "second"}})
        )
        self.assertIsNone(self.settle_refused_testing_dispatch(original))
        self.assertEqual(self.store.list_held_provider_target_reservations(), (original,))
        self.assertFalse(self.input_schedule["enabled"])
        self.assertFalse(triggered)

    def test_new_binding_on_plain_setting_after_save_finishes_captured_inputs_then_repairs(
        self,
    ) -> None:
        self.assert_new_secret_requirement_after_save_finishes_captured_inputs(declaration=False)

    def test_refused_dispatch_settlement_lost_lease_keeps_proof_and_hold(self) -> None:
        _settings, original, schedule, triggered = self.assert_post_image_safety_stops_dispatch(
            "binding_deleted"
        )
        with (
            patch.object(
                self.store,
                "checkpoint_mutation_provider_effect",
                side_effect=RuntimeError("lease lost"),
            ),
            patch("control_plane.dokploy.api.upsert_dokploy_application_schedule") as upsert,
        ):
            result = self.settle_refused_testing_dispatch(original)
            assert result is not None
            self.assertEqual(result.status, "reconcile_required")
            upsert.assert_not_called()
        (retained,) = self.store.list_held_provider_target_reservations()
        self.assertEqual(retained.provider_effect_phase, original.provider_effect_phase)
        self.assertFalse(self.input_schedule["enabled"])
        self.assertFalse(triggered)
        self.assertTrue(schedule["enabled"])

    def test_refused_dispatch_settlement_publication_failure_resumes_held(self) -> None:
        _settings, original, _schedule, triggered = self.assert_post_image_safety_stops_dispatch(
            "binding_deleted"
        )
        with patch.object(
            self.store,
            "publish_generic_web_testing_result",
            side_effect=RuntimeError("publication failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "publication failed"):
                self.settle_refused_testing_dispatch(original)
        (retained,) = self.store.list_held_provider_target_reservations()
        self.assertEqual(retained.provider_effect_phase, original.provider_effect_phase)
        result = self.settle_refused_testing_dispatch(retained)
        assert result is not None
        self.assertEqual(result.status, "completed")
        self.assertFalse(self.input_schedule["enabled"])
        self.assertFalse(triggered)

    def test_refused_dispatch_with_observed_deployment_cannot_settle_failed(self) -> None:
        from control_plane.testing_event_deploy_recovery import settle_obsolete_testing_event_deploy

        _settings, original, _schedule, triggered = self.assert_post_image_safety_stops_dispatch(
            "binding_deleted"
        )
        # The fixture's default observation is an exact successful deployment.
        with patch("control_plane.dokploy.api.upsert_dokploy_application_schedule") as upsert:
            result = settle_obsolete_testing_event_deploy(
                store=self.store,
                reservation=original,
                profile=self.store.read_product_profile_record("site"),
                context="cm",
                current_target_key=original.provider_target_key,
                desired_fingerprint=original.request_fingerprint,
                provider=self.application_inputs_provider,
                root=self.root,
                trace_id="observed-refusal",
                check_execution_lock=lambda: None,
            )
            self.assertIsNone(result)
            upsert.assert_not_called()
        self.assertEqual(self.store.list_held_provider_target_reservations(), (original,))
        self.assertFalse(triggered)

    def test_refused_dispatch_repoint_cannot_settle_or_mutate_original_target(self) -> None:
        _settings, original, _schedule, _triggered = self.assert_post_image_safety_stops_dispatch(
            "target"
        )
        with patch("control_plane.dokploy.api.upsert_dokploy_application_schedule") as upsert:
            self.assertIsNone(self.settle_refused_testing_dispatch(original))
            upsert.assert_not_called()
        self.assertEqual(self.store.list_held_provider_target_reservations(), (original,))

    def test_revoked_dispatch_settles_failed_without_regrant_then_replaces_image(self) -> None:
        self.assert_refused_dispatch_settles_held()

    def test_known_refusal_with_unchanged_inputs_gets_one_canonical_successor(self) -> None:
        from control_plane.generic_web_testing_settlement import TESTING_DISPATCH_REFUSED_SETTLED

        _settings, original, _schedule, triggered = self.assert_post_image_safety_stops_dispatch(
            "policy"
        )
        self.request()
        with patch.object(
            self.application_inputs_provider,
            "observe_artifact_deploy",
            return_value=GenericWebProviderDeploymentObservation(outcome="absent"),
        ):
            self.assertEqual(self.run_once().state, "failed")
        completed = self.store.read_idempotency_record(
            scope=original.scope,
            route_path=original.route_path,
            idempotency_key=original.idempotency_key,
        )
        assert completed is not None
        self.assertEqual(
            completed.response_payload["result"]["error_code"], TESTING_DISPATCH_REFUSED_SETTLED
        )
        self.assertFalse(self.store.list_held_provider_target_reservations())
        self.assertFalse(self.input_schedule["enabled"])
        self.assertFalse(triggered)
        self.request()
        successor = self.reconcile()
        self.assertNotEqual(successor["deploy_idempotency_key"], original.idempotency_key)
        self.assertEqual(len(triggered), 1)
        self.request()
        self.assertEqual(self.reconcile()["reason"], "already_deployed")
        self.assertEqual(len(triggered), 1)

    def test_refused_dispatch_cleanup_failure_preserves_settlement_proof(self) -> None:
        self.assert_refused_dispatch_settles_held(cleanup_failure=True)

    def test_refused_dispatch_publication_failure_preserves_settlement_proof(self) -> None:
        self.assert_refused_dispatch_settles_held(publication_failure=True)

    def test_provider_only_platform_credential_refuses_before_schedule_quiesce(self) -> None:
        payload, triggered = self.application_input_provider()
        original = self.application_input_schedule()
        payload["env"] = "GITHUB_TOKEN=ghp_" + "A1b2C3d4E5" * 4
        settings = RuntimeEnvironmentRecord(
            scope="instance",
            context="cm",
            instance="testing",
            env={"APP_SETTING": "first"},
            updated_at="2026-10-19T00:00:00Z",
        )
        self.store.write_runtime_environment_record(settings)
        self.github.add_run(20, DEPLOYABLE)
        with patch("control_plane.dokploy.api.upsert_dokploy_application_schedule") as upsert:
            for _ in range(2):
                self.request()
                self.assertEqual(self.run_once().state, "failed")
                self.assertFalse(self.store.list_held_provider_target_reservations())
            upsert.assert_not_called()
        self.assertEqual(self.input_schedule, original)
        self.assertFalse(triggered)
        self.store.write_runtime_environment_record(
            settings.model_copy(
                update={"schema_version": 2, "retired_provider_keys": ("GITHUB_TOKEN",)}
            )
        )
        self.request()
        self.assertEqual(self.reconcile()["action"], "deploy")
        self.assertEqual(len(triggered), 1)

    def test_lost_settlement_reply_stays_fenced_after_input_reversion(
        self,
    ) -> None:
        from control_plane.dokploy import api
        from control_plane.generic_web_testing_settlement import (
            TESTING_PRE_IMAGE_SETTLEMENT_PHASE,
            testing_attempt_was_settled,
        )
        from control_plane.provider_operations import ProviderMutationUnknownError

        settings, original, schedule, triggered = self.prepare_interrupted_testing_inputs(
            lifecycle=True
        )
        self.store.write_runtime_environment_record(
            settings.model_copy(update={"env": {"APP_SETTING": "second"}})
        )
        upsert = cast(Any, api.upsert_dokploy_application_schedule).side_effect

        def lost_restore_reply(**kwargs: Any) -> Any:
            guard = kwargs.get("provider_mutation_guard")

            @contextmanager
            def lost_reply() -> Iterator[None]:
                with guard() if callable(guard) else nullcontext():
                    yield
                    raise ProviderMutationUnknownError("lost schedule restoration reply")

            return upsert(**{**kwargs, "provider_mutation_guard": lost_reply})

        self.request()
        with (
            patch.object(
                self.application_inputs_provider,
                "observe_artifact_deploy",
                return_value=GenericWebProviderDeploymentObservation(outcome="absent"),
            ),
            patch.object(
                api, "upsert_dokploy_application_schedule", side_effect=lost_restore_reply
            ),
        ):
            self.assertEqual(self.run_once().state, "failed")
        (held,) = self.store.list_held_provider_target_reservations()
        self.assertEqual(held.provider_effect_phase, TESTING_PRE_IMAGE_SETTLEMENT_PHASE)
        self.assertEqual(self.input_schedule, schedule)
        self.assertFalse(triggered)
        self.store.write_runtime_environment_record(settings)
        self.request()
        with (
            patch.object(
                self.application_inputs_provider,
                "observe_artifact_deploy",
                return_value=GenericWebProviderDeploymentObservation(outcome="absent"),
            ),
            patch.object(api, "upsert_dokploy_application_schedule") as repeated_restore,
        ):
            self.assertEqual(self.run_once().state, "failed")
        repeated_restore.assert_not_called()
        completed = self.store.read_idempotency_record(
            scope=original.scope,
            route_path=original.route_path,
            idempotency_key=original.idempotency_key,
        )
        assert completed is not None
        self.assertFalse(testing_attempt_was_settled(completed))
        (retained,) = self.store.list_held_provider_target_reservations()
        self.assertEqual(retained.record_id, original.record_id)
        self.assertEqual(retained.provider_effect_phase, TESTING_PRE_IMAGE_SETTLEMENT_PHASE)
        self.assertEqual(self.input_schedule, schedule)
        self.assertFalse(triggered)

    def test_settlement_lost_lease_keeps_original_fence_and_baseline(self) -> None:
        settings, original, _schedule, triggered = self.prepare_interrupted_testing_inputs(
            lifecycle=True
        )
        self.store.write_runtime_environment_record(
            settings.model_copy(update={"env": {"APP_SETTING": "second"}})
        )
        before = self.store.list_deployment_records(context_name="cm", instance_name="testing")[0]
        self.request()
        with (
            patch.object(
                self.application_inputs_provider,
                "observe_artifact_deploy",
                return_value=GenericWebProviderDeploymentObservation(outcome="absent"),
            ),
            patch.object(
                self.store,
                "checkpoint_mutation_provider_effect",
                side_effect=RuntimeError("lease lost"),
            ),
            patch("control_plane.dokploy.api.upsert_dokploy_application_schedule") as upsert,
        ):
            self.assertEqual(self.run_once().state, "failed")
            upsert.assert_not_called()
        (held,) = self.store.list_held_provider_target_reservations()
        self.assertEqual(held.record_id, original.record_id)
        self.assertEqual(self.store.read_deployment_record(before.record_id), before)
        self.assertFalse(triggered)

    def assert_service_recovery_settles_application_and_refuses_changed_reviewed_inputs(
        self, *, refused_dispatch: bool = False
    ) -> None:
        from tests.test_generic_web_deploy_recovery import _create_recovery_app, _OPERATOR_TOKEN
        from tests.support.auth import local_operator_policy
        from tests.test_service import _invoke_app
        from control_plane.workflows.generic_web_deploy_provider import (
            decode_generic_web_provider_reconciliation_target,
        )
        from control_plane.generic_web_testing_settlement import testing_attempt_was_settled

        settings, original, schedule, triggered = (
            self.assert_post_image_safety_stops_dispatch("binding_deleted")
            if refused_dispatch
            else self.prepare_interrupted_testing_inputs(lifecycle=True)
        )
        snapshot = decode_generic_web_provider_reconciliation_target(original.reconciliation_key)
        app = _create_recovery_app(
            root=self.root,
            store=self.store,
            authz_policy=local_operator_policy(
                actions=("generic_web_deploy.execute",), products=("site",), contexts=("cm",)
            ),
        )
        payload = {
            "schema_version": 1,
            "product": "site",
            "instance": "testing",
            "original_deploy": snapshot.original_event_deploy,
            "reason": "Resume original controller.",
        }

        def invoke(mode: str, digest: str = "") -> tuple[int, dict[str, Any]]:
            return _invoke_app(
                app,
                method="POST",
                path=f"/v1/admin/generic-web/deploy-recovery/{mode}",
                authorization=f"Bearer {_OPERATOR_TOKEN}",
                payload={**payload, **({"expected_recovery_digest": digest} if digest else {})},
                headers={"Idempotency-Key": original.idempotency_key},
            )

        with (
            patch(
                "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
                return_value=self.application_inputs_provider,
            ),
            patch.object(
                self.application_inputs_provider,
                "observe_artifact_deploy",
                return_value=GenericWebProviderDeploymentObservation(outcome="absent"),
            ),
            patch.object(self.application_inputs_provider, "execute_artifact_deploy") as execute,
        ):
            status, reviewed = invoke("dry-run")
            self.assertEqual(status, 200, reviewed)
            self.assertTrue(reviewed["retry_safe"])
            self.assertEqual(self.store.list_held_provider_target_reservations(), (original,))
            self.assertFalse(self.input_schedule["enabled"])
            self.store.write_runtime_environment_record(
                settings.model_copy(update={"env": {"APP_SETTING": "second"}})
            )
            status, refused = invoke("apply", reviewed["recovery_digest"])
            self.assertEqual(status, 409, refused)
            self.assertEqual(refused["error"]["code"], "stale_recovery_digest")
            status, reviewed = invoke("dry-run")
            self.assertEqual(status, 200, reviewed)
            self.store.write_runtime_environment_record(
                settings.model_copy(update={"env": {"APP_SETTING": "third"}})
            )
            status, refused = invoke("apply", reviewed["recovery_digest"])
            self.assertEqual(status, 409, refused)
            self.assertEqual(refused["error"]["code"], "stale_recovery_digest")
            self.assertEqual(self.store.list_held_provider_target_reservations(), (original,))
            status, current = invoke("dry-run")
            self.assertEqual(status, 200, current)
            status, recovered = invoke("apply", current["recovery_digest"])
            self.assertEqual(status, 202, recovered)
            execute.assert_not_called()
        completed = self.store.read_idempotency_record(
            scope=original.scope,
            route_path=original.route_path,
            idempotency_key=original.idempotency_key,
        )
        assert completed is not None
        self.assertTrue(testing_attempt_was_settled(completed))
        self.assertFalse(self.store.list_held_provider_target_reservations())
        self.assertEqual(
            self.input_schedule, {**schedule, "enabled": False} if refused_dispatch else schedule
        )
        self.assertFalse(triggered)

    def test_service_recovery_settles_application_and_refuses_changed_reviewed_inputs(self) -> None:
        self.assert_service_recovery_settles_application_and_refuses_changed_reviewed_inputs()

    def test_service_recovery_settles_known_refusal_held_and_binds_current_inputs(self) -> None:
        self.assert_service_recovery_settles_application_and_refuses_changed_reviewed_inputs(
            refused_dispatch=True
        )

    def assert_uncaptured_testing_attempt_is_settled(
        self, *, changed_inputs: bool, capture_baseline: bool = False
    ) -> None:
        from control_plane.provider_operations import ProviderMutationUnknownError
        from control_plane.storage.postgres import LaunchplaneIdempotencyRow
        from control_plane.generic_web_testing_settlement import testing_attempt_was_settled

        _payload, triggered = self.application_input_provider()
        schedule = self.application_input_schedule()
        settings = RuntimeEnvironmentRecord(
            scope="instance",
            context="cm",
            instance="testing",
            env={"APP_SETTING": "first"},
            updated_at="2026-10-19T00:00:00Z",
        )
        self.store.write_runtime_environment_record(settings)
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        interruption = (
            patch.object(
                self.store,
                "checkpoint_mutation_provider_effect",
                side_effect=RuntimeError("crash after read-only baseline capture"),
            )
            if capture_baseline
            else patch(
                "control_plane.generic_web_deploy_provider_adapter.GenericWebDeployProviderMutationAdapter.apply",
                side_effect=ProviderMutationUnknownError("crash before lifecycle capture"),
            )
        )
        with interruption:
            self.assertEqual(self.run_once().state, "failed")
        (original,) = self.store.list_held_provider_target_reservations()
        self.assertFalse(original.provider_effect_phase)
        self.assertFalse(original.provider_effect_started_at)
        captured = self.store.list_deployment_records(context_name="cm", instance_name="testing")
        self.assertEqual(bool(captured), capture_baseline)
        expired_at = datetime.now(timezone.utc) - timedelta(hours=1)
        expired = type(original).model_validate(
            {
                **original.model_dump(),
                "state": "running",
                "lease_expires_at": expired_at.isoformat(),
                "updated_at": (expired_at - timedelta(minutes=1)).isoformat(),
                "created_at": (expired_at - timedelta(minutes=2)).isoformat(),
            }
        )
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneIdempotencyRow, original.record_id)
            assert row is not None
            self.store._sync_idempotency_row(row, expired)
            session.commit()
        if changed_inputs:
            self.store.write_runtime_environment_record(
                settings.model_copy(update={"env": {"APP_SETTING": "second"}})
            )
        self.github.add_run(30, NEWEST)
        self.request()
        with (
            patch.object(
                self.application_inputs_provider,
                "observe_artifact_deploy",
                return_value=GenericWebProviderDeploymentObservation(outcome="absent"),
            ),
            patch.object(
                self.store,
                "publish_generic_web_testing_result",
                side_effect=RuntimeError("DB write failed"),
            ),
            patch.object(self.store, "checkpoint_mutation_provider_effect") as checkpoint,
            patch("control_plane.dokploy.api.upsert_dokploy_application_schedule") as upsert,
        ):
            self.assertEqual(self.run_once().state, "failed")
            checkpoint.assert_not_called()
            upsert.assert_not_called()
        (retained,) = self.store.list_held_provider_target_reservations()
        self.assertEqual(retained.record_id, original.record_id)
        self.assertFalse(retained.provider_effect_phase)
        self.assertFalse(retained.provider_effect_started_at)
        self.assertEqual(self.input_schedule, schedule)
        self.assertFalse(triggered)
        repaired = self.recover_testing_inputs(original)
        self.assertEqual(repaired["action"], "deploy")
        completed = self.store.read_idempotency_record(
            scope=original.scope,
            route_path=original.route_path,
            idempotency_key=original.idempotency_key,
        )
        assert completed is not None
        self.assertTrue(testing_attempt_was_settled(completed))
        self.assertFalse(completed.provider_effect_phase)
        self.assertFalse(completed.provider_effect_started_at)
        if captured:
            from control_plane.workflows.generic_web_application_snapshot import (
                recorded_application_lifecycle,
            )

            receipt = recorded_application_lifecycle(
                self.store.read_deployment_record(captured[0].record_id)
            )
            assert receipt is not None
            self.assertEqual(receipt.state, "restored")
            self.assertEqual(receipt.originals["cleanup"], schedule)
        self.assertFalse(self.store.list_held_provider_target_reservations())
        self.assertEqual(len(triggered), 1)
        deployed = self.store.read_deployment_record(str(repaired["deployment_record_id"]))
        self.assertEqual(deployed.source_git_ref, NEWEST)
        self.assertEqual(self.input_schedule, schedule)
        self.request()
        self.assertEqual(self.reconcile()["reason"], "already_deployed")
        self.assertEqual(len(triggered), 1)

    def test_newer_image_settles_expired_attempt_before_any_lifecycle_capture(self) -> None:
        self.assert_uncaptured_testing_attempt_is_settled(changed_inputs=False)

    def test_newer_image_and_inputs_settle_expired_attempt_before_lifecycle_capture(self) -> None:
        self.assert_uncaptured_testing_attempt_is_settled(changed_inputs=True)

    def test_read_only_captured_baseline_settles_without_provider_restoration(self) -> None:
        self.assert_uncaptured_testing_attempt_is_settled(
            changed_inputs=True, capture_baseline=True
        )

    def test_missing_lifecycle_baseline_after_an_effect_stays_fenced(self) -> None:
        _settings, original, _schedule, triggered = self.prepare_interrupted_testing_inputs(
            lifecycle=True
        )
        before = self.store.list_deployment_records(context_name="cm", instance_name="testing")[0]
        missing = before.model_copy(
            update={
                "post_deploy_update": PostDeployUpdateEvidence(),
            }
        )
        with self.assertRaises(ValueError):
            self.store.write_deployment_record(missing)
        read_deployment = self.store.read_deployment_record
        self.github.add_run(30, NEWEST)
        self.request()
        with (
            # Model a historical missing baseline without bypassing protected ingestion.
            patch.object(
                self.store,
                "read_deployment_record",
                side_effect=lambda record_id: (
                    missing if record_id == before.record_id else read_deployment(record_id)
                ),
            ),
            patch.object(
                self.application_inputs_provider,
                "observe_artifact_deploy",
                return_value=GenericWebProviderDeploymentObservation(outcome="absent"),
            ),
            patch("control_plane.dokploy.api.upsert_dokploy_application_schedule") as upsert,
        ):
            self.assertEqual(self.run_once().state, "failed")
            upsert.assert_not_called()
        self.assertEqual(self.store.list_held_provider_target_reservations(), (original,))
        self.assertFalse(self.input_schedule["enabled"])
        self.assertFalse(triggered)

    def assert_runtime_policy_revocation(
        self, *, after_readback: bool, interruption_phase: str = ""
    ) -> None:
        from tests.test_generic_web_deploy import (
            _runtime_secret_record,
            _runtime_secret_version,
            _runtime_secret_binding,
        )

        payload, triggered = self.application_input_provider()
        original_schedule = self.application_input_schedule() if interruption_phase else None
        self.store.write_secret_record(
            _runtime_secret_record(
                secret_id="site-secret",
                scope="context",
                name="api-token",
                version_id="site-version",
                context="cm",
            )
        )
        self.store.write_secret_version(
            _runtime_secret_version(
                secret_id="site-secret", version_id="site-version", plaintext_value="fixture token"
            )
        )
        self.store.write_secret_binding(
            _runtime_secret_binding(secret_id="site-secret", binding_key="APP_TOKEN", context="cm")
        )
        self.store.write_runtime_key_safety_policy_record(
            RuntimeKeySafetyPolicyRecord(
                record_id="site-policy",
                source="test",
                updated_at="2026-10-19T00:00:00Z",
                rules=(RuntimeSecretSafetyRule(binding_key="APP_TOKEN", secret_class="testing"),),
            )
        )
        revoked = False
        list_policy = self.store.list_runtime_key_safety_policy_records
        execute = self.application_inputs_provider.execute_artifact_deploy

        def policies(**kwargs: Any) -> Any:
            return () if revoked else list_policy(**kwargs)

        def apply(**kwargs: Any) -> Any:
            original_checkpoint = kwargs["before_provider_mutation"]

            def checkpoint(phase: str) -> None:
                nonlocal revoked
                original_checkpoint(phase)
                if phase == interruption_phase:
                    from control_plane.provider_operations import ProviderMutationUnknownError

                    raise ProviderMutationUnknownError("interrupted before image write")
                if phase == (
                    "runtime_inputs_verified" if after_readback else "provider_dispatch_prepared"
                ):
                    revoked = True

            kwargs["before_provider_mutation"] = checkpoint
            return execute(**kwargs)

        self.github.add_run(20, DEPLOYABLE)
        self.request()
        with (
            patch.object(
                self.store, "list_runtime_key_safety_policy_records", side_effect=policies
            ),
            patch.object(
                self.application_inputs_provider, "execute_artifact_deploy", side_effect=apply
            ),
        ):
            refused = self.run_once()
        self.assertEqual(refused.state, "failed")
        self.assertFalse(triggered)
        self.assertEqual(bool(self.store.list_held_provider_target_reservations()), after_readback)
        self.assertEqual(
            "APP_TOKEN" in str(payload["env"]),
            after_readback and interruption_phase != "provider_dispatch_prepared",
        )
        if not after_readback:
            self.assertEqual(refused.last_plan["deploy_operation_status"], "refused")
        else:
            held = self.store.list_held_provider_target_reservations()
            self.assertEqual(
                held[0].provider_effect_phase,
                "application_schedule_quiesce"
                if interruption_phase == "provider_dispatch_prepared"
                else "runtime_inputs_verified",
            )
            original = held[0]
            if interruption_phase:
                from control_plane.workflows.generic_web_application_snapshot import (
                    recorded_application_lifecycle,
                )

                before = self.store.list_deployment_records(
                    context_name="cm", instance_name="testing"
                )[0]
                baseline = recorded_application_lifecycle(before)
                assert baseline is not None
                self.assertEqual(baseline.originals["cleanup"], original_schedule)
                self.assertFalse(self.input_schedule["enabled"])
                self.request()
                with (
                    patch.object(
                        self.store, "list_runtime_key_safety_policy_records", return_value=()
                    ),
                    patch.object(
                        self.application_inputs_provider,
                        "observe_artifact_deploy",
                        return_value=GenericWebProviderDeploymentObservation(outcome="absent"),
                    ),
                ):
                    still_refused = self.run_once()
                self.assertEqual(still_refused.state, "failed")
                self.assertEqual(
                    self.store.list_held_provider_target_reservations()[0].record_id,
                    original.record_id,
                )
                retained = self.store.read_deployment_record(before.record_id)
                self.assertEqual(retained.runtime_source, before.runtime_source)
                self.assertEqual(recorded_application_lifecycle(retained), baseline)
            self.request()
            with patch.object(
                self.application_inputs_provider,
                "observe_artifact_deploy",
                side_effect=[
                    GenericWebProviderDeploymentObservation(outcome="absent"),
                    GenericWebProviderDeploymentObservation(
                        outcome="present",
                        deployment_status="success",
                        deployment_id="provider-input-retry",
                        started_at="2026-10-19T00:00:00Z",
                        finished_at="2026-10-19T00:01:00Z",
                    ),
                ],
            ):
                recovered = self.reconcile()
            self.assertEqual(recovered["action"], "deploy")
            self.assertTrue(triggered)
            self.assertFalse(self.store.list_held_provider_target_reservations())
            completed = self.store.read_idempotency_record(
                scope=original.scope,
                route_path=original.route_path,
                idempotency_key=original.idempotency_key,
            )
            assert completed is not None
            self.assertEqual(completed.record_id, original.record_id)
            self.assertEqual(completed.state, "completed")
            if interruption_phase:
                record = self.store.read_deployment_record(before.record_id)
                receipt = recorded_application_lifecycle(record)
                assert receipt is not None
                self.assertEqual(receipt.originals["cleanup"], original_schedule)
                self.assertEqual(receipt.state, "qualified")
                self.assertTrue(self.input_schedule["enabled"])

    def test_runtime_policy_revocation_before_effect_is_a_normal_refusal(self) -> None:
        self.assert_runtime_policy_revocation(after_readback=False)

    def test_runtime_policy_revocation_after_env_write_retains_original_fence(self) -> None:
        self.assert_runtime_policy_revocation(after_readback=True)

    def test_readback_interruption_then_input_refusal_retains_original_receipts(self) -> None:
        self.assert_runtime_policy_revocation(
            after_readback=True, interruption_phase="runtime_inputs_verified"
        )

    def test_quiesce_interruption_then_input_refusal_can_resume_original_lifecycle(self) -> None:
        self.assert_runtime_policy_revocation(
            after_readback=True, interruption_phase="provider_dispatch_prepared"
        )

    def test_input_change_before_effect_is_a_normal_refusal(self) -> None:
        _payload, triggered = self.application_input_provider()
        settings = RuntimeEnvironmentRecord(
            scope="instance",
            context="cm",
            instance="testing",
            env={"APP_SETTING": "first"},
            updated_at="2026-10-19T00:00:00Z",
        )
        self.store.write_runtime_environment_record(settings)
        execute = self.application_inputs_provider.execute_artifact_deploy

        def apply(**kwargs: Any) -> Any:
            original_checkpoint = kwargs["before_provider_mutation"]

            def checkpoint(phase: str) -> None:
                original_checkpoint(phase)
                if phase == "provider_dispatch_prepared":
                    self.store.write_runtime_environment_record(
                        settings.model_copy(update={"env": {"APP_SETTING": "second"}})
                    )

            kwargs["before_provider_mutation"] = checkpoint
            return execute(**kwargs)

        self.github.add_run(20, DEPLOYABLE)
        self.request()
        with patch.object(
            self.application_inputs_provider, "execute_artifact_deploy", side_effect=apply
        ):
            refused = self.run_once()
        self.assertEqual(refused.state, "failed")
        self.assertFalse(triggered)
        self.assertFalse(self.store.list_held_provider_target_reservations())
        self.assertEqual(refused.last_plan["deploy_operation_status"], "refused")

    def test_hold_set_during_adapter_preparation_blocks_dispatch_under_execution_lock(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        resolve = self.deploys.resolve_deploy_target

        def prepare_then_hold(**kwargs: Any) -> Any:
            target = resolve(**kwargs)
            self.hold_testing()
            return target

        with patch.object(self.deploys, "resolve_deploy_target", side_effect=prepare_then_hold):
            plan = self.reconcile()
        self.assertEqual(self.deploys.runtime_identities, [])
        self.assertEqual(plan["reason"], "staff_testing")
        self.assertFalse(self.store.list_held_provider_target_reservations())

    def test_declared_maintenance_changes_testing_retry_authority(self) -> None:
        from control_plane.contracts.generic_web_extensions import (
            GenericWebLaneExtensions,
            GenericWebMaintenanceCommand,
        )
        from control_plane.product_reconcile import _generic_web_testing_authority

        lane = self.store.read_product_profile_record("site").lanes[0]
        before = _generic_web_testing_authority(record_store=self.store, lane=lane)
        changed = lane.model_copy(
            update={
                "generic_web_extensions": GenericWebLaneExtensions(
                    maintenance=(GenericWebMaintenanceCommand(name="migrate", command="./migrate"),)
                )
            }
        )
        after = _generic_web_testing_authority(record_store=self.store, lane=changed)
        self.assertNotEqual(before, after)
        self.assertEqual(
            after, _generic_web_testing_authority(record_store=self.store, lane=changed)
        )

    def prepare_unqualified_verireel_testing(self) -> str:
        self.github.add_run(20, DEPLOYABLE)
        self.github.add_run(30, OLDER)
        self.request()
        plan = self.reconcile()
        self.store.write_product_profile_record(
            _generic_web_profile().model_copy(
                update={
                    "lanes": tuple(
                        lane.model_copy(
                            update={
                                "generic_web_extensions": GenericWebLaneExtensions(
                                    verification_extension="verireel-testing"
                                )
                            }
                        )
                        if lane.instance == "testing"
                        else lane
                        for lane in _generic_web_profile().lanes
                    )
                }
            )
        )
        return cast(str, plan["deployment_record_id"])

    def test_fresh_image_only_native_settlement_permits_event_successor(self) -> None:
        from control_plane.provider_operations import ProviderMutationUnknownError

        self.github.add_run(20, DEPLOYABLE)
        self.request()
        with patch(
            "control_plane.workflows.generic_web_deploy.integration_key_readback",
            side_effect=ProviderMutationUnknownError(
                "Interrupted after original image observation."
            ),
        ):
            self.assertEqual(self.run_once().state, "failed")
        (first_original,) = self.store.list_held_provider_target_reservations()
        self.assertEqual(len(self.deploys.runtime_identities), 1)
        settled = self.settle_terminal_testing_original()
        self.assertEqual(settled.post_deploy_update.status, "fail")
        with self.assertRaises(FileNotFoundError):
            self.store.read_environment_inventory(context_name="cm", instance_name="testing")

        self.request()
        with patch(
            "control_plane.workflows.generic_web_deploy.integration_key_readback",
            side_effect=ProviderMutationUnknownError(
                "Interrupted successor needs its own recovery."
            ),
        ):
            self.assertEqual(self.run_once().state, "failed")
        (second_original,) = self.store.list_held_provider_target_reservations()
        self.assertNotEqual(second_original.idempotency_key, first_original.idempotency_key)
        self.assertEqual(len(self.deploys.runtime_identities), 2)
        self.request()
        self.assertEqual(self.run_once().state, "failed")
        self.assertEqual(len(self.deploys.runtime_identities), 2)
        (held_second,) = self.store.list_held_provider_target_reservations()
        self.assertEqual(held_second.record_id, second_original.record_id)

        self.settle_terminal_testing_original()
        self.request()
        successor = self.reconcile()
        self.assertEqual(successor["deploy_operation_status"], "completed")
        self.assertEqual(successor["deploy_status"], "pass")
        self.assertNotEqual(successor["deploy_idempotency_key"], second_original.idempotency_key)
        self.assertEqual(len(self.deploys.runtime_identities), 3)
        self.request()
        self.assertEqual(self.reconcile()["reason"], "already_deployed")
        self.assertEqual(len(self.deploys.runtime_identities), 3)

    def test_settled_image_only_successor_with_known_failure_replays_its_own_intent(self) -> None:
        from control_plane.provider_operations import ProviderMutationUnknownError
        from control_plane.workflows.generic_web_deploy_provider import (
            GenericWebProviderDeploymentFailed,
        )
        from control_plane.generic_web_testing_settlement import testing_attempt_was_settled

        self.github.add_run(20, DEPLOYABLE)
        self.request()
        with patch(
            "control_plane.workflows.generic_web_deploy.integration_key_readback",
            side_effect=ProviderMutationUnknownError(
                "Interrupted after original image observation."
            ),
        ):
            self.assertEqual(self.run_once().state, "failed")
        (original,) = self.store.list_held_provider_target_reservations()
        self.settle_terminal_testing_original()
        self.deploys.deploy_error = GenericWebProviderDeploymentFailed(
            self.deploys.observation.model_copy(update={"deployment_status": "error"})
        )
        self.request()
        failed = self.run_once()
        key = failed.last_plan["deploy_idempotency_key"]
        assert isinstance(key, str)
        self.assertNotEqual(key, original.idempotency_key)
        self.assertEqual(failed.last_plan["deploy_status"], "fail")
        self.assertEqual(len(self.deploys.runtime_identities), 2)
        result = self.store.read_idempotency_record(
            scope=original.scope,
            route_path=original.route_path,
            idempotency_key=key,
        )
        assert result is not None
        self.assertFalse(testing_attempt_was_settled(result))
        for _ in range(2):
            self.request()
            repeated = self.run_once()
            self.assertEqual(repeated.last_plan["deploy_operation_status"], "replayed")
            self.assertEqual(repeated.last_plan["deploy_idempotency_key"], key)
            self.assertEqual(len(self.deploys.runtime_identities), 2)
        self.assertFalse(self.store.list_held_provider_target_reservations())

    def test_pending_generic_web_qualification_surfaces_original_unknown_fence(self) -> None:
        from control_plane.provider_operations import ProviderMutationUnknownError

        self.github.add_run(20, DEPLOYABLE)
        self.request()
        self.reconcile()
        self.github.add_run(40, NEWEST)
        with patch(
            "control_plane.generic_web_deploy_http._generic_web_post_deploy_executor_for_profile"
        ) as selector:
            executor = selector.return_value
            executor.side_effect = ProviderMutationUnknownError("Product response was lost.")
            self.request()
            first = self.run_once()
            current = self.store.read_environment_inventory(
                context_name="cm", instance_name="testing"
            )
            self.assertEqual(current.post_deploy_update.status, "pending")
            self.request()
            repeated = self.run_once()
        self.assertEqual(first.state, "failed")
        self.assertEqual(repeated.state, "failed")
        self.assertEqual(repeated.last_plan["reason"], "testing_qualification_pending")
        self.assertTrue(repeated.last_error)
        executor.assert_called_once()

    @staticmethod
    def _testing_qualification_evidence(*args: object) -> object:
        from control_plane.contracts.promotion_record import PostDeployUpdateEvidence
        from control_plane.workflows.generic_web_deploy import (
            GenericWebPostDeployContext,
            GenericWebDeployStore,
        )

        context = cast(GenericWebPostDeployContext, args[2])
        from control_plane.workflows.verireel_testing_post_deploy import (
            testing_qualification_authority,
        )

        authority = testing_qualification_authority(cast(GenericWebDeployStore, args[1]), context)
        return PostDeployUpdateEvidence(
            attempted=True,
            status="pass",
            evidence={
                "authority_digest": authority,
                "driver_id": "generic-web",
                "extension_id": "verireel-testing",
                "deployment_record_id": context.deployment_record_id,
                "artifact_id": context.artifact_id,
                "source_git_ref": context.source_git_ref,
                "migration_status": "pass",
                "creator_verification_status": "pass",
                "admin_verification_status": "pass",
                "cleanup_status": "pass",
            },
        )

    def test_verireel_unqualified_image_uses_existing_deploy_once_then_stops(self) -> None:
        deployment_id = self.prepare_unqualified_verireel_testing()
        with patch(
            "control_plane.workflows.verireel_testing_post_deploy.execute_verireel_testing_post_deploy",
            side_effect=self._testing_qualification_evidence,
        ) as qualify:
            self.request()
            first = self.reconcile()
            self.request()
            repeated = self.reconcile()
        self.assertEqual(first["action"], "deploy")
        self.assertEqual(first["post_deploy_status"], "pass")
        self.assertNotEqual(first["deployment_record_id"], deployment_id)
        self.assertEqual(repeated["reason"], "already_deployed")
        qualify.assert_called_once()
        self.assertEqual(len(self.deploys.runtime_identities), 2)
        record = self.store.read_deployment_record(cast(str, first["deployment_record_id"]))
        self.assertEqual(record.post_deploy_update.evidence["source_git_ref"], DEPLOYABLE)
        inventory = self.store.read_environment_inventory(
            context_name="cm", instance_name="testing"
        )
        self.assertEqual(inventory.post_deploy_update, record.post_deploy_update)

    def test_verireel_failed_qualification_is_not_repeated_or_called_healthy(self) -> None:
        from control_plane.contracts.promotion_record import PostDeployUpdateEvidence

        self.prepare_unqualified_verireel_testing()

        def failure(*args: object) -> PostDeployUpdateEvidence:
            result = cast(PostDeployUpdateEvidence, self._testing_qualification_evidence(*args))
            result.evidence["creator_verification_status"] = "fail"
            return result.model_copy(update={"status": "fail"})

        with patch(
            "control_plane.workflows.verireel_testing_post_deploy.execute_verireel_testing_post_deploy",
            side_effect=failure,
        ) as qualify:
            self.request()
            first = self.run_once()
            self.request()
            repeated = self.run_once()
        self.assertEqual(first.state, "failed")
        self.assertEqual(repeated.last_plan["reason"], "testing_qualification_failed")
        self.assertEqual(repeated.state, "failed")
        qualify.assert_called_once()
        self.assertEqual(len(self.deploys.runtime_identities), 2)

    def test_verireel_qualification_respects_staff_testing_hold(self) -> None:
        self.prepare_unqualified_verireel_testing()
        self.store.write_dokploy_target_record(
            DokployTargetRecord(
                context="cm",
                instance="testing",
                target_type="application",
                target_name="site-testing-app",
                updated_at="2026-10-12T00:00:00Z",
                policies=DokployTargetPolicies(
                    staff_testing_hold=DokployTargetStaffTestingHold(
                        reason="staff test",
                        recorded_by="director",
                        recorded_at="2026-10-12T00:00:00Z",
                    )
                ),
            )
        )
        self.request()
        with patch(
            "control_plane.workflows.verireel_testing_post_deploy.execute_verireel_testing_post_deploy"
        ) as qualify:
            plan = self.reconcile()
        self.assertEqual(plan["reason"], "staff_testing")
        qualify.assert_not_called()

    def test_verireel_failure_before_authority_snapshot_has_bounded_retries(self) -> None:
        from control_plane.workflows.verireel_testing_post_deploy import (
            MAX_TRANSIENT_QUALIFICATION_ATTEMPTS,
        )

        self.prepare_unqualified_verireel_testing()
        with patch(
            "control_plane.workflows.verireel_testing_post_deploy.testing_qualification_authority",
            side_effect=FileNotFoundError("authority temporarily unavailable"),
        ) as authority:
            for _ in range(MAX_TRANSIENT_QUALIFICATION_ATTEMPTS):
                self.request()
                self.assertEqual(self.run_once().state, "failed")
            self.request()
            final = self.run_once()
        self.assertEqual(final.last_plan["reason"], "testing_qualification_failed")
        self.assertEqual(authority.call_count, MAX_TRANSIENT_QUALIFICATION_ATTEMPTS)

    def test_verireel_transient_qualification_failure_has_bounded_retries(self) -> None:
        from control_plane.contracts.promotion_record import PostDeployUpdateEvidence
        from control_plane.workflows.generic_web_deploy import GenericWebPostDeployContext
        from control_plane.workflows.verireel_testing_post_deploy import (
            MAX_TRANSIENT_QUALIFICATION_ATTEMPTS,
            previous_transient_attempts,
        )

        self.prepare_unqualified_verireel_testing()

        def transient(*args: object) -> PostDeployUpdateEvidence:
            context = cast(GenericWebPostDeployContext, args[2])
            result = cast(PostDeployUpdateEvidence, self._testing_qualification_evidence(*args))
            result.evidence.update(
                failure_kind="transient",
                transient_attempts=str(previous_transient_attempts(context) + 1),
                creator_verification_status="fail",
            )
            return result.model_copy(update={"status": "fail"})

        with patch(
            "control_plane.workflows.verireel_testing_post_deploy.execute_verireel_testing_post_deploy",
            side_effect=transient,
        ) as qualify:
            for _ in range(MAX_TRANSIENT_QUALIFICATION_ATTEMPTS):
                self.request()
                self.assertEqual(self.run_once().state, "failed")
            self.request()
            final = self.run_once()
        self.assertEqual(final.last_plan["reason"], "testing_qualification_failed")
        self.assertEqual(qualify.call_count, MAX_TRANSIENT_QUALIFICATION_ATTEMPTS)

    def test_verireel_configuration_repair_permits_a_failed_candidate_to_qualify(self) -> None:
        from control_plane.contracts.promotion_record import PostDeployUpdateEvidence

        self.prepare_unqualified_verireel_testing()
        self.application_input_provider()

        def failure(*args: object) -> PostDeployUpdateEvidence:
            result = cast(PostDeployUpdateEvidence, self._testing_qualification_evidence(*args))
            result.evidence["creator_verification_status"] = "fail"
            return result.model_copy(update={"status": "fail"})

        with patch(
            "control_plane.workflows.verireel_testing_post_deploy.execute_verireel_testing_post_deploy",
            side_effect=failure,
        ):
            self.request()
            self.assertEqual(self.run_once().state, "failed")
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="cm",
                instance="testing",
                env={"SYNC_INTERVAL_SECONDS": 1800},
                updated_at="2026-10-12T00:00:00Z",
            )
        )
        with patch(
            "control_plane.workflows.verireel_testing_post_deploy.execute_verireel_testing_post_deploy",
            side_effect=self._testing_qualification_evidence,
        ) as qualify:
            self.request()
            self.assertEqual(self.reconcile()["post_deploy_status"], "pass")
            self.request()
            self.assertEqual(self.reconcile()["reason"], "already_deployed")
        qualify.assert_called_once()

    def test_unknown_declared_qualification_stays_fenced_without_repeating_post_deploy(
        self,
    ) -> None:
        from control_plane.provider_operations import ProviderMutationUnknownError

        self.prepare_unqualified_verireel_testing()
        with patch(
            "control_plane.workflows.verireel_testing_post_deploy.execute_verireel_testing_post_deploy",
            side_effect=ProviderMutationUnknownError("lost fence"),
        ) as qualify:
            self.request()
            self.assertEqual(self.run_once().state, "failed")
            self.request()
            repeated = self.run_once()
        self.assertEqual(repeated.last_plan["deploy_operation_status"], "reconcile_required")
        self.assertEqual(repeated.state, "failed")
        self.assertEqual(len(self.store.list_held_provider_target_reservations()), 1)
        qualify.assert_called_once()

    def test_declared_generic_web_profile_requalifies_then_reuses_complete_evidence(self) -> None:
        from control_plane.contracts.generic_web_extensions import GenericWebLaneExtensions

        self.prepare_unqualified_verireel_testing()
        profile = self.store.read_product_profile_record("site")
        profile = profile.model_copy(
            update={
                "driver_id": "generic-web",
                "lanes": tuple(
                    lane.model_copy(
                        update={
                            "generic_web_extensions": GenericWebLaneExtensions(
                                verification_extension="verireel-testing"
                            )
                        }
                    )
                    if lane.instance == "testing"
                    else lane
                    for lane in profile.lanes
                ),
            }
        )
        self.store.write_product_profile_record(profile)
        with patch(
            "control_plane.workflows.verireel_testing_post_deploy.execute_verireel_testing_post_deploy",
            side_effect=self._testing_qualification_evidence,
        ) as qualify:
            self.request()
            first = self.run_once()
            self.request()
            repeated = self.run_once()
        self.assertEqual(first.state, "done")
        self.assertEqual(repeated.last_plan["reason"], "already_deployed")
        qualify.assert_called_once()

    def test_a_runtime_change_during_qualification_cannot_credit_new_authority(
        self,
    ) -> None:

        self.prepare_unqualified_verireel_testing()
        self.application_input_provider()
        transitioned = False

        def qualify_then_transition(*args: object) -> object:
            nonlocal transitioned
            if not transitioned:
                transitioned = True
                profile = self.store.read_product_profile_record("site")
                lane = next(lane for lane in profile.lanes if lane.instance == "testing")
                self.store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="instance",
                        context=lane.context,
                        instance=lane.instance,
                        env={"SYNC_INTERVAL_SECONDS": 1800},
                        updated_at=profile.updated_at,
                    )
                )
            return self._testing_qualification_evidence(*args)

        with patch(
            "control_plane.workflows.verireel_testing_post_deploy.execute_verireel_testing_post_deploy",
            side_effect=qualify_then_transition,
        ) as qualify:
            self.request()
            before_transition = self.reconcile()
            self.request()
            after_transition = self.reconcile()
            self.request()
            repeated = self.reconcile()
        self.assertEqual(after_transition["action"], "deploy")
        self.assertNotEqual(
            before_transition["deployment_record_id"], after_transition["deployment_record_id"]
        )
        self.assertEqual(after_transition["post_deploy_status"], "pass")
        self.assertEqual(repeated["reason"], "already_deployed")
        self.assertEqual(qualify.call_count, 2)

    def test_a_changed_runtime_input_requalifies_previously_passing_testing(self) -> None:
        self.prepare_unqualified_verireel_testing()
        self.application_input_provider()
        with patch(
            "control_plane.workflows.verireel_testing_post_deploy.execute_verireel_testing_post_deploy",
            side_effect=self._testing_qualification_evidence,
        ) as qualify:
            self.request()
            previous = self.reconcile()
            profile = self.store.read_product_profile_record("site")
            lane = next(lane for lane in profile.lanes if lane.instance == "testing")
            self.store.write_runtime_environment_record(
                RuntimeEnvironmentRecord(
                    scope="instance",
                    context=lane.context,
                    instance=lane.instance,
                    env={"SYNC_INTERVAL_SECONDS": 1800},
                    updated_at=profile.updated_at,
                )
            )
            self.request()
            changed = self.reconcile()
            self.request()
            repeated = self.reconcile()
        self.assertEqual(changed["action"], "deploy")
        self.assertNotEqual(previous["deployment_record_id"], changed["deployment_record_id"])
        self.assertEqual(changed["post_deploy_status"], "pass")
        self.assertEqual(repeated["reason"], "already_deployed")
        self.assertEqual(qualify.call_count, 2)

    def setUp(self) -> None:
        super().setUp()
        encryption_key = patch.dict(
            os.environ,
            {
                "LAUNCHPLANE_MASTER_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                "LAUNCHPLANE_SECRET_KEYS_JSON": "",
            },
        )
        encryption_key.start()
        self.addCleanup(encryption_key.stop)
        self.store.write_product_profile_record(_generic_web_profile())
        self.github = FakeGenericWebGitHub()
        self.deploys = _FakeGenericWebDeployProvider(
            observation=GenericWebProviderDeploymentObservation(
                outcome="present",
                deployment_status="success",
                deployment_id="deployment-provider-1",
                started_at="2026-10-02T12:00:00Z",
                finished_at="2026-10-02T12:01:00Z",
            ),
            target_name="site-testing-app",
        )
        self.testing_hooks = TestingProviderHooks(generic_web_deploy_provider=lambda: self.deploys)

        self.store.write_dokploy_target_record(
            DokployTargetRecord(
                context="cm",
                instance="testing",
                target_type="application",
                target_name=self.deploys.target_name,
                updated_at=self.deploys.observation.started_at,
            )
        )
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context="cm",
                instance="testing",
                target_id=self.deploys.target_id,
                updated_at=self.deploys.observation.started_at,
            )
        )
        self.store.write_provider_target_record(
            ProviderTargetRecord(
                context="cm",
                instance="testing",
                provider_id=self.deploys.provider_id,
                target_category="application",
                provider_target_type="application",
                target_id=self.deploys.target_id,
                display_name=self.deploys.target_name,
                updated_at=self.deploys.observation.started_at,
            )
        )

    def record_legacy_baseline(self, commit: str = OLDER, *, sha_tagged: bool = False) -> None:
        identity = RuntimeIdentity(
            context="cm",
            instance="testing",
            deployment_record_id="deployment-legacy",
            artifact_id=f"{IMAGE_REPOSITORY}@{_digest(commit)}" if sha_tagged else f"sha-{commit}",
            source_git_ref=commit,
            image_reference=(
                f"{IMAGE_REPOSITORY}:sha-{commit}"
                if sha_tagged
                else f"{IMAGE_REPOSITORY}@{_digest(commit)}"
            ),
        )
        self.store.write_environment_inventory(
            EnvironmentInventory(
                context=identity.context,
                instance=identity.instance,
                deployment_record_id=identity.deployment_record_id,
                source_git_ref=commit,
                runtime_identity=identity,
                deploy=DeploymentEvidence(
                    target_name="site-testing-app",
                    target_type="application",
                    deploy_mode="image",
                    status="pass",
                ),
                updated_at="2026-10-05T12:00:00Z",
            )
        )

    def test_sellyouroutboard_legacy_baseline_moves_to_verified_forward_build(self) -> None:
        # #3141: the deployed baseline predates push Build/manifest adoption.
        baseline = "206aff0581fa6fc817a30666094af4c738881a01"
        candidate = "50e779ca85ab6812a82dcd476fbb02cfccca9ec1"
        self.github.first_parents = {candidate: baseline, baseline: ""}
        self.record_legacy_baseline(baseline, sha_tagged=True)
        self.github.add_run(37825455941, candidate)
        self.github.runs[37825455941]["run_started_at"] = "2026-10-08T18:35:20Z"
        self.request()

        provider = DokployGenericWebDeployProvider()

        def execute(**kwargs: Any) -> GenericWebProviderDeploymentObservation | None:
            self.deploys.application_runtime = converged_application_runtime(
                kwargs["resolved_deploy_target"], kwargs["runtime_identity"]
            )
            return provider.execute_artifact_deploy(**kwargs)

        with (
            patch.object(
                provider, "_read_provider_config", return_value=("https://provider.test", "test")
            ),
            patch.object(self.deploys, "execute_artifact_deploy", side_effect=execute),
            patch("control_plane.dokploy.api.latest_deployment_for_target", return_value={}),
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload", return_value={"env": ""}
            ),
            patch("control_plane.dokploy.api.update_dokploy_target_env"),
            patch("control_plane.dokploy.api.dokploy_request") as provider_write,
            patch("control_plane.dokploy.api.trigger_deployment"),
            patch("control_plane.dokploy.api.wait_for_target_deployment"),
        ):
            plan = self.reconcile()

        self.assertEqual(plan["action"], "deploy", plan)
        self.assertEqual(plan["deploy_status"], "pass")
        self.assertEqual(plan["desired_commit"], candidate)
        self.assertTrue(plan["current_commit_seen"])
        self.assertEqual(
            provider_write.call_args.kwargs["payload"]["dockerImage"],
            f"{IMAGE_REPOSITORY}:sha-{candidate}",
        )
        inventory = self.store.read_environment_inventory(
            context_name="cm", instance_name="testing"
        )
        assert inventory.runtime_identity is not None
        self.assertEqual(inventory.runtime_identity.source_git_ref, candidate)
        self.assertEqual(
            inventory.runtime_identity.artifact_id, f"{IMAGE_REPOSITORY}@{_digest(candidate)}"
        )
        self.assertEqual(
            inventory.runtime_identity.image_reference, f"{IMAGE_REPOSITORY}:sha-{candidate}"
        )

    def test_legacy_baseline_selection_covers_native_verireel_and_generic_web(self) -> None:
        self.record_legacy_baseline()
        self.github.add_run(20, DEPLOYABLE)
        self.github.runs[20]["run_started_at"] = "2026-10-08T12:00:00Z"
        for driver_id in ("generic-web",):
            with self.subTest(driver_id=driver_id):
                plan, desired = _plan_testing_target(
                    record_store=self.store,
                    profile=_generic_web_profile().model_copy(update={"driver_id": driver_id}),
                    repository_id=REPOSITORY_ID,
                    transport=self.github,
                )
                self.assertEqual(plan["action"], "deploy", plan)
                self.assertIsNotNone(desired)
        self.assertEqual(self.deploys.runtime_identities, [])

    def test_legacy_baseline_still_requires_a_complete_build_inventory(self) -> None:
        self.record_legacy_baseline()
        self.github.add_run(20, DEPLOYABLE)
        self.github.runs[20]["run_started_at"] = "2026-10-08T12:00:00Z"
        get_json = self.github.get_json
        for total, runs in (
            (2, [self.github.runs[20]]),
            (None, [self.github.runs[20]]),
            (True, [self.github.runs[20]]),
            (2, [self.github.runs[20], self.github.runs[20]]),
        ):
            with self.subTest(total=total, returned=len(runs)):

                def incomplete(path: str) -> object:
                    if "/actions/workflows/build.yml/runs?" in path:
                        return {"total_count": total, "workflow_runs": runs}
                    return get_json(path)

                self.request()
                with patch.object(self.github, "get_json", incomplete):
                    plan = self.reconcile()
                self.assertEqual(
                    (plan["action"], plan["reason"]), ("none", "incomplete_build_runs")
                )
                self.assertTrue(plan["held"])
                self.assertEqual(self.deploys.runtime_identities, [])

    def test_legacy_baseline_with_no_successful_builds_reports_no_verified_build(self) -> None:
        self.record_legacy_baseline()
        self.request()

        plan = self.reconcile()

        self.assertEqual((plan["action"], plan["reason"]), ("none", "no_verified_build"))
        self.assertFalse(plan["held"])
        self.assertEqual(self.deploys.runtime_identities, [])

    def test_legacy_baseline_still_requires_first_parent_history_to_reach_it(self) -> None:
        self.record_legacy_baseline()
        self.github.add_run(20, DEPLOYABLE)
        self.github.runs[20]["run_started_at"] = "2026-10-08T12:00:00Z"
        self.github.first_parents = {NEWEST: DEPLOYABLE, DEPLOYABLE: OLDER}
        self.request()

        plan = self.reconcile()

        self.assertEqual((plan["action"], plan["reason"]), ("none", "incomplete_commit_history"))
        self.assertEqual(self.deploys.runtime_identities, [])

    def test_legacy_baseline_does_not_admit_an_unverified_candidate_manifest(self) -> None:
        self.record_legacy_baseline()
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        with patch.object(
            self.github,
            "get_bytes",
            side_effect=BuildProvenanceError("Manifest unavailable", code="github_read_failed"),
        ):
            plan = self.reconcile()
        self.assertEqual((plan["action"], plan["reason"]), ("none", "no_verified_build"))
        self.assertEqual(
            plan["rejected_builds"],
            [
                {
                    "commit": DEPLOYABLE,
                    "run_id": 20,
                    "code": "github_read_failed",
                    "error": "Manifest unavailable",
                }
            ],
        )
        self.assertEqual(self.deploys.runtime_identities, [])

    def test_legacy_baseline_does_not_admit_older_source_or_builds(self) -> None:
        for current, candidate, started, reason in (
            (DEPLOYABLE, OLDER, "2026-10-08T12:00:00Z", "ancestor_build"),
            (OLDER, DEPLOYABLE, "2026-10-04T12:00:00Z", "build_order_unverified"),
        ):
            with self.subTest(reason=reason):
                self.record_legacy_baseline(current)
                self.github.runs.clear()
                self.github.add_run(20, candidate)
                self.github.runs[20]["run_started_at"] = started
                self.request()

                plan = self.reconcile()

                self.assertEqual((plan["action"], plan["reason"]), ("none", reason))
                self.assertTrue(plan["held"])
                self.assertEqual(self.deploys.runtime_identities, [])

    def test_legacy_baseline_without_a_recorded_digest_stays_held(self) -> None:
        self.record_legacy_baseline()
        inventory = self.store.read_environment_inventory(
            context_name="cm", instance_name="testing"
        )
        assert inventory.runtime_identity is not None
        self.store.write_environment_inventory(
            inventory.model_copy(
                update={
                    "runtime_identity": inventory.runtime_identity.model_copy(
                        update={"image_reference": ""}
                    )
                }
            )
        )
        self.github.add_run(20, DEPLOYABLE)
        self.request()

        plan = self.reconcile()

        self.assertEqual((plan["action"], plan["reason"]), ("none", "incomplete_build_runs"))
        self.assertEqual(self.deploys.runtime_identities, [])

    def failed_deploy(self) -> ProductReconcileRequestRecord:
        from control_plane.workflows.generic_web_deploy_provider import (
            GenericWebProviderDeploymentFailed,
        )

        self.github.add_run(20, DEPLOYABLE)
        self.deploys.observation = self.deploys.observation.model_copy(
            update={"deployment_status": "error"}
        )
        self.deploys.deploy_error = GenericWebProviderDeploymentFailed(self.deploys.observation)
        self.request()
        self.run_once()
        # A failed provider effect is held until its terminal failure is observed.
        self.request()
        failed = self.run_once()
        self.assertEqual(failed.state, "failed")
        self.assertEqual(failed.last_plan["deploy_status"], "fail")
        return failed

    def retry_after_lane_change(self, failed: ProductReconcileRequestRecord) -> None:
        self.deploys.deploy_error = None
        self.deploys.observation = self.deploys.observation.model_copy(
            update={"deployment_status": "success"}
        )
        self.request()
        retried = self.run_once()
        self.assertEqual(retried.state, "done")
        self.assertEqual(retried.last_plan["deploy_status"], "pass")
        self.assertNotEqual(
            retried.last_plan["deploy_idempotency_key"], failed.last_plan["deploy_idempotency_key"]
        )
        self.assertEqual(len(self.deploys.runtime_identities), 2)
        self.assertEqual(
            self.deploys.runtime_identities[0].artifact_id,
            self.deploys.runtime_identities[1].artifact_id,
        )

    def test_failed_deploy_replays_until_its_target_source_changes(self) -> None:
        self.store.write_dokploy_target_record(
            self.store.read_dokploy_target_record(
                context_name="cm", instance_name="testing"
            ).model_copy(update={"updated_at": "2026-10-02T00:00:00Z"})
        )
        failed = self.failed_deploy()
        scan_effect = self.artifact_scanner.side_effect
        self.artifact_scanner.side_effect = AssertionError(
            "A retained deploy must replay without a scan"
        )
        self.request()
        repeated = self.run_once()
        self.assertEqual(repeated.last_plan["deploy_operation_status"], "replayed")
        self.assertEqual(len(self.deploys.runtime_identities), 1)
        self.artifact_scanner.side_effect = scan_effect

        target = self.store.read_dokploy_target_record(context_name="cm", instance_name="testing")
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={
                    "custom_git_url": "https://example.test/site.git",
                    "custom_git_branch": "main",
                    "compose_path": "./compose.yml",
                    # Writers use second-resolution timestamps; content still differs.
                    "updated_at": target.updated_at,
                }
            )
        )
        self.retry_after_lane_change(failed)

    def retry_after_settings_change(
        self, scope: Literal["global", "context", "instance"], context: str, instance: str
    ) -> None:
        settings = RuntimeEnvironmentRecord(
            scope=scope,
            context=context,
            instance=instance,
            env={"SYNC_INTERVAL_SECONDS": 1800},
            updated_at="2026-10-02T00:00:00Z",
        )
        self.store.write_runtime_environment_record(settings)
        failed = self.failed_deploy()
        self.store.write_runtime_environment_record(
            settings.model_copy(
                update={
                    "env": {"SYNC_INTERVAL_SECONDS": 3600},
                    "updated_at": settings.updated_at,
                }
            )
        )
        self.retry_after_lane_change(failed)

    def test_failed_deploy_retries_after_global_settings_change(self) -> None:
        self.retry_after_settings_change("global", "", "")

    def test_failed_deploy_retries_after_provider_target_binding_repair(self) -> None:
        provider_target = ProviderTargetRecord(
            context="cm",
            instance="testing",
            provider_id=self.deploys.provider_id,
            target_category="service",
            target_id=self.deploys.target_id,
            display_name=self.deploys.target_name,
            provider_target_type="managed-service",
            updated_at="2026-10-02T00:00:00Z",
        )
        target_id = DokployTargetIdRecord(
            context="cm",
            instance="testing",
            target_id=self.deploys.target_id,
            updated_at="2026-10-02T00:00:00Z",
        )
        self.store.write_provider_target_record(provider_target)
        self.store.write_dokploy_target_id_record(target_id)
        failed = self.failed_deploy()
        self.deploys.target_id = "repaired-target"
        self.store.write_provider_target_record(
            provider_target.model_copy(
                update={
                    "target_id": self.deploys.target_id,
                    "target_category": "application",
                    "provider_target_type": "application",
                }
            )
        )
        self.store.write_dokploy_target_id_record(
            target_id.model_copy(update={"target_id": self.deploys.target_id})
        )
        self.retry_after_lane_change(failed)
        self.assertEqual(
            self.deploys.resolved_targets[-1].resolved_target.target_id, self.deploys.target_id
        )

    def test_missing_key_configuration_refuses_before_a_provider_effect(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        with patch.dict(
            os.environ,
            {"LAUNCHPLANE_MASTER_ENCRYPTION_KEY": "", "LAUNCHPLANE_SECRET_KEYS_JSON": ""},
        ):
            refused = self.run_once()
        self.assertEqual(refused.state, "failed")
        self.assertEqual(refused.last_plan["deploy_operation_status"], "refused")
        self.assertEqual(self.deploys.runtime_identities, [])

    def test_failed_deploy_retries_after_context_settings_change(self) -> None:
        self.retry_after_settings_change("context", "cm", "")

    def test_failed_deploy_retries_after_lane_settings_change(self) -> None:
        self.retry_after_settings_change("instance", "cm", "testing")

    def test_failed_deploy_retries_after_runtime_secret_rotation_without_reading_values(
        self,
    ) -> None:
        record = SecretRecord(
            secret_id="test-runtime-secret",
            scope="context_instance",
            context="cm",
            instance="testing",
            integration="runtime_environment",
            name="API_TOKEN",
            current_version_id="version-before",
            created_at="2026-10-02T00:00:00Z",
            updated_at="2026-10-02T00:00:00Z",
        )
        # A testing lane that receives managed keys deploys only under an active policy.
        self.store.write_runtime_key_safety_policy_record(
            RuntimeKeySafetyPolicyRecord(
                record_id="test-policy",
                source="test",
                updated_at=record.updated_at,
                rules=(RuntimeSecretSafetyRule(binding_key=record.name, secret_class="testing"),),
            )
        )
        self.store.write_secret_record(record)
        self.store.write_secret_binding(
            SecretBinding(
                binding_id="test-runtime-binding",
                secret_id=record.secret_id,
                integration=record.integration,
                context=record.context,
                instance=record.instance,
                binding_key=record.name,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
        )
        with patch.object(
            self.store, "read_secret_version", side_effect=AssertionError("value read")
        ):
            failed = self.failed_deploy()
            self.store.write_secret_record(
                record.model_copy(update={"current_version_id": "version-after"})
            )
            self.retry_after_lane_change(failed)

    def test_other_lane_settings_do_not_retry_a_failed_deploy(self) -> None:
        self.failed_deploy()
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="cm",
                instance="prod",
                env={"SYNC_INTERVAL_SECONDS": 1800},
                updated_at="2026-10-02T01:00:00Z",
            )
        )
        self.request()
        repeated = self.run_once()
        self.assertEqual(repeated.last_plan["deploy_operation_status"], "replayed")
        self.assertEqual(len(self.deploys.runtime_identities), 1)

    def test_audit_timestamp_changes_do_not_retry_identical_settings(self) -> None:
        settings = RuntimeEnvironmentRecord(
            scope="instance",
            context="cm",
            instance="testing",
            env={"SYNC_INTERVAL_SECONDS": 1800},
            updated_at="2026-10-02T00:00:00Z",
        )
        self.store.write_runtime_environment_record(settings)
        self.failed_deploy()
        self.store.write_runtime_environment_record(
            settings.model_copy(update={"updated_at": "2026-10-02T01:00:00Z"})
        )
        self.request()
        repeated = self.run_once()
        self.assertEqual(repeated.last_plan["deploy_operation_status"], "replayed")
        self.assertEqual(len(self.deploys.runtime_identities), 1)

    def test_a_merge_deploys_its_verified_image_to_testing_with_no_grant(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.github.add_run(30, OLDER)
        self.comments.merged[DEPLOYABLE] = 12
        self.request()

        plan = self.reconcile()

        image = f"{IMAGE_REPOSITORY}@{_digest(DEPLOYABLE)}"
        self.assertEqual(
            (plan["action"], plan["desired_commit"], plan["desired_artifact_id"]),
            ("deploy", DEPLOYABLE, image),
        )
        self.assertEqual(
            (plan["deploy_status"], plan["deploy_operation_status"]), ("pass", "completed")
        )
        (deployed,) = self.deploys.runtime_identities
        self.assertEqual(
            (deployed.instance, deployed.artifact_id, deployed.source_git_ref),
            ("testing", image, DEPLOYABLE),
        )
        inventory = self.store.read_environment_inventory(
            context_name="cm", instance_name="testing"
        )
        assert inventory.runtime_identity is not None
        self.assertEqual(
            inventory.runtime_identity.image_reference, f"{IMAGE_REPOSITORY}:sha-{DEPLOYABLE}"
        )
        # Nothing went to the Odoo artifact store or its operation queue.
        self.assertEqual(self.store.list_artifact_manifests(), ())
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), ())
        (comment,) = self.comments.on(12)
        self.assertIn("The testing lane runs this change.", cast(str, comment["body"]))
        view = product_reconcile_request_view(
            self.store.read_product_reconcile_request("site:testing")
        )
        self.assertEqual(view.last_plan["desired_image_digest"], _digest(DEPLOYABLE))

        self.request()
        repeated = self.reconcile()

        self.assertEqual((repeated["action"], repeated["reason"]), ("none", "already_deployed"))
        self.assertEqual(len(self.deploys.runtime_identities), 1)

    def test_testing_rolled_back_to_the_same_older_image_is_deployed_again(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.github.add_run(10, OLDER)
        self.request()
        self.reconcile()
        deployed = self.store.read_environment_inventory(context_name="cm", instance_name="testing")
        assert deployed.runtime_identity is not None
        older = f"{IMAGE_REPOSITORY}@{_digest(OLDER)}"
        # An admin rolls testing back; the rollback records a deployment of its own.
        self.store.write_environment_inventory(
            deployed.model_copy(
                update={
                    "deployment_record_id": "deployment-rollback",
                    "source_git_ref": OLDER,
                    "runtime_identity": deployed.runtime_identity.model_copy(
                        update={
                            "artifact_id": older,
                            "image_reference": older,
                            "source_git_ref": OLDER,
                        }
                    ),
                }
            )
        )
        self.request()

        redeployed = self.reconcile()

        self.assertEqual((redeployed["action"], redeployed["deploy_status"]), ("deploy", "pass"))
        self.assertEqual(len(self.deploys.runtime_identities), 2)
        self.assertTrue(
            cast(str, redeployed["deploy_idempotency_key"]).endswith(":from-deployment-rollback")
        )

    def test_a_refused_deploy_keeps_provider_text_out_of_the_error(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.comments.merged[DEPLOYABLE] = 12

        self.request()

        with patch.object(
            self.deploys,
            "resolve_deploy_target",
            side_effect=click.ClickException("target site-testing-app (id target-123) on host-7"),
        ):
            completed = self.run_once()

        self.assertEqual(completed.state, "failed")
        self.assertIn("refused before any provider change", completed.last_error)
        (comment,) = self.comments.on(12)
        for text in (completed.last_error, cast(str, comment["body"])):
            self.assertNotIn("host-7", text)
            self.assertNotIn("target-123", text)
        self.assertEqual(self.deploys.runtime_identities, [])

    def test_a_deploy_with_an_unknown_outcome_is_not_run_again(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.deploys.deploy_error = click.ClickException("provider timed out at host-7")
        self.deploys.observation = GenericWebProviderDeploymentObservation(outcome="unknown")
        self.request()

        first = self.run_once()
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="cm",
                instance="testing",
                env={"SYNC_INTERVAL_SECONDS": 3600},
                updated_at="2026-10-02T01:00:00Z",
            )
        )
        self.request()
        second = self.run_once()

        for completed in (first, second):
            self.assertEqual(completed.state, "failed")
            self.assertIn("generic-web deploy recovery", completed.last_error)
            self.assertNotIn("host-7", completed.last_error)
        self.assertEqual(len(self.deploys.runtime_identities), 1)
        from control_plane.event_deploy_recovery_reference import (
            resolve_event_deploy_recovery_coordinates,
        )
        from control_plane.http_app import idempotency_request_fingerprint

        coordinates = resolve_event_deploy_recovery_coordinates(
            store=self.store,
            product="site",
            context="cm",
            fingerprint=idempotency_request_fingerprint,
        )
        self.assertEqual(
            coordinates.reservation.idempotency_key, first.last_plan["deploy_idempotency_key"]
        )
        self.assertEqual(coordinates.original_deploy.deploy.source_git_ref, DEPLOYABLE)

    def unavailable_legacy_event(self, *, refuse: bool = False) -> tuple[dict[str, Any], Any]:
        from control_plane.event_testing_deploy import event_testing_deploy_request
        from control_plane.generic_web_deploy_provider_adapter import (
            GenericWebDeployProviderMutationAdapter,
        )
        from control_plane.workflows.generic_web_deploy_provider import (
            build_generic_web_provider_reconciliation_key,
        )

        profile = self.store.read_product_profile_record("site")
        lane = next(lane for lane in profile.lanes if lane.instance == "testing")
        self.store.write_dokploy_target_record(
            DokployTargetRecord(
                context=lane.context,
                instance="testing",
                target_type="application",
                target_name=self.deploys.target_name,
                updated_at="2026-10-01T00:00:00Z",
            )
        )
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=lane.context,
                instance="testing",
                target_id=self.deploys.target_id,
                updated_at="2026-10-01T00:00:00Z",
            )
        )
        self.store.write_provider_target_record(
            ProviderTargetRecord(
                context=lane.context,
                instance="testing",
                provider_id=self.deploys.provider_id,
                target_category="application",
                provider_target_type="application",
                target_id=self.deploys.target_id,
                display_name="testing",
                updated_at="2026-10-01T00:00:00Z",
            )
        )
        original = event_testing_deploy_request(
            product="site",
            image_reference=f"{IMAGE_REPOSITORY}@{_digest(OLDER)}",
            source_commit=OLDER,
        )
        adapter = GenericWebDeployProviderMutationAdapter(
            control_plane_root=self.root,
            record_store=self.store,
            deploy_request=original,
            profile=profile,
            lane=lane,
            trace_id="original",
            deploy_provider=self.deploys,
        )
        resolved = self.deploys.resolve_deploy_target(
            control_plane_root=self.root,
            request_artifact_id=original.deploy.artifact_id,
            request_source_git_ref=OLDER,
            request_timeout_seconds=600,
            request_no_cache=False,
            record_store=self.store,
            profile=profile,
            lane=lane,
            normalized_artifact_id=original.deploy.artifact_id,
            fallback_target_name="testing",
        )
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value="2000-01-01T00:00:00Z"
        ):
            reserved = self.store.reserve_mutation(
                scope=reconcile_reservation_scope("site"),
                route_path=GENERIC_WEB_DEPLOY_ROUTE,
                idempotency_key=f"{RECONCILE_SOURCE}:site:cm:testing:original",
                request_fingerprint=_fingerprint(original.model_dump(mode="json")),
                lease_owner="old-worker",
                reconciliation_key=build_generic_web_provider_reconciliation_key(
                    resolved, product="site"
                ),
                provider_target_key=adapter.target_key(),
                provider_target_origin=adapter.provider_target_origin(),
            ).record
            held = self.store.mark_mutation_reconcile_required(
                reservation=reserved, reconciliation_key=reserved.reconciliation_key
            ).record
            assert held is not None
        from control_plane.storage.postgres import (
            LaunchplaneIdempotencyRow,
            ORIGINAL_MUTATION_INTENT_ROUTE,
        )
        from sqlalchemy import delete

        with self.store._session_factory.begin() as session:
            session.execute(
                delete(LaunchplaneIdempotencyRow).where(
                    LaunchplaneIdempotencyRow.route_path == ORIGINAL_MUTATION_INTENT_ROUTE,
                    LaunchplaneIdempotencyRow.idempotency_key == reserved.record_id,
                )
            )

        self.github.add_run(20, DEPLOYABLE)
        self.request()
        with patch.object(
            self.deploys,
            "observe_testing_no_live_effect",
            create=True,
            return_value=None if refuse else {"state": "no_live_effect"},
        ) as inspection:
            completed = self.run_once()
            plan = completed.last_plan
        inspection.assert_called_once()
        if refuse:
            self.assertEqual(completed.state, "failed")
            self.assertIn(plan["testing_deploy_recovery_refusal"], completed.last_error)
        return plan, held

    def test_unavailable_legacy_event_with_no_live_effect_retires_and_deploys_new_build(
        self,
    ) -> None:
        plan, held = self.unavailable_legacy_event()
        self.assertEqual(plan["deploy_status"], "pass")
        self.assertEqual(self.deploys.runtime_identities[-1].source_git_ref, DEPLOYABLE)
        retired = self.store.lookup_existing_mutation_reservation(
            route_path=held.route_path,
            idempotency_key=held.idempotency_key,
            request_fingerprint=held.request_fingerprint,
        ).record
        assert retired is not None and retired.response_status_code is not None
        self.assertEqual(retired.state, "completed")
        self.assertGreaterEqual(retired.response_status_code, 400)
        self.assertEqual(retired.response_payload["recovery"], plan["testing_deploy_recovery"])

    def test_unavailable_event_refusal_is_visible_and_preserves_old_fence(self) -> None:
        plan, held = self.unavailable_legacy_event(refuse=True)
        self.assertTrue(plan["testing_deploy_recovery_refusal"])
        self.assertEqual(self.deploys.runtime_identities, [])
        lookup = self.store.lookup_existing_mutation_reservation(
            route_path=held.route_path,
            idempotency_key=held.idempotency_key,
            request_fingerprint=held.request_fingerprint,
        )
        self.assertEqual(lookup.record, held)

    def test_another_event_worker_execution_lock_defers_without_deploying(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.request()
        with patch.object(
            self.store, "testing_replacement_execution_lock", return_value=nullcontext(None)
        ):
            completed = self.run_once()
        self.assertEqual(completed.state, "pending")
        self.assertEqual(completed.last_plan["deferred"], "lane_busy")
        self.assertEqual(self.deploys.runtime_identities, [])
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())

    def test_a_held_testing_lane_waits_then_lifting_resumes_deployment(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.hold_testing()
        self.request()

        plan = self.reconcile()

        self.assertEqual((plan["action"], plan["reason"]), ("wait", "staff_testing"))
        self.assertEqual(self.deploys.runtime_identities, [])
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())

        self.lift_testing_hold()
        resumed = self.reconcile()

        self.assertEqual(resumed["action"], "deploy")
        self.assertEqual(
            [identity.source_git_ref for identity in self.deploys.runtime_identities], [DEPLOYABLE]
        )

    def test_retargeting_does_not_bypass_an_unknown_deploy_to_the_old_app(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.deploys.deploy_error = click.ClickException("provider timed out")
        self.deploys.observation = GenericWebProviderDeploymentObservation(outcome="unknown")
        self.request()
        first = self.run_once()
        self.assertEqual(first.state, "failed")

        self.deploys.target_id = "repaired-target"
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context="cm",
                instance="testing",
                target_id=self.deploys.target_id,
                updated_at="2026-10-02T01:00:00Z",
            )
        )
        self.deploys.deploy_error = None
        self.deploys.observation = GenericWebProviderDeploymentObservation(
            outcome="present",
            deployment_status="success",
            deployment_id="new-deployment",
            started_at="2026-10-02T12:00:00Z",
            finished_at="2026-10-02T12:01:00Z",
        )
        self.request()
        blocked = self.run_once()
        self.assertEqual(blocked.state, "failed")
        self.assertIn("generic-web deploy recovery", blocked.last_error)
        self.assertEqual(blocked.last_plan["deploy_operation_status"], "target_busy")
        self.assertNotEqual(
            first.last_plan["deploy_idempotency_key"], blocked.last_plan["deploy_idempotency_key"]
        )
        self.assertEqual(len(self.deploys.runtime_identities), 1)

    def test_running_deploy_on_old_authority_defers_a_new_attempt(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.store.reserve_mutation(
            scope=reconcile_reservation_scope("site"),
            route_path=GENERIC_WEB_DEPLOY_ROUTE,
            idempotency_key=f"{RECONCILE_SOURCE}:site:cm:testing:old-authority",
            request_fingerprint="old-request",
            lease_owner="other-worker",
            reconciliation_key="old-app-reconciliation",
            provider_target_key="old-app-target",
        )
        self.request()
        deferred = self.run_once()
        self.assertEqual(deferred.state, "pending")
        self.assertEqual(deferred.last_plan["deferred"], "lane_busy")
        self.assertEqual(self.deploys.runtime_identities, [])

    def test_expired_old_authority_reports_recovery_instead_of_deferring_forever(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value="2000-01-01T00:00:00Z"
        ):
            self.store.reserve_mutation(
                scope=reconcile_reservation_scope("site"),
                route_path=GENERIC_WEB_DEPLOY_ROUTE,
                idempotency_key=f"{RECONCILE_SOURCE}:site:cm:testing:old-authority",
                request_fingerprint="old-request",
                lease_owner="crashed-worker",
                reconciliation_key="old-app-reconciliation",
                provider_target_key="old-app-target",
            )
        self.request()
        blocked = self.run_once()
        self.assertEqual(blocked.state, "failed")
        self.assertIn("generic-web deploy recovery", blocked.last_error)
        self.assertEqual(self.deploys.runtime_identities, [])

    def test_changed_authority_still_observes_and_settles_the_same_unknown_deploy(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        succeeded = self.deploys.observation
        self.deploys.deploy_error = click.ClickException("provider timed out")
        self.deploys.observation = GenericWebProviderDeploymentObservation(outcome="unknown")
        self.request()
        first = self.run_once()
        self.assertEqual(first.state, "failed")
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="global",
                env={"SYNC_INTERVAL_SECONDS": 3600},
                updated_at="2026-10-02T01:00:00Z",
            )
        )
        # A renamed app changes reconciliation metadata, but not its target identity.
        self.deploys.target_name = "renamed-testing-app"
        target = self.store.read_dokploy_target_record(context_name="cm", instance_name="testing")
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={
                    "target_name": self.deploys.target_name,
                    "updated_at": "2026-10-02T01:00:00Z",
                }
            )
        )
        self.deploys.observation = succeeded
        self.request()
        still_held = self.run_once()
        self.assertEqual(still_held.state, "failed")
        self.assertEqual(
            still_held.last_plan["deploy_idempotency_key"],
            first.last_plan["deploy_idempotency_key"],
        )
        settled = self.settle_terminal_testing_original()
        self.assertEqual(settled.post_deploy_update.status, "fail")
        self.assertEqual(settled.deploy.deployment_id, succeeded.deployment_id)
        self.assertEqual(len(self.deploys.runtime_identities), 1)
        self.deploys.deploy_error = None
        self.request()
        successor = self.reconcile()
        self.assertNotEqual(
            successor["deploy_idempotency_key"], first.last_plan["deploy_idempotency_key"]
        )
        self.assertEqual(len(self.deploys.runtime_identities), 2)

    def test_the_reconcile_may_deploy_only_the_generic_web_testing_lane(self) -> None:
        def allowed(*, product: str = "site", instance: str = "testing") -> bool:
            return launchplane_reconcile_generic_web_testing_allowed(
                record_store=self.store, product=product, context="cm", instance=instance
            )

        self.assertTrue(allowed())
        self.assertFalse(allowed(instance="prod"))
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_profile())
        )
        self.assertFalse(allowed())


class _MinuteClock(datetime):
    """Each plan is issued a minute after the last; lifecycle order is by issue time."""

    ticks = 0

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> "_MinuteClock":
        cls.ticks += 1
        return cls(2026, 9, 30, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=cls.ticks)


class ProductReconcilePreviewTests(ProductReconcileTestCase):
    def test_preview_provider_read_denial_keeps_its_classification(self) -> None:
        from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied

        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)
        with patch.object(
            self.provider,
            "execute_apply",
            side_effect=NativeSourceAuthorizationDenied("private fixture detail"),
        ):
            failed = self.run_once()
        self.assertEqual(failed.last_plan["last_failed_error_code"], "authorization_denied")
        self.assertNotIn("private fixture detail", failed.model_dump_json())
        self.assertEqual(self.provider.applied, [])

    def test_preview_capacity_refusal_keeps_typed_reason_without_provider_effects(self) -> None:
        from control_plane.dokploy.network_capacity import BridgeNetworkCapacityRefused

        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)
        with patch.object(
            self.provider,
            "execute_apply",
            side_effect=BridgeNetworkCapacityRefused("host_bridge_network_capacity_short"),
        ):
            failed = self.run_once()
        self.assertEqual(failed.state, "failed")
        self.assertEqual(
            failed.last_plan["last_failed_error_code"], "host_bridge_network_capacity_short"
        )
        self.assertEqual(self.provider.applied, [])
        self.assertFalse(self.store.list_preview_records())

    def test_reset_preview_head_cannot_replace_the_serving_newer_build(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)
        self.reconcile()
        before = list(self.provider.applied)
        self.github.first_parents[PR_HEAD] = OLDER
        self.github.pull_request["head"] = {"sha": OLDER}
        self.github.add_run(51, OLDER, event="pull_request")
        self.request("preview", 5)
        plan = self.reconcile()
        self.assertEqual(plan["reason"], "ancestor_build")
        self.assertEqual(plan["action"], "wait")
        self.assertEqual(self.provider.applied, before)

    def install_refused_domain_provider(
        self, refused_path: str = "/api/domain.delete", *, real_inputs: bool = False
    ) -> list[str]:
        self.advance_preview_recovery_grace = True
        calls: list[str] = []
        domain_exists = True

        def provider_request(*, path: str, **_kwargs: object) -> object:
            nonlocal domain_exists
            calls.append(path)
            if path == refused_path:
                if path == "/api/domain.byComposeId":
                    raise DokployRequestFailed(
                        method="GET", path=path, detail="Unavailable", status_code=503
                    )
                raise click.ClickException("Persistent delete refusal")
            if path == "/api/domain.byComposeId":
                return (
                    [{"domainId": "preview-domain", "host": "pr-5.example.test"}]
                    if domain_exists
                    else []
                )
            if path == "/api/project.all":
                return []
            if path == "/api/compose.search":
                return [{"composeId": "cm-odoo-preview-pr-5", "name": "cm-odoo-preview-pr-5"}]
            if path == "/api/domain.delete":
                domain_exists = False
                return None
            if path == "/api/compose.one":
                return {"composeId": "cm-odoo-preview-pr-5"}
            raise AssertionError(path)

        def execute(
            *,
            request: OdooPreviewApplyEnvelope,
            provider_effect_checkpoint: Callable[[str], None],
            propagate_domain_lookup_error: bool = False,
            **_kwargs: object,
        ) -> dict[str, object]:
            return execute_odoo_preview_dokploy_apply(
                control_plane_root=self.root,
                request=request.apply,
                provider_effect_checkpoint=provider_effect_checkpoint,
                propagate_domain_lookup_error=propagate_domain_lookup_error,
            ).model_dump(mode="json")

        for method, replacement in (
            ("execute_apply", execute),
            ("observe_apply", observe_odoo_preview_apply_result),
        ):
            patcher = patch.object(self.provider, method, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, side_effect in (
            ("control_plane.dokploy.api.dokploy_request", provider_request),
            (
                "control_plane.dokploy.source.read_dokploy_config",
                lambda **_kwargs: ("https://dokploy.example.test", "fixture-token"),
            ),
        ):
            provider_patcher = patch(target, side_effect=side_effect)
            provider_patcher.start()
            self.addCleanup(provider_patcher.stop)
        self.write_preview()
        self.github.pull_request["state"] = "closed"
        if real_inputs:
            _write_odoo_preview_template_runtime_environment(store=self.store)
            inputs_patcher = patch.object(
                self.provider, "build_inputs", build_odoo_preview_apply_inputs_result
            )
            inputs_patcher.start()
            self.addCleanup(inputs_patcher.stop)
            for target, value in (
                ("resolve_odoo_preview_url", "https://pr-5.example.test"),
                ("_preview_template_compose_id", ""),
                ("_preview_environment_id", ("", "")),
            ):
                bootstrap_patcher = patch(
                    f"control_plane.workflows.odoo_preview_runtime.{target}", return_value=value
                )
                bootstrap_patcher.start()
                self.addCleanup(bootstrap_patcher.stop)
        return calls

    def test_refused_domain_mutations_stop_but_unknown_destroy_is_observed(self) -> None:
        calls = self.install_refused_domain_provider()
        for attempt in range(PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS):
            self.request("preview", 5)
            with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                failed = self.run_once()
            self.assertEqual(failed.last_plan["destroy_failed_attempts"], attempt + 1)
        held: ProductReconcileRequestRecord | None = None
        for _sweep in range(2):
            self.request("preview", 5)
            held = self.run_once()
            self.assertEqual(held.last_plan["reason"], "preview_destroy_retry_limit")
            self.assertEqual(held.state, "done")
        self.assertEqual(calls.count("/api/domain.delete"), PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS)
        self.assertIn("/api/compose.one", calls)
        self.assertNotIn("/api/compose.delete", calls)
        assert held is not None
        reservation = self.store.read_idempotency_record(
            scope=reconcile_reservation_scope("site"),
            route_path=ODOO_PREVIEW_APPLY_ROUTE,
            idempotency_key=str(held.last_plan["preview_plan_id"]),
        )
        assert reservation is not None
        self.assertEqual(reservation.state, "reconcile_required")
        self.assertEqual(reservation.provider_effect_phase, "domain_delete")
        self.assertEqual(self.store.list_preview_records()[0].state, "active")

        # A missing compose with domains still present cannot complete cleanup.
        self.request("preview", 5)
        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                side_effect=click.ClickException("Dokploy request failed (404): missing compose"),
            ),
        ):
            unproven = self.run_once()
        self.assertEqual(unproven.last_plan["preview_operation_status"], "reconcile_required")
        self.assertEqual(self.store.list_preview_records()[0].state, "active")

        # Independently verified completion is adopted after the mutation budget is spent.
        self.request("preview", 5)
        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                side_effect=click.ClickException("Dokploy request failed (404): missing compose"),
            ),
            patch("control_plane.dokploy.api.dokploy_request", return_value=[]),
        ):
            completed = self.run_once()
        self.assertEqual(completed.last_plan["preview_operation_status"], "adopted")
        self.assertEqual(completed.last_plan["preview_result_status"], "pass")
        self.assertEqual(self.store.list_preview_records()[0].state, "destroyed")
        self.assertEqual(calls.count("/api/domain.delete"), PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS)

    def test_unknown_destroy_survives_preflight_refusals_without_exhausting_observation(
        self,
    ) -> None:
        calls = self.install_refused_domain_provider()
        self.request("preview", 5)
        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            first = self.run_once()
        first_attempts = first.last_plan["destroy_failed_attempts"]
        assert isinstance(first_attempts, int)
        for _attempt in range(PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS + 1):
            self.request("preview", 5)
            with patch.object(self.provider, "build_inputs", side_effect=ValueError("refused")):
                with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                    failed = self.run_once()
            self.assertEqual(
                failed.last_plan["destroy_failed_attempts"],
                first_attempts,
            )
        self.request("preview", 5)
        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            resumed = self.run_once()
        self.assertIn("/api/compose.one", calls)
        self.assertEqual(
            resumed.last_plan["destroy_failed_attempts"],
            first_attempts + 1,
        )

    def test_observer_read_failures_do_not_consume_unknown_destroy_mutation_budget(self) -> None:
        calls = self.install_refused_domain_provider()
        self.request("preview", 5)
        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            first = self.run_once()
        with patch(
            "control_plane.dokploy.api.fetch_dokploy_target_payload",
            side_effect=click.ClickException("Provider read unavailable"),
        ) as reads:
            for _attempt in range(PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS + 1):
                self.request("preview", 5)
                with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                    observed = self.run_once()
                self.assertEqual(
                    observed.last_plan["destroy_failed_attempts"],
                    first.last_plan["destroy_failed_attempts"],
                )
        self.assertGreaterEqual(reads.call_count, PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS + 1)
        self.assertEqual(calls.count("/api/domain.delete"), 1)

    def test_unresolved_compose_destroy_is_observed_without_consuming_attempts(self) -> None:
        calls = self.install_refused_domain_provider("/api/compose.delete", real_inputs=True)
        self.request("preview", 5)
        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            first = self.run_once()
        # A later phase with a present compose returns unknown without retrying.
        for _attempt in range(PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS + 1):
            self.request("preview", 5)
            with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                observed = self.run_once()
            self.assertEqual(
                observed.last_plan["destroy_failed_attempts"],
                first.last_plan["destroy_failed_attempts"],
            )
        self.assertEqual(calls.count("/api/compose.delete"), 1)
        self.assertIn("/api/compose.one", calls)
        # Fresh discovery now refuses because destroy already removed the domain.
        self.assertEqual(observed.last_plan["last_failed_error_code"], "preview_plan_blocked")
        profile = self.store.read_product_profile_record("site")
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "preview": profile.preview.model_copy(update={"template_instance": "repaired"}),
                    "updated_at": "2026-10-05T12:00:00Z",
                }
            )
        )
        self.request("preview", 5)
        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                side_effect=click.ClickException("Dokploy request failed (404): missing compose"),
            ),
            patch("control_plane.dokploy.api.dokploy_request", return_value=[]),
        ):
            completed = self.run_once()
        self.assertEqual(completed.last_plan["preview_operation_status"], "adopted")
        self.assertEqual(self.store.list_preview_records()[0].state, "destroyed")

    def test_retryable_domain_lookup_outage_does_not_consume_mutation_attempts(self) -> None:
        self.install_refused_domain_provider("/api/domain.byComposeId")
        for _attempt in range(PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS + 1):
            self.request("preview", 5)
            with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                failed = self.run_once()
            self.assertEqual(failed.last_plan["destroy_failed_attempts"], 0)

    def test_checkpoint_storage_failure_does_not_consume_unknown_destroy_attempts(self) -> None:
        calls = self.install_refused_domain_provider()
        self.request("preview", 5)
        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            first = self.run_once()
        with patch.object(
            self.store, "checkpoint_mutation_provider_effect", side_effect=OSError("DB unavailable")
        ):
            for _attempt in range(PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS + 1):
                self.request("preview", 5)
                with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                    failed = self.run_once()
                self.assertEqual(
                    failed.last_plan["destroy_failed_attempts"],
                    first.last_plan["destroy_failed_attempts"],
                )
        self.assertEqual(calls.count("/api/domain.delete"), 1)

    def test_profile_repair_replans_when_retained_destroy_authority_is_stale(self) -> None:
        calls = self.install_refused_domain_provider()
        self.request("preview", 5)
        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            self.run_once()
        profile = self.store.read_product_profile_record("site")
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "preview": profile.preview.model_copy(update={"template_instance": "repaired"}),
                    "updated_at": "2026-10-05T12:00:00Z",
                }
            )
        )
        self.request("preview", 5)
        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            repaired = self.run_once()
        self.assertEqual(calls.count("/api/domain.delete"), 2)
        self.assertEqual(repaired.last_plan["destroy_failed_attempts"], 1)

    def test_permanent_domain_lookup_refusal_after_unknown_destroy_is_bounded(self) -> None:
        self.install_refused_domain_provider()
        self.request("preview", 5)
        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            self.run_once()
        original = self.provider.execute_apply

        def refused_lookup(
            *, issued_plan: OdooPreviewApplyInputsResult, **kwargs: object
        ) -> dict[str, object]:
            with patch(
                "control_plane.workflows.preview_resource_destroy._preview_resource_domain_ids",
                side_effect=DokployRequestFailed(
                    method="GET", path="/api/domain.byComposeId", detail="Refused", status_code=403
                ),
            ):
                return original(issued_plan=issued_plan, **kwargs)

        with patch.object(self.provider, "execute_apply", refused_lookup):
            for _sweep in range(PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS):
                self.request("preview", 5)
                self.run_once()
        self.request("preview", 5)
        held = self.run_once()
        self.assertEqual(held.last_plan["reason"], "preview_destroy_retry_limit")
        self.assertEqual(
            held.last_plan["destroy_failed_attempts"], PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS
        )

    def test_raised_failure_after_safe_destroy_retry_consumes_attempts(self) -> None:
        self.install_refused_domain_provider()
        self.request("preview", 5)
        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            self.run_once()
        original = self.provider.execute_apply

        def raised_apply(
            *, issued_plan: OdooPreviewApplyInputsResult, **kwargs: object
        ) -> dict[str, object]:
            original(issued_plan=issued_plan, **kwargs)
            raise RuntimeError("execution failed after the domain checkpoint")

        with patch.object(self.provider, "execute_apply", raised_apply):
            for attempt in range(1, PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS):
                self.request("preview", 5)
                with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                    failed = self.run_once()
                self.assertEqual(failed.last_plan["destroy_failed_attempts"], attempt + 1)
        self.request("preview", 5)
        self.assertEqual(self.run_once().last_plan["reason"], "preview_destroy_retry_limit")

    def test_connection_resets_after_domain_checkpoint_are_bounded(self) -> None:
        self.install_refused_domain_provider()
        original = self.provider.execute_apply

        def reset_connection(
            *, issued_plan: OdooPreviewApplyInputsResult, **kwargs: object
        ) -> dict[str, object]:
            with patch(
                "control_plane.workflows.preview_resource_destroy._delete_domain",
                side_effect=ConnectionResetError("Lost provider response"),
            ):
                return original(issued_plan=issued_plan, **kwargs)

        with patch.object(self.provider, "execute_apply", reset_connection):
            for attempt in range(PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS):
                self.request("preview", 5)
                with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                    failed = self.run_once()
                self.assertEqual(failed.last_plan["destroy_failed_attempts"], attempt + 1)
        self.request("preview", 5)
        self.assertEqual(self.run_once().last_plan["reason"], "preview_destroy_retry_limit")

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

        self.github.pull_request["state"] = "closed"
        self.request("preview", 5)
        destroyed = self.reconcile()

        self.assertEqual(
            (destroyed["action"], destroyed["reason"], destroyed["preview_result_status"]),
            ("destroy", "pull_request_not_open", "pass"),
        )
        self.assertEqual(self.store.list_preview_records()[0].state, "destroyed")

        # The same build asked for again after a destroy is a new operation, not a replay.
        self.github.pull_request["state"] = "open"
        self.request("preview", 5)
        self.assertEqual(self.reconcile()["action"], "apply")
        self.assertEqual(self.provider.applied, [("refresh", 5), ("destroy", 5), ("refresh", 5)])
        self.assertEqual(self.store.list_preview_records()[0].state, "active")

    def test_same_digest_new_head_and_run_refresh_odoo_preview(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)
        self.reconcile()
        pushed = "b" * 40
        self.github.image_digests[pushed] = _digest(PR_HEAD)
        self.github.pull_request["head"] = {"sha": pushed}
        for run_id in (51, 52):
            with self.subTest(run_id=run_id):
                self.github.add_run(run_id, pushed, event="pull_request")
                self.request("preview", 5)
                self.assertEqual(self.reconcile()["action"], "apply")
                (preview,) = self.store.list_preview_records()
                generation = self.store.read_preview_generation_record(
                    preview.serving_generation_id
                )
                self.assertEqual(generation.anchor_summary.head_sha, pushed)
                assert generation.source_build is not None
                self.assertEqual(generation.source_build.run_id, run_id)
                self.request("preview", 5)
                self.assertEqual(self.reconcile()["reason"], "already_serving")

    def test_blocked_destroy_stops_across_sweeps_and_resumes_after_record_changes(self) -> None:
        self.write_preview()
        self.github.pull_request["state"] = "closed"
        before = self.snapshot()
        original = self.provider.build_inputs

        def blocked_inputs(
            *,
            profile: LaunchplaneProductProfileRecord,
            request: OdooPreviewApplyInputsRequest,
            **_kwargs: object,
        ) -> dict[str, object]:
            result = original(profile=profile, request=request)
            result["status"] = "blocked"
            return result

        with patch.object(self.provider, "build_inputs", side_effect=blocked_inputs) as inputs:
            for attempt in range(PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS):
                self.request("preview", 5)
                with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                    failed = self.run_once()
                self.assertEqual(failed.state, "failed")
                self.assertEqual(failed.last_plan["destroy_failed_attempts"], attempt + 1)
                self.assertEqual(failed.last_plan["last_failed_error_code"], "preview_plan_blocked")
            for _sweep in range(2):
                self.request("preview", 5)
                stopped = self.run_once()
                self.assertEqual(stopped.state, "done")
                self.assertTrue(stopped.last_plan["held"])
                self.assertEqual(stopped.last_plan["reason"], "preview_destroy_retry_limit")
                view = product_reconcile_request_view(stopped)
                self.assertIn("stops retrying", str(view.last_plan["destroy_retry_stop_reason"]))
                self.assertEqual(
                    view.last_plan["last_failed_error_summary"],
                    record_failure_summary("preview_plan_blocked"),
                )
            self.assertEqual(inputs.call_count, PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS)
        # A transient failure before planning must not erase the exhausted budget.
        self.request("preview", 5)
        with patch.object(self.github, "get_json", side_effect=OSError("GitHub unavailable")):
            with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                self.assertEqual(self.run_once().state, "failed")
        self.request("preview", 5)
        with patch.object(self.provider, "build_inputs") as inputs:
            self.assertEqual(self.reconcile()["reason"], "preview_destroy_retry_limit")
            inputs.assert_not_called()
        self.assertEqual(
            self.store.read_product_reconcile_request("site:preview:5").last_plan[
                "last_failed_error_code"
            ],
            "preview_plan_blocked",
        )
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.provider.applied, [])

        # A profile repair re-arms the automatic budget without changing the preview.
        profile = self.store.read_product_profile_record("site")
        self.store.write_product_profile_record(
            profile.model_copy(update={"updated_at": "2026-09-30T09:00:00Z"})
        )
        self.request("preview", 5)
        with patch.object(self.provider, "build_inputs", side_effect=blocked_inputs):
            with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                repaired = self.run_once()
        self.assertEqual(repaired.last_plan["destroy_failed_attempts"], 1)

        # A supported record repair changes the lifecycle; a fresh bounded run may succeed.
        preview = self.store.list_preview_records()[0]
        self.store.write_preview_record(
            preview.model_copy(update={"updated_at": "2026-09-30T10:00:00Z"})
        )
        self.request("preview", 5)
        self.assertEqual(self.reconcile()["preview_result_status"], "pass")
        self.assertEqual(self.provider.applied, [("destroy", 5)])

    def test_reopened_pr_is_not_held_by_an_exhausted_destroy(self) -> None:
        self.write_preview()
        self.github.pull_request["state"] = "closed"
        with patch.object(self.provider, "build_inputs", side_effect=ValueError("refused")):
            for _attempt in range(PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS):
                self.request("preview", 5)
                with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                    self.run_once()
        self.request("preview", 5)
        self.assertEqual(self.reconcile()["reason"], "preview_destroy_retry_limit")
        self.github.pull_request["state"] = "open"
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)
        # The legacy generation has no exact build proof; reopening refreshes it.
        self.assertEqual(self.reconcile()["action"], "apply")
        self.request("preview", 5)
        self.assertEqual(self.reconcile()["reason"], "already_serving")

    def test_busy_destroy_does_not_consume_a_failure_attempt(self) -> None:
        self.write_preview()
        self.github.pull_request["state"] = "closed"
        with patch(
            "control_plane.product_reconcile.run_odoo_preview_apply_operation",
        ) as operation:
            operation.return_value.status = "target_busy"
            self.request("preview", 5)
            busy = self.run_once()
        self.assertEqual(busy.state, "pending")
        self.assertEqual(busy.last_plan["destroy_failed_attempts"], 0)
        self.assertEqual(self.reconcile()["preview_result_status"], "pass")

    def test_transport_failure_during_destroy_does_not_consume_attempts(self) -> None:
        self.write_preview()
        self.github.pull_request["state"] = "closed"
        with patch(
            "control_plane.product_reconcile._pull_request_moved",
            side_effect=BuildProvenanceError("GitHub unavailable", code="github_read_failed"),
        ):
            for _attempt in range(PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS + 1):
                self.request("preview", 5)
                with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                    failed = self.run_once()
                self.assertEqual(failed.last_plan["destroy_failed_attempts"], 0)
        self.request("preview", 5)
        self.assertEqual(self.reconcile()["preview_result_status"], "pass")

    def test_preview_plan_records_the_credentials_the_preview_leaves_out(self) -> None:
        self.provider.omitted_integration_credential_keys = ("ODOO_SMTP_PASSWORD",)
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)

        applied = self.reconcile()

        self.assertEqual(applied["preview_result_status"], "pass")
        self.assertEqual(applied["omitted_integration_credential_keys"], ["ODOO_SMTP_PASSWORD"])

    def test_preview_failure_after_the_provider_started_says_why(self) -> None:
        def failed_apply(**_kwargs: object) -> dict[str, object]:
            return {
                "status": "fail",
                "error_message": "Dokploy compose deploy failed: image pull denied.",
                "provider_effect_attempted": True,
            }

        self.provider.execute_apply = failed_apply  # type: ignore[method-assign]
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)

        failed = self.run_once()

        self.assertEqual(failed.state, "failed")
        self.assertEqual(failed.last_plan["preview_operation_status"], "reconcile_required")
        self.assertEqual(failed.last_error, record_failure_summary("reconcile_required"))
        self.assertNotIn("image pull denied", failed.model_dump_json())

    def test_preview_destination_refusal_has_a_fixed_reason(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)
        with patch(
            "control_plane.product_reconcile.launchplane_reconcile_preview_destination_allowed",
            return_value=False,
        ):
            failed = self.run_once()
        self.assertEqual(failed.state, "failed")
        self.assertEqual(failed.last_error, record_failure_summary("preview_destination_refused"))
        self.assertEqual(self.provider.applied, [])

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
                    {"state": "open", "draft": False, "labels": [], **pull_request}
                )
                if built:
                    self.github.add_run(50, PR_HEAD, event="pull_request")
                if live:
                    self.write_preview(number=index)
                    verified = verify_build_artifact(
                        transport=self.github,
                        repository=REPOSITORY,
                        repository_id=REPOSITORY_ID,
                        commit=PR_HEAD,
                        purpose="preview",
                        context="cm",
                        image_repository=IMAGE_REPOSITORY,
                        pull_request_number=index,
                    )
                    preview = next(
                        p for p in self.store.list_preview_records() if p.anchor_pr_number == index
                    )
                    generation = self.store.read_preview_generation_record(
                        preview.serving_generation_id
                    )
                    self.store.write_preview_generation_record(
                        generation.model_copy(
                            update={"source_build": verified.manifest.source_build}
                        )
                    )
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

    def test_preview_follows_the_pull_request_state(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        # A preview stays up until the PR closes or merges; a draft is not closed.
        transitions: tuple[tuple[str, dict[str, object], str, str], ...] = (
            ("opened as a draft", {"draft": True}, "apply", ""),
            ("ready for review", {"draft": False}, "none", "already_serving"),
            ("converted to draft", {"draft": True}, "none", "already_serving"),
            ("closed", {"state": "closed"}, "destroy", "pull_request_not_open"),
            ("reopened as a draft", {"state": "open"}, "apply", ""),
            ("merged", {"state": "closed", "merged": True}, "destroy", "pull_request_not_open"),
        )
        for name, change, action, reason in transitions:
            with self.subTest(name):
                self.github.pull_request.update(change)
                self.request("preview", 5)

                plan = self.reconcile()

                self.assertEqual((plan["action"], plan.get("reason", "")), (action, reason))
        self.assertEqual(
            self.provider.applied, [("refresh", 5), ("destroy", 5), ("refresh", 5), ("destroy", 5)]
        )
        self.assertEqual(self.store.list_preview_records()[0].state, "destroyed")

    def test_removing_any_label_destroys_nothing(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.github.pull_request["body"] = "## Client test notes\nTry the changed page."
        self.github.pull_request["labels"] = [{"name": "preview"}, {"name": "owner-review"}]
        self.request("preview", 5)
        self.assertEqual(self.reconcile()["action"], "apply")

        self.github.pull_request["labels"] = []
        self.request("preview", 5)
        kept = self.reconcile()

        self.assertEqual((kept["action"], kept["reason"]), ("none", "already_serving"))
        self.assertEqual(self.provider.applied, [("refresh", 5)])
        self.assertEqual(self.store.list_preview_records()[0].state, "active")

    def test_a_pr_closed_before_the_provider_change_is_reconciled_again(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        # Reads: the plan, the build verification, then the check just before applying.
        self.github.pull_request_move = (2, {"state": "closed"})
        self.request("preview", 5)

        completed = self.run_once()

        self.assertEqual(completed.state, "pending")
        self.assertEqual(completed.last_plan["deferred"], "pull_request_moved")
        self.assertEqual(self.provider.applied, [])


def _generic_web_preview_profile() -> LaunchplaneProductProfileRecord:
    return _generic_web_profile().model_copy(
        update={
            "preview": ProductPreviewProfile(
                enabled=True,
                context="site-preview",
                slug_template="pr-{number}",
                template_instance="testing",
            )
        }
    )


class FakeGenericWebPreviewDriver:
    """Plays the provider side of a generic-web preview; Launchplane's records are real."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.changes: list[tuple[str, int]] = []
        self.refresh_status = "pass"
        self.during_refresh: Callable[[], None] = lambda: None
        for name, fake in (
            ("execute_generic_web_preview_refresh", self.refresh),
            ("execute_generic_web_preview_destroy", self.destroy),
        ):
            patcher = patch(f"control_plane.generic_web_preview_http.{name}", side_effect=fake)
            patcher.start()
            test.addCleanup(patcher.stop)

    def refresh(
        self,
        *,
        request: GenericWebPreviewRefreshRequest,
        profile: LaunchplaneProductProfileRecord,
        **_kwargs: object,
    ) -> GenericWebPreviewRefreshResult:
        assert request.anchor_pr_number is not None
        self.during_refresh()
        self.changes.append(("refresh", request.anchor_pr_number))
        return GenericWebPreviewRefreshResult(
            refresh_status=cast(Literal["pass", "blocked", "fail"], self.refresh_status),
            refresh_started_at="2026-10-02T12:00:00Z",
            refresh_finished_at="2026-10-02T12:01:00Z",
            product=profile.product,
            context=profile.preview.context,
            preview_slug=request.preview_slug,
            application_name=f"site-{request.preview_slug}",
            preview_url=f"https://{request.preview_slug}.site.example.test",
            runtime_identity=_build_preview_runtime_identity(
                profile=profile,
                preview_slug=request.preview_slug,
                image_reference=request.image_reference,
                anchor_head_sha=request.anchor_head_sha,
            ),
            error_message="" if self.refresh_status == "pass" else PROVIDER_FAILURE_TEXT,
        )

    def destroy(
        self,
        *,
        request: GenericWebPreviewDestroyRequest,
        profile: LaunchplaneProductProfileRecord,
        **_kwargs: object,
    ) -> GenericWebPreviewDestroyResult:
        assert request.anchor_pr_number is not None
        self.changes.append(("destroy", request.anchor_pr_number))
        return GenericWebPreviewDestroyResult(
            destroy_status="pass",
            destroy_started_at="2026-10-02T13:00:00Z",
            destroy_finished_at="2026-10-02T13:01:00Z",
            product=profile.product,
            context=profile.preview.context,
            preview_slug=request.preview_slug,
            application_name=f"site-{request.preview_slug}",
            application_id="application-1",
        )


class ProductReconcileGenericWebPreviewTests(ProductReconcileTestCase):
    def test_pr_closed_during_scan_never_reaches_preview_provider(self) -> None:
        from control_plane.artifact_scan import ArtifactScanResult

        def scan_then_close(image: str) -> ArtifactScanResult:
            self.github.pull_request["state"] = "closed"
            return ArtifactScanResult(
                image_reference=image,
                status="pass",
                reason="clean",
                scanned_at="2026-10-16T12:00:00Z",
            )

        self.artifact_scanner.side_effect = scan_then_close
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)
        self.run_once()
        self.assertFalse(self.driver.changes)

    def test_director_standing_preview_needs_valid_notes_without_a_client_invitation(self) -> None:
        payload = _generic_web_preview_profile().model_dump(mode="json")
        payload["owner"] = {"github_login": "site-owner", "github_id": "4242"}
        payload["release_on_acceptance"] = "director_standing"
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(payload)
        )
        self.github.add_run(50, PR_HEAD, event="pull_request")
        for body, labels in (
            ("## Client test notes\nTry the approval buttons.", []),
            (
                "## Client test notes\nTry the changed page.",
                [{"name": "owner-review"}],
            ),
        ):
            with self.subTest(body=body, labels=labels):
                self.github.pull_request.update(body=body, labels=labels)
                self.request("preview", 5)
                result = self.reconcile()
                self.assertIn(result["action"], ("apply", "none"))
                self.assertIs(result["owner_review_requested"], False)
                for comment in self.comments.on(5):
                    self.assertNotIn("@site-owner", cast(str, comment["body"]))
        self.github.pull_request["body"] = ""
        self.request("preview", 5)
        self.assertEqual(self.reconcile()["action"], "wait")

    def test_reset_preview_head_keeps_the_serving_build(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)
        self.reconcile()
        before = list(self.driver.changes)
        self.github.first_parents[PR_HEAD] = OLDER
        self.github.pull_request["head"] = {"sha": OLDER}
        self.github.add_run(51, OLDER, event="pull_request")
        self.github.image_digests[OLDER] = _digest(PR_HEAD)
        self.request("preview", 5)
        plan = self.reconcile()
        self.assertEqual(plan["reason"], "ancestor_build")
        self.assertEqual(plan["action"], "wait")
        self.assertEqual(self.driver.changes, before)

    def test_failed_active_generation_does_not_allow_repeated_ancestor_refresh(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)
        self.reconcile()
        pushed = "b" * 40
        self.github.pull_request["head"] = {"sha": pushed}
        self.github.add_run(51, pushed, event="pull_request")
        self.driver.refresh_status = "fail"
        self.request("preview", 5)
        self.assertEqual(self.run_once().state, "failed")
        before = list(self.driver.changes)
        self.github.first_parents[PR_HEAD] = OLDER
        self.github.pull_request["head"] = {"sha": OLDER}
        self.github.add_run(52, OLDER, event="pull_request")
        self.driver.refresh_status = "pass"
        for _ in range(2):
            self.request("preview", 5)
            plan = self.reconcile()
            self.assertEqual((plan["action"], plan["reason"]), ("wait", "ancestor_build"))
        self.assertEqual(self.driver.changes, before)

    def setUp(self) -> None:
        super().setUp()
        self.store.write_product_profile_record(_generic_web_preview_profile())
        self.github = FakeGenericWebGitHub()
        self.driver = FakeGenericWebPreviewDriver(self)

    def test_an_open_pr_gets_a_preview_of_its_verified_build_until_it_closes(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)

        applied = self.reconcile()

        self.assertEqual(
            (applied["action"], applied["preview_result_status"], applied["preview_url"]),
            ("apply", "pass", "https://pr-5.site.example.test"),
        )
        (preview,) = self.store.list_preview_records()
        self.assertEqual((preview.anchor_pr_number, preview.state), (5, "active"))
        self.assertEqual(preview.serving_generation_id, preview.active_generation_id)
        (comment,) = self.comments.on(5)
        self.assertIn("https://pr-5.site.example.test", cast(str, comment["body"]))
        # Nothing went to the Odoo artifact store.
        self.assertEqual(self.store.list_artifact_manifests(), ())

        self.request("preview", 5)
        kept = self.reconcile()

        self.assertEqual((kept["action"], kept["reason"]), ("none", "already_serving"))
        self.assertEqual(kept["current_image_digest"], _digest(PR_HEAD))

        self.github.pull_request["state"] = "closed"
        self.request("preview", 5)
        destroyed = self.reconcile()

        self.assertEqual(
            (destroyed["action"], destroyed["preview_result_status"]), ("destroy", "pass")
        )
        self.assertEqual(self.store.list_preview_records()[0].state, "destroyed")
        self.assertEqual(self.driver.changes, [("refresh", 5), ("destroy", 5)])

    def test_refused_destroy_stops_without_removing_the_generic_web_preview(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)
        self.reconcile()
        self.github.pull_request["state"] = "closed"
        before = self.snapshot()
        with patch(
            "control_plane.product_reconcile.resolve_generic_web_preview_slug",
            side_effect=click.ClickException("preview slug unavailable"),
        ) as destroy:
            for _attempt in range(PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS):
                self.request("preview", 5)
                with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                    self.assertEqual(self.run_once().state, "failed")
            self.request("preview", 5)
            self.assertEqual(self.reconcile()["reason"], "preview_destroy_retry_limit")
            self.assertEqual(destroy.call_count, PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS)
        self.assertEqual(self.snapshot(), before)

    def test_retryable_dokploy_outages_do_not_exhaust_destroy_budget(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)
        self.reconcile()
        self.github.pull_request["state"] = "closed"
        for status_code in (None, 503):
            error = DokployRequestFailed(
                method="POST",
                path="/compose.delete",
                detail="unavailable",
                status_code=status_code,
            )
            baseline_error = DokployScheduleExecutionFailed(
                schedule_id="fixture",
                deployment_id="",
                deployment_status="not_started",
                cause="trigger_baseline_unavailable",
            )
            baseline_error.__cause__ = error
            for failure in (error, baseline_error):
                with patch(
                    "control_plane.generic_web_preview_http.execute_generic_web_preview_destroy",
                    side_effect=failure,
                ):
                    for _attempt in range(PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS + 1):
                        self.request("preview", 5)
                        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                            failed = self.run_once()
                        self.assertEqual(failed.last_plan["destroy_failed_attempts"], 0)
        self.request("preview", 5)
        self.assertEqual(self.reconcile()["preview_result_status"], "pass")

    def test_a_new_push_replaces_the_preview_it_serves(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)
        self.reconcile()
        pushed = "b" * 40
        self.github.pull_request["head"] = {"sha": pushed}
        self.github.add_run(51, pushed, event="pull_request")
        self.request("preview", 5)

        replaced = self.reconcile()

        self.assertEqual(replaced["action"], "apply")
        self.request("preview", 5)
        self.assertEqual(self.reconcile()["reason"], "already_serving")
        self.assertEqual(self.driver.changes, [("refresh", 5), ("refresh", 5)])

    def test_same_digest_head_and_rebuilds_update_serving_evidence(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)
        self.reconcile()
        pushed = "b" * 40
        self.github.image_digests[pushed] = _digest(PR_HEAD)
        self.github.pull_request["head"] = {"sha": pushed}
        for run_id, attempt in ((51, 1), (52, 1), (52, 2)):
            with self.subTest(run_id=run_id, attempt=attempt):
                self.github.add_run(run_id, pushed, event="pull_request")
                self.github.runs[run_id]["run_attempt"] = attempt
                self.request("preview", 5)
                result = self.reconcile()
                self.assertEqual(result["action"], "apply")
                (preview,) = self.store.list_preview_records()
                generation = self.store.read_preview_generation_record(
                    preview.serving_generation_id
                )
                self.assertEqual(generation.anchor_summary.head_sha, pushed)
                review_preview = resolve_serving_preview(
                    store=self.store,
                    profile=self.store.read_product_profile_record("site"),
                    pull_request_number=5,
                )
                self.assertEqual(review_preview.head_sha, pushed)
                self.assertEqual(review_preview.preview_url, preview.canonical_url)
                assert generation.runtime_identity is not None
                self.assertEqual(generation.runtime_identity.source_git_ref, pushed)
                assert generation.source_build is not None
                self.assertEqual(
                    (generation.source_build.run_id, generation.source_build.run_attempt),
                    (run_id, attempt),
                )
                before = list(self.driver.changes)
                self.request("preview", 5)
                self.assertEqual(self.reconcile()["reason"], "already_serving")
                self.assertEqual(self.driver.changes, before)
        # Re-verification of the same generation retains its exact build provenance.
        apply_generic_web_preview_verification_result(
            control_plane_root=self.root,
            record_store=self.store,
            request=GenericWebPreviewVerificationEnvelope(
                product="site",
                verification=GenericWebPreviewVerificationRequest(
                    context=preview.context,
                    anchor_repo=preview.anchor_repo,
                    anchor_pr_number=preview.anchor_pr_number,
                    verification_status="pass",
                    verified_at=generation.ready_at,
                ),
            ),
        )
        self.assertEqual(
            self.store.read_preview_generation_record(generation.generation_id).source_build,
            generation.source_build,
        )
        # Losing the newest run cannot move the serving preview back to an older build.
        del self.github.runs[52]
        self.request("preview", 5)
        self.assertEqual(self.reconcile()["reason"], "older_artifact")
        self.assertEqual(self.driver.changes, before)

    def test_a_pr_without_a_verified_build_keeps_the_preview_it_has(self) -> None:
        # A preview the product's old preview workflow made, before its build.yml existed.
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)
        self.reconcile()
        self.github.runs.clear()
        self.github.pull_request["head"] = {"sha": "b" * 40}
        self.request("preview", 5)
        before = self.snapshot()

        waiting = self.reconcile()

        self.assertEqual((waiting["action"], waiting["reason"]), ("wait", "no_verified_build"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.driver.changes, [("refresh", 5)])

        self.assertEqual(self.artifact_scanner.call_count, 1)

    def test_a_failed_refresh_keeps_driver_text_out_and_waits_for_a_new_build(self) -> None:
        self.driver.refresh_status = "fail"
        self.github.add_run(50, PR_HEAD, event="pull_request")
        failures = []
        for _attempt in range(2):
            self.request("preview", 5)
            failures.append(self.run_once())

        for completed in failures:
            self.assertEqual(completed.state, "failed")
            (comment,) = self.comments.on(5)
            for text in (completed.last_error, cast(str, comment["body"])):
                for fragment in PROVIDER_FAILURE_FRAGMENTS:
                    self.assertNotIn(fragment, text)
        self.assertIn("tries again when the PR has a new build", failures[-1].last_error)
        self.assertEqual(self.driver.changes, [("refresh", 5)])
        self.assertEqual(self.artifact_scanner.call_count, 1)

        # A re-run of the Build workflow can produce the same image; it is tried again.
        self.driver.refresh_status = "pass"
        self.github.add_run(51, PR_HEAD, event="pull_request")
        self.request("preview", 5)

        self.assertEqual(self.reconcile()["preview_result_status"], "pass")
        self.assertEqual(self.store.list_preview_records()[0].state, "active")
        self.assertEqual(self.driver.changes, [("refresh", 5), ("refresh", 5)])

    def test_a_failed_replacement_is_not_mistaken_for_the_earlier_preview(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)
        self.reconcile()
        pushed = "b" * 40
        self.github.pull_request["head"] = {"sha": pushed}
        self.github.add_run(51, pushed, event="pull_request")
        self.driver.refresh_status = "fail"
        self.request("preview", 5)
        self.assertEqual(self.run_once().state, "failed")
        # The PR is force-pushed back to the build the preview served before.
        self.driver.refresh_status = "pass"
        self.github.pull_request["head"] = {"sha": PR_HEAD}
        self.request("preview", 5)

        restored = self.reconcile()

        self.assertEqual((restored["action"], restored["reason"]), ("wait", "ancestor_build"))
        self.assertEqual(self.driver.changes, [("refresh", 5)] * 2)

    def test_a_refresh_longer_than_the_lease_keeps_it(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)
        claims: list[object] = []
        started_at = datetime(2026, 10, 3, tzinfo=timezone.utc)
        observed_at = started_at
        advance = Event()
        renewed = Event()
        finished = Event()
        refresh_started = Event()
        renewals: list[bool] = []
        lease_expiries: list[str] = []
        failures: list[str] = []
        renew_lease = self.store.renew_product_reconcile_lease

        def renew(target_key: str, lease_owner: str, lease_seconds: int, *, now: str = "") -> bool:
            background = current_thread() is not main_thread() and refresh_started.is_set()
            if background and len(renewals) < 2 and not finished.is_set():
                if not advance.wait(30):
                    raise TimeoutError("The fixture did not advance the lease clock.")
                advance.clear()
            result = renew_lease(target_key, lease_owner, lease_seconds, now=now)
            if background:
                renewals.append(result)
                renewed.set()
            return result

        def outlast_the_lease() -> None:
            nonlocal observed_at
            refresh_started.set()
            try:
                # Advance only after the real heartbeat commits its previous renewal.
                # Scheduler pressure cannot silently consume this fixture's lease.
                for elapsed in (1, 2):
                    observed_at = started_at + timedelta(seconds=elapsed)
                    renewed.clear()
                    advance.set()
                    if not renewed.wait(30):
                        failures.append("The background heartbeat did not renew.")
                        return
                    lease_expiries.append(
                        self.store.read_product_reconcile_request("site:preview:5").lease_expires_at
                    )
                observed_at = started_at + timedelta(seconds=3)
                claims.append(self.store.claim_next_product_reconcile_request("worker-b", 2))
            finally:
                finished.set()
                advance.set()

        self.driver.during_refresh = outlast_the_lease
        with (
            patch.object(
                self.store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: observed_at.isoformat().replace("+00:00", "Z"),
            ),
            patch.object(self.store, "renew_product_reconcile_lease", side_effect=renew),
        ):
            completed = run_product_reconcile_once(
                record_store=self.store,
                lease_owner="worker-a",
                lease_seconds=2,
                transport_factory=lambda _store, _profile: self.github,
                control_plane_root=self.root,
                preview_hooks=self.provider.hooks(),
                feedback_token=lambda _store, _profile: "feedback-token",
                public_origin=lambda: self.public_origin,
            )

        assert completed is not None
        self.assertEqual(failures, [])
        self.assertTrue(renewals and all(renewals))
        self.assertEqual(
            lease_expiries,
            [
                (started_at + timedelta(seconds=elapsed)).isoformat().replace("+00:00", "Z")
                for elapsed in (3, 4)
            ],
        )
        self.assertEqual((completed.state, completed.last_error), ("done", ""))
        self.assertEqual(claims, [None])

    def test_a_worker_that_lost_its_lease_records_nothing_more(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)

        def lose_the_lease() -> None:
            # The lease expired during the refresh and another worker claimed the PR.
            self.store.renew_product_reconcile_lease(
                "site:preview:5", "worker-a", 1, now="2026-01-01T00:00:00Z"
            )
            self.store.claim_next_product_reconcile_request("worker-b", 600)

        self.driver.during_refresh = lose_the_lease
        with self.assertRaises(ProductReconcileLeaseLostError):
            self.run_once()

        (preview,) = self.store.list_preview_records()
        self.assertNotEqual(preview.state, "active")
        self.assertEqual(self.driver.changes, [("refresh", 5)])

    def test_a_refresh_whose_verification_was_not_recorded_is_run_again(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)
        with patch(
            "control_plane.product_reconcile.apply_generic_web_preview_verification_result",
            side_effect=RuntimeError("worker stopped"),
        ):
            self.assertEqual(self.run_once().state, "failed")
        self.request("preview", 5)

        recovered = self.reconcile()

        self.assertEqual(
            (recovered["action"], recovered["preview_result_status"]), ("apply", "pass")
        )
        (preview,) = self.store.list_preview_records()
        self.assertEqual(preview.state, "active")
        self.assertEqual(preview.serving_generation_id, preview.active_generation_id)

    def test_a_pr_that_moves_before_the_provider_change_is_reconciled_again(self) -> None:
        self.github.add_run(50, PR_HEAD, event="pull_request")
        # Reads: the plan, the build verification, then the check just before refreshing.
        self.github.pull_request_move = (2, {"head": {"sha": "b" * 40}})
        self.request("preview", 5)

        completed = self.run_once()

        self.assertEqual(completed.state, "pending")
        self.assertEqual(completed.last_plan["deferred"], "pull_request_moved")
        self.assertEqual(self.driver.changes, [])

    def test_the_reconcile_may_change_only_the_products_own_previews(self) -> None:
        def allowed(*, context: str = "site-preview", slug: str = "pr-5") -> bool:
            return launchplane_reconcile_preview_destination_allowed(
                record_store=self.store, product="site", context=context, preview_slug=slug
            )

        self.assertTrue(allowed())
        self.assertFalse(allowed(context="cm"))
        self.assertFalse(allowed(slug="prod"))
        self.assertFalse(allowed(slug="testing"))


class ProductReconcilePreviewFeedbackTests(ProductReconcileTestCase):
    def setUp(self) -> None:
        super().setUp()
        clock = patch("control_plane.odoo_preview_apply_http.datetime", _MinuteClock)
        clock.start()
        self.addCleanup(clock.stop)

    def reconcile_preview(self) -> dict[str, object]:
        self.request("preview", 5)
        return self.reconcile()

    def comment_body(self) -> str:
        (comment,) = self.comments.on(5)
        return cast(str, comment["body"])

    def sweep_preview(self) -> bool:
        targets = request_product_reconcile_sweep(
            self.store, "2026-09-30T12:00:00Z", lambda _store, _profile: self.github
        )
        return "site:preview:5" in targets

    def run_swept_preview(self) -> ProductReconcileRequestRecord:
        # Use the real worker even when the testing target is claimed first.
        completed = self.run_once()
        return completed if completed.target_kind == "preview" else self.run_once()

    def test_sweep_recovers_a_closed_absent_preview_after_a_failed_pr_read(self) -> None:
        self.reconcile_preview()
        self.github.pull_request["state"] = "closed"
        self.request("preview", 5)
        with patch.object(self.github, "get_json", side_effect=ProductReconcileError("refused")):
            failed = self.run_once()
        self.assertEqual(failed.state, "failed")
        self.assertIn("Waiting for:", self.comment_body())
        self.assertEqual(self.store.list_preview_records(), ())

        self.assertTrue(self.sweep_preview())
        recovered = self.run_swept_preview()

        feedback = cast(dict[str, object], recovered.last_plan["pr_feedback"])
        self.assertEqual(
            (feedback["status"], feedback["delivery_status"]), ("cleared", "delivered")
        )
        self.assertEqual(self.comments.on(5), [])
        self.assertEqual(self.provider.applied, [])
        self.assertFalse(self.sweep_preview())

    def test_sweep_recovers_failed_clear_delivery_without_another_close_event(self) -> None:
        self.reconcile_preview()
        self.github.pull_request["state"] = "closed"
        self.comments.fail_writes = True
        self.reconcile_preview()
        self.assertIn("Waiting for:", self.comment_body())
        self.comments.fail_writes = False

        self.assertTrue(self.sweep_preview())
        recovered = self.run_swept_preview()

        self.assertEqual(
            cast(dict[str, object], recovered.last_plan["pr_feedback"])["delivery_status"],
            "delivered",
        )
        self.assertEqual(self.comments.on(5), [])
        self.assertFalse(self.sweep_preview())

    def test_unreadable_closed_pr_stops_recovery_and_a_new_event_can_resume(self) -> None:
        self.reconcile_preview()
        self.github.pull_request["state"] = "closed"
        self.request("preview", 5)
        with patch.object(self.github, "get_json", side_effect=ProductReconcileError("denied")):
            self.run_once()
            recoveries = 0
            while self.sweep_preview():
                failed = self.run_swept_preview()
                recoveries += 1
                self.assertLessEqual(
                    recoveries,
                    PREVIEW_FEEDBACK_RECOVERY_MAX_FAILED_ATTEMPTS,
                    "unbounded feedback recovery",
                )
        self.assertIn("Waiting for:", self.comment_body())
        self.assertEqual(self.provider.applied, [])
        self.assertGreater(recoveries, 0)
        self.assertIn("feedback_recovery_stop_reason", failed.last_plan)
        self.assertFalse(self.sweep_preview())

        self.reconcile_preview()

        self.assertEqual(self.comments.on(5), [])

    def test_sweep_does_not_guess_failed_or_legacy_feedback_is_pending(self) -> None:
        self.github.pull_request["state"] = "closed"
        for feedback in (
            {"status": "failed", "delivery_status": "failed"},
            {"delivery_status": "failed"},
        ):
            with self.subTest(feedback=feedback):
                self.request("preview", 5)
                request = self.store.claim_next_product_reconcile_request("seed", 600)
                assert request is not None
                self.store.complete_product_reconcile_request(
                    request.target_key, "seed", "failed", {"pr_feedback": feedback}, "real failure"
                )
                self.assertFalse(self.sweep_preview())

    def test_recovery_discovers_feedback_beyond_the_default_history_limit(self) -> None:
        for number in range(10, 120):
            self.store.request_product_reconcile(
                ProductReconcileTarget(
                    product="site", target_kind="preview", pull_request_number=number
                ),
                "2026-09-28T12:00:00Z",
            )
            request = self.store.claim_next_product_reconcile_request("seed", 600)
            assert request is not None
            self.store.complete_product_reconcile_request(request.target_key, "seed", "done", {})
        self.reconcile_preview()
        self.github.pull_request["state"] = "closed"
        targets = request_product_reconcile_sweep(
            self.store, "2026-09-30T12:00:00Z", lambda _store, _profile: self.github
        )
        self.assertIn("site:preview:5", targets)

    def test_failed_clear_delivery_stops_without_erasing_the_pending_comment(self) -> None:
        self.reconcile_preview()
        self.github.pull_request["state"] = "closed"
        self.comments.fail_writes = True
        self.reconcile_preview()
        recoveries = 0
        while self.sweep_preview():
            failed = self.run_swept_preview()
            recoveries += 1
            self.assertLessEqual(
                recoveries,
                PREVIEW_FEEDBACK_RECOVERY_MAX_FAILED_ATTEMPTS,
                "unbounded failed deletion recovery",
            )
        self.assertGreater(recoveries, 0)
        self.assertIn("feedback_recovery_stop_reason", failed.last_plan)
        self.assertIn("Waiting for:", self.comment_body())
        self.assertEqual(self.provider.applied, [])

    def test_ended_preview_does_not_hide_failed_pending_cleanup_from_the_sweep(self) -> None:
        for state in ("destroyed", "teardown_pending"):
            with self.subTest(state=state):
                self.github.pull_request["state"] = "open"
                self.reconcile_preview()
                self.write_preview(number=5, state=state)
                self.github.pull_request["state"] = "closed"
                self.comments.fail_writes = True
                self.reconcile_preview()
                self.comments.fail_writes = False

                self.assertTrue(self.sweep_preview())
                recovered = self.run_swept_preview()

                self.assertEqual(recovered.last_plan["action"], "none")
                self.assertEqual(self.comments.on(5), [])
                self.assertEqual(self.provider.applied, [])

    def test_a_verified_close_gets_cleanup_retries_after_open_pr_reads_exhausted(self) -> None:
        self.reconcile_preview()
        for _ in range(PREVIEW_FEEDBACK_RECOVERY_MAX_FAILED_ATTEMPTS):
            self.request("preview", 5)
            with patch.object(self.github, "get_json", side_effect=OSError("GitHub unavailable")):
                self.run_once()
        self.assertFalse(self.sweep_preview())
        self.github.pull_request["state"] = "closed"
        self.comments.fail_writes = True
        self.reconcile_preview()
        self.comments.fail_writes = False

        self.assertTrue(self.sweep_preview())
        recovered = self.run_swept_preview()

        self.assertEqual(recovered.last_plan["action"], "none")
        self.assertEqual(self.comments.on(5), [])

    def test_read_failures_after_verified_close_do_not_reset_the_cleanup_budget(self) -> None:
        self.reconcile_preview()
        self.github.pull_request["state"] = "closed"
        self.comments.fail_writes = True
        self.reconcile_preview()
        recoveries = 0
        while self.sweep_preview():
            with patch.object(self.github, "get_json", side_effect=OSError("GitHub unavailable")):
                failed = self.run_swept_preview()
            recoveries += 1
            self.assertLessEqual(recoveries, PREVIEW_FEEDBACK_RECOVERY_MAX_FAILED_ATTEMPTS)
        self.assertGreater(recoveries, 0)
        self.assertIn("feedback_recovery_stop_reason", failed.last_plan)
        self.assertIn("Waiting for:", self.comment_body())

    def test_close_webhook_rearms_exhausted_open_read_recovery_once(self) -> None:
        self.reconcile_preview()
        with patch.object(self.github, "get_json", side_effect=OSError("GitHub unavailable")):
            for _ in range(PREVIEW_FEEDBACK_RECOVERY_MAX_FAILED_ATTEMPTS):
                self.request("preview", 5)
                self.run_once()
        self.assertFalse(self.sweep_preview())
        self.github.pull_request["state"] = "closed"
        target = ProductReconcileTarget(
            product="site", target_kind="preview", pull_request_number=5
        )
        delivery = GitHubAppWebhookDeliveryRecord(
            delivery_id="close-during-outage",
            event="pull_request",
            action="closed",
            repository_id=REPOSITORY_ID,
            received_at="2026-09-30T12:00:00Z",
            target_keys=(target.target_key,),
        )
        self.store.record_github_app_webhook_delivery(delivery, (target,), delivery.received_at)
        with patch.object(self.github, "get_json", side_effect=OSError("GitHub unavailable")):
            self.run_swept_preview()
        self.assertEqual(
            self.store.record_github_app_webhook_delivery(
                delivery, (target,), delivery.received_at
            ),
            "duplicate",
        )
        self.assertTrue(self.sweep_preview())
        recovered = self.run_swept_preview()
        self.assertEqual(recovered.last_plan["action"], "none")
        self.assertEqual(self.comments.on(5), [])
        self.assertEqual(self.provider.applied, [])

    def test_closing_before_a_preview_build_clears_pending_feedback(self) -> None:
        self.assertEqual(self.reconcile_preview()["action"], "wait")
        self.assertIn("Waiting for: a verified build", self.comment_body())
        self.comments.comments[5].append({"id": 1000, "body": "Unrelated review comment"})
        self.github.pull_request["state"] = "closed"

        closed = self.reconcile_preview()

        self.assertEqual((closed["action"], closed["reason"]), ("none", "pull_request_not_open"))
        self.assertEqual(self.comments.on(5), [{"id": 1000, "body": "Unrelated review comment"}])
        feedback = cast(dict[str, object], closed["pr_feedback"])
        self.assertEqual(
            (feedback["status"], feedback["delivery_status"]), ("cleared", "delivered")
        )
        self.assertEqual(self.store.list_preview_records(), ())
        self.assertEqual(self.provider.applied, [])
        self.reconcile_preview()
        self.assertEqual(self.comments.writes, [("POST", 5), ("DELETE", 5)])
        record = max(
            self.store.list_preview_pr_feedback_records(context_name="cm"),
            key=lambda item: item.requested_at,
        )
        self.assertEqual((record.status, record.delivery_status), ("cleared", "delivered"))

    def test_failed_pending_feedback_is_not_created_when_the_pr_closes(self) -> None:
        self.comments.fail_writes = True
        self.reconcile_preview()
        self.comments.fail_writes = False
        self.github.pull_request["state"] = "closed"

        closed = self.reconcile_preview()

        feedback = cast(dict[str, object], closed["pr_feedback"])
        self.assertEqual(feedback["status"], "cleared")
        self.assertEqual(feedback["delivery_status"], "delivered")
        self.assertEqual(feedback["delivery_action"], "no_existing_comment")
        self.assertEqual(self.comments.writes, [])
        records = self.store.list_preview_pr_feedback_records(context_name="cm")
        self.reconcile_preview()
        self.assertEqual(self.store.list_preview_pr_feedback_records(context_name="cm"), records)

    def test_pending_feedback_audit_failure_keeps_cleanup_history(self) -> None:
        with patch.object(self.store, "write_preview_pr_feedback_record", side_effect=RuntimeError):
            pending = cast(dict[str, object], self.reconcile_preview()["pr_feedback"])
        self.assertEqual((pending["status"], pending["delivery_status"]), ("pending", "failed"))
        self.assertIn("Waiting for:", self.comment_body())
        self.github.pull_request["state"] = "closed"

        cleared = cast(dict[str, object], self.reconcile_preview()["pr_feedback"])

        self.assertEqual((cleared["status"], cleared["delivery_status"]), ("cleared", "delivered"))
        self.assertEqual(self.comments.on(5), [])

    def test_cleanup_audit_failure_keeps_status_without_a_stale_delivery_receipt(self) -> None:
        self.reconcile_preview()
        self.github.pull_request["state"] = "closed"
        with patch.object(self.store, "write_preview_pr_feedback_record", side_effect=RuntimeError):
            failed = cast(dict[str, object], self.reconcile_preview()["pr_feedback"])
        self.assertEqual((failed["status"], failed["delivery_status"]), ("cleared", "failed"))
        self.assertNotIn("feedback_id", failed)
        self.assertNotIn("delivery_action", failed)
        self.assertEqual(self.comments.on(5), [])

        recovered = cast(dict[str, object], self.reconcile_preview()["pr_feedback"])

        self.assertEqual(recovered["delivery_status"], "delivered")
        record = next(
            item
            for item in self.store.list_preview_pr_feedback_records(context_name="cm")
            if item.feedback_id == recovered["feedback_id"]
        )
        self.assertEqual(record.status, "cleared")

    def test_failed_pending_feedback_cleanup_retries_on_the_next_reconcile(self) -> None:
        self.reconcile_preview()
        self.github.pull_request["state"] = "closed"
        self.comments.fail_writes = True

        failed = cast(dict[str, object], self.reconcile_preview()["pr_feedback"])

        self.assertEqual((failed["status"], failed["delivery_status"]), ("cleared", "failed"))
        self.assertIn("Waiting for:", self.comment_body())
        self.comments.fail_writes = False
        delivered = cast(dict[str, object], self.reconcile_preview()["pr_feedback"])
        self.assertEqual(delivered["delivery_status"], "delivered")
        self.assertEqual(self.comments.on(5), [])
        self.reconcile_preview()
        self.assertEqual(self.comments.writes, [("POST", 5), ("DELETE", 5)])

    def test_closed_pr_without_feedback_history_posts_nothing(self) -> None:
        self.github.pull_request["state"] = "closed"

        self.assertEqual(self.reconcile_preview()["action"], "none")

        self.assertEqual(self.comments.writes, [])
        self.assertEqual(self.store.list_preview_pr_feedback_records(context_name="cm"), ())

    def test_preview_result_is_one_comment_edited_in_place(self) -> None:
        self.assertEqual(self.reconcile_preview()["action"], "wait")
        self.assertIn("Waiting for: a verified build of this commit", self.comment_body())
        self.reconcile_preview()

        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.assertEqual(self.reconcile_preview()["action"], "apply")
        self.assertIn("preview is ready for PR #5", self.comment_body())
        self.assertIn("https://pr-5.example.test", self.comment_body())
        self.assertIn("Close or merge the PR to remove the preview.", self.comment_body())
        self.assertEqual(self.reconcile_preview()["reason"], "already_serving")

        self.github.pull_request["state"] = "closed"
        destroyed = self.reconcile_preview()

        self.assertIn("retired the preview for PR #5", self.comment_body())
        # Saying the same thing again (a second wait, a kept preview) writes nothing.
        self.assertEqual(self.comments.writes, [("POST", 5), ("PATCH", 5), ("PATCH", 5)])
        self.assertEqual(set(self.comments.tokens), {"feedback-token"})
        feedback = cast(dict[str, object], destroyed["pr_feedback"])
        self.assertEqual(
            (feedback["status"], feedback["delivery_status"], feedback["delivery_action"]),
            ("destroyed", "delivered", "updated_comment"),
        )
        # Feedback record ids carry the request second, so a run that crosses a second
        # boundary keeps one record per second; the newest is the retirement.
        record = max(
            self.store.list_preview_pr_feedback_records(context_name="cm"),
            key=lambda item: item.requested_at,
        )
        self.assertEqual((record.status, record.delivery_status), ("destroyed", "delivered"))

    def test_failed_preview_says_why_on_the_pr(self) -> None:
        def failed_apply(**_kwargs: object) -> dict[str, object]:
            return {
                "status": "fail",
                "error_message": "Dokploy compose deploy failed: image pull denied.",
                "provider_effect_attempted": True,
            }

        self.provider.execute_apply = failed_apply  # type: ignore[method-assign]
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)

        self.assertEqual(self.run_once().state, "failed")

        self.assertIn("preview refresh failed for PR #5", self.comment_body())
        self.assertIn(record_failure_summary("reconcile_required"), self.comment_body())
        self.assertNotIn("image pull denied", self.comment_body())

    def test_ready_preview_mentions_the_owner_only_on_a_marked_pr(self) -> None:
        payload = _profile()
        payload["owner"] = {"github_login": "site-owner", "github_id": "4242"}
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(payload)
        )
        self.github.add_run(50, PR_HEAD, event="pull_request")
        cases: tuple[tuple[int, list[str], str, str | None], ...] = (
            (5, ["owner-review"], self.public_origin, "mentioned"),
            (6, [], self.public_origin, None),
            (7, ["owner-review"], "", "no_public_origin"),
        )
        for number, labels, origin, owner_review in cases:
            with self.subTest(number=number):
                self.github.pull_request["body"] = "## Client test notes\n" + (
                    "Try the changed page."
                    if labels
                    else "Nothing for the Client to test: Build plumbing only."
                )
                self.github.pull_request["labels"] = [{"name": label} for label in labels]
                self.public_origin = origin
                self.request("preview", number)

                feedback = cast(dict[str, object], self.reconcile()["pr_feedback"])

                (comment,) = self.comments.on(number)
                body = cast(str, comment["body"])
                self.assertEqual(feedback["status"], "ready")
                self.assertEqual(feedback.get("owner_review"), owner_review)
                if owner_review == "mentioned":
                    self.assertIn("@site-owner this change is ready for you to look at.", body)
                    link = owner_review_reference_url(
                        public_origin="https://launchplane.example.test",
                        repository=REPOSITORY,
                        pull_request_number=number,
                    )
                    self.assertIn(link, body)
                else:
                    self.assertNotIn("@site-owner", body)

    def test_explicit_review_mark_without_client_explains_the_hold_on_the_preview(self) -> None:
        self.github.pull_request["body"] = "## Client test notes\nTry the price list."
        self.github.pull_request["labels"] = [{"name": "owner-review"}]
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)
        self.assertEqual(self.reconcile()["action"], "apply")
        self.assertIn("no Client is set", self.comment_body())
        self.assertIn("An admin needs to set one", self.comment_body())

    def test_notes_without_label_invite_client_and_missing_notes_are_visible(self) -> None:
        payload = _profile()
        payload["owner"] = {"github_login": "site-owner", "github_id": "4242"}
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(payload)
        )
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.github.pull_request["body"] = "## Client test notes\nTry the changed price list."
        self.request("preview", 5)
        result = self.reconcile()
        self.assertEqual(result["action"], "apply")
        self.assertIn("@site-owner", self.comment_body())
        calls_before_refusal = tuple(self.owner_review_status_calls)
        self.github.pull_request["body"] = "## Client test notes\n<!-- fill me in -->"
        self.request("preview", 5)
        result = self.reconcile()
        self.assertEqual(result["reason"], "client_test_notes_required")
        self.assertIn("Client test notes are missing", self.comment_body())
        self.assertEqual(
            tuple(self.owner_review_status_calls), calls_before_refusal + (("site", 5),)
        )

    def test_pure_dependabot_without_build_requests_no_client_review(self) -> None:
        payload = _profile()
        payload["owner"] = {"github_login": "site-owner", "github_id": "4242"}
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(payload)
        )
        self.github.pull_request.update(
            {
                "body": "Bumps a dependency.",
                "user": {"login": "dependabot[bot]", "type": "Bot"},
                "head": {"sha": PR_HEAD, "repo": {"full_name": REPOSITORY}},
                "base": {"sha": DEPLOYABLE, "repo": {"full_name": REPOSITORY}},
            }
        )
        read = self.github.get_json
        with patch.object(
            self.github,
            "get_json",
            side_effect=lambda path: (
                {"files": [{"filename": "uv.lock", "status": "modified"}]}
                if "/compare/" in path
                else read(path)
            ),
        ):
            result = self.reconcile_preview()
        self.assertEqual(result["reason"], "no_verified_build")
        self.assertIs(result["owner_review_requested"], False)
        self.assertNotIn("@site-owner", self.comment_body())
        self.assertEqual(self.provider.applied, [])

    def test_notes_status_refreshes_without_a_verified_build_or_comment_change(self) -> None:
        payload = _profile()
        payload["owner"] = {"github_login": "site-owner", "github_id": "4242"}
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(payload)
        )
        self.github.pull_request["body"] = "## Client test notes\n"
        self.assertEqual(self.reconcile_preview()["reason"], "client_test_notes_required")
        self.owner_review_status_calls.clear()

        self.github.pull_request["body"] = (
            "## Client test notes\nNothing for the Client to test: CI tools only."
        )
        for _ in range(2):
            result = self.reconcile_preview()
            self.assertEqual(result["reason"], "no_verified_build")
            self.assertIs(result["owner_review_requested"], False)
        self.assertEqual(self.owner_review_status_calls, [("site", 5), ("site", 5)])
        self.assertEqual(self.provider.applied, [])
        self.assertNotIn("@site-owner", self.comment_body())

    def test_notes_status_refreshes_when_preview_apply_fails(self) -> None:
        payload = _profile()
        payload["owner"] = {"github_login": "site-owner", "github_id": "4242"}
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(payload)
        )
        self.github.pull_request["body"] = "## Client test notes\n"
        self.assertEqual(self.reconcile_preview()["reason"], "client_test_notes_required")
        self.owner_review_status_calls.clear()
        self.github.pull_request["body"] = (
            "## Client test notes\nNothing for the Client to test: CI tools only."
        )
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.provider.execute_apply = lambda **_kwargs: {  # type: ignore[method-assign]
            "status": "fail",
            "provider_effect_attempted": True,
        }

        self.request("preview", 5)
        completed = self.run_once()
        self.assertEqual(completed.state, "failed")
        result = completed.last_plan

        self.assertEqual(cast(dict[str, object], result["pr_feedback"])["status"], "failed")
        self.assertEqual(self.owner_review_status_calls, [("site", 5)])

    def test_review_publisher_runs_when_unknown_preview_recovery_fails(self) -> None:
        from github_plan_status.client_test_notes import ENGINEERING_RELEASE_TESTS

        payload = _profile()
        payload["owner"] = {"github_login": "site-owner", "github_id": "4242"}
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(payload)
        )
        self.github.pull_request["body"] = "## Client test notes\nTry the changed price list."
        self.github.add_run(50, PR_HEAD, event="pull_request")
        with patch.object(
            self.provider,
            "execute_apply",
            return_value={"status": "fail", "provider_effect_attempted": True},
        ) as apply:
            self.request("preview", 5)
            self.assertEqual(self.run_once().state, "failed")
            self.assertEqual(apply.call_count, 1)
            self.owner_review_status_calls.clear()
            writes = list(self.comments.writes)
            held_before = self.store.list_held_odoo_preview_reservations(product="site")
            self.github.pull_request["body"] = (
                f"## Client test notes\n{ENGINEERING_RELEASE_TESTS}\nCheck prices at release."
            )

            self.request("preview", 5)
            held = self.run_once()

            self.assertEqual(held.state, "failed")
            self.assertEqual(apply.call_count, 1)
            self.assertEqual(
                self.store.list_held_odoo_preview_reservations(product="site"), held_before
            )
            self.assertEqual(self.owner_review_status_calls, [("site", 5)])
            self.assertEqual(self.comments.writes, writes)

    def test_review_publication_failure_preserves_preview_feedback(self) -> None:
        previous = self.reconcile_preview()["pr_feedback"]
        writes = list(self.comments.writes)

        with patch.object(
            self, "write_owner_review_status", side_effect=ValueError("Invalid public origin")
        ):
            result = self.reconcile_preview()

        self.assertEqual(result["pr_feedback"], previous)
        self.assertEqual(self.comments.writes, writes)

    def test_review_publisher_runs_for_a_deferred_preview(self) -> None:
        from control_plane.product_reconcile import ReconcileOutcome

        self.request("preview", 5)
        with patch(
            "control_plane.product_reconcile.reconcile_preview_target",
            return_value=ReconcileOutcome(
                {"target": "preview", "deferred": "preview_operation_busy"}, deferred=True
            ),
        ):
            pending = self.run_once()

        self.assertEqual(pending.state, "pending")
        self.assertEqual(self.owner_review_status_calls, [("site", 5)])
        self.assertNotIn("pr_feedback", pending.last_plan)
        self.assertEqual(self.comments.writes, [])

    def test_ready_preview_on_an_accepted_head_says_so_instead_of_asking_again(self) -> None:
        payload = _profile()
        payload["owner"] = {"github_login": "site-owner", "github_id": "4242"}
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(payload)
        )
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.github.pull_request["body"] = "## Client test notes\nTry the changed page."
        self.github.pull_request["labels"] = [{"name": "owner-review"}]
        self.owner_review_status = OwnerReviewStatus(
            state="success",
            description="Accepted by @site-owner (carried from bd92cee after a base-only refresh)",
        )

        feedback = cast(dict[str, object], self.reconcile_preview()["pr_feedback"])

        self.assertEqual(feedback.get("owner_review"), "accepted")
        self.assertEqual(self.owner_review_status_calls, [("site", 5)])
        body = self.comment_body()
        self.assertIn(
            "Accepted by site-owner (carried from bd92cee after a base-only refresh).", body
        )
        self.assertNotIn("@site-owner", body)

    def test_owner_label_added_after_the_preview_is_up_adds_the_mention(self) -> None:
        payload = _profile()
        payload["owner"] = {"github_login": "site-owner", "github_id": "4242"}
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(payload)
        )
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.assertEqual(self.reconcile_preview()["action"], "apply")
        self.assertNotIn("@site-owner", self.comment_body())

        self.github.pull_request["body"] = "## Client test notes\nTry the changed page."
        self.github.pull_request["labels"] = [{"name": "owner-review"}]
        marked = self.reconcile_preview()

        self.assertEqual(marked["reason"], "already_serving")
        self.assertIn("@site-owner this change is ready for you to look at.", self.comment_body())
        self.assertIn("https://pr-5.example.test", self.comment_body())
        self.reconcile_preview()
        self.assertEqual(self.comments.writes, [("POST", 5), ("PATCH", 5)])

    def test_the_comment_is_found_past_the_first_page(self) -> None:
        self.comments.comments[5] = [
            {"id": index, "body": f"review comment {index}"} for index in range(1, 101)
        ]
        self.comments.comments[5].append(
            {"id": 101, "body": "<!-- launchplane-reconcile-preview -->"}
        )

        self.reconcile_preview()

        self.assertEqual(len(self.comments.on(5)), 101)
        self.assertEqual(self.comments.writes, [("PATCH", 5)])
        self.assertIn("Waiting for", cast(str, self.comments.on(5)[100]["body"]))

    def test_incomplete_preview_environment_names_the_missing_keys(self) -> None:
        def incomplete_inputs(**_kwargs: object) -> dict[str, object]:
            raise OdooPreviewApplyConfigError(
                context="cm",
                instance="testing",
                missing_keys=("ODOO_DB_PASSWORD", "ODOO_ADMIN_LOGIN"),
            )

        self.provider.build_inputs = incomplete_inputs  # type: ignore[method-assign]
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)

        failed = self.run_once()

        self.assertEqual(failed.state, "failed")
        self.assertEqual(failed.last_plan["missing_keys"], ["ODOO_ADMIN_LOGIN", "ODOO_DB_PASSWORD"])
        self.assertIn("Missing: ODOO_ADMIN_LOGIN, ODOO_DB_PASSWORD.", failed.last_error)
        self.assertIn("ODOO_ADMIN_LOGIN, ODOO_DB_PASSWORD", self.comment_body())

    def test_feedback_that_cannot_be_posted_never_fails_the_preview(self) -> None:
        self.comments.fail_writes = True
        self.github.add_run(50, PR_HEAD, event="pull_request")

        applied = self.reconcile_preview()

        self.assertEqual(applied["preview_result_status"], "pass")
        self.assertEqual(self.store.list_preview_records()[0].state, "active")
        feedback = cast(dict[str, object], applied["pr_feedback"])
        self.assertEqual((feedback["status"], feedback["delivery_status"]), ("ready", "failed"))
        self.assertIn("HTTP Error 502", cast(str, feedback["error"]))
        (record,) = self.store.list_preview_pr_feedback_records(context_name="cm")
        self.assertEqual(record.delivery_status, "failed")

    def test_without_the_merge_train_app_nothing_is_posted(self) -> None:
        def no_app(_store: object, _profile: object) -> str:
            raise ProductReconcileError(
                "No pull request feedback token: the merge train policy for example/site "
                "has no GitHub App."
            )

        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)

        completed = run_product_reconcile_once(
            record_store=self.store,
            lease_owner="worker-a",
            transport_factory=lambda _store, _profile: self.github,
            control_plane_root=self.root,
            preview_hooks=self.provider.hooks(),
            feedback_token=no_app,
        )

        assert completed is not None
        self.assertEqual(completed.state, "done")
        self.assertEqual(self.provider.applied, [("refresh", 5)])
        self.assertEqual(self.comments.tokens, [])
        feedback = cast(dict[str, object], completed.last_plan["pr_feedback"])
        self.assertEqual(feedback["delivery_status"], "failed")
        self.assertIn("has no GitHub App", cast(str, feedback["error"]))


class ProductReconcileCompletionTests(ProductReconcileTestCase):
    def queued_operation(self) -> OdooStableTargetReplacementOperationRecord:
        self.github.add_run(20, DEPLOYABLE)
        self.comments.merged[DEPLOYABLE] = 12
        self.request()
        self.reconcile()
        return self.claim_operation()

    def claim_operation(self) -> OdooStableTargetReplacementOperationRecord:
        now = datetime.now(timezone.utc)
        operation = self.store.claim_next_odoo_stable_target_replacement_operation_record(
            lease_owner="deploy-worker",
            claimed_at=now.isoformat(),
            lease_expires_at=(now + timedelta(minutes=5)).isoformat(),
        )
        assert operation is not None
        return operation

    def complete(self, operation: OdooStableTargetReplacementOperationRecord, status: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        return self.store.complete_odoo_stable_target_replacement_operation_record(
            record=operation.model_copy(
                update={
                    "status": status,
                    "phase": "completed" if status == "pass" else "failed",
                    "updated_at": now,
                    "finished_at": now,
                    "error_message": "deploy failed" if status == "fail" else "",
                }
            ),
            lease_owner=operation.lease_owner,
        )

    def test_completion_publishes_testing_feedback_without_an_event_or_sweep(self) -> None:
        operation = self.queued_operation()
        manifest = self.store.read_artifact_manifest(operation.request.artifact_id)
        self.store.write_release_tuple_record(
            ReleaseTupleRecord(
                tuple_id="running-testing",
                context=operation.context,
                channel="testing",
                artifact_id=manifest.artifact_id,
                repo_shas={"site": manifest.source_commit},
                image_repository=manifest.image.repository,
                image_digest=manifest.image.digest,
                provenance="ship",
                minted_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        self.assertTrue(self.complete(operation, "pass"))
        self.assertEqual(self.store.read_product_reconcile_request("site:testing").state, "pending")
        self.assertEqual(self.reconcile()["reason"], "already_deployed")
        (comment,) = self.comments.on(12)
        self.assertIn("The testing lane runs this change.", cast(str, comment["body"]))
        self.assertEqual(len(self.store.list_odoo_stable_target_replacement_operation_records()), 1)

    def test_completion_retries_failures_with_the_existing_budget_without_a_sweep(self) -> None:
        operation = self.queued_operation()
        for attempt in range(TESTING_DEPLOY_MAX_FAILED_ATTEMPTS):
            self.assertTrue(self.complete(operation, "fail"))
            self.assertEqual(
                self.store.read_product_reconcile_request("site:testing").state, "pending"
            )
            reconciled = self.run_once()
            if attempt + 1 < TESTING_DEPLOY_MAX_FAILED_ATTEMPTS:
                self.assertEqual(reconciled.state, "done")
                operation = self.claim_operation()
            else:
                self.assertEqual(reconciled.state, "failed")
        (comment,) = self.comments.on(12)
        self.assertIn("testing deploy of this change failed", cast(str, comment["body"]))
        self.assertIsNone(
            self.store.claim_next_odoo_stable_target_replacement_operation_record(
                lease_owner="deploy-worker",
                claimed_at=datetime.now(timezone.utc).isoformat(),
                lease_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            )
        )

    def test_completion_and_reconcile_request_commit_together(self) -> None:
        operation = self.queued_operation()
        with patch.object(
            self.store, "_fold_product_reconcile_request", side_effect=RuntimeError("queue failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "queue failed"):
                self.complete(operation, "pass")
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(operation.operation_id),
            operation,
        )
        self.assertEqual(self.store.read_product_reconcile_request("site:testing").state, "done")

    def test_completion_during_another_reconcile_keeps_its_followup_pending(self) -> None:
        operation = self.queued_operation()
        self.request()
        request = self.store.claim_next_product_reconcile_request("other-worker", 300)
        assert request is not None
        self.assertTrue(self.complete(operation, "pass"))
        completed = self.store.complete_product_reconcile_request(
            request.target_key, "other-worker", "done", dict(request.last_plan)
        )
        self.assertEqual(completed.state, "pending")

    def test_pass_without_a_matching_release_does_not_announce_testing_runs_it(self) -> None:
        operation = self.queued_operation()
        self.assertTrue(self.complete(operation, "pass"))
        # A terminal operation alone cannot attest what testing currently runs.
        self.assertEqual(self.reconcile()["action"], "deploy")
        (comment,) = self.comments.on(12)
        self.assertIn("queued the testing deploy", cast(str, comment["body"]))

    def test_lost_lease_cannot_wake_reconciliation(self) -> None:
        operation = self.queued_operation()
        self.assertFalse(
            self.complete(operation.model_copy(update={"lease_owner": "stale"}), "pass")
        )
        self.assertEqual(self.store.read_product_reconcile_request("site:testing").state, "done")

    def test_nonterminal_checkpoint_does_not_wake_reconciliation(self) -> None:
        operation = self.queued_operation()
        self.assertTrue(
            self.store.complete_odoo_stable_target_replacement_operation_record(
                record=operation, lease_owner=operation.lease_owner
            )
        )
        self.assertEqual(self.store.read_product_reconcile_request("site:testing").state, "done")

    def test_independent_testing_operation_does_not_wake_event_reconciliation(self) -> None:
        from control_plane.contracts.odoo_stable_target_replacement_operation import (
            build_odoo_stable_target_replacement_operation_id,
        )

        admit = self.store.create_odoo_stable_target_replacement_operation_record_if_no_active_lane

        def independent(
            record: OdooStableTargetReplacementOperationRecord,
        ) -> tuple[OdooStableTargetReplacementOperationRecord, bool]:
            scope = "independent-testing-operation"
            return admit(
                record.model_copy(
                    update={
                        "idempotency_scope": scope,
                        "operation_id": build_odoo_stable_target_replacement_operation_id(
                            product=record.product,
                            context=record.context,
                            instance=record.instance,
                            created_at=record.created_at,
                            idempotency_key=record.idempotency_key,
                            idempotency_scope=scope,
                        ),
                    }
                )
            )

        with patch.object(
            self.store,
            "create_odoo_stable_target_replacement_operation_record_if_no_active_lane",
            side_effect=independent,
        ):
            operation = self.queued_operation()
        self.assertTrue(self.complete(operation, "pass"))
        self.assertEqual(self.store.read_product_reconcile_request("site:testing").state, "done")


class ProductReconcileTestingFeedbackTests(ProductReconcileTestCase):
    def test_merge_while_testing_is_held_says_it_waits_on_the_merged_pr(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.comments.merged[DEPLOYABLE] = 12
        self.hold_testing()
        self.request()

        self.assertEqual(self.reconcile()["reason"], "staff_testing")

        (comment,) = self.comments.on(12)
        self.assertIn(
            "Waiting: the testing lane is held for staff testing.", cast(str, comment["body"])
        )
        self.assertIn("Staff are testing checkout.", cast(str, comment["body"]))

        self.lift_testing_hold()
        queued = self.reconcile()

        (comment,) = self.comments.on(12)
        self.assertIn("queued the testing deploy", cast(str, comment["body"]))
        self.assertEqual(self.comments.writes, [("POST", 12), ("PATCH", 12)])
        feedback = cast(dict[str, object], queued["pr_feedback"])
        self.assertEqual((feedback["pull_request_number"], feedback["status"]), (12, "queued"))

    def test_hold_reason_is_redacted_before_it_reaches_the_pr(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.comments.merged[DEPLOYABLE] = 12
        self.hold_testing()
        target = self.store.read_dokploy_target_record(context_name="cm", instance_name="testing")
        hold = target.policies.staff_testing_hold
        assert hold is not None
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={
                    "policies": target.policies.model_copy(
                        update={
                            "staff_testing_hold": hold.model_copy(
                                update={
                                    "reason": "Checkout test, notes at "
                                    "https://notes.example.test/plan password=hunter2"
                                }
                            )
                        }
                    )
                }
            )
        )
        self.request()

        self.reconcile()

        (comment,) = self.comments.on(12)
        body = cast(str, comment["body"])
        self.assertIn("- Hold: Checkout test", body)
        self.assertNotIn("hunter2", body)
        self.assertNotIn("notes.example.test", body)

    def test_a_direct_push_is_not_announced_anywhere(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.request()

        feedback = cast(dict[str, object], self.reconcile()["pr_feedback"])

        self.assertEqual(feedback["delivery_action"], "no_merged_pull_request")
        self.assertEqual(self.comments.writes, [])


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
    def write_policy_without_app(self) -> None:
        record = build_test_merge_train_policy_record(repository=REPOSITORY)
        policy = record.policy.model_dump(mode="json")
        policy["policies"][0]["github_token"] = {}
        self.store.write_merge_train_policy_record(
            MergeTrainPolicyRecord(
                record_id=record.record_id,
                source=record.source,
                updated_at=record.updated_at,
                policy=MergeTrainPolicy.model_validate(policy),
            )
        )

    def test_missing_build_workflow_is_a_noop_only_with_complete_actions_inventory(self) -> None:
        scenarios: tuple[tuple[dict[str, object] | int, str, str], ...] = (
            ({"total_count": 0, "workflows": []}, "done", "build_workflow_missing"),
            ({"total_count": 1, "workflows": [{"path": BUILD_WORKFLOW_PATH}]}, "failed", ""),
            ({"total_count": 1, "workflows": []}, "failed", ""),
            ({}, "failed", ""),
            (404, "failed", ""),
            (403, "failed", ""),
        )
        for inventory, state, reason in scenarios:
            with self.subTest(inventory=inventory):
                self.request()
                original_get = self.github.get_json

                def get_json(path: str) -> object:
                    if "/actions/workflows/" in path:
                        transport = GitHubBuildProvenanceTransport(token="test")
                        with patch(
                            "control_plane.build_provenance.urlopen",
                            side_effect=HTTPError(path, 404, "Not Found", Message(), None),
                        ):
                            return transport.get_json(path)
                    if "/actions/workflows?" in path:
                        if isinstance(inventory, int):
                            transport = GitHubBuildProvenanceTransport(token="test")
                            with patch(
                                "control_plane.build_provenance.urlopen",
                                side_effect=HTTPError(path, inventory, "Denied", Message(), None),
                            ):
                                return transport.get_json(path)
                        return inventory
                    return original_get(path)

                with patch.object(self.github, "get_json", side_effect=get_json):
                    if state == "failed":
                        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                            result = self.run_once()
                    else:
                        result = self.run_once()
                self.assertEqual(result.state, state)
                if state == "done":
                    self.assertEqual(result.last_plan["action"], "none")
                    self.assertEqual(result.last_plan["reason"], reason)
                    self.assertEqual(result.last_error, "")
                else:
                    self.assertIn("GitHub read failed", result.last_error)
                self.assertEqual(self.provider.applied, [])

    def test_missing_merge_train_app_fails_without_a_token(self) -> None:
        self.request()
        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            failed = run_product_reconcile_once(record_store=self.store, lease_owner="worker-a")
        assert failed is not None
        self.assertEqual(failed.state, "failed")
        self.assertIn("active merge train policy record is missing", failed.last_error)

        self.write_policy_without_app()
        self.request()
        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            failed = run_product_reconcile_once(record_store=self.store, lease_owner="worker-a")
        assert failed is not None
        self.assertEqual(failed.state, "failed")
        self.assertIn("has no GitHub App", failed.last_error)

    def test_preview_missing_app_has_a_fixed_credential_reason(self) -> None:
        self.write_policy_without_app()
        self.request("preview", 5)
        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            failed = run_product_reconcile_once(record_store=self.store, lease_owner="worker-a")
        assert failed is not None
        self.assertEqual(failed.state, "failed")
        self.assertEqual(
            failed.last_error, record_failure_summary("preview_credentials_unavailable")
        )

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

    def test_feedback_token_is_refused_without_the_merge_train_app(self) -> None:
        # The policy's other token sources are never a fallback for the reconciler's comments.
        self.write_policy_without_app()
        with self.assertRaisesRegex(ProductReconcileError, "has no GitHub App"):
            resolve_pull_request_feedback_token(
                self.store, self.store.read_product_profile_record("site")
            )

    def test_feedback_token_is_minted_from_the_merge_train_app(self) -> None:
        profile = self.store.read_product_profile_record("site")
        self.write_merge_train_app(app_repository_id=int(REPOSITORY_ID))
        token = GitHubAppInstallationToken(
            token="comment-only",
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
                "control_plane.product_reconcile.mint_pull_request_feedback_installation_token",
                return_value=token,
            ) as mint,
        ):
            self.assertEqual(
                resolve_pull_request_feedback_token(self.store, profile), "comment-only"
            )

        self.assertEqual(mint.call_args.kwargs["identity"].app_id, 77)
        self.assertEqual(mint.call_args.kwargs["repository_id"], REPOSITORY_ID)

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
    def test_unreadable_feedback_history_keeps_other_sweep_targets(self) -> None:
        self.write_preview(number=5)
        self.github.open_pulls = [{"number": 7}]
        with patch.object(self.store, "list_product_reconcile_requests", side_effect=ValueError):
            with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                requested = request_product_reconcile_sweep(
                    self.store, "2026-09-29T12:00:00Z", lambda _store, _profile: self.github
                )
        self.assertEqual(set(requested), {"site:testing", "site:preview:5", "site:preview:7"})

    def test_sweep_requests_mapped_testing_targets_and_live_previews(self) -> None:
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_profile("unmapped"))
        )
        self.write_preview(number=5)
        self.write_preview(number=6, state="destroyed")
        # PRs 7 and 8 are open with no preview yet (their events were missed); 8 is a draft.
        self.github.open_pulls = [{"number": 7, "draft": False}, {"number": 8, "draft": True}]

        requested = request_product_reconcile_sweep(
            self.store, "2026-09-29T12:00:00Z", lambda _store, _profile: self.github
        )

        expected = {"site:testing", "site:preview:5", "site:preview:7", "site:preview:8"}
        self.assertEqual(set(requested), expected)
        self.assertEqual(
            {request.target_key for request in self.store.list_product_reconcile_requests()},
            expected,
        )

    def test_an_unreadable_pull_request_list_still_sweeps_the_rest(self) -> None:
        def no_token(_store: object, _profile: LaunchplaneProductProfileRecord) -> FakeGitHub:
            raise ProductReconcileError("No build-provenance token: minting failed.")

        self.write_preview(number=5)

        requested = request_product_reconcile_sweep(self.store, "2026-09-29T12:00:00Z", no_token)

        self.assertEqual(set(requested), {"site:testing", "site:preview:5"})


class _ClaimOrderStore:
    """Answers every worker claim with nothing and remembers the order it was asked."""

    def __init__(self) -> None:
        self.claims: list[str] = []

    def __getattr__(self, name: str) -> object:
        if name == "list_odoo_stable_target_replacement_operation_records":
            return lambda **_kwargs: ()
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
        operation_claims = {
            name
            for protocol in (
                OdooStableOperationWorkerStore,
                OdooProdPromotionOperationWorkerStore,
                OdooProdRollbackOperationWorkerStore,
            )
            for member in protocol.__mro__
            for name in vars(member)
            if name.startswith("claim_next_") and name != "claim_next_product_reconcile_request"
        }
        self.assertTrue(operation_claims)
        self.assertEqual(store.claims[-1], "claim_next_product_reconcile_request")
        self.assertCountEqual(store.claims[:-1], operation_claims)

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


class ProductArtifactScanGateTests(ProductReconcileTestCase):
    def test_read_only_qualification_works_before_activation_without_queueing(self) -> None:
        from control_plane.artifact_scan import qualify_product_artifact

        profile = self.store.read_product_profile_record("site")
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "image": profile.image.model_copy(update={"scan_enabled": False}),
                }
            )
        )
        self.github.add_run(20, DEPLOYABLE)
        result = qualify_product_artifact(self.store, product="site")
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.image_reference, f"{IMAGE_REPOSITORY}@{_digest(DEPLOYABLE)}")
        self.assertFalse(self.store.read_product_profile_record("site").image.scan_enabled)
        self.assertFalse(self.store.list_product_reconcile_requests(product="site"))
        self.assertFalse(self.store.list_odoo_stable_target_replacement_operation_records())
        self.assertFalse(self.store.list_artifact_manifests())
        self.assertFalse(self.provider.applied)

    def test_unavailable_and_vulnerable_scans_hold_before_testing_or_preview_effects(self) -> None:
        from control_plane.artifact_scan import ArtifactScanResult

        for target in ("testing", "preview"):
            for status in ("fail", "unavailable"):
                with self.subTest(target=target, status=status):
                    self.artifact_scanner.side_effect = lambda image: ArtifactScanResult(
                        image_reference=image,
                        status=cast(Any, status),
                        reason="fixture",
                        scanned_at="2026-10-16T12:00:00Z",
                    )
                    commit = PR_HEAD if target == "preview" else DEPLOYABLE
                    self.github.add_run(
                        (50 if target == "preview" else 20) + (status == "unavailable"),
                        commit,
                        event="pull_request" if target == "preview" else "push",
                    )
                    self.request(target, 5 if target == "preview" else None)
                    plan = self.reconcile()
                    self.assertTrue(plan["held"])
                    self.assertEqual(plan["action"], "wait")
                    self.assertEqual(
                        self.artifact_scanner.call_args.args,
                        (f"{IMAGE_REPOSITORY}@{_digest(commit)}",),
                    )
                    self.assertFalse(self.provider.applied)
                    self.assertFalse(
                        self.store.list_odoo_stable_target_replacement_operation_records()
                    )

    def test_preview_destroy_does_not_require_an_image_scan(self) -> None:
        self.github.pull_request["state"] = "closed"
        self.request("preview", 5)
        self.reconcile()
        self.artifact_scanner.assert_not_called()

    def test_profile_can_disable_scanning_without_changing_default_serialization(self) -> None:
        from control_plane.contracts.product_profile_record import ProductImageProfile

        self.assertNotIn(
            "scan_enabled", ProductImageProfile(repository=IMAGE_REPOSITORY).model_dump()
        )
        profile = self.store.read_product_profile_record("site")
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "image": profile.image.model_copy(update={"scan_enabled": False}),
                }
            )
        )
        self.github.add_run(20, PR_HEAD, event="pull_request")
        self.request("preview", 5)
        plan = self.reconcile()
        self.assertEqual(cast(dict[str, object], plan["artifact_scan"])["status"], "disabled")
        self.assertTrue(self.provider.applied)
        self.artifact_scanner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
