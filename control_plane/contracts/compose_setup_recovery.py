"""Retained evidence for an interrupted setup that predates durable allocation."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.dokploy_inventory import InventoryPage, InventoryProject


class RetainedComposeSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_key: str = Field(min_length=1)
    original_scope: str = Field(min_length=1)
    original_trace_id: str = Field(pattern=r"^launchplane_req_[a-f0-9]{32}$")
    original_request: dict[str, Any]
    original_request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    before: InventoryPage
    after: InventoryPage
    before_at: datetime
    after_at: datetime
    project_id: str = Field(min_length=1)
    environment_id: str = Field(min_length=1)
    custody_evidence: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_evidence(self) -> "RetainedComposeSetup":
        if (
            self.before_at.utcoffset() is None
            or self.after_at.utcoffset() is None
            or self.before_at >= self.after_at
        ):
            raise ValueError(
                "Original inventories require an ordered, timezone-aware observation window."
            )
        if canonical_json_sha256(self.original_request) != self.original_request_sha256:
            raise ValueError("Original payload digest differs.")
        for page in (self.before, self.after):
            # inventory_digest describes pre-hydration membership, not these
            # returned target details. The recovery digest binds the full pages.
            environments = [e for p in page.projects for e in p.environments]
            if (
                page.offset
                or page.next_offset is not None
                or len(page.projects) != page.total_projects
                or len(environments) != page.total_environments
                or sum(len(e.targets) for e in environments) != page.total_targets
                or len({p.project_id for p in page.projects}) != len(page.projects)
            ):
                raise ValueError("Complete original inventories are required.")
        before = {p.project_id: p for p in self.before.projects}
        after = {p.project_id: p for p in self.after.projects}
        if (
            self.project_id in before
            or set(after) != set(before) | {self.project_id}
            or any(after[key] != value for key, value in before.items())
        ):
            raise ValueError("Original inventories do not prove exactly one new project.")
        project = self.project()
        if (
            len(project.environments) != 1
            or project.environments[0].environment_id != self.environment_id
            or project.environments[0].name != "production"
            or project.environments[0].targets
        ):
            raise ValueError("Original partial placement must be one empty default environment.")
        return self

    def project(self) -> InventoryProject:
        return next(p for p in self.after.projects if p.project_id == self.project_id)
