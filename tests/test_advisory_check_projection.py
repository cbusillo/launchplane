from __future__ import annotations

import unittest

from control_plane.advisory_check_projection import (
    AdvisoryCheckProjectionError,
    write_advisory_check_projection,
)
from control_plane.contracts.change_impact import ChangeImpactTarget
from control_plane.contracts.advisory_check_projection import (
    AdvisoryCheckConclusion,
    AdvisoryCheckProjection,
    AdvisoryCheckRunStatus,
    OWNER_ACCEPTANCE_CHECK_NAME,
)
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.owner_acceptance_projection import owner_acceptance_workbench_url


HEAD_SHA = "a" * 40
EXTERNAL_ID = "b" * 64


def _projection(
    *,
    check_status: AdvisoryCheckRunStatus = "completed",
    conclusion: AdvisoryCheckConclusion | None = "neutral",
) -> AdvisoryCheckProjection:
    return AdvisoryCheckProjection(
        name=OWNER_ACCEPTANCE_CHECK_NAME,
        repository="example/repo",
        repository_id="123",
        head_sha=HEAD_SHA,
        external_id=EXTERNAL_ID,
        details_url="https://github.com/example/repo/pull/7",
        title="Owner acceptance: pending",
        summary="Launchplane advisory projection.",
        check_status=check_status,
        conclusion=conclusion,
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


def _check_run(
    *,
    external_id: str = EXTERNAL_ID,
    app_id: int = 42,
    check_status: AdvisoryCheckRunStatus = "completed",
    conclusion: AdvisoryCheckConclusion | None = "neutral",
) -> dict[str, object]:
    projection = _projection(check_status=check_status, conclusion=conclusion)
    return {
        "id": 91,
        "name": projection.name,
        "head_sha": projection.head_sha,
        "status": check_status,
        "conclusion": conclusion,
        "external_id": external_id,
        "details_url": projection.details_url,
        "output": {"title": projection.title, "summary": projection.summary},
        "app": {"id": app_id},
    }


class AdvisoryCheckProjectionTests(unittest.TestCase):
    def test_projection_requires_completed_checks_to_have_a_conclusion(self) -> None:
        with self.assertRaisesRegex(ValueError, "require a conclusion"):
            _projection(conclusion=None)

    def test_projection_forbids_in_progress_checks_from_having_a_conclusion(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot have a conclusion"):
            _projection(check_status="in_progress")

    def test_owner_acceptance_details_url_is_server_owned_and_encoded(self) -> None:
        target = ChangeImpactTarget(
            repository_id="123",
            repository_owner_id="456",
            repository="example/repo",
            pull_request_number=7,
            head_sha=HEAD_SHA,
            tree_sha="c" * 40,
        )

        self.assertEqual(
            owner_acceptance_workbench_url(
                public_origin="https://ops.example.test/",
                target=target,
            ),
            "https://ops.example.test/ui/engineering/owner-acceptance?repository=example%2Frepo&pull_request=7",
        )

    def test_owner_acceptance_details_url_rejects_invalid_origin(self) -> None:
        target = ChangeImpactTarget(
            repository_id="123",
            repository_owner_id="456",
            repository="example/repo",
            pull_request_number=7,
            head_sha=HEAD_SHA,
            tree_sha="c" * 40,
        )

        with self.assertRaisesRegex(ValueError, "valid browser public origin"):
            owner_acceptance_workbench_url(
                public_origin="https://github.com/example/repo/pull/7",
                target=target,
            )

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
        self.assertEqual(result.schema_version, 2)
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

    def test_replays_identical_in_progress_projection_without_write(self) -> None:
        calls: list[dict[str, object]] = []
        projection = _projection(check_status="in_progress", conclusion=None)

        def api_request(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            return {"check_runs": [_check_run(check_status="in_progress", conclusion=None)]}

        result = write_advisory_check_projection(
            projection=projection,
            installation_token=_token(),
            api_request=api_request,
        )

        self.assertEqual(result.status, "replayed")
        self.assertEqual(result.check_status, "in_progress")
        self.assertIsNone(result.conclusion)
        self.assertEqual(len(calls), 1)

    def test_creates_fresh_in_progress_run_after_completed_run(self) -> None:
        calls: list[dict[str, object]] = []
        projection = _projection(check_status="in_progress", conclusion=None)

        def api_request(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            if kwargs.get("method") == "POST":
                body = kwargs["body"]
                self.assertNotIn("conclusion", body)
                return {
                    "id": 92,
                    **body,
                    "conclusion": None,
                    "app": {"id": 42},
                }
            return {"check_runs": [_check_run()]}

        result = write_advisory_check_projection(
            projection=projection,
            installation_token=_token(),
            api_request=api_request,
        )

        self.assertEqual(result.status, "projected")
        self.assertEqual(result.check_status, "in_progress")
        self.assertEqual(calls[-1]["method"], "POST")

    def test_replays_exact_pending_run_even_when_completed_run_is_listed_first(self) -> None:
        calls: list[dict[str, object]] = []
        projection = _projection(check_status="in_progress", conclusion=None)

        def api_request(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            return {
                "check_runs": [
                    _check_run(),
                    _check_run(check_status="in_progress", conclusion=None),
                ]
            }

        result = write_advisory_check_projection(
            projection=projection,
            installation_token=_token(),
            api_request=api_request,
        )

        self.assertEqual(result.status, "replayed")
        self.assertEqual(result.check_status, "in_progress")
        self.assertEqual(len(calls), 1)

    def test_completes_existing_in_progress_run(self) -> None:
        calls: list[dict[str, object]] = []

        def api_request(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            if kwargs.get("method") == "PATCH":
                body = kwargs["body"]
                return {
                    **_check_run(check_status="in_progress", conclusion=None),
                    **body,
                }
            return {"check_runs": [_check_run(check_status="in_progress", conclusion=None)]}

        result = write_advisory_check_projection(
            projection=_projection(conclusion="success"),
            installation_token=_token(),
            api_request=api_request,
        )

        self.assertEqual(result.status, "updated")
        self.assertEqual(result.check_status, "completed")
        self.assertEqual(result.conclusion, "success")
        self.assertEqual(calls[-1]["method"], "PATCH")

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

    def test_rejects_post_response_with_mismatched_output_identity(self) -> None:
        def api_request(**kwargs):  # type: ignore[no-untyped-def]
            if kwargs.get("method") == "POST":
                check_run = _check_run()
                check_run["output"] = {
                    "title": "Unexpected title",
                    "summary": "Launchplane advisory projection.",
                }
                return check_run
            return {"check_runs": []}

        with self.assertRaisesRegex(AdvisoryCheckProjectionError, "exact projection identity"):
            write_advisory_check_projection(
                projection=_projection(),
                installation_token=_token(),
                api_request=api_request,
            )

    def test_rejects_patch_response_with_mismatched_output_identity(self) -> None:
        def api_request(**kwargs):  # type: ignore[no-untyped-def]
            if kwargs.get("method") == "PATCH":
                check_run = _check_run()
                check_run["output"] = {
                    "title": "Owner acceptance: pending",
                    "summary": "Unexpected summary",
                }
                return check_run
            return {"check_runs": [_check_run(external_id="c" * 64)]}

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
