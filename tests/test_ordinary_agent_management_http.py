from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.ordinary_agent_lifecycle import OrdinaryAgentEnrollmentIntent
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.ordinary_agent_enrollment_worker import (
    OrdinaryAgentEnrollmentRecoveryState,
    recover_ordinary_agent_enrollments_once,
)
from control_plane.ordinary_agent_session_approval import (
    approve_ordinary_agent_enrollment,
    disconnect_ordinary_agent_principal,
)
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.service_auth import (
    BearerIdentityConfig,
    GitHubHumanIdentity,
    TerminalAgentIdentity,
)
from control_plane.service_human_auth import GitHubOAuthConfig, HumanSessionManager
from control_plane.storage.postgres import PostgresRecordStore
from tests import test_ordinary_agent_effect_storage as effect_support
from tests.support.http import lifespan_client
from tests.support.ordinary_agent_lifecycle import (
    ADMIN_GITHUB_ID,
    enrollment_envelope,
    prepare_approved_test_issuance,
    setup_ordinary_agent_authority,
    replace_policy_without_ordinary_agent_rule,
)


class OrdinaryAgentManagementHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_finite_job_post_accepts_strict_client_intent_only(self) -> None:
        fixture = effect_support.OrdinaryAgentEffectStorageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        session = fixture.fixture
        app = create_launchplane_fastapi_app(
            verifier=Mock(),
            authz_policy=session.policy.policy,
            record_store_factory=lambda: fixture.store,
        )
        headers = {"Authorization": f"Bearer {session.bundle.token.value}"}
        qualification = {
            "schema_version": 2,
            "purpose": "qualification",
            "idempotency_key": "http-finite-client-one",
            "session_id": fixture.request.session_id,
            "lease_id": fixture.request.lease_id,
        }
        with patch.object(
            PostgresRecordStore,
            "admit_ordinary_agent_client_request",
            create=True,
            return_value=fixture.request,
        ) as admit:
            async with lifespan_client(app) as client:
                response = await client.post(
                    "/v1/agent/ordinary-agent-jobs", headers=headers, json=qualification
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["request_id"], fixture.request.request_id)
                admit.assert_called_once()
                self.assertEqual(admit.call_args.kwargs["request"].purpose, "qualification")
                malformed = await client.post(
                    "/v1/agent/ordinary-agent-jobs",
                    headers=headers,
                    json={
                        **qualification,
                        "target": fixture.request.target.model_dump(mode="json"),
                    },
                )
                self.assertEqual(malformed.status_code, 400, malformed.text)

    async def test_finite_job_post_requires_postgres_record_store(self) -> None:
        fixture = effect_support.OrdinaryAgentEffectStorageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        session = fixture.fixture
        app = create_launchplane_fastapi_app(
            verifier=Mock(),
            authz_policy=session.policy.policy,
            record_store_factory=lambda: object(),
        )
        async with lifespan_client(app) as client:
            response = await client.post(
                "/v1/agent/ordinary-agent-jobs",
                headers={"Authorization": f"Bearer {session.bundle.token.value}"},
                json={
                    "schema_version": 2,
                    "purpose": "qualification",
                    "idempotency_key": "http-finite-client-one",
                    "session_id": "session-one",
                    "lease_id": "lease-one",
                },
            )
            self.assertEqual(response.status_code, 503, response.text)

    async def test_finite_job_post_reports_expired_lease_as_client_state_denial(self) -> None:
        fixture = effect_support.OrdinaryAgentEffectStorageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        session = fixture.fixture
        session.clock.return_value = datetime.fromtimestamp(
            session.now + 100, timezone.utc
        ).isoformat()
        app = create_launchplane_fastapi_app(
            verifier=Mock(),
            authz_policy=session.policy.policy,
            record_store_factory=lambda: fixture.store,
        )
        async with lifespan_client(app) as client:
            response = await client.post(
                "/v1/agent/ordinary-agent-jobs",
                headers={"Authorization": f"Bearer {session.bundle.token.value}"},
                json={
                    "schema_version": 2,
                    "purpose": "guarded_delivery",
                    "idempotency_key": "http-expired-lease",
                    "session_id": fixture.request.session_id,
                    "lease_id": fixture.request.lease_id,
                    "base_sha": "a" * 40,
                    "pull_requests": [{"number": 12, "head_sha": "b" * 40}],
                    "permitted_stack_edit_pull_requests": [],
                    "refresh_allowance": 0,
                },
            )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "http_error")

    async def test_guarded_provider_readiness_gap_is_reported_as_service_unavailable(self) -> None:
        fixture = effect_support.OrdinaryAgentEffectStorageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        session = fixture.fixture
        app = create_launchplane_fastapi_app(
            verifier=Mock(),
            authz_policy=session.policy.policy,
            record_store_factory=lambda: fixture.store,
        )
        with patch.object(
            PostgresRecordStore,
            "admit_ordinary_agent_client_request",
            side_effect=OrdinaryAgentSessionAdmissionDenied("provider_readiness_unavailable"),
        ):
            async with lifespan_client(app) as client:
                response = await client.post(
                    "/v1/agent/ordinary-agent-jobs",
                    headers={"Authorization": f"Bearer {session.bundle.token.value}"},
                    json={
                        "schema_version": 2,
                        "purpose": "guarded_delivery",
                        "idempotency_key": "http-guarded-provider-readiness-gap",
                        "session_id": fixture.request.session_id,
                        "lease_id": fixture.request.lease_id,
                        "base_sha": "a" * 40,
                        "pull_requests": [{"number": 12, "head_sha": "b" * 40}],
                        "permitted_stack_edit_pull_requests": [],
                        "refresh_allowance": 0,
                    },
                )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(
            response.json()["error"]["message"],
            "Guarded delivery is unavailable until Launchplane can verify repository protection.",
        )

    async def test_job_reads_use_current_ordinary_or_signed_administrator_identity(self) -> None:
        fixture = effect_support.OrdinaryAgentEffectStorageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        session = fixture.fixture
        app = create_launchplane_fastapi_app(
            verifier=Mock(),
            authz_policy=session.policy.policy,
            record_store_factory=lambda: fixture.store,
            human_session_manager=session.manager,
        )
        agent_headers = {"Authorization": f"Bearer {session.bundle.token.value}"}
        browser_headers = {"Cookie": session.manager.session_cookie_header(session.human)}
        agent_path = f"/v1/agent/ordinary-agent-jobs/{fixture.request.request_id}"
        human_path = (
            f"/v1/ordinary-agent-jobs/{fixture.request.principal_id}/{fixture.request.request_id}"
        )
        async with lifespan_client(app) as client:
            own = await client.get(agent_path, headers=agent_headers)
            self.assertEqual(own.status_code, 200, own.text)
            self.assertEqual(own.json()["request_id"], fixture.request.request_id)
            self.assertEqual(own.json()["principal_id"], fixture.request.principal_id)
            administrator = await client.get(human_path, headers=browser_headers)
            self.assertEqual(administrator.status_code, 200, administrator.text)
            self.assertEqual(administrator.json(), own.json())
            missing = await client.get(
                "/v1/agent/ordinary-agent-jobs/unknown-request", headers=agent_headers
            )
            foreign = await client.get(
                f"/v1/ordinary-agent-jobs/another-principal/{fixture.request.request_id}",
                headers=browser_headers,
            )
            bearer_on_human = await client.get(human_path, headers=agent_headers)
            self.assertEqual(missing.status_code, 403)
            self.assertEqual(foreign.status_code, 403)
            self.assertEqual(bearer_on_human.status_code, 403)
            self.assertNotIn(session.bundle.token.value, own.text + missing.text + foreign.text)
            disconnect_ordinary_agent_principal(
                store=fixture.store,
                manager=session.manager,
                cookie_header=session.manager.session_cookie_header(session.human),
                csrf_token=session.manager.csrf_token(session.human),
                principal_id=fixture.request.principal_id,
                source_event_id="job-read-revoke",
            )
            withdrawn = await client.get(agent_path, headers=agent_headers)
            self.assertEqual(withdrawn.status_code, 403)

    async def test_ordinary_proposal_and_signed_browser_approval_are_separate_authorities(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'db.sqlite3'}"
            )
            self.addCleanup(store.close)
            store.ensure_schema()
            policy, inventory = setup_ordinary_agent_authority(store)
            manager = HumanSessionManager(
                config=GitHubOAuthConfig(
                    client_id="test",
                    client_secret="test",
                    public_url="https://example.test",
                    session_secret="test-session-secret",
                ),
                session_store=store,
            )
            human = manager.issue(
                GitHubHumanIdentity(
                    login="test-admin",
                    github_id=ADMIN_GITHUB_ID,
                    name="Test",
                    email="test@example.test",
                    organizations=frozenset(),
                    teams=frozenset(),
                    role="admin",
                )
            )
            initial = OrdinaryAgentEnrollmentIntent.from_envelope(
                enrollment_envelope(policy_record=policy, inventory=inventory)
            )
            store.propose_ordinary_agent_enrollment(
                intent=initial,
                requester=TerminalAgentIdentity(subject="test-cli", token_label="test"),
            )
            approved = approve_ordinary_agent_enrollment(
                store=store,
                manager=manager,
                cookie_header=manager.session_cookie_header(human),
                csrf_token=manager.csrf_token(human),
                principal_id=initial.principal_id,
                operation_id=initial.operation_id,
            )
            envelope, bundle = prepare_approved_test_issuance(approved)
            with patch(
                "control_plane.ordinary_agent_enrollment_worker.issue_ordinary_agent_credential",
                return_value=bundle,
            ):
                state = OrdinaryAgentEnrollmentRecoveryState()
                recovered = recover_ordinary_agent_enrollments_once(
                    record_store=store, state=state, lease_owner="http-worker", limit=20
                )
                self.assertEqual(recovered.applied, 1)
                self.assertEqual(recovered.failed, 0)
                replayed = recover_ordinary_agent_enrollments_once(
                    record_store=store, state=state, lease_owner="http-worker", limit=20
                )
                self.assertEqual(replayed.processed, 0)
            verifier = Mock()
            app = create_launchplane_fastapi_app(
                verifier=verifier,
                authz_policy=policy.policy,
                record_store_factory=lambda: store,
                human_session_manager=manager,
                bearer_identity_config=BearerIdentityConfig(
                    local_admin_token=bundle.token.value,
                    local_admin_subject="legacy-admin",
                    local_admin_token_label="legacy-admin",
                ),
            )
            now = int(datetime.now(timezone.utc).timestamp())
            browser_headers = {
                "Cookie": manager.session_cookie_header(human),
                "Origin": manager.public_origin,
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
                "X-CSRF-Token": manager.csrf_token(human),
            }
            async with lifespan_client(app) as client:
                proposed = await client.post(
                    "/v1/agent/ordinary-agent-session-proposals",
                    headers={"Authorization": f"Bearer {bundle.token.value}"},
                    json={
                        "operation_id": "http-session-one",
                        "attenuation": {
                            "actions": ["guarded_merge"],
                            "session_expires_at": now + 100,
                            "lease_expires_at": now + 90,
                            "action_limit": 4,
                            "pull_request_limit": 2,
                            "refresh_allowance": 1,
                        },
                    },
                )
                self.assertEqual(proposed.status_code, 200, proposed.text)
                operation = proposed.json()["operation"]
                self.assertEqual(operation["status"], "pending")
                self.assertNotIn(bundle.token.value, proposed.text)
                self.assertNotIn(bundle.candidate.credential_digest, proposed.text)
                path = f"/v1/ordinary-agent-operations/{operation['principal_id']}/{operation['operation_id']}"
                denied = await client.post(
                    path + "/approve", headers={"Authorization": f"Bearer {bundle.token.value}"}
                )
                self.assertEqual(denied.status_code, 403)
                invalid_csrf = await client.post(
                    path + "/approve", headers={**browser_headers, "X-CSRF-Token": "invalid"}
                )
                self.assertEqual(invalid_csrf.status_code, 403)
                reviewed = await client.get(
                    path, headers={"Cookie": manager.session_cookie_header(human)}
                )
                self.assertEqual(reviewed.status_code, 200, reviewed.text)
                self.assertTrue(reviewed.json()["operation"]["can_approve"])
                self.assertEqual(
                    reviewed.json()["operation"]["current_policy_execution_profile"],
                    "guarded_executor",
                )
                accepted = await client.post(path + "/approve", headers=browser_headers)
                self.assertEqual(accepted.status_code, 200, accepted.text)
                session_id = accepted.json()["operation"]["session_id"]
                self.assertTrue(session_id)
                replay = await client.post(path + "/approve", headers=browser_headers)
                self.assertEqual(replay.json()["operation"]["session_id"], session_id)
                revoked = await client.post(
                    f"/v1/ordinary-agent-sessions/agent_one/{session_id}/revoke",
                    headers=browser_headers,
                )
                self.assertEqual(revoked.status_code, 200, revoked.text)
                self.assertEqual(revoked.json()["operation"]["status"], "revoked")
                self.assertEqual(revoked.headers["cache-control"], "no-store")
                replace_policy_without_ordinary_agent_rule(store, current=policy)
                withdrawn = await client.get(
                    path, headers={"Cookie": manager.session_cookie_header(human)}
                )
                self.assertEqual(withdrawn.status_code, 200, withdrawn.text)
                self.assertEqual(withdrawn.json()["operation"]["current_policy_actions"], [])
                self.assertIsNone(withdrawn.json()["operation"]["current_policy_execution_profile"])
                verifier.verify.assert_not_called()

    async def test_terminal_connection_proposal_uses_its_capability_and_never_caller_approval(
        self,
    ) -> None:
        from sqlalchemy import delete
        from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
        from control_plane.contracts.ordinary_agent_client import (
            ORDINARY_AGENT_ENROLLMENT_PROPOSE_ACTION,
        )
        from control_plane.ordinary_agent_enrollment_preparation import (
            PreparedOrdinaryAgentEnrollmentScope,
        )
        from control_plane.service_auth import TerminalAgentPolicyRule
        from control_plane.storage.postgres import LaunchplaneAuthzPolicyRow

        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'db.sqlite3'}"
            )
            self.addCleanup(store.close)
            store.ensure_schema()
            original, inventory = setup_ordinary_agent_authority(store)
            policy = LaunchplaneAuthzPolicyRecord(
                record_id="http-terminal-policy",
                revision=2,
                source="test:http",
                updated_at=original.updated_at,
                policy=original.policy.model_copy(
                    update={
                        "ordinary_agents": (
                            *original.policy.ordinary_agents,
                            original.policy.ordinary_agents[0].model_copy(
                                update={
                                    "principal_id": "agent_two",
                                    "managed_rule_id": "agent_two.launchplane.main",
                                }
                            ),
                        ),
                        "terminal_agents": (
                            TerminalAgentPolicyRule(
                                managed_set_id="terminal-client",
                                managed_rule_id="connect",
                                subjects=("cli-client",),
                                token_labels=("client",),
                                products=("launchplane",),
                                contexts=("launchplane",),
                                actions=(ORDINARY_AGENT_ENROLLMENT_PROPOSE_ACTION,),
                            ),
                        ),
                    }
                ),
            )
            with store._session_factory() as session:
                session.execute(delete(LaunchplaneAuthzPolicyRow))
                session.commit()
            store._write_row(store._authz_policy_row(policy))
            fixture = enrollment_envelope(policy_record=policy, inventory=inventory)
            scope = PreparedOrdinaryAgentEnrollmentScope(
                policy=fixture.policy,
                custody=fixture.custody,
                principal=None,
                credential_id=None,
                credential_version=None,
            )
            second_binding = scope.policy.model_copy(
                update={"managed_rule_id": "agent_two.launchplane.main"}
            )
            second_scope = PreparedOrdinaryAgentEnrollmentScope(
                policy=second_binding,
                custody=scope.custody.model_copy(
                    update={"principal_id": "agent_two", "policy": second_binding}
                ),
                principal=None,
                credential_id=None,
                credential_version=None,
            )
            app = create_launchplane_fastapi_app(
                verifier=Mock(),
                authz_policy=policy.policy,
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(
                    terminal_agent_token="terminal-client-private",
                    terminal_agent_subject="cli-client",
                    terminal_agent_token_label="client",
                    local_admin_token="administrator-private",
                    local_admin_subject="admin",
                    local_admin_token_label="admin",
                ),
            )
            request = {
                "action": "enroll",
                "operation_id": "http-connect-one",
                "principal_id": "agent_one",
                "target": fixture.policy.target.model_dump(mode="json"),
                "github_app_id": 42,
                "secret_binding_id": fixture.custody.managed_secret.binding_id,
                "credential_valid_from": fixture.authentication_credential.valid_from,
                "credential_expires_at": fixture.authentication_credential.expires_at,
                "delivery": fixture.delivery.model_dump(mode="json"),
            }
            with patch(
                "control_plane.ordinary_agent_enrollment_preparation.prepare_ordinary_agent_enrollment_scope",
                side_effect=lambda **kwargs: (
                    scope if kwargs["principal_id"] == "agent_one" else second_scope
                ),
            ) as prepare:
                async with lifespan_client(app) as client:
                    path = "/v1/agent/ordinary-agent-enrollments"
                    denied = await client.post(
                        path,
                        headers={"Authorization": "Bearer administrator-private"},
                        json=request,
                    )
                    self.assertEqual(denied.status_code, 403)
                    prepare.assert_not_called()
                    invalid = await client.post(
                        path,
                        headers={"Authorization": "Bearer terminal-client-private"},
                        json={**request, "approval_sha256": "private-unreviewed-approval"},
                    )
                    self.assertEqual(invalid.status_code, 400)
                    self.assertNotIn("private-unreviewed-approval", invalid.text)
                    prepare.assert_not_called()
                    accepted = await client.post(
                        path,
                        headers={"Authorization": "Bearer terminal-client-private"},
                        json=request,
                    )
                    self.assertEqual(accepted.status_code, 200, accepted.text)
                    self.assertEqual(accepted.json()["operation"]["status"], "pending")
                    self.assertFalse(accepted.json()["operation"]["applied"])
                    self.assertIn("principal_id=agent_one", accepted.json()["review_url"])
                    self.assertNotIn(fixture.delivery.receiver_claim_sha256, accepted.text)
                    self.assertIsNone(
                        store.read_current_ordinary_agent_principal(principal_id="agent_one")
                    )
                    replay = await client.post(
                        path,
                        headers={"Authorization": "Bearer terminal-client-private"},
                        json=request,
                    )
                    self.assertEqual(replay.json(), accepted.json())
                    self.assertEqual(prepare.call_count, 1)
                    conflicting = await client.post(
                        path,
                        headers={"Authorization": "Bearer terminal-client-private"},
                        json={
                            **request,
                            "credential_expires_at": fixture.authentication_credential.expires_at
                            + 1,
                        },
                    )
                    self.assertEqual(conflicting.status_code, 403)
                    self.assertEqual(prepare.call_count, 1)
                    second = await client.post(
                        path,
                        headers={"Authorization": "Bearer terminal-client-private"},
                        json={**request, "principal_id": "agent_two"},
                    )
                    self.assertEqual(second.status_code, 200, second.text)
                    first_operation = accepted.json()["operation"]["operation_id"]
                    second_operation = second.json()["operation"]["operation_id"]
                    self.assertNotEqual(first_operation, second_operation)
                    manager = HumanSessionManager(
                        config=GitHubOAuthConfig(
                            client_id="test",
                            client_secret="test",
                            public_url="https://example.test",
                            session_secret="test-session-secret",
                        ),
                        session_store=store,
                    )
                    human = manager.issue(
                        GitHubHumanIdentity(
                            login="test-admin",
                            github_id=ADMIN_GITHUB_ID,
                            name="Test",
                            email="test@example.test",
                            organizations=frozenset(),
                            teams=frozenset(),
                            role="admin",
                        )
                    )
                    bundles = {}
                    for principal_id, operation_id in (
                        ("agent_one", first_operation),
                        ("agent_two", second_operation),
                    ):
                        approved = approve_ordinary_agent_enrollment(
                            store=store,
                            manager=manager,
                            cookie_header=manager.session_cookie_header(human),
                            csrf_token=manager.csrf_token(human),
                            principal_id=principal_id,
                            operation_id=operation_id,
                        )
                        bundles[principal_id] = prepare_approved_test_issuance(approved)[1]
                    with patch(
                        "control_plane.ordinary_agent_enrollment_worker.issue_ordinary_agent_credential",
                        side_effect=lambda **kwargs: bundles[kwargs["principal_id"]],
                    ):
                        recovered = recover_ordinary_agent_enrollments_once(
                            record_store=store,
                            state=OrdinaryAgentEnrollmentRecoveryState(),
                            lease_owner="two-principal-worker",
                            limit=20,
                        )
                    self.assertEqual(recovered.applied, 2)
                    self.assertEqual(recovered.failed, 0)
                    for principal_id in bundles:
                        self.assertIsNotNone(
                            store.read_current_ordinary_agent_principal(principal_id=principal_id)
                        )
                    prepare.reset_mock()
                    after_apply = await client.post(
                        path,
                        headers={"Authorization": "Bearer terminal-client-private"},
                        json=request,
                    )
                    self.assertEqual(after_apply.status_code, 200, after_apply.text)
                    self.assertTrue(after_apply.json()["operation"]["applied"])
                    self.assertEqual(
                        after_apply.json()["operation"]["operation_id"], first_operation
                    )
                    prepare.assert_not_called()
                    # A concurrent winner can commit after the early read;
                    # newly inspected evidence must not replace its intent.
                    original_replay = store.replay_proposed_ordinary_agent_enrollment
                    prepare.side_effect = None
                    prepare.return_value = PreparedOrdinaryAgentEnrollmentScope(
                        policy=scope.policy,
                        custody=scope.custody.model_copy(
                            update={"provider_inspection_sha256": "9" * 64}
                        ),
                        principal=None,
                        credential_id=None,
                        credential_version=None,
                    )
                    with patch.object(
                        store,
                        "replay_proposed_ordinary_agent_enrollment",
                        side_effect=[
                            None,
                            original_replay(
                                requester=TerminalAgentIdentity(
                                    subject="cli-client", token_label="client"
                                ),
                                principal_id="agent_one",
                                operation_id=first_operation,
                            ),
                        ],
                    ):
                        raced = await client.post(
                            path,
                            headers={"Authorization": "Bearer terminal-client-private"},
                            json=request,
                        )
                    self.assertEqual(raced.status_code, 200, raced.text)
                    self.assertEqual(raced.json(), after_apply.json())
