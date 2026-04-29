from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.data_provenance import DataProvenance
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord


class LaunchplanePreviewSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview: PreviewRecord
    latest_generation: PreviewGenerationRecord | None = None
    recent_generations: tuple[PreviewGenerationRecord, ...] = Field(default_factory=tuple)
    provenance: DataProvenance = DataProvenance(
        source_kind="record",
        freshness_status="missing",
        detail="Launchplane has not recorded preview generation evidence.",
    )
