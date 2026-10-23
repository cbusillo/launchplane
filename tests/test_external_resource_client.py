import contextlib
import io
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductSecretConfigRequirement,
)
from tests.support.profiles import _generic_site_profile_payload


class ExternalResourceClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.helper = root / "launchplane-write-action.py"
        self.helper.touch()
        self.evidence = root / "plan.json"
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _generic_site_profile_payload()
        )
        self.requirement = ProductSecretConfigRequirement(
            binding_key="BOT_ACCESS", context=self.profile.product, instance="testing"
        )
        self.profile.expected_config.managed_secret_bindings = (self.requirement,)
        self.client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/declare-external-resource.py")
        )
        self.reader = Mock(
            side_effect=lambda **_: {
                "trace_id": "launchplane_req_fixture",
                "profile": self.profile.model_dump(mode="json"),
            }
        )
        self.writer = Mock(side_effect=self.write)
        self.transport = {
            "resolve_settings": lambda _: {
                "service_url": "https://fixture.example.invalid",
                "token": "synthetic-operator",
            },
            "validate_service_url": lambda _: None,
            "public_trace_id": lambda value: value,
            "request_launchplane_read": self.reader,
            "request_launchplane": self.writer,
        }
        self.argv = [
            "client",
            "dry-run",
            "--product",
            self.profile.product,
            "--context",
            self.profile.product,
            "--instance",
            "testing",
            "--binding-key",
            "BOT_ACCESS",
            "--provider",
            "discord_bot",
            "--reason",
            "Declare a known provider purpose.",
            "--evidence-file",
            str(self.evidence),
            "--operator-helper",
            str(self.helper),
        ]

    def write(self, **kwargs: Any) -> dict[str, Any]:
        payload = kwargs["body"]
        if payload["mode"] == "apply":
            self.profile.expected_config.managed_secret_bindings = (
                ProductSecretConfigRequirement.model_validate(
                    {**self.requirement.model_dump(mode="json"), "external_provider": "discord_bot"}
                ),
            )
        return {
            "status": "accepted",
            "trace_id": "launchplane_req_fixture",
            "records": {"product_profile": self.profile.product},
            "result": {"product": self.profile.product, "mode": payload["mode"]},
        }

    def run_client(self) -> tuple[int, dict[str, Any]]:
        output = io.StringIO()
        with (
            patch("runpy.run_path", return_value=self.transport),
            patch("sys.argv", self.argv),
            contextlib.redirect_stdout(output),
        ):
            code = self.client["main"]()
        return code, json.loads(output.getvalue())

    def apply_args(self) -> None:
        self.argv[1] = "apply"
        self.argv += ["--reviewed-dry-run", "--idempotency-key", "original-key"]

    def test_declares_without_values_then_verifies_apply_and_original_key_replay(self) -> None:
        self.assertEqual(self.run_client()[0], 0)
        self.assertEqual(self.evidence.stat().st_mode & 0o777, 0o600)
        saved = json.loads(self.evidence.read_text())
        self.assertNotIn("value", saved["request"]["managed_secret_bindings"][0])
        self.apply_args()
        for _ in range(2):
            code, result = self.run_client()
            self.assertEqual(code, 0, result)
            self.assertEqual(result["status"], "verified")
            self.assertEqual(self.writer.call_args.kwargs["idempotency_key"], "original-key")
            self.assertEqual(
                self.writer.call_args.kwargs["body"]["managed_secret_bindings"],
                saved["request"]["managed_secret_bindings"],
            )

    def test_unreviewed_metadata_change_refuses_before_write(self) -> None:
        self.assertEqual(self.run_client()[0], 0)
        self.profile.expected_config.managed_secret_bindings = (
            self.requirement.model_copy(update={"testing_local_credential": True}),
        )
        self.writer.reset_mock()
        self.apply_args()
        code, result = self.run_client()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "unverified")
        self.writer.assert_not_called()

    def test_accepted_declaration_with_wrong_readback_is_not_verified(self) -> None:
        self.assertEqual(self.run_client()[0], 0)
        self.writer.side_effect = lambda **_: {
            "status": "accepted",
            "trace_id": "launchplane_req_fixture",
            "records": {},
            "result": {"product": self.profile.product, "mode": "apply"},
        }
        self.apply_args()
        code, result = self.run_client()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "accepted_unverified")
