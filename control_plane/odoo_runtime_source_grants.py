from pathlib import Path
from typing import TYPE_CHECKING, cast

from control_plane import odoo_runtime_reads as reads

if TYPE_CHECKING:
    from control_plane.odoo_runtime_source_prerequisites import (
        PrerequisiteStore,
        SourceConsumerEvidence,
    )


def runtime_grants(
    connection: reads.OdooRuntimeConnection,
    sources: tuple[str, ...],
    *,
    store: "PrerequisiteStore",
    product: str,
    control_plane_root: Path,
    database_url: str | None,
) -> dict[str, "SourceConsumerEvidence"]:
    from control_plane.odoo_runtime_source_prerequisites import (
        read_source_consumers,
        SourceConsumerEvidence,
        SourceName,
    )

    if not sources or connection.selection.identity.instance != "testing":
        return {name: SourceConsumerEvidence(source=cast(SourceName, name)) for name in sources}
    result = read_source_consumers(
        store=store,
        product=product,
        control_plane_root=control_plane_root,
        database_url=database_url,
        mode="grants",
        sources=sources,
        expected_runtime=connection,
    )
    observations = {source.source: source for source in result.sources}
    return {
        name: observations[name]
        if result.runtime == "verified" and name in observations
        else SourceConsumerEvidence(
            source=cast(SourceName, name),
            failure_stage=result.failure_stage
            or (observations[name].failure_stage if name in observations else None),
        )
        for name in sources
    }
