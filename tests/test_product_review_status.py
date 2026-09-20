import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import click

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_review import (
    ProductReviewDecision,
    ProductReviewDecisionRecord,
)
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.product_review_status import OwnerReviewStatusPublisher
from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneAuthzPolicy
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.http_app_test_support import (
    _asgi_request,
    _browser_mutation_headers,
    _github_oauth_config,
    _post_preview_pr_feedback,
    _preview_generation_read_record,
    _preview_pr_feedback_identity,
    _preview_pr_feedback_payload,
    _preview_pr_feedback_policy,
    _preview_read_record,
    _RejectingVerifier,
)
from tests.support.auth import StubVerifier
from tests.support.profiles import product_profile_payload

_REPOSITORY = "every/example-site"
_PULL_REQUEST = 42
_HEAD_SHA = "abcdef1234567890abcdef1234567890abcdef12"
_OLDER_HEAD_SHA = "1111111111111111111111111111111111111111"
_OWNER_GITHUB_ID = 9001
_APP_ID = 77
_PUBLIC_ORIGIN = "https://launchplane.example.invalid"
_REVIEW_URL = f"{_PUBLIC_ORIGIN}/ui/owner-review?repository=every%2Fexample-site&pull_request=42"


class _GitHub:
    """A pull request's labels, commit statuses, and check runs as GitHub would serve them."""

    def __init__(
        self,
        *,
        repository: str = _REPOSITORY,
        labels: tuple[str, ...] = ("owner-review",),
        head_sha: str = _HEAD_SHA,
    ) -> None:
        self.repository = repository
        self.labels = labels
        self.head_sha = head_sha
        self.statuses: list[dict[str, object]] = []
        self.check_runs: list[dict[str, object]] = []
        self.writes: list[tuple[str, str, dict[str, object]]] = []
        self.fail_writes = False
        self.revoked_app_tokens = 0

    def __call__(self, **kwargs: object) -> object:
        path = str(kwargs["path"])
        method = str(kwargs.get("method") or "GET")
        body = kwargs.get("body")
        repository_path = f"/repos/{self.repository}"
        if path == "/installation/token" and method == "DELETE":
            self.revoked_app_tokens += 1
            return None
        if method != "GET":
            assert isinstance(body, dict)
            if self.fail_writes:
                raise click.ClickException("GitHub is unavailable.")
            self.writes.append((method, path, body))
        if path == f"{repository_path}/pulls/{_PULL_REQUEST}":
            return {
                "head": {"sha": self.head_sha},
                "labels": [{"name": label} for label in self.labels],
                "base": {"repo": {"id": 5150}},
            }
        if path == f"{repository_path}/statuses/{self.head_sha}" and method == "POST":
            assert isinstance(body, dict)
            self.statuses.insert(0, dict(body))
            return dict(body)
        if path == f"{repository_path}/commits/{self.head_sha}/statuses?per_page=100":
            return list(self.statuses)
        if path.startswith(f"{repository_path}/commits/{self.head_sha}/check-runs?"):
            return {"check_runs": list(self.check_runs)}
        if path.startswith(f"{repository_path}/check-runs/") and method == "PATCH":
            assert isinstance(body, dict)
            check_run = next(
                run for run in self.check_runs if run["id"] == int(path.rsplit("/", 1)[1])
            )
            check_run.update(body)
            return check_run
        raise AssertionError(f"Unexpected GitHub request: {method} {path}")

    def owner_review_statuses(self) -> list[dict[str, object]]:
        return [
            status for status in self.statuses if status["context"] == "launchplane/owner-review"
        ]


def _app_token(repository: str, repository_id: str) -> GitHubAppInstallationToken:
    return GitHubAppInstallationToken(
        token="app-token",
        app_id=_APP_ID,
        installation_id=1,
        repository_id=int(repository_id),
        repository=repository,
        expires_at="2026-09-20T13:00:00Z",
    )


def _publisher(github: _GitHub) -> OwnerReviewStatusPublisher:
    return OwnerReviewStatusPublisher(
        control_plane_root=Path("/nonexistent"),
        public_origin=_PUBLIC_ORIGIN,
        github_token=lambda **_: "feedback-token",
        api_request=github,
        github_app_token=_app_token,
    )


def _profile(*, owner_set: bool = True) -> LaunchplaneProductProfileRecord:
    payload = product_profile_payload("example-site")
    payload["repository"] = _REPOSITORY
    payload["preview"] = {"enabled": True, "context": "example-site"}
    if owner_set:
        payload["owner"] = {"github_login": "site-owner", "github_id": str(_OWNER_GITHUB_ID)}
    return LaunchplaneProductProfileRecord.model_validate(payload)


def _decision(
    *, decision: ProductReviewDecision, head_sha: str = _HEAD_SHA, decided_at: str
) -> ProductReviewDecisionRecord:
    return ProductReviewDecisionRecord(
        record_id=f"decision-{decided_at}",
        product="example-site",
        repository=_REPOSITORY,
        pull_request_number=_PULL_REQUEST,
        head_sha=head_sha,
        preview_url="https://pr-42.example.invalid",
        decision=decision,
        reason="The price is wrong." if decision == "changes_requested" else "",
        owner_github_id=str(_OWNER_GITHUB_ID),
        owner_github_login="site-owner",
        decided_at=decided_at,
    )


class OwnerReviewStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.store = FilesystemRecordStore(state_dir=Path(temporary_directory.name))

    def _publish(
        self, github: _GitHub, *, owner_set: bool = True, retire_leftovers: bool = False
    ) -> None:
        _publisher(github).publish(
            store=self.store,
            profile=_profile(owner_set=owner_set),
            pull_request_number=_PULL_REQUEST,
            retire_leftovers=retire_leftovers,
        )

    def test_marked_pull_request_waits_for_the_owner_with_the_review_link(self) -> None:
        github = _GitHub()

        self._publish(github)

        self.assertEqual(
            github.statuses,
            [
                {
                    "state": "pending",
                    "description": "Waiting for @site-owner to review the preview",
                    "context": "launchplane/owner-review",
                    "target_url": _REVIEW_URL,
                }
            ],
        )

    def test_decision_for_the_current_head_decides_the_status(self) -> None:
        cases: tuple[tuple[ProductReviewDecision, str, str], ...] = (
            ("accepted", "success", "Accepted by @site-owner"),
            ("changes_requested", "failure", "Changes requested by @site-owner"),
        )
        for decision, state, description in cases:
            with self.subTest(decision=decision):
                github = _GitHub()
                self.setUp()
                self.store.write_product_review_decision_record(
                    _decision(decision="accepted", decided_at="2026-09-20T10:00:00Z")
                )
                self.store.write_product_review_decision_record(
                    _decision(decision=decision, decided_at="2026-09-20T11:00:00Z")
                )

                self._publish(github)

                self.assertEqual(github.statuses[0]["state"], state)
                self.assertEqual(github.statuses[0]["description"], description)

    def test_decision_for_an_older_head_leaves_the_new_head_waiting(self) -> None:
        github = _GitHub()
        self.store.write_product_review_decision_record(
            _decision(
                decision="accepted", head_sha=_OLDER_HEAD_SHA, decided_at="2026-09-20T10:00:00Z"
            )
        )

        self._publish(github)

        self.assertEqual(github.statuses[0]["state"], "pending")
        self.assertEqual(
            github.statuses[0]["description"], "Waiting for @site-owner to review the preview"
        )

    def test_marked_pull_request_without_an_owner_says_so(self) -> None:
        github = _GitHub()

        self._publish(github, owner_set=False)

        self.assertEqual(github.statuses[0]["state"], "pending")
        self.assertEqual(github.statuses[0]["description"], "No Owner set for this product")

    def test_unmarked_pull_request_gets_no_status(self) -> None:
        github = _GitHub(labels=("launchplane-preview",))

        self._publish(github, retire_leftovers=True)

        self.assertEqual(github.writes, [])

    def test_github_failure_is_contained(self) -> None:
        github = _GitHub()
        github.fail_writes = True

        self._publish(github, retire_leftovers=True)

        self.assertEqual(github.statuses, [])

    def test_stale_leftover_signals_are_retired_once(self) -> None:
        github = _GitHub(labels=())
        github.statuses = [
            {
                "context": "manager-preview-approval",
                "state": "pending",
                "description": "Waiting for the manager to approve the exact current preview",
            }
        ]
        github.check_runs = [
            {
                "id": 31,
                "name": "launchplane/owner-acceptance",
                "app": {"id": _APP_ID},
                "status": "completed",
                "conclusion": "failure",
                "output": {"title": "Owner acceptance: unavailable", "summary": "unavailable"},
            }
        ]

        self._publish(github, retire_leftovers=True)
        writes_after_first = len(github.writes)
        self._publish(github, retire_leftovers=True)

        self.assertEqual(writes_after_first, 2)
        self.assertEqual(len(github.writes), 2)
        self.assertEqual(github.statuses[0]["context"], "manager-preview-approval")
        self.assertEqual(github.statuses[0]["state"], "success")
        self.assertEqual(
            github.statuses[0]["description"], "Retired. Owner review is recorded in Launchplane."
        )
        self.assertEqual(github.check_runs[0]["conclusion"], "neutral")
        output = github.check_runs[0]["output"]
        assert isinstance(output, dict)
        self.assertEqual(output["title"], "Retired")
        self.assertIn("launchplane/owner-review", output["summary"])
        self.assertEqual(github.revoked_app_tokens, 2)

    def test_absent_or_foreign_leftovers_are_not_created_or_touched(self) -> None:
        github = _GitHub(labels=())
        github.check_runs = [
            {
                "id": 32,
                "name": "launchplane/owner-acceptance",
                "app": {"id": _APP_ID + 1},
                "status": "completed",
                "conclusion": "failure",
            }
        ]

        self._publish(github, retire_leftovers=True)

        self.assertEqual(github.writes, [])


class OwnerReviewStatusHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.store = FilesystemRecordStore(state_dir=self.root / "state")
        self.session_manager = HumanSessionManager(
            config=_github_oauth_config(),
            session_store=InMemoryHumanSessionStore(),
        )

    async def _record_changes_requested(self, github: _GitHub) -> int:
        self.store.write_product_profile_record(_profile())
        self.store.write_preview_record(_preview_read_record(anchor_pr_number=_PULL_REQUEST))
        self.store.write_preview_generation_record(
            _preview_generation_read_record(anchor_pr_number=_PULL_REQUEST)
        )
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
            record_store_factory=lambda: self.store,
            human_session_manager=self.session_manager,
            owner_review_status_publisher=_publisher(github),
        )
        session = self.session_manager.issue(
            GitHubHumanIdentity(
                login="site-owner",
                github_id=_OWNER_GITHUB_ID,
                name="site-owner",
                email="site-owner@example.com",
                organizations=frozenset(),
                teams=frozenset(),
                role="read_only",
            )
        )
        response = await _asgi_request(
            app,
            "POST",
            "/v1/product-review/decisions",
            headers=_browser_mutation_headers(self.session_manager, session),
            payload={
                "repository": _REPOSITORY,
                "pull_request": _PULL_REQUEST,
                "decision": "changes_requested",
                "reason": "The price is wrong.",
            },
        )
        return response.status_code

    async def test_recording_a_decision_updates_the_pull_request_status(self) -> None:
        github = _GitHub()

        status_code = await self._record_changes_requested(github)

        self.assertEqual(status_code, 200)
        self.assertEqual(github.statuses[0]["state"], "failure")
        self.assertEqual(github.statuses[0]["description"], "Changes requested by @site-owner")

    async def test_decision_is_kept_when_github_rejects_the_status(self) -> None:
        github = _GitHub()
        github.fail_writes = True

        status_code = await self._record_changes_requested(github)

        self.assertEqual(status_code, 200)
        decisions = self.store.list_product_review_decision_records(
            repository=_REPOSITORY, pull_request_number=_PULL_REQUEST
        )
        self.assertEqual([record.decision for record in decisions], ["changes_requested"])

    async def test_ready_preview_feedback_writes_the_status_and_retires_leftovers(self) -> None:
        github = _GitHub(repository="every/verireel")
        github.statuses = [
            {"context": "manager-preview-approval", "state": "pending", "description": "Waiting"}
        ]
        profile_payload = product_profile_payload("verireel")
        profile_payload["repository"] = "every/verireel"
        profile_payload["owner"] = {
            "github_login": "site-owner",
            "github_id": str(_OWNER_GITHUB_ID),
        }
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile_payload)
        )
        app = create_launchplane_fastapi_app(
            verifier=StubVerifier(_preview_pr_feedback_identity()),
            authz_policy=_preview_pr_feedback_policy(action="preview_pr_feedback.write"),
            control_plane_root_path=self.root,
            record_store_factory=lambda: self.store,
            owner_review_status_publisher=_publisher(github),
        )

        ready = await _post_preview_pr_feedback(app, _preview_pr_feedback_payload())
        github.fail_writes = True
        ready_while_github_is_down = await _post_preview_pr_feedback(
            app, _preview_pr_feedback_payload()
        )

        self.assertEqual(ready.status_code, 202)
        self.assertEqual(ready_while_github_is_down.status_code, 202)
        self.assertEqual(
            [(status["context"], status["state"]) for status in github.statuses[:2]],
            [("manager-preview-approval", "success"), ("launchplane/owner-review", "pending")],
        )
        self.assertEqual(
            github.owner_review_statuses()[0]["description"],
            "Waiting for @site-owner to review the preview",
        )


if __name__ == "__main__":
    unittest.main()
