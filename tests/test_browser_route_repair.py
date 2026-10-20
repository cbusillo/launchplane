from datetime import datetime, timedelta, timezone
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import Any
from unittest.mock import patch

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope
from control_plane.http_app import (
    RouteBindingReconcileEnvelope,
    create_launchplane_fastapi_app,
    idempotency_scope,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import DbOnlyMutationRequest, PostgresRecordStore
from tests.http_app_test_support import (
    _browser_mutation_headers,
    _github_human_identity,
    _github_oauth_config,
    _RejectingVerifier,
)
from tests.support.http import request
from tests.support.stores import _sqlite_database_url
from tests.test_http_app_product import _product_health_monitoring_profile
from tests.test_route_binding_record import (
    _dokploy_target_record,
    _dokploy_target_id_record,
    _edge_endpoint_record,
    _ingress_audit_record,
)


class _BrowserRouteRepairFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(self.directory.name) / "db")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        payload = _product_health_monitoring_profile().model_dump(mode="json")
        payload["product"] = "example-product"
        payload["lanes"] = [
            {**payload["lanes"][0], "context": "example-testing", "instance": "web"}
        ]
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(payload)
        )
        self.target = _dokploy_target_record(domains=("https://app.example.test/old",)).model_copy(
            update={"env": {"PRIVATE": "must-not-escape"}}
        )
        self.target_id = _dokploy_target_id_record()
        self.store.write_dokploy_target_record(self.target)
        self.store.write_dokploy_target_id_record(self.target_id)
        self.store.write_provider_target_record(
            ProviderTargetRecord.from_dokploy_records(
                target_record=self.target, target_id_record=self.target_id
            )
        )
        self.manager = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        self.session = self.manager.issue(_github_human_identity())
        self.policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "github_humans": [
                    {
                        "github_ids": [self.session.identity.github_id],
                        "roles": ["admin"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["authz_policy_grant.write"],
                    }
                ],
            }
        )
        self.app = self.make_app(self.policy)
        self.domain_payload = {
            "mode": "dry-run",
            "operation": "repair-domain-authority",
            "product": "launchplane",
            "context": "example-testing",
            "instance": "web",
            "browser_review": {"product": "example-product"},
        }
        self.config = patch(
            "control_plane.dokploy.source.read_dokploy_config",
            return_value=("https://provider.invalid", "fixture-token"),
        )
        self.config.start()
        self.addCleanup(self.config.stop)
        self.fetch = patch(
            "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
            return_value={"composeId": self.target_id.target_id},
        )
        self.fetch.start()
        self.addCleanup(self.fetch.stop)
        self.domains = patch(
            "control_plane.dokploy_target_setup_http.fetch_dokploy_target_domains_for_setup",
            return_value=({"host": "APP.EXAMPLE.TEST."}, {"host": "app.example.test"}),
        )
        self.domains.start()
        self.addCleanup(self.domains.stop)

    def make_app(self, policy: LaunchplaneAuthzPolicy) -> Any:
        return create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=policy,
            record_store_factory=lambda: self.store,
            human_session_manager=self.manager,
        )

    async def post(
        self,
        path: str,
        payload: dict[str, Any],
        key: str = "",
        *,
        app: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        browser_headers = (
            _browser_mutation_headers(self.manager, self.session) if headers is None else headers
        )
        if key:
            browser_headers = {**browser_headers, "Idempotency-Key": key}
        return await request(
            app or self.app, "POST", path, payload=payload, headers=browser_headers
        )

    async def domain_plan(self) -> dict[str, Any]:
        response = await self.post("/v1/dokploy-targets/setup", self.domain_payload)
        self.assertEqual(response.status_code, 202, response.text)
        review: dict[str, Any] = response.json()["review"]
        return review

    def domain_apply(self, review: dict[str, Any]) -> dict[str, Any]:
        return {
            **self.domain_payload,
            "mode": "apply",
            "reason": "Recognize the observed lane domains.",
            "confirmation": DokployTargetSetupEnvelope.model_validate(
                self.domain_payload
            ).expected_confirmation,
            "browser_review": {
                "product": "example-product",
                "reviewed_plan_sha256": review["plan_sha256"],
            },
        }

    async def assert_domain_apply_and_immutable_replay_precede_source_drift(self) -> None:
        review = await self.domain_plan()
        self.assertEqual(review["proposed_domains"], ["app.example.test"])
        self.assertNotIn("must-not-escape", str(review))
        applied = self.domain_apply(review)
        response = await self.post("/v1/dokploy-targets/setup", applied, "domain-original")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["result"]["reason"], applied["reason"])
        self.assertEqual(
            response.json()["review"]["expected_confirmation"], applied["confirmation"]
        )
        current = self.store.read_dokploy_target_record(
            context_name=self.target.context, instance_name=self.target.instance
        )
        self.assertEqual(current.domains, ("app.example.test",))
        self.assertEqual(current.env, self.target.env)
        self.assertEqual(
            self.store.read_dokploy_target_id_record(
                context_name=self.target.context, instance_name=self.target.instance
            ),
            self.target_id,
        )
        with patch(
            "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
            side_effect=AssertionError("Replay must not refetch"),
        ):
            self.store.write_dokploy_target_record(
                current.model_copy(update={"domains": ("later.example.test",)})
            )
            replay = await self.post("/v1/dokploy-targets/setup", applied, "domain-original")
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["review"], response.json()["review"])
        self.assertEqual(replay.json()["result"]["reason"], applied["reason"])
        self.assertEqual(replay.json()["original_trace_id"], response.json()["trace_id"])

    async def assert_domain_receipt_failure_rolls_back_records_and_retry_can_complete(self) -> None:
        review = await self.domain_plan()
        applied = self.domain_apply(review)
        with patch.object(
            self.store, "_sync_idempotency_row", side_effect=RuntimeError("receipt storage failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "receipt storage failed"):
                await self.post("/v1/dokploy-targets/setup", applied, "domain-atomic")
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.target.context, instance_name=self.target.instance
            ),
            self.target,
        )
        self.assertIsNone(
            self.store.read_idempotency_record(
                scope=idempotency_scope(self.session.identity),
                route_path="/v1/dokploy-targets/setup",
                idempotency_key="domain-atomic",
            )
        )
        response = await self.post("/v1/dokploy-targets/setup", applied, "domain-atomic")
        self.assertEqual(response.status_code, 202, response.text)

    def seed_binding(self) -> dict[str, Any]:
        target = self.target.model_copy(update={"domains": ("app.example.test",)})
        self.store.write_dokploy_target_record(target)
        self.store.write_provider_target_record(
            ProviderTargetRecord.from_dokploy_records(
                target_record=target, target_id_record=self.target_id
            )
        )
        self.store.write_edge_endpoint_record(_edge_endpoint_record())
        self.store.write_ingress_route_audit_record(_ingress_audit_record())
        return {
            "mode": "dry-run",
            "product": "example-product",
            "context": "example-testing",
            "instance": "web",
            "expected_current": {"state": "absent"},
            "browser_review": {"product": "example-product"},
        }

    async def assert_binding_commit_rejects_intervening_domain_repair(self) -> None:
        payload = self.seed_binding()
        review_response = await self.post("/v1/route-bindings/reconcile", payload)
        self.assertEqual(review_response.status_code, 202, review_response.text)
        review = review_response.json()["review"]
        applied = {
            **payload,
            "mode": "apply",
            "reason": "Recognize route evidence.",
            "confirmation": RouteBindingReconcileEnvelope.model_validate(
                payload
            ).expected_confirmation,
            "browser_review": {
                "product": "example-product",
                "reviewed_plan_sha256": review["plan_sha256"],
            },
        }
        reconcile = self.store.reconcile_route_binding_record

        def intervening(*args: Any, **kwargs: Any) -> Any:
            current = self.store.read_dokploy_target_record(
                context_name=self.target.context, instance_name=self.target.instance
            )
            provider = self.store.read_provider_target_record(
                context_name=self.target.context, instance_name=self.target.instance
            )
            status, _receipt = self.store.commit_dokploy_domain_repair(
                expected_record=current,
                expected_target_id_record=self.target_id,
                expected_provider_target_record=provider,
                domains=("later.example.test",),
                updated_at="2026-08-01T00:00:00Z",
                source_label="service:intervening-domain-repair",
                product="example-product",
                mutation=DbOnlyMutationRequest(
                    scope="fixture",
                    route_path="/v1/dokploy-targets/setup",
                    idempotency_key="intervening-domain",
                    request_fingerprint="intervening-domain-payload",
                    lease_owner="intervening",
                    response_status_code=202,
                    response_trace_id="intervening",
                    response_payload={
                        "status": "accepted",
                        "trace_id": "intervening",
                        "records": {},
                    },
                ),
            )
            self.assertEqual(status, "written")
            return reconcile(*args, **kwargs)

        with patch.object(self.store, "reconcile_route_binding_record", side_effect=intervening):
            response = await self.post(
                "/v1/route-bindings/reconcile", applied, "binding-interleaved"
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "stale")
        with self.assertRaises(FileNotFoundError):
            self.store.read_route_binding_record(
                product="example-product", context_name="example-testing", instance_name="web"
            )
        refusal = self.store.read_idempotency_record(
            scope=idempotency_scope(self.session.identity),
            route_path="/v1/route-bindings/reconcile",
            idempotency_key="binding-interleaved",
        )
        self.assertIsNotNone(refusal)
        assert refusal is not None
        self.assertEqual(refusal.response_status_code, 409)
        replay = await self.post("/v1/route-bindings/reconcile", applied, "binding-interleaved")
        self.assertEqual(replay.status_code, 409, replay.text)
        self.assertEqual(replay.json(), response.json())

    async def assert_atomic_terminal_winner(self, kind: str, winner: str) -> None:
        if kind == "domain":
            review = await self.domain_plan()
            payload = self.domain_apply(review)
            route = "/v1/dokploy-targets/setup"
            method_name = "commit_dokploy_domain_repair"
        else:
            initial = self.seed_binding()
            response = await self.post("/v1/route-bindings/reconcile", initial)
            self.assertEqual(response.status_code, 202, response.text)
            review = response.json()["review"]
            payload = {
                **initial,
                "mode": "apply",
                "reason": "Recognize the recorded route.",
                "confirmation": RouteBindingReconcileEnvelope.model_validate(
                    initial
                ).expected_confirmation,
                "browser_review": {
                    "product": "example-product",
                    "reviewed_plan_sha256": review["plan_sha256"],
                },
            }
            route = "/v1/route-bindings/reconcile"
            method_name = "reconcile_route_binding_record"
        key = f"{kind}-{winner}-original"
        reached = Event()
        release = Event()
        pause_method = method_name if winner == "stale" else "write_idempotency_record_once"
        original = getattr(self.store, pause_method)
        canonical_target = self.store.read_dokploy_target_record(
            context_name=self.target.context, instance_name=self.target.instance
        )
        observed = [{"host": "app.example.test"}]

        def pause_loser(*args: Any, **kwargs: Any) -> Any:
            if current_thread().name.startswith("paused-request"):
                reached.set()
                if not release.wait(5):
                    raise AssertionError("Paused request was not released")
            return original(*args, **kwargs)

        with (
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_domains_for_setup",
                side_effect=lambda *_args: tuple(observed),
            ),
            patch.object(self.store, pause_method, side_effect=pause_loser),
        ):
            if winner == "success":
                if kind == "domain":
                    observed[:] = [{"host": "later.example.test"}]
                else:
                    self.store.write_dokploy_target_record(self.target)
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="paused-request") as executor:
                loser = executor.submit(lambda: asyncio.run(self.post(route, payload, key)))
                try:
                    self.assertTrue(await asyncio.to_thread(reached.wait, 5))
                    if winner == "stale":
                        if kind == "domain":
                            observed[:] = [{"host": "later.example.test"}]
                        else:
                            # The old five planned rows remain unchanged; append a newer eligible audit.
                            self.store.write_ingress_route_audit_record(
                                _ingress_audit_record(
                                    record_id="audit-newer-during-request",
                                    recorded_at="2026-07-13T00:00:00Z",
                                )
                            )
                    elif kind == "domain":
                        observed[:] = [{"host": "app.example.test"}]
                    else:
                        # Restore exact authority (ABA) before the original reviewed request commits.
                        self.store.write_dokploy_target_record(canonical_target)
                    completed = await self.post(route, payload, key)
                    self.assertEqual(
                        completed.status_code, 409 if winner == "stale" else 202, completed.text
                    )
                finally:
                    release.set()
                late = await asyncio.to_thread(loser.result, 5)
        self.assertEqual(late.status_code, completed.status_code, late.text)
        replay = await self.post(route, payload, key)
        self.assertEqual(replay.status_code, completed.status_code, replay.text)
        receipt = self.store.read_idempotency_record(
            scope=idempotency_scope(self.session.identity), route_path=route, idempotency_key=key
        )
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(receipt.response_status_code, completed.status_code)
        if winner == "stale":
            self.assertEqual(late.json(), completed.json())
            self.assertEqual(replay.json(), completed.json())
            self.assertEqual(
                self.store.read_dokploy_target_record(
                    context_name=self.target.context, instance_name=self.target.instance
                ),
                canonical_target,
            )
            if kind == "binding":
                with self.assertRaises(FileNotFoundError):
                    self.store.read_route_binding_record(
                        product="example-product",
                        context_name="example-testing",
                        instance_name="web",
                    )
        else:
            self.assertTrue(late.json()["replayed"])
            self.assertTrue(replay.json()["replayed"])
            self.assertEqual(late.json()["review"], completed.json()["review"])
            self.assertEqual(late.json()["result"]["reason"], payload["reason"])
        conflict = await self.post(
            route, {**payload, "reason": "Different intent under the completed key."}, key
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["error"]["code"], "idempotency_key_reused")


class BrowserRouteRepairTests(_BrowserRouteRepairFixture):
    async def test_domain_apply_and_immutable_replay_precede_source_drift(self) -> None:
        await self.assert_domain_apply_and_immutable_replay_precede_source_drift()

    async def test_domain_rejects_changed_review_and_reuses_no_authority_from_browser(self) -> None:
        review = await self.domain_plan()
        self.store.write_dokploy_target_record(
            self.target.model_copy(update={"domains": ("other.example.test",)})
        )
        stale = await self.post(
            "/v1/dokploy-targets/setup", self.domain_apply(review), "domain-stale"
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["error"]["code"], "stale")
        self.store.write_dokploy_target_record(self.target)
        with patch(
            "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
            side_effect=AssertionError("Terminal refusal must not replan"),
        ):
            replay = await self.post(
                "/v1/dokploy-targets/setup", self.domain_apply(review), "domain-stale"
            )
        self.assertEqual(replay.status_code, 409, replay.text)
        self.assertEqual(replay.json(), stale.json())
        with self.assertRaises(ValueError):
            DokployTargetSetupEnvelope.model_validate(
                {**self.domain_payload, "target_id": "browser-injected"}
            )
        with self.assertRaises(ValueError):
            DokployTargetSetupEnvelope.model_validate(
                {**self.domain_payload, "operation": "create-compose"}
            )

    async def test_domain_receipt_failure_rolls_back_records_and_retry_can_complete(self) -> None:
        await self.assert_domain_receipt_failure_rolls_back_records_and_retry_can_complete()

    async def test_domain_preserves_primary_order_and_readback_is_current(self) -> None:
        self.store.write_dokploy_target_record(
            self.target.model_copy(
                update={"domains": ("https://z.example.test/path", "https://a.example.test/")}
            )
        )
        with patch(
            "control_plane.dokploy_target_setup_http.fetch_dokploy_target_domains_for_setup",
            return_value=(
                {"host": "a.example.test"},
                {"host": "z.example.test"},
                {"host": "0.example.test"},
            ),
        ):
            review = await self.domain_plan()
            self.assertEqual(
                review["proposed_domains"], ["z.example.test", "a.example.test", "0.example.test"]
            )
            self.assertEqual(review["proposed_primary_domain"], "z.example.test")
            response = await self.post(
                "/v1/dokploy-targets/setup", self.domain_apply(review), "domain-primary"
            )
            self.assertEqual(response.status_code, 202, response.text)
            current = await self.domain_plan()
        self.assertEqual(current["status"], "unchanged")
        self.assertEqual(current["current_domains"], review["proposed_domains"])

    async def test_domain_respects_retained_hostname_and_testing_custody(self) -> None:
        for retained in (
            "app.example.test",
            "APP.EXAMPLE.TEST.",
            " \thttps://APP.EXAMPLE.TEST.:8443/path?query#fragment\n",
        ):
            with (
                self.subTest(retained=retained),
                patch.object(self.store, "retained_disposable_hostnames", return_value=(retained,)),
            ):
                response = await self.post("/v1/dokploy-targets/setup", self.domain_payload)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(
                    response.json()["error"]["code"], "domain_repair_evidence_unqualified"
                )
        self.target = self.target.model_copy(update={"instance": "testing"})
        self.target_id = self.target_id.model_copy(
            update={"instance": "testing", "target_id": "compose-testing-custody"}
        )
        self.store.write_dokploy_target_record(self.target)
        self.store.write_dokploy_target_id_record(self.target_id)
        self.store.write_provider_target_record(
            ProviderTargetRecord.from_dokploy_records(
                target_record=self.target, target_id_record=self.target_id
            )
        )
        profile = self.store.read_product_profile_record("example-product")
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "lanes": tuple(
                        lane.model_copy(update={"instance": "testing"}) for lane in profile.lanes
                    )
                }
            )
        )
        self.domain_payload = {**self.domain_payload, "instance": "testing"}
        self.enterContext(
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                return_value={"composeId": self.target_id.target_id},
            )
        )
        review = await self.domain_plan()
        from control_plane.application_state_volume import state_volume_target_key

        self.store.reserve_mutation(
            scope="fixture",
            route_path="/v1/dokploy-targets/setup",
            idempotency_key="held-storage",
            request_fingerprint="held-storage-request",
            lease_owner="fixture",
            lease_seconds=600,
            reconciliation_key="held-storage-plan",
            provider_target_key=state_volume_target_key(self.target.context),
        )
        response = await self.post(
            "/v1/dokploy-targets/setup", self.domain_apply(review), "domain-held"
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.target.context, instance_name="testing"
            ),
            self.target,
        )

    async def test_binding_digest_ignores_evaluation_time_and_replay_survives_drift(self) -> None:
        payload = self.seed_binding()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        reviews = []
        for instant in (now, now + timedelta(seconds=4)):
            with patch(
                "control_plane.http_app.utc_now_timestamp",
                return_value=instant.isoformat().replace("+00:00", "Z"),
            ):
                response = await self.post("/v1/route-bindings/reconcile", payload)
            self.assertEqual(response.status_code, 202, response.text)
            reviews.append(response.json()["review"])
        self.assertEqual(reviews[0]["status"], "ready")
        self.assertEqual(reviews[0]["plan_sha256"], reviews[1]["plan_sha256"])
        applied = {
            **payload,
            "mode": "apply",
            "reason": "Recognize recorded route evidence.",
            "confirmation": RouteBindingReconcileEnvelope.model_validate(
                payload
            ).expected_confirmation,
            "browser_review": {
                "product": "example-product",
                "reviewed_plan_sha256": reviews[0]["plan_sha256"],
            },
        }
        response = await self.post("/v1/route-bindings/reconcile", applied, "binding-original")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["result"]["reason"], applied["reason"])
        self.assertEqual(
            response.json()["review"]["expected_confirmation"], applied["confirmation"]
        )
        readback = await self.post("/v1/route-bindings/reconcile", payload)
        self.assertEqual(readback.status_code, 202, readback.text)
        self.assertEqual(readback.json()["review"]["expected_current"]["state"], "present")
        self.assertEqual(
            readback.json()["review"]["current_record_sha256"],
            response.json()["review"]["proposed_record"]["record_sha256"],
        )
        self.store.write_dokploy_target_record(self.target)
        replay = await self.post("/v1/route-bindings/reconcile", applied, "binding-original")
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["review"], response.json()["review"])

    async def test_binding_rejects_source_drift_before_apply(self) -> None:
        payload = self.seed_binding()
        response = await self.post("/v1/route-bindings/reconcile", payload)
        review = response.json()["review"]
        canonical_target = self.store.read_dokploy_target_record(
            context_name=self.target.context, instance_name=self.target.instance
        )
        self.store.write_dokploy_target_record(self.target)
        applied = {
            **payload,
            "mode": "apply",
            "reason": "Recognize route evidence.",
            "confirmation": RouteBindingReconcileEnvelope.model_validate(
                payload
            ).expected_confirmation,
            "browser_review": {
                "product": "example-product",
                "reviewed_plan_sha256": review["plan_sha256"],
            },
        }
        response = await self.post("/v1/route-bindings/reconcile", applied, "binding-stale")
        self.assertEqual(response.status_code, 409, response.text)
        with self.assertRaises(FileNotFoundError):
            self.store.read_route_binding_record(
                product="example-product", context_name="example-testing", instance_name="web"
            )

        self.store.write_dokploy_target_record(canonical_target)
        with patch(
            "control_plane.route_binding_reconcile.plan_route_binding_reconcile",
            side_effect=AssertionError("Terminal refusal must not replan"),
        ):
            replay = await self.post("/v1/route-bindings/reconcile", applied, "binding-stale")
        self.assertEqual(replay.status_code, 409, replay.text)
        self.assertEqual(replay.json(), response.json())

    async def test_binding_commit_rejects_intervening_domain_repair(self) -> None:
        await self.assert_binding_commit_rejects_intervening_domain_repair()

    async def test_binding_row_cas_refusal_is_terminal_after_other_key_wins(self) -> None:
        payload = self.seed_binding()
        review_response = await self.post("/v1/route-bindings/reconcile", payload)
        self.assertEqual(review_response.status_code, 202, review_response.text)
        review = review_response.json()["review"]
        applied = {
            **payload,
            "mode": "apply",
            "reason": "Recognize recorded route evidence.",
            "confirmation": RouteBindingReconcileEnvelope.model_validate(
                payload
            ).expected_confirmation,
            "browser_review": {
                "product": "example-product",
                "reviewed_plan_sha256": review["plan_sha256"],
            },
        }
        reconcile = self.store.reconcile_route_binding_record

        def competing_binding(*args: Any, **kwargs: Any) -> Any:
            winner = reconcile(
                expected_record=None,
                replacement_record=kwargs["replacement_record"],
                mutation=DbOnlyMutationRequest(
                    scope="fixture",
                    route_path="/v1/route-bindings/reconcile",
                    idempotency_key="competing-binding",
                    request_fingerprint="competing-binding-request",
                    lease_owner="competing",
                    response_status_code=202,
                    response_trace_id="competing",
                    response_payload={"status": "accepted", "trace_id": "competing", "records": {}},
                ),
            )
            self.assertEqual(winner.status, "created")
            return reconcile(*args, **kwargs)

        with patch.object(
            self.store, "reconcile_route_binding_record", side_effect=competing_binding
        ):
            refused = await self.post("/v1/route-bindings/reconcile", applied, "binding-row-cas")
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["error"]["code"], "stale")
        replay = await self.post("/v1/route-bindings/reconcile", applied, "binding-row-cas")
        self.assertEqual(replay.status_code, 409, replay.text)
        self.assertEqual(replay.json(), refused.json())

    async def test_domain_concurrent_terminal_winner_is_immutable(self) -> None:
        for winner in ("stale", "success"):
            with self.subTest(winner=winner):
                await self.assert_atomic_terminal_winner("domain", winner)

    async def test_binding_concurrent_terminal_winner_is_immutable(self) -> None:
        for winner in ("stale", "success"):
            with self.subTest(winner=winner):
                await self.assert_atomic_terminal_winner("binding", winner)

    async def test_refusal_and_commit_storage_failures_preserve_unknown_outcome(self) -> None:
        review = await self.domain_plan()
        applied = self.domain_apply(review)
        with patch.object(
            self.store,
            "commit_dokploy_domain_repair",
            side_effect=ValueError("commit storage failure"),
        ):
            with self.assertRaisesRegex(ValueError, "commit storage failure"):
                await self.post("/v1/dokploy-targets/setup", applied, "commit-unknown")
        with (
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_domains_for_setup",
                return_value=({"host": "later.example.test"},),
            ),
            patch.object(
                self.store,
                "write_idempotency_record_once",
                side_effect=RuntimeError("refusal storage failure"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "refusal storage failure"):
                await self.post("/v1/dokploy-targets/setup", applied, "refusal-unknown")
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.target.context, instance_name=self.target.instance
            ),
            self.target,
        )
        for key in ("commit-unknown", "refusal-unknown"):
            self.assertIsNone(
                self.store.read_idempotency_record(
                    scope=idempotency_scope(self.session.identity),
                    route_path="/v1/dokploy-targets/setup",
                    idempotency_key=key,
                )
            )

    async def test_domain_cas_refusal_is_terminal_without_overwriting_changed_authority(
        self,
    ) -> None:
        review = await self.domain_plan()
        applied = self.domain_apply(review)
        commit = self.store.commit_dokploy_domain_repair
        changed = self.target.model_copy(update={"source_label": "concurrent-authority"})

        def change_before_commit(*args: Any, **kwargs: Any) -> Any:
            self.store.write_dokploy_target_record(changed)
            return commit(*args, **kwargs)

        with patch.object(
            self.store, "commit_dokploy_domain_repair", side_effect=change_before_commit
        ):
            refused = await self.post("/v1/dokploy-targets/setup", applied, "domain-cas-stale")
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["error"]["code"], "stale")
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.target.context, instance_name=self.target.instance
            ),
            changed,
        )
        self.store.write_dokploy_target_record(self.target)
        replay = await self.post("/v1/dokploy-targets/setup", applied, "domain-cas-stale")
        self.assertEqual(replay.status_code, 409, replay.text)
        self.assertEqual(replay.json(), refused.json())

    async def test_browser_requires_csrf_strict_administrator_and_exact_lane(self) -> None:
        for path, payload in (
            ("/v1/dokploy-targets/setup", self.domain_payload),
            ("/v1/route-bindings/reconcile", self.seed_binding()),
        ):
            headers = _browser_mutation_headers(self.manager, self.session)
            headers.pop("X-Launchplane-CSRF", None)
            headers.pop("X-CSRF-Token", None)
            headers.pop("Origin")
            denied = await self.post(path, payload, headers=headers)
            self.assertEqual(denied.status_code, 403, denied.text)
            restricted = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "github_humans": [
                        {
                            "github_ids": [self.session.identity.github_id],
                            "roles": ["admin"],
                            "products": ["example-product"],
                            "contexts": ["example-testing"],
                            "actions": ["route_binding.read"],
                        }
                    ],
                }
            )
            denied = await self.post(path, payload, app=self.make_app(restricted))
            self.assertEqual(denied.status_code, 403, denied.text)
        mismatch = {**self.domain_payload, "browser_review": {"product": "other-product"}}
        denied = await self.post("/v1/dokploy-targets/setup", mismatch)
        self.assertEqual(denied.status_code, 404, denied.text)
