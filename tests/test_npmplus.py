import json
import unittest
from typing import cast
from urllib.request import Request

import click
from pydantic import ValidationError

from control_plane.npmplus import (
    NpmplusClient,
    NpmplusCredentials,
    NpmplusLocation,
    NpmplusLocationPayload,
    NpmplusProxyHost,
    NpmplusProxyHostPayload,
)


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode()


class _Opener:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object | None]] = []
        self.token_payload: object = {"expires": "2026-06-01T01:17:27.592Z"}
        self.proxy_hosts_payload: object = [_proxy_host_payload(id=78)]

    def open(self, request: Request, timeout: int | float) -> _Response:
        body_bytes = cast(bytes | None, request.data)
        body = body_bytes.decode() if body_bytes else None
        payload = json.loads(body) if body else None
        self.requests.append((request.get_method(), request.full_url, payload))
        path = request.selector
        method = request.get_method()

        if method == "POST" and path == "/api/tokens":
            return _Response(self.token_payload)
        if method == "POST" and path == "/api/nginx/proxy-hosts":
            assert isinstance(payload, dict)
            return _Response({"id": 78, **payload})
        if method == "GET" and path == "/api/nginx/proxy-hosts/78":
            return _Response(_proxy_host_payload(id=78, enabled=True, npmplus_noindex=True))
        if method == "GET" and path == "/api/nginx/proxy-hosts":
            return _Response(self.proxy_hosts_payload)
        if method == "PUT" and path == "/api/nginx/proxy-hosts/78":
            assert isinstance(payload, dict)
            return _Response({"id": 78, **payload})
        if method == "POST" and path in (
            "/api/nginx/proxy-hosts/78/disable",
            "/api/nginx/proxy-hosts/78/enable",
        ):
            return _Response(True)
        if method == "DELETE" and path == "/api/nginx/proxy-hosts/78":
            return _Response(True)
        raise AssertionError(f"Unexpected request: {method} {path}")


def _credentials() -> NpmplusCredentials:
    return NpmplusCredentials(
        base_url="https://npmplus.example.test/",
        identity="launchplane-ingress@example.test",
        secret="secret",
    )


def _proxy_host_payload(
    *, id: int | None = None, enabled: bool = True, npmplus_noindex: bool = False
) -> dict[str, object]:
    payload: dict[str, object] = {
        "domain_names": ["ingress-canary.example.test"],
        "forward_scheme": "http",
        "forward_host": "192.0.2.10",
        "forward_port": 8123,
        "certificate_id": 47,
        "ssl_forced": True,
        "hsts_enabled": False,
        "hsts_subdomains": False,
        "trust_forwarded_proto": False,
        "http2_support": True,
        "npmplus_http3_support": True,
        "access_list_id": 0,
        "npmplus_noindex": npmplus_noindex,
        "npmplus_crowdsec_appsec": False,
        "npmplus_proxy_request_buffering": False,
        "npmplus_proxy_response_buffering": False,
        "npmplus_upstream_compression": False,
        "npmplus_fancyindex": False,
        "npmplus_x_frame_options": "SAMEORIGIN",
        "npmplus_auth_request": "none",
        "advanced_config": "",
        "enabled": enabled,
        "meta": {},
        "locations": [],
    }
    if id is not None:
        payload["id"] = id
    return payload


class NpmplusProxyHostPayloadTests(unittest.TestCase):
    def test_normalizes_domain_names(self) -> None:
        payload = NpmplusProxyHostPayload.model_validate(
            {
                **_proxy_host_payload(),
                "domain_names": [" Ingress-Canary.Example.Test ", "ingress-canary.example.test"],
            }
        )

        self.assertEqual(payload.domain_names, ("ingress-canary.example.test",))

    def test_rejects_empty_domain_names(self) -> None:
        with self.assertRaises(ValidationError):
            NpmplusProxyHostPayload.model_validate({**_proxy_host_payload(), "domain_names": []})

    def test_proxy_host_response_ignores_unknown_api_fields(self) -> None:
        host = NpmplusProxyHost.model_validate(
            {**_proxy_host_payload(id=78), "future_npmplus_field": True}
        )

        self.assertEqual(host.id, 78)
        self.assertEqual(host.domain_names, ("ingress-canary.example.test",))

    def test_location_payload_rejects_unknown_request_fields(self) -> None:
        with self.assertRaises(ValidationError):
            NpmplusLocationPayload.model_validate(
                {
                    "path": "/ws",
                    "forward_scheme": "http",
                    "forward_host": "192.0.2.11",
                    "forward_port": 9000,
                    "created_on": "2026-05-31T03:00:00Z",
                }
            )

    def test_location_response_ignores_unknown_api_fields(self) -> None:
        location = NpmplusLocation.model_validate(
            {
                "path": "/ws",
                "forward_scheme": "http",
                "forward_host": "192.0.2.11",
                "forward_port": 9000,
                "created_on": "2026-05-31T03:00:00Z",
            }
        )

        self.assertEqual(location.path, "/ws")

    def test_proxy_host_api_payload_excludes_location_ids(self) -> None:
        payload = NpmplusProxyHostPayload.model_validate(
            {
                **_proxy_host_payload(),
                "locations": [
                    {
                        "id": 42,
                        "path": "/ws",
                        "forward_scheme": "http",
                        "forward_host": "192.0.2.11",
                        "forward_port": 9000,
                    }
                ],
            }
        )

        api_payload = payload.to_api_payload()
        locations = api_payload["locations"]
        self.assertIsInstance(locations, list)
        assert isinstance(locations, list)
        location = cast(dict[str, object], locations[0])
        self.assertNotIn("id", location)


class NpmplusClientTests(unittest.TestCase):
    def test_create_proxy_host_authenticates_and_posts_payload(self) -> None:
        opener = _Opener()
        client = NpmplusClient(credentials=_credentials(), opener=opener)

        created = client.create_proxy_host(
            NpmplusProxyHostPayload.model_validate(_proxy_host_payload())
        )

        self.assertEqual(created.id, 78)
        self.assertEqual(created.domain_names, ("ingress-canary.example.test",))
        self.assertEqual(opener.requests[0][0], "POST")
        self.assertEqual(opener.requests[0][1], "https://npmplus.example.test/api/tokens")
        self.assertEqual(opener.requests[1][0], "POST")
        self.assertEqual(
            opener.requests[1][1], "https://npmplus.example.test/api/nginx/proxy-hosts"
        )

    def test_disable_and_enable_reread_proxy_host_after_boolean_response(self) -> None:
        opener = _Opener()
        client = NpmplusClient(credentials=_credentials(), opener=opener)

        disabled = client.disable_proxy_host(78)
        enabled = client.enable_proxy_host(78)

        self.assertTrue(disabled.enabled)
        self.assertTrue(enabled.enabled)
        self.assertEqual(
            [request[1] for request in opener.requests],
            [
                "https://npmplus.example.test/api/tokens",
                "https://npmplus.example.test/api/nginx/proxy-hosts/78/disable",
                "https://npmplus.example.test/api/nginx/proxy-hosts/78",
                "https://npmplus.example.test/api/nginx/proxy-hosts/78/enable",
                "https://npmplus.example.test/api/nginx/proxy-hosts/78",
            ],
        )

    def test_update_proxy_host_puts_full_payload(self) -> None:
        opener = _Opener()
        client = NpmplusClient(credentials=_credentials(), opener=opener)
        payload = NpmplusProxyHostPayload.model_validate(
            {**_proxy_host_payload(), "npmplus_noindex": True}
        )

        updated = client.update_proxy_host(host_id=78, payload=payload)

        self.assertTrue(updated.npmplus_noindex)
        method, url, request_payload = opener.requests[-1]
        self.assertEqual(method, "PUT")
        self.assertEqual(url, "https://npmplus.example.test/api/nginx/proxy-hosts/78")
        self.assertIsInstance(request_payload, dict)
        assert isinstance(request_payload, dict)
        self.assertTrue(request_payload["npmplus_noindex"])

    def test_delete_proxy_host_calls_delete_endpoint(self) -> None:
        opener = _Opener()
        client = NpmplusClient(credentials=_credentials(), opener=opener)

        client.delete_proxy_host(78)

        self.assertEqual(opener.requests[-1][0], "DELETE")
        self.assertEqual(
            opener.requests[-1][1], "https://npmplus.example.test/api/nginx/proxy-hosts/78"
        )

    def test_authentication_rejects_2fa_challenge(self) -> None:
        opener = _Opener()
        opener.token_payload = {"requires_2fa": True, "message": "Enter OTP"}
        client = NpmplusClient(credentials=_credentials(), opener=opener)

        with self.assertRaises(click.ClickException):
            client.list_proxy_hosts()

        self.assertEqual([request[0] for request in opener.requests], ["POST"])

    def test_list_proxy_hosts_rejects_malformed_entries(self) -> None:
        opener = _Opener()
        opener.proxy_hosts_payload = [_proxy_host_payload(id=78), "not-a-host"]
        client = NpmplusClient(credentials=_credentials(), opener=opener)

        with self.assertRaises(click.ClickException):
            client.list_proxy_hosts()

    def test_list_proxy_hosts_normalizes_null_locations(self) -> None:
        opener = _Opener()
        opener.proxy_hosts_payload = [{**_proxy_host_payload(id=78), "locations": None}]
        client = NpmplusClient(credentials=_credentials(), opener=opener)

        proxy_hosts = client.list_proxy_hosts()

        self.assertEqual(proxy_hosts[0].locations, ())

    def test_credentials_fail_closed_for_missing_secret(self) -> None:
        with self.assertRaises(ValueError):
            NpmplusCredentials(base_url="https://npmplus.example.test", identity="user", secret="")

    def test_credentials_strip_operator_whitespace(self) -> None:
        credentials = NpmplusCredentials(
            base_url=" https://npmplus.example.test/ ",
            identity=" launchplane-ingress@example.test ",
            secret=" secret ",
        )

        self.assertEqual(credentials.base_url, "https://npmplus.example.test")
        self.assertEqual(credentials.normalized_base_url, "https://npmplus.example.test")
        self.assertEqual(credentials.identity, "launchplane-ingress@example.test")
        self.assertEqual(credentials.secret, "secret")


if __name__ == "__main__":
    unittest.main()
