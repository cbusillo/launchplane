"""Finish public verification of an installed testing copy without deploying again."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.disposable_lane import TESTING_VERIFICATION_ROUTE
from control_plane.dokploy import api, source
from control_plane.dokploy.compose import ODOO_DATABASE_DATA_DIRECTORY
from control_plane.dokploy.native_copy_stream import inspect_native_testing_receiver
from control_plane.integration_readback import INTEGRATION_READBACK_PASSED_PATH
from control_plane.odoo_testing_restore import (
    fresh_testing_authority_sha256,
    generation_binding,
    read_current_testing_generation,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.odoo_stable_target_replacement import (
    _verify_required_runtime_identity_evidence,
)
from control_plane.workflows.odoo_verification import verify_odoo_stable_readiness
from control_plane.workflows.ship import utc_now_timestamp

ROUTE = TESTING_VERIFICATION_ROUTE
COPY_MARKERS = (
    "odoo_restore_completed",
    "testing_copy_pre_registry_sanitized",
    "testing_copy_sanitized",
    "testing_copy_runtime_isolated",
    "integration_readback_ok",
    "odoo_module_update_completed",
    "odoo_module_update_image_match",
)


class TestingVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    context: str = Field(min_length=1)
    instance: Literal["testing"] = "testing"
    operation_id: str = Field(min_length=1)
    deployment_record_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")


@dataclass
class TestingVerification:
    root: Path
    store: PostgresRecordStore
    product: str
    request: TestingVerificationRequest

    def snapshot(self) -> dict[str, object]:
        generation = read_current_testing_generation(
            self.store, product=self.product, context=self.request.context, instance="testing"
        )
        if generation is None or generation.operation_id != self.request.operation_id:
            raise ValueError("The original testing generation changed.")
        deployment = self.store.read_deployment_record(self.request.deployment_record_id)
        profile = self.store.read_product_profile_record(self.product)
        lanes = [
            lane
            for lane in profile.lanes
            if (lane.context, lane.instance) == (generation.context, "testing")
        ]
        if len(lanes) != 1 or lanes[0].odoo_cutover is not None:
            raise ValueError("Verification requires the original single-target testing lane.")
        identity = deployment.runtime_identity
        if (
            identity is None
            or (deployment.context, deployment.instance) != (generation.context, "testing")
            or (identity.product, identity.context, identity.instance, identity.environment_kind)
            != (self.product, generation.context, "testing", "stable")
            or identity.deployment_record_id != deployment.record_id
            or identity.preview_id
            or identity.preview_generation_id
            or identity.slot
            or deployment.resolved_target is None
            or deployment.resolved_target.target_id != generation.target_id
            or deployment.resolved_target.target_type != "compose"
            or deployment.artifact_identity is None
            or deployment.artifact_identity.artifact_id != identity.artifact_id
            or deployment.source_git_ref != identity.source_git_ref
            or not deployment.deploy.finished_at
            or deployment.post_deploy_update.status != "pass"
            or any(
                deployment.post_deploy_update.evidence.get("post_deploy_readback_" + key) != "true"
                for key in COPY_MARKERS
            )
        ):
            raise ValueError("The deployment lacks exact completed-copy evidence.")
        operations = self.store.list_odoo_stable_target_replacement_operation_records(
            product=self.product, context_name=generation.context, instance_name="testing"
        )
        matching = [op for op in operations if op.deployment_record_id == deployment.record_id]
        if len(matching) != 1:
            raise ValueError("The installed deployment has no unique original operation.")
        operation = matching[0]
        result = (
            operation.execution_failure.result if operation.execution_failure else operation.result
        )
        if (
            operation.status not in {"fail", "cancelled"}
            or not operation.finished_at
            or operation.request.artifact_id != identity.artifact_id
            or operation.request.source_git_ref != identity.source_git_ref
            or result is None
            or result.post_deploy_status != "pass"
            or result.deployment_record_id != deployment.record_id
            or result.target_id != generation.target_id
        ):
            raise ValueError("The original deployment is not terminal with completed post-deploy.")
        latest = self.store.list_deployment_records(
            context_name=generation.context, instance_name="testing", limit=1
        )
        if not latest or latest[0].record_id != deployment.record_id:
            raise ValueError("A later deployment must be qualified instead.")
        artifact = self.store.read_artifact_manifest(identity.artifact_id)
        if (
            artifact.source_commit != identity.source_git_ref
            or artifact.image.repository != profile.image.repository
            or identity.image_reference != f"{artifact.image.repository}@{artifact.image.digest}"
        ):
            raise ValueError("The installed artifact differs from its manifest.")
        for url in (lanes[0].base_url, lanes[0].health_url):
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != generation.domain_host
                or parsed.username
                or parsed.password
            ):
                raise ValueError("Public verification must use this generation's HTTPS hostname.")
        capture = self.store.read_native_testing_copy_capture(generation_binding(generation))
        if generation.source_mode == "container-export" and (
            capture is None or capture.source_binding_sha256 != generation.source_binding_sha256
        ):
            raise ValueError("The original native capture witness is unavailable.")
        return {
            "generation": generation.model_dump(mode="json", exclude={"non_use_invalidated"}),
            "authority": fresh_testing_authority_sha256(self.store, generation),
            "deployment": deployment.model_dump(mode="json"),
            "operation": operation.model_dump(mode="json"),
            "artifact": artifact.model_dump(mode="json"),
            "capture": capture.model_dump(mode="json") if capture else None,
            "inventory": self.store.read_environment_inventory(
                context_name=generation.context, instance_name="testing"
            ).model_dump(mode="json"),
        }

    def plan(self) -> dict[str, object]:
        snapshot = self.snapshot()
        generation = self.store.read_fresh_odoo_testing_generation(
            self.product, self.request.context
        )
        if generation is None or generation.completed_at:
            raise ValueError("This generation is already complete; use readback.")
        return self.receipt(canonical_json_sha256(snapshot), applied=False)

    def readback(self) -> dict[str, object]:
        self.snapshot()
        generation = self.store.read_fresh_odoo_testing_generation(
            self.product, self.request.context
        )
        deployment = self.store.read_deployment_record(self.request.deployment_record_id)
        inventory = self.store.read_environment_inventory(
            context_name=self.request.context, instance_name="testing"
        )
        if (
            generation is None
            or not generation.completed_at
            or deployment.deploy.status != "pass"
            or inventory.deployment_record_id != deployment.record_id
            or inventory.runtime_identity != deployment.runtime_identity
            or deployment.runtime_source.get("testing_verification_plan_sha256")
            != self.request.reviewed_plan_sha256
        ):
            raise ValueError("No matching testing verification publication is available.")
        self.verify()
        return self.receipt(self.request.reviewed_plan_sha256, applied=True)

    def receipt(self, digest: str, *, applied: bool) -> dict[str, object]:
        return {
            "product": self.product,
            "context": self.request.context,
            "instance": "testing",
            "operation_id": self.request.operation_id,
            "deployment_record_id": self.request.deployment_record_id,
            "plan_sha256": digest,
            "applied": applied,
            "generation_preserved": True,
            "copy_preserved": True,
            "public_health_verified": applied,
        }

    def verify(self) -> DeploymentRecord:
        """Provider reads only: no schedule, registry process, copy, or settings writes."""
        generation = read_current_testing_generation(
            self.store, product=self.product, context=self.request.context, instance="testing"
        )
        assert generation is not None
        deployment = self.store.read_deployment_record(self.request.deployment_record_id)
        identity = deployment.runtime_identity
        assert identity is not None
        profile = self.store.read_product_profile_record(self.product)
        lane = next(
            lane
            for lane in profile.lanes
            if (lane.context, lane.instance) == (generation.context, "testing")
        )
        host, token = source.read_dokploy_config(
            control_plane_root=self.root, database_url=self.store.database_url
        )
        provider = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=generation.target_id
        )
        if provider.get("composeId") != generation.target_id:
            raise ValueError("The provider target changed.")
        frames: list[dict[str, object]] = []
        inspect_native_testing_receiver(
            host=host,
            token=token,
            server_id=str(provider.get("serverId") or ""),
            spec={
                "generation": generation.model_dump(mode="json"),
                "compose_project": str(provider.get("appName") or ""),
                "image_reference": identity.image_reference,
                "database_name": api.parse_dokploy_env_text(str(provider.get("env") or "")).get(
                    "ODOO_DB_NAME", ""
                ),
                "database_data_directory": ODOO_DATABASE_DATA_DIRECTORY,
                "copy_binding_sha256": generation_binding(generation),
                "workflow_environment": {},
                "verification_only": True,
                "web_gate_path": INTEGRATION_READBACK_PASSED_PATH,
                "runtime_identity": identity.model_dump(mode="json", exclude={"deployed_at"}),
            },
            receive=frames.append,
        )
        if frames != [{"type": "complete", "copy_verified": True}]:
            raise ValueError("The installed copy witness is unverified.")
        checks = verify_odoo_stable_readiness(
            base_url=lane.base_url, health_url=lane.health_url, timeout_seconds=30
        )
        if checks.error_message or (
            checks.health_status,
            checks.canonical_status,
            checks.logo_status,
        ) != ("pass", "pass", "pass"):
            raise ValueError("Public Odoo readiness did not pass.")
        health = _verify_required_runtime_identity_evidence(
            health_url=lane.health_url, timeout_seconds=30, expected_runtime_identity=identity
        )
        if health.status != "pass" or health.runtime_identity_status != "match":
            raise ValueError("Public runtime identity did not match the installed deployment.")
        finished = utc_now_timestamp()
        return deployment.model_copy(
            update={
                "deploy": deployment.deploy.model_copy(
                    update={"status": "pass", "finished_at": finished}
                ),
                "destination_health": health,
                "failure": None,
                "runtime_identity": identity.model_copy(update={"deployed_at": finished}),
                "runtime_source": {
                    **deployment.runtime_source,
                    "testing_verification_plan_sha256": self.request.reviewed_plan_sha256,
                    "testing_verification_at": finished,
                    "testing_verification_canonical_status": checks.canonical_status,
                    "testing_verification_logo_status": checks.logo_status,
                    "testing_verification_evidence_json": checks.evidence.model_dump_json(),
                },
            }
        )
