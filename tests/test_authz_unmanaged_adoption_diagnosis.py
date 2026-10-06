from __future__ import annotations

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.http_routes.mutation_support import idempotency_scope
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.auth import _StubVerifier
from tests.support.http import lifespan_client
from tests.support.stores import _sqlite_database_url
from tests.test_authz_empty_set_contraction import _policy, _request
from tests.test_authz_grant_service import _active_record_for_policy, _identity


def _adoption_request(
    policy: LaunchplaneAuthzPolicy | None = None, **overrides: object
) -> dict[str, object]:
    policy = policy or _policy()
    managed_rule = policy.local_operators[0].model_copy(
        update={"managed_set_id": "test.adoption", "managed_rule_id": "reader"}
    )
    return _request(
        managed_set_id=managed_rule.managed_set_id,
        desired_policy={
            "schema_version": policy.schema_version,
            "local_operators": [managed_rule.model_dump(mode="json")],
        },
        **overrides,
    ).model_dump(mode="json")


class UnmanagedAdoptionDiagnosisHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_policy_adoption_rejection_is_redacted_and_does_not_write(self) -> None:
        policy = _policy()
        active = _active_record_for_policy(policy)
        managed_rule = policy.local_operators[0].model_copy(
            update={"managed_set_id": "test.adoption", "managed_rule_id": "reader"}
        )
        with (
            TemporaryDirectory() as directory,
            closing(
                PostgresRecordStore(
                    database_url=_sqlite_database_url(Path(directory) / "state.sqlite")
                )
            ) as store,
        ):
            store.ensure_schema()
            with patch.object(store, "list_authz_policy_records", return_value=(active,)):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=policy,
                    record_store_factory=lambda: store,
                )
                async with lifespan_client(app) as client:
                    for mode in ("dry_run", "apply"):
                        with self.subTest(mode=mode):
                            request = _request(
                                mode=mode,
                                managed_set_id=managed_rule.managed_set_id,
                                desired_policy={
                                    "schema_version": policy.schema_version,
                                    "local_operators": [managed_rule.model_dump(mode="json")],
                                },
                                reviewed_plan_sha256="0" * 64 if mode == "apply" else "",
                            )
                            response = await client.post(
                                "/v1/authz-policies/managed-rule-sets/reconcile",
                                headers={
                                    "Authorization": "Bearer valid-token",
                                    "Idempotency-Key": "rejected-adoption",
                                },
                                json=request.model_dump(mode="json"),
                            )
                            self.assertEqual(response.status_code, 409, response.text)
                            error = response.json()["error"]
                            self.assertEqual(
                                error["code"], "authz_policy_unmanaged_adoption_conflict"
                            )
                            self.assertIn("unmanaged rule", error["message"])
                            self.assertNotIn("changed", error["message"])
                            self.assertNotIn("retry", error["message"])
                            for private_value in (
                                active.policy_sha256,
                                request.managed_set_id,
                                managed_rule.subjects[0],
                                _identity().workflow_ref,
                            ):
                                self.assertNotIn(private_value, response.text)
                            self.assertIsNone(
                                store.read_idempotency_record(
                                    scope=idempotency_scope(_identity()),
                                    route_path="/v1/authz-policies/managed-rule-sets/reconcile",
                                    idempotency_key="rejected-adoption",
                                )
                            )
            self.assertEqual(store.list_authz_policy_records(), ())

    async def test_adoption_candidate_with_policy_drift_keeps_drift_diagnosis(self) -> None:
        policy = _policy()
        active = _active_record_for_policy(policy)
        changed = _active_record_for_policy(policy.model_copy(update={"administrator_quorum": 2}))
        with (
            TemporaryDirectory() as directory,
            closing(
                PostgresRecordStore(
                    database_url=_sqlite_database_url(Path(directory) / "state.sqlite")
                )
            ) as store,
        ):
            store.ensure_schema()
            for mode in ("dry_run", "apply"):
                for observations in (((changed,),), ((active,), (changed,))):
                    with (
                        self.subTest(mode=mode, observations=observations),
                        patch.object(store, "list_authz_policy_records", side_effect=observations),
                    ):
                        app = create_launchplane_fastapi_app(
                            verifier=_StubVerifier(_identity()),
                            authz_policy=policy,
                            record_store_factory=lambda: store,
                        )
                        async with lifespan_client(app) as client:
                            response = await client.post(
                                "/v1/authz-policies/managed-rule-sets/reconcile",
                                headers={
                                    "Authorization": "Bearer valid-token",
                                    "Idempotency-Key": "drift-with-adoption-candidate",
                                },
                                json=_adoption_request(
                                    mode=mode,
                                    reviewed_plan_sha256="0" * 64 if mode == "apply" else "",
                                ),
                            )
                        self.assertEqual(response.status_code, 409, response.text)
                        error = response.json()["error"]
                        self.assertEqual(error["code"], "authz_policy_conflict")
                        self.assertIn("changed", error["message"])
                        self.assertNotIn(changed.policy_sha256, response.text)

    async def test_original_key_replays_success_before_new_adoption_rejection(self) -> None:
        unmanaged_policy = _policy().model_copy(update={"schema_version": 2, "ordinary_agents": ()})
        managed_rule = unmanaged_policy.local_operators[0].model_copy(
            update={"managed_set_id": "test.adoption", "managed_rule_id": "reader"}
        )
        managed_policy = unmanaged_policy.model_copy(update={"local_operators": (managed_rule,)})
        managed_record = _active_record_for_policy(managed_policy)
        unmanaged_record = _active_record_for_policy(unmanaged_policy)
        with (
            TemporaryDirectory() as directory,
            closing(
                PostgresRecordStore(
                    database_url=_sqlite_database_url(Path(directory) / "state.sqlite")
                )
            ) as store,
        ):
            store.ensure_schema()
            store.seed_authz_policy_if_absent(managed_record)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=managed_policy,
                record_store_factory=lambda: store,
            )
            path = "/v1/authz-policies/managed-rule-sets/reconcile"
            headers = {"Authorization": "Bearer valid-token"}
            async with lifespan_client(app) as client:
                dry_run = await client.post(
                    path, headers=headers, json=_adoption_request(managed_policy)
                )
                self.assertEqual(dry_run.status_code, 202, dry_run.text)
                request = _adoption_request(
                    managed_policy,
                    mode="apply",
                    reviewed_plan_sha256=dry_run.json()["result"]["diff"]["plan_sha256"],
                )
                apply_headers = {**headers, "Idempotency-Key": "original-managed-noop"}
                applied = await client.post(path, headers=apply_headers, json=request)
                self.assertEqual(applied.status_code, 202, applied.text)
                with patch.object(
                    store, "list_authz_policy_records", return_value=(unmanaged_record,)
                ):
                    # Bind authorization to the new fixed policy so planning reaches adoption.
                    replay_app = create_launchplane_fastapi_app(
                        verifier=_StubVerifier(_identity()),
                        authz_policy=unmanaged_policy,
                        record_store_factory=lambda: store,
                    )
                    async with lifespan_client(replay_app) as replay_client:
                        replayed = await replay_client.post(
                            path, headers=apply_headers, json=request
                        )
                        fresh = await replay_client.post(
                            path,
                            headers={**headers, "Idempotency-Key": "new-adoption-attempt"},
                            json=request,
                        )
                self.assertEqual(replayed.status_code, 202, replayed.text)
                self.assertTrue(replayed.json()["replayed"])
                self.assertEqual(replayed.json()["result"], applied.json()["result"])
                self.assertEqual(fresh.status_code, 409, fresh.text)
                self.assertEqual(
                    fresh.json()["error"]["code"], "authz_policy_unmanaged_adoption_conflict"
                )
            self.assertEqual(store.list_authz_policy_records(status="active"), (managed_record,))
