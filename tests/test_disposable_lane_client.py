"""The evidence client binds collection to its inspection and suppresses private output."""

from contextlib import redirect_stdout
import io
import http.client
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from control_plane.disposable_lane_collection import disposable_collection_coverage


class DisposableLaneClientTests(unittest.TestCase):
    def call_client(
        self,
        *,
        collection_mode: str = "collect",
        command_mode: str = "collect",
        private_fact: bool = False,
        truncated: bool = False,
        provider_mutation: bool = False,
        binding_sha256: str = "a" * 64,
        inspected_binding_sha256: str = "a" * 64,
        usage: str = "unknown",
        reason_code: object = None,
        runtime_facts: bool | None = False,
        qualification: str | None = None,
        data: str = "unknown",
        production_resources: str = "unknown",
        lifetime_seconds: int | None = None,
        expected_binding_sha256: str | None = None,
        expected_observation_sha256: str | None = "d" * 64,
        observation_sha256: str = "d" * 64,
        observation: object = None,
        request_mock: Mock | None = None,
        application_resources: object = None,
        include_application_resources: bool = False,
        instance: str = "testing",
        preview_id: str = "",
    ) -> tuple[int, str, Mock]:
        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/collect-disposable-lane.py")
        )
        facts = {"usage": usage, "data": data, "production_resources": production_resources}
        inspected = {
            "trace_id": "fixture-trace",
            "result": {
                "mode": "inspect",
                "binding_sha256": inspected_binding_sha256,
                "source_coverage": disposable_collection_coverage(),
                "observation_sha256": observation_sha256,
                "observation": observation,
                **facts,
            },
        }
        collected_result: dict[str, object] = {
            "mode": collection_mode,
            "observer_kind": "director_agent_preview_cleanup",
            "source_coverage": disposable_collection_coverage(),
            "binding_sha256": binding_sha256,
            "provider_mutation": provider_mutation,
            "odoo_usage": {
                "recent_signins": runtime_facts,
                "authenticated_sessions": runtime_facts,
            },
            **facts,
        }
        if qualification is not None:
            collected_result["never_served_qualification"] = qualification
        if include_application_resources:
            collected_result["application_resources"] = application_resources
        if reason_code is not None:
            odoo_usage = collected_result["odoo_usage"]
            assert isinstance(odoo_usage, dict)
            odoo_usage["reason_code"] = reason_code
        collected = {
            "trace_id": "fixture-trace",
            "private_debug": "private-fixture",
            "result": collected_result,
        }
        if private_fact:
            collected_result["usage"] = "private-fixture"
        request = request_mock if request_mock is not None else Mock()
        request.side_effect = [
            inspected,
            http.client.IncompleteRead(b"", 1) if truncated else collected,
        ]
        if command_mode == "clear":
            request.side_effect = [
                http.client.IncompleteRead(b"", 1)
                if truncated
                else {
                    "trace_id": "fixture-trace",
                    "result": {
                        "mode": "clear",
                        "provider_mutation": provider_mutation,
                        "binding_sha256": binding_sha256,
                        "observation_sha256": observation_sha256,
                    },
                }
            ]
        transport = {
            "resolve_settings": lambda args: {
                "service_url": "https://service.invalid",
                "token": "fixture-token",
            },
            "validate_service_url": lambda value: None,
            "request_launchplane": request,
            "public_trace_id": lambda value: value,
        }
        with TemporaryDirectory() as directory:
            helper = Path(directory) / "launchplane-write-action.py"
            helper.touch()
            arguments = [
                "collect-disposable-lane.py",
                command_mode,
                "--operator-helper",
                str(helper),
                "--product",
                "example",
                "--context",
                "example",
                "--instance",
                instance,
            ]
            if preview_id:
                arguments += ["--preview-id", preview_id]
            if command_mode == "record":
                arguments += [
                    "--preview-id",
                    "fixture-preview",
                    "--usage",
                    usage,
                    "--data",
                    data,
                    "--production-resources",
                    production_resources,
                    "--usage-evidence",
                    "private-evidence",
                    "--data-evidence",
                    "private-evidence",
                    "--isolation-evidence",
                    "private-evidence",
                ]
            if lifetime_seconds is not None:
                arguments += ["--lifetime-seconds", str(lifetime_seconds)]
            if include_application_resources:
                arguments += ["--application-resources"]
            if command_mode == "clear" and expected_observation_sha256 is not None:
                arguments += ["--expected-observation-sha256", expected_observation_sha256]
            if expected_binding_sha256 is not None:
                arguments += ["--expected-binding-sha256", expected_binding_sha256]
            output = io.StringIO()
            with (
                patch("sys.argv", arguments),
                patch("runpy.run_path", return_value=transport),
                redirect_stdout(output),
            ):
                status = client["main"]()
            return status, output.getvalue(), request

    def test_application_resource_observation_is_explicit_and_value_free(self) -> None:
        from control_plane.contracts.application_resource_observation import (
            ApplicationResourceObservation,
        )

        for metadata in (
            ApplicationResourceObservation(reason_code="resource_inventory_unavailable"),
            ApplicationResourceObservation(
                reason_code="resource_inventory_changed", changed_component="container_metadata"
            ),
        ):
            with self.subTest(metadata=metadata):
                observation = metadata.model_dump(mode="json")
                status, output, request = self.call_client(
                    application_resources=observation, include_application_resources=True
                )
                self.assertEqual(status, 0, output)
                self.assertEqual(json.loads(output)["application_resources"], observation)
                self.assertTrue(request.call_args.kwargs["body"]["include_application_resources"])
                self.assertNotIn(
                    "include_application_resources", request.call_args_list[0].kwargs["body"]
                )
        status, output, request = self.call_client()
        self.assertEqual(status, 0, output)
        self.assertNotIn("application_resources", json.loads(output))
        self.assertNotIn("include_application_resources", request.call_args.kwargs["body"])

    def test_invalid_application_resource_metadata_does_not_escape_the_client(self) -> None:
        status, output, _request = self.call_client(
            include_application_resources=True,
            application_resources={
                "reason_code": "synthetic-private-detail",
                "private_file": "synthetic-private-content",
            },
        )
        self.assertNotEqual(status, 0)
        self.assertNotIn("synthetic-private", output)
        self.assertEqual(json.loads(output)["status"], "unverified")

    def test_invalid_application_resource_scope_is_rejected_before_inspection(self) -> None:
        for instance, preview_id in (("prod", ""), ("testing", "preview")):
            with self.subTest(instance=instance, preview_id=preview_id):
                request = Mock()
                with self.assertRaises(SystemExit) as raised:
                    self.call_client(
                        include_application_resources=True,
                        instance=instance,
                        preview_id=preview_id,
                        request_mock=request,
                    )
                self.assertEqual(raised.exception.code, 2)
                request.assert_not_called()

    def test_record_binds_preview_facts_to_the_inspection_without_printing_references(self) -> None:
        status, output, request = self.call_client(
            command_mode="record",
            collection_mode="record",
            usage="unused",
            data="reproducible",
            production_resources="isolated",
        )
        self.assertEqual(status, 0, output)
        body = request.call_args.kwargs["body"]
        self.assertEqual(body["mode"], "record")
        self.assertTrue(body["expected_binding_sha256"])
        self.assertEqual(body["usage"], "unused")
        self.assertTrue(body["preview_id"])
        self.assertNotIn("private-evidence", output)

    def test_clear_uses_the_existing_evidence_only_service_operation(self) -> None:
        proof = "a" * 64
        observation_proof = "d" * 64
        status, output, request = self.call_client(
            command_mode="clear",
            expected_binding_sha256=proof,
            expected_observation_sha256=observation_proof,
        )
        self.assertEqual(status, 0, output)
        request.assert_called_once()
        body = request.call_args.kwargs["body"]
        self.assertEqual(body["mode"], "clear")
        self.assertEqual(body["expected_binding_sha256"], proof)
        self.assertEqual(body["expected_observation_sha256"], observation_proof)
        self.assertEqual(
            (body["product"], body["context"], body["instance"]), ("example", "example", "testing")
        )

    def test_clear_requires_reviewed_input_and_checks_receipt_without_retry(self) -> None:
        for proof in (None, "", "invalid"):
            request = Mock()
            with self.subTest(proof=proof), self.assertRaises(SystemExit) as refusal:
                self.call_client(
                    command_mode="clear", expected_binding_sha256=proof, request_mock=request
                )
            self.assertEqual(refusal.exception.code, 2)
            request.assert_not_called()
        for proof in (None, "", "invalid"):
            request = Mock()
            with self.subTest(observation=proof), self.assertRaises(SystemExit) as refusal:
                self.call_client(
                    command_mode="clear",
                    expected_binding_sha256="a" * 64,
                    expected_observation_sha256=proof,
                    request_mock=request,
                )
            self.assertEqual(refusal.exception.code, 2)
            request.assert_not_called()
        for changes in (
            {"observation_sha256": "e" * 64},
            {"binding_sha256": "b" * 64},
            {"provider_mutation": True},
            {"truncated": True},
        ):
            with self.subTest(changes=changes):
                status, output, request = self.call_client(
                    command_mode="clear", expected_binding_sha256="a" * 64, **changes
                )
                self.assertEqual(status, 1, output)
                request.assert_called_once()
                self.assertEqual(json.loads(output)["status"], "unverified")

    def test_inspect_exposes_binding_for_review_before_clear(self) -> None:
        proof = "c" * 64
        observation_proof = "e" * 64
        protection = {"usage": "in_use", "data": "only_copy", "production_resources": "shared"}
        for observation in (None, {**protection, "private_debug": "private-fixture"}):
            with self.subTest(observation=observation):
                status, output, request = self.call_client(
                    command_mode="inspect",
                    inspected_binding_sha256=proof,
                    observation_sha256=observation_proof,
                    observation=observation,
                )
                self.assertEqual(status, 0, output)
                request.assert_called_once()
                result = json.loads(output)
                self.assertEqual(result["binding_sha256"], proof)
                self.assertEqual(result.get("observation_sha256"), observation_proof)
                self.assertIn("observation", result)
                self.assertEqual(result["observation"], protection if observation else None)
                self.assertEqual(result["source_coverage"], disposable_collection_coverage())
                self.assertIn("facts", result)
                self.assertNotIn("private-fixture", output)
                self.assertNotIn("expected_observation_sha256", request.call_args.kwargs["body"])

    def test_inspect_refuses_invalid_recorded_protection_without_printing_it(self) -> None:
        protection = {"usage": "in_use", "data": "only_copy", "production_resources": "shared"}
        invalid_reports: tuple[object, ...] = (
            *({**protection, key: "private-fixture"} for key in protection),
            {"usage": "in_use", "private_debug": "private-fixture"},
            "private-fixture",
        )
        for digest, observation in (
            ("private-fixture", None),
            *(("e" * 64, report) for report in invalid_reports),
        ):
            with self.subTest(digest=digest, observation=observation):
                status, output, request = self.call_client(
                    command_mode="inspect", observation_sha256=digest, observation=observation
                )
                self.assertEqual(status, 1, output)
                request.assert_called_once()
                self.assertEqual(json.loads(output)["status"], "unverified")
                self.assertNotIn("private-fixture", output)

    def test_observation_lifetime_uses_the_service_contract_limit(self) -> None:
        from control_plane.contracts.disposable_lane import MAX_DISPOSABLE_LANE_SAFETY_SECONDS

        lifetime = MAX_DISPOSABLE_LANE_SAFETY_SECONDS
        status, output, request = self.call_client(lifetime_seconds=lifetime)
        self.assertEqual(status, 0, output)
        self.assertEqual(request.call_args.kwargs["body"]["lifetime_seconds"], lifetime)
        status, output, request = self.call_client(lifetime_seconds=lifetime + 1)
        self.assertEqual(status, 1, output)
        request.assert_called_once()

    def test_qualified_resource_facts_are_reported_without_private_evidence(self) -> None:
        for data, resources in (("reproducible", "isolated"), ("only_copy", "shared")):
            status, output, _ = self.call_client(data=data, production_resources=resources)
            self.assertEqual(status, 0, output)
            facts = json.loads(output)["facts"]
            self.assertEqual((facts["data"], facts["production_resources"]), (data, resources))
            self.assertNotIn("private-fixture", output)

    def test_collection_uses_inspected_binding_and_reports_only_safe_facts_and_coverage(
        self,
    ) -> None:
        status, output, request = self.call_client()
        self.assertEqual(status, 0)
        first, second = request.call_args_list
        self.assertEqual(second.kwargs["body"]["expected_binding_sha256"], "a" * 64)
        self.assertEqual(first.kwargs["body"]["mode"], "inspect")
        self.assertEqual(second.kwargs["body"]["mode"], "collect")
        self.assertNotIn("private-fixture", output)
        self.assertNotIn("fixture-token", output)
        self.assertEqual(json.loads(output)["source_coverage"], disposable_collection_coverage())

    def test_unsettled_mode_and_invalid_fact_never_report_collected_evidence(self) -> None:
        for mode, private_fact in (("inspect", False), ("collect", True)):
            with self.subTest(mode=mode, private_fact=private_fact):
                status, output, _ = self.call_client(
                    collection_mode=mode, private_fact=private_fact
                )
                self.assertEqual(status, 1)
                self.assertEqual(json.loads(output)["status"], "unverified")
                self.assertNotIn("private-fixture", output)

    def test_service_collected_non_use_is_reported_by_the_bounded_client(self) -> None:
        status, output, _ = self.call_client(usage="unused")
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output)["facts"]["usage"], "unused")

    def test_never_served_uses_no_invented_session_or_login_boolean(self) -> None:
        status, output, _ = self.call_client(
            usage="unused",
            reason_code="never_served",
            runtime_facts=None,
            qualification="never_served",
        )
        self.assertEqual(status, 0)
        result = json.loads(output)
        self.assertIsNone(result["odoo_usage"]["recent_signins"])
        self.assertIsNone(result["odoo_usage"]["authenticated_sessions"])
        status, _, _ = self.call_client(usage="unused", reason_code="never_served")
        self.assertEqual(status, 1)

    def test_never_served_receipt_requires_matching_qualification_and_marks_old_diagnostics_missing(
        self,
    ) -> None:
        status, output, _ = self.call_client()
        self.assertEqual(status, 0)
        self.assertEqual(
            json.loads(output)["never_served_qualification"], "never_served_diagnostics_unavailable"
        )
        for reason, qualification in (("never_served", None), ("complete", "never_served")):
            status, _, _ = self.call_client(
                usage="unused",
                reason_code=reason,
                qualification=qualification,
                runtime_facts=None if reason == "never_served" else False,
            )
            self.assertEqual(status, 1)

    def test_truncated_or_mismatched_receipt_stays_unverified(self) -> None:
        for truncated, mutation, digest in (
            (True, False, "a" * 64),
            (False, True, "a" * 64),
            (False, False, "b" * 64),
        ):
            with self.subTest(truncated=truncated, mutation=mutation, digest=digest):
                status, output, _ = self.call_client(
                    truncated=truncated, provider_mutation=mutation, binding_sha256=digest
                )
                self.assertEqual(status, 1)
                self.assertEqual(json.loads(output)["status"], "unverified")

    def test_client_reports_bounded_reason_and_marks_older_service_diagnostics_unavailable(
        self,
    ) -> None:
        for reason in (None, "serving_process_absent", "session_store_unavailable"):
            with self.subTest(reason=reason):
                status, output, _ = self.call_client(reason_code=reason)
                self.assertEqual(status, 0)
                self.assertEqual(json.loads(output)["facts"]["usage"], "unknown")
                self.assertEqual(
                    json.loads(output)["odoo_usage"]["reason_code"],
                    reason or "diagnostics_unavailable",
                )
        for invalid_reason in (
            "private-fixture topology",
            ["private-fixture"],
            {"private-fixture": True},
        ):
            with self.subTest(reason=invalid_reason):
                status, output, _ = self.call_client(reason_code=invalid_reason)
                self.assertEqual(status, 1)
                self.assertNotIn("private-fixture", output)

    def test_future_diagnostic_keeps_safe_facts_and_trace_without_echoing_unknown_text(
        self,
    ) -> None:
        for usage in ("unknown", "in_use"):
            with self.subTest(usage=usage):
                status, output, _ = self.call_client(usage=usage, reason_code="future_checkpoint")
                self.assertEqual(status, 0)
                result = json.loads(output)
                self.assertEqual(result["odoo_usage"]["reason_code"], "diagnostics_unrecognized")
                self.assertEqual(result["facts"]["usage"], usage)
                self.assertTrue(result["trace_id"])
                self.assertNotIn("future_checkpoint", output)

    def test_negative_usage_requires_consistent_diagnostic_when_service_supplies_one(self) -> None:
        for usage, reason, expected_status in (
            ("unused", "complete", 0),
            ("unused", None, 0),  # Older services have complete booleans but no diagnostic.
            ("unused", "not_collected", 1),
            ("unused", "future_checkpoint", 1),
            ("unknown", "complete", 1),
        ):
            with self.subTest(usage=usage, reason=reason):
                status, _, _ = self.call_client(usage=usage, reason_code=reason)
                self.assertEqual(status, expected_status)
