from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import Mock, patch

from control_plane.odoo_website_indexing import WebsiteIndexingExecution, WebsiteIndexingRequest
from tests.test_product_topology_read_model import (
    _TopologyStore as TopologyStore,
    _route_binding as route_binding,
    _profile as product_profile,
    _provider_target as provider_target,
    _runtime_identity as runtime_identity,
)

import unittest

from pydantic import ValidationError

from control_plane.contracts.odoo_post_deploy_payload import OdooPostDeployPayload


class WebsiteIndexingPayloadTests(unittest.TestCase):
    def test_production_payload_carries_only_the_native_selector_and_authoritative_state(
        self,
    ) -> None:
        indexing = {
            "website_id": 7,
            "public_origin": "https://example.test",
            "production_use": "prelaunch",
        }
        payload = OdooPostDeployPayload.model_validate(
            {"context": "example", "instance": "prod", "website_indexing": indexing}
        )
        self.assertEqual(payload.to_wire_dict()["website_indexing"], indexing)
        with self.assertRaises(ValidationError):
            OdooPostDeployPayload.model_validate(
                {"context": "example", "instance": "testing", "website_indexing": indexing}
            )
        for field, value in (
            ("website_id", True),
            ("production_use", "unknown"),
            ("public_origin", "https://example.test/other"),
        ):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                OdooPostDeployPayload.model_validate(
                    {
                        "context": "example",
                        "instance": "prod",
                        "website_indexing": {**indexing, field: value},
                    }
                )


class IndexingStore(TopologyStore):
    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        route = route_binding()
        route = route.model_copy(
            update={
                "source": route.source.model_copy(
                    update={
                        "refreshed_at": now.isoformat(),
                        "stale_after": (now + timedelta(minutes=10)).isoformat(),
                    }
                )
            }
        )
        super().__init__(route_binding=route)
        self.profile = product_profile(driver_id="odoo").model_copy(
            update={"production_use": "prelaunch"}
        )
        self.target = provider_target()
        self.database_url = "sqlite://"

    def read_product_profile_record(self, product: str) -> Any:
        if product != self.profile.product:
            raise FileNotFoundError(product)
        return self.profile

    def list_product_profile_records(self, **_kwargs: Any) -> Any:
        return (self.profile,)

    def read_provider_target_record(self, **_kwargs: Any) -> Any:
        return self.target

    @staticmethod
    def read_dokploy_target_record(**_kwargs: Any) -> Any:
        return SimpleNamespace(public_hosts=("example.test",))

    @staticmethod
    def read_release_tuple_record(**_kwargs: Any) -> Any:
        return SimpleNamespace(artifact_id="artifact-1")


class WebsiteIndexingBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = IndexingStore()
        self.runtime = runtime_identity(
            image_reference="ghcr.io/example/odoo@sha256:" + "1" * 64
        ).model_copy(update={"database_uuid": "synthetic-database"})
        self.payload = {
            "appName": "example-prod",
            "serverId": "synthetic-server",
            "env": "ODOO_DB_NAME=example\nLAUNCHPLANE_RUNTIME_IDENTITY_JSON="
            + self.runtime.model_dump_json(),
        }
        self.enterContext(
            patch(
                "control_plane.odoo_website_indexing.source.read_dokploy_config",
                return_value=("https://provider.example", "synthetic-token"),
            )
        )
        self.provider = self.enterContext(
            patch(
                "control_plane.odoo_website_indexing.api.fetch_dokploy_target_payload",
                side_effect=lambda **kwargs: self.payload.copy(),
            )
        )
        self.native = self.enterContext(
            patch(
                "control_plane.odoo_website_indexing.run_native_indexing",
                side_effect=self.native_result,
            )
        )
        self.execution = WebsiteIndexingExecution(
            self.store,
            Path("/synthetic"),
            "example-site",
            "example-context",
            WebsiteIndexingRequest(mode="dry-run", public_origin="https://example.test"),
        )
        self.writes = 0

    def native_result(
        self, *, request: Any, before_mutation: Callable[[], None] | None = None, **_kwargs: Any
    ) -> dict[str, Any]:
        if before_mutation:
            before_mutation()
            self.writes += 1
        return {
            "status": "complete",
            "native": {
                **request["indexing"],
                "website_id": 7,
                "field": "custom_code_head",
                "before_sha256": "a" * 64,
                "after_sha256": "b" * 64,
                "changed": True,
                "managed_noindex_before": False,
                "managed_noindex_after": True,
                "committed": request["mode"] == "apply",
            },
        }

    def test_current_binding_produces_reviewed_native_apply_without_maintenance(self) -> None:
        plan = self.execution.observe()
        self.assertEqual(self.writes, 0)
        applied = self.execution.observe(reviewed=plan)
        self.assertTrue(applied["committed"])
        self.assertEqual(self.writes, 1)
        self.assertEqual(applied["native"], plan["native"])

    def test_missing_stale_disabled_or_wrong_host_route_never_reaches_native_execution(
        self,
    ) -> None:
        original = self.store.route_binding
        assert original is not None
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        variants = [
            None,
            original.model_copy(update={"status": "disabled"}),
            original.model_copy(
                update={"source": original.source.model_copy(update={"stale_after": expired})}
            ),
            route_binding(domain_name="other.example"),
        ]
        for route in variants:
            with self.subTest(route=route):
                self.store.route_binding = route
                with self.assertRaises(ValueError):
                    self.execution.observe()
        self.native.assert_not_called()

    def test_mismatched_product_runtime_and_changed_review_binding_refuse_before_write(
        self,
    ) -> None:
        plan = self.execution.observe()
        self.execution.request.website_id = 8
        with self.assertRaisesRegex(ValueError, "website differs"):
            self.execution.observe(reviewed=plan)
        self.execution.request.website_id = None
        self.payload["env"] = self.payload["env"].replace(
            '"product":"example-site"', '"product":"unrelated-site"'
        )
        with self.assertRaisesRegex(ValueError, "exact production runtime"):
            self.execution.observe(reviewed=plan)
        self.assertEqual(self.writes, 0)

    def test_route_change_between_plan_and_dispatch_refuses_before_effect_checkpoint(self) -> None:
        plan = self.execution.observe()
        original = self.native.side_effect

        def changed(**kwargs: Any) -> Any:
            self.store.route_binding = None
            return original(**kwargs)

        self.native.side_effect = changed
        checkpoint = Mock()
        with self.assertRaisesRegex(ValueError, "route authority"):
            self.execution.observe(reviewed=plan, before_mutation=checkpoint)
        checkpoint.assert_not_called()
        self.assertEqual(self.writes, 0)


class WebsiteIndexingRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_indexing_route_requires_authority_and_replays_without_broad_maintenance(
        self,
    ) -> None:
        from tempfile import TemporaryDirectory
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.odoo_post_deploy_http import ODOO_POST_DEPLOY_ROUTE
        from control_plane.storage.postgres import PostgresRecordStore
        from control_plane.service_auth import LaunchplaneAuthzPolicy
        from control_plane.contracts.canonical_json import canonical_json_sha256
        from tests.support.auth import _identity, _StubVerifier
        from tests.support.http import request as http_request

        directory = self.enterContext(TemporaryDirectory())
        root = Path(directory)
        store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{root / 'records.sqlite3'}")
        self.addCleanup(store.close)
        store.ensure_schema()
        store.write_product_profile_record(IndexingStore().profile)
        identity = _identity(
            repository="example/control",
            workflow_ref="example/control/.github/workflows/operator.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        actions = ["product_profile.read"]

        def app() -> Any:
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": identity.repository,
                            "workflow_refs": [identity.workflow_ref],
                            "event_names": ["workflow_dispatch"],
                            "products": ["example-site"],
                            "contexts": ["example-context"],
                            "actions": actions,
                        }
                    ]
                }
            )
            return create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

        headers = {"Authorization": "Bearer valid-token"}
        payload: dict[str, Any] = {
            "product": "example-site",
            "post_deploy": {
                "context": "example-context",
                "instance": "prod",
                "website_indexing": {"mode": "dry-run", "public_origin": "https://example.test"},
            },
        }
        denied = await http_request(
            app(), "POST", ODOO_POST_DEPLOY_ROUTE, headers=headers, payload=payload
        )
        self.assertEqual(denied.status_code, 403)
        actions[:] = ["product_config.plan", "product_config.apply"]
        plan: dict[str, Any] = {
            "binding_sha256": "f" * 64,
            "native": {
                "website_id": 7,
                "public_origin": "https://example.test",
                "production_use": "prelaunch",
                "field": "custom_code_head",
                "before_sha256": "a" * 64,
                "after_sha256": "b" * 64,
                "changed": True,
                "managed_noindex_before": False,
                "managed_noindex_after": True,
            },
        }
        plan = {**plan, "plan_sha256": canonical_json_sha256(plan), "committed": False}
        writes = []

        def native(
            _execution: WebsiteIndexingExecution,
            *,
            reviewed: Any = None,
            before_mutation: Callable[[], None] | None = None,
        ) -> dict[str, Any]:
            if reviewed is not None:
                assert before_mutation is not None
                before_mutation()
                writes.append(reviewed)
            return {**plan, "committed": reviewed is not None}

        with (
            patch.object(WebsiteIndexingExecution, "observe", native),
            patch(
                "control_plane.http_app.execute_odoo_post_deploy_result",
                side_effect=AssertionError("Broad maintenance must not run"),
            ),
        ):
            application = app()
            dry = await http_request(
                application, "POST", ODOO_POST_DEPLOY_ROUTE, headers=headers, payload=payload
            )
            self.assertEqual(dry.status_code, 202, dry.text)
            payload["post_deploy"].update(reason="Apply reviewed prelaunch protection")
            payload["post_deploy"]["website_indexing"].update(
                mode="apply", reviewed_plan=dry.json()["result"]
            )
            missing = await http_request(
                application, "POST", ODOO_POST_DEPLOY_ROUTE, headers=headers, payload=payload
            )
            self.assertEqual(missing.status_code, 400)
            for _ in range(2):
                applied = await http_request(
                    application,
                    "POST",
                    ODOO_POST_DEPLOY_ROUTE,
                    headers={**headers, "Idempotency-Key": "indexing-original"},
                    payload=payload,
                )
                self.assertEqual(applied.status_code, 202, applied.text)
                self.assertTrue(applied.json()["result"]["committed"])
            self.assertEqual(len(writes), 1)
