from __future__ import annotations

import base64
from collections.abc import Callable
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from typing import cast

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from control_plane.contracts.privileged_operation import (
    PRIVILEGED_OPERATION_SUMMARY_READ_ACTION,
    PRIVILEGED_SECRET_OPERATION_APPROVE_ACTION,
    PRIVILEGED_SECRET_OPERATION_CANCEL_ACTION,
    PRIVILEGED_SECRET_OPERATION_PLAN_ACTION,
    PRIVILEGED_SECRET_OPERATION_READ_ACTION,
    PRIVILEGED_SECRET_OPERATION_REVOKE_ACTION,
)
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.http_routes.privileged_operations import (
    PrivilegedOperationRouteDependencies,
    register_privileged_operation_routes,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import (
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    TerminalAgentIdentity,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.support.http import lifespan_client


class _ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="allow")


def _human() -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login="operator",
        github_id=123,
        name="Operator",
        email="operator@example.com",
        organizations=frozenset(),
        teams=frozenset(),
        role="admin",
    )


def _agent() -> TerminalAgentIdentity:
    return TerminalAgentIdentity(subject="agent:planner", token_label="planner")


def _policy(
    *,
    duplicate_human_rule: bool = False,
    unmanaged_only: bool = False,
    github_id_pinned: bool = True,
) -> LaunchplaneAuthzPolicy:
    human_rule: dict[str, object] = {
        "managed_set_id": "privileged-operations.secret-planning",
        "managed_rule_id": "human-secret-planner",
        "github_ids": [123],
        "roles": ["admin"],
        "products": ["launchplane"],
        "contexts": ["launchplane"],
        "actions": [
            PRIVILEGED_SECRET_OPERATION_PLAN_ACTION,
            PRIVILEGED_SECRET_OPERATION_READ_ACTION,
            PRIVILEGED_SECRET_OPERATION_CANCEL_ACTION,
            PRIVILEGED_SECRET_OPERATION_APPROVE_ACTION,
            PRIVILEGED_SECRET_OPERATION_REVOKE_ACTION,
        ],
    }
    if not github_id_pinned:
        human_rule.pop("github_ids")
    if unmanaged_only:
        human_rule.pop("managed_set_id")
        human_rule.pop("managed_rule_id")
        human_rule["actions"] = []
    human_rules = [human_rule]
    if duplicate_human_rule:
        human_rules.append(
            {
                **human_rule,
                "managed_rule_id": "human-secret-planner-duplicate",
            }
        )
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_humans": human_rules,
            "terminal_agents": [
                {
                    "managed_set_id": "privileged-operations.agent-summary",
                    "managed_rule_id": "agent-summary-reader",
                    "subjects": ["agent:planner"],
                    "token_labels": ["planner"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": [PRIVILEGED_OPERATION_SUMMARY_READ_ACTION],
                }
            ],
        }
    )


def _policy_record(policy: LaunchplaneAuthzPolicy) -> LaunchplaneAuthzPolicyRecord:
    return LaunchplaneAuthzPolicyRecord(
        record_id="launchplane-authz-policy-test",
        revision=3,
        source="test",
        updated_at="2026-08-22T19:55:00+00:00",
        policy=policy,
    )


class PrivilegedOperationHttpTests(unittest.IsolatedAsyncioTestCase):
    def _app(
        self,
        *,
        store: FilesystemRecordStore,
        policy: LaunchplaneAuthzPolicy,
        human_reader: Mock | None = None,
        policy_record_reader: Callable[[], object] | None = None,
    ) -> FastAPI:
        app = FastAPI()
        reader = human_reader or Mock(return_value=_human())

        def read_human() -> GitHubHumanIdentity:
            return cast(GitHubHumanIdentity, reader())

        trace_counter = iter(range(1, 100))

        def http_error(
            *,
            status_code: int,
            trace_id: str,
            code: str,
            message: str,
            authz: dict[str, object] | None = None,
        ) -> HTTPException:
            _ = trace_id, authz
            return HTTPException(status_code=status_code, detail={"code": code, "message": message})

        register_privileged_operation_routes(
            cast(ApiRouteRegistrar, app),
            dependencies=PrivilegedOperationRouteDependencies(
                common=ReadRouteDependencies(
                    read_identity=lambda: _agent(),
                    get_record_store=lambda: store,
                    next_trace_id=lambda: f"trace-{next(trace_counter)}",
                    authorization_allows=lambda **_: False,
                    http_error=http_error,
                    error_response_model=_ErrorResponse,
                ),
                read_github_human_identity=read_human,
                read_github_human_mutation_identity=read_human,
                policy_reader=lambda: policy,
                policy_record_reader=policy_record_reader or (lambda: _policy_record(policy)),
            ),
        )
        return app

    async def test_openapi_exposes_human_transitions_but_no_execute(self) -> None:
        with TemporaryDirectory() as directory:
            app = self._app(
                store=FilesystemRecordStore(Path(directory)),
                policy=_policy(),
            )
            paths = app.openapi()["paths"]

        self.assertIn("/v1/privileged-operations/plans/{operation_id}/approve", paths)
        self.assertIn("/v1/privileged-operations/plans/{operation_id}/revoke", paths)
        self.assertIn("/v1/privileged-operations/plans/{operation_id}/cancel", paths)
        self.assertNotIn("/v1/privileged-operations/plans/{operation_id}/execute", paths)

    async def test_human_plan_list_and_agent_summary_are_redacted(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_MASTER_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"x" * 32).decode()},
                clear=False,
            ),
        ):
            app = self._app(
                store=FilesystemRecordStore(Path(temporary_directory)),
                policy=_policy(),
            )
            async with lifespan_client(app) as client:
                create_response = await client.post(
                    "/v1/privileged-operations/plans",
                    json={
                        "descriptor_id": "managed-secret-reencryption",
                        "source_event_id": "browser-request-1",
                        "request": {
                            "reason": "Inspect canonical root migration",
                        },
                    },
                )
                self.assertEqual(create_response.status_code, 200)
                operation_id = create_response.json()["record"]["operation_id"]

                list_response = await client.get("/v1/privileged-operations/plans")
                summary_response = await client.get(
                    f"/v1/agent/privileged-operations/plans/{operation_id}"
                )

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.json()["total"], 1)
        self.assertEqual(summary_response.status_code, 200)
        summary_payload = summary_response.text
        self.assertNotIn("active_key_id", summary_payload)
        self.assertNotIn("retirement_blocked_key_ids", summary_payload)
        self.assertNotIn("request", summary_payload)
        self.assertIn("configured_secret_count", summary_payload)

    async def test_human_approval_and_revocation_are_replay_safe(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_MASTER_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"x" * 32).decode()},
                clear=False,
            ),
        ):
            app = self._app(
                store=FilesystemRecordStore(Path(temporary_directory)),
                policy=_policy(),
            )
            async with lifespan_client(app) as client:
                planned = await client.post(
                    "/v1/privileged-operations/plans",
                    json={
                        "source_event_id": "approval-plan-1",
                        "request": {"reason": "Inspect canonical root migration"},
                    },
                )
                operation_id = planned.json()["record"]["operation_id"]
                approval_payload = {
                    "source_event_id": "approval-event-1",
                    "reason": "Reviewed the redacted plan evidence",
                }
                approved = await client.post(
                    f"/v1/privileged-operations/plans/{operation_id}/approve",
                    json=approval_payload,
                )
                approval_replay = await client.post(
                    f"/v1/privileged-operations/plans/{operation_id}/approve",
                    json=approval_payload,
                )
                revocation_payload = {
                    "source_event_id": "revocation-event-1",
                    "reason": "Withdraw approval before worker execution",
                }
                revoked = await client.post(
                    f"/v1/privileged-operations/plans/{operation_id}/revoke",
                    json=revocation_payload,
                )
                revocation_replay = await client.post(
                    f"/v1/privileged-operations/plans/{operation_id}/revoke",
                    json=revocation_payload,
                )

        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["record"]["status"], "approved")
        self.assertEqual(approved.json()["record"]["approval"]["approver"]["github_id"], 123)
        self.assertEqual(approval_replay.json()["write_status"], "replayed")
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(revoked.json()["record"]["status"], "revoked")
        self.assertEqual(revocation_replay.json()["write_status"], "replayed")

    async def test_approval_requires_an_explicit_github_id_selector(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_MASTER_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"x" * 32).decode()},
                clear=False,
            ),
        ):
            app = self._app(
                store=FilesystemRecordStore(Path(temporary_directory)),
                policy=_policy(github_id_pinned=False),
            )
            async with lifespan_client(app) as client:
                planned = await client.post(
                    "/v1/privileged-operations/plans",
                    json={
                        "source_event_id": "unpinned-plan-1",
                        "request": {"reason": "Inspect canonical root migration"},
                    },
                )
                operation_id = planned.json()["record"]["operation_id"]
                response = await client.post(
                    f"/v1/privileged-operations/plans/{operation_id}/approve",
                    json={
                        "source_event_id": "unpinned-approval-1",
                        "reason": "Attempt approval without immutable identity pinning",
                    },
                )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "authorization_denied")

    async def test_approval_maps_active_policy_read_failure_to_service_unavailable(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            app = self._app(
                store=FilesystemRecordStore(Path(temporary_directory)),
                policy=_policy(),
                policy_record_reader=Mock(side_effect=LookupError("active policy unavailable")),
            )
            async with lifespan_client(app) as client:
                response = await client.post(
                    "/v1/privileged-operations/plans/privileged-operation-" + "a" * 32 + "/approve",
                    json={
                        "source_event_id": "policy-read-failure-1",
                        "reason": "Attempt approval while policy storage is unavailable",
                    },
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "authz_policy_unavailable")

    async def test_unmanaged_action_empty_rule_cannot_authorize_route(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            app = self._app(
                store=FilesystemRecordStore(Path(temporary_directory)),
                policy=_policy(unmanaged_only=True),
            )
            async with lifespan_client(app) as client:
                response = await client.get("/v1/privileged-operations/plans")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "authorization_denied")

    async def test_duplicate_managed_matches_fail_closed(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            app = self._app(
                store=FilesystemRecordStore(Path(temporary_directory)),
                policy=_policy(duplicate_human_rule=True),
            )
            async with lifespan_client(app) as client:
                response = await client.get("/v1/privileged-operations/plans")

        self.assertEqual(response.status_code, 403)

    async def test_human_dependency_runs_before_policy_reader(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            human_reader = Mock(side_effect=HTTPException(status_code=403, detail="human only"))
            policy = _policy()
            policy_reader = Mock(return_value=policy)
            app = FastAPI()

            def read_human() -> GitHubHumanIdentity:
                return cast(GitHubHumanIdentity, human_reader())

            register_privileged_operation_routes(
                cast(ApiRouteRegistrar, app),
                dependencies=PrivilegedOperationRouteDependencies(
                    common=ReadRouteDependencies(
                        read_identity=lambda: _agent(),
                        get_record_store=lambda: FilesystemRecordStore(Path(temporary_directory)),
                        next_trace_id=lambda: "trace-1",
                        authorization_allows=lambda **_: False,
                        http_error=lambda **kwargs: HTTPException(
                            status_code=int(kwargs["status_code"]), detail=kwargs["code"]
                        ),
                        error_response_model=_ErrorResponse,
                    ),
                    read_github_human_identity=read_human,
                    read_github_human_mutation_identity=read_human,
                    policy_reader=policy_reader,
                ),
            )
            async with lifespan_client(app) as client:
                response = await client.get("/v1/privileged-operations/plans")

        self.assertEqual(response.status_code, 403)
        policy_reader.assert_not_called()

    async def test_malformed_source_event_id_is_rejected_as_request_validation(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            app = self._app(
                store=FilesystemRecordStore(Path(temporary_directory)),
                policy=_policy(),
            )
            async with lifespan_client(app) as client:
                response = await client.post(
                    "/v1/privileged-operations/plans",
                    json={
                        "source_event_id": "browser request 1",
                        "request": {"reason": "Inspect canonical root migration"},
                    },
                )

        self.assertEqual(response.status_code, 422)

    async def test_store_protocol_failures_are_service_unavailable(self) -> None:
        app = self._app(
            store=cast(FilesystemRecordStore, object()),
            policy=_policy(),
        )
        async with lifespan_client(app) as client:
            responses = (
                await client.post(
                    "/v1/privileged-operations/plans",
                    json={
                        "source_event_id": "browser-request-1",
                        "request": {"reason": "Inspect canonical root migration"},
                    },
                ),
                await client.get("/v1/privileged-operations/plans"),
                await client.get(
                    "/v1/privileged-operations/plans/privileged-operation-" + "a" * 32
                ),
                await client.post(
                    "/v1/privileged-operations/plans/privileged-operation-" + "a" * 32 + "/cancel",
                    json={
                        "source_event_id": "browser-cancel-1",
                        "reason": "Cancel stale plan",
                    },
                ),
                await client.post(
                    "/v1/privileged-operations/plans/privileged-operation-" + "a" * 32 + "/approve",
                    json={
                        "source_event_id": "browser-approve-1",
                        "reason": "Approve reviewed plan",
                    },
                ),
                await client.post(
                    "/v1/privileged-operations/plans/privileged-operation-" + "a" * 32 + "/revoke",
                    json={
                        "source_event_id": "browser-revoke-1",
                        "reason": "Revoke reviewed plan",
                    },
                ),
                await client.get(
                    "/v1/agent/privileged-operations/plans/privileged-operation-" + "a" * 32
                ),
            )

        self.assertEqual(
            tuple(response.status_code for response in responses),
            (503, 503, 503, 503, 503, 503, 503),
        )
