import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from httpx2 import Response

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    route_binding_record_sha256,
)
from control_plane.http_app import create_launchplane_fastapi_app, idempotency_scope
from control_plane.route_binding_external_reconcile import (
    ExternalRouteBindingReconcileRequest,
    plan_external_route_binding_reconcile,
)
from control_plane.route_binding_reconcile import (
    RouteBindingExpectedCurrent,
    RouteBindingReconcileRequest,
    build_route_binding_candidate,
)
from control_plane.route_binding_refresh_controller import (
    RouteBindingRefreshTargetInvariantError,
    RouteBindingRefreshTargetLimitExceeded,
    discover_active_odoo_testing_route_bindings,
    discover_remaining_odoo_stable_route_bindings,
    plan_remaining_odoo_stable_route_binding_refresh,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _asgi_request
from tests.support.auth import _identity, _StubVerifier
from tests.test_external_route_binding_reconcile import _profile as external_profile
from tests.test_external_route_binding_reconcile import _provider_target as external_target
from tests.test_route_binding_record import (
    _dokploy_target_id_record,
    _dokploy_target_record,
    _ingress_audit_record,
    _provider_target_record,
)
from tests.test_route_binding_refresh_controller import (
    _profile,
    _sqlite_database_url,
    _Store,
    _write_refresh_fixture,
)

ROUTE = "/v1/route-bindings/odoo-stable/controller/run-once"


def _fixtures(store: PostgresRecordStore) -> None:
    _write_refresh_fixture(store)
    store.write_provider_target_record(
        _provider_target_record(context="example-prod", instance="prod").model_copy(
            update={"target_id": "production-compose-id"}
        )
    )
    store.write_dokploy_target_record(
        _dokploy_target_record(context="example-prod", instance="prod")
    )
    store.write_dokploy_target_id_record(
        _dokploy_target_id_record(
            context="example-prod", instance="prod", target_id="production-compose-id"
        )
    )
    store.write_ingress_route_audit_record(
        _ingress_audit_record(context="example-prod", record_id="prod-audit")
    )
    prod = _binding(store)
    store.write_route_binding_record(
        prod.model_copy(
            update={
                "ingress": prod.ingress.model_copy(
                    update={"provider_evidence": {"audit_record": "prod-audit"}}
                )
            }
        )
    )
    # External authority has its own profile/target evidence and no proxy claim.
    profile = external_profile()
    lane = profile.lanes[0].model_copy(
        update={"instance": "testing", "context": "external-testing"}
    )
    profile = profile.model_copy(update={"product": "external-product", "lanes": (lane,)})
    target = external_target().model_copy(
        update={
            "instance": "testing",
            "context": "external-testing",
            "target_id": "external-compose-id",
        }
    )
    store.write_product_profile_record(profile)
    store.write_provider_target_record(target)
    plan = plan_external_route_binding_reconcile(
        record_store=store,
        request=ExternalRouteBindingReconcileRequest(
            product=profile.product,
            context=lane.context,
            instance=lane.instance,
            expected_current=RouteBindingExpectedCurrent(state="absent"),
            evaluated_at="2026-07-22T00:00:00Z",
        ),
    )
    assert plan.record is not None
    store.write_route_binding_record(plan.record)


def _policy(*, external: bool = True, controller: bool = True) -> LaunchplaneAuthzPolicy:
    identity = _identity()
    common = {
        "repository": identity.repository,
        "workflow_refs": [identity.workflow_ref],
        "event_names": [identity.event_name],
    }
    rules = []
    if controller:
        rules.append(
            {
                **common,
                "products": ["launchplane"],
                "contexts": ["launchplane"],
                "actions": [
                    "route_binding.odoo_stable_refresh.plan",
                    "route_binding.odoo_stable_refresh.apply",
                ],
            }
        )
    rules.append(
        {
            **common,
            "products": ["example-product"],
            "contexts": ["example-prod"],
            "instances": ["prod"],
            "actions": ["route_binding.read", "route_binding.apply"],
        }
    )
    if external:
        rules.append(
            {
                **common,
                "products": ["external-product"],
                "contexts": ["external-testing"],
                "instances": ["testing"],
                "actions": ["route_binding.external.plan", "route_binding.external.apply"],
            }
        )
    return LaunchplaneAuthzPolicy.model_validate({"schema_version": 2, "github_actions": rules})


def _payload(mode: str) -> dict[str, object]:
    return {
        "mode": mode,
        "reason": "Maintain independent route evidence.",
        "confirmation": "APPLY ODOO STABLE ROUTE BINDING REFRESH" if mode == "apply" else "",
    }


def _binding(
    store: PostgresRecordStore, *, external: bool = False
) -> EnvironmentRouteBindingRecord:
    return store.read_route_binding_record(
        product="external-product" if external else "example-product",
        context_name="external-testing" if external else "example-prod",
        instance_name="testing" if external else "prod",
    )


class StableRouteRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(self.directory.name) / "store.db")
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        _fixtures(self.store)

    async def _request(
        self,
        mode: str,
        *,
        now: str,
        policy: LaunchplaneAuthzPolicy | None = None,
        key: str = "refresh",
    ) -> Response:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=policy or _policy(),
            record_store_factory=lambda: self.store,
        )
        with patch("control_plane.http_app.utc_now_timestamp", return_value=now):
            return await _asgi_request(
                app,
                "POST",
                ROUTE,
                headers={"Authorization": "Bearer valid-token", "Idempotency-Key": key},
                payload=_payload(mode),
                capture_server_error_response=True,
            )

    async def test_managed_and_external_use_independent_half_lives_without_testing_overlap(
        self,
    ) -> None:
        production, external = _binding(self.store), _binding(self.store, external=True)
        plan = plan_remaining_odoo_stable_route_binding_refresh(
            record_store=self.store, evaluated_at="2026-07-22T01:00:00Z", target_limit=25
        )
        self.assertEqual(
            {(o.product, o.instance, o.status) for o in plan.outcomes},
            {
                ("example-product", "prod", "planned_refresh"),
                ("external-product", "testing", "unchanged"),
            },
        )
        self.assertEqual(_binding(self.store), production)
        self.assertEqual(_binding(self.store, external=True), external)
        # Derive the external deadline from its contract-generated attestation.
        start = datetime.fromisoformat(external.source.refreshed_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(external.source.stale_after.replace("Z", "+00:00"))
        half_life = start + (end - start) / 2
        for when, expected in [
            (half_life - timedelta(seconds=1), "unchanged"),
            (half_life, "planned_refresh"),
        ]:
            result = plan_remaining_odoo_stable_route_binding_refresh(
                record_store=self.store, evaluated_at=when.isoformat(), target_limit=25
            )
            outcome = next(o for o in result.outcomes if o.product == "external-product")
            self.assertEqual(outcome.status, expected)

    async def test_split_refresh_preserves_custody_and_fences_profile_changes(self) -> None:
        profile = self.store.read_product_profile_record("example-product").model_dump(mode="json")
        profile.update(
            driver_id="generic-web",
            public_website="required",
            lanes=[
                {
                    "context": "example-prod",
                    "instance": "prod",
                    "base_url": "https://app.example.test",
                    "health_url": "https://app.example.test/healthz",
                    "health_monitoring": {
                        "monitoring_intent": "public",
                        "checks": [{"name": "public-ingress", "kind": "public_http"}],
                    },
                }
            ],
        )
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile)
        )
        target = self.store.read_dokploy_target_record(
            context_name="example-prod", instance_name="prod"
        ).model_copy(update={"domains": ("app.example.test", "operator.example.test")})
        target_id = self.store.read_dokploy_target_id_record(
            context_name="example-prod", instance_name="prod"
        )
        self.store.write_dokploy_target_record(target)
        self.store.write_provider_target_record(
            ProviderTargetRecord.from_dokploy_records(
                target_record=target, target_id_record=target_id
            )
        )
        self.store.write_ingress_route_audit_record(
            _ingress_audit_record(
                context="example-prod", record_id="prod-alias", domains=("operator.example.test",)
            )
        )
        plan = build_route_binding_candidate(
            record_store=self.store,
            request=RouteBindingReconcileRequest.model_validate(
                {
                    "product": "example-product",
                    "context": "example-prod",
                    "instance": "prod",
                    "evaluated_at": "2026-07-22T00:00:00Z",
                    "external_ingress": {
                        "domains": ["app.example.test"],
                        "custodian": "Office operator",
                        "evidence_url": "https://example.test/report",
                    },
                }
            ),
        )
        assert plan.record is not None
        self.store.write_route_binding_record(plan.record)
        response = await self._request("apply", now="2026-07-22T13:00:00Z", key="split-refresh")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(
            next(
                o
                for o in response.json()["result"]["outcomes"]
                if o["product"] == "example-product"
            )["status"],
            "refreshed",
        )
        refreshed = _binding(self.store)
        self.assertEqual(refreshed.external_ingress, plan.record.external_ingress)
        self.assertGreater(refreshed.source.stale_after, plan.record.source.stale_after)
        original = self.store.reconcile_route_binding_record

        def change_profile(**kwargs: Any) -> Any:
            if kwargs["replacement_record"].product == "example-product":
                current = self.store.read_product_profile_record("example-product")
                self.store.write_product_profile_record(
                    current.model_copy(update={"display_name": "Changed during refresh"})
                )
            return original(**kwargs)

        with patch.object(self.store, "reconcile_route_binding_record", side_effect=change_profile):
            refused = await self._request(
                "apply", now="2026-07-23T02:00:00Z", key="split-refresh-stale"
            )
        self.assertEqual(refused.status_code, 202, refused.text)
        self.assertEqual(
            next(
                o for o in refused.json()["result"]["outcomes"] if o["product"] == "example-product"
            )["status"],
            "conflict",
        )
        self.assertEqual(_binding(self.store), refreshed)

    async def test_refresh_readback_and_replay_preserve_material_authority(self) -> None:
        before = _binding(self.store, external=True)
        testing = self.store.read_route_binding_record(
            product="example-product", context_name="example-testing", instance_name="testing"
        )
        response = await self._request("apply", now="2026-08-06T00:00:00Z")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["result"]["refreshed_count"], 2)
        after = _binding(self.store, external=True)
        self.assertEqual(after.ingress, before.ingress)
        self.assertEqual(after.tls, before.tls)
        self.assertEqual(after.domains, before.domains)
        self.assertEqual(after.provider_target, before.provider_target)
        self.assertNotEqual(route_binding_record_sha256(after), route_binding_record_sha256(before))
        self.assertEqual(
            self.store.read_route_binding_record(
                product="example-product", context_name="example-testing", instance_name="testing"
            ),
            testing,
        )
        replay = await self._request("apply", now="2026-08-06T01:00:00Z")
        self.assertEqual(replay.json()["result"], response.json()["result"])
        self.assertEqual(_binding(self.store, external=True), after)

    async def test_denied_external_target_prevents_all_writes_and_reservations(self) -> None:
        before = _binding(self.store)
        for policy in [_policy(external=False), _policy(controller=False)]:
            response = await self._request("apply", now="2026-08-06T00:00:00Z", policy=policy)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["error"]["code"], "authorization_denied")
            self.assertEqual(_binding(self.store), before)
            self.assertIsNone(
                self.store.read_idempotency_record(
                    scope=idempotency_scope(_identity()),
                    route_path=ROUTE,
                    idempotency_key="refresh",
                )
            )

    async def test_missing_external_store_capability_is_an_explicit_prerequisite(self) -> None:
        before = _binding(self.store)
        with patch.object(self.store, "read_product_profile_record", None):
            response = await self._request("apply", now="2026-08-06T00:00:00Z")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")
        self.assertEqual(_binding(self.store), before)

    async def test_external_authority_drift_is_conflict_not_automatic_replacement(self) -> None:
        before = _binding(self.store, external=True)
        profile = self.store.read_product_profile_record("external-product")
        lane = profile.lanes[0].model_copy(update={"base_url": "https://changed.example.test"})
        self.store.write_product_profile_record(profile.model_copy(update={"lanes": (lane,)}))
        response = await self._request("apply", now="2026-08-06T00:00:00Z")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["result"]["status"], "attention")
        outcome = next(
            o for o in response.json()["result"]["outcomes"] if o["product"] == "external-product"
        )
        self.assertEqual(outcome["status"], "conflict")
        self.assertEqual(_binding(self.store, external=True), before)

    async def test_partial_batch_retry_continues_remaining_binding_under_same_key(self) -> None:
        original = self.store.reconcile_route_binding_record

        def interrupted(**kwargs: Any) -> Any:
            if kwargs["replacement_record"].product == "external-product":
                raise RuntimeError("interrupted after managed refresh")
            return original(**kwargs)

        with patch.object(self.store, "reconcile_route_binding_record", side_effect=interrupted):
            response = await self._request("apply", now="2026-08-06T00:00:00Z")
        self.assertEqual(response.status_code, 500)
        managed = _binding(self.store)
        resumed = await self._request("apply", now="2026-08-06T00:00:00Z")
        self.assertEqual(resumed.status_code, 202, resumed.text)
        self.assertEqual(resumed.json()["result"]["unchanged_count"], 1)
        self.assertEqual(resumed.json()["result"]["refreshed_count"], 1)
        self.assertEqual(_binding(self.store), managed)

    async def test_discovery_refuses_overflow_and_mismatched_identity(self) -> None:
        with self.assertRaises(RouteBindingRefreshTargetLimitExceeded):
            discover_remaining_odoo_stable_route_bindings(self.store, target_limit=1)
        wrong = _binding(self.store).model_copy(update={"product": "wrong-product"})
        with patch.object(self.store, "read_route_binding_record", return_value=wrong):
            with self.assertRaises(RouteBindingRefreshTargetInvariantError):
                discover_remaining_odoo_stable_route_bindings(self.store, target_limit=25)

    def test_noncanonical_instances_report_identity_conflict_instead_of_empty_success(self) -> None:
        from control_plane.contracts.product_profile_record import ProductLaneProfile

        for instance, discover in [
            ("Testing", discover_active_odoo_testing_route_bindings),
            ("Prod", discover_remaining_odoo_stable_route_bindings),
        ]:
            with self.subTest(instance=instance):
                binding = _binding(self.store).model_copy(update={"instance": instance})
                profile = _profile(
                    lanes=(ProductLaneProfile(instance=instance, context=binding.context),)
                )
                store = _Store(profiles=(profile,), bindings=(binding,))
                with self.assertRaises(RouteBindingRefreshTargetInvariantError):
                    discover(store, target_limit=25)
