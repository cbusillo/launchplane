"""Fresh lane/preview admission happens before the first provider create."""

from unittest.mock import Mock, patch
import unittest

from control_plane.contracts.host_network_capacity import BridgeNetworkCapacity
from control_plane.dokploy.network_capacity import (
    BRIDGE_NETWORK_CAPACITY_MARGIN,
    BridgeNetworkCapacityRefused,
)
from control_plane.workflows.dokploy_target_adoption import create_dokploy_compose_target
from control_plane.workflows.odoo_preview_runtime import (
    _resolve_or_create_compose,
    build_odoo_preview_dokploy_dry_run,
    OdooPreviewDokployDryRunRequest,
)
from tests.test_odoo_preview_runtime import _endpoint_spec, _runtime_plan


class LaneNetworkCapacityTests(unittest.TestCase):
    def test_fresh_lane_refuses_short_or_unknown_before_any_create_or_record(self) -> None:
        for observation in (
            BridgeNetworkCapacity(total_slots=16, free_slots=0),
            BridgeNetworkCapacity(total_slots=16, free_slots=BRIDGE_NETWORK_CAPACITY_MARGIN),
            BridgeNetworkCapacityRefused("host_bridge_network_capacity_unavailable"),
        ):
            with self.subTest(observation=observation):
                store = Mock()
                store.list_physical_provider_target_records.return_value = ()
                store.read_provider_target_record.side_effect = FileNotFoundError
                mutate = Mock(side_effect=AssertionError("No provider create may run"))
                with (
                    patch(
                        "control_plane.dokploy.network_capacity.read_bridge_network_capacity",
                        **(
                            {"side_effect": observation}
                            if isinstance(observation, Exception)
                            else {"return_value": observation}
                        ),
                    ),
                    self.assertRaises(BridgeNetworkCapacityRefused),
                ):
                    create_dokploy_compose_target(
                        record_store=store,
                        host="https://provider.invalid",
                        token="fixture",
                        context="synthetic-site",
                        instance="testing",
                        target_name="fresh-lane",
                        project_name="fresh-project",
                        server_id="selected-server",
                        apply=True,
                        mutate_provider=mutate,
                        fetch_target_payload=Mock(),
                    )
                mutate.assert_not_called()
                store.write_product_authority_bundle.assert_not_called()

    def test_lane_exact_margin_admits_first_create_on_selected_host(self) -> None:
        store = Mock()
        store.list_physical_provider_target_records.return_value = ()
        store.read_provider_target_record.side_effect = FileNotFoundError
        mutate = Mock(side_effect=RuntimeError("first-create-reached"))
        with (
            patch(
                "control_plane.dokploy.network_capacity.read_bridge_network_capacity",
                return_value=BridgeNetworkCapacity(
                    total_slots=16, free_slots=1 + BRIDGE_NETWORK_CAPACITY_MARGIN
                ),
            ) as read,
            self.assertRaisesRegex(RuntimeError, "first-create-reached"),
        ):
            create_dokploy_compose_target(
                record_store=store,
                host="https://provider.invalid",
                token="fixture",
                context="synthetic-site",
                instance="testing",
                target_name="fresh-lane",
                project_name="fresh-project",
                server_id="selected-server",
                apply=True,
                mutate_provider=mutate,
                fetch_target_payload=Mock(),
            )
        self.assertEqual(read.call_args.kwargs["server_id"], "selected-server")
        self.assertEqual(mutate.call_count, 1)
        with (
            patch(
                "control_plane.dokploy.network_capacity.read_bridge_network_capacity",
                side_effect=AssertionError("Original partial creation must keep its recovery path"),
            ) as repeated_read,
            self.assertRaisesRegex(RuntimeError, "first-create-reached"),
        ):
            create_dokploy_compose_target(
                record_store=store,
                host="https://provider.invalid",
                token="fixture",
                context="synthetic-site",
                instance="testing",
                target_name="fresh-lane",
                project_id="original-project",
                server_id="selected-server",
                apply=True,
                resuming_create=True,
                mutate_provider=mutate,
                fetch_target_payload=Mock(),
            )
        repeated_read.assert_not_called()

    def test_fresh_preview_refuses_before_create_checkpoint_with_existing_precedence(self) -> None:
        plan = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(),
                endpoint_spec=_endpoint_spec(),
                template_compose_id="template",
                environment_id="environment",
            )
        )
        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime._find_compose_id_by_environment_and_name",
                return_value="",
            ) as find,
            patch(
                "control_plane.dokploy.network_capacity.read_bridge_network_capacity",
                return_value=BridgeNetworkCapacity(total_slots=16, free_slots=0),
            ) as read,
            patch(
                "control_plane.workflows.odoo_preview_runtime.dokploy_api.dokploy_request",
                return_value={"composeId": "new-compose"},
            ) as mutate,
        ):
            checkpoint = Mock()
            with self.assertRaises(BridgeNetworkCapacityRefused):
                _resolve_or_create_compose(
                    host="https://provider.invalid",
                    token="fixture",
                    plan=plan,
                    template_payload={"serverId": "preview-host"},
                    steps=[],
                    provider_effect_checkpoint=checkpoint,
                )
            checkpoint.assert_not_called()
            mutate.assert_not_called()
            self.assertEqual(read.call_args.kwargs["server_id"], "preview-host")
            read.reset_mock()
            find.return_value = "original-compose"
            self.assertEqual(
                _resolve_or_create_compose(
                    host="https://provider.invalid",
                    token="fixture",
                    plan=plan,
                    template_payload={"serverId": "preview-host"},
                    steps=[],
                    provider_effect_checkpoint=checkpoint,
                ),
                ("original-compose", False),
            )
            read.assert_not_called()
