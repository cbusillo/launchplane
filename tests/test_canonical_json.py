import hashlib
import json
import math
import unittest

from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256


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
