import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_retirement import (
    ProductRetirementProviderObservation,
    provider_identifier_sha256,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.dokploy.api import DokployRequestFailed
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.product_retirement import build_provider_observation
from control_plane.service_auth import LaunchplaneAuthzPolicy, LocalOperatorPolicyRule
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
        payload={"applicationId": TARGET_ID, "name": name, "applicationStatus": "running"},
        domains=(),
        latest_deployment={"status": "done"},
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
    def _store(self, root: Path) -> PostgresRecordStore:
        store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{root / 'launchplane.sqlite3'}"
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
        return create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(
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
            ),
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

    async def test_provider_absent_apply_retires_and_replays_idempotently(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = self._store(Path(temporary_directory_name))
            app = self._app(
                store,
                actions=("product_retirement.plan", "product_retirement.apply"),
            )
            with patch(
                "control_plane.product_retirement.observe_tracked_dokploy_application",
                side_effect=(_observation(), _absent_observation()),
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
        self.assertEqual(sum(record.outcome == "started" for record in records), 1)

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
            outcomes = {
                record.outcome
                for record in store.list_product_retirement_records(product="example-site")
            }
            store.close()
        self.assertEqual(apply.status_code, 409)
        self.assertEqual(profile.lifecycle_state, "retiring")
        self.assertIn("reconcile_required", outcomes)


if __name__ == "__main__":
    unittest.main()
