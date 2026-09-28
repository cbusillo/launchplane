"""Rebuild VeriReel's production environment record from its last promotion.

VeriReel prod promotions wrote only the promotion record, so the prod
environment record kept a pre-runtime-identity deployment from before the
driver route existed. Release review reads production's identity from that
record and could not compile a checklist. Promotions now write the record; this
one-time repair rebuilds it from the promotion and deployment that production
runs, verified against the live health endpoint before this change landed.

The record ids and identity are pinned on purpose: this repairs one known
record and refuses anything else. It never replaces a record that already has
a runtime identity, so it is idempotent and cannot overwrite a later promotion.
"""

from __future__ import annotations

import logging
from typing import Literal

import sqlalchemy as sa

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.workflows.inventory import build_environment_inventory

CONTEXT = "verireel"
INSTANCE = "prod"
PROMOTION_RECORD_ID = "promotion-verireel-testing-to-prod-run-33647941501-attempt-1"
DEPLOYMENT_RECORD_ID = "deployment-20260902T153259Z-verireel-prod"
ARTIFACT_ID = (
    "ghcr.io/cbusillo/verireel-app"
    "@sha256:cbfe9a1b4d0377d5f9567042e82460001e4a3e6cbb80aeb015ffb4c1eeed3025"
)
SOURCE_GIT_REF = "5c4a7b9527eb93618dceb671803949242521acb2"

BackfillOutcome = Literal["written", "current", "absent", "refused"]

_LOGGER = logging.getLogger(__name__)

_PROMOTIONS = sa.table(
    "launchplane_promotions", sa.column("record_id", sa.String), sa.column("payload", sa.JSON)
)
_DEPLOYMENTS = sa.table(
    "launchplane_deployments", sa.column("record_id", sa.String), sa.column("payload", sa.JSON)
)
_INVENTORY = sa.table(
    "launchplane_inventory",
    sa.column("context", sa.String),
    sa.column("instance", sa.String),
    sa.column("artifact_id", sa.String),
    sa.column("source_git_ref", sa.String),
    sa.column("updated_at", sa.String),
    sa.column("deployment_record_id", sa.String),
    sa.column("promotion_record_id", sa.String),
    sa.column("promoted_from_instance", sa.String),
    sa.column("payload", sa.JSON),
)


def _mismatch(promotion: PromotionRecord, deployment: DeploymentRecord) -> str:
    identity = deployment.runtime_identity
    checks = {
        "promotion_context": promotion.context == CONTEXT,
        "promotion_instance": promotion.to_instance == INSTANCE,
        "promotion_deployment": promotion.deployment_record_id == DEPLOYMENT_RECORD_ID,
        "promotion_artifact": promotion.artifact_identity.artifact_id == ARTIFACT_ID,
        "promotion_status": promotion.deploy.status == "pass",
        "deployment_context": deployment.context == CONTEXT,
        "deployment_instance": deployment.instance == INSTANCE,
        "deployment_artifact": deployment.artifact_identity is not None
        and deployment.artifact_identity.artifact_id == ARTIFACT_ID,
        "deployment_source": deployment.source_git_ref == SOURCE_GIT_REF,
        "deployment_status": deployment.deploy.status == "pass",
        "runtime_identity": identity is not None
        and identity.context == CONTEXT
        and identity.instance == INSTANCE
        and identity.artifact_id == ARTIFACT_ID
        and identity.source_git_ref == SOURCE_GIT_REF,
    }
    return ",".join(name for name, passed in checks.items() if not passed)


def backfill_verireel_prod_inventory(
    connection: sa.Connection, *, updated_at: str
) -> BackfillOutcome:
    current = connection.execute(
        sa.select(_INVENTORY.c.payload).where(
            _INVENTORY.c.context == CONTEXT, _INVENTORY.c.instance == INSTANCE
        )
    ).scalar_one_or_none()
    if current is not None and EnvironmentInventory.model_validate(current).runtime_identity:
        _LOGGER.info("verireel prod inventory backfill: current, nothing to do")
        return "current"
    promotion_payload = connection.execute(
        sa.select(_PROMOTIONS.c.payload).where(_PROMOTIONS.c.record_id == PROMOTION_RECORD_ID)
    ).scalar_one_or_none()
    deployment_payload = connection.execute(
        sa.select(_DEPLOYMENTS.c.payload).where(_DEPLOYMENTS.c.record_id == DEPLOYMENT_RECORD_ID)
    ).scalar_one_or_none()
    if promotion_payload is None or deployment_payload is None:
        _LOGGER.info("verireel prod inventory backfill: source records absent, nothing to do")
        return "absent"
    promotion = PromotionRecord.model_validate(promotion_payload)
    deployment = DeploymentRecord.model_validate(deployment_payload)
    mismatch = _mismatch(promotion, deployment)
    if mismatch:
        _LOGGER.warning("verireel prod inventory backfill refused: mismatch=%s", mismatch)
        return "refused"
    inventory = build_environment_inventory(
        deployment_record=deployment,
        updated_at=updated_at,
        promotion_record_id=promotion.record_id,
        promoted_from_instance=promotion.from_instance,
    )
    values = {
        "artifact_id": ARTIFACT_ID,
        "source_git_ref": inventory.source_git_ref,
        "updated_at": inventory.updated_at,
        "deployment_record_id": inventory.deployment_record_id,
        "promotion_record_id": inventory.promotion_record_id,
        "promoted_from_instance": inventory.promoted_from_instance,
        "payload": inventory.model_dump(mode="json", exclude_none=True),
    }
    if current is None:
        connection.execute(_INVENTORY.insert().values(context=CONTEXT, instance=INSTANCE, **values))
    else:
        connection.execute(
            _INVENTORY.update()
            .where(_INVENTORY.c.context == CONTEXT, _INVENTORY.c.instance == INSTANCE)
            .values(**values)
        )
    _LOGGER.warning(
        "verireel prod inventory backfill written: promotion=%s deployment=%s source=%s",
        PROMOTION_RECORD_ID,
        DEPLOYMENT_RECORD_ID,
        SOURCE_GIT_REF,
    )
    return "written"
