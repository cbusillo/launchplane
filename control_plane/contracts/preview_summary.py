from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord


class LaunchplanePreviewSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview: PreviewRecord
    latest_generation: PreviewGenerationRecord | None = None
    recent_generations: tuple[PreviewGenerationRecord, ...] = Field(default_factory=tuple)
