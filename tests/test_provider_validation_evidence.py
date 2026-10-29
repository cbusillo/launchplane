from dataclasses import dataclass
import json
from pathlib import Path
from traceback import walk_tb
from typing import Any
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError

from control_plane import http_error_evidence, npmplus
from tests.test_npmplus import _Opener, _credentials


@dataclass(frozen=True)
class Failure:
    evidence: dict[str, Any]
    text: str
    error: ValueError


class ProviderValidationEvidenceTests(unittest.TestCase):
    def capture_failure(self, changes: dict[str, object]) -> Failure:
        opener = _Opener()
        opener.host_details.update(changes)
        client = npmplus.NpmplusClient(credentials=_credentials(), opener=opener)
        try:
            client.get_proxy_host(78, public_only=True)
        except ValueError as error:
            with self.assertLogs(http_error_evidence.__name__, level="WARNING") as logs:
                http_error_evidence.log_handled_error_cause(error, "provider-validation-test")
            text = logs.records[-1].getMessage()
            return Failure(json.loads(text), text, error)
        self.fail("Invalid provider data was accepted")

    def test_selected_read_reports_only_model_fields_and_error_kinds(self) -> None:
        private = "private-provider-value-sentinel"
        field = "certificate_id"
        failure = self.capture_failure({field: private, "meta": {private: private}})
        validation = failure.evidence["validation"]
        original = (
            failure.error if isinstance(failure.error, ValidationError) else failure.error.__cause__
        )
        self.assertIsInstance(original, ValidationError)
        assert isinstance(original, ValidationError)
        self.assertTrue(validation)
        self.assertTrue(all(row["loc"][0] == field for row in validation))
        self.assertEqual(
            [row["type"] for row in validation],
            [row["type"] for row in original.errors()],
        )
        self.assertNotIn(private, failure.text)

    def test_root_validator_reports_its_trusted_raise_site_without_message(self) -> None:
        failure = self.capture_failure(
            {"npmplus_access_list_type": "custom", "npmplus_access_list_ids": [-1]}
        )
        original = (
            failure.error if isinstance(failure.error, ValidationError) else failure.error.__cause__
        )
        assert isinstance(original, ValidationError)
        root = original.errors(include_input=False, include_url=False)[0]["ctx"]["error"]
        frame, line = list(walk_tb(root.__traceback__))[-1]
        self.assertEqual(Path(frame.f_code.co_filename), Path(npmplus.__file__))
        validation = failure.evidence["validation"]
        self.assertEqual(validation[0]["sites"], [{"module": npmplus.__name__, "line": line}])
        self.assertNotIn(str(root), failure.text)

    def test_nested_model_fields_remain_identifiable(self) -> None:
        private = "private-location-value-sentinel"
        location = ("locations", 0, "forward_scheme")
        failure = self.capture_failure(
            {
                location[0]: [
                    {"path": "/", location[2]: private, "forward_host": "backend.example.test"}
                ]
            }
        )
        self.assertTrue(all(row["loc"] == list(location) for row in failure.evidence["validation"]))
        self.assertNotIn(private, failure.text)

    def test_distinct_response_models_remain_identifiable_for_the_same_field(self) -> None:
        private = "private-id-value-sentinel"
        host_failure = self.capture_failure({"id": private})
        opener = _Opener()
        opener.certificate_payload["id"] = private
        client = npmplus.NpmplusClient(credentials=_credentials(), opener=opener)
        with (
            patch.object(client, "_request", return_value=opener.certificate_payload),
            self.assertRaises(ValueError) as caught,
        ):
            client.get_certificate(47)
        with self.assertLogs(http_error_evidence.__name__, level="WARNING") as logs:
            http_error_evidence.log_handled_error_cause(caught.exception, "certificate-test")
        certificate = json.loads(logs.records[-1].getMessage())["validation"]
        host = host_failure.evidence["validation"]
        self.assertEqual(host[0]["loc"], certificate[0]["loc"])
        self.assertEqual(host[0]["model"], npmplus.NpmplusProxyHost.__name__)
        self.assertEqual(certificate[0]["model"], npmplus.NpmplusCertificate.__name__)
        self.assertNotIn(private, host_failure.text + logs.records[-1].getMessage())

    def test_dynamic_locations_titles_inputs_and_large_error_sets_stay_private_and_bounded(
        self,
    ) -> None:
        private = "private-provider-diagnostic-sentinel"
        location = ("meta", *(f"{private}{index}" for index in range(100)))
        errors: list[InitErrorDetails] = [
            {"type": "int_type", "loc": location, "input": private} for _ in range(200)
        ]
        errors[0]["type"] = PydanticCustomError(private, private, {private: private})
        error = ValidationError.from_exception_data(private, errors)
        with patch.object(npmplus.NpmplusProxyHost, "model_validate", side_effect=error):
            failure = self.capture_failure({})
        validation = failure.evidence["validation"]
        self.assertGreater(len(validation), 0)
        self.assertLess(len(validation), len(errors))
        self.assertTrue(all(len(row["loc"]) < len(location) for row in validation))
        self.assertTrue(all(row["loc"] == list(location[:1]) for row in validation))
        self.assertTrue(
            all(row["loc"][0] in npmplus.NpmplusProxyHost.model_fields for row in validation)
        )
        self.assertNotIn(private, failure.text)
