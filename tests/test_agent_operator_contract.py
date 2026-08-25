from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import chdir
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from control_plane import agent_operator_contract as contract_module
from control_plane.agent_operator_contract import (
    INVARIANTS,
    OPERATION_SPECS,
    PROTECTED_WORKFLOWS,
    AgentOperatorContractError,
    build_agent_operator_contract,
    validate_agent_operator_contract,
    write_agent_operator_contract,
)
from control_plane.openapi_export import canonical_openapi_document


class AgentOperatorContractTests(unittest.TestCase):
    def test_exact_operations_dependencies_and_workflows(self) -> None:
        artifact = build_agent_operator_contract(source_commit_sha="a" * 40)
        operations = artifact["contract"]["operations"]

        self.assertEqual(
            [(operation["method"], operation["path"]) for operation in operations],
            [(spec.method, spec.path) for spec in OPERATION_SPECS],
        )
        expected_operation_ids = {
            ("GET", "/v1/agent/context"): "read_agent_context",
            ("POST", "/v1/agent/write-intents/evaluate"): "evaluate_agent_write_intent",
            (
                "POST",
                "/v1/authorization-recovery/public/prepare",
            ): "prepare_authorization_recovery",
            (
                "GET",
                "/v1/authorization-recovery/public/challenges/{challenge_id}",
            ): "read_authorization_recovery_challenge",
            ("POST", "/v1/product-config/apply"): "apply_product_config",
            ("POST", "/v1/change-impact/policies/apply"): "apply_change_impact_policy",
            ("GET", "/v1/change-impact/policy"): "read_change_impact_policy",
            (
                "POST",
                "/v1/work-graph/merge-train/controller/run-once",
            ): "write_merge_train_controller_run_once",
            ("POST", "/v1/previews/pr-feedback/remediation"): "remediate_preview_pr_feedback",
            ("POST", "/v1/product-retirement"): "execute_product_retirement",
            ("POST", "/v1/detached-application-retirement"): (
                "execute_detached_application_retirement"
            ),
            ("POST", "/v1/authz-policies/managed-rule-sets/reconcile"): (
                "reconcile_managed_authz_policy"
            ),
            ("POST", "/v1/product-profiles/stable-lane-repair/apply"): (
                "apply_product_stable_lane_repair"
            ),
            ("GET", "/v1/governance/projection"): "read_governance_projection",
        }
        expected_dependencies = {
            "read_agent_context": ["read_identity"],
            "evaluate_agent_write_intent": ["read_browser_mutation_identity"],
            "prepare_authorization_recovery": ["reject_authorization_recovery_credentials"],
            "read_authorization_recovery_challenge": [
                "reject_authorization_recovery_credentials"
            ],
            "apply_product_config": ["read_browser_mutation_identity"],
            "apply_change_impact_policy": ["read_write_identity"],
            "read_change_impact_policy": ["read_identity"],
            "write_merge_train_controller_run_once": ["read_write_identity"],
            "remediate_preview_pr_feedback": ["read_write_identity"],
            "execute_product_retirement": ["read_write_identity"],
            "execute_detached_application_retirement": ["read_write_identity"],
            "reconcile_managed_authz_policy": ["read_browser_mutation_identity"],
            "apply_product_stable_lane_repair": ["read_write_identity"],
            "read_governance_projection": ["read_identity"],
        }
        for operation in operations:
            key = (operation["method"], operation["path"])
            self.assertEqual(operation["operation_id"], expected_operation_ids[key])
            self.assertEqual(
                operation["identity_dependencies"],
                expected_dependencies[operation["operation_id"]],
            )
            self.assertRegex(operation["schema_fingerprint_sha256"], r"^[0-9a-f]{64}$")

        change_impact = next(
            operation
            for operation in operations
            if operation["path"] == "/v1/change-impact/policies/apply"
        )
        self.assertEqual(change_impact["idempotency"], "none")
        self.assertEqual(
            change_impact["reviewed_evidence"],
            ["reviewed_dry_run", "expected_policy_digest"],
        )

        self.assertEqual(artifact["contract"]["protected_workflows"], list(PROTECTED_WORKFLOWS))
        self.assertEqual(artifact["contract"]["invariants"], INVARIANTS)

    def test_noise_and_provenance_do_not_change_semantic_digest(self) -> None:
        document = canonical_openapi_document()
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
        document = canonical_openapi_document()
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
        document = canonical_openapi_document()
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
        document = canonical_openapi_document()
        changed_document = copy.deepcopy(document)
        request_schema = changed_document["paths"]["/v1/product-config/apply"]["post"][
            "requestBody"
        ]["content"]["application/json"]["schema"]
        request_schema["properties"]["runtime_env"]["anyOf"][1]["default"] = {"env": {}}

        original = build_agent_operator_contract(openapi_document=document)
        changed = build_agent_operator_contract(openapi_document=changed_document)

        self.assertNotEqual(original["semantic_digest_sha256"], changed["semantic_digest_sha256"])

    def test_idempotency_metadata_must_match_live_openapi_parameters(self) -> None:
        document = canonical_openapi_document()
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
        change_impact = unexpected_header["paths"]["/v1/change-impact/policies/apply"]["post"]
        change_impact.setdefault("parameters", []).append(
            {
                "in": "header",
                "name": "Idempotency-Key",
                "required": True,
                "schema": {"type": "string"},
            }
        )
        with self.assertRaisesRegex(AgentOperatorContractError, "Idempotency metadata"):
            build_agent_operator_contract(openapi_document=unexpected_header)

    def test_contract_build_is_independent_of_current_working_directory(self) -> None:
        frontend_directory = Path("frontend").resolve()

        with chdir(frontend_directory):
            artifact = build_agent_operator_contract(source_commit_sha="a" * 40)

        self.assertEqual(len(artifact["contract"]["protected_workflows"]), 4)

    def test_structural_and_agent_owned_semantics_change_digest(self) -> None:
        document = canonical_openapi_document()
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


if __name__ == "__main__":
    unittest.main()
