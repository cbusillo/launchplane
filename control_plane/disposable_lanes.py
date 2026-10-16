"""Lane eligibility is separate from identity and from historical recovery proof."""

from datetime import datetime, timezone
import hashlib
import json
from typing import Protocol

from control_plane.contracts.disposable_lane import DisposableLaneSafety
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.runtime_key_safety_policy import RuntimeKeySafetyPolicyRecord
from control_plane.lane_usage import evaluate_lane_usage

LaneConfigurationRecord = (
    RuntimeEnvironmentRecord
    | SecretRecord
    | SecretBinding
    | OdooInstanceOverrideRecord
    | RuntimeKeySafetyPolicyRecord
)


class DisposableLaneSafetyUnknown(ValueError):
    """The service lacks current facts needed to treat this lane as disposable."""


class DisposableLaneStore(Protocol):
    def list_runtime_key_safety_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[RuntimeKeySafetyPolicyRecord, ...]: ...

    def read_odoo_instance_override_record(
        self, *, context_name: str, instance_name: str
    ) -> OdooInstanceOverrideRecord: ...

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...

    def read_disposable_lane_safety(
        self, product: str, context: str, instance: str
    ) -> DisposableLaneSafety | None: ...

    def list_dokploy_target_id_records(self) -> tuple[DokployTargetIdRecord, ...]: ...

    def list_preview_records(self, *, context_name: str = "") -> tuple[PreviewRecord, ...]: ...

    def list_product_profile_records(
        self, *, driver_id: str = ""
    ) -> tuple[LaunchplaneProductProfileRecord, ...]: ...

    def list_runtime_environment_records(
        self, *, scope: str = "", context_name: str = "", instance_name: str = ""
    ) -> tuple[RuntimeEnvironmentRecord, ...]: ...

    def list_secret_records(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretRecord, ...]: ...

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]: ...


def disposable_lane_binding_sha256(
    profile: LaunchplaneProductProfileRecord,
    target: DokployTargetRecord | None,
    target_id: DokployTargetIdRecord | None,
    preview: PreviewRecord | None = None,
    configuration: tuple[LaneConfigurationRecord, ...] = (),
    *,
    preview_lifecycle: bool = True,
) -> str:
    # The observation itself is not part of the binding it observes. Updating
    # timestamps or retirement lifecycle state does not change lane ownership.
    payload = profile.model_dump(mode="json", exclude={"updated_at", "lifecycle_state", "source"})
    # Retirement disables future preview admission while retaining its ownership.
    preview_profile = payload.get("preview")
    if isinstance(preview_profile, dict):
        preview_profile.pop("enabled", None)
    bindings: list[object] = [
        payload,
        *(item.model_dump(mode="json") if item else None for item in (target, target_id)),
        (
            preview.model_dump(
                mode="json",
                include=(
                    None
                    if preview_lifecycle
                    else {
                        "preview_id",
                        "context",
                        "anchor_repo",
                        "anchor_pr_number",
                        "anchor_pr_url",
                        "canonical_url",
                        "state",
                    }
                ),
            )
            if preview
            else None
        ),
    ]
    bindings.append(
        sorted(
            (item.model_dump(mode="json") for item in configuration),
            key=lambda item: json.dumps(item, sort_keys=True),
        )
    )
    return hashlib.sha256(
        json.dumps(bindings, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def require_disposable_lane(
    store: DisposableLaneStore,
    *,
    product: str,
    context: str,
    instance: str,
    retirement: bool = False,
    separation_only: bool = False,
    preview_refresh: bool = False,
    preview: PreviewRecord | None = None,
    now: datetime | None = None,
    testing_hold_actor: str = "",
    usage_store: object | None = None,
) -> str:
    profile = store.read_product_profile_record(product)
    if instance == "prod" or any(
        lane.instance == instance and lane.instance != "testing" for lane in profile.lanes
    ):
        raise DisposableLaneSafetyUnknown("Production cannot use disposable-lane permission.")
    if preview is None and instance != "testing":
        preview = resolve_disposable_preview(store, profile, context, instance)
    stable = [
        lane for lane in profile.lanes if (lane.context, lane.instance) == (context, instance)
    ]
    owned_preview = (
        preview is not None
        and preview.context == context == profile.preview.context
        and instance == profile.preview.slug_template.format(number=preview.anchor_pr_number)
    )
    if not ((instance == "testing" and len(stable) == 1) or owned_preview):
        raise DisposableLaneSafetyUnknown(
            "Disposable operation requires an exact owned testing or preview lane."
        )
    if (
        retirement
        and profile.production_use_authority == "director_agent"
        and any(lane.instance != "testing" for lane in profile.lanes)
    ):
        raise DisposableLaneSafetyUnknown(
            "Agent-attested never-live retirement requires testing-only ownership."
        )
    if retirement and profile.production_use != "prelaunch":
        raise DisposableLaneSafetyUnknown(
            "Agent product retirement requires a product recorded never live."
        )
    try:
        target = store.read_dokploy_target_record(context_name=context, instance_name=instance)
    except FileNotFoundError:
        target = None
    try:
        target_id = store.read_dokploy_target_id_record(
            context_name=context, instance_name=instance
        )
    except FileNotFoundError:
        target_id = None
    if target_id is not None and any(
        (other.context, other.instance) != (context, instance)
        and other.target_id == target_id.target_id
        for other in store.list_dokploy_target_id_records()
    ):
        raise DisposableLaneSafetyUnknown(
            "The testing or preview target aliases another lane's target."
        )
    rehearsal_binding = ""
    if target is not None and target.policies.staff_testing_hold is not None:
        hold = target.policies.staff_testing_hold
        if (
            not testing_hold_actor
            or instance != "testing"
            or retirement
            or separation_only
            or preview_refresh
            or hold.purpose != "staff_testing"
            or hold.recorded_by != testing_hold_actor
            or hold.rehearsal is None
        ):
            raise DisposableLaneSafetyUnknown(
                "The lane is held for staff testing; usage must be resolved before disposable work."
            )
        # Only undo this service's rehearsal-hold metadata for qualification.
        # All actual resource/configuration changes still invalidate the observation.
        rehearsal_binding = hold.rehearsal.binding_sha256
        target = target.model_copy(
            update={
                "policies": target.policies.model_copy(update={"staff_testing_hold": None}),
                "updated_at": hold.rehearsal.target_updated_at,
                "source_label": hold.rehearsal.target_source_label,
            }
        )
    evidence = store.read_disposable_lane_safety(product, context, instance)
    usage = evaluate_lane_usage(
        usage_store if usage_store is not None else store,
        profile=profile,
        target=target,
        target_id=target_id,
        instance=instance,
        usage=evidence.usage if evidence else "unknown",
        require_unused=not (preview_refresh or retirement),
        now=now,
    )
    if preview_refresh:
        # Overall direction makes previews disposable unless there is an actual
        # protected use or needed copy. No source produces a universal positive
        # occupancy/custody fact. Resource safety is checked separately against
        # the real Docker host immediately before refresh effects.
        if not owned_preview or profile.driver_id != "odoo":
            raise DisposableLaneSafetyUnknown(
                "Refresh qualification requires an owned Odoo preview."
            )
        if preview is not None and preview.state != "active":
            raise DisposableLaneSafetyUnknown("Only an active existing preview can be refreshed.")
        if evidence is not None and (
            usage.protected
            or evidence.data == "only_copy"
            or evidence.production_resources in {"shared", "separation_ready"}
        ):
            # Expiry or config drift cannot silently erase a protective report.
            raise DisposableLaneSafetyUnknown(
                "The preview has a recorded protected use or resource."
            )
        binding = disposable_lane_binding_sha256(
            profile,
            target,
            target_id,
            preview,
            disposable_lane_configuration(store, context, instance),
            preview_lifecycle=False,
        )
        return binding
    if retirement:
        # Prelaunch is the product's authoritative disposable classification.
        # Retirement stops an owned application; it neither starts a lane nor
        # needs a recipe for reproducing disposable output. Known exceptions
        # remain holds, including old observations: expiry cannot erase them.
        if evidence is not None and (
            usage.protected
            or evidence.data == "only_copy"
            or evidence.production_resources in {"shared", "separation_ready"}
        ):
            raise DisposableLaneSafetyUnknown(
                "Retirement is held by recorded usage, needed data or shared resources."
            )
        retired_secret_ids = {
            item.secret_id
            for item in store.list_secret_records(context_name=context, instance_name=instance)
        }
        if any(
            item.secret_id in retired_secret_ids
            and item.status != "disabled"
            and (item.context, item.instance) != (context, instance)
            for item in store.list_secret_bindings()
        ):
            raise DisposableLaneSafetyUnknown(
                "Retirement would disable a secret referenced outside this lane."
            )
        return disposable_lane_binding_sha256(
            profile,
            target,
            target_id,
            preview,
            disposable_lane_configuration(store, context, instance),
        )
    if evidence is None:
        raise DisposableLaneSafetyUnknown(
            "Disposable lane usage, only-copy and production-isolation evidence is unavailable."
        )
    current_time = now or datetime.now(timezone.utc)
    if not evidence.observed_at <= current_time < evidence.expires_at:
        raise DisposableLaneSafetyUnknown(
            "Disposable lane safety evidence is stale or not yet valid."
        )
    if evidence.binding_sha256 != disposable_lane_binding_sha256(
        profile, target, target_id, preview, disposable_lane_configuration(store, context, instance)
    ):
        raise DisposableLaneSafetyUnknown(
            "Disposable lane bindings changed after the safety observation."
        )
    if rehearsal_binding and evidence.binding_sha256 != rehearsal_binding:
        raise DisposableLaneSafetyUnknown("The rehearsal's original lane binding changed.")
    from control_plane.testing_never_served import (
        NON_USE_EVIDENCE_PREFIX,
        read_never_served_testing_usage,
    )

    if evidence.usage_evidence.startswith(NON_USE_EVIDENCE_PREFIX):
        reader = getattr(store, "read_testing_non_use_records", None)
        qualified = (
            read_never_served_testing_usage(
                store, product=product, context=context, target_id=target_id.target_id
            )
            if callable(reader) and target_id is not None
            else None
        )
        if (
            qualified is None
            or qualified.usage != "unused"
            or evidence.usage_evidence != NON_USE_EVIDENCE_PREFIX + qualified.proof_sha256
        ):
            raise DisposableLaneSafetyUnknown(
                "The never-served generation proof is no longer current."
            )
    allowed_resources = {"isolated", "separation_ready"} if separation_only else {"isolated"}
    if (
        usage.protected
        or evidence.data != "reproducible"
        or evidence.production_resources not in allowed_resources
    ):
        raise DisposableLaneSafetyUnknown(
            "The lane is in use, holds needed data, shares production resources, or safety is unknown."
        )
    return evidence.binding_sha256


def resolve_disposable_preview(
    store: DisposableLaneStore,
    profile: LaunchplaneProductProfileRecord,
    context: str,
    instance: str,
) -> PreviewRecord:
    """Resolve an actual uniquely owned preview; a slug alone is never authority."""
    if instance == "prod" or context != profile.preview.context:
        raise DisposableLaneSafetyUnknown("The requested lane is not this product's preview.")
    if any(
        other.product != profile.product
        and context
        in {
            other.preview.context,
            *other.historical_contexts,
            *(lane.context for lane in other.lanes),
        }
        for other in store.list_product_profile_records()
    ):
        raise DisposableLaneSafetyUnknown("The preview context has shared or uncertain ownership.")
    matches = [
        item
        for item in store.list_preview_records(context_name=context)
        if profile.preview.slug_template.format(number=item.anchor_pr_number) == instance
    ]
    if (
        len(matches) != 1
        or matches[0].anchor_repo
        not in {
            profile.repository,
            profile.repository.partition("/")[2],
        }
        or matches[0].state == "destroyed"
    ):
        raise DisposableLaneSafetyUnknown(
            "The preview is absent, ambiguous or owned by another product."
        )
    return matches[0]


def disposable_lane_configuration(
    store: DisposableLaneStore, context: str, instance: str
) -> tuple[LaneConfigurationRecord, ...]:
    # Odoo previews copy their template lane, including bootstrap overrides.
    # Bind the actual copied inputs, not just records under the preview slug.
    instances = {"", instance}
    overrides: list[OdooInstanceOverrideRecord] = []
    policies: tuple[RuntimeKeySafetyPolicyRecord, ...] = ()
    # Stable testing recovery uses an atomic lane snapshot, not a preview's
    # template. Keep that snapshot independent of the product catalog.
    profiles = (
        []
        if instance == "testing"
        else [
            profile
            for profile in store.list_product_profile_records(driver_id="odoo")
            if profile.preview.context == context
            and not any(
                (lane.context, lane.instance) == (context, instance) for lane in profile.lanes
            )
        ]
    )
    if len(profiles) == 1:
        policies = store.list_runtime_key_safety_policy_records(status="active", limit=1)
        template = profiles[0].preview.template_instance
        instances.add(template)
        try:
            overrides.append(
                store.read_odoo_instance_override_record(
                    context_name=context, instance_name=template
                )
            )
        except FileNotFoundError:
            pass
    # Site values and version pointers stay inside the service and only enter
    # the digest. Secret values are never read or returned.
    bindings = tuple(
        item
        for item in store.list_secret_bindings(integration="runtime_environment")
        if item.context in ("", context) and item.instance in instances
    )
    referenced_secrets = {item.secret_id for item in bindings}
    return (
        *(
            item
            for item in store.list_runtime_environment_records()
            if item.context in ("", context) and item.instance in instances
        ),
        *(
            item
            for item in store.list_secret_records(integration="runtime_environment")
            if (item.context in ("", context) and item.instance in instances)
            or item.secret_id in referenced_secrets
        ),
        *bindings,
        *overrides,
        *policies,
    )
