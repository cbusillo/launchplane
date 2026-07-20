import json
import unittest
from unittest.mock import patch
from urllib.request import Request

from control_plane.cli import _post_launchplane_service_json


class _JsonResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_JsonResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class LaunchplaneServiceHttpClientTests(unittest.TestCase):
    def test_session_cookie_post_fetches_csrf_and_sends_browser_boundary_headers(self) -> None:
        requests: list[Request] = []

        def fake_urlopen(request: Request, *, timeout: int) -> _JsonResponse:
            self.assertEqual(timeout, 30)
            requests.append(request)
            if request.get_method() == "GET":
                return _JsonResponse(
                    {
                        "status": "ok",
                        "trace_id": "launchplane_req_session",
                        "csrf_token": "v1.0.csrf-signature",
                    }
                )
            return _JsonResponse(
                {
                    "status": "accepted",
                    "trace_id": "launchplane_req_write",
                    "records": {},
                    "result": {},
                }
            )

        with patch("control_plane.cli.urlopen", side_effect=fake_urlopen):
            response = _post_launchplane_service_json(
                service_url="https://launchplane.example",
                path="/v1/authz-policies/managed-rule-sets/reconcile",
                payload={"mode": "dry_run"},
                session_cookie="launchplane_session=signed",
            )

        self.assertEqual(response["status"], "accepted")
        self.assertEqual(len(requests), 2)
        session_request, write_request = requests
        self.assertEqual(session_request.full_url, "https://launchplane.example/v1/auth/session")
        self.assertEqual(session_request.get_method(), "GET")
        self.assertEqual(write_request.get_method(), "POST")
        headers = {key.lower(): value for key, value in write_request.header_items()}
        self.assertEqual(headers["cookie"], "launchplane_session=signed")
        self.assertEqual(headers["origin"], "https://launchplane.example")
        self.assertEqual(headers["sec-fetch-site"], "same-origin")
        self.assertEqual(headers["sec-fetch-mode"], "cors")
        self.assertEqual(headers["sec-fetch-dest"], "empty")
        self.assertEqual(headers["x-csrf-token"], "v1.0.csrf-signature")

    def test_bearer_post_does_not_fetch_or_require_browser_headers(self) -> None:
        requests: list[Request] = []

        def fake_urlopen(request: Request, *, timeout: int) -> _JsonResponse:
            self.assertEqual(timeout, 30)
            requests.append(request)
            return _JsonResponse(
                {
                    "status": "accepted",
                    "trace_id": "launchplane_req_write",
                    "records": {},
                    "result": {},
                }
            )

        with patch("control_plane.cli.urlopen", side_effect=fake_urlopen):
            response = _post_launchplane_service_json(
                service_url="https://launchplane.example",
                path="/v1/product-config/apply",
                payload={"mode": "dry-run"},
                bearer_token="service-token",
            )

        self.assertEqual(response["status"], "accepted")
        self.assertEqual(len(requests), 1)
        headers = {key.lower(): value for key, value in requests[0].header_items()}
        self.assertEqual(headers["authorization"], "Bearer service-token")
        self.assertNotIn("origin", headers)
        self.assertNotIn("x-csrf-token", headers)


if __name__ == "__main__":
    unittest.main()
