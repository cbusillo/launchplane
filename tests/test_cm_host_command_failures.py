import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from control_plane.copy_readback_diagnostics import (
    COPY_FAILURE_EXIT_MARKER,
    COPY_FAILURE_KIND_MARKER,
    COPY_FAILURE_STAGE_MARKER,
    COPY_FAILURE_EVIDENCE_MARKER,
    CopyFailureStage,
)
from control_plane import http_error_evidence
from control_plane.dokploy import native_copy_stream as transport
from tests.test_odoo_testing_native_source import Connection

from control_plane.dokploy.native_testing_stream import NATIVE_TESTING_STREAM_WRITE_PROGRAM
from control_plane.dokploy.testing_retained_copy import retained_copy_host_program
from control_plane.dokploy.testing_route_transport import testing_route_host_program
from control_plane.odoo_testing_resources import ODOO_TESTING_RESOURCE_PROGRAM, OdooTestingResources
from control_plane.testing_candidate_probe import CANDIDATE_HOST_PROBE


class CMHostCommandFailureTests(unittest.TestCase):
    def test_dispatched_programs_preserve_command_cause_without_exposing_command_output(
        self,
    ) -> None:
        namespace = "lp-testing-" + "a" * 32
        resources = OdooTestingResources(
            product="fixture",
            context="fixture",
            operation_id="original",
            target_id="candidate",
            namespace=namespace,
        )
        programs: tuple[tuple[str, dict[str, object], bool, CopyFailureStage], ...] = (
            (
                ODOO_TESTING_RESOURCE_PROGRAM,
                resources.host_spec("prepare"),
                False,
                CopyFailureStage.HOST_PREPARATION,
            ),
            (
                retained_copy_host_program(),
                {"mode": "read", "project": "retained"},
                True,
                CopyFailureStage.HOST_PREPARATION,
            ),
            (
                retained_copy_host_program(),
                {"mode": "remove", "project": "retained"},
                True,
                CopyFailureStage.CLEANUP,
            ),
            (
                testing_route_host_program(),
                {
                    "mode": "read",
                    "route_id": "lp-testing-route-" + "a" * 32,
                    "host": "testing.example.invalid",
                },
                True,
                CopyFailureStage.HOST_PREPARATION,
            ),
            (
                CANDIDATE_HOST_PROBE,
                {
                    "generation": {"instance": "testing", "namespace": namespace},
                    "compose_project": namespace,
                    "database_name": "fixture",
                },
                True,
                CopyFailureStage.HOST_PREPARATION,
            ),
            (
                NATIVE_TESTING_STREAM_WRITE_PROGRAM,
                {
                    "container": "a" * 64,
                    "capture_id": "b" * 32,
                    "part": "database",
                    "artifact": {"size": 5, "sha256": "c" * 64},
                    "reserve_bytes": 0,
                },
                True,
                CopyFailureStage.SOURCE_TRANSFER,
            ),
        )
        for program, spec, framed, stage in programs:
            with self.subTest(program=program[:60]), TemporaryDirectory() as temporary:
                output = io.StringIO()
                private = "password=fixture-secret /credential/path dump-content"

                def failed(
                    command: list[str], **kwargs: object
                ) -> subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str]:
                    if kwargs.get("text"):
                        return subprocess.CompletedProcess(
                            command, 7, stdout=private, stderr=private
                        )
                    return subprocess.CompletedProcess(
                        command, 7, stdout=private.encode(), stderr=private.encode()
                    )

                child = SimpleNamespace(
                    stdin=io.BytesIO(),
                    stdout=io.BytesIO(),
                    stderr=io.BytesIO(private.encode()),
                    wait=lambda: 7,
                    poll=lambda: 7,
                )
                with (
                    patch.object(Path, "home", return_value=Path(temporary)),
                    patch.object(
                        sys,
                        "argv",
                        ["program", base64.b64encode(json.dumps(spec).encode()).decode(), "FRAME:"],
                    ),
                    patch("subprocess.run", side_effect=failed),
                    patch("subprocess.Popen", return_value=child),
                    redirect_stdout(output),
                ):
                    try:
                        exec(
                            compile(program, "<host-command-test>", "exec"),
                            {"__name__": "__main__"},
                        )
                    except SystemExit:
                        pass
                public = output.getvalue()
                for value in private.split():
                    self.assertNotIn(value, public)
                frame = (
                    json.loads(public.splitlines()[-1].removeprefix("FRAME:"))
                    if framed
                    else dict(line.split("=", 1) for line in public.splitlines())
                )
                self.assertEqual(frame.get(COPY_FAILURE_KIND_MARKER), "command_failed")
                self.assertEqual(frame.get(COPY_FAILURE_STAGE_MARKER), stage)
                self.assertEqual(frame.get(COPY_FAILURE_EXIT_MARKER), "7")
                self.assertEqual(frame.get(COPY_FAILURE_EVIDENCE_MARKER), "unavailable")
                self.assertEqual(list(Path(temporary).iterdir()), [])
                if framed:
                    with self.assertRaises(transport.NativeProgramFailed) as raised:
                        transport._read_program(
                            connection=Connection(frame),
                            program=program,
                            payload=spec,
                            timeout_seconds=5,
                            receive=lambda _frame: None,
                        )
                    with self.assertLogs(http_error_evidence.__name__, level="WARNING") as logs:
                        http_error_evidence.log_handled_error_cause(
                            raised.exception, "host-command-test"
                        )
                    public_log = logs.records[-1].getMessage()
                    for value in private.split():
                        self.assertNotIn(value, public_log)
                    native = json.loads(public_log)["native_programs"][0]
                    self.assertEqual(
                        native[COPY_FAILURE_KIND_MARKER], frame[COPY_FAILURE_KIND_MARKER]
                    )
                    self.assertEqual(native[COPY_FAILURE_STAGE_MARKER], stage)
                    self.assertEqual(
                        native[COPY_FAILURE_EXIT_MARKER], frame[COPY_FAILURE_EXIT_MARKER]
                    )
