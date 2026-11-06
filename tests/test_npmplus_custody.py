import io
import json
from email.message import Message
import traceback
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from control_plane.npmplus import NpmplusClient, NpmplusCredentials
from tests.test_npmplus import _Opener


SECRET = "fictional-dns-token-never-retain-872"


class NpmplusCustodyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opener = _Opener()
        self.client = NpmplusClient(
            credentials=NpmplusCredentials(
                base_url="https://edge.example.test",
                identity="automation@example.test",
                secret=SECRET,
            ),
            opener=self.opener,
        )
        self.client.authenticate()

    def test_provider_failures_cannot_render_secret_response_or_exception_input(self) -> None:
        failures = (
            HTTPError(
                "https://edge.example.test", 400, SECRET, Message(), io.BytesIO(SECRET.encode())
            ),
            URLError(SECRET),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with patch.object(self.opener, "open", side_effect=failure):
                    try:
                        self.client.get_proxy_host(78)
                    except Exception as error:
                        self.assertNotIn(SECRET, "".join(traceback.format_exception(error)))
                    else:
                        self.fail("A failed provider response must be rejected")

    def test_malformed_provider_response_never_becomes_error_input(self) -> None:
        self.opener.host_details["id"] = SECRET
        try:
            self.client.get_proxy_host(78)
        except Exception as error:
            self.assertNotIn(SECRET, "".join(traceback.format_exception(error)))
        else:
            self.fail("Invalid provider identity must be rejected")

    def test_host_read_discards_provider_metadata_before_serialization(self) -> None:
        self.opener.host_details["meta"] = {"dns_provider_credentials": SECRET}
        self.opener.host_details["npmplus_dns_provider_credentials"] = SECRET
        host = self.client.get_proxy_host(78)
        self.assertNotIn(SECRET, host.model_dump_json())
        self.assertNotIn(SECRET, repr(host))

    def test_credentials_repr_does_not_disclose_authentication_secret(self) -> None:
        credentials = NpmplusCredentials(
            base_url="https://edge.example.test", identity="automation@example.test", secret=SECRET
        )
        self.assertNotIn(SECRET, repr(credentials))

    def test_non_json_response_is_rejected_without_retaining_body(self) -> None:
        self.opener.token_payload = SECRET.encode()
        client = NpmplusClient(
            credentials=NpmplusCredentials(
                base_url="https://edge.example.test",
                identity="automation@example.test",
                secret=SECRET,
            ),
            opener=self.opener,
        )
        try:
            client.authenticate()
        except Exception as error:
            self.assertNotIn(SECRET, "".join(traceback.format_exception(error)))
        else:
            self.fail("Malformed authentication responses must be rejected")

    def test_certificate_metadata_drops_credential_on_existing_read_path(self) -> None:
        self.opener.certificate_payload["meta"] = {"dns_provider_credentials": SECRET}
        self.opener.certificate_payload["npmplus_dns_provider_credentials"] = SECRET
        self.assertNotIn(SECRET, json.dumps(self.client.get_certificate(47).model_dump()))
