import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch as mock_patch
from urllib.parse import quote

from control_plane.contracts.merge_train_branch_refresh_record import (
    MergeTrainBranchRefreshRecord,
    build_merge_train_branch_refresh_record,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_review import ProductReviewCarry, ProductReviewDecisionRecord
from control_plane.merge_train_branch_refresh import merge_train_branch_refresh_recorder
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    RecordingMergeTrainGitHubTransport,
)
from control_plane.product_review import require_product_review_store
from control_plane.product_review_carry import carry_owner_acceptance
from control_plane.product_review_status import OwnerReviewStatusPublisher
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.profiles import product_profile_payload
from tests.support.stores import sqlite_database_url
from tests.test_product_review_status import _app_token

_REPOSITORY = "every/example-site"
_PULL_REQUEST = 92
_ACCEPTED_HEAD = "bd92ceed" + "0" * 32
_REFRESHED_HEAD = "b064ecdc" + "0" * 32
_SECOND_REFRESH_HEAD = "c0ffee00" + "0" * 32
# Another merge of the same two parents with the same change, pushed by someone else.
_OTHER_MERGE_HEAD = "d00dfeed" + "0" * 32
_REBASED_HEAD = "eeeeeeee" + "0" * 32
_BASE_COMMIT = "1111111111111111111111111111111111111111"
_NEWER_BASE_COMMIT = "2222222222222222222222222222222222222222"
_OWNER_GITHUB_ID = "9001"
_REFRESH_REQUESTED_AT = datetime(2026, 10, 2, 23, 20, tzinfo=timezone.utc)
_PATCH = "@@ -1 +1 @@\n-Old headline\n+New headline"


def _change(*, patch: str = _PATCH, blob: str = "a" * 40) -> list[dict[str, object]]:
    return [
        {
            "filename": "website/views/home.xml",
            "status": "modified",
            "sha": blob,
            "changes": 2,
            "patch": patch,
        }
    ]


class _GitHub:
    """A marked pull request and its comparisons as GitHub would serve them."""

    def __init__(self) -> None:
        self.head_sha = _REFRESHED_HEAD
        self.base_branch = "main"
        self.base_sha = _BASE_COMMIT
        self.commit_comparison: dict[str, object] = {"status": "diverged"}
        self.file_modes = {
            head: {"website/views/home.xml": "100644"}
            for head in (_ACCEPTED_HEAD, _REFRESHED_HEAD, _OTHER_MERGE_HEAD, _SECOND_REFRESH_HEAD)
        }
        self.tree_truncated = False
        self.tree_reads: list[str] = []
        self.merge_bases = {
            head: _BASE_COMMIT
            for head in (_ACCEPTED_HEAD, _REFRESHED_HEAD, _OTHER_MERGE_HEAD, _SECOND_REFRESH_HEAD)
        }
        self.base_changes: dict[tuple[str, str], list[dict[str, object]]] = {}
        # Commits on the base branch, and each head's change against a base commit.
        self.on_base = {_BASE_COMMIT, _NEWER_BASE_COMMIT}
        self.changes: dict[tuple[str, str], list[dict[str, object]]] = {
            (_BASE_COMMIT, head): _change()
            for head in (_ACCEPTED_HEAD, _REFRESHED_HEAD, _OTHER_MERGE_HEAD)
        }
        self.statuses: list[dict[str, object]] = []
        self.check_runs: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        path: str,
        token: str,
        method: str = "GET",
        body: dict[str, object] | None = None,
    ) -> object:
        repository_path = f"/repos/{_REPOSITORY}"
        if path == f"{repository_path}/branches/{quote(self.base_branch, safe='')}":
            return {"commit": {"sha": self.base_sha}} if self.base_sha in self.on_base else {}
        if path.startswith(f"{repository_path}/git/trees/"):
            self.tree_reads.append(path)
            head = path.rsplit("/", 1)[1].split("?")[0]
            return {
                "truncated": self.tree_truncated,
                "tree": [
                    {"path": filename, "mode": mode, "type": "blob"}
                    for filename, mode in self.file_modes[head].items()
                ],
            }
        if path == f"{repository_path}/pulls/{_PULL_REQUEST}":
            return {
                "body": "## Client test notes\nTry the changed page.",
                "head": {"sha": self.head_sha},
                "labels": [{"name": "owner-review"}],
                "base": {"ref": self.base_branch, "repo": {"id": 5150}},
            }
        if path == f"{repository_path}/statuses/{self.head_sha}" and method == "POST":
            assert token and body is not None
            self.statuses.insert(0, dict(body))
            return body
        if path == "/installation/token" and method == "DELETE":
            return None
        if "/check-runs?" in path:
            return {"check_runs": list(self.check_runs)}
        if path == f"{repository_path}/check-runs" and method == "POST":
            assert body is not None
            run = {"id": len(self.check_runs) + 1, "app": {"id": 77}, **body}
            self.check_runs.insert(0, run)
            self._record_check(run)
            return run
        if path.startswith(f"{repository_path}/check-runs/") and method == "PATCH":
            assert body is not None
            run = next(run for run in self.check_runs if run["id"] == int(path.rsplit("/", 1)[1]))
            run.update(body)
            self._record_check(run)
            return run
        if path.startswith(f"{repository_path}/compare/"):
            base, head = path.rsplit("/", 1)[1].split("...")
            if base == _ACCEPTED_HEAD:
                return self.commit_comparison
            if head == self.base_branch:
                return {"status": "ahead" if base in self.on_base else "diverged"}
            if head in self.on_base:
                return {"status": "ahead", "files": self.base_changes.get((base, head), [])}
            return {
                "status": "diverged",
                "merge_base_commit": {"sha": self.merge_bases[head]},
                "files": self.changes[(base, head)],
            }
        raise AssertionError(f"Unexpected GitHub request: {method} {path}")

    def _record_check(self, run: dict[str, object]) -> None:
        output = run["output"]
        assert isinstance(output, dict)
        self.statuses.insert(
            0,
            {
                "state": "pending" if run["status"] == "in_progress" else run["conclusion"],
                "description": output["title"],
            },
        )


class CarryOwnerAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.store = FilesystemRecordStore(state_dir=Path(temporary_directory.name))
        self.github = _GitHub()
        payload = product_profile_payload("example-site")
        payload["repository"] = _REPOSITORY
        payload["preview"] = {"enabled": True, "context": "example-site"}
        payload["owner"] = {"github_login": "site-owner", "github_id": _OWNER_GITHUB_ID}
        self.profile = LaunchplaneProductProfileRecord.model_validate(payload)
        self.accepted = ProductReviewDecisionRecord(
            record_id="decision-accepted",
            feedback_requested=True,
            feedback_url=f"https://github.com/{_REPOSITORY}/pull/{_PULL_REQUEST}#issuecomment-1",
            product="example-site",
            repository=_REPOSITORY,
            pull_request_number=_PULL_REQUEST,
            head_sha=_ACCEPTED_HEAD,
            preview_url="https://pr-92.example.invalid",
            decision="accepted",
            owner_github_id=_OWNER_GITHUB_ID,
            owner_github_login="site-owner",
            decided_at="2026-10-02T22:00:00.000Z",
            base_branch="main",
        )
        self.store.write_product_review_decision_record(self.accepted)

    def _train_refreshed(
        self,
        expected_head_sha: str = _ACCEPTED_HEAD,
        result_head_sha: str = _REFRESHED_HEAD,
        merged_base_sha: str = _BASE_COMMIT,
        *,
        base_branch: str = "main",
    ) -> str:
        record = build_merge_train_branch_refresh_record(
            repository=_REPOSITORY,
            base_branch=base_branch,
            pull_request_number=_PULL_REQUEST,
            expected_head_sha=expected_head_sha,
            result_head_sha=result_head_sha,
            merged_base_sha=merged_base_sha,
            requested_at=_REFRESH_REQUESTED_AT,
        )
        self.store.write_merge_train_branch_refresh_record(record)
        return record.record_id

    def _publish(self) -> dict[str, object]:
        OwnerReviewStatusPublisher(
            control_plane_root=Path("/nonexistent"),
            public_origin="https://launchplane.example.invalid",
            github_token=lambda **_: "feedback-token",
            api_request=self.github,
            github_app_token=_app_token,
        ).publish(
            store=require_product_review_store(self.store),
            profile=self.profile,
            pull_request_number=_PULL_REQUEST,
        )
        return self.github.statuses[0]

    def _decisions(self) -> tuple[ProductReviewDecisionRecord, ...]:
        return self.store.list_product_review_decision_records(
            repository=_REPOSITORY, pull_request_number=_PULL_REQUEST
        )

    def assert_waits_for_the_client(self, status: dict[str, object]) -> None:
        self.assertEqual(status["state"], "pending")
        self.assertEqual(self._decisions(), (self.accepted,))

    def _later_commits(self, *messages: str) -> list[dict[str, object]]:
        heads = [_SECOND_REFRESH_HEAD, _REFRESHED_HEAD][-len(messages) :]
        parents = [_ACCEPTED_HEAD, *heads[:-1]]
        commits: list[dict[str, object]] = [
            {"sha": head, "parents": [{"sha": parent}], "commit": {"message": message}}
            for head, parent, message in zip(heads, parents, messages, strict=True)
        ]
        self.github.commit_comparison = {
            "status": "ahead",
            "ahead_by": len(commits),
            "behind_by": 0,
            "total_commits": len(commits),
            "merge_base_commit": {"sha": _ACCEPTED_HEAD},
            "commits": commits,
        }
        return commits

    def test_test_only_followup_carries_acceptance_and_records_its_trailer(self) -> None:
        # verireel#525: an accepted product patch, then a test-only follow-up.
        commits = self._later_commits("Strengthen billing tests\n\nClient-impact: none")
        self.github.changes[(_BASE_COMMIT, _REFRESHED_HEAD)] += [
            {**_change()[0], "filename": "tests/test_billing.py"}
        ]
        self.assertEqual(self._publish()["state"], "success")
        carried, accepted = self._decisions()
        self.assertEqual(accepted, self.accepted)
        evidence = carried.model_dump()["carried_from"]
        self.assertEqual(evidence["reason"], "client_impact_none")
        self.assertEqual(
            evidence["commits"],
            ({"sha": commits[0]["sha"], "client_impact": "none"},),
        )
        self.assertEqual(carried.owner_github_id, accepted.owner_github_id)
        self.assertEqual(carried.feedback_url, accepted.feedback_url)
        self.assertEqual(evidence["comparison_base_sha"], "")
        self._publish()
        self.assertEqual(len(self._decisions()), 2)

    def test_every_later_commit_must_have_an_unambiguous_none_trailer(self) -> None:
        for message in (
            "Change tests",
            "Change behavior\n\nClient-impact: visible",
            "Mention Client-impact: none in prose",
            "Client-impact: none",
            "Client-impact: none\n\nLater body text",
            "Change tests\n\nClient-impact: none\nClient-impact: visible",
            "Change tests\n\nClient-impact: unknown",
        ):
            with self.subTest(message=message):
                self.setUp()
                # Even an identical final patch cannot erase a visible/missing claim.
                self._later_commits(message, "Change tests\n\nClient-impact: none")
                self.assert_waits_for_the_client(self._publish())

    def test_all_none_commits_carry_in_order_and_remain_bound_to_the_change(self) -> None:
        commits = self._later_commits(
            "Adjust test\n\nClient-impact: none\nSigned-off-by: Example <e@example.invalid>",
            "More tests\n\nClient-impact: none",
        )
        self.assertEqual(self._publish()["state"], "success")
        carry = self._decisions()[0].carried_from
        assert carry is not None
        self.assertEqual([commit.sha for commit in carry.commits], [c["sha"] for c in commits])
        self.github.base_branch = "release"
        self.assertEqual(self._publish()["state"], "pending")

    def test_trailer_history_uses_the_normalized_accepted_head(self) -> None:
        self.accepted = self.accepted.model_copy(update={"head_sha": _ACCEPTED_HEAD.upper()})
        self.store.write_product_review_decision_record(self.accepted)
        self._later_commits("Adjust tests\n\nClient-impact: none")
        self.assertEqual(self._publish()["state"], "success")

    def test_conflict_merge_needs_a_new_decision_even_if_marked_none(self) -> None:
        # verireel#520: resolve a conflict with an earlier landed change.
        commits = self._later_commits("Resolve billing conflict\n\nClient-impact: none")
        commits[0]["parents"] = [{"sha": _ACCEPTED_HEAD}, {"sha": _BASE_COMMIT}]
        self.github.changes[(_BASE_COMMIT, _REFRESHED_HEAD)] = _change(
            patch=_PATCH + "\n+Resolved billing behavior"
        )
        self.assert_waits_for_the_client(self._publish())
        # A matching final patch does not prove the merge was conflict-free either.
        self.github.changes[(_BASE_COMMIT, _REFRESHED_HEAD)] = _change()
        self.assert_waits_for_the_client(self._publish())

    def test_incomplete_or_unrelated_commit_history_cannot_carry(self) -> None:
        for mutation in ("truncated", "count", "diverged", "parent", "head", "duplicate"):
            with self.subTest(mutation=mutation):
                self.setUp()
                commits = self._later_commits(
                    "Adjust tests\n\nClient-impact: none", "More tests\n\nClient-impact: none"
                )
                if mutation == "truncated":
                    commits.pop(0)
                elif mutation == "count":
                    self.github.commit_comparison["total_commits"] = True
                elif mutation == "diverged":
                    self.github.commit_comparison["merge_base_commit"] = {"sha": _BASE_COMMIT}
                elif mutation == "parent":
                    commits[0]["parents"] = [{"sha": _OTHER_MERGE_HEAD}]
                elif mutation == "head":
                    commits[-1]["sha"] = _OTHER_MERGE_HEAD
                else:
                    commits[-1]["sha"] = commits[0]["sha"]
                self.assert_waits_for_the_client(self._publish())

    def test_acceptance_carries_across_the_trains_base_only_refresh(self) -> None:
        refresh_id = self._train_refreshed()

        status = self._publish()

        self.assertEqual(status["state"], "success")
        self.assertEqual(
            status["description"],
            "Accepted by @site-owner (carried from bd92cee after a base-only refresh)",
        )
        carried, accepted = self._decisions()
        self.assertEqual(accepted, self.accepted)
        self.assertEqual(
            (carried.head_sha, carried.decision, carried.base_branch),
            (_REFRESHED_HEAD, "accepted", "main"),
        )
        assert carried.carried_from is not None
        self.assertEqual(
            (
                carried.carried_from.record_id,
                carried.carried_from.head_sha,
                carried.carried_from.reason,
                carried.carried_from.refresh_record_ids,
            ),
            ("decision-accepted", _ACCEPTED_HEAD, "merge_train_base_refresh", (refresh_id,)),
        )
        # Carried, not re-decided: still the same Client and their delivered feedback.
        self.assertEqual(
            (carried.owner_github_id, carried.feedback_url),
            (_OWNER_GITHUB_ID, self.accepted.feedback_url),
        )

        self._publish()
        self.assertEqual(len(self._decisions()), 2)

    def test_acceptance_carries_across_repeated_train_refreshes(self) -> None:
        first = self._train_refreshed()
        second = self._train_refreshed(_REFRESHED_HEAD, _SECOND_REFRESH_HEAD, _NEWER_BASE_COMMIT)
        self.github.head_sha = _SECOND_REFRESH_HEAD
        self.github.changes = {
            (_NEWER_BASE_COMMIT, _ACCEPTED_HEAD): _change(),
            (_NEWER_BASE_COMMIT, _SECOND_REFRESH_HEAD): _change(),
        }

        self.assertEqual(self._publish()["state"], "success")
        carried = self._decisions()[0]
        assert carried.carried_from is not None
        self.assertEqual(carried.carried_from.refresh_record_ids, (first, second))

    def test_acceptance_carries_across_a_rebase_outside_the_train(self) -> None:
        self.github.head_sha = _REBASED_HEAD
        self.github.file_modes[_REBASED_HEAD] = self.github.file_modes[_ACCEPTED_HEAD]
        self.github.merge_bases[_REBASED_HEAD] = _BASE_COMMIT
        self.github.changes[(_BASE_COMMIT, _REBASED_HEAD)] = _change()

        self.assertEqual(self._publish()["state"], "success")
        carried, accepted = self._decisions()
        self.assertEqual(accepted, self.accepted)
        assert carried.carried_from is not None
        self.assertEqual(carried.carried_from.record_id, accepted.record_id)
        self.assertEqual(carried.carried_from.head_sha, accepted.head_sha)
        self.assertEqual(carried.carried_from.reason, "unchanged_patch_base_update")
        self.assertEqual(carried.carried_from.comparison_base_sha, self.github.base_sha)
        self.assertEqual(carried.carried_from.refresh_record_ids, ())
        self._publish()
        self.assertEqual(len(self._decisions()), 2)

    def test_unrecorded_base_merge_needs_a_new_decision(self) -> None:
        commits = self._later_commits("Merge main\n\nClient-impact: none")
        self.github.head_sha = _OTHER_MERGE_HEAD
        commits[0]["sha"] = _OTHER_MERGE_HEAD
        commits[0]["parents"] = [{"sha": _ACCEPTED_HEAD}, {"sha": _BASE_COMMIT}]
        self.assert_waits_for_the_client(self._publish())

    def test_rebase_does_not_require_ancestry_or_a_refresh_store(self) -> None:
        # Comparisons may be diverged: a rebase no longer contains the accepted head.
        self.github.head_sha = _SECOND_REFRESH_HEAD
        self.github.base_sha = _NEWER_BASE_COMMIT
        self.github.changes = {
            (_NEWER_BASE_COMMIT, _ACCEPTED_HEAD): _change(),
            (_NEWER_BASE_COMMIT, _SECOND_REFRESH_HEAD): _change(blob="b" * 40),
        }
        with mock_patch(
            "control_plane.product_review_carry.optional_merge_train_branch_refresh_read_store",
            return_value=None,
        ):
            self.assertEqual(self._publish()["state"], "success")

    def test_changed_patch_outside_the_train_needs_a_new_decision(self) -> None:
        self.github.changes[(_BASE_COMMIT, _REFRESHED_HEAD)] = _change(
            patch=_PATCH + "\n+Conflict resolution"
        )

        self.assert_waits_for_the_client(self._publish())

    def test_nontrain_update_moving_the_change_to_another_function_needs_acceptance(self) -> None:
        accepted_patch = (
            "@@ -10,2 +10,3 @@ def delete_user():\n request = parse_request()\n"
            "+ require_admin(request)\n remove_user(request)"
        )
        moved_patch = (
            "@@ -40,2 +40,3 @@ def list_users():\n request = parse_request()\n"
            "+ require_admin(request)\n return users"
        )
        self.github.changes[(_BASE_COMMIT, _ACCEPTED_HEAD)] = _change(patch=accepted_patch)
        self.github.changes[(_BASE_COMMIT, _REFRESHED_HEAD)] = _change(patch=moved_patch)

        self.assert_waits_for_the_client(self._publish())

    def test_nontrain_update_can_shift_hunk_positions_without_changing_context(self) -> None:
        self.github.base_sha = _NEWER_BASE_COMMIT
        self.github.merge_bases[_REFRESHED_HEAD] = _NEWER_BASE_COMMIT
        self.github.changes[(_NEWER_BASE_COMMIT, _ACCEPTED_HEAD)] = _change()
        self.github.changes[(_NEWER_BASE_COMMIT, _REFRESHED_HEAD)] = _change(
            patch=_PATCH.replace("@@ -1 +1 @@", "@@ -120 +120 @@"), blob="b" * 40
        )
        self.github.base_changes[(_BASE_COMMIT, _NEWER_BASE_COMMIT)] = _change(
            patch="@@ -0,0 +1,119 @@\n" + "\n".join(["+Unrelated base line"] * 119)
        )

        self.assertEqual(self._publish()["state"], "success")

    def test_relocated_identical_hunk_without_a_base_line_shift_needs_acceptance(self) -> None:
        self.github.changes[(_BASE_COMMIT, _REFRESHED_HEAD)] = _change(
            patch=_PATCH.replace("@@ -1 +1 @@", "@@ -20 +20 @@")
        )
        self.assert_waits_for_the_client(self._publish())

    def test_diff_content_line_separators_do_not_change_hunk_line_counts(self) -> None:
        content = "Unicode\u2028separator\r\f\x1c\x85end"
        self.github.base_sha = _NEWER_BASE_COMMIT
        self.github.merge_bases[_REFRESHED_HEAD] = _NEWER_BASE_COMMIT
        feature_patch = "@@ -5 +5 @@\n-Old " + content + "\n+New " + content + "\n"
        self.github.changes[(_NEWER_BASE_COMMIT, _ACCEPTED_HEAD)] = _change(patch=feature_patch)
        self.github.changes[(_NEWER_BASE_COMMIT, _REFRESHED_HEAD)] = _change(
            patch=feature_patch.replace("@@ -5 +5 @@", "@@ -6 +6 @@")
        )
        self.github.base_changes[(_BASE_COMMIT, _NEWER_BASE_COMMIT)] = _change(
            patch="@@ -0,0 +1 @@\n+" + content + "\n"
        )
        self.assertEqual(self._publish()["state"], "success")

    def test_missing_or_partial_position_evidence_needs_acceptance(self) -> None:
        self.github.merge_bases[_REFRESHED_HEAD] = ""
        self.assert_waits_for_the_client(self._publish())
        self.github.merge_bases[_REFRESHED_HEAD] = _BASE_COMMIT
        self.github.changes[(_BASE_COMMIT, _ACCEPTED_HEAD)] = _change(
            patch="@@ -1 +1 @@\n-Old headline"
        )
        self.github.changes[(_BASE_COMMIT, _REFRESHED_HEAD)] = self.github.changes[
            (_BASE_COMMIT, _ACCEPTED_HEAD)
        ]
        self.assert_waits_for_the_client(self._publish())

    def test_changed_text_does_not_fetch_head_trees(self) -> None:
        self.github.changes[(_BASE_COMMIT, _REFRESHED_HEAD)] = _change(patch=_PATCH + "\n+Extra")
        self.assert_waits_for_the_client(self._publish())
        self.assertEqual(self.github.tree_reads, [])

    def test_overlapping_base_edits_do_not_supply_a_position_proof(self) -> None:
        self.github.base_sha = _NEWER_BASE_COMMIT
        self.github.merge_bases[_REFRESHED_HEAD] = _NEWER_BASE_COMMIT
        self.github.changes[(_NEWER_BASE_COMMIT, _ACCEPTED_HEAD)] = _change()
        self.github.changes[(_NEWER_BASE_COMMIT, _REFRESHED_HEAD)] = _change()
        self.github.base_changes[(_BASE_COMMIT, _NEWER_BASE_COMMIT)] = _change()
        self.assert_waits_for_the_client(self._publish())

    def test_nearby_base_edit_context_does_not_count_as_an_overlapping_edit(self) -> None:
        self.github.base_sha = _NEWER_BASE_COMMIT
        self.github.merge_bases[_REFRESHED_HEAD] = _NEWER_BASE_COMMIT
        feature = _change(
            patch="@@ -5,3 +5,3 @@\n Context A\n-Old headline\n+New headline\n Context B"
        )
        self.github.changes[(_NEWER_BASE_COMMIT, _ACCEPTED_HEAD)] = feature
        self.github.changes[(_NEWER_BASE_COMMIT, _REFRESHED_HEAD)] = feature
        self.github.base_changes[(_BASE_COMMIT, _NEWER_BASE_COMMIT)] = _change(
            patch="@@ -7,4 +7,4 @@\n Context B\n Unchanged\n-Other old\n+Other new\n Footer"
        )
        self.assertEqual(self._publish()["state"], "success")

    def test_file_mode_changes_and_incomplete_mode_evidence_do_not_carry(self) -> None:
        for mode in ("100755", "120000", ""):
            with self.subTest(mode=mode):
                self.github.file_modes[_REFRESHED_HEAD]["website/views/home.xml"] = mode
                self.assert_waits_for_the_client(self._publish())
        self.github.file_modes[_REFRESHED_HEAD]["website/views/home.xml"] = "100644"
        self.github.tree_truncated = True
        self.assert_waits_for_the_client(self._publish())

    def test_changed_filename_whitespace_is_a_different_feature_patch(self) -> None:
        original = "website/views/home.xml"
        moved = " " + original
        added = {**_change()[0], "status": "added", "patch": "@@ -0,0 +1 @@\n+New headline"}
        self.github.changes[(_BASE_COMMIT, _ACCEPTED_HEAD)] = [added]
        self.github.changes[(_BASE_COMMIT, _REFRESHED_HEAD)] = [{**added, "filename": moved}]
        self.github.file_modes[_REFRESHED_HEAD] = {moved: "100644"}
        self.assert_waits_for_the_client(self._publish())

    def test_a_rebase_after_a_train_refresh_uses_the_strict_nontrain_proof(self) -> None:
        self._train_refreshed()
        self.github.head_sha = _OTHER_MERGE_HEAD
        self.assertEqual(self._publish()["state"], "success")
        carry = self._decisions()[0].carried_from
        assert carry is not None
        self.assertEqual(carry.reason, "unchanged_patch_base_update")
        self.assertEqual(carry.refresh_record_ids, ())

    def test_nontrain_carry_on_a_base_branch_containing_a_slash(self) -> None:
        self.github.base_branch = "release/1.0"
        self.store.write_product_review_decision_record(
            self.accepted.model_copy(update={"base_branch": self.github.base_branch})
        )
        self.assertEqual(self._publish()["state"], "success")
        self.assertEqual(self._decisions()[0].base_branch, self.github.base_branch)

    def test_base_moving_between_reads_does_not_change_the_comparison_base(self) -> None:
        comparisons: list[str] = []

        def moving_base(path: str) -> object:
            result = self.github(path=path, token="fixture-token")
            if "/compare/" in path:
                comparisons.append(path)
            if f"/compare/{_BASE_COMMIT}..." in path:
                self.github.base_sha = _NEWER_BASE_COMMIT
            return result

        carried = carry_owner_acceptance(
            store=require_product_review_store(self.store),
            profile=self.profile,
            pull_request_number=_PULL_REQUEST,
            head_sha=_REFRESHED_HEAD,
            base_branch="main",
            read=moving_base,
        )
        self.assertIsNotNone(carried)
        # The history read is separate; both feature comparisons use the pinned base.
        self.assertEqual(
            {
                path.rsplit("/", 1)[1].split("...")[0]
                for path in comparisons
                if not path.endswith(f"/{_ACCEPTED_HEAD}...{_REFRESHED_HEAD}")
            },
            {_BASE_COMMIT},
        )

    def test_unreadable_or_truncated_comparisons_do_not_carry(self) -> None:
        changes: tuple[list[dict[str, object]], ...] = (
            [],
            [{"filename": "logo.png", "status": "modified"}],
            _change() * 300,
        )
        for change in changes:
            with self.subTest(change_count=len(change)):
                self.github.changes[(_BASE_COMMIT, _REFRESHED_HEAD)] = change
                self.assert_waits_for_the_client(self._publish())

    def test_missing_or_invalid_base_revision_does_not_carry(self) -> None:
        for base in ("", "main", "a" * 39, "g" * 40):
            with self.subTest(base=base):
                self.github.base_sha = base
                self.github.on_base.add(base)
                self.assert_waits_for_the_client(self._publish())

    def test_new_client_and_undelivered_acceptance_do_not_carry(self) -> None:
        self._later_commits("Adjust tests\n\nClient-impact: none")
        for update in ({"owner_github_id": "9002"}, {"feedback_url": ""}):
            with self.subTest(update=update):
                self.store.write_product_review_decision_record(
                    self.accepted.model_copy(update=update)
                )
                self.assertEqual(self._publish()["state"], "pending")
                self.assertEqual(len(self._decisions()), 1)

    def test_an_unrelated_current_head_decision_does_not_mask_a_carry(self) -> None:
        for update in ({"owner_github_id": "9002"}, {"product": "another-product"}):
            with self.subTest(update=update):
                self.setUp()
                self._later_commits("Adjust tests\n\nClient-impact: none")
                unrelated = self.accepted.model_copy(
                    update={
                        "record_id": "unrelated-client-decision",
                        "head_sha": _REFRESHED_HEAD,
                        "decision": "changes_requested",
                        "reason": "Unrelated feedback remains saved.",
                        "decided_at": "2026-10-02T22:30:00.000Z",
                        **update,
                    }
                )
                self.store.write_product_review_decision_record(unrelated)

                self.assertEqual(self._publish()["state"], "success")
                carried, preserved, accepted = self._decisions()
                self.assertEqual(preserved, unrelated)
                self.assertEqual(accepted, self.accepted)
                assert carried.carried_from is not None
                self.assertEqual(carried.carried_from.record_id, self.accepted.record_id)

    def test_base_is_recorded_only_on_the_current_clients_applicable_decision(self) -> None:
        accepted = self.accepted.model_copy(update={"base_branch": ""})
        unrelated = accepted.model_copy(
            update={
                "record_id": "former-client-decision",
                "owner_github_id": "9002",
                "decided_at": "2026-10-02T22:30:00.000Z",
            }
        )
        self.store.write_product_review_decision_record(accepted)
        self.store.write_product_review_decision_record(unrelated)
        self.github.head_sha = _ACCEPTED_HEAD

        self.assertEqual(self._publish()["state"], "success")
        preserved, updated = self._decisions()
        self.assertEqual(preserved, unrelated)
        self.assertEqual(updated, accepted.model_copy(update={"base_branch": "main"}))

    def test_newer_client_rejection_during_comparison_is_not_overridden(self) -> None:
        def reject_during_read(path: str) -> object:
            result = self.github(path=path, token="fixture-token")
            if path.endswith(f"...{_REFRESHED_HEAD}"):
                self.store.write_product_review_decision_record(
                    self.accepted.model_copy(
                        update={
                            "record_id": "concurrent-rejection",
                            "head_sha": _REFRESHED_HEAD,
                            "decision": "changes_requested",
                            "reason": "Please revise this feature.",
                            "decided_at": "2026-10-02T22:30:00.000Z",
                        }
                    )
                )
            return result

        self.assertIsNone(
            carry_owner_acceptance(
                store=require_product_review_store(self.store),
                profile=self.profile,
                pull_request_number=_PULL_REQUEST,
                head_sha=_REFRESHED_HEAD,
                base_branch="main",
                read=reject_during_read,
            )
        )
        self.assertEqual(self._publish()["state"], "failure")
        self.assertEqual(len(self._decisions()), 2)

    def test_carry_provenance_round_trips_through_database_storage(self) -> None:
        self._later_commits("Adjust tests\n\nClient-impact: none")
        with TemporaryDirectory() as directory:
            database = PostgresRecordStore(
                database_url=sqlite_database_url(Path(directory) / "records.sqlite")
            )
            self.addCleanup(database.close)
            database.ensure_schema()
            database.write_product_review_decision_record(self.accepted)
            carried = carry_owner_acceptance(
                store=require_product_review_store(database),
                profile=self.profile,
                pull_request_number=_PULL_REQUEST,
                head_sha=_REFRESHED_HEAD,
                base_branch="main",
                read=lambda path: self.github(path=path, token="fixture-token"),
            )
            self.assertIsNotNone(carried)
            self.assertEqual(
                database.list_product_review_decision_records(
                    repository=_REPOSITORY, pull_request_number=_PULL_REQUEST
                )[0],
                carried,
            )

    def test_newer_rejection_during_trailer_read_is_not_overridden(self) -> None:
        self._later_commits("Adjust tests\n\nClient-impact: none")
        self.test_newer_client_rejection_during_comparison_is_not_overridden()

    def test_carry_contract_requires_evidence_but_reads_legacy_train_carries(self) -> None:
        legacy = {"record_id": self.accepted.record_id, "head_sha": _ACCEPTED_HEAD}
        train = ProductReviewCarry.model_validate({**legacy, "refresh_record_ids": ["refresh-1"]})
        self.assertEqual(train.reason, "merge_train_base_refresh")
        self.assertEqual(train.comparison_base_sha, "")
        for evidence in (
            legacy,
            {**legacy, "reason": "unchanged_patch_base_update"},
            {**legacy, "reason": "unchanged_patch_base_update", "comparison_base_sha": "main"},
            {**legacy, "reason": "client_impact_none"},
            {
                **legacy,
                "reason": "client_impact_none",
                "commits": [{"sha": _REFRESHED_HEAD, "client_impact": "visible"}],
            },
            {
                **legacy,
                "reason": "client_impact_none",
                "commits": [{"sha": "main", "client_impact": "none"}],
            },
            {
                **legacy,
                "reason": "client_impact_none",
                "commits": [{"sha": _REFRESHED_HEAD, "client_impact": "none"}] * 2,
            },
        ):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                ProductReviewCarry.model_validate(evidence)

    def test_acceptance_carries_when_base_edits_only_move_or_surround_the_change(
        self,
    ) -> None:
        # odoo-tenant-cm-website#92: a docs change on main edited two files the
        # pull request also changes. The lines it adds and removes are the same.
        self._train_refreshed()
        readme = (
            "@@ -79,3 +79,5 @@ Palette\n Colors\n-Old headline\n+New headline\n"
            "+Second line\n Footer"
        )
        moved_readme = (
            "@@ -133,3 +133,5 @@ Brand\n Palette colors\n-Old headline\n+New headline\n"
            "+Second line\n Footer note"
        )
        notes = "@@ -10 +10,2 @@\n Notes\n+Release note\n\\ No newline at end of file"
        accepted = _change(patch=readme) + [
            {"filename": "docs/README.md", "status": "modified", "sha": "d" * 40, "patch": notes}
        ]
        refreshed = _change(patch=moved_readme, blob="b" * 40) + [
            {"filename": "docs/README.md", "status": "modified", "sha": "e" * 40, "patch": notes}
        ]
        self.github.changes[(_BASE_COMMIT, _ACCEPTED_HEAD)] = accepted
        self.github.changes[(_BASE_COMMIT, _REFRESHED_HEAD)] = refreshed

        status = self._publish()

        self.assertEqual(status["state"], "success")
        self.assertEqual(self._decisions()[0].head_sha, _REFRESHED_HEAD)

    def test_changed_diff_needs_a_new_decision(self) -> None:
        self._train_refreshed()
        # A conflict resolution or anything else that changes the pull request's change.
        cases: dict[str, list[dict[str, object]]] = {
            "added line": _change(patch=_PATCH + "\n+Extra line"),
            "removed line": _change(patch="@@ -1 +1 @@\n-Older headline\n+New headline"),
            "line order": _change(patch="@@ -1 +1 @@\n+New headline\n-Old headline"),
            "end of file": _change(patch=_PATCH + "\n\\ No newline at end of file"),
            "status": [{**_change()[0], "status": "added"}],
            "file name": [{**_change()[0], "filename": "website/views/about.xml"}],
            "renamed from": [{**_change()[0], "previous_filename": "website/views/old.xml"}],
            "another file": _change() + [{**_change()[0], "filename": "website/b.xml"}],
            "binary": [{"filename": "logo.png", "status": "modified", "sha": "c" * 40}],
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.github.changes[(_BASE_COMMIT, _REFRESHED_HEAD)] = change

                self.assert_waits_for_the_client(self._publish())

    def test_different_base_needs_a_new_decision(self) -> None:
        cases: tuple[tuple[str, str, set[str]], ...] = (
            # Accepted on main, retargeted to release, then the train refreshed on release.
            ("accepted on another base", "release", {_BASE_COMMIT}),
            ("the merged commit is not on the base branch", "main", set()),
        )
        for name, base_branch, on_base in cases:
            with self.subTest(name):
                self.setUp()
                self._train_refreshed(base_branch=base_branch)
                self.github.base_branch = base_branch
                self.github.on_base = on_base

                self.assert_waits_for_the_client(self._publish())

    def test_carried_acceptance_stops_applying_when_the_base_changes(self) -> None:
        self._train_refreshed()
        self.assertEqual(self._publish()["state"], "success")

        # Retargeted without a new commit: the same head is a different change now.
        self.github.base_branch = "release"

        self.assertEqual((self._publish()["state"], len(self._decisions())), ("pending", 2))

    def test_acceptance_without_a_recorded_base_does_not_carry(self) -> None:
        self.store.write_product_review_decision_record(
            self.accepted.model_copy(update={"base_branch": ""})
        )
        self._train_refreshed()

        self.assertEqual(self._publish()["state"], "pending")

    def test_a_decision_keeps_the_base_it_was_first_shown_on(self) -> None:
        self.store.write_product_review_decision_record(
            self.accepted.model_copy(update={"base_branch": ""})
        )
        self.github.head_sha = _ACCEPTED_HEAD

        self.assertEqual(self._publish()["state"], "success")
        (decision,) = self._decisions()
        self.assertEqual(decision.base_branch, "main")

    def test_a_newer_decision_is_never_overridden_by_a_carry(self) -> None:
        self._train_refreshed()
        self.store.write_product_review_decision_record(
            self.accepted.model_copy(
                update={
                    "record_id": "decision-changes",
                    "decision": "changes_requested",
                    "reason": "The headline is wrong.",
                    "decided_at": "2026-10-02T22:30:00.000Z",
                }
            )
        )

        status = self._publish()

        self.assertEqual(status["state"], "pending")
        self.assertEqual(len(self._decisions()), 2)


def _refresh(client: GitHubMergeTrainClient) -> None:
    client.update_pull_request_branch(
        repository=_REPOSITORY,
        pull_request_number=_PULL_REQUEST,
        expected_head_sha=_ACCEPTED_HEAD.upper(),
    )


def _pull_request(head_sha: str) -> dict[str, object]:
    return {"head": {"sha": head_sha}}


def _commit(*parents: str) -> dict[str, object]:
    return {"parents": [{"sha": parent} for parent in parents]}


class MergeTrainBranchRefreshRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.store = FilesystemRecordStore(state_dir=Path(temporary_directory.name))
        self.waits: list[float] = []

    def _client(self, transport: RecordingMergeTrainGitHubTransport) -> GitHubMergeTrainClient:
        return GitHubMergeTrainClient(
            transport=transport,
            branch_refresh_recorder=merge_train_branch_refresh_recorder(
                store=self.store, base_branch="main", trace_id="trace-1"
            ),
            wait=self.waits.append,
        )

    def _refreshes(self) -> tuple[MergeTrainBranchRefreshRecord, ...]:
        return self.store.list_merge_train_branch_refresh_records(
            repository=_REPOSITORY, pull_request_number=_PULL_REQUEST
        )

    def test_train_records_the_merge_commit_its_refresh_made(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                {},
                # GitHub has not made the merge commit yet, then it has.
                _pull_request(_ACCEPTED_HEAD),
                _pull_request(_REFRESHED_HEAD),
                _commit(_ACCEPTED_HEAD, _BASE_COMMIT),
            )
        )

        _refresh(self._client(transport))

        (record,) = self._refreshes()
        self.assertEqual(
            (
                record.expected_head_sha,
                record.result_head_sha,
                record.merged_base_sha,
                record.base_branch,
                record.trace_id,
            ),
            (_ACCEPTED_HEAD, _REFRESHED_HEAD, _BASE_COMMIT, "main", "trace-1"),
        )
        self.assertEqual(transport.requests[0].method, "PUT")
        self.assertEqual(len(self.waits), 1)

    def test_a_head_that_is_not_the_refresh_merge_is_not_recorded(self) -> None:
        cases = {
            "someone pushed a commit": _commit(_ACCEPTED_HEAD),
            "a merge from another head": _commit(_OTHER_MERGE_HEAD, _BASE_COMMIT),
        }
        for name, commit in cases.items():
            with self.subTest(name):
                transport = RecordingMergeTrainGitHubTransport(
                    responses=({}, _pull_request(_REFRESHED_HEAD), commit)
                )

                _refresh(self._client(transport))

                self.assertEqual(self._refreshes(), ())

    def test_a_refresh_never_seen_is_not_recorded(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=({},) + (_pull_request(_ACCEPTED_HEAD),) * 20
        )

        with self.assertLogs("control_plane.merge_train_github", "INFO"):
            _refresh(self._client(transport))

        self.assertEqual(self._refreshes(), ())

    def test_database_keeps_each_pull_requests_refreshes(self) -> None:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(temporary_directory.name) / "lp.sqlite3")
        )
        self.addCleanup(store.close)
        store.ensure_schema()
        records = [
            build_merge_train_branch_refresh_record(
                repository=_REPOSITORY.upper(),
                base_branch="main",
                pull_request_number=number,
                expected_head_sha=_ACCEPTED_HEAD,
                result_head_sha=_REFRESHED_HEAD,
                merged_base_sha=_BASE_COMMIT,
                requested_at=_REFRESH_REQUESTED_AT,
            )
            for number in (_PULL_REQUEST, _PULL_REQUEST + 1)
        ]
        for record in records:
            store.write_merge_train_branch_refresh_record(record)

        self.assertEqual(
            store.list_merge_train_branch_refresh_records(
                repository=_REPOSITORY, pull_request_number=_PULL_REQUEST
            ),
            (records[0],),
        )

    def test_refresh_is_not_recorded_when_the_provider_refuses_it(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(RuntimeError("expected head moved"),)
        )

        with self.assertRaises(RuntimeError):
            _refresh(self._client(transport))

        self.assertEqual(self._refreshes(), ())

    def test_a_refresh_that_cannot_be_recorded_still_happens(self) -> None:
        def unavailable(**_: object) -> None:
            raise OSError("records are unavailable")

        transport = RecordingMergeTrainGitHubTransport(
            responses=({}, _pull_request(_REFRESHED_HEAD), _commit(_ACCEPTED_HEAD, _BASE_COMMIT))
        )

        with self.assertLogs("control_plane.merge_train_github", "WARNING"):
            _refresh(
                GitHubMergeTrainClient(transport=transport, branch_refresh_recorder=unavailable)
            )

        self.assertEqual(transport.requests[0].method, "PUT")


if __name__ == "__main__":
    unittest.main()
