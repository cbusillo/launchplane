"""Render merge-train controller results as PR feedback payloads.

Standard library only: the Merge Train Runner workflow runs this on a runner
without Launchplane's dependencies installed.
"""

from __future__ import annotations

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


def build_feedback_payloads(
    *,
    response: dict[str, Any],
    source: str = "workflow:merge-train-runner",
    phase: str = "controller",
) -> list[dict[str, object]]:
    result = _as_dict(response.get("result"))
    records = _as_dict(response.get("records"))
    controller_action = _controller_action(result=result, phase=phase)
    repository, base_branch = _response_identity(result=result, phase=phase)
    event = _feedback_event(controller_action=controller_action, result=result)
    if event == "skip":
        return []

    pull_request_numbers = _pull_request_numbers(
        result, controller_action=controller_action, phase=phase
    )
    if not pull_request_numbers:
        return []

    controller_record_id = _controller_record_id(records)
    message = _feedback_message(
        controller_action=controller_action,
        event=event,
        result=result,
        controller_record_id=controller_record_id,
        phase=phase,
    )
    payloads: list[dict[str, object]] = [
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
    if controller_action in {"plan_candidate", "candidate_failed"}:
        payloads.extend(
            {
                "schema_version": 1,
                "repository": repository,
                "base_branch": base_branch,
                "pull_request_number": pull_request_number,
                "event": "blocked",
                "controller_action": controller_action,
                "controller_record_id": controller_record_id,
                "message": held_out_message,
                "source": source,
            }
            for pull_request_number, held_out_message in _held_out_messages(
                result=result,
                base_branch=base_branch,
                stopped=controller_action == "candidate_failed",
            )
        )
    return payloads


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
    if not _as_dict(result.get("candidate")):
        next_action = _string(result.get("next_action"))
        if next_action in {"block", "wait_for_checks", "update_branch", "stack_unsupported"}:
            return next_action
    return "plan_candidate"


def _response_identity(*, result: dict[str, Any], phase: str) -> tuple[str, str]:
    if phase == "controller":
        return _container_identity(result, "controller result")

    mode = _required_string(result.get("mode"), "mode")
    candidate = _as_dict(result.get("candidate"))
    dry_run_result = _as_dict(result.get("dry_run_result"))
    stack_collapse_plan = _as_dict(result.get("stack_collapse_plan"))
    landing_plan = _as_dict(result.get("landing_plan"))

    if phase == "batch-candidate":
        if mode in {"build", "observe"}:
            return _container_identity(candidate, "batch candidate")
        if mode != "plan":
            raise ValueError(f"unsupported merge train feedback phase/mode {phase}:{mode}")
        if candidate:
            return _identity_with_cross_check(
                primary=candidate,
                primary_name="batch candidate",
                secondary=dry_run_result,
                secondary_name="candidate dry-run result",
            )
        if stack_collapse_plan:
            return _identity_with_cross_check(
                primary=stack_collapse_plan,
                primary_name="stack collapse plan",
                secondary=dry_run_result,
                secondary_name="candidate dry-run result",
            )
        if _string(result.get("next_action")) in {
            "block",
            "wait_for_checks",
            "update_branch",
            "stack_unsupported",
        }:
            return _container_identity(dry_run_result, "candidate dry-run result")
        raise ValueError("batch-candidate response missing candidate or stack collapse plan")

    if phase == "stack-collapse":
        if mode == "execute":
            return _container_identity(stack_collapse_plan, "stack collapse plan")
        if mode == "admit":
            return _identity_with_cross_check(
                primary=candidate,
                primary_name="batch candidate",
                secondary=dry_run_result,
                secondary_name="stack-collapse dry-run result",
            )
        raise ValueError(f"unsupported merge train feedback phase/mode {phase}:{mode}")

    if phase == "batch-landing":
        if mode not in {"plan", "land"}:
            raise ValueError(f"unsupported merge train feedback phase/mode {phase}:{mode}")
        return _identity_with_cross_check(
            primary=landing_plan,
            primary_name="landing plan",
            secondary=stack_collapse_plan,
            secondary_name="landing stack collapse plan",
        )

    raise ValueError(f"unsupported merge train feedback phase {phase}")


def _identity_with_cross_check(
    *,
    primary: dict[str, Any],
    primary_name: str,
    secondary: dict[str, Any],
    secondary_name: str,
) -> tuple[str, str]:
    identity = _container_identity(primary, primary_name)
    if not secondary:
        return identity
    secondary_identity = _container_identity(secondary, secondary_name)
    if secondary_identity != identity:
        raise ValueError(
            f"merge train feedback identity mismatch between {primary_name} and {secondary_name}"
        )
    return identity


def _container_identity(container: dict[str, Any], container_name: str) -> tuple[str, str]:
    return (
        _required_string(container.get("repository"), f"{container_name} repository"),
        _required_string(container.get("base_branch"), f"{container_name} base_branch"),
    )


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
    phase: str,
) -> str:
    if event == "completed":
        batch_pr = _as_dict(result.get("landing_plan")).get("candidate_pull_request_number")
        if type(batch_pr) is int and batch_pr > 0:
            return f"Launchplane landed this pull request through protected batch PR #{batch_pr}."
        return "Launchplane finished the merge-train step for this pull request."
    if event == "stale_policy":
        return "Launchplane stopped using this train record because its stored evidence is stale."
    if controller_action == "update_branch" and phase == "controller":
        if _as_dict(result.get("branch_update_result")).get("status") == "updated":
            return (
                "Launchplane updated this pull request's branch and is waiting for "
                "fresh mergeability and required checks."
            )
        return "Launchplane needs to refresh this pull request's branch before continuing."
    if event == "blocked":
        applied = _as_dict(result.get("block_result"))
        if applied.get("status") == "blocked":
            selected = _as_dict(_as_dict(result.get("dry_run_result")).get("selected_pr"))
            detail = (
                "pull request has merge conflicts"
                if selected.get("mergeable") == "conflicting"
                else "current-head review or required checks failed"
                if selected.get("owner_review_required") is True
                else "required checks failed"
            )
            return (
                f"Launchplane held this pull request out of the train: {detail}. "
                "Other eligible pull requests can proceed. Resolve the failure and "
                "remove the block label to rejoin the queue."
            )
        detail = _blocking_detail(result)
        if (
            phase == "batch-candidate"
            and controller_action == "block"
            and "candidate" not in result
        ):
            selected = _as_dict(_as_dict(result.get("dry_run_result")).get("selected_pr"))
            if selected.get("mergeable") == "conflicting":
                detail = "pull request has merge conflicts"
            elif selected.get("required_checks_status") == "fail" and (
                selected.get("owner_review_required") is not True
            ):
                detail = "required checks failed"
        if detail:
            return f"Launchplane needs attention before the train can continue: {detail}"
        return "Launchplane needs attention before the train can continue."
    if event == "waiting":
        selected = _as_dict(_as_dict(result.get("dry_run_result")).get("selected_pr"))
        if selected.get("owner_review_required") is True and "candidate" not in result:
            return (
                "Launchplane is waiting for current-head Client review and required checks "
                "on this pull request."
            )
        return "Launchplane is waiting for required checks or fresh GitHub state."
    if controller_record_id:
        return f"Launchplane is advancing `{controller_action}` with `{controller_record_id}`."
    return f"Launchplane is advancing `{controller_action}` for this train pass."


def _blocking_detail(result: dict[str, Any]) -> str:
    blocking_reason = _as_dict(result.get("blocking_reason"))
    blocking_message = _string(blocking_reason.get("message"))
    if blocking_message:
        return blocking_message
    dry_run_result = _as_dict(result.get("dry_run_result"))
    detail = _string(dry_run_result.get("next_action_detail"))
    if detail:
        return detail
    candidate = _as_dict(result.get("candidate"))
    required_checks_status = _string(candidate.get("required_checks_status"))
    if required_checks_status == "fail":
        return "candidate required checks failed"
    return ""


def _held_out_messages(
    *, result: dict[str, Any], base_branch: str, stopped: bool
) -> list[tuple[int, str]]:
    """Tell each held-out pull request which queued pull requests it conflicts with.

    A stopped failed batch reports only the pull requests its own probe just held
    out, so later passes do not repeat the message.
    """
    probed: set[object] | None = None
    if stopped:
        probe = _as_dict(result.get("conflict_probe"))
        probed = {
            _as_dict(entry).get("pull_request_number")
            for entry in _as_list(probe.get("held_out"))
            if probe.get("status") == "ran"
            and _as_dict(entry).get("pull_request_number")
            in _as_list(probe.get("pull_request_numbers"))
        }
    queue_state = (
        "The failed batch ahead of it stays stopped until someone resolves it."
        if stopped
        else "The rest of the queue continues without it."
    )
    messages: list[tuple[int, str]] = []
    for held_out in _as_list(_as_dict(result.get("candidate")).get("held_out")):
        held_out_entry = _as_dict(held_out)
        number = held_out_entry.get("pull_request_number")
        if not isinstance(number, int) or number <= 0:
            continue
        if probed is not None and number not in probed:
            continue
        conflicts_with: list[int] = [
            other
            for other in _as_list(held_out_entry.get("conflicts_with"))
            if isinstance(other, int) and other > 0
        ]
        if conflicts_with:
            conflict = "conflicts with " + ", ".join(f"#{other}" for other in conflicts_with)
            conflict += ", queued ahead of it"
        else:
            conflict = f"does not merge cleanly onto `{base_branch}`"
        messages.append(
            (
                number,
                f"Launchplane left this pull request out of the merge-train batch: it "
                f"{conflict}. {queue_state} It rejoins the queue when its head changes; "
                "resolve the conflict, typically after the others land.",
            )
        )
    return messages


def _pull_request_numbers(
    result: dict[str, Any], *, controller_action: str, phase: str
) -> list[int]:
    selected_actions = {"wait_for_checks", "block", "update_branch"}
    if controller_action in selected_actions and "candidate" not in result:
        selected = _as_dict(_as_dict(result.get("dry_run_result")).get("selected_pr"))
        number = selected.get("number")
        # Ordinary waits and refreshes have a selected PR before any candidate exists.
        if (
            controller_action in {"wait_for_checks", "update_branch"}
            or phase == "batch-candidate"
            or "merge_train_batch_candidate_record_id" in result
            or selected.get("owner_review_required") is True
            or _as_dict(result.get("block_result")).get("status") == "blocked"
        ) and (isinstance(number, int) and number > 0):
            return [number]
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
    return (
        bool(entries)
        and all(_string(_as_dict(entry).get("status")) in {"merged", "stale"} for entry in entries)
        and any(_string(_as_dict(entry).get("status")) == "stale" for entry in entries)
    )


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
