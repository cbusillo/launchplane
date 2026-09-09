from __future__ import annotations

import json
import unittest
from dataclasses import replace

from control_plane.contracts.ordinary_agent_lifecycle import (
    OrdinaryAgentDeliveryBinding,
    OrdinaryAgentEnrollApplyEnvelope,
)
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.ordinary_agent_authentication import (
    OrdinaryAgentClaimSecret,
    OrdinaryAgentIssuanceBundle,
    OrdinaryAgentToken,
    decrypt_ordinary_agent_issuance,
    generate_receiver_claim_secret,
    issue_ordinary_agent_credential,
    parse_ordinary_agent_token,
    receiver_claim_sha256,
    validate_ordinary_agent_issuance,
    verify_receiver_claim,
)


class _TestCipher:
    def __init__(self) -> None:
        self.plaintexts: dict[str, str] = {}
        self.decrypt_calls = 0

    def encrypt(self, plaintext: str) -> tuple[str, str]:
        ciphertext = f"ciphertext-{len(self.plaintexts) + 1}"
        self.plaintexts[ciphertext] = plaintext
        return ciphertext, "test-key"

    def decrypt(self, ciphertext: str, key_id: str) -> str:
        self.decrypt_calls += 1
        if key_id != "test-key":
            raise ValueError("unknown key")
        return self.plaintexts[ciphertext]


class OrdinaryAgentAuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cipher = _TestCipher()
        self.claim = generate_receiver_claim_secret(random_bytes=lambda size: b"c" * size)
        self.claim_digest = receiver_claim_sha256(self.claim)
        self.intent_digest = canonical_json_sha256({"intent": "test issuance"})

    def issue(self, *, random_byte: bytes = b"t") -> OrdinaryAgentIssuanceBundle:
        return issue_ordinary_agent_credential(
            principal_id="agent_one",
            credential_id="agent_credential",
            credential_version=1,
            valid_from=100,
            expires_at=1_000,
            operation_id="ordinary-agent-enroll-1",
            receiver_claim_sha256=self.claim_digest,
            delivery_expires_at=200,
            intent_sha256=self.intent_digest,
            random_bytes=lambda size: random_byte * size,
            encrypt=self.cipher.encrypt,
        )

    def decrypt_bundle(
        self,
        bundle: OrdinaryAgentIssuanceBundle,
        *,
        ciphertext: str | None = None,
        operation_id: str | None = None,
    ) -> OrdinaryAgentToken:
        return decrypt_ordinary_agent_issuance(
            ciphertext=bundle.ciphertext if ciphertext is None else ciphertext,
            key_id=bundle.key_id,
            expected_candidate=bundle.candidate,
            expected_credential_version=bundle.credential_version,
            expected_operation_id=bundle.operation_id if operation_id is None else operation_id,
            expected_receiver_claim_sha256=bundle.receiver_claim_sha256,
            expected_delivery_expires_at=bundle.delivery_expires_at,
            expected_intent_sha256=bundle.intent_sha256,
            expected_ciphertext_sha256=bundle.ciphertext_sha256,
            decrypt=self.cipher.decrypt,
        )

    def test_issued_bundle_round_trips_with_redacted_private_material(self) -> None:
        bundle = self.issue()

        proof = parse_ordinary_agent_token(bundle.token.value)
        self.assertEqual(proof.credential_id, bundle.candidate.credential_id)
        self.assertEqual(proof.credential_version, bundle.credential_version)
        self.assertEqual(proof.credential_digest, bundle.candidate.credential_digest)
        restored = self.decrypt_bundle(bundle)
        self.assertEqual(restored.value, bundle.token.value)
        for rendered in (repr(bundle), repr(bundle.token), repr(proof), repr(self.claim)):
            self.assertNotIn(bundle.token.value, rendered)
            self.assertNotIn(self.claim.value, rendered)

    def test_fresh_issuance_randomness_changes_secret_but_not_reviewed_bindings(self) -> None:
        first = self.issue(random_byte=b"a")
        second = self.issue(random_byte=b"b")

        self.assertNotEqual(first.token.value, second.token.value)
        self.assertNotEqual(first.candidate.credential_digest, second.candidate.credential_digest)
        self.assertEqual(first.intent_sha256, second.intent_sha256)
        self.assertEqual(first.receiver_claim_sha256, second.receiver_claim_sha256)
        self.assertEqual(first.operation_id, second.operation_id)

    def test_validate_rejects_private_bundle_drift_from_reviewed_envelope(self) -> None:
        bundle = self.issue()
        envelope = OrdinaryAgentEnrollApplyEnvelope.model_construct(
            action="enroll",
            operation_id=bundle.operation_id,
            principal_id=bundle.candidate.principal_id,
            authentication_credential=bundle.candidate,
            delivery=OrdinaryAgentDeliveryBinding(
                receiver_claim_sha256=bundle.receiver_claim_sha256,
                expires_at=bundle.delivery_expires_at,
            ),
        )
        validate_ordinary_agent_issuance(bundle, envelope, intent_sha256=bundle.intent_sha256)

        changed_delivery = replace(bundle, delivery_expires_at=bundle.delivery_expires_at + 1)
        with self.assertRaisesRegex(ValueError, "reviewed envelope"):
            validate_ordinary_agent_issuance(
                changed_delivery, envelope, intent_sha256=bundle.intent_sha256
            )
        changed_principal = envelope.model_copy(update={"principal_id": "agent_other"})
        with self.assertRaisesRegex(ValueError, "principal"):
            validate_ordinary_agent_issuance(
                bundle, changed_principal, intent_sha256=bundle.intent_sha256
            )
        changed_token = replace(bundle, token=type(bundle.token)(bundle.token.value[:-1] + "A"))
        with self.assertRaisesRegex(ValueError, "token does not match"):
            validate_ordinary_agent_issuance(
                changed_token, envelope, intent_sha256=bundle.intent_sha256
            )

    def test_decrypt_rejects_ciphertext_or_context_drift(self) -> None:
        bundle = self.issue()
        with self.assertRaisesRegex(ValueError, "ciphertext digest"):
            self.decrypt_bundle(bundle, ciphertext="changed")
        self.assertEqual(self.cipher.decrypt_calls, 0)

        with self.assertRaisesRegex(ValueError, "committed bindings"):
            self.decrypt_bundle(bundle, operation_id="ordinary-agent-enroll-other")

        capsule = json.loads(self.cipher.plaintexts[bundle.ciphertext])
        capsule["token"] = "not-a-token"
        self.cipher.plaintexts[bundle.ciphertext] = json.dumps(capsule)
        with self.assertRaisesRegex(ValueError, "malformed"):
            self.decrypt_bundle(bundle)

    def test_claim_proof_is_distinct_and_wrong_proof_fails_closed(self) -> None:
        same_material = OrdinaryAgentClaimSecret(self.claim.value)
        self.assertTrue(verify_receiver_claim(same_material, self.claim_digest))
        self.assertFalse(verify_receiver_claim("wrong", self.claim_digest))
        self.assertFalse(verify_receiver_claim("wrong", "0" * 64))
        issued = self.issue()
        self.assertNotEqual(self.claim_digest, issued.candidate.credential_digest)

    def test_parser_rejects_ambiguous_or_oversized_tokens(self) -> None:
        token = self.issue().token.value
        parts = token.split(".")
        malformed = (
            ".".join((parts[0], parts[1], "01", parts[3])),
            ".".join((parts[0], parts[1], "9" * 20, parts[3])),
            ".".join((parts[0], parts[1], parts[2], parts[3] + "=")),
            ".".join((parts[0], parts[1], parts[2], parts[3][:-1] + "B")),
            token + ("x" * 512),
        )
        for value in malformed:
            with self.subTest(value_length=len(value)):
                with self.assertRaisesRegex(ValueError, "malformed"):
                    parse_ordinary_agent_token(value)

    def test_delivery_must_expire_within_credential_lifetime(self) -> None:
        with self.assertRaisesRegex(ValueError, "delivery expiry"):
            issue_ordinary_agent_credential(
                principal_id="agent_one",
                credential_id="agent_credential",
                credential_version=1,
                valid_from=100,
                expires_at=1_000,
                operation_id="ordinary-agent-enroll-1",
                receiver_claim_sha256=self.claim_digest,
                delivery_expires_at=1_001,
                intent_sha256=self.intent_digest,
                random_bytes=lambda size: b"z" * size,
                encrypt=self.cipher.encrypt,
            )


if __name__ == "__main__":
    unittest.main()
