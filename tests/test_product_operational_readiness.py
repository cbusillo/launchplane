from __future__ import annotations

from dataclasses import replace
import unittest

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.data_provenance import DataProvenance
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.driver_descriptor import DriverActionDescriptor
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.product_environment_read_model import (
    ProductManagedSecretConfigStatusItem,
    ProductRuntimeConfigStatusItem,
)
from control_plane.contracts.product_operational_readiness import (
    ProductOperationalReadinessInputs,
    build_product_operational_readiness,
    combine_operational_readiness_states,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_topology_read_model import (
    ProductDesiredTopology,
    ProductEnvironmentTopology,
    ProductObservedIngress,
    ProductObservedPlacement,
    ProductObservedTlsDomain,
    ProductObservedTopology,
    ProductProviderRecordedTopology,
    ProductTopologyWarning,
    ProductTopologyIngress,
    ProductTopologyPlacement,
    ProductTopologyTlsOwnership,
)
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    HealthcheckEvidence,
)
from control_plane.drivers.registry import read_driver_descriptor
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy
from tests.support.artifact_manifests import artifact_manifest_v2


_ARTIFACT_ID = "artifact-example-odoo-0123456789abcdef"
_REPLACEMENT_ARTIFACT_ID = "artifact-example-odoo-fedcba9876543210"
_ACTION = "odoo_target_replacement_plan.read"
_WORKFLOW_REF = "example/example-odoo/.github/workflows/replace.yml@refs/heads/main"
_JOB_WORKFLOW_REF = (
    "example/launchplane/.github/workflows/reusable-target-replacement.yml@"
    "0123456789abcdef0123456789abcdef01234567"
)


def _profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord.model_validate(
        {
            "product": "example-odoo",
            "display_name": "Example Odoo",
            "repository": "example/example-odoo",
            "driver_id": "odoo",
            "image": {"repository": "ghcr.io/example/example-odoo"},
            "lanes": [
                {
                    "instance": "testing",
                    "context": "example-odoo",
                    "base_url": "https://example-odoo.invalid",
                    "health_url": "https://example-odoo.invalid/launchplane/health",
                }
            ],
            "preview": {"enabled": False},
            "expected_config": {
                "runtime_environment_keys": [
                    {
                        "key": "ODOO_DB_NAME",
                        "context": "example-odoo",
                        "instance": "testing",
                    }
                ],
                "managed_secret_bindings": [
                    {
                        "binding_key": "ODOO_DB_PASSWORD",
                        "context": "example-odoo",
                        "instance": "testing",
                    }
                ],
            },
            "updated_at": "2026-07-23T09:00:00Z",
            "source": "test",
        }
    )


def _identity(*, instance: str = "testing") -> GitHubActionsIdentity:
    return GitHubActionsIdentity(
        repository="example/example-odoo",
        repository_owner="example",
        repository_id="1001",
        repository_owner_id="1000",
        workflow_ref=_WORKFLOW_REF,
        job_workflow_ref=_JOB_WORKFLOW_REF,
        ref="refs/heads/main",
        ref_type="branch",
        event_name="workflow_dispatch",
        environment=instance,
        subject="repo:example/example-odoo:ref:refs/heads/main",
        sha="89abcdef0123456789abcdef0123456789abcdef",
        raw_claims={},
    )


def _policy_record(
    *,
    instance: str = "testing",
    rule_overrides: dict[str, object] | None = None,
) -> LaunchplaneAuthzPolicyRecord:
    rule: dict[str, object] = {
        "managed_set_id": "test.operational-readiness",
        "managed_rule_id": "example-odoo.testing.replacement-plan",
        "repository": "example/example-odoo",
        "repository_id": "1001",
        "repository_owner_id": "1000",
        "workflow_refs": [_WORKFLOW_REF],
        "job_workflow_refs": [_JOB_WORKFLOW_REF],
        "event_names": ["workflow_dispatch"],
        "products": ["example-odoo"],
        "contexts": ["example-odoo"],
        "instances": [instance],
        "actions": [_ACTION],
    }
    rule.update(rule_overrides or {})
    return LaunchplaneAuthzPolicyRecord(
        record_id="launchplane-authz-policy-ready",
        revision=12,
        source="test",
        updated_at="2026-07-23T09:01:00Z",
        policy=LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "github_actions": [rule],
            }
        ),
    )


def _action() -> DriverActionDescriptor:
    return DriverActionDescriptor(
        action_id="target_replacement_plan",
        label="Plan stable target replacement",
        description="Plan replacement from Launchplane-owned evidence.",
        safety="read",
        scope="instance",
        method="POST",
        route_path="/v1/drivers/odoo/target-replacement-plan",
        authz_action=_ACTION,
        readiness_requirements=(
            "provider_target",
            "route_binding",
            "runtime_environment",
            "managed_secrets",
            "artifact",
            "deployment",
            "topology",
        ),
    )


def _deployment() -> DeploymentRecord:
    return DeploymentRecord(
        record_id="deployment-example-odoo-testing",
        artifact_identity=ArtifactIdentityReference(artifact_id=_ARTIFACT_ID),
        context="example-odoo",
        instance="testing",
        source_git_ref="89abcdef0123456789abcdef0123456789abcdef",
        deploy=DeploymentEvidence(
            target_name="example-odoo-testing",
            target_type="compose",
            deploy_mode="runtime-provider-api",
            deployment_id="provider-deployment-example-odoo-testing",
            status="pass",
            started_at="2026-07-23T08:55:00Z",
            finished_at="2026-07-23T08:57:00Z",
        ),
        destination_health=HealthcheckEvidence(
            verified=True,
            urls=("https://example-odoo.invalid/launchplane/health",),
            timeout_seconds=30,
            status="pass",
            runtime_identity_status="match",
        ),
    )


def _topology() -> ProductEnvironmentTopology:
    return ProductEnvironmentTopology(
        desired=ProductDesiredTopology(trust_state="recorded"),
        provider_recorded=ProductProviderRecordedTopology(
            authority_status="active",
            placement=ProductTopologyPlacement(
                provider="dokploy",
                target_type="compose",
                target_name="example-odoo-testing",
                provider_target_type="compose",
                provider_target_record_present=True,
                trust_state="recorded",
            ),
            ingress=ProductTopologyIngress(
                provider="npmplus",
                endpoint_key="example-nonprod",
                termination_kind="edge",
                path="edge_to_provider",
                trust_state="recorded",
            ),
            tls=ProductTopologyTlsOwnership(
                owner="provider",
                terminator="provider",
                provider="npmplus",
                trust_state="recorded",
            ),
            trust_state="recorded",
        ),
        observed=ProductObservedTopology(
            placement=ProductObservedPlacement(
                runtime_identity_status="match",
                trust_state="verified",
            ),
            ingress=ProductObservedIngress(status="pass", trust_state="verified"),
            tls_domains=(
                ProductObservedTlsDomain(
                    domain_name="example-odoo.invalid",
                    status="valid",
                    public_name_match=True,
                    trust_state="verified",
                ),
            ),
            trust_state="verified",
        ),
        trust_state="recorded",
    )


def _inputs() -> ProductOperationalReadinessInputs:
    profile = _profile()
    artifact_manifest = artifact_manifest_v2(
        artifact_id=_ARTIFACT_ID,
        image_repository="ghcr.io/example/example-odoo",
    )
    return ProductOperationalReadinessInputs(
        profile=profile,
        lane=profile.lanes[0],
        requested_action=_ACTION,
        action=_action(),
        identity=_identity(),
        active_policy_records=(_policy_record(),),
        lane_summary=LaunchplaneLaneSummary(
            context="example-odoo",
            instance="testing",
            artifact_manifest=artifact_manifest,
            latest_deployment=_deployment(),
            provider_target=ProviderTargetRecord(
                context="example-odoo",
                instance="testing",
                provider_id="dokploy",
                target_category="compose",
                target_id="provider-private-id",
                display_name="example-odoo-testing",
                provider_target_type="compose",
                provider_evidence={"host_id": "provider-private-host"},
                updated_at="2026-07-23T08:50:00Z",
                source_label="test",
            ),
            provenance=DataProvenance(
                source_kind="record",
                source_record_id="example-odoo/testing",
                recorded_at="2026-07-23T08:57:00Z",
                refreshed_at="2026-07-23T08:57:00Z",
                freshness_status="verified",
                detail="Current generic test evidence.",
            ),
        ),
        runtime_settings=(
            ProductRuntimeConfigStatusItem(
                key="ODOO_DB_NAME",
                status="configured",
                context="example-odoo",
                instance="testing",
                source_label="test",
                updated_at="2026-07-23T08:45:00Z",
                trust_state="recorded",
            ),
        ),
        managed_secrets=(
            ProductManagedSecretConfigStatusItem(
                binding_key="ODOO_DB_PASSWORD",
                status="configured",
                integration="runtime_environment",
                context="example-odoo",
                instance="testing",
                updated_at="2026-07-23T08:46:00Z",
                trust_state="recorded",
            ),
        ),
        topology=_topology(),
        requested_artifact_id=_ARTIFACT_ID,
        expected_current_artifact_id=_ARTIFACT_ID,
        current_inventory_artifact_id=_ARTIFACT_ID,
        artifact_manifest=artifact_manifest,
        generated_at="2026-07-23T09:05:00Z",
    )


class ProductOperationalReadinessTests(unittest.TestCase):
    def test_ready_result_uses_exact_managed_workflow_and_redacts_runtime_authority(self) -> None:
        readiness = build_product_operational_readiness(_inputs())

        self.assertTrue(readiness.ready)
        self.assertEqual(readiness.state, "ready")
        self.assertEqual(readiness.action.requested_action, _ACTION)
        self.assertEqual(readiness.caller.identity_type, "github_actions")
        self.assertEqual(readiness.caller.job_workflow_ref, _JOB_WORKFLOW_REF)
        self.assertTrue(all(dimension.state == "ready" for dimension in readiness.dimensions))

        payload = readiness.model_dump_json()
        self.assertNotIn("provider-private-id", payload)
        self.assertNotIn("provider-private-host", payload)
        self.assertNotIn("secret_id", payload)
        self.assertNotIn("ciphertext", payload)
        self.assertNotIn("subject", payload)

    def test_provider_target_uses_own_record_instead_of_lane_provenance(self) -> None:
        inputs = _inputs()
        lane_summary = inputs.lane_summary
        assert lane_summary is not None
        missing_provenance = DataProvenance(
            source_kind="record",
            freshness_status="missing",
            detail="Raw storage summary has not been enriched.",
        )

        readiness = build_product_operational_readiness(
            replace(
                inputs,
                lane_summary=lane_summary.model_copy(update={"provenance": missing_provenance}),
            )
        )

        provider_target = next(
            dimension
            for dimension in readiness.dimensions
            if dimension.dimension == "provider_target"
        )
        self.assertEqual(provider_target.state, "ready")
        self.assertEqual(provider_target.evidence[0].freshness_status, "recorded")

    def test_target_replacement_remains_ready_for_broken_live_deployment(self) -> None:
        inputs = _inputs()
        lane_summary = inputs.lane_summary
        assert lane_summary is not None
        deployment = lane_summary.latest_deployment
        assert deployment is not None
        failed_health = deployment.destination_health.model_copy(
            update={
                "verified": False,
                "status": "fail",
                "runtime_identity_status": "unchecked",
            }
        )
        topology = inputs.topology.model_copy(
            update={
                "warnings": (
                    ProductTopologyWarning(
                        code="public_ingress_failure",
                        scope="observation",
                        severity="error",
                        detail="The current public ingress observation failed.",
                    ),
                )
            }
        )
        descriptor = read_driver_descriptor("odoo")
        action = next(
            action for action in descriptor.actions if action.action_id == "target_replacement_plan"
        )

        readiness = build_product_operational_readiness(
            replace(
                inputs,
                action=action,
                lane_summary=lane_summary.model_copy(
                    update={
                        "latest_deployment": deployment.model_copy(
                            update={"destination_health": failed_health}
                        )
                    }
                ),
                topology=topology,
            )
        )

        self.assertTrue(readiness.ready)
        dimensions = {dimension.dimension: dimension for dimension in readiness.dimensions}
        self.assertEqual(dimensions["route_binding"].state, "ready")
        self.assertNotIn("deployment", dimensions)
        self.assertNotIn("topology", dimensions)

    def test_warning_severity_controls_route_and_topology_readiness(self) -> None:
        cases = (
            (
                ProductTopologyWarning(
                    code="external_ingress_internals_unsupported",
                    scope="ingress",
                    severity="warning",
                    detail="External ingress internals are outside Launchplane authority.",
                ),
                "ready",
                "ready",
            ),
            (
                ProductTopologyWarning(
                    code="placement_divergence",
                    scope="placement",
                    severity="error",
                    detail="Recorded placement does not match current authority.",
                ),
                "blocked",
                "blocked",
            ),
            (
                ProductTopologyWarning(
                    code="public_ingress_failure",
                    scope="observation",
                    severity="error",
                    detail="The current public ingress observation failed.",
                ),
                "ready",
                "blocked",
            ),
        )
        for warning, expected_route_state, expected_topology_state in cases:
            with self.subTest(warning=warning.code, severity=warning.severity):
                inputs = _inputs()
                topology = inputs.topology.model_copy(update={"warnings": (warning,)})

                readiness = build_product_operational_readiness(replace(inputs, topology=topology))

                dimensions = {dimension.dimension: dimension for dimension in readiness.dimensions}
                self.assertEqual(dimensions["route_binding"].state, expected_route_state)
                self.assertEqual(dimensions["topology"].state, expected_topology_state)
                if warning.scope != "observation":
                    self.assertIn(warning.code, dimensions["route_binding"].details)
                self.assertIn(warning.code, dimensions["topology"].details)

    def test_deployment_and_topology_keep_runtime_identity_ownership_separate(self) -> None:
        inputs = _inputs()
        lane_summary = inputs.lane_summary
        assert lane_summary is not None
        deployment = lane_summary.latest_deployment
        assert deployment is not None
        unchecked_health = deployment.destination_health.model_copy(
            update={
                "verified": False,
                "urls": (),
                "timeout_seconds": None,
                "status": "skipped",
                "runtime_identity_status": "unchecked",
            }
        )
        topology = inputs.topology.model_copy(
            update={
                "observed": inputs.topology.observed.model_copy(
                    update={
                        "placement": ProductObservedPlacement(
                            runtime_identity_status="unchecked",
                            trust_state="missing",
                        )
                    }
                )
            }
        )
        self.assertEqual(topology.trust_state, "recorded")
        self.assertEqual(topology.observed.ingress.trust_state, "verified")

        readiness = build_product_operational_readiness(
            replace(
                inputs,
                lane_summary=lane_summary.model_copy(
                    update={
                        "latest_deployment": deployment.model_copy(
                            update={"destination_health": unchecked_health}
                        )
                    }
                ),
                topology=topology,
            )
        )

        dimensions = {dimension.dimension: dimension for dimension in readiness.dimensions}
        self.assertEqual(dimensions["deployment"].state, "blocked")
        self.assertIn(
            "runtime_identity_status:unchecked",
            dimensions["deployment"].details,
        )
        self.assertEqual(dimensions["topology"].state, "ready")

    def test_stale_passing_deployment_remains_stale(self) -> None:
        inputs = _inputs()
        lane_summary = inputs.lane_summary
        assert lane_summary is not None

        readiness = build_product_operational_readiness(
            replace(
                inputs,
                lane_summary=lane_summary.model_copy(
                    update={
                        "provenance": lane_summary.provenance.model_copy(
                            update={"freshness_status": "stale"}
                        )
                    }
                ),
            )
        )

        deployment = next(
            dimension for dimension in readiness.dimensions if dimension.dimension == "deployment"
        )
        self.assertEqual(deployment.state, "stale")
        self.assertFalse(readiness.ready)

    def test_replacement_artifact_is_independent_from_current_deployment(self) -> None:
        inputs = _inputs()
        replacement_manifest = artifact_manifest_v2(
            artifact_id=_REPLACEMENT_ARTIFACT_ID,
            image_repository="ghcr.io/example/example-odoo",
        )

        readiness = build_product_operational_readiness(
            replace(
                inputs,
                requested_artifact_id=_REPLACEMENT_ARTIFACT_ID,
                artifact_manifest=replacement_manifest,
            )
        )

        self.assertTrue(readiness.ready)
        self.assertEqual(readiness.state, "ready")
        states = {dimension.dimension: dimension.state for dimension in readiness.dimensions}
        self.assertEqual(states["artifact"], "ready")
        self.assertEqual(states["deployment"], "ready")

    def test_stale_current_artifact_snapshot_blocks_readiness(self) -> None:
        readiness = build_product_operational_readiness(
            replace(
                _inputs(),
                expected_current_artifact_id="artifact-example-odoo-stale",
            )
        )

        deployment = next(
            dimension for dimension in readiness.dimensions if dimension.dimension == "deployment"
        )
        self.assertEqual(deployment.state, "blocked")
        self.assertFalse(readiness.ready)

    def test_missing_inventory_blocks_expected_current_artifact_readiness(self) -> None:
        readiness = build_product_operational_readiness(
            replace(
                _inputs(),
                current_inventory_artifact_id=None,
            )
        )

        deployment = next(
            dimension for dimension in readiness.dimensions if dimension.dimension == "deployment"
        )
        self.assertEqual(deployment.state, "missing")
        self.assertEqual(deployment.owner_record_type, "environment_inventory")
        self.assertFalse(readiness.ready)

    def test_candidate_artifact_must_match_product_image_repository(self) -> None:
        replacement_manifest = artifact_manifest_v2(
            artifact_id=_REPLACEMENT_ARTIFACT_ID,
            image_repository="ghcr.io/example/other-product",
        )

        readiness = build_product_operational_readiness(
            replace(
                _inputs(),
                requested_artifact_id=_REPLACEMENT_ARTIFACT_ID,
                artifact_manifest=replacement_manifest,
            )
        )

        artifact = next(
            dimension for dimension in readiness.dimensions if dimension.dimension == "artifact"
        )
        self.assertEqual(artifact.state, "blocked")
        self.assertFalse(readiness.ready)

    def test_wrong_instance_authorization_blocks_otherwise_ready_lane(self) -> None:
        inputs = replace(_inputs(), active_policy_records=(_policy_record(instance="prod"),))

        readiness = build_product_operational_readiness(inputs)

        self.assertFalse(readiness.ready)
        self.assertEqual(readiness.state, "blocked")
        authorization = next(
            dimension
            for dimension in readiness.dimensions
            if dimension.dimension == "authorization"
        )
        self.assertEqual(authorization.state, "blocked")

    def test_top_level_workflow_without_reusable_identity_is_blocked(self) -> None:
        inputs = replace(
            _inputs(),
            identity=replace(_identity(), job_workflow_ref=""),
        )

        readiness = build_product_operational_readiness(inputs)

        authorization = next(
            dimension
            for dimension in readiness.dimensions
            if dimension.dimension == "authorization"
        )
        self.assertEqual(authorization.state, "blocked")
        self.assertFalse(readiness.ready)

    def test_singleton_mutable_reusable_identity_is_blocked(self) -> None:
        mutable_job_workflow_ref = (
            "example/launchplane/.github/workflows/reusable-target-replacement.yml@refs/heads/main"
        )
        inputs = replace(
            _inputs(),
            identity=replace(
                _identity(),
                job_workflow_ref=mutable_job_workflow_ref,
            ),
            active_policy_records=(
                _policy_record(rule_overrides={"job_workflow_refs": [mutable_job_workflow_ref]}),
            ),
        )

        readiness = build_product_operational_readiness(inputs)

        authorization = next(
            dimension
            for dimension in readiness.dimensions
            if dimension.dimension == "authorization"
        )
        self.assertEqual(authorization.state, "blocked")
        self.assertFalse(readiness.ready)

    def test_overbroad_managed_rule_selectors_never_report_exact_readiness(self) -> None:
        cases: tuple[tuple[str, dict[str, object]], ...] = (
            ("wildcard instance", {"instances": ["*"]}),
            ("multiple instances", {"instances": ["testing", "prod"]}),
            ("multiple products", {"products": ["example-odoo", "other-product"]}),
            ("multiple contexts", {"contexts": ["example-odoo", "other-context"]}),
            ("multiple actions", {"actions": [_ACTION, "product_environment.read"]}),
            (
                "multiple caller workflows",
                {
                    "workflow_refs": [
                        _WORKFLOW_REF,
                        "example/example-odoo/.github/workflows/other.yml@refs/heads/main",
                    ]
                },
            ),
            (
                "mutable reusable workflow",
                {
                    "job_workflow_refs": [
                        _JOB_WORKFLOW_REF,
                        "example/launchplane/.github/workflows/reusable-target-replacement.yml@refs/heads/main",
                    ]
                },
            ),
        )
        for label, rule_overrides in cases:
            with self.subTest(label=label):
                inputs = replace(
                    _inputs(),
                    active_policy_records=(_policy_record(rule_overrides=rule_overrides),),
                )

                readiness = build_product_operational_readiness(inputs)

                states = {
                    dimension.dimension: dimension.state for dimension in readiness.dimensions
                }
                self.assertEqual(states["authorization"], "blocked")
                self.assertFalse(readiness.ready)

    def test_missing_route_and_config_fail_closed(self) -> None:
        inputs = _inputs()
        missing_topology = inputs.topology.model_copy(
            update={
                "provider_recorded": ProductProviderRecordedTopology(),
                "trust_state": "missing",
            }
        )
        inputs = replace(
            inputs,
            runtime_settings=(),
            managed_secrets=(),
            topology=missing_topology,
        )

        readiness = build_product_operational_readiness(inputs)

        self.assertFalse(readiness.ready)
        self.assertEqual(readiness.state, "missing")
        states = {dimension.dimension: dimension.state for dimension in readiness.dimensions}
        self.assertEqual(states["route_binding"], "missing")
        self.assertEqual(states["runtime_environment"], "missing")
        self.assertEqual(states["managed_secrets"], "missing")

    def test_unsupported_action_is_never_ready(self) -> None:
        inputs = replace(_inputs(), action=None, requested_action="unknown.execute")

        readiness = build_product_operational_readiness(inputs)

        self.assertFalse(readiness.ready)
        self.assertEqual(readiness.state, "unsupported")
        self.assertEqual(readiness.dimensions[1].dimension, "action")
        self.assertEqual(readiness.dimensions[1].state, "unsupported")

    def test_state_combiner_never_treats_empty_or_non_ready_inputs_as_ready(self) -> None:
        self.assertEqual(combine_operational_readiness_states(()), "unsupported")
        self.assertEqual(
            combine_operational_readiness_states(("ready", "stale", "missing")),
            "missing",
        )
        self.assertEqual(
            combine_operational_readiness_states(("ready", "missing", "blocked")),
            "blocked",
        )


if __name__ == "__main__":
    unittest.main()
