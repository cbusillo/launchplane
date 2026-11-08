import hashlib
import json
import math
import unittest

from control_plane.contracts.canonical_json import (
    CANONICAL_JSON_INTEGER_MIN,
    CANONICAL_JSON_INTEGER_MAX,
    canonical_json_bytes,
    canonical_json_sha256,
    nonvalidating_compact_sorted_json_sha256,
)
from control_plane.contracts.privileged_operation import (
    ManagedSecretReencryptionPlanInput,
    privileged_operation_request_digest,
)


class NonvalidatingCompactSortedJsonSha256Test(unittest.TestCase):
    def test_matches_json_defaults_for_inputs_rejected_by_canonical_json(self) -> None:
        payloads = (
            {"number": 1.25},
            {"wide": 2**80},
            {1: "integer key"},
            {"unicode": "caf\N{LATIN SMALL LETTER E WITH ACUTE}"},
            {"not_a_number": math.nan},
        )

        for payload in payloads:
            with self.subTest(payload=payload):
                serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
                expected = hashlib.sha256(serialized.encode()).hexdigest()
                self.assertEqual(nonvalidating_compact_sorted_json_sha256(payload), expected)

    def test_preserves_json_serialization_errors(self) -> None:
        circular: list[object] = []
        circular.append(circular)

        for payload, error in ((object(), TypeError), (circular, ValueError)):
            with self.subTest(error=error):
                with self.assertRaises(error):
                    nonvalidating_compact_sorted_json_sha256(payload)


class CanonicalJsonTests(unittest.TestCase):
    def test_canonical_json_rejects_floats_non_finite_numbers_and_oversized_integers(self) -> None:
        invalid_payloads = (
            {"value": 1.0},
            {"value": float("nan")},
            {"value": float("inf")},
            {"value": CANONICAL_JSON_INTEGER_MAX + 1},
            {"value": CANONICAL_JSON_INTEGER_MIN - 1},
        )

        for invalid_payload in invalid_payloads:
            with self.subTest(payload=invalid_payload):
                with self.assertRaises(ValueError):
                    canonical_json_bytes(invalid_payload)

    def test_canonical_json_bytes_and_sha256_are_stable(self) -> None:
        payload = {"b": 2, "a": [True, None, "μ"]}
        reordered = {"a": [True, None, "μ"], "b": 2}
        serialized = canonical_json_bytes(payload)
        self.assertEqual(serialized, canonical_json_bytes(reordered))
        self.assertEqual(canonical_json_sha256(payload), hashlib.sha256(serialized).hexdigest())
        self.assertEqual(json.loads(serialized), payload)

    def test_privileged_operation_digest_uses_canonical_json(self) -> None:
        request = ManagedSecretReencryptionPlanInput(
            reason="Review canonical root migration",
            source_label="test-plan",
        )
        self.assertEqual(
            privileged_operation_request_digest(request),
            canonical_json_sha256(request.model_dump(mode="json")),
        )
