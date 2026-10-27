from dataclasses import dataclass
from typing import Literal

from control_plane.contracts.idempotency_record import parse_launchplane_mutation_timestamp
from control_plane.dokploy import api

MAX_PROGRAM_ROWS = 10000
type ProgramHistoryStatus = Literal[
    "terminal", "pending", "empty", "ambiguous", "uncorrelated", "unavailable"
]


@dataclass(frozen=True)
class ProgramInventory:
    status: Literal["present", "missing", "ambiguous", "unavailable"]
    program: api.JsonObject | None = None


@dataclass(frozen=True)
class ProgramHistory:
    status: ProgramHistoryStatus
    rows: tuple[api.JsonObject, ...] = ()


def program_rows(payload: object) -> tuple[api.JsonObject, ...] | None:
    if (
        not isinstance(payload, list)
        or len(payload) > MAX_PROGRAM_ROWS
        or any(not isinstance(row, dict) for row in payload)
    ):
        return None
    return tuple(row for row in payload if isinstance(row, dict))


def classify_program_inventory(payload: object, *, name: str, namespace: str) -> ProgramInventory:
    rows = program_rows(payload)
    if rows is None:
        return ProgramInventory("unavailable")
    matches = [row for row in rows if row.get("name") == name and row.get("appName") == namespace]
    if not matches:
        return ProgramInventory("missing")
    if len(matches) != 1:
        return ProgramInventory("ambiguous")
    return ProgramInventory("present", matches[0])


def classify_program_history(
    payload: object, *, schedule_id: str, original: bool
) -> ProgramHistory:
    rows = program_rows(payload)
    if rows is None or not schedule_id:
        return ProgramHistory("unavailable")
    if any(row.get("scheduleId") != schedule_id for row in rows):
        return ProgramHistory("uncorrelated")
    identifiers = [api.deployment_key(row) for row in rows]
    if any(not identifier for identifier in identifiers) or len(set(identifiers)) != len(
        identifiers
    ):
        return ProgramHistory("uncorrelated")
    if not rows:
        return ProgramHistory("empty", rows)
    if original and len(rows) != 1:
        return ProgramHistory("ambiguous", rows)
    if not all(program_execution_terminal(row) for row in rows):
        return ProgramHistory("pending", rows)
    return ProgramHistory("terminal", rows)


def program_execution_terminal(row: api.JsonObject) -> bool:
    if api.deployment_status(row) not in {"done", "error"}:
        return False
    started_at, finished_at = row.get("startedAt"), row.get("finishedAt")
    if not isinstance(started_at, str) or not isinstance(finished_at, str):
        return False
    try:
        started = parse_launchplane_mutation_timestamp(started_at, field_name="startedAt")
        finished = parse_launchplane_mutation_timestamp(finished_at, field_name="finishedAt")
    except ValueError:
        return False
    return finished >= started
