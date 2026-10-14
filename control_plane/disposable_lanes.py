"""Lane eligibility is separate from identity and from historical recovery proof."""

from datetime import datetime, timezone
import hashlib
import json
from typing import Protocol

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord


class DisposableLaneSafetyUnknown(ValueError):
    """The service lacks current facts needed to treat this lane as disposable."""


class DisposableLaneStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...

    def list_dokploy_target_id_records(self) -> tuple[DokployTargetIdRecord, ...]: ...

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
    configuration: tuple[RuntimeEnvironmentRecord | SecretRecord | SecretBinding, ...] = (),
) -> str:
    # The observation itself is not part of the binding it observes. Updating
    # timestamps or retirement lifecycle state does not change lane ownership.
    payload = profile.model_dump(
        mode="json", exclude={"disposable_lane_safety", "updated_at", "lifecycle_state", "source"}
    )
    # Retirement disables future preview admission while retaining its ownership.
    preview_profile = payload.get("preview")
    if isinstance(preview_profile, dict):
        preview_profile.pop("enabled", None)
    bindings: list[object] = [
        payload,
        *(item.model_dump(mode="json") if item else None for item in (target, target_id, preview)),
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
    preview: PreviewRecord | None = None,
    now: datetime | None = None,
) -> str:
    profile = store.read_product_profile_record(product)
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
        other.instance == "prod" and other.target_id == target_id.target_id
        for other in store.list_dokploy_target_id_records()
    ):
        raise DisposableLaneSafetyUnknown(
            "The testing or preview target aliases a production target."
        )
    if target is not None and target.policies.staff_testing_hold is not None:
        raise DisposableLaneSafetyUnknown(
            "The lane is held for staff testing; usage must be resolved before disposable work."
        )
    observations = [
        item
        for item in profile.disposable_lane_safety
        if (item.context, item.instance) == (context, instance)
    ]
    if len(observations) != 1:
        raise DisposableLaneSafetyUnknown(
            "Disposable lane usage, only-copy and production-isolation evidence is unavailable."
        )
    evidence = observations[0]
    current_time = now or datetime.now(timezone.utc)
    if not evidence.observed_at <= current_time < evidence.expires_at:
        raise DisposableLaneSafetyUnknown(
            "Disposable lane safety evidence is stale or not yet valid."
        )
    if evidence.binding_sha256 != disposable_lane_binding_sha256(
        profile, target, target_id, preview, disposable_lane_configuration(store, context)
    ):
        raise DisposableLaneSafetyUnknown(
            "Disposable lane bindings changed after the safety observation."
        )
    allowed_resources = {"isolated", "separation_ready"} if separation_only else {"isolated"}
    if (
        evidence.usage != "unused"
        or evidence.data != "reproducible"
        or evidence.production_resources not in allowed_resources
    ):
        raise DisposableLaneSafetyUnknown(
            "The lane is in use, holds needed data, shares production resources, or safety is unknown."
        )
    return evidence.binding_sha256


def disposable_lane_configuration(
    store: DisposableLaneStore, context: str
) -> tuple[RuntimeEnvironmentRecord | SecretRecord | SecretBinding, ...]:
    # Site values and version pointers stay inside the service and only enter
    # the digest. Secret values are never read or returned.
    return (
        *store.list_runtime_environment_records(context_name=context),
        *store.list_secret_records(integration="runtime_environment", context_name=context),
        *store.list_secret_bindings(integration="runtime_environment", context_name=context),
    )
