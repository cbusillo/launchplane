import unittest
import json
import base64
import shlex
import click
from control_plane.preview_namespaces import require_postgres_preview_cleanup
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch
from types import SimpleNamespace
from sqlalchemy import update
from control_plane.contracts.product_retirement import canonical_sha256
from control_plane.storage.postgres import LaunchplaneProductProfileRow
from tests.test_preview_namespaces import _preview


from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.product_generic_web_transition import (
    GenericWebPreviewDeclaration,
    ProductGenericWebTransitionRequest,
    build_generic_web_transition_plan,
)
from control_plane.service_auth import BearerIdentityConfig, LocalOperatorPolicyRule
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
            "transport_extension": "postgres",
            "transport_database_prefix": "example_site",
            "app_name_suffix": "-app",
        },
        "reason": "Use declared shared lifecycle",
    }


class GenericWebTransitionTests(unittest.TestCase):
    def test_legacy_transition_reviews_effective_endpoint_without_rewriting_runtime_settings(
        self,
    ) -> None:
        from tests.test_generic_web_preview_extensions import _driver_profile

        payload = _driver_profile().model_dump(mode="json")
        payload["runtime_port"] = 8081
        payload["health_path"] = "/ready"
        payload["preview"]["transport_extension"] = "verireel-postgres"
        payload["preview"]["domain_certificate_type"] = "letsencrypt"
        payload["preview"].pop("generated_secret_env")
        before = LaunchplaneProductProfileRecord.model_validate(
            payload, context={"stored_product_profile": True}
        )
        raw = before.model_dump_json()
        request = ProductGenericWebTransitionRequest.model_validate(
            {
                "lanes": [
                    {"context": lane.context, "instance": lane.instance, "extensions": {}}
                    for lane in before.lanes
                ],
                "reason": "Retain preview declarations",
            }
        )
        with self.assertRaises(ValueError):
            build_generic_web_transition_plan(before, request)
        incomplete = {
            key: getattr(_driver_profile().preview, key)
            for key in GenericWebPreviewDeclaration.model_fields
            if key != "copied_env_keys"
        }
        with self.assertRaises(ValueError):
            build_generic_web_transition_plan(
                before,
                request.model_copy(
                    update={"preview": GenericWebPreviewDeclaration.model_validate(incomplete)}
                ),
            )
        request = request.model_copy(
            update={
                "preview": GenericWebPreviewDeclaration.model_validate(
                    {
                        key: getattr(_driver_profile().preview, key)
                        for key in GenericWebPreviewDeclaration.model_fields
                    }
                )
            }
        )
        plan, after = build_generic_web_transition_plan(before, request)
        self.assertEqual(before.model_dump_json(), raw)
        assert request.preview is not None
        self.assertEqual(after.preview.generated_secret_env, request.preview.generated_secret_env)
        self.assertIsNone(plan.preview_endpoint_before)
        assert plan.preview_endpoint_after is not None
        self.assertEqual(plan.preview_endpoint_after.runtime_port, before.runtime_port)
        self.assertEqual(plan.preview_endpoint_after.health_path, before.health_path)
        self.assertEqual(after.runtime_port, before.runtime_port)
        self.assertEqual(after.health_path, before.health_path)

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

    def test_legacy_transition_can_remove_retired_testing_browser_declaration(self) -> None:
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
        _, replacement = build_generic_web_transition_plan(
            before, valid.model_copy(update={"lanes": lanes})
        )
        self.assertTrue(
            all(
                not lane.generic_web_extensions.verification_extension for lane in replacement.lanes
            )
        )
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

    async def test_stored_legacy_transition_and_retiring_cleanup_keep_original_resource_names(
        self,
    ) -> None:
        policy = _github_human_product_config_policy(
            actions=("product_profile.write", "product_profile.read", "preview_destroy.execute"),
            product="example-site",
            context="launchplane",
        )
        policy = policy.model_copy(
            update={
                "github_humans": (
                    policy.github_humans[0].model_copy(
                        update={"contexts": ("launchplane", "example-site-preview")}
                    ),
                )
            }
        )
        policy = policy.model_copy(
            update={
                "local_operators": (
                    LocalOperatorPolicyRule(
                        subjects=("fixture-agent",),
                        token_labels=("fixture",),
                        products=("example-site",),
                        contexts=("example-site-preview",),
                        actions=("preview_destroy.execute",),
                    ),
                )
            }
        )
        self.app = create_launchplane_fastapi_app(
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="fixture-cleanup-token",
                local_operator_subject="fixture-agent",
                local_operator_token_label="fixture",
            ),
            verifier=_RejectingVerifier(),
            authz_policy=policy,
            human_session_manager=self.sessions,
            record_store_factory=lambda: self.store,
        )
        for number, (state, transport) in enumerate(
            (
                ("active", "verireel-postgres"),
                ("retiring", "verireel-postgres"),
                ("retiring", "postgres"),
                ("retired", "verireel-postgres"),
            ),
            start=7,
        ):
            with self.subTest(state=state, transport=transport):
                payload = profile().model_dump(mode="json")
                payload.update(
                    driver_id="generic-web", lifecycle_state=state, production_use="prelaunch"
                )
                payload["lanes"] = [
                    lane for lane in payload["lanes"] if lane["instance"] == "testing"
                ]
                payload["preview"].update(
                    enabled=state == "active",
                    context="example-site-preview",
                    data_transport_mode="driver",
                    transport_extension=transport,
                    app_name_prefix="retained",
                    app_name_suffix="-app",
                    transport_database_prefix="retained_",
                    slug_template="pr-{number}",
                )
                with self.store._engine.begin() as connection:
                    connection.execute(
                        update(LaunchplaneProductProfileRow)
                        .where(LaunchplaneProductProfileRow.product == "example-site")
                        .values(payload=payload, driver_id="generic-web")
                    )
                before = self.store.read_product_profile_record("example-site")
                fence = canonical_sha256(before.model_dump(mode="json"))
                preview = _preview(before).model_copy(
                    update={
                        "preview_id": f"retained-{number}",
                        "anchor_pr_number": number,
                        "preview_label": f"pr-{number}",
                    }
                )
                self.store.write_preview_record(preview)
                declarations = draft(before)
                declarations["preview"] = {
                    "data_transport_mode": "driver",
                    "transport_extension": "postgres",
                    "transport_database_prefix": before.preview.transport_database_prefix,
                    "copied_env_keys": [],
                    "app_name_suffix": before.preview.app_name_suffix,
                    "generated_secret_env": {},
                    "preview_url_env_keys": [],
                    "preview_domain_env_keys": [],
                }
                if state == "active":
                    review = await self.post(declarations)
                    self.assertEqual(review.status_code, 202, review.text)
                    apply = {
                        **declarations,
                        "mode": "apply",
                        "reviewed_plan_sha256": review.json()["result"]["plan_sha256"],
                    }
                    applied = await self.post(apply, "legacy-transition")
                    self.assertEqual(applied.status_code, 202, applied.text)
                    replay = await self.post(apply, "legacy-transition")
                    self.assertTrue(replay.json()["replayed"])
                    self.assertEqual(
                        self.store.read_product_profile_record(
                            "example-site"
                        ).preview.transport_extension,
                        "postgres",
                    )
                else:
                    self.assertEqual((await self.post(declarations)).status_code, 400)
                with (
                    patch(
                        "control_plane.workflows.postgres_preview.dokploy_source.read_dokploy_config",
                        return_value=("https://provider.invalid", "fixture"),
                    ),
                    patch(
                        "control_plane.workflows.postgres_preview._template_application_payload",
                        return_value=(
                            SimpleNamespace(
                                context=before.lanes[0].context,
                                instance="testing",
                                target_id="template",
                                target_name="fixture-template",
                            ),
                            {"env": "DATABASE_URL=postgresql://admin:fixture@db.invalid/testing"},
                        ),
                    ),
                    patch(
                        "control_plane.workflows.postgres_preview._resolve_preview_base_url",
                        return_value="https://preview.example.test",
                    ),
                    patch(
                        "control_plane.workflows.postgres_preview._find_application_by_name",
                        return_value=None,
                    ) as find,
                    patch(
                        "control_plane.workflows.postgres_preview._run_application_command"
                    ) as drop,
                ):

                    async def destroy(key: str) -> _AsgiResponse:
                        return await _asgi_request(
                            self.app,
                            "POST",
                            "/v1/drivers/generic-web/preview-destroy",
                            headers={
                                "Authorization": "Bearer fixture-cleanup-token",
                                "Idempotency-Key": key,
                            },
                            payload={
                                "product": before.product,
                                "destroy": {
                                    "product": before.product,
                                    "preview_slug": f"pr-{number}",
                                    "anchor_pr_number": number,
                                    "destroy_reason": "closed",
                                },
                            },
                        )

                    if state == "retiring":
                        with patch(
                            "control_plane.workflows.postgres_preview._run_application_command_with_retries",
                            side_effect=click.ClickException("database cleanup failed"),
                        ):
                            failed = await destroy(f"failed-{number}")
                        self.assertEqual(failed.status_code, 202, failed.text)
                        self.assertEqual(failed.json()["result"]["destroy_status"], "fail")
                        self.assertNotEqual(
                            self.store.read_preview_record(preview.preview_id).state, "destroyed"
                        )
                        with self.assertRaises(ValueError):
                            require_postgres_preview_cleanup(
                                self.store.read_product_profile_record(before.product),
                                self.store.list_preview_records(),
                            )
                    destroyed = await destroy(f"cleanup-{number}")
                if state == "retired":
                    self.assertEqual(destroyed.status_code, 400, destroyed.text)
                    drop.assert_not_called()
                    continue
                self.assertEqual(destroyed.status_code, 202, destroyed.text)
                self.assertEqual(destroyed.json()["result"]["destroy_status"], "pass")
                self.assertEqual(
                    find.call_args.kwargs["application_name"],
                    f"{before.preview.app_name_prefix}-pr-{number}{before.preview.app_name_suffix}",
                )
                command = drop.call_args.kwargs["command"]
                encoded = next(
                    token.partition("=")[2]
                    for token in shlex.split(command)
                    if token.startswith("PREVIEW_DB_ARGS_BASE64=")
                )
                arguments = json.loads(base64.b64decode(encoded))
                expected_database = f"{before.preview.transport_database_prefix}pr_{number}"
                self.assertEqual(
                    arguments[arguments.index("--database-name") + 1], expected_database
                )
                self.assertEqual(arguments[arguments.index("--role-name") + 1], expected_database)
                self.assertEqual(
                    self.store.read_preview_record(preview.preview_id).state, "destroyed"
                )
                if state == "retiring":
                    self.assertEqual(
                        canonical_sha256(
                            self.store.read_product_profile_record("example-site").model_dump(
                                mode="json"
                            )
                        ),
                        fence,
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
