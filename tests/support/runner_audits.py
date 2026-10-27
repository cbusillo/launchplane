from control_plane.contracts.runner_lane_inventory import (
    RunnerLaneRecord,
    build_runner_lane_inventory,
)
from control_plane.contracts.runner_lane_registration import (
    RunnerLaneLifecycleOperation,
    RunnerLaneRegistrationAuditRecord,
    RunnerLaneRegistrationAuditStatus,
    RunnerLaneRegistrationPlan,
    RunnerLaneRegistrationPolicy,
    RunnerLaneRegistrationRequest,
    plan_runner_lane_registration,
    plan_runner_lane_retirement,
)


def lifecycle_audit(
    *,
    operation: RunnerLaneLifecycleOperation = "register",
    key: str = "runner-audit/identity-test",
    status: RunnerLaneRegistrationAuditStatus = "planned",
    request_changes: dict[str, object] | None = None,
) -> RunnerLaneRegistrationAuditRecord:
    request = RunnerLaneRegistrationRequest.model_validate(
        {
            "operation": operation,
            "repository": "example/control",
            "host_name": "build-1",
            "lane_name": "general-1",
            "registration_root": "/srv/runners",
            "labels": ("general", "candidate") if operation != "retire" else (),
            "expected_runner_id": 7 if operation == "relabel" else None,
            "expected_custom_labels": ("general",) if operation == "relabel" else (),
            "mutate": True,
            "audit_record_key": key,
            **(request_changes or {}),
        }
    )
    inventory = build_runner_lane_inventory(
        repository=request.repository,
        observed_at="2026-10-16T23:00:00Z",
        lanes=()
        if operation == "register"
        else (
            RunnerLaneRecord(
                repository=request.repository,
                name=request.lane_name,
                github_id=request.expected_runner_id or 7,
                status="online",
                busy=False,
                labels=("self-hosted", "general"),
                observed_at="2026-10-16T23:00:00Z",
            ),
        ),
    )
    policy = RunnerLaneRegistrationPolicy(
        allowed_repositories=(request.repository,),
        approved_hosts=(request.host_name,),
        allowed_registration_roots=(request.registration_root,),
        required_labels=("general",),
    )
    if operation == "relabel":
        plan = RunnerLaneRegistrationPlan(
            operation=operation,
            status="ready",
            repository=request.repository,
            host_name=request.host_name,
            lane_name=request.lane_name,
            registration_root=request.registration_root,
            labels=request.labels,
            mutate=request.mutate,
            audit_record_key=key,
            summary="replace exact existing runner custom labels",
        )
    else:
        planner = (
            plan_runner_lane_registration
            if operation == "register"
            else plan_runner_lane_retirement
        )
        plan = planner(policy=policy, request=request, inventory=inventory)
    return RunnerLaneRegistrationAuditRecord(
        audit_record_key=key,
        status=status,
        request=request,
        plan=plan,
        pre_inventory=inventory,
        post_inventory=inventory if status != "planned" else None,
        message=f"{operation} {status} evidence",
    )
