"""Trigger a Dokploy schedule once and observe the provider job it started.

Dokploy keeps ``schedule.runManually`` open until the job ends, so a slow job
looks like a failed trigger. These tests drive a fake provider through the
trigger outcomes that matter: accepted then timed out, refused before any job
was created, and unclear with a read-back that cannot resolve it. Every case
must send exactly one trigger and report what actually happened.
"""

from __future__ import annotations

import unittest
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from control_plane import dokploy as control_plane_dokploy
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import post_deploy as dokploy_post_deploy
from control_plane.dokploy.api import JsonObject, JsonValue

SCHEDULE_ID = "schedule-123"
TRIGGER_PATH = "/api/schedule.runManually"
LIST_PATH = "/api/deployment.allByType"


def _timestamp(offset: timedelta) -> str:
    return (datetime.now(timezone.utc) + offset).isoformat().replace("+00:00", "Z")


def _deployment(deployment_id: str, status: str, offset: timedelta) -> JsonObject:
    return {"deploymentId": deployment_id, "status": status, "createdAt": _timestamp(offset)}


BEFORE = _deployment("deployment-before", "done", timedelta(hours=-1))
OLDER = _deployment("deployment-older", "error", timedelta(days=-2))


class FakeProvider:
    """Serves the schedule trigger and deployment list from scripted responses."""

    def __init__(
        self,
        *,
        trigger: Callable[[], JsonValue],
        listings: list[Callable[[], list[JsonObject]]],
    ) -> None:
        self._trigger = trigger
        self._listings = listings
        self.trigger_calls: list[int | float] = []
        self.list_calls = 0

    def request(self, **kwargs: object) -> JsonValue:
        path = kwargs["path"]
        if path == TRIGGER_PATH:
            self.trigger_calls.append(kwargs["timeout_seconds"])  # type: ignore[arg-type]
            return self._trigger()
        if path == LIST_PATH:
            listing = self._listings[min(self.list_calls, len(self._listings) - 1)]
            self.list_calls += 1
            return list(listing())
        if path == "/api/deployment.readLogs":
            return {"logs": []}
        raise AssertionError(f"unexpected Dokploy request {path}")


def _timed_out() -> JsonValue:
    raise dokploy_api.DokployRequestFailed(
        method="POST", path=TRIGGER_PATH, detail="response read timed out"
    )


def _refused() -> JsonValue:
    raise dokploy_api.DokployRequestFailed(
        method="POST", path=TRIGGER_PATH, status_code=400, detail="Schedule not found"
    )


def _list_unavailable() -> list[JsonObject]:
    raise dokploy_api.DokployRequestFailed(
        method="GET", path=LIST_PATH, status_code=502, detail="Bad Gateway"
    )


def _run(provider: FakeProvider, *, execution_timeout_seconds: int = 7200) -> str:
    with patch.object(dokploy_api, "dokploy_request", side_effect=provider.request):
        return dokploy_api.trigger_dokploy_schedule_and_wait(
            host="https://dokploy.example.com",
            token="secret-token",
            schedule_id=SCHEDULE_ID,
            before_key="deployment-before",
            execution_timeout_seconds=execution_timeout_seconds,
            readback_grace_seconds=0,
            readback_interval_seconds=0,
            observation_interval_seconds=0,
        )


class ScheduleTriggerTests(unittest.TestCase):
    def test_trigger_deadline_is_separate_from_the_execution_budget(self) -> None:
        provider = FakeProvider(
            trigger=lambda: {"ok": True},
            listings=[
                lambda: [BEFORE, _deployment("deployment-new", "done", timedelta(seconds=1))]
            ],
        )

        result = _run(provider, execution_timeout_seconds=7200)

        self.assertEqual(result, "deployment=deployment-new status=done")
        self.assertEqual(
            provider.trigger_calls, [dokploy_api.DEFAULT_DOKPLOY_SCHEDULE_TRIGGER_TIMEOUT_SECONDS]
        )

    def test_accepted_trigger_that_times_out_is_observed_to_success(self) -> None:
        running = _deployment("deployment-new", "running", timedelta(seconds=1))
        done = {**running, "status": "done"}
        provider = FakeProvider(
            trigger=_timed_out,
            listings=[
                lambda: [OLDER, BEFORE, running],
                lambda: [OLDER, BEFORE, running],
                lambda: [OLDER, BEFORE, done],
            ],
        )

        result = _run(provider)

        self.assertEqual(result, "deployment=deployment-new status=done")
        self.assertEqual(len(provider.trigger_calls), 1)

    def test_accepted_trigger_that_times_out_reports_the_job_failure(self) -> None:
        running = _deployment("deployment-new", "running", timedelta(seconds=1))
        provider = FakeProvider(
            trigger=_timed_out,
            listings=[lambda: [BEFORE, running], lambda: [BEFORE, {**running, "status": "error"}]],
        )

        with self.assertRaises(dokploy_api.DokployDeploymentFailed) as raised:
            _run(provider)

        self.assertEqual(raised.exception.deployment_id, "deployment-new")
        self.assertEqual(raised.exception.deployment_status, "error")
        self.assertEqual(len(provider.trigger_calls), 1)

    def test_job_still_running_after_the_budget_is_reported_as_running(self) -> None:
        running = _deployment("deployment-new", "running", timedelta(seconds=1))
        provider = FakeProvider(trigger=_timed_out, listings=[lambda: [BEFORE, running]])

        with self.assertRaises(dokploy_api.DokployScheduleExecutionFailed) as raised:
            _run(provider, execution_timeout_seconds=1)

        failure = raised.exception
        self.assertEqual(failure.cause, "execution_timeout")
        self.assertEqual(failure.deployment_id, "deployment-new")
        self.assertEqual(failure.deployment_status, "running")
        self.assertIn("neither cancelled nor retried", failure.message)
        self.assertEqual(len(provider.trigger_calls), 1)

    def test_refused_trigger_without_a_new_job_is_reported_as_not_started(self) -> None:
        provider = FakeProvider(trigger=_refused, listings=[lambda: [OLDER, BEFORE]])

        with self.assertRaises(dokploy_api.DokployScheduleExecutionFailed) as raised:
            _run(provider)

        failure = raised.exception
        self.assertEqual(failure.cause, "trigger_rejected")
        self.assertEqual(failure.deployment_status, "not_started")
        self.assertEqual(failure.deployment_id, "")
        self.assertEqual(len(provider.trigger_calls), 1)

    def test_timed_out_trigger_with_no_new_job_is_unknown_not_not_started(self) -> None:
        provider = FakeProvider(trigger=_timed_out, listings=[lambda: [OLDER, BEFORE]])

        with self.assertRaises(dokploy_api.DokployScheduleExecutionFailed) as raised:
            _run(provider)

        failure = raised.exception
        self.assertEqual(failure.cause, "trigger_outcome_unknown")
        self.assertEqual(failure.deployment_status, "unknown")
        self.assertIn("not retried", failure.message)
        self.assertEqual(len(provider.trigger_calls), 1)

    def test_failed_read_back_is_unknown_and_not_retried(self) -> None:
        provider = FakeProvider(trigger=_timed_out, listings=[_list_unavailable])

        with self.assertRaises(dokploy_api.DokployScheduleExecutionFailed) as raised:
            _run(provider)

        failure = raised.exception
        self.assertEqual(failure.cause, "trigger_outcome_unknown")
        self.assertIn("Read-back failed", failure.message)
        self.assertIn("not retried", failure.message)
        self.assertEqual(len(provider.trigger_calls), 1)

    def test_several_new_jobs_are_ambiguous_and_none_is_chosen(self) -> None:
        provider = FakeProvider(
            trigger=_timed_out,
            listings=[
                lambda: [
                    BEFORE,
                    _deployment("deployment-a", "running", timedelta(seconds=1)),
                    _deployment("deployment-b", "running", timedelta(seconds=2)),
                ]
            ],
        )

        with self.assertRaises(dokploy_api.DokployScheduleExecutionFailed) as raised:
            _run(provider)

        failure = raised.exception
        self.assertEqual(failure.cause, "trigger_outcome_unknown")
        self.assertEqual(failure.deployment_id, "")
        self.assertIn("deployment-a, deployment-b", failure.message)
        self.assertEqual(len(provider.trigger_calls), 1)

    def test_remote_command_failure_is_the_provider_answer(self) -> None:
        def remote_failure() -> JsonValue:
            raise dokploy_api.DokployRequestFailed(
                method="POST",
                path=TRIGGER_PATH,
                status_code=500,
                detail="Remote command failed with exit code 40",
                remote_command_failed=True,
            )

        provider = FakeProvider(trigger=remote_failure, listings=[lambda: [BEFORE]])

        with self.assertRaises(dokploy_api.DokployRequestFailed):
            _run(provider)

        self.assertEqual(len(provider.trigger_calls), 1)
        self.assertEqual(provider.list_calls, 0)


class DataWorkflowExecutionBudgetTests(unittest.TestCase):
    def test_restore_gets_a_bounded_budget_larger_than_the_deploy_timeout(self) -> None:
        resolve = dokploy_post_deploy.resolve_data_workflow_execution_timeout_seconds

        self.assertEqual(
            resolve(deploy_timeout_seconds=900, run_destructive_restore=True),
            dokploy_post_deploy.DEFAULT_ODOO_UPSTREAM_RESTORE_EXECUTION_TIMEOUT_SECONDS,
        )
        self.assertEqual(resolve(deploy_timeout_seconds=900, run_destructive_restore=False), 900)
        self.assertEqual(
            resolve(
                deploy_timeout_seconds=900,
                run_destructive_restore=True,
                requested_timeout_seconds=5400,
            ),
            5400,
        )

    def test_slow_restore_is_observed_past_its_trigger_timeout(self) -> None:
        target_definition = control_plane_dokploy.DokployTargetDefinition(
            context="opw",
            instance="testing",
            target_id="compose-123",
            target_name="opw-testing",
            deploy_timeout_seconds=900,
        )
        running = _deployment("deployment-restore", "running", timedelta(seconds=1))
        done: JsonObject = {
            **running,
            "status": "done",
            "logs": [
                "odoo_module_update_image_match=true",
                "odoo_module_update_modules_configured=true",
                "odoo_module_update_completed=true",
            ],
        }
        provider = FakeProvider(
            trigger=_timed_out,
            listings=[
                lambda: [BEFORE],
                lambda: [BEFORE, running],
                lambda: [BEFORE, done],
            ],
        )
        with (
            patch.object(dokploy_api, "dokploy_request", side_effect=provider.request),
            patch.object(
                dokploy_api,
                "fetch_dokploy_target_payload",
                return_value={
                    "env": (
                        "ODOO_DB_NAME=opw_testing\n"
                        "ODOO_UPSTREAM_HOST=source.example.com\n"
                        "ODOO_UPSTREAM_USER=root\n"
                        "ODOO_UPSTREAM_DB_NAME=upstream\n"
                        "ODOO_UPSTREAM_DB_USER=odoo\n"
                        "ODOO_UPSTREAM_FILESTORE_PATH=/source/filestore\n"
                    ),
                    "appName": "opw-testing-app",
                    "serverId": "server-123",
                },
            ),
            patch.object(dokploy_api, "find_matching_dokploy_schedule", return_value=None),
            patch.object(
                dokploy_api, "upsert_dokploy_schedule", return_value={"scheduleId": SCHEDULE_ID}
            ),
        ):
            evidence = dokploy_post_deploy.run_compose_post_deploy_update(
                host="https://dokploy.example.com",
                token="secret-token",
                target_definition=target_definition,
                env_file=None,
                run_destructive_restore=True,
            )

        self.assertEqual(evidence["schedule_deployment_id"], "deployment-restore")
        self.assertEqual(evidence["odoo_module_update_completed"], "true")
        self.assertEqual(
            provider.trigger_calls, [dokploy_api.DEFAULT_DOKPLOY_SCHEDULE_TRIGGER_TIMEOUT_SECONDS]
        )


if __name__ == "__main__":
    unittest.main()
