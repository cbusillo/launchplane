from __future__ import annotations

import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pydantic import BaseModel, ValidationError

from control_plane.contracts.canonical_json import canonical_json_bytes, canonical_json_sha256
from control_plane.contracts.owner_control import (
    ApprovalRequest,
    ChallengeResponse,
    ReviewItem,
    ServerReviewPayload,
    owner_control_approval_request_digest,
    owner_control_challenge_response_digest,
)
from control_plane.contracts.privileged_operation import (
    ManagedSecretReencryptionPlanInput,
    privileged_operation_request_digest,
)
from control_plane.owner_control_contract import (
    OwnerControlContractError,
    build_owner_control_contract,
    validate_owner_control_contract,
    write_owner_control_contract,
)
from control_plane.privileged_operation_registry import list_privileged_operation_descriptors


def _approval_request() -> ApprovalRequest:
    review = ServerReviewPayload(
        review_id="review-generic",
        title="Owner approval required",
        summary="Review this exact privileged operation before confirming.",
        items=(
            ReviewItem(
                key="descriptor",
                label="Operation class",
                value="managed-secret-reencryption",
            ),
        ),
    )
    return ApprovalRequest(
        operation_id="privileged-operation-0123456789abcdef0123456789abcdef",
        descriptor_id="managed-secret-reencryption",
        descriptor_version=1,
        request_digest="1" * 64,
        plan_digest="2" * 64,
        evidence_digest="3" * 64,
        pre_state_digest="4" * 64,
        policy_record_id="owner-policy-generic",
        policy_revision=1,
        policy_sha256="5" * 64,
        owner_github_id=100001,
        server_review=review,
        nonce="owner-control-nonce-0000000000000001",
        issued_at="2030-01-02T03:00:05+00:00",
        expires_at="2030-01-02T03:09:05+00:00",
    )


class CanonicalJsonTests(unittest.TestCase):
    def test_canonical_json_bytes_and_sha256_are_stable(self) -> None:
        payload = {"b": 2, "a": [True, None, "μ"]}

        self.assertEqual(
            canonical_json_bytes(payload),
            b'{"a":[true,null,"\\u03bc"],"b":2}',
        )
        self.assertEqual(
            canonical_json_sha256(payload),
            "47c7f639fc01073ea65bbd5ee2207801ffa734723151617d7de493ef8c80b7c9",
        )

    def test_canonical_json_rejects_floats_non_finite_numbers_and_oversized_integers(self) -> None:
        invalid_payloads = (
            {"value": 1.0},
            {"value": float("nan")},
            {"value": float("inf")},
            {"value": 2**63},
            {"value": -(2**63) - 1},
        )

        for invalid_payload in invalid_payloads:
            with self.subTest(payload=invalid_payload):
                with self.assertRaises(ValueError):
                    canonical_json_bytes(invalid_payload)

    def test_privileged_operation_digest_bytes_remain_compatible(self) -> None:
        request = ManagedSecretReencryptionPlanInput(
            reason="Review canonical root migration",
            source_label="test-plan",
        )

        self.assertEqual(
            privileged_operation_request_digest(request),
            "4e6864697fe5f5eb851f76b93865dc28b7897c3e8a84c68b1104912dd0b7d816",
        )
        self.assertEqual(
            privileged_operation_request_digest(request),
            canonical_json_sha256(request.model_dump(mode="json")),
        )


class OwnerControlContractModelTests(unittest.TestCase):
    def test_request_and_response_digests_bind_canonical_models(self) -> None:
        request = _approval_request()
        response = ChallengeResponse(
            approval_request=request,
            approval_request_digest=owner_control_approval_request_digest(request),
            decision="approved",
            channel_binding_sha256="6" * 64,
            confirmed_at="2030-01-02T03:04:05+00:00",
        )

        self.assertEqual(
            owner_control_approval_request_digest(request),
            canonical_json_sha256(request.model_dump(mode="json")),
        )
        self.assertEqual(
            owner_control_challenge_response_digest(response),
            canonical_json_sha256(response.model_dump(mode="json")),
        )

    def test_contracts_reject_invalid_versions_digests_timestamps_and_nonces(self) -> None:
        payload = _approval_request().model_dump(mode="json")
        invalid_payloads = (
            {**payload, "schema_version": 2},
            {**payload, "request_digest": "A" * 64},
            {**payload, "expires_at": "2030-01-02T03:09:05Z"},
            {**payload, "expires_at": "2030-01-02T03:09:05.000000+00:00"},
            {**payload, "nonce": "short"},
        )

        for invalid_payload in invalid_payloads:
            with self.subTest(invalid_payload=invalid_payload):
                with self.assertRaises(ValidationError):
                    ApprovalRequest.model_validate_json(json.dumps(invalid_payload))

    def test_challenge_response_rejects_mismatched_request_digest_and_expiry(self) -> None:
        request = _approval_request()

        with self.assertRaises(ValidationError):
            ChallengeResponse(
                approval_request=request,
                approval_request_digest="0" * 64,
                decision="approved",
                channel_binding_sha256="6" * 64,
                confirmed_at="2030-01-02T03:04:05+00:00",
            )
        with self.assertRaises(ValidationError):
            ChallengeResponse(
                approval_request=request,
                approval_request_digest=owner_control_approval_request_digest(request),
                decision="approved",
                channel_binding_sha256="6" * 64,
                confirmed_at="2030-01-02T03:10:05+00:00",
            )
        with self.assertRaises(ValidationError):
            ChallengeResponse(
                approval_request=request,
                approval_request_digest=owner_control_approval_request_digest(request),
                decision="approved",
                channel_binding_sha256="6" * 64,
                confirmed_at="2030-01-02T02:59:05+00:00",
            )

    def test_exported_schema_exposes_single_field_constraints(self) -> None:
        artifact = build_owner_control_contract()
        approval_schema = artifact["schemas"]["approval_request"]
        response_schema = artifact["schemas"]["challenge_response"]

        self.assertEqual(approval_schema["properties"]["schema_version"]["const"], 1)
        self.assertEqual(approval_schema["properties"]["descriptor_version"]["const"], 1)
        self.assertEqual(
            approval_schema["properties"]["request_digest"]["pattern"],
            "^[0-9a-f]{64}$",
        )
        self.assertEqual(
            approval_schema["properties"]["nonce"]["pattern"],
            "^[A-Za-z0-9_-]{16,128}$",
        )
        self.assertIn("pattern", response_schema["properties"]["confirmed_at"])


class OwnerControlArtifactTests(unittest.TestCase):
    def test_golden_vectors_cover_every_registered_descriptor(self) -> None:
        artifact = build_owner_control_contract()
        expected_descriptors = {
            (descriptor.descriptor_id, descriptor.descriptor_version)
            for descriptor in list_privileged_operation_descriptors()
        }
        actual_descriptors = {
            (vector["descriptor_id"], vector["descriptor_version"])
            for vector in artifact["golden_vectors"]
        }

        self.assertEqual(actual_descriptors, expected_descriptors)
        for vector in artifact["golden_vectors"]:
            request = ApprovalRequest.model_validate_json(
                json.dumps(vector["approval_request"]["payload"])
            )
            response = ChallengeResponse.model_validate_json(
                json.dumps(vector["challenge_response"]["payload"])
            )
            self.assertEqual(
                vector["approval_request"]["canonical_json"],
                canonical_json_bytes(request.model_dump(mode="json")).decode("utf-8"),
            )
            self.assertEqual(
                vector["approval_request"]["sha256"],
                owner_control_approval_request_digest(request),
            )
            self.assertEqual(
                vector["challenge_response"]["canonical_json"],
                canonical_json_bytes(response.model_dump(mode="json")).decode("utf-8"),
            )
            self.assertEqual(
                vector["challenge_response"]["sha256"],
                owner_control_challenge_response_digest(response),
            )

    def test_validator_rejects_contract_drift(self) -> None:
        artifact = build_owner_control_contract()
        changed = copy.deepcopy(artifact)
        changed["golden_vectors"][0]["approval_request"]["sha256"] = "0" * 64

        with self.assertRaises(OwnerControlContractError):
            validate_owner_control_contract(changed)

    def test_negative_vectors_are_rejected_by_the_named_models(self) -> None:
        artifact = build_owner_control_contract()
        models: dict[str, type[BaseModel]] = {
            "approval_request": ApprovalRequest,
            "challenge_response": ChallengeResponse,
            "server_review_payload": ServerReviewPayload,
        }

        for vector in artifact["negative_vectors"]:
            with self.subTest(rule=vector["rule"]):
                with self.assertRaises(ValidationError) as raised:
                    models[vector["model"]].model_validate_json(json.dumps(vector["payload"]))
                errors = raised.exception.errors()
                self.assertEqual(len(errors), 1)
                self.assertEqual(list(errors[0]["loc"]), vector["error_location"])

    def test_checked_artifact_matches_generated_contract(self) -> None:
        checked = json.loads(
            Path("contracts/owner-control-contract.json").read_text(encoding="utf-8")
        )

        self.assertEqual(checked, build_owner_control_contract())

    def test_writer_is_deterministic(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "owner-control-contract.json"
            write_owner_control_contract(output_path)
            first = output_path.read_bytes()
            write_owner_control_contract(output_path)
            second = output_path.read_bytes()

        self.assertEqual(first, second)
        self.assertEqual(
            first,
            json.dumps(build_owner_control_contract(), indent=2, sort_keys=True).encode() + b"\n",
        )
