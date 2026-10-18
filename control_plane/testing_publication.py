"""Plan canonical adoption from the prepared copy, without reading secret values."""

from datetime import datetime

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.promotion_record import ArtifactIdentityReference, DeploymentEvidence
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    RouteBindingDomain,
    RouteBindingIngress,
    RouteBindingProviderTarget,
    RouteBindingSource,
    RouteBindingTls,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.testing_candidate import TestingCandidateAllocation
from control_plane.contracts.testing_publication import (
    TestingPublicationPlan,
    TestingServingSnapshot,
)
from control_plane.dokploy.testing_route import TestingLaneRoute
from control_plane.odoo_testing_restore import FRESH_TESTING_LOCAL_CREDENTIAL_KEYS
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import (
    RuntimeEnvironmentSetExpectation,
    SecretDeliverySetExpectation,
)
from control_plane.testing_candidate_allocation import candidate_resources
from control_plane.testing_candidate_runtime import (
    neutralized_candidate_overrides,
    candidate_authority_sha256,
    candidate_runtime_fences,
)


def plan_testing_publication(
    store: PostgresRecordStore,
    *,
    candidate: TestingCandidateAllocation,
    before: TestingServingSnapshot,
    route: TestingLaneRoute,
    reservation: LaunchplaneIdempotencyRecord,
    now: datetime,
) -> TestingPublicationPlan:
    app = candidate.application
    if app is None or app.prepared is None or app.phase != "prepared":
        raise ValueError("Publication planning requires a prepared candidate.")
    plan = app.plan
    stamp = now.isoformat()
    profile = store.read_product_profile_record(candidate.product)
    key = canonical_json_sha256(
        {
            "product": candidate.product,
            "context": candidate.context,
            "operation": candidate.operation_id,
        }
    )
    target = before.target.model_copy(
        update={
            "project_name": candidate.namespace,
            "target_name": candidate.namespace,
            "source_type": "raw",
            "env": {},
            "domains": (),
            "public_hosts": (),
            "pending_public_host_effects": (),
            "updated_at": stamp,
            "source_label": "service:testing-rebuild",
        }
    )
    identity = before.target_id.model_copy(
        update={
            "target_id": candidate.compose_id,
            "updated_at": stamp,
            "source_label": "service:testing-rebuild",
        }
    )
    provider = before.provider.model_copy(
        update={
            "target_id": candidate.compose_id,
            "display_name": candidate.namespace,
            "provider_evidence": {"project_name": candidate.namespace},
            "updated_at": stamp,
            "source_label": "service:testing-rebuild",
        }
    )
    old_runtime = before.runtime
    env = serving_runtime_values(candidate, old_runtime, runtime_port=profile.runtime_port)
    runtime = RuntimeEnvironmentRecord(
        scope="instance",
        context=candidate.context,
        instance="testing",
        env=env,
        # Old provider-env retirement remains relevant to the new compose too.
        schema_version=old_runtime.schema_version if old_runtime else 1,
        retired_provider_keys=old_runtime.retired_provider_keys if old_runtime else (),
        updated_at=stamp,
        source_label="service:testing-rebuild",
    )
    overrides = neutralized_candidate_overrides(
        before.overrides, context=candidate.context, base_url=plan.base_url, updated_at=stamp
    )
    old_binding = before.binding
    binding = EnvironmentRouteBindingRecord(
        product=candidate.product,
        context=candidate.context,
        instance="testing",
        provider_target=RouteBindingProviderTarget(
            provider_id="dokploy",
            target_category="compose",
            provider_target_type="compose",
            target_name=candidate.namespace,
        ),
        domains=old_binding.domains
        if old_binding
        else (RouteBindingDomain(domain_name=route.host),),
        ingress=old_binding.ingress
        if old_binding
        else RouteBindingIngress(provider="traefik", termination_kind="direct"),
        tls=old_binding.tls if old_binding else RouteBindingTls(owner="provider"),
        source=RouteBindingSource(
            source_kind="service",
            source_label="service:testing-rebuild",
            source_record_ids=(candidate.namespace,),
            source_versions={"testing_publication": key, "testing_route": route.digest},
            refreshed_at=stamp,
        ),
        updated_at=stamp,
    )
    artifact = ArtifactIdentityReference(
        artifact_id=plan.artifact.artifact_id, manifest_version=plan.artifact.schema_version
    )
    deployed = DeploymentEvidence(
        target_name=candidate.namespace,
        target_type="compose",
        deploy_mode="testing-rebuild",
        deployment_id=app.deployment_id,
        status="pass",
        started_at=candidate.created_at.isoformat(),
        finished_at=app.prepared.observed_at.isoformat(),
    )
    deployment = DeploymentRecord(
        record_id=plan.runtime_identity.deployment_record_id,
        artifact_identity=artifact,
        context=candidate.context,
        instance="testing",
        source_git_ref=plan.artifact.source_commit,
        resolved_target=ResolvedTargetEvidence(
            target_type="compose", target_id=candidate.compose_id, target_name=candidate.namespace
        ),
        runtime_identity=plan.runtime_identity,
        deploy=deployed,
        runtime_source={"testing_publication": key, "testing_candidate": candidate.namespace},
    )
    inventory = EnvironmentInventory(
        context=candidate.context,
        instance="testing",
        artifact_identity=artifact,
        source_git_ref=plan.artifact.source_commit,
        deploy=deployed,
        runtime_identity=plan.runtime_identity,
        updated_at=stamp,
        deployment_record_id=deployment.record_id,
    )
    selected_runtime = RuntimeEnvironmentSetExpectation(
        contexts=(candidate.context,),
        instances=((candidate.context, "testing"),),
        include_global=False,
        records=(),
    )
    selected_runtime = selected_runtime.model_copy(
        update={
            "records": tuple(
                item
                for item in store.list_runtime_environment_records()
                if selected_runtime.includes(item)
            )
        }
    )
    return TestingPublicationPlan(
        mutation_record_id=reservation.record_id,
        provider_target_key=reservation.provider_target_key,
        profile=profile,
        candidate=candidate,
        before=before,
        after=TestingServingSnapshot(
            target=target,
            target_id=identity,
            provider=provider,
            runtime=runtime,
            overrides=overrides,
            inventory=inventory,
            binding=binding,
            generation=plan.generation.model_copy(update={"completed_at": stamp})
            if plan.generation
            else None,
        ),
        deployment=deployment,
        route=route,
        runtime_read_set=selected_runtime,
        secret_read_set=SecretDeliverySetExpectation(
            integration="runtime_environment",
            context=candidate.context,
            instance="testing",
            records=store.list_secret_records(
                integration="runtime_environment", context_name=candidate.context
            ),
            bindings=store.list_secret_bindings(
                integration="runtime_environment", context_name=candidate.context
            ),
        ),
        created_at=now,
    )


def require_original_candidate_inputs(plan: TestingPublicationPlan) -> None:
    """Bind publication's read sets back to the private copy's original inputs."""
    app = plan.candidate.application
    assert app is not None
    credentials: list[dict[str, object]] = []
    for key in sorted(FRESH_TESTING_LOCAL_CREDENTIAL_KEYS):
        pairs = [
            (binding, record)
            for binding in plan.secret_read_set.bindings
            for record in plan.secret_read_set.records
            if binding.binding_key == key
            and binding.secret_id == record.secret_id
            and binding.status == record.status == "configured"
            and record.scope == "context_instance"
            and (binding.context, binding.instance, record.context, record.instance)
            == (plan.candidate.context, "testing", plan.candidate.context, "testing")
        ]
        if len(pairs) != 1:
            raise ValueError("Publication requires the exact generated credential metadata.")
        binding, record = pairs[0]
        if binding.value_source != "generated" or binding.declared_secret_class != "testing":
            raise ValueError("Publication cannot replace generated testing credentials.")
        credentials.append(
            {
                "binding": binding.model_dump(mode="json"),
                "record": record.model_dump(mode="json"),
                "version": {"version_id": record.current_version_id, "secret_id": record.secret_id},
            }
        )
    if plan.after.runtime is None or canonical_json_sha256(
        plan.after.runtime.env
    ) != canonical_json_sha256(
        serving_runtime_values(
            plan.candidate, plan.before.runtime, runtime_port=plan.profile.runtime_port
        )
    ):
        raise ValueError(
            "Publication must retain resource selection without materialized passwords."
        )
    target_changes = {
        "project_name",
        "target_name",
        "source_type",
        "env",
        "domains",
        "public_hosts",
        "pending_public_host_effects",
        "updated_at",
        "source_label",
    }
    if plan.after.target.model_dump(exclude=target_changes) != plan.before.target.model_dump(
        exclude=target_changes
    ):
        raise ValueError("Publication cannot alter testing policy or unrelated target intent.")
    credential_sha = canonical_json_sha256(credentials)
    overrides = neutralized_candidate_overrides(
        plan.before.overrides,
        context=plan.candidate.context,
        base_url=app.plan.base_url,
        updated_at=plan.candidate.created_at.isoformat(),
    )
    if plan.after.overrides is None or canonical_json_sha256(
        plan.after.overrides.model_dump(
            mode="json", exclude={"last_apply", "updated_at", "source_label"}
        )
    ) != canonical_json_sha256(
        overrides.model_dump(mode="json", exclude={"last_apply", "updated_at", "source_label"})
    ):
        raise ValueError("Publication must retain exactly the neutralized testing intent.")
    authority = candidate_authority_sha256(
        profile=plan.profile,
        target=plan.before.target,
        runtime_records=plan.runtime_read_set.records,
        credential_binding=credential_sha,
        native=app.plan.native_source,
        upstream=app.plan.upstream_source,
        overrides=overrides,
    )
    if (
        credential_sha != app.plan.credential_bindings_sha256
        or authority != app.plan.authority_sha256
    ):
        raise ValueError("Publication inputs differ from the original private candidate.")


def serving_runtime_values(
    candidate: TestingCandidateAllocation,
    record: RuntimeEnvironmentRecord | None,
    *,
    runtime_port: int,
) -> dict[str, str | int | float | bool]:
    app = candidate.application
    assert app is not None
    env = dict(record.env) if record else {}
    for name in (*app.plan.omitted_runtime_keys, *FRESH_TESTING_LOCAL_CREDENTIAL_KEYS):
        env.pop(name, None)
    env.update(candidate_resources(candidate).volumes)
    env.update(candidate_runtime_fences(candidate, runtime_port=runtime_port))
    return env
