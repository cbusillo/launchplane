import json
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane.dokploy.service_cutover import OdooHostCapabilities, bind_odoo_driver
from control_plane.dokploy.slot_writers import ContainerSlotWriters, SlotWriterContainer
from tests import test_odoo_cutover_website_provider as fixtures


class CutoverWriterLivenessTests(unittest.TestCase):
    def test_compatible_maintenance_cannot_release_a_running_candidate_scheduler(self) -> None:
        fixture = fixtures.WebsiteProviderAdapterTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        plan = fixture.plan.model_copy(
            update={"website_settings": None, "production_website_settings": None}
        )
        targets = tuple(
            SlotWriterContainer(
                writer,
                slot.slot_id,
                slot.slot_id + ("-web" if writer.kind == "web" else "-scheduler"),
                slot.image,
            )
            for slot in (plan.old, plan.new)
            for writer in plan.writers
        )
        running = {target.container: True for target in targets}
        images = {target.container: target.image for target in targets}

        def execute(argv: tuple[str, ...], stdin: str | None) -> str:
            if argv[1] == "stop":
                running[argv[-1]] = False
                return ""
            return json.dumps(
                [{"Config": {"Image": images[argv[-1]]}, "State": {"Running": running[argv[-1]]}}]
            )

        writers = ContainerSlotWriters(
            plan, targets, execute, lambda target: int(running[target.container])
        )
        capabilities = OdooHostCapabilities(fixture.driver, writers, execute, lambda slot: "runner")
        bound = bind_odoo_driver(plan, capabilities)

        def write_marker(*args: Any, **kwargs: Any) -> Mock:
            self.assertFalse(running[plan.new.slot_id + "-scheduler"])
            self.assertTrue(running[plan.old.slot_id + "-scheduler"])
            return Mock()

        with patch(
            "control_plane.dokploy.service_cutover.run_planned_slot_maintenance",
            side_effect=write_marker,
        ) as maintenance:
            assert bound.maintenance is not None
            bound.maintenance(plan)
        maintenance.assert_called_once()

    def test_running_container_without_native_liveness_cannot_report_writer_ownership(self) -> None:
        fixture = fixtures.WebsiteProviderAdapterTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        plan = fixture.plan
        targets = tuple(
            SlotWriterContainer(writer, slot.slot_id, slot.slot_id + writer.writer_id, slot.image)
            for slot in (plan.old, plan.new)
            for writer in plan.writers
        )
        commands = Mock()
        writers = ContainerSlotWriters(plan, targets, commands, lambda target: 0)
        with self.assertRaises(ValueError):
            writers.handoff(plan.new)
        commands.assert_not_called()
