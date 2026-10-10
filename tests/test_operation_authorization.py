from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import Any
from unittest.mock import Mock, PropertyMock, patch

from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import ValidationError

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.operation_descriptor import OperationDescriptor
from control_plane.drivers.native_routes import bind_native_fastapi_driver_handler
from control_plane.drivers.registry import list_driver_descriptors
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.operation_authorization import (
    OBSERVATION_AUTHZ_ACTION,
    bind_operation_handler,
    observation_authorization_allows,
    register_operation_route,
    validate_operation_routes,
)
from control_plane.service_auth import (
    LaunchplaneAuthzPolicy,
    LocalOperatorIdentity,
    LocalOperatorPolicyRule,
)
from control_plane.service_github_delivery_controls import (
    SERVICE_GITHUB_DELIVERY_ROUTE,
    SERVICE_TOKEN_RETIREMENT_ROUTE,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _asgi_request,
    _local_operator_bearer_config,
    _RejectingVerifier,
)
from tests.support.stores import sqlite_database_url
from tests.test_service_github_delivery_controls import retirement_request, seed_metadata


def standing_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy(
        schema_version=2,
        local_operators=(
            LocalOperatorPolicyRule(
                subjects=("local-owner-agent",),
                token_labels=("local-owner-read",),
                products=("launchplane",),
                contexts=("launchplane",),
                actions=(OBSERVATION_AUTHZ_ACTION,),
            ),
        ),
    )


class OperationDeclarationTests(unittest.TestCase):
    def test_new_observation_uses_standing_grant_without_its_own_action_grant(self) -> None:
        def endpoint() -> None:
            pass

        descriptor = OperationDescriptor(
            method="GET",
            route_path="/v1/example",
            scope="context",
            authz_action="example.inspect",
            mode_effects={"read": "observation", "apply": "operation"},
        )
        policy = standing_policy()
        identity = LocalOperatorIdentity(
            subject="local-owner-agent", token_label="local-owner-read"
        )
        request: dict[str, Any] = dict(
            endpoint=endpoint,
            authorization_allows=policy.allows,
            identity=identity,
            product="launchplane",
            context="launchplane",
        )
        self.assertFalse(observation_authorization_allows(mode="read", **request))
        bind_operation_handler(descriptor=descriptor, endpoint=endpoint, declared_methods=("GET",))
        self.assertTrue(observation_authorization_allows(mode="read", **request))
        for mode in ("apply", "dry-run", "unknown"):
            self.assertFalse(observation_authorization_allows(mode=mode, **request))
        for changes in (
            {"context": "other"},
            {"product": "other"},
            {"identity": LocalOperatorIdentity(subject=identity.subject, token_label="other")},
            {"authorization_allows": LaunchplaneAuthzPolicy().allows},
        ):
            self.assertFalse(
                observation_authorization_allows(mode="read", **{**request, **changes})
            )

    def test_undeclared_or_invalid_modes_never_call_authorization(self) -> None:
        def endpoint() -> None:
            pass

        allows = Mock(return_value=True)
        request: dict[str, Any] = dict(
            endpoint=endpoint,
            mode="read",
            authorization_allows=allows,
            identity=LocalOperatorIdentity(subject="agent", token_label="label"),
            product="launchplane",
            context="launchplane",
        )
        self.assertFalse(observation_authorization_allows(**request))
        bind_operation_handler(
            descriptor=OperationDescriptor(
                method="GET", route_path="/v1/example", authz_action="example.read", scope="context"
            ),
            endpoint=endpoint,
            declared_methods=("GET",),
        )
        self.assertFalse(observation_authorization_allows(**request))
        allows.assert_not_called()
        with self.assertRaises(ValidationError):
            OperationDescriptor.model_validate(
                {
                    "method": "GET",
                    "route_path": "/v1/example",
                    "scope": "context",
                    "mode_effects": {"read": "unknown"},
                }
            )

    def test_route_registration_rejects_drift_and_missing_handler_declaration(self) -> None:
        def endpoint() -> None:
            pass

        descriptor = OperationDescriptor(
            method="GET",
            route_path="/v1/example",
            authz_action="example.read",
            scope="context",
            mode_effects={"read": "observation"},
        )
        with self.assertRaises(ValueError):
            bind_operation_handler(
                descriptor=descriptor, endpoint=endpoint, declared_methods=("POST",)
            )
        app = FastAPI()
        register_operation_route(app, descriptor=descriptor, endpoint=endpoint)
        validate_operation_routes(app)
        route = app.routes[-1]
        assert isinstance(route, APIRoute)
        route.endpoint = lambda: None
        with self.assertRaises(ValueError):
            validate_operation_routes(app)

    def test_native_driver_binding_reuses_operation_effects(self) -> None:
        descriptors = list_driver_descriptors()
        driver = next(
            driver
            for driver in descriptors
            if any(
                action.safety == "read" and action.scope == "context" for action in driver.actions
            )
        )
        action = next(
            action
            for action in driver.actions
            if action.safety == "read" and action.scope == "context"
        )
        declared = action.model_copy(update={"mode_effects": {"read": "observation"}})
        modified = driver.model_copy(
            update={
                "actions": tuple(declared if item == action else item for item in driver.actions)
            }
        )

        def endpoint() -> None:
            pass

        with patch(
            "control_plane.drivers.native_routes.list_driver_descriptors",
            return_value=tuple(modified if item == driver else item for item in descriptors),
        ):
            bind_native_fastapi_driver_handler(
                route_path=action.route_path, endpoint=endpoint, declared_methods=(action.method,)
            )
        policy = standing_policy()
        self.assertTrue(
            observation_authorization_allows(
                endpoint=endpoint,
                mode="read",
                authorization_allows=policy.allows,
                identity=LocalOperatorIdentity(
                    subject="local-owner-agent", token_label="local-owner-read"
                ),
                product="launchplane",
                context="launchplane",
            )
        )


class DeliveryObservationHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_db_grant_read_redaction_write_denial_and_revocation(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=sqlite_database_url(Path(directory) / "state.sqlite3")
            )
            self.addCleanup(store.close)
            store.ensure_schema()
            seed_metadata(store)
            policy = standing_policy()
            active = store.seed_authz_policy_if_absent(
                LaunchplaneAuthzPolicyRecord(
                    record_id="observation-r1",
                    revision=1,
                    source="test:authorized-policy",
                    updated_at="2026-10-06T00:00:00Z",
                    policy=policy,
                )
            )
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=_local_operator_bearer_config(),
            )
            headers = {"Authorization": "Bearer local-operator-token"}
            before = (
                store.list_secret_records(),
                store.list_secret_bindings(),
                store.list_runtime_environment_records(),
            )
            # Exercise per-request DB policy refresh using the SQLite rehearsal store.
            with (
                patch.object(
                    PostgresRecordStore,
                    "database_dialect_name",
                    new_callable=PropertyMock,
                    return_value="postgresql",
                ),
                patch.object(
                    store,
                    "read_secret_version",
                    side_effect=AssertionError("Observation cannot resolve secret values."),
                ),
                patch(
                    "control_plane.launchplane_github_delivery.mint_delivery_installation_token",
                    side_effect=AssertionError("Observation cannot mint credentials."),
                ),
                patch.object(
                    store,
                    "write_outbox_delivery_record",
                    side_effect=AssertionError("Observation cannot queue work."),
                ),
            ):
                response = await _asgi_request(
                    app, "GET", SERVICE_GITHUB_DELIVERY_ROUTE, headers=headers
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["app_id"], "76")
                self.assertNotIn("encrypted_value", response.text)
                self.assertNotIn("private_key_pem", response.text)
                denied_write = await _asgi_request(
                    app,
                    "POST",
                    SERVICE_TOKEN_RETIREMENT_ROUTE,
                    headers=headers,
                    payload=retirement_request().model_dump(mode="json"),
                )
                self.assertEqual(denied_write.status_code, 403, denied_write.text)
                for route in app.routes:
                    if isinstance(route, APIRoute) and route.path == SERVICE_GITHUB_DELIVERY_ROUTE:
                        endpoint = route.endpoint
                        break
                with patch.object(endpoint, "__launchplane_operation_descriptor__", None):
                    undeclared = await _asgi_request(
                        app, "GET", SERVICE_GITHUB_DELIVERY_ROUTE, headers=headers
                    )
                self.assertEqual(undeclared.status_code, 403, undeclared.text)
                revoked = LaunchplaneAuthzPolicyRecord(
                    record_id="observation-r2",
                    revision=2,
                    source="test:authorized-policy-revocation",
                    updated_at="2026-10-06T00:01:00Z",
                    policy=LaunchplaneAuthzPolicy(schema_version=2),
                )
                changed = store.compare_and_write_authz_policy_record(
                    expected_record=active, replacement_record=revoked
                )
                self.assertEqual(changed.status, "written")
                revoked_response = await _asgi_request(
                    app, "GET", SERVICE_GITHUB_DELIVERY_ROUTE, headers=headers
                )
                self.assertEqual(revoked_response.status_code, 403, revoked_response.text)
                self.assertEqual(
                    before,
                    (
                        store.list_secret_records(),
                        store.list_secret_bindings(),
                        store.list_runtime_environment_records(),
                    ),
                )
