from __future__ import annotations

import unittest

from pydantic import ValidationError

from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.product_environment_read_model import ProductSiteOverview
from control_plane.contracts.work_graph_read_model import (
    WorkGraphIssueSnapshot,
    WorkGraphPlanningIssueFacts,
    WorkGraphRepoSnapshot,
    WorkGraphSnapshot,
    build_work_graph_queue,
    build_work_graph_snapshot_from_records,
)


def _repo(
    repository: str = "cbusillo/launchplane",
    classification: str = "managed_runtime",
) -> WorkGraphRepoSnapshot:
    return WorkGraphRepoSnapshot.model_validate(
        {
            "repository": repository,
            "classification": classification,
            "product": "launchplane" if classification == "managed_runtime" else "",
            "display_name": "Launchplane",
        }
    )


def _issue(**overrides: object) -> WorkGraphIssueSnapshot:
    payload: dict[str, object] = {
        "repository": "cbusillo/launchplane",
        "number": 190,
        "title": "Build What To Work On Next cockpit",
        "url": "https://github.com/cbusillo/launchplane/issues/190",
        "focus": "Next",
        "manager": "Chris",
        "finish_line": "Ranked Launchplane work queue from Code Plans with links.",
        "labels": ("plan", "plan:active"),
        "updated_at": "2026-05-06T01:39:23Z",
    }
    payload.update(overrides)
    return WorkGraphIssueSnapshot.model_validate(payload)


def _product_overview(**overrides: object) -> ProductSiteOverview:
    payload: dict[str, object] = {
        "product": "launchplane",
        "display_name": "Launchplane",
        "repository": "cbusillo/launchplane",
        "driver_id": "launchplane-service",
        "preview": {"enabled": False},
        "trust_state": "recorded",
        "provenance": {"source_kind": "record", "freshness_status": "recorded"},
    }
    payload.update(overrides)
    return ProductSiteOverview.model_validate(payload)


def _work_request(**overrides: object) -> EveryCodeWorkRequestRecord:
    payload: dict[str, object] = {
        "request_id": "every-code-cbusillo-launchplane-190",
        "source": "github_issue_label",
        "state": "queued",
        "repository": "cbusillo/launchplane",
        "issue_number": 190,
        "issue_url": "https://github.com/cbusillo/launchplane/issues/190",
        "issue_title": "Build What To Work On Next cockpit",
        "trigger_label": "every-code",
        "trigger_actor": "cbusillo",
        "github_delivery_id": "delivery-190",
        "queued_at": "2026-05-06T02:00:00Z",
        "updated_at": "2026-05-06T02:00:00Z",
    }
    payload.update(overrides)
    return EveryCodeWorkRequestRecord.model_validate(payload)


class WorkGraphReadModelTests(unittest.TestCase):
    def test_build_queue_ranks_ready_plan_items_with_reasons(self) -> None:
        snapshot = WorkGraphSnapshot.model_validate(
            {
                "generated_at": "2026-05-06T01:45:00Z",
                "repos": (_repo().model_dump(mode="json"),),
                "issues": (
                    _issue(number=190, focus="Next", blocking=2).model_dump(mode="json"),
                    _issue(
                        number=260,
                        title="Blocked tenant work",
                        url="https://github.com/cbusillo/launchplane/issues/260",
                        focus="Waiting",
                        labels=("plan", "plan:blocked"),
                        blocked_by=1,
                    ).model_dump(mode="json"),
                ),
            }
        )

        queue = build_work_graph_queue(snapshot)

        self.assertEqual(queue.generated_at, "2026-05-06T01:45:00Z")
        self.assertEqual([item.number for item in queue.items], [190, 260])
        first = queue.items[0]
        self.assertEqual(first.state, "ready")
        self.assertEqual(first.recommendation, "quick_win")
        self.assertEqual(first.repo_classification, "managed_runtime")
        self.assertEqual(first.product, "launchplane")
        self.assertGreater(first.score, queue.items[1].score)
        self.assertTrue(first.safe_to_start)
        self.assertEqual(first.blocked_by_count, 0)
        self.assertEqual(first.source_of_truth_url, first.url)
        self.assertEqual(first.handoff_url, first.url)
        self.assertIn("Start a focused branch", first.next_action)
        self.assertIn("unblocks 2", first.why_now)
        self.assertIn("source_of_truth", {entry.code for entry in first.evidence})
        self.assertIn("repo_classification", {reason.code for reason in first.reasons})
        self.assertIn("finish_line", {reason.code for reason in first.reasons})

    def test_failed_signal_becomes_attention_needed(self) -> None:
        snapshot = WorkGraphSnapshot.model_validate(
            {
                "generated_at": "2026-05-06T01:45:00Z",
                "repos": (_repo().model_dump(mode="json"),),
                "issues": (
                    _issue(number=153, focus="Later", check_state="failure").model_dump(
                        mode="json"
                    ),
                    _issue(number=190, focus="Next").model_dump(mode="json"),
                ),
            }
        )

        queue = build_work_graph_queue(snapshot)

        self.assertEqual(queue.items[0].number, 153)
        self.assertEqual(queue.items[0].state, "ready")
        self.assertEqual(queue.items[0].recommendation, "attention_needed")
        self.assertTrue(queue.items[0].safe_to_start)
        self.assertIn("failing check", queue.items[0].next_action)
        self.assertIn("operator attention", queue.items[0].why_now)
        self.assertIn("checks", {entry.code for entry in queue.items[0].evidence})
        self.assertIn("failed_signal", {reason.code for reason in queue.items[0].reasons})

    def test_blocked_item_is_not_safe_to_start_and_names_dependency_count(self) -> None:
        snapshot = WorkGraphSnapshot.model_validate(
            {
                "generated_at": "2026-05-06T01:45:00Z",
                "repos": (_repo().model_dump(mode="json"),),
                "issues": (
                    _issue(
                        number=260,
                        title="Blocked tenant work",
                        url="https://github.com/cbusillo/launchplane/issues/260",
                        focus="Waiting",
                        labels=("plan", "plan:blocked"),
                        blocked_by=2,
                    ).model_dump(mode="json"),
                ),
            }
        )

        queue = build_work_graph_queue(snapshot)

        item = queue.items[0]
        self.assertFalse(item.safe_to_start)
        self.assertEqual(item.state, "blocked")
        self.assertEqual(item.blocked_by_count, 2)
        self.assertIn("blocking dependency", item.next_action)
        self.assertIn("dependency cleanup", item.why_now)
        self.assertIn("blocked_by", {entry.code for entry in item.evidence})

    def test_closed_and_done_items_are_hidden(self) -> None:
        snapshot = WorkGraphSnapshot.model_validate(
            {
                "generated_at": "2026-05-06T01:45:00Z",
                "repos": (_repo().model_dump(mode="json"),),
                "issues": (
                    _issue(number=153, state="closed", focus="Done").model_dump(mode="json"),
                    _issue(number=190, focus="Next").model_dump(mode="json"),
                ),
            }
        )

        queue = build_work_graph_queue(snapshot)

        self.assertEqual([item.number for item in queue.items], [190])
        self.assertEqual(queue.hidden_count, 1)

    def test_limit_counts_hidden_overflow(self) -> None:
        snapshot = WorkGraphSnapshot.model_validate(
            {
                "generated_at": "2026-05-06T01:45:00Z",
                "repos": (_repo().model_dump(mode="json"),),
                "issues": (
                    _issue(number=190).model_dump(mode="json"),
                    _issue(
                        number=191, title="Next item", url="https://github.com/x/y/1"
                    ).model_dump(mode="json"),
                ),
            }
        )

        queue = build_work_graph_queue(snapshot, limit=1)

        self.assertEqual(len(queue.items), 1)
        self.assertEqual(queue.hidden_count, 1)

    def test_snapshot_requires_repo_classification_for_each_issue(self) -> None:
        with self.assertRaisesRegex(ValidationError, "missing repo classifications"):
            WorkGraphSnapshot.model_validate(
                {
                    "generated_at": "2026-05-06T01:45:00Z",
                    "repos": (),
                    "issues": (_issue().model_dump(mode="json"),),
                }
            )

    def test_managed_runtime_repo_requires_product(self) -> None:
        with self.assertRaisesRegex(ValidationError, "requires product"):
            WorkGraphRepoSnapshot.model_validate(
                {"repository": "cbusillo/launchplane", "classification": "managed_runtime"}
            )

    def test_rejects_invalid_limit(self) -> None:
        snapshot = WorkGraphSnapshot.model_validate(
            {
                "generated_at": "2026-05-06T01:45:00Z",
                "repos": (_repo().model_dump(mode="json"),),
                "issues": (_issue().model_dump(mode="json"),),
            }
        )

        with self.assertRaisesRegex(ValueError, "limit must be positive"):
            build_work_graph_queue(snapshot, limit=0)

    def test_build_snapshot_from_launchplane_records_classifies_product_and_awareness_repos(
        self,
    ) -> None:
        snapshot = build_work_graph_snapshot_from_records(
            generated_at="2026-05-06T03:05:00Z",
            product_overviews=(_product_overview(),),
            work_requests=(
                _work_request(),
                _work_request(
                    request_id="every-code-cbusillo-code-12",
                    repository="cbusillo/code",
                    issue_number=12,
                    issue_url="https://github.com/cbusillo/code/issues/12",
                    issue_title="Improve local worker",
                    state="blocked",
                    claimed_at="2026-05-06T02:05:00Z",
                    claimed_by_host="mac-mini",
                    started_at="2026-05-06T02:06:00Z",
                    finished_at="2026-05-06T02:07:00Z",
                    updated_at="2026-05-06T02:07:00Z",
                    error_message="Checkout is missing.",
                ),
            ),
        )

        repos = {repo.repository: repo for repo in snapshot.repos}
        self.assertEqual(repos["cbusillo/launchplane"].classification, "managed_runtime")
        self.assertEqual(repos["cbusillo/launchplane"].product, "launchplane")
        self.assertEqual(repos["cbusillo/code"].classification, "active_awareness")
        issues = {issue.repository: issue for issue in snapshot.issues}
        self.assertEqual(issues["cbusillo/launchplane"].focus, "Next")
        self.assertEqual(issues["cbusillo/code"].focus, "Waiting")
        self.assertEqual(issues["cbusillo/code"].blocked_by, 1)

    def test_build_snapshot_applies_compact_planning_facts_without_copying_bodies(
        self,
    ) -> None:
        snapshot = build_work_graph_snapshot_from_records(
            generated_at="2026-05-06T03:55:00Z",
            product_overviews=(_product_overview(),),
            work_requests=(_work_request(trigger_label="every-code"),),
            planning_issue_facts=(
                WorkGraphPlanningIssueFacts.model_validate(
                    {
                        "repository": "cbusillo/launchplane",
                        "number": 190,
                        "focus": "Now",
                        "manager": "Chris",
                        "finish_line": "Ranked queue is driven by Code Plans fields.",
                        "labels": ("plan", "plan:active"),
                        "blocking": 2,
                        "updated_at": "2026-05-06T03:54:00Z",
                        "check_state": "success",
                    }
                ),
            ),
        )

        issue = snapshot.issues[0]
        self.assertEqual(issue.focus, "Now")
        self.assertEqual(issue.manager, "Chris")
        self.assertEqual(issue.finish_line, "Ranked queue is driven by Code Plans fields.")
        self.assertEqual(issue.labels, ("every-code", "plan", "plan:active"))
        self.assertEqual(issue.blocking, 2)
        self.assertEqual(issue.updated_at, "2026-05-06T03:54:00Z")
        self.assertEqual(issue.check_state, "success")

    def test_build_snapshot_includes_project_only_planning_facts_for_known_repos(
        self,
    ) -> None:
        snapshot = build_work_graph_snapshot_from_records(
            generated_at="2026-05-06T14:20:00Z",
            product_overviews=(_product_overview(),),
            work_requests=(),
            planning_issue_facts=(
                WorkGraphPlanningIssueFacts.model_validate(
                    {
                        "repository": "cbusillo/launchplane",
                        "number": 307,
                        "title": "Refactor large work graph and operator UI source files",
                        "url": "https://github.com/cbusillo/launchplane/issues/307",
                        "focus": "Next",
                        "manager": "@cellmechanic",
                        "finish_line": "Work graph changes land in focused modules.",
                        "labels": ("plan", "plan:active"),
                        "blocking": 1,
                        "updated_at": "2026-05-06T13:28:25Z",
                    }
                ),
                WorkGraphPlanningIssueFacts.model_validate(
                    {
                        "repository": "cbusillo/unmanaged",
                        "number": 12,
                        "title": "Unmanaged project issue",
                        "url": "https://github.com/cbusillo/unmanaged/issues/12",
                    }
                ),
            ),
        )

        self.assertEqual(len(snapshot.issues), 1)
        issue = snapshot.issues[0]
        self.assertEqual(issue.repository, "cbusillo/launchplane")
        self.assertEqual(issue.number, 307)
        self.assertEqual(issue.focus, "Next")
        self.assertEqual(issue.manager, "@cellmechanic")
        self.assertEqual(issue.finish_line, "Work graph changes land in focused modules.")
        self.assertEqual(issue.labels, ("plan", "plan:active"))
        self.assertEqual(issue.blocking, 1)
        self.assertEqual(issue.updated_at, "2026-05-06T13:28:25Z")

    def test_empty_planning_facts_do_not_erase_every_code_facts(self) -> None:
        snapshot = build_work_graph_snapshot_from_records(
            generated_at="2026-05-06T03:55:00Z",
            product_overviews=(_product_overview(),),
            work_requests=(
                _work_request(
                    state="claimed",
                    claimed_at="2026-05-06T03:49:00Z",
                    claimed_by_host="local-mac",
                    updated_at="2026-05-06T03:50:00Z",
                ),
            ),
            planning_issue_facts=(
                WorkGraphPlanningIssueFacts.model_validate(
                    {"repository": "cbusillo/launchplane", "number": 190}
                ),
            ),
        )

        issue = snapshot.issues[0]
        self.assertEqual(issue.focus, "Now")
        self.assertEqual(issue.manager, "claimed_local_worker")
        self.assertEqual(issue.updated_at, "2026-05-06T03:50:00Z")

    def test_sparse_planning_subissue_completion_does_not_break_snapshot(self) -> None:
        snapshot = build_work_graph_snapshot_from_records(
            generated_at="2026-05-06T03:55:00Z",
            product_overviews=(_product_overview(),),
            work_requests=(_work_request(),),
            planning_issue_facts=(
                WorkGraphPlanningIssueFacts.model_validate(
                    {
                        "repository": "cbusillo/launchplane",
                        "number": 190,
                        "subissues_completed": 1,
                    }
                ),
            ),
        )

        issue = snapshot.issues[0]
        self.assertEqual(issue.subissues_total, 0)
        self.assertEqual(issue.subissues_completed, 0)


if __name__ == "__main__":
    unittest.main()
