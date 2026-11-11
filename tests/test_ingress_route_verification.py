from pathlib import Path
from collections.abc import Callable
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.promotion_record import ArtifactIdentityReference, DeploymentEvidence
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.route_binding_record import route_binding_record_sha256
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.http import request
from tests.support.ingress import _FakeNpmplusIngressClient, _npmplus_proxy_host
from tests.test_http_app_product import _product_health_monitoring_profile
from tests.test_route_binding_record import (
    _dokploy_target_id_record,
    _dokploy_target_record,
    _edge_endpoint_record,
    _provider_target_record,
    _route_binding_record,
)


class IngressRouteVerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(directory) / 'db'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        profile = _product_health_monitoring_profile().model_dump(mode="json")
        profile.update(product="example-product", production_use="prelaunch")
        profile["lanes"] = [
            {
                **profile["lanes"][0],
                "context": "example-testing",
                "instance": "prod",
                "base_url": "https://app.example.test",
                "health_url": "https://app.example.test/health",
            }
        ]
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile)
        )
        self.store.write_provider_target_record(_provider_target_record(instance="prod"))
        self.store.write_dokploy_target_record(_dokploy_target_record(instance="prod"))
        self.store.write_dokploy_target_id_record(_dokploy_target_id_record(instance="prod"))
        self.store.write_edge_endpoint_record(_edge_endpoint_record())
        current = _route_binding_record(instance="prod")
        self.current = current.model_copy(
            update={
                "source": current.source.model_copy(update={"source_kind": "service"}),
                "tls": current.tls.model_copy(update={"owner": "provider"}),
            }
        )
        self.store.write_route_binding_record(self.current)
        self.client = _FakeNpmplusIngressClient(
            (
                _npmplus_proxy_host(
                    domain_names=("app.example.test",),
                    forward_host="192.0.2.10",
                    certificate_id=101,
                ),
            )
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(schema_version=2),
            bearer_identity_config=_local_operator_bearer_config(),
            record_store_factory=lambda: self.store,
            npmplus_ingress_client_factory=lambda: self.client,
        )
        self.now = "2026-11-11T10:00:00Z"
        self.enterContext(
            patch.object(
                self.store, "_database_mutation_timestamp", side_effect=lambda _session: self.now
            )
        )

    async def post(self, path: str, payload: dict[str, Any], *, key: str = "") -> Any:
        headers = {"Authorization": "Bearer local-operator-token"}
        if key:
            headers["Idempotency-Key"] = key
        with patch("control_plane.http_app.utc_now_timestamp", return_value=self.now):
            return await request(self.app, "POST", path, headers=headers, payload=payload)

    async def verify(self, **overrides: Any) -> Any:
        return await self.post(
            "/v1/drivers/ingress/route-apply",
            {
                "product": "example-product",
                "context": "example-testing",
                "instance": "prod",
                "ingress": {
                    "mode": "dry-run",
                    "expected_host_id": 79,
                    "verify_route_binding": True,
                    "require_exact_expected_host_domains": True,
                    "allow_create": False,
                    "allow_update": False,
                    "allow_enable_disable": False,
                    "reason": "Observe the existing fictional route",
                    "route": {
                        "domain_names": ["app.example.test"],
                        "edge_endpoint_key": "example-edge",
                        "certificate_id": 101,
                    },
                    **overrides,
                },
            },
        )

    async def reconcile(self, *, mode: str = "dry-run", key: str = "") -> Any:
        return await self.post(
            "/v1/route-bindings/reconcile",
            {
                "product": "example-product",
                "context": "example-testing",
                "instance": "prod",
                "mode": mode,
                "expected_current": {
                    "state": "present",
                    "record_sha256": route_binding_record_sha256(self.current),
                },
                "reason": "Use the observed fictional route",
                "confirmation": "APPLY LAUNCHPLANE ROUTE BINDING RECONCILE"
                if mode == "apply"
                else "",
            },
            key=key,
        )

    async def test_current_observation_replaces_missing_history_without_provider_mutation(
        self,
    ) -> None:
        before = await self.reconcile()
        self.assertEqual(before.status_code, 202, before.text)
        self.assertEqual(before.json()["result"]["status"], "blocked")
        observed = await self.verify()
        self.assertEqual(observed.status_code, 202, observed.text)
        self.assertEqual(observed.json()["result"]["status"], "unchanged")
        audits = self.store.list_ingress_route_audit_records(product="example-product")
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].mode, "dry-run")
        planned = await self.reconcile()
        self.assertEqual(planned.status_code, 202, planned.text)
        self.assertEqual(planned.json()["result"]["status"], "ready", planned.text)
        applied = await self.reconcile(mode="apply", key="observed-route")
        self.assertEqual(applied.status_code, 202, applied.text)
        self.assertEqual(applied.json()["records"]["route_binding_status"], "refreshed")
        self.assertEqual(
            self.store.list_ingress_route_audit_records(product="example-product"), audits
        )
        self.assertEqual(set(self.client.calls), {"list"})

    def binding(self) -> Any:
        return self.store.read_route_binding_record(
            product="example-product", context_name="example-testing", instance_name="prod"
        )

    async def apply_with_commit_change(self, change: Callable[[], object], key: str) -> Any:
        original = self.store.reconcile_route_binding_record

        def changed(**kwargs: Any) -> Any:
            change()
            return original(**kwargs)

        with patch.object(self.store, "reconcile_route_binding_record", side_effect=changed):
            return await self.reconcile(mode="apply", key=key)

    async def test_legacy_dry_run_is_not_new_authority(self) -> None:
        original = self.post

        async def legacy(path: str, payload: dict[str, Any], *, key: str = "") -> Any:
            payload["ingress"].pop("verify_route_binding")
            return await original(path, payload, key=key)

        with patch.object(self, "post", side_effect=legacy):
            observed = await self.verify()
        self.assertEqual(observed.status_code, 202, observed.text)
        audit = self.store.list_ingress_route_audit_records()[0]
        self.assertIsNone(audit.route_verification)
        self.assertNotIn("route_verification", audit.model_dump(mode="json"))
        planned = await self.reconcile()
        self.assertEqual(planned.json()["result"]["status"], "blocked")
        self.assertEqual(self.binding(), self.current)

    async def test_observation_expiry_cannot_be_extended_by_reconciliation(self) -> None:
        observed = await self.verify()
        self.assertEqual(observed.status_code, 202, observed.text)
        applied = await self.reconcile(mode="apply", key="first-observation")
        self.assertEqual(applied.status_code, 202, applied.text)
        self.current = self.binding()
        self.now = "2026-11-11T23:00:00Z"
        repeated = await self.reconcile(mode="apply", key="same-observation")
        self.assertEqual(repeated.status_code, 202, repeated.text)
        self.assertEqual(repeated.json()["records"]["route_binding_status"], "unchanged")
        self.assertEqual(self.binding(), self.current)
        self.now = self.current.source.stale_after
        expired = await self.reconcile(mode="apply", key="expired-observation")
        self.assertEqual(expired.json()["result"]["status"], "blocked")
        self.assertEqual(self.binding(), self.current)
        renewed = await self.verify()
        self.assertEqual(renewed.status_code, 202, renewed.text)
        fresh = await self.reconcile(mode="apply", key="new-observation")
        self.assertEqual(fresh.json()["records"]["route_binding_status"], "refreshed", fresh.text)
        self.assertGreater(self.binding().source.stale_after, self.current.source.stale_after)

    async def test_route_drift_observation_blocks_previous_matching_observation(self) -> None:
        self.assertEqual((await self.verify()).status_code, 202)
        self.now = "2026-11-11T10:01:00Z"
        self.client.proxy_hosts[0] = self.client.proxy_hosts[0].model_copy(
            update={"forward_host": "192.0.2.99"}
        )
        changed = await self.verify()
        self.assertEqual(changed.status_code, 202, changed.text)
        self.assertEqual(changed.json()["result"]["status"], "planned")
        plan = await self.reconcile(mode="apply", key="drift")
        self.assertEqual(plan.json()["result"]["status"], "blocked")
        self.assertEqual(self.binding(), self.current)
        self.assertEqual(set(self.client.calls), {"list"})

    async def test_provider_ambiguity_never_creates_observation(self) -> None:
        self.client.proxy_hosts.append(self.client.proxy_hosts[0].model_copy(update={"id": 80}))
        observed = await self.verify()
        self.assertEqual(observed.status_code, 400, observed.text)
        self.assertEqual(self.store.list_ingress_route_audit_records(), ())
        self.assertEqual(self.binding(), self.current)

    async def test_pending_original_effect_is_not_settled_by_observation(self) -> None:
        from tests.test_route_binding_record import _ingress_audit_record

        self.store.write_ingress_route_audit_record(_ingress_audit_record(status="pending"))
        self.assertEqual((await self.verify()).status_code, 202)
        plan = await self.reconcile(mode="apply", key="unresolved-original")
        self.assertEqual(plan.json()["result"]["findings"][0]["code"], "ingress_audit_unresolved")
        self.assertEqual(self.binding(), self.current)

    async def test_changed_source_requires_new_observation(self) -> None:
        self.assertEqual((await self.verify()).status_code, 202)
        self.store.write_dokploy_target_record(
            _dokploy_target_record(instance="prod").model_copy(
                update={"source_git_ref": "new-source"}
            )
        )
        plan = await self.reconcile(mode="apply", key="changed-source")
        self.assertEqual(plan.json()["result"]["status"], "blocked")
        self.assertEqual(self.binding(), self.current)
        self.now = "2026-11-11T10:01:00Z"
        self.assertEqual((await self.verify()).status_code, 202)
        retry = await self.reconcile(mode="apply", key="new-source-observed")
        self.assertEqual(retry.json()["records"]["route_binding_status"], "refreshed", retry.text)

    async def test_sources_and_binding_are_rechecked_at_commit(self) -> None:
        profile = self.store.read_product_profile_record("example-product")
        changes: dict[str, Callable[[], object]] = {
            "target": lambda: self.store.write_dokploy_target_record(
                _dokploy_target_record(instance="prod").model_copy(
                    update={"source_git_ref": "changed"}
                )
            ),
            "edge": lambda: self.store.write_edge_endpoint_record(
                _edge_endpoint_record().model_copy(update={"upstream_host": "192.0.2.99"})
            ),
            "profile": lambda: self.store.write_product_profile_record(
                profile.model_copy(update={"production_use": "live"})
            ),
            "binding": lambda: self.store.write_route_binding_record(
                self.current.model_copy(update={"status": "disabled"})
            ),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                self.store.write_dokploy_target_record(_dokploy_target_record(instance="prod"))
                self.store.write_edge_endpoint_record(_edge_endpoint_record())
                self.store.write_product_profile_record(profile)
                self.store.write_route_binding_record(self.current)
                self.assertEqual((await self.verify()).status_code, 202)

                result = await self.apply_with_commit_change(change, f"changed-{name}")
                self.assertEqual(result.status_code, 409, result.text)
                self.assertEqual(self.binding().source, self.current.source)

    async def test_expiry_between_plan_and_commit_refuses(self) -> None:
        self.assertEqual((await self.verify()).status_code, 202)
        original = self.store.reconcile_route_binding_record

        def expire(**kwargs: Any) -> Any:
            self.now = "2026-11-12T10:00:00Z"
            return original(**kwargs)

        with patch.object(self.store, "reconcile_route_binding_record", side_effect=expire):
            result = await self.reconcile(mode="apply", key="expired-at-commit")
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(self.binding(), self.current)

    def deployment_sources(self) -> tuple[EnvironmentInventory, DeploymentRecord]:
        runtime = RuntimeIdentity(
            product="example-product",
            context="example-testing",
            instance="prod",
            deployment_record_id="deployment-one",
            artifact_id="artifact-one",
            source_git_ref="source-one",
        )
        deploy = DeploymentEvidence(
            target_name="example-target", target_type="compose", deploy_mode="deploy", status="pass"
        )
        artifact = ArtifactIdentityReference(artifact_id=runtime.artifact_id)
        deployment = DeploymentRecord(
            record_id=runtime.deployment_record_id,
            context=runtime.context,
            instance=runtime.instance,
            source_git_ref=runtime.source_git_ref,
            runtime_identity=runtime,
            artifact_identity=artifact,
            deploy=deploy,
            resolved_target=ResolvedTargetEvidence(
                target_type="compose",
                target_id="compose-provider-id",
                target_name="example-target",
            ),
        )
        inventory = EnvironmentInventory(
            context=runtime.context,
            instance=runtime.instance,
            source_git_ref=runtime.source_git_ref,
            runtime_identity=runtime,
            artifact_identity=artifact,
            deploy=deploy,
            updated_at=self.now,
            deployment_record_id=deployment.record_id,
        )
        self.store.write_deployment_record(deployment)
        self.store.write_environment_inventory(inventory)
        return inventory, deployment

    async def test_inventory_and_deployment_binding_survives_observation_and_cas(self) -> None:
        inventory, deployment = self.deployment_sources()
        observed = await self.verify()
        self.assertEqual(observed.status_code, 202, observed.text)
        changes: dict[str, Callable[[], object]] = {
            "inventory": lambda: self.store.write_environment_inventory(
                inventory.model_copy(update={"source_git_ref": "other-source"})
            ),
            "deployment": lambda: self.store.write_deployment_record(
                deployment.model_copy(
                    update={
                        "artifact_identity": ArtifactIdentityReference(artifact_id="other-artifact")
                    }
                )
            ),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                self.store.write_environment_inventory(inventory)
                self.store.write_deployment_record(deployment)

                result = await self.apply_with_commit_change(change, f"changed-{name}")
                self.assertEqual(result.status_code, 409, result.text)
                self.assertEqual(self.binding(), self.current)
                refused = await self.verify()
                self.assertEqual(refused.status_code, 409, refused.text)
        self.store.write_environment_inventory(inventory)
        self.store.write_deployment_record(deployment)
        result = await self.reconcile(mode="apply", key="coherent-deployment")
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(result.json()["records"]["route_binding_status"], "refreshed")
        self.assertEqual(
            self.store.read_environment_inventory(
                context_name=inventory.context, instance_name=inventory.instance
            ),
            inventory,
        )
        self.assertEqual(self.store.read_deployment_record(deployment.record_id), deployment)

    async def test_inventory_appearing_after_observation_is_a_source_change(self) -> None:
        self.assertEqual((await self.verify()).status_code, 202)
        original = self.store.reconcile_route_binding_record

        def changed(**kwargs: Any) -> Any:
            self.deployment_sources()
            return original(**kwargs)

        with patch.object(self.store, "reconcile_route_binding_record", side_effect=changed):
            result = await self.reconcile(mode="apply", key="new-inventory")
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(self.binding(), self.current)

    async def test_verification_cannot_request_provider_effects(self) -> None:
        for changes in (
            {"mode": "apply"},
            {"allow_update": True},
            {"allow_create": True},
            {"allow_enable_disable": True},
            {"require_exact_expected_host_domains": False},
        ):
            with self.subTest(changes=changes):
                result = await self.verify(**changes)
                self.assertEqual(result.status_code, 400, result.text)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.store.list_ingress_route_audit_records(), ())

    async def test_source_change_during_provider_read_does_not_attest(self) -> None:
        original = self.client.get_proxy_host

        def changed(host_id: int) -> Any:
            self.store.write_dokploy_target_id_record(
                _dokploy_target_id_record(instance="prod", target_id="other-target")
            )
            return original(host_id)

        with patch.object(self.client, "get_proxy_host", side_effect=changed):
            result = await self.verify()
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(self.store.list_ingress_route_audit_records(), ())
        self.assertEqual(self.binding(), self.current)
