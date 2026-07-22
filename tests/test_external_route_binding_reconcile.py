import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from unittest.mock import patch

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneHealthCheck,
    ProductLaneHealthMonitoringPolicy,
    ProductLaneProfile,
)
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    RouteBindingDomain,
    RouteBindingIngress,
    RouteBindingProviderTarget,
    RouteBindingSource,
    RouteBindingTls,
    route_binding_record_sha256,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.route_binding_external_reconcile import (
    ExternalRouteBindingReconcileRequest,
    plan_external_route_binding_reconcile,
)
from control_plane.route_binding_reconcile import RouteBindingExpectedCurrent
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _asgi_get, _asgi_request
from tests.support.auth import _StubVerifier, _identity
from tests.support.stores import _sqlite_database_url


def _profile(
    *,
    domain: str = "app.example.test",
    require_runtime_identity: bool = True,
) -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="example-product",
        display_name="Example Product",
        repository="every/example-product",
        driver_id="odoo",
        image=ProductImageProfile(repository="ghcr.io/every/example-product"),
        runtime_port=8069,
        health_path="/web/health",
        lanes=(
            ProductLaneProfile(
                context="example-testing",
                instance="web",
                base_url=f"https://{domain}",
                health_monitoring=ProductLaneHealthMonitoringPolicy(
                    checks=(
                        ProductLaneHealthCheck(
                            name="public-ingress",
                            require_runtime_identity=require_runtime_identity,
                        ),
                    )
                ),
            ),
        ),
        updated_at="2026-07-21T00:00:00Z",
        source="test",
    )


def _provider_target() -> ProviderTargetRecord:
    return ProviderTargetRecord(
        context="example-testing",
        instance="web",
        provider_id="dokploy",
        target_category="compose",
        target_id="compose-provider-id",
        display_name="example-target",
        provider_target_type="compose",
        updated_at="2026-07-21T00:00:00Z",
        source_label="test",
    )


def _external_record(*, domain: str = "app.example.test") -> EnvironmentRouteBindingRecord:
    return EnvironmentRouteBindingRecord(
        product="example-product",
        context="example-testing",
        instance="web",
        provider_target=RouteBindingProviderTarget(
            provider_id="dokploy",
            target_category="compose",
            provider_target_type="compose",
            target_name="example-target",
            provider_evidence={"target_record": "example-testing:web"},
        ),
        ingress=RouteBindingIngress(
            provider="external",
            endpoint_key=f"external:{domain}",
            termination_kind="edge",
        ),
        domains=(RouteBindingDomain(domain_name=domain, role="primary"),),
        tls=RouteBindingTls(owner="external"),
        source=RouteBindingSource(
            source_kind="operator",
            source_label="operator-external-reconcile",
            source_record_ids=(
                "product-profile:example-product",
                "provider-target:example-testing:web",
            ),
            source_versions={
                "product-profile:example-product": "2026-07-21T00:00:00Z",
                "provider-target:example-testing:web": "2026-07-21T00:00:00Z",
            },
            refreshed_at="2026-07-22T00:00:00Z",
            freshness_status="recorded",
            stale_after="2026-08-21T00:00:00Z",
        ),
        updated_at="2026-07-22T00:00:00Z",
    )


def _managed_record() -> EnvironmentRouteBindingRecord:
    record = _external_record()
    return record.model_copy(
        update={
            "ingress": RouteBindingIngress(
                provider="npmplus",
                endpoint_key="managed-edge",
                termination_kind="edge",
            ),
            "tls": RouteBindingTls(owner="launchplane"),
            "source": record.source.model_copy(update={"source_kind": "service"}),
        }
    )


class _Store:
    def __init__(
        self,
        *,
        profile: LaunchplaneProductProfileRecord | None = None,
        provider_target: ProviderTargetRecord | None = None,
        route_binding: EnvironmentRouteBindingRecord | None = None,
    ) -> None:
        self.profile = profile
        self.provider_target = provider_target
        self.route_binding = route_binding

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if self.profile is None:
            raise FileNotFoundError("missing profile")
        return self.profile

    def read_provider_target_record(
        self, *, context_name: str, instance_name: str
    ) -> ProviderTargetRecord:
        if self.provider_target is None:
            raise FileNotFoundError("missing provider target")
        return self.provider_target

    def read_route_binding_record(
        self,
        *,
        product: str,
        context_name: str,
        instance_name: str,
    ) -> EnvironmentRouteBindingRecord:
        if self.route_binding is None:
            raise FileNotFoundError("missing route binding")
        return self.route_binding


def _request(
    *,
    current_record: EnvironmentRouteBindingRecord | None = None,
    desired_status: Literal["active", "disabled"] = "active",
) -> ExternalRouteBindingReconcileRequest:
    expected_current = (
        RouteBindingExpectedCurrent(state="absent")
        if current_record is None
        else RouteBindingExpectedCurrent(
            state="present",
            record_sha256=route_binding_record_sha256(current_record),
        )
    )
    return ExternalRouteBindingReconcileRequest(
        product="example-product",
        context="example-testing",
        instance="web",
        expected_current=expected_current,
        desired_status=desired_status,
        evaluated_at="2026-07-22T00:00:00Z",
    )


class ExternalRouteBindingPlannerTests(unittest.TestCase):
    def test_plans_external_edge_binding_from_db_records(self) -> None:
        plan = plan_external_route_binding_reconcile(
            record_store=_Store(profile=_profile(), provider_target=_provider_target()),
            request=_request(),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.operation, "create")
        self.assertIsNotNone(plan.record)
        assert plan.record is not None
        self.assertEqual(plan.record.ingress.provider, "external")
        self.assertEqual(plan.record.ingress.termination_kind, "edge")
        self.assertEqual(plan.record.ingress.endpoint_key, "external:app.example.test")
        self.assertEqual(plan.record.tls.owner, "external")
        self.assertEqual(plan.record.source.source_kind, "operator")
        self.assertEqual(plan.record.source.stale_after, "2026-08-21T00:00:00Z")
        self.assertEqual(plan.record.domains[0].domain_name, "app.example.test")

    def test_blocks_without_strict_public_runtime_identity_check(self) -> None:
        plan = plan_external_route_binding_reconcile(
            record_store=_Store(
                profile=_profile(require_runtime_identity=False),
                provider_target=_provider_target(),
            ),
            request=_request(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            plan.findings[0].code,
            "strict_public_runtime_identity_check_missing",
        )

    def test_refuses_managed_route_binding_takeover(self) -> None:
        current_record = _managed_record()
        plan = plan_external_route_binding_reconcile(
            record_store=_Store(
                profile=_profile(),
                provider_target=_provider_target(),
                route_binding=current_record,
            ),
            request=_request(current_record=current_record),
        )

        self.assertEqual(plan.status, "conflict")
        self.assertEqual(plan.findings[0].code, "route_binding_ownership_conflict")

    def test_replaces_only_operator_owned_external_authority_under_cas(self) -> None:
        current_record = _external_record(domain="old.example.test")
        plan = plan_external_route_binding_reconcile(
            record_store=_Store(
                profile=_profile(),
                provider_target=_provider_target(),
                route_binding=current_record,
            ),
            request=_request(current_record=current_record),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.operation, "replace")
        self.assertEqual(plan.findings[0].code, "external_route_authority_change")
        self.assertIsNotNone(plan.record)
        assert plan.record is not None
        self.assertEqual(plan.record.domains[0].domain_name, "app.example.test")

    def test_relinquishes_external_authority_without_provider_access(self) -> None:
        current_record = _external_record()
        plan = plan_external_route_binding_reconcile(
            record_store=_Store(route_binding=current_record),
            request=_request(current_record=current_record, desired_status="disabled"),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.operation, "relinquish")
        self.assertEqual(plan.findings[0].code, "external_route_authority_relinquish")
        self.assertIsNotNone(plan.record)
        assert plan.record is not None
        self.assertEqual(plan.record.status, "disabled")


def _policy(*, action: str, instances: tuple[str, ...] = ("web",)) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["example-product"],
                    "contexts": ["example-testing"],
                    "instances": list(instances),
                    "actions": [action],
                }
            ],
        }
    )


def _payload(
    *,
    mode: str = "apply",
    instance: str = "web",
    desired_status: Literal["active", "disabled"] = "active",
    current_record: EnvironmentRouteBindingRecord | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "mode": mode,
        "product": "example-product",
        "context": "example-testing",
        "instance": instance,
        "expected_current": (
            {"state": "absent"}
            if current_record is None
            else {
                "state": "present",
                "record_sha256": route_binding_record_sha256(current_record),
            }
        ),
        "desired_status": desired_status,
    }
    if mode == "apply":
        payload.update(
            {
                "reason": "Record reviewed externally managed public ingress.",
                "confirmation": "APPLY EXTERNAL ROUTE BINDING RECONCILE",
            }
        )
    return payload


class ExternalRouteBindingHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_dry_run_uses_separate_plan_action(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_product_profile_record(_profile())
            store.write_provider_target_record(_provider_target())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_policy(action="route_binding.external.plan"),
                record_store_factory=lambda: store,
            )
            with patch(
                "control_plane.http_app.utc_now_timestamp",
                return_value="2026-07-22T00:00:00Z",
            ):
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/route-bindings/external/reconcile",
                    headers={"Authorization": "Bearer valid-token"},
                    payload=_payload(mode="dry-run"),
                )
            store.close()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["records"]["route_binding_status"], "planned_create")

    async def test_managed_apply_grant_cannot_write_external_authority(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_policy(action="route_binding.apply"),
            record_store_factory=lambda: _Store(
                profile=_profile(), provider_target=_provider_target()
            ),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/route-bindings/external/reconcile",
            headers={
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "external-route-binding-denied",
            },
            payload=_payload(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_external_apply_requires_exact_instance_authority(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_policy(action="route_binding.external.apply"),
            record_store_factory=lambda: _Store(
                profile=_profile(), provider_target=_provider_target()
            ),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/route-bindings/external/reconcile",
            headers={
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "external-route-binding-wrong-instance",
            },
            payload=_payload(instance="other"),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_external_apply_writes_atomically_and_replays(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_product_profile_record(_profile())
            store.write_provider_target_record(_provider_target())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_policy(action="route_binding.external.apply"),
                record_store_factory=lambda: store,
            )
            headers = {
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "external-route-binding-1",
            }
            with patch(
                "control_plane.http_app.utc_now_timestamp",
                return_value="2026-07-22T00:00:00Z",
            ):
                first = await _asgi_request(
                    app,
                    "POST",
                    "/v1/route-bindings/external/reconcile",
                    headers=headers,
                    payload=_payload(),
                )
                replay = await _asgi_request(
                    app,
                    "POST",
                    "/v1/route-bindings/external/reconcile",
                    headers=headers,
                    payload=_payload(),
                )
            stored = store.read_route_binding_record(
                product="example-product",
                context_name="example-testing",
                instance_name="web",
            )
            store.close()

        self.assertEqual(first.status_code, 202)
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(first.json()["records"]["route_binding_status"], "created")
        self.assertEqual(replay.json()["records"]["route_binding_status"], "created")
        self.assertEqual(
            first.json()["result"]["actor"],
            "github-actions:every/verireel:every/verireel/.github/workflows/"
            "preview-control-plane.yml@refs/heads/main",
        )
        self.assertEqual(
            first.json()["result"]["reason"],
            "Record reviewed externally managed public ingress.",
        )
        self.assertEqual(
            first.json()["result"]["candidate_record_sha256"],
            replay.json()["result"]["candidate_record_sha256"],
        )
        self.assertEqual(stored.ingress.provider, "external")
        self.assertEqual(stored.tls.owner, "external")

    async def test_external_apply_relinquishes_without_profile_or_provider_access(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            current_record = _external_record()
            store.write_route_binding_record(current_record)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_policy(action="route_binding.external.apply"),
                record_store_factory=lambda: store,
            )
            with patch(
                "control_plane.http_app.utc_now_timestamp",
                return_value="2026-07-22T00:30:00Z",
            ):
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/route-bindings/external/reconcile",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "external-route-binding-relinquish",
                    },
                    payload=_payload(
                        current_record=current_record,
                        desired_status="disabled",
                    ),
                )
            stored = store.read_route_binding_record(
                product="example-product",
                context_name="example-testing",
                instance_name="web",
            )
            store.close()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["operation"], "relinquish")
        self.assertEqual(stored.status, "disabled")

    async def test_openapi_includes_external_route_binding_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_policy(action="route_binding.external.plan"),
            record_store_factory=lambda: _Store(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        self.assertEqual(
            openapi["paths"]["/v1/route-bindings/external/reconcile"]["post"]["operationId"],
            "reconcile_external_route_binding",
        )
        self.assertIn(
            "ExternalRouteBindingReconcileEnvelope",
            openapi["components"]["schemas"],
        )


if __name__ == "__main__":
    unittest.main()
