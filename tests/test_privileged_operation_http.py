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

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.authz_candidate_preparation import (
    ADMINISTRATOR_PRODUCT_EVIDENCE_CONTEXT_RULE_ID,
    ADMINISTRATOR_PRODUCT_EVIDENCE_ENVIRONMENT_RULE_ID,
    ADMINISTRATOR_PRODUCT_EVIDENCE_READ_ACTIONS,
    ADMINISTRATOR_PRODUCT_EVIDENCE_READ_MANAGED_SET_ID,
    ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS,
    ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_RULE_ID,
    ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID,
)
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
    ManagedAuthzPolicySetHumanEvidence,
    ManagedAuthzPolicySetProposalInput,
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
from control_plane.storage.postgres import (
    LaunchplanePrivilegedOperationRow,
    PostgresRecordStore,
)
from tests.support.http import lifespan_client
from tests.support.stores import _sqlite_database_url
from tests.test_ordinary_agent_activation_storage import (
    _event as _activation_event,
    _record as _activation_record,
)


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


def _policy_with_product_evidence(*, github_id: int = 123) -> LaunchplaneAuthzPolicy:
    payload = _policy().model_dump(mode="json")
    payload["github_humans"].extend(
        (
            {
                "managed_set_id": ADMINISTRATOR_PRODUCT_EVIDENCE_READ_MANAGED_SET_ID,
                "managed_rule_id": ADMINISTRATOR_PRODUCT_EVIDENCE_CONTEXT_RULE_ID,
                "github_ids": [github_id],
                "roles": ["admin"],
                "contexts": ["launchplane"],
                "actions": list(ADMINISTRATOR_PRODUCT_EVIDENCE_READ_ACTIONS),
            },
            {
                "managed_set_id": ADMINISTRATOR_PRODUCT_EVIDENCE_READ_MANAGED_SET_ID,
                "managed_rule_id": ADMINISTRATOR_PRODUCT_EVIDENCE_ENVIRONMENT_RULE_ID,
                "github_ids": [github_id],
                "roles": ["admin"],
                "contexts": ["launchplane"],
                "instances": ["*"],
                "actions": list(ADMINISTRATOR_PRODUCT_EVIDENCE_READ_ACTIONS),
            },
        )
    )
    return LaunchplaneAuthzPolicy.model_validate(payload)


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

        self.assertIn("/v1/privileged-operations/plans/{operation_id}/review", paths)
        self.assertIn("/v1/privileged-operations/plans/{operation_id}/approve", paths)
        self.assertIn("/v1/privileged-operations/plans/{operation_id}/revoke", paths)
        self.assertIn("/v1/privileged-operations/plans/{operation_id}/cancel", paths)
        self.assertNotIn("/v1/privileged-operations/plans/{operation_id}/execute", paths)

    async def test_closed_authorization_candidate_plans_once_and_replays(self) -> None:
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
            payload = {
                "candidate_id": "ordinary-agent-delivery-administration",
                "intent": "add",
                "source_event_id": "ui:authorization-candidate:add:stable-retry",
            }
            async with lifespan_client(app) as client:
                first = await client.post(
                    "/v1/privileged-operations/authorization-candidates/prepare",
                    json=payload,
                )
                replay = await client.post(
                    "/v1/privileged-operations/authorization-candidates/prepare",
                    json=payload,
                )
                review = await client.get(
                    f"/v1/privileged-operations/plans/{first.json()['operation_id']}/review"
                )
            operation_records = store.list_privileged_operation_records(limit=None)
            operation_events = store.list_privileged_operation_event_records(
                operation_id=first.json()["operation_id"], limit=None
            )
            active_policy_record = store.list_authz_policy_records(status="active", limit=2)[0]
            store.close()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["state"], "planned")
        self.assertEqual(replay.json()["operation_id"], first.json()["operation_id"])
        self.assertEqual(review.status_code, 200, review.text)
        self.assertEqual(
            len(operation_records),
            1,
        )
        self.assertEqual(
            len(operation_events),
            1,
        )
        self.assertEqual(review.json()["review"]["title"], "Review agent delivery administration")
        self.assertIn("does not enroll", review.json()["review"]["change"]["summary"])
        self.assertEqual(policy_record, active_policy_record)

    async def test_closed_authorization_candidate_active_is_noop(self) -> None:
        policy_payload = _policy().model_dump(mode="json")
        policy_payload["github_humans"].append(
            {
                "managed_set_id": ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID,
                "managed_rule_id": ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_RULE_ID,
                "github_ids": [123],
                "roles": ["admin"],
                "products": ["launchplane"],
                "contexts": ["launchplane"],
                "actions": list(ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS),
            }
        )
        policy = LaunchplaneAuthzPolicy.model_validate(policy_payload)
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            policy_record = store.seed_authz_policy_if_absent(_policy_record(policy))
            app = self._app(
                store=store,
                policy=policy,
                policy_record_reader=lambda: policy_record,
            )
            async with lifespan_client(app) as client:
                response = await client.post(
                    "/v1/privileged-operations/authorization-candidates/prepare",
                    json={
                        "candidate_id": "ordinary-agent-delivery-administration",
                        "intent": "add",
                        "source_event_id": "already-active",
                    },
                )
            records = store.list_privileged_operation_records(limit=None)
            store.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "already_satisfied")
        self.assertNotIn("operation_id", response.json())
        self.assertEqual(records, ())

    async def test_product_evidence_candidate_plans_once_with_clear_human_review(self) -> None:
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
            payload = {
                "candidate_id": "administrator-product-evidence-read",
                "intent": "add",
                "source_event_id": "ui:authorization-candidate:product-evidence:add",
            }
            async with lifespan_client(app) as client:
                first = await client.post(
                    "/v1/privileged-operations/authorization-candidates/prepare",
                    json=payload,
                )
                replay = await client.post(
                    "/v1/privileged-operations/authorization-candidates/prepare",
                    json=payload,
                )
                review = await client.get(
                    f"/v1/privileged-operations/plans/{first.json()['operation_id']}/review"
                )
            records = store.list_privileged_operation_records(limit=None)
            events = store.list_privileged_operation_event_records(
                operation_id=first.json()["operation_id"], limit=None
            )
            active_policy_record = store.list_authz_policy_records(status="active", limit=2)[0]
            store.close()

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["state"], "planned")
        self.assertEqual(replay.json()["operation_id"], first.json()["operation_id"])
        self.assertEqual(len(records), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(policy_record, active_policy_record)
        planned = records[0]
        assert isinstance(planned.request, ManagedAuthzPolicySetProposalInput)
        self.assertEqual(
            planned.request.managed_set_id, ADMINISTRATOR_PRODUCT_EVIDENCE_READ_MANAGED_SET_ID
        )
        self.assertEqual(
            {rule.managed_rule_id for rule in planned.request.desired_policy.github_humans},
            {
                ADMINISTRATOR_PRODUCT_EVIDENCE_CONTEXT_RULE_ID,
                ADMINISTRATOR_PRODUCT_EVIDENCE_ENVIRONMENT_RULE_ID,
            },
        )
        self.assertEqual(review.status_code, 200, review.text)
        semantic_review = review.json()["review"]
        self.assertEqual(semantic_review["title"], "Review administrator product evidence access")
        self.assertEqual(semantic_review["blockers"]["state"], "clear")
        self.assertTrue(semantic_review["can_approve"])
        summary = semantic_review["change"]["summary"]
        for phrase in (
            "requesting administrator",
            "project-level and environment-level",
            "all current and future projects",
            "standing until a separately governed removal",
            "Approve-by deadline only bounds this plan",
            "no writes or agent authority",
        ):
            self.assertIn(phrase, summary)

    async def test_product_evidence_candidate_noops_create_no_operation(self) -> None:
        cases = (
            (_policy_with_product_evidence(), "add"),
            (_policy(), "remove"),
        )
        for policy, intent in cases:
            with self.subTest(intent=intent), TemporaryDirectory() as directory:
                store = PostgresRecordStore(
                    database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
                )
                store.ensure_schema()
                policy_record = store.seed_authz_policy_if_absent(_policy_record(policy))
                app = self._app(
                    store=store,
                    policy=policy,
                    policy_record_reader=lambda: policy_record,
                )
                async with lifespan_client(app) as client:
                    response = await client.post(
                        "/v1/privileged-operations/authorization-candidates/prepare",
                        json={
                            "candidate_id": "administrator-product-evidence-read",
                            "intent": intent,
                            "source_event_id": f"product-evidence-noop-{intent}",
                        },
                    )
                records = store.list_privileged_operation_records(limit=None)
                store.close()

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json(),
                {
                    "trace_id": response.json()["trace_id"],
                    "state": "already_satisfied",
                },
            )
            self.assertEqual(records, ())

    async def test_product_evidence_removal_does_not_depend_on_delivery_activation(self) -> None:
        policy = _policy_with_product_evidence()
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            policy_record = store.seed_authz_policy_if_absent(_policy_record(policy))
            activation = _activation_record(
                operation_id="privileged-operation-" + "c" * 32,
                installed_at="2026-09-12T12:00:00Z",
                expires_at="2026-09-13T12:00:00Z",
            )
            store.install_ordinary_agent_delivery_activation(
                activation,
                _activation_event(
                    activation,
                    action="installed",
                    source_operation_id=activation.source_setup_operation_id,
                ),
            )
            app = self._app(store=store, policy=policy, policy_record_reader=lambda: policy_record)
            async with lifespan_client(app) as client:
                response = await client.post(
                    "/v1/privileged-operations/authorization-candidates/prepare",
                    json={
                        "candidate_id": "administrator-product-evidence-read",
                        "intent": "remove",
                        "source_event_id": "product-evidence-remove-with-active-delivery",
                    },
                )
                review = await client.get(
                    f"/v1/privileged-operations/plans/{response.json()['operation_id']}/review"
                )
            records = store.list_privileged_operation_records(limit=None)
            store.close()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "planned")
        self.assertEqual(len(records), 1)
        planned = records[0]
        assert isinstance(planned.request, ManagedAuthzPolicySetProposalInput)
        self.assertEqual(planned.request.desired_policy.github_humans, ())
        self.assertEqual(review.status_code, 200, review.text)
        summary = review.json()["review"]["change"]["summary"]
        self.assertIn("Access granted elsewhere may remain", summary)
        self.assertNotIn("Stop the current agent delivery setup", summary)

    async def test_product_evidence_candidate_set_collision_is_bounded_conflict(self) -> None:
        policy_payload = _policy().model_dump(mode="json")
        policy_payload["github_humans"].append(
            {
                "managed_set_id": ADMINISTRATOR_PRODUCT_EVIDENCE_READ_MANAGED_SET_ID,
                "managed_rule_id": ADMINISTRATOR_PRODUCT_EVIDENCE_CONTEXT_RULE_ID,
                "github_ids": [456],
                "roles": ["admin"],
                "contexts": ["launchplane"],
                "actions": list(ADMINISTRATOR_PRODUCT_EVIDENCE_READ_ACTIONS),
            }
        )
        policy = LaunchplaneAuthzPolicy.model_validate(policy_payload)
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            policy_record = store.seed_authz_policy_if_absent(_policy_record(policy))
            app = self._app(store=store, policy=policy, policy_record_reader=lambda: policy_record)
            async with lifespan_client(app) as client:
                response = await client.post(
                    "/v1/privileged-operations/authorization-candidates/prepare",
                    json={
                        "candidate_id": "administrator-product-evidence-read",
                        "intent": "add",
                        "source_event_id": "product-evidence-set-collision",
                    },
                )
            records = store.list_privileged_operation_records(limit=None)
            store.close()

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "authorization_candidate_set_conflict")
        self.assertEqual(records, ())

    async def test_wrong_human_product_evidence_shape_keeps_generic_review(self) -> None:
        policy = _policy()
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            policy_record = store.seed_authz_policy_if_absent(_policy_record(policy))
            app = self._app(store=store, policy=policy, policy_record_reader=lambda: policy_record)
            async with lifespan_client(app) as client:
                planned = await client.post(
                    "/v1/privileged-operations/plans",
                    json={
                        "descriptor_id": "managed-authz-policy-set",
                        "source_event_id": "wrong-human-product-evidence-shape",
                        "request": {
                            "managed_set_id": ADMINISTRATOR_PRODUCT_EVIDENCE_READ_MANAGED_SET_ID,
                            "reason": "Prepare read-only administrator access to product evidence.",
                            "related_issue": "#2058",
                            "desired_policy": {
                                "schema_version": 2,
                                "github_humans": [
                                    {
                                        "managed_set_id": (
                                            ADMINISTRATOR_PRODUCT_EVIDENCE_READ_MANAGED_SET_ID
                                        ),
                                        "managed_rule_id": (
                                            ADMINISTRATOR_PRODUCT_EVIDENCE_CONTEXT_RULE_ID
                                        ),
                                        "github_ids": [456],
                                        "roles": ["admin"],
                                        "contexts": ["launchplane"],
                                        "actions": list(
                                            ADMINISTRATOR_PRODUCT_EVIDENCE_READ_ACTIONS
                                        ),
                                    },
                                    {
                                        "managed_set_id": (
                                            ADMINISTRATOR_PRODUCT_EVIDENCE_READ_MANAGED_SET_ID
                                        ),
                                        "managed_rule_id": (
                                            ADMINISTRATOR_PRODUCT_EVIDENCE_ENVIRONMENT_RULE_ID
                                        ),
                                        "github_ids": [456],
                                        "roles": ["admin"],
                                        "contexts": ["launchplane"],
                                        "instances": ["*"],
                                        "actions": list(
                                            ADMINISTRATOR_PRODUCT_EVIDENCE_READ_ACTIONS
                                        ),
                                    },
                                ],
                            },
                        },
                    },
                )
                self.assertEqual(planned.status_code, 200, planned.text)
                review = await client.get(
                    "/v1/privileged-operations/plans/"
                    f"{planned.json()['record']['operation_id']}/review"
                )
                candidate_replay = await client.post(
                    "/v1/privileged-operations/authorization-candidates/prepare",
                    json={
                        "candidate_id": "administrator-product-evidence-read",
                        "intent": "add",
                        "source_event_id": "wrong-human-product-evidence-shape",
                    },
                )
            store.close()

        self.assertEqual(review.status_code, 200, review.text)
        self.assertEqual(review.json()["review"]["title"], "Managed authorization policy review")
        self.assertEqual(candidate_replay.status_code, 409, candidate_replay.text)
        self.assertEqual(
            candidate_replay.json()["detail"]["code"], "privileged_operation_plan_conflict"
        )

    async def test_authorization_candidate_replay_rejects_both_cross_candidate_directions(
        self,
    ) -> None:
        candidates = (
            "ordinary-agent-delivery-administration",
            "administrator-product-evidence-read",
        )
        for first_candidate, second_candidate in (candidates, tuple(reversed(candidates))):
            with self.subTest(first=first_candidate), TemporaryDirectory() as directory:
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
                source_event_id = "cross-candidate-replay-conflict"
                async with lifespan_client(app) as client:
                    first = await client.post(
                        "/v1/privileged-operations/authorization-candidates/prepare",
                        json={
                            "candidate_id": first_candidate,
                            "intent": "add",
                            "source_event_id": source_event_id,
                        },
                    )
                    conflict = await client.post(
                        "/v1/privileged-operations/authorization-candidates/prepare",
                        json={
                            "candidate_id": second_candidate,
                            "intent": "add",
                            "source_event_id": source_event_id,
                        },
                    )
                records = store.list_privileged_operation_records(limit=None)
                store.close()

            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(conflict.status_code, 409, conflict.text)
            self.assertEqual(
                conflict.json()["detail"]["code"], "privileged_operation_plan_conflict"
            )
            self.assertEqual(len(records), 1)

    async def test_closed_authorization_candidate_rejects_replay_for_other_recipient(
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
            source_event_id = "candidate-recipient-conflict"
            generic_payload = {
                "descriptor_id": "managed-authz-policy-set",
                "source_event_id": source_event_id,
                "request": {
                    "managed_set_id": (ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID),
                    "desired_policy": {
                        "schema_version": 2,
                        "github_humans": [
                            {
                                "managed_set_id": (
                                    ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID
                                ),
                                "managed_rule_id": (
                                    ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_RULE_ID
                                ),
                                "github_ids": [456],
                                "roles": ["admin"],
                                "products": ["launchplane"],
                                "contexts": ["launchplane"],
                                "actions": list(ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS),
                            }
                        ],
                    },
                    "schema_migration": "reject",
                    "reason": "Prepare bounded ordinary-agent delivery administration.",
                    "related_issue": "#2369",
                },
            }
            async with lifespan_client(app) as client:
                generic_plan = await client.post(
                    "/v1/privileged-operations/plans",
                    json=generic_payload,
                )
                self.assertEqual(generic_plan.status_code, 200, generic_plan.text)
                generic_review = await client.get(
                    "/v1/privileged-operations/plans/"
                    f"{generic_plan.json()['record']['operation_id']}/review"
                )
                response = await client.post(
                    "/v1/privileged-operations/authorization-candidates/prepare",
                    json={
                        "candidate_id": "ordinary-agent-delivery-administration",
                        "intent": "add",
                        "source_event_id": source_event_id,
                    },
                )
            operation_records = store.list_privileged_operation_records(limit=None)
            store.close()

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(generic_review.status_code, 200, generic_review.text)
        self.assertEqual(
            generic_review.json()["review"]["title"],
            "Managed authorization policy review",
        )
        self.assertEqual(
            response.json()["detail"]["code"],
            "privileged_operation_plan_conflict",
        )
        self.assertEqual(len(operation_records), 1)
        planned_request = operation_records[0].request
        assert isinstance(planned_request, ManagedAuthzPolicySetProposalInput)
        planned_rule = planned_request.desired_policy.github_humans[0]
        self.assertEqual(planned_rule.github_ids, (456,))

    async def test_closed_authorization_candidate_removal_requires_stopped_activation(
        self,
    ) -> None:
        policy_payload = _policy().model_dump(mode="json")
        policy_payload["github_humans"].append(
            {
                "managed_set_id": (ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID),
                "managed_rule_id": (ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_RULE_ID),
                "github_ids": [123],
                "roles": ["admin"],
                "products": ["launchplane"],
                "contexts": ["launchplane"],
                "actions": list(ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS),
            }
        )
        policy = LaunchplaneAuthzPolicy.model_validate(policy_payload)
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            policy_record = store.seed_authz_policy_if_absent(_policy_record(policy))
            activation = _activation_record(
                operation_id="privileged-operation-" + "a" * 32,
                installed_at="2026-09-12T12:00:00Z",
                expires_at="2026-09-13T12:00:00Z",
            )
            store.install_ordinary_agent_delivery_activation(
                activation,
                _activation_event(
                    activation,
                    action="installed",
                    source_operation_id=activation.source_setup_operation_id,
                ),
            )
            app = self._app(
                store=store,
                policy=policy,
                policy_record_reader=lambda: policy_record,
            )
            source_event_id = "candidate-removal-after-stop"
            async with lifespan_client(app) as client:
                blocked = await client.post(
                    "/v1/privileged-operations/authorization-candidates/prepare",
                    json={
                        "candidate_id": "ordinary-agent-delivery-administration",
                        "intent": "remove",
                        "source_event_id": source_event_id,
                    },
                )
                revoked = type(activation).model_validate(
                    {
                        **activation.model_dump(mode="json"),
                        "desired_state": "revoked",
                        "effective_state": "revoked",
                        "revision": activation.revision + 1,
                        "updated_at": "2026-09-12T12:05:00Z",
                        "revoked_at": "2026-09-12T12:05:00Z",
                        "activation_sha256": "",
                    }
                )
                store.revoke_ordinary_agent_delivery_activation(
                    revoked,
                    _activation_event(
                        revoked,
                        action="revoked",
                        source_operation_id="privileged-operation-" + "b" * 32,
                        previous=activation,
                    ),
                )
                removal = await client.post(
                    "/v1/privileged-operations/authorization-candidates/prepare",
                    json={
                        "candidate_id": "ordinary-agent-delivery-administration",
                        "intent": "remove",
                        "source_event_id": source_event_id,
                    },
                )
                opposite_intent = await client.post(
                    "/v1/privileged-operations/authorization-candidates/prepare",
                    json={
                        "candidate_id": "ordinary-agent-delivery-administration",
                        "intent": "add",
                        "source_event_id": source_event_id,
                    },
                )
            operation_records = store.list_privileged_operation_records(limit=None)
            store.close()

        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(
            blocked.json()["detail"]["code"],
            "authorization_candidate_activation_current",
        )
        self.assertIn("Stop the current agent delivery setup", blocked.text)
        self.assertEqual(removal.status_code, 200, removal.text)
        self.assertEqual(removal.json()["state"], "planned")
        self.assertEqual(len(operation_records), 1)
        removal_request = operation_records[0].request
        assert isinstance(removal_request, ManagedAuthzPolicySetProposalInput)
        self.assertEqual(removal_request.desired_policy.github_humans, ())
        self.assertEqual(opposite_intent.status_code, 409, opposite_intent.text)
        self.assertEqual(
            opposite_intent.json()["detail"]["code"],
            "privileged_operation_plan_conflict",
        )

    async def test_closed_authorization_candidate_requires_runtime_propose(self) -> None:
        runtime_payload = _policy().model_dump(mode="json")
        runtime_payload["github_humans"][1]["actions"].remove(AUTHZ_POLICY_OPERATION_PROPOSE_ACTION)
        runtime_policy = LaunchplaneAuthzPolicy.model_validate(runtime_payload)
        await self._assert_candidate_denied_without_writes(
            runtime_policy=runtime_policy,
            persisted_policy=_policy(),
            source_event_id="candidate-runtime-propose-denied",
        )

    async def test_closed_authorization_candidate_requires_fresh_db_propose(self) -> None:
        persisted_payload = _policy().model_dump(mode="json")
        persisted_payload["github_humans"][1]["actions"].remove(
            AUTHZ_POLICY_OPERATION_PROPOSE_ACTION
        )
        persisted_policy = LaunchplaneAuthzPolicy.model_validate(persisted_payload)
        await self._assert_candidate_denied_without_writes(
            runtime_policy=_policy(),
            persisted_policy=persisted_policy,
            source_event_id="candidate-db-propose-denied",
        )

    async def test_closed_authorization_candidate_requires_strict_administrator(self) -> None:
        policy_payload = _policy().model_dump(mode="json")
        policy_payload["github_humans"][1]["logins"] = ["operator"]
        non_strict_policy = LaunchplaneAuthzPolicy.model_validate(policy_payload)
        await self._assert_candidate_denied_without_writes(
            runtime_policy=non_strict_policy,
            persisted_policy=non_strict_policy,
            source_event_id="candidate-strict-admin-denied",
        )

    async def test_product_evidence_candidate_requires_runtime_and_fresh_db_authority(self) -> None:
        runtime_denied_payload = _policy().model_dump(mode="json")
        runtime_denied_payload["github_humans"][1]["actions"].remove(
            AUTHZ_POLICY_OPERATION_PROPOSE_ACTION
        )
        persisted_denied_payload = _policy().model_dump(mode="json")
        persisted_denied_payload["github_humans"][1]["actions"].remove(
            AUTHZ_POLICY_OPERATION_PROPOSE_ACTION
        )
        cases = (
            (
                LaunchplaneAuthzPolicy.model_validate(runtime_denied_payload),
                _policy(),
                "product-evidence-runtime-propose-denied",
            ),
            (
                _policy(),
                LaunchplaneAuthzPolicy.model_validate(persisted_denied_payload),
                "product-evidence-db-propose-denied",
            ),
        )
        for runtime_policy, persisted_policy, source_event_id in cases:
            with self.subTest(source_event_id=source_event_id):
                await self._assert_candidate_denied_without_writes(
                    runtime_policy=runtime_policy,
                    persisted_policy=persisted_policy,
                    source_event_id=source_event_id,
                    candidate_id="administrator-product-evidence-read",
                )

    async def test_closed_authorization_candidate_rejects_caller_policy_or_identity_fields(
        self,
    ) -> None:
        for field, value in (
            ("github_id", 123),
            ("desired_policy", {"schema_version": 2}),
            ("managed_set_id", "caller-selected-set"),
            ("reason", "caller supplied reason"),
        ):
            with self.subTest(field=field), TemporaryDirectory() as directory:
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
                payload = {
                    "candidate_id": "ordinary-agent-delivery-administration",
                    "intent": "add",
                    "source_event_id": f"candidate-extra-{field}",
                    field: value,
                }
                async with lifespan_client(app) as client:
                    response = await client.post(
                        "/v1/privileged-operations/authorization-candidates/prepare",
                        json=payload,
                    )
                operation_records = store.list_privileged_operation_records(limit=None)
                policy_records = store.list_authz_policy_records(status="active", limit=None)
                store.close()

            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(operation_records, ())
            self.assertEqual(policy_records, (policy_record,))

    async def test_authorization_candidate_discriminator_remains_required(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            policy = _policy()
            policy_record = store.seed_authz_policy_if_absent(_policy_record(policy))
            app = self._app(store=store, policy=policy, policy_record_reader=lambda: policy_record)
            async with lifespan_client(app) as client:
                response = await client.post(
                    "/v1/privileged-operations/authorization-candidates/prepare",
                    json={
                        "intent": "add",
                        "source_event_id": "candidate-without-discriminator",
                    },
                )
            operation_records = store.list_privileged_operation_records(limit=None)
            store.close()

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(operation_records, ())

    async def _assert_candidate_denied_without_writes(
        self,
        *,
        runtime_policy: LaunchplaneAuthzPolicy,
        persisted_policy: LaunchplaneAuthzPolicy,
        source_event_id: str,
        candidate_id: str = "ordinary-agent-delivery-administration",
    ) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            policy_record = store.seed_authz_policy_if_absent(_policy_record(persisted_policy))
            app = self._app(
                store=store,
                policy=runtime_policy,
                policy_record_reader=lambda: policy_record,
            )
            async with lifespan_client(app) as client:
                response = await client.post(
                    "/v1/privileged-operations/authorization-candidates/prepare",
                    json={
                        "candidate_id": candidate_id,
                        "intent": "add",
                        "source_event_id": source_event_id,
                    },
                )
            operation_records = store.list_privileged_operation_records(limit=None)
            policy_records = store.list_authz_policy_records(status="active", limit=None)
            store.close()

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["detail"]["code"], "authorization_denied")
        self.assertEqual(operation_records, ())
        self.assertEqual(policy_records, (policy_record,))

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

    async def test_merge_train_policy_approval_reports_history_drift_as_stale(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = FilesystemRecordStore(Path(temporary_directory))
            store.write_merge_train_policy_record(
                build_test_merge_train_policy_record(
                    repository="cbusillo/sellyouroutboard",
                    record_id="merge-train-policy-active",
                )
            )
            app = self._app(store=store, policy=_policy())
            async with lifespan_client(app) as client:
                planned = await client.post(
                    "/v1/privileged-operations/plans",
                    json=_merge_train_policy_plan_payload("browser-merge-train-stale-plan"),
                )
                self.assertEqual(planned.status_code, 200, planned.text)
                operation_id = planned.json()["record"]["operation_id"]
                store.write_merge_train_policy_record(
                    build_test_merge_train_policy_record(
                        repository="cbusillo/codex-skills",
                        record_id="merge-train-policy-candidate",
                    ).model_copy(update={"status": "superseded"})
                )

                approval = await client.post(
                    f"/v1/privileged-operations/plans/{operation_id}/approve",
                    json={
                        "source_event_id": "browser-merge-train-stale-approval",
                        "reason": "Approve the stale merge-train policy plan.",
                    },
                )

        self.assertEqual(approval.status_code, 409, approval.text)
        self.assertEqual(
            approval.json()["detail"]["code"],
            "privileged_operation_plan_stale",
        )

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
                review_response = await client.get(
                    f"/v1/privileged-operations/plans/{operation_id}/review"
                )
                detail_response = await client.get(
                    f"/v1/privileged-operations/plans/{operation_id}"
                )
                summary_response = await client.get(
                    f"/v1/agent/privileged-operations/plans/{operation_id}"
                )

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.json()["total"], 1)
        self.assertIn("reviews", list_response.json())
        self.assertNotIn("records", list_response.json())
        self.assertEqual(review_response.status_code, 200)
        self.assertEqual(review_response.json()["review"]["operation_id"], operation_id)
        self.assertFalse(review_response.json()["review"]["authorizes_execution"])
        self.assertNotIn("active_key_id", list_response.text)
        self.assertNotIn("active_key_id", review_response.text)
        self.assertIn("active_key_id", detail_response.text)
        self.assertEqual(summary_response.status_code, 200)
        summary_payload = summary_response.text
        self.assertNotIn("active_key_id", summary_payload)
        self.assertNotIn("retirement_blocked_key_ids", summary_payload)
        self.assertNotIn("request", summary_payload)
        self.assertIn("configured_secret_count", summary_payload)

    async def test_human_list_reads_historical_authz_request_digest(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            policy = _policy()
            store.seed_authz_policy_if_absent(_policy_record(policy))
            app = self._app(
                store=store,
                policy=policy,
                policy_record_reader=lambda: store.list_authz_policy_records(
                    status="active", limit=2
                )[0],
            )
            try:
                async with lifespan_client(app) as client:
                    create_response = await client.post(
                        "/v1/privileged-operations/plans",
                        json=_managed_authz_plan_payload("historical-authz-plan"),
                    )
                    self.assertEqual(create_response.status_code, 200, create_response.text)
                    operation_id = create_response.json()["record"]["operation_id"]
                    with store._session_factory() as session:  # noqa: SLF001
                        row = session.get(LaunchplanePrivilegedOperationRow, operation_id)
                        assert row is not None
                        payload = dict(row.payload)
                        payload["request_digest"] = canonical_json_sha256(payload["request"])
                        evidence = ManagedAuthzPolicySetHumanEvidence.model_validate(
                            payload["evidence"]
                        ).model_dump(mode="json")
                        evidence_diff = evidence["diff"]
                        assert isinstance(evidence_diff, dict)
                        for field_name in (
                            "previous_administrator_quorum",
                            "administrator_quorum",
                            "administrator_quorum_changed",
                            "strict_human_administrator_count",
                            "quorum_satisfied",
                            "solo_administration_active",
                        ):
                            evidence_diff.pop(field_name)
                        payload["evidence_digest"] = canonical_json_sha256(evidence)
                        stored_evidence = json.loads(json.dumps(payload["evidence"]))
                        stored_diff = stored_evidence["diff"]
                        assert isinstance(stored_diff, dict)
                        for field_name in (
                            "previous_administrator_quorum",
                            "administrator_quorum",
                            "administrator_quorum_changed",
                            "strict_human_administrator_count",
                            "quorum_satisfied",
                            "solo_administration_active",
                        ):
                            stored_diff.pop(field_name)
                        payload["evidence"] = stored_evidence
                        row.payload = payload
                        session.commit()

                    list_response = await client.get(
                        "/v1/privileged-operations/plans",
                        params={
                            "descriptor_id": "managed-authz-policy-set",
                            "limit": 50,
                        },
                    )
            finally:
                store.close()

        self.assertEqual(list_response.status_code, 200, list_response.text)
        self.assertEqual(list_response.json()["total"], 1)

    async def test_human_projection_reads_do_not_reconcile_expiry_or_write_events(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_MASTER_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"x" * 32).decode()},
                clear=False,
            ),
        ):
            store = FilesystemRecordStore(Path(temporary_directory))
            app = self._app(store=store, policy=_policy())
            async with lifespan_client(app) as client:
                create_response = await client.post(
                    "/v1/privileged-operations/plans",
                    json={
                        "descriptor_id": "managed-secret-reencryption",
                        "source_event_id": "expired-browser-request-1",
                        "expires_in_seconds": 300,
                        "request": {"reason": "Inspect expired plan without mutation"},
                    },
                )
                self.assertEqual(create_response.status_code, 200, create_response.text)
                operation_id = create_response.json()["record"]["operation_id"]
                record_path = (
                    Path(temporary_directory)
                    / "launchplane_privileged_operations"
                    / f"{operation_id}.json"
                )
                payload = json.loads(record_path.read_text(encoding="utf-8"))
                payload["created_at"] = "2026-01-01T00:00:00+00:00"
                payload["updated_at"] = "2026-01-01T00:00:00+00:00"
                payload["expires_at"] = "2026-01-01T00:05:00+00:00"
                record_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
                before_events = store.list_privileged_operation_event_records(
                    operation_id=operation_id
                )

                list_response = await client.get("/v1/privileged-operations/plans")
                review_response = await client.get(
                    f"/v1/privileged-operations/plans/{operation_id}/review"
                )
                after_events = store.list_privileged_operation_event_records(
                    operation_id=operation_id
                )

                detail_response = await client.get(
                    f"/v1/privileged-operations/plans/{operation_id}"
                )
                after_detail_events = store.list_privileged_operation_event_records(
                    operation_id=operation_id
                )

        self.assertEqual(list_response.status_code, 200, list_response.text)
        self.assertEqual(review_response.status_code, 200, review_response.text)
        self.assertEqual(detail_response.status_code, 200, detail_response.text)
        self.assertEqual(
            tuple(event.event_id for event in after_events),
            tuple(event.event_id for event in before_events),
        )
        self.assertNotIn("expired", {event.action for event in after_events})
        self.assertEqual(
            list_response.json()["reviews"][0]["lifecycle"]["expiry_state"],
            "past_expiry_unreconciled",
        )
        self.assertFalse(list_response.json()["reviews"][0]["can_approve"])
        self.assertFalse(review_response.json()["review"]["persists_state"])
        self.assertIn("expired", {event.action for event in after_detail_events})

    async def test_postgres_projection_reads_leave_operation_and_event_rows_unchanged(
        self,
    ) -> None:
        with (
            TemporaryDirectory() as temporary_directory,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_MASTER_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"x" * 32).decode()},
                clear=False,
            ),
        ):
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory) / "projection.sqlite3")
            )
            store.ensure_schema()
            app = self._app(store=store, policy=_policy())
            try:
                async with lifespan_client(app) as client:
                    create_response = await client.post(
                        "/v1/privileged-operations/plans",
                        json={
                            "descriptor_id": "managed-secret-reencryption",
                            "source_event_id": "postgres-expired-projection",
                            "expires_in_seconds": 300,
                            "request": {"reason": "Verify projection reads are non-mutating"},
                        },
                    )
                    self.assertEqual(create_response.status_code, 200, create_response.text)
                    operation_id = create_response.json()["record"]["operation_id"]
                    with store._session_factory() as session:  # noqa: SLF001
                        row = session.get(LaunchplanePrivilegedOperationRow, operation_id)
                        assert row is not None
                        payload = dict(row.payload)
                        payload["created_at"] = "2026-01-01T00:00:00+00:00"
                        payload["updated_at"] = "2026-01-01T00:00:00+00:00"
                        payload["expires_at"] = "2026-01-01T00:05:00+00:00"
                        row.payload = payload
                        session.commit()
                    before_record = store.read_privileged_operation_record(operation_id)
                    before_events = store.list_privileged_operation_event_records(
                        operation_id=operation_id
                    )

                    list_response = await client.get("/v1/privileged-operations/plans")
                    review_response = await client.get(
                        f"/v1/privileged-operations/plans/{operation_id}/review"
                    )

                    after_record = store.read_privileged_operation_record(operation_id)
                    after_events = store.list_privileged_operation_event_records(
                        operation_id=operation_id
                    )
            finally:
                store.close()

        self.assertEqual(list_response.status_code, 200, list_response.text)
        self.assertEqual(review_response.status_code, 200, review_response.text)
        self.assertEqual(after_record, before_record)
        self.assertEqual(after_events, before_events)

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
