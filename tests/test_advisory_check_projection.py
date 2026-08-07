from __future__ import annotations

import unittest

from control_plane.advisory_check_projection import (
    AdvisoryCheckProjectionError,
    write_advisory_check_projection,
)
from control_plane.contracts.advisory_check_projection import (
    AdvisoryCheckProjection,
    OWNER_ACCEPTANCE_CHECK_NAME,
)
from control_plane.github_app_identity import GitHubAppInstallationToken


HEAD_SHA = "a" * 40
EXTERNAL_ID = "b" * 64


def _projection() -> AdvisoryCheckProjection:
    return AdvisoryCheckProjection(
        name=OWNER_ACCEPTANCE_CHECK_NAME,
        repository="example/repo",
        repository_id="123",
        head_sha=HEAD_SHA,
        external_id=EXTERNAL_ID,
        details_url="https://github.com/example/repo/pull/7",
        title="Owner acceptance: pending",
        summary="Launchplane advisory projection.",
    )


def _token() -> GitHubAppInstallationToken:
    return GitHubAppInstallationToken(
        token="secret-token",
        app_id=42,
        installation_id=77,
        repository_id=123,
        repository="example/repo",
        expires_at="2026-08-07T15:00:00Z",
    )


def _check_run(*, external_id: str = EXTERNAL_ID, app_id: int = 42) -> dict[str, object]:
    projection = _projection()
    return {
        "id": 91,
        "name": projection.name,
        "head_sha": projection.head_sha,
        "status": "completed",
        "conclusion": "neutral",
        "external_id": external_id,
        "details_url": projection.details_url,
        "output": {"title": projection.title, "summary": projection.summary},
        "app": {"id": app_id},
    }


class AdvisoryCheckProjectionTests(unittest.TestCase):
    def test_replays_identical_exact_projection_without_write(self) -> None:
        calls: list[dict[str, object]] = []

        def api_request(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            return {"check_runs": [_check_run()]}

        result = write_advisory_check_projection(
            projection=_projection(),
            installation_token=_token(),
            api_request=api_request,
        )

        self.assertEqual(result.status, "replayed")
        self.assertEqual(len(calls), 1)
        self.assertIn("check_name=launchplane%2Fowner-acceptance", str(calls[0]["path"]))

    def test_updates_same_head_when_binding_changes(self) -> None:
        calls: list[dict[str, object]] = []

        def api_request(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            if kwargs.get("method") == "PATCH":
                updated = _check_run()
                updated["external_id"] = kwargs["body"]["external_id"]
                return updated
            return {"check_runs": [_check_run(external_id="c" * 64)]}

        result = write_advisory_check_projection(
            projection=_projection(),
            installation_token=_token(),
            api_request=api_request,
        )

        self.assertEqual(result.status, "updated")
        self.assertEqual(calls[-1]["method"], "PATCH")
        self.assertEqual(calls[-1]["path"], "/repos/example/repo/check-runs/91")

    def test_rejects_check_run_written_by_another_app(self) -> None:
        def api_request(**kwargs):  # type: ignore[no-untyped-def]
            if kwargs.get("method") == "POST":
                return _check_run(app_id=99)
            return {"check_runs": []}

        with self.assertRaisesRegex(AdvisoryCheckProjectionError, "exact projection identity"):
            write_advisory_check_projection(
                projection=_projection(),
                installation_token=_token(),
                api_request=api_request,
            )

    def test_rejects_token_scoped_to_another_repository_name(self) -> None:
        token = GitHubAppInstallationToken(
            token="secret-token",
            app_id=42,
            installation_id=77,
            repository_id=123,
            repository="example/other",
            expires_at="2026-08-07T15:00:00Z",
        )

        with self.assertRaisesRegex(AdvisoryCheckProjectionError, "repository name"):
            write_advisory_check_projection(
                projection=_projection(),
                installation_token=token,
                api_request=lambda **_: {},
            )


if __name__ == "__main__":
    unittest.main()
