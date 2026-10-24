from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.service_auth import BearerIdentityConfig, LaunchplaneAuthzPolicy
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _browser_mutation_headers,
    _github_human_identity,
    _github_oauth_config,
    _RejectingVerifier,
    _MissingProductReadStore,
    _post_product_health_monitoring,
    _product_health_monitoring_policy,
)
from tests.test_http_app_product import (
    _product_health_monitoring_identity,
    _product_health_monitoring_payload,
    _product_health_monitoring_profile,
)
from tests.support.http import request
from tests.support.auth import _StubVerifier, scoped_agent_test
from tests.support.stores import _sqlite_database_url


class HealthMonitoringBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_director_agents_apply_monitoring_without_per_action_grants(self) -> None:
        for transport in ("local_operator", "terminal_agent"):
            for instance in ("testing", "prod"):
                for kind in ("public_http", "private_http"):
                    with (
                        self.subTest(transport=transport, instance=instance, kind=kind),
                        TemporaryDirectory() as directory,
                    ):
                        store = PostgresRecordStore(
                            database_url=_sqlite_database_url(Path(directory) / "db")
                        )
                        self.addCleanup(store.close)
                        store.ensure_schema()
                        profile_data = _product_health_monitoring_profile().model_dump(mode="json")
                        profile_data["production_use"] = "live"
                        profile_data["lanes"][0]["instance"] = instance
                        profile = LaunchplaneProductProfileRecord.model_validate(profile_data)
                        store.write_product_profile_record(profile)
                        endpoint = PrivateHealthEndpointRecord(
                            endpoint_key="runtime-health",
                            product=profile.product,
                            context=profile.lanes[0].context,
                            instance=instance,
                            url="https://runtime.internal/health",
                            updated_at=profile.updated_at,
                        )
                        store.write_private_health_endpoint_record(endpoint)
                        app = create_launchplane_fastapi_app(
                            verifier=_RejectingVerifier(),
                            bearer_identity_config=BearerIdentityConfig.model_validate(
                                {
                                    f"{transport}_token": "fixture-token",
                                    f"{transport}_subject": "fixture-agent",
                                    f"{transport}_token_label": "fixture-label",
                                }
                            ),
                            authz_policy=LaunchplaneAuthzPolicy(schema_version=3),
                            record_store_factory=lambda: store,
                        )
                        payload = {
                            **_product_health_monitoring_payload(),
                            "instance": instance,
                            "check_name": "runtime-health",
                            "check_kind": kind,
                            "monitoring_intent": "private" if kind == "private_http" else "public",
                            "private_endpoint_key": endpoint.endpoint_key
                            if kind == "private_http"
                            else "",
                        }
                        headers = {"Authorization": "Bearer fixture-token"}
                        route = "/v1/product-profiles/health-monitoring/apply"
                        plan = await request(app, "POST", route, payload=payload, headers=headers)
                        self.assertEqual(plan.status_code, 202, plan.text)
                        applied = {**payload, "mode": "apply"}
                        applied["reviewed_plan_sha256"] = plan.json()["result"]["plan_sha256"]
                        headers["Idempotency-Key"] = "monitoring-change"
                        response = await request(
                            app, "POST", route, payload=applied, headers=headers
                        )
                        self.assertEqual(response.status_code, 202, response.text)
                        self.assertTrue(response.json()["result"]["applied"])
                        saved = store.read_product_profile_record(profile.product)
                        check = next(
                            check
                            for check in saved.lanes[0].health_monitoring.checks
                            if check.name == payload["check_name"]
                        )
                        self.assertTrue(check.enabled)
                        self.assertTrue(check.require_runtime_identity)
                        self.assertEqual(check.kind, kind)
                        self.assertEqual(saved.production_use, profile.production_use)
                        self.assertEqual(saved.lanes[0].base_url, profile.lanes[0].base_url)
                        self.assertEqual(store.list_runtime_environment_records(), ())
                        self.assertEqual(
                            store.read_private_health_endpoint_record(endpoint.endpoint_key),
                            endpoint,
                        )
                        replay = await request(app, "POST", route, payload=applied, headers=headers)
                        self.assertEqual(replay.status_code, 202, replay.text)
                        self.assertTrue(replay.json()["replayed"])

    async def test_workflow_plan_apply_and_replay_keep_browser_evidence_out_of_result(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=_sqlite_database_url(Path(directory) / "db"))
            store.ensure_schema()
            store.write_product_profile_record(_product_health_monitoring_profile())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_health_monitoring_identity()),
                authz_policy=_product_health_monitoring_policy(),
                record_store_factory=lambda: store,
            )
            for creating in (False, True):
                profile_payload = _product_health_monitoring_profile().model_dump(mode="json")
                profile_payload["lanes"][0]["health_monitoring"]["checks"][0][
                    "expected_dns_targets"
                ] = [
                    {
                        "public_name": "cm-testing.example.com",
                        "addresses": ["8.8.8.8"],
                    }
                ]
                if not creating:
                    profile_payload["lanes"][0]["health_monitoring"]["dependency_health"] = {
                        "mode": "required"
                    }
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(profile_payload)
                )
                payload = _product_health_monitoring_payload()
                if creating:
                    payload["check_name"] = "new-health-check"
                plan = await _post_product_health_monitoring(app, payload)
                self.assertEqual(plan.status_code, 202)
                self.assertNotIn("current_private_endpoint_key", plan.json()["result"])
                self.assertNotIn("current_dependency_health", plan.json()["result"])
                self.assertNotIn("requested_dependency_health", plan.json()["result"])
                self.assertNotIn("current_expected_dns_targets", plan.json()["result"])
                self.assertNotIn("requested_expected_dns_targets", plan.json()["result"])
                applied = {
                    **payload,
                    "mode": "apply",
                    "reviewed_plan_sha256": plan.json()["result"]["plan_sha256"],
                }
                for _ in range(2):
                    response = await _post_product_health_monitoring(
                        app, applied, idempotency_key=f"compatible-workflow-{creating}"
                    )
                    self.assertEqual(response.status_code, 202)
                    self.assertNotIn("current_private_endpoint_key", response.json()["result"])
                    self.assertNotIn("current_dependency_health", response.json()["result"])
                    self.assertNotIn("requested_dependency_health", response.json()["result"])
                    self.assertNotIn("current_expected_dns_targets", response.json()["result"])
                    self.assertNotIn("requested_expected_dns_targets", response.json()["result"])
                    if creating:
                        for field in (
                            "current_check_kind",
                            "current_enabled",
                            "current_require_runtime_identity",
                        ):
                            self.assertIn(field, response.json()["result"])
                            self.assertIsNone(response.json()["result"][field])
                self.assertTrue(response.json()["replayed"])
            store.close()

    @scoped_agent_test
    async def test_scoped_terminal_agent_cannot_use_monitoring_even_with_broad_policy(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            bearer_identity_config=BearerIdentityConfig(
                terminal_agent_token="fixture-token",
                terminal_agent_subject="fixture-reader",
                terminal_agent_token_label="fixture-read",
            ),
            authz_policy=LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "terminal_agents": [
                        {
                            "subjects": ["fixture-reader"],
                            "token_labels": ["fixture-read"],
                            "products": ["odoo-product"],
                            "contexts": ["cm"],
                            "instances": ["testing"],
                        }
                    ],
                }
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )
        response = await request(
            app,
            "POST",
            "/v1/product-profiles/health-monitoring/apply",
            payload=_product_health_monitoring_payload(),
            headers={"Authorization": "Bearer fixture-token"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_admin_reviews_applies_and_replays_with_csrf(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=_sqlite_database_url(Path(directory) / "db"))
            store.ensure_schema()
            original = _product_health_monitoring_profile()
            store.write_product_profile_record(original)
            manager = HumanSessionManager(
                config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
            )
            session = manager.issue(_github_human_identity())
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "github_humans": [
                        {
                            "github_ids": [session.identity.github_id],
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
            )
            path = "/v1/product-profiles/health-monitoring/apply"
            payload = _product_health_monitoring_payload()
            expected_addresses = ("8.8.8.8",)
            payload["expected_dns_targets"] = [
                {
                    "public_name": "cm-testing.example.com",
                    "addresses": list(expected_addresses),
                }
            ]
            rejected = await request(
                app,
                "POST",
                path,
                payload=payload,
                headers={"Cookie": manager.session_cookie_header(session)},
            )
            self.assertEqual(rejected.status_code, 403)
            self.assertEqual(store.read_product_profile_record(original.product), original)
            plan = await request(
                app,
                "POST",
                path,
                payload=payload,
                headers=_browser_mutation_headers(manager, session),
            )
            self.assertEqual(plan.status_code, 202)
            self.assertEqual(
                plan.json()["result"]["current_private_endpoint_key"],
                original.lanes[0].health_monitoring.checks[0].private_endpoint_key,
            )
            self.assertEqual(store.read_product_profile_record(original.product), original)
            self.assertEqual(
                plan.json()["result"]["requested_expected_dns_targets"],
                payload["expected_dns_targets"],
            )
            apply_payload = {
                **payload,
                "mode": "apply",
                "reviewed_plan_sha256": plan.json()["result"]["plan_sha256"],
            }
            changed_payload = {
                **apply_payload,
                "expected_dns_targets": [
                    {
                        "public_name": "cm-testing.example.com",
                        "addresses": ["1.1.1.1"],
                    }
                ],
            }
            changed_response = await request(
                app,
                "POST",
                path,
                payload=changed_payload,
                headers={
                    **_browser_mutation_headers(manager, session),
                    "Idempotency-Key": "changed-dns",
                },
            )
            self.assertEqual(changed_response.status_code, 409)
            self.assertEqual(store.read_product_profile_record(original.product), original)
            responses = []
            for _ in range(2):
                responses.append(
                    await request(
                        app,
                        "POST",
                        path,
                        payload=apply_payload,
                        headers={
                            **_browser_mutation_headers(manager, session),
                            "Idempotency-Key": "browser-monitoring",
                        },
                    )
                )
            self.assertTrue(all(response.status_code == 202 for response in responses))
            self.assertTrue(responses[0].json()["result"]["applied"])
            self.assertTrue(responses[1].json()["replayed"])
            saved = store.read_product_profile_record(original.product)
            self.assertTrue(saved.lanes[0].health_monitoring.checks[0].require_runtime_identity)
            self.assertEqual(
                saved.lanes[0].health_monitoring.checks[0].expected_dns_targets[0].addresses,
                expected_addresses,
            )
            self.assertEqual(saved.lanes[0].health_url, original.lanes[0].health_url)
            self.assertEqual(saved.lanes[1:], original.lanes[1:])
            store.close()

    async def test_scoped_human_plan_cannot_apply_or_target_another_instance(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=_sqlite_database_url(Path(directory) / "db"))
            store.ensure_schema()
            original = _product_health_monitoring_profile()
            store.write_product_profile_record(original)
            manager = HumanSessionManager(
                config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
            )
            session = manager.issue(_github_human_identity())
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "github_humans": [
                        {
                            "github_ids": [session.identity.github_id],
                            "roles": ["admin"],
                            "products": [original.product],
                            "contexts": ["cm"],
                            "instances": ["testing"],
                            "actions": ["product_profile.health_monitoring.plan"],
                        }
                    ],
                }
            )
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                record_store_factory=lambda: store,
                human_session_manager=manager,
            )
            path = "/v1/product-profiles/health-monitoring/apply"
            payload = _product_health_monitoring_payload()
            for change in (
                {"instance": "prod"},
                {"mode": "apply", "reviewed_plan_sha256": "a" * 64},
            ):
                response = await request(
                    app,
                    "POST",
                    path,
                    payload={**payload, **change},
                    headers={
                        **_browser_mutation_headers(manager, session),
                        "Idempotency-Key": "denied-browser-monitoring",
                    },
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"]["code"], "authorization_denied")
            self.assertEqual(store.read_product_profile_record(original.product), original)
            store.close()
