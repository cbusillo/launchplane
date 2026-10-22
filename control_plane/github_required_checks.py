from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from control_plane.merge_train_candidate_failures import (
    FAILURE_CONCLUSIONS,
    INFRASTRUCTURE_CONCLUSIONS,
)

RequiredCheckState = Literal["pass", "pending", "fail", "unavailable"]


class RequiredCheckIdentity(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def app_id(self) -> int | None: ...


class RequiredCheckSignal(RequiredCheckIdentity, Protocol):
    @property
    def source(self) -> Literal["commit_status", "check_run"]: ...

    @property
    def state(self) -> RequiredCheckState: ...


@dataclass(frozen=True)
class CheckRequirement:
    name: str
    app_id: int | None = None


@dataclass(frozen=True)
class CheckSignal:
    source: Literal["commit_status", "check_run"]
    name: str
    app_id: int | None
    state: RequiredCheckState


@dataclass(frozen=True)
class RequiredCheckVerdict:
    status: RequiredCheckState
    missing: tuple[str, ...] = ()


def matches_required_check(
    *,
    requirement: RequiredCheckIdentity,
    name: str,
    app_id: int | None,
    source: Literal["commit_status", "check_run"],
) -> bool:
    return name.casefold() == requirement.name.casefold() and (
        requirement.app_id is None or app_id == requirement.app_id
    )


def evaluate_required_checks(
    *, requirements: Sequence[RequiredCheckIdentity], signals: Sequence[RequiredCheckSignal]
) -> RequiredCheckVerdict:
    if not requirements:
        return RequiredCheckVerdict("unavailable")
    states: set[RequiredCheckState] = set()
    missing: list[str] = []
    for requirement in requirements:
        matches = tuple(
            signal
            for signal in signals
            if matches_required_check(
                requirement=requirement,
                name=signal.name,
                app_id=signal.app_id,
                source=signal.source,
            )
        )
        if matches:
            states.update(signal.state for signal in matches)
        else:
            states.add("pending")
            missing.append(
                requirement.name
                if requirement.app_id is None
                else f"{requirement.name} (app_id={requirement.app_id})"
            )
    for state in ("fail", "pending", "unavailable"):
        if state in states:
            return RequiredCheckVerdict(state, tuple(missing))
    return RequiredCheckVerdict("pass")


def commit_status_state(value: str) -> RequiredCheckState:
    normalized = value.strip().lower()
    if normalized == "success":
        return "pass"
    if normalized in {"failure", "error"}:
        return "fail"
    return "pending" if normalized == "pending" else "unavailable"


def check_run_state(check_run: dict[str, object]) -> RequiredCheckState:
    status = str(check_run.get("status") or "").strip().lower()
    if status != "completed":
        return (
            "pending"
            if status in {"queued", "in_progress", "pending", "waiting", "requested"}
            else "unavailable"
        )
    conclusion = str(check_run.get("conclusion") or "").strip().lower()
    if conclusion in {"success", "neutral", "skipped"}:
        return "pass"
    if conclusion in FAILURE_CONCLUSIONS or conclusion in INFRASTRUCTURE_CONCLUSIONS:
        return "fail"
    return "unavailable"
