import unittest

from control_plane.contracts.structured_health import structured_health_evidence_from_payload


class StructuredHealthTests(unittest.TestCase):
    def test_parses_bounded_version_and_identity_fields(self) -> None:
        evidence = structured_health_evidence_from_payload(
            {
                "status": "ok",
                "version": "2026.06.14",
                "source_git_ref": "abc123",
                "image_reference": "ghcr.io/example/product@sha256:abc",
                "summary": "fresh",
                "components": {"discord": {"status": "ok"}, "db": {"status": "ok"}},
                "secret_like_extra": "not persisted",
            }
        )

        self.assertEqual(evidence.status, "pass")
        self.assertEqual(evidence.version, "2026.06.14")
        self.assertEqual(evidence.source_git_ref, "abc123")
        self.assertEqual(evidence.image_reference, "ghcr.io/example/product@sha256:abc")
        self.assertEqual(evidence.detail, "fresh")
        self.assertEqual(evidence.components, ("db", "discord"))

    def test_marks_failure_status(self) -> None:
        evidence = structured_health_evidence_from_payload(
            {"status": "not_ready", "summary": "last sync is stale"}
        )

        self.assertEqual(evidence.status, "fail")
        self.assertEqual(evidence.detail, "last sync is stale")

    def test_marks_unknown_status_malformed(self) -> None:
        evidence = structured_health_evidence_from_payload({"status": "purple"})

        self.assertEqual(evidence.status, "malformed")
        self.assertEqual(evidence.reported_status, "purple")

    def test_none_payload_is_unchecked(self) -> None:
        evidence = structured_health_evidence_from_payload(None)

        self.assertEqual(evidence.status, "unchecked")

    def test_accepts_alias_fields(self) -> None:
        evidence = structured_health_evidence_from_payload(
            {
                "state": "ready",
                "build_version": "2026.06.14",
                "commit": "abc123",
                "image": "ghcr.io/example/product@sha256:abc",
            }
        )

        self.assertEqual(evidence.status, "pass")
        self.assertEqual(evidence.version, "2026.06.14")
        self.assertEqual(evidence.source_git_ref, "abc123")
        self.assertEqual(evidence.image_reference, "ghcr.io/example/product@sha256:abc")

    def test_non_object_payload_is_malformed(self) -> None:
        evidence = structured_health_evidence_from_payload(["ok"])

        self.assertEqual(evidence.status, "malformed")


if __name__ == "__main__":
    unittest.main()
