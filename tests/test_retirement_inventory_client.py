from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import Mock, patch


CLIENT_PATH = Path(__file__).resolve().parents[1] / "scripts/inspect-retirement-inventory.py"
CLIENT = runpy.run_path(str(CLIENT_PATH))


class RetirementInventoryClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = {"service_url": "https://example.invalid", "token": "PRIVATE_TOKEN"}
        self.transport: dict[str, Any] = {
            "request_launchplane_read": Mock(),
            "request_launchplane": Mock(),
            "public_trace_id": lambda value: value,
        }
        self.args = argparse.Namespace(context="example", instance="testing")

    def runtime_response(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "trace_id": "launchplane_req_fixture",
            "context": "example",
            "instance": "testing",
            "settings": [
                {
                    "scope": "global",
                    "context": "",
                    "instance": "",
                    "keys": ["BUILD_INPUT"],
                    "values": {"BUILD_INPUT": "PRIVATE_BUILD_VALUE"},
                    "withheld_keys": [],
                    "updated_at": "2026-01-01T00:00:00Z",
                },
                {
                    "scope": "instance",
                    "context": "example",
                    "instance": "testing",
                    "keys": ["API_PASSWORD", "RUNTIME_INPUT"],
                    "values": {"RUNTIME_INPUT": "PRIVATE_VALUE"},
                    "withheld_keys": ["API_PASSWORD"],
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            ],
        }

    def inspect(self, response: object) -> dict[str, object]:
        self.transport["request_launchplane_read"].return_value = response
        return cast(
            dict[str, object], CLIENT["inspect_settings"](self.transport, self.settings, self.args)
        )

    def test_setting_values_never_leave_the_projection(self) -> None:
        result = self.inspect(self.runtime_response())
        rendered = json.dumps(result)
        self.assertNotIn("PRIVATE", rendered)
        self.assertIn("BUILD_INPUT", rendered)
        self.assertIn("API_PASSWORD", rendered)
        self.assertFalse(result["absence_proven"])
        self.transport["request_launchplane"].assert_not_called()
        self.assertEqual(
            self.transport["request_launchplane_read"].call_args.kwargs["path"],
            CLIENT["operation_route"]("list_instance_runtime_settings", "get").format(
                context=self.args.context, instance=self.args.instance
            ),
        )

    def test_wrong_scope_and_incomplete_key_coverage_fail(self) -> None:
        for mutation in ("scope", "record_scope", "keys", "duplicate"):
            with self.subTest(mutation=mutation):
                response = self.runtime_response()
                if mutation == "scope":
                    response["context"] = "another"
                elif mutation == "record_scope":
                    response["settings"][0]["context"] = "another"
                elif mutation == "keys":
                    response["settings"][0]["keys"].append("UNACCOUNTED_KEY")
                else:
                    response["settings"].append(response["settings"][0])
                with self.assertRaises(ValueError):
                    self.inspect(response)

    def test_empty_visibility_does_not_prove_absence(self) -> None:
        response = self.runtime_response()
        response["settings"] = []
        self.assertFalse(self.inspect(response)["absence_proven"])

    def test_policy_projection_omits_other_principals_and_fields(self) -> None:
        self.transport["request_launchplane_read"].return_value = {
            "status": "ok",
            "trace_id": "launchplane_req_fixture",
            "policy": {
                "record_id": "policy-fixture",
                "revision": 1,
                "policy_sha256": "a" * 64,
                "unmanaged_rule_counts": {"github_actions": 1},
                "managed_rules": [
                    {
                        "principal_type": "github_actions",
                        "managed_set_id": "example.preview",
                        "managed_rule_id": "read",
                        "rule_sha256": "b" * 64,
                        "subjects": "PRIVATE",
                    },
                    {"principal_type": "local_operators", "subjects": "PRIVATE"},
                ],
            },
        }
        result = CLIENT["inspect_policy"](
            self.transport, self.settings, argparse.Namespace(workflow=[])
        )
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertFalse(result["workflow_scope_details_available"])
        self.transport["request_launchplane"].assert_not_called()

    def test_invalid_identifiers_fail_before_url_formatting(self) -> None:
        for value in (".", "..", "../another", "example?apply=true", "example/testing"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                CLIENT["identifier"](value)

    def test_metadata_gate_runs_on_success_and_refuses_unsafe_metadata(self) -> None:
        def public_identifier(value: str) -> str:
            if "UNSAFE" in value:
                raise ValueError("Unsafe identifier fixture")
            return value

        transport = {
            **self.transport,
            "resolve_settings": lambda args: self.settings,
            "validate_service_url": lambda value: None,
            "is_denied_key": lambda key: key in {"token", "values"},
            "public_identifier": public_identifier,
        }
        with TemporaryDirectory() as directory:
            helper = Path(directory) / "launchplane-write-action.py"
            helper.write_text("")
            for key, expected in (("odoo.db-filter", "ok"), ("UNSAFE", "unverified")):
                response = self.runtime_response()
                response["settings"][0].update(keys=[key], values={key: "PRIVATE"})
                self.transport["request_launchplane_read"].return_value = response
                output = io.StringIO()
                with (
                    patch(
                        "sys.argv",
                        [
                            str(CLIENT_PATH),
                            "--operator-helper",
                            str(helper),
                            "settings",
                            "--context",
                            "example",
                            "--instance",
                            "testing",
                        ],
                    ),
                    patch.object(runpy, "run_path", return_value=transport),
                    redirect_stdout(output),
                ):
                    code = CLIENT["main"]()
                result = json.loads(output.getvalue())
                self.assertEqual(result["status"], expected)
                self.assertEqual(code == 0, expected == "ok")
                self.assertNotIn("PRIVATE", output.getvalue())
                if expected == "ok":
                    self.assertIn(key, output.getvalue())
                else:
                    self.assertNotIn("UNSAFE", output.getvalue())

    def test_workflow_evidence_binds_policy_and_rejects_partial_or_changed_result(self) -> None:
        policy: dict[str, Any] = {
            "record_id": "policy-fixture",
            "revision": 1,
            "policy_sha256": "a" * 64,
            "updated_at": "2026-01-01T00:00:00Z",
            "schema_version": 2,
        }
        self.transport["request_launchplane_read"].return_value = {
            "status": "ok",
            "trace_id": "launchplane_req_fixture",
            "policy": {**policy, "managed_rules": [], "unmanaged_rule_counts": {}},
        }
        arguments = argparse.Namespace(
            repository="example/site",
            workflow=["example/site/.github/workflows/build.yml"],
            binding="job_workflow_ref",
        )
        for mutation in (None, "policy", "indices"):
            evidence: dict[str, Any] = {
                "status": "ok",
                "trace_id": "launchplane_req_evidence",
                "policy": dict(policy),
                "history": {
                    "returned_count": 1,
                    "state": "partial",
                    "reason_codes": ["history_truncated"],
                },
                "managed_sets": [],
                "unmanaged_workflows": [
                    {
                        "index": 0,
                        "state": "no_matching_rule",
                        "rule_sha256s": [],
                        "reason_codes": [],
                    }
                ],
            }
            if mutation == "policy":
                evidence["policy"]["revision"] += 1
            elif mutation == "indices":
                evidence["unmanaged_workflows"] = []
            self.transport["request_launchplane"].return_value = evidence
            with self.subTest(mutation=mutation):
                if mutation is None:
                    result = CLIENT["inspect_policy"](self.transport, self.settings, arguments)
                    self.assertEqual(result["workflow_evidence"]["history"]["state"], "partial")
                    body = self.transport["request_launchplane"].call_args.kwargs["body"]
                    for key in ("record_id", "revision", "sha256"):
                        source = "policy_sha256" if key == "sha256" else key
                        target = "policy_sha256" if key == "sha256" else "policy_" + key
                        self.assertEqual(body[target], policy[source])
                    self.assertEqual(body["workflows"][0]["binding"], arguments.binding)
                else:
                    with self.assertRaises(ValueError):
                        CLIENT["inspect_policy"](self.transport, self.settings, arguments)

    def test_transport_failure_is_bounded_and_never_retried(self) -> None:
        transport = {
            **self.transport,
            "resolve_settings": lambda args: self.settings,
            "validate_service_url": lambda value: None,
        }
        transport["request_launchplane_read"].side_effect = OSError("PRIVATE")
        output = io.StringIO()
        with TemporaryDirectory() as directory:
            helper = Path(directory) / "launchplane-write-action.py"
            helper.write_text("")
            with (
                patch(
                    "sys.argv",
                    [
                        str(CLIENT_PATH),
                        "--operator-helper",
                        str(helper),
                        "settings",
                        "--context",
                        "example",
                    ],
                ),
                patch.object(runpy, "run_path", return_value=transport),
                redirect_stdout(output),
            ):
                self.assertEqual(CLIENT["main"](), 1)
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["status"], "unverified")
        self.transport["request_launchplane_read"].assert_called_once()
        self.transport["request_launchplane"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
