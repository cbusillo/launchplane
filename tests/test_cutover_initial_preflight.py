import json
import unittest

from control_plane.dokploy.slot_compose import render_odoo_app_slot_compose
from control_plane.odoo_instance_overrides import ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY
from tests import test_odoo_slot_startup as fixtures


class CutoverInitialPreflightTests(unittest.TestCase):
    def test_initial_preflight_proves_candidate_definition_without_candidate_containers(
        self,
    ) -> None:
        fixture = fixtures.SlotStartupTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for name in tuple(fixture.containers):
            if name.startswith(fixture.plan.new.compose_id):
                del fixture.containers[name]
        fixture.fail_readback = fixture.plan.new.compose_id
        definition = render_odoo_app_slot_compose(
            fixture.plan, data_volume="isolated-data", private_network="isolated-network"
        )
        fixture.startup.preflight_candidate(fixture.plan, definition)
        self.assertEqual(fixture.effects, [])
        invalid = json.loads(definition)
        invalid["services"]["scheduler"]["environment"][ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY] = (
            "inert"
        )
        with self.assertRaises(ValueError):
            fixture.startup.preflight_candidate(fixture.plan, json.dumps(invalid))
        self.assertEqual(fixture.effects, [])
