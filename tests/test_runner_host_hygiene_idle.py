from __future__ import annotations

import unittest

from pydantic import ValidationError

from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyPolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyRequest
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneReport
from control_plane.contracts.runner_host_hygiene import plan_runner_host_hygiene_apply
from control_plane.contracts.runner_host_hygiene_idle import (
    RunnerHostHygieneIdleConvergence,
    RunnerHostHygieneIdleObservation,
)
from control_plane.contracts.runner_host_hygiene_idle import (
    RunnerHostHygieneIdleSource,
)
from control_plane.contracts.runner_host_hygiene_idle import (
    RunnerHostHygieneIdleSourceRequirement,
)
from control_plane.contracts.runner_host_hygiene_idle import (
    build_runner_host_hygiene_idle_convergence,
)


class RunnerHostHygieneIdleContractTests(unittest.TestCase):
    def test_full_host_sources_converge_idle(self) -> None:
        convergence = _full_host_convergence()

        self.assertEqual(convergence.status, "idle")
        self.assertEqual(convergence.blocker_codes, ())

    def test_active_source_prevents_idle_convergence(self) -> None:
        convergence = _full_host_convergence(active_source="local_processes")

        self.assertEqual(convergence.status, "active")
        self.assertIn("source_active", convergence.blocker_codes)

    def test_unauthorized_source_fails_closed(self) -> None:
        convergence = _full_host_convergence(
            incomplete_source="github_runners",
            reason_code="source_unauthorized",
        )

        self.assertEqual(convergence.status, "incomplete")
        self.assertIn("source_unauthorized", convergence.blocker_codes)

    def test_truncated_source_fails_closed(self) -> None:
        convergence = _full_host_convergence(
            incomplete_source="github_jobs",
            reason_code="source_truncated",
            truncated=True,
        )

        self.assertEqual(convergence.status, "incomplete")
        self.assertIn("source_truncated", convergence.blocker_codes)

    def test_insufficient_temporal_window_fails_closed(self) -> None:
        convergence = _full_host_convergence(
            short_window_source="local_processes",
        )

        self.assertEqual(convergence.status, "incomplete")
        self.assertIn("observation_window_too_short", convergence.blocker_codes)

    def test_contradictory_source_has_distinct_status(self) -> None:
        convergence = _full_host_convergence(
            incomplete_source="runner_services",
            reason_code="source_contradictory",
        )

        self.assertEqual(convergence.status, "conflicting")
        self.assertIn("source_conflicting", convergence.blocker_codes)
        self.assertNotIn("source_unavailable", convergence.blocker_codes)

    def test_observation_scope_must_match_convergence(self) -> None:
        with self.assertRaisesRegex(ValidationError, "scope must match convergence"):
            build_runner_host_hygiene_idle_convergence(
                scope="full_host",
                scope_key="host",
                evaluated_at="2026-07-30T12:00:05Z",
                minimum_observation_interval_seconds=5,
                maximum_evidence_age_seconds=300,
                requirements=(
                    RunnerHostHygieneIdleSourceRequirement(
                        source="local_processes",
                        subject_key="host-processes",
                    ),
                ),
                observations=(
                    RunnerHostHygieneIdleObservation(
                        source="local_processes",
                        scope="isolated_user",
                        scope_key="cache",
                        subject_key="host-processes",
                        observed_at="2026-07-30T12:00:05Z",
                        availability="available",
                        state="idle",
                        sample_count=2,
                        observation_window_seconds=5,
                        active_count=0,
                    ),
                ),
            )

    def test_apply_plan_accepts_fresh_matching_idle_scope(self) -> None:
        plan = plan_runner_host_hygiene_apply(
            policy=_apply_policy(),
            request=_apply_request(),
            report=_report(_full_host_convergence()),
        )

        self.assertEqual(plan.status, "ready")

    def test_apply_plan_blocks_stale_decision(self) -> None:
        plan = plan_runner_host_hygiene_apply(
            policy=_apply_policy(),
            request=_apply_request(idle_decision_at="2026-07-30T12:06:00Z"),
            report=_report(_full_host_convergence()),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn("idle_evidence_stale", {blocker.code for blocker in plan.blockers})

    def test_apply_plan_rejects_incomplete_required_source_set(self) -> None:
        convergence = build_runner_host_hygiene_idle_convergence(
            scope="full_host",
            scope_key="host",
            evaluated_at="2026-07-30T12:00:05Z",
            minimum_observation_interval_seconds=5,
            maximum_evidence_age_seconds=300,
            requirements=(
                RunnerHostHygieneIdleSourceRequirement(
                    source="local_processes",
                    subject_key="host-processes",
                ),
            ),
            observations=(_observation(source="local_processes", subject_key="host-processes"),),
        )

        plan = plan_runner_host_hygiene_apply(
            policy=_apply_policy(),
            request=_apply_request(),
            report=_report(convergence),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn(
            "idle_evidence_not_converged",
            {blocker.code for blocker in plan.blockers},
        )


def _full_host_convergence(
    *,
    active_source: RunnerHostHygieneIdleSource | None = None,
    incomplete_source: RunnerHostHygieneIdleSource | None = None,
    short_window_source: RunnerHostHygieneIdleSource | None = None,
    reason_code: str = "source_unavailable",
    truncated: bool = False,
) -> RunnerHostHygieneIdleConvergence:
    sources: tuple[tuple[RunnerHostHygieneIdleSource, str], ...] = (
        ("github_jobs", "lane-key"),
        ("github_runners", "lane-key"),
        ("local_processes", "host-processes"),
        ("runner_services", "runner-services"),
    )
    requirements = tuple(
        RunnerHostHygieneIdleSourceRequirement(
            source=source,
            subject_key=subject_key,
            minimum_observation_count=2,
        )
        for source, subject_key in sources
    )
    observations = []
    for source, subject_key in sources:
        if source == incomplete_source:
            observations.append(
                RunnerHostHygieneIdleObservation(
                    source=source,
                    scope="full_host",
                    scope_key="host",
                    subject_key=subject_key,
                    observed_at="2026-07-30T12:00:05Z",
                    availability="partial" if truncated else "unavailable",
                    state="unknown",
                    sample_count=2,
                    observation_window_seconds=5,
                    truncated=truncated,
                    reason_code=reason_code,
                )
            )
            continue
        observations.append(
            _observation(
                source=source,
                subject_key=subject_key,
                active=source == active_source,
                observation_window_seconds=(0 if source == short_window_source else 5),
            )
        )
    return build_runner_host_hygiene_idle_convergence(
        scope="full_host",
        scope_key="host",
        evaluated_at="2026-07-30T12:00:05Z",
        minimum_observation_interval_seconds=5,
        maximum_evidence_age_seconds=300,
        requirements=requirements,
        observations=tuple(observations),
    )


def _observation(
    *,
    source: RunnerHostHygieneIdleSource,
    subject_key: str,
    active: bool = False,
    observation_window_seconds: int = 5,
) -> RunnerHostHygieneIdleObservation:
    return RunnerHostHygieneIdleObservation(
        source=source,
        scope="full_host",
        scope_key="host",
        subject_key=subject_key,
        observed_at="2026-07-30T12:00:05Z",
        availability="available",
        state="active" if active else "idle",
        sample_count=2,
        observation_window_seconds=observation_window_seconds,
        active_count=1 if active else 0,
        total_count=1,
    )


def _apply_policy() -> RunnerHostHygieneApplyPolicy:
    return RunnerHostHygieneApplyPolicy(
        approved_hosts=("runner-host",),
        require_healthy_report=False,
        require_audit_record=False,
        require_idle_convergence=True,
        allow_docker_cache_prune=True,
    )


def _apply_request(
    *,
    idle_decision_at: str = "2026-07-30T12:00:06Z",
) -> RunnerHostHygieneApplyRequest:
    return RunnerHostHygieneApplyRequest(
        action="prune_docker_cache",
        host_name="runner-host",
        mutate=True,
        idle_observation_count=2,
        idle_observation_interval_seconds=5,
        idle_max_age_seconds=300,
        idle_decision_at=idle_decision_at,
    )


def _report(convergence: RunnerHostHygieneIdleConvergence) -> RunnerHostHygieneReport:
    return RunnerHostHygieneReport(
        status="healthy",
        host_name="runner-host",
        observed_at="2026-07-30T12:00:05Z",
        idle_convergences=(convergence,),
        summary="runner host hygiene report is healthy",
    )


if __name__ == "__main__":
    unittest.main()
