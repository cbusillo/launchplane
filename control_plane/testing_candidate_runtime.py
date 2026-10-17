"""Compile an admitted Odoo copy without changing the serving lane's records."""

from dataclasses import dataclass, field
from collections.abc import Iterable
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from control_plane.contracts.artifact_identity import artifact_manifest_matches_image_repository
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.odoo_instance_override_record import (
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
)
from control_plane.contracts.odoo_stable_target_replacement import (
    missing_required_odoo_modules_from_artifact,
)
from control_plane.contracts.odoo_testing_source import NativeOdooTestingSource
from control_plane.contracts.odoo_runtime_environment import merge_required_odoo_addons_path
from control_plane.contracts.runtime_identity import RuntimeIdentity, runtime_identity_env
from control_plane.contracts.testing_candidate import TestingCandidateAllocation
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.odoo_testing_source import NativeOdooSourceAuthority
from control_plane.contracts.testing_candidate_application import TestingCandidateApplicationPlan
from control_plane.dokploy import compose, post_deploy
from control_plane.dokploy.source import DokployTargetDefinition
from control_plane.integration_readback import INTEGRATION_FAMILIES
from control_plane.odoo_connection_isolation import CONNECTION_ISOLATION_PARAMETERS
from control_plane.odoo_instance_overrides import build_post_deploy_environment
from control_plane.odoo_testing_native_source import (
    native_testing_source_authority,
    recorded_native_testing_capture,
    resolve_native_testing_source,
)
from control_plane.odoo_testing_restore import (
    FRESH_TESTING_LOCAL_CREDENTIAL_KEYS,
    FreshOdooTestingGeneration,
    preflight_fresh_testing_intent,
)
from control_plane.runtime_environments import (
    resolve_site_runtime_environment,
    site_delivered_secret_bindings_from_store,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import RuntimeEnvironmentSetExpectation
from control_plane.testing_candidate_allocation import (
    candidate_resources,
    require_candidate_target_custody,
)
from control_plane.testing_replacement_bootstrap import (
    testing_credential_omissions,
    require_testing_credential_safety,
)

CandidateSource = Literal["empty", "production", "upstream"]


@dataclass(frozen=True)
class TestingCandidateRuntimeInputs:
    plan: TestingCandidateApplicationPlan
    target: DokployTargetDefinition
    compose_file: str
    runtime_environment: dict[str, str] = field(repr=False)
    workflow_environment: dict[str, str] = field(repr=False)
    required_workflow_keys: tuple[str, ...]
    native_source: NativeOdooTestingSource | None = field(default=None, repr=False)


def candidate_application_intent(
    *, product: str, context: str, artifact_id: str, source: CandidateSource
) -> str:
    """The worker binds its selected build and source before allocating storage."""
    return canonical_json_sha256(
        {"product": product, "context": context, "artifact": artifact_id, "source": source}
    )


def neutralized_candidate_overrides(
    record: OdooInstanceOverrideRecord | None, *, context: str, base_url: str, updated_at: str
) -> OdooInstanceOverrideRecord:
    """Project testing intent for a clean copy without rewriting the live record.

    Integration connections and secret carriers are not reapplied after SQL
    neutralization. Existing literal application settings and website bootstrap
    remain, with the same public origin and the established connection fences.
    """
    if record is None:
        record = OdooInstanceOverrideRecord(
            context=context, instance="testing", updated_at=updated_at
        )
    if (record.context, record.instance) != (context, "testing"):
        raise ValueError("Candidate overrides belong to another lane.")
    forbidden = {key for family in INTEGRATION_FAMILIES for key in family.config_parameters}
    forced = {
        **CONNECTION_ISOLATION_PARAMETERS,
        "web.base.url": base_url,
        "web.base.url.freeze": "True",
    }
    parameters = tuple(
        item
        for item in record.config_parameters
        if item.value.source == "literal"
        and item.key not in forbidden
        and item.key not in forced
        and not item.key.startswith(("google_analytics.", "plausible.", "google_tag_manager."))
    )
    parameters += tuple(
        OdooConfigParameterOverride(key=key, value=OdooOverrideValue(source="literal", value=value))
        for key, value in forced.items()
    )
    return record.model_copy(
        update={
            "config_parameters": parameters,
            "addon_settings": tuple(
                item
                for item in record.addon_settings
                if item.addon != "shopify" and item.value.source == "literal"
            ),
            "website_bootstrap": record.website_bootstrap.model_copy(
                update={"canonical_url": base_url}
            )
            if record.website_bootstrap is not None
            else None,
            "updated_at": updated_at,
            "source_label": "service:testing-candidate",
        }
    )


def candidate_runtime_inputs(
    *,
    root: Path,
    store: PostgresRecordStore,
    allocation: TestingCandidateAllocation,
    artifact_id: str,
    source: CandidateSource,
) -> TestingCandidateRuntimeInputs:
    """Read managed inputs only; no secret write, target adoption or live mutation."""
    if allocation.intent_sha256 != candidate_application_intent(
        product=allocation.product,
        context=allocation.context,
        artifact_id=artifact_id,
        source=source,
    ):
        raise ValueError("Candidate build/source differs from its original admitted operation.")
    resources = candidate_resources(allocation)
    if allocation.resources_binding_sha256 != resources.binding_sha256:
        raise ValueError("Candidate needs its independent storage receipt first.")
    require_candidate_target_custody(store, allocation)
    profile = store.read_product_profile_record(allocation.product)
    lane = next(
        lane
        for lane in profile.lanes
        if (lane.context, lane.instance) == (allocation.context, "testing")
    )
    artifact = store.read_artifact_manifest(artifact_id)
    if not artifact_manifest_matches_image_repository(
        artifact, expected_repository=profile.image.repository
    ) or missing_required_odoo_modules_from_artifact(artifact):
        raise ValueError("Candidate requires its product's admitted Odoo artifact/modules.")
    omitted = testing_credential_omissions(store, allocation.context, None)
    require_testing_credential_safety(store, allocation.context, omitted)
    delivered = site_delivered_secret_bindings_from_store(
        record_store=store, context_name=allocation.context, instance_name="testing"
    )
    credentials: list[dict[str, object]] = []
    for key in sorted(FRESH_TESTING_LOCAL_CREDENTIAL_KEYS):
        binding = delivered.get(key)
        if (
            binding is None
            or binding.value_source != "generated"
            or binding.declared_secret_class != "testing"
        ):
            raise ValueError(
                "Candidate requires the existing service-generated testing-local credentials."
            )
        record = store.read_secret_record(binding.secret_id)
        version = store.read_secret_version(record.current_version_id)
        if (record.context, record.instance, record.scope) != (
            allocation.context,
            "testing",
            "context_instance",
        ) or version.secret_id != record.secret_id:
            raise ValueError("Candidate local credential provenance is inconsistent.")
        credentials.append(
            {
                "binding": binding.model_dump(mode="json"),
                "record": record.model_dump(mode="json"),
                "version": {"version_id": version.version_id, "secret_id": version.secret_id},
            }
        )
    credential_binding = canonical_json_sha256(credentials)
    runtime = resolve_site_runtime_environment(
        control_plane_root=root,
        database_url=store.database_url,
        context_name=allocation.context,
        instance_name="testing",
        target_environment_override={},
        secret_binding_keys=FRESH_TESTING_LOCAL_CREDENTIAL_KEYS,
    )
    if any(not runtime.values.get(key) for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS):
        raise ValueError("Candidate local credentials are unavailable.")
    target = store.read_dokploy_target_record(
        context_name=allocation.context, instance_name="testing"
    )
    try:
        original_overrides = store.read_odoo_instance_override_record(
            context_name=allocation.context, instance_name="testing"
        )
    except FileNotFoundError:
        original_overrides = None
    overrides = neutralized_candidate_overrides(
        original_overrides,
        context=allocation.context,
        base_url=lane.base_url.rstrip("/"),
        updated_at=allocation.created_at.isoformat(),
    )
    workflow = build_post_deploy_environment(overrides, record_store=store)
    modules = ",".join(artifact.odoo_install_modules)
    preflight_fresh_testing_intent(
        modules=modules,
        encoded_payload=workflow.inline_environment.get("ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64", ""),
    )
    native: NativeOdooTestingSource | None = None
    authority = None
    upstream: dict[str, str] = {}
    previous = allocation.application.plan if allocation.application is not None else None
    if source == "production":
        captured = (
            recorded_native_testing_capture(store, previous.generation)
            if previous is not None and previous.generation is not None
            else None
        )
        if captured is not None:
            native = captured.source_observation
            assert previous is not None
            authority = previous.native_source
        else:
            authority = native_testing_source_authority(
                store, profile=profile, testing_context=allocation.context
            )
            native = resolve_native_testing_source(
                store, root=root, profile=profile, testing_context=allocation.context, probe=True
            )
    elif source == "upstream":
        upstream = post_deploy.resolve_upstream_restore_workflow_environment(
            desired_env_map={**runtime.values, **target.env}
        )
    source_binding = authority.binding_sha256 if authority is not None else ""
    generation = (
        None
        if source == "empty"
        else FreshOdooTestingGeneration(
            product=allocation.product,
            context=allocation.context,
            operation_id=allocation.operation_id,
            target_id=allocation.compose_id,
            namespace=allocation.namespace,
            domain_host=str(urlsplit(lane.base_url).hostname),
            resources_binding_sha256=resources.binding_sha256,
            bootstrap_plan_sha256=allocation.intent_sha256,
            request_sha256=allocation.intent_sha256,
            omitted_runtime_keys=tuple(sorted(omitted)),
            prepared_at=allocation.created_at.isoformat(),
            source_mode="container-export" if source == "production" else "ssh",
            source_binding_sha256=source_binding,
        )
    )
    runtime_selection = RuntimeEnvironmentSetExpectation(
        contexts=(allocation.context,),
        instances=((allocation.context, "testing"),),
        include_global=True,
        records=(),
    )
    authority_binding = candidate_authority_sha256(
        profile=profile,
        target=target,
        runtime_records=(
            item
            for item in store.list_runtime_environment_records()
            if runtime_selection.includes(item)
        ),
        credential_binding=credential_binding,
        native=authority,
        upstream=upstream,
        overrides=overrides,
    )
    image_reference = f"{artifact.image.repository}@{artifact.image.digest}"
    identity = RuntimeIdentity(
        product=allocation.product,
        context=allocation.context,
        instance="testing",
        deployment_record_id=allocation.namespace + "-deployment",
        artifact_id=artifact.artifact_id,
        source_git_ref=artifact.source_commit,
        image_reference=image_reference,
        image_digest=artifact.image.digest,
        deployed_at=allocation.created_at.isoformat(),
    )
    plan = TestingCandidateApplicationPlan(
        product=allocation.product,
        context=allocation.context,
        operation_id=allocation.operation_id,
        namespace=allocation.namespace,
        target_id=allocation.compose_id,
        base_url=lane.base_url.rstrip("/"),
        artifact=artifact,
        runtime_identity=identity,
        source=source,
        generation=generation,
        native_source=authority,
        upstream_source=upstream,
        authority_sha256=authority_binding,
        credential_bindings_sha256=credential_binding,
        omitted_runtime_keys=tuple(sorted(omitted)),
    )
    if previous is not None and previous != plan:
        raise ValueError("Candidate input authority changed; preserve its original operation.")
    values = {
        key: value
        for key, value in runtime.values.items()
        if key not in omitted and key not in post_deploy.ODOO_UPSTREAM_RESTORE_WORKFLOW_ENV_KEYS
    }
    addons_path = merge_required_odoo_addons_path(values.get("ODOO_ADDONS_PATH", ""))
    if addons_path:
        values["ODOO_ADDONS_PATH"] = addons_path
    else:
        values.pop("ODOO_ADDONS_PATH", None)
    values.update(resources.volumes)
    values.update(
        {
            "ODOO_PROJECT_NAME": allocation.namespace,
            "ODOO_STACK_NAME": allocation.namespace,
            "PLATFORM_CONTEXT": allocation.context,
            "PLATFORM_INSTANCE": "testing",
            "ODOO_DB_HOST": "database",
            "ODOO_DB_PORT": "5432",
            "ODOO_HTTP_PORT": "8069",
            "ODOO_INSTALL_MODULES": modules,
            "ODOO_MAX_CRON_THREADS": "0",
            "DOCKER_IMAGE_REFERENCE": image_reference,
            "LAUNCHPLANE_IMAGE_REFERENCE": image_reference,
        }
    )
    values.update(runtime_identity_env(identity))
    values.update(workflow.inline_environment)
    return TestingCandidateRuntimeInputs(
        plan=plan,
        target=DokployTargetDefinition(
            context=allocation.context,
            instance="testing",
            target_type="compose",
            target_id=allocation.compose_id,
            target_name=allocation.namespace,
            project_name=allocation.namespace,
        ),
        compose_file=compose.render_odoo_raw_compose_file(
            image_reference=image_reference,
            hold_web_until_integration_readback=True,
            publish_host_ports=False,
            mount_workflow_ssh=False,
            isolated_testing=True,
            private_backend_alias=allocation.namespace,
        ),
        runtime_environment=values,
        workflow_environment=workflow.inline_environment,
        required_workflow_keys=workflow.required_container_environment_keys,
        native_source=native,
    )


def candidate_authority_sha256(
    *,
    profile: LaunchplaneProductProfileRecord,
    target: DokployTargetRecord,
    runtime_records: Iterable[RuntimeEnvironmentRecord],
    credential_binding: str,
    native: NativeOdooSourceAuthority | None,
    upstream: dict[str, str],
    overrides: OdooInstanceOverrideRecord,
) -> str:
    """One input binding for private preparation and atomic serving publication."""
    return canonical_json_sha256(
        {
            "profile": profile.model_dump(mode="json"),
            "target": target.model_dump(mode="json"),
            "runtime": [
                item.model_dump(mode="json")
                for item in sorted(
                    runtime_records, key=lambda item: (item.scope, item.context, item.instance)
                )
            ],
            "credentials": credential_binding,
            "native": native.model_dump(mode="json") if native is not None else None,
            "upstream": upstream,
            "overrides": overrides.model_dump(
                mode="json", exclude={"last_apply", "updated_at", "source_label"}
            ),
        }
    )
