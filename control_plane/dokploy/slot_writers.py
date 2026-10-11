"""Observe and hand off explicitly inventoried slot writer containers."""

from dataclasses import dataclass
import json
from collections.abc import Callable

from control_plane.contracts.odoo_cutover import OdooCutoverPlan, OdooSlot
from control_plane.contracts.prepared_public_site import (
    PreparedPublicSite,
    WriterObservation,
    WriterTarget,
)
from control_plane.dokploy.slot_maintenance import ContainerCommand


@dataclass(frozen=True)
class SlotWriterContainer:
    writer: WriterTarget
    slot_id: str
    container: str
    image: str


class ContainerSlotWriters:
    """The lane's exclusive release lock covers every stop/start/readback.

    Container coordinates come from the bound provider inventory. The database
    observer must count live jobs/connections belonging to each stopped writer;
    a stopped container alone does not establish a coherent recovery point.
    """

    def __init__(
        self,
        plan: OdooCutoverPlan,
        targets: tuple[SlotWriterContainer, ...],
        execute: ContainerCommand,
        active_jobs: Callable[[SlotWriterContainer], int],
    ) -> None:
        expected = {
            (slot.slot_id, writer.writer_id)
            for slot in (plan.old, plan.new)
            for writer in plan.writers
        }
        if {(target.slot_id, target.writer.writer_id) for target in targets} != expected or len(
            targets
        ) != len(expected):
            raise ValueError("writer inventory must bind each lane writer to both slots")
        if len({target.container for target in targets}) != len(targets):
            raise ValueError("writer containers must have distinct provider identities")
        for target in targets:
            slot = plan.old if target.slot_id == plan.old.slot_id else plan.new
            if target.image != slot.image or target.writer not in plan.writers:
                raise ValueError("writer inventory differs from immutable release targets")
        self.plan, self.targets, self.execute, self.active_jobs = (
            plan,
            targets,
            execute,
            active_jobs,
        )

    def _running(self, target: SlotWriterContainer) -> bool:
        observed = json.loads(self.execute(("docker", "inspect", target.container), None))
        if len(observed) != 1 or observed[0]["Config"]["Image"] != target.image:
            raise ValueError("writer container identity/image changed")
        if observed[0]["State"].get("Restarting"):
            raise ValueError("writer state is uncertain while restarting")
        return bool(observed[0]["State"]["Running"])

    def _stop(self, target: SlotWriterContainer) -> None:
        if self._running(target):
            self.execute(("docker", "stop", target.container), None)
        if self._running(target) or self.active_jobs(target) != 0:
            raise ValueError("writer has not drained after fencing")

    def fence(self, site: PreparedPublicSite, pause_id: str) -> tuple[WriterObservation, ...]:
        if site.plan.binding != self.plan.old.binding or site.plan.writers != self.plan.writers:
            raise ValueError("writer pause differs from the admitted release")
        # The privately prepared candidate must also be unable to schedule work.
        for target in self.targets:
            self._stop(target)
        return tuple(
            WriterObservation(
                binding=site.plan.binding,
                pause_id=pause_id,
                writer=writer,
                fenced=True,
                active_jobs=0,
                evidence_id=pause_id + ":" + writer.writer_id,
            )
            for writer in self.plan.writers
        )

    def handoff(self, slot: OdooSlot) -> tuple[tuple[str, int], ...]:
        if slot not in (self.plan.old, self.plan.new):
            raise ValueError("writer resume target differs from the release")
        for target in self.targets:
            if target.slot_id != slot.slot_id:
                self._stop(target)
        for target in self.targets:
            if target.slot_id == slot.slot_id and not self._running(target):
                self.execute(("docker", "start", target.container), None)
        owners = []
        for writer in self.plan.writers:
            running = [
                target
                for target in self.targets
                if target.writer == writer and self._running(target)
            ]
            if len(running) != 1 or running[0].slot_id != slot.slot_id:
                raise ValueError("writer resume has no unique observed target owner")
            owners.append((writer.writer_id, len(running)))
        return tuple(owners)
