import hashlib
import unittest
from unittest.mock import patch

from control_plane.dokploy.native_copy_stream import NATIVE_PROGRAM_SOURCE_SITE_LIMIT
from control_plane.dokploy.post_deploy import extract_odoo_post_deploy_readback_markers
from control_plane.testing_application_capabilities import (
    CAPABILITY_FAILURE_REPORT,
    LEGACY_CAPABILITY_FAILURE_REPORT,
    capability_program_body,
    retained_application_capability_program,
)
from control_plane.testing_program_evidence import CAPABILITY_FAILURE_SITES_MARKER
from control_plane.testing_separation_inspection import inspect_program_history
from control_plane.testing_separation_resources import SeparationProgramInspection


class CapabilityFailureEvidenceTests(unittest.TestCase):
    def test_marker_parser_retains_only_bounded_positive_numeric_sites(self) -> None:
        valid = "1,2"
        for value in (
            valid,
            "0",
            "-1",
            "1,private-token",
            "1 " + "2",
            "1" * 21,
            ",".join(["1"] * (NATIVE_PROGRAM_SOURCE_SITE_LIMIT + 1)),
        ):
            with self.subTest(value=value):
                markers = extract_odoo_post_deploy_readback_markers(
                    {"logs": [f"{CAPABILITY_FAILURE_SITES_MARKER}={value}"]}
                )
                self.assertEqual(
                    markers.get(CAPABILITY_FAILURE_SITES_MARKER), valid if value == valid else None
                )

    def test_inspection_binds_failure_locations_to_the_actual_recognized_program(self) -> None:
        script = retained_application_capability_program({"application": "fixture"})
        body = capability_program_body(script)
        history = [
            {
                "scheduleId": "capability",
                "deploymentId": "execution",
                "status": "error",
                "startedAt": "2026-10-24T00:00:00Z",
                "finishedAt": "2026-10-24T00:00:01Z",
            }
        ]
        legacy = script.replace(CAPABILITY_FAILURE_REPORT, LEGACY_CAPABILITY_FAILURE_REPORT, 1)
        for kind, matched, disabled, program, sites, available, rows in (
            ("retained_capability", True, True, script, "1,2", True, history),
            ("retained_capability_readback", True, True, script, "1,2", True, history),
            ("retained_capability", False, True, script, "1,2", True, history),
            ("retained_capability", True, False, script, "1,2", True, history),
            ("retained_capability", True, True, legacy, "1,2", True, history),
            (
                "retained_capability",
                True,
                True,
                script,
                str(len(body.splitlines()) + 1),
                True,
                history,
            ),
            ("retained_capability", True, True, script, "0", True, history),
            ("retained_capability", True, True, script, "private-token", True, history),
            ("retained_capability", True, True, script, "1,2", False, history),
            (
                "retained_capability",
                True,
                True,
                script,
                "1,2",
                True,
                [{**history[0], "status": "running"}],
            ),
            ("storage_preparation", True, True, script, "1,2", True, history),
        ):
            with self.subTest(
                kind=kind, matched=matched, disabled=disabled, sites=sites, available=available
            ):
                observed = SeparationProgramInspection.model_validate(
                    {
                        "kind": kind,
                        "status": "observed",
                        "schedule_id": "capability",
                        "script_matches": matched,
                        "disabled": disabled,
                    }
                )
                with (
                    patch(
                        "control_plane.testing_separation_inspection.api.dokploy_request",
                        return_value=rows,
                    ),
                    patch(
                        "control_plane.testing_separation_inspection.post_deploy._read_odoo_post_deploy_log_markers",
                        return_value={
                            "log_available": str(available).lower(),
                            "testing_resources_ok": "false",
                            CAPABILITY_FAILURE_SITES_MARKER: sites,
                        },
                    ),
                ):
                    inspect_program_history(
                        host="https://fixture.invalid",
                        token="fixture-token",
                        observed=observed,
                        binding=None,
                        program=program,
                    )
                valid = (
                    kind.startswith("retained_capability")
                    and matched
                    and disabled
                    and program == script
                    and sites == "1,2"
                    and available
                    and rows == history
                )
                self.assertEqual(observed.failure_program_sites, (1, 2) if valid else None)
                self.assertEqual(
                    observed.failure_program_sha256,
                    hashlib.sha256(body.encode()).hexdigest() if valid else None,
                )
