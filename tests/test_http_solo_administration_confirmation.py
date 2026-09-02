from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.authz_grant_service import plan_managed_authz_policy_reconcile
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.http_app import (
    SOLO_ADMINISTRATION_CONFIRMATION_ACKNOWLEDGEMENT,
    create_launchplane_fastapi_app,
)
from control_plane.service_auth import GitHubHumanPolicyRule, LaunchplaneAuthzPolicy
from control_plane.service_human_auth import (
    HumanSessionManager,
    InMemoryHumanSessionStore,
    LaunchplaneHumanSession,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _github_human_identity, _github_oauth_config
from tests.support.auth import StubVerifier, identity
from tests.support.http import lifespan_client


def _active_policy_record() -> LaunchplaneAuthzPolicyRecord:
    policy = LaunchplaneAuthzPolicy(
        schema_version=2,
        github_humans=(
            GitHubHumanPolicyRule(
                github_ids=(123,),
                roles=("admin",),
                products=("launchplane",),
                contexts=("launchplane",),
                actions=("authz_policy_grant.write",),
            ),
        ),
    )
    policy_sha256 = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=1, policy_sha256=policy_sha256),
        revision=1,
        status="active",
        source="test",
        updated_at="2026-08-31T12:00:00Z",
        policy_sha256=policy_sha256,
        policy=policy,
    )


def _dry_run_payload(store: PostgresRecordStore) -> tuple[dict[str, object], str]:
    request = {
        "schema_version": 2,
        "product": "launchplane",
        "mode": "dry_run",
        "managed_set_id": "operator.launchplane",
        "administrator_quorum_change": 1,
        "reason": "Emergency solo administration recovery.",
        "desired_policy": {"schema_version": 2},
    }
    from control_plane.authz_grant_service import AuthzManagedPolicyReconcileEnvelope

    envelope = AuthzManagedPolicyReconcileEnvelope.model_validate(request)
    _, _, _, diff = plan_managed_authz_policy_reconcile(record_store=store, request=envelope)
    return request, diff.plan_sha256


class SoloAdministrationConfirmationHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_issue_and_apply_consume_confirmation_with_browser_binding(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = f"sqlite:///{Path(directory) / 'launchplane.sqlite3'}"
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.seed_authz_policy_if_absent(_active_policy_record())
            session_manager = HumanSessionManager(
                config=_github_oauth_config(),
                session_store=InMemoryHumanSessionStore(),
            )
            session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=StubVerifier(identity()),
                authz_policy=_active_policy_record().policy,
                database_url=database_url,
                record_store_factory=lambda: store,
                human_session_manager=session_manager,
            )
            issue_payload, plan_sha256 = _dry_run_payload(store)
            issue_payload = {
                "reconcile": issue_payload,
                "reviewed_plan_sha256": plan_sha256,
                "acknowledgement": SOLO_ADMINISTRATION_CONFIRMATION_ACKNOWLEDGEMENT,
            }
            headers = {
                "Cookie": session_manager.session_cookie_header(session),
                "Idempotency-Key": "future-apply-1",
                **session_manager_browser_headers(session_manager, session),
            }
            async with lifespan_client(app) as client:
                issue_response = await client.post(
                    "/v1/authz-policies/solo-administration-confirmations",
                    json=issue_payload,
                    headers=headers,
                )
                self.assertEqual(issue_response.status_code, 200)
                issue_result = issue_response.json()
                self.assertTrue(issue_result["secret"])
                confirmation_id = issue_result["confirmation_id"]
                secret = issue_result["secret"]

                current_session = session_manager.read_cookie_without_renewal(
                    session_manager.session_cookie_header(session)
                )
                assert current_session is not None
                apply_payload = {
                    "schema_version": 2,
                    "product": "launchplane",
                    "mode": "apply",
                    "managed_set_id": "operator.launchplane",
                    "administrator_quorum_change": 1,
                    "solo_administration_confirmation_id": confirmation_id,
                    "reviewed_plan_sha256": plan_sha256,
                    "reason": "Emergency solo administration recovery.",
                    "desired_policy": {"schema_version": 2},
                }
                wrong_secret_headers = {
                    "Cookie": session_manager.session_cookie_header(current_session),
                    "Idempotency-Key": "future-apply-1",
                    "X-Solo-Administration-Confirmation-Secret": "wrong-secret",
                    **session_manager_browser_headers(session_manager, current_session),
                }
                wrong_secret_response = await client.post(
                    "/v1/authz-policies/managed-rule-sets/reconcile",
                    json=apply_payload,
                    headers=wrong_secret_headers,
                )
                self.assertEqual(wrong_secret_response.status_code, 409)
                self.assertEqual(
                    wrong_secret_response.json()["error"]["code"],
                    "solo_administration_confirmation_invalid",
                )
                self.assertEqual(
                    store.read_solo_administration_confirmation(confirmation_id).state,
                    "issued",
                )

                current_session = session_manager.read_cookie_without_renewal(
                    session_manager.session_cookie_header(session)
                )
                assert current_session is not None
                apply_headers = {
                    "Cookie": session_manager.session_cookie_header(current_session),
                    "Idempotency-Key": "future-apply-1",
                    "X-Solo-Administration-Confirmation-Secret": secret,
                    **session_manager_browser_headers(session_manager, current_session),
                }
                apply_response = await client.post(
                    "/v1/authz-policies/managed-rule-sets/reconcile",
                    json=apply_payload,
                    headers=apply_headers,
                )
                self.assertEqual(apply_response.status_code, 202)
                self.assertEqual(
                    store.read_solo_administration_confirmation(confirmation_id).state,
                    "consumed",
                )

                current_session = session_manager.read_cookie_without_renewal(
                    session_manager.session_cookie_header(session)
                )
                assert current_session is not None
                replay_response = await client.post(
                    "/v1/authz-policies/managed-rule-sets/reconcile",
                    json=apply_payload,
                    headers={
                        "Cookie": session_manager.session_cookie_header(current_session),
                        "Idempotency-Key": "future-apply-1",
                        "X-Solo-Administration-Confirmation-Secret": secret,
                        **session_manager_browser_headers(session_manager, current_session),
                    },
                )
                self.assertEqual(replay_response.status_code, 202)
                self.assertEqual(
                    replay_response.json()["records"],
                    apply_response.json()["records"],
                )
                self.assertEqual(
                    store.read_solo_administration_confirmation(confirmation_id).state,
                    "consumed",
                )
            store.close()


def session_manager_browser_headers(
    manager: HumanSessionManager,
    session: LaunchplaneHumanSession,
) -> dict[str, str]:
    from control_plane.service_human_auth import build_browser_mutation_request_headers

    return build_browser_mutation_request_headers(
        origin=manager.public_origin,
        csrf_token=manager.csrf_token(session),
    )
