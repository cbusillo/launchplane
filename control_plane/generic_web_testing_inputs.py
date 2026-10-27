"""Site-only managed inputs for the ordinary application testing deploy."""

import json
from dataclasses import dataclass
from typing import Protocol, cast

import click

from control_plane import live_target_runtime as runtime
from control_plane import runtime_environments as environments
from control_plane import secrets
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord
from control_plane.runtime_key_safety import RuntimeKeySafetyPolicyReadStore

DELIVERY_RECEIPT_KEY = "testing_runtime_inputs_verified_sha256"
TESTING_DISPATCH_REFUSED_PHASE = "testing_dispatch_refused"


class TestingInputSafetyRefused(ValueError):
    """Refuse the next effect while the durable runner preserves earlier effects."""


class TestingTargetAuthorityRefused(TestingInputSafetyRefused):
    """Current authority no longer permits provider writes to the captured target."""


@dataclass(frozen=True)
class TestingSecretProvenance:
    binding_id: str
    secret_id: str
    binding_integration: str
    binding_type: str
    binding_key: str
    binding_context: str
    binding_instance: str
    binding_status: str
    secret_integration: str
    secret_scope: str
    secret_context: str
    secret_instance: str
    secret_status: str


@dataclass(frozen=True)
class TestingInputEnvelope:
    environment: environments.SiteRuntimeEnvironment
    safety_keys: frozenset[str]
    provenance: tuple[TestingSecretProvenance, ...]


class TestingInputStore(
    environments.SiteRuntimeEnvironmentStore,
    runtime.LiveTargetRuntimeProfileStore,
    RuntimeKeySafetyPolicyReadStore,
    Protocol,
):
    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...
    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...
    def read_provider_target_record(
        self, *, context_name: str, instance_name: str
    ) -> ProviderTargetRecord: ...


def require_testing_application_target_authority(
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    *,
    target_id: str,
    provider_id: str,
    driver_id: str = "generic-web",
) -> None:
    """Keep dispatch bound to the active application even after inputs are saved."""
    store = cast(TestingInputStore, record_store)
    current = store.read_product_profile_record(profile.product)
    definition = store.read_dokploy_target_record(
        context_name=lane.context, instance_name=lane.instance
    )
    binding = store.read_dokploy_target_id_record(
        context_name=lane.context, instance_name=lane.instance
    )
    provider = store.read_provider_target_record(
        context_name=lane.context, instance_name=lane.instance
    )
    if (
        not current.is_active
        or current.driver_id != driver_id
        or lane.instance != "testing"
        or not any(
            item.context == lane.context
            and item.instance == "testing"
            and item.generic_web_cutover is None
            for item in current.lanes
        )
        or definition.target_type != "application"
        or binding.target_id != target_id
        or provider.target_id != target_id
        or provider.provider_id != provider_id
        or provider.target_category != "application"
        or provider.provider_target_type != "application"
    ):
        raise TestingTargetAuthorityRefused(
            "Application testing target authority changed before mutation."
        )


def testing_input_authority(
    record_store: object, profile: LaunchplaneProductProfileRecord, lane: ProductLaneProfile
) -> str:
    """Fingerprint effective site inputs without decrypting a secret version."""
    if (
        profile.driver_id != "generic-web"
        or lane.instance != "testing"
        or lane.generic_web_cutover is not None
    ):
        return ""
    store = cast(TestingInputStore, record_store)
    profile = store.read_product_profile_record(profile.product)
    if (
        not profile.is_active
        or profile.driver_id != "generic-web"
        or not any(
            current.context == lane.context
            and current.instance == lane.instance
            and current.generic_web_cutover is None
            for current in profile.lanes
        )
    ):
        return ""
    try:
        target = store.read_dokploy_target_record(
            context_name=lane.context, instance_name=lane.instance
        )
        binding = store.read_dokploy_target_id_record(
            context_name=lane.context, instance_name=lane.instance
        )
    except (AttributeError, FileNotFoundError):
        return ""
    if target.target_type != "application":
        return ""
    records = tuple(store.list_runtime_environment_records())
    site_records = tuple(
        record
        for record in records
        if record.context == lane.context
        and (
            record.scope == "context"
            or (record.scope == "instance" and record.instance == lane.instance)
        )
    )
    selected = secrets.resolve_effective_secret_bindings_from_store(
        record_store=store,
        integration=secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        context_name=lane.context,
        instance_name=lane.instance,
        scopes=environments.site_secret_scopes(lane.instance),
    )
    retired = environments.retired_provider_keys_from_store(
        record_store=store, context_name=lane.context, instance_name=lane.instance
    )
    declared = runtime.declared_runtime_keys(
        profile=profile, context_name=lane.context, instance_name=lane.instance
    )
    if (
        not target.env
        and not selected
        and not declared
        and not retired
        and not any(record.env for record in site_records)
    ):
        return ""
    return secrets.keyed_secret_payload_fingerprint(
        json.dumps(
            {
                "contract": "site-application-testing-inputs-v1",
                "target": {
                    "context": lane.context,
                    "instance": lane.instance,
                    "id": binding.target_id,
                    "env": target.env,
                },
                "settings": sorted(
                    (
                        record.model_dump(mode="json", exclude={"updated_at", "source_label"})
                        for record in site_records
                    ),
                    key=lambda item: json.dumps(item, sort_keys=True),
                ),
                "declared": sorted(declared),
                "required_managed_secrets": sorted(
                    runtime.require_product_profile_runtime_secret_keys(
                        record_store=store,
                        product_name=profile.product,
                        context_name=lane.context,
                        instance_name=lane.instance,
                    )
                ),
                "secrets": {
                    key: {
                        "binding": value.model_dump(
                            mode="json", exclude={"created_at", "updated_at"}
                        ),
                        "version": record.current_version_id,
                    }
                    for key, (value, record) in selected.items()
                    if key not in retired
                },
            },
            sort_keys=True,
        ),
        purpose="generic-web-testing-inputs",
    )


def prepare_testing_inputs(
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    expected_digest: str,
) -> TestingInputEnvelope:
    store = cast(TestingInputStore, record_store)
    profile = store.read_product_profile_record(profile.product)
    if testing_input_authority(store, profile, lane) != expected_digest:
        raise click.ClickException("Application testing input authority changed before delivery.")
    target = store.read_dokploy_target_record(
        context_name=lane.context, instance_name=lane.instance
    )
    environment = environments.resolve_site_runtime_environment_from_store(
        record_store=store,
        context_name=lane.context,
        instance_name=lane.instance,
        target_environment=target.env,
    )
    declared = runtime.product_lane_declared_keys(
        record_store=store,
        product_name=profile.product,
        context_name=lane.context,
        instance_name=lane.instance,
        profile=profile,
    )
    runtime.require_declared_runtime_keys_present(
        declared_keys=declared,
        available_keys=set(environment.values),
        target=f"{lane.context}/{lane.instance}",
    )
    required_secrets = runtime.require_product_profile_runtime_secret_keys(
        record_store=store,
        product_name=profile.product,
        context_name=lane.context,
        instance_name=lane.instance,
    )
    if required_secrets - environment.secret_keys:
        raise click.ClickException(
            "Declared managed secret values are missing: "
            + ", ".join(sorted(required_secrets - environment.secret_keys))
        )
    runtime.validate_provider_key_retirement(
        record_store=store,
        product_name=profile.product,
        context_name=lane.context,
        instance_name=lane.instance,
        retired_keys=set(environment.retired_keys),
        application_keys=declared | environments.site_application_keys(environment.setting_keys),
    )
    transported = runtime.transported_runtime_secret_keys(
        record_store=store,
        context_name=lane.context,
        instance_name=lane.instance,
        transported_keys=environment.values,
    )
    safety_keys = frozenset(required_secrets | transported | environment.secret_keys)
    sources = _selected_testing_secret_sources(store, lane)
    if environment.secret_keys - sources.keys():
        raise TestingInputSafetyRefused(
            "Application testing secret suppliers changed during capture."
        )
    envelope = TestingInputEnvelope(
        environment,
        safety_keys,
        tuple(_secret_provenance(*sources[key]) for key in sorted(environment.secret_keys)),
    )
    require_testing_input_key_safety(store, lane, envelope)
    if (
        profile.expected_config
        != store.read_product_profile_record(profile.product).expected_config
        or testing_input_authority(store, profile, lane) != expected_digest
    ):
        raise click.ClickException(
            "Application testing input authority changed while resolving values."
        )
    return envelope


def _selected_testing_secret_sources(
    store: TestingInputStore, lane: ProductLaneProfile
) -> dict[str, tuple[SecretBinding, SecretRecord]]:
    # This is the canonical site-delivery selection. Retirement was already
    # applied to captured environment.secret_keys; today's retirement must not
    # revoke the continuation of values saved by this execution.
    return secrets.resolve_effective_secret_bindings_from_store(
        record_store=store,
        integration=secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        context_name=lane.context,
        instance_name=lane.instance,
        scopes=environments.site_secret_scopes(lane.instance),
    )


def _secret_provenance(binding: SecretBinding, secret: SecretRecord) -> TestingSecretProvenance:
    return TestingSecretProvenance(
        binding_id=binding.binding_id,
        secret_id=secret.secret_id,
        binding_integration=binding.integration,
        binding_type=binding.binding_type,
        binding_key=binding.binding_key,
        binding_context=binding.context,
        binding_instance=binding.instance,
        binding_status=binding.status,
        secret_integration=secret.integration,
        secret_scope=secret.scope,
        secret_context=secret.context,
        secret_instance=secret.instance,
        secret_status=secret.status,
    )


def require_testing_input_key_safety(
    record_store: object,
    lane: ProductLaneProfile,
    captured: TestingInputEnvelope,
) -> None:
    """Current policy and original bindings govern the captured payload, not later inputs."""
    store = cast(TestingInputStore, record_store)
    sources = _selected_testing_secret_sources(store, lane)
    delivered_keys = {source.binding_key for source in captured.provenance}
    suppliers: list[SecretBinding] = []
    for original in captured.provenance:
        current = sources.get(original.binding_key)
        if (
            current is None
            or _secret_provenance(*current) != original
            or current[0].status != "configured"
            or current[1].status != "configured"
        ):
            raise TestingInputSafetyRefused("Captured application testing secret supplier changed.")
        suppliers.append(current[0])
    # A frozen safety requirement can exist without a delivered secret (for
    # example a plain setting sharing a global binding's key). Keep its current
    # fail-closed policy lookup rather than silently dropping that requirement.
    policy_only_keys = captured.safety_keys - delivered_keys
    if policy_only_keys:
        suppliers.extend(
            binding
            for binding in store.list_secret_bindings(
                integration=secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION, limit=None
            )
            if binding.binding_key in policy_only_keys
        )
    runtime.evaluate_runtime_key_safety_for_live_target_sync(
        record_store=store,
        context_name=lane.context,
        instance_name=lane.instance,
        required_binding_keys=tuple(sorted(captured.safety_keys)),
        secret_bindings=tuple(suppliers),
    )
