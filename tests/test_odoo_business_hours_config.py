import json
import unittest
from datetime import UTC, datetime
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from typing import Any, cast

from fastapi import FastAPI
from control_plane.service_auth import LaunchplaneAuthzPolicy

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity, RUNTIME_IDENTITY_ENV_KEY
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.odoo_business_hours import read_business_hours_window
from control_plane.product_config_http import ProductConfigApplyEnvelope
from control_plane.http_app import canonical_product_config_request_payload
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from tests.support.stores import sqlite_database_url


class CalendarConfigurationTests(unittest.TestCase):
    def test_calendar_section_survives_canonical_normalization(self) -> None:
        payload = {
            "mode": "dry-run",
            "product": "example-site",
            "context": "cm",
            "instance": "prod",
            "reason": "Select company calendar",
            "odoo_business_hours": {
                "operation": "set",
                "company_id": 7,
                "expected_calendar_id": 17,
            },
        }
        request = ProductConfigApplyEnvelope.model_validate(payload)
        canonical = canonical_product_config_request_payload(
            request.model_dump(mode="json", exclude_none=True)
        )
        self.assertEqual(canonical["odoo_business_hours"], payload["odoo_business_hours"])


class CalendarRuntimeFixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = self.open_store(self.root)
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        payload = _odoo_profile_payload_with_prod_lane("example-site")
        payload.update(
            production_use="live",
            odoo_business_hours={"company_id": 7, "private_endpoint_key": "calendar-prod"},
        )
        self.profile = LaunchplaneProductProfileRecord.model_validate(payload)
        self.store.write_product_profile_record(self.profile)
        self.identity = RuntimeIdentity(
            product=self.profile.product,
            context="cm",
            instance="prod",
            environment_kind="stable",
            deployment_record_id="deployment-prod",
            artifact_id="artifact-prod",
            source_git_ref="a" * 40,
            image_reference="ghcr.io/example/site@sha256:" + "a" * 64,
        )
        self.store.write_environment_inventory(
            EnvironmentInventory(
                context="cm",
                instance="prod",
                source_git_ref=self.identity.source_git_ref,
                deploy=DeploymentEvidence(
                    status="pass",
                    deploy_mode="dokploy-compose-api",
                    target_name="calendar-prod",
                    target_type="compose",
                ),
                updated_at=self.profile.updated_at,
                deployment_record_id=self.identity.deployment_record_id,
                runtime_identity=self.identity,
            )
        )
        self.store.write_dokploy_target_record(
            DokployTargetRecord(
                context="cm",
                instance="prod",
                target_name="calendar-prod",
                updated_at=self.profile.updated_at,
            )
        )
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context="cm",
                instance="prod",
                target_id="target-prod",
                updated_at=self.profile.updated_at,
            )
        )
        self.endpoint = PrivateHealthEndpointRecord(
            endpoint_key="calendar-prod",
            product=self.profile.product,
            context="cm",
            instance="prod",
            url="https://odoo.internal/launchplane/health",
            updated_at=self.profile.updated_at,
        )
        self.store.write_private_health_endpoint_record(self.endpoint)
        self.provider_calls: list[str] = []
        self.rpc_calls: list[dict[str, Any]] = []
        self.origins: list[str] = []
        self.enterContext(
            patch(
                "control_plane.odoo_runtime_reads.source.read_dokploy_config",
                return_value=("https://provider.invalid", "inert-provider"),
            )
        )
        self.enterContext(
            patch("control_plane.dokploy.api.dokploy_request", side_effect=self.provider)
        )
        self.enterContext(
            patch("control_plane.odoo_runtime_reads._http_json", side_effect=self.http)
        )
        self.enterContext(
            patch("control_plane.odoo_business_hours._http_json", side_effect=self.http)
        )
        self.now = datetime(2026, 10, 22, 15, tzinfo=UTC)

    def open_store(self, root: Path) -> PostgresRecordStore:
        return PostgresRecordStore(database_url=sqlite_database_url(root / "records.sqlite3"))

    def provider(self, **kwargs: Any) -> object:
        path = kwargs["path"]
        self.provider_calls.append(path)
        if path == "/api/compose.one":
            return {"name": "calendar-prod", "appName": "calendar-prod", "serverId": "server"}
        if path == "/api/docker.getContainersByAppNameMatch":
            return [
                {"containerId": "123456abcdef", "name": "calendar-prod-web-1", "state": "running"}
            ]
        if path == "/api/docker.getConfig":
            return {
                "State": {"Running": True},
                "Config": {
                    "Image": self.identity.image_reference,
                    "Labels": {
                        "com.docker.compose.project": "calendar-prod",
                        "com.docker.compose.service": "web",
                    },
                    "Env": [
                        f"{RUNTIME_IDENTITY_ENV_KEY}={self.identity.model_dump_json()}",
                        "ODOO_DB_NAME=example_db",
                        "ODOO_ADMIN_LOGIN=configured-reader",
                        "ODOO_ADMIN_PASSWORD=inert-password",
                    ],
                },
            }
        raise AssertionError(f"Unexpected provider call {path}")

    def http(self, _opener: object, request: Any) -> object:
        self.origins.append(request.full_url)
        if request.data is None:
            return {"runtime_identity": self.identity.model_dump(mode="json")}
        body = json.loads(request.data)
        params = body["params"]
        self.rpc_calls.append(params)
        envelope = {"jsonrpc": "2.0", "id": body["id"]}
        if request.full_url.endswith("authenticate"):
            return {
                **envelope,
                "result": {
                    "uid": 7,
                    "user_companies": {
                        "allowed_companies": {"7": {"id": 7, "name": "Example Company"}},
                        "current_company": 7,
                    },
                },
            }
        if request.full_url.endswith("destroy"):
            return envelope
        if params["method"] == "fields_get":
            return {**envelope, "result": {}}
        self.assertEqual(params["method"], "search_read")
        rows: dict[str, list[dict[str, object]]] = {
            "res.company": [
                {
                    "id": 7,
                    "name": "Example Company",
                    "resource_calendar_id": [17, "Company calendar"],
                }
            ],
            "resource.calendar": [
                {
                    "id": 17,
                    "tz": "America/New_York",
                    "two_weeks_calendar": False,
                    "flexible_hours": False,
                    "duration_based": False,
                }
            ],
            "resource.calendar.attendance": [
                {
                    "id": 1,
                    "dayofweek": str(self.now.weekday()),
                    "hour_from": 9,
                    "hour_to": 17,
                    "week_type": False,
                    "display_type": False,
                }
            ],
            "resource.calendar.leaves": [],
        }
        return {
            **envelope,
            "result": [
                {key: row[key] for key in params["kwargs"]["fields"]}
                for row in rows[params["model"]]
            ],
        }


class CalendarRuntimeTests(CalendarRuntimeFixture):
    def test_distinct_private_origin_preserves_canonical_runtime_authority(self) -> None:
        window = read_business_hours_window(
            store=self.store,
            profile=self.profile,
            control_plane_root=self.root,
            observed_at=self.now,
        )
        self.assertTrue(window.next_release_at)
        self.assertEqual(window.timezone, "America/New_York")
        self.assertTrue(self.provider_calls)
        self.assertTrue(self.origins)
        self.assertTrue(all(url.startswith("https://odoo.internal/") for url in self.origins))
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)
        self.assertEqual(self.rpc_calls[-1], {})

    def test_live_reader_refuses_wrong_company_calendar_and_missing_membership(self) -> None:
        original = self.http
        for contradiction in ("company", "calendar", "membership"):
            with self.subTest(contradiction=contradiction):

                def contradictory(opener: object, request: Any) -> object:
                    response = cast(dict[str, Any], original(opener, request))
                    if request.data is not None:
                        params = json.loads(request.data)["params"]
                        if contradiction == "membership" and request.full_url.endswith(
                            "authenticate"
                        ):
                            response["result"].pop("user_companies")
                        elif contradiction == "company" and params.get("model") == "res.company":
                            response["result"][0]["id"] = 8
                        elif (
                            contradiction == "calendar"
                            and params.get("model") == "resource.calendar"
                        ):
                            response["result"][0]["id"] = 18
                    return response

                with patch(
                    "control_plane.odoo_runtime_reads._http_json", side_effect=contradictory
                ):
                    window = read_business_hours_window(
                        store=self.store,
                        profile=self.profile,
                        control_plane_root=self.root,
                        observed_at=self.now,
                    )
                self.assertTrue(window.applies)
                self.assertTrue(window.blocked_reason)
                self.assertFalse(window.next_release_at)


class CalendarConfigurationHttpTests(CalendarRuntimeFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        from tests.http_app_test_support import _product_profile_write_policy
        from tests.support.auth import StubVerifier, identity
        from control_plane.service_auth import LaunchplaneAuthzPolicy

        policy = _product_profile_write_policy(product=self.profile.product).model_dump(mode="json")
        rule = policy["github_actions"][0]
        rule.update(
            products=[self.profile.product, "launchplane"],
            contexts=["cm", "launchplane"],
            actions=[
                "product_config.plan",
                "product_config.apply",
                "product_profile.read",
                "product_profile.write",
                "operations.read",
            ],
        )
        self.policy = LaunchplaneAuthzPolicy.model_validate(policy)
        self.app = self.make_app(
            verifier=StubVerifier(identity()),
            authz_policy=self.policy,
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )
        self.request_payload: dict[str, Any] = {
            "schema_version": 1,
            "mode": "dry-run",
            "product": self.profile.product,
            "context": "cm",
            "instance": "prod",
            "reason": "Select verified company calendar",
            "odoo_business_hours": {
                "operation": "set",
                "company_id": 7,
                "expected_calendar_id": 17,
                "private_endpoint_key": "calendar-prod",
            },
        }

    def make_app(self, *, authz_policy: LaunchplaneAuthzPolicy, **kwargs: Any) -> FastAPI:
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.contracts.authz_policy_record import (
            LaunchplaneAuthzPolicyRecord,
            authz_policy_sha256,
            build_authz_policy_record_id,
        )

        if self.store.database_dialect_name == "postgresql":
            digest = authz_policy_sha256(authz_policy)

            def record(revision: int) -> LaunchplaneAuthzPolicyRecord:
                return LaunchplaneAuthzPolicyRecord(
                    record_id=build_authz_policy_record_id(revision=revision, policy_sha256=digest),
                    revision=revision,
                    source="test:calendar-config",
                    updated_at=self.profile.updated_at,
                    policy=authz_policy,
                )

            current = self.store.seed_authz_policy_if_absent(record(1))
            if current.policy_sha256 != digest:
                changed = self.store.compare_and_write_authz_policy_record(
                    expected_record=current, replacement_record=record(current.revision + 1)
                )
                self.assertEqual(changed.status, "written")
        return create_launchplane_fastapi_app(authz_policy=authz_policy, **kwargs)

    async def post(self, body: dict[str, Any], key: str = "") -> Any:
        from tests.support.http import request

        return await request(
            self.app,
            "POST",
            "/v1/product-config/apply",
            headers={"Authorization": "Bearer valid-token", "Idempotency-Key": key},
            payload=body,
        )

    async def test_prelaunch_inspect_then_set_apply_replay_and_clear(self) -> None:
        before = LaunchplaneProductProfileRecord.model_validate(
            self.profile.model_copy(
                update={"production_use": "prelaunch", "odoo_business_hours": None}
            ).model_dump(mode="json")
        )
        self.store.write_product_profile_record(before)
        inspect = await self.post(
            {
                **self.request_payload,
                "odoo_business_hours": {
                    "operation": "inspect",
                    "private_endpoint_key": "calendar-prod",
                },
            }
        )
        self.assertEqual(inspect.status_code, 202, inspect.text)
        evidence = inspect.json()["result"]["odoo_business_hours"]
        self.assertEqual(evidence["companies"][0]["company_id"], 7)
        self.assertEqual(evidence["companies"][0]["calendar_id"], 17)
        self.assertFalse(evidence["apply_eligible"])
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), before)
        dry = await self.post(self.request_payload)
        self.assertEqual(dry.status_code, 202, dry.text)
        plan = dry.json()["result"]["odoo_business_hours"]
        self.assertTrue(plan["apply_eligible"])
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), before)
        body = {
            **self.request_payload,
            "mode": "apply",
            "reviewed_plan_sha256": plan["plan_digest"],
        }
        applied = await self.post(body, "set-calendar")
        self.assertEqual(applied.status_code, 202, applied.text)
        after = self.store.read_product_profile_record(self.profile.product)
        self.assertEqual(after.odoo_business_hours, self.profile.odoo_business_hours)
        assert after.odoo_business_hours is not None
        expected = before.model_dump(mode="json")
        expected.update(
            odoo_business_hours=after.odoo_business_hours.model_dump(mode="json"),
            updated_at=after.updated_at,
            source=after.source,
        )
        self.assertEqual(after.model_dump(mode="json"), expected)
        calls = list(self.provider_calls)
        replay = await self.post(body, "set-calendar")
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(self.provider_calls, calls)
        self.assertEqual(replay.json()["original_trace_id"], applied.json()["trace_id"])
        conflict = await self.post({**body, "reason": "different review"}, "set-calendar")
        self.assertEqual(conflict.status_code, 409, conflict.text)
        clear = {**self.request_payload, "odoo_business_hours": {"operation": "clear"}}
        dry_clear = await self.post(clear)
        self.assertEqual(dry_clear.status_code, 202, dry_clear.text)
        cleared = await self.post(
            {
                **clear,
                "mode": "apply",
                "reviewed_plan_sha256": dry_clear.json()["result"]["odoo_business_hours"][
                    "plan_digest"
                ],
            },
            "clear-calendar",
        )
        self.assertEqual(cleared.status_code, 202, cleared.text)
        self.assertIsNone(
            self.store.read_product_profile_record(self.profile.product).odoo_business_hours
        )
        self.assertEqual(self.provider_calls, calls)

    async def test_live_local_operator_can_review_set_and_clear_calendar(self) -> None:
        from control_plane.service_auth import BearerIdentityConfig
        from tests.support.auth import StubVerifier, identity, local_operator_policy
        from tests.support.http import request

        policy = local_operator_policy(
            products=(self.profile.product, "launchplane"),
            contexts=("cm", "launchplane"),
            actions=(
                "product_config.plan",
                "product_config.apply",
                "product_profile.read",
                "product_profile.write",
                "operations.read",
            ),
        )
        self.app = self.make_app(
            verifier=StubVerifier(identity()),
            authz_policy=policy,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="inert-operator-token",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )
        for operation in ("set", "clear"):
            body = {**self.request_payload}
            if operation == "clear":
                body["odoo_business_hours"] = {"operation": "clear"}

            async def send(payload: dict[str, Any], key: str = "") -> Any:
                return await request(
                    self.app,
                    "POST",
                    "/v1/product-config/apply",
                    headers={
                        "Authorization": "Bearer inert-operator-token",
                        "Idempotency-Key": key,
                    },
                    payload=payload,
                )

            dry = await send(body)
            self.assertEqual(dry.status_code, 202, dry.text)
            applied = await send(
                {
                    **body,
                    "mode": "apply",
                    "reviewed_plan_sha256": dry.json()["result"]["odoo_business_hours"][
                        "plan_digest"
                    ],
                },
                "local-calendar-" + operation,
            )
            self.assertEqual(applied.status_code, 202, applied.text)
        self.assertIsNone(
            self.store.read_product_profile_record(self.profile.product).odoo_business_hours
        )

    async def test_scope_authorization_and_standalone_contract_refuse_before_provider(self) -> None:
        from tests.support.auth import StubVerifier, identity

        for missing in ("product_profile.write", "operations.read", "product_profile.read"):
            policy = self.policy.model_copy(
                update={
                    "github_actions": tuple(
                        rule.model_copy(
                            update={
                                "actions": tuple(
                                    action for action in rule.actions if action != missing
                                )
                            }
                        )
                        for rule in self.policy.github_actions
                    )
                }
            )
            self.app = self.make_app(
                verifier=StubVerifier(identity()),
                authz_policy=policy,
                record_store_factory=lambda: self.store,
                control_plane_root_path=self.root,
            )
            payload = (
                self.request_payload
                if missing != "product_profile.write"
                else {**self.request_payload, "mode": "apply", "reviewed_plan_sha256": "a" * 64}
            )
            denied = await self.post(payload, "denied")
            self.assertEqual(denied.status_code, 403, denied.text)
            self.assertFalse(self.provider_calls)
        updates: tuple[dict[str, object], ...] = (
            {"instance": "testing"},
            {"runtime_env": {}},
            {"secrets": [{"name": "secret", "value": "inert"}]},
            {"public_hosts": []},
        )
        for update in updates:
            response = await self.post({**self.request_payload, **update})
            self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse(self.provider_calls)

    async def test_references_stale_profile_and_missing_review_cannot_apply(self) -> None:
        bad = await self.post(
            {
                **self.request_payload,
                "odoo_business_hours": {
                    **self.request_payload["odoo_business_hours"],
                    "expected_calendar_id": 18,
                },
            }
        )
        self.assertEqual(bad.status_code, 503, bad.text)
        dry = await self.post(self.request_payload)
        self.assertEqual(dry.status_code, 202, dry.text)
        body = {
            **self.request_payload,
            "mode": "apply",
            "reviewed_plan_sha256": dry.json()["result"]["odoo_business_hours"]["plan_digest"],
        }
        missing_key = await self.post(body)
        self.assertEqual(missing_key.status_code, 400, missing_key.text)
        changed = self.profile.model_copy(update={"source": "another-editor"})
        self.store.write_product_profile_record(changed)
        stale = await self.post(body, "stale-calendar")
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), changed)

    async def test_raw_writer_cannot_set_clear_or_initially_register_policy(self) -> None:
        from tests.support.http import request

        for profile in (
            self.profile.model_copy(update={"odoo_business_hours": None}),
            self.profile.model_copy(update={"product": "new-product"}),
        ):
            response = await request(
                self.app,
                "POST",
                "/v1/product-profiles",
                headers={"Authorization": "Bearer valid-token"},
                payload=profile.model_dump(mode="json"),
            )
            self.assertEqual(
                response.status_code,
                409 if profile.product == self.profile.product else 403,
                response.text,
            )
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)

    async def test_generic_profile_write_cannot_overwrite_a_concurrent_policy_apply(self) -> None:
        from tests.support.http import request
        from control_plane.contracts.odoo_business_hours import OdooBusinessHoursPolicy
        from control_plane.contracts.product_profile_record import (
            validate_product_profile_history_transition,
        )

        concurrent = self.profile.model_copy(
            update={
                "odoo_business_hours": OdooBusinessHoursPolicy(company_id=8),
                "source": "service:reviewed-calendar",
            }
        )

        def race(**kwargs: Any) -> None:
            result = self.store.compare_and_write_product_profile_record(
                expected_record=self.profile, replacement_record=concurrent
            )
            self.assertEqual(result.status, "written")
            validate_product_profile_history_transition(**kwargs)

        with patch(
            "control_plane.http_app.validate_product_profile_history_transition", side_effect=race
        ):
            response = await request(
                self.app,
                "POST",
                "/v1/product-profiles",
                headers={"Authorization": "Bearer valid-token"},
                payload=self.profile.model_dump(mode="json"),
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), concurrent)

    def test_absent_profile_bundle_cannot_overwrite_a_newly_configured_profile(self) -> None:
        from control_plane.storage.product_authority_bundle import (
            ProductAuthorityBundle,
            ProductProfileConflictError,
        )

        absent = self.profile.model_copy(
            update={"product": "new-product", "odoo_business_hours": None}
        )
        creation = ProductAuthorityBundle(
            product_profiles=(absent,), absent_product_ids=(absent.product,)
        )
        self.store.write_product_authority_bundle(creation)
        configured = absent.model_copy(
            update={"odoo_business_hours": self.profile.odoo_business_hours}
        )
        self.assertEqual(
            self.store.compare_and_write_product_profile_record(
                expected_record=absent, replacement_record=configured
            ).status,
            "written",
        )
        with self.assertRaises(ProductProfileConflictError):
            self.store.write_product_authority_bundle(creation)
        self.assertEqual(self.store.read_product_profile_record(absent.product), configured)

    async def test_set_requires_same_identity_review_and_binds_reason_source_and_reference(
        self,
    ) -> None:
        from tests.support.auth import StubVerifier, identity

        dry = await self.post(self.request_payload)
        self.assertEqual(dry.status_code, 202, dry.text)
        body = {
            **self.request_payload,
            "mode": "apply",
            "reviewed_plan_sha256": dry.json()["result"]["odoo_business_hours"]["plan_digest"],
        }
        for change in (
            {"reason": "Different reviewed reason"},
            {"source_label": "other-source"},
            {"reviewed_plan_sha256": "0" * 64},
        ):
            refusal = await self.post({**body, **change}, "changed")
            self.assertEqual(refusal.status_code, 409, refusal.text)
        self.app = self.make_app(
            verifier=StubVerifier(replace(identity(), subject="another-subject")),
            authz_policy=self.policy,
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )
        refusal = await self.post(body, "other-actor")
        self.assertEqual(refusal.status_code, 409, refusal.text)

    async def test_clock_progress_does_not_change_digest_and_endpoint_change_does(self) -> None:
        from datetime import timedelta

        with patch("control_plane.odoo_business_hours_config.datetime") as clock:
            clock.now.return_value = self.now
            first = await self.post(self.request_payload)
            clock.now.return_value = self.now + timedelta(minutes=1)
            second = await self.post(self.request_payload)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 202, second.text)
        digest = first.json()["result"]["odoo_business_hours"]["plan_digest"]
        self.assertEqual(digest, second.json()["result"]["odoo_business_hours"]["plan_digest"])
        self.store.write_private_health_endpoint_record(
            self.endpoint.model_copy(update={"updated_at": "2026-10-22T16:00:00Z"})
        )
        changed = await self.post(
            {**self.request_payload, "mode": "apply", "reviewed_plan_sha256": digest},
            "endpoint-changed",
        )
        self.assertEqual(changed.status_code, 409, changed.text)

    async def test_commit_race_preserves_the_concurrent_profile_and_no_success_receipt(
        self,
    ) -> None:
        from control_plane.http_routes.mutation_support import idempotency_scope
        from tests.support.auth import identity

        dry = await self.post(self.request_payload)
        self.assertEqual(dry.status_code, 202, dry.text)
        body = {
            **self.request_payload,
            "mode": "apply",
            "reviewed_plan_sha256": dry.json()["result"]["odoo_business_hours"]["plan_digest"],
        }
        original = self.store.write_product_authority_bundle
        raced = self.profile.model_copy(update={"display_name": "Concurrent name"})

        def commit_race(bundle: Any) -> None:
            if bundle.product_profiles:
                self.store.write_product_profile_record(raced)
            original(bundle)

        with patch.object(self.store, "write_product_authority_bundle", side_effect=commit_race):
            response = await self.post(body, "racing-policy")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), raced)
        self.assertIsNone(
            self.store.read_idempotency_record(
                scope=idempotency_scope(identity()),
                route_path="/v1/product-config/apply",
                idempotency_key="racing-policy",
            )
        )

    async def test_endpoint_foreign_disabled_or_rebound_fails_safely(self) -> None:
        for change in ({"product": "other-site"}, {"status": "disabled"}, {"instance": "testing"}):
            self.store.write_private_health_endpoint_record(self.endpoint.model_copy(update=change))
            response = await self.post(self.request_payload)
            self.assertEqual(response.status_code, 503, response.text)
            self.assertFalse(self.rpc_calls)
        self.store.write_private_health_endpoint_record(self.endpoint)
        original = self.http

        def rebound(opener: object, request: Any) -> object:
            response = cast(dict[str, Any], original(opener, request))
            if request.full_url.endswith("/web/session/destroy"):
                self.store.write_private_health_endpoint_record(
                    self.endpoint.model_copy(update={"status": "disabled"})
                )
            return response

        with patch("control_plane.odoo_runtime_reads._http_json", side_effect=rebound):
            response = await self.post(self.request_payload)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn("inert-password", response.text)
        self.assertNotIn("odoo.internal", response.text)

    async def test_inspect_scope_and_private_labels_are_explicit(self) -> None:
        original = self.http

        def unsafe_label(opener: object, request: Any) -> object:
            response = cast(dict[str, Any], original(opener, request))
            if request.data is not None:
                params = json.loads(request.data)["params"]
                if params.get("model") == "res.company":
                    response["result"][0]["name"] = "https://private.example.invalid/token=value"
            return response

        body = {
            **self.request_payload,
            "odoo_business_hours": {
                "operation": "inspect",
                "private_endpoint_key": "calendar-prod",
            },
        }
        with patch("control_plane.odoo_runtime_reads._http_json", side_effect=unsafe_label):
            response = await self.post(body)
        self.assertEqual(response.status_code, 202, response.text)
        result = response.json()["result"]["odoo_business_hours"]
        self.assertEqual(result["permitted_company_ids"], [7])
        self.assertTrue(result["companies"][0]["label_omitted"])
        self.assertEqual(result["companies"][0]["label"], "")
        self.assertNotIn("private.example.invalid", response.text)

        def missing_scope(opener: object, request: Any) -> object:
            response = cast(dict[str, Any], original(opener, request))
            if request.full_url.endswith("authenticate"):
                response["result"].pop("user_companies")
            return response

        with patch("control_plane.odoo_runtime_reads._http_json", side_effect=missing_scope):
            unavailable = await self.post(body)
        self.assertEqual(unavailable.status_code, 503, unavailable.text)

    async def test_human_session_requires_csrf_for_existing_config_route(self) -> None:
        from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
        from tests.http_app_test_support import (
            _github_oauth_config,
            _github_human_identity,
            _github_human_product_config_policy,
            _browser_mutation_headers,
            _RejectingVerifier,
        )
        from tests.support.http import request

        sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        session = sessions.issue(_github_human_identity())
        policy = _github_human_product_config_policy(
            actions=("product_config.plan", "product_profile.read", "operations.read"),
            product=self.profile.product,
            context="cm",
        )
        policy = policy.model_copy(
            update={
                "github_humans": tuple(
                    rule.model_copy(
                        update={
                            "products": (self.profile.product, "launchplane"),
                            "contexts": ("cm", "launchplane"),
                        }
                    )
                    for rule in policy.github_humans
                )
            }
        )
        app = self.make_app(
            verifier=_RejectingVerifier(),
            authz_policy=policy,
            human_session_manager=sessions,
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )
        denied = await request(
            app,
            "POST",
            "/v1/product-config/apply",
            headers={"Cookie": sessions.session_cookie_header(session)},
            payload=self.request_payload,
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertFalse(self.provider_calls)
        dry = await request(
            app,
            "POST",
            "/v1/product-config/apply",
            headers=_browser_mutation_headers(sessions, session),
            payload=self.request_payload,
        )
        self.assertEqual(dry.status_code, 202, dry.text)

    async def test_initial_policy_registration_is_refused_with_matching_profile_authority(
        self,
    ) -> None:
        from tests.support.auth import StubVerifier, identity
        from tests.support.http import request

        policy = self.policy.model_copy(
            update={
                "github_actions": tuple(
                    rule.model_copy(
                        update={"products": (self.profile.product, "new-product", "launchplane")}
                    )
                    for rule in self.policy.github_actions
                )
            }
        )
        self.app = self.make_app(
            verifier=StubVerifier(identity()),
            authz_policy=policy,
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )
        initial = self.profile.model_copy(update={"product": "new-product"})
        response = await request(
            self.app,
            "POST",
            "/v1/product-profiles",
            headers={"Authorization": "Bearer valid-token"},
            payload=initial.model_dump(mode="json"),
        )
        self.assertEqual(response.status_code, 409, response.text)
        with self.assertRaises(FileNotFoundError):
            self.store.read_product_profile_record(initial.product)

    async def test_same_key_generic_profile_commit_race_replays_atomic_receipt(self) -> None:
        from control_plane.storage.product_authority_bundle import ProductProfileConflictError
        from tests.support.http import request

        original = self.store.write_product_authority_bundle
        replacement = self.profile.model_copy(update={"source": "reviewed-unrelated-update"})

        def late_original(bundle: Any) -> None:
            original(bundle)
            raise ProductProfileConflictError(
                "A same-key original completed before this stale writer."
            )

        with patch.object(self.store, "write_product_authority_bundle", side_effect=late_original):
            response = await request(
                self.app,
                "POST",
                "/v1/product-profiles",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "same-key-profile",
                },
                payload=replacement.model_dump(mode="json"),
            )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(response.json()["replayed"])
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), replacement)


class CalendarBundleAgreementTests(unittest.TestCase):
    def test_absence_and_existing_expectations_preserve_calendar_for_both_stores(self) -> None:
        from control_plane.storage.filesystem import FilesystemRecordStore
        from control_plane.storage.product_authority_bundle import (
            ProductAuthorityBundle,
            ProductProfileConflictError,
        )
        from tests.test_product_display_name import profile
        from control_plane.contracts.odoo_business_hours import OdooBusinessHoursPolicy

        for database in (False, True):
            with self.subTest(database=database), TemporaryDirectory() as directory:
                root = Path(directory)
                store = (
                    PostgresRecordStore(database_url=sqlite_database_url(root / "records.sqlite"))
                    if database
                    else FilesystemRecordStore(root)
                )
                if isinstance(store, PostgresRecordStore):
                    store.ensure_schema()
                    self.addCleanup(store.close)
                before = profile()
                creation = ProductAuthorityBundle(
                    product_profiles=(before,), absent_product_ids=(before.product,)
                )
                store.write_product_authority_bundle(creation)
                configured = before.model_copy(
                    update={"odoo_business_hours": OdooBusinessHoursPolicy(company_id=7)}
                )
                store.write_product_authority_bundle(
                    ProductAuthorityBundle(
                        product_profiles=(configured,), expected_product_profiles=(before,)
                    )
                )
                for stale in (
                    creation,
                    ProductAuthorityBundle(
                        product_profiles=(before,), expected_product_profiles=(before,)
                    ),
                ):
                    with self.assertRaises(ProductProfileConflictError):
                        store.write_product_authority_bundle(stale)
                self.assertEqual(store.read_product_profile_record(before.product), configured)

    def test_onboarding_preserves_selection_and_refuses_both_stale_plan_kinds(self) -> None:
        from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
        from control_plane.contracts.odoo_business_hours import OdooBusinessHoursPolicy
        from control_plane.storage.product_authority_bundle import ProductProfileConflictError
        from control_plane.workflows.product_onboarding import (
            plan_product_onboarding_authority_bundle,
        )
        from tests.test_product_onboarding import _manifest_payload

        for exists in (False, True):
            with self.subTest(exists=exists), TemporaryDirectory() as directory:
                store = PostgresRecordStore(
                    database_url=sqlite_database_url(Path(directory) / "db.sqlite")
                )
                self.addCleanup(store.close)
                store.ensure_schema()
                payload = _manifest_payload()
                payload["driver_id"] = "odoo"
                manifest = ProductOnboardingManifest.model_validate(payload)
                initial, initial_bundle = plan_product_onboarding_authority_bundle(
                    record_store=store, manifest=manifest
                )
                if exists:
                    store.write_product_authority_bundle(initial_bundle)
                _, stale = plan_product_onboarding_authority_bundle(
                    record_store=store, manifest=manifest
                )
                if not exists:
                    store.write_product_authority_bundle(initial_bundle)
                configured = initial.product_profile.model_copy(
                    update={"odoo_business_hours": OdooBusinessHoursPolicy(company_id=7)}
                )
                store.write_product_profile_record(configured)
                with self.assertRaises(ProductProfileConflictError):
                    store.write_product_authority_bundle(stale)
                self.assertEqual(store.read_product_profile_record(manifest.product), configured)
                refreshed, _ = plan_product_onboarding_authority_bundle(
                    record_store=store, manifest=manifest
                )
                self.assertEqual(
                    refreshed.product_profile.odoo_business_hours, configured.odoo_business_hours
                )

    def test_generic_planner_refuses_service_calendar_section(self) -> None:
        from control_plane.product_config import (
            ProductConfigError,
            ProductConfigStore,
            plan_product_config_authority_bundle,
        )

        with self.assertRaises(ProductConfigError) as refusal:
            plan_product_config_authority_bundle(
                record_store=cast(ProductConfigStore, object()),
                payload={"odoo_business_hours": {"operation": "clear"}},
                mode="dry-run",
                actor="test",
                source_label="test",
            )
        self.assertEqual(refusal.exception.code, "odoo_business_hours_service_required")
