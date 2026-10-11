from typing import cast
from pathlib import Path
from dataclasses import replace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore, ProductProfileCompareWriteResult
from tests.http_app_test_support import (
    _browser_mutation_headers,
    _github_human_identity,
    _github_oauth_config,
    _local_operator_bearer_config,
    _RejectingVerifier,
)
from tests.support.http import request
from tests.support.profiles import product_profile_payload
from tests.support.stores import _sqlite_database_url


class SignedInProductSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_setup_plans_and_applies_without_releasing_or_changing_production(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "state.db")
            )
            store.ensure_schema()
            original = LaunchplaneProductProfileRecord.model_validate(
                {
                    **product_profile_payload("example-site"),
                    "production_use": "live",
                    "lanes": [{"context": "example-site", "instance": "prod"}],
                    "preview": {"enabled": False},
                }
            )
            store.write_product_profile_record(original)
            target = DokployTargetRecord(
                context="example-site",
                instance="testing",
                target_type="application",
                target_name="isolated-test",
                source_type="docker",
                domains=("testing.example.com",),
                healthcheck_path="/health",
                updated_at=original.updated_at,
                source_label="test",
            )
            target_id = DokployTargetIdRecord(
                context=target.context,
                instance=target.instance,
                target_id="test-target",
                updated_at=original.updated_at,
                source_label="test",
            )
            store.write_dokploy_target_record(target)
            store.write_dokploy_target_id_record(target_id)
            store.write_provider_target_record(
                ProviderTargetRecord.from_dokploy_records(
                    target_record=target,
                    target_id_record=target_id,
                )
            )
            manager = HumanSessionManager(
                config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
            )
            session = manager.issue(_github_human_identity())
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "local_operators": [
                        {
                            "subjects": ["local-owner-agent"],
                            "token_labels": ["local-owner-read"],
                            "products": [original.product],
                            "contexts": ["launchplane"],
                            "actions": ["product_profile.expected_config.apply"],
                        }
                    ],
                    "terminal_agents": [
                        {
                            "subjects": ["read-only-agent"],
                            "token_labels": ["context-read"],
                            "products": ["*"],
                            "contexts": ["*"],
                            "actions": [],
                        }
                    ],
                    "github_humans": [
                        {
                            "github_ids": [123],
                            "roles": ["admin"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["authz_policy_grant.write"],
                        }
                    ],
                }
            )
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                record_store_factory=lambda: store,
                human_session_manager=manager,
                bearer_identity_config=_local_operator_bearer_config().model_copy(
                    update={
                        "terminal_agent_token": "read-token",
                        "terminal_agent_subject": "read-only-agent",
                        "terminal_agent_token_label": "context-read",
                    }
                ),
            )
            environment = {
                "schema_version": 1,
                "product": original.product,
                "context": target.context,
                "instance": target.instance,
                "base_url": "https://testing.example.com",
                "mode": "dry-run",
                "reason": "Isolated testing metadata",
            }
            route = "/v1/product-profiles/stable-lane-repair/apply"
            plan = await request(
                app,
                "POST",
                route,
                payload=environment,
                headers=_browser_mutation_headers(manager, session),
            )
            self.assertEqual(plan.status_code, 202, plan.json())
            self.assertEqual(store.read_product_profile_record(original.product), original)
            apply = await request(
                app,
                "POST",
                route,
                payload={
                    **environment,
                    "mode": "apply",
                    "reviewed_plan_sha256": plan.json()["result"]["plan_sha256"],
                },
                headers={
                    **_browser_mutation_headers(manager, session),
                    "Idempotency-Key": "setup-environment",
                },
            )
            self.assertEqual(apply.status_code, 202, apply.json())
            replay = await request(
                app,
                "POST",
                route,
                payload={
                    **environment,
                    "mode": "apply",
                    "reviewed_plan_sha256": plan.json()["result"]["plan_sha256"],
                },
                headers={
                    **_browser_mutation_headers(manager, session),
                    "Idempotency-Key": "setup-environment",
                },
            )
            self.assertEqual(replay.status_code, 202, replay.json())
            self.assertTrue(replay.json()["replayed"])
            with_lane = store.read_product_profile_record(original.product)
            self.assertEqual(with_lane.lanes[0], original.lanes[0])
            self.assertEqual(with_lane.release_on_acceptance, original.release_on_acceptance)
            self.assertEqual(with_lane.owner, original.owner)
            declarations = {
                "schema_version": 1,
                "product": original.product,
                "mode": "dry-run",
                "reason": "Testing fields",
                "managed_secret_bindings": [
                    {
                        "binding_key": name,
                        "integration": "runtime_environment",
                        "context": target.context,
                        "instance": target.instance,
                    }
                    for name in ("BOT_TOKEN", "BRIDGE_TOKEN")
                ],
            }
            route = "/v1/product-profiles/expected-config/apply"
            plan = await request(
                app,
                "POST",
                route,
                payload=declarations,
                headers=_browser_mutation_headers(manager, session),
            )
            self.assertEqual(plan.status_code, 202, plan.json())
            self.assertEqual(
                plan.json()["result"]["managed_secret_bindings"]["added"],
                [
                    {**item, "owner_input": None}
                    for item in cast(
                        list[dict[str, object]], declarations["managed_secret_bindings"]
                    )
                ],
            )
            self.assertEqual(store.read_product_profile_record(original.product), with_lane)
            apply = await request(
                app,
                "POST",
                route,
                payload={**declarations, "mode": "apply"},
                headers={
                    **_browser_mutation_headers(manager, session),
                    "Idempotency-Key": "setup-declarations",
                },
            )
            self.assertEqual(apply.status_code, 202, apply.json())
            stored = store.read_product_profile_record(original.product)
            self.assertEqual(
                [item.instance for item in stored.expected_config.managed_secret_bindings],
                [target.instance, target.instance],
            )
            self.assertEqual(stored.lanes, with_lane.lanes)
            self.assertEqual(stored.release_on_acceptance, original.release_on_acceptance)
            self.assertEqual(
                store.list_secret_bindings(integration="runtime_environment", limit=None), ()
            )
            for route, payload in (
                (route, declarations),
                ("/v1/product-profiles/stable-lane-repair/apply", environment),
            ):
                for headers in (
                    {"Cookie": manager.session_cookie_header(session)},
                    {"Authorization": "Bearer local-operator-token"},
                    {"Authorization": "Bearer read-token"},
                ):
                    denied = await request(app, "POST", route, payload=payload, headers=headers)
                    self.assertEqual(denied.status_code, 403, denied.json())
                    if (
                        route.endswith("expected-config/apply")
                        and headers.get("Authorization") == "Bearer local-operator-token"
                    ):
                        self.assertEqual(
                            denied.json()["error"]["code"], "live_product_requires_operator"
                        )
                read_only_session = manager.issue(
                    replace(_github_human_identity(role="read_only"), github_id=124)
                )
                denied = await request(
                    app,
                    "POST",
                    route,
                    payload=payload,
                    headers=_browser_mutation_headers(manager, read_only_session),
                )
                self.assertEqual(denied.status_code, 403, denied.json())
                self.assertEqual(store.read_product_profile_record(original.product), stored)
            concurrent = stored.model_copy(update={"display_name": "Concurrent profile edit"})
            compare = store.compare_and_write_product_profile_record

            def concurrent_write(
                *,
                expected_record: LaunchplaneProductProfileRecord,
                replacement_record: LaunchplaneProductProfileRecord,
            ) -> ProductProfileCompareWriteResult:
                store.write_product_profile_record(concurrent)
                return compare(
                    expected_record=expected_record, replacement_record=replacement_record
                )

            with patch.object(
                store, "compare_and_write_product_profile_record", side_effect=concurrent_write
            ):
                refused = await request(
                    app,
                    "POST",
                    "/v1/product-profiles/expected-config/apply",
                    payload={
                        **declarations,
                        "mode": "apply",
                        "managed_secret_bindings": [
                            {
                                "binding_key": "ANOTHER_TOKEN",
                                "integration": "runtime_environment",
                                "context": target.context,
                                "instance": target.instance,
                            }
                        ],
                    },
                    headers={
                        **_browser_mutation_headers(manager, session),
                        "Idempotency-Key": "raced-declaration",
                    },
                )
            self.assertEqual(refused.status_code, 409, refused.json())
            self.assertEqual(store.read_product_profile_record(original.product), concurrent)
            store.close()
