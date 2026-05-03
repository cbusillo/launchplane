from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_environment_read_model import (
    ACTION_AUTHZ_BY_ROUTE,
    build_product_environment_detail,
    build_product_site_overview,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.secret_record import SecretBinding
from control_plane.storage.postgres import PostgresRecordStore


def _site_profile_payload(
    *,
    product: str = "example-site",
    preview_enabled: bool = True,
    preview_context: str = "shared-preview",
    testing_context: str = "example-site-testing",
    prod_context: str = "example-site-prod",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": product,
        "display_name": "Example Site",
        "repository": f"every/{product}",
        "driver_id": "generic-web",
        "image": {"repository": f"ghcr.io/every/{product}"},
        "runtime_port": 3000,
        "health_path": "/healthz",
        "lanes": (
            {
                "instance": "testing",
                "context": testing_context,
                "base_url": f"https://testing.{product}.example",
                "health_url": f"https://testing.{product}.example/healthz",
            },
            {
                "instance": "prod",
                "context": prod_context,
                "base_url": f"https://{product}.example",
                "health_url": f"https://{product}.example/healthz",
            },
        ),
        "preview": {
            "enabled": preview_enabled,
            "context": preview_context,
            "slug_template": "pr-{number}",
        },
        "updated_at": "2026-05-02T22:30:00Z",
        "source": "test",
    }


def _preview_record(
    *,
    preview_id: str,
    context: str,
    anchor_repo: str,
    state: str,
    updated_at: str,
) -> PreviewRecord:
    return PreviewRecord.model_validate(
        {
            "schema_version": 1,
            "preview_id": preview_id,
            "context": context,
            "anchor_repo": anchor_repo,
            "anchor_pr_number": 1,
            "anchor_pr_url": "https://github.com/every/example-site/pull/1",
            "preview_label": "pr-1",
            "canonical_url": f"https://{preview_id}.example.invalid",
            "state": state,
            "created_at": "2026-05-02T09:00:00Z",
            "updated_at": updated_at,
            "eligible_at": "2026-05-02T09:00:00Z",
        }
    )


class _PreviewRecordStore:
    def __init__(
        self, profile: LaunchplaneProductProfileRecord, previews: tuple[PreviewRecord, ...]
    ) -> None:
        self._profile = profile
        self._previews = previews
        self.preview_record_calls: list[tuple[str, str]] = []

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != self._profile.product:
            raise FileNotFoundError(product)
        return self._profile

    def list_product_profile_records(
        self, *, driver_id: str = ""
    ) -> tuple[LaunchplaneProductProfileRecord, ...]:
        if driver_id and driver_id != self._profile.driver_id:
            return ()
        return (self._profile,)

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]:
        self.preview_record_calls.append((context_name, anchor_repo))
        return self._previews


class ProductEnvironmentReadModelTest(unittest.TestCase):
    def test_action_authz_map_matches_live_service_handlers(self) -> None:
        self.assertEqual(
            ACTION_AUTHZ_BY_ROUTE["/v1/drivers/odoo/artifact-publish"],
            "odoo_artifact_publish.write",
        )
        self.assertEqual(
            ACTION_AUTHZ_BY_ROUTE["/v1/drivers/verireel/testing-verification"],
            "deployment.write",
        )
        self.assertEqual(
            ACTION_AUTHZ_BY_ROUTE["/v1/drivers/verireel/runtime-verification"],
            "verireel_stable_environment.read",
        )
        self.assertEqual(
            ACTION_AUTHZ_BY_ROUTE["/v1/drivers/verireel/preview-verification"],
            "preview_generation.write",
        )

    def test_product_site_overview_filters_preview_summaries_by_repository_and_state(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(_site_profile_payload())
        store = _PreviewRecordStore(
            profile,
            (
                _preview_record(
                    preview_id="other-site-active",
                    context="shared-preview",
                    anchor_repo="other-site",
                    state="active",
                    updated_at="2026-05-02T14:00:00Z",
                ),
                _preview_record(
                    preview_id="example-site-destroyed",
                    context="shared-preview",
                    anchor_repo="example-site",
                    state="destroyed",
                    updated_at="2026-05-02T13:00:00Z",
                ),
                _preview_record(
                    preview_id="example-site-active",
                    context="shared-preview",
                    anchor_repo="example-site",
                    state="active",
                    updated_at="2026-05-02T12:00:00Z",
                ),
            ),
        )

        overview = build_product_site_overview(
            record_store=store,
            product=profile.product,
            action_allowed=lambda *_: False,
        )

        self.assertIn(("shared-preview", "example-site"), store.preview_record_calls)
        self.assertEqual(overview.preview.active_count, 1)
        self.assertEqual(overview.preview.latest_preview_id, "example-site-active")

    def test_product_site_overview_uses_canonical_prod_context_for_prod_actions(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(
                preview_enabled=False,
                testing_context="example-site-prod",
                prod_context="example-site-prod",
            )
        )

        def action_allowed(action: str, product: str, context: str) -> bool:
            return (
                action
                in {
                    "generic_web_prod_promotion.dispatch",
                    "generic_web_prod_promotion.execute",
                }
                and context == "example-site-prod"
            )

        overview = build_product_site_overview(
            record_store=_PreviewRecordStore(profile, ()),
            product=profile.product,
            action_allowed=action_allowed,
        )

        actions = {action.action_id: action for action in overview.available_actions}
        self.assertTrue(actions["prod_promotion_workflow"].enabled)
        self.assertTrue(actions["prod_promotion"].enabled)
        self.assertFalse(actions["preview_refresh"].enabled)

    def test_product_site_overview_uses_testing_context_for_deploy_actions(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False)
        )

        def action_allowed(action: str, product: str, context: str) -> bool:
            return action == "generic_web_deploy.execute" and context == "example-site-testing"

        overview = build_product_site_overview(
            record_store=_PreviewRecordStore(profile, ()),
            product=profile.product,
            action_allowed=action_allowed,
        )

        actions = {action.action_id: action for action in overview.available_actions}
        self.assertTrue(actions["stable_deploy"].enabled)
        self.assertFalse(actions["prod_promotion"].enabled)

    def test_product_site_overview_disables_generic_web_prod_promotion_for_mixed_contexts(
        self,
    ) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False)
        )

        overview = build_product_site_overview(
            record_store=_PreviewRecordStore(profile, ()),
            product=profile.product,
            action_allowed=lambda *_: True,
        )

        actions = {action.action_id: action for action in overview.available_actions}
        self.assertTrue(actions["prod_promotion_workflow"].enabled)
        self.assertFalse(actions["prod_promotion"].enabled)
        self.assertIn(
            "share a context",
            actions["prod_promotion"].disabled_reasons[0],
        )

    def test_preview_disabled_hides_generic_web_preview_actions(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False, preview_context="")
        )

        overview = build_product_site_overview(
            record_store=_PreviewRecordStore(profile, ()),
            product=profile.product,
            action_allowed=lambda *_: True,
        )

        actions = {action.action_id: action for action in overview.available_actions}
        self.assertFalse(actions["preview_desired_state"].enabled)
        self.assertFalse(actions["preview_inventory"].enabled)
        self.assertFalse(actions["preview_readiness"].enabled)
        self.assertFalse(actions["preview_refresh"].enabled)
        self.assertFalse(actions["preview_destroy"].enabled)

    def test_product_site_overview_does_not_enable_prod_actions_for_testing_only_authz(
        self,
    ) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False)
        )

        def action_allowed(action: str, product: str, context: str) -> bool:
            return context == "example-site-testing" and action in {
                "generic_web_prod_promotion.dispatch",
                "generic_web_prod_promotion.execute",
            }

        overview = build_product_site_overview(
            record_store=_PreviewRecordStore(profile, ()),
            product=profile.product,
            action_allowed=action_allowed,
        )

        actions = {action.action_id: action for action in overview.available_actions}
        self.assertFalse(actions["prod_promotion_workflow"].enabled)
        self.assertFalse(actions["prod_promotion"].enabled)

    def test_product_site_overview_hides_prod_actions_when_no_prod_lane_exists(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            {
                "schema_version": 1,
                "product": "verireel",
                "display_name": "VeriReel",
                "repository": "every/verireel",
                "driver_id": "verireel",
                "image": {"repository": "ghcr.io/every/verireel"},
                "runtime_port": 3000,
                "health_path": "/healthz",
                "lanes": (
                    {
                        "instance": "testing",
                        "context": "verireel-testing",
                        "base_url": "https://testing.verireel.example",
                        "health_url": "https://testing.verireel.example/healthz",
                    },
                ),
                "preview": {
                    "enabled": False,
                    "context": "",
                    "slug_template": "pr-{number}",
                },
                "updated_at": "2026-05-02T22:30:00Z",
                "source": "test",
            }
        )

        overview = build_product_site_overview(
            record_store=_PreviewRecordStore(profile, ()),
            product=profile.product,
            action_allowed=lambda *_: True,
        )

        actions = {action.action_id: action for action in overview.available_actions}
        self.assertFalse(actions["prod_deploy"].enabled)
        self.assertFalse(actions["prod_backup_gate"].enabled)
        self.assertFalse(actions["prod_promotion"].enabled)
        self.assertFalse(actions["prod_rollback"].enabled)
        self.assertIn("prod lane", actions["prod_deploy"].disabled_reasons[0])

    def test_product_environment_detail_preserves_disabled_secret_bindings(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path}"
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            profile = LaunchplaneProductProfileRecord.model_validate(
                _site_profile_payload(preview_enabled=False, preview_context="")
            )
            store.write_product_profile_record(profile)
            store.write_secret_binding(
                SecretBinding(
                    binding_id="binding-1",
                    secret_id="secret-1",
                    integration="runtime_environment",
                    binding_key="SMTP_PASSWORD",
                    context="example-site-prod",
                    instance="prod",
                    status="disabled",
                    created_at="2026-05-02T22:31:00Z",
                    updated_at="2026-05-02T22:32:00Z",
                )
            )

            detail = build_product_environment_detail(
                record_store=store,
                product=profile.product,
                environment="prod",
                action_allowed=lambda *_: False,
            )

        self.assertEqual(detail.managed_secrets[0].status, "disabled")
        self.assertEqual(detail.managed_secrets[0].trust_state, "disabled")
