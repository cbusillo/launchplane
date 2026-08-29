from __future__ import annotations

import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, get_args
import unittest

from pydantic import BaseModel, ValidationError

from control_plane.contracts.canonical_json import canonical_json_bytes, canonical_json_sha256
from control_plane.contracts.owner_control import (
    ApprovalRequest,
    ChannelBindingRecord,
    ChallengeResponse,
    OwnerControlConfirmationEnvelope,
    ReviewItem,
    ServerReviewPayload,
    owner_control_approval_request_digest,
    owner_control_channel_binding_sha256,
    owner_control_challenge_response_digest,
    owner_control_signature_payload_bytes,
    verify_owner_control_confirmation_signature,
)
from control_plane.contracts.owner_control_shadow_verifier import (
    OwnerControlChallengeLifecycleEventRecord,
    OwnerControlChannelSessionRecord,
    OwnerControlIssuedChallengeRecord,
    OwnerControlShadowVerificationReason,
    evaluate_owner_control_shadow_verification,
    terminalize_expired_owner_control_challenge_record,
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
        binding_schema = artifact["schemas"]["channel_binding_record"]
        envelope_schema = artifact["schemas"]["owner_control_confirmation_envelope"]
        signature_payload_schema = artifact["schemas"]["owner_control_signature_payload"]

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
        self.assertEqual(binding_schema["properties"]["signature_algorithm"]["const"], "ed25519")
        self.assertEqual(
            signature_payload_schema["properties"]["domain"]["const"],
            "launchplane-owner-control-confirmation-v1",
        )
        self.assertEqual(envelope_schema["properties"]["signature_algorithm"]["const"], "ed25519")

    def test_confirmation_verifies_only_for_the_exact_signed_payload(self) -> None:
        vector = build_owner_control_contract()["confirmation_golden_vectors"][0]
        binding = ChannelBindingRecord.model_validate_json(
            json.dumps(vector["channel_binding"]["payload"])
        )
        response = ChallengeResponse.model_validate_json(
            json.dumps(vector["challenge_response"]["payload"])
        )
        envelope = OwnerControlConfirmationEnvelope.model_validate_json(
            json.dumps(vector["confirmation_envelope"]["payload"])
        )

        self.assertEqual(
            vector["channel_binding"]["canonical_json"],
            canonical_json_bytes(binding.model_dump(mode="json")).decode(),
        )
        self.assertEqual(
            vector["channel_binding"]["sha256"],
            owner_control_channel_binding_sha256(binding),
        )
        self.assertEqual(
            vector["signature_payload"]["canonical_json"],
            owner_control_signature_payload_bytes(response).decode(),
        )
        self.assertTrue(verify_owner_control_confirmation_signature(envelope))

        tampered = envelope.model_copy(
            update={
                "challenge_response": response.model_copy(
                    update={"confirmed_at": "2030-01-02T03:05:05+00:00"}
                )
            }
        )
        self.assertFalse(verify_owner_control_confirmation_signature(tampered))


class OwnerControlArtifactTests(unittest.TestCase):
    def _assert_named_validation_error(
        self,
        error: ValidationError,
        vector: dict[str, Any],
    ) -> None:
        errors = error.errors()
        self.assertEqual(len(errors), 1)
        self.assertEqual(list(errors[0]["loc"]), vector["error_location"])
        if expected_message := vector.get("error_message_contains"):
            self.assertIn(expected_message, errors[0]["msg"])

    def test_v3_contract_preserves_every_v2_section(self) -> None:
        artifact = build_owner_control_contract()
        compatibility = artifact["compatibility"]

        self.assertEqual(artifact["schema_version"], 3)
        self.assertEqual(compatibility["container_schema_version"], 3)
        self.assertEqual(compatibility["previous_container_schema_version"], 2)
        self.assertEqual(compatibility["unknown_container_versions"], "reject")
        self.assertEqual(compatibility["wire_model_schema_versions"], [1])
        self.assertEqual(compatibility["shadow_verifier_schema_versions"], [1])
        self.assertEqual(artifact["signature_declaration"]["contract_schema_version"], 2)
        for section, expected_sha256 in compatibility["preserved_v2_section_sha256"].items():
            with self.subTest(section=section):
                self.assertEqual(canonical_json_sha256(artifact[section]), expected_sha256)

    def test_existing_approval_and_challenge_vectors_remain_byte_compatible(self) -> None:
        vector = next(
            vector
            for vector in build_owner_control_contract()["golden_vectors"]
            if vector["descriptor_id"] == "managed-secret-reencryption"
        )

        self.assertEqual(
            vector["approval_request"]["canonical_json"],
            '{"descriptor_id":"managed-secret-reencryption","descriptor_version":1,"evidence_digest":"9823b32b0823bd4004bacdbe08391b3e0120e8f24c36f91474115ef9fa01155d","expires_at":"2030-01-02T03:09:05+00:00","issued_at":"2030-01-02T03:00:05+00:00","nonce":"owner-control-1bb1a85cd3cf848e5c7a0ee17410addb","operation_id":"privileged-operation-9a9d1838b14102ef23bc2528687abda0","owner_github_id":2594086616,"plan_digest":"f87d15dd32b95865b4fe98cc67c5c418757a0559488488355bb0cf52a06cdf95","policy_record_id":"owner-policy-9a9d1838b14102ef23bc","policy_revision":1,"policy_sha256":"158ffbbe76bc73789a3e644e339411d8569ed880328b1e7728e7db3a25f1be8f","pre_state_digest":"96cb3e8e7bc9a596d31b699bd6ea1681651eda477f9b7a6c482f9378e6798d58","request_digest":"4a9624ebacf973b34eeb4762279d052e3c637d8090cb79ecbb217fa320a58d68","schema_version":1,"server_review":{"items":[{"key":"descriptor","label":"Operation class","value":"managed-secret-reencryption"},{"key":"safety_class","label":"Safety class","value":"secret_backed"}],"review_id":"review-9a9d1838b14102ef23bc2528","schema_version":1,"summary":"Review this exact privileged operation before confirming.","title":"Owner approval required"}}',
        )
        self.assertEqual(
            vector["approval_request"]["sha256"],
            "a158dabb09e35c75932985b8682c34f7f598a2b1404ac80a9e4fa0d4a19313c4",
        )
        self.assertEqual(
            vector["challenge_response"]["canonical_json"],
            '{"approval_request":{"descriptor_id":"managed-secret-reencryption","descriptor_version":1,"evidence_digest":"9823b32b0823bd4004bacdbe08391b3e0120e8f24c36f91474115ef9fa01155d","expires_at":"2030-01-02T03:09:05+00:00","issued_at":"2030-01-02T03:00:05+00:00","nonce":"owner-control-1bb1a85cd3cf848e5c7a0ee17410addb","operation_id":"privileged-operation-9a9d1838b14102ef23bc2528687abda0","owner_github_id":2594086616,"plan_digest":"f87d15dd32b95865b4fe98cc67c5c418757a0559488488355bb0cf52a06cdf95","policy_record_id":"owner-policy-9a9d1838b14102ef23bc","policy_revision":1,"policy_sha256":"158ffbbe76bc73789a3e644e339411d8569ed880328b1e7728e7db3a25f1be8f","pre_state_digest":"96cb3e8e7bc9a596d31b699bd6ea1681651eda477f9b7a6c482f9378e6798d58","request_digest":"4a9624ebacf973b34eeb4762279d052e3c637d8090cb79ecbb217fa320a58d68","schema_version":1,"server_review":{"items":[{"key":"descriptor","label":"Operation class","value":"managed-secret-reencryption"},{"key":"safety_class","label":"Safety class","value":"secret_backed"}],"review_id":"review-9a9d1838b14102ef23bc2528","schema_version":1,"summary":"Review this exact privileged operation before confirming.","title":"Owner approval required"}},"approval_request_digest":"a158dabb09e35c75932985b8682c34f7f598a2b1404ac80a9e4fa0d4a19313c4","channel_binding_sha256":"4a000cc8036cc97f077bc431ad78ce27f5a940d5766377ef2de3d964229b8a9c","confirmed_at":"2030-01-02T03:04:05+00:00","decision":"approved","schema_version":1}',
        )
        self.assertEqual(
            vector["challenge_response"]["sha256"],
            "76b409c883d422aef5378d03dfe1527c16f2f1ac41b8b81202a71c2375641a28",
        )

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
                canonical_json_bytes(request.model_dump(mode="json")).decode(),
            )
            self.assertEqual(
                vector["approval_request"]["sha256"],
                owner_control_approval_request_digest(request),
            )
            self.assertEqual(
                vector["challenge_response"]["canonical_json"],
                canonical_json_bytes(response.model_dump(mode="json")).decode(),
            )
            self.assertEqual(
                vector["challenge_response"]["sha256"],
                owner_control_challenge_response_digest(response),
            )

    def test_confirmation_vectors_cover_every_descriptor_and_verify(self) -> None:
        artifact = build_owner_control_contract()
        expected_descriptors = {
            (descriptor.descriptor_id, descriptor.descriptor_version)
            for descriptor in list_privileged_operation_descriptors()
        }
        actual_descriptors = {
            (vector["descriptor_id"], vector["descriptor_version"])
            for vector in artifact["confirmation_golden_vectors"]
        }

        self.assertEqual(actual_descriptors, expected_descriptors)
        for vector in artifact["confirmation_golden_vectors"]:
            with self.subTest(descriptor_id=vector["descriptor_id"]):
                binding = ChannelBindingRecord.model_validate_json(
                    json.dumps(vector["channel_binding"]["payload"])
                )
                response = ChallengeResponse.model_validate_json(
                    json.dumps(vector["challenge_response"]["payload"])
                )
                envelope = OwnerControlConfirmationEnvelope.model_validate_json(
                    json.dumps(vector["confirmation_envelope"]["payload"])
                )
                self.assertEqual(
                    vector["channel_binding"]["sha256"],
                    owner_control_channel_binding_sha256(binding),
                )
                self.assertEqual(
                    vector["signature_payload"]["canonical_json"],
                    owner_control_signature_payload_bytes(response).decode(),
                )
                self.assertTrue(verify_owner_control_confirmation_signature(envelope))

    def test_verification_state_vectors_cover_and_replay_every_outcome(self) -> None:
        vectors = build_owner_control_contract()["verification_state_vectors"]
        expected_reasons = set(get_args(OwnerControlShadowVerificationReason))
        actual_reasons = {
            vector["expected"]["rejection_reason"]
            for vector in vectors
            if vector["expected"]["rejection_reason"] is not None
        }

        self.assertEqual(actual_reasons, expected_reasons)
        self.assertTrue(
            any(vector["expected"]["verification_status"] == "verified" for vector in vectors)
        )
        for vector in vectors:
            with self.subTest(name=vector["name"]):
                session_payload = vector["channel_session"]
                challenge_payload = vector["issued_challenge"]
                evaluation = evaluate_owner_control_shadow_verification(
                    envelope=OwnerControlConfirmationEnvelope.model_validate_json(
                        json.dumps(vector["confirmation_envelope"])
                    ),
                    channel_session=(
                        OwnerControlChannelSessionRecord.model_validate_json(
                            json.dumps(session_payload)
                        )
                        if session_payload is not None
                        else None
                    ),
                    issued_challenge=(
                        OwnerControlIssuedChallengeRecord.model_validate_json(
                            json.dumps(challenge_payload)
                        )
                        if challenge_payload is not None
                        else None
                    ),
                    observed_at=vector["observed_at"],
                )
                expected = vector["expected"]
                self.assertEqual(
                    evaluation.model_dump(mode="json"),
                    {
                        "verification_status": expected["verification_status"],
                        "rejection_reason": expected["rejection_reason"],
                        "consume_challenge": expected["consume_challenge"],
                        "resulting_challenge_state": expected["resulting_challenge_state"],
                    },
                )
                self.assertEqual(expected["verifier_mode"], "shadow")
                self.assertFalse(expected["authorizes_execution"])
                self.assertEqual(expected["authority_state"], "inert")

    def test_lifecycle_vector_replays_exact_boundary_without_attempt_consumption(self) -> None:
        vectors = build_owner_control_contract()["challenge_lifecycle_vectors"]

        self.assertEqual(len(vectors), 1)
        vector = vectors[0]
        issued = OwnerControlIssuedChallengeRecord.model_validate_json(
            json.dumps(vector["issued_challenge"])
        )
        terminalized, event = terminalize_expired_owner_control_challenge_record(
            issued,
            observed_at=vector["observed_at"],
        )

        self.assertEqual(
            terminalized.model_dump(mode="json"),
            vector["expected_terminalized_challenge"],
        )
        self.assertEqual(
            event,
            OwnerControlChallengeLifecycleEventRecord.model_validate_json(
                json.dumps(vector["expected_lifecycle_event"])
            ),
        )
        self.assertEqual(terminalized.attempt_count, issued.attempt_count)
        self.assertEqual(event.occurred_at, issued.expires_at)
        self.assertFalse(event.authorizes_execution)
        self.assertNotIn("envelope_sha256", vector["expected_lifecycle_event"])
        self.assertNotIn("sequence", vector["expected_lifecycle_event"])

    def test_server_state_vectors_are_public_safe(self) -> None:
        artifact = build_owner_control_contract()
        serialized = json.dumps(
            {
                "verification_state_vectors": artifact["verification_state_vectors"],
                "challenge_lifecycle_vectors": artifact["challenge_lifecycle_vectors"],
            },
            sort_keys=True,
        ).lower()

        for forbidden_value in (
            *(scheme + "://" for scheme in ("http", "https")),
            "private_key",
            "access_token",
            "credential",
            "repository",
            "tenant",
            "endpoint",
        ):
            with self.subTest(forbidden_value=forbidden_value):
                self.assertNotIn(forbidden_value, serialized)

    def test_validator_rejects_contract_drift(self) -> None:
        artifact = build_owner_control_contract()
        changed = copy.deepcopy(artifact)
        changed["golden_vectors"][0]["approval_request"]["sha256"] = "0" * 64

        with self.assertRaises(OwnerControlContractError):
            validate_owner_control_contract(changed)

        changed = copy.deepcopy(artifact)
        changed["verification_state_vectors"][0]["expected"]["authorizes_execution"] = True

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
                self._assert_named_validation_error(raised.exception, vector)

    def test_negative_confirmation_vectors_fail_closed(self) -> None:
        artifact = build_owner_control_contract()
        for vector in artifact["negative_confirmation_vectors"]:
            with self.subTest(rule=vector["rule"]):
                if vector.get("verification") == "invalid":
                    envelope = OwnerControlConfirmationEnvelope.model_validate_json(
                        json.dumps(vector["payload"])
                    )
                    self.assertFalse(verify_owner_control_confirmation_signature(envelope))
                    continue
                with self.assertRaises(ValidationError) as raised:
                    OwnerControlConfirmationEnvelope.model_validate_json(
                        json.dumps(vector["payload"])
                    )
                self._assert_named_validation_error(raised.exception, vector)

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
