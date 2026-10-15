"""Concrete provider pause authority and both-slot writer fencing."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import MagicMock

from control_plane.contracts.odoo_cutover_lane import OdooCutoverLane, OdooSlotCoordinates
from control_plane.dokploy.odoo_cutover import DokployOdooCutoverProvider, OdooSlotDriver
from control_plane.dokploy.slot_writers import ContainerSlotWriters, SlotWriterContainer
from tests.test_odoo_cutover_website_settings import WebsiteProvider
from control_plane.odoo_cutover_plan import rollback_pause_identity


class WebsiteProviderAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = WebsiteProvider(Path(self.temporary.name))
        self.addCleanup(self.fixture.engine.dispose)
        self.plan = self.fixture.cutover
        plan = self.plan
        self.site = self.fixture.prepare_public(plan)
        lane = OdooCutoverLane(
            route_id=plan.route_id,
            host=plan.host,
            database=plan.old.binding.database,
            database_uuid=plan.old.runtime_identity.database_uuid,
            prepared_origin="http://prepared:8080",
            slots=cast(
                tuple[OdooSlotCoordinates, OdooSlotCoordinates],
                tuple(
                    OdooSlotCoordinates(
                        **slot.model_dump(include={"slot_id", "compose_id", "private_url"})
                    )
                    for slot in (plan.old, plan.new)
                ),
            ),
            writers=plan.writers,
        )
        self.route: Any = MagicMock()
        self.route.evidence_id.return_value = "fixture-applied-route"
        self.route.target.route_id = plan.route_id
        self.route.target.host = plan.host
        self.current = plan.old.private_url
        self.staged: dict[str, Any] = {}

        def publish(target: str, *, expected: tuple[str, ...]) -> str:
            if self.current not in expected:
                raise ValueError("foreign route")
            self.current = target
            return target

        self.route.publish.side_effect = publish
        self.route.observed_target.side_effect = lambda: self.current

        def stage(site: Any, pause_id: str) -> str:
            self.staged[pause_id] = site
            return lane.prepared_origin

        def load(pause_id: str) -> Any:
            if pause_id not in self.staged:
                raise FileNotFoundError(pause_id)
            return self.staged[pause_id]

        def observe(_plan: Any, target: str) -> Any:
            slot = plan.old if target == plan.old.private_url else plan.new
            self.fixture.target = slot
            health = self.fixture.current_route(plan).public_health
            return slot.slot_id, slot.binding, health

        self.driver = OdooSlotDriver(
            exclusive=self.fixture.exclusive,
            prepare_candidate=MagicMock(),
            prepare_public=self.fixture.prepare_public,
            backup=self.fixture.backup,
            maintenance=self.fixture.maintenance,
            readiness=self.fixture.readiness,
            observe_public=observe,
            stage_public=stage,
            observe_prepared=lambda pause_id: self.staged[pause_id].content_digest,
            fence_and_drain=self.fixture.fence_and_drain,
            handoff_writers=MagicMock(),
            drain_old=self.fixture.drain_old,
            load_prepared=load,
            lane=lane,
            verify_endpoint=MagicMock(),
            website_settings=self.fixture.website_settings,
        )
        self.provider = DokployOdooCutoverProvider(
            plan, self.driver, self.route, pause_id="release-pause"
        )

    def test_rollback_pause_can_publish_from_new_but_forward_pause_cannot(self) -> None:
        self.current = self.plan.new.private_url
        with self.assertRaises(ValueError):
            self.provider.publish(self.site, "release-pause")
        self.assertEqual(self.current, self.plan.new.private_url)
        digest = self.provider.publish(self.site, rollback_pause_identity("release-pause"))
        self.assertEqual(digest, self.site.content_digest)
        self.assertEqual(
            self.provider.observed_public_copy(rollback_pause_identity("release-pause")), digest
        )
        with self.assertRaises(ValueError):
            self.provider.publish(self.site, "unrelated-pause")

    def test_recreated_provider_switches_from_verified_rollback_publication(self) -> None:
        self.current = self.plan.new.private_url
        self.provider.publish(self.site, rollback_pause_identity("release-pause"))
        recreated = DokployOdooCutoverProvider(
            self.plan, self.driver, self.route, pause_id="release-pause"
        )
        observation = recreated.switch(self.plan, self.plan.old)
        self.assertEqual(observation.binding, self.plan.old.binding)
        self.assertEqual(self.current, self.plan.old.private_url)

    def test_preflight_refusal_happens_before_candidate_preparation(self) -> None:
        prepare = self.driver.prepare_candidate
        assert isinstance(prepare, MagicMock)
        with self.assertRaises(ValueError):
            self.provider.prepare_candidate(self.plan)
        prepare.assert_not_called()
        order: list[str] = []
        self.driver = self.driver.__class__(
            **{
                **self.driver.__dict__,
                "website_settings_preflight": lambda _plan: order.append("preflight"),
                "prepare_candidate": lambda _plan: order.append("prepare"),
            }
        )
        provider = DokployOdooCutoverProvider(
            self.plan, self.driver, self.route, pause_id="release-pause"
        )
        provider.prepare_candidate(self.plan)
        self.assertEqual(order, ["preflight", "prepare"])

    def test_concrete_writer_fence_stops_both_web_slots_before_settings(self) -> None:
        targets = tuple(
            SlotWriterContainer(
                writer=writer,
                slot_id=slot.slot_id,
                container=slot.slot_id + "-" + writer.writer_id,
                image=slot.image,
            )
            for slot in (self.plan.old, self.plan.new)
            for writer in self.plan.writers
        )
        running = {target.container: True for target in targets}
        images = {target.container: target.image for target in targets}

        def execute(argv: tuple[str, ...], _stdin: str | None) -> str:
            container = argv[-1]
            if argv[1] == "stop":
                running[container] = False
                return ""
            return json.dumps(
                [{"Config": {"Image": images[container]}, "State": {"Running": running[container]}}]
            )

        writers = ContainerSlotWriters(
            self.plan, targets, execute, active_jobs=lambda target: int(running[target.container])
        )
        observations = writers.fence(self.site, rollback_pause_identity("release-pause"))
        self.assertTrue(observations)
        self.assertFalse(any(running.values()))
        for target in targets:
            if target.writer.kind == "web":
                self.assertFalse(running[target.container])
        self.assertTrue(
            all(
                item.pause_id == rollback_pause_identity("release-pause") and item.active_jobs == 0
                for item in observations
            )
        )
