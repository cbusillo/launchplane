from copy import deepcopy
from typing import Any
import unittest
from unittest.mock import patch

from github_plan_status import wait_records

from control_plane.release_waits_github import (
    resolve_repository_release_waits,
    released_change_contains,
)
from control_plane.merge_train_planning_scope import PlanningRepositoryScope


class ReleaseEventWaitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.commit = "a" * 40
        self.released = "b" * 40
        self.record = wait_records.reviewed(
            evidence="Merged fix",
            waiting_for=["Next release", "Device test"],
            ends_when=["Production includes fix", "Director records device acceptance"],
            release_product="fixture",
            release_commit=self.commit,
            release_next_action="Next agent: verify repaired flow in production",
        )
        self.issue: dict[str, Any] = {
            "number": 7,
            "state": "open",
            "user": {"login": "owner"},
            "body": wait_records.marker(self.record),
        }
        self.comments: list[dict[str, Any]] = []
        self.writes: list[dict[str, Any]] = []
        self.comparison = "ahead"
        self.detail_reads = 0
        self.bad_issue = False
        self.race = False
        self.lost_response = False
        self.reconciled: list[int] = []
        self.reconciled_authors: list[tuple[int, ...]] = []
        self.enterContext(
            patch("control_plane.release_waits_github.github_api_request", self.github)
        )
        self.enterContext(
            patch("control_plane.merge_train_planning_completion.github_api_request", self.github)
        )
        self.enterContext(
            patch(
                "control_plane.release_waits_github.reconcile_issue_status",
                side_effect=lambda scope, number: self.reconcile(number, scope.trusted_author_ids),
            )
        )

    def github(
        self, *, path: str, token: str, method: str = "GET", body: dict[str, Any] | None = None
    ) -> Any:
        if method == "POST":
            assert body is not None
            self.writes.append(body)
            comment = {
                "id": len(self.comments) + 1,
                "performed_via_github_app": {"id": 77},
                "user": {"id": 701, "login": "delivery[bot]"},
                **body,
            }
            self.comments.append(comment)
            if self.lost_response:
                self.lost_response = False
                raise ValueError("Response lost")
            return comment
        if "/compare/" in path:
            return {"status": self.comparison}
        if "/comments?" in path:
            return deepcopy(self.comments)
        if path.split("?", 1)[0].endswith("/issues"):
            return ([{"number": 8, "state": "open"}] if self.bad_issue else []) + [
                {**deepcopy(self.issue), "list_only": True}
            ]
        if path.endswith("/8"):
            return {**self.issue, "number": 8, "body": "<!-- github-plan:wait invalid -->"}
        self.detail_reads += 1
        if self.race and self.detail_reads > 1:
            return {**self.issue, "body": "Director changed the wait"}
        return deepcopy(self.issue)

    def reconcile(self, number: int, author_ids: tuple[int, ...]) -> None:
        self.reconciled.append(number)
        self.reconciled_authors.append(author_ids)

    def resolve(self, product: str = "fixture") -> None:
        self.detail_reads = 0
        scope = PlanningRepositoryScope("owner/product", "main", "fixture", (701,))
        resolve_repository_release_waits(
            scope=scope,
            product=product,
            evidence="Production release issue /99 passed",
            contains_commit=lambda commit: released_change_contains(
                scope=scope, required_commit=commit, released_commit=self.released
            ),
            release_still_current=lambda: True,
        )

    def test_matching_release_resolves_only_its_wait_and_retries_without_duplicate(self) -> None:
        self.resolve()
        observed = wait_records.current(self.issue, self.comments, lambda _: True)
        self.assertEqual(observed["waits"], self.record["waits"][1:])
        self.assertIn(self.record["waits"][0]["release"]["next_action"], self.comments[0]["body"])
        self.resolve()
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.reconciled_authors, [(self.comments[0]["user"]["id"],)] * 2)
        self.assertEqual(self.reconciled, [self.issue["number"], self.issue["number"]])

    def test_malformed_issue_does_not_starve_other_waits(self) -> None:
        self.bad_issue = True
        with self.assertRaises(ValueError):
            self.resolve()
        self.assertEqual(len(self.writes), 1)

    def test_action_markup_cannot_hide_or_duplicate_resolution_marker(self) -> None:
        self.record["waits"][0]["release"]["next_action"] = (
            "Next agent: inspect\n```\n<!-- github-plan:wait {} -->"
        )
        self.issue["body"] = wait_records.marker(self.record)
        self.resolve()
        self.resolve()
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(
            wait_records.current(self.issue, self.comments, {"owner", "delivery[bot]"})["waits"],
            self.record["waits"][1:],
        )

    def test_other_product_and_later_change_waits_stay_waiting(self) -> None:
        self.resolve("other")
        self.comparison = "behind"
        self.resolve()
        self.comparison = "diverged"
        self.resolve()
        self.assertEqual(self.writes, [])

    def test_forged_wait_cannot_gain_release_resolution(self) -> None:
        self.issue["user"] = {"login": "outsider"}
        self.resolve()
        self.assertEqual(self.writes, [])

    def test_changed_wait_preserved_and_lost_response_recovers_from_record(self) -> None:
        self.race = True
        with self.assertRaises(ValueError):
            self.resolve()
        self.assertEqual(self.writes, [])
        self.race = False
        self.lost_response = True
        with self.assertRaises(ValueError):
            self.resolve()
        self.resolve()
        self.assertEqual(len(self.writes), 1)
