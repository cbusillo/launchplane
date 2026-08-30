from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError

from control_plane import authz_policy_activation
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneAuthzPolicy
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _RejectingVerifier,
    _browser_mutation_headers,
    _github_oauth_config,
)
from tests.support.auth import _identity, _StubVerifier
from tests.support.http import lifespan_client
from tests.support.stores import _sqlite_database_url


_DRY_RUN_ROUTE = "/v1/authz-policies/privileged-policy-operations/activation/dry-run"
_APPLY_ROUTE = "/v1/authz-policies/privileged-policy-operations/activation/apply"


def _human_identity(*, github_id: int = 123) -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login="example-owner",
        github_id=github_id,
        name="Example Owner",
        email="owner@example.test",
        organizations=frozenset({"example-org"}),
        teams=frozenset({"example-org/owners"}),
        role="admin",
    )


def _admin_rule(*, github_id: int, managed_rule_id: str) -> dict[str, object]:
    return {
        "managed_set_id": "test.policy-administrators",
        "managed_rule_id": managed_rule_id,
        "github_ids": [github_id],
        "roles": ["admin"],
        "products": ["launchplane"],
        "contexts": ["launchplane"],
        "actions": ["authz_policy_grant.write"],
    }


def _policy(*, independent_admin: bool = True) -> LaunchplaneAuthzPolicy:
    rules = [_admin_rule(github_id=123, managed_rule_id="applying-admin")]
    if independent_admin:
        rules.append(_admin_rule(github_id=456, managed_rule_id="independent-admin"))
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_humans": rules,
        }
    )


def _mutable_only_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_humans": [
                {
                    "managed_set_id": "test.policy-administrators",
                    "managed_rule_id": "mutable-admin",
                    "logins": ["example-owner"],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["authz_policy_grant.write"],
                }
            ],
        }
    )


def _record(policy: LaunchplaneAuthzPolicy) -> LaunchplaneAuthzPolicyRecord:
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
        revision=1,
        source="test:authz-policy-activation",
        updated_at="2026-08-30T00:00:00Z",
        policy_sha256=digest,
        policy=policy,
    )


class AuthzPolicyOperationActivationDomainTests(unittest.TestCase):
    def test_compiled_set_is_exactly_one_immutable_human_rule(self) -> None:
        request = authz_policy_activation.build_authz_policy_operation_activation_reconcile_request(
            github_id=123,
            mode="dry_run",
            reason="Activate the reviewed privileged-policy lifecycle.",
        )

        self.assertEqual(
            request.managed_set_id,
            authz_policy_activation.AUTHZ_POLICY_OPERATION_ACTIVATION_MANAGED_SET_ID,
        )
        self.assertEqual(request.desired_policy.github_actions, ())
        self.assertEqual(request.desired_policy.terminal_agents, ())
        self.assertEqual(request.desired_policy.local_operators, ())
        self.assertEqual(request.desired_policy.local_admins, ())
        self.assertEqual(len(request.desired_policy.github_humans), 1)
        rule = request.desired_policy.github_humans[0]
        self.assertEqual(rule.github_ids, (123,))
        self.assertEqual(rule.logins, ())
        self.assertEqual(rule.organizations, ())
        self.assertEqual(rule.teams, ())
        self.assertEqual(rule.roles, ("admin",))
        self.assertEqual(rule.products, ("launchplane",))
        self.assertEqual(rule.contexts, ("launchplane",))
        self.assertEqual(
            rule.actions,
            authz_policy_activation.AUTHZ_POLICY_OPERATION_ACTIVATION_ACTIONS,
        )

    def test_activation_state_rejects_noncompiled_managed_set(self) -> None:
        conflicting_policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "github_humans": [
                    {
                        "managed_set_id": (
                            authz_policy_activation.AUTHZ_POLICY_OPERATION_ACTIVATION_MANAGED_SET_ID
                        ),
                        "managed_rule_id": "wrong-rule",
                        "github_ids": [123],
                        "roles": ["admin"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["authz_policy_operation.read"],
                    }
                ],
            }
        )

        self.assertEqual(
            authz_policy_activation.authz_policy_operation_activation_state(conflicting_policy),
            "conflict",
        )

    def test_request_contract_rejects_caller_supplied_identity_and_policy(self) -> None:
        for field_name, value in (
            ("github_id", 999),
            ("login", "alternate-user"),
            ("managed_set_id", "alternate-set"),
            ("desired_policy", {}),
        ):
            with self.subTest(field_name=field_name), self.assertRaises(ValidationError):
                authz_policy_activation.AuthzPolicyOperationActivationDryRunRequest.model_validate(
                    {
                        "reason": "Activate the reviewed privileged-policy lifecycle.",
                        field_name: value,
                    }
                )


class AuthzPolicyOperationActivationHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_apply_readback_replay_and_self_retirement(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            policy = _policy()
            store.seed_authz_policy_if_absent(_record(policy))
            session_manager = HumanSessionManager(
                config=_github_oauth_config(),
                session_store=InMemoryHumanSessionStore(),
            )
            session = session_manager.issue(_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                record_store_factory=lambda: store,
                human_session_manager=session_manager,
                control_plane_root_path=root,
                state_dir=root / "state",
            )
            self.assertNotIn(_DRY_RUN_ROUTE, app.openapi()["paths"])
            self.assertNotIn(_APPLY_ROUTE, app.openapi()["paths"])
            try:
                async with lifespan_client(app) as client:
                    dry_run = await client.post(
                        _DRY_RUN_ROUTE,
                        headers={
                            **_browser_mutation_headers(session_manager, session),
                            "Content-Type": "application/json",
                        },
                        content=json.dumps(
                            {"reason": "Activate the reviewed privileged-policy lifecycle."}
                        ),
                    )
                    self.assertEqual(dry_run.status_code, 202, dry_run.text)
                    dry_run_payload = dry_run.json()
                    activation = dry_run_payload["result"]["activation"]
                    self.assertTrue(activation["changed"])
                    self.assertTrue(activation["applying_admin_retained"])
                    self.assertTrue(activation["independent_admin_reachable"])
                    self.assertEqual(activation["read_back"]["activation_state"], "available")
                    review_digest = activation["review_digest"]
                    apply_payload = {
                        "reason": "Activate the reviewed privileged-policy lifecycle.",
                        "reviewed_plan_sha256": review_digest,
                    }
                    stale_review = await client.post(
                        _APPLY_ROUTE,
                        headers={
                            **_browser_mutation_headers(session_manager, session),
                            "Content-Type": "application/json",
                            "Idempotency-Key": "issue-2277-stale-review",
                        },
                        content=json.dumps(
                            {
                                **apply_payload,
                                "reviewed_plan_sha256": "0" * 64,
                            }
                        ),
                    )
                    self.assertEqual(stale_review.status_code, 409, stale_review.text)
                    self.assertEqual(stale_review.json()["error"]["code"], "authz_policy_conflict")
                    apply_headers = {
                        **_browser_mutation_headers(session_manager, session),
                        "Content-Type": "application/json",
                        "Idempotency-Key": "issue-2277-activation",
                    }
                    applied = await client.post(
                        _APPLY_ROUTE,
                        headers=apply_headers,
                        content=json.dumps(apply_payload),
                    )
                    self.assertEqual(applied.status_code, 202, applied.text)
                    applied_payload = applied.json()
                    self.assertEqual(
                        applied_payload["result"]["activation"]["bridge_state"], "retired"
                    )
                    self.assertEqual(
                        applied_payload["result"]["activation"]["read_back"]["activation_state"],
                        "active",
                    )
                    replayed = await client.post(
                        _APPLY_ROUTE,
                        headers={
                            **_browser_mutation_headers(session_manager, session),
                            "Content-Type": "application/json",
                            "Idempotency-Key": "issue-2277-activation",
                        },
                        content=json.dumps(apply_payload),
                    )
                    self.assertEqual(replayed.status_code, 202, replayed.text)
                    self.assertTrue(replayed.json()["replayed"])
                    conflicting_replay = await client.post(
                        _APPLY_ROUTE,
                        headers={
                            **_browser_mutation_headers(session_manager, session),
                            "Content-Type": "application/json",
                            "Idempotency-Key": "issue-2277-activation",
                        },
                        content=json.dumps(
                            {
                                **apply_payload,
                                "reason": "A different activation request.",
                            }
                        ),
                    )
                    self.assertEqual(
                        conflicting_replay.status_code,
                        409,
                        conflicting_replay.text,
                    )
                    self.assertEqual(
                        conflicting_replay.json()["error"]["code"],
                        "idempotency_key_reused",
                    )
                    retired_apply = await client.post(
                        _APPLY_ROUTE,
                        headers={
                            **_browser_mutation_headers(session_manager, session),
                            "Content-Type": "application/json",
                            "Idempotency-Key": "issue-2277-second-activation",
                        },
                        content=json.dumps(apply_payload),
                    )
                    self.assertEqual(retired_apply.status_code, 410, retired_apply.text)
                    self.assertEqual(
                        retired_apply.json()["error"]["code"],
                        "authz_policy_operation_activation_retired",
                    )
                    retired = await client.post(
                        _DRY_RUN_ROUTE,
                        headers={
                            **_browser_mutation_headers(session_manager, session),
                            "Content-Type": "application/json",
                        },
                        content=json.dumps({"reason": "Attempt a second activation dry-run."}),
                    )
                active_records = store.list_authz_policy_records(status="active", limit=2)
            finally:
                store.close()

        self.assertEqual(retired.status_code, 410, retired.text)
        self.assertEqual(
            retired.json()["error"]["code"], "authz_policy_operation_activation_retired"
        )
        self.assertEqual(len(active_records), 1)
        active_record = active_records[0]
        self.assertEqual(
            active_record.source,
            authz_policy_activation.AUTHZ_POLICY_OPERATION_ACTIVATION_SOURCE,
        )
        self.assertEqual(
            authz_policy_activation.authz_policy_operation_activation_state(active_record.policy),
            "active",
        )
        self.assertEqual(
            authz_policy_activation.authz_policy_operation_activation_github_id(
                active_record.policy
            ),
            123,
        )
        self.assertEqual(len(active_record.policy.github_humans), 3)
        self.assertEqual(
            {
                rule.managed_rule_id
                for rule in active_record.policy.github_humans
                if rule.managed_set_id == "test.policy-administrators"
            },
            {"applying-admin", "independent-admin"},
        )

    async def test_browser_mutation_requires_and_consumes_single_use_csrf(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            policy = _policy()
            store.seed_authz_policy_if_absent(_record(policy))
            session_manager = HumanSessionManager(
                config=_github_oauth_config(),
                session_store=InMemoryHumanSessionStore(),
            )
            session = session_manager.issue(_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                record_store_factory=lambda: store,
                human_session_manager=session_manager,
                control_plane_root_path=root,
                state_dir=root / "state",
            )
            try:
                cookie_header = session_manager.session_cookie_header(session)
                mutation_headers = {
                    **_browser_mutation_headers(session_manager, session),
                    "Content-Type": "application/json",
                }
                async with lifespan_client(app) as client:
                    missing_proof = await client.post(
                        _DRY_RUN_ROUTE,
                        headers={
                            "Cookie": cookie_header,
                            "Content-Type": "application/json",
                        },
                        content=json.dumps({"reason": "Missing browser proof."}),
                    )
                    accepted = await client.post(
                        _DRY_RUN_ROUTE,
                        headers=mutation_headers,
                        content=json.dumps({"reason": "Consume browser proof."}),
                    )
                    replayed_csrf = await client.post(
                        _DRY_RUN_ROUTE,
                        headers=mutation_headers,
                        content=json.dumps({"reason": "Replay browser proof."}),
                    )
            finally:
                store.close()

        self.assertEqual(missing_proof.status_code, 403, missing_proof.text)
        self.assertEqual(missing_proof.json()["error"]["code"], "browser_mutation_denied")
        self.assertEqual(missing_proof.headers["cache-control"], "no-store")
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(accepted.headers["cache-control"], "no-store")
        self.assertEqual(replayed_csrf.status_code, 403, replayed_csrf.text)
        self.assertEqual(replayed_csrf.json()["error"]["code"], "browser_mutation_denied")
        self.assertEqual(replayed_csrf.headers["cache-control"], "no-store")

    async def test_route_rejects_nonhuman_transport_and_caller_supplied_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            policy = _policy()
            store.seed_authz_policy_if_absent(_record(policy))
            session_manager = HumanSessionManager(
                config=_github_oauth_config(),
                session_store=InMemoryHumanSessionStore(),
            )
            session = session_manager.issue(_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                record_store_factory=lambda: store,
                human_session_manager=session_manager,
                control_plane_root_path=root,
                state_dir=root / "state",
            )
            try:
                async with lifespan_client(app) as client:
                    bearer_response = await client.post(
                        _DRY_RUN_ROUTE,
                        headers={
                            "Authorization": "Bearer valid-token",
                            "Content-Type": "application/json",
                        },
                        content=json.dumps({"reason": "forbidden"}),
                    )
                    identity_response = await client.post(
                        _DRY_RUN_ROUTE,
                        headers={
                            **_browser_mutation_headers(session_manager, session),
                            "Content-Type": "application/json",
                        },
                        content=json.dumps(
                            {
                                "reason": "forbidden",
                                "github_id": 999,
                            }
                        ),
                    )
            finally:
                store.close()

        self.assertEqual(bearer_response.status_code, 403, bearer_response.text)
        self.assertEqual(bearer_response.json()["error"]["code"], "authorization_denied")
        self.assertEqual(bearer_response.headers["cache-control"], "no-store")
        self.assertEqual(identity_response.status_code, 400, identity_response.text)
        self.assertEqual(identity_response.json()["error"]["code"], "invalid_request")
        self.assertEqual(identity_response.headers["cache-control"], "no-store")

    async def test_route_rejects_mutable_only_policy_administrator(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            policy = _mutable_only_policy()
            store.seed_authz_policy_if_absent(_record(policy))
            session_manager = HumanSessionManager(
                config=_github_oauth_config(),
                session_store=InMemoryHumanSessionStore(),
            )
            session = session_manager.issue(_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                record_store_factory=lambda: store,
                human_session_manager=session_manager,
                control_plane_root_path=root,
                state_dir=root / "state",
            )
            try:
                async with lifespan_client(app) as client:
                    response = await client.post(
                        _DRY_RUN_ROUTE,
                        headers={
                            **_browser_mutation_headers(session_manager, session),
                            "Content-Type": "application/json",
                        },
                        content=json.dumps({"reason": "forbidden"}),
                    )
            finally:
                store.close()

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_apply_requires_distinct_reachable_policy_administrator(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            policy = _policy(independent_admin=False)
            store.seed_authz_policy_if_absent(_record(policy))
            session_manager = HumanSessionManager(
                config=_github_oauth_config(),
                session_store=InMemoryHumanSessionStore(),
            )
            session = session_manager.issue(_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                record_store_factory=lambda: store,
                human_session_manager=session_manager,
                control_plane_root_path=root,
                state_dir=root / "state",
            )
            try:
                async with lifespan_client(app) as client:
                    dry_run = await client.post(
                        _DRY_RUN_ROUTE,
                        headers={
                            **_browser_mutation_headers(session_manager, session),
                            "Content-Type": "application/json",
                        },
                        content=json.dumps({"reason": "Review continuity."}),
                    )
                    self.assertEqual(dry_run.status_code, 202, dry_run.text)
                    activation = dry_run.json()["result"]["activation"]
                    self.assertFalse(activation["independent_admin_reachable"])
                    apply_response = await client.post(
                        _APPLY_ROUTE,
                        headers={
                            **_browser_mutation_headers(session_manager, session),
                            "Content-Type": "application/json",
                            "Idempotency-Key": "missing-independent-admin",
                        },
                        content=json.dumps(
                            {
                                "reason": "Review continuity.",
                                "reviewed_plan_sha256": activation["review_digest"],
                            }
                        ),
                    )
                active_records = store.list_authz_policy_records(status="active", limit=2)
            finally:
                store.close()

        self.assertEqual(apply_response.status_code, 409, apply_response.text)
        self.assertEqual(
            apply_response.json()["error"]["code"],
            "authz_policy_independent_admin_unreachable",
        )
        self.assertEqual(len(active_records), 1)
        self.assertEqual(
            authz_policy_activation.authz_policy_operation_activation_state(
                active_records[0].policy
            ),
            "available",
        )
