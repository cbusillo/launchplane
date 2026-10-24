"""Review binding and recovery contract for the public-edge client."""

from tests.support import bounded_testing_client as support


class TestingPublicIngressClientTests(support.BoundedTestingClientContract):
    script_filename = "prepare-testing-public-ingress.py"

    def setUp(self) -> None:
        super().setUp()
        self.request.side_effect = lambda **_: {
            **self.receipt,
            "result": {
                **self.receipt["result"],
                "public_tls_verified": self.receipt["result"]["applied"],
            },
        }

    def test_old_or_unverified_service_receipt_does_not_claim_tls_success(self) -> None:
        self.assertEqual(self.call("dry-run")[0], 0)
        self.receipt["result"]["applied"] = True
        self.request.side_effect = None
        self.receipt["result"]["public_tls_verified"] = False
        self.assertEqual(self.call("readback")[0], 1)
