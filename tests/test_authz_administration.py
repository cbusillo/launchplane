from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import Literal, cast

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from control_plane.contracts.authz_administration import AUTHZ_POLICY_ADMINISTRATION_READ_ACTION
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.privileged_operation import AUTHZ_POLICY_OPERATION_PROPOSE_ACTION
from control_plane.http_routes.authz_administration import (
    AuthzAdministrationRouteDependencies,
    register_authz_administration_routes,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.http import lifespan_client


def _record(
    *,
    policy: LaunchplaneAuthzPolicy,
    revision: int,
    status: Literal["active", "superseded"] = "active",
) -> LaunchplaneAuthzPolicyRecord:
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=revision, policy_sha256=digest),
        revision=revision,
        status=status,
        source="test:authz-administration",
        updated_at="2026-08-27T12:00:00+00:00",
        policy_sha256=digest,
        policy=policy,
        audit={
            "operation": "managed_rule_set_reconcile",
            "mode": "apply",
            "managed_set_id": "admin.set",
            "changed": True,
            "diff": {"added_rule_count": 1, "removed_rule_count": 0},
        },
    )


def _policy(*, managed_rule_id: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_humans": [
                {
                    "managed_set_id": "admin.set",
                    "managed_rule_id": managed_rule_id,
                    "github_ids": [101],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": [
                        AUTHZ_POLICY_ADMINISTRATION_READ_ACTION,
                        AUTHZ_POLICY_OPERATION_PROPOSE_ACTION,
                    ],
                }
            ],
        }
    )


def _identity() -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login="admin",
        github_id=101,
        name="Admin",
        email="admin@example.test",
        organizations=frozenset(),
        teams=frozenset(),
        role="admin",
    )


class _ErrorResponse(BaseModel):
    trace_id: str


def _http_error(
    *,
    status_code: int,
    trace_id: str,
    code: str,
    message: str,
    authz: dict[str, object] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"trace_id": trace_id, "code": code, "message": message}
    )


def _app(*, store: PostgresRecordStore, policy: LaunchplaneAuthzPolicy) -> FastAPI:
    app = FastAPI()
    common = ReadRouteDependencies(
        read_identity=lambda: _identity(),
        get_record_store=lambda: store,
        next_trace_id=lambda: "trace-authz-administration",
        authorization_allows=policy.allows,
        http_error=_http_error,
        error_response_model=_ErrorResponse,
    )
    register_authz_administration_routes(
        cast(ApiRouteRegistrar, app),
        dependencies=AuthzAdministrationRouteDependencies(
            common=common,
            read_nonrenewing_identity=lambda: _identity(),
            runtime_policy_reader=lambda: policy,
        ),
    )
    return app


class AuthzAdministrationRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_administration_read_history_and_rollback_are_bounded_and_nonmutating(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'db.sqlite3'}"
            )
            store.ensure_schema()
            historical_policy = _policy(managed_rule_id="historical-admin")
            historical_record = store.seed_authz_policy_if_absent(
                _record(policy=historical_policy, revision=1)
            )
            active_policy = _policy(managed_rule_id="active-admin")
            active_record = _record(policy=active_policy, revision=2)
            store.compare_and_write_authz_policy_record(
                expected_record=historical_record,
                replacement_record=active_record,
            )
            app = _app(store=store, policy=active_policy)
            before = store.list_authz_policy_records()
            async with lifespan_client(app) as client:
                administration = await client.get("/v1/authz-policies/administration")
                history = await client.get("/v1/authz-policies/revisions")
                rollback = await client.post(
                    "/v1/authz-policies/managed-rule-sets/rollback-proposal",
                    json={
                        "target_revision": 1,
                        "managed_set_id": "admin.set",
                        "reason": "Restore the reviewed historical managed set.",
                        "related_issue": "#2180",
                        "source_event_id": "rollback-proposal-2180",
                    },
                )
            after = store.list_authz_policy_records()
            store.close()

        self.assertEqual(administration.status_code, 200, administration.text)
        self.assertEqual(administration.headers["cache-control"], "no-store")
        payload = administration.json()
        self.assertEqual(payload["policy"]["revision"], 2)
        self.assertEqual(payload["managed_rules"]["items"][0]["managed_rule_id"], "active-admin")
        self.assertNotIn("github_ids", str(payload))
        self.assertNotIn(AUTHZ_POLICY_OPERATION_PROPOSE_ACTION, str(payload))
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.headers["cache-control"], "no-store")
        self.assertEqual(
            history.json()["revisions"][0]["audit"]["diff_counts"],
            {"added_rule_count": 1, "removed_rule_count": 0},
        )
        self.assertEqual(rollback.status_code, 200, rollback.text)
        self.assertEqual(rollback.headers["cache-control"], "no-store")
        proposal = rollback.json()["proposal"]
        self.assertEqual(proposal["descriptor_id"], "managed-authz-policy-set")
        self.assertEqual(
            proposal["request"]["desired_policy"]["github_humans"][0]["managed_rule_id"],
            "historical-admin",
        )
        self.assertEqual(before, after)

    async def test_export_requires_existing_managed_proposal_authority(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'db.sqlite3'}"
            )
            store.ensure_schema()
            policy = _policy(managed_rule_id="active-admin")
            store.seed_authz_policy_if_absent(_record(policy=policy, revision=1))
            app = _app(store=store, policy=policy)
            async with lifespan_client(app) as client:
                response = await client.get("/v1/authz-policies/active/export")
            store.close()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["canonical_policy"]["schema_version"], 2)

    async def test_export_rejects_an_administration_reader_without_proposal_authority(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'db.sqlite3'}"
            )
            store.ensure_schema()
            full_policy = _policy(managed_rule_id="active-admin")
            reader_policy = full_policy.model_copy(
                update={
                    "github_humans": (
                        full_policy.github_humans[0].model_copy(
                            update={"actions": (AUTHZ_POLICY_ADMINISTRATION_READ_ACTION,)}
                        ),
                    )
                }
            )
            store.seed_authz_policy_if_absent(_record(policy=reader_policy, revision=1))
            app = _app(store=store, policy=reader_policy)
            async with lifespan_client(app) as client:
                response = await client.get("/v1/authz-policies/active/export")
            store.close()

        self.assertEqual(response.status_code, 403, response.text)
