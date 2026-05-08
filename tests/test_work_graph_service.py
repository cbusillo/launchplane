from __future__ import annotations

import unittest
from typing import cast

from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.work_graph_read_model import WorkGraphPlanningIssueFacts
from control_plane.work_graph_service import (
    WorkGraphRankEnvelope,
    build_repo_product_mapping_service_payload,
    build_work_graph_rank_result,
    build_work_graph_snapshot_service_payload,
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
    def __init__(
        self, records: tuple[LaunchplaneProductProfileRecord, ...] = ()
    ) -> None:
        self.records = records

    def list_product_profile_records(
        self,
        *,
        driver_id: str = "",
    ) -> tuple[LaunchplaneProductProfileRecord, ...]:
        if not driver_id:
            return self.records
        return tuple(record for record in self.records if record.driver_id == driver_id)

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        for record in self.records:
            if record.product == product:
                return record
        raise AssertionError(f"test product store has no product profile for {product!r}")


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


class WorkGraphServiceTests(unittest.TestCase):
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
                                {"instance": "testing", "context": "example-site"},
                                {"instance": "prod", "context": "example-site"},
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

    def test_snapshot_payload_builds_source_counts_and_applies_planning_facts(self) -> None:
        work_request_store = _WorkRequestStore((_work_request(),))

        payload = build_work_graph_snapshot_service_payload(
            generated_at="2026-05-06T02:05:00Z",
            product_store=_EmptyProductStore(),
            work_request_store=work_request_store,
            action_allowed=lambda _action, _product, _context: True,
            planning_facts_provider=lambda: (
                WorkGraphPlanningIssueFacts.model_validate(
                    {
                        "repository": "cbusillo/launchplane",
                        "number": 190,
                        "focus": "Now",
                        "manager": "@cellmechanic",
                        "blocking": 2,
                    }
                ),
            ),
        )

        self.assertIsNone(work_request_store.seen_limit)
        self.assertEqual(
            payload["source"],
            {"product_count": 0, "work_request_count": 1, "planning_fact_count": 1},
        )
        snapshot = cast(dict[str, object], payload["snapshot"])
        repos = cast(list[dict[str, object]], snapshot["repos"])
        issues = cast(list[dict[str, object]], snapshot["issues"])
        self.assertEqual(snapshot["generated_at"], "2026-05-06T02:05:00Z")
        self.assertEqual(repos[0]["classification"], "active_awareness")
        self.assertEqual(issues[0]["focus"], "Now")
        self.assertEqual(issues[0]["manager"], "@cellmechanic")
        self.assertEqual(issues[0]["blocking"], 2)

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

    def test_snapshot_payload_does_not_classify_unauthorized_product_repo_as_managed(
        self,
    ) -> None:
        product = LaunchplaneProductProfileRecord.model_validate(
            {
                "schema_version": 1,
                "product": "example-site",
                "display_name": "Example Site",
                "repository": "every/example-site",
                "driver_id": "generic-web",
                "image": {"repository": "ghcr.io/every/example-site"},
                "runtime_port": 3000,
                "health_path": "/healthz",
                "lanes": ({"instance": "prod", "context": "example-site"},),
                "updated_at": "2026-05-02T22:30:00Z",
                "source": "test",
            }
        )

        payload = build_work_graph_snapshot_service_payload(
            generated_at="2026-05-06T02:05:00Z",
            product_store=_EmptyProductStore((product,)),
            work_request_store=_WorkRequestStore(
                (_work_request(repository="every/example-site"),)
            ),
            action_allowed=lambda _action, _product, _context: False,
            planning_facts_provider=None,
        )

        snapshot = cast(dict[str, object], payload["snapshot"])
        source = cast(dict[str, object], payload["source"])
        repos = cast(list[dict[str, object]], snapshot["repos"])
        self.assertEqual(source["product_count"], 1)
        self.assertEqual(repos[0]["classification"], "active_awareness")
        self.assertEqual(repos[0]["product"], "")

    def test_rank_result_returns_summary_and_driver_queue(self) -> None:
        snapshot_payload = build_work_graph_snapshot_service_payload(
            generated_at="2026-05-06T02:05:00Z",
            product_store=_EmptyProductStore(),
            work_request_store=_WorkRequestStore((_work_request(),)),
            action_allowed=lambda _action, _product, _context: True,
            planning_facts_provider=None,
        )
        rank_request = WorkGraphRankEnvelope.model_validate(
            {"snapshot": snapshot_payload["snapshot"], "limit": 5}
        )

        result, driver_result = build_work_graph_rank_result(rank_request)

        self.assertEqual(result, {"item_count": 1, "hidden_count": 0})
        queue = cast(dict[str, object], driver_result["queue"])
        items = cast(list[dict[str, object]], queue["items"])
        self.assertEqual(items[0]["number"], 190)


if __name__ == "__main__":
    unittest.main()
