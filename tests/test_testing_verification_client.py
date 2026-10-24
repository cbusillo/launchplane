"""The bounded verification client binds review identity and never repeats an uncertain call."""

from tests.support import bounded_testing_client as support


class TestingVerificationClientTests(support.BoundedTestingClientContract):
    script_filename = "complete-testing-verification.py"
    extra_result = {
        "deployment_record_id": "installed-deployment",
        "public_health_verified": False,
    }
    extra_arguments = ("--deployment-record-id", "installed-deployment")

    def test_review_is_bound_to_the_installed_deployment(self) -> None:
        self.receipt["result"]["deployment_record_id"] = "other-deployment"
        self.assertEqual(self.call("dry-run")[0], 1)
        self.assertFalse(self.evidence.exists())
        self.receipt["result"]["deployment_record_id"] = "installed-deployment"
        self.assertEqual(self.call("dry-run")[0], 0)
        self.assertEqual(
            self.request.call_args.kwargs["body"]["deployment_record_id"], "installed-deployment"
        )
