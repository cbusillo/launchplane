import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import urlsplit
import yaml

from control_plane.deploy_failure_alert import replacement_may_have_dispatched


ACTION_ENTRYPOINT = Path(".github/actions/launchplane-request/dist/index.js")


class LaunchplaneRequestActionTests(unittest.TestCase):
    def test_train_runner_defers_only_controller_lease_contention(self) -> None:
        workflow = yaml.safe_load(Path(".github/workflows/merge-train-runner.yml").read_text())
        steps = {step.get("id"): step for step in workflow["jobs"]["run"]["steps"]}
        for code, succeeds in (
            ("merge_train_controller_lease_held", True),
            ("merge_train_effect_reconciliation_required", False),
            ("authorization_denied", False),
        ):
            with self.subTest(code=code), TemporaryDirectory() as directory:
                response = Path(directory) / "response.json"
                output = Path(directory) / "output"
                summary = Path(directory) / "summary"
                request = self.run_action(
                    inputs={
                        "launchplane-url": "https://launchplane.example",
                        "route-path": "/v1/work-graph/merge-train/controller/run-once",
                        "expected-status": steps["controller_run"]["with"].get(
                            "expected-status", ""
                        ),
                        "response-output-file": str(response),
                        "log-response-body": "false",
                        "fail-result-paths": "",
                    },
                    environment={"TEST_STATUS": "409", "TEST_ERROR_CODE": code},
                )
                self.assertEqual(request.returncode, 0, request.stderr)
                result = subprocess.run(
                    ["bash", "-c", steps["controller_summary"]["run"]],
                    env={
                        **os.environ,
                        "STATUS_CODE": "409",
                        "RESPONSE_FILE": str(response),
                        "GITHUB_OUTPUT": str(output),
                        "GITHUB_STEP_SUMMARY": str(summary),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                if succeeds:
                    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
                    self.assertEqual(int(outputs["feedback_count"]), 0)
                else:
                    self.assertFalse(output.exists())

    def run_action(
        self,
        *,
        inputs: dict[str, str],
        environment: dict[str, str] | None = None,
        output_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if shutil.which("node") is None:
            self.skipTest("node is required to test the Launchplane request action")

        env = os.environ.copy()
        env.update(
            {
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example/token",
            }
        )
        if output_path is not None:
            env["GITHUB_OUTPUT"] = str(output_path)
        if environment:
            env.update(environment)
        for name, value in inputs.items():
            env[f"INPUT_{name.upper()}"] = value

        script = f"""
const calls = [];
let launchplaneRequestCount = 0;
let oidcTokenCount = 0;
let testClock = 0;
const clockStep = Number(process.env.TEST_CLOCK_STEP_MS || '0');
if (clockStep) Date.now = () => testClock;
global.fetch = async (url, init) => {{
  calls.push({{url, init}});
  if (url.startsWith('https://oidc.example/token')) {{
    oidcTokenCount += 1;
    const oidcFailures = String(process.env.TEST_OIDC_FAILURE_ATTEMPTS || '').split(',').map(Number);
    if (oidcFailures.includes(oidcTokenCount)) {{
      testClock += clockStep;
      return new Response(JSON.stringify({{error: 'temporarily unavailable'}}), {{status: 500}});
    }}
    return new Response(JSON.stringify({{value: `oidc-token-${{oidcTokenCount}}`}}), {{status: 200}});
  }}
  launchplaneRequestCount += 1;
  testClock += clockStep;
  const failureAttempts = String(process.env.TEST_LAUNCHPLANE_NETWORK_FAILURE_ATTEMPTS || '')
    .split(',')
    .filter(Boolean)
    .map((value) => Number(value));
  if (failureAttempts.includes(launchplaneRequestCount)) {{
    throw new TypeError('simulated Launchplane network failure');
  }}
  const ingressAttempts = String(process.env.TEST_INGRESS_404_ATTEMPTS || '')
    .split(',')
    .filter(Boolean)
    .map((value) => Number(value));
  if (ingressAttempts.includes(launchplaneRequestCount)) {{
    return new Response('404 page not found', {{
      status: Number(process.env.TEST_INGRESS_STATUS || '404'),
      headers: {{'content-type': 'text/plain; charset=utf-8'}}
    }});
  }}
  const configuredStatusCodes = String(process.env.TEST_STATUS_SEQUENCE || '').split(',').filter(Boolean);
  const statusCode = Number(configuredStatusCodes[launchplaneRequestCount - 1] || process.env.TEST_STATUS || '200');
  const configuredRuntimeImages = String(process.env.TEST_RUNTIME_IMAGES || '').split(',').filter(Boolean);
  const configuredRuntimeMarkers = String(process.env.TEST_RUNTIME_MARKERS || '').split(',').filter(Boolean);
  if (configuredRuntimeImages.length > 0) {{
    const imageReference = configuredRuntimeImages[launchplaneRequestCount - 1] || configuredRuntimeImages.at(-1);
    return new Response(JSON.stringify({{
      runtime: {{
        docker_image_reference: imageReference,
        deployment_marker: configuredRuntimeMarkers[launchplaneRequestCount - 1] || configuredRuntimeMarkers.at(-1)
      }}
    }}), {{status: statusCode}});
  }}
  const configuredStatuses = String(process.env.TEST_REFRESH_STATUSES || '').split(',').filter(Boolean);
  const refreshStatus = configuredStatuses[launchplaneRequestCount - 1] || process.env.TEST_REFRESH_STATUS || 'pass';
  if (process.env.TEST_TRACE_ONLY === 'true') {{
    return new Response(JSON.stringify({{
      status: 'rejected',
      trace_id: process.env.TEST_TRACE_ID || ''
    }}), {{status: statusCode}});
  }}
  const errorCode = String(process.env.TEST_ERROR_CODES || '').split(',')[launchplaneRequestCount - 1] || process.env.TEST_ERROR_CODE;
  if (errorCode) {{
    return new Response(JSON.stringify({{
      status: 'rejected',
      trace_id: process.env.TEST_TRACE_ID || '',
      error: {{
        code: errorCode,
        message: process.env.TEST_ERROR_MESSAGE || 'request rejected'
      }}
    }}), {{status: statusCode}});
  }}
  return new Response(JSON.stringify({{
    ok: true,
    result: {{
      [process.env.TEST_RESULT_FIELD || 'refresh_status']: refreshStatus,
      error_message: process.env.TEST_ERROR_MESSAGE || '',
      application_id: 'app-123'
      }}
  }}), {{status: statusCode}});
}};
require('./{ACTION_ENTRYPOINT.as_posix()}');
process.on('beforeExit', () => {{
  console.error(JSON.stringify(calls.map((call) => ({{
    url: call.url,
    method: call.init.method,
    headers: call.init.headers,
    body: call.init.body || ''
  }}))));
}});
"""
        return subprocess.run(
            ["node", "-e", script],
            check=False,
            capture_output=True,
            env=env,
            text=True,
        )

    def test_unavailable_budget_preserves_healthy_drain_and_resets_after_recovery(self) -> None:
        for failures in ("", "2,4"):
            with self.subTest(failures=failures):
                result = self.run_action(
                    inputs={
                        "launchplane-url": "https://launchplane.example",
                        "route-path": "/v1/drivers/launchplane/self-deploy",
                        "poll-result-path": "result.deploy_state",
                        "poll-result-statuses": "draining",
                        "poll-retry-on-request-error": "true",
                        "poll-unavailable-timeout-ms": "25",
                        "poll-timeout-ms": "1000",
                        "poll-interval-ms": "1",
                        "retry-attempts": "1",
                    },
                    environment={
                        "TEST_CLOCK_STEP_MS": "10",
                        "TEST_RESULT_FIELD": "deploy_state",
                        "TEST_REFRESH_STATUSES": "draining,draining,draining,draining,draining,requested",
                        "TEST_LAUNCHPLANE_NETWORK_FAILURE_ATTEMPTS": failures,
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_unavailable_budget_bounds_network_and_ingress_outages_with_long_drain_budget(
        self,
    ) -> None:
        for kind in ("TEST_LAUNCHPLANE_NETWORK_FAILURE_ATTEMPTS", "TEST_INGRESS_404_ATTEMPTS"):
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                response = Path(directory) / "response.json"
                result = self.run_action(
                    inputs={
                        "launchplane-url": "https://launchplane.example",
                        "route-path": "/v1/drivers/launchplane/self-deploy",
                        "response-output-file": str(response),
                        "poll-result-path": "result.deploy_state",
                        "poll-result-statuses": "draining,dispatch_in_progress",
                        "poll-retry-on-request-error": "true",
                        "poll-unavailable-timeout-ms": "25",
                        "poll-timeout-ms": "1000",
                        "poll-interval-ms": "1",
                        "ingress-retry-delay-ms": "0",
                        "retry-attempts": "1",
                    },
                    environment={
                        "TEST_CLOCK_STEP_MS": "10",
                        "TEST_RESULT_FIELD": "deploy_state",
                        "TEST_REFRESH_STATUSES": "dispatch_in_progress"
                        if kind == "TEST_INGRESS_404_ATTEMPTS"
                        else "draining",
                        kind: ",".join(str(index) for index in range(2, 100)),
                    },
                )
                self.assertNotEqual(result.returncode, 0)
                calls = json.loads(result.stderr.strip().splitlines()[-1])
                mutations = [
                    call
                    for call in calls
                    if urlsplit(call["url"]).hostname == "launchplane.example"
                ]
                self.assertLess(len(mutations), 10)
                if kind == "TEST_LAUNCHPLANE_NETWORK_FAILURE_ATTEMPTS":
                    self.assertIsNone(json.loads(response.read_text()))
                else:
                    self.assertNotEqual(
                        (json.loads(response.read_text()) or {})
                        .get("result", {})
                        .get("deploy_state"),
                        "draining",
                    )

    def test_replacement_deadline_starts_at_marker_and_carries_elapsed_outage(self) -> None:
        for failures, markers, start_step in (
            ("", ["old"] * 6 + ["forward"], 7),
            ("2", ["old", "old", "forward"], 1),
            ("1", ["old"] * 4 + ["forward"], 5),
        ):
            with self.subTest(failures=failures), TemporaryDirectory() as directory:
                clock_step = 1000
                short_budget = 4000
                output = Path(directory) / "output"
                result = self.run_action(
                    output_path=output,
                    inputs={
                        "launchplane-url": "https://launchplane.example",
                        "route-path": "/v1/service/runtime",
                        "method": "GET",
                        "poll-until-path": "runtime.deployment_marker",
                        "poll-until-value": markers[-1],
                        "poll-retry-on-request-error": "true",
                        "poll-unavailable-timeout-ms": str(short_budget),
                        "poll-timeout-ms": "30000",
                        "poll-interval-ms": "1",
                        "retry-attempts": "1",
                    },
                    environment={
                        "TEST_CLOCK_STEP_MS": str(clock_step),
                        "TEST_RUNTIME_IMAGES": "registry.example/service@sha256:same-image",
                        "TEST_RUNTIME_MARKERS": ",".join(markers),
                        "TEST_LAUNCHPLANE_NETWORK_FAILURE_ATTEMPTS": failures,
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                deadline = int(
                    output.read_text().split("observation-deadline-epoch<<")[1].splitlines()[1]
                )
                window_start = start_step * clock_step
                self.assertEqual(deadline, (window_start + short_budget) // 1000)

    def test_pre_request_auth_and_ingress_outages_preserve_known_drain(self) -> None:
        for failure in ("auth", "ingress"):
            for recovers in (True, False):
                with self.subTest(failure=failure, recovers=recovers), TemporaryDirectory() as name:
                    response = Path(name) / "response.json"
                    result = self.run_action(
                        inputs={
                            "launchplane-url": "https://launchplane.example",
                            "route-path": "/v1/launchplane/self-deploy",
                            "poll-until-path": "result.deploy_state",
                            "poll-until-value": "requested",
                            "poll-retry-on-request-error": "true",
                            "poll-retry-on-unexpected-status": "true",
                            "poll-unavailable-timeout-ms": "2000",
                            "poll-timeout-ms": "10000",
                            "poll-interval-ms": "1",
                            "retry-attempts": "1",
                            "ingress-retry-delay-ms": "0",
                            "response-output-file": str(response),
                        },
                        environment={
                            "TEST_CLOCK_STEP_MS": "1000",
                            "TEST_RESULT_FIELD": "deploy_state",
                            "TEST_REFRESH_STATUSES": "draining,requested"
                            if failure == "auth"
                            else "draining,draining,draining,draining,requested",
                            "TEST_OIDC_FAILURE_ATTEMPTS": ",".join(
                                str(i) for i in range(2, 5 if recovers else 20)
                            )
                            if failure == "auth"
                            else "",
                            "TEST_INGRESS_404_ATTEMPTS": ",".join(
                                str(i) for i in range(2, 5 if recovers else 20)
                            )
                            if failure == "ingress"
                            else "",
                        },
                    )
                    if recovers:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(
                            json.loads(response.read_text())["result"]["deploy_state"], "requested"
                        )
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(
                            json.loads(response.read_text())["result"]["deploy_state"], "draining"
                        )

    def test_auth_outage_never_overwrites_dispatch_proof(self) -> None:
        for unavailable_budget in ("0", "2000"):
            for uncertain in (False, True):
                with TemporaryDirectory() as name:
                    response = Path(name) / "response.json"
                    result = self.run_action(
                        inputs={
                            "launchplane-url": "https://launchplane.example",
                            "route-path": "/v1/launchplane/self-deploy",
                            "poll-until-path": "result.deploy_state",
                            "poll-until-value": "requested",
                            "poll-retry-on-request-error": "true",
                            "poll-unavailable-timeout-ms": unavailable_budget,
                            "poll-timeout-ms": "8000",
                            "poll-interval-ms": "1",
                            "retry-attempts": "1",
                            "response-output-file": str(response),
                        },
                        environment={
                            "TEST_CLOCK_STEP_MS": "1000",
                            "TEST_LAUNCHPLANE_NETWORK_FAILURE_ATTEMPTS": "1" if uncertain else "",
                            "TEST_OIDC_FAILURE_ATTEMPTS": ",".join(
                                str(i) for i in range(2 if uncertain else 1, 20)
                            ),
                        },
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(
                        replacement_may_have_dispatched(response),
                        uncertain or unavailable_budget == "0",
                    )
                    calls = json.loads(result.stderr.strip().splitlines()[-1])
                    self.assertEqual(
                        any(
                            urlsplit(call["url"]).hostname == "launchplane.example"
                            for call in calls
                        ),
                        uncertain,
                    )

    def test_later_auth_refusal_cannot_erase_dispatch_uncertainty(self) -> None:
        for prior in ("none", "busy", "unknown"):
            for code, status in (
                ("authentication_required", "401"),
                ("authorization_denied", "403"),
            ):
                with self.subTest(prior=prior, code=code), TemporaryDirectory() as name:
                    response = Path(name) / "response.json"
                    result = self.run_action(
                        inputs={
                            "launchplane-url": "https://launchplane.example",
                            "route-path": "/v1/launchplane/self-deploy",
                            "poll-result-path": "result.deploy_state",
                            "poll-result-statuses": "draining,dispatch_in_progress",
                            "poll-retry-on-request-error": "true",
                            "poll-unavailable-timeout-ms": "4000",
                            "poll-timeout-ms": "10000",
                            "poll-interval-ms": "1",
                            "retry-attempts": "1",
                            "response-output-file": str(response),
                        },
                        environment={
                            "TEST_CLOCK_STEP_MS": "1000",
                            "TEST_RESULT_FIELD": "deploy_state",
                            "TEST_REFRESH_STATUSES": "dispatch_in_progress",
                            "TEST_STATUS_SEQUENCE": status if prior == "none" else "200," + status,
                            "TEST_ERROR_CODES": code if prior == "none" else "," + code,
                            "TEST_LAUNCHPLANE_NETWORK_FAILURE_ATTEMPTS": "1"
                            if prior == "unknown"
                            else "",
                        },
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(replacement_may_have_dispatched(response), prior != "none")

    def test_gateway_lost_reply_after_drain_starts_failure_window(self) -> None:
        for status in ("502", "503", "504"):
            with self.subTest(status=status), TemporaryDirectory() as name:
                response = Path(name) / "response.json"
                result = self.run_action(
                    inputs={
                        "launchplane-url": "https://launchplane.example",
                        "route-path": "/v1/launchplane/self-deploy",
                        "poll-result-path": "result.deploy_state",
                        "poll-result-statuses": "draining,dispatch_in_progress",
                        "poll-retry-on-request-error": "true",
                        "poll-unavailable-timeout-ms": "2000",
                        "poll-timeout-ms": "10000",
                        "poll-interval-ms": "1",
                        "ingress-retry-delay-ms": "0",
                        "retry-attempts": "1",
                        "response-output-file": str(response),
                    },
                    environment={
                        "TEST_CLOCK_STEP_MS": "1000",
                        "TEST_RESULT_FIELD": "deploy_state",
                        "TEST_REFRESH_STATUSES": "draining",
                        "TEST_INGRESS_STATUS": status,
                        "TEST_INGRESS_404_ATTEMPTS": ",".join(str(i) for i in range(2, 20)),
                    },
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(replacement_may_have_dispatched(response))
                calls = json.loads(result.stderr.strip().splitlines()[-1])
                self.assertLess(
                    sum(urlsplit(call["url"]).hostname == "launchplane.example" for call in calls),
                    10,
                )

    def test_authoritative_drain_restores_no_dispatch_after_lost_reply(self) -> None:
        for failure in ("network", "gateway"):
            with self.subTest(failure=failure), TemporaryDirectory() as name:
                response = Path(name) / "response.json"
                result = self.run_action(
                    inputs={
                        "launchplane-url": "https://launchplane.example",
                        "route-path": "/v1/launchplane/self-deploy",
                        "poll-result-path": "result.deploy_state",
                        "poll-result-statuses": "draining,dispatch_in_progress",
                        "poll-retry-on-request-error": "true",
                        "poll-unavailable-timeout-ms": "2000",
                        "poll-timeout-ms": "10000",
                        "poll-interval-ms": "1",
                        "ingress-retry-delay-ms": "0",
                        "retry-attempts": "1",
                        "response-output-file": str(response),
                    },
                    environment={
                        "TEST_CLOCK_STEP_MS": "1000",
                        "TEST_RESULT_FIELD": "deploy_state",
                        "TEST_REFRESH_STATUS": "draining",
                        "TEST_LAUNCHPLANE_NETWORK_FAILURE_ATTEMPTS": "2"
                        if failure == "network"
                        else "",
                        "TEST_INGRESS_404_ATTEMPTS": "2" if failure == "gateway" else "",
                        "TEST_INGRESS_STATUS": "502",
                    },
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(replacement_may_have_dispatched(response))
                calls = json.loads(result.stderr.strip().splitlines()[-1])
                self.assertGreaterEqual(
                    sum(urlsplit(call["url"]).hostname == "launchplane.example" for call in calls),
                    10,
                )

    def test_posts_oidc_authenticated_json_with_idempotency_key(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "github-output.txt"
            result = self.run_action(
                output_path=output_path,
                inputs={
                    "launchplane-url": "https://launchplane.example",
                    "route-path": "/v1/drivers/generic-web/preview-refresh",
                    "payload": '{"schema_version":1,"product":"sellyouroutboard"}',
                    "idempotency-key": "generic-web-preview-refresh:sellyouroutboard:42:sha",
                    "output-paths": "application_id=result.application_id",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = json.loads(result.stderr.strip().splitlines()[-1])
            self.assertEqual(
                calls[0]["url"],
                "https://oidc.example/token?audience=launchplane.example",
            )
            self.assertEqual(
                calls[1]["url"],
                "https://launchplane.example/v1/drivers/generic-web/preview-refresh",
            )
            self.assertEqual(calls[1]["headers"]["Authorization"], "Bearer oidc-token-1")
            self.assertEqual(
                [call for call in calls if call["url"].startswith("https://oidc.example/token")],
                [calls[0]],
            )
            self.assertEqual(
                calls[1]["headers"]["Idempotency-Key"],
                "generic-web-preview-refresh:sellyouroutboard:42:sha",
            )
            self.assertEqual(json.loads(calls[1]["body"])["product"], "sellyouroutboard")
            self.assertIn("application_id<<", output_path.read_text(encoding="utf-8"))

    def test_get_request_omits_payload_and_idempotency_header(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            response_output_path = Path(temporary_directory) / "response.json"
            output_path = Path(temporary_directory) / "github-output.txt"
            result = self.run_action(
                output_path=output_path,
                inputs={
                    "launchplane-url": "https://launchplane.example",
                    "route-path": "/v1/service/runtime",
                    "method": "GET",
                    "response-output-file": str(response_output_path),
                    "log-response-body": "false",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = json.loads(result.stderr.strip().splitlines()[-1])
            self.assertEqual(calls[1]["method"], "GET")
            self.assertEqual(
                calls[1]["url"],
                "https://launchplane.example/v1/service/runtime",
            )
            self.assertEqual(calls[1]["headers"]["Authorization"], "Bearer oidc-token-1")
            self.assertNotIn("Idempotency-Key", calls[1]["headers"])
            self.assertNotIn("Content-Type", calls[1]["headers"])
            self.assertEqual(calls[1]["body"], "")
            self.assertIn("status-code<<", output_path.read_text(encoding="utf-8"))
            self.assertEqual(
                json.loads(response_output_path.read_text(encoding="utf-8"))["result"][
                    "refresh_status"
                ],
                "pass",
            )

    def test_accepts_configured_non_2xx_status(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            response_output_path = Path(temporary_directory) / "response.json"
            output_path = Path(temporary_directory) / "github-output.txt"
            result = self.run_action(
                output_path=output_path,
                inputs={
                    "launchplane-url": "https://launchplane.example",
                    "route-path": "/v1/drivers/odoo/preview-apply-inputs",
                    "payload": '{"schema_version":1,"product":"demo"}',
                    "expected-status": "202,403,503",
                    "response-output-file": str(response_output_path),
                    "log-response-body": "false",
                },
                environment={"TEST_STATUS": "403", "TEST_ERROR_MESSAGE": "forbidden"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(response_output_path.read_text(encoding="utf-8"))["result"][
                    "error_message"
                ],
                "forbidden",
            )
            self.assertIn("status-code<<", output_path.read_text(encoding="utf-8"))
            self.assertIn("403", output_path.read_text(encoding="utf-8"))
            self.assertEqual(result.stdout, "")

    def test_rejects_unexpected_status_when_expected_status_is_configured(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/drivers/odoo/preview-apply-inputs",
                "payload": '{"schema_version":1,"product":"demo"}',
                "expected-status": "202,403,503",
                "log-response-body": "false",
            },
            environment={"TEST_STATUS": "401"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Launchplane request failed with 401", result.stderr)

    def test_reports_public_safe_error_code_when_body_logging_is_disabled(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/work-graph/merge-train/controller/run-once",
                "payload": '{"schema_version":1,"repository":"example/repo"}',
                "log-response-body": "false",
            },
            environment={
                "TEST_STATUS": "409",
                "TEST_ERROR_CODE": "merge_train_landing_not_admitted",
                "TEST_ERROR_MESSAGE": "sensitive diagnostic detail",
                "TEST_TRACE_ID": "launchplane_req_example",
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("code=merge_train_landing_not_admitted", result.stderr)
        self.assertIn("trace_id=launchplane_req_example", result.stderr)
        self.assertNotIn("sensitive diagnostic detail", result.stderr)

    def test_reports_trace_id_without_error_wrapper(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/work-graph/merge-train/controller/run-once",
                "payload": '{"schema_version":1,"repository":"example/repo"}',
                "log-response-body": "false",
            },
            environment={
                "TEST_STATUS": "502",
                "TEST_TRACE_ONLY": "true",
                "TEST_TRACE_ID": "launchplane_req_gateway",
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("trace_id=launchplane_req_gateway", result.stderr)

    def test_rejects_invalid_expected_status_input(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/drivers/odoo/preview-apply-inputs",
                "payload": '{"schema_version":1,"product":"demo"}',
                "expected-status": "202,not-a-status",
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "expected-status entry 'not-a-status' must be a three-digit HTTP status code",
            result.stderr,
        )

    def test_posts_payload_list_with_per_payload_idempotency_keys(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            payload_list_path = Path(temporary_directory) / "payloads.json"
            response_output_path = Path(temporary_directory) / "responses.json"
            payloads = [
                {
                    "schema_version": 1,
                    "repository": "example/repo",
                    "pull_request_number": 1,
                },
                {
                    "schema_version": 1,
                    "repository": "example/repo",
                    "pull_request_number": 2,
                },
            ]
            payload_list_path.write_text(json.dumps(payloads), encoding="utf-8")
            result = self.run_action(
                inputs={
                    "launchplane-url": "https://launchplane.example",
                    "route-path": "/v1/work-graph/merge-train/pr-feedback",
                    "payload-list-file": str(payload_list_path),
                    "idempotency-key-prefix": "merge-train-feedback:controller:run-1",
                    "response-output-file": str(response_output_path),
                    "log-response-body": "false",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(response_output_path.read_text(encoding="utf-8")),
                [
                    {
                        "ok": True,
                        "result": {
                            "refresh_status": "pass",
                            "error_message": "",
                            "application_id": "app-123",
                        },
                    },
                    {
                        "ok": True,
                        "result": {
                            "refresh_status": "pass",
                            "error_message": "",
                            "application_id": "app-123",
                        },
                    },
                ],
            )
            calls = json.loads(result.stderr.strip().splitlines()[-1])
            launchplane_calls = [
                call
                for call in calls
                if call["url"]
                == "https://launchplane.example/v1/work-graph/merge-train/pr-feedback"
            ]
            self.assertEqual(len(launchplane_calls), 2)
            self.assertEqual(
                [call["headers"]["Authorization"] for call in launchplane_calls],
                ["Bearer oidc-token-1", "Bearer oidc-token-2"],
            )
            self.assertTrue(
                all(
                    call["headers"]["Idempotency-Key"].startswith(
                        "merge-train-feedback:controller:run-1:"
                    )
                    for call in launchplane_calls
                )
            )
            self.assertNotEqual(
                launchplane_calls[0]["headers"]["Idempotency-Key"],
                launchplane_calls[1]["headers"]["Idempotency-Key"],
            )
            self.assertEqual(json.loads(launchplane_calls[0]["body"]), payloads[0])
            self.assertEqual(json.loads(launchplane_calls[1]["body"]), payloads[1])
            self.assertEqual(result.stdout, "")

    def test_get_request_polls_until_json_path_matches_expected_value(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            response_output_path = Path(temporary_directory) / "runtime.json"
            result = self.run_action(
                inputs={
                    "launchplane-url": "https://launchplane.example",
                    "route-path": "/v1/service/runtime",
                    "method": "GET",
                    "poll-until-path": "runtime.docker_image_reference",
                    "poll-until-value": "registry.example/launchplane@sha256:new",
                    "poll-interval-ms": "1",
                    "poll-timeout-ms": "1000",
                    "response-output-file": str(response_output_path),
                    "log-response-body": "false",
                },
                environment={
                    "TEST_RUNTIME_IMAGES": (
                        "registry.example/launchplane@sha256:old,"
                        "registry.example/launchplane@sha256:new"
                    )
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = json.loads(result.stderr.strip().splitlines()[-1])
            launchplane_calls = [
                call
                for call in calls
                if call["url"] == "https://launchplane.example/v1/service/runtime"
            ]
            self.assertEqual(len(launchplane_calls), 2)
            self.assertEqual(
                [call["headers"]["Authorization"] for call in launchplane_calls],
                ["Bearer oidc-token-1", "Bearer oidc-token-2"],
            )
            self.assertEqual(
                json.loads(response_output_path.read_text(encoding="utf-8"))["runtime"][
                    "docker_image_reference"
                ],
                "registry.example/launchplane@sha256:new",
            )
            self.assertIn(
                "waiting for registry.example/launchplane@sha256:new",
                result.stderr,
            )

    def test_poll_until_can_retry_launchplane_request_error(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/service/runtime",
                "method": "GET",
                "poll-until-path": "runtime.docker_image_reference",
                "poll-until-value": "registry.example/launchplane@sha256:new",
                "poll-retry-on-request-error": "true",
                "poll-interval-ms": "1",
                "poll-timeout-ms": "1000",
                "retry-attempts": "1",
                "log-response-body": "false",
            },
            environment={
                "TEST_LAUNCHPLANE_NETWORK_FAILURE_ATTEMPTS": "1",
                "TEST_RUNTIME_IMAGES": "registry.example/launchplane@sha256:new",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = json.loads(result.stderr.strip().splitlines()[-1])
        launchplane_calls = [
            call
            for call in calls
            if call["url"] == "https://launchplane.example/v1/service/runtime"
        ]
        self.assertEqual(len(launchplane_calls), 2)
        self.assertIn(
            "Launchplane request failed while waiting for runtime.docker_image_reference",
            result.stderr,
        )

    def test_poll_until_rejects_unexpected_status_by_default(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/service/runtime",
                "method": "GET",
                "expected-status": "200",
                "poll-until-path": "runtime.docker_image_reference",
                "poll-until-value": "registry.example/launchplane@sha256:new",
                "poll-interval-ms": "1",
                "poll-timeout-ms": "1000",
                "log-response-body": "false",
            },
            environment={
                "TEST_RUNTIME_IMAGES": "registry.example/launchplane@sha256:new",
                "TEST_STATUS": "503",
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Launchplane request failed with 503", result.stderr)
        calls = json.loads(result.stderr.strip().splitlines()[-1])
        launchplane_calls = [
            call
            for call in calls
            if call["url"] == "https://launchplane.example/v1/service/runtime"
        ]
        self.assertEqual(len(launchplane_calls), 1)

    def test_poll_until_can_retry_unexpected_status_when_enabled(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/service/runtime",
                "method": "GET",
                "expected-status": "200",
                "poll-until-path": "runtime.docker_image_reference",
                "poll-until-value": "registry.example/launchplane@sha256:new",
                "poll-retry-on-unexpected-status": "true",
                "poll-interval-ms": "1",
                "poll-timeout-ms": "1000",
                "log-response-body": "false",
            },
            environment={
                "TEST_RUNTIME_IMAGES": (
                    "registry.example/launchplane@sha256:old,"
                    "registry.example/launchplane@sha256:new"
                ),
                "TEST_STATUS_SEQUENCE": "503,200",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = json.loads(result.stderr.strip().splitlines()[-1])
        launchplane_calls = [
            call
            for call in calls
            if call["url"] == "https://launchplane.example/v1/service/runtime"
        ]
        self.assertEqual(len(launchplane_calls), 2)
        self.assertIn(
            "Launchplane result runtime.docker_image_reference is HTTP 503", result.stderr
        )

    def test_poll_until_requires_expected_value(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/service/runtime",
                "method": "GET",
                "poll-until-path": "runtime.docker_image_reference",
            }
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "poll-until-value is required when poll-until-path is set",
            result.stderr,
        )

    def test_poll_until_and_poll_result_are_mutually_exclusive(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/service/runtime",
                "method": "GET",
                "poll-until-path": "runtime.docker_image_reference",
                "poll-until-value": "registry.example/launchplane@sha256:new",
                "poll-result-path": "result.refresh_status",
            }
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Use either poll-until-path or poll-result-path, not both",
            result.stderr,
        )

    def test_payload_list_requires_idempotency_prefix(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            payload_list_path = Path(temporary_directory) / "payloads.json"
            payload_list_path.write_text("[]", encoding="utf-8")
            result = self.run_action(
                inputs={
                    "launchplane-url": "https://launchplane.example",
                    "route-path": "/v1/work-graph/merge-train/pr-feedback",
                    "payload-list-file": str(payload_list_path),
                },
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("idempotency-key-prefix is required", result.stderr)

    def test_payload_list_validates_all_entries_before_posting(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            payload_list_path = Path(temporary_directory) / "payloads.json"
            payload_list_path.write_text(
                json.dumps(
                    [
                        {"schema_version": 1, "pull_request_number": 1},
                        ["not", "an", "object"],
                    ]
                ),
                encoding="utf-8",
            )
            result = self.run_action(
                inputs={
                    "launchplane-url": "https://launchplane.example",
                    "route-path": "/v1/work-graph/merge-train/pr-feedback",
                    "payload-list-file": str(payload_list_path),
                    "idempotency-key-prefix": "merge-train-feedback:controller:run-1",
                },
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("payload-list-file entry 2 must be a JSON object", result.stderr)
        self.assertEqual(json.loads(result.stderr.strip().splitlines()[-1]), [])

    def test_payload_list_writes_prior_responses_when_later_request_throws(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            payload_list_path = Path(temporary_directory) / "payloads.json"
            response_output_path = Path(temporary_directory) / "responses.json"
            payload_list_path.write_text(
                json.dumps(
                    [
                        {"schema_version": 1, "pull_request_number": 1},
                        {"schema_version": 1, "pull_request_number": 2},
                    ]
                ),
                encoding="utf-8",
            )
            result = self.run_action(
                inputs={
                    "launchplane-url": "https://launchplane.example",
                    "route-path": "/v1/work-graph/merge-train/pr-feedback",
                    "payload-list-file": str(payload_list_path),
                    "idempotency-key-prefix": "merge-train-feedback:controller:run-1",
                    "response-output-file": str(response_output_path),
                    "retry-attempts": "1",
                    "log-response-body": "false",
                },
                environment={"TEST_LAUNCHPLANE_NETWORK_FAILURE_ATTEMPTS": "2"},
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("simulated Launchplane network failure", result.stderr)
            self.assertEqual(
                json.loads(response_output_path.read_text(encoding="utf-8")),
                [
                    {
                        "ok": True,
                        "result": {
                            "refresh_status": "pass",
                            "error_message": "",
                            "application_id": "app-123",
                        },
                    }
                ],
            )

    def test_retries_launchplane_request_with_fresh_oidc_token(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/drivers/odoo/prod-promotion-run",
                "payload": '{"schema_version":1,"product":"odoo-tenant-cm-website","run":{"schema_version":1,"context":"cm_website","request_id":"27489057866-1"}}',
                "retry-attempts": "2",
                "retry-delay-ms": "1",
            },
            environment={"TEST_LAUNCHPLANE_NETWORK_FAILURE_ATTEMPTS": "1"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = json.loads(result.stderr.strip().splitlines()[-1])
        launchplane_calls = [
            call
            for call in calls
            if call["url"] == "https://launchplane.example/v1/drivers/odoo/prod-promotion-run"
        ]
        self.assertEqual(len(launchplane_calls), 2)
        self.assertEqual(
            [call["headers"]["Authorization"] for call in launchplane_calls],
            ["Bearer oidc-token-1", "Bearer oidc-token-2"],
        )

    def test_resends_request_answered_by_ingress_instead_of_launchplane(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/drivers/odoo/prod-promotion-run",
                "payload": '{"schema_version":1}',
                "idempotency-key": "promotion-1",
                "ingress-retry-delay-ms": "1",
            },
            environment={"TEST_INGRESS_404_ATTEMPTS": "1,2"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = json.loads(result.stderr.strip().splitlines()[-1])
        launchplane_calls = [
            call for call in calls if urlsplit(call["url"]).hostname == "launchplane.example"
        ]
        self.assertEqual(len(launchplane_calls), 3)
        self.assertEqual(
            {call["headers"]["Idempotency-Key"] for call in launchplane_calls}, {"promotion-1"}
        )

    def test_launchplane_json_404_is_not_retried(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/missing",
                "payload": '{"schema_version":1}',
                "ingress-retry-delay-ms": "1",
            },
            environment={"TEST_STATUS": "404", "TEST_ERROR_CODE": "not_found"},
        )

        self.assertNotEqual(result.returncode, 0)
        calls = json.loads(result.stderr.strip().splitlines()[-1])
        launchplane_calls = [
            call for call in calls if urlsplit(call["url"]).hostname == "launchplane.example"
        ]
        self.assertEqual(len(launchplane_calls), 1)

    def test_writes_mapped_response_value_to_file(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            response_output_path = Path(temporary_directory) / "result.json"
            result = self.run_action(
                inputs={
                    "launchplane-url": "https://launchplane.example",
                    "route-path": "/v1/drivers/generic-web/preview-refresh",
                    "payload": '{"schema_version":1,"product":"sellyouroutboard"}',
                    "response-output-file": str(response_output_path),
                    "response-output-path": "result",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(response_output_path.read_text(encoding="utf-8")),
                {
                    "refresh_status": "pass",
                    "error_message": "",
                    "application_id": "app-123",
                },
            )

    def test_can_write_response_file_without_logging_response_body(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            response_output_path = Path(temporary_directory) / "result.json"
            result = self.run_action(
                inputs={
                    "launchplane-url": "https://launchplane.example",
                    "route-path": "/v1/ingress/route-audits/records?product=demo&context=dev",
                    "method": "GET",
                    "response-output-file": str(response_output_path),
                    "log-response-body": "false",
                },
                environment={"TEST_ERROR_MESSAGE": "sensitive-audit-payload"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("sensitive-audit-payload", result.stdout)
            self.assertIn(
                "sensitive-audit-payload",
                response_output_path.read_text(encoding="utf-8"),
            )

    def test_logs_response_body_by_default(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/drivers/generic-web/preview-refresh",
                "payload": '{"schema_version":1,"product":"sellyouroutboard"}',
            },
            environment={"TEST_ERROR_MESSAGE": "default-log-marker"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("default-log-marker", result.stdout)

    def test_omits_non_ok_response_body_from_error_when_logging_disabled(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            response_output_path = Path(temporary_directory) / "result.json"
            result = self.run_action(
                inputs={
                    "launchplane-url": "https://launchplane.example",
                    "route-path": "/v1/ingress/route-audits/records?product=demo&context=dev",
                    "method": "GET",
                    "response-output-file": str(response_output_path),
                    "log-response-body": "false",
                },
                environment={
                    "TEST_ERROR_MESSAGE": "sensitive-audit-error",
                    "TEST_STATUS": "500",
                },
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Launchplane request failed with 500", result.stderr)
            self.assertNotIn("sensitive-audit-error", result.stdout)
            self.assertNotIn("sensitive-audit-error", result.stderr)
            self.assertIn(
                "sensitive-audit-error",
                response_output_path.read_text(encoding="utf-8"),
            )

    def test_overlays_payload_fields_before_request(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/drivers/odoo/prod-rollback",
                "payload": '{"schema_version":1,"product":"odoo","rollback":{"schema_version":1}}',
                "payload-fields": "\n".join(
                    [
                        "rollback.context=cm",
                        "rollback.instance=prod",
                        "rollback.reason=manual rollback requested",
                        "rollback.wait_for_deploy=false",
                        "rollback.timeout_seconds=300",
                    ]
                ),
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = json.loads(result.stderr.strip().splitlines()[-1])
        payload = json.loads(calls[1]["body"])
        self.assertEqual(payload["rollback"]["context"], "cm")
        self.assertEqual(payload["rollback"]["reason"], "manual rollback requested")
        self.assertIs(payload["rollback"]["wait_for_deploy"], False)
        self.assertEqual(payload["rollback"]["timeout_seconds"], 300)

    def test_overlays_payload_json_files_before_request(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "artifact.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "artifact_id": "artifact-cm-123",
                        "source_commit": "abc123",
                    }
                ),
                encoding="utf-8",
            )
            result = self.run_action(
                inputs={
                    "launchplane-url": "https://launchplane.example",
                    "route-path": "/v1/drivers/odoo/post-deploy",
                    "payload": '{"schema_version":1,"product":"odoo","publish":{"schema_version":1,"context":"cm"}}',
                    "payload-fields": "publish.instance=testing",
                    "payload-json-files": f"publish.manifest={manifest_path}",
                },
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = json.loads(result.stderr.strip().splitlines()[-1])
        payload = json.loads(calls[1]["body"])
        self.assertEqual(payload["publish"]["instance"], "testing")
        self.assertEqual(payload["publish"]["manifest"]["artifact_id"], "artifact-cm-123")
        self.assertEqual(payload["publish"]["manifest"]["source_commit"], "abc123")

    def test_rejects_payload_fields_without_object_payload(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/drivers/odoo/prod-rollback",
                "payload-fields": "rollback.context=cm",
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("payload-fields requires payload or payload-file", result.stderr)

    def test_rejects_payload_json_files_without_object_payload(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/drivers/odoo/post-deploy",
                "payload-json-files": "publish.manifest=artifact.json",
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("payload-json-files requires payload or payload-file", result.stderr)

    def test_fails_when_driver_result_status_is_configured_as_failure(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/drivers/generic-web/preview-refresh",
                "payload": '{"schema_version":1,"product":"sellyouroutboard"}',
            },
            environment={
                "TEST_REFRESH_STATUS": "blocked",
                "TEST_ERROR_MESSAGE": "Preview refresh blocked.",
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Preview refresh blocked.", result.stderr)

    def test_polls_until_driver_status_leaves_pending(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "github-output.txt"
            result = self.run_action(
                output_path=output_path,
                inputs={
                    "launchplane-url": "https://launchplane.example",
                    "route-path": "/v1/drivers/generic-web/preview-refresh",
                    "payload": '{"schema_version":1,"product":"sellyouroutboard"}',
                    "poll-result-path": "result.refresh_status",
                    "poll-result-statuses": "pending",
                    "poll-interval-ms": "1",
                    "poll-timeout-ms": "1000",
                    "output-paths": "refresh_status=result.refresh_status",
                },
                environment={"TEST_REFRESH_STATUSES": "pending,pending,pass"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = json.loads(result.stderr.strip().splitlines()[-1])
            launchplane_calls = [
                call
                for call in calls
                if call["url"]
                == "https://launchplane.example/v1/drivers/generic-web/preview-refresh"
            ]
            self.assertEqual(len(launchplane_calls), 3)
            self.assertEqual(
                [call["headers"]["Authorization"] for call in launchplane_calls],
                ["Bearer oidc-token-1", "Bearer oidc-token-2", "Bearer oidc-token-3"],
            )
            self.assertIn("refresh_status<<", output_path.read_text(encoding="utf-8"))

    def test_result_polling_retries_network_error_with_the_same_payload_and_key(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/drivers/launchplane/self-deploy",
                "payload": '{"deploy":{"image_reference":"synthetic"}}',
                "idempotency-key": "drain-intent",
                "poll-result-path": "result.deploy_state",
                "poll-result-statuses": "draining,dispatch_in_progress",
                "poll-retry-on-request-error": "true",
                "poll-interval-ms": "1",
                "poll-timeout-ms": "1000",
                "retry-attempts": "1",
                "log-response-body": "false",
            },
            environment={
                "TEST_RESULT_FIELD": "deploy_state",
                "TEST_REFRESH_STATUSES": "draining,draining,requested",
                "TEST_LAUNCHPLANE_NETWORK_FAILURE_ATTEMPTS": "2",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [
            call
            for call in json.loads(result.stderr.strip().splitlines()[-1])
            if call["url"].endswith("/self-deploy")
        ]
        self.assertEqual(len(calls), 3)
        self.assertEqual(len({call["body"] for call in calls}), 1)
        self.assertEqual({call["headers"]["Idempotency-Key"] for call in calls}, {"drain-intent"})

    def test_poll_timeout_retains_latest_progress_but_network_failure_clears_it(self) -> None:
        for network_failure in (False, True):
            with self.subTest(network_failure=network_failure), TemporaryDirectory() as directory:
                response_file = Path(directory) / "response.json"
                result = self.run_action(
                    inputs={
                        "launchplane-url": "https://launchplane.example",
                        "route-path": "/v1/drivers/launchplane/self-deploy",
                        "payload": "{}",
                        "poll-result-path": "result.deploy_state",
                        "poll-result-statuses": "draining",
                        "poll-interval-ms": "1",
                        "poll-timeout-ms": "1" if not network_failure else "1000",
                        "retry-attempts": "1",
                        "response-output-file": str(response_file),
                        "log-response-body": "false",
                    },
                    environment={
                        "TEST_RESULT_FIELD": "deploy_state",
                        "TEST_REFRESH_STATUS": "draining",
                        "TEST_LAUNCHPLANE_NETWORK_FAILURE_ATTEMPTS": "2" if network_failure else "",
                    },
                )
                self.assertNotEqual(result.returncode, 0)
                payload = json.loads(response_file.read_text())
                if network_failure:
                    self.assertIsNone(
                        payload, "a newer uncertain request cannot retain stale draining proof"
                    )
                else:
                    self.assertEqual(payload["result"]["deploy_state"], "draining")

    def test_fails_when_polling_times_out(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/drivers/generic-web/preview-refresh",
                "payload": '{"schema_version":1,"product":"sellyouroutboard"}',
                "poll-result-path": "result.refresh_status",
                "poll-result-statuses": "pending",
                "poll-interval-ms": "1",
                "poll-timeout-ms": "1",
            },
            environment={"TEST_REFRESH_STATUS": "pending"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Timed out waiting for Launchplane result", result.stderr)


if __name__ == "__main__":
    unittest.main()
