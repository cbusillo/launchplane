from __future__ import annotations

import hashlib
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import (
    RuntimeEnvironmentDeleteEvent,
    RuntimeEnvironmentRecord,
)
from control_plane.contracts.secret_record import (
    SecretAuditEvent,
    SecretBinding,
    SecretRecord,
    SecretVersion,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.ship import utc_now_timestamp


ContextCutoverMode = Literal["dry-run", "apply"]
LegacyContextCleanupMode = Literal["dry-run", "apply"]


class ProductContextCutoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    source_context: str
    target_context: str
    mode: ContextCutoverMode = "dry-run"
    display_name: str = ""
    source_label: str = "service:product-context-cutover"

    @model_validator(mode="after")
    def _validate_request(self) -> "ProductContextCutoverRequest":
        self.product = self.product.strip()
        self.source_context = self.source_context.strip()
        self.target_context = self.target_context.strip()
        self.display_name = self.display_name.strip()
        self.source_label = self.source_label.strip() or "service:product-context-cutover"
        if not self.product:
            raise ValueError("Product context cutover requires product.")
        if not self.source_context or not self.target_context:
            raise ValueError("Product context cutover requires source_context and target_context.")
        if self.source_context == self.target_context:
            raise ValueError(
                "Product context cutover source_context and target_context must differ."
            )
        return self


class LegacyContextCleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    source_context: str
    target_context: str
    mode: LegacyContextCleanupMode = "dry-run"
    actor: str = ""
    source_label: str = "service:legacy-context-cleanup"

    @model_validator(mode="after")
    def _validate_request(self) -> "LegacyContextCleanupRequest":
        self.product = self.product.strip()
        self.source_context = self.source_context.strip()
        self.target_context = self.target_context.strip()
        self.actor = self.actor.strip()
        self.source_label = self.source_label.strip() or "service:legacy-context-cleanup"
        if not self.product:
            raise ValueError("Legacy context cleanup requires product.")
        if not self.source_context or not self.target_context:
            raise ValueError("Legacy context cleanup requires source_context and target_context.")
        if self.source_context == self.target_context:
            raise ValueError(
                "Legacy context cleanup source_context and target_context must differ."
            )
        return self


class LegacyContextCleanupBoundaryError(ValueError):
    pass


def _slug(value: str) -> str:
    compact = "".join(character.lower() if character.isalnum() else "-" for character in value)
    return "-".join(part for part in compact.split("-") if part) or "value"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _target_secret_id(record: SecretRecord, *, target_context: str) -> str:
    parts = ["secret", record.integration, record.name]
    if target_context.strip():
        parts.append(target_context)
    if record.instance.strip():
        parts.append(record.instance)
    return "-".join(_slug(part) for part in parts)


def _target_secret_binding_id(*, secret_id: str, binding_key: str) -> str:
    return f"{secret_id}-binding-{_slug(binding_key)}"


def _target_secret_version_id(*, secret_id: str, source_version_id: str) -> str:
    return f"{secret_id}-version-copy-{_digest(source_version_id)}"


def _target_secret_event_id(*, secret_id: str, source_secret_id: str) -> str:
    return f"{secret_id}-event-imported-{_digest(source_secret_id)}"


def _summarize_counts(items: list[dict[str, object]]) -> dict[str, int]:
    return {
        "created": sum(1 for item in items if item.get("action") == "created"),
        "skipped": sum(1 for item in items if item.get("action") == "skipped"),
    }


def _summarize_actions(items: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        action = str(item.get("action") or "")
        if action:
            counts.setdefault(action, 0)
            counts[action] += 1
    return counts


def _profile_allowed_contexts(profile: LaunchplaneProductProfileRecord) -> frozenset[str]:
    contexts = {profile.product.strip()}
    contexts.update(lane.context.strip() for lane in profile.lanes if lane.context.strip())
    if profile.preview.enabled and profile.preview.context.strip():
        contexts.add(profile.preview.context.strip())
    return frozenset(context for context in contexts if context)


def _runtime_delete_event(
    *,
    record: RuntimeEnvironmentRecord,
    actor: str,
    source_label: str,
    now: str,
) -> RuntimeEnvironmentDeleteEvent:
    return RuntimeEnvironmentDeleteEvent(
        event_id=f"runtime-env-delete-{uuid.uuid4().hex[:12]}",
        recorded_at=now,
        actor=actor,
        scope=record.scope,
        context=record.context,
        instance=record.instance,
        source_label=record.source_label,
        env_keys=tuple(sorted(record.env.keys())),
        env_value_count=len(record.env),
        detail=f"deleted by {source_label}",
    )


def _secret_cleanup_event_id(secret_id: str) -> str:
    return f"{secret_id}-event-disabled-{uuid.uuid4().hex[:12]}"


def _record_target_context_exists(
    *,
    record_store: PostgresRecordStore,
    source_context: str,
    target_context: str,
    product: str,
) -> None:
    profile = record_store.read_product_profile_record(product)
    target_contexts = _profile_allowed_contexts(profile)
    if target_context not in target_contexts:
        raise LegacyContextCleanupBoundaryError(
            "Legacy context cleanup target_context is not owned by the product profile."
        )
    if source_context in target_contexts:
        raise LegacyContextCleanupBoundaryError(
            "Legacy context cleanup source_context is still owned by the product profile."
        )
    for other_profile in record_store.list_product_profile_records():
        if other_profile.product == product:
            continue
        if source_context in _profile_allowed_contexts(other_profile):
            raise LegacyContextCleanupBoundaryError(
                "Legacy context cleanup source_context is owned by another product profile."
            )


def _target_runtime_route_exists(
    *,
    record_store: PostgresRecordStore,
    source_record: RuntimeEnvironmentRecord,
    target_context: str,
) -> bool:
    return any(
        target.scope == source_record.scope and target.instance == source_record.instance
        for target in record_store.list_runtime_environment_records(context_name=target_context)
    )


def _target_secret_route_exists(
    *,
    record_store: PostgresRecordStore,
    source_record: SecretRecord,
    target_context: str,
) -> bool:
    return (
        record_store.find_secret_record(
            scope=source_record.scope,
            integration=source_record.integration,
            name=source_record.name,
            context=target_context,
            instance=source_record.instance,
        )
        is not None
    )


def _target_instance_exists(
    records: tuple[object, ...], *, target_context: str, instance: str
) -> bool:
    return any(
        getattr(record, "context", "") == target_context
        and getattr(record, "instance", "") == instance
        for record in records
    )


def _source_secret_bindings(
    *, record_store: PostgresRecordStore, record: SecretRecord
) -> tuple[SecretBinding, ...]:
    return tuple(
        binding
        for binding in record_store.list_secret_bindings(
            integration=record.integration,
            context_name=record.context,
            instance_name=record.instance,
            limit=None,
        )
        if binding.secret_id == record.secret_id
    )


def _profile_after_cutover(
    profile: LaunchplaneProductProfileRecord,
    *,
    source_context: str,
    target_context: str,
    display_name: str,
    now: str,
    source_label: str,
) -> LaunchplaneProductProfileRecord:
    source_preview_context = profile.preview.context.strip()
    lanes = tuple(
        lane.model_copy(update={"context": target_context})
        if lane.context == source_context or lane.instance in {"testing", "prod"}
        else lane
        for lane in profile.lanes
    )
    preview = profile.preview.model_copy(
        update={"context": target_context} if profile.preview.enabled else {}
    )
    historical_contexts = tuple(
        dict.fromkeys(
            context.strip()
            for context in (*profile.historical_contexts, source_context, source_preview_context)
            if context.strip() and context.strip() != target_context
        )
    )
    return profile.model_copy(
        update={
            "display_name": display_name or profile.display_name,
            "lanes": lanes,
            "historical_contexts": historical_contexts,
            "preview": preview,
            "updated_at": now,
            "source": source_label,
        }
    )


def _profile_semantic_payload(profile: LaunchplaneProductProfileRecord) -> dict[str, object]:
    return {
        "display_name": profile.display_name,
        "lanes": [lane.model_dump(mode="json") for lane in profile.lanes],
        "historical_contexts": list(profile.historical_contexts),
        "preview": profile.preview.model_dump(mode="json"),
    }


def plan_product_context_cutover(
    *,
    record_store: PostgresRecordStore,
    request: ProductContextCutoverRequest,
) -> dict[str, object]:
    profile = record_store.read_product_profile_record(request.product)
    source_runtime_records = record_store.list_runtime_environment_records(
        context_name=request.source_context
    )
    target_runtime_routes = {
        (record.scope, record.instance)
        for record in record_store.list_runtime_environment_records(
            context_name=request.target_context
        )
    }
    runtime_records = [
        {
            "scope": record.scope,
            "instance": record.instance,
            "env_keys": sorted(record.env.keys()),
            "env_value_count": len(record.env),
            "action": "skipped"
            if (record.scope, record.instance) in target_runtime_routes
            else "created",
        }
        for record in source_runtime_records
    ]

    source_target_records = tuple(
        record
        for record in record_store.list_dokploy_target_records()
        if record.context == request.source_context
    )
    target_target_instances = {
        record.instance
        for record in record_store.list_dokploy_target_records()
        if record.context == request.target_context
    }
    dokploy_targets = [
        {
            "instance": record.instance,
            "target_type": record.target_type,
            "target_name": record.target_name,
            "domains": list(record.domains),
            "env_keys": sorted(record.env.keys()),
            "env_value_count": len(record.env),
            "action": "skipped" if record.instance in target_target_instances else "created",
        }
        for record in source_target_records
    ]

    source_target_ids = tuple(
        record
        for record in record_store.list_dokploy_target_id_records()
        if record.context == request.source_context
    )
    target_id_instances = {
        record.instance
        for record in record_store.list_dokploy_target_id_records()
        if record.context == request.target_context
    }
    dokploy_target_ids = [
        {
            "instance": record.instance,
            "target_id": record.target_id,
            "action": "skipped" if record.instance in target_id_instances else "created",
        }
        for record in source_target_ids
    ]

    source_secret_records = record_store.list_secret_records(
        context_name=request.source_context,
        limit=None,
    )
    managed_secrets: list[dict[str, object]] = []
    for record in source_secret_records:
        target = record_store.find_secret_record(
            scope=record.scope,
            integration=record.integration,
            name=record.name,
            context=request.target_context,
            instance=record.instance,
        )
        bindings = tuple(
            binding
            for binding in record_store.list_secret_bindings(
                integration=record.integration,
                context_name=request.source_context,
                instance_name=record.instance,
                limit=None,
            )
            if binding.secret_id == record.secret_id
        )
        managed_secrets.append(
            {
                "name": record.name,
                "scope": record.scope,
                "integration": record.integration,
                "instance": record.instance,
                "binding_keys": sorted(binding.binding_key for binding in bindings),
                "action": "skipped" if target is not None else "created",
            }
        )

    source_inventory_records = tuple(
        record
        for record in record_store.list_environment_inventory()
        if record.context == request.source_context
    )
    target_inventory_instances = {
        record.instance
        for record in record_store.list_environment_inventory()
        if record.context == request.target_context
    }
    inventory_records = [
        {
            "instance": record.instance,
            "artifact_id": str(getattr(record.artifact_identity, "artifact_id", "") or ""),
            "deployment_record_id": record.deployment_record_id,
            "promotion_record_id": record.promotion_record_id,
            "action": "skipped" if record.instance in target_inventory_instances else "created",
        }
        for record in source_inventory_records
    ]

    source_release_tuples = tuple(
        record
        for record in record_store.list_release_tuple_records()
        if record.context == request.source_context
    )
    target_release_channels = {
        record.channel
        for record in record_store.list_release_tuple_records()
        if record.context == request.target_context
    }
    release_tuples = [
        {
            "channel": record.channel,
            "artifact_id": record.artifact_id,
            "provenance": record.provenance,
            "action": "skipped" if record.channel in target_release_channels else "created",
        }
        for record in source_release_tuples
    ]

    next_profile = _profile_after_cutover(
        profile,
        source_context=request.source_context,
        target_context=request.target_context,
        display_name=request.display_name,
        now=utc_now_timestamp(),
        source_label=request.source_label,
    )
    profile_changed = _profile_semantic_payload(profile) != _profile_semantic_payload(next_profile)
    groups = {
        "runtime_environment_records": runtime_records,
        "managed_secret_records": managed_secrets,
        "dokploy_targets": dokploy_targets,
        "dokploy_target_ids": dokploy_target_ids,
        "inventory_records": inventory_records,
        "release_tuple_records": release_tuples,
    }
    return {
        "product": request.product,
        "source_context": request.source_context,
        "target_context": request.target_context,
        "mode": request.mode,
        "profile": {
            "action": "updated" if profile_changed else "unchanged",
            "display_name": next_profile.display_name,
            "lane_contexts": {
                lane.instance: lane.context
                for lane in next_profile.lanes
                if lane.instance in {"testing", "prod"}
            },
            "preview_context": next_profile.preview.context,
        },
        "groups": groups,
        "counts": {name: _summarize_counts(items) for name, items in groups.items()},
        "guardrails": [
            "Append-only deployments, promotions, backup gates, and preview history are not copied.",
            "Runtime values, secret plaintext, secret ciphertext, and full provider env text are not returned.",
        ],
    }


def apply_product_context_cutover(
    *,
    record_store: PostgresRecordStore,
    request: ProductContextCutoverRequest,
) -> dict[str, object]:
    plan = plan_product_context_cutover(record_store=record_store, request=request)
    if request.mode == "dry-run":
        return plan

    now = utc_now_timestamp()
    for record in record_store.list_runtime_environment_records(
        context_name=request.source_context
    ):
        exists = any(
            target.scope == record.scope and target.instance == record.instance
            for target in record_store.list_runtime_environment_records(
                context_name=request.target_context
            )
        )
        if not exists:
            record_store.write_runtime_environment_record(
                record.model_copy(
                    update={
                        "context": request.target_context,
                        "updated_at": now,
                        "source_label": request.source_label,
                    }
                )
            )

    for record in tuple(
        item
        for item in record_store.list_dokploy_target_records()
        if item.context == request.source_context
    ):
        exists = any(
            target.context == request.target_context and target.instance == record.instance
            for target in record_store.list_dokploy_target_records()
        )
        if not exists:
            record_store.write_dokploy_target_record(
                record.model_copy(
                    update={
                        "context": request.target_context,
                        "updated_at": now,
                        "source_label": request.source_label,
                    }
                )
            )

    for record in tuple(
        item
        for item in record_store.list_dokploy_target_id_records()
        if item.context == request.source_context
    ):
        exists = any(
            target.context == request.target_context and target.instance == record.instance
            for target in record_store.list_dokploy_target_id_records()
        )
        if not exists:
            record_store.write_dokploy_target_id_record(
                DokployTargetIdRecord(
                    context=request.target_context,
                    instance=record.instance,
                    target_id=record.target_id,
                    updated_at=now,
                    source_label=request.source_label,
                )
            )

    for record in record_store.list_secret_records(
        context_name=request.source_context,
        limit=None,
    ):
        target = record_store.find_secret_record(
            scope=record.scope,
            integration=record.integration,
            name=record.name,
            context=request.target_context,
            instance=record.instance,
        )
        if target is not None:
            continue
        target_secret_id = _target_secret_id(record, target_context=request.target_context)
        source_version = record_store.read_secret_version(record.current_version_id)
        target_version_id = _target_secret_version_id(
            secret_id=target_secret_id,
            source_version_id=source_version.version_id,
        )
        record_store.write_secret_version(
            SecretVersion(
                version_id=target_version_id,
                secret_id=target_secret_id,
                created_at=now,
                created_by=request.source_label,
                cipher_alg=source_version.cipher_alg,
                key_id=source_version.key_id,
                ciphertext=source_version.ciphertext,
            )
        )
        record_store.write_secret_record(
            SecretRecord(
                secret_id=target_secret_id,
                scope=record.scope,
                integration=record.integration,
                name=record.name,
                context=request.target_context,
                instance=record.instance,
                description=record.description,
                policy=record.policy,
                status=record.status,
                current_version_id=target_version_id,
                created_at=now,
                updated_at=now,
                last_validated_at=record.last_validated_at,
                updated_by=request.source_label,
            )
        )
        for binding in tuple(
            item
            for item in record_store.list_secret_bindings(
                integration=record.integration,
                context_name=request.source_context,
                instance_name=record.instance,
                limit=None,
            )
            if item.secret_id == record.secret_id
        ):
            record_store.write_secret_binding(
                SecretBinding(
                    binding_id=_target_secret_binding_id(
                        secret_id=target_secret_id,
                        binding_key=binding.binding_key,
                    ),
                    secret_id=target_secret_id,
                    integration=binding.integration,
                    binding_type=binding.binding_type,
                    binding_key=binding.binding_key,
                    context=request.target_context,
                    instance=binding.instance,
                    status=binding.status,
                    created_at=now,
                    updated_at=now,
                )
            )
        record_store.write_secret_audit_event(
            SecretAuditEvent(
                event_id=_target_secret_event_id(
                    secret_id=target_secret_id,
                    source_secret_id=record.secret_id,
                ),
                secret_id=target_secret_id,
                event_type="imported",
                recorded_at=now,
                actor=request.source_label,
                detail="Launchplane imported managed secret during product context cutover.",
                metadata={
                    "source": request.source_label,
                    "source_secret_id": record.secret_id,
                    "source_context": request.source_context,
                    "target_context": request.target_context,
                },
            )
        )

    for record in tuple(
        item
        for item in record_store.list_environment_inventory()
        if item.context == request.source_context
    ):
        exists = any(
            target.context == request.target_context and target.instance == record.instance
            for target in record_store.list_environment_inventory()
        )
        if not exists:
            record_store.write_environment_inventory(
                record.model_copy(update={"context": request.target_context, "updated_at": now})
            )

    for record in tuple(
        item
        for item in record_store.list_release_tuple_records()
        if item.context == request.source_context
    ):
        exists = any(
            target.context == request.target_context and target.channel == record.channel
            for target in record_store.list_release_tuple_records()
        )
        if not exists:
            record_store.write_release_tuple_record(
                record.model_copy(
                    update={
                        "context": request.target_context,
                        "tuple_id": f"{request.target_context}-{record.channel}-{record.artifact_id}",
                    }
                )
            )

    profile = record_store.read_product_profile_record(request.product)
    next_profile = _profile_after_cutover(
        profile,
        source_context=request.source_context,
        target_context=request.target_context,
        display_name=request.display_name,
        now=now,
        source_label=request.source_label,
    )
    if _profile_semantic_payload(profile) != _profile_semantic_payload(next_profile):
        record_store.write_product_profile_record(next_profile)
    return {**plan, "applied": True}


def plan_legacy_context_cleanup(
    *,
    record_store: PostgresRecordStore,
    request: LegacyContextCleanupRequest,
) -> dict[str, object]:
    _record_target_context_exists(
        record_store=record_store,
        source_context=request.source_context,
        target_context=request.target_context,
        product=request.product,
    )

    runtime_records: list[dict[str, object]] = []
    for record in record_store.list_runtime_environment_records(
        context_name=request.source_context
    ):
        target_exists = _target_runtime_route_exists(
            record_store=record_store,
            source_record=record,
            target_context=request.target_context,
        )
        runtime_records.append(
            {
                "scope": record.scope,
                "instance": record.instance,
                "env_keys": sorted(record.env.keys()),
                "env_value_count": len(record.env),
                "action": "deleted" if target_exists else "blocked_missing_target",
            }
        )

    secret_records: list[dict[str, object]] = []
    for record in record_store.list_secret_records(
        context_name=request.source_context,
        limit=None,
    ):
        bindings = _source_secret_bindings(record_store=record_store, record=record)
        target_exists = _target_secret_route_exists(
            record_store=record_store,
            source_record=record,
            target_context=request.target_context,
        )
        action = "blocked_missing_target"
        if target_exists:
            action = "skipped" if record.status == "disabled" else "disabled"
        secret_records.append(
            {
                "secret_id": record.secret_id,
                "scope": record.scope,
                "integration": record.integration,
                "name": record.name,
                "instance": record.instance,
                "status": record.status,
                "binding_keys": sorted(binding.binding_key for binding in bindings),
                "binding_statuses": {binding.binding_key: binding.status for binding in bindings},
                "action": action,
            }
        )

    target_records = record_store.list_dokploy_target_records()
    dokploy_targets = [
        {
            "instance": record.instance,
            "target_type": record.target_type,
            "target_name": record.target_name,
            "domains": list(record.domains),
            "env_keys": sorted(record.env.keys()),
            "env_value_count": len(record.env),
            "action": "deleted"
            if _target_instance_exists(
                target_records,
                target_context=request.target_context,
                instance=record.instance,
            )
            else "blocked_missing_target",
        }
        for record in target_records
        if record.context == request.source_context
    ]

    target_id_records = record_store.list_dokploy_target_id_records()
    dokploy_target_ids = [
        {
            "instance": record.instance,
            "target_id": record.target_id,
            "action": "deleted"
            if _target_instance_exists(
                target_id_records,
                target_context=request.target_context,
                instance=record.instance,
            )
            else "blocked_missing_target",
        }
        for record in target_id_records
        if record.context == request.source_context
    ]

    preserved_inventory = tuple(
        record
        for record in record_store.list_environment_inventory()
        if record.context == request.source_context
    )
    preserved_release_tuples = tuple(
        record
        for record in record_store.list_release_tuple_records()
        if record.context == request.source_context
    )
    groups = {
        "runtime_environment_records": runtime_records,
        "managed_secret_records": secret_records,
        "dokploy_targets": dokploy_targets,
        "dokploy_target_ids": dokploy_target_ids,
    }
    blocked = any(
        str(item.get("action") or "").startswith("blocked")
        for items in groups.values()
        for item in items
    )
    return {
        "product": request.product,
        "source_context": request.source_context,
        "target_context": request.target_context,
        "mode": request.mode,
        "blocked": blocked,
        "groups": groups,
        "counts": {name: _summarize_actions(items) for name, items in groups.items()},
        "preserved_records": {
            "inventory_records": len(preserved_inventory),
            "release_tuple_records": len(preserved_release_tuples),
            "reason": "Historical evidence is preserved; cleanup only removes mutable lookup records and disables legacy secrets.",
        },
        "guardrails": [
            "Refuses cleanup while source_context is still owned by this or another product profile.",
            "Blocks each mutable source record unless the matching target_context record already exists.",
            "Runtime values, secret plaintext, secret ciphertext, and full provider env text are not returned.",
            "Inventory, release tuples, deployments, promotions, backup gates, and preview history are preserved as evidence.",
        ],
    }


def apply_legacy_context_cleanup(
    *,
    record_store: PostgresRecordStore,
    request: LegacyContextCleanupRequest,
) -> dict[str, object]:
    plan = plan_legacy_context_cleanup(record_store=record_store, request=request)
    if request.mode == "dry-run":
        return plan
    if plan["blocked"]:
        raise ValueError("Legacy context cleanup plan has blocked records.")

    now = utc_now_timestamp()
    actor = request.actor or request.source_label
    for record in record_store.list_runtime_environment_records(
        context_name=request.source_context
    ):
        status = record_store.delete_runtime_environment_record_with_event(
            event=_runtime_delete_event(
                record=record,
                actor=actor,
                source_label=request.source_label,
                now=now,
            ),
            expected_record=record,
        )
        if status == "changed":
            raise ValueError("Runtime environment record changed during cleanup.")

    for record in record_store.list_secret_records(
        context_name=request.source_context,
        limit=None,
    ):
        if record.status != "disabled":
            record_store.write_secret_record(
                record.model_copy(
                    update={
                        "status": "disabled",
                        "updated_at": now,
                        "updated_by": actor,
                    }
                )
            )
            record_store.write_secret_audit_event(
                SecretAuditEvent(
                    event_id=_secret_cleanup_event_id(record.secret_id),
                    secret_id=record.secret_id,
                    event_type="disabled",
                    recorded_at=now,
                    actor=actor,
                    detail="Launchplane disabled managed secret during legacy context cleanup.",
                    metadata={
                        "source": request.source_label,
                        "source_context": request.source_context,
                        "target_context": request.target_context,
                    },
                )
            )
        for binding in _source_secret_bindings(record_store=record_store, record=record):
            if binding.status != "disabled":
                record_store.write_secret_binding(
                    binding.model_copy(update={"status": "disabled", "updated_at": now})
                )

    for record in tuple(
        item
        for item in record_store.list_dokploy_target_id_records()
        if item.context == request.source_context
    ):
        status = record_store.delete_dokploy_target_id_record(expected_record=record)
        if status == "changed":
            raise ValueError("Dokploy target ID record changed during cleanup.")

    for record in tuple(
        item
        for item in record_store.list_dokploy_target_records()
        if item.context == request.source_context
    ):
        status = record_store.delete_dokploy_target_record(expected_record=record)
        if status == "changed":
            raise ValueError("Dokploy target record changed during cleanup.")

    return {**plan, "applied": True}
