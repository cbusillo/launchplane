from __future__ import annotations

import base64
from collections.abc import Callable
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from typing import cast

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from control_plane.contracts.privileged_operation import (
    AUTHZ_POLICY_OPERATION_APPROVE_ACTION,
    AUTHZ_POLICY_OPERATION_CANCEL_ACTION,
    AUTHZ_POLICY_OPERATION_PROPOSE_ACTION,
    AUTHZ_POLICY_OPERATION_READ_ACTION,
    AUTHZ_POLICY_OPERATION_REVOKE_ACTION,
    MERGE_TRAIN_POLICY_OPERATION_APPROVE_ACTION,
    MERGE_TRAIN_POLICY_OPERATION_CANCEL_ACTION,
    MERGE_TRAIN_POLICY_OPERATION_PROPOSE_ACTION,
    MERGE_TRAIN_POLICY_OPERATION_READ_ACTION,
    MERGE_TRAIN_POLICY_OPERATION_REVOKE_ACTION,
    MERGE_TRAIN_POLICY_OPERATION_SUMMARY_READ_ACTION,
    PRIVILEGED_OPERATION_SUMMARY_READ_ACTION,
    PRIVILEGED_POLICY_OPERATION_SUMMARY_READ_ACTION,
    PRIVILEGED_SECRET_OPERATION_APPROVE_ACTION,
    PRIVILEGED_SECRET_OPERATION_CANCEL_ACTION,
    PRIVILEGED_SECRET_OPERATION_PLAN_ACTION,
    PRIVILEGED_SECRET_OPERATION_READ_ACTION,
    PRIVILEGED_SECRET_OPERATION_REVOKE_ACTION,
)
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
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
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.http import lifespan_client
from tests.support.stores import _sqlite_database_url


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
    include_merge_train_policy_operation: bool = True,
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
    human_rules.append(
        {
            "managed_set_id": "privileged-operations.policy-planning",
            "managed_rule_id": "human-policy-planner",
            "github_ids": [123],
            "roles": ["admin"],
            "products": ["launchplane"],
            "contexts": ["launchplane"],
            "actions": [
                AUTHZ_POLICY_OPERATION_PROPOSE_ACTION,
                AUTHZ_POLICY_OPERATION_READ_ACTION,
                AUTHZ_POLICY_OPERATION_CANCEL_ACTION,
                AUTHZ_POLICY_OPERATION_APPROVE_ACTION,
                AUTHZ_POLICY_OPERATION_REVOKE_ACTION,
                "authz_policy_grant.write",
            ],
        }
    )
    human_rules.append(
        {
            "managed_set_id": "privileged-operations.policy-safety",
            "managed_rule_id": "independent-policy-admin",
            "github_ids": [456],
            "roles": ["admin"],
            "products": ["launchplane"],
            "contexts": ["launchplane"],
            "actions": ["authz_policy_grant.write"],
        }
    )
    if include_merge_train_policy_operation:
        human_rules.append(
            {
                "managed_set_id": "privileged-operations.merge-train-policy-planning",
                "managed_rule_id": "human-merge-train-policy-planner",
                "github_ids": [123],
                "roles": ["admin"],
                "products": ["launchplane"],
                "contexts": ["launchplane"],
                "actions": [
                    MERGE_TRAIN_POLICY_OPERATION_PROPOSE_ACTION,
                    MERGE_TRAIN_POLICY_OPERATION_READ_ACTION,
                    MERGE_TRAIN_POLICY_OPERATION_CANCEL_ACTION,
                    MERGE_TRAIN_POLICY_OPERATION_APPROVE_ACTION,
                    MERGE_TRAIN_POLICY_OPERATION_REVOKE_ACTION,
                ],
            }
        )
    terminal_rules: list[dict[str, object]] = [
        {
            "managed_set_id": "privileged-operations.agent-summary",
            "managed_rule_id": "agent-summary-reader",
            "subjects": ["agent:planner"],
            "token_labels": ["planner"],
            "products": ["launchplane"],
            "contexts": ["launchplane"],
            "actions": [PRIVILEGED_OPERATION_SUMMARY_READ_ACTION],
        },
        {
            "managed_set_id": "privileged-operations.policy-agent",
            "managed_rule_id": "agent-policy-proposer",
            "subjects": ["agent:planner"],
            "token_labels": ["planner"],
            "products": ["launchplane"],
            "contexts": ["launchplane"],
            "actions": [
                AUTHZ_POLICY_OPERATION_PROPOSE_ACTION,
                PRIVILEGED_POLICY_OPERATION_SUMMARY_READ_ACTION,
            ],
        },
        {
            "managed_set_id": "privileged-operations.policy-agent-other",
            "managed_rule_id": "agent-policy-proposer-other",
            "subjects": ["agent:other"],
            "token_labels": ["other"],
            "products": ["launchplane"],
            "contexts": ["launchplane"],
            "actions": [PRIVILEGED_POLICY_OPERATION_SUMMARY_READ_ACTION],
        },
    ]
    if include_merge_train_policy_operation:
        terminal_rules.append(
            {
                "managed_set_id": "privileged-operations.merge-train-policy-agent",
                "managed_rule_id": "agent-merge-train-policy-proposer",
                "subjects": ["agent:planner"],
                "token_labels": ["planner"],
                "products": ["launchplane"],
                "contexts": ["launchplane"],
                "actions": [
                    MERGE_TRAIN_POLICY_OPERATION_PROPOSE_ACTION,
                    MERGE_TRAIN_POLICY_OPERATION_SUMMARY_READ_ACTION,
                ],
            }
        )
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_humans": human_rules,
            "terminal_agents": terminal_rules,
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


def _managed_authz_plan_payload(source_event_id: str) -> dict[str, object]:
    return {
        "descriptor_id": "managed-authz-policy-set",
        "source_event_id": source_event_id,
        "request": {
            "managed_set_id": "privileged-operations.policy-planning",
            "reason": "Review the exact managed policy plan.",
            "desired_policy": {
                "schema_version": 2,
                "github_humans": [
                    {
                        "managed_set_id": "privileged-operations.policy-planning",
                        "managed_rule_id": "human-policy-planner",
                        "github_ids": [123],
                        "roles": ["admin"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            AUTHZ_POLICY_OPERATION_APPROVE_ACTION,
                            AUTHZ_POLICY_OPERATION_CANCEL_ACTION,
                            AUTHZ_POLICY_OPERATION_PROPOSE_ACTION,
                            AUTHZ_POLICY_OPERATION_READ_ACTION,
                            AUTHZ_POLICY_OPERATION_REVOKE_ACTION,
                            "authz_policy_grant.write",
                        ],
                    }
                ],
            },
        },
    }


def _merge_train_policy_plan_payload(source_event_id: str) -> dict[str, object]:
    return {
        "descriptor_id": "managed-merge-train-policy-import",
        "source_event_id": source_event_id,
        "request": {
            "record": build_test_merge_train_policy_record(
                repository="cbusillo/codex-skills",
                record_id="merge-train-policy-candidate",
                updated_at="2026-08-22T20:00:00+00:00",
            ).model_dump(mode="json"),
            "reason": "Review exact merge-train policy import.",
            "related_issue": "cbusillo/launchplane#2296",
        },
    }


class PrivilegedOperationHttpTests(unittest.IsolatedAsyncioTestCase):
    def _app(
        self,
        *,
        store: object,
        policy: LaunchplaneAuthzPolicy,
        human_reader: Mock | None = None,
        agent_identity: TerminalAgentIdentity | None = None,
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
                read_bearer_identity=lambda: agent_identity or _agent(),
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

    async def test_terminal_agent_proposes_and_reads_only_its_redacted_policy_summary(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            policy = _policy()
            policy_record = store.seed_authz_policy_if_absent(_policy_record(policy))
            app = self._app(
                store=store,
                policy=policy,
                policy_record_reader=lambda: policy_record,
            )
            try:
                async with lifespan_client(app) as client:
                    proposed = await client.post(
                        "/v1/agent/privileged-operations/plans",
                        json={
                            "source_event_id": "agent-policy-proposal-1",
                            "request": {
                                "managed_set_id": "test.policy-operation",
                                "reason": "Propose a bounded policy operation.",
                                "related_issue": "cbusillo/launchplane#2238",
                                "desired_policy": {
                                    "schema_version": 2,
                                    "github_humans": [
                                        {
                                            "managed_set_id": "test.policy-operation",
                                            "managed_rule_id": "policy-operation-reader",
                                            "github_ids": [789],
                                            "roles": ["admin"],
                                            "products": ["launchplane"],
                                            "contexts": ["launchplane"],
                                            "actions": ["authz_policy_operation.read"],
                                        }
                                    ],
                                },
                            },
                        },
                    )
                    operation_id = proposed.json()["summary"]["operation_id"]
                    read_response = await client.get(
                        f"/v1/agent/privileged-operations/plans/{operation_id}"
                    )
                other_app = self._app(
                    store=store,
                    policy=policy,
                    agent_identity=TerminalAgentIdentity(
                        subject="agent:other",
                        token_label="other",
                    ),
                    policy_record_reader=lambda: policy_record,
                )
                async with lifespan_client(other_app) as client:
                    other_response = await client.get(
                        f"/v1/agent/privileged-operations/plans/{operation_id}"
                    )
            finally:
                store.close()

        self.assertEqual(proposed.status_code, 200)
        self.assertEqual(read_response.status_code, 200)
        rendered = read_response.text
        self.assertNotIn("desired_policy", rendered)
        self.assertNotIn("policy-operation-reader", rendered)
        self.assertNotIn("Propose a bounded policy operation", rendered)
        self.assertEqual(other_response.status_code, 403)

    async def test_browser_plans_merge_train_policy_import_with_dedicated_actions(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            store.write_merge_train_policy_record(
                build_test_merge_train_policy_record(
                    repository="cbusillo/sellyouroutboard",
                    record_id="merge-train-policy-active",
                    updated_at="2026-08-22T19:00:00+00:00",
                )
            )
            app = self._app(store=store, policy=_policy())

            async with lifespan_client(app) as client:
                response = await client.post(
                    "/v1/privileged-operations/plans",
                    json=_merge_train_policy_plan_payload("browser-merge-train-plan-1"),
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["record"]["descriptor_id"], "managed-merge-train-policy-import")
        self.assertEqual(
            payload["record"]["evidence"]["added_policy_keys"], ["cbusillo/codex-skills:main"]
        )
        evidence_json = json.dumps(payload["record"]["evidence"], sort_keys=True)
        self.assertNotIn("GH_TOKEN", evidence_json)
        self.assertNotIn("launchplane-merge-train", evidence_json)

    async def test_authz_policy_operation_grant_does_not_authorize_merge_train_policy_operation(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            store.write_merge_train_policy_record(build_test_merge_train_policy_record())
            app = self._app(
                store=store,
                policy=_policy(include_merge_train_policy_operation=False),
            )

            async with lifespan_client(app) as client:
                response = await client.post(
                    "/v1/privileged-operations/plans",
                    json=_merge_train_policy_plan_payload("browser-merge-train-denied"),
                )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "authorization_denied")

    async def test_terminal_agent_proposes_merge_train_policy_import_through_generic_route(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            store.write_merge_train_policy_record(
                build_test_merge_train_policy_record(
                    repository="cbusillo/sellyouroutboard",
                    record_id="merge-train-policy-active",
                    updated_at="2026-08-22T19:00:00+00:00",
                )
            )
            policy = _policy()
            app = self._app(store=store, policy=policy)

            async with lifespan_client(app) as client:
                proposed = await client.post(
                    "/v1/agent/privileged-operations/plans",
                    json=_merge_train_policy_plan_payload("agent-merge-train-plan-1"),
                )
                operation_id = proposed.json()["summary"]["operation_id"]
                read_response = await client.get(
                    f"/v1/agent/privileged-operations/plans/{operation_id}"
                )

        self.assertEqual(proposed.status_code, 200, proposed.text)
        self.assertEqual(read_response.status_code, 200, read_response.text)
        self.assertEqual(
            proposed.json()["summary"]["descriptor_id"],
            "managed-merge-train-policy-import",
        )
        self.assertEqual(
            proposed.json()["summary"]["added_policy_keys"],
            ["cbusillo/codex-skills:main"],
        )
        self.assertNotIn("GH_TOKEN", read_response.text)

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

    async def test_managed_authz_approval_rejects_stale_and_accepts_current_exact_plan(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            policy = _policy()
            current_record = store.seed_authz_policy_if_absent(_policy_record(policy))
            app = self._app(
                store=store,
                policy=policy,
                policy_record_reader=lambda: store.list_authz_policy_records(
                    status="active", limit=2
                )[0],
            )
            try:
                async with lifespan_client(app) as client:
                    planned = await client.post(
                        "/v1/privileged-operations/plans",
                        json=_managed_authz_plan_payload("managed-policy-plan-1"),
                    )
                    self.assertEqual(planned.status_code, 200, planned.text)
                    operation_id = planned.json()["record"]["operation_id"]
                    revised_policy = policy.model_copy(update={"administrator_quorum": 2})
                    revised_digest = authz_policy_sha256(revised_policy)
                    revised_record = current_record.model_copy(
                        update={
                            "record_id": build_authz_policy_record_id(
                                revision=current_record.revision + 1,
                                policy_sha256=revised_digest,
                            ),
                            "revision": current_record.revision + 1,
                            "source": "test:intervening-policy-revision",
                            "policy_sha256": revised_digest,
                            "policy": revised_policy,
                        }
                    )
                    self.assertEqual(
                        store.compare_and_write_authz_policy_record(
                            expected_record=current_record,
                            replacement_record=revised_record,
                        ).status,
                        "written",
                    )
                    stale_approval = await client.post(
                        f"/v1/privileged-operations/plans/{operation_id}/approve",
                        json={
                            "source_event_id": "managed-policy-approval-stale",
                            "reason": "Approve the stale managed policy plan.",
                        },
                    )
                    current_plan = await client.post(
                        "/v1/privileged-operations/plans",
                        json=_managed_authz_plan_payload("managed-policy-plan-2"),
                    )
                    current_operation_id = current_plan.json()["record"]["operation_id"]
                    current_approval = await client.post(
                        f"/v1/privileged-operations/plans/{current_operation_id}/approve",
                        json={
                            "source_event_id": "managed-policy-approval-current",
                            "reason": "Approve the current managed policy plan.",
                        },
                    )
            finally:
                store.close()

        self.assertEqual(stale_approval.status_code, 409, stale_approval.text)
        self.assertEqual(stale_approval.json()["detail"]["code"], "privileged_operation_plan_stale")
        self.assertEqual(current_plan.status_code, 200, current_plan.text)
        self.assertEqual(current_approval.status_code, 200, current_approval.text)
        self.assertEqual(current_approval.json()["record"]["status"], "approved")

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
                    read_bearer_identity=lambda: _agent(),
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
