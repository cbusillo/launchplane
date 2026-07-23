from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
import unittest

from control_plane.contracts.artifact_identity import (
    ArtifactIdentityManifest,
)
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_health_monitoring_migration import (
    canonical_health_check_record_token,
)
from control_plane.contracts.product_environment_read_model import (
    ACTION_AUTHZ_BY_ROUTE,
    ProductEnvironmentReadModelCapabilityError,
    build_product_activity_read_model,
    build_product_environment_config_status,
    build_product_environment_detail,
    build_product_site_overview,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.public_ingress_monitoring import PublicIngressObservationRecord
from control_plane.contracts.public_ingress_monitoring import PublicIngressIncidentRecord
from control_plane.contracts.public_ingress_monitoring import PublicIngressTargetObservation
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    RouteBindingDomain,
    RouteBindingIngress,
    RouteBindingProviderTarget,
    RouteBindingSource,
    RouteBindingTls,
)
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    HealthcheckEvidence,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.secret_record import SecretBinding
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.service_auth import LaunchplaneAuthzPolicy
from tests.support.artifact_manifests import artifact_manifest_v2


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
                "odoo_prelaunch_rebuild": {
                    "enabled": True,
                    "approval_issue_url": "https://github.com/cbusillo/launchplane/issues/573",
                    "data_source_mode": "upstream_restore",
                    "confirmation": "restore opw upstream",
                    "expected_target_name": f"{product}-prod",
                    "expected_domains": [f"{product}.example"],
                },
                "odoo_data_policy": {
                    "data_authority": "restorable",
                    "allowed_rebuild_sources": ["upstream_restore"],
                    "upstream_source": "example-site/prod/upstream",
                    "requires_backup_before_destroy": True,
                    "requires_restore_proof": True,
                    "requires_runtime_identity": True,
                },
            },
        ),
        "preview": {
            "enabled": preview_enabled,
            "context": preview_context,
            "slug_template": "pr-{number}",
        },
        "expected_config": {
            "runtime_environment_keys": [
                {"key": "PUBLIC_BASE_URL", "context": testing_context, "instance": "testing"},
                {"key": "RESEND_FROM_EMAIL", "context": prod_context, "instance": "prod"},
            ],
            "managed_secret_bindings": [
                {"binding_key": "SMTP_PASSWORD", "context": prod_context, "instance": "prod"},
                {"binding_key": "RESEND_API_KEY", "context": prod_context, "instance": "prod"},
            ],
        },
        "updated_at": "2026-05-02T22:30:00Z",
        "source": "test",
    }


def _odoo_profile_payload() -> dict[str, object]:
    payload = _site_profile_payload(
        product="odoo-tenant-cm",
        preview_context="cm",
        testing_context="cm",
        prod_context="cm",
    )
    payload["display_name"] = "Odoo Tenant CM"
    payload["repository"] = "cbusillo/odoo-tenant-cm"
    payload["driver_id"] = "odoo"
    return payload


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


class _RuntimeIdentityReadModelStore(_PreviewRecordStore):
    def __init__(
        self,
        profile: LaunchplaneProductProfileRecord,
        inventory: EnvironmentInventory,
    ) -> None:
        super().__init__(profile, ())
        self._inventory = inventory
        self._artifact_manifest = artifact_manifest_v2(
            artifact_id="ghcr.io/every/example-site@sha256:abc123",
            image_repository="ghcr.io/every/example-site",
        )

    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory:
        if context_name == self._inventory.context and instance_name == self._inventory.instance:
            return self._inventory
        raise FileNotFoundError(f"{context_name}/{instance_name}")

    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest:
        if artifact_id == self._artifact_manifest.artifact_id:
            return self._artifact_manifest
        raise FileNotFoundError(artifact_id)


class _PublicIngressReadModelStore(_PreviewRecordStore):
    def __init__(
        self,
        profile: LaunchplaneProductProfileRecord,
        observations: tuple[PublicIngressObservationRecord, ...],
        incidents: tuple[PublicIngressIncidentRecord, ...] = (),
    ) -> None:
        super().__init__(profile, ())
        self._observations = observations
        self._incidents = incidents

    def list_public_ingress_observation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        check_name: str = "",
        check_kind: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressObservationRecord, ...]:
        records = [
            record
            for record in self._observations
            if (not product or record.product == product)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
            and (
                not check_name
                or canonical_health_check_record_token(record.check_name)
                == canonical_health_check_record_token(check_name)
            )
            and (not check_kind or record.check_kind == check_kind)
        ]
        records.sort(key=lambda record: (record.observed_at, record.record_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def list_public_ingress_incident_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        check_name: str = "",
        check_kind: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressIncidentRecord, ...]:
        incidents = [
            incident
            for incident in self._incidents
            if (not product or incident.product == product)
            and (not context_name or incident.context == context_name)
            and (not instance_name or incident.instance == instance_name)
            and (
                not check_name
                or canonical_health_check_record_token(incident.check_name)
                == canonical_health_check_record_token(check_name)
            )
            and (not check_kind or incident.check_kind == check_kind)
            and (not status or incident.status == status)
        ]
        incidents.sort(
            key=lambda incident: (incident.opened_at, incident.incident_id), reverse=True
        )
        if limit is not None:
            incidents = incidents[:limit]
        return tuple(incidents)


class _PublicIngressObservationsOnlyStore(_PreviewRecordStore):
    def __init__(
        self,
        profile: LaunchplaneProductProfileRecord,
        observations: tuple[PublicIngressObservationRecord, ...],
    ) -> None:
        super().__init__(profile, ())
        self._observations = observations

    def list_public_ingress_observation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        check_name: str = "",
        check_kind: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressObservationRecord, ...]:
        records = [
            record
            for record in self._observations
            if (not product or record.product == product)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
            and (
                not check_name
                or canonical_health_check_record_token(record.check_name)
                == canonical_health_check_record_token(check_name)
            )
            and (not check_kind or record.check_kind == check_kind)
        ]
        records.sort(key=lambda record: (record.observed_at, record.record_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)


def _managed_github_rule(
    *,
    managed_rule_id: str,
    products: tuple[str, ...],
    actions: tuple[str, ...] = ("product_profile.read",),
    managed_set_id: str = "test.product-activity",
) -> dict[str, object]:
    return {
        "managed_set_id": managed_set_id,
        "managed_rule_id": managed_rule_id,
        "repository": "every/product-operator",
        "repository_id": "1001",
        "repository_owner_id": "2001",
        "products": products,
        "contexts": ("*",),
        "actions": actions,
    }


def _authz_policy(*rules: dict[str, object]) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_actions": rules,
        }
    )


def _authz_record(
    *,
    record_id: str,
    revision: int,
    status: str,
    updated_at: str,
    policy: LaunchplaneAuthzPolicy,
    audit: dict[str, object] | None = None,
    source: str = "test-policy",
) -> LaunchplaneAuthzPolicyRecord:
    return LaunchplaneAuthzPolicyRecord.model_validate(
        {
            "record_id": record_id,
            "revision": revision,
            "status": status,
            "source": source,
            "updated_at": updated_at,
            "policy": policy,
            "audit": audit or {},
        }
    )


def _managed_authz_audit(
    *,
    previous_record_id: str,
    changes: tuple[dict[str, object], ...],
    managed_set_id: str = "test.product-activity",
) -> dict[str, object]:
    return {
        "operation": "managed_rule_set_reconcile",
        "managed_set_id": managed_set_id,
        "previous_policy_record_id": previous_record_id,
        "diff": {
            "managed_set_id": managed_set_id,
            "previous_record_id": previous_record_id,
            "changes": changes,
        },
    }


def _default_authz_activity_records() -> tuple[LaunchplaneAuthzPolicyRecord, ...]:
    previous_record = _authz_record(
        record_id="authz-policy-1",
        revision=1,
        status="superseded",
        updated_at="2026-05-02T12:59:00Z",
        policy=_authz_policy(),
    )
    current_record = _authz_record(
        record_id="authz-policy-2",
        revision=2,
        status="active",
        updated_at="2026-05-02T13:00:00Z",
        policy=_authz_policy(
            _managed_github_rule(
                managed_rule_id="example-site.read",
                products=("example-site",),
            )
        ),
        audit=_managed_authz_audit(
            previous_record_id=previous_record.record_id,
            changes=(
                {
                    "managed_rule_id": "example-site.read",
                    "change": "added",
                    "previous_principal_type": None,
                    "desired_principal_type": "github_actions",
                },
            ),
        ),
    )
    return (current_record, previous_record)


class _ActivityRecordStore(_PreviewRecordStore):
    def __init__(
        self,
        profile: LaunchplaneProductProfileRecord,
        previews: tuple[PreviewRecord, ...],
        *,
        authz_records: tuple[LaunchplaneAuthzPolicyRecord, ...] | None = None,
    ) -> None:
        super().__init__(profile, previews)
        self._authz_records = authz_records or _default_authz_activity_records()

    def list_deployment_records(
        self, *, context_name: str = "", instance_name: str = "", limit: int | None = None
    ) -> tuple[object, ...]:
        if context_name != "example-site-prod" or instance_name != "prod":
            return ()
        return (
            SimpleNamespace(
                record_id="deployment-prod-1",
                deploy=SimpleNamespace(
                    status="pass",
                    started_at="2026-05-02T10:00:00Z",
                    finished_at="2026-05-02T10:05:00Z",
                ),
            ),
        )

    def list_promotion_records(
        self,
        *,
        context_name: str = "",
        from_instance_name: str = "",
        to_instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[object, ...]:
        if context_name != "example-site-prod" or to_instance_name != "prod":
            return ()
        return (
            SimpleNamespace(
                record_id="promotion-prod-1",
                deployment_record_id="deployment-prod-1",
                backup_record_id="backup-prod-1",
                from_instance="testing",
                to_instance="prod",
                deploy=SimpleNamespace(
                    status="pass",
                    started_at="2026-05-02T11:00:00Z",
                    finished_at="2026-05-02T11:07:00Z",
                ),
                rollback=SimpleNamespace(
                    attempted=False, status="skipped", started_at="", finished_at=""
                ),
            ),
        )

    def list_backup_gate_records(
        self, *, context_name: str = "", instance_name: str = "", limit: int | None = None
    ) -> tuple[object, ...]:
        if context_name != "example-site-prod" or instance_name != "prod":
            return ()
        return (
            SimpleNamespace(
                record_id="backup-prod-1",
                status="pass",
                created_at="2026-05-02T10:50:00Z",
            ),
        )

    def list_preview_desired_state_records(
        self, *, context_name: str = "", limit: int | None = None
    ) -> tuple[object, ...]:
        if context_name != "shared-preview":
            return ()
        return (
            SimpleNamespace(
                desired_state_id="desired-example-1",
                product="example-site",
                context="shared-preview",
                status="pass",
                discovered_at="2026-05-02T12:00:00Z",
                desired_count=1,
            ),
            SimpleNamespace(
                desired_state_id="desired-other-1",
                product="other-site",
                context="shared-preview",
                status="pass",
                discovered_at="2026-05-02T12:30:00Z",
                desired_count=1,
            ),
        )

    def list_preview_lifecycle_cleanup_records(
        self, *, context_name: str = "", limit: int | None = None
    ) -> tuple[object, ...]:
        return ()

    def list_preview_pr_feedback_records(
        self, *, context_name: str = "", limit: int | None = None
    ) -> tuple[object, ...]:
        return ()

    def list_authz_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[object, ...]:
        records = tuple(
            record for record in self._authz_records if not status or record.status == status
        )
        return records[:limit] if limit is not None else records


class _ManyDeploymentActivityRecordStore(_PreviewRecordStore):
    def __init__(
        self, profile: LaunchplaneProductProfileRecord, previews: tuple[PreviewRecord, ...]
    ) -> None:
        super().__init__(profile, previews)
        self.deployment_limits: list[int | None] = []

    def list_deployment_records(
        self, *, context_name: str = "", instance_name: str = "", limit: int | None = None
    ) -> tuple[object, ...]:
        self.deployment_limits.append(limit)
        if context_name != "example-site-prod" or instance_name != "prod":
            return ()
        records = tuple(
            SimpleNamespace(
                record_id=f"deployment-prod-{index}",
                deploy=SimpleNamespace(
                    status="pass",
                    started_at=f"2026-05-02T10:{index:02d}:00Z",
                    finished_at=f"2026-05-02T10:{index:02d}:30Z",
                ),
            )
            for index in range(12, 0, -1)
        )
        if limit is not None:
            return records[:limit]
        return records


class _HistoricalActivityRecordStore(_PreviewRecordStore):
    def __init__(
        self, profile: LaunchplaneProductProfileRecord, previews: tuple[PreviewRecord, ...]
    ) -> None:
        super().__init__(profile, previews)
        self.deployment_calls: list[tuple[str, str, int | None]] = []

    def list_deployment_records(
        self, *, context_name: str = "", instance_name: str = "", limit: int | None = None
    ) -> tuple[object, ...]:
        self.deployment_calls.append((context_name, instance_name, limit))
        if instance_name != "prod" or context_name not in {
            "example-site-prod",
            "example-site-legacy",
        }:
            return ()
        return (
            SimpleNamespace(
                record_id=f"deployment-{context_name}",
                deploy=SimpleNamespace(
                    status="pass",
                    started_at="2026-05-02T10:00:00Z",
                    finished_at=(
                        "2026-05-02T12:00:00Z"
                        if context_name == "example-site-prod"
                        else "2026-05-02T09:00:00Z"
                    ),
                ),
            ),
        )


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

    def test_product_site_overview_hides_non_operator_driver_actions(self) -> None:
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
                    {
                        "instance": "prod",
                        "context": "verireel",
                        "base_url": "https://verireel.example",
                        "health_url": "https://verireel.example/healthz",
                    },
                ),
                "preview": {
                    "enabled": True,
                    "context": "verireel-testing",
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
        self.assertNotIn("testing_verification", actions)
        self.assertNotIn("preview_verification", actions)
        self.assertTrue(
            all(environment.driver_extensions.odoo is None for environment in overview.environments)
        )

    def test_odoo_product_site_overview_uses_inherited_generic_web_actions(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(_odoo_profile_payload())

        overview = build_product_site_overview(
            record_store=_PreviewRecordStore(profile, ()),
            product=profile.product,
            action_allowed=lambda *_: True,
        )

        actions = {action.action_id: action for action in overview.available_actions}
        self.assertEqual(actions["stable_deploy"].route_path, "/v1/drivers/generic-web/deploy")
        self.assertEqual(
            actions["preview_refresh"].route_path,
            "/v1/drivers/generic-web/preview-refresh",
        )
        self.assertEqual(
            actions["preview_apply_inputs"].route_path,
            "/v1/drivers/odoo/preview-apply-inputs",
        )
        self.assertEqual(actions["preview_apply"].route_path, "/v1/drivers/odoo/preview-apply")
        self.assertNotIn("preview_verification", actions)
        self.assertTrue(
            all(
                environment.driver_extensions.odoo is not None
                for environment in overview.environments
            )
        )

    def test_product_site_overview_raises_for_unknown_product(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(_site_profile_payload())
        store = _PreviewRecordStore(profile, ())

        with self.assertRaisesRegex(FileNotFoundError, "unknown-site"):
            build_product_site_overview(
                record_store=store,
                product="unknown-site",
                action_allowed=lambda _action, _product, _context, _instances: True,
            )

    def test_product_environment_detail_raises_for_unknown_environment(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(_site_profile_payload())
        store = _PreviewRecordStore(profile, ())

        with self.assertRaisesRegex(FileNotFoundError, "staging"):
            build_product_environment_detail(
                record_store=store,
                product="example-site",
                environment="staging",
                action_allowed=lambda _action, _product, _context, _instances: True,
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

        def action_allowed(
            action: str,
            product: str,
            context: str,
            _instances: tuple[str, ...],
        ) -> bool:
            return (
                action
                in {
                    "generic_web_prod_promotion.dispatch",
                    "generic_web_prod_promotion.execute",
                    "generic_web_prod_rollback.plan",
                    "generic_web_prod_rollback.execute",
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
        self.assertTrue(actions["prod_rollback_plan"].enabled)
        self.assertTrue(actions["prod_rollback"].enabled)
        self.assertFalse(actions["preview_refresh"].enabled)

    def test_odoo_product_site_overview_uses_prod_context_for_inherited_rollback_plan(
        self,
    ) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(_odoo_profile_payload())
        seen_contexts: list[tuple[str, str]] = []

        def action_allowed(
            action: str,
            _product: str,
            context: str,
            _instances: tuple[str, ...],
        ) -> bool:
            if action == "generic_web_prod_rollback.plan":
                seen_contexts.append((action, context))
                return context == "cm"
            return True

        overview = build_product_site_overview(
            record_store=_PreviewRecordStore(profile, ()),
            product=profile.product,
            action_allowed=action_allowed,
        )

        actions = {action.action_id: action for action in overview.available_actions}
        self.assertEqual(
            actions["prod_rollback_plan"].route_path,
            "/v1/drivers/generic-web/prod-rollback-plan",
        )
        self.assertTrue(actions["prod_rollback_plan"].enabled)
        self.assertTrue(seen_contexts)
        self.assertEqual({context for _action, context in seen_contexts}, {"cm"})

    def test_inherited_rollback_plan_is_disabled_without_prod_lane(self) -> None:
        payload = _odoo_profile_payload()
        lanes = cast(tuple[dict[str, object], ...], payload["lanes"])
        payload["lanes"] = tuple(lane for lane in lanes if lane["instance"] != "prod")
        profile = LaunchplaneProductProfileRecord.model_validate(payload)

        overview = build_product_site_overview(
            record_store=_PreviewRecordStore(profile, ()),
            product=profile.product,
            action_allowed=lambda *_: True,
        )

        actions = {action.action_id: action for action in overview.available_actions}
        self.assertFalse(actions["prod_rollback_plan"].enabled)
        self.assertIn("prod lane", actions["prod_rollback_plan"].disabled_reasons[0])

    def test_product_site_overview_uses_testing_context_for_deploy_actions(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False)
        )

        def action_allowed(
            action: str,
            product: str,
            context: str,
            _instances: tuple[str, ...],
        ) -> bool:
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

    def test_product_site_overview_authorizes_generic_web_prod_workflow_with_testing_context(
        self,
    ) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False)
        )

        def action_allowed(
            action: str,
            product: str,
            context: str,
            _instances: tuple[str, ...],
        ) -> bool:
            return (
                action == "generic_web_prod_promotion.dispatch"
                and context == "example-site-testing"
            )

        overview = build_product_site_overview(
            record_store=_PreviewRecordStore(profile, ()),
            product=profile.product,
            action_allowed=action_allowed,
        )

        actions = {action.action_id: action for action in overview.available_actions}
        self.assertTrue(actions["prod_promotion_workflow"].enabled)

    def test_product_site_overview_does_not_authorize_generic_web_prod_workflow_with_prod_only_context(
        self,
    ) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False)
        )

        def action_allowed(
            action: str,
            product: str,
            context: str,
            _instances: tuple[str, ...],
        ) -> bool:
            return (
                action == "generic_web_prod_promotion.dispatch" and context == "example-site-prod"
            )

        overview = build_product_site_overview(
            record_store=_PreviewRecordStore(profile, ()),
            product=profile.product,
            action_allowed=action_allowed,
        )

        actions = {action.action_id: action for action in overview.available_actions}
        self.assertFalse(actions["prod_promotion_workflow"].enabled)

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

    def test_product_site_overview_uses_testing_only_authz_for_workflow_not_direct_prod(
        self,
    ) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False)
        )

        def action_allowed(
            action: str,
            product: str,
            context: str,
            _instances: tuple[str, ...],
        ) -> bool:
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
        self.assertTrue(actions["prod_promotion_workflow"].enabled)
        self.assertFalse(actions["prod_promotion"].enabled)

    def test_product_site_overview_hides_generic_web_prod_workflow_without_prod_lane(
        self,
    ) -> None:
        payload = _site_profile_payload(preview_enabled=False)
        lanes = cast("tuple[dict[str, object], ...]", payload["lanes"])
        payload["lanes"] = tuple(lane for lane in lanes if lane["instance"] != "prod")
        profile = LaunchplaneProductProfileRecord.model_validate(payload)

        overview = build_product_site_overview(
            record_store=_PreviewRecordStore(profile, ()),
            product=profile.product,
            action_allowed=lambda *_: True,
        )

        actions = {action.action_id: action for action in overview.available_actions}
        self.assertFalse(actions["prod_promotion_workflow"].enabled)
        self.assertFalse(actions["prod_promotion"].enabled)
        self.assertIn(
            "prod lane",
            actions["prod_promotion_workflow"].disabled_reasons[0],
        )

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

    def test_product_read_model_exposes_prelaunch_rebuild_policy(self) -> None:
        payload = _site_profile_payload(product="odoo-tenant-opw", preview_enabled=False)
        payload["driver_id"] = "odoo"
        profile = LaunchplaneProductProfileRecord.model_validate(payload)
        store = _PreviewRecordStore(profile, ())

        overview = build_product_site_overview(
            record_store=store,
            product="odoo-tenant-opw",
            action_allowed=lambda *_: False,
        )
        detail = build_product_environment_detail(
            record_store=store,
            product="odoo-tenant-opw",
            environment="prod",
            action_allowed=lambda *_: False,
        )

        prod_summary = {summary.environment: summary for summary in overview.environments}["prod"]
        prod_odoo = prod_summary.driver_extensions.odoo
        detail_odoo = detail.driver_extensions.odoo
        self.assertIsNotNone(prod_odoo)
        self.assertIsNotNone(detail_odoo)
        assert prod_odoo is not None
        assert detail_odoo is not None
        self.assertTrue(prod_odoo.prelaunch_rebuild_allowed)
        self.assertEqual(
            prod_odoo.prelaunch_rebuild_data_source_mode,
            "upstream_restore",
        )
        self.assertEqual(
            prod_odoo.prelaunch_rebuild_approval_issue_url,
            "https://github.com/cbusillo/launchplane/issues/573",
        )
        self.assertTrue(detail_odoo.prelaunch_rebuild_allowed)
        self.assertEqual(detail_odoo.prelaunch_rebuild_data_source_mode, "upstream_restore")
        self.assertEqual(prod_odoo.data_authority, "restorable")
        self.assertEqual(prod_odoo.allowed_rebuild_sources, ("upstream_restore",))
        self.assertEqual(prod_odoo.upstream_source, "example-site/prod/upstream")
        self.assertTrue(detail_odoo.requires_backup_before_destroy)
        self.assertTrue(detail_odoo.requires_restore_proof)
        self.assertTrue(detail_odoo.requires_runtime_identity)

    def test_product_read_model_exposes_public_ingress_observation(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False)
        )
        observation = PublicIngressObservationRecord(
            record_id="public-ingress-example-site-prod-20260529t120000z",
            product="example-site",
            context="example-site-prod",
            instance="prod",
            observed_at="2026-05-29T12:00:00Z",
            status="fail",
            failure_code="http_error",
            base_url="https://example-site.example",
            health_url="https://example-site.example/healthz",
            targets=(
                PublicIngressTargetObservation(
                    target="base_url",
                    url="https://example-site.example",
                    status="fail",
                    failure_code="http_error",
                    http_status=503,
                    summary="HTTP 503",
                ),
            ),
            notification_sent=True,
            summary="Public ingress failed for example-site/prod: HTTP 503",
        )
        incident = PublicIngressIncidentRecord(
            incident_id="public-ingress-incident-example-site-prod-20260529t120000z",
            product="example-site",
            context="example-site-prod",
            instance="prod",
            status="open",
            opened_at="2026-05-29T12:00:00Z",
            opened_observation_id=observation.record_id,
            latest_observation_id=observation.record_id,
            latest_observed_at=observation.observed_at,
            failure_code="http_error",
            summary="Public ingress failed for example-site/prod: HTTP 503",
        )
        store = _PublicIngressReadModelStore(profile, (observation,), (incident,))

        overview = build_product_site_overview(
            record_store=store,
            product="example-site",
            action_allowed=lambda *_: False,
        )
        detail = build_product_environment_detail(
            record_store=store,
            product="example-site",
            environment="prod",
            action_allowed=lambda *_: False,
        )

        prod_summary = {summary.environment: summary for summary in overview.environments}["prod"]
        self.assertEqual(prod_summary.public_ingress.status, "fail")
        self.assertEqual(prod_summary.public_ingress.trust_state, "verified")
        self.assertEqual(prod_summary.public_ingress.record_id, observation.record_id)
        self.assertEqual(prod_summary.public_ingress.incident_status, "open")
        self.assertEqual(prod_summary.public_ingress.incident_id, incident.incident_id)
        self.assertTrue(detail.public_ingress.notification_sent)

    def test_product_environment_detail_fails_without_public_ingress_incident_support(
        self,
    ) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False)
        )
        observation = PublicIngressObservationRecord(
            record_id="public-ingress-example-site-prod-20260529t120000z",
            product="example-site",
            context="example-site-prod",
            instance="prod",
            observed_at="2026-05-29T12:00:00Z",
            status="fail",
            failure_code="http_error",
            base_url="https://example-site.example",
            health_url="https://example-site.example/healthz",
            targets=(
                PublicIngressTargetObservation(
                    target="base_url",
                    url="https://example-site.example",
                    status="fail",
                    failure_code="http_error",
                    http_status=503,
                    summary="HTTP 503",
                ),
            ),
            notification_sent=True,
            summary="Public ingress failed for example-site/prod: HTTP 503",
        )
        store = _PublicIngressObservationsOnlyStore(profile, (observation,))

        with self.assertRaisesRegex(
            ProductEnvironmentReadModelCapabilityError,
            r"missing store method\(s\): list_public_ingress_incident_records",
        ):
            build_product_environment_detail(
                record_store=store,
                product="example-site",
                environment="prod",
                action_allowed=lambda *_: False,
            )

    def test_product_environment_detail_exposes_runtime_identity_evidence(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False)
        )
        expected = RuntimeIdentity(
            product="example-site",
            context="example-site-prod",
            instance="prod",
            deployment_record_id="deployment-prod-1",
            artifact_id="ghcr.io/every/example-site@sha256:abc123",
            source_git_ref="abc123",
        )
        inventory = EnvironmentInventory(
            context="example-site-prod",
            instance="prod",
            artifact_identity=ArtifactIdentityReference(artifact_id=expected.artifact_id),
            source_git_ref="abc123",
            deploy=DeploymentEvidence(
                target_name="example-site-prod",
                target_type="application",
                deploy_mode="dokploy-application-api",
                deployment_id="control-plane-dokploy",
                status="pass",
                started_at="2026-05-02T10:00:00Z",
                finished_at="2026-05-02T10:01:00Z",
            ),
            runtime_identity=expected,
            destination_health=HealthcheckEvidence(
                verified=True,
                urls=("https://example-site.example/healthz",),
                timeout_seconds=30,
                status="pass",
                runtime_identity_status="match",
                runtime_identity_detail="Runtime identity matches the expected deployment record.",
                observed_runtime_identity=expected,
            ),
            updated_at="2026-05-02T10:01:00Z",
            deployment_record_id="deployment-prod-1",
        )
        store = _RuntimeIdentityReadModelStore(profile, inventory)

        detail = build_product_environment_detail(
            record_store=store,
            product="example-site",
            environment="prod",
            action_allowed=lambda *_: False,
        )

        self.assertEqual(detail.target.expected_runtime_identity, expected)
        self.assertEqual(detail.target.observed_runtime_identity, expected)
        self.assertEqual(detail.target.runtime_identity_status, "match")

    def test_driver_lane_summary_exposes_artifact_build_provenance(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False)
        )
        inventory = EnvironmentInventory(
            context="example-site-prod",
            instance="prod",
            artifact_identity=ArtifactIdentityReference(
                artifact_id="ghcr.io/every/example-site@sha256:abc123"
            ),
            source_git_ref="abc123",
            deploy=DeploymentEvidence(
                target_name="example-site-prod",
                target_type="application",
                deploy_mode="dokploy-application-api",
                deployment_id="control-plane-dokploy",
                status="pass",
                started_at="2026-05-02T10:00:00Z",
                finished_at="2026-05-02T10:01:00Z",
            ),
            updated_at="2026-05-02T10:01:00Z",
            deployment_record_id="deployment-prod-1",
        )
        store = _RuntimeIdentityReadModelStore(profile, inventory)

        detail = build_product_environment_detail(
            record_store=store,
            product="example-site",
            environment="prod",
            action_allowed=lambda *_: False,
        )

        artifact_manifest = detail.target.artifact_manifest
        assert artifact_manifest is not None
        self.assertEqual(
            artifact_manifest.build_provenance.base_images[0].source_repository,
            "cbusillo/odoo-docker",
        )
        self.assertEqual(
            artifact_manifest.build_provenance.build_tools[0].name,
            "odoo-devkit",
        )
        dependency_provenance = artifact_manifest.dependency_provenance
        assert dependency_provenance is not None
        self.assertEqual(
            dependency_provenance.python_environments["linux/amd64"].python_version,
            "3.13.5",
        )
        self.assertEqual(dependency_provenance.uv_locks[1].scope, "tenant")
        self.assertIn("odoo-devkit", detail.model_dump_json())
        self.assertIn("simple-zpl2", detail.model_dump_json())

    def test_product_environment_detail_exposes_physical_provider_target(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path}"
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            profile = LaunchplaneProductProfileRecord.model_validate(
                _site_profile_payload(preview_enabled=False, preview_context="")
            )
            store.write_product_profile_record(profile)
            store.write_dokploy_target_record(
                DokployTargetRecord(
                    context="example-site-prod",
                    instance="prod",
                    project_name="example-site-prod-project",
                    target_type="application",
                    target_name="example-site-prod",
                    updated_at="2026-05-02T22:30:00Z",
                    source_label="test",
                )
            )
            store.write_dokploy_target_id_record(
                DokployTargetIdRecord(
                    context="example-site-prod",
                    instance="prod",
                    target_id="app-example-prod",
                    updated_at="2026-05-02T22:31:00Z",
                    source_label="test",
                )
            )
            store.write_provider_target_record(
                ProviderTargetRecord(
                    context="example-site-prod",
                    instance="prod",
                    provider_id="dokploy",
                    target_category="application",
                    target_id="app-example-prod",
                    display_name="example-site-prod",
                    provider_target_type="application",
                    provider_evidence={"project_name": "example-site-prod-project"},
                    updated_at="2026-05-02T22:32:00Z",
                    source_label="test",
                )
            )
            store.write_route_binding_record(
                EnvironmentRouteBindingRecord(
                    product=profile.product,
                    context="example-site-prod",
                    instance="prod",
                    provider_target=RouteBindingProviderTarget(
                        provider_id="dokploy",
                        target_category="application",
                        provider_target_type="application",
                        target_name="example-site-prod",
                        provider_evidence={"host_id": "provider-host-private"},
                    ),
                    ingress=RouteBindingIngress(
                        provider="dokploy",
                        termination_kind="direct",
                    ),
                    domains=(
                        RouteBindingDomain(
                            domain_name="example-site.example",
                            role="primary",
                        ),
                    ),
                    tls=RouteBindingTls(owner="provider"),
                    source=RouteBindingSource(
                        source_kind="service",
                        source_label="test",
                        refreshed_at="2026-05-02T22:32:00Z",
                        freshness_status="recorded",
                    ),
                    updated_at="2026-05-02T22:32:00Z",
                )
            )

            detail = build_product_environment_detail(
                record_store=store,
                product=profile.product,
                environment="prod",
                action_allowed=lambda *_: False,
            )
            store.close()

        self.assertEqual(detail.target.provider, "dokploy")
        self.assertEqual(detail.target.target_type, "application")
        self.assertEqual(detail.target.target_name, "example-site-prod")
        self.assertEqual(detail.target.provider_target_type, "application")
        self.assertTrue(detail.target.target_id_recorded)
        self.assertEqual(detail.target.trust_state, "recorded")
        self.assertNotIn("app-example-prod", detail.model_dump_json())
        self.assertNotIn("provider-host-private", detail.model_dump_json())

    def test_product_environment_detail_does_not_project_provider_target_from_dokploy_pair(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path}"
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            profile = LaunchplaneProductProfileRecord.model_validate(
                _site_profile_payload(preview_enabled=False, preview_context="")
            )
            store.write_product_profile_record(profile)
            store.write_dokploy_target_record(
                DokployTargetRecord(
                    context="example-site-prod",
                    instance="prod",
                    project_name="example-site-prod-project",
                    target_type="application",
                    target_name="example-site-prod",
                    updated_at="2026-05-02T22:30:00Z",
                    source_label="test",
                )
            )
            store.write_dokploy_target_id_record(
                DokployTargetIdRecord(
                    context="example-site-prod",
                    instance="prod",
                    target_id="app-example-prod",
                    updated_at="2026-05-02T22:31:00Z",
                    source_label="test",
                )
            )

            detail = build_product_environment_detail(
                record_store=store,
                product=profile.product,
                environment="prod",
                action_allowed=lambda *_: False,
            )
            store.close()

        self.assertEqual(detail.target.provider, "")
        self.assertEqual(detail.target.target_type, "")
        self.assertEqual(detail.target.target_name, "")
        self.assertEqual(detail.target.provider_target_type, "")
        self.assertFalse(detail.target.target_id_recorded)
        self.assertEqual(detail.target.trust_state, "missing")

    def test_product_environment_config_status_reports_expected_key_states(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path}"
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            profile = LaunchplaneProductProfileRecord.model_validate(
                _site_profile_payload(preview_enabled=False, preview_context="")
            )
            store.write_product_profile_record(profile)
            store.write_runtime_environment_record(
                RuntimeEnvironmentRecord(
                    scope="instance",
                    context="example-site-prod",
                    instance="prod",
                    env={"RESEND_FROM_EMAIL": "noreply@example.invalid"},
                    updated_at="2026-05-02T22:32:00Z",
                    source_label="test",
                )
            )
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

            config_status = build_product_environment_config_status(
                record_store=store,
                product=profile.product,
                environment="prod",
            )

        runtime_statuses = {item.key: item.status for item in config_status.runtime_settings}
        secret_statuses = {item.binding_key: item.status for item in config_status.managed_secrets}
        self.assertEqual(runtime_statuses, {"RESEND_FROM_EMAIL": "configured"})
        self.assertEqual(
            secret_statuses,
            {"SMTP_PASSWORD": "disabled", "RESEND_API_KEY": "missing"},
        )
        response_text = config_status.model_dump_json()
        self.assertNotIn("noreply@example.invalid", response_text)

    def test_product_activity_read_model_aggregates_product_records(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(
                preview_context="shared-preview",
                testing_context="example-site-prod",
                prod_context="example-site-prod",
            )
        )
        store = _ActivityRecordStore(
            profile,
            (
                _preview_record(
                    preview_id="example-preview-1",
                    context="shared-preview",
                    anchor_repo="example-site",
                    state="active",
                    updated_at="2026-05-02T12:10:00Z",
                ),
            ),
        )

        activity = build_product_activity_read_model(
            record_store=store,
            product="example-site",
        )

        event_types = {event.event_type for event in activity.events}
        self.assertIn("deployment", event_types)
        self.assertIn("promotion", event_types)
        self.assertIn("backup_gate", event_types)
        self.assertIn("preview", event_types)
        self.assertIn("preview_desired_state", event_types)
        self.assertIn("authz_policy", event_types)
        self.assertNotIn(
            "desired-other-1",
            {link.record_id for event in activity.events for link in event.records},
        )
        self.assertEqual(activity.events[0].event_type, "authz_policy")

    def test_product_activity_authz_grant_uses_managed_mutation_delta(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False, preview_context="")
        )
        previous_record = _authz_record(
            record_id="authz-policy-grant-1",
            revision=1,
            status="superseded",
            updated_at="2026-05-02T12:59:00Z",
            policy=_authz_policy(),
        )
        current_record = _authz_record(
            record_id="authz-policy-grant-2",
            revision=2,
            status="active",
            updated_at="2026-05-02T13:00:00Z",
            policy=_authz_policy(
                _managed_github_rule(
                    managed_rule_id="example-site.read",
                    products=("example-site",),
                )
            ),
            audit=_managed_authz_audit(
                previous_record_id=previous_record.record_id,
                changes=(
                    {
                        "managed_rule_id": "example-site.read",
                        "change": "added",
                        "previous_principal_type": None,
                        "desired_principal_type": "github_actions",
                    },
                ),
            ),
            source="managed-authz",
        )
        store = _ActivityRecordStore(
            profile,
            (),
            authz_records=(current_record, previous_record),
        )

        activity = build_product_activity_read_model(
            record_store=store,
            product=profile.product,
        )

        authz_events = tuple(
            event for event in activity.events if event.event_type == "authz_policy"
        )
        self.assertEqual(len(authz_events), 1)
        event = authz_events[0]
        self.assertEqual(event.action_id, "authz_policy.grant")
        self.assertEqual(event.title, "Example Site authorization granted")
        self.assertIn("managed-authz", event.summary)
        self.assertIn("example-site.read", event.summary)

    def test_product_activity_authz_removal_uses_previous_policy_rule(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False, preview_context="")
        )
        previous_record = _authz_record(
            record_id="authz-policy-remove-1",
            revision=1,
            status="superseded",
            updated_at="2026-05-02T12:59:00Z",
            policy=_authz_policy(
                _managed_github_rule(
                    managed_rule_id="example-site.read",
                    products=("example-site",),
                )
            ),
        )
        current_record = _authz_record(
            record_id="authz-policy-remove-2",
            revision=2,
            status="active",
            updated_at="2026-05-02T13:00:00Z",
            policy=_authz_policy(),
            audit=_managed_authz_audit(
                previous_record_id=previous_record.record_id,
                changes=(
                    {
                        "managed_rule_id": "example-site.read",
                        "change": "removed",
                        "previous_principal_type": "github_actions",
                        "desired_principal_type": None,
                    },
                ),
            ),
            source="managed-authz",
        )
        store = _ActivityRecordStore(
            profile,
            (),
            authz_records=(current_record, previous_record),
        )

        activity = build_product_activity_read_model(
            record_store=store,
            product=profile.product,
        )

        authz_events = tuple(
            event for event in activity.events if event.event_type == "authz_policy"
        )
        self.assertEqual(len(authz_events), 1)
        event = authz_events[0]
        self.assertEqual(event.action_id, "authz_policy.remove")
        self.assertEqual(event.title, "Example Site authorization removed")
        self.assertIn("managed-authz", event.summary)
        self.assertIn("example-site.read", event.summary)

    def test_product_activity_authz_resolves_rule_id_within_managed_set(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False, preview_context="")
        )
        previous_record = _authz_record(
            record_id="authz-policy-managed-set-1",
            revision=1,
            status="superseded",
            updated_at="2026-05-02T12:59:00Z",
            policy=_authz_policy(
                _managed_github_rule(
                    managed_set_id="target.manager",
                    managed_rule_id="shared.read",
                    products=("example-site",),
                ),
                _managed_github_rule(
                    managed_set_id="other.manager",
                    managed_rule_id="shared.read",
                    products=("other-site",),
                ),
            ),
        )
        current_record = _authz_record(
            record_id="authz-policy-managed-set-2",
            revision=2,
            status="active",
            updated_at="2026-05-02T13:00:00Z",
            policy=_authz_policy(
                _managed_github_rule(
                    managed_set_id="other.manager",
                    managed_rule_id="shared.read",
                    products=("other-site",),
                )
            ),
            audit=_managed_authz_audit(
                managed_set_id="target.manager",
                previous_record_id=previous_record.record_id,
                changes=(
                    {
                        "managed_rule_id": "shared.read",
                        "change": "removed",
                        "previous_principal_type": "github_actions",
                        "desired_principal_type": None,
                    },
                ),
            ),
            source="managed-authz",
        )

        activity = build_product_activity_read_model(
            record_store=_ActivityRecordStore(
                profile,
                (),
                authz_records=(current_record, previous_record),
            ),
            product=profile.product,
        )

        authz_events = tuple(
            event for event in activity.events if event.event_type == "authz_policy"
        )
        self.assertEqual(len(authz_events), 1)
        self.assertEqual(authz_events[0].action_id, "authz_policy.remove")

    def test_product_activity_authz_matches_products_by_principal_semantics(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False, preview_context="")
        )
        previous_record = _authz_record(
            record_id="authz-policy-principals-1",
            revision=1,
            status="superseded",
            updated_at="2026-05-02T12:59:00Z",
            policy=_authz_policy(),
        )
        current_policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "github_actions": [
                    {
                        **_managed_github_rule(
                            managed_set_id="test.principals",
                            managed_rule_id="actions.wildcard",
                            products=("example-*",),
                        )
                    }
                ],
                "github_humans": [
                    {
                        "managed_set_id": "test.principals",
                        "managed_rule_id": "human.exact",
                        "logins": ["operator"],
                        "products": ["example-site"],
                        "actions": ["product_profile.read"],
                    },
                    {
                        "managed_set_id": "test.principals",
                        "managed_rule_id": "human.wildcard",
                        "logins": ["operator"],
                        "products": ["example-*"],
                        "actions": ["product_profile.read"],
                    },
                    {
                        "managed_set_id": "test.principals",
                        "managed_rule_id": "human.global",
                        "logins": ["operator"],
                        "actions": ["product_profile.read"],
                    },
                ],
                "terminal_agents": [
                    {
                        "managed_set_id": "test.principals",
                        "managed_rule_id": "agent.exact",
                        "subjects": ["agent:test"],
                        "products": ["example-site"],
                        "actions": ["product_profile.read"],
                    },
                    {
                        "managed_set_id": "test.principals",
                        "managed_rule_id": "agent.wildcard",
                        "subjects": ["agent:test"],
                        "products": ["example-*"],
                        "actions": ["product_profile.read"],
                    },
                    {
                        "managed_set_id": "test.principals",
                        "managed_rule_id": "agent.global",
                        "subjects": ["agent:test"],
                        "actions": ["product_profile.read"],
                    },
                ],
                "local_operators": [
                    {
                        "managed_set_id": "test.principals",
                        "managed_rule_id": "operator.wildcard",
                        "subjects": ["operator:test"],
                        "products": ["example-*"],
                        "actions": ["product_profile.read"],
                    }
                ],
                "local_admins": [
                    {
                        "managed_set_id": "test.principals",
                        "managed_rule_id": "admin.wildcard",
                        "subjects": ["admin:test"],
                        "products": ["*"],
                        "actions": ["product_profile.read"],
                    }
                ],
            }
        )
        changes: list[dict[str, object]] = []
        for principal_type, managed_rule_id in (
            ("github_actions", "actions.wildcard"),
            ("github_humans", "human.exact"),
            ("github_humans", "human.wildcard"),
            ("github_humans", "human.global"),
            ("terminal_agents", "agent.exact"),
            ("terminal_agents", "agent.wildcard"),
            ("terminal_agents", "agent.global"),
            ("local_operators", "operator.wildcard"),
            ("local_admins", "admin.wildcard"),
        ):
            changes.append(
                {
                    "managed_rule_id": managed_rule_id,
                    "change": "added",
                    "previous_principal_type": None,
                    "desired_principal_type": principal_type,
                }
            )
        current_record = _authz_record(
            record_id="authz-policy-principals-2",
            revision=2,
            status="active",
            updated_at="2026-05-02T13:00:00Z",
            policy=current_policy,
            audit=_managed_authz_audit(
                managed_set_id="test.principals",
                previous_record_id=previous_record.record_id,
                changes=tuple(changes),
            ),
            source="managed-authz",
        )

        activity = build_product_activity_read_model(
            record_store=_ActivityRecordStore(
                profile,
                (),
                authz_records=(current_record, previous_record),
            ),
            product=profile.product,
        )

        authz_events = tuple(
            event for event in activity.events if event.event_type == "authz_policy"
        )
        self.assertEqual(len(authz_events), 1)
        summary = authz_events[0].summary
        for managed_rule_id in (
            "actions.wildcard",
            "human.exact",
            "human.global",
            "agent.exact",
            "agent.global",
            "operator.wildcard",
            "admin.wildcard",
        ):
            self.assertIn(managed_rule_id, summary)
        self.assertNotIn("human.wildcard", summary)
        self.assertNotIn("agent.wildcard", summary)

    def test_product_activity_authz_does_not_guess_missing_previous_record(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False, preview_context="")
        )
        adjacent_record = _authz_record(
            record_id="authz-policy-adjacent",
            revision=1,
            status="superseded",
            updated_at="2026-05-02T12:59:00Z",
            policy=_authz_policy(
                _managed_github_rule(
                    managed_rule_id="example-site.read",
                    products=("example-site",),
                )
            ),
        )
        current_record = _authz_record(
            record_id="authz-policy-missing-previous",
            revision=3,
            status="active",
            updated_at="2026-05-02T13:00:00Z",
            policy=_authz_policy(),
            audit=_managed_authz_audit(
                previous_record_id="authz-policy-not-returned",
                changes=(
                    {
                        "managed_rule_id": "example-site.read",
                        "change": "removed",
                        "previous_principal_type": "github_actions",
                        "desired_principal_type": None,
                    },
                ),
            ),
            source="managed-authz",
        )

        activity = build_product_activity_read_model(
            record_store=_ActivityRecordStore(
                profile,
                (),
                authz_records=(current_record, adjacent_record),
            ),
            product=profile.product,
        )

        self.assertFalse(any(event.event_type == "authz_policy" for event in activity.events))

    def test_product_activity_authz_requires_managed_set_id(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False, preview_context="")
        )
        previous_record = _authz_record(
            record_id="authz-policy-missing-set-1",
            revision=1,
            status="superseded",
            updated_at="2026-05-02T12:59:00Z",
            policy=_authz_policy(),
        )
        current_record = _authz_record(
            record_id="authz-policy-missing-set-2",
            revision=2,
            status="active",
            updated_at="2026-05-02T13:00:00Z",
            policy=_authz_policy(
                _managed_github_rule(
                    managed_rule_id="example-site.read",
                    products=("example-site",),
                )
            ),
            audit={
                "operation": "managed_rule_set_reconcile",
                "previous_policy_record_id": previous_record.record_id,
                "diff": {
                    "previous_record_id": previous_record.record_id,
                    "changes": [
                        {
                            "managed_rule_id": "example-site.read",
                            "change": "added",
                            "previous_principal_type": None,
                            "desired_principal_type": "github_actions",
                        }
                    ],
                },
            },
            source="managed-authz",
        )

        activity = build_product_activity_read_model(
            record_store=_ActivityRecordStore(
                profile,
                (),
                authz_records=(current_record, previous_record),
            ),
            product=profile.product,
        )

        self.assertFalse(any(event.event_type == "authz_policy" for event in activity.events))

    def test_product_activity_authz_update_requires_named_previous_record(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False, preview_context="")
        )
        adjacent_record = _authz_record(
            record_id="authz-policy-update-adjacent",
            revision=1,
            status="superseded",
            updated_at="2026-05-02T12:59:00Z",
            policy=_authz_policy(),
        )
        current_record = _authz_record(
            record_id="authz-policy-update-missing-previous",
            revision=3,
            status="active",
            updated_at="2026-05-02T13:00:00Z",
            policy=_authz_policy(
                _managed_github_rule(
                    managed_rule_id="example-site.read",
                    products=("example-site",),
                    actions=("product_profile.read", "product_environment.read"),
                )
            ),
            audit=_managed_authz_audit(
                previous_record_id="authz-policy-update-not-returned",
                changes=(
                    {
                        "managed_rule_id": "example-site.read",
                        "change": "updated",
                        "previous_principal_type": "github_actions",
                        "desired_principal_type": "github_actions",
                    },
                ),
            ),
            source="managed-authz",
        )

        activity = build_product_activity_read_model(
            record_store=_ActivityRecordStore(
                profile,
                (),
                authz_records=(current_record, adjacent_record),
            ),
            product=profile.product,
        )

        self.assertFalse(any(event.event_type == "authz_policy" for event in activity.events))

    def test_product_activity_excludes_unrelated_multi_product_rule_update(self) -> None:
        previous_record = _authz_record(
            record_id="authz-policy-multi-1",
            revision=1,
            status="superseded",
            updated_at="2026-05-02T12:59:00Z",
            policy=_authz_policy(
                _managed_github_rule(
                    managed_rule_id="shared.read",
                    products=("example-site",),
                )
            ),
        )
        current_record = _authz_record(
            record_id="authz-policy-multi-2",
            revision=2,
            status="active",
            updated_at="2026-05-02T13:00:00Z",
            policy=_authz_policy(
                _managed_github_rule(
                    managed_rule_id="shared.read",
                    products=("example-site", "other-site"),
                )
            ),
            audit=_managed_authz_audit(
                previous_record_id=previous_record.record_id,
                changes=(
                    {
                        "managed_rule_id": "shared.read",
                        "change": "updated",
                        "previous_principal_type": "github_actions",
                        "desired_principal_type": "github_actions",
                    },
                ),
            ),
            source="managed-authz",
        )
        records = (current_record, previous_record)
        example_profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False, preview_context="")
        )
        other_profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(
                product="other-site",
                preview_enabled=False,
                preview_context="",
                testing_context="other-site-prod",
                prod_context="other-site-prod",
            )
        )

        example_activity = build_product_activity_read_model(
            record_store=_ActivityRecordStore(
                example_profile,
                (),
                authz_records=records,
            ),
            product=example_profile.product,
        )
        other_activity = build_product_activity_read_model(
            record_store=_ActivityRecordStore(
                other_profile,
                (),
                authz_records=records,
            ),
            product=other_profile.product,
        )

        self.assertFalse(
            any(event.event_type == "authz_policy" for event in example_activity.events)
        )
        other_authz_events = tuple(
            event for event in other_activity.events if event.event_type == "authz_policy"
        )
        self.assertEqual(len(other_authz_events), 1)
        self.assertEqual(other_authz_events[0].action_id, "authz_policy.grant")

    def test_product_activity_legacy_authz_uses_adjacent_snapshot_comparison(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(preview_enabled=False, preview_context="")
        )
        first_record = _authz_record(
            record_id="authz-policy-legacy-1",
            revision=1,
            status="superseded",
            updated_at="2026-05-02T12:58:00Z",
            policy=_authz_policy(
                _managed_github_rule(
                    managed_rule_id="shared.read",
                    products=("example-site",),
                )
            ),
        )
        unrelated_record = _authz_record(
            record_id="authz-policy-legacy-2",
            revision=2,
            status="superseded",
            updated_at="2026-05-02T12:59:00Z",
            policy=_authz_policy(
                _managed_github_rule(
                    managed_rule_id="shared.read",
                    products=("example-site", "other-site"),
                )
            ),
            source="legacy-unrelated",
        )
        changed_record = _authz_record(
            record_id="authz-policy-legacy-3",
            revision=3,
            status="active",
            updated_at="2026-05-02T13:00:00Z",
            policy=_authz_policy(
                _managed_github_rule(
                    managed_rule_id="shared.read",
                    products=("example-site", "other-site"),
                    actions=("product_profile.read", "product_environment.read"),
                )
            ),
            source="legacy-product-change",
        )
        store = _ActivityRecordStore(
            profile,
            (),
            authz_records=(changed_record, unrelated_record, first_record),
        )

        activity = build_product_activity_read_model(
            record_store=store,
            product=profile.product,
        )

        authz_events = tuple(
            event for event in activity.events if event.event_type == "authz_policy"
        )
        self.assertEqual(len(authz_events), 1)
        event = authz_events[0]
        self.assertEqual(event.action_id, "authz_policy.legacy_change")
        self.assertEqual(event.title, "Example Site authorization changed (legacy record)")
        self.assertIn("legacy-product-change", event.summary)
        self.assertNotIn("legacy-unrelated", event.summary)

    def test_product_activity_read_model_keeps_preview_history_when_previews_disabled(
        self,
    ) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(
                preview_enabled=False,
                preview_context="shared-preview",
                testing_context="example-site-prod",
                prod_context="example-site-prod",
            )
        )
        store = _ActivityRecordStore(
            profile,
            (
                _preview_record(
                    preview_id="example-preview-1",
                    context="shared-preview",
                    anchor_repo="example-site",
                    state="destroyed",
                    updated_at="2026-05-02T12:10:00Z",
                ),
            ),
        )

        activity = build_product_activity_read_model(
            record_store=store,
            product="example-site",
        )

        event_types = {event.event_type for event in activity.events}
        self.assertIn("preview", event_types)
        self.assertIn("preview_desired_state", event_types)

    def test_product_activity_read_model_reads_historical_contexts_after_cutover(
        self,
    ) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            {
                **_site_profile_payload(
                    preview_enabled=False,
                    preview_context="",
                    testing_context="example-site-prod",
                    prod_context="example-site-prod",
                ),
                "historical_contexts": ("example-site-legacy",),
            }
        )
        store = _HistoricalActivityRecordStore(profile, ())

        activity = build_product_activity_read_model(
            record_store=store,
            product="example-site",
            limit=10,
        )

        deployment_contexts = {
            event.context for event in activity.events if event.event_type == "deployment"
        }
        self.assertIn("example-site-prod", deployment_contexts)
        self.assertIn("example-site-legacy", deployment_contexts)
        self.assertIn(("example-site-legacy", "prod", 10), store.deployment_calls)

    def test_product_activity_read_model_limits_after_merging_all_sources(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _site_profile_payload(
                preview_enabled=False,
                preview_context="",
                testing_context="example-site-prod",
                prod_context="example-site-prod",
            )
        )
        store = _ManyDeploymentActivityRecordStore(profile, ())

        activity = build_product_activity_read_model(
            record_store=store,
            product="example-site",
            limit=12,
        )

        deployment_record_ids = {
            link.record_id
            for event in activity.events
            for link in event.records
            if link.record_type == "deployment"
        }
        self.assertEqual(len(activity.events), 12)
        self.assertIn("deployment-prod-1", deployment_record_ids)
        self.assertIn(12, store.deployment_limits)
