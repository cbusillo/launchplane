import base64
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
import logging
import re
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict

from control_plane import secrets
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    product_config_requirement_applies_to_lane,
)
from control_plane.contracts.secret_record import (
    SecretAuditEvent,
    SecretBinding,
    SecretRecord,
    SecretVersion,
)
from control_plane.contracts.testing_external_resources import DiscordBotResourceObservation
from control_plane.discord_external_resources import discord_snowflake, observe_discord_bot
from control_plane.product_secret_copy import require_product_lanes
from control_plane.runtime_environments import site_delivered_secret_bindings_from_store
from control_plane.testing_external_resources import (
    ExternalResourceInputsChanged,
    EXTERNAL_RESOURCE_READ_SECONDS,
)
from control_plane.testing_local_credentials import (
    TestingCredentialStore,
    _require_unambiguous_lane,
)

ROUTE = "/v1/products/{product}/environments/{environment}/discord-identity"
_logger = logging.getLogger(__name__)


class DiscordIdentityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["observed", "unavailable", "matches_production"] = "unavailable"
    binding_key: str = ""
    version_id: str = ""
    observation: DiscordBotResourceObservation | None = None
    failure_code: (
        Literal["configuration_unavailable", "inputs_changed", "credential_ineligible"] | None
    ) = None
    qualification: Literal["not_evaluated"] = "not_evaluated"


class DiscordIdentityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    result: DiscordIdentityResult


def observation_complete(observation: DiscordBotResourceObservation | None) -> bool:
    return (
        observation is not None
        and observation.identity_status == observation.guild_status == "observed"
        and observation.guild_enumeration_complete
    )


@dataclass(frozen=True, repr=False)
class DiscordIdentityInputs:
    profiles: tuple[LaunchplaneProductProfileRecord, ...]
    binding: SecretBinding
    record: SecretRecord
    version: SecretVersion
    production: tuple[tuple[SecretBinding, SecretRecord, SecretVersion], ...]


def capture_identity_inputs(store: TestingCredentialStore, product: str) -> DiscordIdentityInputs:
    profiles = tuple(sorted(store.list_product_profile_records(), key=lambda item: item.product))
    profile = next(item for item in profiles if item.product == product)
    lanes = [lane for lane in profile.lanes if lane.instance == "testing"]
    if len(lanes) != 1:
        raise ValueError("Testing lane unavailable")
    lane = lanes[0]
    require_product_lanes(store, product=product, routes=((lane.context, "testing"),))
    _require_unambiguous_lane(store, lane.context, "testing")
    keys = {
        item.binding_key
        for item in profile.expected_config.managed_secret_bindings
        if item.integration == "runtime_environment"
        and item.external_provider == "discord_bot"
        and product_config_requirement_applies_to_lane(
            requirement_context=item.context, requirement_instance=item.instance, lane=lane
        )
    }
    if len(keys) != 1:
        raise ValueError("Discord purpose unavailable")
    binding = site_delivered_secret_bindings_from_store(
        record_store=store, context_name=lane.context, instance_name="testing"
    )[keys.pop()]
    record = store.read_secret_record(binding.secret_id)
    version = store.read_secret_version(record.current_version_id)
    if (
        binding.status != "configured"
        or record.status != "configured"
        or binding.declared_secret_class != "testing"
        or record.scope != "context_instance"
        or (record.context, record.instance) != (lane.context, "testing")
        or (binding.context, binding.instance) != (record.context, record.instance)
        or record.integration != "runtime_environment"
        or binding.integration != record.integration
        or version.secret_id != record.secret_id
        or version.version_id != record.current_version_id
        or version.value_origin == "generated"
    ):
        raise ValueError("Testing credential unavailable")
    production = []
    for source in profiles:
        for source_lane in source.lanes:
            if source_lane.instance != "prod":
                continue
            require_product_lanes(
                store, product=source.product, routes=((source_lane.context, "prod"),)
            )
            _require_unambiguous_lane(store, source_lane.context, "prod")
            selected = secrets.resolve_effective_secret_bindings_from_store(
                record_store=store,
                integration="runtime_environment",
                context_name=source_lane.context,
                instance_name="prod",
                scopes=frozenset({"context", "context_instance"}),
            )
            for key in sorted(selected):
                source_binding, source_record = selected[key]
                source_version = store.read_secret_version(source_record.current_version_id)
                if source_version.secret_id != source_record.secret_id:
                    raise ValueError("Production comparison unavailable")
                production.append((source_binding, source_record, source_version))
    return DiscordIdentityInputs(
        tuple(item.model_copy(deep=True) for item in profiles),
        binding.model_copy(deep=True),
        record.model_copy(deep=True),
        version.model_copy(deep=True),
        tuple(
            (
                binding.model_copy(deep=True),
                record.model_copy(deep=True),
                version.model_copy(deep=True),
            )
            for binding, record, version in production
        ),
    )


def token_bot_id(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", value) is None:
        return ""
    first = value.split(".", 1)[0]
    try:
        decoded = base64.b64decode(first + "=" * (-len(first) % 4), altchars=b"-_", validate=True)
        return discord_snowflake(decoded.decode("ascii"))
    except (ValueError, UnicodeError):
        return ""


def read_discord_identity(
    store: TestingCredentialStore,
    product: str,
    *,
    profiles: tuple[LaunchplaneProductProfileRecord, ...],
    trace_id: str,
    actor: str,
    captured_inputs: DiscordIdentityInputs | None = None,
    purpose: Literal["identity_diagnostic", "activation_admission"] = "identity_diagnostic",
    activation_plan_sha256: str = "",
    before_read: Callable[[], None] | None = None,
) -> DiscordIdentityResult:
    try:
        captured = captured_inputs or capture_identity_inputs(store, product)
        if captured.profiles != tuple(sorted(profiles, key=lambda item: item.product)):
            raise ExternalResourceInputsChanged()

        def check_current() -> None:
            if before_read is not None:
                before_read()
            try:
                if capture_identity_inputs(store, product) == captured:
                    return
            except Exception:
                raise ExternalResourceInputsChanged() from None
            raise ExternalResourceInputsChanged()

        check_current()
        value = secrets._decrypt_secret_value(captured.version.ciphertext, captured.version.key_id)
        production_values = tuple(
            secrets._decrypt_secret_value(version.ciphertext, version.key_id)
            for _, _, version in captured.production
        )
        expected_bot = token_bot_id(value)
        matches_production = any(
            hmac.compare_digest(value.encode(), production_value.encode())
            or bool(expected_bot and expected_bot == token_bot_id(production_value.strip()))
            for production_value in production_values
        )
        check_current()
        if matches_production:
            return DiscordIdentityResult(status="matches_production")
        if not expected_bot:
            return DiscordIdentityResult(failure_code="credential_ineligible")
        check_current()
        store.write_secret_audit_event(
            SecretAuditEvent(
                event_id=secrets._audit_event_id(
                    secret_id=captured.record.secret_id, event_type="resolution_attempted"
                ),
                secret_id=captured.record.secret_id,
                event_type="resolution_attempted",
                recorded_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                actor=actor,
                detail="Testing Discord credential identification attempt.",
                metadata={
                    "trace_id": trace_id,
                    "version_id": captured.version.version_id,
                    "binding_id": captured.binding.binding_id,
                    "destination_class": "discord_bot",
                    "purpose": purpose,
                    **(
                        {"activation_plan_sha256": activation_plan_sha256}
                        if activation_plan_sha256
                        else {}
                    ),
                },
            )
        )
        observation = observe_discord_bot(
            value,
            deadline=monotonic() + EXTERNAL_RESOURCE_READ_SECONDS,
            check_current=check_current,
        )
        check_current()
        if observation.identity_status == "observed" and observation.bot_id != expected_bot:
            return DiscordIdentityResult(failure_code="credential_ineligible")
        return DiscordIdentityResult(
            status="observed" if observation_complete(observation) else "unavailable",
            binding_key=captured.binding.binding_key,
            version_id=captured.version.version_id,
            observation=observation,
        )
    except ExternalResourceInputsChanged:
        return DiscordIdentityResult(failure_code="inputs_changed")
    except Exception:
        _logger.warning(
            "Discord identity unavailable: trace=%s code=configuration_unavailable", trace_id
        )
        return DiscordIdentityResult(failure_code="configuration_unavailable")
