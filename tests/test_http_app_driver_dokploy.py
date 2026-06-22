import json
import unittest
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import (
    Any,
    cast,
)
from unittest.mock import patch

from a2wsgi import WSGIMiddleware
from click import ClickException
from starlette.types import ASGIApp

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.service_human_auth import (
    HumanSessionManager,
    InMemoryHumanSessionStore,
    LaunchplaneHumanSession,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _asgi_get,
    _asgi_request,
    _driver_context_store,
    _driver_read_policy,
    _get_dokploy_target_inspect,
    _get_driver_context_view,
    _get_driver_descriptor,
    _get_driver_descriptors,
    _get_driver_instance_view,
    _get_tracked_target_logs,
    _github_human_driver_read_policy,
    _github_human_identity,
    _github_oauth_config,
    _local_operator_bearer_config,
    _MissingProductReadStore,
    _record_read_policy,
    _RejectingVerifier,
    _seed_dokploy_target_inspect_records,
)
from tests.test_service import (
    _identity,
    _seed_tracked_target_records,
    _sqlite_database_url,
    _StubVerifier,
    create_launchplane_service_app,
)


class FastApiDriverDescriptorTests(unittest.IsolatedAsyncioTestCase):
    async def test_driver_descriptors_return_provider_neutral_metadata(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_driver_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        list_response = await _get_driver_descriptors(app)
        show_response = await _get_driver_descriptor(app, "odoo")

        self.assertEqual(list_response.status_code, 200)
        list_payload = list_response.json()
        self.assertEqual(
            [driver["driver_id"] for driver in list_payload["drivers"]],
            ["generic-web", "ingress", "odoo", "verireel"],
        )
        ingress_driver = next(
            driver for driver in list_payload["drivers"] if driver["driver_id"] == "ingress"
        )
        self.assertEqual(ingress_driver["context_patterns"], [])
        self.assertNotIn("Dokploy", json.dumps(list_payload["drivers"]))
        self.assertTrue(str(list_payload["trace_id"]).startswith("launchplane_req_"))

        self.assertEqual(show_response.status_code, 200)
        show_payload = show_response.json()
        self.assertEqual(show_payload["driver"]["driver_id"], "odoo")
        rollback_actions = [
            action
            for action in show_payload["driver"]["actions"]
            if action["action_id"] == "prod_rollback"
        ]
        self.assertEqual(rollback_actions[0]["safety"], "destructive")

    async def test_driver_descriptor_returns_not_found_for_unknown_driver(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_driver_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _get_driver_descriptor(app, "missing")

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "not_found")

    async def test_driver_descriptors_require_bearer_or_human_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_driver_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _get_driver_descriptors(app, authorization="")

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")
        self.assertEqual(response.headers["WWW-Authenticate"], 'Bearer realm="Launchplane API"')

    async def test_driver_descriptor_requires_bearer_or_human_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_driver_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _get_driver_descriptor(app, "odoo", authorization="")

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")
        self.assertEqual(response.headers["WWW-Authenticate"], 'Bearer realm="Launchplane API"')

    async def test_driver_descriptors_reject_wrong_context_grant(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_driver_read_policy(context="other-context"),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _get_driver_descriptors(app)

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_driver_descriptor_rejects_wrong_context_grant(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_driver_read_policy(context="other-context"),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _get_driver_descriptor(app, "odoo")

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_driver_descriptors_accept_human_session_when_mounted_over_wsgi(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            oauth_config = _github_oauth_config()
            session_store = InMemoryHumanSessionStore()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=session_store,
            )
            human_session = session_manager.issue(_github_human_identity())
            policy = _github_human_driver_read_policy()
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                record_store_factory=lambda: _MissingProductReadStore(),
                human_session_manager=session_manager,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                github_oauth_config=oauth_config,
                human_session_store=session_store,
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _get_driver_descriptors(
                app,
                authorization="",
                headers={"Cookie": session_manager.session_cookie_header(human_session)},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["drivers"][0]["driver_id"], "generic-web")
        self.assertNotIn("Set-Cookie", response.headers)

    async def test_driver_descriptors_renew_expiring_human_session_cookie(self) -> None:
        session_store = InMemoryHumanSessionStore()
        oauth_config = _github_oauth_config()
        session_manager = HumanSessionManager(
            config=oauth_config,
            session_store=session_store,
        )
        session = LaunchplaneHumanSession(
            session_id="expiring-session",
            identity=_github_human_identity(),
            created_at=datetime.now(timezone.utc) - timedelta(days=13),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        )
        session_store.write_session(session)
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_github_human_driver_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
        )

        response = await _get_driver_descriptors(
            app,
            authorization="",
            headers={"Cookie": session_manager.session_cookie_header(session)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("launchplane_session=", response.headers["Set-Cookie"])
        self.assertIn("Max-Age=1209600", response.headers["Set-Cookie"])
        renewed_session = session_store.read_session("expiring-session")
        self.assertIsNotNone(renewed_session)
        assert renewed_session is not None
        self.assertGreater(renewed_session.expires_at, session.expires_at)

    async def test_driver_descriptors_preserve_renewed_session_cookie_on_denial(
        self,
    ) -> None:
        session_store = InMemoryHumanSessionStore()
        oauth_config = _github_oauth_config()
        session_manager = HumanSessionManager(
            config=oauth_config,
            session_store=session_store,
        )
        session = LaunchplaneHumanSession(
            session_id="expiring-session",
            identity=_github_human_identity(),
            created_at=datetime.now(timezone.utc) - timedelta(days=13),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        )
        session_store.write_session(session)
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_github_human_driver_read_policy(context="other-context"),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
        )

        response = await _get_driver_descriptors(
            app,
            authorization="",
            headers={"Cookie": session_manager.session_cookie_header(session)},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
        self.assertIn("launchplane_session=", response.headers["Set-Cookie"])
        renewed_session = session_store.read_session("expiring-session")
        self.assertIsNotNone(renewed_session)
        assert renewed_session is not None
        self.assertGreater(renewed_session.expires_at, session.expires_at)

    async def test_driver_descriptor_preserves_renewed_session_cookie_on_validation_error(
        self,
    ) -> None:
        session_store = InMemoryHumanSessionStore()
        oauth_config = _github_oauth_config()
        session_manager = HumanSessionManager(
            config=oauth_config,
            session_store=session_store,
        )
        session = LaunchplaneHumanSession(
            session_id="expiring-session",
            identity=_github_human_identity(),
            created_at=datetime.now(timezone.utc) - timedelta(days=13),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        )
        session_store.write_session(session)
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_github_human_driver_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
        )

        response = await _get_driver_descriptor(
            app,
            "bad driver id",
            authorization="",
            headers={"Cookie": session_manager.session_cookie_header(session)},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertIn("launchplane_session=", response.headers["Set-Cookie"])
        renewed_session = session_store.read_session("expiring-session")
        self.assertIsNotNone(renewed_session)
        assert renewed_session is not None
        self.assertGreater(renewed_session.expires_at, session.expires_at)

    async def test_driver_descriptors_use_session_when_bearer_header_is_malformed(
        self,
    ) -> None:
        oauth_config = _github_oauth_config()
        session_store = InMemoryHumanSessionStore()
        session_manager = HumanSessionManager(
            config=oauth_config,
            session_store=session_store,
        )
        human_session = session_manager.issue(_github_human_identity())
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_github_human_driver_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
        )

        response = await _get_driver_descriptors(
            app,
            authorization="Token malformed",
            headers={"Cookie": session_manager.session_cookie_header(human_session)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["drivers"][0]["driver_id"], "generic-web")

    async def test_openapi_includes_driver_descriptor_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_driver_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        list_route = openapi["paths"]["/v1/drivers"]["get"]
        show_route = openapi["paths"]["/v1/drivers/{driver_id}"]["get"]
        self.assertEqual(list_route["operationId"], "read_driver_descriptors")
        self.assertEqual(show_route["operationId"], "read_driver_descriptor")
        self.assertEqual(
            list_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/DriverDescriptorsResponse",
        )
        self.assertEqual(
            show_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/DriverDescriptorResponse",
        )
        self.assertIn("LaunchplaneErrorResponse", json.dumps(list_route))
        self.assertIn("LaunchplaneErrorResponse", json.dumps(show_route))
        self.assertEqual(
            openapi["components"]["schemas"]["DriverDescriptorsResponse"]["additionalProperties"],
            False,
        )
        self.assertEqual(
            openapi["components"]["schemas"]["DriverDescriptorResponse"]["additionalProperties"],
            False,
        )

    async def test_fastapi_driver_descriptors_precede_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = _driver_read_policy()
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                record_store_factory=lambda: _MissingProductReadStore(),
                bearer_identity_config=_local_operator_bearer_config(),
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _get_driver_descriptors(app)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("trace_id", payload)
        self.assertNotIn("authz", payload)


class FastApiDriverContextViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_driver_instance_view_returns_lane_summary(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = _driver_context_store(Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_driver_read_policy(context="example-site"),
                record_store_factory=lambda: record_store,
                bearer_identity_config=_local_operator_bearer_config(),
            )

            response = await _get_driver_instance_view(app, "example-site", "testing")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["view"]["context"], "example-site")
        self.assertEqual(payload["view"]["instance"], "testing")
        self.assertEqual(payload["view"]["drivers"][0]["driver_id"], "example-site")
        self.assertEqual(
            payload["view"]["drivers"][0]["descriptor"]["base_driver_id"], "generic-web"
        )
        available_actions = {
            action["action_id"]: action
            for action in payload["view"]["drivers"][0]["available_actions"]
        }
        self.assertEqual(
            available_actions["prod_promotion"]["route_path"],
            "/v1/drivers/generic-web/prod-promotion",
        )
        self.assertEqual(
            payload["view"]["drivers"][0]["lane_summary"]["latest_deployment"]["record_id"],
            "deployment-example-site-testing",
        )

    async def test_driver_context_view_returns_context_summary(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = _driver_context_store(Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_driver_read_policy(context="example-site"),
                record_store_factory=lambda: record_store,
                bearer_identity_config=_local_operator_bearer_config(),
            )

            response = await _get_driver_context_view(app, "example-site")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["view"]["context"], "example-site")
        self.assertEqual(payload["view"]["instance"], "")
        self.assertEqual(payload["view"]["drivers"][0]["driver_id"], "example-site")
        self.assertEqual(
            payload["view"]["drivers"][0]["descriptor"]["base_driver_id"], "generic-web"
        )

    async def test_driver_context_view_requires_bearer_or_human_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_driver_read_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _get_driver_context_view(app, "example-site", authorization="")

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")
        self.assertEqual(response.headers["WWW-Authenticate"], 'Bearer realm="Launchplane API"')

    async def test_driver_context_view_rejects_wrong_context_grant(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_driver_read_policy(context="other-context"),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _get_driver_instance_view(app, "example-site", "testing")

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_driver_context_view_accepts_human_session_when_mounted_over_wsgi(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            oauth_config = _github_oauth_config()
            session_store = InMemoryHumanSessionStore()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=session_store,
            )
            human_session = session_manager.issue(_github_human_identity())
            policy = _github_human_driver_read_policy(context="example-site")
            record_store = _driver_context_store(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                record_store_factory=lambda: record_store,
                human_session_manager=session_manager,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                github_oauth_config=oauth_config,
                human_session_store=session_store,
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _get_driver_context_view(
                app,
                "example-site",
                authorization="",
                headers={"Cookie": session_manager.session_cookie_header(human_session)},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["view"]["drivers"][0]["driver_id"], "example-site")
        self.assertEqual(
            payload["view"]["drivers"][0]["descriptor"]["base_driver_id"], "generic-web"
        )
        self.assertNotIn("Set-Cookie", response.headers)

    async def test_openapi_includes_driver_view_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_driver_read_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        context_route = openapi["paths"]["/v1/contexts/{context}/driver-view"]["get"]
        instance_route = openapi["paths"][
            "/v1/contexts/{context}/instances/{instance}/driver-view"
        ]["get"]
        self.assertEqual(context_route["operationId"], "read_driver_context_view")
        self.assertEqual(instance_route["operationId"], "read_driver_instance_view")
        self.assertEqual(
            context_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/DriverContextViewResponse",
        )
        self.assertEqual(
            instance_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/DriverContextViewResponse",
        )
        self.assertIn("LaunchplaneErrorResponse", json.dumps(context_route))
        self.assertIn("LaunchplaneErrorResponse", json.dumps(instance_route))
        self.assertEqual(
            openapi["components"]["schemas"]["DriverContextViewResponse"]["additionalProperties"],
            False,
        )


class FastApiDokployTargetInspectReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_dokploy_target_inspect_reads_redacted_provider_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_dokploy_target_inspect_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            with (
                patch(
                    "control_plane.http_app.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.invalid", "token"),
                ) as read_dokploy_config,
                patch(
                    "control_plane.dokploy_target_inspect.control_plane_dokploy.fetch_dokploy_target_payload",
                    return_value={
                        "id": "compose-cm-prod",
                        "name": "cm-prod",
                        "serverId": "server-123",
                        "environment": {
                            "id": "env-prod",
                            "name": "prod",
                            "project": {"id": "project-odoo", "name": "odoo"},
                        },
                        "env": "ODOO_DB_PASSWORD=secret\nDISABLE_ODOO_ONLINE=true\n",
                    },
                ),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="dokploy_target.inspect",
                        context="launchplane",
                    ),
                    database_url=database_url,
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )

                response = await _get_dokploy_target_inspect(
                    app,
                    context="cm_website",
                    instance="prod",
                )
                app_store.close()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["inspect"]["target_id"], "compose-cm-prod")
        self.assertEqual(payload["inspect"]["tracked_target"]["target_name"], "cm-prod")
        self.assertEqual(payload["inspect"]["provider"]["environment"]["id"], "env-prod")
        self.assertEqual(
            payload["inspect"]["provider"]["env"]["keys"],
            ["DISABLE_ODOO_ONLINE", "ODOO_DB_PASSWORD"],
        )
        self.assertTrue(payload["inspect"]["provider_payload_redacted"])
        self.assertNotIn("secret", str(payload))
        self.assertNotIn("provider_evidence", str(payload))
        read_dokploy_config.assert_called_once_with(
            control_plane_root=root,
            database_url=database_url,
        )

    async def test_dokploy_target_inspect_rejects_without_authz(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                database_url=database_url,
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _get_dokploy_target_inspect(
                app,
                target_type="compose",
                target_id="compose-123",
            )
            store.close()

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_dokploy_target_inspect_requires_database_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="dokploy_target.inspect",
                context="launchplane",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_dokploy_target_inspect(
            app,
            target_type="compose",
            target_id="compose-123",
        )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_required")

    async def test_dokploy_target_inspect_rejects_invalid_query_mode(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="dokploy_target.inspect",
                    context="launchplane",
                ),
                database_url=database_url,
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _get_dokploy_target_inspect(
                app,
                context="cm_website",
                instance="prod",
                target_type="compose",
                target_id="compose-123",
            )
            store.close()

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_dokploy_target_inspect")

    async def test_dokploy_target_inspect_returns_not_found_for_unknown_route(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            with patch(
                "control_plane.http_app.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.invalid", "token"),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="dokploy_target.inspect",
                        context="launchplane",
                    ),
                    database_url=database_url,
                    record_store_factory=lambda: store,
                    control_plane_root_path=root,
                )

                response = await _get_dokploy_target_inspect(
                    app,
                    context="missing",
                    instance="prod",
                )
                store.close()

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "not_found")

    async def test_openapi_includes_dokploy_target_inspect_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="dokploy_target.inspect",
                context="launchplane",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        inspect_route = openapi["paths"]["/v1/dokploy-targets/inspect"]["get"]
        self.assertEqual(inspect_route["operationId"], "read_dokploy_target_inspect")
        self.assertEqual(
            inspect_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/DokployTargetInspectResponse",
        )
        self.assertIn("LaunchplaneErrorResponse", json.dumps(inspect_route))
        self.assertEqual(
            openapi["components"]["schemas"]["DokployTargetInspectResponse"][
                "additionalProperties"
            ],
            False,
        )

    async def test_fastapi_dokploy_target_inspect_precedes_legacy_wsgi_fallback(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_dokploy_target_inspect_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            with (
                patch(
                    "control_plane.http_app.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.invalid", "token"),
                ),
                patch(
                    "control_plane.dokploy_target_inspect.control_plane_dokploy.fetch_dokploy_target_payload",
                    return_value={"id": "compose-cm-prod", "name": "cm-prod"},
                ),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="dokploy_target.inspect",
                        context="launchplane",
                    ),
                    database_url=database_url,
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )
                legacy_app = create_launchplane_service_app(
                    state_dir=root / "state",
                    verifier=_StubVerifier(_identity()),
                    authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                    control_plane_root_path=root,
                )
                app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

                response = await _get_dokploy_target_inspect(
                    app,
                    context="cm_website",
                    instance="prod",
                )
                app_store.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")


class FastApiDokployTargetSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_openapi_includes_dokploy_target_setup_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="dokploy_target.setup",
                context="launchplane",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        setup_route = openapi["paths"]["/v1/dokploy-targets/setup"]["post"]
        self.assertEqual(setup_route["operationId"], "setup_dokploy_target")
        self.assertEqual(
            setup_route["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/DokployTargetSetupEnvelope",
        )
        self.assertEqual(
            setup_route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        self.assertIn("LaunchplaneErrorResponse", json.dumps(setup_route))
        self.assertEqual(
            openapi["components"]["schemas"]["DokployTargetSetupEnvelope"]["additionalProperties"],
            False,
        )

    async def test_dokploy_target_setup_requires_database_storage(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="dokploy_target.setup",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/dokploy-targets/setup",
                headers={"Authorization": "Bearer valid-token"},
                payload={
                    "schema_version": 1,
                    "mode": "dry-run",
                    "operation": "create-compose",
                    "product": "launchplane",
                    "context": "cm_website",
                    "instance": "testing",
                    "target_name": "cm-website-testing",
                    "project_name": "Odoo",
                    "environment_name": "production",
                    "server_id": "server-123",
                },
            )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_required")

    async def test_fastapi_dokploy_target_setup_precedes_legacy_wsgi_fallback(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            with patch(
                "control_plane.dokploy_target_setup_http.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.invalid", "token"),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="dokploy_target.setup",
                        context="launchplane",
                    ),
                    database_url=database_url,
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )
                legacy_app = create_launchplane_service_app(
                    state_dir=root / "state",
                    verifier=_StubVerifier(_identity()),
                    authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                    control_plane_root_path=root,
                )
                app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/dokploy-targets/setup",
                    headers={"Authorization": "Bearer valid-token"},
                    payload={
                        "schema_version": 1,
                        "mode": "dry-run",
                        "operation": "create-compose",
                        "product": "launchplane",
                        "context": "cm_website",
                        "instance": "testing",
                        "target_name": "cm-website-testing",
                        "project_name": "Odoo",
                        "environment_name": "production",
                        "server_id": "server-123",
                        "domains": ["cm-website-testing.example.invalid"],
                        "runtime_port": 8069,
                        "deploy_timeout_seconds": 900,
                    },
                )
                app_store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["result"]["mode"], "dry-run")


class FastApiTrackedTargetLogsReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_tracked_target_logs_returns_redacted_application_logs(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="app-123",
                target_type="application",
                target_name="syo-testing-app",
            )
            app_store = PostgresRecordStore(database_url=database_url)
            with (
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "secret-token"),
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_target_payload",
                    return_value={"appName": "syo-testing-gfbiqh", "serverId": "server-1"},
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_application_logs",
                    return_value=("contact form submitted",),
                ) as logs_mock,
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="target_logs.read",
                        context="sellyouroutboard-testing",
                    ),
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )
                response = await _get_tracked_target_logs(
                    app,
                    "sellyouroutboard-testing",
                    "testing",
                    lines="2",
                    since="5m",
                    search="contact",
                )
                app_store.close()

        self.assertEqual(response.status_code, 200)
        logs_mock.assert_called_once_with(
            host="https://dokploy.example.com",
            token="secret-token",
            application_id="app-123",
            line_count=2,
            since="5m",
            search="contact",
        )
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["context"], "sellyouroutboard-testing")
        self.assertEqual(payload["instance"], "testing")
        self.assertEqual(payload["target"]["target_name"], "syo-testing-app")
        self.assertEqual(payload["target"]["app_name"], "syo-testing-gfbiqh")
        self.assertEqual(payload["request"], {"line_count": 2, "since": "5m", "search": "contact"})
        self.assertEqual(payload["logs"]["lines"], ["contact form submitted"])
        self.assertTrue(payload["logs"]["redacted"])
        self.assertNotIn("secret-token", json.dumps(payload))

    async def test_tracked_target_logs_redacts_raw_secret_values_from_provider_logs(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="app-123",
                target_type="application",
                target_name="syo-testing-app",
            )
            app_store = PostgresRecordStore(database_url=database_url)
            with (
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "secret-token"),
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_target_payload",
                    return_value={"appName": "syo-testing-gfbiqh", "serverId": "server-1"},
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_application_logs",
                    return_value=("API_TOKEN=plain-secret-value",),
                ),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="target_logs.read",
                        context="sellyouroutboard-testing",
                    ),
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )
                response = await _get_tracked_target_logs(
                    app,
                    "sellyouroutboard-testing",
                    "testing",
                    lines="2",
                )
                app_store.close()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["logs"]["lines"], ["API_TOKEN=[redacted]"])
        self.assertNotIn("plain-secret-value", json.dumps(payload))

    async def test_tracked_target_logs_normalizes_uppercase_path_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="app-123",
                target_type="application",
                target_name="syo-testing-app",
            )
            app_store = PostgresRecordStore(database_url=database_url)
            with (
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "secret-token"),
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_target_payload",
                    return_value={"appName": "syo-testing-gfbiqh", "serverId": "server-1"},
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_application_logs",
                    return_value=("contact form submitted",),
                ),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="target_logs.read",
                        context="SELLYOUROUTBOARD-TESTING",
                    ),
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )
                response = await _get_tracked_target_logs(
                    app,
                    "SELLYOUROUTBOARD-TESTING",
                    "TESTING",
                    lines="2",
                )
                app_store.close()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["context"], "sellyouroutboard-testing")
        self.assertEqual(payload["instance"], "testing")

    async def test_tracked_target_logs_returns_redacted_compose_logs(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="cm_website",
                instance="testing",
                target_id="compose-123",
                target_type="compose",
                target_name="cm-website-testing",
            )
            app_store = PostgresRecordStore(database_url=database_url)
            with (
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "secret-token"),
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_target_payload",
                    return_value={"appName": "cm-website-testing-iul0ql", "serverId": "server-1"},
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_compose_logs",
                    return_value=("booting", "ODOO_ADMIN_PASSWORD=[redacted]"),
                ) as logs_mock,
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="target_logs.read",
                        context="cm_website",
                    ),
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )
                response = await _get_tracked_target_logs(
                    app,
                    "cm_website",
                    "testing",
                    lines="2",
                    since="5m",
                )
                app_store.close()

        self.assertEqual(response.status_code, 200)
        logs_mock.assert_called_once_with(
            host="https://dokploy.example.com",
            token="secret-token",
            compose_id="compose-123",
            app_name="cm-website-testing-iul0ql",
            server_id="server-1",
            line_count=2,
            since="5m",
            search="",
        )
        payload = response.json()
        self.assertEqual(payload["target"]["target_type"], "compose")
        self.assertEqual(payload["target"]["target_name"], "cm-website-testing")
        self.assertEqual(payload["target"]["app_name"], "cm-website-testing-iul0ql")
        self.assertEqual(payload["logs"]["lines"], ["booting", "ODOO_ADMIN_PASSWORD=[redacted]"])
        self.assertNotIn("secret-token", json.dumps(payload))

    async def test_tracked_target_logs_delegates_compose_log_search_to_provider(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="cm_website",
                instance="testing",
                target_id="compose-123",
                target_type="compose",
                target_name="cm-website-testing",
            )
            app_store = PostgresRecordStore(database_url=database_url)
            with (
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "secret-token"),
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_target_payload",
                    return_value={"appName": "cm-website-testing-iul0ql", "serverId": "server-1"},
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_compose_logs",
                    return_value=("website_bootstrap_applied name=Cell Mechanic",),
                ) as logs_mock,
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="target_logs.read",
                        context="cm_website",
                    ),
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )
                response = await _get_tracked_target_logs(
                    app,
                    "cm_website",
                    "testing",
                    lines="2",
                    since="2h",
                    search="website_bootstrap_applied",
                )
                app_store.close()

        self.assertEqual(response.status_code, 200)
        logs_mock.assert_called_once_with(
            host="https://dokploy.example.com",
            token="secret-token",
            compose_id="compose-123",
            app_name="cm-website-testing-iul0ql",
            server_id="server-1",
            line_count=2,
            since="2h",
            search="website_bootstrap_applied",
        )
        payload = response.json()
        self.assertEqual(
            payload["request"],
            {"line_count": 2, "since": "2h", "search": "website_bootstrap_applied"},
        )
        self.assertEqual(payload["logs"]["lines"], ["website_bootstrap_applied name=Cell Mechanic"])

    async def test_tracked_target_logs_requires_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="target_logs.read",
                context="sellyouroutboard-testing",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_tracked_target_logs(
            app,
            "sellyouroutboard-testing",
            "testing",
            authorization="",
        )

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_tracked_target_logs_requires_authz_action(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="driver.read",
                context="sellyouroutboard-testing",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_tracked_target_logs(
            app,
            "sellyouroutboard-testing",
            "testing",
        )

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_tracked_target_logs_requires_db_backed_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="target_logs.read",
                context="sellyouroutboard-testing",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_tracked_target_logs(
            app,
            "sellyouroutboard-testing",
            "testing",
        )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_required")

    async def test_tracked_target_logs_returns_invalid_request_for_missing_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            with patch(
                "control_plane.tracked_target_logs.control_plane_dokploy.read_dokploy_config"
            ) as read_config_mock:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="target_logs.read",
                        context="sellyouroutboard-testing",
                    ),
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )
                response = await _get_tracked_target_logs(
                    app,
                    "sellyouroutboard-testing",
                    "missing",
                )
                app_store.close()

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        read_config_mock.assert_not_called()

    async def test_tracked_target_logs_reports_provider_unavailable(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="app-123",
                target_type="application",
                target_name="syo-testing-app",
            )
            app_store = PostgresRecordStore(database_url=database_url)
            with patch(
                "control_plane.tracked_target_logs.control_plane_dokploy.read_dokploy_config",
                side_effect=ClickException("Dokploy credentials unavailable."),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="target_logs.read",
                        context="sellyouroutboard-testing",
                    ),
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )
                response = await _get_tracked_target_logs(
                    app,
                    "sellyouroutboard-testing",
                    "testing",
                )
                app_store.close()

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "target_logs_unavailable")

    async def test_tracked_target_logs_validates_query_values(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="target_logs.read",
                context="sellyouroutboard-testing",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        line_response = await _get_tracked_target_logs(
            app,
            "sellyouroutboard-testing",
            "testing",
            lines="0",
        )
        since_response = await _get_tracked_target_logs(
            app,
            "sellyouroutboard-testing",
            "testing",
            since="yesterday",
        )
        max_line_response = await _get_tracked_target_logs(
            app,
            "sellyouroutboard-testing",
            "testing",
            lines="1001",
        )

        self.assertEqual(line_response.status_code, 400)
        self.assertEqual(since_response.status_code, 400)
        self.assertEqual(max_line_response.status_code, 400)
        self.assertEqual(line_response.json()["error"]["code"], "invalid_query")
        self.assertEqual(since_response.json()["error"]["code"], "invalid_query")
        self.assertEqual(max_line_response.json()["error"]["code"], "invalid_query")

    async def test_openapi_includes_tracked_target_logs_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="target_logs.read",
                context="sellyouroutboard-testing",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/contexts/{context}/instances/{instance}/logs"]["get"]
        self.assertEqual(route["operationId"], "read_tracked_target_logs")
        self.assertEqual(
            route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/TrackedTargetLogsResponse",
        )
        self.assertIn("LaunchplaneErrorResponse", json.dumps(route))
        self.assertIn("400", route["responses"])
        self.assertIn("401", route["responses"])
        self.assertIn("403", route["responses"])
        self.assertIn("503", route["responses"])
        self.assertEqual(
            openapi["components"]["schemas"]["TrackedTargetLogsResponse"]["additionalProperties"],
            False,
        )

    async def test_fastapi_tracked_target_logs_precedes_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="app-123",
                target_type="application",
                target_name="syo-testing-app",
            )
            app_store = PostgresRecordStore(database_url=database_url)
            with (
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "secret-token"),
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_target_payload",
                    return_value={"appName": "syo-testing-gfbiqh", "serverId": "server-1"},
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_application_logs",
                    return_value=("contact form submitted",),
                ),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="target_logs.read",
                        context="sellyouroutboard-testing",
                    ),
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )
                legacy_app = create_launchplane_service_app(
                    state_dir=root / "state",
                    verifier=_StubVerifier(_identity()),
                    authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                    control_plane_root_path=root,
                )
                app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

                response = await _get_tracked_target_logs(
                    app,
                    "sellyouroutboard-testing",
                    "testing",
                    lines="2",
                )
                app_store.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
