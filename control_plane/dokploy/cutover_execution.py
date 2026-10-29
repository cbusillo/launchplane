from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from control_plane.contracts.canonical_json import canonical_model_sha256
from control_plane.contracts.odoo_cutover import OdooCutoverPlan


@dataclass(frozen=True)
class CutoverExecution:
    plan_sha256: str
    dispatch_guard: Callable[[], None]


_EXECUTION: ContextVar[CutoverExecution | None] = ContextVar("cutover_execution", default=None)


@contextmanager
def bind_cutover_execution(
    plan: OdooCutoverPlan,
    checkpoint: Callable[[str], None],
    *,
    dispatch_guard: Callable[[], None] | None = None,
) -> Iterator[None]:
    if _EXECUTION.get() is not None:
        raise ValueError("cutover execution cannot replace another admitted operation")
    from control_plane.workflows.odoo_stable_target_replacement import (
        TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE,
    )

    guard = dispatch_guard or (lambda: checkpoint(TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE))
    token = _EXECUTION.set(CutoverExecution(canonical_model_sha256(plan), guard))
    try:
        yield
    finally:
        _EXECUTION.reset(token)


def require_cutover_execution(plan: OdooCutoverPlan) -> None:
    execution = _EXECUTION.get()
    if execution is None or execution.plan_sha256 != canonical_model_sha256(plan):
        raise ValueError("cutover host requires the original admitted release execution")
    execution.dispatch_guard()
