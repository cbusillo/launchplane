#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ATTENTION_ACTIONS = {
    "block",
    "candidate_failed",
    "candidate_stopped",
    "stack_unsupported",
    "update_branch",
}
BUILDING_ACTIONS = {
    "admit_collapsed_root",
    "build_candidate",
    "execute_stack_collapse",
    "land_batch",
    "observe_candidate",
    "plan_candidate",
    "plan_landing",
    "plan_stack_collapse",
}
WAITING_ACTIONS = {"wait_for_checks", "wait_for_root_checks"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render merge-train controller PR feedback payloads."
    )
    parser.add_argument("--response-file", required=True, type=Path)
    parser.add_argument("--source", default="workflow:merge-train-runner")
    parser.add_argument(
        "--phase",
        choices=("controller", "batch-candidate", "stack-collapse", "batch-landing"),
        default="controller",
    )
    args = parser.parse_args()

    response = json.loads(args.response_file.read_text(encoding="utf-8"))
    payloads = build_feedback_payloads(response=response, source=args.source, phase=args.phase)
    json.dump(payloads, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    print(f"Rendered {len(payloads)} feedback payload(s).", file=sys.stderr)
    return 0


def build_feedback_payloads(
    *,
    response: dict[str, Any],
    source: str = "workflow:merge-train-runner",
    phase: str = "controller",
) -> list[dict[str, object]]:
    result = _as_dict(response.get("result"))
    records = _as_dict(response.get("records"))
    repository = _required_string(result.get("repository"), "repository")
    base_branch = _required_string(result.get("base_branch"), "base_branch")
    controller_action = _controller_action(result=result, phase=phase)
    event = _feedback_event(controller_action=controller_action, result=result)
    if event == "skip":
        return []

    pull_request_numbers = _pull_request_numbers(result)
    if not pull_request_numbers:
        return []

    controller_record_id = _controller_record_id(records)
    message = _feedback_message(
        controller_action=controller_action,
        event=event,
        result=result,
        controller_record_id=controller_record_id,
    )
    return [
        {
            "schema_version": 1,
            "repository": repository,
            "base_branch": base_branch,
            "pull_request_number": pull_request_number,
            "event": event,
            "controller_action": controller_action,
            "controller_record_id": controller_record_id,
            "message": message,
            "source": source,
        }
        for pull_request_number in pull_request_numbers
    ]


def _controller_action(*, result: dict[str, Any], phase: str) -> str:
    explicit_action = _string(result.get("controller_action"))
    if explicit_action:
        return explicit_action

    mode = _required_string(result.get("mode"), "mode")
    phase_actions: dict[tuple[str, str], str] = {
        ("batch-candidate", "plan"): _batch_candidate_plan_action(result),
        ("batch-candidate", "build"): "build_candidate",
        ("batch-candidate", "observe"): "observe_candidate",
        ("stack-collapse", "execute"): "execute_stack_collapse",
        ("stack-collapse", "admit"): "admit_collapsed_root",
        ("batch-landing", "plan"): "plan_landing",
        ("batch-landing", "land"): "land_batch",
    }
    action = phase_actions.get((phase, mode))
    if not action:
        raise ValueError(f"unsupported merge train feedback phase/mode {phase}:{mode}")
    return action


def _batch_candidate_plan_action(result: dict[str, Any]) -> str:
    if _as_dict(result.get("stack_collapse_plan")):
        return "plan_stack_collapse"
    return "plan_candidate"


def _feedback_event(*, controller_action: str, result: dict[str, Any]) -> str:
    candidate = _as_dict(result.get("candidate"))
    landing_plan = _as_dict(result.get("landing_plan"))
    candidate_status = _string(candidate.get("status"))
    required_checks_status = _string(candidate.get("required_checks_status"))

    if controller_action in {"idle", "candidate_stopped"} and candidate_status == "stale":
        return "stale_policy"
    if _landing_plan_stale(landing_plan):
        return "stale_policy"
    if controller_action == "batch_landed" or _landing_plan_complete(landing_plan):
        return "completed"
    if candidate_status in {"blocked", "failed", "stale"}:
        return "blocked" if candidate_status != "stale" else "stale_policy"
    if required_checks_status in {"pending", "unknown"}:
        return "waiting"
    if controller_action in WAITING_ACTIONS:
        return "waiting"
    if controller_action in ATTENTION_ACTIONS:
        return "blocked"
    if controller_action in BUILDING_ACTIONS:
        return "building"
    return "skip"


def _feedback_message(
    *,
    controller_action: str,
    event: str,
    result: dict[str, Any],
    controller_record_id: str,
) -> str:
    if event == "completed":
        return "Launchplane finished the merge-train step for this pull request."
    if event == "stale_policy":
        return "Launchplane stopped using this train record because its stored evidence is stale."
    if event == "blocked":
        detail = _blocking_detail(result)
        if detail:
            return f"Launchplane needs attention before the train can continue: {detail}"
        return "Launchplane needs attention before the train can continue."
    if event == "waiting":
        return "Launchplane is waiting for required checks or fresh GitHub state."
    if controller_record_id:
        return f"Launchplane is advancing `{controller_action}` with `{controller_record_id}`."
    return f"Launchplane is advancing `{controller_action}` for this train pass."


def _blocking_detail(result: dict[str, Any]) -> str:
    dry_run_result = _as_dict(result.get("dry_run_result"))
    detail = _string(dry_run_result.get("next_action_detail"))
    if detail:
        return detail
    candidate = _as_dict(result.get("candidate"))
    required_checks_status = _string(candidate.get("required_checks_status"))
    if required_checks_status == "fail":
        return "candidate required checks failed"
    return ""


def _pull_request_numbers(result: dict[str, Any]) -> list[int]:
    containers = (
        _as_dict(result.get("landing_plan")).get("entries"),
        _as_dict(result.get("candidate")).get("entries"),
        _as_dict(result.get("stack_collapse_plan")).get("entries"),
    )
    seen: set[int] = set()
    numbers: list[int] = []
    for container in containers:
        for entry in _as_list(container):
            number = _as_dict(entry).get("pull_request_number")
            if isinstance(number, int) and number > 0 and number not in seen:
                seen.add(number)
                numbers.append(number)
    return numbers


def _controller_record_id(records: dict[str, Any]) -> str:
    for key in (
        "merge_train_batch_landing_plan_record_id",
        "merge_train_batch_candidate_record_id",
        "merge_train_stack_collapse_plan_record_id",
        "merge_train_run_id",
    ):
        value = _string(records.get(key))
        if value:
            return value
    return ""


def _landing_plan_complete(landing_plan: dict[str, Any]) -> bool:
    entries = _as_list(landing_plan.get("entries"))
    return bool(entries) and all(
        _string(_as_dict(entry).get("status")) == "merged" for entry in entries
    )


def _landing_plan_stale(landing_plan: dict[str, Any]) -> bool:
    entries = _as_list(landing_plan.get("entries"))
    return bool(entries) and all(
        _string(_as_dict(entry).get("status")) in {"merged", "stale"}
        for entry in entries
    ) and any(_string(_as_dict(entry).get("status")) == "stale" for entry in entries)


def _required_string(value: object, field_name: str) -> str:
    normalized = _string(value)
    if not normalized:
        raise ValueError(f"controller response missing {field_name}")
    return normalized


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


if __name__ == "__main__":
    raise SystemExit(main())
