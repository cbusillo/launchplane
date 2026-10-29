import unittest
from unittest.mock import patch

import click

from control_plane.dokploy import api


class ApplicationDeploymentHistoryTests(unittest.TestCase):
    def test_complete_application_history_refuses_discarded_rows_and_ambiguous_identity(
        self,
    ) -> None:
        original = {"deploymentId": "original", "status": "done"}
        for payload in (
            [original, None],
            {"unexpected": [original]},
            {"data": [original], "items": [original]},
            [original, original],
            [{"status": "done"}],
        ):
            with (
                self.subTest(payload=payload),
                patch.object(api, "dokploy_request", return_value=payload),
                self.assertRaises(click.ClickException),
            ):
                api.list_deployments_for_target(
                    host="https://provider.test",
                    token="fixture",
                    target_type="application",
                    target_id="candidate",
                    require_complete=True,
                )

    def test_complete_application_history_keeps_supported_empty_and_wrapped_responses(self) -> None:
        original = {"deploymentId": "original", "status": "done"}
        for payload, expected in (
            ([], []),
            ([original], [original]),
            ({"items": [original]}, [original]),
        ):
            with (
                self.subTest(payload=payload),
                patch.object(api, "dokploy_request", return_value=payload),
            ):
                self.assertEqual(
                    api.list_deployments_for_target(
                        host="https://provider.test",
                        token="fixture",
                        target_type="application",
                        target_id="candidate",
                        require_complete=True,
                    ),
                    expected,
                )
