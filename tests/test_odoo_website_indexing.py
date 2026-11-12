from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from control_plane.provider_operations import ProviderOperationLease
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import Mock, patch, create_autospec

from control_plane.odoo_website_indexing import (
    WebsiteIndexingAdapter,
    WebsiteIndexingExecution,
    WebsiteIndexingRequest,
)
from tests.test_product_topology_read_model import (
    _TopologyStore as TopologyStore,
    _route_binding as route_binding,
    _profile as product_profile,
    _provider_target as provider_target,
    _runtime_identity as runtime_identity,
)

import unittest

from pydantic import ValidationError

from control_plane.contracts.dokploy_target_record import DokployTargetRecord, PublicHostEffect
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

    def test_indexing_shell_never_runs_startup_credential_preparation(self) -> None:
        import os
        import sys
        from control_plane._odoo_website_settings_program import WEBSITE_INDEXING_PROGRAM

        preparation = Mock(side_effect=AssertionError("credential preparation invoked"))
        startup = SimpleNamespace(
            _load_settings=lambda arguments: arguments,
            _write_runtime_config=lambda settings: None,
            _run_odoo_shell=preparation,
            _build_odoo_shell_command=lambda settings: ["/odoo/odoo-bin", "shell", "--no-http"],
            _odoo_environment=lambda settings: {"PGPASSWORD": "synthetic-only"},
        )
        with (
            patch.dict(sys.modules, {"run_odoo_startup": startup}),
            patch.object(sys, "argv", ["program", "fixture-db", "{}"]),
            patch.object(sys, "path", list(sys.path)),
            patch.dict(os.environ, {"ODOO_COPY_PURPOSE": "testing-copy"}),
            patch("subprocess.run") as shell,
        ):
            exec(WEBSITE_INDEXING_PROGRAM, {})
        preparation.assert_not_called()
        shell.assert_called_once()
        self.assertEqual(shell.call_args.kwargs["env"]["PGPASSWORD"], "synthetic-only")
        self.assertIsInstance(shell.call_args.kwargs["input"], bytes)


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
        self.profile.image.repository = "ghcr.io/example/odoo"
        self.target = provider_target().model_copy(update={"provider_evidence": {}})
        self.dokploy_target = DokployTargetRecord(
            context="example-context",
            instance="prod",
            target_name="example-site-prod",
            public_hosts=("example.test",),
            updated_at=self.target.updated_at,
        )
        self.database_url = "sqlite://"

    def read_product_profile_record(self, product: str) -> Any:
        if product != self.profile.product:
            raise FileNotFoundError(product)
        return self.profile

    def list_product_profile_records(self, **_kwargs: Any) -> Any:
        return (self.profile,)

    def read_provider_target_record(self, **_kwargs: Any) -> Any:
        return self.target

    def read_dokploy_target_record(self, **_kwargs: Any) -> Any:
        return self.dokploy_target

    @staticmethod
    def read_dokploy_target_id_record(**_kwargs: Any) -> Any:
        from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord

        return DokployTargetIdRecord(
            context="example-context",
            instance="prod",
            target_id="provider-target-private-123",
            updated_at="2026-07-14T10:25:00Z",
        )

    @staticmethod
    def read_release_tuple_record(**_kwargs: Any) -> Any:
        return SimpleNamespace(artifact_id="artifact-1")

    @staticmethod
    def read_artifact_manifest(_artifact_id: str) -> Any:
        return SimpleNamespace(
            image=SimpleNamespace(repository="ghcr.io/example/odoo", digest="sha256:" + "1" * 64),
            source_commit="refs/heads/main",
        )


class WebsiteIndexingBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = IndexingStore()
        self.runtime = runtime_identity(
            image_reference="ghcr.io/example/odoo@sha256:" + "1" * 64
        ).model_copy(update={"database_uuid": "synthetic-database"})
        self.payload = {
            "composeId": self.store.target.target_id,
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
                "database_uuid": "synthetic-database",
            },
        }

    def test_current_binding_produces_reviewed_native_apply_without_maintenance(self) -> None:
        plan = self.execution.observe()
        self.assertEqual(self.writes, 0)
        applied = self.execution.observe(reviewed=plan)
        self.assertTrue(applied["committed"])
        self.assertEqual(self.writes, 1)
        self.assertEqual(applied["native"], plan["native"])

    def test_missing_stale_or_disabled_route_never_reaches_native_execution(
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
        ]
        for route in variants:
            with self.subTest(route=route):
                self.store.route_binding = route
                with self.assertRaises(ValueError):
                    self.execution.observe()
        self.native.assert_not_called()

    def _route_serving(self, host: str) -> Any:
        route = self.store.route_binding
        assert route is not None
        return route.model_copy(
            update={"domains": (route.domains[0].model_copy(update={"domain_name": host}),)}
        )

    def test_public_host_origin_beside_an_internal_route_domain_reaches_the_native_plan(
        self,
    ) -> None:
        self.store.route_binding = self._route_serving("example-prod.internal.test")
        self.store.dokploy_target = self.store.dokploy_target.model_copy(
            update={"domains": ("example-prod.internal.test",)}
        )
        plan = self.execution.observe()
        self.assertEqual(plan["native"]["public_origin"], "https://example.test")
        self.native.assert_called_once()
        self.assertEqual(self.writes, 0)

    def test_origin_outside_route_domains_and_committed_public_hosts_never_reaches_native(
        self,
    ) -> None:
        self.store.route_binding = self._route_serving("other.example")
        pending = PublicHostEffect(
            host="example.test",
            provider_host="https://provider.example",
            target_id=self.store.target.target_id,
            runtime_port=8069,
        )
        recorded = self.store.dokploy_target
        for dokploy_target in (
            recorded.model_copy(update={"public_hosts": ()}),
            recorded.model_copy(update={"pending_public_host_effects": (pending,)}),
        ):
            with self.subTest(dokploy_target=dokploy_target):
                self.store.dokploy_target = dokploy_target
                with self.assertRaisesRegex(ValueError, "route and provider target differ"):
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

    def test_refreshed_authority_preserves_reviewed_identity_but_stale_authority_refuses(
        self,
    ) -> None:
        plan = self.execution.observe()
        route = self.store.route_binding
        assert route is not None
        now = datetime.now(timezone.utc)
        self.store.route_binding = route.model_copy(
            update={
                "source": route.source.model_copy(
                    update={
                        "refreshed_at": now.isoformat(),
                        "stale_after": (now + timedelta(minutes=20)).isoformat(),
                    }
                )
            }
        )
        self.store.profile = self.store.profile.model_copy(
            update={"updated_at": now.isoformat(), "display_name": "New name"}
        )
        self.store.target = self.store.target.model_copy(update={"updated_at": now.isoformat()})
        self.assertEqual(self.execution.observe()["binding_sha256"], plan["binding_sha256"])
        adapter = WebsiteIndexingAdapter(self.execution, plan, "synthetic")
        before = adapter.observe("original", "website-indexing", adapter.reconciliation_key())
        self.assertEqual(before.outcome, "absent")
        self.assertTrue(before.retry_safe)
        original_native = self.native_result

        def after(**kwargs: Any) -> dict[str, Any]:
            result = original_native(**kwargs)
            result["native"].update(
                before_sha256="b" * 64, changed=False, managed_noindex_before=True
            )
            return result

        self.native.side_effect = after
        self.assertEqual(
            adapter.observe("original", "website-indexing", adapter.reconciliation_key()).outcome,
            "present",
        )
        self.store.route_binding = route.model_copy(
            update={
                "source": route.source.model_copy(
                    update={"stale_after": (now - timedelta(seconds=1)).isoformat()}
                )
            }
        )
        self.assertEqual(
            adapter.observe("original", "website-indexing", adapter.reconciliation_key()).outcome,
            "unknown",
        )
        self.assertEqual(self.writes, 0)

    def test_native_database_uuid_binds_ordinary_runtime_without_injected_uuid(self) -> None:
        runtime = self.runtime.model_copy(update={"database_uuid": ""})
        self.payload["env"] = (
            "ODOO_DB_NAME=example\nLAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + runtime.model_dump_json()
        )
        plan = self.execution.observe()
        self.assertEqual(plan["database_uuid"], "synthetic-database")
        self.execution.observe(reviewed=plan)
        self.assertEqual(
            self.native.call_args.kwargs["request"]["database_uuid"], plan["database_uuid"]
        )

    def test_changed_target_id_refuses_before_native_execution(self) -> None:
        self.store.target = self.store.target.model_copy(update={"target_id": "foreign-target"})
        with self.assertRaisesRegex(ValueError, "target records differ"):
            self.execution.observe()
        self.native.assert_not_called()

    def test_wrong_artifact_image_refuses_before_native_execution(self) -> None:
        self.payload["env"] = self.payload["env"].replace(
            "sha256:" + "1" * 64, "sha256:" + "2" * 64
        )
        with self.assertRaisesRegex(ValueError, "artifact authority"):
            self.execution.observe()
        self.native.assert_not_called()

    def test_interrupted_native_result_reconciles_before_after_or_unknown_without_writes(
        self,
    ) -> None:
        from control_plane.odoo_website_indexing import WebsiteIndexingUnknown

        plan = self.execution.observe()
        adapter = WebsiteIndexingAdapter(self.execution, plan, "synthetic")
        for outcome in ("refused", "committed", "diverged"):
            with self.subTest(outcome=outcome):

                def interrupted(
                    *,
                    request: Any,
                    before_mutation: Callable[[], None] | None = None,
                    **kwargs: Any,
                ) -> dict[str, Any]:
                    if request["mode"] == "apply":
                        assert before_mutation is not None
                        before_mutation()
                        return {"status": "refused" if outcome == "refused" else "unknown"}
                    result = self.native_result(request=request, **kwargs)
                    if outcome != "refused":
                        result["native"].update(
                            before_sha256=("b" if outcome == "committed" else "c") * 64,
                            changed=outcome != "committed",
                            managed_noindex_before=outcome == "committed",
                        )
                    return result

                self.native.side_effect = interrupted
                lease = create_autospec(ProviderOperationLease, instance=True)
                with self.assertRaises(WebsiteIndexingUnknown):
                    adapter.apply("original", lease)
                lease.checkpoint_effect.assert_called_once()
                observed = adapter.observe(
                    "original", "website-indexing", adapter.reconciliation_key()
                )
                self.assertEqual(
                    observed.outcome,
                    {"refused": "absent", "committed": "present", "diverged": "unknown"}[outcome],
                )
                self.assertEqual(observed.retry_safe, outcome == "refused")
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
            "database_uuid": "synthetic-database",
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
