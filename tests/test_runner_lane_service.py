from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from typing import cast
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
import jwt

from control_plane import runner_lane_service
from control_plane.runner_lane_github import GitHubRunnerLaneInventoryReader
from control_plane.contracts.github_app_installation_token import GitHubAppInstallationToken
from control_plane.github_app_identity import (
    GitHubAppIdentity,
    GitHubAppIdentityError,
    mint_runner_lifecycle_installation_token,
)
from control_plane.http_app import LaunchplaneErrorResponse, _launchplane_http_error
from control_plane.http_routes import runner_lanes
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.runner_lane_service import (
    RUNNER_LANE_SERVICE_ROUTE,
    RunnerLaneServiceRequest,
    RunnerLaneServiceResponse,
    RunnerLaneStore,
    execute_runner_lane_service_request,
    runner_lifecycle_token,
)
from control_plane.service_auth import GitHubHumanIdentity
from tests.test_runner_host_hygiene_configuration import workflow_identity
from tests.support.http import request as http_request


def installation_token() -> GitHubAppInstallationToken:
    return GitHubAppInstallationToken(
        token="synthetic-installation-token",
        app_id=42,
        installation_id=77,
        repository_id=123,
        repository="example/repo",
        expires_at="2026-08-07T15:00:00Z",
    )


class RunnerLifecycleTokenTests(unittest.TestCase):
    def test_mint_narrows_all_repository_installation_and_revokes_invalid_tokens(self) -> None:
        permissions = {"administration": "write", "actions": "read", "metadata": "read"}
        for defect in (
            "none",
            "repository",
            "extra_repository",
            "permission",
            "expiry",
            "client",
            "owner",
            "suspended",
        ):
            with self.subTest(defect=defect):
                calls: list[dict[str, object]] = []

                def api_request(**kwargs: object) -> object:
                    calls.append(kwargs)
                    path = kwargs["path"]
                    if path == "/app":
                        return {
                            "id": 42,
                            "client_id": "wrong" if defect == "client" else "Iv1.runner",
                        }
                    if path == "/repos/example/repo/installation":
                        return {
                            "id": 77,
                            "app_id": 42,
                            "repository_selection": "all",
                            "account": {
                                "id": 8,
                                "login": "other" if defect == "owner" else "example",
                            },
                            "permissions": permissions,
                            "suspended_at": "2026-08-07T00:00:00Z"
                            if defect == "suspended"
                            else None,
                        }
                    if path == "/installation/token":
                        return None
                    self.assertEqual(
                        kwargs["body"], {"repository_ids": [123], "permissions": permissions}
                    )
                    repositories = [
                        {"id": 124 if defect == "repository" else 123, "full_name": "example/repo"}
                    ]
                    if defect == "extra_repository":
                        repositories.append({"id": 456, "full_name": "example/other"})
                    return {
                        "token": "synthetic-installation-token",
                        "expires_at": "2026-08-07T13:00:00Z"
                        if defect == "expiry"
                        else "2026-08-07T15:00:00Z",
                        "permissions": {
                            **permissions,
                            **({"contents": "write"} if defect == "permission" else {}),
                        },
                        "repositories": repositories,
                    }

                with patch.object(jwt, "encode", return_value="synthetic-jwt"):

                    def mint() -> GitHubAppInstallationToken:
                        return mint_runner_lifecycle_installation_token(
                            identity=GitHubAppIdentity(
                                app_id=42, client_id="Iv1.runner", private_key="synthetic-key"
                            ),
                            repository="example/repo",
                            repository_id="123",
                            repository_owner_id="8",
                            api_request=api_request,
                            now=datetime(2026, 8, 7, 14, tzinfo=timezone.utc),
                        )

                    if defect == "none":
                        token = mint()
                        self.assertEqual(token.repository_id, 123)
                        self.assertNotIn(token.token, repr(token))
                    else:
                        with self.assertRaises(GitHubAppIdentityError):
                            mint()
                        if defect not in {"client", "owner", "suspended"}:
                            self.assertEqual(calls[-1]["path"], "/installation/token")
                        else:
                            self.assertFalse(any(call.get("method") == "POST" for call in calls))

    def test_service_revokes_after_success_or_provider_failure(self) -> None:
        store = Mock()
        store.list_repository_inventory_records.return_value = ()
        for failure in (False, True):
            with (
                self.subTest(failure=failure),
                patch.object(
                    runner_lane_service,
                    "current_tracked_inventory_record",
                    return_value=Mock(repository_id="123", repository_owner_id="8"),
                ),
                patch.object(
                    runner_lane_service,
                    "runner_lifecycle_identity",
                    return_value=GitHubAppIdentity(app_id=42, private_key="synthetic-key"),
                ),
                patch.object(
                    runner_lane_service,
                    "mint_runner_lifecycle_installation_token",
                    return_value=installation_token(),
                ) as mint,
                patch.object(runner_lane_service, "revoke_installation_token") as revoke,
            ):

                def use() -> None:
                    with runner_lifecycle_token(cast(RunnerLaneStore, store), "example/repo"):
                        if failure:
                            raise MergeTrainGitHubError("synthetic-provider-failure")

                if failure:
                    with self.assertRaises(MergeTrainGitHubError):
                        use()
                else:
                    use()
                self.assertEqual(mint.call_args.kwargs["repository_id"], "123")
                revoke.assert_called_once_with(installation_token=installation_token())

    def test_service_does_not_return_installation_token(self) -> None:
        with (
            patch.object(
                runner_lane_service,
                "runner_lifecycle_token",
                return_value=nullcontext(installation_token()),
            ),
            patch.object(GitHubRunnerLaneInventoryReader, "read_runner_lane_inventory") as read,
        ):
            read.return_value.model_dump.return_value = {"repository": "example/repo", "lanes": []}
            result = execute_runner_lane_service_request(
                cast(RunnerLaneStore, Mock()),
                RunnerLaneServiceRequest(repository="example/repo", operation="inventory"),
            )
        self.assertNotIn(installation_token().token, result.model_dump_json())
        self.assertEqual(result.repository_id, installation_token().repository_id)


class RunnerLifecycleRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_lifecycle_workflow_can_obtain_registration_or_delete(self) -> None:
        identity = replace(
            workflow_identity(),
            workflow_ref="example/control/.github/workflows/runner-lane-registration.yml@refs/heads/main",
        )
        allowed = True
        app = FastAPI()
        runner_lanes.register_runner_lane_service_routes(
            cast(ApiRouteRegistrar, cast(object, app)),
            dependencies=ReadRouteDependencies(
                read_identity=lambda: identity,
                get_record_store=lambda: Mock(),
                next_trace_id=lambda: "test-trace",
                authorization_allows=lambda **kwargs: allowed,
                http_error=_launchplane_http_error,
                error_response_model=LaunchplaneErrorResponse,
            ),
        )
        result = RunnerLaneServiceResponse(
            repository="example/repo",
            operation="registration_token",
            app_id=42,
            installation_id=77,
            repository_id=123,
            expires_at=installation_token().expires_at,
            payload={"token": "synthetic-registration-configuration"},
        )
        with patch.object(
            runner_lanes, "execute_runner_lane_service_request", return_value=result
        ) as execute:
            payload = {"repository": "example/repo", "operation": "registration_token"}
            response = await http_request(app, "POST", RUNNER_LANE_SERVICE_ROUTE, payload=payload)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            allowed = False
            response = await http_request(app, "POST", RUNNER_LANE_SERVICE_ROUTE, payload=payload)
            self.assertEqual(response.status_code, 403)
            allowed = True
            identity = replace(
                identity, workflow_ref="example/control/.github/workflows/other.yml@refs/heads/main"
            )
            response = await http_request(app, "POST", RUNNER_LANE_SERVICE_ROUTE, payload=payload)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(execute.call_count, 1)

    async def test_human_inventory_does_not_enable_mutations_and_provider_errors_are_scrubbed(
        self,
    ) -> None:
        identity = GitHubHumanIdentity(
            login="director",
            github_id=8,
            name="Director",
            email="",
            organizations=frozenset(),
            teams=frozenset(),
            role="admin",
        )
        app = FastAPI()
        runner_lanes.register_runner_lane_service_routes(
            cast(ApiRouteRegistrar, cast(object, app)),
            dependencies=ReadRouteDependencies(
                read_identity=lambda: identity,
                get_record_store=lambda: Mock(),
                next_trace_id=lambda: "test-trace",
                authorization_allows=lambda **kwargs: True,
                http_error=_launchplane_http_error,
                error_response_model=LaunchplaneErrorResponse,
            ),
        )
        with patch.object(
            runner_lanes,
            "execute_runner_lane_service_request",
            side_effect=MergeTrainGitHubError("synthetic-sensitive-provider-response"),
        ) as execute:
            for operation, expected in (
                ("inventory", 503),
                ("registration_token", 403),
                ("retire", 403),
            ):
                payload: dict[str, object] = {"repository": "example/repo", "operation": operation}
                if operation == "retire":
                    payload["runner_id"] = 456
                response = await http_request(
                    app, "POST", RUNNER_LANE_SERVICE_ROUTE, payload=payload
                )
                self.assertEqual(response.status_code, expected, response.text)
                self.assertNotIn("synthetic-sensitive-provider-response", response.text)
            execute.assert_called_once()
