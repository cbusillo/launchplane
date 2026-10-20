"""Native route authority must survive rebuild publication and later maintenance."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from typing import Any
import unittest
from unittest.mock import patch

from httpx2 import Response
from control_plane.contracts.route_binding_record import EnvironmentRouteBindingRecord
from control_plane.dokploy import api
from control_plane.dokploy.testing_route import route_document
from control_plane.dokploy.testing_route_transport import DokployTestingRouteTransport
from control_plane.dokploy.native_copy_stream import (
    NativeSourceAuthorizationDenied,
    NativeSourceUnavailable,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.storage import postgres as db
from control_plane.service_auth import LaunchplaneAuthzPolicy
from tests.http_app_test_support import _asgi_request
from tests.support.auth import _StubVerifier, _identity
from tests.test_route_binding_refresh_controller import _controller_policy, _refresh_payload
from tests import test_testing_publication as publication_tests
from tests import test_testing_route as route_tests
from tests.test_testing_rebuild_service import exercise_published_copy_maintenance


class NativeTestingRouteRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = publication_tests.TestingPublicationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.prepare()
        self.publication = self.fixture.publish()
        self.store = self.fixture.store
        self.candidate = self.fixture.candidate
        self.now = datetime.now(UTC)
        clock = self.enterContext(
            patch("control_plane.native_testing_route_refresh.datetime", wraps=datetime)
        )
        clock.now.side_effect = lambda _zone: self.now
        routed = route_tests.TestingRouteTests()
        routed.setUp()
        self.frame: dict[str, Any] = {
            "raw": routed.apply(
                self.fixture.route, self.fixture.route.new_backend, retire_old=True
            ),
            "owned_file": json.dumps(
                route_document(self.fixture.route, self.fixture.route.new_backend)
            ),
        }
        self.read = self.enterContext(
            patch.object(
                DokployTestingRouteTransport, "read", side_effect=lambda: deepcopy(self.frame)
            )
        )
        self.fetch = self.enterContext(
            patch.object(
                api,
                "fetch_dokploy_target_payload",
                return_value={
                    "composeId": self.candidate.compose_id,
                    "name": self.candidate.namespace,
                    "appName": self.candidate.namespace,
                    "serverId": self.candidate.server_id,
                },
            )
        )

    def binding(self) -> EnvironmentRouteBindingRecord:
        return self.store.read_route_binding_record(
            product=self.candidate.product,
            context_name=self.candidate.context,
            instance_name="testing",
        )

    async def refresh(self, key: str = "refresh", *, allowed: bool = True) -> Response:
        payload = _controller_policy(mode="apply", include_binding_authority=allowed).model_dump()
        if allowed:
            payload["github_actions"][1].update(
                products=[self.candidate.product], contexts=[self.candidate.context]
            )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy.model_validate(payload),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.fixture.fixture.root,
        )
        with patch("control_plane.http_app.utc_now_timestamp", return_value=self.now.isoformat()):
            return await _asgi_request(
                app,
                "POST",
                "/v1/route-bindings/odoo-testing/controller/run-once",
                headers={"Authorization": "Bearer valid-token", "Idempotency-Key": key},
                payload=_refresh_payload(mode="apply"),
            )

    async def test_refresh_renews_publication_without_provider_domains(self) -> None:
        before = self.binding()
        response = await self.refresh()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["result"]["refreshed_count"], 1, response.text)
        after = self.binding()
        self.assertEqual(after.domains, before.domains)
        self.assertEqual(after.provider_target, before.provider_target)
        self.assertEqual(
            after.source.source_versions["testing_publication"], self.publication.plan.key
        )
        self.assertGreater(datetime.fromisoformat(after.source.stale_after), self.now)
        self.read.assert_called_once()
        self.now += timedelta(hours=13)
        response = await self.refresh("second-refresh")
        self.assertEqual(response.json()["result"]["refreshed_count"], 1, response.text)
        self.assertEqual(self.binding().domains, before.domains)
        self.assertEqual(self.read.call_count, 2)

    async def test_later_ordinary_deploy_keeps_the_original_route_custody_refreshable(self) -> None:
        exercise_published_copy_maintenance(self, self.fixture)
        response = await self.refresh()
        self.assertEqual(response.json()["result"]["refreshed_count"], 1, response.text)
        self.assertEqual(
            self.binding().source.source_versions["testing_route"], self.fixture.route.digest
        )

    async def test_fresh_binding_still_observes_actual_drift_without_restamping(self) -> None:
        await self.refresh()
        before = self.binding()
        self.frame["raw"]["services"][self.fixture.route.route_id + "@file"]["loadBalancer"][
            "servers"
        ] = [{"url": "http://foreign-copy:8069"}]
        response = await self.refresh("drift")
        self.assertEqual(response.json()["result"]["blocked_count"], 1, response.text)
        self.assertEqual(self.binding(), before)
        self.assertEqual(self.read.call_count, 2)

    async def test_missing_publication_provenance_never_falls_back_to_legacy(self) -> None:
        binding = self.binding()
        self.store.write_route_binding_record(
            binding.model_copy(
                update={"source": binding.source.model_copy(update={"source_versions": {}})}
            )
        )
        before = self.binding()
        response = await self.refresh()
        self.assertEqual(response.json()["result"]["blocked_count"], 1, response.text)
        self.assertEqual(self.binding(), before)
        self.read.assert_not_called()

    async def test_foreign_lane_and_physical_aliases_block_renewal(self) -> None:
        for physical in (False, True):
            with self.subTest(physical=physical):
                with self.store._session_factory() as session:
                    row: db.LaunchplaneProviderTargetRow | db.LaunchplaneDokployTargetIdRow
                    if physical:
                        original = self.fixture.plan.after.provider
                        row = self.store._provider_target_row(
                            original.model_copy(
                                update={"context": "foreign-context", "instance": "prod"}
                            )
                        )
                    else:
                        original_id = self.fixture.plan.after.target_id
                        row = self.store._dokploy_target_id_row(
                            original_id.model_copy(
                                update={"context": "foreign-context", "instance": "prod"}
                            )
                        )
                    # Rehearse preexisting corrupt storage that ordinary writers refuse.
                    session.add(row)
                    session.commit()
                before = self.binding()
                response = await self.refresh("alias-" + str(physical))
                self.assertEqual(response.json()["result"]["blocked_count"], 1, response.text)
                self.assertEqual(self.binding(), before)
                with self.store._session_factory() as session:
                    session.delete(session.get(type(row), ("foreign-context", "prod")))
                    session.commit()

    async def test_authorization_precedes_observation_and_denial_is_not_retried(self) -> None:
        response = await self.refresh(allowed=False)
        self.assertEqual(response.status_code, 403)
        self.read.assert_not_called()
        before = self.binding()
        self.read.side_effect = NativeSourceAuthorizationDenied("fixture refusal")
        response = await self.refresh("provider-denied")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
        self.read.assert_called_once()
        self.assertEqual(self.binding(), before)

        self.fetch.side_effect = api.DokployRequestFailed(
            method="GET", path="/api/compose.one", detail="fixture refusal", status_code=403
        )
        response = await self.refresh("provider-target-denied")
        self.assertEqual(response.status_code, 403)
        self.read.assert_called_once()
        self.assertEqual(self.binding(), before)

    async def test_unavailable_route_read_is_attention_without_new_freshness(self) -> None:
        before = self.binding()
        self.read.side_effect = NativeSourceUnavailable("fixture unavailable")
        response = await self.refresh()
        self.assertEqual(response.json()["result"]["blocked_count"], 1, response.text)
        self.assertEqual(self.binding(), before)

    async def test_concurrent_source_change_is_fenced_at_the_atomic_write(self) -> None:
        before = self.binding()
        reconcile = self.store.reconcile_route_binding_record

        def changed(**kwargs: Any) -> Any:
            profile = self.store.read_product_profile_record(self.candidate.product)
            self.store.write_product_profile_record(
                profile.model_copy(update={"runtime_port": profile.runtime_port + 1})
            )
            return reconcile(**kwargs)

        with patch.object(self.store, "reconcile_route_binding_record", side_effect=changed):
            response = await self.refresh()
        self.assertEqual(response.json()["result"]["conflict_count"], 1, response.text)
        self.assertEqual(self.binding(), before)
