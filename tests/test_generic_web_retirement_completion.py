import unittest
from unittest.mock import patch

from tests import test_generic_web_testing_rebuild as generic


class RetirementCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = generic.ApplicationAbsenceTests()
        self.fixture.setUp()

    def stopping_container(self) -> None:
        self.fixture.containers["stopping"] = {
            "Id": "stopping",
            "Config": {"Labels": {"com.docker.swarm.service.id": "original-service"}},
            "State": {"Status": "exited"},
            "Mounts": [],
        }

    def test_retirement_waits_for_container_teardown_after_service_removal(self) -> None:
        self.fixture.services["original-service"] = {
            "ID": "original-service",
            "Spec": {"Name": "original-app", "TaskTemplate": {"ContainerSpec": {"Mounts": []}}},
        }
        docker = self.fixture.docker

        def command(*arguments: str) -> str:
            result = docker(*arguments)
            if arguments == ("service", "rm", "original-service"):
                self.stopping_container()
            return result

        with (
            patch.object(self.fixture, "docker", side_effect=command),
            patch(
                "time.sleep", side_effect=lambda _seconds: self.fixture.containers.clear()
            ) as wait,
        ):
            self.fixture.observe("retire")
        self.assertTrue(wait.called)
        self.assertEqual(self.fixture.removed, ["original-service"])
        self.assertEqual(self.fixture.containers, {})

    def test_original_reentry_waits_when_only_a_stopping_container_remains(self) -> None:
        self.stopping_container()
        with patch(
            "time.sleep", side_effect=lambda _seconds: self.fixture.containers.clear()
        ) as wait:
            self.fixture.observe("retire")
        self.assertTrue(wait.called)
        self.assertEqual(self.fixture.removed, [])

    def test_unfinished_teardown_reaches_a_bounded_refusal_without_another_remove(self) -> None:
        self.stopping_container()
        with patch("time.monotonic", side_effect=[0, float("inf")]), self.assertRaises(ValueError):
            self.fixture.observe("retire")
        self.assertEqual(self.fixture.removed, [])
        self.assertIn("stopping", self.fixture.containers)
