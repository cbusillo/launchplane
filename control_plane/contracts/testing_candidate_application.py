"""An admitted build/source for a private copy, before serving publication."""

from datetime import datetime
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.prepared_public_site import FrozenRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.odoo_testing_source import NativeOdooSourceAuthority
from control_plane.odoo_testing_restore import FreshOdooTestingGeneration


class TestingCandidateApplicationPlan(FrozenRecord):
    product: str = Field(min_length=1)
    context: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^lp-testing-[a-f0-9]{32}$")
    target_id: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    artifact: ArtifactIdentityManifest
    runtime_identity: RuntimeIdentity
    source: Literal["empty", "production", "upstream"]
    generation: FreshOdooTestingGeneration | None = None
    native_source: NativeOdooSourceAuthority | None = None
    upstream_source: dict[str, str] = Field(default_factory=dict)
    authority_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    credential_bindings_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    omitted_runtime_keys: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))

    @property
    def image_reference(self) -> str:
        return f"{self.artifact.image.repository}@{self.artifact.image.digest}"

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        # The service resolves this plan from admitted records. It is not an
        # external request or permission to select a target, source or URL.
        identity = self.runtime_identity
        if (identity.product, identity.context, identity.instance) != (
            self.product,
            self.context,
            "testing",
        ):
            raise ValueError("Candidate identity must belong to the admitted testing lane.")
        if (
            identity.artifact_id,
            identity.source_git_ref,
            identity.image_digest,
            identity.image_reference,
        ) != (
            self.artifact.artifact_id,
            self.artifact.source_commit,
            self.artifact.image.digest,
            self.image_reference,
        ):
            raise ValueError("Candidate identity must bind the exact admitted artifact.")
        origin = urlsplit(self.base_url)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username is not None
            or origin.password is not None
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
        ):
            raise ValueError("Candidate requires the unchanged HTTPS lane origin.")
        if (self.source == "empty") != (self.generation is None):
            raise ValueError("Only a copy restore requires a generation/source binding.")
        if (self.source == "production") != (self.native_source is not None) or (
            self.source == "upstream"
        ) != bool(self.upstream_source):
            raise ValueError("Candidate source coordinates must match its admitted source kind.")
        generation = self.generation
        if generation is not None and (
            (
                generation.product,
                generation.context,
                generation.operation_id,
                generation.namespace,
                generation.target_id,
                generation.domain_host,
            )
            != (
                self.product,
                self.context,
                self.operation_id,
                self.namespace,
                self.target_id,
                origin.hostname,
            )
            or generation.completed_at
            or generation.source_mode
            != ("container-export" if self.source == "production" else "ssh")
            or set(generation.omitted_runtime_keys) != set(self.omitted_runtime_keys)
        ):
            raise ValueError("Copy generation differs from the admitted candidate/source.")
        if (
            self.native_source is not None
            and generation is not None
            and self.native_source.binding_sha256 != generation.source_binding_sha256
        ):
            raise ValueError("Native copy must retain its original managed source descriptor.")
        if self.native_source is not None and self.native_source.product != self.product:
            raise ValueError("Native source must belong to the candidate's product.")
        if self.upstream_source:
            from control_plane.dokploy.post_deploy import (
                resolve_upstream_restore_workflow_environment,
            )

            if (
                resolve_upstream_restore_workflow_environment(desired_env_map=self.upstream_source)
                != self.upstream_source
            ):
                raise ValueError(
                    "Upstream snapshots accept only supported noncredential source coordinates."
                )
        return self


class TestingCandidatePrepared(FrozenRecord):
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime_identity: RuntimeIdentity
    private_endpoint: str = Field(min_length=1)
    deployment_id: str = Field(min_length=1)
    source_witness: str = Field(min_length=1)
    integration_isolation_verified: Literal[True]
    credentials_verified: Literal[True]
    observed_at: datetime

    @model_validator(mode="after")
    def aware(self) -> Self:
        if self.observed_at.utcoffset() is None:
            raise ValueError("Candidate readiness requires an aware observation time.")
        return self


CandidateApplicationPhase = Literal[
    "planned",
    "configuring",
    "configured",
    "deploying",
    "deployed",
    "restoring",
    "prepared",
    "abandoned",
]


class TestingCandidateApplication(FrozenRecord):
    plan: TestingCandidateApplicationPlan
    phase: CandidateApplicationPhase = "planned"
    deployment_id: str = ""
    prepared: TestingCandidatePrepared | None = None
    abandonment_evidence: str = ""
    restore_schedule_id: str = ""
    restore_previous_deployments: tuple[str, ...] = ()
    workflow_evidence: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_progress(self) -> Self:
        if self.phase in {"deployed", "restoring", "prepared"} and not self.deployment_id:
            raise ValueError("Candidate execution requires its original deployment receipt.")
        if self.prepared is not None and (
            self.prepared.plan_sha256 != self.plan.digest
            or self.prepared.runtime_identity != self.plan.runtime_identity
            or self.prepared.deployment_id != self.deployment_id
        ):
            raise ValueError("Prepared evidence differs from the original candidate/build.")
        if self.phase == "prepared" and self.prepared is None:
            raise ValueError("Candidate completion requires private readiness evidence.")
        if (self.phase == "abandoned") != bool(self.abandonment_evidence):
            raise ValueError("Candidate abandonment requires a terminal provider/custody receipt.")
        return self

    def require_successor(self, previous: Self) -> None:
        if (
            self.plan != previous.plan
            or previous.deployment_id
            and self.deployment_id != previous.deployment_id
            or previous.restore_schedule_id
            and (self.restore_schedule_id, self.restore_previous_deployments)
            != (previous.restore_schedule_id, previous.restore_previous_deployments)
            or previous.workflow_evidence
            and self.workflow_evidence != previous.workflow_evidence
            or previous.prepared is not None
            and self.prepared != previous.prepared
        ):
            raise ValueError(
                "Candidate continuation cannot replace its original intent or receipts."
            )
        phases = (
            "planned",
            "configuring",
            "configured",
            "deploying",
            "deployed",
            "restoring",
            "prepared",
            "abandoned",
        )
        if (
            phases.index(self.phase) < phases.index(previous.phase)
            or previous.abandonment_evidence
            and self.abandonment_evidence != previous.abandonment_evidence
        ):
            raise ValueError("Candidate continuation cannot forget an effect or abandonment.")
