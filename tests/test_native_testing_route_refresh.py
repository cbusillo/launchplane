"""Native route authority must survive rebuild publication and later maintenance."""

import asyncio
from threading import Event
from pathlib import Path
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from typing import Any
import unittest
from unittest.mock import patch

from httpx2 import Response
from control_plane.contracts.route_binding_record import EnvironmentRouteBindingRecord
from control_plane.contracts.route_binding_record import route_binding_record_sha256
from control_plane.contracts.product_profile_record import ProductLaneProfile
from control_plane.route_binding_reconcile import (
    RouteBindingReconcilePlan,
    RouteBindingReconcileRequest,
    RouteBindingExpectedCurrent,
    plan_route_binding_reconcile,
)
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


async def refresh_testing_binding_http(
    store: db.PostgresRecordStore,
    root: Path,
    product: str,
    context: str,
    now: datetime,
    key: str = "refresh",
    *,
    allowed: bool = True,
    mode: str = "apply",
) -> Response:
    payload = _controller_policy(
        mode="apply" if mode == "apply" else "plan", include_binding_authority=allowed
    ).model_dump()
    if allowed:
        payload["github_actions"][1].update(products=[product], contexts=[context])
    app = create_launchplane_fastapi_app(
        verifier=_StubVerifier(_identity()),
        authz_policy=LaunchplaneAuthzPolicy.model_validate(payload),
        record_store_factory=lambda: store,
        control_plane_root_path=root,
    )
    with patch("control_plane.http_app.utc_now_timestamp", return_value=now.isoformat()):
        return await _asgi_request(
            app,
            "POST",
            "/v1/route-bindings/odoo-testing/controller/run-once",
            headers={"Authorization": "Bearer valid-token", "Idempotency-Key": key},
            payload=_refresh_payload(mode=mode),
        )


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
        identity = self.fixture.plan.deployment.runtime_identity
        assert identity is not None
        self.frame: dict[str, Any] = {
            "old_identity": identity.model_dump(mode="json"),
            "old_endpoints": (self.fixture.route.new_backend,),
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
        return await refresh_testing_binding_http(
            self.store,
            self.fixture.fixture.root,
            self.candidate.product,
            self.candidate.context,
            self.now,
            key,
            allowed=allowed,
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
        self.read.assert_called()
        self.read.reset_mock()
        self.now += timedelta(hours=13)
        response = await self.refresh("second-refresh")
        self.assertEqual(response.json()["result"]["refreshed_count"], 1, response.text)
        self.assertEqual(self.binding().domains, before.domains)
        self.read.assert_called()

    async def test_later_ordinary_deploy_keeps_the_original_route_custody_refreshable(self) -> None:
        exercise_published_copy_maintenance(self, self.fixture)
        inventory = self.fixture.current().inventory
        assert inventory is not None and inventory.runtime_identity is not None
        self.frame["old_identity"] = inventory.runtime_identity.model_dump(mode="json")
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
        self.read.assert_called()

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

    async def test_stopped_or_missing_copy_cannot_renew_a_valid_route_file(self) -> None:
        self.frame.update(old_identity=None, old_endpoints=())
        before = self.binding()
        response = await self.refresh()
        self.assertEqual(response.json()["result"]["blocked_count"], 1, response.text)
        self.assertEqual(self.binding(), before)

    async def test_remote_observation_does_not_block_other_http_requests(self) -> None:
        started, release = Event(), Event()

        def held_read() -> dict[str, Any]:
            started.set()
            if not release.wait(10):
                raise NativeSourceUnavailable("An unrelated HTTP request could not progress.")
            return deepcopy(self.frame)

        self.read.side_effect = held_read
        task = asyncio.create_task(self.refresh())
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 10))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: self.store,
            )
            response = await _asgi_request(app, "GET", "/v1/auth/session")
            self.assertEqual(response.status_code, 401)
        finally:
            release.set()
        response = await task
        self.assertEqual(response.json()["result"]["refreshed_count"], 1, response.text)

    async def test_refresh_preserves_the_original_publication_inverse(self) -> None:
        response = await self.refresh()
        self.assertEqual(response.json()["result"]["refreshed_count"], 1, response.text)
        self.assertEqual(self.fixture.publish(inverse=True).state, "reversed")
        self.assertEqual(self.fixture.current(), self.fixture.before)

    async def test_route_provenance_changes_still_block_the_publication_inverse(self) -> None:
        await self.refresh()
        binding = self.binding()
        self.store.write_route_binding_record(
            binding.model_copy(
                update={
                    "source": binding.source.model_copy(
                        update={
                            "source_versions": {
                                **binding.source.source_versions,
                                "testing_route": "foreign-route",
                            }
                        }
                    ),
                }
            )
        )
        with self.assertRaises(ValueError):
            self.fixture.publish(inverse=True)

    async def test_dry_run_observes_native_route_without_writing(self) -> None:
        before = self.binding()
        response = await refresh_testing_binding_http(
            self.store,
            self.fixture.fixture.root,
            self.candidate.product,
            self.candidate.context,
            self.now,
            mode="dry-run",
        )
        self.assertEqual(response.json()["result"]["planned_refresh_count"], 1, response.text)
        self.assertEqual(self.binding(), before)
        self.read.assert_called()

    async def test_replay_returns_receipt_without_another_remote_observation(self) -> None:
        first = await self.refresh()
        self.read.reset_mock()
        self.read.side_effect = AssertionError("A completed key must not start a remote read.")
        replay = await self.refresh()
        self.assertEqual(replay.json()["result"], first.json()["result"])
        self.read.assert_not_called()

    async def test_each_native_lane_is_written_before_observing_the_next(self) -> None:
        profile = self.store.read_product_profile_record(self.candidate.product)
        other_product, other_context = "other-product", "other-context"
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "product": other_product,
                    "lanes": (ProductLaneProfile(instance="testing", context=other_context),),
                }
            )
        )
        self.store.write_route_binding_record(
            self.binding().model_copy(
                update={
                    "product": other_product,
                    "context": other_context,
                }
            )
        )
        rules = _controller_policy(mode="apply").model_dump()
        rules["github_actions"][1].update(
            products=[self.candidate.product, other_product],
            contexts=[self.candidate.context, other_context],
        )
        events: list[tuple[str, str]] = []
        reconcile = self.store.reconcile_route_binding_record

        def planned(
            _store: Any, *, current: EnvironmentRouteBindingRecord, **kwargs: Any
        ) -> RouteBindingReconcilePlan:
            events.append(("observe", current.context))
            replacement = current.model_copy(update={"updated_at": self.now.isoformat()})
            return RouteBindingReconcilePlan(
                status="ready",
                operation="refresh",
                current_record=current,
                record=replacement,
                current_record_sha256=route_binding_record_sha256(current),
                candidate_record_sha256=route_binding_record_sha256(replacement),
            )

        def written(**kwargs: Any) -> Any:
            events.append(("write", kwargs["replacement_record"].context))
            return reconcile(**kwargs)

        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy.model_validate(rules),
            record_store_factory=lambda: self.store,
        )
        with (
            patch("control_plane.http_app.plan_native_testing_route_refresh", side_effect=planned),
            patch.object(self.store, "reconcile_route_binding_record", side_effect=written),
        ):
            response = await _asgi_request(
                app,
                "POST",
                "/v1/route-bindings/odoo-testing/controller/run-once",
                headers={"Authorization": "Bearer valid-token", "Idempotency-Key": "batch-order"},
                payload=_refresh_payload(mode="apply"),
            )
        self.assertEqual(response.json()["result"]["refreshed_count"], 2, response.text)
        contexts = sorted((self.candidate.context, other_context))
        self.assertEqual(
            events, [(phase, context) for context in contexts for phase in ("observe", "write")]
        )

    async def test_manual_reconcile_identifies_the_native_refresh_path(self) -> None:
        current = self.binding()
        result = plan_route_binding_reconcile(
            record_store=self.store,
            request=RouteBindingReconcileRequest(
                product=current.product,
                context=current.context,
                instance=current.instance,
                expected_current=RouteBindingExpectedCurrent(
                    state="present", record_sha256=route_binding_record_sha256(current)
                ),
                evaluated_at=self.now.isoformat(),
            ),
        )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.findings[0].code, "native_testing_route_requires_controller")

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
