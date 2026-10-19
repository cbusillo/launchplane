"""Native runner routes keep browser CSRF and own-actor authority."""

from dataclasses import replace
import unittest

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import BearerIdentityConfig, LaunchplaneAuthzPolicy
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from tests.http_app_test_support import (
    _RejectingVerifier,
    _browser_mutation_headers,
    _github_human_identity,
    _github_oauth_config,
)
from tests.support.auth import StubVerifier, identity as workflow_identity
from tests.support.http import request
from tests import test_runner_relabel_native as scenarios


class HttpRunnerRelabelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        fixture = scenarios.RunnerRelabelNativeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.store = fixture.store
        self.policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_humans": [
                    {
                        "github_ids": [123],
                        "roles": ["admin"],
                        "actions": ["authz_policy_grant.write"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                    }
                ]
            }
        )
        self.sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        self.human = _github_human_identity()
        self.session = self.sessions.issue(self.human)
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=self.policy,
            record_store_factory=lambda: self.store,
            human_session_manager=self.sessions,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="fixture-agent",
                local_operator_subject="fixture-agent",
                local_operator_token_label="fixture",
            ),
        )
        self.path = "/v1/runner-relabel/fixture/reviews"
        self.body = {
            "desired_custom_labels": ["candidate", "general"],
            "reason": "fixture reviewed routing",
        }

    def headers(self) -> dict[str, str]:
        return _browser_mutation_headers(self.sessions, self.session)

    async def test_browser_review_is_csrf_guarded_and_numeric_actor_bound(self) -> None:
        denied = await request(
            self.app,
            "POST",
            self.path,
            headers={"Cookie": self.sessions.session_cookie_header(self.session)},
            payload=self.body,
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"]["code"], "browser_mutation_denied")
        review = await request(
            self.app, "POST", self.path, headers=self.headers(), payload=self.body
        )
        self.assertEqual(review.status_code, 202, review.text)
        saved = review.json()["result"]
        self.assertEqual(
            saved["actor"], {"type": "github_human", "github_id": self.human.github_id}
        )
        self.assertNotIn(self.human.login, saved["actor"])
        self.assertEqual(self.fixture.transport.puts, 0)

    async def test_policy_review_requires_strict_admin_not_team_role(self) -> None:
        session = self.sessions.issue(replace(self.human, github_id=456))
        response = await request(
            self.app,
            "POST",
            "/v1/runner-relabel/fixture/policy",
            headers=_browser_mutation_headers(self.sessions, session),
            payload={
                "policy": self.fixture.policy.model_dump(mode="json"),
                "reason": "fixture",
                "mode": "apply",
            },
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_agent_can_plan_policy_but_cannot_activate_authority(self) -> None:
        body = {
            "policy": self.fixture.policy.model_dump(mode="json"),
            "reason": "inert agent proposal",
        }
        headers = {"Authorization": "Bearer fixture-agent"}
        review = await request(
            self.app, "POST", "/v1/runner-relabel/fixture/policy", headers=headers, payload=body
        )
        self.assertEqual(review.status_code, 202, review.text)
        applied = await request(
            self.app,
            "POST",
            "/v1/runner-relabel/fixture/policy",
            headers=headers,
            payload={
                **body,
                "mode": "apply",
                "expected_plan_sha256": review.json()["result"]["plan_sha256"],
            },
        )
        self.assertEqual(applied.status_code, 403, applied.text)
        self.assertEqual(self.fixture.transport.puts, 0)

    async def test_missing_capability_returns_precise_activation_code(self) -> None:
        from control_plane.runner_relabel import RunnerRelabelInactive

        self.fixture.factory.side_effect = RunnerRelabelInactive(
            "runner_purpose_capability_not_accepted"
        )
        response = await request(
            self.app, "POST", self.path, headers=self.headers(), payload=self.body
        )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "runner_purpose_capability_not_accepted")
        self.assertEqual(self.fixture.transport.puts, 0)

    async def test_callers_cannot_supply_policy_or_token_in_relabel_request(self) -> None:
        for field in ("policy", "token", "repository", "host_name"):
            with self.subTest(field=field):
                response = await request(
                    self.app,
                    "POST",
                    self.path,
                    headers=self.headers(),
                    payload={**self.body, field: "caller authority"},
                )
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.fixture.transport.puts, 0)

    async def test_foreign_actor_cannot_read_original_review(self) -> None:
        review = await request(
            self.app, "POST", self.path, headers=self.headers(), payload=self.body
        )
        self.assertEqual(review.status_code, 202, review.text)
        session = self.sessions.issue(replace(self.human, github_id=456))
        response = await request(
            self.app,
            "GET",
            self.path + "/" + review.json()["result"]["review_id"],
            headers={"Cookie": self.sessions.session_cookie_header(session)},
        )
        self.assertEqual(response.status_code, 409, response.text)

    async def test_workflow_and_read_only_sessions_cannot_apply(self) -> None:
        body = {"review_id": "a" * 32, "expected_review_sha256": "b" * 64}
        workflow_app = create_launchplane_fastapi_app(
            verifier=StubVerifier(workflow_identity()),
            authz_policy=self.policy,
            record_store_factory=lambda: self.store,
        )
        workflow = await request(
            workflow_app,
            "POST",
            "/v1/runner-relabel/fixture/operations",
            headers={"Authorization": "Bearer valid-token", "Idempotency-Key": "fixture"},
            payload=body,
        )
        self.assertEqual(workflow.status_code, 403, workflow.text)
        read_session = self.sessions.issue(_github_human_identity(role="read_only"))
        response = await request(
            self.app,
            "POST",
            "/v1/runner-relabel/fixture/operations",
            headers=_browser_mutation_headers(self.sessions, read_session),
            payload=body,
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.fixture.transport.puts, 0)
