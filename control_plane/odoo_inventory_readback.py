from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import odoo_runtime_reads as reads
from control_plane.contracts.runtime_identity import RUNTIME_IDENTITY_ENV_KEY
from control_plane.dokploy.compose import ODOO_WEB_CONFIG_PATH
from control_plane.dokploy.native_copy_stream import _read_remote
from control_plane.odoo_inventory_readback_program import CONTAINER_PROGRAM, HOST_PROGRAM
from control_plane.odoo_source_prerequisite_programs import FailureStage
from control_plane.timestamps import utc_now_timestamp

InventoryFailureStage = FailureStage | Literal["application_read"]

ROUTE = "/v1/products/{product}/environments/{environment}/odoo-inventory-readback"
Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_.:-]+$")]
Count = Annotated[int, Field(strict=True, ge=0)]
Text = Annotated[str, Field(max_length=512)]
Number = Annotated[str, Field(max_length=80, pattern=r"^-?[0-9]+(?:\.[0-9]+)?$")]


class InventoryReadQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: Identifier
    deployment_record_id: Identifier
    source_git_ref: str = Field(pattern=r"^[a-f0-9]{40}$")
    artifact_id: Identifier
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    target_id: Identifier
    company_id: int = Field(ge=1)
    part_ids: tuple[Annotated[str, Field(pattern=r"^[1-9][0-9]{0,17}$")], ...] = Field(
        min_length=1, max_length=10
    )
    purchaser_xmlid: Literal["cm_custom.user_gunneet"]

    @model_validator(mode="after")
    def distinct_parts(self) -> InventoryReadQuery:
        if len(self.part_ids) != len(set(self.part_ids)):
            raise ValueError("Witness part IDs must be distinct.")
        return self


class StoredInventoryFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parts: Count | None
    supplier_cost_rows: Count | None
    listing_bindings: Count | None
    nonzero_sale_price_parts: Count | None
    stockable_parts: Count | None
    lot_tracked_parts: Count | None
    serial_tracked_parts: Count | None
    current_conflicts: Count | None
    parts_with_extra_details: Count | None
    parts_with_upc: Count | None
    parts_with_packaging: Count | None
    stock_move_rows: Count | None
    done_stock_move_rows: Count | None
    internal_quantity: Number | None
    historical_fields_copied: None = None
    historical_listing_conflicts: None = None
    historical_unmapped_lines: None = None
    historical_tracking_results: None = None


class SupplierCostWitness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supplier_id: Count
    supplier_name: Text | None
    supplier_part_number: Text | None
    cost: Number | None
    currency_id: Count | None
    unit_id: Count | None


class PartWitness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_id: str = Field(pattern=r"^[1-9][0-9]{0,17}$")
    status: Literal["present", "missing", "unavailable"]
    template_id: Count | None = None
    name: Text | None = None
    part_number: Text | None = None
    barcode: Text | None = None
    active: bool | None = Field(default=None, strict=True)
    cost: Number | None = None
    sale_price: Number | None = None
    tracking: Literal["none", "lot", "serial"] | None = None
    internal_quantity: Number | None = None
    supplier_costs: tuple[SupplierCostWitness, ...] | None = Field(default=None, max_length=10)
    suppliers_truncated: bool | None = Field(default=None, strict=True)


class PurchaserWitness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    xmlid: Literal["cm_custom.user_gunneet"]
    status: Literal["present", "missing", "unavailable"]
    module_state: (
        Literal[
            "installed", "uninstalled", "to install", "to upgrade", "to remove", "uninstallable"
        ]
        | None
    ) = None
    user_id: Count | None = None
    active: bool | None = Field(default=None, strict=True)
    login: Text | None = None
    name: Text | None = None
    group_xmlids: tuple[Identifier, ...] | None = Field(default=None, max_length=100)
    groups_without_xmlid: Count | None = None
    first_signin: Literal["not_observed"] = "not_observed"


class InventoryReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime: Literal["verified", "unknown"] = "unknown"
    evidence: reads.OdooRuntimeEvidence | None = None
    observed_at: str | None = None
    database_snapshot: str | None = Field(
        default=None, max_length=4096, pattern=r"^[0-9]+:[0-9]+:[0-9,]*$"
    )
    database_uuid: str | None = Field(default=None, pattern=r"^[a-f0-9-]{36}$")
    company_id: Count | None = None
    facts: StoredInventoryFacts | None = None
    parts: tuple[PartWitness, ...] = Field(default=(), max_length=10)
    purchaser: PurchaserWitness | None = None
    failure_stage: InventoryFailureStage | None = None
    applied: Literal[False] = False
    scope: Literal["current_database_stored_facts"] = "current_database_stored_facts"


def read_inventory(
    *,
    store: reads.OdooRuntimeReadStore,
    product: str,
    query: InventoryReadQuery,
    control_plane_root: Path,
    database_url: str | None,
) -> InventoryReadResult:
    stage: InventoryFailureStage = "runtime_selection"
    # noinspection PyBroadException
    try:
        profile = store.read_product_profile_record(product)
        selection = reads.select_stable_runtime(store, profile, "testing")
        identity = selection.identity
        if (
            identity.product != product
            or identity.context != query.context
            or identity.deployment_record_id != query.deployment_record_id
            or identity.source_git_ref != query.source_git_ref
            or identity.artifact_id != query.artifact_id
            or identity.image_reference.rpartition("@")[2] != query.image_digest
        ):
            return InventoryReadResult(failure_stage=stage)
        stage = "runtime_connection"
        connection = reads.connect_runtime(
            store=store,
            selection=selection,
            control_plane_root=control_plane_root,
            database_url=database_url,
        )
        if connection.evidence.target_id != query.target_id:
            return InventoryReadResult(failure_stage=stage)
        stage = "runtime_inspection"
        observed = reads.inspect_runtime_container(
            connection.host,
            connection.token,
            connection.server_id,
            connection.evidence.container_id,
            connection.app_name,
            identity,
        )
        stage = "transport"
        frames: list[dict[str, object]] = []
        _read_remote(
            host=connection.host,
            token=connection.token,
            server_id=connection.server_id,
            program=HOST_PROGRAM.replace("__IDENTITY_KEY__", repr(RUNTIME_IDENTITY_ENV_KEY)),
            payload={
                "project": connection.app_name,
                "container": connection.evidence.container_id,
                "identity": json.loads(observed[RUNTIME_IDENTITY_ENV_KEY]),
                "company_id": query.company_id,
                "part_ids": query.part_ids,
                "purchaser_xmlid": query.purchaser_xmlid,
                "container_program": CONTAINER_PROGRAM.replace(
                    "__IDENTITY_KEY__", repr(RUNTIME_IDENTITY_ENV_KEY)
                ).replace("__CONFIG_PATH__", repr(ODOO_WEB_CONFIG_PATH)),
            },
            receive=frames.append,
            timeout_seconds=50,
        )
        stage = "result_validation"
        if (
            len(frames) != 1
            or set(frames[0]) != {"type", "result"}
            or frames[0]["type"] != "complete"
        ):
            return InventoryReadResult(failure_stage=stage)
        result = InventoryReadResult.model_validate(frames[0]["result"])
        if result.runtime == "unknown":
            return InventoryReadResult(failure_stage=result.failure_stage or stage)
        if (
            result.evidence is not None
            or result.observed_at is not None
            or result.failure_stage is not None
            or result.facts is None
            or result.database_snapshot is None
            or result.company_id != query.company_id
            or tuple(part.part_id for part in result.parts) != query.part_ids
            or result.purchaser is None
            or result.purchaser.xmlid != query.purchaser_xmlid
        ):
            return InventoryReadResult(failure_stage=stage)
        stage = "drift"
        reads.assert_selection_current(store, selection)
        current = reads.connect_runtime(
            store=store,
            selection=selection,
            control_plane_root=control_plane_root,
            database_url=database_url,
        )
        if current.evidence.model_dump(exclude={"observed_at"}) != connection.evidence.model_dump(
            exclude={"observed_at"}
        ):
            return InventoryReadResult(failure_stage=stage)
        return result.model_copy(
            update={"evidence": current.evidence, "observed_at": utc_now_timestamp()}
        )
    except Exception:
        return InventoryReadResult(failure_stage=stage)
