from collections.abc import Callable

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.testing_activation import (
    TestingActivationRecord,
    TestingActivationRefusal,
    TestingActivationRefusalCode,
)
from control_plane.contracts.testing_external_resources import TestingExternalCapabilityProof
from control_plane.discord_identity import (
    DiscordIdentityInputs,
    DiscordIdentityResult,
    capture_identity_inputs,
    observation_complete,
    read_discord_identity,
)
from control_plane.storage.postgres import PostgresRecordStore


class ExternalCapabilityReadRefusal(TestingActivationRefusal):
    def __init__(self, result: DiscordIdentityResult) -> None:
        super().__init__(
            "Complete current private Discord scope remains unproved.",
            code=TestingActivationRefusalCode.EXTERNAL_CAPABILITY_UNVERIFIED,
        )
        self.result = result


def discord_inputs_digest(captured: DiscordIdentityInputs) -> str:
    return canonical_json_sha256(
        {
            "testing": (
                captured.binding.binding_id,
                captured.record.secret_id,
                captured.version.version_id,
            ),
            "production": sorted(
                (binding.binding_id, secret.secret_id, version.version_id)
                for binding, secret, version in captured.production
            ),
        }
    )


def require_discord_capability(
    store: PostgresRecordStore,
    record: TestingActivationRecord,
    proof: TestingExternalCapabilityProof,
) -> None:
    specification = record.request.application
    if specification is None or len(specification.external_credentials) != 1:
        raise ValueError("A reviewed external credential is required.")
    reviewed = specification.external_credentials[0]
    captured = capture_identity_inputs(
        store, record.product, compare_production=not record.published
    )
    if (
        captured.binding.context != record.context
        or proof.activation_plan_sha256 != record.plan_sha256
        or (proof.binding_key, proof.binding_id, proof.secret_id, proof.version_id)
        != (
            reviewed.binding_key,
            captured.binding.binding_id,
            captured.record.secret_id,
            captured.version.version_id,
        )
        or captured.binding.binding_key != reviewed.binding_key
        or not set(proof.guild_ids).issubset(reviewed.guild_ids)
        or (not record.published and proof.inputs_sha256 != discord_inputs_digest(captured))
    ):
        raise ValueError("The original external credential proof changed.")


def observe_activation_discord(
    store: PostgresRecordStore,
    record: TestingActivationRecord,
    *,
    authorize: Callable[[], tuple[LaunchplaneProductProfileRecord, ...]],
    check_current: Callable[[], None],
    actor: str,
    trace_id: str,
) -> TestingExternalCapabilityProof:
    profiles = authorize()
    captured = capture_identity_inputs(store, record.product)
    specification = record.request.application
    if specification is None or len(specification.external_credentials) != 1:
        raise ValueError("Review one external credential before admission.")
    reviewed = specification.external_credentials[0]
    if (
        captured.binding.context != record.context
        or captured.binding.binding_key != reviewed.binding_key
    ):
        raise ValueError("The reviewed external binding does not match this testing lane.")
    result = read_discord_identity(
        store,
        record.product,
        profiles=profiles,
        trace_id=trace_id,
        actor=actor,
        captured_inputs=captured,
        purpose="activation_admission",
        activation_plan_sha256=record.plan_sha256,
        before_read=check_current,
    )
    observation = result.observation
    if (
        result.status != "observed"
        or result.failure_code is not None
        or not observation_complete(observation)
        or observation is None
        or observation.failure_code is not None
        or (result.binding_key, result.version_id)
        != (reviewed.binding_key, captured.version.version_id)
        or not set(observation.guild_ids).issubset(reviewed.guild_ids)
    ):
        raise ExternalCapabilityReadRefusal(result)
    try:
        check_current()
        if capture_identity_inputs(store, record.product) != captured:
            raise ValueError("External inputs changed.")
    except Exception:
        raise ExternalCapabilityReadRefusal(
            result.model_copy(update={"status": "unavailable", "failure_code": "inputs_changed"})
        ) from None
    return TestingExternalCapabilityProof(
        activation_plan_sha256=record.plan_sha256,
        binding_key=result.binding_key,
        binding_id=captured.binding.binding_id,
        secret_id=captured.record.secret_id,
        version_id=result.version_id,
        bot_id=observation.bot_id,
        application_id=observation.application_id,
        guild_ids=observation.guild_ids,
        observed_at=observation.finished_at,
        trace_id=trace_id,
        inputs_sha256=discord_inputs_digest(captured),
        rate_limits=observation.rate_limits,
    )
