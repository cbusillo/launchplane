import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from sqlalchemy.exc import OperationalError

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_retirement import (
    ProductRetirementProviderObservation,
    ProductRetirementRequest,
    ProductRetirementRecord,
    provider_identifier_sha256,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.dokploy.api import DokployRequestFailed
from control_plane.http_app import (
    create_launchplane_fastapi_app,
    idempotency_request_fingerprint,
    idempotency_scope,
)
from control_plane.product_retirement import BoundProductRetirement, build_provider_observation
from control_plane.product_retirement import ProductRetirementBlockedError
from control_plane.product_retirement_no_target import (
    bind_no_target_retirement,
    observe_no_target_absence,
)
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.product_reconcile import ProductReconcileTarget
from control_plane.product_reconcile import request_product_reconcile_sweep
from control_plane.service_auth import (
    LaunchplaneAuthzPolicy,
    LocalOperatorPolicyRule,
    LocalOperatorIdentity,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _asgi_request, _local_operator_bearer_config
from tests.support.auth import _StubVerifier, _identity
from tests.support.profiles import _generic_site_profile_payload


NOW = "2026-08-11T02:00:00Z"
TARGET_ID = "application-private-http-1"
TARGET_SHA256 = provider_identifier_sha256(TARGET_ID)


def _observation(*, name: str = "example-site-prod") -> ProductRetirementProviderObservation:
    return build_provider_observation(
        target_id=TARGET_ID,
        payload={"applicationId": TARGET_ID, "name": name, "applicationStatus": "idle"},
        domains=(),
        latest_deployment={"status": "done"},
        observed_at=NOW,
    )


def _no_history_observation(
    *, name: str = "example-site-prod"
) -> ProductRetirementProviderObservation:
    return build_provider_observation(
        target_id=TARGET_ID,
        payload={
            "applicationId": TARGET_ID,
            "name": name,
            "applicationStatus": "idle",
        },
        domains=(),
        latest_deployment=None,
        deployment_history_state="no_history",
        observed_at=NOW,
    )


def _absent_observation() -> ProductRetirementProviderObservation:
    return _observation().model_copy(
        update={
            "state": "absent",
            "application_fingerprint_sha256": "",
            "application_name_sha256": "",
            "project_reference_sha256": "",
            "deployment_status": "",
            "retirable": False,
        }
    )


def _plan_payload() -> dict[str, object]:
    return {
        "mode": "plan",
        "product": "example-site",
        "instance": "prod",
        "expected_target_sha256": TARGET_SHA256,
        "reason": "Retire an obsolete stable application.",
        "related_issue": "cbusillo/launchplane#2008",
    }


def _apply_payload(plan_response: dict[str, object]) -> dict[str, object]:
    records = plan_response["records"]
    result = plan_response["result"]
    assert isinstance(records, dict)
    assert isinstance(result, dict)
    return {
        **_plan_payload(),
        "mode": "apply",
        "reviewed_plan_record_id": records["product_retirement_plan_id"],
        "reviewed_plan_sha256": result["plan_sha256"],
        "confirmation": (f"retire product example-site instance prod target {TARGET_SHA256}"),
    }


class ProductRetirementHttpTests(unittest.IsolatedAsyncioTestCase):
    def _no_target_store(
        self, root: Path, *, database_url: str = "", previews: bool = True
    ) -> PostgresRecordStore:
        store = self._store(root, database_url=database_url)
        profile = store.read_product_profile_record("example-site")
        store.write_product_profile_record(
            profile.model_copy(
                update={
                    "lanes": tuple(lane for lane in profile.lanes if lane.instance == "prod"),
                    "preview": profile.preview.model_copy(
                        update={
                            "enabled": previews,
                            "context": profile.preview.context if previews else "",
                        }
                    ),
                }
            )
        )
        store.delete_provider_target_record(
            expected_record=store.read_provider_target_record(
                context_name="example-site", instance_name="prod"
            )
        )
        store.delete_dokploy_target_id_record(
            expected_record=store.read_dokploy_target_id_record(
                context_name="example-site", instance_name="prod"
            )
        )
        if not previews:
            return store
        store.write_preview_record(
            PreviewRecord(
                preview_id="stale-preview",
                context="example-site-preview",
                anchor_repo="example-site",
                anchor_pr_number=1,
                anchor_pr_url="https://github.com/every/example-site/pull/1",
                preview_label="launchplane-preview",
                canonical_url="https://pr-1.example.invalid",
                state="failed",
                created_at=NOW,
                updated_at=NOW,
                eligible_at=NOW,
            )
        )
        store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="context",
                context="example-site-preview",
                instance="",
                env={"PREVIEW_BASE_URL": "https://preview.example.invalid"},
                updated_at=NOW,
            )
        )
        return store

    async def test_no_target_retirement_closes_previews_without_provider_writes_and_replays(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = self._no_target_store(Path(directory))
            app = self._app(store, actions=("product_retirement.plan", "product_retirement.apply"))
            payload = {**_plan_payload(), "no_target": True, "expected_target_sha256": ""}
            with (
                patch(
                    "control_plane.product_retirement_no_target.dokploy_source.read_dokploy_config",
                    return_value=("https://provider.invalid", "test"),
                ),
                patch(
                    "control_plane.product_retirement_no_target.dokploy_api.search_dokploy_applications",
                    return_value=(),
                ) as inventory,
                patch(
                    "control_plane.product_retirement.dokploy_api.delete_dokploy_application"
                ) as delete,
            ):
                plan_response = await _asgi_request(
                    app, "POST", "/v1/product-retirement", headers=self.headers, payload=payload
                )
                self.assertEqual(plan_response.status_code, 202, plan_response.text)
                plan = json.loads(plan_response.text)
                self.assertEqual(store.read_preview_record("stale-preview").state, "failed")
                runtime_before = store.list_runtime_environment_records()
                apply_payload = {
                    **payload,
                    "mode": "apply",
                    "reviewed_plan_record_id": plan["records"]["product_retirement_plan_id"],
                    "reviewed_plan_sha256": plan["result"]["plan_sha256"],
                    "confirmation": "retire product example-site instance prod with no target",
                }
                with patch.object(
                    store,
                    "commit_no_target_retirement",
                    side_effect=OperationalError(
                        "fixture statement", {}, RuntimeError("fixture failure")
                    ),
                ):
                    unconfirmed = await _asgi_request(
                        app,
                        "POST",
                        "/v1/product-retirement",
                        headers=self.headers,
                        payload=apply_payload,
                    )
                self.assertEqual(unconfirmed.status_code, 409, unconfirmed.text)
                self.assertEqual(store.read_preview_record("stale-preview").state, "failed")
                self.assertTrue(
                    any(
                        record.outcome == "reconcile_required"
                        for record in store.list_product_retirement_records(product="example-site")
                    )
                )
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/product-retirement",
                    headers=self.headers,
                    payload=apply_payload,
                )
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(
                    json.loads(response.text)["result"]["closed_preview_ids"], ["stale-preview"]
                )
                self.assertTrue(json.loads(response.text)["result"]["provider_absence_verified"])
                self.assertEqual(store.read_preview_record("stale-preview").state, "destroyed")
                profile = store.read_product_profile_record("example-site")
                self.assertEqual(profile.lifecycle_state, "retired")
                self.assertFalse(profile.preview.enabled)
                self.assertEqual(store.list_runtime_environment_records(), runtime_before)
                self.assertEqual(request_product_reconcile_sweep(store, NOW), ())
                inventory_reads_before_replay = inventory.call_count
                replay = await _asgi_request(
                    app,
                    "POST",
                    "/v1/product-retirement",
                    headers=self.headers,
                    payload=apply_payload,
                )
                self.assertEqual(replay.json()["result"], response.json()["result"])
                self.assertEqual(replay.json()["records"], response.json()["records"])
                self.assertEqual(inventory.call_count, inventory_reads_before_replay)
                delete.assert_not_called()
            store.close()

    async def test_no_target_committed_connection_error_adopts_terminal_result(self) -> None:
        for delayed_visibility in (False, True):
            with (
                self.subTest(delayed_visibility=delayed_visibility),
                TemporaryDirectory() as directory,
            ):
                store = self._no_target_store(Path(directory))
                app = self._app(
                    store, actions=("product_retirement.plan", "product_retirement.apply")
                )
                payload = {**_plan_payload(), "no_target": True, "expected_target_sha256": ""}
                with (
                    patch(
                        "control_plane.product_retirement_no_target.dokploy_source.read_dokploy_config",
                        return_value=("https://provider.invalid", "test"),
                    ),
                    patch(
                        "control_plane.product_retirement_no_target.dokploy_api.search_dokploy_applications",
                        return_value=(),
                    ) as inventory,
                ):
                    plan_response = await _asgi_request(
                        app, "POST", "/v1/product-retirement", headers=self.headers, payload=payload
                    )
                    plan = plan_response.json()
                    apply_payload = {
                        **payload,
                        "mode": "apply",
                        "reviewed_plan_record_id": plan["records"]["product_retirement_plan_id"],
                        "reviewed_plan_sha256": plan["result"]["plan_sha256"],
                        "confirmation": "retire product example-site instance prod with no target",
                    }
                    real_commit = store.commit_no_target_retirement

                    def commit_then_disconnect(
                        *, bound: BoundProductRetirement, terminal: ProductRetirementRecord
                    ) -> None:
                        real_commit(bound=bound, terminal=terminal)
                        raise OperationalError(
                            "fixture COMMIT", {}, RuntimeError("connection lost")
                        )

                    with patch.object(
                        store, "commit_no_target_retirement", side_effect=commit_then_disconnect
                    ):
                        unconfirmed = await _asgi_request(
                            app,
                            "POST",
                            "/v1/product-retirement",
                            headers=self.headers,
                            payload=apply_payload,
                        )
                    self.assertEqual(unconfirmed.status_code, 409, unconfirmed.text)
                    retired = next(
                        record
                        for record in store.list_product_retirement_records(product="example-site")
                        if record.outcome == "retired"
                    )
                    reads_before_retry = inventory.call_count
                    real_list = store.list_product_retirement_records
                    first_read = True

                    def visible_records(
                        *,
                        product: str = "",
                        actor: str = "",
                        mode: str = "",
                        idempotency_key: str = "",
                        limit: int | None = None,
                    ) -> tuple[ProductRetirementRecord, ...]:
                        nonlocal first_read
                        records = real_list(
                            product=product,
                            actor=actor,
                            mode=mode,
                            idempotency_key=idempotency_key,
                            limit=limit,
                        )
                        if delayed_visibility and first_read:
                            first_read = False
                            return tuple(
                                record for record in records if record.outcome != "retired"
                            )
                        return records

                    with patch.object(
                        store, "list_product_retirement_records", side_effect=visible_records
                    ):
                        replay = await _asgi_request(
                            app,
                            "POST",
                            "/v1/product-retirement",
                            headers=self.headers,
                            payload=apply_payload,
                        )
                    self.assertEqual(replay.status_code, 202, replay.text)
                    self.assertEqual(
                        replay.json()["records"]["product_retirement_record_id"], retired.record_id
                    )
                    self.assertEqual(inventory.call_count, reads_before_retry)
                    self.assertEqual(
                        store.read_product_profile_record("example-site").lifecycle_state, "retired"
                    )
                store.close()

    async def test_no_target_empty_preview_context_is_not_shared_authority(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._no_target_store(Path(directory), previews=False)
            profile = store.read_product_profile_record("example-site")
            store.write_product_profile_record(
                profile.model_copy(
                    update={
                        "product": "another-site",
                        "repository": "every/another-site",
                        "lanes": tuple(
                            lane.model_copy(update={"context": "another-site"})
                            for lane in profile.lanes
                        ),
                    }
                )
            )
            app = self._app(store, actions=("product_retirement.plan", "product_retirement.apply"))
            payload = {**_plan_payload(), "no_target": True, "expected_target_sha256": ""}
            with (
                patch(
                    "control_plane.product_retirement_no_target.dokploy_source.read_dokploy_config",
                    return_value=("https://provider.invalid", "test"),
                ),
                patch(
                    "control_plane.product_retirement_no_target.dokploy_api.search_dokploy_applications",
                    return_value=(),
                ),
            ):
                plan = await _asgi_request(
                    app, "POST", "/v1/product-retirement", headers=self.headers, payload=payload
                )
                self.assertEqual(plan.status_code, 202, plan.text)
                applied = await _asgi_request(
                    app,
                    "POST",
                    "/v1/product-retirement",
                    headers=self.headers,
                    payload={
                        **payload,
                        "mode": "apply",
                        "reviewed_plan_record_id": plan.json()["records"][
                            "product_retirement_plan_id"
                        ],
                        "reviewed_plan_sha256": plan.json()["result"]["plan_sha256"],
                        "confirmation": "retire product example-site instance prod with no target",
                    },
                )
            self.assertEqual(applied.status_code, 202, applied.text)
            self.assertEqual(
                store.read_product_profile_record("another-site").lifecycle_state, "active"
            )
            store.close()

    async def test_no_target_apply_refuses_changed_authority_and_running_reconcile(self) -> None:
        for change in ("target", "preview", "reconcile", "profile"):
            with self.subTest(change=change), TemporaryDirectory() as directory:
                store = self._no_target_store(Path(directory))
                app = self._app(
                    store, actions=("product_retirement.plan", "product_retirement.apply")
                )
                payload = {**_plan_payload(), "no_target": True, "expected_target_sha256": ""}
                with (
                    patch(
                        "control_plane.product_retirement_no_target.dokploy_source.read_dokploy_config",
                        return_value=("https://provider.invalid", "test"),
                    ),
                    patch(
                        "control_plane.product_retirement_no_target.dokploy_api.search_dokploy_applications",
                        return_value=(),
                    ),
                ):
                    response = await _asgi_request(
                        app, "POST", "/v1/product-retirement", headers=self.headers, payload=payload
                    )
                    self.assertEqual(response.status_code, 202, response.text)
                    plan = json.loads(response.text)
                    if change == "target":
                        store.write_dokploy_target_id_record(
                            DokployTargetIdRecord(
                                context="example-site",
                                instance="prod",
                                target_id="late-target",
                                updated_at=NOW,
                            )
                        )
                    elif change == "preview":
                        preview = store.read_preview_record("stale-preview")
                        store.write_preview_record(preview.model_copy(update={"state": "active"}))
                    elif change == "profile":
                        profile = store.read_product_profile_record("example-site")
                        store.write_product_profile_record(
                            profile.model_copy(update={"display_name": "changed"})
                        )
                    else:
                        store.request_product_reconcile(
                            ProductReconcileTarget(product="example-site", target_kind="testing"),
                            NOW,
                        )
                        store.claim_next_product_reconcile_request("busy-reconciler", 60, now=NOW)
                    response = await _asgi_request(
                        app,
                        "POST",
                        "/v1/product-retirement",
                        headers=self.headers,
                        payload={
                            **payload,
                            "mode": "apply",
                            "reviewed_plan_record_id": plan["records"][
                                "product_retirement_plan_id"
                            ],
                            "reviewed_plan_sha256": plan["result"]["plan_sha256"],
                            "confirmation": "retire product example-site instance prod with no target",
                        },
                    )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(
                    store.read_product_profile_record("example-site").lifecycle_state, "active"
                )
                self.assertNotEqual(store.read_preview_record("stale-preview").state, "destroyed")
                store.close()

    async def test_no_target_provider_matches_and_incomplete_inventory_block_plan(self) -> None:
        for payload in (
            {"applicationId": "orphan", "name": "example-site-prod"},
            {"applicationId": "orphan", "name": "example-site-preview-pr-7"},
            {"applicationId": "orphan", "name": "renamed", "appName": "example-site-prod-old"},
            {
                "applicationId": "orphan",
                "name": "renamed",
                "dockerImage": "ghcr.io/every/example-site@sha256:test",
            },
            {
                "applicationId": "orphan",
                "name": "renamed",
                "repository": "example-site",
                "owner": "every",
            },
            {
                "applicationId": "orphan",
                "name": "renamed",
                "gitlabRepository": "every/example-site",
            },
            {
                "applicationId": "orphan",
                "name": "renamed",
                "customGitUrl": "https://github.com/every/example-site.git",
            },
            {"applicationId": "wrong", "name": "unrelated"},
            {"applicationId": "orphan"},
        ):
            with self.subTest(payload=payload), TemporaryDirectory() as directory:
                store = self._no_target_store(Path(directory))
                with (
                    patch(
                        "control_plane.product_retirement_no_target.dokploy_source.read_dokploy_config",
                        return_value=("https://provider.invalid", "test"),
                    ),
                    patch(
                        "control_plane.product_retirement_no_target.dokploy_api.search_dokploy_applications",
                        return_value=({"applicationId": "orphan"},),
                    ),
                    patch(
                        "control_plane.product_retirement_no_target.dokploy_api.fetch_dokploy_target_payload",
                        return_value=payload,
                    ),
                ):
                    response = await _asgi_request(
                        self._app(store, actions=("product_retirement.plan",)),
                        "POST",
                        "/v1/product-retirement",
                        headers=self.headers,
                        payload={
                            **_plan_payload(),
                            "no_target": True,
                            "expected_target_sha256": "",
                        },
                    )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(store.list_product_retirement_records(product="example-site"), ())
                store.close()

    def test_no_target_normalized_app_names_block_renamed_or_ambiguous_applications(self) -> None:
        for target_name, preview_prefix, app_name, blocked in (
            ("Example Site PROD", "Example Preview", "example-site-prod-abc123", True),
            ("Example Site PROD", "Example Preview", "example-site-prod", True),
            ("Example Site PROD", "Example Preview", "example-preview-pr-7-abc123", True),
            ("Example Site PROD", "Example Preview", "example-site-pr-7-abc123", True),
            ("Example Site PROD", "Example Preview", "EXAMPLE-SITE-PR-7-ABC123", True),
            ("Example  Site PROD", "Example Preview", "example--site-prod-abc123", True),
            ("Example.Site_PROD", "Example Preview", "example.site_prod-abc123", True),
            # A neighbouring application can share the base: ambiguity must refuse.
            ("Example Site PROD", "Example Preview", "example-site-prod-other-site", True),
            ("Demo Service Prod", "Example Preview", "demo-service-production-abc123", False),
            ("Demo.Site_PROD", "Example Preview", "demo-site-prod-abc123", False),
            ("Example Site PROD", "Example Preview", "example-sites-pr-7-abc123", False),
            ("Example Site PROD", "Example Preview", "example-previewing-abc123", False),
        ):
            with self.subTest(app_name=app_name), TemporaryDirectory() as directory:
                store = self._no_target_store(Path(directory))
                target = store.read_dokploy_target_record(
                    context_name="example-site", instance_name="prod"
                )
                store.write_dokploy_target_record(
                    target.model_copy(update={"target_name": target_name})
                )
                profile = store.read_product_profile_record("example-site")
                store.write_product_profile_record(
                    profile.model_copy(
                        update={
                            "preview": profile.preview.model_copy(
                                update={"app_name_prefix": preview_prefix}
                            )
                        }
                    )
                )
                bound = bind_no_target_retirement(
                    record_store=store,
                    request=ProductRetirementRequest.model_validate(
                        {**_plan_payload(), "no_target": True, "expected_target_sha256": ""}
                    ),
                )
                with (
                    patch(
                        "control_plane.product_retirement_no_target.dokploy_source.read_dokploy_config",
                        return_value=("https://provider.invalid", "test"),
                    ),
                    patch(
                        "control_plane.product_retirement_no_target.dokploy_api.search_dokploy_applications",
                        return_value=({"applicationId": "orphan"},),
                    ),
                    patch(
                        "control_plane.product_retirement_no_target.dokploy_api.fetch_dokploy_target_payload",
                        return_value={
                            "applicationId": "orphan",
                            "name": "renamed",
                            "appName": app_name,
                            "repository": "unrelated-site",
                            "dockerImage": "ghcr.io/another/unrelated:latest",
                        },
                    ),
                    patch(
                        "control_plane.product_retirement_no_target.dokploy_api.fetch_dokploy_application_domains",
                        return_value=({"host": "unrelated.invalid"},),
                    ),
                    patch(
                        "control_plane.product_retirement_no_target.dokploy_api.dokploy_request",
                        side_effect=AssertionError("Unexpected provider request"),
                    ),
                ):
                    if blocked:
                        with self.assertRaises(ProductRetirementBlockedError):
                            observe_no_target_absence(
                                control_plane_root=Path(directory), bound=bound, observed_at=NOW
                            )
                    else:
                        observation = observe_no_target_absence(
                            control_plane_root=Path(directory), bound=bound, observed_at=NOW
                        )
                        self.assertEqual(observation.state, "absent")
                self.assertEqual(
                    store.read_product_profile_record("example-site").lifecycle_state, "active"
                )
                self.assertEqual(store.read_preview_record("stale-preview").state, "failed")
                store.close()

    def _store(self, root: Path, *, database_url: str = "") -> PostgresRecordStore:
        store = PostgresRecordStore(
            database_url=database_url or f"sqlite+pysqlite:///{root / 'launchplane.sqlite3'}"
        )
        store.ensure_schema()
        profile_payload = _generic_site_profile_payload()
        profile_payload["preview"] = {
            "enabled": False,
            "context": "example-site-preview",
        }
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile_payload)
        )
        store.write_provider_target_record(
            ProviderTargetRecord(
                context="example-site",
                instance="prod",
                provider_id="dokploy",
                target_category="application",
                target_id=TARGET_ID,
                display_name="example-site-prod",
                provider_target_type="application",
                updated_at=NOW,
            )
        )
        store.write_dokploy_target_record(
            DokployTargetRecord(
                context="example-site",
                instance="prod",
                target_type="application",
                target_name="example-site-prod",
                updated_at=NOW,
            )
        )
        store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context="example-site",
                instance="prod",
                target_id=TARGET_ID,
                updated_at=NOW,
            )
        )
        store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="example-site",
                instance="prod",
                env={"PORT": "3000"},
                updated_at=NOW,
            )
        )
        return store

    def _app(self, store: object, *, actions: tuple[str, ...]) -> FastAPI:
        policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            local_operators=(
                LocalOperatorPolicyRule(
                    subjects=("local-owner-agent",),
                    token_labels=("local-owner-write",),
                    products=("example-site",),
                    contexts=("example-site",),
                    instances=("prod",),
                    actions=actions,
                ),
            ),
        )
        if isinstance(store, PostgresRecordStore) and not store.database_url.startswith("sqlite"):
            store.seed_authz_policy_if_absent(
                LaunchplaneAuthzPolicyRecord(
                    record_id="retirement-test-policy",
                    source="test",
                    updated_at=NOW,
                    policy=policy,
                )
            )
        return create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=policy,
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            control_plane_root_path=Path("."),
            record_store_factory=lambda: store,
        )

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer local-operator-token",
            "Idempotency-Key": "retire-example-site-prod",
        }

    async def test_route_requires_database_and_exact_authorization(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            filesystem_store = FilesystemRecordStore(Path(temporary_directory_name))
            database_required = await _asgi_request(
                self._app(filesystem_store, actions=("product_retirement.plan",)),
                "POST",
                "/v1/product-retirement",
                headers=self.headers,
                payload=_plan_payload(),
            )
            store = self._store(Path(temporary_directory_name))
            denied = await _asgi_request(
                self._app(store, actions=("product_environment.read",)),
                "POST",
                "/v1/product-retirement",
                headers=self.headers,
                payload=_plan_payload(),
            )
            store.close()
        self.assertEqual(database_required.status_code, 503)
        self.assertEqual(denied.status_code, 403)

    async def test_active_preview_blocks_plan_before_provider_observation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = self._store(Path(temporary_directory_name))
            store.write_preview_record(
                PreviewRecord(
                    preview_id="preview-active",
                    context="example-site-preview",
                    anchor_repo="every/example-site",
                    anchor_pr_number=1,
                    anchor_pr_url="https://github.com/every/example-site/pull/1",
                    preview_label="launchplane-preview",
                    canonical_url="https://pr-1.example.invalid",
                    state="active",
                    created_at=NOW,
                    updated_at=NOW,
                    eligible_at=NOW,
                )
            )
            with patch(
                "control_plane.http_app.control_plane_product_retirement."
                "observe_tracked_dokploy_application"
            ) as observe:
                response = await _asgi_request(
                    self._app(store, actions=("product_retirement.plan",)),
                    "POST",
                    "/v1/product-retirement",
                    headers=self.headers,
                    payload=_plan_payload(),
                )
            store.close()
        self.assertEqual(response.status_code, 409)
        observe.assert_not_called()

    async def test_reviewed_plan_mismatch_and_changed_provider_are_denied(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = self._store(Path(temporary_directory_name))
            app = self._app(
                store,
                actions=("product_retirement.plan", "product_retirement.apply"),
            )
            with patch(
                "control_plane.product_retirement.observe_tracked_dokploy_application",
                return_value=_observation(),
            ):
                plan = await _asgi_request(
                    app,
                    "POST",
                    "/v1/product-retirement",
                    headers=self.headers,
                    payload=_plan_payload(),
                )
            self.assertEqual(plan.status_code, 202, plan.text)
            mismatched = _apply_payload(plan.json())
            mismatched["reviewed_plan_sha256"] = "f" * 64
            mismatch_response = await _asgi_request(
                app,
                "POST",
                "/v1/product-retirement",
                headers=self.headers,
                payload=mismatched,
            )
            with patch(
                "control_plane.product_retirement.observe_tracked_dokploy_application",
                return_value=_observation(name="changed-name"),
            ):
                changed_response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/product-retirement",
                    headers=self.headers,
                    payload=_apply_payload(plan.json()),
                )
            store.close()
        self.assertEqual(mismatch_response.status_code, 409)
        self.assertEqual(changed_response.status_code, 409)

    async def test_plan_accepts_idle_application_with_explicit_no_deployment_history(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = self._store(Path(temporary_directory_name))
            with patch(
                "control_plane.product_retirement.observe_tracked_dokploy_application",
                return_value=_no_history_observation(),
            ):
                response = await _asgi_request(
                    self._app(store, actions=("product_retirement.plan",)),
                    "POST",
                    "/v1/product-retirement",
                    headers=self.headers,
                    payload=_plan_payload(),
                )
            store.close()
        self.assertEqual(response.status_code, 202, response.text)

    async def test_apply_rejects_no_history_plan_when_provider_evidence_changes(self) -> None:
        changed_observations = {
            "history becomes present": _observation(),
            "application fingerprint changes": _no_history_observation(name="changed-name"),
        }
        for change_name, current_observation in changed_observations.items():
            with (
                self.subTest(change_name=change_name),
                TemporaryDirectory() as temporary_directory_name,
            ):
                store = self._store(Path(temporary_directory_name))
                app = self._app(
                    store,
                    actions=("product_retirement.plan", "product_retirement.apply"),
                )
                with patch(
                    "control_plane.product_retirement.observe_tracked_dokploy_application",
                    return_value=_no_history_observation(),
                ):
                    plan = await _asgi_request(
                        app,
                        "POST",
                        "/v1/product-retirement",
                        headers=self.headers,
                        payload=_plan_payload(),
                    )
                self.assertEqual(plan.status_code, 202, plan.text)
                with patch(
                    "control_plane.product_retirement.observe_tracked_dokploy_application",
                    return_value=current_observation,
                ):
                    apply = await _asgi_request(
                        app,
                        "POST",
                        "/v1/product-retirement",
                        headers=self.headers,
                        payload=_apply_payload(plan.json()),
                    )
                store.close()
                self.assertEqual(apply.status_code, 409, apply.text)

    async def test_provider_absent_apply_retires_and_replays_idempotently(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = self._store(Path(temporary_directory_name))
            app = self._app(
                store,
                actions=("product_retirement.plan", "product_retirement.apply"),
            )
            with patch(
                "control_plane.product_retirement.observe_tracked_dokploy_application",
                side_effect=(_no_history_observation(), _absent_observation()),
            ):
                plan = await _asgi_request(
                    app,
                    "POST",
                    "/v1/product-retirement",
                    headers=self.headers,
                    payload=_plan_payload(),
                )
                self.assertEqual(plan.status_code, 202, plan.text)
                apply = await _asgi_request(
                    app,
                    "POST",
                    "/v1/product-retirement",
                    headers=self.headers,
                    payload=_apply_payload(plan.json()),
                )
            # Simulate the completed reservation persisted by the pre-no-target service.
            stored = store.read_idempotency_record(
                scope=idempotency_scope(
                    LocalOperatorIdentity(
                        subject="local-owner-agent",
                        token_label="local-owner-write",
                    )
                ),
                route_path="/v1/product-retirement",
                idempotency_key=self.headers["Idempotency-Key"],
            )
            assert stored is not None
            legacy_payload = ProductRetirementRequest.model_validate(
                _apply_payload(plan.json())
            ).model_dump(mode="json")
            legacy_payload.pop("no_target")
            store.write_idempotency_record(
                stored.model_copy(
                    update={
                        "request_fingerprint": idempotency_request_fingerprint(
                            route_path="/v1/product-retirement",
                            payload=legacy_payload,
                        ),
                    }
                )
            )
            replay = await _asgi_request(
                app,
                "POST",
                "/v1/product-retirement",
                headers=self.headers,
                payload=_apply_payload(plan.json()),
            )
            profile = store.read_product_profile_record("example-site")
            records = store.list_product_retirement_records(product="example-site")
            store.close()
        self.assertEqual(plan.status_code, 202, plan.text)
        self.assertEqual(apply.status_code, 202, apply.text)
        self.assertEqual(apply.json()["result"]["outcome"], "already_absent")
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["result"]["outcome"], "already_absent")
        self.assertEqual(
            replay.json()["records"]["product_retirement_plan_id"],
            apply.json()["records"]["product_retirement_plan_id"],
        )
        self.assertEqual(profile.lifecycle_state, "retired")
        self.assertNotIn(TARGET_ID, json.dumps(apply.json()))
        self.assertEqual(
            sum(
                record.outcome == "started" and not record.mutation_evidence.finalization_at
                for record in records
            ),
            1,
        )

    async def test_tracked_partial_finalization_recovers_through_http(self) -> None:
        for failure, change in (
            ("profile_write", "unchanged"),
            ("terminal_write", "unchanged"),
            *(
                ("profile_write", change)
                for change in (
                    "key",
                    "request",
                    "plan",
                    "profile",
                    "target",
                    "runtime",
                    "unknown",
                    "present",
                    "missing_evidence",
                )
            ),
        ):
            with self.subTest(failure=failure, change=change), TemporaryDirectory() as directory:
                store = self._store(Path(directory))
                app = self._app(
                    store, actions=("product_retirement.plan", "product_retirement.apply")
                )
                with patch(
                    "control_plane.product_retirement.observe_tracked_dokploy_application",
                    return_value=_observation(),
                ):
                    plan = await _asgi_request(
                        app,
                        "POST",
                        "/v1/product-retirement",
                        headers=self.headers,
                        payload=_plan_payload(),
                    )
                    other_plan = await _asgi_request(
                        app,
                        "POST",
                        "/v1/product-retirement",
                        headers={**self.headers, "Idempotency-Key": "other-plan"},
                        payload=_plan_payload(),
                    )
                self.assertEqual(plan.status_code, 202, plan.text)
                payload = _apply_payload(plan.json())
                original_profile_write = store.compare_and_write_product_profile_record
                original_record_write = store.write_product_retirement_record

                def profile_write(
                    *,
                    expected_record: LaunchplaneProductProfileRecord,
                    replacement_record: LaunchplaneProductProfileRecord,
                ) -> object:
                    replacement = replacement_record
                    if failure == "profile_write" and replacement.lifecycle_state == "retired":
                        raise ValueError("injected final profile write failure")
                    return original_profile_write(
                        expected_record=expected_record, replacement_record=replacement_record
                    )

                def record_write(record: ProductRetirementRecord) -> object:
                    if failure == "terminal_write" and record.outcome == "already_absent":
                        raise ValueError("injected terminal evidence failure")
                    return original_record_write(record)

                with (
                    patch(
                        "control_plane.product_retirement.observe_tracked_dokploy_application",
                        return_value=_absent_observation(),
                    ),
                    patch.object(store, "compare_and_write_product_profile_record", profile_write),
                    patch.object(store, "write_product_retirement_record", record_write),
                ):
                    failed = await _asgi_request(
                        app,
                        "POST",
                        "/v1/product-retirement",
                        headers=self.headers,
                        payload=payload,
                    )
                self.assertEqual(failed.status_code, 409, failed.text)
                with self.assertRaises(FileNotFoundError):
                    store.read_provider_target_record(
                        context_name="example-site", instance_name="prod"
                    )
                # A fresh app and store exercise recovery from durable evidence only.
                store.close()
                store = PostgresRecordStore(
                    database_url=f"sqlite+pysqlite:///{Path(directory) / 'launchplane.sqlite3'}"
                )
                app = self._app(
                    store, actions=("product_retirement.plan", "product_retirement.apply")
                )
                headers = self.headers
                if change == "key":
                    headers = {**headers, "Idempotency-Key": "different-apply"}
                elif change == "request":
                    payload = {**payload, "reason": "different intent"}
                elif change == "plan":
                    payload = _apply_payload(other_plan.json())
                elif change == "profile":
                    profile = store.read_product_profile_record("example-site")
                    store.write_product_profile_record(
                        profile.model_copy(update={"source": "changed"})
                    )
                elif change == "target":
                    store.write_dokploy_target_id_record(
                        DokployTargetIdRecord(
                            context="example-site",
                            instance="prod",
                            target_id="replacement",
                            updated_at=NOW,
                        )
                    )
                elif change == "runtime":
                    store.write_runtime_environment_record(
                        RuntimeEnvironmentRecord(
                            scope="instance",
                            context="example-site",
                            instance="prod",
                            env={"PORT": "4000"},
                            updated_at=NOW,
                        )
                    )
                records = store.list_product_retirement_records(product="example-site")
                if change == "missing_evidence":
                    records = tuple(
                        record for record in records if not record.mutation_evidence.finalization_at
                    )
                with (
                    patch.object(store, "list_product_retirement_records", return_value=records),
                    patch(
                        "control_plane.product_retirement.observe_tracked_dokploy_application",
                        side_effect=TimeoutError("unavailable") if change == "unknown" else None,
                        return_value=_observation()
                        if change == "present"
                        else _absent_observation(),
                    ),
                    patch(
                        "control_plane.product_retirement.dokploy_api.delete_dokploy_application"
                    ) as delete,
                ):
                    retry = await _asgi_request(
                        app,
                        "POST",
                        "/v1/product-retirement",
                        headers=headers,
                        payload=payload,
                    )
                    replay = await _asgi_request(
                        app,
                        "POST",
                        "/v1/product-retirement",
                        headers=headers,
                        payload=payload,
                    )
                if change == "unchanged":
                    self.assertEqual(retry.status_code, 202, retry.text)
                    self.assertEqual(replay.status_code, 202, replay.text)
                    self.assertEqual(replay.json()["result"], retry.json()["result"])
                    self.assertEqual(replay.json()["records"], retry.json()["records"])
                    self.assertTrue(retry.json()["result"]["provider_absence_verified"])
                    self.assertEqual(
                        store.read_product_profile_record("example-site").lifecycle_state, "retired"
                    )
                    delete.assert_not_called()
                else:
                    self.assertEqual(retry.status_code, 409, retry.text)
                    self.assertEqual(replay.status_code, 409, replay.text)
                    self.assertEqual(
                        store.read_product_profile_record("example-site").lifecycle_state,
                        "retiring",
                    )
                    held = store.read_idempotency_record(
                        scope=idempotency_scope(
                            LocalOperatorIdentity(
                                subject="local-owner-agent", token_label="local-owner-write"
                            )
                        ),
                        route_path="/v1/product-retirement",
                        idempotency_key=self.headers["Idempotency-Key"],
                    )
                    assert held is not None
                    self.assertEqual(held.state, "reconcile_required")
                    delete.assert_not_called()
                store.close()

    async def test_tracked_uncertain_completion_retry_preserves_conflict_guards(self) -> None:
        for change in (
            "key",
            "request",
            "plan",
            "observation",
            "unknown",
            "retry_observation",
            "unchanged",
        ):
            with self.subTest(change=change), TemporaryDirectory() as directory:
                store = self._store(Path(directory))
                app = self._app(
                    store, actions=("product_retirement.plan", "product_retirement.apply")
                )
                with patch(
                    "control_plane.product_retirement.observe_tracked_dokploy_application",
                    return_value=_observation(),
                ):
                    plan = await _asgi_request(
                        app,
                        "POST",
                        "/v1/product-retirement",
                        headers=self.headers,
                        payload=_plan_payload(),
                    )
                    replacement_plan = await _asgi_request(
                        app,
                        "POST",
                        "/v1/product-retirement",
                        headers={**self.headers, "Idempotency-Key": "another-plan-key"},
                        payload=_plan_payload(),
                    )
                self.assertEqual(plan.status_code, 202, plan.text)
                self.assertEqual(replacement_plan.status_code, 202, replacement_plan.text)
                payload = _apply_payload(plan.json())
                with (
                    patch(
                        "control_plane.product_retirement.observe_tracked_dokploy_application",
                        return_value=_observation(),
                    ),
                    patch(
                        "control_plane.product_retirement.dokploy_source.read_dokploy_config",
                        return_value=("https://provider.invalid", "test"),
                    ),
                    patch(
                        "control_plane.product_retirement.dokploy_api.delete_dokploy_application",
                        side_effect=TimeoutError("lost delete response"),
                    ),
                ):
                    failed = await _asgi_request(
                        app,
                        "POST",
                        "/v1/product-retirement",
                        headers=self.headers,
                        payload=payload,
                    )
                self.assertEqual(failed.status_code, 409, failed.text)
                headers = self.headers
                if change == "key":
                    headers = {**headers, "Idempotency-Key": "different-apply-key"}
                elif change == "request":
                    payload = {**payload, "reason": "A different retirement intent."}
                elif change == "plan":
                    payload = _apply_payload(replacement_plan.json())
                observations: list[ProductRetirementProviderObservation | Exception] = [
                    _observation(name="changed-name")
                    if change == "observation"
                    else TimeoutError("observation unavailable")
                ]
                if change == "unchanged":
                    observations = [_observation(), _observation(), _absent_observation()]
                elif change == "key":
                    observations = [_observation(), _absent_observation()]
                elif change == "retry_observation":
                    observations = [
                        _observation(),
                        TimeoutError("retry observation unavailable"),
                        _observation(),
                        TimeoutError("retry observation unavailable"),
                    ]
                with (
                    patch(
                        "control_plane.product_retirement.observe_tracked_dokploy_application",
                        side_effect=observations,
                    ) as observe,
                    patch(
                        "control_plane.product_retirement.dokploy_source.read_dokploy_config",
                        return_value=("https://provider.invalid", "test"),
                    ),
                    patch(
                        "control_plane.product_retirement.dokploy_api.delete_dokploy_application"
                    ) as delete,
                ):
                    retry = await _asgi_request(
                        app,
                        "POST",
                        "/v1/product-retirement",
                        headers=headers,
                        payload=payload,
                    )
                    if change == "retry_observation":
                        self.assertEqual(retry.status_code, 409, retry.text)
                        retry = await _asgi_request(
                            app,
                            "POST",
                            "/v1/product-retirement",
                            headers=headers,
                            payload=payload,
                        )
                if change == "unchanged":
                    self.assertEqual(retry.status_code, 202, retry.text)
                    self.assertTrue(retry.json()["result"]["provider_absence_verified"])
                    self.assertEqual(
                        store.read_product_profile_record("example-site").lifecycle_state,
                        "retired",
                    )
                    delete.assert_called_once()
                else:
                    self.assertEqual(retry.status_code, 409, retry.text)
                    self.assertEqual(
                        retry.json()["error"]["code"],
                        {
                            "request": "product_retirement_blocked",
                            "plan": "idempotency_key_reused",
                        }.get(change, "mutation_reconciliation_required"),
                    )
                    self.assertEqual(
                        store.read_product_profile_record("example-site").lifecycle_state,
                        "retiring",
                    )
                    delete.assert_not_called()
                    if change in {"key", "request", "plan"}:
                        observe.assert_not_called()
                    held = store.read_idempotency_record(
                        scope=idempotency_scope(
                            LocalOperatorIdentity(
                                subject="local-owner-agent", token_label="local-owner-write"
                            )
                        ),
                        route_path="/v1/product-retirement",
                        idempotency_key=self.headers["Idempotency-Key"],
                    )
                    self.assertIsNotNone(held)
                    assert held is not None
                    self.assertEqual(held.state, "reconcile_required")
                store.close()

    async def test_provider_failure_persists_reconciliation_and_retiring_lifecycle(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = self._store(Path(temporary_directory_name))
            app = self._app(
                store,
                actions=("product_retirement.plan", "product_retirement.apply"),
            )
            with patch(
                "control_plane.product_retirement.observe_tracked_dokploy_application",
                return_value=_observation(),
            ):
                plan = await _asgi_request(
                    app,
                    "POST",
                    "/v1/product-retirement",
                    headers=self.headers,
                    payload=_plan_payload(),
                )
            self.assertEqual(plan.status_code, 202, plan.text)
            provider_failure = DokployRequestFailed(
                method="POST",
                path="/api/application.delete",
                detail="lost response",
                status_code=500,
            )
            with (
                patch(
                    "control_plane.product_retirement.observe_tracked_dokploy_application",
                    return_value=_observation(),
                ),
                patch(
                    "control_plane.product_retirement.dokploy_source.read_dokploy_config",
                    return_value=("https://dokploy.invalid", "token"),
                ),
                patch(
                    "control_plane.product_retirement.dokploy_api.delete_dokploy_application",
                    side_effect=provider_failure,
                ),
            ):
                apply = await _asgi_request(
                    app,
                    "POST",
                    "/v1/product-retirement",
                    headers=self.headers,
                    payload=_apply_payload(plan.json()),
                )
            profile = store.read_product_profile_record("example-site")
            records = store.list_product_retirement_records(product="example-site")
            outcomes = {record.outcome for record in records}
            reconcile_record = next(
                record for record in records if record.outcome == "reconcile_required"
            )
            reader = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(
                    schema_version=2,
                    local_operators=(
                        LocalOperatorPolicyRule(
                            subjects=("local-owner-agent",),
                            token_labels=("local-owner-write",),
                            products=("launchplane",),
                            contexts=("example-site",),
                            instances=("prod",),
                            actions=("operations.read",),
                        ),
                    ),
                ),
                bearer_identity_config=_local_operator_bearer_config(
                    token_label="local-owner-write"
                ),
                record_store_factory=lambda: store,
            )
            read = await _asgi_request(
                reader,
                "GET",
                f"/v1/product-retirements/{reconcile_record.record_id}",
                headers={"Authorization": "Bearer local-operator-token"},
            )
            denied = await _asgi_request(
                app,
                "GET",
                f"/v1/product-retirements/{reconcile_record.record_id}",
                headers={"Authorization": "Bearer local-operator-token"},
            )
            with (
                patch(
                    "control_plane.product_retirement.observe_tracked_dokploy_application",
                    return_value=_absent_observation(),
                ) as observe,
                patch(
                    "control_plane.product_retirement.dokploy_api.delete_dokploy_application"
                ) as delete,
            ):
                retry = await _asgi_request(
                    app,
                    "POST",
                    "/v1/product-retirement",
                    headers=self.headers,
                    payload=_apply_payload(plan.json()),
                )
                self.assertEqual(retry.status_code, 202, retry.text)
                self.assertTrue(retry.json()["result"]["provider_absence_verified"])
                self.assertEqual(
                    store.read_product_profile_record("example-site").lifecycle_state, "retired"
                )
                self.assertGreater(observe.call_count, 0)
                delete.assert_not_called()
            store.close()
        self.assertEqual(apply.status_code, 409)
        self.assertEqual(profile.lifecycle_state, "retiring")
        self.assertIn("reconcile_required", outcomes)
        self.assertEqual(read.status_code, 200, read.text)
        view = read.json()["record"]
        self.assertEqual(
            (view["outcome"], view["lifecycle_after"], view["provider_effect_attempted"]),
            ("reconcile_required", "retiring", True),
        )
        self.assertEqual(view["error_code"], reconcile_record.mutation_evidence.error_code)
        self.assertTrue(view["free_text_omitted"])
        self.assertNotIn("lost response", read.text)
        self.assertNotIn(reconcile_record.reason, read.text)
        self.assertEqual(denied.status_code, 403)


if __name__ == "__main__":
    unittest.main()
