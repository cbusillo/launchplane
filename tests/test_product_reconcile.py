import io
import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from control_plane.build_provenance import BUILD_WORKFLOW_PATH
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewPullRequestSummary,
)
from control_plane.contracts.preview_record import PreviewRecord, PreviewState
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_reconcile import (
    ProductReconcileLeaseLostError,
    ProductReconcileTarget,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.product_reconcile import (
    request_product_reconcile_sweep,
    run_product_reconcile_once,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.odoo_stable_operation_worker import (
    OdooStableOperationWorkerResult,
    OdooStableOperationWorkerStore,
    run_odoo_stable_operation_worker_loop,
    run_odoo_stable_operation_worker_once,
)
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
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
            return self.pull_request
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
            image_repository=IMAGE_REPOSITORY, tenant_source_repository=REPOSITORY
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


def _profile(product: str = "site", *, repository_id: str = REPOSITORY_ID) -> dict[str, object]:
    payload = _odoo_preview_profile_payload(product)
    payload.update(
        repository=REPOSITORY if product == "site" else f"example/{product}",
        image={"repository": IMAGE_REPOSITORY},
        repository_id=repository_id,
        repository_owner_id="1" if repository_id else "",
        preview={**cast(dict[str, object], payload["preview"]), "enable_label": LABEL},
    )
    return payload


class ProductReconcileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(temporary_directory.name) / "lp.sqlite3")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_profile())
        )
        self.github = FakeGitHub()

    def request(self, target_kind: str = "testing", number: int | None = None) -> str:
        target = ProductReconcileTarget.model_validate(
            {"product": "site", "target_kind": target_kind, "pull_request_number": number}
        )
        return self.store.request_product_reconcile(target, "2026-09-29T12:00:00Z").target_key

    def reconcile(self) -> dict[str, object]:
        completed = run_product_reconcile_once(
            record_store=self.store,
            lease_owner="worker-a",
            transport_factory=lambda _store, _profile: self.github,
        )
        assert completed is not None
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


class ProductReconcilePlanTests(ProductReconcileTestCase):
    def test_testing_plan_picks_newest_first_parent_build_over_a_late_older_build(self) -> None:
        self.github.add_run(20, DEPLOYABLE)
        self.github.add_run(30, OLDER)
        self.github.add_run(40, OFF_HISTORY)
        self.request()
        before = self.snapshot()

        plan = self.reconcile()

        self.assertEqual(plan["action"], "deploy")
        self.assertEqual(plan["desired_commit"], DEPLOYABLE)
        self.assertEqual(plan["desired_artifact_id"], "artifact-cm-run-20-1")
        self.assertEqual(plan["desired_image_digest"], _digest(DEPLOYABLE))
        self.assertEqual((plan["current_artifact_id"], plan["held"]), ("", True))
        self.assertEqual(plan["mode"], "plan_only")
        self.assertEqual(self.snapshot(), before)

    def test_testing_plan_is_none_when_the_release_already_has_that_digest(self) -> None:
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

    def test_preview_plans(self) -> None:
        cases: tuple[tuple[str, dict[str, object], bool, bool, str], ...] = (
            ("apply without a preview", {}, True, False, "apply"),
            ("none when serving the build", {}, True, True, "none"),
            ("destroy after close", {"state": "closed"}, True, True, "destroy"),
            ("destroy after unlabel", {"labels": []}, True, True, "destroy"),
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

                self.assertEqual(plan["action"], action)
                self.assertEqual(plan["held"], action in {"apply", "destroy"})
                self.assertEqual(self.snapshot(), before)

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


class ProductReconcileSweepTests(ProductReconcileTestCase):
    def test_sweep_requests_mapped_testing_targets_and_live_previews(self) -> None:
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_profile("unmapped", repository_id=""))
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
