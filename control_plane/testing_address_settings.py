import base64
from collections.abc import Callable
import json
import time
from typing import TYPE_CHECKING, Any, Literal

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.odoo_post_deploy_payload import OdooPostDeployPayload
from control_plane.contracts.testing_address import TestingAddressPlan, ADDRESS_SCOPE
from control_plane.contracts.testing_address_settings import TestingAddressSettingsPlan
from control_plane.contracts.testing_publication import TestingPublicationRecord
from control_plane.connection_isolation_qualification import require_connection_isolation_lane
from control_plane.dokploy import api, compose, post_deploy, source
from control_plane.dokploy.native_copy_stream import (
    run_managed_server_program,
    NativeSourceUnavailable,
)
from control_plane.integration_readback import (
    INTEGRATION_READBACK_PASSED_PATH,
    INTEGRATION_READBACK_PROGRAM,
)
from control_plane.odoo_testing_restore import (
    FRESH_TESTING_RESTORE_HOST_PROGRAM,
    TESTING_COPY_COMPLETION_PARAMETER,
    generation_binding,
)
from control_plane.provider_operations import (
    ProviderEvidenceLease,
    DurableProviderMutationAdapter,
    ProviderMutationOutcome,
    ProviderObservation,
    ProviderOperationLease,
    run_durable_provider_operation,
)
from control_plane.testing_candidate_probe import (
    CANDIDATE_CONTAINER_PROBE,
    sanitized_testing_readback_policy,
)

if TYPE_CHECKING:
    from control_plane.testing_address import TestingAddressAdapter

PAYLOAD_KEY = post_deploy.ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY
SETTINGS_ROUTE = "service:testing-address:canonical-settings"


def canonical_address_payload(encoded: str, *, context: str, before: str, after: str) -> str:
    raw = json.loads(base64.b64decode(encoded, validate=True))
    payload = OdooPostDeployPayload.model_validate(raw)
    if (payload.context, payload.instance) != (
        context,
        "testing",
    ) or payload.website_settings is not None:
        raise ValueError("Canonical settings require the existing testing payload.")
    parameters = raw.get("config_parameters", [])
    keys = [item["key"] for item in parameters]
    if len(set(item.key for item in payload.config_parameters)) != len(keys) or keys != [
        item.key for item in payload.config_parameters
    ]:
        raise ValueError("Canonical settings require unambiguous existing parameters.")
    current = next((item for item in parameters if item["key"] == "web.base.url"), None)
    if not isinstance(current, dict) or current.get("value") != {
        "source": "literal",
        "value": before,
    }:
        raise ValueError("The existing payload does not own the reviewed address.")
    replacements = {"web.base.url": after, "web.base.url.freeze": "True"}
    for item in parameters:
        if item["key"] in replacements:
            item["value"] = {"source": "literal", "value": replacements[item["key"]]}
    raw["config_parameters"] = parameters + [
        {"key": key, "value": {"source": "literal", "value": value}}
        for key, value in replacements.items()
        if key not in keys
    ]
    bootstrap = raw.get("website_bootstrap")
    if not isinstance(bootstrap, dict) or bootstrap.get("canonical_url") != before:
        raise ValueError("Canonical settings require the original website intent.")
    bootstrap["canonical_url"] = after
    OdooPostDeployPayload.model_validate(raw)
    return base64.b64encode(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ).decode()


def canonical_environment_sha256(environment: dict[str, str]) -> str:
    return canonical_json_sha256(
        {key: value for key, value in environment.items() if key != PAYLOAD_KEY}
    )


def require_canonical_provider(
    plan: TestingAddressPlan, settings: TestingAddressSettingsPlan, provider: api.JsonObject
) -> None:
    environment = api.parse_dokploy_env_text(str(provider.get("env") or ""))
    if (
        (
            provider.get("composeId"),
            provider.get("name"),
            provider.get("serverId"),
            provider.get("appName"),
            provider.get("domains"),
        )
        != (
            plan.before.target_id.target_id,
            plan.before.target.target_name,
            plan.server_id,
            plan.provider_project,
            [],
        )
        or canonical_environment_sha256(environment) != settings.provider_environment_sha256
        or environment.get(PAYLOAD_KEY) not in {settings.original_payload, settings.payload}
    ):
        raise ValueError("Canonical settings cannot change another target or environment.")


def require_canonical_workflows(host: str, token: str, plan: TestingAddressPlan) -> None:
    schedules = api.list_dokploy_schedules(
        host=host,
        token=token,
        target_id=plan.before.target_id.target_id,
        schedule_type="compose",
    )
    for item in schedules:
        history = item.get("deployments")
        if (
            item.get("enabled") is not False
            or api.parse_deployment_history(history).state == "unknown"
        ):
            raise ValueError("Canonical settings require complete disabled target workflows.")
        assert isinstance(history, list)
        if any(not isinstance(row, dict) or not api.deployment_is_terminal(row) for row in history):
            raise ValueError("Canonical settings require idle target workflows.")
    deployments = api.list_deployments_for_target(
        host=host,
        token=token,
        target_type="compose",
        target_id=plan.before.target_id.target_id,
        require_complete=True,
    )
    if any(not api.deployment_is_terminal(item) for item in deployments):
        raise NativeSourceUnavailable("Canonical settings require settled target deployments.")


def canonical_settings_frame(
    *,
    host: str,
    token: str,
    plan: TestingAddressPlan,
    publication: TestingPublicationRecord,
    settings: TestingAddressSettingsPlan | None,
    provider: api.JsonObject,
    mode: Literal["inspect", "gate", "readback", "observe"],
    dispatch_guard: Callable[[Callable[[], None]], None] | None = None,
) -> dict[str, Any]:
    identity = plan.before.inventory.runtime_identity if plan.before.inventory else None
    if identity is None:
        raise ValueError("Canonical settings require the current testing identity.")
    require_canonical_workflows(host, token, plan)
    if settings is not None:
        require_canonical_provider(plan, settings, provider)
    candidate = publication.plan.candidate
    environment = api.parse_dokploy_env_text(str(provider.get("env") or ""))
    payload = (
        settings.payload
        if settings and mode != "inspect"
        else settings.original_payload
        if settings
        else environment.get(PAYLOAD_KEY, "")
    )
    bootstrap = OdooPostDeployPayload.model_validate_json(
        base64.b64decode(payload, validate=True)
    ).website_bootstrap
    spec: dict[str, object] = {
        "generation": plan.before.generation.model_dump(mode="json")
        if plan.before.generation
        else {
            "namespace": candidate.namespace,
            "instance": "testing",
            "resources_binding_sha256": candidate.resources_binding_sha256,
        },
        "compose_project": plan.provider_project,
        "database_name": environment.get("ODOO_DB_NAME", ""),
        "database_data_directory": compose.ODOO_DATABASE_DATA_DIRECTORY,
        "image_reference": identity.image_reference,
        "base_url": "https://"
        + (plan.route_before.host if mode == "inspect" else plan.route_after.host),
        "http_port": plan.profile.runtime_port,
        "web_config": compose.ODOO_WEB_CONFIG_PATH,
        "runtime_identity": identity.model_dump(mode="json", exclude={"deployed_at"}),
        "credential_program": base64.b64encode(CANDIDATE_CONTAINER_PROBE.encode()).decode(),
        "verification_only": True,
        "inspection_only": mode != "gate",
        "copy_binding_sha256": generation_binding(plan.before.generation)
        if plan.before.generation
        else "",
        "data_workflow_lock_path": post_deploy.DEFAULT_DATA_WORKFLOW_LOCK_PATH,
        "web_gate_path": INTEGRATION_READBACK_PASSED_PATH,
        "readback_program": INTEGRATION_READBACK_PROGRAM,
        "readback_spec": sanitized_testing_readback_policy().encoded_spec(),
        "canonical_settings": {
            "mode": mode,
            "payload": payload,
            "runtime_identity": identity.model_dump(mode="json", exclude={"deployed_at"}),
            "canonical_url": "https://" + plan.route_after.host,
            "before_url": "https://" + plan.route_before.host,
            "primary_page_xmlid_set": bool(bootstrap and bootstrap.primary_page_xmlid),
            "website_id": settings.website_id if settings else 0,
            "database_values_sha256": settings.database_values_sha256 if settings else "",
            "copy_witness_parameter": TESTING_COPY_COMPLETION_PARAMETER,
        },
    }
    frames: list[dict[str, object]] = []
    run_managed_server_program(
        host=host,
        token=token,
        server_id=plan.server_id,
        program=FRESH_TESTING_RESTORE_HOST_PROGRAM,
        payload=spec,
        receive=frames.append,
        timeout_seconds=120,
        dispatch_guard=dispatch_guard,
    )
    if len(frames) != 1 or frames[0].get("type") != "complete":
        raise NativeSourceUnavailable("Canonical settings have no complete native observation.")
    return frames[0]


def plan_canonical_settings(
    *,
    host: str,
    token: str,
    plan: TestingAddressPlan,
    publication: TestingPublicationRecord,
    provider: api.JsonObject,
) -> TestingAddressSettingsPlan:
    environment = api.parse_dokploy_env_text(str(provider.get("env") or ""))
    original = environment.get(PAYLOAD_KEY, "")
    payload = canonical_address_payload(
        original,
        context=plan.context,
        before="https://" + plan.route_before.host,
        after="https://" + plan.route_after.host,
    )
    observed = canonical_settings_frame(
        host=host,
        token=token,
        plan=plan,
        publication=publication,
        settings=None,
        provider=provider,
        mode="inspect",
    )
    return TestingAddressSettingsPlan(
        original_payload=original,
        payload=payload,
        provider_environment_sha256=canonical_environment_sha256(environment),
        website_id=observed["website_id"],
        database_values_sha256=observed["database_values_sha256"],
    )


class CanonicalAddressEffect(DurableProviderMutationAdapter):
    def __init__(
        self,
        adapter: TestingAddressAdapter,
        parent: ProviderOperationLease | None,
        phase: Literal["environment", "deployment", "gate"],
    ) -> None:
        self.adapter, self.parent, self.phase = adapter, parent, phase
        self.plan = adapter.plan
        assert self.plan.canonical_settings is not None
        self.settings = self.plan.canonical_settings

    def target_key(self) -> str:
        return f"testing-address-canonical:{self.plan.before.target_id.target_id}:{self.phase}"

    def reconciliation_key(self) -> str:
        return self.plan.digest

    def title(self) -> str:
        return "testing-address-canonical-" + self.plan.digest

    def checked_provider(self) -> tuple[str, str, api.JsonObject, TestingPublicationRecord]:
        from control_plane.testing_address_storage import (
            address_overrides,
            read_testing_address,
            verify_testing_address_retained_custody,
        )
        from control_plane.testing_serving_copy import published_testing_copy

        if self.parent is not None:
            self.parent.assert_current()
        self.adapter.canonical_lane_checkpoint()
        require_connection_isolation_lane(
            self.adapter.store,
            product=self.plan.product,
            context=self.plan.context,
            instance="testing",
        )
        record = read_testing_address(self.adapter.store, self.plan.publication_key)
        publication = published_testing_copy(
            self.adapter.store, product=self.plan.product, context=self.plan.context
        )
        if (
            record is None
            or record.state != "applied"
            or record.plan != self.plan
            or record.applied_at is None
            or publication is None
            or canonical_json_sha256(publication.model_dump(mode="json"))
            != self.plan.publication_sha256
        ):
            raise ValueError("Canonical settings require their committed address intent.")
        current = self.adapter.store.read_odoo_instance_override_record(
            context_name=self.plan.context, instance_name="testing"
        )
        expected = address_overrides(
            self.plan.before.overrides,
            context=self.plan.context,
            base_url="https://" + self.plan.route_after.host,
            stamp=record.applied_at.isoformat(),
        )
        ignored = {"last_apply", "updated_at", "source_label"}
        if current.model_dump(exclude=ignored) != expected.model_dump(exclude=ignored):
            raise ValueError("The committed canonical settings intent changed.")
        verify_testing_address_retained_custody(self.adapter.store, self.plan)
        host, token = source.read_dokploy_config(
            control_plane_root=self.adapter.root, database_url=self.adapter.store.database_url
        )
        provider = api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="compose",
            target_id=self.plan.before.target_id.target_id,
        )
        require_canonical_provider(self.plan, self.settings, provider)
        require_canonical_workflows(host, token, self.plan)
        return host, token, provider, publication

    def frame(
        self,
        mode: Literal["inspect", "gate", "readback", "observe"],
        *,
        dispatch_guard: Callable[[Callable[[], None]], None] | None = None,
    ) -> dict[str, Any]:
        host, token, provider, publication = self.checked_provider()
        return canonical_settings_frame(
            host=host,
            token=token,
            plan=self.plan,
            publication=publication,
            settings=self.settings,
            provider=provider,
            mode=mode,
            dispatch_guard=dispatch_guard,
        )

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.plan.digest:
            return ProviderObservation("unknown")
        _, _, provider, _ = self.checked_provider()
        if self.phase == "environment":
            if (
                api.parse_dokploy_env_text(str(provider.get("env") or "")).get(PAYLOAD_KEY)
                == self.settings.payload
            ):
                return ProviderObservation("present", 200, {"canonical_environment_updated": True})
            return ProviderObservation(
                "absent", retry_safe=True, preserve_effect_checkpoint=bool(provider_effect_phase)
            )
        if not provider_effect_phase:
            return ProviderObservation("absent", retry_safe=True)
        if self.phase == "deployment":
            self.frame("observe")
            return ProviderObservation("present", 200, {"canonical_deployment_verified": True})
        frame = self.frame("observe")
        match frame.get("canonical_gate_released"):
            case True:
                return ProviderObservation("present", 200, {"canonical_gate_released": True})
            case False:
                return ProviderObservation(
                    "absent", retry_safe=True, preserve_effect_checkpoint=True
                )
            case _:
                return ProviderObservation("unknown")

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        assert self.parent is not None
        host, token, provider, _ = self.checked_provider()
        parent = self.parent

        def checkpoint(send: Callable[[], None]) -> None:
            self.checked_provider()
            parent.checkpoint_effect("testing-address-canonical-settings")
            lease.checkpoint_effect("canonical-" + self.phase)
            send()

        if self.phase == "environment":
            frame = self.frame("inspect")
            if frame.get("database_values_sha256") != self.settings.database_values_sha256:
                raise ValueError("Canonical database values changed since review.")
            environment = api.parse_dokploy_env_text(str(provider.get("env") or ""))
            environment[PAYLOAD_KEY] = self.settings.payload
            text = api.serialize_dokploy_env_text(
                environment,
                target_type="compose",
                compose_type=str(provider.get("composeType") or "docker-compose"),
                current_env_text=str(provider.get("env") or ""),
            )
            checkpoint(
                lambda: api.update_dokploy_target_env(
                    host=host,
                    token=token,
                    target_type="compose",
                    target_id=self.plan.before.target_id.target_id,
                    target_payload=provider,
                    env_text=text,
                )
            )
            return ProviderMutationOutcome(200, {"canonical_environment_updated": True})
        if self.phase == "deployment":
            checkpoint(
                lambda: api.trigger_deployment(
                    host=host,
                    token=token,
                    target_type="compose",
                    target_id=self.plan.before.target_id.target_id,
                    no_cache=False,
                    title=self.title(),
                )
            )
            deadline = time.monotonic() + 180
            while True:
                try:
                    self.frame("observe")
                    break
                except NativeSourceUnavailable as error:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise NativeSourceUnavailable(
                            "The original canonical deployment has no qualified runtime proof."
                        ) from error
                    time.sleep(min(2, remaining))
            return ProviderMutationOutcome(200, {"canonical_deployment_verified": True})
        frame = self.frame("gate", dispatch_guard=checkpoint)
        match frame.get("canonical_gate_released"):
            case True:
                return ProviderMutationOutcome(200, {"canonical_gate_released": True})
            case _:
                raise NativeSourceUnavailable("The original canonical gate writer is unverified.")


def preflight_canonical_settings(
    adapter: TestingAddressAdapter, lease: ProviderOperationLease
) -> None:
    if adapter.plan.canonical_settings is None:
        return
    from control_plane.testing_address_storage import verify_testing_address_sources
    from control_plane.testing_serving_copy import published_testing_copy

    lease.assert_current()
    adapter.canonical_lane_checkpoint()
    verify_testing_address_sources(adapter.store, adapter.plan)
    publication = published_testing_copy(
        adapter.store, product=adapter.plan.product, context=adapter.plan.context
    )
    if (
        publication is None
        or canonical_json_sha256(publication.model_dump(mode="json"))
        != adapter.plan.publication_sha256
    ):
        raise ValueError("Canonical preflight requires its original publication.")
    host, token = source.read_dokploy_config(
        control_plane_root=adapter.root, database_url=adapter.store.database_url
    )
    provider = api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="compose",
        target_id=adapter.plan.before.target_id.target_id,
    )
    frame = canonical_settings_frame(
        host=host,
        token=token,
        plan=adapter.plan,
        publication=publication,
        settings=adapter.plan.canonical_settings,
        provider=provider,
        mode="inspect",
    )
    if (
        frame.get("database_values_sha256")
        != adapter.plan.canonical_settings.database_values_sha256
    ):
        raise ValueError("Canonical database values changed since review.")
    lease.assert_current()
    adapter.canonical_lane_checkpoint()
    verify_testing_address_sources(adapter.store, adapter.plan)


def apply_canonical_settings(adapter: TestingAddressAdapter, lease: ProviderOperationLease) -> None:
    if adapter.plan.canonical_settings is None:
        return
    assert isinstance(lease, ProviderEvidenceLease)
    for phase in ("environment", "deployment", "gate"):
        effect = CanonicalAddressEffect(adapter, lease, phase)
        result = run_durable_provider_operation(
            store=adapter.store,
            scope=ADDRESS_SCOPE,
            route_path=SETTINGS_ROUTE + ":" + phase,
            idempotency_key=adapter.plan.digest,
            request_fingerprint=adapter.plan.digest,
            lease_owner=lease.evidence_reservation().lease_owner,
            response_trace_id=adapter.trace_id,
            adapter=effect,
            original_intent={"testing_address_plan": adapter.plan.model_dump(mode="json")},
        )
        if result.status not in {"completed", "replayed", "adopted"}:
            raise NativeSourceUnavailable(
                "Continue the original canonical settings phase with its original request and key."
            )
    verify_canonical_settings(adapter, lease, wait_seconds=120)


def verify_canonical_settings(
    adapter: TestingAddressAdapter,
    lease: ProviderOperationLease | None = None,
    *,
    wait_seconds: float = 0,
) -> None:
    if adapter.plan.canonical_settings is None:
        return
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            frame = CanonicalAddressEffect(adapter, lease, "gate").frame("readback")
            if (
                frame.get("canonical_settings_verified") is True
                and frame.get("canonical_gate_released") is True
            ):
                return
        except NativeSourceUnavailable:
            if time.monotonic() >= deadline:
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NativeSourceUnavailable(
                "The current Odoo canonical settings have not been independently verified."
            )
        time.sleep(min(2, remaining))
