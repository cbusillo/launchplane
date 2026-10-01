from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import chdir
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

from fastapi import FastAPI

from control_plane import agent_operator_contract as contract_module
from control_plane.agent_operator_contract import (
    OPERATION_SPECS,
    AgentOperatorContractError,
    build_agent_operator_contract,
    validate_agent_operator_contract,
    write_agent_operator_contract,
)
from control_plane.openapi_export import (
    build_deterministic_export_app,
    canonical_openapi_document,
)


class AgentOperatorContractTests(unittest.TestCase):
    # Building the export app takes seconds; build it once and hand each test
    # a copy of the document so mutations stay local to that test.
    app: ClassVar[FastAPI]
    document: ClassVar[dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = build_deterministic_export_app()
        cls.document = canonical_openapi_document()

    def setUp(self) -> None:
        self.enterContext(
            patch.object(
                contract_module,
                "canonical_openapi_document",
                lambda: copy.deepcopy(self.document),
            )
        )
        self.enterContext(
            patch.object(contract_module, "build_deterministic_export_app", lambda: self.app)
        )

    def test_operations_follow_the_allow_list(self) -> None:
        artifact = build_agent_operator_contract(source_commit_sha="a" * 40)
        operations = artifact["contract"]["operations"]

        self.assertEqual(
            [(operation["method"], operation["path"]) for operation in operations],
            [(spec.method, spec.path) for spec in OPERATION_SPECS],
        )
        for operation in operations:
            self.assertRegex(operation["schema_fingerprint_sha256"], r"^[0-9a-f]{64}$")

    def test_noise_and_provenance_do_not_change_semantic_digest(self) -> None:
        document = copy.deepcopy(self.document)
        noisy_document = copy.deepcopy(document)
        noisy_document["paths"]["/v1/unrelated"] = {
            "get": {"responses": {"200": {"description": "unrelated"}}}
        }
        noisy_document["components"]["schemas"]["Unrelated"] = {
            "description": "unrelated",
            "type": "string",
        }
        noisy_document["paths"]["/v1/agent/context"]["get"]["summary"] = "changed"
        noisy_document["components"]["schemas"]["AgentContextResponse"]["description"] = (
            "changed raw description"
        )

        original = build_agent_operator_contract(
            openapi_document=document,
            source_commit_sha="a" * 40,
        )
        noisy = build_agent_operator_contract(
            openapi_document=noisy_document,
            source_commit_sha="b" * 40,
        )

        self.assertEqual(original["semantic_digest_sha256"], noisy["semantic_digest_sha256"])
        self.assertEqual(original["contract"], noisy["contract"])

    def test_unrelated_local_definitions_cannot_change_selected_fingerprints(self) -> None:
        document = copy.deepcopy(self.document)
        polluted_document = copy.deepcopy(document)
        polluted_document["components"]["schemas"]["Unrelated"] = {
            "$defs": {
                "ProductConfigRuntimeInput": {
                    "properties": {"polluted": {"type": "boolean"}},
                    "type": "object",
                }
            },
            "$ref": "#/$defs/ProductConfigRuntimeInput",
        }

        original = build_agent_operator_contract(openapi_document=document)
        polluted = build_agent_operator_contract(openapi_document=polluted_document)

        self.assertEqual(original["contract"], polluted["contract"])

    def test_nested_local_definitions_cannot_pollute_sibling_schemas(self) -> None:
        document = copy.deepcopy(self.document)
        polluted_document = copy.deepcopy(document)
        request_schema = polluted_document["paths"]["/v1/product-config/apply"]["post"][
            "requestBody"
        ]["content"]["application/json"]["schema"]
        request_schema["properties"]["runtime_env"]["anyOf"][0]["$defs"] = {
            "ProductConfigSecretInput": {"type": "integer"}
        }

        original = build_agent_operator_contract(openapi_document=document)
        polluted = build_agent_operator_contract(openapi_document=polluted_document)

        self.assertEqual(original["contract"], polluted["contract"])

    def test_reference_sibling_semantics_change_digest(self) -> None:
        document = copy.deepcopy(self.document)
        changed_document = copy.deepcopy(document)
        request_schema = changed_document["paths"]["/v1/product-config/apply"]["post"][
            "requestBody"
        ]["content"]["application/json"]["schema"]
        request_schema["properties"]["runtime_env"]["anyOf"][1]["default"] = {"env": {}}

        original = build_agent_operator_contract(openapi_document=document)
        changed = build_agent_operator_contract(openapi_document=changed_document)

        self.assertNotEqual(original["semantic_digest_sha256"], changed["semantic_digest_sha256"])

    def test_idempotency_metadata_must_match_live_openapi_parameters(self) -> None:
        document = copy.deepcopy(self.document)
        missing_header = copy.deepcopy(document)
        product_config = missing_header["paths"]["/v1/product-config/apply"]["post"]
        product_config["parameters"] = [
            parameter
            for parameter in product_config["parameters"]
            if parameter.get("name") != "Idempotency-Key"
        ]
        with self.assertRaisesRegex(AgentOperatorContractError, "Idempotency metadata"):
            build_agent_operator_contract(openapi_document=missing_header)

        unexpected_header = copy.deepcopy(document)
        context = unexpected_header["paths"]["/v1/agent/context"]["get"]
        context.setdefault("parameters", []).append(
            {
                "in": "header",
                "name": "Idempotency-Key",
                "required": True,
                "schema": {"type": "string"},
            }
        )
        with self.assertRaisesRegex(AgentOperatorContractError, "Idempotency metadata"):
            build_agent_operator_contract(openapi_document=unexpected_header)

    def test_structural_and_agent_owned_semantics_change_digest(self) -> None:
        document = copy.deepcopy(self.document)
        changed_document = copy.deepcopy(document)
        intent_schema = changed_document["components"]["schemas"]["AgentWriteIntentRequest"]
        intent_schema["properties"]["intent"]["enum"].append("new_intent")

        original = build_agent_operator_contract(
            openapi_document=document,
            source_commit_sha="a" * 40,
        )
        changed = build_agent_operator_contract(
            openapi_document=changed_document,
            source_commit_sha="a" * 40,
        )
        self.assertNotEqual(original["semantic_digest_sha256"], changed["semantic_digest_sha256"])

        changed_specs = list(OPERATION_SPECS)
        changed_specs[0] = replace(changed_specs[0], purpose="Changed agent-owned purpose.")
        with patch.object(contract_module, "OPERATION_SPECS", tuple(changed_specs)):
            changed_overlay = build_agent_operator_contract(source_commit_sha="a" * 40)
        self.assertNotEqual(
            original["semantic_digest_sha256"],
            changed_overlay["semantic_digest_sha256"],
        )

    def test_validation_is_strict_and_public_safe(self) -> None:
        artifact = build_agent_operator_contract(source_commit_sha="a" * 40)
        validate_agent_operator_contract(artifact)

        unexpected = copy.deepcopy(artifact)
        unexpected["unexpected"] = True
        with self.assertRaises(AgentOperatorContractError):
            validate_agent_operator_contract(unexpected)

        unsafe_specs = list(OPERATION_SPECS)
        unsafe_specs[0] = replace(
            unsafe_specs[0],
            purpose="Read from https://private.example.invalid.",
        )
        with patch.object(contract_module, "OPERATION_SPECS", tuple(unsafe_specs)):
            with self.assertRaisesRegex(AgentOperatorContractError, "Unsafe public"):
                build_agent_operator_contract(source_commit_sha="a" * 40)

        broken_digest = copy.deepcopy(artifact)
        broken_digest["semantic_digest_sha256"] = "0" * 64
        with self.assertRaisesRegex(AgentOperatorContractError, "Semantic digest"):
            validate_agent_operator_contract(broken_digest)

    def test_write_preserves_provenance_for_unchanged_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "agent-operator-contract.json"
            write_agent_operator_contract(output_path, source_commit_sha="a" * 40)
            write_agent_operator_contract(output_path, source_commit_sha="b" * 40)
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["provenance"]["source_commit_sha"], "a" * 40)

    def test_write_recovers_unknown_or_malformed_preserved_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "agent-operator-contract.json"
            write_agent_operator_contract(output_path, source_commit_sha="unknown")
            write_agent_operator_contract(output_path, source_commit_sha="b" * 40)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["provenance"], {"source_commit_sha": "b" * 40})

            payload["provenance"]["unexpected"] = "ignored"
            output_path.write_text(json.dumps(payload), encoding="utf-8")
            write_agent_operator_contract(output_path, source_commit_sha="c" * 40)
            recovered = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(recovered["provenance"], {"source_commit_sha": "b" * 40})

    def test_checked_artifact_matches_generated_semantics(self) -> None:
        checked_path = Path("contracts/agent-operator-contract.json")
        if not checked_path.exists():
            self.skipTest("checked agent/operator contract has not been generated")
        checked = json.loads(checked_path.read_text(encoding="utf-8"))
        generated = build_agent_operator_contract(source_commit_sha="a" * 40)

        self.assertEqual(
            checked["normalization_version"],
            generated["normalization_version"],
        )
        self.assertEqual(
            checked["semantic_digest_sha256"],
            generated["semantic_digest_sha256"],
        )


class AgentOperatorContractWorkingDirectoryTests(unittest.TestCase):
    def test_contract_build_is_independent_of_current_working_directory(self) -> None:
        expected = build_agent_operator_contract(source_commit_sha="a" * 40)

        with chdir(Path("frontend").resolve()):
            artifact = build_agent_operator_contract(source_commit_sha="a" * 40)

        self.assertEqual(artifact, expected)


if __name__ == "__main__":
    unittest.main()
