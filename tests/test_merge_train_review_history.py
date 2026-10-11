from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.merge_train_stack_collapse import (
    build_merge_train_stack_collapse_plan_record,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.merge_train import MergeTrainDryRunSnapshot, MergeTrainPullRequestSnapshot
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.merge_train_original_review import original_review_recovery_records
from control_plane.merge_train_review_history import read_original_review_history
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.http_app_test_support import _asgi_get
from tests.support.auth import _StubVerifier
from tests.support.merge_train import (
    _merge_train_service_identity,
    _merge_train_service_policy,
    _seed_merge_train_policy,
    _seed_merge_train_stack_collapse_plan_record,
)


class ReviewHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name) / "state"
        self.store = FilesystemRecordStore(self.state)
        policy = _seed_merge_train_policy(self.state)
        original_id = _seed_merge_train_stack_collapse_plan_record(self.state, policy=policy.policy)
        self.original = next(
            record
            for record in self.store.list_merge_train_stack_collapse_plan_records()
            if record.record_id == original_id
        )
        self.repository = self.original.plan.repository
        self.base = self.original.plan.base_branch

    async def test_history_is_scoped_paged_inert_and_keeps_retired_latest_progress(self) -> None:
        original_digest = read_original_review_history(
            store=self.store, repository=self.repository, base_branch=self.base, trace_id="before"
        ).history_digest
        stamp = (
            datetime.fromisoformat(self.original.updated_at.replace("Z", "+00:00"))
            + timedelta(seconds=1)
        ).isoformat()
        retired = build_merge_train_stack_collapse_plan_record(
            plan=self.original.plan.model_copy(update={"updated_at": stamp}),
            source="private-sentinel",
            updated_at=stamp,
        ).model_copy(update={"status": "superseded"})
        self.store.write_merge_train_stack_collapse_plan_record(retired)
        other = build_merge_train_stack_collapse_plan_record(
            plan=self.original.plan.model_copy(
                update={"collapse_id": self.original.plan.collapse_id + "-other"}
            ),
            source="fixture",
            updated_at=stamp,
        )
        self.store.write_merge_train_stack_collapse_plan_record(other)
        before = self.store.list_merge_train_stack_collapse_plan_records()
        with patch.object(
            self.store,
            "write_merge_train_stack_collapse_plan_record",
            side_effect=AssertionError("History read cannot write"),
        ):
            first = read_original_review_history(
                store=self.store,
                repository=self.repository,
                base_branch=self.base,
                trace_id="read-one",
                limit=1,
            )
            second = read_original_review_history(
                store=self.store,
                repository=self.repository,
                base_branch=self.base,
                trace_id="read-two",
                limit=1,
                cursor=first.next_cursor or "",
            )
            foreign = read_original_review_history(
                store=self.store,
                repository="example/another",
                base_branch=self.base,
                trace_id="foreign",
            )
        self.assertNotEqual(first.history_digest, original_digest)
        self.assertTrue(first.truncated)
        self.assertFalse(second.truncated)
        self.assertEqual(first.history_digest, second.history_digest)
        self.assertEqual(first.entries[0].record_id, retired.record_id)
        self.assertEqual(
            first.entries[0].root_initial_head_sha, self.original.plan.root_initial_head_sha
        )
        self.assertEqual(
            first.entries[0].original_changes[0].child_head_sha,
            self.original.plan.mutations[0].child_head_sha,
        )
        self.assertEqual(foreign.entries, ())
        self.assertNotIn("private-sentinel", first.model_dump_json())
        self.assertEqual(self.store.list_merge_train_stack_collapse_plan_records(), before)
        changed = read_original_review_history(
            store=self.store, repository=self.repository, base_branch=self.base, trace_id="again"
        )
        self.assertEqual(changed.history_digest, first.history_digest)

    async def test_authenticated_history_route_uses_existing_scope_without_provider_reads(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_merge_train_service_identity()),
            authz_policy=_merge_train_service_policy(),
            record_store_factory=lambda: self.store,
        )
        url = f"/v1/service/merge-train/review-history?repository={self.repository}&base_branch={self.base}"
        with (
            patch(
                "control_plane.merge_train_github.UrllibMergeTrainGitHubTransport.request",
                side_effect=AssertionError("History read cannot call provider"),
            ),
            patch.object(
                self.store,
                "write_merge_train_stack_collapse_plan_record",
                side_effect=AssertionError("History read cannot write"),
            ),
        ):
            response = await _asgi_get(app, url, headers={"Authorization": "Bearer valid-token"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["entries"][0]["record_id"], self.original.record_id)
            anonymous = await _asgi_get(app, url)
            self.assertEqual(anonymous.status_code, 401, anonymous.text)
            unavailable = await _asgi_get(
                app,
                url.replace(self.repository, "example/unconfigured"),
                headers={"Authorization": "Bearer valid-token"},
            )
            self.assertNotEqual(unavailable.status_code, 200)
            invalid_limit = await _asgi_get(
                app, url + "&limit=0", headers={"Authorization": "Bearer valid-token"}
            )
            self.assertEqual(invalid_limit.status_code, 400)

    async def test_history_denial_does_not_read_stack_history(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_merge_train_service_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: self.store,
        )
        with patch.object(
            self.store,
            "list_merge_train_stack_collapse_plan_records",
            side_effect=AssertionError("Denied read cannot inspect history"),
        ):
            response = await _asgi_get(
                app,
                f"/v1/service/merge-train/review-history?repository={self.repository}&base_branch={self.base}",
                headers={"Authorization": "Bearer valid-token"},
            )
        self.assertEqual(response.status_code, 403, response.text)

    def test_original_review_evidence_handles_squash_ownership_and_scoped_read_errors(self) -> None:
        mutation = self.original.plan.mutations[0]
        parent = MergeTrainPullRequestSnapshot(
            number=mutation.parent_pull_request_number,
            created_at=self.original.updated_at,
            head_sha="carried-parent",
            head_ref=mutation.parent_head_ref,
            base_ref=self.base,
            requires_individual_landing=True,
        )
        snapshot = MergeTrainDryRunSnapshot(
            repository=self.repository,
            base_branch=self.base,
            base_sha="current-base",
            pull_requests=(parent,),
        )

        class Reader:
            ownership = True
            merged = ""
            error_status = 0

            def branch_contains_commit(
                self, *, repository: str, branch_ref: str, commit_sha: str
            ) -> bool:
                if self.error_status:
                    raise MergeTrainGitHubError(
                        "Provider evidence unavailable", status_code=self.error_status
                    )
                return (
                    branch_ref == parent.head_sha and commit_sha == mutation.child_head_sha
                ) or (branch_ref == snapshot.base_sha and commit_sha == "squash-merge")

            def carried_change_requires_individual_landing(self, **_: object) -> bool:
                return self.ownership

            def pull_request_is_merged(
                self, *, repository: str, pull_request_number: int, expected_head_sha: str
            ) -> str:
                self_test.assertEqual(pull_request_number, mutation.child_pull_request_number)
                self_test.assertEqual(expected_head_sha, mutation.child_head_sha)
                return self.merged

        self_test = self
        reader = Reader()

        def check() -> tuple[object, ...]:
            return original_review_recovery_records(
                records=(self.original,), snapshot=snapshot, ancestry=reader
            )

        self.assertEqual(check(), (self.original,))
        reader.ownership = False
        self.assertEqual(check(), ())
        reader.ownership = True
        reader.merged = "squash-merge"
        self.assertEqual(check(), ())
        reader.merged = "merge-outside-base"
        self.assertEqual(check(), (self.original,))
        for status in (404, 503):
            reader.error_status = status
            self.assertEqual(check(), (self.original,))
        for status in (401, 403):
            reader.error_status = status
            with self.assertRaises(MergeTrainGitHubError):
                check()
