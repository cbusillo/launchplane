import os
import unittest
from unittest.mock import patch

import click

from control_plane.npmplus import NpmplusClient
from control_plane.workflows.ingress_provider import (
    NPMPLUS_BASE_URL_ENV_KEY,
    NPMPLUS_IDENTITY_ENV_KEY,
    NPMPLUS_SECRET_ENV_KEY,
    NpmplusIngressProvider,
    build_npmplus_ingress_client_from_env,
    default_ingress_provider,
)


class IngressProviderTests(unittest.TestCase):
    def test_build_npmplus_ingress_client_from_env_fails_closed_when_missing_env(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(click.ClickException) as raised:
                build_npmplus_ingress_client_from_env()

        message = str(raised.exception)
        self.assertIn(NPMPLUS_BASE_URL_ENV_KEY, message)
        self.assertIn(NPMPLUS_IDENTITY_ENV_KEY, message)
        self.assertIn(NPMPLUS_SECRET_ENV_KEY, message)

    def test_build_npmplus_ingress_client_from_env_builds_client(self) -> None:
        with patch.dict(
            os.environ,
            {
                NPMPLUS_BASE_URL_ENV_KEY: "https://npmplus.example.test",
                NPMPLUS_IDENTITY_ENV_KEY: "launchplane@example.test",
                NPMPLUS_SECRET_ENV_KEY: "secret",
            },
            clear=True,
        ):
            client = build_npmplus_ingress_client_from_env()

        self.assertIsInstance(client, NpmplusClient)

    def test_default_ingress_provider_wraps_npmplus_provider(self) -> None:
        with patch.dict(
            os.environ,
            {
                NPMPLUS_BASE_URL_ENV_KEY: "https://npmplus.example.test",
                NPMPLUS_IDENTITY_ENV_KEY: "launchplane@example.test",
                NPMPLUS_SECRET_ENV_KEY: "secret",
            },
            clear=True,
        ):
            provider = default_ingress_provider()

        self.assertIsInstance(provider, NpmplusIngressProvider)
        self.assertEqual(provider.provider_id, "npmplus")
        self.assertEqual(provider.delegated_executor, "control-plane.npmplus")


if __name__ == "__main__":
    unittest.main()
