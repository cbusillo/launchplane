from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane import authz_policy_recovery
from control_plane import authz_policy_activation
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneAuthzPolicy
from control_plane.service_human_auth import (
    HumanSessionManager,
    InMemoryHumanSessionStore,
    LaunchplaneHumanSession,
)
from control_plane.storage.postgres import PostgresRecordStore
from httpx2 import AsyncClient
from tests.http_app_test_support import (
    _RejectingVerifier,
    _browser_mutation_headers,
    _github_oauth_config,
)
from tests.support.http import lifespan_client
from tests.support.stores import _sqlite_database_url


_RECOVERY_PREFIX = "/v1/authz-policies/privileged-policy-operations/recovery"
_DRY_RUN_ROUTE = f"{_RECOVERY_PREFIX}/candidates/dry-run"
_APPLY_ROUTE = f"{_RECOVERY_PREFIX}/candidates/apply"
_CONFIRM_ROUTE = f"{_RECOVERY_PREFIX}/confirmations"
_DIAGNOSTIC_ROUTE = f"{_RECOVERY_PREFIX}/diagnostic"
_LEGACY_ACTIVATION_APPLY_ROUTE = "/v1/authz-policies/privileged-policy-operations/activation/apply"


def _identity() -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login="example-owner",
        github_id=123,
        name="Example Owner",
        email="owner@example.test",
        organizations=frozenset(),
        teams=frozenset(),
        role="admin",
    )


def _policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "administrator_quorum": 1,
            "github_humans": [
                {
                    "managed_set_id": "test.policy-administrators",
                    "managed_rule_id": "owner-policy-administrator",
                    "github_ids": [123],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["authz_policy_grant.write"],
                },
                {
                    "managed_set_id": "operator.privileged-policy-operation",
                    "managed_rule_id": "github-human-policy-operator",
                    "github_ids": [123],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": [
                        "authz_policy_operation.approve",
                        "authz_policy_operation.cancel",
                        "authz_policy_operation.propose",
                        "authz_policy_operation.read",
                        "authz_policy_operation.revoke",
                    ],
                },
                {
                    "managed_set_id": "operator.privileged-operation-bootstrap",
                    "managed_rule_id": "github-owner-policy-operation-review",
                    "github_ids": [123],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": [
                        "authz_policy_operation.approve",
                        "authz_policy_operation.cancel",
                        "authz_policy_operation.read",
                        "authz_policy_operation.revoke",
                    ],
                },
            ],
            "terminal_agents": [
                {
                    "managed_set_id": "operator.privileged-operation-bootstrap",
                    "managed_rule_id": "terminal-agent-policy-operation-propose",
                    "subjects": ["terminal-agent:test"],
                    "token_labels": ["test-token"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["authz_policy_operation.propose"],
                }
            ],
        }
    )


def _record(policy: LaunchplaneAuthzPolicy) -> LaunchplaneAuthzPolicyRecord:
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=369, policy_sha256=digest),
        revision=369,
        source="test:revision-369",
        updated_at="2026-09-01T00:00:00Z",
        policy_sha256=digest,
        policy=policy,
    )


class AuthzPolicyRecoveryHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfirmed_activation_reset_fresh_confirmed_activation_and_bootstrap_retirement(
        self,
    ) -> None:
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
            session = session_manager.issue(_identity())
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
                    await self._apply_candidate(
                        client=client,
                        session_manager=session_manager,
                        session=session,
                        candidate_id="reset-unconfirmed-privileged-policy-operation-activation",
                        idempotency_key="recovery-reset",
                    )
                    legacy_apply = await client.post(
                        _LEGACY_ACTIVATION_APPLY_ROUTE,
                        headers={
                            **self._mutation_headers(session_manager, session),
                            "Idempotency-Key": "legacy-after-recovery-reset",
                        },
                        content=json.dumps(
                            {
                                "reason": "Attempt the retired quorum-one activation path.",
                                "reviewed_plan_sha256": "0" * 64,
                            }
                        ),
                    )
                    self.assertEqual(legacy_apply.status_code, 409, legacy_apply.text)
                    self.assertEqual(
                        legacy_apply.json()["error"]["code"],
                        "authz_policy_operation_activation_requires_recovery_confirmation",
                    )
                    self.assertEqual(
                        authz_policy_activation.authz_policy_operation_activation_state(
                            store.list_authz_policy_records(status="active", limit=2)[0].policy
                        ),
                        "available",
                    )
                    await self._apply_candidate(
                        client=client,
                        session_manager=session_manager,
                        session=session,
                        candidate_id="activate-privileged-policy-operation",
                        idempotency_key="recovery-fresh-activation",
                    )
                    diagnostic = await client.get(
                        _DIAGNOSTIC_ROUTE,
                        headers={
                            **self._mutation_headers(session_manager, session),
                            "Cookie": session_manager.session_cookie_header(
                                self._current_session(session_manager, session)
                            ),
                        },
                    )
                    self.assertEqual(diagnostic.status_code, 200, diagnostic.text)
                    self.assertTrue(
                        diagnostic.json()["result"]["active_policy"][
                            "consumed_confirmation_backing"
                        ]
                    )
                    await self._apply_candidate(
                        client=client,
                        session_manager=session_manager,
                        session=session,
                        candidate_id="retire-privileged-operation-bootstrap",
                        idempotency_key="recovery-retire-bootstrap",
                    )
                active_record = store.list_authz_policy_records(status="active", limit=2)[0]
            finally:
                store.close()

        cardinality = authz_policy_recovery.recovery_action_match_cardinality(
            policy=active_record.policy,
            github_id=123,
        )
        self.assertTrue(
            all(value == 1 for value in cardinality["activation"].values()),
            cardinality,
        )
        self.assertTrue(
            all(value == 0 for value in cardinality["bootstrap"].values()),
            cardinality,
        )

    async def _apply_candidate(
        self,
        *,
        client: AsyncClient,
        session_manager: HumanSessionManager,
        session: LaunchplaneHumanSession,
        candidate_id: authz_policy_recovery.AuthzPolicyRecoveryCandidateId,
        idempotency_key: str,
    ) -> None:
        reason = f"Recover {candidate_id}."
        dry_run = await client.post(
            _DRY_RUN_ROUTE,
            headers=self._mutation_headers(session_manager, session),
            content=json.dumps({"candidate_id": candidate_id, "reason": reason}),
        )
        self.assertEqual(dry_run.status_code, 202, dry_run.text)
        reviewed_plan_sha256 = dry_run.json()["result"]["candidate"]["review_digest"]
        confirmation = await client.post(
            _CONFIRM_ROUTE,
            headers={
                **self._mutation_headers(session_manager, session),
                "Idempotency-Key": idempotency_key,
            },
            content=json.dumps(
                {
                    "candidate_id": candidate_id,
                    "reason": reason,
                    "reviewed_plan_sha256": reviewed_plan_sha256,
                    "acknowledgement": (
                        authz_policy_recovery.AUTHZ_POLICY_RECOVERY_CONFIRMATION_ACKNOWLEDGEMENT
                    ),
                }
            ),
        )
        self.assertEqual(confirmation.status_code, 200, confirmation.text)
        confirmation_payload = confirmation.json()
        applied = await client.post(
            _APPLY_ROUTE,
            headers={
                **self._mutation_headers(session_manager, session),
                "Idempotency-Key": idempotency_key,
                "X-Solo-Administration-Confirmation-Secret": confirmation_payload["secret"],
            },
            content=json.dumps(
                {
                    "candidate_id": candidate_id,
                    "reason": reason,
                    "reviewed_plan_sha256": reviewed_plan_sha256,
                    "solo_administration_confirmation_id": confirmation_payload["confirmation_id"],
                }
            ),
        )
        self.assertEqual(applied.status_code, 202, applied.text)

    def _current_session(
        self,
        manager: HumanSessionManager,
        original_session: LaunchplaneHumanSession,
    ) -> LaunchplaneHumanSession:
        current_session = manager.read_cookie_without_renewal(
            manager.session_cookie_header(original_session)
        )
        self.assertIsNotNone(current_session)
        assert current_session is not None
        return current_session

    def _mutation_headers(
        self,
        manager: HumanSessionManager,
        original_session: LaunchplaneHumanSession,
    ) -> dict[str, str]:
        return {
            **_browser_mutation_headers(manager, self._current_session(manager, original_session)),
            "Content-Type": "application/json",
        }
