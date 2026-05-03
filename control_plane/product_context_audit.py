from __future__ import annotations

from typing import Protocol

from control_plane.secrets import SECRET_STATUS_CONFIGURED
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord


class ProductContextAuditStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def list_runtime_environment_records(
        self, *, context_name: str = "", instance_name: str = ""
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

    def list_dokploy_target_records(self) -> tuple[DokployTargetRecord, ...]: ...

    def list_dokploy_target_id_records(self) -> tuple[DokployTargetIdRecord, ...]: ...

    def list_environment_inventory(self) -> tuple[EnvironmentInventory, ...]: ...

    def list_release_tuple_records(self) -> tuple[ReleaseTupleRecord, ...]: ...

    def list_backup_gate_records(
        self, *, context_name: str = "", instance_name: str = "", limit: int | None = None
    ) -> tuple[BackupGateRecord, ...]: ...

    def list_deployment_records(
        self, *, context_name: str = "", instance_name: str = "", limit: int | None = None
    ) -> tuple[DeploymentRecord, ...]: ...

    def list_promotion_records(
        self,
        *,
        context_name: str = "",
        from_instance_name: str = "",
        to_instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[PromotionRecord, ...]: ...


def _artifact_id_or_empty(artifact_identity: object) -> str:
    if artifact_identity is None:
        return ""
    return str(getattr(artifact_identity, "artifact_id", "") or "")


def summarize_product_profile_record(
    record: LaunchplaneProductProfileRecord,
) -> dict[str, object]:
    return {
        "product": record.product,
        "display_name": record.display_name,
        "repository": record.repository,
        "driver_id": record.driver_id,
        "image_repository": record.image.repository,
        "runtime_port": record.runtime_port,
        "health_path": record.health_path,
        "lane_count": len(record.lanes),
        "preview_enabled": record.preview.enabled,
        "preview_context": record.preview.context,
        "updated_at": record.updated_at,
        "source": record.source,
    }


def _summarize_product_profile_lane(record: ProductLaneProfile) -> dict[str, object]:
    return {
        "instance": record.instance,
        "context": record.context,
        "base_url": record.base_url,
        "health_url": record.health_url,
    }


def _summarize_runtime_environment_record(
    record: RuntimeEnvironmentRecord,
) -> dict[str, object]:
    return {
        "scope": record.scope,
        "context": record.context,
        "instance": record.instance,
        "updated_at": record.updated_at,
        "source_label": record.source_label,
        "env_keys": sorted(record.env.keys()),
        "env_value_count": len(record.env),
    }


def _summarize_environment_inventory(record: EnvironmentInventory) -> dict[str, object]:
    return {
        "context": record.context,
        "instance": record.instance,
        "artifact_id": _artifact_id_or_empty(record.artifact_identity),
        "source_git_ref": record.source_git_ref,
        "updated_at": record.updated_at,
        "deployment_record_id": record.deployment_record_id,
        "promotion_record_id": record.promotion_record_id,
        "promoted_from_instance": record.promoted_from_instance,
        "deploy_status": record.deploy.status,
        "post_deploy_update_status": record.post_deploy_update.status,
        "destination_health_status": record.destination_health.status,
    }


def _dokploy_target_route(record: DokployTargetRecord | DokployTargetIdRecord) -> tuple[str, str]:
    return (record.context.strip().lower(), record.instance.strip().lower())


def _target_id_map(
    target_id_records: tuple[DokployTargetIdRecord, ...],
) -> dict[tuple[str, str], str]:
    return {_dokploy_target_route(record): record.target_id for record in target_id_records}


def _summarize_dokploy_target_record(
    record: DokployTargetRecord,
    *,
    target_id: str = "",
) -> dict[str, object]:
    return {
        "context": record.context,
        "instance": record.instance,
        "target_id": target_id,
        "target_type": record.target_type,
        "project_name": record.project_name,
        "target_name": record.target_name,
        "source_git_ref": record.source_git_ref,
        "compose_path": record.compose_path,
        "watch_paths": list(record.watch_paths),
        "domains": list(record.domains),
        "require_test_gate": record.require_test_gate,
        "require_prod_gate": record.require_prod_gate,
        "healthcheck_enabled": record.healthcheck_enabled,
        "healthcheck_path": record.healthcheck_path,
        "env_keys": sorted(record.env.keys()),
        "env_value_count": len(record.env),
        "shopify_protected_store_keys": sorted(record.policies.shopify.protected_store_keys),
        "updated_at": record.updated_at,
        "source_label": record.source_label,
    }


def _summarize_secret_record_for_context_audit(
    *,
    record_store: ProductContextAuditStore,
    record: SecretRecord,
) -> dict[str, object]:
    binding = next(
        (
            binding
            for binding in record_store.list_secret_bindings(
                integration=record.integration,
                context_name=record.context,
                instance_name=record.instance,
                limit=None,
            )
            if binding.secret_id == record.secret_id and binding.status == SECRET_STATUS_CONFIGURED
        ),
        None,
    )
    return {
        "secret_id": record.secret_id,
        "scope": record.scope,
        "integration": record.integration,
        "name": record.name,
        "context": record.context,
        "instance": record.instance,
        "status": record.status,
        "binding_key": binding.binding_key if binding is not None else "",
        "binding_status": binding.status if binding is not None else "",
        "updated_at": record.updated_at,
    }


def _context_cutover_route_payload(
    *,
    record_store: ProductContextAuditStore,
    context_name: str,
) -> dict[str, object]:
    runtime_records = record_store.list_runtime_environment_records(context_name=context_name)
    secret_records = record_store.list_secret_records(context_name=context_name, limit=None)
    target_records = tuple(
        record
        for record in record_store.list_dokploy_target_records()
        if record.context == context_name
    )
    target_ids_by_route = _target_id_map(
        tuple(
            record
            for record in record_store.list_dokploy_target_id_records()
            if record.context == context_name
        )
    )
    inventory_records = tuple(
        record
        for record in record_store.list_environment_inventory()
        if record.context == context_name
    )
    release_tuple_records = tuple(
        record
        for record in record_store.list_release_tuple_records()
        if record.context == context_name
    )
    return {
        "context": context_name,
        "runtime_environment_records": [
            _summarize_runtime_environment_record(record) for record in runtime_records
        ],
        "managed_secret_records": [
            _summarize_secret_record_for_context_audit(
                record_store=record_store,
                record=record,
            )
            for record in secret_records
        ],
        "dokploy_targets": [
            _summarize_dokploy_target_record(
                record,
                target_id=target_ids_by_route.get(_dokploy_target_route(record), ""),
            )
            for record in target_records
        ],
        "inventory_records": [
            _summarize_environment_inventory(record) for record in inventory_records
        ],
        "release_tuple_records": [
            {
                "tuple_id": record.tuple_id,
                "context": record.context,
                "channel": record.channel,
                "artifact_id": record.artifact_id,
                "image_repository": record.image_repository,
                "image_digest": record.image_digest,
                "deployment_record_id": record.deployment_record_id,
                "promotion_record_id": record.promotion_record_id,
                "promoted_from_channel": record.promoted_from_channel,
                "provenance": record.provenance,
                "minted_at": record.minted_at,
            }
            for record in release_tuple_records
        ],
        "append_only_evidence_counts": {
            "backup_gates": len(
                record_store.list_backup_gate_records(context_name=context_name, limit=None)
            ),
            "deployments": len(
                record_store.list_deployment_records(context_name=context_name, limit=None)
            ),
            "promotions": len(
                record_store.list_promotion_records(context_name=context_name, limit=None)
            ),
        },
    }


def _build_context_cutover_warnings(
    *,
    profile: LaunchplaneProductProfileRecord,
    source_context: str,
    target_context: str,
    preview_context: str,
    source_payload: dict[str, object],
    target_payload: dict[str, object],
) -> list[str]:
    warnings: list[str] = []
    stable_lane_contexts = {
        lane.instance: lane.context
        for lane in profile.lanes
        if lane.instance in {"testing", "prod"}
    }
    if source_context in stable_lane_contexts.values():
        warnings.append(
            f"Stable product profile lanes still reference legacy context {source_context!r}."
        )
    if target_context not in stable_lane_contexts.values():
        warnings.append(
            f"No stable product profile lane currently references target context {target_context!r}."
        )
    if profile.preview.enabled and profile.preview.context not in {preview_context, target_context}:
        warnings.append(
            f"Preview profile context {profile.preview.context!r} differs from requested preview context."
        )
    if source_payload["append_only_evidence_counts"] != {
        "backup_gates": 0,
        "deployments": 0,
        "promotions": 0,
    }:
        warnings.append(
            "Legacy context has append-only evidence; do not rewrite those records during cutover."
        )
    if (
        target_payload["runtime_environment_records"]
        and source_payload["runtime_environment_records"]
    ):
        warnings.append(
            "Both source and target contexts have runtime environment records; compare key sets before deleting either side."
        )
    return warnings


def build_product_context_cutover_audit(
    *,
    record_store: ProductContextAuditStore,
    product: str,
    source_context: str,
    target_context: str,
    preview_context: str = "",
) -> dict[str, object]:
    normalized_product = product.strip()
    normalized_source_context = source_context.strip()
    normalized_target_context = target_context.strip()
    if not normalized_product:
        raise ValueError("Provide a non-empty product.")
    if not normalized_source_context or not normalized_target_context:
        raise ValueError("Provide non-empty source_context and target_context.")
    if normalized_source_context == normalized_target_context:
        raise ValueError("Source and target contexts must differ.")

    profile = record_store.read_product_profile_record(normalized_product)
    normalized_preview_context = preview_context.strip() or profile.preview.context.strip()
    source_payload = _context_cutover_route_payload(
        record_store=record_store,
        context_name=normalized_source_context,
    )
    target_payload = _context_cutover_route_payload(
        record_store=record_store,
        context_name=normalized_target_context,
    )
    preview_payload = (
        _context_cutover_route_payload(
            record_store=record_store,
            context_name=normalized_preview_context,
        )
        if normalized_preview_context
        and normalized_preview_context not in {normalized_source_context, normalized_target_context}
        else None
    )
    warnings = _build_context_cutover_warnings(
        profile=profile,
        source_context=normalized_source_context,
        target_context=normalized_target_context,
        preview_context=normalized_preview_context,
        source_payload=source_payload,
        target_payload=target_payload,
    )
    stable_lanes = [lane for lane in profile.lanes if lane.instance in {"testing", "prod"}]
    return {
        "status": "ok",
        "product": profile.product,
        "source_context": normalized_source_context,
        "target_context": normalized_target_context,
        "preview_context": normalized_preview_context,
        "profile": {
            "summary": summarize_product_profile_record(profile),
            "stable_lanes": [_summarize_product_profile_lane(lane) for lane in stable_lanes],
            "preview": {
                "enabled": profile.preview.enabled,
                "context": profile.preview.context,
                "slug_template": profile.preview.slug_template,
                "template_instance": profile.preview.template_instance,
            },
        },
        "contexts": {
            "source": source_payload,
            "target": target_payload,
            "preview": preview_payload,
        },
        "warnings": warnings,
        "guardrails": [
            "Do not rewrite append-only deployments, promotions, backup gates, or preview history.",
            "Compare runtime key sets before deleting source or target runtime records.",
            "Apply product profile lane changes only after target current-authority records exist.",
        ],
    }
