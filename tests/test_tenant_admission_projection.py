from __future__ import annotations

import unittest
from typing import cast

from control_plane.tenant_admission_projection import (
    TenantAdmissionProjectionError,
    TenantAdmissionProjectionStaleCandidateError,
    build_tenant_admission_projection,
    read_current_tenant_admission_candidate,
    write_tenant_admission_projection,
)
from control_plane.tenant_admission_status import TENANT_ADMISSION_STATUS_CONTEXT
from tests.test_tenant_admission_status import (
    _candidate,
    _classification,
    _classification_only_store,
    _status_with_path_states,
)
from control_plane.tenant_admission_status import get_tenant_admission_status


EVALUATED_AT = "2026-07-31T12:10:00Z"


class TenantAdmissionProjectionTests(unittest.TestCase):
    def test_projection_maps_public_status_categories(self) -> None:
        cases = (
            (
                _status_with_path_states(
                    trusted_state="pending",
                    waiver_state="pending",
                    manager_state="pending",
                ),
                "pending",
            ),
            (
                _status_with_path_states(
                    trusted_state="pending",
                    waiver_state="pending",
                    manager_state="satisfied",
                ),
                "success",
            ),
            (
                _status_with_path_states(
                    trusted_state="pending",
                    waiver_state="satisfied",
                    manager_state="pending",
                ),
                "success",
            ),
            (
                _status_with_path_states(
                    trusted_state="satisfied",
                    waiver_state="pending",
                    manager_state="pending",
                ),
                "success",
            ),
            (
                _status_with_path_states(
                    trusted_state="pending",
                    waiver_state="pending",
                    manager_state="stale",
                ),
                "failure",
            ),
            (
                _status_with_path_states(
                    trusted_state="pending",
                    waiver_state="pending",
                    manager_state="denied",
                ),
                "failure",
            ),
            (
                _status_with_path_states(
                    trusted_state="pending",
                    waiver_state="pending",
                    manager_state="unavailable",
                ),
                "error",
            ),
        )
        for read_model, expected_state in cases:
            with self.subTest(category=read_model.category):
                projection = build_tenant_admission_projection(
                    read_model=read_model,
                    candidate=_candidate(),
                    pull_request_url="https://github.com/example/example-site/pull/17",
                )
                self.assertTrue(projection.required)
                self.assertEqual(projection.state, expected_state)
                self.assertLessEqual(len(projection.description), 140)

    def test_engineering_projection_is_not_required(self) -> None:
        candidate = _candidate()
        read_model = get_tenant_admission_status(
            store=_classification_only_store((_classification(kind="engineering"),)),
            candidate=candidate,
            evaluated_at=EVALUATED_AT,
        )
        projection = build_tenant_admission_projection(
            read_model=read_model,
            candidate=candidate,
            pull_request_url="https://github.com/example/example-site/pull/17",
        )
        calls: list[dict[str, object]] = []

        result = write_tenant_admission_projection(
            projection=projection,
            token="token",
            api_request=lambda **kwargs: calls.append(kwargs),
        )

        self.assertFalse(projection.required)
        self.assertEqual(result.status, "not_required")
        self.assertEqual(calls, [])

    def test_current_candidate_requires_exact_numeric_repository_and_head(self) -> None:
        candidate = _candidate()

        pull_request_url = read_current_tenant_admission_candidate(
            expected=candidate,
            token="token",
            api_request=lambda **_kwargs: _pull_request_payload(),
        )

        self.assertEqual(
            pull_request_url,
            "https://github.com/example/example-site/pull/17",
        )
        with self.assertRaises(TenantAdmissionProjectionStaleCandidateError):
            read_current_tenant_admission_candidate(
                expected=candidate,
                token="token",
                api_request=lambda **_kwargs: _pull_request_payload(head_sha="2" * 40),
            )
        with self.assertRaises(TenantAdmissionProjectionStaleCandidateError):
            read_current_tenant_admission_candidate(
                expected=candidate,
                token="token",
                api_request=lambda **_kwargs: _pull_request_payload(repository_id=9999),
            )

    def test_projection_replays_matching_current_status(self) -> None:
        projection = build_tenant_admission_projection(
            read_model=_status_with_path_states(
                trusted_state="pending",
                waiver_state="pending",
                manager_state="satisfied",
            ),
            candidate=_candidate(),
            pull_request_url="https://github.com/example/example-site/pull/17",
        )
        calls: list[dict[str, object]] = []

        def api_request(**kwargs: object) -> object:
            calls.append(kwargs)
            return {
                "statuses": [
                    {
                        "context": TENANT_ADMISSION_STATUS_CONTEXT,
                        "state": projection.state,
                        "description": projection.description,
                        "target_url": projection.target_url,
                    }
                ]
            }

        result = write_tenant_admission_projection(
            projection=projection,
            token="token",
            api_request=api_request,
        )

        self.assertEqual(result.status, "replayed")
        self.assertEqual(len(calls), 1)

    def test_projection_writes_exact_context_and_validates_response(self) -> None:
        projection = build_tenant_admission_projection(
            read_model=_status_with_path_states(
                trusted_state="pending",
                waiver_state="satisfied",
                manager_state="pending",
            ),
            candidate=_candidate(),
            pull_request_url="https://github.com/example/example-site/pull/17",
        )
        calls: list[dict[str, object]] = []

        def api_request(**kwargs: object) -> object:
            calls.append(kwargs)
            if kwargs.get("method") == "POST":
                return {
                    "context": TENANT_ADMISSION_STATUS_CONTEXT,
                    "state": projection.state,
                    "sha": projection.head_sha,
                }
            return {"statuses": []}

        result = write_tenant_admission_projection(
            projection=projection,
            token="token",
            api_request=api_request,
        )

        self.assertEqual(result.status, "projected")
        request_body = cast(dict[str, object], calls[-1]["body"])
        self.assertEqual(request_body["context"], TENANT_ADMISSION_STATUS_CONTEXT)
        self.assertEqual(request_body["state"], "success")

        def mismatched_response(**kwargs: object) -> object:
            if kwargs.get("method") == "POST":
                return {"context": "other", "state": projection.state, "sha": projection.head_sha}
            return {"statuses": []}

        with self.assertRaises(TenantAdmissionProjectionError):
            write_tenant_admission_projection(
                projection=projection,
                token="token",
                api_request=mismatched_response,
            )

        def missing_confirmation(**kwargs: object) -> object:
            if kwargs.get("method") == "POST":
                return {}
            return {"statuses": []}

        with self.assertRaises(TenantAdmissionProjectionError):
            write_tenant_admission_projection(
                projection=projection,
                token="token",
                api_request=missing_confirmation,
            )

    def test_projection_delivery_failure_never_returns_success(self) -> None:
        projection = build_tenant_admission_projection(
            read_model=_status_with_path_states(
                trusted_state="pending",
                waiver_state="pending",
                manager_state="satisfied",
            ),
            candidate=_candidate(),
            pull_request_url="https://github.com/example/example-site/pull/17",
        )

        def unavailable(**_kwargs: object) -> object:
            raise RuntimeError("provider unavailable")

        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            write_tenant_admission_projection(
                projection=projection,
                token="token",
                api_request=unavailable,
            )


def _pull_request_payload(
    *,
    repository_id: int = 1001,
    repository_owner_id: int = 2001,
    head_sha: str = "1" * 40,
) -> dict[str, object]:
    return {
        "number": 17,
        "state": "open",
        "html_url": "https://github.com/example/example-site/pull/17",
        "base": {
            "repo": {
                "id": repository_id,
                "full_name": "example/example-site",
                "owner": {"id": repository_owner_id},
            }
        },
        "head": {"sha": head_sha},
    }
