from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from pathlib import Path
from typing import Any, cast
import unittest
from unittest.mock import patch

from control_plane.merge_train import (
    MergeTrainDryRunSnapshot,
    MergeTrainPullRequestSnapshot,
    build_merge_train_dry_run_result,
    merge_train_snapshot_of,
    discover_merge_train_stack,
    merge_train_stack_child_readiness_reasons,
)
from control_plane.contracts.merge_train_batch import build_merge_train_batch_candidate
from control_plane.contracts.merge_train_branch_refresh_record import (
    build_merge_train_branch_refresh_record,
)
from control_plane.merge_train_codeowners import individual_landing_snapshots
from control_plane.merge_train_controller_run_once import (
    _merge_train_candidate_matches_dry_run_queue,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainSnapshotReader,
    GitHubMergeTrainClient,
    MergeTrainGitHubError,
    RecordingMergeTrainGitHubTransport,
    _stack_collapse_commit_message,
)
from control_plane.merge_train_handoff import MergeTrainReadyHandoff, handoff_head
from control_plane.merge_train_release_hold import MergeTrainReleaseHold
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlan,
    build_merge_train_stack_collapse_plan,
    build_merge_train_stack_collapse_plan_record,
    execute_merge_train_stack_collapse_plan,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.merge_train_stack_collapse import (
    MergeTrainStackCollapseRunOnceEnvelope,
    execute_merge_train_stack_collapse_run_once,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.test_merge_train_stack_collapse import _RecordingStackCollapseBranchClient
from tests.test_merge_train import _pull_request
from tests.test_merge_train_github import (
    _check_run,
    _combined_status,
    _conversation_rule,
    _github_branch,
    _github_pull_request,
)
from tests.test_merge_train_dependency_refresh import _change
from tests.support.merge_train import _FakeExpandedMergeTrainSnapshotReader
from tests.test_merge_train_dependency_requalification import (
    _controller,
    _DependencyState,
    _post,
    _entries,
    _batch_ids,
)


HEAD = "a" * 40
REPOSITORY = "example/site"


def _ready(number: int = 1) -> MergeTrainPullRequestSnapshot:
    return _pull_request(number, labels=(), actor_id=10, repository=REPOSITORY).model_copy(
        update={
            "head_sha": HEAD,
            "ready_handoff": MergeTrainReadyHandoff(
                status="ready", head_sha=HEAD, actor_id=20, actor_role="repo_admin", comment_id=1
            ),
        }
    )


def _snapshot(*prs: MergeTrainPullRequestSnapshot) -> MergeTrainDryRunSnapshot:
    return MergeTrainDryRunSnapshot(
        repository=REPOSITORY, base_branch="main", base_sha="base", pull_requests=prs
    )


def _comment(body: str | None = None, **changes: Any) -> dict[str, Any]:
    return {
        "id": 1,
        "body": body if body is not None else f"Ready for the merge train\n\nHead: {HEAD}",
        "user": {"id": 20, "login": "reviewer"},
        "created_at": "2026-10-16T10:00:00Z",
        **changes,
    }


def _stack_fixture(
    depth: int = 2,
) -> tuple[
    MergeTrainPolicyRecord, MergeTrainStackCollapsePlan, tuple[MergeTrainPullRequestSnapshot, ...]
]:
    policy = build_test_merge_train_policy_record(repository=REPOSITORY)
    members = tuple(
        _ready(number).model_copy(
            update={
                "head_ref": f"work-{number}",
                "base_ref": "main" if number == 1 else f"work-{number - 1}",
                "head_sha": chr(96 + number) * 40,
                "ready_handoff": MergeTrainReadyHandoff(
                    status="ready",
                    head_sha=chr(96 + number) * 40,
                    actor_id=20,
                    actor_role="repo_admin",
                ),
            }
        )
        for number in range(1, depth + 1)
    )
    plan = build_merge_train_stack_collapse_plan(
        discovery_result=discover_merge_train_stack(
            policy=policy.policy, snapshot=_snapshot(*members), root_pull_request_number=1
        ),
        policy_key=policy.policy.find_repository_policy(
            repository=REPOSITORY, base_branch="main"
        ).policy_key,
        policy_sha256=policy.policy_sha256,
        created_at="2026-10-16T09:00:00Z",
    )
    return policy, plan, members


class HandoffAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = build_test_merge_train_policy(repository=REPOSITORY)

    def test_trusted_current_head_handoff_admits_without_any_label_write(self) -> None:
        for batch in (False, True):
            result = build_merge_train_dry_run_result(
                policy=self.policy,
                snapshot=_snapshot(_ready()),
                batch_landing=batch,
                new_candidate=True,
            )
            self.assertEqual(result.queue_order, (1,))
            self.assertEqual(result.intended_next_action, "merge")
            self.assertFalse(result.queue[0].labels)

    def test_unproven_heads_have_visible_reasons_and_do_not_block_other_ready_prs(self) -> None:
        cases: tuple[dict[str, object], ...] = (
            {"required_checks_status": "pending"},
            {"required_checks_status": "unknown"},
            {"required_checks_status": "fail"},
            {"mergeable": "conflicting"},
            {"mergeable": "unknown"},
            {"client_test_notes_error": "Client test notes are missing."},
            {"labels": (self.policy.policies[0].blocked_label,)},
            {"is_draft": True},
            {"state": "closed"},
            {"actor_role": "unknown", "actor_id": 999},
            {"head_sha": "b" * 40},
            {"ready_handoff": None},
        )
        for change in cases:
            with self.subTest(change=change):
                result = build_merge_train_dry_run_result(
                    policy=self.policy,
                    snapshot=_snapshot(_ready().model_copy(update=change), _ready(2)),
                    batch_landing=True,
                    new_candidate=True,
                )
                self.assertEqual(result.queue_order, (2,))
                self.assertFalse(result.queue[0].eligible)
                self.assertTrue(result.queue[0].ineligible_reasons)

    def test_bad_or_untrusted_handoffs_refuse_even_on_green_heads(self) -> None:
        for status in ("missing", "waiting", "stale", "unreadable", "ready"):
            handoff = MergeTrainReadyHandoff.model_validate(
                {"status": status, "head_sha": HEAD, "actor_id": 999, "actor_role": "write"}
            )
            result = build_merge_train_dry_run_result(
                policy=self.policy,
                snapshot=_snapshot(_ready().model_copy(update={"ready_handoff": handoff})),
                batch_landing=True,
                new_candidate=True,
            )
            self.assertFalse(result.queue_order)
            self.assertTrue(result.queue[0].ineligible_reasons)

    def test_manual_block_holds_handoff_admission_in_every_queue_mode(self) -> None:
        held = _ready().model_copy(update={"labels": (self.policy.policies[0].blocked_label,)})
        for batch in (False, True):
            result = build_merge_train_dry_run_result(
                policy=self.policy, snapshot=_snapshot(held), batch_landing=batch
            )
            self.assertFalse(result.queue_order)
            self.assertTrue(result.queue[0].ineligible_reasons)

    def test_cap_area_and_owned_boundaries_defer_with_reasons_and_keep_age_order(self) -> None:
        prs = tuple(
            _ready(number).model_copy(update={"changed_paths": (path,)})
            for number, path in ((1, "api/a.py"), (2, "api/b.py"), (3, "frontend/a.ts"))
        )
        for cap, expected in ((None, (1, 2)), (1, (1,))):
            policy = self.policy.model_copy(
                update={
                    "policies": (
                        self.policy.policies[0].model_copy(update={"max_batch_size": cap}),
                    )
                }
            )
            result = build_merge_train_dry_run_result(
                policy=policy, snapshot=_snapshot(*prs), batch_landing=True, new_candidate=True
            )
            self.assertEqual(result.queue_order, expected)
            self.assertTrue(
                all(
                    entry.deferred_reasons for entry in result.queue if entry.number not in expected
                )
            )
        owned = prs[1].model_copy(update={"requires_individual_landing": True})
        self.assertEqual(
            build_merge_train_dry_run_result(
                policy=self.policy,
                snapshot=_snapshot(prs[0], owned),
                batch_landing=True,
                new_candidate=True,
            ).queue_order,
            (1,),
        )
        features = tuple(pr.model_copy(update={"owner_review_required": True}) for pr in prs)
        self.assertEqual(
            build_merge_train_dry_run_result(
                policy=self.policy,
                snapshot=_snapshot(*features),
                batch_landing=True,
                new_candidate=True,
            ).queue_order,
            tuple(pr.number for pr in features),
        )

    def test_new_arrivals_and_grouping_do_not_rebuild_an_active_candidate(self) -> None:
        original = _ready(2).model_copy(update={"changed_paths": ("api/a.py",)})
        result = build_merge_train_dry_run_result(
            policy=self.policy, snapshot=_snapshot(original), batch_landing=True, new_candidate=True
        )
        candidate = build_merge_train_batch_candidate(
            dry_run_result=result,
            base_sha="base",
            policy_sha256="digest",
            created_at="2026-10-16T10:00:00Z",
        )
        live = _snapshot(_ready(1), original, _ready(3))
        recheck = build_merge_train_dry_run_result(
            policy=self.policy, snapshot=merge_train_snapshot_of(live, [2]), batch_landing=True
        )
        self.assertTrue(
            _merge_train_candidate_matches_dry_run_queue(
                candidate=candidate, dry_run_result=recheck, base_sha="base"
            )
        )


class GitHubHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = build_test_merge_train_policy(repository=REPOSITORY).policies[0]

    def _read(self, comments: tuple[object, ...]) -> MergeTrainReadyHandoff:
        transport = RecordingMergeTrainGitHubTransport(responses=comments)
        reader = GitHubMergeTrainSnapshotReader(transport=transport, repository_policy=self.policy)
        with patch.object(reader, "_actor_role_for_user", return_value="repo_admin"):
            handoff = reader._read_ready_handoff(
                repository_path=REPOSITORY, number=1, head_sha=HEAD
            )
        self.assertTrue(
            all(
                request.method == "GET" or request.path == "/graphql"
                for request in transport.requests
            )
        )
        return handoff

    def test_latest_trusted_handoff_cancels_ready_and_edits_are_unproven(self) -> None:
        for body, expected in (
            (f"Ready for the merge train\n\nHead: {'b' * 40}", "stale"),
            ("Engineering ready; waiting for the Client's acceptance", "waiting"),
            (f"Ready for the merge train\nHead: {HEAD}\nHead: {HEAD}", "stale"),
        ):
            with self.subTest(expected=expected):
                comments = [_comment(), _comment(body, id=2, created_at="2026-10-16T11:00:00Z")]
                self.assertEqual(self._read((comments,)).status, expected)
                comments[1]["updated_at"] = "2026-10-16T12:00:00Z"
                self.assertEqual(self._read((comments,)).status, "edited")
                comments[1]["created_at"] = comments[1]["updated_at"] = "2026-10-16T09:00:00Z"
                self.assertEqual(self._read((comments,)).status, "ready")

    def test_erased_cancellation_is_unproven_but_bound_managed_updates_do_not_cancel(self) -> None:
        edited = _comment(
            "Changed text", id=2, node_id="comment-node", updated_at="2026-10-16T11:00:00Z"
        )
        self.assertEqual(self._read(([_comment(), edited],)).status, "unreadable")
        policy = self.policy.model_copy(
            update={
                "enqueue": self.policy.enqueue.model_copy(
                    update={"trusted_automation_github_user_ids": (42,)}
                )
            }
        )
        for editor_id, expected in ((42, "ready"), (99, "edited")):
            transport = RecordingMergeTrainGitHubTransport(
                responses=(
                    [
                        _comment(user={"id": 20, "login": "example"}),
                        {**edited, "user": {"id": 20, "login": "example"}},
                    ],
                    {
                        "data": {
                            "node": {
                                "id": edited["node_id"],
                                "body": edited["body"],
                                "updatedAt": edited["updated_at"],
                                "editor": {"__typename": "Bot", "databaseId": editor_id},
                            }
                        }
                    },
                )
            )
            reader = GitHubMergeTrainSnapshotReader(transport=transport, repository_policy=policy)
            self.assertEqual(
                reader._read_ready_handoff(
                    repository_path=REPOSITORY, number=1, head_sha=HEAD
                ).status,
                expected,
            )

    def test_fresh_handoff_supersedes_older_edits_without_editor_queries(self) -> None:
        edited = _comment(
            "Older managed status",
            id=2,
            node_id="comment-node",
            user={"id": 20, "login": "example"},
            updated_at="2026-10-16T11:00:00Z",
        )
        ready = _comment(
            id=3, user={"id": 20, "login": "example"}, created_at="2026-10-16T12:00:00Z"
        )
        self.assertEqual(self._read(([edited, ready],)).status, "ready")

    def test_complete_pagination_and_unavailable_or_malformed_evidence_fail_closed(self) -> None:
        ready_page = [_comment(id=index) for index in range(1, 101)]
        wait = _comment(
            "Engineering ready; waiting for repairs", id=101, created_at="2026-10-16T11:00:00Z"
        )
        self.assertEqual(self._read((ready_page, [wait])).status, "waiting")
        response: object
        for response in ({}, MergeTrainGitHubError("unavailable"), [_comment(created_at="bad")]):
            self.assertEqual(self._read((response,)).status, "unreadable")

    def test_foreign_and_user_token_app_comments_do_not_supply_authority(self) -> None:
        for user in ({"id": 999, "login": "outsider"}, {"id": 20, "login": "reviewer"}):
            comment = _comment(user=user)
            transport = RecordingMergeTrainGitHubTransport(responses=([comment],))
            reader = GitHubMergeTrainSnapshotReader(
                transport=transport, repository_policy=self.policy
            )
            if user["id"] == 20:
                comment["performed_via_github_app"] = {"slug": "some-app"}
            with patch.object(reader, "_actor_role_for_user", return_value="write"):
                self.assertEqual(
                    reader._read_ready_handoff(
                        repository_path=REPOSITORY, number=1, head_sha=HEAD
                    ).status,
                    "missing",
                )

    def test_automation_handoff_uses_numeric_policy_identity(self) -> None:
        policy = self.policy.model_copy(
            update={
                "enqueue": self.policy.enqueue.model_copy(
                    update={"trusted_automation_github_user_ids": (20,)}
                )
            }
        )
        for actor_id, expected in ((20, "ready"), (21, "missing")):
            transport = RecordingMergeTrainGitHubTransport(
                responses=(
                    [_comment(user={"id": actor_id, "login": "automation[bot]", "type": "Bot"})],
                    MergeTrainGitHubError("No collaborator permission", status_code=404),
                )
            )
            reader = GitHubMergeTrainSnapshotReader(transport=transport, repository_policy=policy)
            self.assertEqual(
                reader._read_ready_handoff(
                    repository_path=REPOSITORY, number=1, head_sha=HEAD
                ).status,
                expected,
            )

    def test_unlabelled_pr_is_enriched_and_admitted_from_live_evidence(self) -> None:
        pull = _github_pull_request(
            1, head_sha=HEAD, repository=REPOSITORY, author_association="OWNER"
        )
        pull["labels"] = []
        pull["body"] = "## Client test notes\nNothing for the Client to test: internal scheduling."
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(),
                [pull],
                [_comment(user={"id": 20, "login": "example"})],
                pull,
                _combined_status(),
                {"check_runs": [_check_run("completed", "success")]},
                _conversation_rule(),
                [],
                [{"filename": "api/a.py"}],
                deepcopy(pull),
            )
        )
        with patch("control_plane.merge_train_codeowners._read_patterns", return_value=()):
            snapshot = GitHubMergeTrainSnapshotReader(
                transport=transport, repository_policy=self.policy
            ).read_merge_train_snapshot(repository=REPOSITORY, base_branch="main")
        result = build_merge_train_dry_run_result(
            policy=build_test_merge_train_policy(repository=REPOSITORY),
            snapshot=snapshot,
            batch_landing=True,
            new_candidate=True,
        )
        self.assertEqual(result.queue_order, (1,))
        self.assertEqual(snapshot.pull_requests[0].changed_paths, ("api/a.py",))
        self.assertTrue(
            all(
                request.method == "GET" or request.path == "/graphql"
                for request in transport.requests
            )
        )

    def test_push_between_list_and_detail_invalidates_ready(self) -> None:
        pull = _github_pull_request(
            1, head_sha=HEAD, repository=REPOSITORY, author_association="OWNER"
        )
        pull["labels"] = []
        reader = GitHubMergeTrainSnapshotReader(
            transport=RecordingMergeTrainGitHubTransport(
                responses=([_comment(user={"id": 20, "login": "example"})],)
            ),
            repository_policy=self.policy,
        )
        self.assertTrue(reader._needs_enrichment(pull))
        newer = _github_pull_request(
            1, head_sha="b" * 40, repository=REPOSITORY, author_association="OWNER"
        )
        newer["labels"] = []
        reader.transport = RecordingMergeTrainGitHubTransport(
            responses=(
                newer,
                _combined_status(),
                {"check_runs": [_check_run("completed", "success")]},
            )
        )
        current = reader._pull_request_snapshot(repository_path=REPOSITORY, pull_request=pull)
        result = build_merge_train_dry_run_result(
            policy=build_test_merge_train_policy(repository=REPOSITORY),
            snapshot=_snapshot(current),
            batch_landing=True,
            new_candidate=True,
        )
        self.assertFalse(result.queue_order)
        self.assertTrue(result.queue[0].ineligible_reasons)

    def test_head_parser_rejects_short_quoted_and_duplicate_heads(self) -> None:
        for body in (
            "Head: abc",
            f"> Head: {HEAD}",
            f"Head: {HEAD}\nHead: {HEAD}",
            f"```\nHead: {HEAD}\n```",
            f"<!-- Head: {HEAD} -->",
        ):
            self.assertFalse(handoff_head(body))
        self.assertEqual(handoff_head(f"Head: `{HEAD}`"), HEAD)

    def test_recorded_refresh_carries_only_the_verified_unchanged_source_patch(self) -> None:
        refreshed, base = "b" * 40, "c" * 40
        record = build_merge_train_branch_refresh_record(
            repository=REPOSITORY,
            base_branch="main",
            pull_request_number=1,
            expected_head_sha=HEAD,
            result_head_sha=refreshed,
            merged_base_sha=base,
            requested_at=datetime.now(timezone.utc),
        )
        for recorded, unchanged, expected in (
            (True, True, "ready"),
            (True, False, "stale"),
            (False, True, "stale"),
        ):
            transport = RecordingMergeTrainGitHubTransport(
                responses=(
                    [_comment(user={"id": 20, "login": "example"})],
                    {"sha": refreshed, "parents": [{"sha": HEAD}, {"sha": base}]},
                    {"status": "identical"},
                    _change(),
                    _change(patch_text="-different\n+patch") if not unchanged else _change(),
                )
            )
            store = SimpleNamespace(
                list_merge_train_branch_refresh_records=lambda **_: (record,) if recorded else ()
            )
            reader = GitHubMergeTrainSnapshotReader(
                transport=transport, repository_policy=self.policy, branch_refresh_store=store
            )
            handoff = reader._read_ready_handoff(
                repository_path=REPOSITORY,
                number=1,
                head_sha=refreshed,
                base_branch="main",
                base_sha=base,
            )
            self.assertEqual(handoff.status, expected)
            if expected == "ready":
                self.assertEqual(handoff.head_sha, refreshed)

    def test_collapsed_root_handoff_requires_current_recorded_plan_and_exact_parents(self) -> None:
        policy_record = build_test_merge_train_policy_record(repository=REPOSITORY)
        root = _ready().model_copy(update={"head_ref": "root"})
        child = _ready(2).model_copy(
            update={
                "head_ref": "child",
                "base_ref": "root",
                "head_sha": "b" * 40,
                "ready_handoff": MergeTrainReadyHandoff(
                    status="ready", head_sha="b" * 40, actor_id=20, actor_role="repo_admin"
                ),
            }
        )
        discovery = discover_merge_train_stack(
            policy=policy_record.policy,
            snapshot=_snapshot(root, child),
            root_pull_request_number=root.number,
        )
        plan = build_merge_train_stack_collapse_plan(
            discovery_result=discovery,
            policy_key=self.policy.policy_key,
            policy_sha256=policy_record.policy_sha256,
            created_at="2026-10-16T09:00:00Z",
        )
        collapsed = execute_merge_train_stack_collapse_plan(
            plan=plan,
            branch_client=_RecordingStackCollapseBranchClient(merge_commit_shas=("d" * 40,)),
            child_readiness_reasons=lambda _: (),
            updated_at="2026-10-16T09:30:00Z",
            checkpoint=lambda progress: None,
        )
        for changed, expected in (
            ("none", "ready"),
            ("parents", "stale"),
            ("policy", "stale"),
            ("blocked", "stale"),
            ("superseded", "stale"),
        ):
            with self.subTest(changed=changed), TemporaryDirectory() as directory:
                store = FilesystemRecordStore(Path(directory))
                store.write_merge_train_policy_record(policy_record)
                saved = collapsed.model_copy(
                    update={"policy_sha256": "wrong"}
                    if changed == "policy"
                    else {"status": "blocked"}
                    if changed == "blocked"
                    else {}
                )
                stored = build_merge_train_stack_collapse_plan_record(
                    plan=saved, source="test", updated_at=saved.updated_at
                )
                store.write_merge_train_stack_collapse_plan_record(
                    stored.model_copy(update={"status": "superseded"})
                    if changed == "superseded"
                    else stored
                )
                transport = RecordingMergeTrainGitHubTransport(
                    responses=(
                        [_comment(user={"id": 20, "login": "example"})],
                        {
                            "sha": "d" * 40,
                            "tree": {"sha": collapsed.mutations[0].expected_merge_tree_sha},
                            "parents": [
                                {"sha": "e" * 40 if changed == "parents" else HEAD},
                                {"sha": child.head_sha},
                            ],
                        },
                    )
                )
                reader = GitHubMergeTrainSnapshotReader(
                    transport=transport, repository_policy=self.policy, branch_refresh_store=store
                )
                self.assertEqual(
                    reader._read_ready_handoff(
                        repository_path=REPOSITORY,
                        number=1,
                        head_sha="d" * 40,
                        base_branch="main",
                        base_sha="c" * 40,
                    ).status,
                    expected,
                )

    def test_three_deep_handoff_only_stack_continues_after_intermediate_merge(self) -> None:
        policy, plan, members = _stack_fixture(3)
        branch_client = _RecordingStackCollapseBranchClient(merge_commit_shas=("d" * 40, "e" * 40))
        checked: list[int] = []
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            store.write_merge_train_policy_record(policy)

            def checkpoint(progress: MergeTrainStackCollapsePlan) -> None:
                store.write_merge_train_stack_collapse_plan_record(
                    build_merge_train_stack_collapse_plan_record(
                        plan=progress, source="test", updated_at=progress.updated_at
                    )
                )

            def readiness(number: int) -> tuple[str, ...]:
                checked.append(number)
                member = members[number - 1]
                current = "d" * 40 if number == 2 else member.head_sha
                responses: tuple[object, ...] = (
                    [
                        _comment(
                            f"Ready for the merge train\nHead: {member.head_sha}",
                            user={"id": 20, "login": "example"},
                        )
                    ],
                )
                if current != member.head_sha:
                    responses += (
                        {
                            "sha": current,
                            "tree": {"sha": f"tree:{member.head_sha}:{members[2].head_sha}"},
                            "parents": [{"sha": member.head_sha}, {"sha": members[2].head_sha}],
                        },
                    )
                reader = GitHubMergeTrainSnapshotReader(
                    transport=RecordingMergeTrainGitHubTransport(responses=responses),
                    repository_policy=self.policy,
                    branch_refresh_store=store,
                )
                handoff = reader._read_ready_handoff(
                    repository_path=REPOSITORY,
                    number=number,
                    head_sha=current,
                    base_branch=member.base_ref,
                    base_sha="f" * 40,
                )
                return merge_train_stack_child_readiness_reasons(
                    repository_policy=self.policy,
                    pull_request=member.model_copy(
                        update={"head_sha": current, "ready_handoff": handoff}
                    ),
                )

            result = execute_merge_train_stack_collapse_plan(
                plan=plan,
                branch_client=branch_client,
                child_readiness_reasons=readiness,
                updated_at="2026-10-16T09:30:00Z",
                checkpoint=checkpoint,
            )
        self.assertEqual(result.status, "waiting_for_root_checks")
        self.assertEqual(set(checked), {2, 3})
        self.assertTrue(all(mutation.status == "mutated" for mutation in result.mutations))

    def test_uncheckpointed_provider_commit_cannot_carry_a_handoff(self) -> None:
        policy, plan, members = _stack_fixture()
        merge_sha = "e" * 40
        merge = {
            "commit": {
                "message": _stack_collapse_commit_message(
                    collapse_id=plan.collapse_id,
                    child_pull_request_number=2,
                    parent_pull_request_number=1,
                )
            },
            "parents": [{"sha": HEAD}, {"sha": members[1].head_sha}],
        }
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            store.write_merge_train_policy_record(policy)
            progress = plan.model_copy(update={"status": "collapsing"})
            store.write_merge_train_stack_collapse_plan_record(
                build_merge_train_stack_collapse_plan_record(
                    plan=progress,
                    source="test",
                    updated_at=progress.updated_at,
                )
            )
            # Even a provider commit matching the plan's message and parents
            # cannot be promoted into mutated progress by an admission read.
            reader = GitHubMergeTrainSnapshotReader(
                transport=RecordingMergeTrainGitHubTransport(
                    responses=(
                        [_comment(user={"id": 20, "login": "example"})],
                        {"commit": {"sha": merge_sha}},
                        merge,
                        {"sha": merge_sha, "parents": merge["parents"]},
                    )
                ),
                repository_policy=self.policy,
                branch_refresh_store=store,
            )
            handoff = reader._read_ready_handoff(
                repository_path=REPOSITORY,
                number=1,
                head_sha=merge_sha,
                base_branch="main",
                base_sha="f" * 40,
            )
            self.assertEqual(handoff.status, "stale")
            result = build_merge_train_dry_run_result(
                policy=policy.policy,
                snapshot=_snapshot(
                    members[0].model_copy(update={"head_sha": merge_sha, "ready_handoff": handoff})
                ),
                batch_landing=True,
            )
            self.assertFalse(result.queue_order)
            self.assertTrue(result.queue[0].ineligible_reasons)

    def test_standalone_four_deep_execute_persists_progress_before_next_readiness(self) -> None:
        policy, plan, members = _stack_fixture(4)
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            store.write_merge_train_policy_record(policy)
            saved = build_merge_train_stack_collapse_plan_record(
                plan=plan,
                source="test",
                updated_at=plan.updated_at,
            )
            store.write_merge_train_stack_collapse_plan_record(saved)
            checked: list[int] = []
            heads = {member.number: member.head_sha for member in members}
            edges: dict[str, tuple[str, str]] = {}

            class Client(_RecordingStackCollapseBranchClient):
                def begin_snapshot_pass(self, **_: object) -> None:
                    pass

                def merge_stack_child_into_parent(self, **kwargs: Any) -> str:
                    result = super().merge_stack_child_into_parent(**kwargs)
                    heads[kwargs["parent_pull_request_number"]] = result
                    edges[result] = (kwargs["expected_parent_head_sha"], kwargs["child_head_sha"])
                    return result

                def read_pull_request_snapshot(
                    self, *, repository: str, pull_request_number: int
                ) -> MergeTrainPullRequestSnapshot:
                    checked.append(pull_request_number)
                    member = members[pull_request_number - 1]
                    current = heads[pull_request_number]
                    responses: tuple[object, ...] = (
                        [
                            _comment(
                                f"Ready for the merge train\nHead: {member.head_sha}",
                                user={"id": 20, "login": "example"},
                            )
                        ],
                    )
                    if current != member.head_sha:
                        responses += (
                            {
                                "sha": current,
                                "tree": {"sha": f"tree:{edges[current][0]}:{edges[current][1]}"},
                                "parents": [{"sha": value} for value in edges[current]],
                            },
                        )
                    reader = GitHubMergeTrainSnapshotReader(
                        transport=RecordingMergeTrainGitHubTransport(responses=responses),
                        repository_policy=policy.policy.find_repository_policy(
                            repository=repository, base_branch="main"
                        ),
                        branch_refresh_store=store,
                    )
                    handoff = reader._read_ready_handoff(
                        repository_path=repository,
                        number=pull_request_number,
                        head_sha=current,
                        base_branch=member.base_ref,
                        base_sha="f" * 40,
                    )
                    return member.model_copy(update={"head_sha": current, "ready_handoff": handoff})

            client = Client(merge_commit_shas=("e" * 40, "f" * 40, "1" * 40))
            with patch(
                "control_plane.merge_train_stack_collapse.GitHubMergeTrainClient",
                return_value=client,
            ):
                result = execute_merge_train_stack_collapse_run_once(
                    request=MergeTrainStackCollapseRunOnceEnvelope(
                        repository=REPOSITORY, stack_collapse_plan_record_id=saved.record_id
                    ),
                    policy=policy.policy,
                    policy_sha256=policy.policy_sha256,
                    token="fixture",
                    trace_id="test",
                    recorded_at="2026-10-16T10:00:00Z",
                    stack_collapse_store=store,
                )
            self.assertEqual(set(checked), {2, 3, 4})
            completed = cast(dict[str, object], result.accepted_result["stack_collapse_plan"])
            self.assertEqual(completed["status"], "waiting_for_root_checks")

    def test_standalone_admit_uses_live_reader_and_stored_collapse_proof(self) -> None:
        policy, plan, members = _stack_fixture()
        collapsed = execute_merge_train_stack_collapse_plan(
            plan=plan,
            branch_client=_RecordingStackCollapseBranchClient(merge_commit_shas=("d" * 40,)),
            child_readiness_reasons=lambda _: (),
            updated_at="2026-10-16T09:30:00Z",
            checkpoint=lambda progress: None,
        )
        pull = _github_pull_request(
            1, head_sha="d" * 40, repository=REPOSITORY, author_association="OWNER"
        )
        pull["labels"] = []
        pull["body"] = "## Client test notes\nNothing for the Client to test: internal scheduling."
        cast(dict[str, object], pull["head"])["ref"] = members[0].head_ref
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(),
                [pull],
                [_comment(user={"id": 20, "login": "example"})],
                {
                    "sha": "d" * 40,
                    "tree": {"sha": collapsed.mutations[0].expected_merge_tree_sha},
                    "parents": [{"sha": HEAD}, {"sha": members[1].head_sha}],
                },
                pull,
                _combined_status(),
                {"check_runs": [_check_run("completed", "success")]},
                _conversation_rule(),
                [],
                [{"filename": "api/a.py"}],
                deepcopy(pull),
            )
        )
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            store.write_merge_train_policy_record(policy)
            saved = build_merge_train_stack_collapse_plan_record(
                plan=collapsed, source="test", updated_at=collapsed.updated_at
            )
            store.write_merge_train_stack_collapse_plan_record(saved)
            with (
                patch(
                    "control_plane.merge_train_stack_collapse.UrllibMergeTrainGitHubTransport",
                    return_value=transport,
                ),
                patch("control_plane.merge_train_codeowners._read_patterns", return_value=()),
            ):
                result = execute_merge_train_stack_collapse_run_once(
                    request=MergeTrainStackCollapseRunOnceEnvelope(
                        repository=REPOSITORY,
                        mode="admit",
                        stack_collapse_plan_record_id=saved.record_id,
                    ),
                    policy=policy.policy,
                    policy_sha256=policy.policy_sha256,
                    token="fixture",
                    trace_id="test",
                    recorded_at="2026-10-16T10:00:00Z",
                    stack_collapse_store=store,
                    batch_candidate_store=store,
                )
            self.assertEqual(
                cast(dict[str, object], result.accepted_result["dry_run_result"])["queue_order"],
                [1],
            )
            candidates = store.list_merge_train_batch_candidate_records(
                repository=REPOSITORY, base_branch="main"
            )
            self.assertEqual(candidates[0].candidate.entries[0].head_sha, "d" * 40)

    def test_managed_comment_graphql_quota_preserves_backoff(self) -> None:
        edited = _comment(
            "Managed status",
            id=2,
            node_id="comment-node",
            updated_at="2026-10-16T11:00:00Z",
            user={"id": 20, "login": "example"},
        )
        reader = GitHubMergeTrainSnapshotReader(
            transport=RecordingMergeTrainGitHubTransport(
                responses=(
                    [_comment(user={"id": 20, "login": "example"}), edited],
                    {"errors": [{"type": "RATE_LIMITED", "message": "quota"}]},
                )
            ),
            repository_policy=self.policy,
        )
        with self.assertRaises(MergeTrainGitHubError) as raised:
            reader._read_ready_handoff(repository_path=REPOSITORY, number=1, head_sha=HEAD)
        self.assertTrue(raised.exception.rate_limited)

    def test_refresh_store_failure_is_only_unreadable_handoff_evidence(self) -> None:
        def unavailable(**_: object) -> tuple[()]:
            raise OSError("unavailable store")

        reader = GitHubMergeTrainSnapshotReader(
            transport=RecordingMergeTrainGitHubTransport(
                responses=([_comment(user={"id": 20, "login": "example"})],)
            ),
            repository_policy=self.policy,
            branch_refresh_store=SimpleNamespace(
                list_merge_train_branch_refresh_records=unavailable
            ),
        )
        self.assertEqual(
            reader._read_ready_handoff(
                repository_path=REPOSITORY,
                number=1,
                head_sha="b" * 40,
                base_branch="main",
                base_sha="c" * 40,
            ).status,
            "unreadable",
        )

    def test_client_single_pr_reader_keeps_policy_for_handoff_stack_children(self) -> None:
        pull = _github_pull_request(
            2, repository=REPOSITORY, head_sha=HEAD, author_association="OWNER"
        )
        pull["labels"] = []
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                pull,
                [_comment(user={"id": 20, "login": "example"})],
                _combined_status(),
                {"check_runs": [_check_run("completed", "success")]},
                _conversation_rule(),
                [],
            )
        )
        client = GitHubMergeTrainClient(transport=transport)
        client.begin_snapshot_pass(policy=self.policy, read_only=False)
        child = client.read_pull_request_snapshot(repository=REPOSITORY, pull_request_number=2)
        self.assertIsNotNone(child.ready_handoff)
        result = build_merge_train_dry_run_result(
            policy=build_test_merge_train_policy(repository=REPOSITORY),
            snapshot=_snapshot(child),
            batch_landing=True,
        )
        self.assertEqual(result.queue_order, (2,))

    def test_ignored_enqueue_label_does_not_hide_a_valid_independent_handoff(self) -> None:
        pull = _github_pull_request(
            1, repository=REPOSITORY, head_sha=HEAD, author_association="OWNER"
        )
        pull["labels"] = [{"name": self.policy.enqueue_label}]
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                pull,
                [
                    {
                        "event": "labeled",
                        "label": {"name": self.policy.enqueue_label},
                        "actor": {"id": 99, "login": "outsider", "type": "User"},
                    }
                ],
                {"permission": "write"},
                [_comment(user={"id": 20, "login": "example"})],
                _combined_status(),
                {"check_runs": [_check_run("completed", "success")]},
                _conversation_rule(),
                [],
            )
        )
        client = GitHubMergeTrainClient(transport=transport)
        client.begin_snapshot_pass(policy=self.policy, read_only=False)
        pr = client.read_pull_request_snapshot(repository=REPOSITORY, pull_request_number=1)
        result = build_merge_train_dry_run_result(
            policy=build_test_merge_train_policy(repository=REPOSITORY),
            snapshot=_snapshot(pr),
            batch_landing=True,
        )
        self.assertEqual(result.queue_order, (1,))

    def test_optional_area_read_failure_does_not_hold_a_repo_without_ownership_rules(self) -> None:
        failure: object
        for failure in ([], MergeTrainGitHubError("File service unavailable", status_code=503)):
            with patch("control_plane.merge_train_codeowners._read_patterns", return_value=()):
                (pr,) = individual_landing_snapshots(
                    transport=RecordingMergeTrainGitHubTransport(responses=(failure,)),
                    repository_path=REPOSITORY,
                    base_sha="base",
                    pull_requests=(_ready(),),
                )
            self.assertFalse(pr.requires_individual_landing)
            self.assertFalse(pr.changed_paths)

    def test_handoff_and_optional_area_reads_preserve_provider_quota_backoff(self) -> None:
        quota = MergeTrainGitHubError("quota", status_code=429, rate_limited=True)
        reader = GitHubMergeTrainSnapshotReader(
            transport=RecordingMergeTrainGitHubTransport(responses=(quota,)),
            repository_policy=self.policy,
        )
        with self.assertRaises(MergeTrainGitHubError) as raised:
            reader._read_ready_handoff(repository_path=REPOSITORY, number=1, head_sha=HEAD)
        self.assertIs(raised.exception, quota)
        with patch("control_plane.merge_train_codeowners._read_patterns", return_value=()):
            with self.assertRaises(MergeTrainGitHubError) as raised:
                individual_landing_snapshots(
                    transport=RecordingMergeTrainGitHubTransport(responses=(quota,)),
                    repository_path=REPOSITORY,
                    base_sha="base",
                    pull_requests=(_ready(),),
                )
        self.assertIs(raised.exception, quota)


class HandoffControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_label_free_candidate_lands_and_preserves_release_hold_and_membership(
        self,
    ) -> None:
        settled = False
        rolling_unknown = False

        class Reader(_FakeExpandedMergeTrainSnapshotReader):
            def read_merge_train_snapshot(
                self, *, repository: str, base_branch: str
            ) -> MergeTrainDryRunSnapshot:
                snapshot = super().read_merge_train_snapshot(
                    repository=repository, base_branch=base_branch
                )
                return snapshot.model_copy(
                    update={
                        "pull_requests": tuple(
                            pr.model_copy(
                                update={
                                    "labels": (),
                                    "label_actors": (),
                                    "mergeable": "unknown"
                                    if pr.number == 2 and rolling_unknown
                                    else "mergeable",
                                    "required_checks_status": "pending"
                                    if pr.number == 1 and not settled
                                    else "pass",
                                    "ready_handoff": MergeTrainReadyHandoff(
                                        status="ready",
                                        head_sha=pr.head_sha,
                                        actor_id=20,
                                        actor_role="repo_admin",
                                    ),
                                }
                            )
                            for pr in snapshot.pull_requests
                        )
                    }
                )

        async with _controller(_DependencyState(mergeable="mergeable"), Reader) as (
            app,
            store,
            request,
        ):
            planned = await _post(app, request, "plan_candidate")
            self.assertEqual(_entries(planned), [2])
            with patch(
                "control_plane.http_app.merge_train_release_hold_reader",
                return_value=lambda repository: MergeTrainReleaseHold(product="site"),
            ):
                held = await _post(app, request, "release_hold")
                self.assertEqual(held["blocking_reason"]["code"], "product_release_in_progress")
                self.assertFalse(store.list_merge_train_batch_landing_plan_records())
            settled = True
            for action in ("build_candidate", "observe_candidate", "plan_landing", "land_batch"):
                await _post(app, request, action)
                if action == "build_candidate":
                    rolling_unknown = True
            self.assertEqual(len(_batch_ids(store)), 1)
