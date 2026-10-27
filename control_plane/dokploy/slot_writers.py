"""Observe and hand off explicitly inventoried slot writer containers."""

from dataclasses import dataclass
import json
import hashlib
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


def physical_writer_targets(
    plan: OdooCutoverPlan, targets: tuple[SlotWriterContainer, ...]
) -> tuple[SlotWriterContainer, ...]:
    expected = {
        (slot.slot_id, writer.writer_id) for slot in (plan.old, plan.new) for writer in plan.writers
    }
    if {(target.slot_id, target.writer.writer_id) for target in targets} != expected or len(
        targets
    ) != len(expected):
        raise ValueError("writer inventory must bind each lane writer to both slots")
    physical: dict[str, SlotWriterContainer] = {}
    for target in targets:
        slot = plan.old if target.slot_id == plan.old.slot_id else plan.new
        if not target.container or target.image != slot.image or target.writer not in plan.writers:
            raise ValueError("writer inventory differs from immutable release targets")
        previous = physical.setdefault(target.container, target)
        if (previous.slot_id, previous.image) != (target.slot_id, target.image):
            raise ValueError("writer container cannot be shared across slots or images")
    return tuple(physical.values())


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
        *,
        native_ready: Callable[[SlotWriterContainer], None] | None = None,
        database_evidence: Callable[[SlotWriterContainer], str] | None = None,
    ) -> None:
        physical_writer_targets(plan, targets)
        self.plan, self.targets, self.execute, self.active_jobs = (
            plan,
            targets,
            execute,
            active_jobs,
        )
        self.native_ready = native_ready
        self.database_evidence = database_evidence

    def fence_candidate(self) -> None:
        targets = tuple(
            target for target in self.targets if target.slot_id == self.plan.new.slot_id
        )
        for target in physical_writer_targets(self.plan, self.targets):
            if target.slot_id == self.plan.new.slot_id:
                self._stop(target)
        self.require_drained(targets)

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
        if self._running(target):
            raise ValueError("writer has not drained after fencing")

    def require_drained(self, targets: tuple[SlotWriterContainer, ...]) -> None:
        if any(self.active_jobs(target) != 0 for target in targets):
            raise ValueError("logical writer database jobs are not drained")

    def fence(self, site: PreparedPublicSite, pause_id: str) -> tuple[WriterObservation, ...]:
        if site.plan.binding != self.plan.old.binding or site.plan.writers != self.plan.writers:
            raise ValueError("writer pause differs from the admitted release")
        if self.database_evidence is None:
            raise ValueError("writer fence requires native database observation evidence")
        # The privately prepared candidate must also be unable to schedule work.
        for target in physical_writer_targets(self.plan, self.targets):
            self._stop(target)
        self.require_drained(self.targets)
        evidence = {}
        for target in self.targets:
            observed = self.database_evidence(target)
            if not observed:
                raise ValueError("logical writer drain has no native observation evidence")
            evidence[target.slot_id, target.writer.writer_id] = observed
        return tuple(
            WriterObservation(
                binding=site.plan.binding,
                pause_id=pause_id,
                writer=writer,
                fenced=True,
                active_jobs=0,
                evidence_id=hashlib.sha256(
                    json.dumps(
                        [
                            pause_id,
                            writer.writer_id,
                            *[
                                evidence[slot.slot_id, writer.writer_id]
                                for slot in (self.plan.old, self.plan.new)
                            ],
                        ]
                    ).encode()
                ).hexdigest(),
            )
            for writer in self.plan.writers
        )

    def handoff(self, slot: OdooSlot) -> tuple[tuple[str, int], ...]:
        if slot not in (self.plan.old, self.plan.new):
            raise ValueError("writer resume target differs from the release")
        if self.native_ready is None:
            raise ValueError("writer handoff requires native process liveness evidence")
        physical = physical_writer_targets(self.plan, self.targets)
        for target in physical:
            if target.slot_id != slot.slot_id:
                self._stop(target)
        self.require_drained(
            tuple(target for target in self.targets if target.slot_id != slot.slot_id)
        )
        for target in physical:
            if target.slot_id == slot.slot_id and not self._running(target):
                self.execute(("docker", "start", target.container), None)
        running_containers = {target.container for target in physical if self._running(target)}
        owners = []
        for writer in self.plan.writers:
            running = [
                target
                for target in self.targets
                if target.writer == writer and target.container in running_containers
            ]
            if len(running) != 1 or running[0].slot_id != slot.slot_id:
                raise ValueError("writer resume has no unique observed target owner")
            self.native_ready(running[0])
            owners.append((writer.writer_id, len(running)))
        return tuple(owners)
