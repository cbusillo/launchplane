from copy import deepcopy
from typing import Any
import unittest
from unittest.mock import Mock, patch

from tests import test_odoo_upgrade_rehearsal as rehearsal_tests
from tests.test_odoo_rehearsal_business_validation import receipt


class PreSwitchVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = rehearsal_tests.RehearsalHostSafetyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.host = self.fixture.host
        self.spec, self.root, self.containers = self.fixture.storage_fixture(
            {"database_bytes": 40, "filestore_bytes": 80, "staging_bytes": 20, "reserve_bytes": 10}
        )
        self.spec.update(action="simulate-final", phase_elapsed={})
        self.spec["record"]["target_artifact"] = {
            "artifact_id": "candidate",
            "source_commit": "b" * 40,
        }
        self.spec["record"]["product"] = "fixture"
        self.spec["business_validation_identity"] = {
            "product": "fixture",
            "rehearsal_id": self.fixture.identifier,
            "artifact_id": "candidate",
            "source_commit": "b" * 40,
            "database": self.host["database_names"](self.spec)["final_upgrade"],
            "attempt_sha256": self.spec["attempt_sha256"],
        }
        self.candidate = deepcopy(self.containers["web"])
        self.candidate[1]["State"]["Running"] = True
        self.containers["pre-switch-web"] = ("candidate-web", self.candidate[1])
        self.containers["web"][1]["State"]["Running"] = False
        self.events: list[str] = []
        for key in ("clone", "compose", "sql", "copy_runner", "before_web"):
            self.host[key] = Mock()
        self.host["before_web"].side_effect = lambda spec, *args, **kwargs: self.host[
            "phase_stamp"
        ](spec, "before_web")
        self.host["inspection"] = Mock(return_value=self.containers)
        self.host["private_health"] = Mock(
            side_effect=lambda *args, **kwargs: self.events.append(kwargs.get("hostname", "web"))
        )
        self.host["business_validation"] = Mock(side_effect=self.business)
        self.host["connection_readback"] = Mock(
            side_effect=lambda *args: self.events.append("isolation")
        )
        self.host["docker"] = Mock(side_effect=self.docker)

    def docker(self, *args: str, **kwargs: Any) -> bytes:
        if args[0] == "start":
            self.assertIn("pre_switch_verified", self.spec["phase_timing"]["observed"])
            self.events.append("switch")
        return b""

    def business(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.events.append("business")
        self.assertIs(kwargs["timed"], False)
        return receipt(self.spec["business_validation_identity"])

    def test_candidate_http_business_and_isolation_pass_before_normal_web_starts(self) -> None:
        self.host["upgrade_final"](self.spec, self.root, self.containers)
        self.assertEqual(self.events, ["pre-switch-web", "business", "isolation", "switch", "web"])
        timing = self.spec["phase_timing"]
        self.assertEqual(
            timing["pre_switch_validation"], receipt(self.spec["business_validation_identity"])
        )
        self.assertLessEqual(
            timing["observed"]["pre_switch_verified"],
            timing["observed"]["private_switch_verified"],
        )
        self.assertNotIn("product_started", timing["observed"])
        retained = self.host["pre_switch_timing"](self.spec, self.root)
        self.assertEqual(
            retained["observed"]["pre_switch_verified"], timing["observed"]["pre_switch_verified"]
        )
        self.assertNotIn("private_switch_verified", retained["observed"])

    def test_lost_switch_keeps_original_t4_and_refuses_another_attempt(self) -> None:
        def fail_switch(*args: str, **kwargs: Any) -> bytes:
            if args[0] == "start":
                raise RuntimeError("switch interrupted")
            return b""

        self.host["docker"].side_effect = fail_switch
        with self.assertRaises(RuntimeError):
            self.host["upgrade_final"](self.spec, self.root, self.containers)
        first = self.host["pre_switch_timing"](self.spec, self.root)
        observed = self.fixture.run_host({**self.spec, "mode": "observe"})
        self.assertFalse(observed["completed"])
        self.assertEqual(observed["phase_timing"], first)
        self.assertNotIn("private_switch_verified", first["observed"])
        self.host["business_validation"].assert_called_once()
        with self.assertRaises(RuntimeError):
            self.host["pre_switch_timing"]({**self.spec, "attempt_sha256": "f" * 64}, self.root)

    def test_failed_candidate_checks_leave_normal_web_stopped_and_t4_unknown(self) -> None:
        for failure in ("http", "business", "isolation"):
            with self.subTest(failure=failure):
                spec = deepcopy(self.spec)
                self.host["private_health"].side_effect = (
                    RuntimeError("http") if failure == "http" else None
                )
                self.host["business_validation"].return_value = receipt(
                    spec["business_validation_identity"]
                )
                self.host["business_validation"].side_effect = None
                if failure == "business":
                    self.host["business_validation"].return_value["passed"] = False
                self.host["connection_readback"].side_effect = (
                    RuntimeError("isolation") if failure == "isolation" else None
                )
                self.host["docker"].reset_mock()
                with self.assertRaises(RuntimeError):
                    self.host["upgrade_final"](spec, self.root, self.containers)
                self.assertNotIn(
                    "pre_switch_verified", spec.get("phase_timing", {}).get("observed", {})
                )
                self.assertFalse(
                    any(call.args[0] == "start" for call in self.host["docker"].call_args_list)
                )
                self.assertIn(
                    ("stop", "candidate-web"),
                    [call.args for call in self.host["docker"].call_args_list],
                )


class CandidateIsolationTests(unittest.TestCase):
    def test_candidate_reuses_only_rehearsal_storage_and_private_network(self) -> None:
        fixture = rehearsal_tests.RehearsalHostSafetyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        candidate = deepcopy(fixture.fixture.containers["web"])
        candidate["Id"] = "candidate-id"
        candidate["Config"]["Labels"]["com.docker.compose.service"] = "pre-switch-web"
        candidate["State"]["Running"] = False
        fixture.fixture.containers["pre-switch-web"] = candidate
        with self.assertRaises(RuntimeError):
            fixture.inspect()
        fixture.spec["pre_switch_candidate"] = True
        self.assertIn("pre-switch-web", fixture.inspect())
        candidate["State"]["Running"] = True
        with self.assertRaises(RuntimeError):
            fixture.inspect()
        fixture.spec["candidate_may_run"] = True
        self.assertIn("pre-switch-web", fixture.inspect())
        for change in ("port", "network", "credentials", "storage"):
            saved = deepcopy(candidate)
            with self.subTest(change=change):
                if change == "port":
                    candidate["HostConfig"]["PortBindings"] = {"8069/tcp": [{"HostPort": "8069"}]}
                elif change == "network":
                    candidate["NetworkSettings"]["Networks"]["dokploy-network"] = {}
                elif change == "credentials":
                    candidate["Config"]["Env"].append("ODOO_KEY=live-key")
                else:
                    candidate["Mounts"].append(
                        {"Type": "bind", "Source": "/private", "Destination": "/host"}
                    )
                with self.assertRaises(RuntimeError):
                    fixture.inspect()
            candidate.clear()
            candidate.update(saved)

    def test_http_probe_checks_exact_runtime_identity_and_refuses_redirects(self) -> None:
        import io
        import json
        import sys
        import urllib.request

        fixture = rehearsal_tests.RehearsalHostSafetyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        spec = {
            "record": {
                "product": "fixture",
                "rehearsal_id": fixture.identifier,
                "target_artifact": {"artifact_id": "candidate", "source_commit": "b" * 40},
            },
            "target_image": "fixture@sha256:" + "c" * 64,
        }
        expected = fixture.host["runtime_identity"](spec, spec["target_image"])
        opener = Mock()

        def docker(*args: str, **kwargs: Any) -> bytes:
            with patch.object(sys, "argv", ["probe", *args[5:]]):
                exec(compile(args[4], "<private-probe>", "exec"), {})
            return b""

        fixture.host["docker"] = docker
        for change in ({}, {"image_reference": "other"}, {"deployment_record_id": "other"}):

            def response(*args: Any, **kwargs: Any) -> Any:
                body = io.BytesIO(json.dumps({"runtime_identity": {**expected, **change}}).encode())
                body.status = 200  # type: ignore[attr-defined]
                return body

            opener.open.side_effect = response
            with (
                self.subTest(change=change),
                patch.object(urllib.request, "build_opener", return_value=opener) as build,
                patch("time.sleep"),
            ):
                if change:
                    with self.assertRaises(AssertionError):
                        fixture.native_private_health(
                            spec, {"script-runner": ("runner", {})}, hostname="pre-switch-web"
                        )
                else:
                    fixture.native_private_health(
                        spec, {"script-runner": ("runner", {})}, hostname="pre-switch-web"
                    )
                    self.assertEqual(
                        opener.open.call_args.args[0], "http://pre-switch-web:8069/web/login"
                    )
                with self.assertRaises(RuntimeError):
                    build.call_args.args[1].redirect_request(
                        None, None, 302, "redirect", {}, "https://example.invalid"
                    )


if __name__ == "__main__":
    unittest.main()
