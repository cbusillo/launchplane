from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from control_plane.contracts.generic_web_deploy_recovery import (
    GenericWebDeployRecoveryApplyResponse,
    GenericWebDeployRecoveryDryRunResponse,
    GenericWebDeployRecoveryReferenceRequest,
)
from control_plane.event_deploy_recovery_reference import resolve_event_deploy_recovery_coordinates
from control_plane.generic_web_deploy_recovery_http import (
    GENERIC_WEB_DEPLOY_RECOVERY_APPLY_ROUTE,
    GENERIC_WEB_DEPLOY_RECOVERY_DRY_RUN_ROUTE,
)
from control_plane.http_app import idempotency_request_fingerprint
from tests import test_generic_web_testing_abandonment as abandonment_fixture


class TestingAbandonmentClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = abandonment_fixture.GenericWebTestingAbandonmentTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/abandon-generic-web-testing.py")
        )
        coordinates = resolve_event_deploy_recovery_coordinates(
            store=self.fixture.store,
            product=self.fixture.fixture.profile.product,
            context=self.fixture.deployment.context,
            fingerprint=idempotency_request_fingerprint,
        )
        self.plan = GenericWebDeployRecoveryDryRunResponse.model_validate(self.fixture.inspect())
        self.draft = GenericWebDeployRecoveryReferenceRequest(
            product=self.plan.product,
            recovery_reference=coordinates.reference,
            reason="Abandon the unqualified testing fixture without claiming its outcome.",
        )
        self.applied = GenericWebDeployRecoveryApplyResponse(
            trace_id="launchplane_req_abandonment_fixture",
            product=self.plan.product,
            context=self.plan.context,
            instance="testing",
            reservation_state="completed",
            reservation_attempt=self.plan.reservation_attempt,
            recovery_action="abandon_testing_original",
            recovery_digest=self.plan.recovery_digest,
            provider_outcome="unknown",
            retry_safe=False,
        )
        self.readback = self.plan.model_copy(
            update={
                "reservation_state": "completed",
                "proposed_action": "replay_completed",
                "provider_outcome": "not_inspected",
            }
        )
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.helper = self.root / "launchplane-write-action.py"
        self.helper.touch()
        self.request = self.root / "request.json"
        self.request.write_text(self.draft.model_dump_json())
        self.evidence = self.root / "evidence.json"
        self.post = Mock()
        self.transport: dict[str, Any] = {
            "resolve_settings": lambda _args: {
                "service_url": "https://service.invalid",
                "token": "fixture-private-token",
            },
            "validate_service_url": lambda _url: None,
            "request_launchplane": self.post,
            "read_payload_file": lambda path: json.loads(Path(path).read_text()),
        }

    def invoke(self, mode: str) -> tuple[int, dict[str, Any]]:
        output = io.StringIO()
        argv = [
            "client",
            mode,
            "--operator-helper",
            str(self.helper),
            "--request-file",
            str(self.request),
            "--evidence-file",
            str(self.evidence),
        ]
        if mode == "apply":
            argv.append("--reviewed-dry-run")
        with (
            patch("runpy.run_path", return_value=self.transport),
            patch("sys.argv", argv),
            redirect_stdout(output),
        ):
            status = self.client["main"]()
        self.assertNotIn("fixture-private-token", output.getvalue())
        return status, json.loads(output.getvalue())

    def test_review_binding_apply_and_read_only_readback(self) -> None:
        self.post.side_effect = [
            self.plan.model_dump(mode="json"),
            self.applied.model_dump(mode="json"),
            self.readback.model_dump(mode="json"),
            self.readback.model_dump(mode="json"),
        ]
        for mode in ("dry-run", "apply", "readback"):
            status, output = self.invoke(mode)
            self.assertEqual(status, 0, output)
        self.assertEqual(self.evidence.stat().st_mode & 0o077, 0)
        self.assertEqual(
            [call.kwargs["path"] for call in self.post.call_args_list],
            [
                GENERIC_WEB_DEPLOY_RECOVERY_DRY_RUN_ROUTE,
                GENERIC_WEB_DEPLOY_RECOVERY_APPLY_ROUTE,
                GENERIC_WEB_DEPLOY_RECOVERY_DRY_RUN_ROUTE,
                GENERIC_WEB_DEPLOY_RECOVERY_DRY_RUN_ROUTE,
            ],
        )
        self.request.write_text(
            self.draft.model_copy(update={"reason": "A different review"}).model_dump_json()
        )
        count = self.post.call_count
        self.assertEqual(self.invoke("apply")[0], 1)
        self.request.write_text(
            self.draft.model_copy(update={"instance": "prod"}).model_dump_json()
        )
        self.assertEqual(self.invoke("apply")[0], 1)
        self.assertEqual(self.post.call_count, count)

    def test_lost_reply_retains_review_and_readback_never_applies_an_unfinished_original(
        self,
    ) -> None:
        self.post.return_value = self.plan.model_dump(mode="json")
        self.assertEqual(self.invoke("dry-run")[0], 0)
        saved = self.evidence.read_bytes()
        self.post.reset_mock()
        self.post.side_effect = URLError("Lost the apply response")
        status, output = self.invoke("apply")
        self.assertEqual(status, 1)
        self.assertEqual(output["status"], "outcome_unknown")
        self.assertEqual(self.post.call_count, 1)
        self.post.reset_mock()
        self.post.side_effect = None
        self.post.return_value = self.plan.model_dump(mode="json")
        self.assertEqual(self.invoke("readback")[0], 1)
        self.assertEqual(
            self.post.call_args.kwargs["path"], GENERIC_WEB_DEPLOY_RECOVERY_DRY_RUN_ROUTE
        )
        self.assertEqual(self.evidence.read_bytes(), saved)
        self.post.reset_mock()
        self.post.side_effect = [
            self.applied.model_dump(mode="json"),
            URLError("Readback unavailable"),
        ]
        status, output = self.invoke("apply")
        self.assertEqual(status, 1)
        self.assertEqual(output["status"], "accepted_unverified")
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self.evidence.read_bytes(), saved)
