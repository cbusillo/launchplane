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
        self.other_hosts_payload: object = [
            {"domain_names": ["claimed.example.test"], "enabled": True},
            {"domain_names": ["disabled.example.test"], "enabled": False},
        ]
        self.host_details: dict[str, object] = {
            **_proxy_host_payload(id=78),
            "npmplus_nginx_online": True,
            "npmplus_mtls_certificate_id": 0,
            "npmplus_mtls_verify_client_optional": False,
            "npmplus_mtls_bypass_auth_request": False,
            "npmplus_auth_request_upstream": "",
            "npmplus_location_config": "",
            "npmplus_directory": "",
        }
        self.certificate_payload = {
            "id": 47,
            "domain_names": ["*.example.test"],
            "expires_on": "2027-01-01 00:00:00",
            "meta": {"dns_provider_credentials": "fixture-secret"},
        }
        self.access_list_payload = {
            "id": 1,
            "name": "Private testing",
            "satisfy_any": False,
            "pass_auth": False,
            "items": [{"username": "fixture", "password": "fixture-secret"}],
        }

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
            if set(payload) & {"meta", "enabled", "http2_support", "trust_forwarded_proto"}:
                raise AssertionError("Current NPMplus rejects legacy request properties")
            return _Response(_proxy_host_response(payload))
        if method == "GET" and path == "/api/nginx/proxy-hosts/78":
            return _Response({**self.host_details, "npmplus_noindex": True})
        if method == "GET" and path == "/api/nginx/proxy-hosts":
            return _Response(self.proxy_hosts_payload)
        if method == "GET" and path in ("/api/nginx/redirection-hosts", "/api/nginx/dead-hosts"):
            return _Response(self.other_hosts_payload)
        if method == "GET" and path == f"/api/nginx/certificates/{self.certificate_payload['id']}":
            return _Response(self.certificate_payload)
        if method == "GET" and path == "/api/nginx/access-lists/1":
            return _Response(self.access_list_payload)
        if method == "PUT" and path == "/api/nginx/proxy-hosts/78":
            assert isinstance(payload, dict)
            return _Response(_proxy_host_response(payload))
        if method == "POST" and path in (
            "/api/nginx/proxy-hosts/78/disable",
            "/api/nginx/proxy-hosts/78/enable",
        ):
            return _Response(True)
        if method == "DELETE" and path == "/api/nginx/proxy-hosts/78":
            return _Response(True)
        raise AssertionError(f"Unexpected request: {method} {path}")


def _access_list_response(payload: dict[str, object]) -> dict[str, object]:
    access_list_ids = payload["npmplus_access_list_ids"]
    assert isinstance(access_list_ids, list)
    return {
        **{key: value for key, value in payload.items() if key != "npmplus_access_list_ids"},
        "access_lists": [{"id": access_list_id} for access_list_id in access_list_ids],
    }


def _proxy_host_response(payload: dict[str, object]) -> dict[str, object]:
    locations = payload["locations"]
    assert isinstance(locations, list)
    return {
        **_access_list_response(payload),
        "id": 78,
        "locations": [_access_list_response(location) for location in locations],
    }


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

    def test_proxy_host_response_maps_public_access_list_fields(self) -> None:
        host = NpmplusProxyHost.model_validate(
            {
                **_proxy_host_payload(id=78),
                "npmplus_access_list_ids": [],
                "npmplus_access_list_type": "public",
            }
        )

        self.assertEqual(host.access_list_id, 0)

    def test_proxy_host_response_maps_single_custom_access_list(self) -> None:
        host = NpmplusProxyHost.model_validate(
            {
                **_proxy_host_payload(id=78),
                "npmplus_access_list_ids": [42],
                "npmplus_access_list_type": "custom",
            }
        )

        self.assertEqual(host.access_list_id, 42)

    def test_proxy_host_response_rejects_incomplete_access_list_fields(self) -> None:
        with self.assertRaises(ValidationError):
            NpmplusProxyHost.model_validate(
                {
                    **_proxy_host_payload(id=78),
                    "npmplus_access_list_ids": [],
                }
            )

    def test_proxy_host_response_requires_explicit_legacy_access_list(self) -> None:
        payload = _proxy_host_payload(id=78)
        del payload["access_list_id"]
        with self.assertRaises(ValidationError):
            NpmplusProxyHost.model_validate(payload)

    def test_proxy_host_response_rejects_unverified_access_list_relations(self) -> None:
        cases: tuple[dict[str, object], ...] = (
            {"npmplus_access_list_type": "custom", "access_lists": []},
            {"npmplus_access_list_type": "custom", "access_lists": [{"id": 42}, {"id": 43}]},
            {"npmplus_access_list_type": "custom", "access_lists": [{"id": 42}, {"id": 42}]},
            {"npmplus_access_list_type": "public", "access_lists": [{"id": 42}]},
            {"npmplus_access_list_type": "custom", "access_lists": None},
            {"npmplus_access_list_type": "custom", "access_lists": [{"name": "staff"}]},
            {"npmplus_access_list_type": "custom", "access_lists": [42]},
            {"npmplus_access_list_type": "custom", "access_lists": [{"id": True}]},
            {"npmplus_access_list_type": "custom", "access_lists": [{"id": 0}]},
            {"npmplus_access_list_type": "custom", "access_lists": [{"id": "42"}]},
            {"access_lists": []},
            {"npmplus_access_list_type": "custom"},
            {
                "npmplus_access_list_type": "custom",
                "npmplus_access_list_ids": [42],
                "access_lists": [{"id": 43}],
            },
            {
                "npmplus_access_list_type": "custom",
                "npmplus_access_list_ids": [True],
                "access_lists": [{"id": 1}],
            },
        )
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                NpmplusProxyHost.model_validate({**_proxy_host_payload(id=78), **fields})

    def test_proxy_host_response_rejects_multiple_custom_access_lists(self) -> None:
        with self.assertRaises(ValidationError):
            NpmplusProxyHost.model_validate(
                {
                    **_proxy_host_payload(id=78),
                    "npmplus_access_list_ids": [42, 43],
                    "npmplus_access_list_type": "custom",
                }
            )

    def test_location_response_rejects_unrepresented_access_list_policies(self) -> None:
        location = {"path": "/app", "forward_scheme": "http", "forward_host": "192.0.2.11"}
        cases: tuple[dict[str, object], ...] = (
            {"npmplus_access_list_type": "custom", "access_lists": [{"id": 42}]},
            {"npmplus_access_list_type": "public", "access_lists": []},
            {"npmplus_access_list_type": "global", "access_lists": [{"id": 42}]},
            {"npmplus_access_list_type": "global"},
            {"access_lists": []},
            {"npmplus_access_list_type": "global", "npmplus_access_list_ids": [42]},
            {"npmplus_access_list_type": "global", "access_lists": None},
        )
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                NpmplusLocation.model_validate({**location, **fields})

    def test_location_inherited_access_list_readback_preserves_receipt_payload(self) -> None:
        location = {"path": "/app", "forward_scheme": "http", "forward_host": "192.0.2.11"}
        observed = NpmplusLocation.model_validate(
            {**location, "npmplus_access_list_type": "global", "access_lists": []}
        )
        self.assertEqual(
            observed.model_dump(), NpmplusLocation.model_validate(location).model_dump()
        )

    def test_location_write_and_readback_preserve_host_acl_inheritance(self) -> None:
        payload = NpmplusProxyHostPayload.model_validate(
            {
                **_proxy_host_payload(),
                "access_list_id": 42,
                "locations": [
                    {"path": "/app", "forward_scheme": "http", "forward_host": "192.0.2.11"}
                ],
            }
        )
        wire = payload.to_api_payload()
        locations = wire["locations"]
        assert isinstance(locations, list) and isinstance(locations[0], dict)
        self.assertEqual(locations[0]["npmplus_access_list_type"], "global")
        self.assertEqual(locations[0]["npmplus_access_list_ids"], [])
        client = NpmplusClient(credentials=_credentials(), opener=_Opener())
        created = client.create_proxy_host(payload)
        self.assertEqual(created.locations[0].model_dump(), payload.locations[0].model_dump())

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

    def test_proxy_host_api_payload_maps_access_list_fields(self) -> None:
        public_payload = NpmplusProxyHostPayload.model_validate(_proxy_host_payload())
        restricted_payload = NpmplusProxyHostPayload.model_validate(
            {**_proxy_host_payload(), "access_list_id": 42}
        )

        public_api_payload = public_payload.to_api_payload()
        restricted_api_payload = restricted_payload.to_api_payload()

        self.assertNotIn("access_list_id", public_api_payload)
        self.assertEqual(public_api_payload["npmplus_access_list_ids"], [])
        self.assertEqual(public_api_payload["npmplus_access_list_type"], "public")
        self.assertEqual(restricted_api_payload["npmplus_access_list_ids"], [42])
        self.assertEqual(restricted_api_payload["npmplus_access_list_type"], "custom")


class NpmplusClientTests(unittest.TestCase):
    def test_selected_host_read_maps_related_access_lists(self) -> None:
        for access_type, access_lists in (("public", []), ("custom", [{"id": 42}])):
            with self.subTest(access_type=access_type):
                opener = _Opener()
                del opener.host_details["access_list_id"]
                opener.host_details.update(
                    npmplus_access_list_type=access_type,
                    access_lists=access_lists,
                )
                client = NpmplusClient(credentials=_credentials(), opener=opener)
                host = client.get_proxy_host(78, public_only=True)
                self.assertEqual(host.access_list_id, access_lists[0]["id"] if access_lists else 0)
                self.assertNotIn("access_lists", host.model_dump())

    def test_collection_preserves_foreign_claims_without_route_qualification(self) -> None:
        owned = _proxy_host_payload(id=78)
        cases = (
            {"npmplus_access_list_type": "custom", "npmplus_access_list_ids": [1, 2]},
            {"npmplus_auth_request": "oauth2proxy"},
            {"forward_scheme": "empty", "forward_host": ""},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                names = ("other.example.test",)
                foreign = {**_proxy_host_payload(id=79), "domain_names": list(names), **changes}
                opener = _Opener()
                opener.proxy_hosts_payload = [owned, foreign]
                client = NpmplusClient(credentials=_credentials(), opener=opener)
                claims = client.list_proxy_hosts()
                self.assertEqual([host.id for host in claims], [owned["id"], foreign["id"]])
                self.assertEqual(claims[1].domain_names, names)
                self.assertEqual(
                    client.get_proxy_host(78).domain_names,
                    NpmplusProxyHost.model_validate(owned).domain_names,
                )

    def test_selected_host_read_rejects_a_different_id(self) -> None:
        opener = _Opener()
        opener.host_details["id"] = 79
        client = NpmplusClient(credentials=_credentials(), opener=opener)
        with self.assertRaises(click.ClickException):
            client.get_proxy_host(78)

    def test_access_list_read_checks_identity_and_discards_authentication_entries(self) -> None:
        opener = _Opener()
        client = NpmplusClient(credentials=_credentials(), opener=opener)
        access_list = client.get_access_list(1)
        self.assertEqual(access_list.id, opener.access_list_payload["id"])
        self.assertNotIn("fixture-secret", access_list.model_dump_json())
        opener.access_list_payload["id"] = 2
        with self.assertRaises(click.ClickException):
            client.get_access_list(1)

    def test_disabled_creation_refuses_before_exposing_a_temporarily_enabled_host(self) -> None:
        opener = _Opener()
        client = NpmplusClient(credentials=_credentials(), opener=opener)
        with self.assertRaises(click.ClickException):
            client.create_proxy_host(
                NpmplusProxyHostPayload.model_validate({**_proxy_host_payload(), "enabled": False})
            )
        self.assertEqual(opener.requests, [])

    def test_public_read_refuses_private_tls_and_unloaded_configuration(self) -> None:
        opener = _Opener()
        client = NpmplusClient(credentials=_credentials(), opener=opener)
        self.assertTrue(client.get_proxy_host(78, public_only=True).enabled)
        for field, value in (
            ("npmplus_mtls_certificate_id", 12),
            ("npmplus_nginx_online", False),
            ("npmplus_location_config", "return 403;"),
        ):
            with self.subTest(field=field):
                original = opener.host_details[field]
                opener.host_details[field] = value
                with self.assertRaises(click.ClickException):
                    client.get_proxy_host(78, public_only=True)
                opener.host_details[field] = original

    def test_other_host_claims_are_read_before_any_proxy_write(self) -> None:
        opener = _Opener()
        client = NpmplusClient(credentials=_credentials(), opener=opener)
        domains = client.list_other_host_domains()
        self.assertIn("claimed.example.test", domains)
        self.assertNotIn("disabled.example.test", domains)
        self.assertFalse(
            any(
                method != "GET" and path.endswith("proxy-hosts")
                for method, path, _ in opener.requests
            )
        )

    def test_disabled_public_read_keeps_hidden_configuration_checks(self) -> None:
        opener = _Opener()
        client = NpmplusClient(credentials=_credentials(), opener=opener)
        opener.host_details.update(enabled=False, npmplus_nginx_online=False)
        self.assertFalse(client.get_proxy_host(78, public_only=True).enabled)
        for field, value in (
            ("npmplus_mtls_certificate_id", 12),
            ("npmplus_location_config", "return 403;"),
            ("npmplus_auth_request_upstream", "http://other-auth"),
        ):
            with self.subTest(field=field):
                original = opener.host_details[field]
                opener.host_details[field] = value
                with self.assertRaises(click.ClickException):
                    client.get_proxy_host(78, public_only=True)
                opener.host_details[field] = original

    def test_removed_protocol_settings_refuse_before_transport(self) -> None:
        opener = _Opener()
        client = NpmplusClient(credentials=_credentials(), opener=opener)
        with self.assertRaises(ValueError):
            client.create_proxy_host(
                NpmplusProxyHostPayload.model_validate(
                    {**_proxy_host_payload(), "trust_forwarded_proto": True}
                )
            )
        self.assertFalse(any(path.endswith("proxy-hosts") for _, path, _ in opener.requests))

    def test_certificate_reader_retains_only_public_metadata(self) -> None:
        opener = _Opener()
        client = NpmplusClient(credentials=_credentials(), opener=opener)
        certificate = client.get_certificate(cast(int, opener.certificate_payload["id"]))
        self.assertEqual(
            certificate.domain_names,
            tuple(cast(list[str], opener.certificate_payload["domain_names"])),
        )
        self.assertNotIn("meta", certificate.model_dump())
        self.assertNotIn("fixture-secret", certificate.model_dump_json())

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
        valid = _proxy_host_payload(id=78)
        cases = (
            "not-a-host",
            {key: value for key, value in valid.items() if key != "id"},
            {**valid, "id": 0},
            {**valid, "domain_names": []},
            {**valid, "domain_names": [" "]},
            {**valid, "domain_names": "hidden.example.test"},
        )
        for malformed in cases:
            with self.subTest(malformed=malformed):
                opener = _Opener()
                opener.proxy_hosts_payload = [valid, malformed]
                client = NpmplusClient(credentials=_credentials(), opener=opener)
                with self.assertRaises((click.ClickException, ValueError)):
                    client.list_proxy_hosts()

    def test_selected_host_read_normalizes_null_locations(self) -> None:
        opener = _Opener()
        opener.host_details["locations"] = None
        client = NpmplusClient(credentials=_credentials(), opener=opener)
        self.assertEqual(client.get_proxy_host(78).locations, ())

    def test_collection_retains_duplicate_wildcard_and_regex_claims(self) -> None:
        domains = (
            ["owned.example.test"],
            ["owned.example.test", "alias.example.test"],
            ["*.example.test"],
            ["~.*"],
        )
        opener = _Opener()
        payloads = [
            {
                **_proxy_host_payload(id=index + 1),
                "domain_names": names,
                "npmplus_auth_request": "oauth2proxy",
            }
            for index, names in enumerate(domains)
        ]
        opener.proxy_hosts_payload = payloads
        client = NpmplusClient(credentials=_credentials(), opener=opener)
        claims = client.list_proxy_hosts()
        self.assertEqual([claim.id for claim in claims], [payload["id"] for payload in payloads])
        self.assertEqual(
            [claim.domain_names for claim in claims], [tuple(names) for names in domains]
        )

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
