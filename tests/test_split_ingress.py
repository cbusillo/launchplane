import unittest
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from pydantic import ValidationError

from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    redacted_route_binding_record,
    route_binding_record_sha256,
)
from tests.test_route_binding_record import _route_binding_record
from tests.test_browser_route_repair import _BrowserRouteRepairFixture
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_topology_read_model import build_product_environment_topology
from control_plane.workflows.public_ingress_monitor import discover_public_ingress_monitor_targets
from control_plane.split_ingress import split_ingress_check_name
from tests.test_route_binding_record import _dokploy_target_record, _ingress_audit_record
from tests.test_product_topology_read_model import (
    _NOW,
    _TopologyStore,
    _strict_public_profile,
    _http_observation,
    _lane_summary,
)
from control_plane.lane_runtime_verification import read_lane_runtime_verification
from control_plane.contracts.product_operational_readiness import (
    build_product_operational_readiness,
)
from tests.test_product_operational_readiness import _inputs
from dataclasses import replace
from tests.test_route_binding_record import _route_binding_policy
from tests.support.auth import _StubVerifier, _identity
from tests.support.http import request
from control_plane.http_app import create_launchplane_fastapi_app, RouteBindingReconcileEnvelope
from control_plane.route_binding_reconcile import (
    plan_route_binding_reconcile,
    RouteBindingReconcileRequest,
    RouteBindingExpectedCurrent,
    is_evidence_only_route_binding_refresh,
)


def split_binding() -> EnvironmentRouteBindingRecord:
    payload = _route_binding_record(domain="business.example.test").model_dump(mode="json")
    payload["domains"].extend(
        [
            {"domain_name": "www.business.example.test", "role": "alias"},
            {"domain_name": "operator.example.test", "role": "alias"},
        ]
    )
    payload["external_ingress"] = {
        "domains": ["business.example.test", "www.business.example.test"],
        "custodian": "Office proxy operator",
        "evidence_url": "https://example.test/evidence/office-proxy",
        "attested_at": payload["updated_at"],
    }
    return EnvironmentRouteBindingRecord.model_validate(payload)


class SplitIngressTests(unittest.TestCase):
    def test_business_and_operator_hosts_retain_distinct_custody(self) -> None:
        binding = split_binding()
        self.assertEqual(binding.ingress_for_domain("business.example.test").provider, "external")
        self.assertEqual(binding.tls_for_domain("www.business.example.test").owner, "external")
        self.assertEqual(binding.ingress_for_domain("operator.example.test"), binding.ingress)
        self.assertEqual(binding.tls_for_domain("operator.example.test"), binding.tls)
        public = redacted_route_binding_record(binding)
        self.assertEqual(public.external_ingress, binding.external_ingress)
        self.assertEqual(public.external_ingress_expires_at, binding.external_ingress_expires_at)
        self.assertEqual(public.record_sha256, route_binding_record_sha256(binding))

    def test_external_scope_cannot_hide_or_replace_managed_hosts(self) -> None:
        payload = split_binding().model_dump(mode="json")
        for domains in (["unknown.example.test"], [d["domain_name"] for d in payload["domains"]]):
            with self.subTest(domains=domains), self.assertRaises(ValidationError):
                EnvironmentRouteBindingRecord.model_validate(
                    {
                        **payload,
                        "external_ingress": {**payload["external_ingress"], "domains": domains},
                    }
                )
        with self.assertRaises(ValidationError):
            EnvironmentRouteBindingRecord.model_validate(
                {
                    **payload,
                    "external_ingress": {
                        **payload["external_ingress"],
                        "evidence_url": "https://example.test/report?token=private",
                    },
                }
            )

    def test_unknown_host_has_no_inferred_authority(self) -> None:
        with self.assertRaises(ValueError):
            split_binding().ingress_for_domain("unknown.example.test")

    def test_one_failed_or_old_host_cannot_borrow_another_paths_pass(self) -> None:
        binding = split_binding().model_copy(
            update={
                "product": "example-site",
                "context": "example-context",
                "instance": "prod",
            }
        )
        profile = _strict_public_profile(domain_name="business.example.test")
        summary = _lane_summary()
        observations = tuple(
            _http_observation(
                domain_name=domain.domain_name,
                monitoring_intent="public",
            ).model_copy(
                update={
                    "record_id": f"observed-{domain.domain_name}",
                    "check_name": split_ingress_check_name(domain.domain_name),
                }
            )
            for domain in binding.domains
        )
        original = _http_observation(
            domain_name="business.example.test", monitoring_intent="public"
        )
        store = _TopologyStore(route_binding=binding, observations=(*observations, original))

        def failing_hosts() -> set[str]:
            topology = build_product_environment_topology(
                record_store=store,
                profile=profile,
                lane=profile.lanes[0],
                lane_summary=summary,
                now=_NOW,
            )
            readiness = build_product_operational_readiness(replace(_inputs(), topology=topology))
            if any(
                warning.scope == "observation"
                and warning.severity == "error"
                and warning.domain_name
                for warning in topology.warnings
            ):
                self.assertEqual(
                    next(
                        dimension.state
                        for dimension in readiness.dimensions
                        if dimension.dimension == "topology"
                    ),
                    "blocked",
                )
            return {
                warning.domain_name
                for warning in topology.warnings
                if warning.scope == "observation"
                and warning.severity == "error"
                and warning.domain_name
            }

        self.assertEqual(failing_hosts(), set())
        verification = read_lane_runtime_verification(
            record_store=store, profile=profile, lane=profile.lanes[0], summary=summary, now=_NOW
        )
        self.assertEqual(verification.provenance.freshness_status, "verified")
        store.route_binding = binding.model_copy(
            update={
                "updated_at": _NOW.isoformat(),
                "source": binding.source.model_copy(
                    update={"stale_after": (_NOW - timedelta(hours=1)).isoformat()}
                ),
            }
        )
        refreshed_topology = build_product_environment_topology(
            record_store=store,
            profile=profile,
            lane=profile.lanes[0],
            lane_summary=summary,
            now=_NOW,
        )
        self.assertFalse(
            any(
                warning.code == "stale_route_authority" and warning.severity == "error"
                for warning in refreshed_topology.warnings
            )
        )
        self.assertEqual(
            read_lane_runtime_verification(
                record_store=store,
                profile=profile,
                lane=profile.lanes[0],
                summary=summary,
                now=_NOW,
            ).provenance.freshness_status,
            "verified",
        )
        assert binding.external_ingress is not None
        store.route_binding = binding.model_copy(
            update={
                "external_ingress": binding.external_ingress.model_copy(
                    update={"attested_at": (_NOW - timedelta(days=31)).isoformat()}
                )
            }
        )
        expired_topology = build_product_environment_topology(
            record_store=store,
            profile=profile,
            lane=profile.lanes[0],
            lane_summary=summary,
            now=_NOW,
        )
        self.assertTrue(
            any(
                warning.code == "stale_route_authority" and warning.severity == "error"
                for warning in expired_topology.warnings
            )
        )
        store.route_binding = binding
        failed = _http_observation(
            domain_name="business.example.test",
            monitoring_intent="public",
            runtime_identity_status="mismatch",
        ).model_copy(
            update={
                "check_name": observations[0].check_name,
                "record_id": "business-failure",
            }
        )
        stale = observations[1].model_copy(
            update={"observed_at": (_NOW - timedelta(days=2)).isoformat()}
        )
        store.observations = (failed, stale, observations[2], original)
        self.assertEqual(failing_hosts(), {"business.example.test", "www.business.example.test"})
        verification = read_lane_runtime_verification(
            record_store=store, profile=profile, lane=profile.lanes[0], summary=summary, now=_NOW
        )
        self.assertNotEqual(verification.provenance.freshness_status, "verified")


class SplitIngressBrowserTests(_BrowserRouteRepairFixture):
    def seed_split(self) -> dict[str, Any]:
        payload = self.seed_binding()
        self.target = _dokploy_target_record(
            domains=("app.example.test", "www.example.test", "operator.example.test")
        )
        self.store.write_dokploy_target_record(self.target)
        self.store.write_provider_target_record(
            ProviderTargetRecord.from_dokploy_records(
                target_record=self.target,
                target_id_record=self.target_id,
            )
        )
        self.store.write_ingress_route_audit_record(
            _ingress_audit_record(
                record_id="audit-alias",
                domains=("operator.example.test",),
            )
        )
        profile = self.store.read_product_profile_record("example-product").model_dump(mode="json")
        profile["driver_id"] = "generic-web"
        profile["lanes"][0]["base_url"] = "https://app.example.test"
        profile["lanes"][0]["health_url"] = "https://app.example.test/healthz"
        profile["lanes"][0]["health_monitoring"]["monitoring_intent"] = "public"
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile)
        )
        return {
            **payload,
            "external_ingress": {
                "domains": ["app.example.test", "www.example.test"],
                "custodian": "Business proxy operator",
                "evidence_url": "https://example.test/report",
            },
        }

    async def test_signed_review_saves_split_and_all_readers_keep_each_host(self) -> None:
        payload = self.seed_split()
        route = "/v1/route-bindings/reconcile"
        response = await self.post(route, payload)
        self.assertEqual(response.status_code, 202, response.text)
        review = response.json()["review"]
        self.assertEqual(review["status"], "ready", review)
        self.assertEqual(set(review["proposed_domains"]), set(self.target.domains))
        applied = await self.post(
            route,
            {
                **payload,
                "external_ingress": review["proposed_record"]["external_ingress"],
                "mode": "apply",
                "reason": "Record both existing ingress paths.",
                "confirmation": review["expected_confirmation"],
                "browser_review": {
                    "product": payload["product"],
                    "reviewed_plan_sha256": review["plan_sha256"],
                },
            },
            "split-existing-routes",
        )
        self.assertEqual(applied.status_code, 202, applied.text)
        current = self.store.read_route_binding_record(
            product=payload["product"],
            context_name=payload["context"],
            instance_name=payload["instance"],
        )
        assert current.external_ingress is not None
        self.assertEqual(
            current.external_ingress.domains, tuple(payload["external_ingress"]["domains"])
        )
        profile = self.store.read_product_profile_record(payload["product"])
        topology = build_product_environment_topology(
            record_store=self.store, profile=profile, lane=profile.lanes[0], lane_summary=None
        )
        self.assertEqual(
            {domain.domain_name for domain in topology.desired.domains}, set(self.target.domains)
        )
        self.assertFalse(
            {
                "domain_divergence",
                "missing_route_authority",
                "ingress_ownership_unknown",
                "tls_ownership_unknown",
            }
            & {warning.code for warning in topology.warnings}
        )
        self.assertEqual(
            {
                warning.domain_name
                for warning in topology.warnings
                if warning.code == "public_ingress_observation_missing" and warning.domain_name
            },
            set(self.target.domains),
        )
        hosts = {domain.domain_name: domain for domain in topology.provider_recorded.domains}
        assert hosts["app.example.test"].ingress is not None
        assert hosts["operator.example.test"].ingress is not None
        self.assertEqual(hosts["app.example.test"].ingress.provider, "external")
        self.assertEqual(hosts["operator.example.test"].ingress.provider, current.ingress.provider)
        targets = discover_public_ingress_monitor_targets(self.store, profiles=(profile,))
        strict = {
            target.check_name: target
            for target in targets
            if target.check_kind == "public_http"
            and target.check_name
            in {split_ingress_check_name(host) for host in self.target.domains}
        }
        self.assertEqual(
            set(strict), {split_ingress_check_name(host) for host in self.target.domains}
        )
        self.assertTrue(
            all(
                target.require_runtime_identity and target.route_binding_sha256
                for target in strict.values()
            )
        )
        self.assertEqual(
            {
                target.tls_domain_name: target.tls_owner
                for target in targets
                if target.check_kind == "tls"
            },
            {
                "app.example.test": "external",
                "www.example.test": "external",
                "operator.example.test": current.tls.owner,
            },
        )
        replay = await self.post(
            route, {**payload, "external_ingress": review["proposed_record"]["external_ingress"]}
        )
        self.assertEqual(replay.json()["review"]["status"], "unchanged")
        machine = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_route_binding_policy(action="route_binding.apply"),
            record_store_factory=lambda: self.store,
        )
        refresh_payload = {
            "product": current.product,
            "context": current.context,
            "instance": current.instance,
            "expected_current": {
                "state": "present",
                "record_sha256": route_binding_record_sha256(current),
            },
            "mode": "apply",
            "reason": "Refresh unchanged managed evidence.",
            "confirmation": review["expected_confirmation"],
        }
        refreshed = await request(
            machine,
            "POST",
            route,
            payload=refresh_payload,
            headers={
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "split-machine-refresh",
            },
        )
        self.assertEqual(refreshed.status_code, 202, refreshed.text)
        self.assertEqual(
            self.store.read_route_binding_record(
                product=current.product,
                context_name=current.context,
                instance_name=current.instance,
            ).external_ingress,
            current.external_ingress,
        )

    async def test_external_scope_change_after_review_cannot_apply(self) -> None:
        payload = self.seed_split()
        review = (await self.post("/v1/route-bindings/reconcile", payload)).json()["review"]
        response = await self.post(
            "/v1/route-bindings/reconcile",
            {
                **payload,
                "external_ingress": {
                    **payload["external_ingress"],
                    "domains": ["www.example.test"],
                },
                "mode": "apply",
                "reason": "Changed custody after review.",
                "confirmation": review["expected_confirmation"],
                "browser_review": {
                    "product": payload["product"],
                    "reviewed_plan_sha256": review["plan_sha256"],
                },
            },
            "split-stale-scope",
        )
        self.assertEqual(response.status_code, 409, response.text)
        with self.assertRaises(FileNotFoundError):
            self.store.read_route_binding_record(
                product=payload["product"],
                context_name=payload["context"],
                instance_name=payload["instance"],
            )

    async def test_signed_review_can_change_or_relinquish_custody_with_managed_proof(self) -> None:
        payload = self.seed_split()
        route = "/v1/route-bindings/reconcile"

        async def save(candidate: dict[str, Any], key: str) -> dict[str, Any]:
            review = (await self.post(route, candidate)).json()["review"]
            self.assertEqual(review["status"], "ready", review)
            proposed = review["proposed_record"]
            apply_payload = {
                **candidate,
                "mode": "apply",
                "reason": "Record the reviewed custody.",
                "external_ingress": proposed.get("external_ingress"),
                "expected_current": review["expected_current"],
                "confirmation": review["expected_confirmation"],
                "browser_review": {
                    "product": payload["product"],
                    "reviewed_plan_sha256": review["plan_sha256"],
                },
            }
            applied = await self.post(route, apply_payload, key)
            self.assertEqual(applied.status_code, 202, applied.text)
            saved_record: dict[str, Any] = applied.json()["review"]["proposed_record"]
            return saved_record

        await save(payload, "create-split-custody")
        changed = {
            **payload,
            "external_ingress": {
                **payload["external_ingress"],
                "custodian": "Successor custodian",
                "evidence_url": "https://example.test/new-report",
            },
        }
        updated = await save(changed, "update-split-custody")
        self.assertEqual(
            updated["external_ingress"]["custodian"], changed["external_ingress"]["custodian"]
        )
        blocked = (await self.post(route, {**payload, "external_ingress": None})).json()["review"]
        self.assertEqual(blocked["status"], "blocked")
        self.store.write_ingress_route_audit_record(
            _ingress_audit_record(record_id="audit-all-managed", domains=self.target.domains)
        )
        managed = await save({**payload, "external_ingress": None}, "relinquish-split-custody")
        self.assertNotIn("external_ingress", managed)

    async def test_http_only_managed_alias_is_a_blocked_plan(self) -> None:
        payload = self.seed_split()
        self.store.write_ingress_route_audit_record(
            _ingress_audit_record(
                record_id="audit-alias",
                domains=("operator.example.test",),
                tls_owner="none",
                provider_certificate_ref="",
            )
        )
        response = await self.post("/v1/route-bindings/reconcile", payload)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(
            [finding["code"] for finding in response.json()["review"]["findings"]],
            ["managed_tls_ownership_required"],
        )

    async def test_managed_apply_grant_cannot_establish_split_custody(self) -> None:
        payload = self.seed_split()
        payload.pop("browser_review")
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_route_binding_policy(action="route_binding.apply"),
            record_store_factory=lambda: self.store,
        )
        response = await request(
            app,
            "POST",
            "/v1/route-bindings/reconcile",
            payload={
                **payload,
                "mode": "apply",
                "reason": "Recognize split custody.",
                "confirmation": RouteBindingReconcileEnvelope.model_validate(
                    payload
                ).expected_confirmation,
            },
            headers={
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "split-machine-apply",
            },
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_refresh_preserves_external_expiry_and_missing_alias_audit_blocks(self) -> None:
        payload = self.seed_split()
        evaluated_at = "2026-07-20T00:00:00Z"
        external = {**payload["external_ingress"], "attested_at": "2026-07-01T00:00:00Z"}
        planned = plan_route_binding_reconcile(
            record_store=self.store,
            request=RouteBindingReconcileRequest.model_validate(
                {
                    **{
                        key: value
                        for key, value in payload.items()
                        if key not in {"mode", "browser_review"}
                    },
                    "external_ingress": external,
                    "evaluated_at": evaluated_at,
                }
            ),
        )
        self.assertEqual(planned.status, "ready", planned.findings)
        assert planned.record is not None
        assert planned.record.external_ingress is not None
        self.assertEqual(planned.record.external_ingress.attested_at, external["attested_at"])
        with patch.object(self.store, "read_route_binding_record", return_value=planned.record):
            refresh = plan_route_binding_reconcile(
                record_store=self.store,
                request=RouteBindingReconcileRequest(
                    product=planned.record.product,
                    context=planned.record.context,
                    instance=planned.record.instance,
                    expected_current=RouteBindingExpectedCurrent(
                        state="present", record_sha256=route_binding_record_sha256(planned.record)
                    ),
                    evaluated_at="2026-07-20T13:00:00Z",
                ),
            )
        self.assertTrue(is_evidence_only_route_binding_refresh(refresh))
        assert refresh.record is not None
        self.assertEqual(refresh.record.external_ingress, planned.record.external_ingress)
        expired = plan_route_binding_reconcile(
            record_store=self.store,
            request=RouteBindingReconcileRequest.model_validate(
                {
                    **{
                        key: value
                        for key, value in payload.items()
                        if key not in {"mode", "browser_review"}
                    },
                    "external_ingress": external,
                    "evaluated_at": "2026-08-02T00:00:00Z",
                }
            ),
        )
        self.assertEqual(
            [finding.code for finding in expired.findings], ["external_attestation_not_current"]
        )
        self.store.write_ingress_route_audit_record(
            _ingress_audit_record(record_id="audit-alias", domains=("foreign.example.test",))
        )
        missing = await self.post("/v1/route-bindings/reconcile", payload)
        self.assertEqual(
            [finding["code"] for finding in missing.json()["review"]["findings"]],
            ["ingress_audit_missing"],
        )

    async def test_profile_change_during_commit_settles_without_binding(self) -> None:
        payload = self.seed_split()
        route = "/v1/route-bindings/reconcile"
        review = (await self.post(route, payload)).json()["review"]
        original = self.store.reconcile_route_binding_record

        def change_profile(*args: Any, **kwargs: Any) -> Any:
            profile = self.store.read_product_profile_record(payload["product"])
            self.store.write_product_profile_record(
                profile.model_copy(update={"display_name": "Changed after review"})
            )
            return original(*args, **kwargs)

        with patch.object(self.store, "reconcile_route_binding_record", side_effect=change_profile):
            response = await self.post(
                route,
                {
                    **payload,
                    "external_ingress": review["proposed_record"]["external_ingress"],
                    "mode": "apply",
                    "reason": "Record both routes.",
                    "confirmation": review["expected_confirmation"],
                    "browser_review": {
                        "product": payload["product"],
                        "reviewed_plan_sha256": review["plan_sha256"],
                    },
                },
                "split-profile-changed",
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "stale")
        with self.assertRaises(FileNotFoundError):
            self.store.read_route_binding_record(
                product=payload["product"],
                context_name=payload["context"],
                instance_name=payload["instance"],
            )
