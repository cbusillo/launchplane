from __future__ import annotations

import unittest
from typing import cast

from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.repo_product_mapping_read_model import (
    build_repo_product_mapping_service_payload,
)


def _work_request(**overrides: object) -> EveryCodeWorkRequestRecord:
    payload: dict[str, object] = {
        "request_id": "every-code-cbusillo-launchplane-190",
        "source": "github_issue_label",
        "state": "queued",
        "repository": "cbusillo/launchplane",
        "issue_number": 190,
        "issue_url": "https://github.com/cbusillo/launchplane/issues/190",
        "issue_title": "Build What To Work On Next cockpit",
        "trigger_label": "every-code",
        "trigger_actor": "cbusillo",
        "github_delivery_id": "delivery-190",
        "queued_at": "2026-05-06T02:00:00Z",
        "updated_at": "2026-05-06T02:00:00Z",
    }
    payload.update(overrides)
    return EveryCodeWorkRequestRecord.model_validate(payload)


class _EmptyProductStore:
    def __init__(self, records: tuple[LaunchplaneProductProfileRecord, ...] = ()) -> None:
        self.records = records

    def list_product_profile_records(
        self,
        *,
        driver_id: str = "",
    ) -> tuple[LaunchplaneProductProfileRecord, ...]:
        if not driver_id:
            return self.records
        return tuple(record for record in self.records if record.driver_id == driver_id)


class _CoveredProductStore(_EmptyProductStore):
    def __init__(
        self,
        records: tuple[LaunchplaneProductProfileRecord, ...],
        coverage: str,
    ) -> None:
        super().__init__(records)
        self.coverage = coverage

    def collection_read_coverage(self, collection: str) -> str:
        return self.coverage if collection == "product_profiles" else "unsupported"


class _WorkRequestStore:
    def __init__(self, records: tuple[EveryCodeWorkRequestRecord, ...]) -> None:
        self.records = records
        self.seen_limit: int | None = None

    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]:
        self.seen_limit = limit
        records = self.records
        if repository:
            records = tuple(record for record in records if record.repository == repository)
        if state:
            records = tuple(record for record in records if record.state == state)
        if offset > 0:
            records = records[offset:]
        if limit is not None:
            records = records[:limit]
        return records


class RepoProductMappingServiceTests(unittest.TestCase):
    def _profile(
        self,
        *,
        product: str,
        repository: str,
        repository_id: str = "",
        driver_id: str = "odoo",
        director_standing: bool = False,
    ) -> LaunchplaneProductProfileRecord:
        return LaunchplaneProductProfileRecord.model_validate(
            {
                "schema_version": 1,
                "product": product,
                "display_name": product.replace("-", " ").title(),
                "repository": repository,
                "repository_id": repository_id,
                "repository_owner_id": "7007" if repository_id else "",
                "driver_id": driver_id,
                "owner": {"github_login": product, "github_id": "42"},
                "release_on_acceptance": "director_standing" if director_standing else "held",
                "image": {"repository": f"ghcr.io/synthetic/{product}"},
                "runtime_port": 8069,
                "health_path": "/web/health",
                "lanes": ({"instance": "testing", "context": product},),
                "updated_at": "2026-10-29T12:00:00Z",
                "source": "test",
            }
        )

    def test_repo_product_mapping_payload_classifies_products_and_awareness_repos(self) -> None:
        payload = build_repo_product_mapping_service_payload(
            generated_at="2026-05-08T18:05:00Z",
            product_store=_EmptyProductStore(
                (
                    LaunchplaneProductProfileRecord.model_validate(
                        {
                            "schema_version": 1,
                            "product": "example-site",
                            "display_name": "Example Site",
                            "repository": "every/example-site",
                            "driver_id": "generic-web",
                            "image": {"repository": "ghcr.io/every/example-site"},
                            "runtime_port": 3000,
                            "health_path": "/healthz",
                            "lanes": (
                                {
                                    "instance": "testing",
                                    "context": "example-site",
                                    "base_url": "https://testing.example-site.example.test",
                                },
                                {
                                    "instance": "prod",
                                    "context": "example-site",
                                    "base_url": "https://example-site.example.test",
                                },
                            ),
                            "historical_contexts": ("example-site-legacy",),
                            "preview": {"enabled": True, "context": "example-site-preview"},
                            "updated_at": "2026-05-02T22:30:00Z",
                            "source": "test",
                        }
                    ),
                )
            ),
            work_request_store=_WorkRequestStore(
                (
                    _work_request(repository="every/example-site"),
                    _work_request(
                        request_id="every-code-cbusillo-tooling-12",
                        repository="cbusillo/tooling",
                        issue_number=12,
                        issue_url="https://github.com/cbusillo/tooling/issues/12",
                    ),
                )
            ),
        )

        self.assertEqual(payload["source"], {"product_count": 1, "work_request_count": 2})
        mapping = cast(dict[str, object], payload["mapping"])
        repositories = cast(list[dict[str, object]], mapping["repositories"])
        by_repository = {str(repo["repository"]): repo for repo in repositories}
        product_repo = by_repository["every/example-site"]
        self.assertEqual(product_repo["classification"], "managed_runtime")
        self.assertEqual(product_repo["product"], "example-site")
        self.assertEqual(product_repo["display_name"], "Example Site")
        self.assertEqual(product_repo["driver_id"], "generic-web")
        self.assertEqual(product_repo["contexts"], ["example-site", "example-site-legacy"])
        self.assertEqual(product_repo["environments"], ["testing", "prod"])
        self.assertEqual(product_repo["preview_context"], "example-site-preview")
        self.assertEqual(product_repo["source"], "product_profile")
        awareness_repo = by_repository["cbusillo/tooling"]
        self.assertEqual(awareness_repo["classification"], "active_awareness")
        self.assertEqual(awareness_repo["product"], "")
        self.assertEqual(awareness_repo["source"], "every_code_work_request")

    def test_repo_product_mapping_reads_past_first_hundred_work_requests(self) -> None:
        work_request_store = _WorkRequestStore(
            tuple(
                _work_request(
                    request_id=f"every-code-cbusillo-tool-{index}",
                    repository=f"cbusillo/tool-{index}",
                    issue_number=index + 1,
                    issue_url=f"https://github.com/cbusillo/tool-{index}/issues/{index + 1}",
                )
                for index in range(105)
            )
        )

        payload = build_repo_product_mapping_service_payload(
            generated_at="2026-05-08T18:05:00Z",
            product_store=_EmptyProductStore(),
            work_request_store=work_request_store,
        )

        mapping = cast(dict[str, object], payload["mapping"])
        repositories = cast(list[dict[str, object]], mapping["repositories"])
        self.assertIsNone(work_request_store.seen_limit)
        self.assertEqual(payload["source"], {"product_count": 0, "work_request_count": 105})
        self.assertIn(
            "cbusillo/tool-104",
            {str(repository["repository"]) for repository in repositories},
        )

    def test_ambiguous_aliases_reference_every_product_in_shared_group(self) -> None:
        profiles = (
            self._profile(
                product="synthetic-client-one",
                repository="synthetic/client-one",
                repository_id="420042",
            ),
            self._profile(
                product="synthetic-client-two",
                repository="synthetic/client-two-renamed",
                repository_id="420042",
            ),
        )

        payload = build_repo_product_mapping_service_payload(
            generated_at="2026-10-29T12:00:00Z",
            product_store=_CoveredProductStore(profiles, "complete"),
            work_request_store=_WorkRequestStore(()),
        )

        mapping = cast(dict[str, object], payload["mapping"])
        self.assertEqual(
            mapping["ambiguous_repositories"],
            ["synthetic/client-one", "synthetic/client-two-renamed"],
        )
        references = cast(
            list[dict[str, object]], mapping["ambiguous_repository_product_references"]
        )
        self.assertEqual(
            {str(reference["repository"]): reference["products"] for reference in references},
            {
                "synthetic/client-one": ["synthetic-client-one", "synthetic-client-two"],
                "synthetic/client-two-renamed": [
                    "synthetic-client-one",
                    "synthetic-client-two",
                ],
            },
        )
        self.assertEqual(
            cast(dict[str, object], mapping["product_reference_coverage"])["status"],
            "complete",
        )

    def test_ambiguous_references_filter_products_without_changing_ambiguity(self) -> None:
        profiles = (
            self._profile(product="visible-client", repository="synthetic/shared"),
            self._profile(product="hidden-client", repository="synthetic/shared"),
            self._profile(
                product="visible-director",
                repository="synthetic/shared",
                driver_id="generic-web",
                director_standing=True,
            ),
        )

        payload = build_repo_product_mapping_service_payload(
            generated_at="2026-10-29T12:00:00Z",
            product_store=_CoveredProductStore(profiles, "complete"),
            work_request_store=_WorkRequestStore(()),
            product_reference_allowed=lambda product: product != "hidden-client",
        )

        mapping = cast(dict[str, object], payload["mapping"])
        self.assertEqual(mapping["ambiguous_repositories"], ["synthetic/shared"])
        references = cast(
            list[dict[str, object]], mapping["ambiguous_repository_product_references"]
        )
        self.assertEqual(
            references,
            [
                {
                    "repository": "synthetic/shared",
                    "products": ["visible-client", "visible-director"],
                }
            ],
        )
        coverage = cast(dict[str, object], mapping["product_reference_coverage"])
        self.assertEqual(coverage["source"], "complete")
        self.assertEqual(coverage["authorization"], "scoped")
        self.assertEqual(coverage["status"], "scoped")

    def test_reference_coverage_is_unknown_for_legacy_store_even_without_references(self) -> None:
        payload = build_repo_product_mapping_service_payload(
            generated_at="2026-10-29T12:00:00Z",
            product_store=_EmptyProductStore(),
            work_request_store=_WorkRequestStore(()),
        )

        mapping = cast(dict[str, object], payload["mapping"])
        self.assertNotIn("ambiguous_repository_product_references", mapping)
        coverage = cast(dict[str, object], mapping["product_reference_coverage"])
        self.assertEqual(coverage["status"], "unknown")
        self.assertEqual(coverage["source"], "unknown")

        limited_payload = build_repo_product_mapping_service_payload(
            generated_at="2026-10-29T12:00:00Z",
            product_store=_CoveredProductStore((), "limited"),
            work_request_store=_WorkRequestStore(()),
        )
        limited_mapping = cast(dict[str, object], limited_payload["mapping"])
        limited_coverage = cast(dict[str, object], limited_mapping["product_reference_coverage"])
        self.assertEqual(limited_coverage["status"], "limited")
        self.assertEqual(limited_coverage["source"], "limited")


if __name__ == "__main__":
    unittest.main()
