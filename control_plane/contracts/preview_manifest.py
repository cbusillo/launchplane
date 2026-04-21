from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.preview_generation_record import PreviewPullRequestSummary, PreviewSourceRecord


class LaunchplaneResolvedPreviewManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    baseline_channel: str
    baseline_release_tuple_id: str
    resolved_manifest_fingerprint: str
    source_map: tuple[PreviewSourceRecord, ...]
    companion_summaries: tuple[PreviewPullRequestSummary, ...] = ()

    @model_validator(mode="after")
    def _validate_manifest(self) -> "LaunchplaneResolvedPreviewManifest":
        if not self.context.strip():
            raise ValueError("resolved Launchplane preview manifest requires context")
        if not self.baseline_channel.strip():
            raise ValueError("resolved Launchplane preview manifest requires baseline_channel")
        if not self.baseline_release_tuple_id.strip():
            raise ValueError("resolved Launchplane preview manifest requires baseline_release_tuple_id")
        if not self.resolved_manifest_fingerprint.strip():
            raise ValueError("resolved Launchplane preview manifest requires resolved_manifest_fingerprint")
        if not self.source_map:
            raise ValueError("resolved Launchplane preview manifest requires source_map")
        return self
