"""The administrator adapter admits the common native operation without provider writes."""

import asyncio
from threading import Event
from typing import Any
import unittest
from dataclasses import replace
from functools import partial
from email.message import Message
from urllib.error import HTTPError
from unittest.mock import patch

from httpx2 import Response

from control_plane.build_provenance import VerifiedBuildArtifact
from control_plane.contracts.artifact_identity import ArtifactSourceBuild
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.http_routes import testing_rebuild as routes
from control_plane.http_routes.mutation_support import idempotency_scope
from control_plane import testing_rebuild_preflight as preflight
from control_plane import odoo_target_replacement_apply_http as admission
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from tests import test_testing_rebuild_worker as worker_tests
from tests import test_odoo_prod_promotion_operation as administrator_tests
from tests.http_app_test_support import (
    _browser_mutation_headers,
    _github_human_identity,
    _github_oauth_config,
    _RejectingVerifier,
)
from tests.support.http import LIFESPAN_HANG_DEADLINE_SECONDS, lifespan_client, request


class TestingRebuildHttpFixture(unittest.TestCase):
    def assert_conditional_plan(self, response: Response, code: str) -> None:
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "pending")
        self.assertEqual(response.json()["blockers"][0]["code"], code)
        self.assertTrue(response.json()["steps"])

    def setUp(self) -> None:
        self.fixture = worker_tests.NativeTestingWorkerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.product = self.fixture.operation.product
        self.policy = administrator_tests._ADMINISTRATOR_POLICY
        self.store.seed_authz_policy_if_absent(administrator_tests._policy_record(self.policy))
        self.sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        self.session = self.sessions.issue(_github_human_identity())
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=self.policy,
            record_store_factory=lambda: self.store,
            human_session_manager=self.sessions,
            control_plane_root_path=self.fixture.root,
        )
        artifact = self.fixture.fixture.fixture.artifact
        self.manifest = artifact.model_copy(
            update={
                "artifact_id": "ui-current-build",
                "source_commit": "c" * 40,
                "source_build": ArtifactSourceBuild(
                    repository=self.fixture.intent.profile.repository,
                    repository_id="12345",
                    workflow_path=".github/workflows/build.yml",
                    event="push",
                    purpose="release",
                    run_id=55,
                    run_attempt=1,
                    github_artifact_id=66,
                    manifest_artifact_id="producer-build",
                ),
            }
        )
        self.latest = self.enterContext(
            patch.object(
                routes,
                "latest_verified_testing_release",
                return_value=VerifiedBuildArtifact(manifest=self.manifest),
            )
        )
        self.enterContext(
            patch.object(preflight, "capture_rebuild_intent", return_value=self.fixture.intent)
        )
        self.url = f"/v1/products/{self.product}/testing-rebuild"
        self.payload: dict[str, object] = {
            "data_source_mode": "empty",
            "artifact_id": self.manifest.artifact_id,
            "source_git_ref": self.manifest.source_commit,
            "reason": "Rebuild disposable testing",
        }

    async def call(
        self,
        method: str,
        payload: dict[str, object] | None = None,
        *,
        key: str = "button-one",
        path: str = "",
        csrf: bool = True,
    ) -> Response:
        headers = _browser_mutation_headers(self.sessions, self.session)
        headers["Idempotency-Key"] = key
        if not csrf:
            headers.pop("X-CSRF-Token", None)
        return await request(self.app, method, path or self.url, headers=headers, payload=payload)


class TestingRebuildHttpTests(TestingRebuildHttpFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(
            patch(
                "control_plane.product_reconcile.latest_verified_testing_release",
                side_effect=self.latest,
            )
        )

    def test_migrated_candidate_plan_does_not_compare_against_current_release(self) -> None:
        from tests.test_migrated_testing_source import MigratedTestingRequestTests

        source = self.manifest.source_build
        assert source is not None
        candidate = self.manifest.model_copy(
            update={
                "artifact_id": (
                    f"artifact-{self.fixture.operation.context}-run-{source.run_id}-{source.run_attempt}"
                ),
                "source_commit": "d" * 40,
                "source_build": source.model_copy(
                    update={"event": "pull_request", "purpose": "preview", "pull_request_number": 8}
                ),
            }
        )
        migrated = MigratedTestingRequestTests().request().migrated_source
        assert migrated is not None
        replacement = self.fixture.operation.request.model_copy(
            update={
                "artifact_id": candidate.artifact_id,
                "source_git_ref": candidate.source_commit,
                "data_source_mode": "migrated_restore",
                "allow_empty_data": False,
                "migrated_source": migrated.model_copy(
                    update={
                        "product": self.product,
                        "context": self.fixture.operation.context,
                        "target_image": candidate.image.digest,
                    }
                ),
            }
        )
        with (
            patch(
                "control_plane.product_reconcile.verified_testing_rebuild_artifact",
                return_value=VerifiedBuildArtifact(manifest=candidate),
            ),
            patch.object(preflight, "_inspect_execution", side_effect=lambda *args: args[4]),
        ):
            plan, verified = preflight.inspect_current_testing_rebuild(
                root=self.fixture.root,
                store=self.store,
                request=replacement,
                actor="fixture-agent",
            )
        self.assertEqual(
            (plan.artifact_id, plan.source_git_ref),
            (candidate.artifact_id, candidate.source_commit),
        )
        self.assertEqual(verified.manifest, candidate)
        with self.assertRaises(FileNotFoundError):
            self.store.read_artifact_manifest(candidate.artifact_id)
        self.assertEqual(self.fixture.effects, [])

    def test_invalid_source_and_build_are_refused_before_conditional_planning(self) -> None:
        replacement = self.fixture.operation.request.model_copy(
            update={
                "artifact_id": self.manifest.artifact_id,
                "source_git_ref": self.manifest.source_commit,
            }
        )
        for changes, expected in (
            ({"data_source_mode": "existing"}, ValueError),
            ({"artifact_id": "not-the-verified-build"}, ValueError),
        ):
            with (
                self.subTest(changes=changes),
                patch.object(preflight, "capture_rebuild_intent") as capture,
                self.assertRaises(expected),
            ):
                preflight.inspect_testing_rebuild(
                    root=self.fixture.root,
                    store=self.store,
                    request=replacement.model_copy(update=changes),
                    verified_build=VerifiedBuildArtifact(manifest=self.manifest),
                )
            capture.assert_not_called()
        self.assertEqual(self.fixture.effects, [])

    def test_current_rebuild_rejects_non_testing_and_in_place_before_build_reads(self) -> None:
        for changes in ({"instance": "prod"}, {"strategy": "recreate-in-place"}):
            with (
                self.subTest(changes=changes),
                patch(
                    "control_plane.product_reconcile.latest_verified_testing_release",
                    side_effect=AssertionError("Invalid rebuilds must not read a build"),
                ),
                self.assertRaises(ValueError),
            ):
                preflight.inspect_current_testing_rebuild(
                    root=self.fixture.root,
                    store=self.store,
                    request=self.fixture.operation.request.model_copy(update=changes),
                    actor="fixture-agent",
                )

    def settlement_payload(self) -> dict[str, object]:
        return {
            **self.payload,
            "original_scope": idempotency_scope(_github_human_identity()),
        }

    async def test_exclusion_is_durable_and_cannot_be_reused_for_another_intent(self) -> None:
        payload = self.settlement_payload()
        before = self.store.list_odoo_stable_target_replacement_operation_records()
        excluded = await self.call("POST", payload, path=self.url + "/settle")
        self.assertEqual(excluded.status_code, 200, excluded.text)
        self.assertEqual(excluded.json()["outcome"], "excluded")
        repeated = await self.call("POST", payload, path=self.url + "/settle")
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["outcome"], "excluded")
        for changed, status in (
            ({"reason": "Another intent"}, 409),
            ({"original_scope": "another-actor"}, 403),
        ):
            refused = await self.call("POST", {**payload, **changed}, path=self.url + "/settle")
            self.assertEqual(refused.status_code, status, refused.text)
            self.assertNotIn("operation_id", refused.text)
        late = await self.call("POST", self.payload)
        self.assertEqual(late.status_code, 409, late.text)
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        self.assertEqual(self.fixture.effects, [])

    async def test_settlement_observes_admitted_operation_without_cancelling_it(self) -> None:
        accepted = await self.call("POST", self.payload)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        settled = await self.call("POST", self.settlement_payload(), path=self.url + "/settle")
        self.assertEqual(settled.status_code, 200, settled.text)
        self.assertEqual(settled.json()["outcome"], "observed")
        self.assertEqual(settled.json()["operation"], accepted.json()["operation"])
        again = await self.call("POST", self.payload)
        self.assertEqual(again.json()["operation"], accepted.json()["operation"])
        self.assertEqual(self.fixture.effects, [])

    async def test_actor_binding_replays_the_legacy_original_request(self) -> None:
        accepted = await self.call("POST", self.payload)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        bound = await self.call("POST", self.settlement_payload())
        self.assertEqual(bound.status_code, 200, bound.text)
        self.assertEqual(bound.json()["operation"], accepted.json()["operation"])

    async def test_settlement_requires_original_actor_binding_and_csrf(self) -> None:
        before = self.store.list_odoo_stable_target_replacement_operation_records()
        missing_binding = await self.call("POST", self.payload, path=self.url + "/settle")
        self.assertEqual(missing_binding.status_code, 400, missing_binding.text)
        missing_csrf = await self.call(
            "POST", self.settlement_payload(), path=self.url + "/settle", csrf=False
        )
        self.assertEqual(missing_csrf.status_code, 403, missing_csrf.text)
        foreign_submit = await self.call(
            "POST", {**self.payload, "original_scope": "another-actor"}
        )
        self.assertEqual(foreign_submit.status_code, 403, foreign_submit.text)
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        self.assertEqual(self.fixture.effects, [])

    async def test_settlement_excludes_original_held_before_admission(self) -> None:
        entered = asyncio.Event()
        release = Event()
        loop = asyncio.get_running_loop()
        inspect = preflight.inspect_testing_rebuild

        def held_inspection(**kwargs: Any) -> object:
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(LIFESPAN_HANG_DEADLINE_SECONDS):
                raise AssertionError("Original inspection was not released")
            return inspect(**kwargs)

        def headers() -> dict[str, str]:
            current = _browser_mutation_headers(self.sessions, self.session)
            current["Idempotency-Key"] = "settlement-race"
            return current

        before = self.store.list_odoo_stable_target_replacement_operation_records()
        with patch.object(preflight, "inspect_testing_rebuild", side_effect=held_inspection):
            async with lifespan_client(self.app) as client:
                original = asyncio.create_task(
                    request(client, "POST", self.url, headers=headers(), payload=self.payload)
                )
                try:
                    await asyncio.wait_for(entered.wait(), LIFESPAN_HANG_DEADLINE_SECONDS)
                    absent = await request(
                        client,
                        "POST",
                        self.url + "/lookup",
                        headers=headers(),
                        payload=self.payload,
                    )
                    self.assertEqual(absent.status_code, 404, absent.text)
                    settled = await request(
                        client,
                        "POST",
                        self.url + "/settle",
                        headers=headers(),
                        payload=self.settlement_payload(),
                    )
                    self.assertEqual(settled.status_code, 200, settled.text)
                    self.assertEqual(settled.json()["outcome"], "excluded")
                    self.assertFalse(original.done())
                finally:
                    release.set()
                    late = await original
                self.assertEqual(late.status_code, 409, late.text)
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        self.assertEqual(self.fixture.effects, [])

    async def test_director_rebuild_uses_current_verified_build_and_preserves_original_receipt(
        self,
    ) -> None:
        await self.exercise_director_migrated_rebuild(candidate=False)

    async def test_director_candidate_rebuild_retains_provenance_outside_release_store(
        self,
    ) -> None:
        await self.exercise_director_migrated_rebuild(candidate=True)

    async def exercise_director_migrated_rebuild(self, *, candidate: bool) -> None:
        from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget
        from tests.test_migrated_testing_source import MigratedTestingRequestTests
        from control_plane.service_auth import (
            BearerIdentityConfig,
            configured_local_operator_identity,
            launchplane_identity_actor,
        )
        from control_plane.odoo_target_replacement_apply_http import (
            ODOO_TARGET_REPLACEMENT_APPLY_ROUTE,
        )
        from control_plane.odoo_target_replacement_plan_http import (
            ODOO_TARGET_REPLACEMENT_PLAN_ROUTE,
        )
        from control_plane.contracts.odoo_stable_target_replacement import (
            OdooStableTargetReplacementRequest,
        )

        if candidate:
            build = self.manifest.source_build
            assert build is not None
            self.manifest = self.manifest.model_copy(
                update={
                    "artifact_id": f"artifact-{self.fixture.operation.context}-run-{build.run_id}-{build.run_attempt}",
                    "source_build": build.model_copy(
                        update={
                            "event": "pull_request",
                            "purpose": "preview",
                            "pull_request_number": 8,
                        }
                    ),
                }
            )

        config = BearerIdentityConfig(
            local_operator_token="fixture-operator",
            local_operator_subject="fixture-agent",
            local_operator_token_label="fixture-agent-token",
        )
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=self.policy,
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.fixture.root,
            bearer_identity_config=config,
        )
        source = MigratedTestingRequestTests().request().migrated_source
        assert source is not None
        profile = self.store.read_product_profile_record(self.product)
        testing = next(lane for lane in profile.lanes if lane.instance == "testing")
        testing.odoo_testing_drill_storage = OdooTestingDrillStorageBudget(
            database_bytes=1024, filestore_bytes=1024, staging_bytes=1024, reserve_bytes=1024
        )
        self.store.write_product_profile_record(profile)
        self.fixture.intent = self.fixture.intent.model_copy(update={"profile": profile})
        replacement = self.fixture.operation.request.model_copy(
            update={
                "reason": "Rebuild the disposable testing copy",
                "artifact_id": self.manifest.artifact_id,
                "source_git_ref": self.manifest.source_commit,
                "data_source_mode": "migrated_restore",
                "allow_empty_data": False,
                "migrated_source": source.model_copy(
                    update={
                        "product": self.product,
                        "context": self.fixture.operation.context,
                        "target_image": self.manifest.image.digest,
                    }
                ),
            }
        )
        self.enterContext(
            patch(
                "control_plane.product_reconcile.verified_testing_rebuild_artifact",
                return_value=VerifiedBuildArtifact(manifest=self.manifest),
            )
        )
        identity = configured_local_operator_identity(config)
        assert identity is not None
        hold = self.fixture.apply_staff_hold(
            True,
            "Stage an independent empty copy",
            purpose="isolation_staging",
            actor=launchplane_identity_actor(identity),
        )
        intent = self.fixture.intent.model_copy(update={"before": self.fixture.current()})
        self.enterContext(patch.object(preflight, "capture_rebuild_intent", return_value=intent))
        from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
        from control_plane.product_reconcile import ProductReconcileError

        for failure, status in (
            (NativeSourceAuthorizationDenied("Fixture refused the source read"), 403),
            (ProductReconcileError("Fixture unavailable", code="testing_build_unavailable"), 409),
        ):
            for route in (ODOO_TARGET_REPLACEMENT_PLAN_ROUTE, ODOO_TARGET_REPLACEMENT_APPLY_ROUTE):
                with (
                    self.subTest(failure=type(failure), route=route),
                    patch(
                        "control_plane.product_reconcile.verified_testing_rebuild_artifact",
                        side_effect=failure,
                    ),
                ):
                    body = replacement.model_dump(
                        mode="json",
                        include=set(OdooStableTargetReplacementRequest.model_fields)
                        if route == ODOO_TARGET_REPLACEMENT_PLAN_ROUTE
                        else None,
                    )
                    refused = await request(
                        app,
                        "POST",
                        route,
                        headers={
                            "Authorization": "Bearer fixture-operator",
                            "Idempotency-Key": "unavailable-build",
                        },
                        payload={"product": self.product, "replacement": body},
                    )
                    self.assertEqual(refused.status_code, status, refused.text)
        inspected = await request(
            app,
            "POST",
            ODOO_TARGET_REPLACEMENT_PLAN_ROUTE,
            headers={"Authorization": "Bearer fixture-operator"},
            payload={
                "product": self.product,
                "replacement": replacement.model_dump(
                    mode="json", include=set(OdooStableTargetReplacementRequest.model_fields)
                ),
            },
        )
        self.assertEqual(inspected.status_code, 202, inspected.text)
        with self.assertRaises(FileNotFoundError):
            self.store.read_artifact_manifest(self.manifest.artifact_id)
        assert replacement.migrated_source is not None
        for changed, expected_status in (
            ({"artifact_id": "unverified-build"}, 400),
            ({"source_git_ref": "f" * 40}, 400),
            (
                {
                    "migrated_source": replacement.migrated_source.model_copy(
                        update={"target_image": "sha256:" + "f" * 64}
                    )
                },
                409,
            ),
            (
                {
                    "migrated_source": replacement.migrated_source.model_copy(
                        update={"context": "other-context"}
                    )
                },
                409,
            ),
            (
                {
                    "migrated_source": replacement.migrated_source.model_copy(
                        update={"product": "other-product"}
                    )
                },
                400,
            ),
        ):
            with self.subTest(changed=changed):
                mismatched = replacement.model_copy(update=changed).model_dump(
                    mode="json", include=set(OdooStableTargetReplacementRequest.model_fields)
                )
                refused = await request(
                    app,
                    "POST",
                    ODOO_TARGET_REPLACEMENT_PLAN_ROUTE,
                    headers={"Authorization": "Bearer fixture-operator"},
                    payload={"product": self.product, "replacement": mismatched},
                )
                self.assertEqual(refused.status_code, expected_status, refused.text)
        with self.assertRaises(FileNotFoundError):
            self.store.read_artifact_manifest(self.manifest.artifact_id)
        ordinary = await request(
            app,
            "POST",
            ODOO_TARGET_REPLACEMENT_APPLY_ROUTE,
            headers={"Authorization": "Bearer fixture-operator", "Idempotency-Key": "ordinary"},
            payload={
                "product": self.product,
                "replacement": replacement.model_copy(
                    update={
                        "strategy": "recreate-in-place",
                        "data_source_mode": "empty",
                        "allow_empty_data": True,
                        "migrated_source": None,
                    }
                ).model_dump(mode="json"),
            },
        )
        self.assertEqual(ordinary.status_code, 403, ordinary.text)
        response = await request(
            app,
            "POST",
            ODOO_TARGET_REPLACEMENT_APPLY_ROUTE,
            headers={
                "Authorization": "Bearer fixture-operator",
                "Idempotency-Key": "native-director-rebuild",
            },
            payload={"product": self.product, "replacement": replacement.model_dump(mode="json")},
        )
        self.assertEqual(response.status_code, 202, response.text)
        if candidate:
            with self.assertRaises(FileNotFoundError):
                self.store.read_artifact_manifest(self.manifest.artifact_id)
        else:
            self.assertEqual(
                self.store.read_artifact_manifest(self.manifest.artifact_id), self.manifest
            )
        operations = self.store.list_odoo_stable_target_replacement_operation_records()
        saved = next(
            item for item in operations if item.idempotency_key == "native-director-rebuild"
        )
        self.assertEqual(saved.request, replacement)
        self.assertEqual(saved.testing_candidate_manifest, self.manifest if candidate else None)
        if candidate:
            with self.assertRaises(ValueError):
                self.store.write_odoo_stable_target_replacement_operation_record(
                    saved.model_copy(update={"testing_candidate_manifest": None})
                )
        self.assertTrue(saved.is_native_testing_rebuild)
        self.assertEqual(self.fixture.effects, [])
        self.assertEqual(self.fixture.current().target.policies.staff_testing_hold, hold)
        with patch(
            "control_plane.product_reconcile.verified_testing_rebuild_artifact",
            side_effect=AssertionError("An original request must not rediscover its build"),
        ):
            replayed = await request(
                app,
                "POST",
                ODOO_TARGET_REPLACEMENT_APPLY_ROUTE,
                headers={
                    "Authorization": "Bearer fixture-operator",
                    "Idempotency-Key": "native-director-rebuild",
                },
                payload={
                    "product": self.product,
                    "replacement": replacement.model_dump(mode="json"),
                },
            )
        self.assertEqual(replayed.status_code, 202, replayed.text)
        if candidate:
            from control_plane.contracts.testing_rebuild import REBUILD_SCOPE, REBUILD_ROUTE

            held = saved.model_copy(
                update={
                    "status": "reconciliation_required",
                    "error_code": "authorization_denied",
                    "error_message": "Fixture interrupted before preparation",
                }
            )
            self.store.write_odoo_stable_target_replacement_operation_record(held)
            reserved = self.store.reserve_mutation(
                scope=REBUILD_SCOPE,
                route_path=REBUILD_ROUTE,
                idempotency_key=held.operation_id,
                request_fingerprint=held.request_fingerprint,
                lease_owner="fixture-worker",
                reconciliation_key="original-candidate-provenance",
            )
            assert reserved.record is not None
            marked = self.store.mark_mutation_reconcile_required(
                reservation=reserved.record,
                reconciliation_key="original-candidate-provenance",
            )
            assert marked.record is not None
            outer = marked.record
            with patch(
                "control_plane.product_reconcile.verified_testing_rebuild_artifact",
                side_effect=AssertionError("Resume must keep the original candidate"),
            ):
                resumed = await request(
                    app,
                    "POST",
                    self.url + "/operations/" + held.operation_id + "/resume",
                    headers={
                        "Authorization": "Bearer fixture-operator",
                        "Idempotency-Key": held.idempotency_key,
                    },
                    payload={
                        "expected_updated_at": held.updated_at,
                        "expected_attempt": held.attempt,
                    },
                )
            self.assertEqual(resumed.status_code, 200, resumed.text)
            current = self.store.read_odoo_stable_target_replacement_operation_record(
                saved.operation_id
            )
            self.assertEqual(current.testing_candidate_manifest, saved.testing_candidate_manifest)
            self.assertEqual(current.request, saved.request)
            self.assertEqual(current.idempotency_key, saved.idempotency_key)
            self.assertEqual(
                self.store.read_idempotency_record(
                    scope=REBUILD_SCOPE, route_path=REBUILD_ROUTE, idempotency_key=held.operation_id
                ),
                outer,
            )
        cancelled = await request(
            app,
            "POST",
            f"/v1/drivers/odoo/target-replacement/operations/{saved.operation_id}/cancel",
            headers={"Authorization": "Bearer fixture-operator"},
            payload={"reason": "Withdraw the pending testing rebuild"},
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(
                saved.operation_id
            ).status,
            "cancelled",
        )
        from control_plane.contracts.odoo_testing_drill import TESTING_STORAGE_BUDGET_REQUIRED

        profile = self.store.read_product_profile_record(self.product)
        profile.preview.clean_testing_snapshot = True
        testing = next(lane for lane in profile.lanes if lane.instance == "testing")
        testing.odoo_testing_drill_storage = None
        self.store.write_product_profile_record(profile)
        before = self.store.list_odoo_stable_target_replacement_operation_records()
        for mode in ("production_restore", "upstream_restore"):
            with self.subTest(source=mode):
                refused = await request(
                    app,
                    "POST",
                    ODOO_TARGET_REPLACEMENT_APPLY_ROUTE,
                    headers={"Authorization": "Bearer fixture-operator", "Idempotency-Key": mode},
                    payload={
                        "product": self.product,
                        "replacement": replacement.model_copy(
                            update={"data_source_mode": mode, "migrated_source": None}
                        ).model_dump(mode="json"),
                    },
                )
                self.assertEqual(refused.status_code, 409, refused.text)
                self.assertEqual(refused.json()["error"]["code"], TESTING_STORAGE_BUDGET_REQUIRED)
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        self.assertEqual(self.fixture.effects, [])

    async def test_native_preflight_exposes_fixed_checkpoint_without_private_exception(
        self,
    ) -> None:
        from control_plane.testing_rebuild_service import TestingRebuildRefused
        from control_plane.odoo_target_replacement_plan_http import (
            ODOO_TARGET_REPLACEMENT_PLAN_ROUTE,
        )
        from control_plane.service_auth import BearerIdentityConfig
        from control_plane.contracts.odoo_stable_target_replacement import (
            OdooStableTargetReplacementRequest,
        )

        before = self.store.list_odoo_stable_target_replacement_operation_records()
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=self.policy,
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.fixture.root,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="fixture-operator",
                local_operator_subject="fixture-agent",
                local_operator_token_label="fixture-agent-token",
            ),
        )
        failure = TestingRebuildRefused(
            "testing_route_compilation_failed",
            "The observed testing ingress cannot be safely compiled for its retained copy.",
        )
        with patch.object(preflight, "capture_rebuild_intent", side_effect=failure):
            response = await request(
                app,
                "POST",
                ODOO_TARGET_REPLACEMENT_PLAN_ROUTE,
                headers={"Authorization": "Bearer fixture-operator"},
                payload={
                    "product": self.product,
                    "replacement": self.fixture.operation.request.model_copy(
                        update={
                            "artifact_id": self.manifest.artifact_id,
                            "source_git_ref": self.manifest.source_commit,
                        }
                    ).model_dump(
                        mode="json", include=set(OdooStableTargetReplacementRequest.model_fields)
                    ),
                },
            )
        self.assertEqual(response.status_code, 202, response.text)
        result = response.json()["result"]
        self.assertEqual(result["blockers"][0]["code"], failure.code)
        self.assertEqual(result["status"], "pending")
        self.assertTrue(response.json()["trace_id"])
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        self.assertEqual(self.fixture.effects, [])

    async def test_lookup_rejects_missing_or_malformed_body_without_changing_operations(
        self,
    ) -> None:
        before = self.store.list_odoo_stable_target_replacement_operation_records()
        for body in (b"", b'{"replacement":'):
            with self.subTest(body=body):
                headers = _browser_mutation_headers(self.sessions, self.session)
                headers.update({"Idempotency-Key": "invalid-body", "Content-Type": "text/plain"})
                response = await request(
                    self.app,
                    "POST",
                    self.url + "/lookup",
                    headers=headers,
                    raw_body=body,
                    capture_server_error_response=True,
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        self.assertEqual(self.fixture.effects, [])

    async def test_director_agent_cannot_queue_odoo_through_product_native_route(self) -> None:
        from tests.http_app_test_support import _local_operator_bearer_config

        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=self.policy,
            bearer_identity_config=_local_operator_bearer_config(),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.fixture.root,
        )
        before = self.store.list_odoo_stable_target_replacement_operation_records()
        refused = await request(
            app,
            "POST",
            self.url,
            headers={
                "Authorization": "Bearer local-operator-token",
                "Idempotency-Key": "native-odoo-agent",
            },
            payload=self.payload,
        )
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        self.assertEqual(self.fixture.effects, [])

    async def test_director_reads_preserve_original_binding_and_mutation_guards(self) -> None:
        from control_plane.service_auth import (
            BearerIdentityConfig,
            configured_local_operator_identity,
            configured_terminal_agent_identity,
        )
        from control_plane.contracts.environment_inventory import EnvironmentInventory
        from control_plane.contracts.promotion_record import (
            ArtifactIdentityReference,
            DeploymentEvidence,
        )
        from tests.support.auth import scoped_agent_transport

        config = BearerIdentityConfig(
            local_operator_token="fixture-director",
            local_operator_subject="fixture-agent",
            local_operator_token_label="local-lookup",
            terminal_agent_token="fixture-terminal",
            terminal_agent_subject="fixture-terminal",
            terminal_agent_token_label="terminal-lookup",
        )
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=self.policy,
            bearer_identity_config=config,
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.fixture.root,
        )
        artifact = self.fixture.fixture.fixture.artifact
        self.store.write_environment_inventory(
            EnvironmentInventory(
                context=self.fixture.operation.context,
                instance="testing",
                artifact_identity=ArtifactIdentityReference(artifact_id=artifact.artifact_id),
                source_git_ref=artifact.source_commit,
                deploy=DeploymentEvidence(
                    deploy_mode="compose",
                    target_type="compose",
                    status="pass",
                    target_name=self.fixture.intent.before.target.target_name,
                ),
                updated_at=self.fixture.operation.created_at,
                deployment_record_id="lookup-serving-build",
            )
        )
        read_only_build = self.manifest.model_copy(
            update={"artifact_id": "read-only-current-build"}
        )
        for omit_defaults in (False, True):
            with self.subTest(omitted_defaults=omit_defaults):
                self.latest.return_value = VerifiedBuildArtifact(manifest=self.manifest)
                key = f"native-lookup-{omit_defaults}"
                headers = {"Authorization": "Bearer fixture-director", "Idempotency-Key": key}
                replacement = self.fixture.operation.request.model_copy(
                    update={
                        "reason": "Rebuild the disposable testing copy",
                        "no_cache": True,
                        "artifact_id": self.manifest.artifact_id,
                        "source_git_ref": self.manifest.source_commit,
                        "expected_current_artifact_id": artifact.artifact_id,
                    }
                ).model_dump(mode="json", exclude_defaults=omit_defaults)
                body: dict[str, object] = {"product": self.product, "replacement": replacement}
                admitted = await request(
                    app,
                    "POST",
                    admission.ODOO_TARGET_REPLACEMENT_APPLY_ROUTE,
                    headers=headers,
                    payload=body,
                )
                self.assertEqual(admitted.status_code, 202, admitted.text)
                original = next(
                    item
                    for item in self.store.list_odoo_stable_target_replacement_operation_records()
                    if item.idempotency_key == key
                )
                self.store.write_odoo_stable_target_replacement_operation_record(
                    original.model_copy(
                        update={
                            "status": "fail",
                            "phase": "failed",
                            "error_message": "Fixture completed without a candidate",
                            "finished_at": original.updated_at,
                        }
                    )
                )
                self.latest.return_value = VerifiedBuildArtifact(manifest=read_only_build)
                before = self.store.list_odoo_stable_target_replacement_operation_records()
                operation_path = self.url + "/operations/" + original.operation_id
                for token, configure, lookup_status in (
                    ("fixture-director", configured_local_operator_identity, 200),
                    ("fixture-terminal", configured_terminal_agent_identity, 404),
                ):
                    identity = configure(config)
                    assert identity is not None
                    caller_headers = {**headers, "Authorization": "Bearer " + token}
                    for method, path, payload in (
                        ("GET", self.url + "/inputs", None),
                        ("GET", operation_path, None),
                        ("POST", self.url + "/lookup", body),
                    ):
                        observed = await request(
                            app, method, path, headers=caller_headers, payload=payload
                        )
                        self.assertEqual(
                            observed.status_code,
                            lookup_status if method == "POST" else 200,
                            observed.text,
                        )
                        if method == "GET" and path.endswith("inputs"):
                            self.assertEqual(
                                observed.json()["original_scope"], idempotency_scope(identity)
                            )
                        if method == "POST" and lookup_status == 200:
                            self.assertEqual(
                                observed.json()["operation"]["operation_id"], original.operation_id
                            )
                        with scoped_agent_transport():
                            refused = await request(
                                app, method, path, headers=caller_headers, payload=payload
                            )
                        self.assertEqual(refused.status_code, 403, refused.text)
                    for path in (self.url, self.url + "/settle"):
                        refused = await request(
                            app,
                            "POST",
                            path,
                            headers=caller_headers,
                            payload={**self.payload, "original_scope": idempotency_scope(identity)},
                        )
                        self.assertEqual(refused.status_code, 403, refused.text)
                for change in ({"reason": "other intent"}, {"no_cache": False}):
                    refused = await request(
                        app,
                        "POST",
                        self.url + "/lookup",
                        headers=headers,
                        payload={**body, "replacement": {**replacement, **change}},
                    )
                    self.assertEqual(refused.status_code, 409, refused.text)
                normalized = (
                    {**replacement, "verify_logo": True}
                    if omit_defaults
                    else {
                        name: value for name, value in replacement.items() if name != "verify_logo"
                    }
                )
                refused = await request(
                    app,
                    "POST",
                    self.url + "/lookup",
                    headers=headers,
                    payload={**body, "replacement": normalized},
                )
                self.assertEqual(refused.status_code, 409, refused.text)
                foreign = await request(
                    app,
                    "GET",
                    operation_path.replace(self.product, "other-product"),
                    headers=headers,
                )
                self.assertEqual(foreign.status_code, 404, foreign.text)
                wrong_shape = await request(
                    app, "POST", self.url + "/lookup", headers=headers, payload=self.payload
                )
                self.assertEqual(wrong_shape.status_code, 400, wrong_shape.text)
                human_wrong_shape = await self.call(
                    "POST", body, path=self.url + "/lookup", key=key
                )
                self.assertEqual(human_wrong_shape.status_code, 400, human_wrong_shape.text)
                with patch(
                    "control_plane.http_routes.testing_rebuild.read_active_authz_policy_record",
                    side_effect=LookupError,
                ):
                    refused = await request(app, "GET", self.url + "/inputs", headers=headers)
                self.assertEqual(refused.status_code, 503, refused.text)
                self.assertEqual(
                    self.store.list_odoo_stable_target_replacement_operation_records(), before
                )
                with self.assertRaises(FileNotFoundError):
                    self.store.read_artifact_manifest(read_only_build.artifact_id)
                self.assertEqual(self.fixture.effects, [])

    async def test_read_only_preflight_does_not_admit_artifact_or_candidate(self) -> None:
        from control_plane import testing_rebuild_service as service
        from control_plane.odoo_testing_usage import OdooTestingUsage

        before = self.store.list_odoo_stable_target_replacement_operation_records()
        response = await self.call("GET", path=self.url + "/inputs")
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["base_url"], "https://" + self.fixture.fixture.route.host)
        self.assertEqual(response.json()["artifact_id"], self.manifest.artifact_id)
        self.assertEqual(response.json()["candidate_verification"], "pending")
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        with self.assertRaises(FileNotFoundError):
            self.store.read_artifact_manifest(self.manifest.artifact_id)
        with patch.object(
            service, "read_odoo_testing_usage", return_value=OdooTestingUsage("in_use")
        ):
            refused = await self.call("POST", self.payload)
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        with self.assertRaises(FileNotFoundError):
            self.store.read_artifact_manifest(self.manifest.artifact_id)
        self.assertEqual(self.fixture.effects, [])

    async def test_signed_in_staging_preflight_and_submit_keep_the_original_hold(self) -> None:
        from control_plane.service_auth import launchplane_identity_actor

        hold = self.fixture.apply_staff_hold(
            True,
            "Prepare the independent empty testing copy",
            purpose="isolation_staging",
            actor=launchplane_identity_actor(self.session.identity),
        )
        intent = self.fixture.intent.model_copy(update={"before": self.fixture.current()})
        with patch.object(preflight, "capture_rebuild_intent", return_value=intent):
            inspected = await self.call("GET", path=self.url + "/inputs")
            submitted = await self.call("POST", self.payload)
        self.assertEqual(inspected.status_code, 200, inspected.text)
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(self.fixture.current().target.policies.staff_testing_hold, hold)
        self.assertEqual(self.fixture.effects, [])

    async def test_snapshot_hold_without_budget_refuses_before_enqueue(self) -> None:
        from control_plane.contracts.odoo_testing_drill import TESTING_STORAGE_BUDGET_REQUIRED

        profile = self.fixture.intent.profile.model_copy(deep=True)
        profile.preview.clean_testing_snapshot = True
        testing = next(lane for lane in profile.lanes if lane.instance == "testing")
        testing.odoo_testing_drill_storage = None
        intent = self.fixture.intent.model_copy(update={"profile": profile})
        before = self.store.list_odoo_stable_target_replacement_operation_records()
        with (
            patch.object(preflight, "capture_rebuild_intent", return_value=intent),
            patch.object(preflight, "native_testing_source_authority") as source,
        ):
            for mode in ("production_restore", "upstream_restore"):
                with self.subTest(source=mode):
                    self.payload["data_source_mode"] = mode
                    inspected = await self.call(
                        "GET", path=self.url + "/inputs?data_source_mode=" + mode
                    )
                    submitted = await self.call("POST", self.payload, key=mode)
                    self.assert_conditional_plan(inspected, TESTING_STORAGE_BUDGET_REQUIRED)
                    self.assertEqual(submitted.status_code, 409, submitted.text)
        source.assert_not_called()
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        self.assertEqual(self.fixture.effects, [])

    async def test_submit_admission_disposition_is_independent_of_inspection_checkpoint(
        self,
    ) -> None:
        from control_plane.testing_rebuild_service import TestingRebuildRefused

        before = self.store.list_odoo_stable_target_replacement_operation_records()
        failure = TestingRebuildRefused(
            "testing_route_compilation_failed", "Testing route compilation refused."
        )
        with patch.object(preflight, "capture_rebuild_intent", side_effect=failure):
            inspected = await self.call("GET", path=self.url + "/inputs")
            submitted = await self.call("POST", self.payload)
        self.assert_conditional_plan(inspected, failure.code)
        self.assertEqual(submitted.status_code, 409, submitted.text)
        self.assertEqual(submitted.json()["error"]["code"], "testing_rebuild_not_ready")
        self.assertEqual(submitted.json()["error"]["message"], str(failure))
        self.assertEqual(submitted.json()["error"]["reason"], failure.code)
        self.assertTrue(submitted.json()["trace_id"])
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        self.assertEqual(self.fixture.effects, [])

    async def test_provider_denial_and_unavailable_preflight_cannot_queue(self) -> None:
        import click
        from control_plane.dokploy import api
        from control_plane.dokploy.native_copy_stream import (
            NativeSourceUnavailable,
            NativeProgramFailed,
        )
        from control_plane.copy_readback_diagnostics import ReadbackPredicate

        before = self.store.list_odoo_stable_target_replacement_operation_records()
        for failure, status in (
            (
                api.DokployRequestFailed(
                    method="GET",
                    path="/api/compose.one",
                    detail="private-provider-message",
                    status_code=403,
                ),
                403,
            ),
            (
                api.DokployRequestFailed(
                    method="GET",
                    path="/api/compose.one",
                    detail="private-provider-message",
                    status_code=503,
                ),
                409,
            ),
            (click.ClickException("private-bootstrap-message"), 409),
            (ValueError("private-retained-identity-message"), 409),
            (FileNotFoundError("private-retained-record"), 409),
            (NativeSourceUnavailable("private-transient-observation"), 409),
            (
                NativeProgramFailed(
                    "private-value", predicate=ReadbackPredicate.CUSTODY, kind="refused"
                ),
                409,
            ),
        ):
            for method in ("GET", "POST"):
                with (
                    self.subTest(failure=type(failure).__name__, method=method),
                    patch.object(preflight, "capture_rebuild_intent", side_effect=failure),
                ):
                    response = await self.call(
                        method,
                        self.payload if method == "POST" else None,
                        path=self.url if method == "POST" else self.url + "/inputs",
                    )
                    code = (
                        "authorization_denied"
                        if status == 403
                        else "testing_rebuild_not_ready"
                        if method == "POST"
                        else "testing_rebuild_observation_unavailable"
                        if isinstance(failure, NativeSourceUnavailable)
                        else "testing_rebuild_inputs_unverified"
                    )
                    if method == "GET" and status != 403:
                        self.assert_conditional_plan(response, code)
                    else:
                        self.assertEqual(response.status_code, status, response.text)
                        self.assertTrue(response.json()["trace_id"])
                        self.assertEqual(response.json()["error"]["code"], code)
                    if isinstance(failure, NativeProgramFailed):
                        self.assertIn(failure.code, response.text)
                    self.assertNotIn("private-", response.text)
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        self.assertEqual(self.fixture.effects, [])

    async def test_pre_admission_enqueue_validation_has_a_fixed_refusal(self) -> None:
        before = self.store.list_odoo_stable_target_replacement_operation_records()
        with patch.object(
            admission,
            "_validate_target_replacement_apply_artifact_authority",
            side_effect=ValueError("private-revalidated-artifact"),
        ):
            response = await self.call("POST", self.payload)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "testing_rebuild_not_ready")
        self.assertTrue(response.json()["trace_id"])
        self.assertNotIn("private-", response.text)
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)

    async def test_lost_create_reply_keeps_the_admitted_operation_uncertain(self) -> None:
        create = self.store.create_odoo_stable_target_replacement_operation_record_if_no_active_lane

        def committed(
            record: OdooStableTargetReplacementOperationRecord,
        ) -> tuple[OdooStableTargetReplacementOperationRecord, bool]:
            create(record)
            raise ValueError("fixture lost database reply after commit")

        with (
            patch.object(
                self.store,
                "create_odoo_stable_target_replacement_operation_record_if_no_active_lane",
                side_effect=committed,
            ),
            self.assertRaises(ValueError),
        ):
            await self.call("POST", self.payload)
        saved = next(
            operation
            for operation in self.store.list_odoo_stable_target_replacement_operation_records()
            if operation.idempotency_key == "button-one"
        )
        self.assertTrue(saved.is_native_testing_rebuild)
        observed = await self.call("POST", self.payload, path=self.url + "/lookup")
        self.assertEqual(observed.status_code, 200, observed.text)
        self.assertEqual(observed.json()["operation"]["operation_id"], saved.operation_id)

    async def test_one_submit_replay_and_poll_keep_original_build_and_reason(self) -> None:
        accepted = await self.call("POST", self.payload)
        self.assertEqual(accepted.status_code, 200, accepted.json())
        operation = accepted.json()["operation"]
        saved = self.store.read_odoo_stable_target_replacement_operation_record(
            operation["operation_id"]
        )
        self.assertTrue(saved.is_native_testing_rebuild)
        self.assertEqual(saved.request.artifact_id, self.manifest.artifact_id)
        self.assertEqual(saved.request.reason, self.payload["reason"])
        self.assertIsNotNone(saved.authorization)
        self.latest.side_effect = AssertionError("A replay must not choose another build")
        replay = await self.call("POST", self.payload)
        self.assertEqual(replay.status_code, 200, replay.json())
        self.assertEqual(replay.json()["operation"], operation)
        polled = await self.call("GET", path=self.url + "/operations/" + operation["operation_id"])
        self.assertEqual(polled.status_code, 200, polled.json())
        self.assertEqual(polled.json()["operation"], operation)
        self.assertNotIn("request", polled.json()["operation"])
        self.assertEqual(self.fixture.effects, [])

    async def test_original_key_lookup_only_reads_and_never_admits_missing_or_changed_intent(
        self,
    ) -> None:
        before = self.store.list_odoo_stable_target_replacement_operation_records()
        missing_csrf = await self.call("POST", self.payload, path=self.url + "/lookup", csrf=False)
        self.assertEqual(missing_csrf.status_code, 403, missing_csrf.text)
        absent = await self.call("POST", self.payload, path=self.url + "/lookup")
        self.assertEqual(absent.status_code, 404, absent.text)
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        accepted = await self.call("POST", self.payload)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.latest.side_effect = AssertionError("Lookup cannot reselect, prepare or enqueue")
        looked_up = await self.call("POST", self.payload, path=self.url + "/lookup")
        self.assertEqual(looked_up.status_code, 200, looked_up.text)
        self.assertEqual(looked_up.json()["operation"], accepted.json()["operation"])
        changed = await self.call(
            "POST", {**self.payload, "reason": "Changed intent"}, path=self.url + "/lookup"
        )
        self.assertEqual(changed.status_code, 409, changed.text)
        self.assertNotIn(accepted.json()["operation"]["operation_id"], changed.text)
        self.assertEqual(self.fixture.effects, [])

    async def test_absent_lookup_does_not_exclude_an_original_submission_in_flight(self) -> None:
        entered = asyncio.Event()
        release = Event()
        loop = asyncio.get_running_loop()
        inspect = preflight.inspect_testing_rebuild

        def held_inspection(**kwargs: Any) -> object:
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(LIFESPAN_HANG_DEADLINE_SECONDS):
                raise AssertionError("The test did not release its original inspection")
            return inspect(**kwargs)

        def headers() -> dict[str, str]:
            current = _browser_mutation_headers(self.sessions, self.session)
            current["Idempotency-Key"] = "original-in-flight"
            return current

        with patch.object(preflight, "inspect_testing_rebuild", side_effect=held_inspection):
            async with lifespan_client(self.app) as client:
                original = asyncio.create_task(
                    request(client, "POST", self.url, headers=headers(), payload=self.payload)
                )
                try:
                    await asyncio.wait_for(entered.wait(), LIFESPAN_HANG_DEADLINE_SECONDS)
                    absent = await request(
                        client,
                        "POST",
                        self.url + "/lookup",
                        headers=headers(),
                        payload=self.payload,
                    )
                    self.assertEqual(absent.status_code, 404, absent.text)
                    self.assertFalse(original.done())
                finally:
                    release.set()
                    accepted = await original
                self.assertTrue(accepted.is_success, accepted.text)
                found = await request(
                    client, "POST", self.url + "/lookup", headers=headers(), payload=self.payload
                )
                self.assertEqual(
                    found.json()["operation"]["operation_id"],
                    accepted.json()["operation"]["operation_id"],
                )
        self.assertEqual(self.fixture.effects, [])

    async def test_missing_csrf_and_non_testing_input_cannot_queue(self) -> None:
        before = self.store.list_odoo_stable_target_replacement_operation_records()
        missing = await self.call("POST", self.payload, csrf=False)
        self.assertEqual(missing.status_code, 403, missing.json())
        for extra in ({"instance": "prod"}, {"target_id": "foreign"}, {"reason": "  "}):
            result = await self.call("POST", {**self.payload, **extra})
            self.assertEqual(result.status_code, 400, result.json())
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        self.assertEqual(self.fixture.effects, [])

    async def test_changed_build_refuses_without_admitting_old_or_new_manifest(self) -> None:
        self.latest.return_value = VerifiedBuildArtifact(
            manifest=self.manifest.model_copy(update={"source_commit": "d" * 40})
        )
        response = await self.call("POST", self.payload)
        self.assertEqual(response.status_code, 409, response.json())
        with self.assertRaises(FileNotFoundError):
            self.store.read_artifact_manifest(self.manifest.artifact_id)
        self.assertEqual(self.fixture.effects, [])

    async def test_another_signed_in_person_cannot_inspect_or_submit(self) -> None:
        self.session = self.sessions.issue(replace(_github_human_identity(), github_id=124))
        for method, path, payload in (
            ("GET", self.url + "/inputs", None),
            ("POST", self.url, self.payload),
            ("POST", self.url + "/lookup", self.payload),
            ("POST", self.url + "/settle", self.settlement_payload()),
        ):
            result = await self.call(method, payload, path=path)
            self.assertEqual(result.status_code, 403, result.json())
        self.latest.assert_not_called()
        self.assertEqual(self.fixture.effects, [])

    async def test_expired_saved_file_positive_cannot_wedge_a_fresh_non_use_plan(self) -> None:
        from datetime import UTC, datetime, timedelta
        from control_plane import testing_rebuild_service as service
        from control_plane.odoo_testing_usage import OdooTestingUsage
        from tests.support.disposable_lanes import record_safety

        context = self.fixture.operation.context
        now = datetime.now(UTC)
        fresh = OdooTestingUsage("unused", False, False, "complete")
        for name, valid_until, blocked in (
            ("expired_observation", now - timedelta(seconds=1), False),
            ("live_observation", now + timedelta(minutes=5), True),
        ):
            with self.subTest(name=name):
                record_safety(
                    self.store,
                    self.product,
                    context,
                    "testing",
                    usage="in_use",
                    usage_actor="odoo-user:8, odoo-user:9",
                    usage_valid_until=valid_until,
                    observed_at=now - timedelta(minutes=5),
                )
                with patch.object(service, "read_odoo_testing_usage", return_value=fresh):
                    inspected = await self.call("GET", path=self.url + "/inputs")
                self.assertEqual(inspected.status_code, 200, inspected.json())
                codes = [item["code"] for item in inspected.json()["blockers"]]
                self.assertEqual("testing_in_use" in codes, blocked, codes)

    async def test_known_use_returns_conditional_plan_but_blocks_submission(self) -> None:
        from datetime import UTC, datetime, timedelta
        from control_plane import testing_rebuild_service as service
        from control_plane.odoo_testing_usage import OdooTestingUsage

        since = datetime.now(UTC) - timedelta(minutes=1)
        expiry = since + timedelta(minutes=5)
        with patch.object(
            service,
            "read_odoo_testing_usage",
            return_value=OdooTestingUsage(
                "in_use", actors=("odoo-user:42",), since=since, valid_until=expiry
            ),
        ):
            inspected = await self.call("GET", path=self.url + "/inputs")
            submitted = await self.call("POST", self.payload)
        self.assertEqual(inspected.status_code, 200, inspected.json())
        plan = inspected.json()
        self.assertEqual(plan["status"], "pending")
        self.assertEqual(plan["blockers"][0]["code"], "testing_in_use")
        for fact in ("odoo-user:42", since.isoformat(), expiry.isoformat()):
            self.assertIn(fact, plan["blockers"][0]["message"])
        self.assertTrue(plan["steps"])
        self.assertEqual(plan["base_url"], self.fixture.intent.profile.lanes[0].base_url)
        self.assertEqual(submitted.status_code, 409, submitted.json())
        with self.assertRaises(FileNotFoundError):
            self.store.read_artifact_manifest(self.manifest.artifact_id)
        self.assertEqual(self.fixture.effects, [])

    async def test_verified_down_historical_use_allows_preflight_and_the_same_queue(self) -> None:
        from datetime import UTC, datetime
        from control_plane import testing_rebuild_service as service
        from control_plane.odoo_testing_usage import OdooTestingUsage
        from tests.support.down_lanes import record_lane_health
        from tests.support.disposable_lanes import record_safety

        before = {
            item.operation_id
            for item in self.store.list_odoo_stable_target_replacement_operation_records(
                product=self.product
            )
        }
        record_lane_health(
            self.store, self.product, self.fixture.operation.context, datetime.now(UTC)
        )
        record_safety(
            self.store, self.product, self.fixture.operation.context, "testing", usage="in_use"
        )
        intent = self.fixture.intent.model_copy(
            update={"profile": self.store.read_product_profile_record(self.product)}
        )
        with (
            patch.object(preflight, "capture_rebuild_intent", return_value=intent),
            patch.object(
                service, "read_odoo_testing_usage", return_value=OdooTestingUsage("in_use")
            ),
        ):
            inspected = await self.call("GET", path=self.url + "/inputs")
            submitted = await self.call("POST", self.payload)
        self.assertEqual(inspected.status_code, 200, inspected.json())
        self.assertTrue(submitted.is_success, submitted.json())
        self.assertEqual(self.fixture.effects, [])
        operations = self.store.list_odoo_stable_target_replacement_operation_records(
            product=self.product
        )
        operation_id = submitted.json()["operation"]["operation_id"]
        self.assertEqual({item.operation_id for item in operations} - before, {operation_id})
        operation = next(item for item in operations if item.operation_id == operation_id)
        self.assertTrue(operation.is_native_testing_rebuild)

    async def test_provider_read_denial_keeps_trace_and_does_not_enqueue(self) -> None:
        from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied

        self.latest.side_effect = NativeSourceAuthorizationDenied("Fixture provider refusal")
        response = await self.call("POST", self.payload)
        self.assertEqual(response.status_code, 403, response.json())
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
        self.assertTrue(response.json()["trace_id"])
        with self.assertRaises(FileNotFoundError):
            self.store.read_artifact_manifest(self.manifest.artifact_id)
        self.assertEqual(self.fixture.effects, [])

    async def test_generated_credential_provenance_is_checked_without_rotating_values(self) -> None:
        binding = next(
            item
            for item in self.store.list_secret_bindings(
                context_name=self.fixture.operation.context, instance_name="testing"
            )
            if item.value_source == "generated"
        )
        before = self.store.read_secret_record(binding.secret_id)
        self.store.write_secret_binding(binding.model_copy(update={"value_source": "supplied"}))
        response = await self.call("GET", path=self.url + "/inputs")
        self.assert_conditional_plan(response, "testing_credentials_unverified")
        self.assertEqual(self.store.read_secret_record(binding.secret_id), before)
        self.assertEqual(self.fixture.effects, [])

    async def test_upstream_preflight_refuses_missing_source_configuration(self) -> None:
        path = self.url + "/inputs?data_source_mode=upstream_restore"
        configured = await self.call("GET", path=path)
        self.assertEqual(configured.status_code, 200, configured.json())
        record = next(
            item
            for item in self.store.list_runtime_environment_records(
                context_name=self.fixture.operation.context, instance_name="testing"
            )
            if item.scope == "instance" and item.instance == "testing"
        )
        self.store.write_runtime_environment_record(
            record.model_copy(
                update={
                    "env": {
                        key: value
                        for key, value in record.env.items()
                        if key != "ODOO_UPSTREAM_HOST"
                    }
                }
            )
        )
        refused = await self.call("GET", path=path)
        self.assert_conditional_plan(refused, "testing_restore_source_unconfigured")
        self.assertEqual(self.fixture.effects, [])

    def test_current_build_reader_stops_at_denial_without_selecting_another_build(self) -> None:
        from control_plane import product_reconcile
        from control_plane.build_provenance import BuildProvenanceError
        from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
        from unittest.mock import Mock

        calls: list[str] = []

        def denied(path: str) -> object:
            calls.append(path)
            raise BuildProvenanceError(
                "Fixture read refused", code="github_read_failed"
            ) from HTTPError("https://api.github.com/read", 403, "Forbidden", Message(), None)

        reader = product_reconcile._TestingRebuildBuildReads(Mock(get_json=denied))
        with self.assertRaises(NativeSourceAuthorizationDenied):
            # This is the selector's invalid-build fallback behavior. A genuine
            # denial must escape it before a read of another candidate.
            try:
                reader.get_json("current")
            except BuildProvenanceError:
                reader.get_json("older")
        self.assertEqual(calls, ["current"])

    def test_rate_limit_or_expired_storage_url_stops_selection_without_identity_denial(
        self,
    ) -> None:
        from control_plane import product_reconcile
        from control_plane.build_provenance import BuildProvenanceError
        from unittest.mock import Mock

        for url, headers, retry_at in (
            ("https://api.github.com/repos/example/actions", {"X-RateLimit-Remaining": "0"}, ""),
            ("https://api.github.com/repos/example/actions", {"Retry-After": "60"}, ""),
            ("https://artifacts.example.invalid/archive", {}, ""),
            (
                "https://api.github.com/repos/example/actions",
                {"X-RateLimit-Remaining": "0"},
                "2026-11-03T21:11:49Z",
            ),
            (
                "https://api.github.com/repos/example/actions",
                {"Retry-After": ""},
                "2026-11-03T21:11:49Z",
            ),
        ):
            calls: list[str] = []
            values = Message()
            for key, value in headers.items():
                values[key] = value

            def unavailable(path: str) -> object:
                calls.append(path)
                raise BuildProvenanceError(
                    "fixture temporary read failure", code="github_read_failed", retry_at=retry_at
                ) from HTTPError(url, 403, "Forbidden", values, None)

            reader = product_reconcile._TestingRebuildBuildReads(Mock(get_json=unavailable))
            with (
                self.subTest(url=url, headers=headers, retry_at=retry_at),
                self.assertRaises(product_reconcile.ProductReconcileError),
            ):
                try:
                    reader.get_json("current")
                except BuildProvenanceError:
                    reader.get_json("older")
            self.assertEqual(calls, ["current"])

    async def test_new_machine_admission_requires_reason_but_original_receipt_still_replays(
        self,
    ) -> None:
        from control_plane.odoo_target_replacement_apply_http import (
            OdooTargetReplacementApplyEnvelope,
            enqueue_odoo_target_replacement_apply_operation,
        )

        accepted = await self.call("POST", self.payload)
        self.assertEqual(accepted.status_code, 200, accepted.json())
        saved = self.store.read_odoo_stable_target_replacement_operation_record(
            accepted.json()["operation"]["operation_id"]
        )
        assert saved.authorization is not None
        no_reason = OdooTargetReplacementApplyEnvelope(
            product=saved.product, replacement=saved.request.model_copy(update={"reason": ""})
        )
        enqueue = partial(
            enqueue_odoo_target_replacement_apply_operation,
            record_store=self.store,
            request=no_reason,
            context=saved.context,
            idempotency_scope=saved.idempotency_scope,
            request_fingerprint=saved.request_fingerprint,
            created_at=saved.created_at,
            authorization=saved.authorization,
        )
        # Read an already admitted receipt before checking new-operation prerequisites.
        _, replayed = enqueue(idempotency_key=saved.idempotency_key)
        self.assertEqual(replayed["operation_id"], saved.operation_id)
        with self.assertRaises(ValueError):
            enqueue(idempotency_key="new-machine-request")
        self.assertEqual(self.fixture.effects, [])

    async def test_native_admission_requires_a_caller_who_can_resume(self) -> None:
        from control_plane.contracts.durable_operation_authorization import (
            DurableOperationAuthorization,
        )
        from control_plane.durable_operation_authorization import (
            DurableOperationAuthorizationDeniedError,
        )

        accepted = await self.call("POST", self.payload)
        self.assertEqual(accepted.status_code, 200, accepted.json())
        saved = self.store.read_odoo_stable_target_replacement_operation_record(
            accepted.json()["operation"]["operation_id"]
        )
        assert saved.authorization is not None
        workflow = DurableOperationAuthorization.model_validate(
            {
                **saved.authorization.model_dump(mode="json"),
                "grant": "policy_rule",
                "managed_set_id": "fixture-workflow-set",
                "managed_rule_id": "fixture-workflow-rule",
                "caller": {
                    "identity_type": "github_actions",
                    "subject": "fixture-workflow",
                    "repository": "example/product",
                    "workflow_ref": "example/product/.github/workflows/testing.yml@main",
                },
            }
        )
        # Historical receipts remain readable, without admitting another effect.
        _, replayed = admission.enqueue_odoo_target_replacement_apply_operation(
            record_store=self.store,
            request=admission.OdooTargetReplacementApplyEnvelope(
                product=saved.product, replacement=saved.request
            ),
            context=saved.context,
            idempotency_key=saved.idempotency_key,
            idempotency_scope=saved.idempotency_scope,
            request_fingerprint=saved.request_fingerprint,
            created_at=saved.created_at,
            authorization=workflow,
        )
        self.assertEqual(replayed["operation_id"], saved.operation_id)
        with self.assertRaises(DurableOperationAuthorizationDeniedError):
            admission.build_odoo_target_replacement_apply_operation_record(
                replacement_request=saved.request,
                context=saved.context,
                idempotency_key="new-workflow-request",
                idempotency_scope=saved.idempotency_scope,
                request_fingerprint=saved.request_fingerprint,
                created_at=saved.created_at,
                authorization=workflow,
            )
        self.assertEqual(self.fixture.effects, [])

    async def test_queue_race_reports_the_same_key_conflict_as_the_initial_lookup(self) -> None:
        accepted = await self.call("POST", self.payload)
        self.assertEqual(accepted.status_code, 200, accepted.json())
        saved = self.store.read_odoo_stable_target_replacement_operation_record(
            accepted.json()["operation"]["operation_id"]
        )
        self.store.write_odoo_stable_target_replacement_operation_record(
            saved.model_copy(
                update={
                    "status": "fail",
                    "phase": "failed",
                    "finished_at": saved.created_at,
                    "error_message": "The fixture worker failed before provider effects.",
                }
            )
        )
        different = {**self.payload, "reason": "A different rebuild intent"}
        initial = await self.call("POST", different)
        self.assertEqual(initial.status_code, 409, initial.json())
        # Another request can commit between the handler's lookup and queue CAS.
        # Keep the real queue's second lookup and immutable request check.
        with patch.object(
            routes,
            "find_odoo_target_replacement_apply_operation_by_idempotency_key",
            return_value=None,
        ):
            raced = await self.call("POST", different)
        self.assertEqual(raced.status_code, initial.status_code, raced.json())
        self.assertEqual(raced.json()["error"]["code"], initial.json()["error"]["code"])
        self.assertEqual(self.fixture.effects, [])

    async def test_receipt_projects_the_saved_operation_without_extra_helper_fields(self) -> None:
        from control_plane.odoo_target_replacement_apply_http import (
            enqueue_odoo_target_replacement_apply_operation as enqueue,
        )

        def without_full_payload(**kwargs: object) -> tuple[dict[str, object], dict[str, object]]:
            from typing import Any, cast

            records, payload = enqueue(**cast(Any, kwargs))
            return records, {**payload, "provider_detail": "not a browser operation field"}

        with patch.object(
            routes,
            "enqueue_odoo_target_replacement_apply_operation",
            side_effect=without_full_payload,
        ):
            response = await self.call("POST", self.payload)
        self.assertEqual(response.status_code, 200, response.json())
        operation = response.json()["operation"]
        saved = self.store.read_odoo_stable_target_replacement_operation_record(
            operation["operation_id"]
        )
        self.assertEqual(operation["status"], saved.status)
        self.assertNotIn("provider_detail", operation)
        self.assertEqual(self.fixture.effects, [])


class GenericWebTestingRebuildHttpTests(
    TestingRebuildHttpFixture, unittest.IsolatedAsyncioTestCase
):
    def setUp(self) -> None:
        super().setUp()
        from control_plane import product_reconcile
        from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
        from tests import test_product_reconcile as build_tests

        payload = self.fixture.intent.profile.model_dump(mode="json")
        payload.update(
            driver_id="generic-web",
            repository=build_tests.REPOSITORY,
            repository_id=build_tests.REPOSITORY_ID,
            repository_owner_id="1",
            image={"repository": build_tests.IMAGE_REPOSITORY},
        )
        self.profile = LaunchplaneProductProfileRecord.model_validate(payload)
        self.declare_state({"kind": "none"})
        self.store.write_repository_inventory_record(build_tests._inventory())
        self.github = build_tests.FakeGenericWebGitHub()
        self.github.add_run(30, build_tests.NEWEST)
        self.enterContext(
            patch.object(
                product_reconcile, "resolve_build_provenance_transport", return_value=self.github
            )
        )
        self.enterContext(
            patch.object(
                routes,
                "latest_verified_testing_release",
                product_reconcile.latest_verified_testing_release,
            )
        )
        self.artifact_id = (
            f"{build_tests.IMAGE_REPOSITORY}@{build_tests._digest(build_tests.NEWEST)}"
        )
        self.payload.update(artifact_id=self.artifact_id, source_git_ref=build_tests.NEWEST)
        self.enterContext(
            patch.object(
                preflight,
                "capture_rebuild_intent",
                side_effect=self.fixture.capture_driver_function,
            )
        )
        self.provider_config = self.enterContext(
            patch.object(preflight, "record_verified_build_artifact")
        )

    def declare_state(self, state: dict[str, str] | None) -> None:
        from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord

        payload = self.profile.model_dump(mode="json")
        for lane in payload["lanes"]:
            if lane["instance"] == "testing":
                lane["generic_web_testing_state"] = state
        self.profile = LaunchplaneProductProfileRecord.model_validate(payload)
        self.store.write_product_profile_record(self.profile)

    async def test_none_and_volume_admission_remain_read_only_until_execution_exists(self) -> None:
        from control_plane.dokploy import source
        from control_plane.testing_rebuild_driver import TESTING_REBUILD_EXECUTION_UNAVAILABLE_CODE

        before = self.store.list_odoo_stable_target_replacement_operation_records()
        before_reservations = self.store.list_held_provider_target_reservations()
        for state in ({"kind": "none"}, {"kind": "volume", "container_path": "/app/data"}):
            with self.subTest(state=state), patch.object(source, "read_dokploy_config") as provider:
                self.declare_state(state)
                response = await self.call("GET", path=self.url + "/inputs")
                self.assertEqual(response.status_code, 200, response.text)
                inspected = response.json()
                self.assertNotEqual(inspected["status"], "pass")
                self.assertEqual(inspected["artifact_id"], self.payload["artifact_id"])
                self.assertEqual(inspected["source_git_ref"], self.payload["source_git_ref"])
                self.assertEqual(inspected["context"], self.fixture.operation.context)
                submitted = await self.call("POST", self.payload)
                self.assertEqual(submitted.status_code, 409, submitted.text)
                self.assertEqual(
                    submitted.json()["error"]["reason"], TESTING_REBUILD_EXECUTION_UNAVAILABLE_CODE
                )
                provider.assert_not_called()
        self.provider_config.assert_not_called()
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        self.assertEqual(self.store.list_held_provider_target_reservations(), before_reservations)
        with self.assertRaises(FileNotFoundError):
            self.store.read_artifact_manifest(self.artifact_id)
        self.assertEqual(self.fixture.effects, [])

    async def test_unknown_postgresql_and_restore_refuse_before_provider_reads(self) -> None:
        from control_plane.dokploy import source

        for state, source_mode in (
            (None, "empty"),
            ({"kind": "postgresql"}, "empty"),
            ({"kind": "none"}, "production_restore"),
            ({"kind": "volume", "container_path": "/app/data"}, "upstream_restore"),
        ):
            with (
                self.subTest(state=state, source=source_mode),
                patch.object(source, "read_dokploy_config") as provider,
            ):
                self.declare_state(state)
                response = await self.call(
                    "GET", path=self.url + "/inputs?data_source_mode=" + source_mode
                )
                if source_mode != "empty":
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["status"], "pending")
                    self.assertTrue(response.json()["blockers"])
                else:
                    self.assertEqual(response.status_code, 409, response.text)
                provider.assert_not_called()
        self.assertEqual(self.fixture.effects, [])

    async def test_recorded_protection_blocks_generic_web_inspection(self) -> None:
        from tests.support.disposable_lanes import record_safety

        for facts in (
            {"usage": "in_use", "data": "unknown", "production_resources": "unknown"},
            {"usage": "unknown", "data": "only_copy", "production_resources": "unknown"},
            {"usage": "unknown", "data": "unknown", "production_resources": "shared"},
        ):
            with self.subTest(facts=facts):
                record_safety(
                    self.store, self.product, self.fixture.operation.context, "testing", **facts
                )
                response = await self.call("GET", path=self.url + "/inputs")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "pending")
                self.assertTrue(response.json()["blockers"])
                self.assertEqual((await self.call("POST", self.payload)).status_code, 409)
        self.assertEqual(self.fixture.effects, [])

    async def test_application_lane_reports_support_from_declared_state(self) -> None:
        from control_plane.contracts.product_environment_read_model import (
            build_product_environment_detail,
        )

        context = self.fixture.operation.context
        target = self.store.read_dokploy_target_record(
            context_name=context, instance_name="testing"
        )
        provider = self.store.read_provider_target_record(
            context_name=context, instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            target.model_copy(update={"target_type": "application"})
        )
        self.store.write_provider_target_record(
            provider.model_copy(update={"target_category": "application"})
        )
        response = await self.call("GET", path=self.url + "/inputs")
        self.assert_conditional_plan(response, "testing_rebuild_state_unverified")
        for state, supported in (
            ({"kind": "none"}, True),
            (None, False),
            ({"kind": "postgresql"}, False),
        ):
            with self.subTest(state=state):
                self.declare_state(state)
                with patch(
                    "control_plane.contracts.product_environment_read_model.split_ingress_monitoring_lane",
                    side_effect=lambda _profile, lane, _binding: lane.model_copy(
                        update={"health_url": "https://private.invalid/ready"}
                    ),
                ):
                    detail = build_product_environment_detail(
                        record_store=self.store,
                        product=self.product,
                        environment="testing",
                        action_allowed=lambda *_: True,
                    )
                self.assertEqual(detail.testing_rebuild_supported, supported)

    async def test_missing_product_is_a_bounded_preflight_and_submission_refusal(self) -> None:
        from control_plane.dokploy import source

        missing = "/v1/products/absent-product/testing-rebuild"
        with patch.object(source, "read_dokploy_config") as provider:
            for method, path, payload in (
                ("GET", missing + "/inputs", None),
                ("POST", missing, self.payload),
            ):
                with self.subTest(method=method):
                    response = await self.call(method, payload, path=path)
                    self.assertEqual(response.status_code, 409, response.text)
            provider.assert_not_called()
        self.assertEqual(self.fixture.effects, [])

    def test_worker_refuses_generic_web_before_reserving_or_dispatching_effects(self) -> None:
        from unittest.mock import Mock
        from control_plane import testing_rebuild_service as service
        from control_plane.dokploy import source
        from control_plane.testing_rebuild_driver import TESTING_REBUILD_EXECUTION_UNAVAILABLE_CODE

        operation = self.fixture.operation.model_copy(update={"operation_id": "generic-fence"})
        before = self.store.list_held_provider_target_reservations()
        with (
            patch.object(
                service, "capture_rebuild_intent", side_effect=self.fixture.capture_driver_function
            ),
            patch.object(source, "read_dokploy_config") as provider,
            self.assertRaises(service.TestingRebuildRefused) as refused,
        ):
            service.execute_native_testing_rebuild(
                root=self.fixture.root,
                store=self.store,
                operation=operation,
                authorize=Mock(),
                lease_owner="test-worker",
            )
        self.assertEqual(refused.exception.code, TESTING_REBUILD_EXECUTION_UNAVAILABLE_CODE)
        provider.assert_not_called()
        self.assertEqual(self.store.list_held_provider_target_reservations(), before)
        self.assertEqual(self.fixture.effects, [])

    async def test_real_build_verifier_refuses_wrong_image_repository_without_effects(self) -> None:
        from control_plane.dokploy import source

        changed = self.profile.model_copy(
            update={
                "image": self.profile.image.model_copy(
                    update={"repository": "ghcr.io/example/different"}
                )
            }
        )
        self.store.write_product_profile_record(changed)
        with (
            patch.object(self.github, "get_bytes", wraps=self.github.get_bytes) as manifest_read,
            patch.object(source, "read_dokploy_config") as provider,
        ):
            response = await self.call("GET", path=self.url + "/inputs")
        self.assertEqual(response.status_code, 409, response.text)
        manifest_read.assert_called()
        provider.assert_not_called()
        self.provider_config.assert_not_called()
        self.assertEqual(self.fixture.effects, [])

    async def test_director_agent_reads_real_generic_web_build_without_mutation(self) -> None:
        from control_plane.service_auth import BearerIdentityConfig
        from control_plane.dokploy import source

        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=self.policy,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="fixture-director",
                local_operator_subject="fixture-agent",
                local_operator_token_label="generic-read",
            ),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.fixture.root,
        )
        with patch.object(source, "read_dokploy_config") as provider:
            response = await request(
                app,
                "GET",
                self.url + "/inputs",
                headers={"Authorization": "Bearer fixture-director"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["artifact_id"], self.payload["artifact_id"])
        provider.assert_not_called()
        self.assertEqual(self.fixture.effects, [])
