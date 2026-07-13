import unittest
from collections.abc import Callable
from unittest.mock import patch

import click

from control_plane.workflows.preview_resource_destroy import (
    PreviewResourceDestroyResult,
    PreviewResourceType,
    destroy_dokploy_preview_resource,
)


PATCH_TARGET = (
    "control_plane.workflows.preview_resource_destroy.control_plane_dokploy.dokploy_request"
)


def _run_destroy_with_fake_requests(
    *,
    requests: list[dict[str, object]],
    response_by_path: dict[str, object],
    resource_type: PreviewResourceType,
    resource_id: str,
    domain_host: str = "",
    delete_volumes: bool = False,
    continue_after_domain_cleanup_error: bool = True,
    missing_resource_is_clean: bool = False,
    before_provider_mutation: Callable[[str], None] | None = None,
) -> PreviewResourceDestroyResult:
    def _fake_dokploy_request(**kwargs: object) -> object:
        requests.append(dict(kwargs))
        path = kwargs["path"]
        if not isinstance(path, str):
            raise AssertionError("fake Dokploy request requires a string path")
        response = response_by_path.get(path, {})
        if isinstance(response, Exception):
            raise response
        return response

    with patch(PATCH_TARGET, side_effect=_fake_dokploy_request):
        return destroy_dokploy_preview_resource(
            host="https://dokploy.example",
            token="token",
            resource_type=resource_type,
            resource_id=resource_id,
            domain_host=domain_host,
            delete_volumes=delete_volumes,
            continue_after_domain_cleanup_error=continue_after_domain_cleanup_error,
            missing_resource_is_clean=missing_resource_is_clean,
            before_provider_mutation=before_provider_mutation,
        )


class PreviewResourceDestroyTests(unittest.TestCase):
    def test_destroy_application_deletes_all_domains_and_application(self) -> None:
        requests: list[dict[str, object]] = []

        result = _run_destroy_with_fake_requests(
            requests=requests,
            response_by_path={
                "/api/domain.byApplicationId": [
                    {"domainId": "domain-one"},
                    {"domainId": "domain-two"},
                ]
            },
            resource_type="application",
            resource_id="app-one",
        )

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.domain_ids, ("domain-one", "domain-two"))
        self.assertEqual(
            [request["path"] for request in requests],
            [
                "/api/domain.byApplicationId",
                "/api/domain.delete",
                "/api/domain.delete",
                "/api/application.delete",
            ],
        )

    def test_destroy_application_continues_after_domain_cleanup_failure(self) -> None:
        requests: list[dict[str, object]] = []

        result = _run_destroy_with_fake_requests(
            requests=requests,
            response_by_path={
                "/api/domain.byApplicationId": click.ClickException("domain lookup unavailable")
            },
            resource_type="application",
            resource_id="app-one",
        )

        self.assertEqual(result.status, "fail")
        self.assertIn("domain cleanup failed", result.cleanup_errors[0])
        self.assertEqual(
            [request["path"] for request in requests],
            ["/api/domain.byApplicationId", "/api/application.delete"],
        )

    def test_destroy_compose_filters_domain_host_and_deletes_volumes(self) -> None:
        requests: list[dict[str, object]] = []

        result = _run_destroy_with_fake_requests(
            requests=requests,
            response_by_path={
                "/api/domain.byComposeId": [
                    {"domainId": "domain-keep", "host": "pr-45.example.test"},
                    {"domainId": "domain-other", "host": "other.example.test"},
                ]
            },
            resource_type="compose",
            resource_id="compose-one",
            domain_host="pr-45.example.test",
            delete_volumes=True,
            continue_after_domain_cleanup_error=False,
        )

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.domain_ids, ("domain-keep",))
        compose_delete_payload = requests[-1]["payload"]
        self.assertIsInstance(compose_delete_payload, dict)
        assert isinstance(compose_delete_payload, dict)
        self.assertEqual(compose_delete_payload["composeId"], "compose-one")
        self.assertTrue(compose_delete_payload["deleteVolumes"])

    def test_destroy_compose_can_fail_closed_on_domain_cleanup_failure(self) -> None:
        requests: list[dict[str, object]] = []

        result = _run_destroy_with_fake_requests(
            requests=requests,
            response_by_path={
                "/api/domain.byComposeId": click.ClickException("domain lookup unavailable")
            },
            resource_type="compose",
            resource_id="compose-one",
            continue_after_domain_cleanup_error=False,
        )

        self.assertEqual(result.status, "fail")
        self.assertIn("domain cleanup failed", result.cleanup_errors[0])
        self.assertEqual([request["path"] for request in requests], ["/api/domain.byComposeId"])

    def test_destroy_compose_can_treat_missing_resource_as_clean(self) -> None:
        requests: list[dict[str, object]] = []

        result = _run_destroy_with_fake_requests(
            requests=requests,
            response_by_path={
                "/api/domain.byComposeId": [],
                "/api/compose.delete": click.ClickException(
                    "Dokploy API POST /api/compose.delete failed (404): Not Found"
                ),
            },
            resource_type="compose",
            resource_id="compose-one",
            missing_resource_is_clean=True,
        )

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.cleanup_errors, ())
        self.assertEqual(
            [step.name for step in result.steps],
            ["domain_lookup", "compose_delete"],
        )

    def test_destroy_rechecks_fence_before_each_provider_write(self) -> None:
        requests: list[dict[str, object]] = []
        phases: list[str] = []

        def checkpoint(phase: str) -> None:
            phases.append(phase)
            if phase == "compose_destroy":
                raise RuntimeError("reservation recovered")

        with self.assertRaisesRegex(RuntimeError, "reservation recovered"):
            _run_destroy_with_fake_requests(
                requests=requests,
                response_by_path={
                    "/api/domain.byComposeId": [
                        {"domainId": "domain-one", "host": "pr-45.example.test"}
                    ]
                },
                resource_type="compose",
                resource_id="compose-one",
                before_provider_mutation=checkpoint,
            )

        self.assertEqual(phases, ["domain_delete", "compose_destroy"])
        self.assertEqual(
            [request["path"] for request in requests],
            ["/api/domain.byComposeId", "/api/domain.delete"],
        )


if __name__ == "__main__":
    unittest.main()
