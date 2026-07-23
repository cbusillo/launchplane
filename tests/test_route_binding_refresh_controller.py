import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
)
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    RouteBindingDomain,
    RouteBindingSource,
)
from control_plane.route_binding_reconcile import (
    RouteBindingExpectedCurrent,
    RouteBindingReconcileRequest,
    plan_route_binding_reconcile,
)
from control_plane.route_binding_refresh_controller import (
    RouteBindingRefreshTargetInvariantError,
    RouteBindingRefreshTargetLimitExceeded,
    discover_active_odoo_testing_route_bindings,
    plan_odoo_testing_route_binding_refresh,
)
from control_plane.http_app import (
    create_launchplane_fastapi_app,
    idempotency_scope,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _asgi_get, _asgi_request
from tests.support.auth import _StubVerifier, _identity
from tests.test_route_binding_record import (
    _RouteBindingReconcileFakeStore,
    _dokploy_target_id_record,
    _dokploy_target_record,
    _edge_endpoint_record,
    _ingress_audit_record,
    _provider_target_record,
)


def _profile(
    *,
    product: str = "example-product",
    driver_id: str = "odoo",
    lanes: tuple[ProductLaneProfile, ...] = (
        ProductLaneProfile(instance="testing", context="example-testing"),
    ),
) -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product=product,
        display_name=product,
        repository=f"cbusillo/{product}",
        driver_id=driver_id,
        image=ProductImageProfile(repository=f"ghcr.io/cbusillo/{product}"),
        lanes=lanes,
        updated_at="2026-07-20T00:00:00Z",
        source="test",
    )


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _controller_policy(
    *,
    mode: str,
    include_binding_authority: bool = True,
) -> LaunchplaneAuthzPolicy:
    controller_action = f"route_binding.odoo_testing_refresh.{mode}"
    binding_action = "route_binding.apply" if mode == "apply" else "route_binding.read"
    rules: list[dict[str, object]] = [
        {
            "repository": "every/verireel",
            "workflow_refs": [
                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
            ],
            "event_names": ["pull_request"],
            "products": ["launchplane"],
            "contexts": ["launchplane"],
            "actions": [controller_action],
        }
    ]
    if include_binding_authority:
        rules.append(
            {
                "repository": "every/verireel",
                "workflow_refs": [
                    "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                ],
                "event_names": ["pull_request"],
                "products": ["example-product"],
                "contexts": ["example-testing"],
                "instances": ["testing"],
                "actions": [binding_action],
            }
        )
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_actions": rules,
        }
    )


def _refresh_payload(*, mode: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "mode": mode,
        "reason": "Maintain testing route-binding evidence before attestation expiry.",
    }
    if mode == "apply":
        payload["confirmation"] = "APPLY ODOO TESTING ROUTE BINDING REFRESH"
    return payload


def _service_binding(
    *,
    product: str = "example-product",
    context: str = "example-testing",
    instance: str = "testing",
    evaluated_at: str = "2026-07-20T00:00:00Z",
) -> EnvironmentRouteBindingRecord:
    store = _RouteBindingReconcileFakeStore(
        provider_target=_provider_target_record(context=context, instance=instance),
        dokploy_target=_dokploy_target_record(context=context, instance=instance),
        dokploy_target_id=_dokploy_target_id_record(context=context, instance=instance),
        edge_endpoints=(_edge_endpoint_record(),),
        ingress_audits=(_ingress_audit_record(context=context),),
    )
    plan = plan_route_binding_reconcile(
        record_store=store,
        request=RouteBindingReconcileRequest(
            product=product,
            context=context,
            instance=instance,
            expected_current=RouteBindingExpectedCurrent(state="absent"),
            source_label="enrolled-testing",
            evaluated_at=evaluated_at,
        ),
    )
    assert plan.record is not None
    return plan.record


class _Store(_RouteBindingReconcileFakeStore):
    def __init__(
        self,
        *,
        profiles: tuple[LaunchplaneProductProfileRecord, ...],
        bindings: tuple[EnvironmentRouteBindingRecord, ...],
    ) -> None:
        super().__init__(
            provider_target=_provider_target_record(context="example-testing", instance="testing"),
            dokploy_target=_dokploy_target_record(context="example-testing", instance="testing"),
            dokploy_target_id=_dokploy_target_id_record(
                context="example-testing", instance="testing"
            ),
            edge_endpoints=(_edge_endpoint_record(),),
            ingress_audits=(_ingress_audit_record(context="example-testing"),),
        )
        self.profiles = profiles
        self.bindings = {
            (record.product, record.context, record.instance): record for record in bindings
        }

    def list_product_profile_records(
        self, *, driver_id: str = ""
    ) -> tuple[LaunchplaneProductProfileRecord, ...]:
        return tuple(
            profile for profile in self.profiles if not driver_id or profile.driver_id == driver_id
        )

    def read_route_binding_record(
        self,
        *,
        product: str,
        context_name: str,
        instance_name: str,
    ) -> EnvironmentRouteBindingRecord:
        try:
            return self.bindings[(product, context_name, instance_name)]
        except KeyError as error:
            raise FileNotFoundError("missing route binding") from error


class RouteBindingRefreshControllerTests(unittest.TestCase):
    def test_discovers_only_active_service_owned_odoo_testing_bindings(self) -> None:
        testing = _service_binding()
        production = _service_binding(context="example-prod", instance="prod")
        disabled = testing.model_copy(update={"product": "disabled-product", "status": "disabled"})
        operator_owned = testing.model_copy(
            update={
                "product": "operator-product",
                "source": RouteBindingSource(
                    source_kind="operator",
                    source_label="operator",
                    source_record_ids=("operator:test",),
                    refreshed_at="2026-07-20T00:00:00Z",
                    freshness_status="recorded",
                    stale_after="2026-08-20T00:00:00Z",
                ),
            }
        )
        profiles = (
            _profile(
                lanes=(
                    ProductLaneProfile(instance="testing", context="example-testing"),
                    ProductLaneProfile(instance="prod", context="example-prod"),
                )
            ),
            _profile(product="disabled-product"),
            _profile(product="operator-product"),
            _profile(product="generic-product", driver_id="generic-web"),
        )

        targets = discover_active_odoo_testing_route_bindings(
            _Store(
                profiles=profiles,
                bindings=(testing, production, disabled, operator_owned),
            ),
            target_limit=25,
        )

        self.assertEqual(
            [(target.product, target.context, target.instance) for target in targets],
            [("example-product", "example-testing", "testing")],
        )

    def test_plans_unchanged_before_half_life(self) -> None:
        binding = _service_binding()
        plan = plan_odoo_testing_route_binding_refresh(
            record_store=_Store(profiles=(_profile(),), bindings=(binding,)),
            evaluated_at="2026-07-20T01:00:00Z",
            target_limit=25,
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.target_count, 1)
        self.assertEqual(plan.outcomes[0].status, "unchanged")
        self.assertEqual(plan.outcomes[0].operation, "none")

    def test_plans_refresh_at_half_life_and_preserves_source_label(self) -> None:
        binding = _service_binding()
        plan = plan_odoo_testing_route_binding_refresh(
            record_store=_Store(profiles=(_profile(),), bindings=(binding,)),
            evaluated_at="2026-07-20T12:00:00Z",
            target_limit=25,
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.outcomes[0].status, "planned_refresh")
        self.assertEqual(plan.outcomes[0].operation, "refresh")
        assert plan.outcomes[0].reconcile_plan is not None
        assert plan.outcomes[0].reconcile_plan.record is not None
        self.assertEqual(
            plan.outcomes[0].reconcile_plan.record.source.source_label,
            "enrolled-testing",
        )

    def test_returns_empty_without_enrolled_testing_bindings(self) -> None:
        plan = plan_odoo_testing_route_binding_refresh(
            record_store=_Store(profiles=(_profile(),), bindings=()),
            evaluated_at="2026-07-20T12:00:00Z",
            target_limit=25,
        )

        self.assertEqual(plan.status, "empty")
        self.assertEqual(plan.target_count, 0)
        self.assertEqual(plan.outcomes, ())

    def test_target_limit_fails_closed_before_planning(self) -> None:
        first = _service_binding()
        second = first.model_copy(update={"product": "second-product"})

        with self.assertRaises(RouteBindingRefreshTargetLimitExceeded):
            discover_active_odoo_testing_route_bindings(
                _Store(
                    profiles=(
                        _profile(),
                        _profile(product="second-product"),
                    ),
                    bindings=(first, second),
                ),
                target_limit=1,
            )

    def test_discovery_rejects_store_identity_mismatch(self) -> None:
        mismatched = _service_binding().model_copy(update={"context": "other-testing"})
        store = _Store(profiles=(_profile(),), bindings=())
        store.bindings[("example-product", "example-testing", "testing")] = mismatched

        with self.assertRaises(RouteBindingRefreshTargetInvariantError):
            discover_active_odoo_testing_route_bindings(
                store,
                target_limit=25,
            )

    def test_reports_blocked_refresh_without_mutating(self) -> None:
        binding = _service_binding()
        store = _Store(profiles=(_profile(),), bindings=(binding,))
        store.provider_target = None

        plan = plan_odoo_testing_route_binding_refresh(
            record_store=store,
            evaluated_at="2026-07-20T12:00:00Z",
            target_limit=25,
        )

        self.assertEqual(plan.status, "attention")
        self.assertEqual(plan.outcomes[0].status, "blocked")
        self.assertEqual(plan.outcomes[0].findings[0].code, "provider_target_missing")

    def test_reports_authority_conflict_without_mutating(self) -> None:
        binding = _service_binding().model_copy(
            update={
                "domains": (
                    RouteBindingDomain(
                        domain_name="unexpected.example.test",
                        role="primary",
                    ),
                )
            }
        )

        plan = plan_odoo_testing_route_binding_refresh(
            record_store=_Store(profiles=(_profile(),), bindings=(binding,)),
            evaluated_at="2026-07-20T12:00:00Z",
            target_limit=25,
        )

        self.assertEqual(plan.status, "attention")
        self.assertEqual(plan.outcomes[0].status, "conflict")
        self.assertEqual(
            plan.outcomes[0].findings[0].code,
            "route_binding_authority_conflict",
        )


def _write_refresh_fixture(
    store: PostgresRecordStore,
    *,
    include_provider_target: bool = True,
) -> None:
    store.write_product_profile_record(
        _profile(
            lanes=(
                ProductLaneProfile(instance="testing", context="example-testing"),
                ProductLaneProfile(instance="prod", context="example-prod"),
            )
        )
    )
    store.write_route_binding_record(_service_binding())
    store.write_route_binding_record(_service_binding(context="example-prod", instance="prod"))
    if include_provider_target:
        store.write_provider_target_record(
            _provider_target_record(context="example-testing", instance="testing")
        )
    store.write_dokploy_target_record(
        _dokploy_target_record(context="example-testing", instance="testing")
    )
    store.write_dokploy_target_id_record(
        _dokploy_target_id_record(context="example-testing", instance="testing")
    )
    store.write_edge_endpoint_record(_edge_endpoint_record())
    store.write_ingress_route_audit_record(_ingress_audit_record(context="example-testing"))


class RouteBindingRefreshControllerHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_discovers_only_testing_records_and_does_not_write(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            _write_refresh_fixture(store)
            testing_before = store.read_route_binding_record(
                product="example-product",
                context_name="example-testing",
                instance_name="testing",
            )
            production_before = store.read_route_binding_record(
                product="example-product",
                context_name="example-prod",
                instance_name="prod",
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_controller_policy(mode="plan"),
                record_store_factory=lambda: store,
            )

            with patch(
                "control_plane.http_app.utc_now_timestamp",
                return_value="2026-07-20T01:00:00Z",
            ):
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/route-bindings/odoo-testing/controller/run-once",
                    headers={"Authorization": "Bearer valid-token"},
                    payload=_refresh_payload(mode="dry-run"),
                )
            testing_after = store.read_route_binding_record(
                product="example-product",
                context_name="example-testing",
                instance_name="testing",
            )
            production_after = store.read_route_binding_record(
                product="example-product",
                context_name="example-prod",
                instance_name="prod",
            )
            store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["result"]["status"], "ok")
        self.assertEqual(payload["result"]["target_count"], 1)
        self.assertEqual(payload["result"]["unchanged_count"], 1)
        self.assertEqual(payload["result"]["outcomes"][0]["instance"], "testing")
        self.assertEqual(testing_after, testing_before)
        self.assertEqual(production_after, production_before)

    async def test_apply_refreshes_due_testing_record_and_replays(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            _write_refresh_fixture(store)
            production_before = store.read_route_binding_record(
                product="example-product",
                context_name="example-prod",
                instance_name="prod",
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_controller_policy(mode="apply"),
                record_store_factory=lambda: store,
            )
            headers = {
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "odoo-testing-route-binding-refresh-1",
            }

            with patch(
                "control_plane.http_app.utc_now_timestamp",
                return_value="2026-07-20T12:00:00Z",
            ):
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/route-bindings/odoo-testing/controller/run-once",
                    headers=headers,
                    payload=_refresh_payload(mode="apply"),
                )
            refreshed = store.read_route_binding_record(
                product="example-product",
                context_name="example-testing",
                instance_name="testing",
            )
            replay = await _asgi_request(
                app,
                "POST",
                "/v1/route-bindings/odoo-testing/controller/run-once",
                headers=headers,
                payload=_refresh_payload(mode="apply"),
            )
            denied_replay_app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_controller_policy(
                    mode="apply",
                    include_binding_authority=False,
                ),
                record_store_factory=lambda: store,
            )
            denied_replay = await _asgi_request(
                denied_replay_app,
                "POST",
                "/v1/route-bindings/odoo-testing/controller/run-once",
                headers=headers,
                payload=_refresh_payload(mode="apply"),
            )
            production_after = store.read_route_binding_record(
                product="example-product",
                context_name="example-prod",
                instance_name="prod",
            )
            parent_idempotency = store.read_idempotency_record(
                scope=idempotency_scope(_identity()),
                route_path="/v1/route-bindings/odoo-testing/controller/run-once",
                idempotency_key="odoo-testing-route-binding-refresh-1",
            )
            store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["result"]["status"], "ok")
        self.assertEqual(payload["result"]["refreshed_count"], 1)
        self.assertEqual(refreshed.source.refreshed_at, "2026-07-20T12:00:00Z")
        self.assertEqual(refreshed.source.source_label, "enrolled-testing")
        self.assertEqual(production_after, production_before)
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json()["result"], payload["result"])
        self.assertEqual(denied_replay.status_code, 403)
        self.assertEqual(denied_replay.json()["error"]["code"], "authorization_denied")
        self.assertIsNotNone(parent_idempotency)

    async def test_apply_requires_every_discovered_binding_authority(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            _write_refresh_fixture(store)
            testing_before = store.read_route_binding_record(
                product="example-product",
                context_name="example-testing",
                instance_name="testing",
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_controller_policy(
                    mode="apply",
                    include_binding_authority=False,
                ),
                record_store_factory=lambda: store,
            )

            with patch(
                "control_plane.http_app.utc_now_timestamp",
                return_value="2026-07-20T12:00:00Z",
            ):
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/route-bindings/odoo-testing/controller/run-once",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "odoo-testing-route-binding-refresh-denied",
                    },
                    payload=_refresh_payload(mode="apply"),
                )
            testing_after = store.read_route_binding_record(
                product="example-product",
                context_name="example-testing",
                instance_name="testing",
            )
            stored_idempotency = store.read_idempotency_record(
                scope=idempotency_scope(_identity()),
                route_path="/v1/route-bindings/odoo-testing/controller/run-once",
                idempotency_key="odoo-testing-route-binding-refresh-denied",
            )
            store.close()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
        self.assertEqual(testing_after, testing_before)
        self.assertIsNone(stored_idempotency)

    async def test_apply_records_blocked_outcome_without_binding_write(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            _write_refresh_fixture(store, include_provider_target=False)
            testing_before = store.read_route_binding_record(
                product="example-product",
                context_name="example-testing",
                instance_name="testing",
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_controller_policy(mode="apply"),
                record_store_factory=lambda: store,
            )
            headers = {
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "odoo-testing-route-binding-refresh-blocked",
            }

            with patch(
                "control_plane.http_app.utc_now_timestamp",
                return_value="2026-07-20T12:00:00Z",
            ):
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/route-bindings/odoo-testing/controller/run-once",
                    headers=headers,
                    payload=_refresh_payload(mode="apply"),
                )
            testing_after = store.read_route_binding_record(
                product="example-product",
                context_name="example-testing",
                instance_name="testing",
            )
            replay = await _asgi_request(
                app,
                "POST",
                "/v1/route-bindings/odoo-testing/controller/run-once",
                headers=headers,
                payload=_refresh_payload(mode="apply"),
            )
            parent_idempotency = store.read_idempotency_record(
                scope=idempotency_scope(_identity()),
                route_path="/v1/route-bindings/odoo-testing/controller/run-once",
                idempotency_key="odoo-testing-route-binding-refresh-blocked",
            )
            store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["result"]["status"], "attention")
        self.assertEqual(payload["result"]["blocked_count"], 1)
        self.assertEqual(testing_after, testing_before)
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json()["result"], payload["result"])
        self.assertIsNotNone(parent_idempotency)
        assert parent_idempotency is not None
        self.assertEqual(parent_idempotency.state, "completed")

    async def test_parent_reservation_fences_retry_after_partial_completion(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            _write_refresh_fixture(store)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_controller_policy(mode="apply"),
                record_store_factory=lambda: store,
            )
            headers = {
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "odoo-testing-route-binding-refresh-partial",
            }

            with (
                patch(
                    "control_plane.http_app.utc_now_timestamp",
                    return_value="2026-07-20T12:00:00Z",
                ),
                patch.object(
                    store,
                    "complete_mutation_reservation",
                    side_effect=RuntimeError("parent completion failed"),
                ),
            ):
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/route-bindings/odoo-testing/controller/run-once",
                    headers=headers,
                    payload=_refresh_payload(mode="apply"),
                    capture_server_error_response=True,
                )
            refreshed = store.read_route_binding_record(
                product="example-product",
                context_name="example-testing",
                instance_name="testing",
            )
            parent_reservation = store.read_idempotency_record(
                scope=idempotency_scope(_identity()),
                route_path="/v1/route-bindings/odoo-testing/controller/run-once",
                idempotency_key="odoo-testing-route-binding-refresh-partial",
            )
            retry = await _asgi_request(
                app,
                "POST",
                "/v1/route-bindings/odoo-testing/controller/run-once",
                headers=headers,
                payload=_refresh_payload(mode="apply"),
            )
            store.close()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(refreshed.source.refreshed_at, "2026-07-20T12:00:00Z")
        self.assertIsNotNone(parent_reservation)
        assert parent_reservation is not None
        self.assertEqual(parent_reservation.state, "running")
        self.assertEqual(retry.status_code, 409)
        self.assertEqual(retry.json()["error"]["code"], "mutation_in_progress")

    async def test_openapi_includes_refresh_controller_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_controller_policy(mode="plan"),
            record_store_factory=lambda: object(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/route-bindings/odoo-testing/controller/run-once"]["post"]
        self.assertEqual(route["operationId"], "run_odoo_testing_route_binding_refresh")
        self.assertIn(
            "OdooTestingRouteBindingRefreshEnvelope",
            openapi["components"]["schemas"],
        )


if __name__ == "__main__":
    unittest.main()
