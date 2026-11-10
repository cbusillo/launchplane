from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.odoo_website_settings import OdooWebsiteIndexing
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_topology_read_model import build_product_environment_topology
from control_plane.contracts.public_hosts import resolve_public_base_url
from control_plane.contracts.runtime_identity import parse_runtime_identity_payload
from control_plane.dokploy import api, source
from control_plane.dokploy.website_indexing import run_native_indexing
from control_plane.odoo_product_driver_http import resolve_odoo_product_route
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderObservation,
    ProviderOperationLease,
)


Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class NativeIndexingPlan(OdooWebsiteIndexing):
    field: Literal["custom_code_head"]
    before_sha256: Digest
    after_sha256: Digest
    changed: StrictBool
    managed_noindex_before: StrictBool
    managed_noindex_after: StrictBool


class WebsiteIndexingPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_sha256: Digest
    database_uuid: str = Field(min_length=1)
    native: NativeIndexingPlan
    plan_sha256: Digest
    committed: StrictBool


class WebsiteIndexingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["dry-run", "apply", "readback"]
    website_id: int | None = Field(default=None, ge=1, strict=True)
    public_origin: str
    reviewed_plan: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_request(self) -> WebsiteIndexingRequest:
        OdooWebsiteIndexing(
            website_id=self.website_id or 1,
            public_origin=self.public_origin,
            production_use="prelaunch",
        )
        if self.reviewed_plan is not None:
            WebsiteIndexingPlan.model_validate(self.reviewed_plan)
        return self


class WebsiteIndexingUnknown(RuntimeError):
    pass


FIRST_LIVE_SCOPE = "launchplane:client-release"
FIRST_LIVE_ROUTE = "service:first-live-indexing"


def first_live_inverse_started(store: Any, decision: Any) -> bool:
    return (
        store.read_idempotency_record(
            scope=FIRST_LIVE_SCOPE,
            route_path=FIRST_LIVE_ROUTE,
            idempotency_key=decision.record_id + ":inverse",
        )
        is not None
    )


def production_origin(store: Any, profile: LaunchplaneProductProfileRecord) -> str:
    lane = next(lane for lane in profile.lanes if lane.instance == "prod")
    try:
        target = store.read_dokploy_target_record(context_name=lane.context, instance_name="prod")
    except FileNotFoundError:
        return lane.base_url.rstrip("/")
    return resolve_public_base_url(
        instance="prod", public_hosts=target.public_hosts
    ) or lane.base_url.rstrip("/")


def indexing_binding(store: Any, product: str, context: str, origin: str) -> dict[str, Any]:
    from control_plane.contracts.deploy_target import ProviderTargetRecord
    from control_plane.route_binding_reconcile import provider_target_projection

    profile = resolve_odoo_product_route(
        record_store=store, product=product, context=context, instance="prod"
    )
    if not profile.is_active or profile.production_use not in {"prelaunch", "live"}:
        raise ValueError("Website indexing requires an active product with known production use.")
    lane = next(
        lane for lane in profile.lanes if lane.context == context and lane.instance == "prod"
    )
    topology = build_product_environment_topology(
        record_store=store, profile=profile, lane=lane, lane_summary=None
    )
    if topology.provider_recorded.trust_state not in {"verified", "recorded"} or any(
        warning.code
        in {"stale_route_authority", "missing_route_authority", "route_authority_disabled"}
        for warning in topology.warnings
    ):
        raise ValueError("Website indexing requires current route authority.")
    if production_origin(store, profile) != origin.rstrip("/"):
        raise ValueError("Website indexing origin differs from production.")
    target = store.read_provider_target_record(context_name=context, instance_name="prod")
    expected_target = ProviderTargetRecord.from_dokploy_records(
        target_record=store.read_dokploy_target_record(context_name=context, instance_name="prod"),
        target_id_record=store.read_dokploy_target_id_record(
            context_name=context, instance_name="prod"
        ),
    )
    if provider_target_projection(target) != provider_target_projection(expected_target):
        raise ValueError("Website indexing target records differ.")
    route = store.read_route_binding_record(
        product=product, context_name=context, instance_name="prod"
    )
    if (
        target.provider_id != "dokploy"
        or target.target_category != "compose"
        or route.provider_target.provider_id != target.provider_id
        or route.provider_target.target_category != target.target_category
        or route.provider_target.provider_target_type != target.provider_target_type
        or route.provider_target.target_name != target.display_name
        or origin.split("://", 1)[1].rstrip("/")
        not in {domain.domain_name for domain in route.domains}
    ):
        raise ValueError("Website indexing route and provider target differ.")
    return {
        "profile": profile.model_dump(
            mode="json", include={"product", "driver_id", "image", "production_use"}
        ),
        "context": context,
        "target": target.model_dump(
            mode="json", include={"provider_id", "target_category", "target_id", "display_name"}
        ),
        "route": {
            "ingress": route.ingress.model_dump(
                mode="json", include={"provider", "endpoint_key", "termination_kind"}
            ),
            "domains": [domain.model_dump(mode="json") for domain in route.domains],
        },
        "public_origin": origin.rstrip("/"),
    }


@dataclass
class WebsiteIndexingExecution:
    store: Any
    root: Path
    product: str
    context: str
    request: WebsiteIndexingRequest
    desired_state: Literal["prelaunch", "live"] | None = None
    require_authority: Callable[[], None] | None = None
    expected_artifact_id: str = ""

    def observe(
        self,
        *,
        reviewed: dict[str, Any] | None = None,
        before_mutation: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        binding = indexing_binding(
            self.store, self.product, self.context, self.request.public_origin
        )
        target = binding["target"]
        host, token = source.read_dokploy_config(
            control_plane_root=self.root, database_url=self.store.database_url
        )
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=target["target_id"]
        )
        environment = api.parse_dokploy_env_text(str(payload.get("env") or ""))
        runtime = parse_runtime_identity_payload(
            environment.get("LAUNCHPLANE_RUNTIME_IDENTITY_JSON", "")
        )
        artifact_id = (
            self.expected_artifact_id
            or self.store.read_release_tuple_record(
                context_name=self.context, channel_name="prod"
            ).artifact_id
        )
        if (
            runtime is None
            or not runtime.image_reference
            or (runtime.product, runtime.context, runtime.instance, runtime.artifact_id)
            != (self.product, self.context, "prod", artifact_id)
            or not payload.get("serverId")
            or payload.get("composeId") != target["target_id"]
            or not payload.get("appName")
            or not environment.get("ODOO_DB_NAME")
        ):
            raise ValueError("Website indexing requires the exact production runtime.")
        manifest = self.store.read_artifact_manifest(artifact_id)
        if (
            runtime.image_reference != f"{manifest.image.repository}@{manifest.image.digest}"
            or runtime.source_git_ref != manifest.source_commit
            or manifest.image.repository != binding["profile"]["image"]["repository"]
        ):
            raise ValueError("Website indexing runtime differs from its artifact authority.")
        transport_binding = {
            "project": payload["appName"],
            "database": environment["ODOO_DB_NAME"],
            "runtime": json.loads(environment["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"]),
            "image": runtime.image_reference,
        }
        identity = {**binding, "runtime": transport_binding, "server_id": payload["serverId"]}
        identity_digest = canonical_json_sha256(identity)
        state = self.desired_state or binding["profile"]["production_use"]
        selector = {
            "website_id": self.request.website_id,
            "public_origin": binding["public_origin"],
            "production_use": state,
        }
        if reviewed is not None:
            if reviewed.get("binding_sha256") != identity_digest:
                raise ValueError("Website indexing binding changed after review.")
            native = reviewed["native"]
            if (
                self.request.website_id is not None
                and self.request.website_id != native["website_id"]
            ):
                raise ValueError("Website indexing reviewed website differs.")
            selector["website_id"] = native["website_id"]
            if any(
                selector[key] != native[key]
                for key in ("website_id", "public_origin", "production_use")
            ):
                raise ValueError("Website indexing reviewed selector differs.")
        native_request: dict[str, object] = {
            "mode": "apply" if reviewed is not None else "dry-run",
            "indexing": selector,
            "database_uuid": reviewed["database_uuid"]
            if reviewed is not None
            else runtime.database_uuid,
        }
        if reviewed is not None:
            native_request["reviewed_native"] = reviewed["native"]

        def checkpoint() -> None:
            if self.require_authority is not None:
                self.require_authority()
            if (
                indexing_binding(self.store, self.product, self.context, self.request.public_origin)
                != binding
            ):
                raise ValueError("Website indexing authority changed before mutation.")
            fresh = api.fetch_dokploy_target_payload(
                host=host, token=token, target_type="compose", target_id=target["target_id"]
            )
            if any(fresh.get(key) != payload.get(key) for key in ("serverId", "appName", "env")):
                raise ValueError("Website indexing provider binding changed before mutation.")
            if before_mutation is not None:
                before_mutation()

        result = run_native_indexing(
            host=host,
            token=token,
            server_id=str(payload["serverId"]),
            binding=transport_binding,
            request=native_request,
            before_mutation=checkpoint if reviewed is not None else None,
        )
        raw_native = result.get("native")
        if result.get("status") != "complete" or not isinstance(raw_native, dict):
            raise WebsiteIndexingUnknown("Native indexing did not prove its outcome.")
        native_result = dict(raw_native)
        committed = native_result.pop("committed", None)
        database_uuid = native_result.pop("database_uuid", None)
        if (
            not isinstance(database_uuid, str)
            or not database_uuid.strip()
            or (
                native_request["database_uuid"] and database_uuid != native_request["database_uuid"]
            )
        ):
            raise WebsiteIndexingUnknown("Native indexing database binding is unverified.")
        NativeIndexingPlan.model_validate(native_result)
        if (
            committed is not (reviewed is not None)
            or any(
                native_result.get(key) != value
                for key, value in selector.items()
                if value is not None
            )
            or native_result.get("field") != "custom_code_head"
            or native_result.get("managed_noindex_after") is not (state == "prelaunch")
        ):
            raise WebsiteIndexingUnknown("Native indexing readback does not match its request.")
        if reviewed is not None and native_result != reviewed["native"]:
            raise WebsiteIndexingUnknown("Native indexing readback does not match its baseline.")
        plan = {
            "binding_sha256": identity_digest,
            "database_uuid": database_uuid,
            "native": native_result,
        }
        return {**plan, "plan_sha256": canonical_json_sha256(plan), "committed": committed}


@dataclass
class WebsiteIndexingAdapter:
    execution: WebsiteIndexingExecution
    plan: dict[str, Any]
    trace_id: str

    def __post_init__(self) -> None:
        WebsiteIndexingPlan.model_validate(self.plan)
        expected = canonical_json_sha256(
            {key: self.plan[key] for key in ("binding_sha256", "database_uuid", "native")}
        )
        if self.plan.get("plan_sha256") != expected:
            raise ValueError("Website indexing plan digest differs from its reviewed content.")

    def target_key(self) -> str:
        return f"odoo-website-indexing:{self.execution.product}:{self.execution.context}:prod"

    def reconciliation_key(self) -> str:
        return str(self.plan["plan_sha256"])

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        del provider_operation_key
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation(outcome="unknown")
        if not provider_effect_phase:
            return ProviderObservation(outcome="absent")
        try:
            current = self.execution.observe()
        except (ValueError, RuntimeError, FileNotFoundError):
            return ProviderObservation(outcome="unknown")
        if (current["binding_sha256"], current["database_uuid"]) != (
            self.plan["binding_sha256"],
            self.plan["database_uuid"],
        ) or any(
            current["native"][key] != self.plan["native"][key]
            for key in ("website_id", "public_origin", "production_use")
        ):
            return ProviderObservation(outcome="unknown")
        if (
            current["native"]["before_sha256"] == self.plan["native"]["after_sha256"]
            and not current["native"]["changed"]
        ):
            return ProviderObservation(
                outcome="present",
                response_status_code=202,
                response_payload=self.response({**self.plan, "committed": True}),
            )
        if current["native"] == self.plan["native"]:
            return ProviderObservation(outcome="absent", retry_safe=True)
        return ProviderObservation(outcome="unknown")

    def response(self, result: dict[str, Any]) -> dict[str, object]:
        return {"status": "accepted", "trace_id": self.trace_id, "records": {}, "result": result}

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        del provider_operation_key
        started = False

        def checkpoint() -> None:
            nonlocal started
            lease.assert_current()
            lease.checkpoint_effect("website-indexing")
            started = True

        try:
            result = self.execution.observe(reviewed=self.plan, before_mutation=checkpoint)
        except Exception as error:
            if not started:
                raise ProviderMutationRejectedError(error) from error
            raise
        return ProviderMutationOutcome(
            response_status_code=202,
            response_payload=self.response(result),
            provider_effect_performed=started,
        )


def first_live_state_completed(store: Any, decision: Any) -> bool:
    receipt = store.read_idempotency_record(
        scope=FIRST_LIVE_SCOPE,
        route_path=FIRST_LIVE_ROUTE,
        idempotency_key=decision.record_id + ":state",
    )
    return receipt is not None and receipt.state == "completed"


def complete_first_live(store: Any, root: Path, profile: Any, decision: Any) -> None:
    from control_plane.client_release import accepted_release_can_start
    from control_plane.provider_operations import run_durable_provider_operation
    from control_plane.release_review import release_version
    from control_plane.storage.postgres import DbOnlyMutationRequest
    from control_plane.timestamps import utc_now_timestamp

    # A completed acceptance is spent. It cannot override a later classification.
    if first_live_state_completed(store, decision):
        return
    if (
        profile.driver_id != "odoo"
        or profile.production_use != "prelaunch"
        or decision.decision != "accepted"
        or not decision.checklist.first_live_origin
    ):
        raise ValueError("First live completion requires its prelaunch accepted candidate.")
    if first_live_inverse_started(store, decision):
        _restore_first_live_protection(store, root, profile, decision)
        return
    context = next(lane.context for lane in profile.lanes if lane.instance == "prod")
    scope, route, key = FIRST_LIVE_SCOPE, FIRST_LIVE_ROUTE, decision.record_id
    original = store.read_idempotency_record(scope=scope, route_path=route, idempotency_key=key)

    def forward_authorized() -> bool:
        authority_profile = store.read_product_profile_record(profile.product)
        return (
            authority_profile.production_use == "prelaunch"
            and accepted_release_can_start(store, decision)
            and production_origin(store, authority_profile) == decision.checklist.first_live_origin
            and release_version(store=store, profile=authority_profile, instance="prod")
            == decision.checklist.candidate
        )

    def require_acceptance() -> None:
        if not forward_authorized():
            raise ValueError("First live authority changed before mutation.")

    execution = WebsiteIndexingExecution(
        store,
        root,
        profile.product,
        context,
        WebsiteIndexingRequest(mode="apply", public_origin=decision.checklist.first_live_origin),
        "live",
        require_acceptance,
    )
    if original is None:
        require_acceptance()
        plan = execution.observe()
    else:
        plan = store.read_original_mutation_intent(original)["plan"]
    trace = f"first-live-{decision.record_id}"
    adapter = WebsiteIndexingAdapter(execution, plan, trace)
    if forward_authorized():
        result = run_durable_provider_operation(
            store=store,
            scope=scope,
            route_path=route,
            idempotency_key=key,
            request_fingerprint=canonical_json_sha256(
                {"decision": decision.record_id, "plan": plan}
            ),
            lease_owner=trace,
            response_trace_id=trace,
            adapter=adapter,
            original_intent={"plan": plan},
        )
        if (
            result.status not in {"completed", "adopted", "replayed"}
            or result.response_status_code != 202
        ):
            raise WebsiteIndexingUnknown(
                "The original first live indexing operation needs reconciliation."
            )
    # Even a completed provider receipt needs fresh after-image proof before the state CAS.
    observation = adapter.observe("", "website-indexing", adapter.reconciliation_key())
    if observation.outcome != "present":
        raise WebsiteIndexingUnknown(
            "First live native after-image is unverified; retain the original operation."
        )
    for _ in range(3):
        current = store.read_product_profile_record(profile.product)
        if not forward_authorized():
            break
        replacement = current.model_copy(
            update={
                "production_use": "live",
                "production_use_authority": "explicit",
                "updated_at": utc_now_timestamp(),
            }
        )
        changed = store.compare_and_write_product_profile_record(
            expected_record=current,
            replacement_record=replacement,
            mutation=DbOnlyMutationRequest(
                scope=scope,
                route_path=route,
                idempotency_key=key + ":state",
                request_fingerprint=canonical_json_sha256({"decision": key, "plan": plan}),
                lease_owner=trace,
                response_status_code=200,
                response_trace_id=trace,
                response_payload={"decision": key, "production_use": "live", "plan": plan},
            ),
        )
        if changed.status in {"written", "replayed"}:
            return
        if changed.status != "changed":
            break
    if forward_authorized():
        raise WebsiteIndexingUnknown(
            "First live state completion is pending; retain the original operation."
        )
    if store.read_product_profile_record(profile.product).production_use != "prelaunch":
        raise WebsiteIndexingUnknown(
            "First live authority changed; reconcile against current production use."
        )

    _restore_first_live_protection(store, root, profile, decision, forward_plan=plan)


def first_live_inverse_pending(store: Any, decision: Any) -> bool:
    original = store.read_idempotency_record(
        scope=FIRST_LIVE_SCOPE,
        route_path=FIRST_LIVE_ROUTE,
        idempotency_key=decision.record_id + ":inverse",
    )
    return original is not None and original.state != "completed"


def _restore_first_live_protection(
    store: Any,
    root: Path,
    profile: Any,
    decision: Any,
    *,
    forward_plan: dict[str, Any] | None = None,
) -> None:
    from control_plane.provider_operations import run_durable_provider_operation

    scope, route, key = FIRST_LIVE_SCOPE, FIRST_LIVE_ROUTE, decision.record_id
    trace = f"first-live-{key}"
    context = next(lane.context for lane in profile.lanes if lane.instance == "prod")
    original = store.read_idempotency_record(
        scope=scope, route_path=route, idempotency_key=key + ":inverse"
    )
    if original is not None and original.state == "completed":
        return

    def require_prelaunch() -> None:
        if store.read_product_profile_record(profile.product).production_use != "prelaunch":
            raise ValueError("The inverse no longer matches recorded production use.")

    inverse = WebsiteIndexingExecution(
        store,
        root,
        profile.product,
        context,
        WebsiteIndexingRequest(mode="apply", public_origin=decision.checklist.first_live_origin),
        "prelaunch",
        require_prelaunch,
    )
    if original is not None:
        inverse_plan = store.read_original_mutation_intent(original)["plan"]
    else:
        if forward_plan is None:
            raise ValueError("An inverse requires its original forward proof.")
        inverse_plan = inverse.observe()
        if (inverse_plan["binding_sha256"], inverse_plan["database_uuid"]) != (
            forward_plan["binding_sha256"],
            forward_plan["database_uuid"],
        ) or inverse_plan["native"]["before_sha256"] != forward_plan["native"]["after_sha256"]:
            raise WebsiteIndexingUnknown("First live inverse binding or baseline is unknown.")
    restored = run_durable_provider_operation(
        store=store,
        scope=scope,
        route_path=route,
        idempotency_key=key + ":inverse",
        request_fingerprint=canonical_json_sha256(inverse_plan),
        lease_owner=trace + ":inverse",
        response_trace_id=trace,
        adapter=WebsiteIndexingAdapter(inverse, inverse_plan, trace),
        original_intent={"plan": inverse_plan},
    )
    if (
        restored.status not in {"completed", "adopted", "replayed"}
        or restored.response_status_code != 202
    ):
        raise WebsiteIndexingUnknown("First live indexing inverse needs reconciliation.")
