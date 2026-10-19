"""Review binding and recovery contract for the public-edge client."""

from tests.support import bounded_testing_client as support


class TestingPublicIngressClientTests(support.BoundedTestingClientContract):
    script_filename = "prepare-testing-public-ingress.py"
