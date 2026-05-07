from __future__ import annotations

import unittest
from typing import cast

from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.work_graph_read_model import WorkGraphPlanningIssueFacts
from control_plane.work_graph_service import (
    WorkGraphRankEnvelope,
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
    def list_product_profile_records(
        self,
        *,
        driver_id: str = "",
    ) -> tuple[LaunchplaneProductProfileRecord, ...]:
        return ()

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        raise AssertionError("empty product store should not read product profiles")


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
        return self.records


class WorkGraphServiceTests(unittest.TestCase):
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

        self.assertEqual(work_request_store.seen_limit, 100)
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
