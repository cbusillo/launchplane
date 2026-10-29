import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

import click

from control_plane.runner_host_hygiene_configuration import (
    RunnerHostHygieneDispatch,
    RunnerHostHygieneDispatchIntent,
)
from control_plane.runner_host_hygiene_metadata import (
    RunnerHostHygieneMetadata,
    RunnerHostHygieneMetadataRequest,
)
from control_plane.contracts.runner_host_hygiene_idle import RunnerHostHygieneIdleObservation
from control_plane.workflows.runner_host_hygiene_audit_spool import RunnerHostHygieneAuditSpool
from control_plane.workflows.runner_host_hygiene_executor import (
    BearerTokenProvider,
    GitHubIdleEvidenceReader,
    GitHubRunStateReader,
    RunnerHostHygieneExecutorRequest,
    build_local_command_runner,
    build_refreshing_service_audit_poster,
    execute_runner_host_hygiene_executor,
    validate_local_executor_environment,
    _github_binding_subject_key,
)

PostServiceJson = Callable[[str, dict[str, object]], object]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def build_service_hygiene_post(
    *,
    service_url: str,
    bearer_token_provider: BearerTokenProvider,
    timeout_seconds: int = 900,
) -> PostServiceJson:
    base = service_url.rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Hygiene service transport requires a trusted HTTPS URL.")

    def post(path: str, payload: dict[str, object]) -> object:
        request = Request(
            f"{base}/v1/service/runner-host-hygiene/{path}",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {bearer_token_provider()}",
                "Content-Type": "application/json",
            },
        )
        with build_opener(_NoRedirect()).open(
            request, timeout=timeout_seconds if path == "metadata" else 30
        ) as response:
            if response.status != 200:
                raise ValueError("Hygiene service read failed; local action remains blocked.")
            return json.load(response)

    return post


def build_service_hygiene_readers(
    *,
    request: RunnerHostHygieneExecutorRequest,
    configuration_sha256: str,
    post: PostServiceJson,
) -> tuple[GitHubIdleEvidenceReader, GitHubRunStateReader]:
    pending_sample: dict[str, tuple[RunnerHostHygieneIdleObservation, ...]] = {}

    def read_metadata(**selection: object) -> RunnerHostHygieneMetadata:
        payload = RunnerHostHygieneMetadataRequest.model_validate(
            {
                "configuration_sha256": configuration_sha256,
                "current_runner_name": request.current_runner_name,
                **selection,
            }
        )
        response = RunnerHostHygieneMetadata.model_validate(
            post("metadata", payload.model_dump(mode="json"))
        )
        if response.configuration_sha256 != configuration_sha256:
            raise ValueError("Hygiene evidence belongs to another configuration.")
        return response

    def idle(
        repository: str, lane: str, runner: str, exclude: bool
    ) -> tuple[RunnerHostHygieneIdleObservation, ...]:
        expected_exclusion = (
            repository == request.repository_scope.lower()
            and lane == request.execution_lane.lower()
        )
        if exclude != expected_exclusion:
            raise ValueError("Hygiene self-exclusion must match the dispatched executor.")
        matches = [
            binding
            for binding in request.github_idle_bindings
            if (binding.repository_scope, binding.execution_lane, binding.runner_name)
            == (repository, lane, runner)
        ]
        if len(matches) != 1:
            raise ValueError("Hygiene evidence requires an exact dispatched binding.")
        subject = _github_binding_subject_key(matches[0])
        if subject not in pending_sample:
            observations = read_metadata(kind="idle").observations
            pending_sample.clear()
            pending_sample.update(
                {
                    _github_binding_subject_key(binding): tuple(
                        observation
                        for observation in observations
                        if observation.subject_key == _github_binding_subject_key(binding)
                    )
                    for binding in request.github_idle_bindings
                }
            )
        return pending_sample.pop(subject)

    def runs(repository: str, run_ids: tuple[int, ...]) -> Mapping[int, str]:
        roots = [
            root
            for root in request.generated_run_cache_roots
            if root.source_repository == repository
            and (
                not request.target_generated_cache_key
                or root.key == request.target_generated_cache_key
            )
        ]
        if not roots:
            raise ValueError("Run evidence requires the dispatched generated cache.")
        states: dict[int, str] = {}
        for start in range(0, len(run_ids), 128):
            batch = run_ids[start : start + 128]
            response = read_metadata(kind="run_states", cache_key=roots[0].key, run_ids=batch)
            if set(response.run_states) != set(batch):
                raise ValueError("Run evidence does not cover the exact requested runs.")
            states.update(response.run_states)
        return states

    return idle, runs


def execute_service_hygiene_dispatch(
    *,
    intent: RunnerHostHygieneDispatchIntent,
    post: PostServiceJson,
    execution_lane: str,
    repository_scope: str,
    service_url: str,
    bearer_token_provider: BearerTokenProvider,
    audit_spool_root: Path,
    artifact_directory: Path,
) -> tuple[dict[str, object], ...]:
    dispatch = RunnerHostHygieneDispatch.model_validate(
        post("dispatch", intent.model_dump(mode="json"))
    )
    if dispatch.execution_lane != execution_lane or not dispatch.requests:
        raise ValueError("Hygiene dispatch does not match the scheduler bootstrap lane.")
    for request in dispatch.requests:
        if (
            request.execution_lane != execution_lane
            or request.repository_scope != repository_scope
            or request.current_runner_name != intent.current_runner_name
        ):
            raise ValueError("Hygiene dispatch does not match the running job.")
        validate_local_executor_environment(request=request)
    results: list[dict[str, object]] = []
    for index, request in enumerate(dispatch.requests):
        idle, runs = build_service_hygiene_readers(
            request=request,
            configuration_sha256=dispatch.configuration_sha256,
            post=post,
        )
        try:
            result = execute_runner_host_hygiene_executor(
                request=request,
                remote_runner=build_local_command_runner(),
                audit_poster=build_refreshing_service_audit_poster(
                    service_url=service_url,
                    bearer_token_provider=bearer_token_provider,
                ),
                audit_spool=RunnerHostHygieneAuditSpool(
                    root=audit_spool_root,
                    artifact_file=artifact_directory
                    / f"runner-host-hygiene-audit-evidence-{index}.json",
                ),
                github_idle_evidence_reader=idle,
                github_run_state_reader=runs,
            )
            results.append({**result.model_dump(mode="json"), "mutate": request.mutate})
        except (click.ClickException, OSError, ValueError):
            results.append(
                {
                    "status": "failed",
                    "audit_record_key": request.audit_record_key,
                    "execution_failed": True,
                }
            )
    return tuple(results)


@click.command()
@click.option("--intent-file", type=Path, required=True)
@click.option("--service-url", required=True)
@click.option("--execution-lane", required=True)
@click.option("--audit-spool-root", type=Path, required=True)
@click.option("--artifact-directory", type=Path, required=True)
def main(
    intent_file: Path,
    service_url: str,
    execution_lane: str,
    audit_spool_root: Path,
    artifact_directory: Path,
) -> None:
    import os
    from control_plane.cli_runner_lanes import _runner_host_hygiene_bearer_token

    intent = RunnerHostHygieneDispatchIntent.model_validate_json(intent_file.read_text())
    if intent.current_runner_name != os.environ.get("RUNNER_NAME"):
        raise click.ClickException("Hygiene intent must identify the current runner.")

    def bearer_token() -> str:
        return _runner_host_hygiene_bearer_token(
            service_url=service_url, token_env="LAUNCHPLANE_TOKEN"
        )

    post = build_service_hygiene_post(
        service_url=service_url,
        bearer_token_provider=bearer_token,
        timeout_seconds=intent.timeout_seconds if intent.mode == "manual" else 900,
    )
    results = execute_service_hygiene_dispatch(
        intent=intent,
        post=post,
        execution_lane=execution_lane,
        repository_scope=os.environ.get("GITHUB_REPOSITORY", ""),
        service_url=service_url,
        bearer_token_provider=bearer_token,
        audit_spool_root=audit_spool_root,
        artifact_directory=artifact_directory,
    )
    click.echo(json.dumps(results, sort_keys=True))
    if any(
        result.get("execution_failed")
        or result.get("audit_delivery_pending")
        or (result.get("mutate") and result["status"] not in {"completed", "not_needed"})
        for result in results
    ):
        raise click.ClickException("Hygiene actions require attention; inspect accepted audits.")


if __name__ == "__main__":
    main()
