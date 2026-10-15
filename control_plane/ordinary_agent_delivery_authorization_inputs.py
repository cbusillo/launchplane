"""Bounded inventory projection retained for historical policy preparation validation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import cast

from control_plane.contracts.ordinary_agent_delivery_authorization_inputs import (
    AuthorizationCandidateInputDiagnostic,
    RepositoryInventoryProjectionState,
)
from control_plane.contracts.repository_inventory import (
    RepositoryInventoryRecord,
)


# This bounds historical rows, not repository count. Reaching it must report
# incomplete evidence; a latest-per-repository storage read can replace the scan.
MAX_AUTHORIZATION_INPUT_SOURCE_RECORDS = 1000


def _read_current_inventory(
    record_store: object,
) -> tuple[
    RepositoryInventoryProjectionState,
    tuple[RepositoryInventoryRecord, ...],
    tuple[AuthorizationCandidateInputDiagnostic, ...],
]:
    reader = getattr(record_store, "list_repository_inventory_records", None)
    if not callable(reader):
        return (
            "unavailable",
            (),
            (_diagnostic("inventory_unavailable", "Repository inventory is unavailable."),),
        )
    try:
        raw_records = tuple(
            cast(Callable[..., tuple[object, ...]], reader)(
                limit=MAX_AUTHORIZATION_INPUT_SOURCE_RECORDS + 1
            )
        )
    except Exception:
        return (
            "unavailable",
            (),
            (_diagnostic("inventory_unavailable", "Repository inventory is unavailable."),),
        )
    try:
        records = tuple(RepositoryInventoryRecord.model_validate(record) for record in raw_records)
    except (TypeError, ValueError):
        return (
            "unavailable",
            (),
            (_diagnostic("inventory_unavailable", "Repository inventory is unavailable."),),
        )
    if len(records) > MAX_AUTHORIZATION_INPUT_SOURCE_RECORDS:
        return (
            "truncated",
            (),
            (
                _diagnostic(
                    "inventory_truncated",
                    "Repository inventory exceeds the bounded configuration read.",
                ),
            ),
        )

    records_by_id: dict[str, list[RepositoryInventoryRecord]] = defaultdict(list)
    for record in records:
        records_by_id[record.repository_id].append(record)
    ambiguous_ids: set[str] = set()
    current_records: list[RepositoryInventoryRecord] = []
    for repository_id, repository_records in records_by_id.items():
        highest_revision = max(record.inventory_revision for record in repository_records)
        highest_records = tuple(
            record for record in repository_records if record.inventory_revision == highest_revision
        )
        if len(highest_records) != 1:
            ambiguous_ids.add(repository_id)
            continue
        current_records.append(highest_records[0])

    tracked_records = [
        record
        for record in current_records
        if record.repository_id not in ambiguous_ids and record.inventory_state == "tracked"
    ]
    tracked_ids_by_repository: dict[str, set[str]] = defaultdict(set)
    for record in tracked_records:
        tracked_ids_by_repository[record.repository].add(record.repository_id)
    ambiguous_repositories = {
        repository
        for repository, repository_ids in tracked_ids_by_repository.items()
        if len(repository_ids) > 1
    }
    selected_records = tuple(
        sorted(
            (
                record
                for record in tracked_records
                if record.repository not in ambiguous_repositories
            ),
            key=lambda record: (record.repository, record.repository_id),
        )
    )
    if ambiguous_ids or ambiguous_repositories:
        return (
            "ambiguous",
            selected_records,
            (
                _diagnostic(
                    "inventory_ambiguous",
                    "Repository inventory has an ambiguous current configuration.",
                ),
            ),
        )
    if not selected_records:
        return (
            "complete",
            (),
            (
                _diagnostic(
                    "tracked_inventory_missing",
                    "No current tracked repository inventory configuration was found.",
                ),
            ),
        )
    return "complete", selected_records, ()


def _diagnostic(code: str, message: str) -> AuthorizationCandidateInputDiagnostic:
    return AuthorizationCandidateInputDiagnostic(code=code, message=message)
