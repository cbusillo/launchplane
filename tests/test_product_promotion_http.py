import asyncio
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import cast
from unittest.mock import Mock, patch

from pydantic import ValidationError

from control_plane.contracts.deploy_target import DeployedTargetReference, ProviderTargetRecord
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.outbox_delivery import OutboxDeliveryRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
)
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    HealthcheckEvidence,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.ship_request import ShipRequest
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy
from control_plane.product_promotion_http import (
    ProductPromotionDryRunEnvelope,
    ProductPromotionStatus,
    ProductPromotionWorkflowDispatchEnvelope,
    build_product_promotion_status,
    product_promotion_confirmation,
    product_promotion_delivery_status,
    product_promotion_intent_matches,
    product_promotion_request_payload,
    product_promotion_workflow_request,
)
from control_plane.generic_web_promotion_http import (
    build_generic_web_promotion_workflow_outbox_delivery,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import OutboxWithIdempotencyRequest, PostgresRecordStore
from control_plane.workflows.generic_web_promotion import (
    GenericWebProdPromotionRequest,
    GenericWebProdPromotionResult,
)
from control_plane.workflows.generic_web_deploy_provider import GenericWebResolvedDeployTarget
from tests.http_app_test_support import (
    _asgi_get,
    _asgi_request,
    _browser_mutation_headers,
    _github_human_identity,
    _github_human_product_config_policy,
    _github_oauth_config,
    _RejectingVerifier,
)
from tests.support.stores import _sqlite_database_url


NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
TESTING_ARTIFACT = f"ghcr.io/example/atlas-commerce@sha256:{'a' * 64}"
PROD_ARTIFACT = f"ghcr.io/example/atlas-commerce@sha256:{'b' * 64}"
NEW_TESTING_ARTIFACT = f"ghcr.io/example/atlas-commerce@sha256:{'c' * 64}"
TESTING_SOURCE_REF = "1" * 40
PROD_SOURCE_REF = "2" * 40
NEW_TESTING_SOURCE_REF = "3" * 40


class _PromotionStore(PostgresRecordStore):
    def __init__(
        self,
        *,
        profile: LaunchplaneProductProfileRecord,
        summaries: dict[tuple[str, str], LaunchplaneLaneSummary],
    ) -> None:
        self.profile = profile
        self.summaries = summaries

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != self.profile.product:
            raise FileNotFoundError(product)
        return self.profile

    def read_lane_summary(
        self,
        *,
        context_name: str,
        instance_name: str,
    ) -> LaunchplaneLaneSummary:
        try:
            return self.summaries[(context_name, instance_name)]
        except KeyError as error:
            raise FileNotFoundError(instance_name) from error


class _AcceptingVerifier:
    def __init__(self, identity: GitHubActionsIdentity) -> None:
        self.identity = identity

    def verify(self, token: str) -> GitHubActionsIdentity:
        del token
        return self.identity


def _profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="atlas-commerce",
        display_name="Atlas Commerce",
        repository="example/atlas-commerce",
        driver_id="generic-web",
        image=ProductImageProfile(repository="ghcr.io/example/atlas-commerce"),
        runtime_port=3000,
        health_path="/health",
        lanes=(
            ProductLaneProfile(
                instance="testing",
                context="atlas-commerce",
                base_url="https://testing.example.test",
                health_url="https://testing.example.test/health",
            ),
            ProductLaneProfile(
                instance="prod",
                context="atlas-commerce",
                base_url="https://example.test",
                health_url="https://example.test/health",
            ),
        ),
        updated_at="2026-07-15T08:30:00Z",
        source="test",
    )


def _inventory(
    *,
    instance: str,
    artifact_id: str,
    source_git_ref: str,
    updated_at: str = "2026-07-15T08:45:00Z",
) -> EnvironmentInventory:
    deployment_record_id = f"deployment-{instance}"
    runtime_identity = RuntimeIdentity(
        product="atlas-commerce",
        context="atlas-commerce",
        instance=instance,
        deployment_record_id=deployment_record_id,
        artifact_id=artifact_id,
        source_git_ref=source_git_ref,
    )
    return EnvironmentInventory(
        context="atlas-commerce",
        instance=instance,
        artifact_identity=ArtifactIdentityReference(artifact_id=artifact_id),
        source_git_ref=source_git_ref,
        deploy=DeploymentEvidence(
            target_name=f"atlas-{instance}",
            target_type="application",
            deploy_mode="dokploy-application-api",
            status="pass",
        ),
        runtime_identity=runtime_identity,
        destination_health=HealthcheckEvidence(
            verified=True,
            urls=(f"https://{instance}.example.test/health",),
            timeout_seconds=5,
            status="pass",
            runtime_identity_status="match",
            runtime_identity_detail="Runtime identity matches.",
            observed_runtime_identity=runtime_identity,
        ),
        updated_at=updated_at,
        deployment_record_id=deployment_record_id,
    )


def _store(
    *,
    testing_inventory: EnvironmentInventory | None = None,
) -> _PromotionStore:
    profile = _profile()
    testing = testing_inventory or _inventory(
        instance="testing",
        artifact_id=TESTING_ARTIFACT,
        source_git_ref=TESTING_SOURCE_REF,
    )
    prod = _inventory(
        instance="prod",
        artifact_id=PROD_ARTIFACT,
        source_git_ref=PROD_SOURCE_REF,
    )
    summaries = {
        ("atlas-commerce", "testing"): LaunchplaneLaneSummary(
            context="atlas-commerce",
            instance="testing",
            inventory=testing,
        ),
        ("atlas-commerce", "prod"): LaunchplaneLaneSummary(
            context="atlas-commerce",
            instance="prod",
            inventory=prod,
            provider_target=ProviderTargetRecord(
                context="atlas-commerce",
                instance="prod",
                provider_id="dokploy",
                target_category="application",
                target_id="app-prod",
                display_name="Atlas Commerce prod",
                updated_at="2026-07-15T08:45:00Z",
            ),
        ),
    }
    return _PromotionStore(profile=profile, summaries=summaries)


def _status(
    store: _PromotionStore,
) -> tuple[LaunchplaneProductProfileRecord, ProductLaneProfile, ProductPromotionStatus]:
    return build_product_promotion_status(
        record_store=store,
        product="atlas-commerce",
        destination_environment="prod",
        action_allowed=lambda _action, _product, _context: True,
        workflow_credentials_ready=lambda _context: True,
        now=NOW,
    )


class ProductPromotionStatusTests(unittest.TestCase):
    def test_status_derives_reviewed_identity_from_runtime_evidence(self) -> None:
        profile, lane, status = _status(_store())

        self.assertEqual(profile.product, "atlas-commerce")
        self.assertEqual(lane.instance, "prod")
        self.assertEqual(
            status.source.artifact_id,
            TESTING_ARTIFACT,
        )
        self.assertEqual(status.source.source_git_ref, TESTING_SOURCE_REF)
        self.assertEqual(status.source.runtime_identity_status, "match")
        self.assertEqual(status.trust_state, "verified")
        self.assertTrue(status.direct_dry_run.enabled)
        self.assertTrue(status.workflow_dry_run.enabled)
        self.assertTrue(status.workflow_live.enabled)
        self.assertEqual(
            status.live_confirmations.minor,
            (
                f"PROMOTE atlas-commerce {TESTING_ARTIFACT} {TESTING_SOURCE_REF} "
                "TO prod BUMP minor CREATE RELEASE TAG AND DEPLOY PRODUCTION"
            ),
        )

    def test_status_blocks_missing_generated_runtime_identity(self) -> None:
        inventory = _inventory(
            instance="testing",
            artifact_id=TESTING_ARTIFACT,
            source_git_ref=TESTING_SOURCE_REF,
        ).model_copy(update={"runtime_identity": None})

        _, _, status = _status(_store(testing_inventory=inventory))

        self.assertFalse(status.direct_dry_run.enabled)
        self.assertIn(
            "Testing inventory does not contain generated runtime identity evidence.",
            status.direct_dry_run.disabled_reasons,
        )
        self.assertEqual(status.source.artifact_id, "")
        self.assertEqual(status.trust_state, "missing")

    def test_status_blocks_stale_runtime_evidence(self) -> None:
        inventory = _inventory(
            instance="testing",
            artifact_id=TESTING_ARTIFACT,
            source_git_ref=TESTING_SOURCE_REF,
            updated_at=(NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        )

        _, _, status = _status(_store(testing_inventory=inventory))

        self.assertFalse(status.direct_dry_run.enabled)
        self.assertIn(
            "Testing inventory evidence is stale.",
            status.direct_dry_run.disabled_reasons,
        )
        self.assertEqual(status.trust_state, "stale")

    def test_status_blocks_future_dated_runtime_evidence(self) -> None:
        inventory = _inventory(
            instance="testing",
            artifact_id=TESTING_ARTIFACT,
            source_git_ref=TESTING_SOURCE_REF,
            updated_at=(NOW + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
        )

        _, _, status = _status(_store(testing_inventory=inventory))

        self.assertFalse(status.direct_dry_run.enabled)
        self.assertIn(
            "Testing inventory timestamp is invalid.",
            status.direct_dry_run.disabled_reasons,
        )

    def test_status_blocks_runtime_identity_inventory_mismatch(self) -> None:
        inventory = _inventory(
            instance="testing",
            artifact_id=TESTING_ARTIFACT,
            source_git_ref=TESTING_SOURCE_REF,
        ).model_copy(
            update={
                "artifact_identity": ArtifactIdentityReference(artifact_id=NEW_TESTING_ARTIFACT)
            }
        )

        _, _, status = _status(_store(testing_inventory=inventory))

        self.assertFalse(status.workflow_live.enabled)
        self.assertIn(
            "Testing runtime identity does not match current inventory evidence.",
            status.workflow_live.disabled_reasons,
        )

    def test_status_blocks_mutable_artifact_and_source_ref(self) -> None:
        inventory = _inventory(
            instance="testing",
            artifact_id="ghcr.io/example/atlas-commerce:latest",
            source_git_ref="refs/heads/main",
        )

        _, _, status = _status(_store(testing_inventory=inventory))

        self.assertFalse(status.direct_dry_run.enabled)
        self.assertIn(
            "Testing runtime identity does not contain a valid immutable artifact.",
            status.direct_dry_run.disabled_reasons,
        )
        self.assertIn(
            "Testing runtime identity does not contain an immutable source commit.",
            status.direct_dry_run.disabled_reasons,
        )

    def test_status_blocks_missing_or_stale_production_evidence(self) -> None:
        store = _store()
        prod_key = ("atlas-commerce", "prod")
        prod_summary = store.summaries[prod_key]
        store.summaries[prod_key] = prod_summary.model_copy(update={"inventory": None})

        _, _, missing_status = _status(store)

        self.assertIn(
            "Current production inventory is unavailable.",
            missing_status.workflow_live.disabled_reasons,
        )
        self.assertEqual(missing_status.trust_state, "missing")

        stale_inventory = _inventory(
            instance="prod",
            artifact_id=PROD_ARTIFACT,
            source_git_ref=PROD_SOURCE_REF,
            updated_at=(NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        )
        store.summaries[prod_key] = prod_summary.model_copy(update={"inventory": stale_inventory})

        _, _, stale_status = _status(store)

        self.assertIn(
            "Production inventory evidence is stale.",
            stale_status.workflow_live.disabled_reasons,
        )
        self.assertEqual(stale_status.trust_state, "stale")

    def test_status_blocks_unhealthy_production_and_legacy_target_only(self) -> None:
        store = _store()
        prod_key = ("atlas-commerce", "prod")
        prod_summary = store.summaries[prod_key]
        assert prod_summary.inventory is not None
        unhealthy_inventory = prod_summary.inventory.model_copy(
            update={
                "destination_health": prod_summary.inventory.destination_health.model_copy(
                    update={"status": "fail"}
                )
            }
        )
        store.summaries[prod_key] = prod_summary.model_copy(
            update={
                "inventory": unhealthy_inventory,
                "provider_target": None,
                "deployed_target": DeployedTargetReference(
                    provider_id="dokploy",
                    target_category="application",
                    target_id="legacy-app-prod",
                    display_name="Legacy Atlas Commerce prod",
                ),
            }
        )

        _, _, status = _status(store)

        self.assertFalse(status.workflow_live.enabled)
        self.assertIn(
            "Production health evidence is not passing.",
            status.workflow_live.disabled_reasons,
        )
        self.assertIn(
            "Production provider target authority is unavailable.",
            status.workflow_live.disabled_reasons,
        )

    def test_status_requires_verified_health_identity_evidence(self) -> None:
        inventory = _inventory(
            instance="testing",
            artifact_id=TESTING_ARTIFACT,
            source_git_ref=TESTING_SOURCE_REF,
        )
        inventory = inventory.model_copy(
            update={
                "destination_health": inventory.destination_health.model_copy(
                    update={
                        "verified": False,
                    }
                )
            }
        )

        _, _, status = _status(_store(testing_inventory=inventory))

        self.assertFalse(status.direct_dry_run.enabled)
        self.assertIn(
            "Testing health evidence is not verified.",
            status.direct_dry_run.disabled_reasons,
        )

    def test_status_rejects_blank_product_runtime_identity(self) -> None:
        inventory = _inventory(
            instance="testing",
            artifact_id=TESTING_ARTIFACT,
            source_git_ref=TESTING_SOURCE_REF,
        )
        assert inventory.runtime_identity is not None
        blank_product_identity = inventory.runtime_identity.model_copy(update={"product": ""})
        inventory = inventory.model_copy(
            update={
                "runtime_identity": blank_product_identity,
                "destination_health": inventory.destination_health.model_copy(
                    update={"observed_runtime_identity": blank_product_identity}
                ),
            }
        )

        _, _, status = _status(_store(testing_inventory=inventory))

        self.assertFalse(status.workflow_live.enabled)
        self.assertIn(
            "Testing runtime identity does not match current inventory evidence.",
            status.workflow_live.disabled_reasons,
        )

    def test_status_rejects_preview_environment_kind_identity(self) -> None:
        inventory = _inventory(
            instance="testing",
            artifact_id=TESTING_ARTIFACT,
            source_git_ref=TESTING_SOURCE_REF,
        )
        assert inventory.runtime_identity is not None
        preview_identity = inventory.runtime_identity.model_copy(
            update={"environment_kind": "preview"}
        )
        inventory = inventory.model_copy(
            update={
                "runtime_identity": preview_identity,
                "destination_health": inventory.destination_health.model_copy(
                    update={"observed_runtime_identity": preview_identity}
                ),
            }
        )

        _, _, status = _status(_store(testing_inventory=inventory))

        self.assertFalse(status.workflow_live.enabled)
        self.assertIn(
            "Testing runtime identity does not match current inventory evidence.",
            status.workflow_live.disabled_reasons,
        )

    def test_status_fingerprint_binds_production_provider_target(self) -> None:
        store = _store()
        _, _, original = _status(store)
        prod_key = ("atlas-commerce", "prod")
        prod_summary = store.summaries[prod_key]
        assert prod_summary.provider_target is not None
        store.summaries[prod_key] = prod_summary.model_copy(
            update={
                "provider_target": prod_summary.provider_target.model_copy(
                    update={
                        "target_id": "replacement-app-prod",
                        "updated_at": "2026-07-15T08:55:00Z",
                    }
                )
            }
        )

        _, _, replacement = _status(store)

        self.assertNotEqual(
            original.evidence_fingerprint,
            replacement.evidence_fingerprint,
        )

    def test_status_surfaces_authz_and_unsupported_driver_blockers(self) -> None:
        store = _store()
        store.profile = store.profile.model_copy(update={"driver_id": "custom-driver"})

        _, _, status = build_product_promotion_status(
            record_store=store,
            product="atlas-commerce",
            destination_environment="prod",
            action_allowed=lambda _action, _product, _context: False,
            workflow_credentials_ready=lambda _context: True,
            now=NOW,
        )

        self.assertEqual(status.driver_id, "custom-driver")
        self.assertEqual(status.base_driver_id, "")
        self.assertFalse(status.direct_dry_run.enabled)
        self.assertIn(
            "Product driver does not support generic-web promotion.",
            status.direct_dry_run.disabled_reasons,
        )
        self.assertIn(
            "Caller is not authorized to dry-run generic-web promotion.",
            status.direct_dry_run.disabled_reasons,
        )
        self.assertIn(
            "Caller is not authorized to dispatch the promotion workflow.",
            status.workflow_live.disabled_reasons,
        )


class ProductPromotionRequestTests(unittest.TestCase):
    def test_product_request_rejects_client_identity_fields(self) -> None:
        with self.assertRaises(ValidationError):
            ProductPromotionDryRunEnvelope.model_validate(
                {
                    "reason": "Review prod promotion",
                    "evidence_fingerprint": "evidence-1",
                    "artifact_id": "client-controlled",
                }
            )

    def test_workflow_request_carries_reviewed_runtime_identity(self) -> None:
        profile, _, status = _status(_store())
        request = ProductPromotionWorkflowDispatchEnvelope(
            dry_run=False,
            reason="Ship reviewed release",
            evidence_fingerprint=status.evidence_fingerprint,
            bump="minor",
            confirmation=product_promotion_confirmation(status=status, bump="minor"),
        )

        envelope = product_promotion_workflow_request(
            profile=profile,
            status=status,
            request=request,
        )
        payload = product_promotion_request_payload(
            product=profile.product,
            destination_environment=status.destination_environment,
            request=request,
        )

        self.assertEqual(envelope.workflow.artifact_id, status.source.artifact_id)
        self.assertEqual(envelope.workflow.source_git_ref, status.source.source_git_ref)
        self.assertNotIn("artifact_id", payload)
        self.assertNotIn("source_git_ref", payload)
        self.assertNotIn("context", payload)

    def test_product_promotion_intent_binds_current_target_evidence(self) -> None:
        profile, _, status = _status(_store())
        workflow_request = ProductPromotionWorkflowDispatchEnvelope(
            dry_run=False,
            reason="Ship reviewed release",
            evidence_fingerprint=status.evidence_fingerprint,
            bump="minor",
            confirmation=product_promotion_confirmation(status=status, bump="minor"),
        )
        delivery = build_generic_web_promotion_workflow_outbox_delivery(
            request=product_promotion_workflow_request(
                profile=profile,
                status=status,
                request=workflow_request,
            ),
            profile=profile,
            delivery_key="reviewed-intent",
        ).model_copy(
            update={
                "provider_id": "github",
                "provider_operation_key": "github-workflow-operation",
            }
        )
        request = GenericWebProdPromotionRequest(
            product=profile.product,
            artifact_id=status.source.artifact_id,
            source_git_ref=status.source.source_git_ref,
            promotion_intent_id=delivery.delivery_id,
        )

        self.assertTrue(
            product_promotion_intent_matches(
                record=delivery,
                profile=profile,
                status=status,
                request=request,
            )
        )
        changed_status = status.model_copy(
            update={"evidence_fingerprint": "replacement-target-evidence"}
        )
        self.assertFalse(
            product_promotion_intent_matches(
                record=delivery,
                profile=profile,
                status=changed_status,
                request=request,
            )
        )

    def test_delivery_status_keeps_dispatch_and_run_state_distinct(self) -> None:
        pending_record = OutboxDeliveryRecord(
            delivery_id="outbox-promotion",
            kind="github_workflow_dispatch",
            aggregate_type="generic_web_promotion_workflow",
            aggregate_id="atlas-commerce:atlas-commerce",
            dedupe_key="promotion-key",
            created_at="2026-07-15T09:00:00Z",
            updated_at="2026-07-15T09:00:00Z",
            next_attempt_at="2026-07-15T09:00:00Z",
        )
        delivered_record = pending_record.model_copy(
            update={
                "state": "delivered",
                "external_id": "123",
                "external_url": "https://example.test/runs/123",
                "action": "dispatched_workflow",
                "payload": {"run_status": "queued", "run_conclusion": ""},
            }
        )

        pending = product_promotion_delivery_status(pending_record)
        delivered = product_promotion_delivery_status(delivered_record)

        self.assertEqual(pending.dispatch_status, "pending")
        self.assertEqual(pending.run_observation_status, "pending")
        self.assertEqual(delivered.dispatch_status, "dispatched")
        self.assertEqual(delivered.run_status, "queued")
        self.assertEqual(delivered.run_observation_status, "observed")
        self.assertEqual(delivered.run_id, 123)


class FastApiProductPromotionTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_dry_run_replay_and_workflow_dispatch_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            self._seed_store(store)
            session_manager = HumanSessionManager(
                config=_github_oauth_config(),
                session_store=InMemoryHumanSessionStore(),
            )
            session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                control_plane_root_path=root,
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_product_config_policy(
                    actions=(
                        "product_environment.read",
                        "generic_web_prod_promotion.execute",
                        "generic_web_prod_promotion.dispatch",
                    ),
                    product="atlas-commerce",
                    context="atlas-commerce",
                ),
                record_store_factory=lambda: store,
                human_session_manager=session_manager,
            )
            openapi = app.openapi()
            browser_headers = _browser_mutation_headers(session_manager, session)
            with patch(
                "control_plane.http_app.resolve_launchplane_github_token",
                return_value="managed-github-token",
            ):
                status_response = await _asgi_get(
                    app,
                    "/v1/products/atlas-commerce/environments/prod/promotion-status",
                    headers=browser_headers,
                )
                status = status_response.json()["promotion_status"]
                evidence_fingerprint = status["evidence_fingerprint"]
                invalid_direct = await _asgi_request(
                    app,
                    "POST",
                    "/v1/products/atlas-commerce/environments/prod/promotion/dry-run",
                    headers={
                        **_browser_mutation_headers(session_manager, session),
                        "Idempotency-Key": "promotion-invalid-client-identity",
                    },
                    payload={
                        "reason": "Attempt to inject client identity.",
                        "evidence_fingerprint": evidence_fingerprint,
                        "bump": "patch",
                        "artifact_id": "client-controlled",
                    },
                )
                unreviewed_dispatch = await _asgi_request(
                    app,
                    "POST",
                    ("/v1/products/atlas-commerce/environments/prod/promotion/workflow-dispatch"),
                    headers={
                        **_browser_mutation_headers(session_manager, session),
                        "Idempotency-Key": "workflow-before-review",
                    },
                    payload={
                        "reason": "Dispatch before the required review.",
                        "evidence_fingerprint": evidence_fingerprint,
                        "dry_run": True,
                        "bump": "patch",
                    },
                )
                direct_payload = {
                    "reason": "Review the current production promotion evidence.",
                    "evidence_fingerprint": evidence_fingerprint,
                    "bump": "patch",
                }
                direct_response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/products/atlas-commerce/environments/prod/promotion/dry-run",
                    headers={
                        **_browser_mutation_headers(session_manager, session),
                        "Idempotency-Key": "promotion-direct-review",
                    },
                    payload=direct_payload,
                )
                replay_response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/products/atlas-commerce/environments/prod/promotion/dry-run",
                    headers={
                        **_browser_mutation_headers(session_manager, session),
                        "Idempotency-Key": "promotion-direct-review",
                    },
                    payload=direct_payload,
                )
                dispatch_response = await _asgi_request(
                    app,
                    "POST",
                    ("/v1/products/atlas-commerce/environments/prod/promotion/workflow-dispatch"),
                    headers={
                        **_browser_mutation_headers(session_manager, session),
                        "Idempotency-Key": "promotion-workflow-review",
                    },
                    payload={
                        "reason": "Dispatch the reviewed workflow dry-run.",
                        "evidence_fingerprint": evidence_fingerprint,
                        "dry_run": True,
                        "bump": "patch",
                    },
                )
                self.assertEqual(dispatch_response.status_code, 202, dispatch_response.json())
                delivery_id = dispatch_response.json()["result"]["delivery_id"]
                delivery_response = await _asgi_get(
                    app,
                    (
                        "/v1/products/atlas-commerce/environments/prod/"
                        f"promotion/workflow-deliveries/{delivery_id}"
                    ),
                    headers=browser_headers,
                )
                delivery_record = store.read_outbox_delivery_record(delivery_id)
                delivery_inputs = cast(dict[str, str], delivery_record.payload["inputs"])
                live_payload = {
                    "reason": "Dispatch the reviewed live promotion workflow.",
                    "evidence_fingerprint": evidence_fingerprint,
                    "dry_run": False,
                    "bump": "patch",
                    "confirmation": status["live_confirmations"]["patch"],
                }
                live_response = await _asgi_request(
                    app,
                    "POST",
                    ("/v1/products/atlas-commerce/environments/prod/promotion/workflow-dispatch"),
                    headers={
                        **_browser_mutation_headers(session_manager, session),
                        "Idempotency-Key": "promotion-workflow-live",
                    },
                    payload=live_payload,
                )
                live_replay = await _asgi_request(
                    app,
                    "POST",
                    ("/v1/products/atlas-commerce/environments/prod/promotion/workflow-dispatch"),
                    headers={
                        **_browser_mutation_headers(session_manager, session),
                        "Idempotency-Key": "promotion-workflow-live",
                    },
                    payload=live_payload,
                )
                original_write_idempotency = store.write_idempotency_record
                simulated_direct_conflict = False

                def write_idempotency_then_conflict(
                    record: LaunchplaneIdempotencyRecord,
                ) -> None:
                    nonlocal simulated_direct_conflict
                    if (
                        record.idempotency_key == "promotion-direct-concurrent"
                        and not simulated_direct_conflict
                    ):
                        simulated_direct_conflict = True
                        original_write_idempotency(record)
                        raise RuntimeError("simulated concurrent idempotency winner")
                    original_write_idempotency(record)

                with patch.object(
                    store,
                    "write_idempotency_record",
                    side_effect=write_idempotency_then_conflict,
                ):
                    concurrent_direct = await _asgi_request(
                        app,
                        "POST",
                        "/v1/products/atlas-commerce/environments/prod/promotion/dry-run",
                        headers={
                            **_browser_mutation_headers(session_manager, session),
                            "Idempotency-Key": "promotion-direct-concurrent",
                        },
                        payload={
                            "reason": "Review concurrent request recovery.",
                            "evidence_fingerprint": evidence_fingerprint,
                            "bump": "minor",
                        },
                    )
                original_enqueue = store.enqueue_outbox_delivery_with_idempotency
                simulated_workflow_conflict = False

                def enqueue_then_conflict(
                    request: OutboxWithIdempotencyRequest,
                ) -> OutboxDeliveryRecord:
                    nonlocal simulated_workflow_conflict
                    result = original_enqueue(request)
                    if not simulated_workflow_conflict:
                        simulated_workflow_conflict = True
                        raise RuntimeError("simulated concurrent outbox winner")
                    return result

                with patch.object(
                    store,
                    "enqueue_outbox_delivery_with_idempotency",
                    side_effect=enqueue_then_conflict,
                ):
                    concurrent_workflow = await _asgi_request(
                        app,
                        "POST",
                        (
                            "/v1/products/atlas-commerce/environments/prod/"
                            "promotion/workflow-dispatch"
                        ),
                        headers={
                            **_browser_mutation_headers(session_manager, session),
                            "Idempotency-Key": "promotion-workflow-concurrent",
                        },
                        payload={
                            "reason": "Recover the concurrent workflow response.",
                            "evidence_fingerprint": evidence_fingerprint,
                            "dry_run": True,
                            "bump": "minor",
                        },
                    )
            store.close()

        self.assertEqual(status_response.status_code, 200)
        for route_path in (
            "/v1/products/{product}/environments/{environment}/promotion/dry-run",
            "/v1/products/{product}/environments/{environment}/promotion/workflow-dispatch",
        ):
            idempotency_parameter = next(
                parameter
                for parameter in openapi["paths"][route_path]["post"]["parameters"]
                if parameter["name"] == "Idempotency-Key"
            )
            self.assertTrue(idempotency_parameter["required"])
        self.assertTrue(status["direct_dry_run"]["enabled"])
        self.assertEqual(invalid_direct.status_code, 400)
        self.assertEqual(invalid_direct.json()["error"]["code"], "invalid_request")
        self.assertEqual(unreviewed_dispatch.status_code, 409)
        self.assertEqual(
            unreviewed_dispatch.json()["error"]["code"],
            "matching_dry_run_required",
        )
        self.assertEqual(direct_response.status_code, 202)
        self.assertTrue(direct_response.json()["result"]["dry_run"])
        self.assertEqual(
            direct_response.json()["result"]["evidence_fingerprint"],
            evidence_fingerprint,
        )
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        self.assertEqual(
            replay_response.json()["original_trace_id"],
            direct_response.json()["trace_id"],
        )
        self.assertEqual(dispatch_response.status_code, 202)
        self.assertEqual(dispatch_response.json()["result"]["dispatch_status"], "pending")
        self.assertEqual(dispatch_response.json()["result"]["run_status"], "pending")
        self.assertEqual(delivery_response.status_code, 200)
        self.assertEqual(delivery_response.json()["delivery"]["dispatch_status"], "pending")
        self.assertEqual(
            delivery_inputs["artifact_id"],
            TESTING_ARTIFACT,
        )
        self.assertEqual(delivery_inputs["source_git_ref"], TESTING_SOURCE_REF)
        self.assertEqual(delivery_inputs["promotion_intent_id"], delivery_id)
        self.assertEqual(
            delivery_record.payload["promotion_evidence_fingerprint"],
            evidence_fingerprint,
        )
        self.assertEqual(live_response.status_code, 202)
        self.assertFalse(live_response.json()["result"]["dry_run"])
        self.assertEqual(live_response.json()["result"]["dispatch_status"], "pending")
        self.assertEqual(live_replay.status_code, 202)
        self.assertTrue(live_replay.json()["replayed"])
        self.assertEqual(
            live_replay.json()["original_trace_id"],
            live_response.json()["trace_id"],
        )
        self.assertEqual(concurrent_direct.status_code, 202)
        self.assertTrue(concurrent_direct.json()["replayed"])
        self.assertEqual(concurrent_workflow.status_code, 202)
        self.assertTrue(concurrent_workflow.json()["replayed"])

    async def test_workflow_dispatch_rejects_changed_evidence_and_wrong_confirmation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            self._seed_store(store)
            session_manager = HumanSessionManager(
                config=_github_oauth_config(),
                session_store=InMemoryHumanSessionStore(),
            )
            session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                control_plane_root_path=root,
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_product_config_policy(
                    actions=(
                        "product_environment.read",
                        "generic_web_prod_promotion.execute",
                        "generic_web_prod_promotion.dispatch",
                    ),
                    product="atlas-commerce",
                    context="atlas-commerce",
                ),
                record_store_factory=lambda: store,
                human_session_manager=session_manager,
            )
            browser_headers = _browser_mutation_headers(session_manager, session)
            with patch(
                "control_plane.http_app.resolve_launchplane_github_token",
                return_value="managed-github-token",
            ):
                status_response = await _asgi_get(
                    app,
                    "/v1/products/atlas-commerce/environments/prod/promotion-status",
                    headers=browser_headers,
                )
                status = status_response.json()["promotion_status"]
                evidence_fingerprint = status["evidence_fingerprint"]
                direct_response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/products/atlas-commerce/environments/prod/promotion/dry-run",
                    headers={
                        **_browser_mutation_headers(session_manager, session),
                        "Idempotency-Key": "promotion-live-review",
                    },
                    payload={
                        "reason": "Review live promotion.",
                        "evidence_fingerprint": evidence_fingerprint,
                        "bump": "minor",
                    },
                )
                wrong_confirmation = await _asgi_request(
                    app,
                    "POST",
                    ("/v1/products/atlas-commerce/environments/prod/promotion/workflow-dispatch"),
                    headers={
                        **_browser_mutation_headers(session_manager, session),
                        "Idempotency-Key": "promotion-live-wrong",
                    },
                    payload={
                        "reason": "Ship the reviewed release.",
                        "evidence_fingerprint": evidence_fingerprint,
                        "dry_run": False,
                        "bump": "minor",
                        "confirmation": "PROMOTE SOMETHING ELSE",
                    },
                )
                self.assertEqual(wrong_confirmation.status_code, 400, wrong_confirmation.json())
                store.write_environment_inventory(
                    _inventory(
                        instance="testing",
                        artifact_id=NEW_TESTING_ARTIFACT,
                        source_git_ref=NEW_TESTING_SOURCE_REF,
                        updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    )
                )
                changed_evidence = await _asgi_request(
                    app,
                    "POST",
                    ("/v1/products/atlas-commerce/environments/prod/promotion/workflow-dispatch"),
                    headers={
                        **_browser_mutation_headers(session_manager, session),
                        "Idempotency-Key": "promotion-live-stale",
                    },
                    payload={
                        "reason": "Ship stale reviewed evidence.",
                        "evidence_fingerprint": evidence_fingerprint,
                        "dry_run": False,
                        "bump": "minor",
                        "confirmation": status["live_confirmations"]["minor"],
                    },
                )
            store.close()

        self.assertEqual(direct_response.status_code, 202)
        self.assertEqual(wrong_confirmation.json()["error"]["code"], "confirmation_required")
        self.assertEqual(changed_evidence.status_code, 409)
        self.assertEqual(changed_evidence.json()["error"]["code"], "promotion_evidence_changed")

    async def test_raw_live_execution_requires_current_product_intent(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            self._seed_store(store)
            original_target = store.read_provider_target_record(
                context_name="atlas-commerce",
                instance_name="prod",
            )
            replacement_target = ProviderTargetRecord(
                context="atlas-commerce",
                instance="prod",
                provider_id="dokploy",
                target_category="application",
                target_id="replacement-app-prod",
                display_name="Atlas Commerce replacement prod",
                updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )
            profile, _, status = build_product_promotion_status(
                record_store=store,
                product="atlas-commerce",
                destination_environment="prod",
                action_allowed=lambda _action, _product, _context: True,
                workflow_credentials_ready=lambda _context: True,
            )
            workflow_request = ProductPromotionWorkflowDispatchEnvelope(
                dry_run=False,
                reason="Ship reviewed release",
                evidence_fingerprint=status.evidence_fingerprint,
                bump="patch",
                confirmation=product_promotion_confirmation(status=status, bump="patch"),
            )
            intent = build_generic_web_promotion_workflow_outbox_delivery(
                request=product_promotion_workflow_request(
                    profile=profile,
                    status=status,
                    request=workflow_request,
                ),
                profile=profile,
                delivery_key="raw-live-reviewed-intent",
            ).model_copy(
                update={
                    "provider_id": "github",
                    "provider_operation_key": "github-workflow-operation",
                }
            )
            store.write_outbox_delivery_record(intent)
            stale_intent = build_generic_web_promotion_workflow_outbox_delivery(
                request=product_promotion_workflow_request(
                    profile=profile,
                    status=status,
                    request=workflow_request,
                ),
                profile=profile,
                delivery_key="raw-live-stale-intent",
            ).model_copy(
                update={
                    "provider_id": "github",
                    "provider_operation_key": "github-stale-workflow-operation",
                }
            )
            store.write_outbox_delivery_record(stale_intent)
            identity = GitHubActionsIdentity(
                repository="example/atlas-commerce",
                repository_owner="example",
                workflow_ref=(
                    "example/atlas-commerce/.github/workflows/promote-prod.yml@refs/heads/main"
                ),
                job_workflow_ref=(
                    "example/atlas-commerce/.github/workflows/promote-prod.yml@refs/heads/main"
                ),
                ref="refs/heads/main",
                ref_type="branch",
                event_name="workflow_dispatch",
                environment="",
                subject="repo:example/atlas-commerce:ref:refs/heads/main",
                sha="4" * 40,
                raw_claims={},
            )
            authz_policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "example/atlas-commerce",
                            "workflow_refs": [identity.workflow_ref],
                            "event_names": ["workflow_dispatch"],
                            "products": ["atlas-commerce"],
                            "contexts": ["atlas-commerce"],
                            "actions": [
                                "generic_web_prod_promotion.execute",
                                "generic_web_prod_promotion.execute_unreviewed",
                            ],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_app(
                control_plane_root_path=root,
                verifier=_AcceptingVerifier(identity),
                authz_policy=authz_policy,
                record_store_factory=lambda: store,
            )
            replay_app = create_launchplane_fastapi_app(
                control_plane_root_path=root,
                verifier=_AcceptingVerifier(
                    replace(
                        identity,
                        subject="repo:example/atlas-commerce:environment:production",
                    )
                ),
                authz_policy=authz_policy,
                record_store_factory=lambda: store,
            )
            request_payload = {
                "schema_version": 1,
                "product": "atlas-commerce",
                "promotion": {
                    "schema_version": 1,
                    "product": "atlas-commerce",
                    "artifact_id": status.source.artifact_id,
                    "source_git_ref": status.source.source_git_ref,
                    "promotion_intent_id": intent.delivery_id,
                },
            }
            result = GenericWebProdPromotionResult(
                product="atlas-commerce",
                context="atlas-commerce",
                from_instance="testing",
                to_instance="prod",
                artifact_id=status.source.artifact_id,
                source_git_ref=status.source.source_git_ref,
                promotion_record_id="promotion-atlas-testing-to-prod",
                deployment_record_id="deployment-atlas-prod",
                inventory_record_id="atlas-commerce-prod",
                promotion_status="pass",
                deployment_status="pass",
                backup_status="skipped",
                source_health_status="pass",
                destination_health_status="pass",
            )
            resolved_deploy_target = GenericWebResolvedDeployTarget(
                ship_request=ShipRequest(
                    artifact_id=status.source.artifact_id,
                    context="atlas-commerce",
                    instance="prod",
                    source_git_ref=status.source.source_git_ref,
                    target_name="Atlas Commerce prod",
                    target_type="application",
                    provider_id="dokploy",
                    target_category="application",
                    provider_target_type="application",
                    deploy_mode="redeploy",
                ),
                resolved_target=ResolvedTargetEvidence(
                    target_type="application",
                    target_id="app-prod",
                    target_name="Atlas Commerce prod",
                ),
                deployed_target=DeployedTargetReference(
                    provider_id="dokploy",
                    target_category="application",
                    target_id="app-prod",
                    display_name="Atlas Commerce prod",
                    provider_target_type="application",
                ),
                deploy_timeout_seconds=60,
            )
            deploy_provider = Mock()
            mutate_target_after_resolution = [False]

            def resolve_deploy_target(**kwargs: object) -> GenericWebResolvedDeployTarget:
                target_store = cast(PostgresRecordStore, kwargs["record_store"])
                current_target = target_store.read_provider_target_record(
                    context_name="atlas-commerce",
                    instance_name="prod",
                )
                snapshot = resolved_deploy_target.model_copy(
                    update={
                        "ship_request": resolved_deploy_target.ship_request.model_copy(
                            update={"target_name": current_target.display_name}
                        ),
                        "resolved_target": ResolvedTargetEvidence(
                            target_type="application",
                            target_id=current_target.target_id,
                            target_name=current_target.display_name,
                        ),
                        "deployed_target": current_target.to_deployed_target_reference(),
                    }
                )
                if mutate_target_after_resolution[0]:
                    mutate_target_after_resolution[0] = False
                    target_store.write_provider_target_record(replacement_target)
                return snapshot

            deploy_provider.resolve_deploy_target.side_effect = resolve_deploy_target
            execution_started = Event()
            allow_execution_to_finish = Event()
            resolved_target_ids: list[str] = []

            def execute_promotion(**kwargs: object) -> GenericWebProdPromotionResult:
                resolved_target = cast(
                    GenericWebResolvedDeployTarget,
                    kwargs["resolved_deploy_target"],
                )
                resolved_target_ids.append(resolved_target.resolved_target.target_id)
                checkpoint = kwargs["provider_effect_checkpoint"]
                assert callable(checkpoint)
                checkpoint("deploy_trigger")
                if len(resolved_target_ids) == 1:
                    running_reservation = store.read_idempotency_record(
                        scope=f"promotion-intent:{intent.delivery_id}",
                        route_path="/v1/drivers/generic-web/prod-promotion",
                        idempotency_key=intent.delivery_id,
                    )
                    assert running_reservation is not None
                    assert running_reservation.state == "running"
                store.write_provider_target_record(replacement_target)
                execution_started.set()
                if not allow_execution_to_finish.wait(timeout=5):
                    raise AssertionError("promotion execution was not released")
                return result

            with (
                patch(
                    "control_plane.generic_web_promotion_http.execute_generic_web_prod_promotion",
                    side_effect=execute_promotion,
                ) as execute_mock,
                patch(
                    "control_plane.http_app.default_generic_web_deploy_provider",
                    return_value=deploy_provider,
                ),
            ):
                accepted_task = asyncio.create_task(
                    _asgi_request(
                        app,
                        "POST",
                        "/v1/drivers/generic-web/prod-promotion",
                        headers={
                            "Authorization": "Bearer oidc-token",
                            "Idempotency-Key": intent.delivery_id,
                        },
                        payload=request_payload,
                    )
                )
                if not await asyncio.to_thread(execution_started.wait, 5):
                    allow_execution_to_finish.set()
                    early_response = await accepted_task
                    self.fail(
                        "promotion execution did not start: "
                        f"{early_response.status_code} {early_response.json()}"
                    )
                try:
                    concurrent = await _asgi_request(
                        app,
                        "POST",
                        "/v1/drivers/generic-web/prod-promotion",
                        headers={
                            "Authorization": "Bearer oidc-token",
                            "Idempotency-Key": intent.delivery_id,
                        },
                        payload=request_payload,
                    )
                finally:
                    allow_execution_to_finish.set()
                accepted = await accepted_task
                replayed = await _asgi_request(
                    replay_app,
                    "POST",
                    "/v1/drivers/generic-web/prod-promotion",
                    headers={
                        "Authorization": "Bearer oidc-token",
                        "Idempotency-Key": intent.delivery_id,
                    },
                    payload=request_payload,
                )
                stale_request_payload = {
                    **request_payload,
                    "promotion": {
                        **cast(dict[str, object], request_payload["promotion"]),
                        "promotion_intent_id": stale_intent.delivery_id,
                    },
                }
                drifted = await _asgi_request(
                    app,
                    "POST",
                    "/v1/drivers/generic-web/prod-promotion",
                    headers={
                        "Authorization": "Bearer oidc-token",
                        "Idempotency-Key": stale_intent.delivery_id,
                    },
                    payload=stale_request_payload,
                )
                unreviewed_request_payload = {
                    **request_payload,
                    "promotion": {
                        key: value
                        for key, value in cast(
                            dict[str, object], request_payload["promotion"]
                        ).items()
                        if key != "promotion_intent_id"
                    },
                }
                unreviewed = await _asgi_request(
                    app,
                    "POST",
                    "/v1/drivers/generic-web/prod-promotion",
                    headers={
                        "Authorization": "Bearer oidc-token",
                        "Idempotency-Key": "raw-live-unreviewed",
                    },
                    payload=unreviewed_request_payload,
                )
                unreviewed_replay = await _asgi_request(
                    app,
                    "POST",
                    "/v1/drivers/generic-web/prod-promotion",
                    headers={
                        "Authorization": "Bearer oidc-token",
                        "Idempotency-Key": "raw-live-unreviewed",
                    },
                    payload=unreviewed_request_payload,
                )
                store.write_provider_target_record(original_target)
                mutate_target_after_resolution[0] = True
                target_raced = await _asgi_request(
                    app,
                    "POST",
                    "/v1/drivers/generic-web/prod-promotion",
                    headers={
                        "Authorization": "Bearer oidc-token",
                        "Idempotency-Key": stale_intent.delivery_id,
                    },
                    payload=stale_request_payload,
                )
            intent_reservation = store.read_idempotency_record(
                scope=f"promotion-intent:{intent.delivery_id}",
                route_path="/v1/drivers/generic-web/prod-promotion",
                idempotency_key=intent.delivery_id,
            )
            store.close()

        self.assertEqual(accepted.status_code, 202, accepted.json())
        self.assertEqual(concurrent.status_code, 409, concurrent.json())
        self.assertEqual(concurrent.json()["error"]["code"], "mutation_in_progress")
        self.assertEqual(replayed.status_code, 202, replayed.json())
        self.assertTrue(replayed.json()["replayed"])
        self.assertEqual(
            replayed.json()["original_trace_id"],
            accepted.json()["trace_id"],
        )
        self.assertEqual(drifted.status_code, 409, drifted.json())
        self.assertEqual(drifted.json()["error"]["code"], "promotion_intent_invalid")
        self.assertEqual(unreviewed.status_code, 202, unreviewed.json())
        self.assertEqual(unreviewed_replay.status_code, 202, unreviewed_replay.json())
        self.assertTrue(unreviewed_replay.json()["replayed"])
        self.assertEqual(target_raced.status_code, 409, target_raced.json())
        self.assertEqual(target_raced.json()["error"]["code"], "promotion_target_changed")
        assert intent_reservation is not None
        self.assertEqual(intent_reservation.state, "completed")
        self.assertEqual(resolved_target_ids, ["app-prod", "replacement-app-prod"])
        self.assertEqual(deploy_provider.resolve_deploy_target.call_count, 4)
        self.assertEqual(execute_mock.call_count, 2)

    @staticmethod
    def _seed_store(store: PostgresRecordStore) -> None:
        store.write_product_profile_record(_profile())
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        store.write_environment_inventory(
            _inventory(
                instance="testing",
                artifact_id=TESTING_ARTIFACT,
                source_git_ref=TESTING_SOURCE_REF,
                updated_at=now,
            )
        )
        store.write_environment_inventory(
            _inventory(
                instance="prod",
                artifact_id=PROD_ARTIFACT,
                source_git_ref=PROD_SOURCE_REF,
                updated_at=now,
            )
        )
        store.write_provider_target_record(
            ProviderTargetRecord(
                context="atlas-commerce",
                instance="prod",
                provider_id="dokploy",
                target_category="application",
                target_id="app-prod",
                display_name="Atlas Commerce prod",
                updated_at=now,
                source_label="test",
            )
        )


if __name__ == "__main__":
    unittest.main()
