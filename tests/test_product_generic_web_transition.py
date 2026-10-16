import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.product_generic_web_transition import (
    ProductGenericWebTransitionRequest,
    build_generic_web_transition_plan,
)
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore, ProductProfileCompareWriteResult
from tests.http_app_test_support import (
    _AsgiResponse,
    _RejectingVerifier,
    _asgi_request,
    _browser_mutation_headers,
    _github_human_identity,
    _github_human_product_config_policy,
    _github_oauth_config,
)
from tests.support.profiles import _generic_site_profile_payload
from tests.support.stores import sqlite_database_url


def profile() -> LaunchplaneProductProfileRecord:
    payload = _generic_site_profile_payload("example-site")
    payload.update(driver_id="verireel", production_use="live", historical_contexts=["old-site"])
    record = LaunchplaneProductProfileRecord.model_validate(payload)
    return record.model_copy(
        update={
            "preview": record.preview.model_copy(
                update={
                    "data_transport_mode": "driver",
                    "app_name_prefix": "example-site",
                }
            )
        }
    )


def draft(record: LaunchplaneProductProfileRecord) -> dict[str, object]:
    return {
        "lanes": [
            {
                "context": lane.context,
                "instance": lane.instance,
                "extensions": {
                    "verification_extension": "verireel-testing"
                    if lane.instance == "testing"
                    else "",
                    "maintenance": [{"name": "migrate", "command": "node /app/migrate.js"}],
                    "schedules": [
                        {
                            "name": "recover",
                            "command": "node /app/recover.js",
                            "cron_expression": "*/10 * * * *",
                        }
                    ],
                },
            }
            for lane in record.lanes
        ],
        "preview": {
            "data_transport_mode": "driver",
            "transport_extension": "verireel-postgres",
            "transport_database_prefix": "example_site",
            "app_name_suffix": "-app",
        },
        "reason": "Use declared shared lifecycle",
    }


class GenericWebTransitionTests(unittest.TestCase):
    def test_declared_hooks_replace_only_driver_extension_fields_and_bind_review(self) -> None:
        before = profile()
        request = ProductGenericWebTransitionRequest.model_validate(draft(before))
        plan, after = build_generic_web_transition_plan(before, request)
        self.assertTrue(plan.changed)
        self.assertEqual(after.driver_id, plan.driver_after)
        self.assertEqual(
            [lane.generic_web_extensions for lane in after.lanes],
            [item.extensions for item in request.lanes],
        )
        self.assertEqual(
            after.preview.transport_extension,
            request.preview.transport_extension if request.preview else "",
        )
        preserved = after.model_copy(
            update={
                "schema_version": before.schema_version,
                "driver_id": before.driver_id,
                "lanes": before.lanes,
                "preview": before.preview,
            }
        )
        self.assertEqual(preserved, before)
        moved_plan, _ = build_generic_web_transition_plan(
            before.model_copy(update={"source": "concurrent-edit"}), request
        )
        self.assertNotEqual(plan.plan_sha256, moved_plan.plan_sha256)
        changed = request.model_copy(update={"reason": "Another reason"})
        changed_plan, _ = build_generic_web_transition_plan(before, changed)
        self.assertNotEqual(plan.plan_sha256, changed_plan.plan_sha256)

    def test_omitted_duplicate_or_moved_lanes_and_production_testing_hooks_refuse(self) -> None:
        before = profile()
        valid = ProductGenericWebTransitionRequest.model_validate(draft(before))
        for lanes in (
            (),
            (*valid.lanes, valid.lanes[0]),
            (valid.lanes[0].model_copy(update={"context": "unowned"}),),
        ):
            with self.subTest(lanes=lanes), self.assertRaises(ValueError):
                build_generic_web_transition_plan(before, valid.model_copy(update={"lanes": lanes}))
        production = next(item for item in valid.lanes if item.instance == "prod")
        lanes = tuple(
            item.model_copy(
                update={
                    "extensions": item.extensions.model_copy(
                        update={"verification_extension": "verireel-testing"}
                    )
                }
            )
            if item == production
            else item
            for item in valid.lanes
        )
        with self.assertRaises(ValueError):
            build_generic_web_transition_plan(before, valid.model_copy(update={"lanes": lanes}))

    def test_implicit_transport_inactive_product_and_unreviewed_apply_refuse(self) -> None:
        before = profile()
        valid = ProductGenericWebTransitionRequest.model_validate(draft(before))
        with self.assertRaises(ValueError):
            build_generic_web_transition_plan(before, valid.model_copy(update={"preview": None}))
        with self.assertRaises(ValueError):
            build_generic_web_transition_plan(
                before.model_copy(update={"lifecycle_state": "retired"}), valid
            )
        with self.assertRaises(ValueError):
            ProductGenericWebTransitionRequest.model_validate({**draft(before), "mode": "apply"})
        with self.assertRaises(ValueError):
            ProductGenericWebTransitionRequest.model_validate(
                {**draft(before), "owner": {"github_login": "another"}}
            )

    def test_a_transition_cannot_drop_the_existing_post_deploy_qualification(self) -> None:
        before = profile()
        valid = ProductGenericWebTransitionRequest.model_validate(draft(before))
        lanes = tuple(
            item.model_copy(
                update={
                    "extensions": item.extensions.model_copy(update={"verification_extension": ""})
                }
            )
            for item in valid.lanes
        )
        with self.assertRaises(ValueError):
            build_generic_web_transition_plan(before, valid.model_copy(update={"lanes": lanes}))
        with self.assertRaises(ValueError):
            build_generic_web_transition_plan(
                before.model_copy(update={"driver_id": "odoo"}), valid
            )

    def test_preview_commands_and_schedules_can_be_reviewed_without_touching_production(
        self,
    ) -> None:
        before = profile()
        request = ProductGenericWebTransitionRequest.model_validate(
            {
                **draft(before),
                "preview": {
                    "data_transport_mode": "none",
                    "generic_web_extensions": {
                        "maintenance": [{"name": "migrate", "command": "./migrate"}],
                        "schedules": [
                            {
                                "name": "housekeeping",
                                "command": "./housekeeping",
                                "cron_expression": "*/15 * * * *",
                            }
                        ],
                    },
                },
            }
        )
        plan, after = build_generic_web_transition_plan(before, request)
        assert request.preview is not None
        self.assertEqual(
            [lane.generic_web_extensions for lane in after.lanes],
            [item.extensions for item in request.lanes],
        )
        self.assertEqual(
            after.preview.generic_web_extensions, request.preview.generic_web_extensions
        )
        self.assertEqual(plan.preview.generic_web_extensions, after.preview.generic_web_extensions)

    def test_partial_preview_preserves_unmentioned_resource_suffix(self) -> None:
        before = profile()
        before = before.model_copy(
            update={"preview": before.preview.model_copy(update={"app_name_suffix": "-app"})}
        )
        request = ProductGenericWebTransitionRequest.model_validate(
            {**draft(before), "preview": {"data_transport_mode": "none"}}
        )
        plan, after = build_generic_web_transition_plan(before, request)
        self.assertEqual(after.preview.app_name_suffix, before.preview.app_name_suffix)
        self.assertEqual(plan.preview.app_name_suffix, before.preview.app_name_suffix)

    def test_reordered_lane_input_has_one_normalized_plan_and_review(self) -> None:
        before = profile()
        request = ProductGenericWebTransitionRequest.model_validate(draft(before))
        original, _ = build_generic_web_transition_plan(before, request)
        reordered, after = build_generic_web_transition_plan(
            before, request.model_copy(update={"lanes": tuple(reversed(request.lanes))})
        )
        self.assertEqual(original.plan_sha256, reordered.plan_sha256)
        self.assertEqual(original.lanes, reordered.lanes)
        self.assertEqual(
            [(item.context, item.instance) for item in reordered.lanes],
            [(item.context, item.instance) for item in after.lanes],
        )


class GenericWebTransitionHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(directory.name) / "db.sqlite3")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.store.write_product_profile_record(profile())
        self.sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        self.session = self.sessions.issue(_github_human_identity())
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_github_human_product_config_policy(
                actions=("product_profile.write", "product_profile.read"),
                product="example-site",
                context="launchplane",
            ),
            human_session_manager=self.sessions,
            record_store_factory=lambda: self.store,
        )

    async def post(self, payload: dict[str, object], key: str = "") -> _AsgiResponse:
        return await _asgi_request(
            self.app,
            "POST",
            "/v1/product-profiles/example-site/generic-web-transition",
            headers={
                **_browser_mutation_headers(self.sessions, self.session),
                "Idempotency-Key": key,
            },
            payload=payload,
        )

    async def test_review_apply_readback_replay_receipt_and_conflict(self) -> None:
        before = self.store.read_product_profile_record("example-site")
        request = draft(before)
        dry = await self.post(request)
        self.assertEqual(dry.status_code, 202, dry.text)
        self.assertEqual(self.store.read_product_profile_record("example-site"), before)
        body = {
            **request,
            "mode": "apply",
            "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
        }
        self.assertEqual((await self.post(body)).status_code, 400)
        applied = await self.post(body, "transition")
        self.assertEqual(applied.status_code, 202, applied.text)
        after = self.store.read_product_profile_record("example-site")
        _, expected = build_generic_web_transition_plan(
            before, ProductGenericWebTransitionRequest.model_validate(request)
        )
        self.assertEqual(
            after,
            expected.model_copy(update={"updated_at": after.updated_at, "source": after.source}),
        )
        replay = await self.post(body, "transition")
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["original_trace_id"], applied.json()["trace_id"])
        conflict = await self.post({**body, "reason": "Different"}, "transition")
        self.assertEqual(conflict.json()["error"]["code"], "idempotency_key_reused")
        receipt = await _asgi_request(
            self.app,
            "GET",
            "/v1/product-profiles/example-site/mutation-receipts/generic-web-transition?operation_key=transition",
            headers={"Cookie": self.sessions.session_cookie_header(self.session)},
        )
        self.assertEqual(receipt.json()["state"], "completed")

    async def test_stale_and_concurrent_edits_preserve_other_writer(self) -> None:
        before = self.store.read_product_profile_record("example-site")
        request = draft(before)
        dry = await self.post(request)
        body = {
            **request,
            "mode": "apply",
            "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
        }
        raced = before.model_copy(update={"display_name": "Changed by another admin"})
        original_write = self.store.compare_and_write_product_profile_record

        def race(**kwargs: Any) -> ProductProfileCompareWriteResult:
            self.store.write_product_profile_record(raced)
            return original_write(**kwargs)

        with patch.object(self.store, "compare_and_write_product_profile_record", side_effect=race):
            result = await self.post(body, "race")
        self.assertEqual(result.json()["error"]["code"], "stale")
        self.assertEqual(self.store.read_product_profile_record("example-site"), raced)
        self.assertEqual((await self.post(body, "stale")).json()["error"]["code"], "stale")

    async def test_optional_preview_round_trip_applies_the_normalized_review(self) -> None:
        before = self.store.read_product_profile_record("example-site")
        before = before.model_copy(
            update={
                "preview": before.preview.model_copy(
                    update={"data_transport_mode": "none", "app_name_suffix": "-app"}
                )
            }
        )
        self.store.write_product_profile_record(before)
        request = draft(before)
        request.pop("preview")
        dry = await self.post(request)
        self.assertEqual(dry.status_code, 202, dry.text)
        body = {
            **request,
            "lanes": dry.json()["result"]["lanes"],
            "preview": dry.json()["result"]["preview"],
            "mode": "apply",
            "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
        }
        applied = await self.post(body, "normalized")
        self.assertEqual(applied.status_code, 202, applied.text)
        after = self.store.read_product_profile_record("example-site")
        self.assertEqual(after.driver_id, dry.json()["result"]["driver_after"])
        self.assertEqual(after.preview, before.preview)

    async def test_missing_csrf_nonadmin_and_unrelated_fields_refuse(self) -> None:
        before = self.store.read_product_profile_record("example-site")
        request = draft(before)
        self.assertEqual(
            (await self.post({**request, "release_on_acceptance": "held"})).status_code, 400
        )
        no_csrf = await _asgi_request(
            self.app,
            "POST",
            "/v1/product-profiles/example-site/generic-web-transition",
            headers={"Cookie": self.sessions.session_cookie_header(self.session)},
            payload=request,
        )
        self.assertEqual(no_csrf.status_code, 403)
        self.session = self.sessions.issue(_github_human_identity(role="read_only"))
        self.assertEqual((await self.post(request)).status_code, 403)
        self.assertEqual(self.store.read_product_profile_record("example-site"), before)
