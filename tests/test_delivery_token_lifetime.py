"""Operation credentials are revoked before control returns to a caller."""

import asyncio
from pathlib import Path
import unittest
from unittest.mock import patch

import click

from control_plane.workflows.launchplane import (
    _resolve_companion_sources,
    launchplane_github_token,
)
from control_plane.contracts.preview_request_metadata import LaunchplanePreviewRequestMetadata
from tests import test_launchplane_github_delivery as delivery_tests


class DeliveryTokenLifetimeTests(unittest.TestCase):
    def test_validated_token_is_revoked_after_success_failure_and_interruption(self) -> None:
        fixture = delivery_tests.DeliveryGitHubTokenTests()
        fixture.setUpClass()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for failure in (
            None,
            click.ClickException("provider failure"),
            KeyboardInterrupt(),
            asyncio.CancelledError(),
        ):
            with self.subTest(failure=type(failure).__name__):
                fixture.revoked = False
                observed: BaseException | None = None
                try:
                    with launchplane_github_token(
                        control_plane_root=fixture.root,
                        context_name="site",
                        repository="example/site",
                        purpose="workflow_dispatch",
                        token_resolver=lambda **kwargs: fixture.resolve(kwargs["purpose"]),
                        api_request=fixture.provider,
                    ) as token:
                        self.assertEqual(token, "installation-token")
                        self.assertFalse(fixture.revoked)
                        if failure is not None:
                            raise failure
                except BaseException as error:
                    observed = error
                self.assertIs(observed, failure)
                self.assertTrue(fixture.revoked)

    def test_revocation_failure_preserves_provider_result_and_exception_without_secret_logging(
        self,
    ) -> None:
        token = "fixture-private-token"
        revoked: list[str] = []

        def unavailable(**kwargs: object) -> object:
            value = kwargs["token"]
            assert isinstance(value, str)
            revoked.append(value)
            raise click.ClickException(f"{token} https://private.example/secret")

        failure = ValueError("provider operation failed")
        for error in (None, failure):
            with (
                self.subTest(error=error),
                self.assertLogs("control_plane.workflows.launchplane", level="WARNING") as logs,
            ):
                observed: ValueError | None = None
                try:
                    with launchplane_github_token(
                        control_plane_root=Path("."),
                        context_name="site",
                        token_resolver=lambda **kwargs: token,
                        api_request=unavailable,
                    ) as leased:
                        self.assertEqual(leased, token)
                        if error:
                            raise error
                except ValueError as caught:
                    observed = caught
                self.assertIs(observed, error)
                self.assertNotIn(token, " ".join(logs.output))
                self.assertNotIn("private.example", " ".join(logs.output))
        self.assertEqual(revoked, [token, token])

    def test_no_credential_or_failed_mint_does_not_revoke_an_unowned_token(self) -> None:
        for value in ("", click.ClickException("mint unavailable")):
            with self.subTest(value=value):
                with patch("control_plane.workflows.launchplane.github_api_request") as provider:

                    def resolver(**_kwargs: object) -> str:
                        if isinstance(value, click.ClickException):
                            raise value
                        return value

                    try:
                        with launchplane_github_token(
                            control_plane_root=Path("."),
                            context_name="site",
                            token_resolver=resolver,
                        ) as token:
                            self.assertEqual(token, "")
                    except click.ClickException as error:
                        self.assertIs(error, value)
                    provider.assert_not_called()

    def test_companion_revokes_its_own_token_after_success_or_failed_read(self) -> None:
        metadata = LaunchplanePreviewRequestMetadata.model_validate(
            {"companions": [{"repo": "shared-addons", "pr_number": 11}]}
        )
        for failed_repository in ("", "shared-addons"):
            with self.subTest(failed_repository=failed_repository):
                active: set[str] = set()
                revoked: list[str] = []

                def mint(**kwargs: object) -> str:
                    token = kwargs["repository"]
                    assert isinstance(token, str)
                    active.add(token)
                    return token

                def provider(**kwargs: object) -> object:
                    token = kwargs["token"]
                    assert isinstance(token, str)
                    self.assertIn(token, active)
                    if kwargs.get("method") == "DELETE":
                        self.assertEqual(kwargs["path"], "/installation/token")
                        active.remove(token)
                        revoked.append(token)
                        return None
                    request_path = kwargs["path"]
                    assert isinstance(request_path, str)
                    if request_path.startswith(f"/repos/example/{failed_repository}/"):
                        raise click.ClickException("companion provider unavailable")
                    return {
                        "head": {"sha": "a" * 40},
                        "html_url": f"https://github.com/{token}/pull/11",
                    }

                with (
                    patch(
                        "control_plane.workflows.launchplane.resolve_launchplane_github_token",
                        side_effect=mint,
                    ),
                    patch(
                        "control_plane.workflows.launchplane.github_api_request",
                        side_effect=provider,
                    ),
                ):
                    sources, summaries = _resolve_companion_sources(
                        control_plane_root=Path("."),
                        context_name="site",
                        anchor_pr_url="https://github.com/example/site/pull/1",
                        metadata=metadata,
                    )
                self.assertEqual(active, set())
                self.assertEqual(revoked, ["example/shared-addons"])
                if failed_repository:
                    self.assertIsNone(sources)
                else:
                    assert sources is not None and summaries is not None
                    self.assertEqual(tuple(item.repo for item in sources), ("shared-addons",))
