from __future__ import annotations

import base64
from itertools import product
from typing import get_args
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from control_plane.contracts.owner_control import ChannelBindingRecord
from control_plane.contracts.owner_control_enrollment_provenance import (
    OwnerControlChannelEnrollment,
    OwnerControlEnrollmentProvenanceConflictError,
    OwnerControlGestureSourceClaim,
    OwnerControlHostPrincipalClaim,
    OwnerControlKeyCustodyClaim,
    OwnerControlPrincipalSeparationClaim,
    build_owner_control_enrollment_provenance_record,
    derive_owner_control_provenance_tier,
    is_published_owner_control_synthetic_public_key,
)
from control_plane.contracts.owner_control_shadow_verifier import (
    build_owner_control_channel_session_record,
)


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _binding(*, signer: Ed25519PrivateKey | None = None) -> ChannelBindingRecord:
    resolved_key = signer or Ed25519PrivateKey.from_private_bytes(bytes([42]) * 32)
    return ChannelBindingRecord(
        channel_session_id="owner-control-session",
        owner_github_id=100001,
        signature_algorithm="ed25519",
        owner_public_key=_base64url(resolved_key.public_key().public_bytes_raw()),
        session_issued_at="2026-08-29T12:00:00+00:00",
        session_expires_at="2026-08-29T13:00:00+00:00",
    )


def _claim(**changes: object) -> OwnerControlHostPrincipalClaim:
    payload: dict[str, object] = {
        "host_instance_id": "owner-host-01",
        "principal_id": "owner-control-user",
        "principal_separation": "not_claimed",
        "key_custody": "not_claimed",
        "gesture_source": "not_claimed",
    }
    payload.update(changes)
    return OwnerControlHostPrincipalClaim.model_validate(payload)


class OwnerControlEnrollmentProvenanceTests(unittest.TestCase):
    def test_every_reachable_claim_combination_remains_self_asserted(self) -> None:
        binding = _binding()
        for principal_separation, key_custody, gesture_source in product(
            get_args(OwnerControlPrincipalSeparationClaim),
            get_args(OwnerControlKeyCustodyClaim),
            get_args(OwnerControlGestureSourceClaim),
        ):
            with self.subTest(
                principal_separation=principal_separation,
                key_custody=key_custody,
                gesture_source=gesture_source,
            ):
                claim = _claim(
                    principal_separation=principal_separation,
                    key_custody=key_custody,
                    gesture_source=gesture_source,
                )
                record = build_owner_control_enrollment_provenance_record(
                    binding=binding,
                    claim=claim,
                    enrolled_at="2026-08-29T12:01:00+00:00",
                )

                self.assertEqual(record.provenance_tier, "self_asserted")
                self.assertEqual(record.server_observed_corroboration, "none")
                self.assertEqual(record.authority_state, "inert")
                self.assertFalse(record.authorizes_execution)
                self.assertEqual(record.channel_binding(), binding)
                self.assertEqual(record.host_principal_claim(), claim)
                self.assertEqual(
                    derive_owner_control_provenance_tier(
                        claim=claim,
                        server_observed_corroboration="none",
                    ),
                    "self_asserted",
                )

    def test_claim_rejects_unknown_fields_versions_and_values(self) -> None:
        valid = _claim().model_dump(mode="json")
        for payload in (
            {**valid, "unexpected": True},
            {**valid, "schema_version": 2},
            {**valid, "principal_separation": "trusted"},
            {**valid, "key_custody": "secure_enclave"},
            {**valid, "gesture_source": "remote_agent"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    OwnerControlHostPrincipalClaim.model_validate(payload)

    def test_record_rejects_noncanonical_claim_and_digest_drift(self) -> None:
        record = build_owner_control_enrollment_provenance_record(
            binding=_binding(),
            claim=_claim(),
            enrolled_at="2026-08-29T12:01:00+00:00",
        )
        payload = record.model_dump(mode="json")
        for changed in (
            {**payload, "host_principal_claim_json": '{"principal_id":"changed"}'},
            {**payload, "host_principal_claim_sha256": "0" * 64},
            {**payload, "server_observed_corroboration": "hardware_attested"},
            {**payload, "provenance_tier": "trusted"},
            {**payload, "authorizes_execution": True},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(ValidationError):
                    record.__class__.model_validate(changed)

    def test_atomic_pair_rejects_session_provenance_drift(self) -> None:
        binding = _binding()
        enrolled_at = "2026-08-29T12:01:00+00:00"
        session = build_owner_control_channel_session_record(
            binding=binding,
            enrolled_at=enrolled_at,
        )
        provenance = build_owner_control_enrollment_provenance_record(
            binding=binding,
            claim=_claim(),
            enrolled_at=enrolled_at,
        )

        enrollment = OwnerControlChannelEnrollment(session=session, provenance=provenance)

        self.assertEqual(enrollment.session, session)
        self.assertEqual(enrollment.provenance, provenance)
        with self.assertRaises(ValidationError):
            OwnerControlChannelEnrollment(
                session=session.model_copy(update={"enrolled_at": "2026-08-29T12:02:00+00:00"}),
                provenance=provenance,
            )

    def test_published_synthetic_conformance_keys_are_rejected(self) -> None:
        for seed in (bytes(range(32)), bytes(range(31, -1, -1))):
            with self.subTest(seed=seed):
                binding = _binding(signer=Ed25519PrivateKey.from_private_bytes(seed))
                self.assertTrue(
                    is_published_owner_control_synthetic_public_key(binding.owner_public_key)
                )
                with self.assertRaisesRegex(
                    OwnerControlEnrollmentProvenanceConflictError,
                    "conformance keys cannot be enrolled",
                ):
                    build_owner_control_enrollment_provenance_record(
                        binding=binding,
                        claim=_claim(),
                        enrolled_at="2026-08-29T12:01:00+00:00",
                    )
        zero_key_binding = _binding().model_copy(update={"owner_public_key": _base64url(bytes(32))})
        self.assertTrue(
            is_published_owner_control_synthetic_public_key(zero_key_binding.owner_public_key)
        )
        with self.assertRaises(OwnerControlEnrollmentProvenanceConflictError):
            build_owner_control_enrollment_provenance_record(
                binding=zero_key_binding,
                claim=_claim(),
                enrolled_at="2026-08-29T12:01:00+00:00",
            )


if __name__ == "__main__":
    unittest.main()
