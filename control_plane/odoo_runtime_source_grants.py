from pathlib import Path
from typing import TYPE_CHECKING

from control_plane import odoo_runtime_reads as reads
from control_plane.odoo_source_probe import GrantStatus

if TYPE_CHECKING:
    from control_plane.odoo_runtime_source_prerequisites import PrerequisiteStore


def runtime_grants(
    connection: reads.OdooRuntimeConnection,
    sources: tuple[str, ...],
    *,
    store: "PrerequisiteStore",
    product: str,
    control_plane_root: Path,
    database_url: str | None,
) -> dict[str, GrantStatus]:
    from control_plane.odoo_runtime_source_prerequisites import read_source_consumers

    if not sources or connection.selection.identity.instance != "testing":
        return {name: "unknown" for name in sources}
    result = read_source_consumers(
        store=store,
        product=product,
        control_plane_root=control_plane_root,
        database_url=database_url,
        mode="grants",
        sources=sources,
    )
    return {
        name: next((source.grants for source in result.sources if source.source == name), "unknown")
        if result.runtime == "verified"
        else "unknown"
        for name in sources
    }
