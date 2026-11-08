from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import selectors
import subprocess
import tempfile
import time
from typing import Protocol
import zipfile

from control_plane.build_provenance import BuildProvenanceTransport

from control_plane.contracts.runner_lane_inventory import RunnerLaneInventory
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationTokenRecord
from control_plane.github_payload import repository_full_name
from control_plane.merge_train_github import MergeTrainGitHubError


ADDON_RUNNER_LABEL_PREFIX = "lp-addon-"
ADDON_RECIPE_ARTIFACT_PREFIX = "addon-runner-recipe-"
ADDON_RECIPE_FILES = frozenset({"Dockerfile", "entrypoint.sh", "source-commit"})
CONTAINER_TIMEOUT_SECONDS = 45 * 60


@dataclass(frozen=True)
class AddonRunnerRequest:
    repository: str
    run_id: int
    run_attempt: int
    job_id: int

    def __post_init__(self) -> None:
        repository_full_name(self.repository)
        if min(self.run_id, self.run_attempt, self.job_id) <= 0:
            raise ValueError("Addon runner requires positive run, attempt and job identities.")

    @property
    def label(self) -> str:
        return f"{ADDON_RUNNER_LABEL_PREFIX}{self.run_id}-{self.run_attempt}"

    @property
    def name(self) -> str:
        return f"lp-addon-{self.run_id}-{self.run_attempt}-{self.job_id}"


class AddonRunnerClient(Protocol):
    def read_addon_job(self, request: AddonRunnerRequest) -> dict[str, object]: ...

    def read_runner_lane_inventory(self, *, repository: str) -> RunnerLaneInventory: ...

    def fetch_registration_token(
        self, *, repository: str
    ) -> tuple[str, RunnerLaneRegistrationTokenRecord]: ...

    def delete_runner(self, *, repository: str, runner_id: int) -> None: ...


Command = Callable[..., subprocess.CompletedProcess[str]]
Receipt = Callable[[dict[str, object]], None]
StartRunner = Callable[[str, str, Receipt], subprocess.CompletedProcess[str]]


def read_addon_job(
    *,
    transport: BuildProvenanceTransport,
    request: AddonRunnerRequest,
    repository_id: int,
    include_recipe: bool = True,
) -> dict[str, object]:
    root = f"/repos/{request.repository}/actions"
    run = transport.get_json(f"{root}/runs/{request.run_id}")
    job = transport.get_json(f"{root}/jobs/{request.job_id}")
    if not isinstance(run, dict) or not isinstance(job, dict):
        raise ValueError("Addon run and job identity are unavailable.")
    repository = run.get("repository")
    head_repository = run.get("head_repository")
    if (
        not isinstance(repository, dict)
        or not isinstance(head_repository, dict)
        or repository.get("id") != repository_id
        or repository.get("private") is not True
        or head_repository.get("id") != repository_id
        or run.get("id") != request.run_id
        or run.get("run_attempt") != request.run_attempt
        or run.get("event") not in {"push", "pull_request"}
        or job.get("id") != request.job_id
        or job.get("run_id") != request.run_id
        or not isinstance(job.get("labels"), list)
        or request.label not in job["labels"]
    ):
        raise ValueError("The selected run is not the current private addon job.")
    commit = run.get("head_sha")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Addon job source is unavailable.")
    result: dict[str, object] = {
        "status": job.get("status"),
        "conclusion": job.get("conclusion"),
        "source_commit": commit,
        "runner_id": job.get("runner_id"),
        "job_url": job.get("html_url"),
    }
    if result["status"] != "queued" or not include_recipe:
        return result
    artifacts = transport.get_json(f"{root}/runs/{request.run_id}/artifacts?per_page=100")
    if not isinstance(artifacts, dict) or not isinstance(artifacts.get("artifacts"), list):
        raise ValueError("Addon recipe artifact inventory is unavailable.")
    matches = [
        artifact
        for artifact in artifacts["artifacts"]
        if isinstance(artifact, dict)
        and artifact.get("name") == f"{ADDON_RECIPE_ARTIFACT_PREFIX}{request.run_attempt}"
        and artifact.get("expired") is False
    ]
    if len(matches) != 1:
        raise ValueError("The current attempt's addon runner recipe is unavailable.")
    artifact = matches[0]
    origin = artifact.get("workflow_run")
    artifact_id = artifact.get("id")
    if (
        type(artifact_id) is not int
        or artifact_id <= 0
        or not isinstance(origin, dict)
        or origin.get("id") != request.run_id
        or origin.get("head_sha") != commit
    ):
        raise ValueError("Addon recipe artifact belongs to another source.")
    archive = transport.get_bytes(f"{root}/artifacts/{artifact_id}/zip")
    if (
        len(archive) > 131072
        or artifact.get("digest") != "sha256:" + hashlib.sha256(archive).hexdigest()
    ):
        raise ValueError("Addon recipe artifact digest is unverified.")
    result["recipe"] = _read_recipe_archive(archive, commit)
    return result


def _read_recipe_archive(archive: bytes, commit: str) -> dict[str, str]:
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            files = source.infolist()
            if (
                len(files) != len(ADDON_RECIPE_FILES)
                or {item.filename for item in files} != ADDON_RECIPE_FILES
            ):
                raise ValueError("Addon runner archive contains unexpected files.")
            if any(item.file_size > 65536 for item in files):
                raise ValueError("Addon runner archive exceeds its source limit.")
            recipe = {item.filename: source.read(item).decode() for item in files}
    except (zipfile.BadZipFile, UnicodeDecodeError) as error:
        raise ValueError("Addon runner recipe is not a valid source archive.") from error
    recipe["source-commit"] = recipe["source-commit"].strip()
    if recipe["source-commit"] != commit:
        raise ValueError("Addon recipe does not match the job's exact source.")
    return recipe


def run_ephemeral_addon_job(
    *,
    client: AddonRunnerClient,
    request: AddonRunnerRequest,
    output: Path,
    command: Command = subprocess.run,
    start_runner: StartRunner | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "repository": request.repository,
        "run_id": request.run_id,
        "run_attempt": request.run_attempt,
        "job_id": request.job_id,
        "status": "failed",
        "container_removed": False,
        "registration_removed": False,
    }
    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o700)
    _write_result(output, result)
    job = client.read_addon_job(request)
    if job.get("status") != "queued":
        result.update(status="not_queued", job_status=job.get("status", "unknown"))
        _write_result(output, result)
        return result
    if any(
        lane.name == request.name
        for lane in client.read_runner_lane_inventory(repository=request.repository).lanes
    ):
        raise ValueError("An earlier addon registration must be reconciled before execution.")
    container_id = ""
    volumes: tuple[str, ...] = ()
    image = ""
    registration_requested = False
    token = ""
    try:
        with tempfile.TemporaryDirectory(prefix="addon-runner-") as temporary:
            context = Path(temporary)
            _write_recipe(context, job)
            image = f"launchplane-addon-runner:{request.name}"
            result["recipe_image"] = image
            _write_result(output, result)
            _docker(command, ["build", "--tag", image, str(context)], timeout=600)
            result["container_creation_requested"] = True
            _write_result(output, result)
            created = _docker(
                command,
                [
                    "create",
                    "--interactive",
                    "--privileged",
                    "--cpus",
                    "8",
                    "--memory",
                    "16g",
                    "--memory-swap",
                    "16g",
                    "--pids-limit",
                    "4096",
                    "--name",
                    request.name,
                    "--label",
                    f"launchplane.ephemeral-addon={request.name}",
                    "--env",
                    f"RUNNER_REPOSITORY={request.repository}",
                    "--env",
                    f"RUNNER_NAME={request.name}",
                    "--env",
                    f"RUNNER_LABEL={request.label}",
                    image,
                ],
            ).stdout.strip()
            if not re.fullmatch(r"[0-9a-f]{64}", created):
                raise ValueError("Docker did not return an immutable container identity.")
            container_id = created
            result["container_id"] = container_id
            _write_result(output, result)
            mounts = json.loads(
                _docker(command, ["inspect", "--format", "{{json .Mounts}}", container_id]).stdout
            )
            volumes = tuple(str(mount["Name"]) for mount in mounts if mount.get("Type") == "volume")
            result["volumes"] = volumes
            _write_result(output, result)
            token, _ = client.fetch_registration_token(repository=request.repository)
            registration_requested = True
            result.update(registration_requested=True, recipe_image=image)
            _write_result(output, result)

            def registered(receipt: dict[str, object]) -> None:
                runner_id = receipt.get("runner_id")
                if (
                    receipt.get("ephemeral") is not True
                    or not isinstance(runner_id, int)
                    or isinstance(runner_id, bool)
                    or runner_id <= 0
                ):
                    raise ValueError("Native one-job registration was not proved.")
                result.update(runner_id=runner_id, ephemeral=True)
                _write_result(output, result)

            started = (start_runner or _start_runner_process)(container_id, token, registered)
            diagnostic = (started.stdout + started.stderr).replace(token, "[REDACTED]")
            (output / "runner.log").write_text(diagnostic)
            result["listener_exit_code"] = started.returncode
            observed = client.read_addon_job(request)
            deadline = time.monotonic() + 30
            while (
                observed.get("status") in {"queued", "in_progress"} and time.monotonic() < deadline
            ):
                time.sleep(1)
                observed = client.read_addon_job(request)
            result.update(
                job_status=observed.get("status", "unknown"),
                job_conclusion=observed.get("conclusion", ""),
                job_runner_id=observed.get("runner_id"),
            )
            if (
                started.returncode == 0
                and observed.get("status") == "completed"
                and observed.get("conclusion") == "success"
                and observed.get("runner_id") == result.get("runner_id")
            ):
                result["status"] = "completed"
    finally:
        result.update(
            container_id=container_id,
            volumes=volumes,
            recipe_image=image,
            registration_requested=registration_requested,
        )
        _cleanup_ephemeral_addon_job(client, request, result, command)
        _write_result(output, result)
    return result


def _start_runner_process(
    container_id: str,
    token: str,
    registered: Receipt,
    *,
    timeout_seconds: int = CONTAINER_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    args = ["docker", "start", "--attach", "--interactive", container_id]
    started = time.monotonic()
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors
        ) as process:
            try:
                assert process.stdin is not None and process.stdout is not None
                process.stdin.write((token + "\n").encode())
                process.stdin.close()
                process.stdin = None
                descriptor = process.stdout.fileno()
                os.set_blocking(descriptor, False)
                prefix = b""
                deadline = started + min(180, timeout_seconds)
                with selectors.DefaultSelector() as selector:
                    selector.register(descriptor, selectors.EVENT_READ)
                    while b"\n" not in prefix:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            raise TimeoutError(
                                "Native ephemeral registration did not report its identity."
                            )
                        chunk = os.read(descriptor, 4096)
                        if not chunk or len(prefix) + len(chunk) > 65536:
                            raise ValueError(
                                "Native ephemeral registration receipt is unavailable."
                            )
                        prefix += chunk
                receipt = json.loads(prefix.split(b"\n", 1)[0])
                if not isinstance(receipt, dict):
                    raise ValueError("Native runner identity is invalid.")
                registered(receipt)
                os.set_blocking(descriptor, True)
                stdout, _ = process.communicate(
                    timeout=max(1, timeout_seconds - (time.monotonic() - started))
                )
                errors.seek(0)
                return subprocess.CompletedProcess(
                    args,
                    process.returncode,
                    (prefix + stdout).decode(errors="replace"),
                    errors.read().decode(errors="replace"),
                )
            except BaseException:
                process.kill()
                process.communicate()
                raise


def _write_result(output: Path, result: dict[str, object]) -> None:
    temporary = output / "result.tmp"
    temporary.write_text(json.dumps(result, indent=2) + "\n")
    temporary.replace(output / "result.json")


def cleanup_ephemeral_addon_job(
    *,
    client: AddonRunnerClient,
    request: AddonRunnerRequest,
    output: Path,
    command: Command = subprocess.run,
) -> dict[str, object]:
    result = json.loads((output / "result.json").read_text())
    if not isinstance(result, dict) or any(
        result.get(key) != value
        for key, value in (
            ("repository", request.repository),
            ("run_id", request.run_id),
            ("run_attempt", request.run_attempt),
            ("job_id", request.job_id),
        )
    ):
        raise ValueError("Cleanup evidence belongs to another addon operation.")
    container_id = result.get("container_id", "")
    if (
        not isinstance(container_id, str)
        or container_id
        and not re.fullmatch(r"[0-9a-f]{64}", container_id)
    ):
        raise ValueError("Cleanup requires its original immutable container identity.")
    _cleanup_ephemeral_addon_job(client, request, result, command)
    _write_result(output, result)
    return result


def _cleanup_ephemeral_addon_job(
    client: AddonRunnerClient,
    request: AddonRunnerRequest,
    result: dict[str, object],
    command: Command,
) -> None:
    errors: list[str] = []
    container_id = str(result.get("container_id", ""))
    if not container_id and result.get("container_creation_requested"):
        try:
            recovered = _docker(
                command,
                [
                    "container",
                    "ls",
                    "--all",
                    "--no-trunc",
                    "--quiet",
                    "--filter",
                    f"name=^/{request.name}$",
                    "--filter",
                    f"label=launchplane.ephemeral-addon={request.name}",
                ],
            ).stdout.strip()
            if recovered:
                if not re.fullmatch(r"[0-9a-f]{64}", recovered):
                    raise ValueError("Original addon container identity is ambiguous.")
                container_id = recovered
                result["container_id"] = container_id
                mounts = json.loads(
                    _docker(
                        command, ["inspect", "--format", "{{json .Mounts}}", container_id]
                    ).stdout
                )
                result["volumes"] = tuple(
                    str(mount["Name"]) for mount in mounts if mount.get("Type") == "volume"
                )
            else:
                result["container_removed"] = True
        except (OSError, ValueError, subprocess.SubprocessError):
            errors.append("container_identity_unconfirmed")
    if container_id and not result.get("container_removed"):
        try:
            present = _docker(
                command, ["container", "ls", "--all", "--quiet", "--filter", f"id={container_id}"]
            )
            if present.stdout.strip():
                label = _docker(
                    command,
                    [
                        "inspect",
                        "--format",
                        '{{index .Config.Labels "launchplane.ephemeral-addon"}}',
                        container_id,
                    ],
                )
                if label.stdout.strip() != request.name:
                    raise ValueError("Original addon container ownership changed.")
                _docker(command, ["rm", "--force", "--volumes", container_id])
            present = _docker(
                command, ["container", "ls", "--all", "--quiet", "--filter", f"id={container_id}"]
            )
            if present.stdout.strip():
                raise ValueError("The original addon container is still present.")
            volumes = result.get("volumes", ())
            if not isinstance(volumes, (tuple, list)):
                raise ValueError("Original addon storage identities are unavailable.")
            for volume in volumes:
                if not isinstance(volume, str) or not re.fullmatch(r"[0-9a-f]{64}", volume):
                    raise ValueError(
                        "Addon storage requires an original anonymous-volume identity."
                    )
                present = _docker(
                    command, ["volume", "ls", "--quiet", "--filter", f"name=^{volume}$"]
                )
                if present.stdout.strip():
                    raise ValueError("An original addon storage volume remains.")
            result["container_removed"] = True
        except (OSError, ValueError, subprocess.SubprocessError):
            errors.append("container_cleanup_unconfirmed")
    if result.get("registration_requested"):
        try:
            _retire_registration(client, request, result.get("runner_id"))
            result["registration_removed"] = True
        except (OSError, ValueError, MergeTrainGitHubError):
            errors.append("registration_cleanup_unconfirmed")
    image = result.get("recipe_image")
    if image and not result.get("recipe_image_removed"):
        try:
            if image != f"launchplane-addon-runner:{request.name}":
                raise ValueError("Addon recipe image identity changed.")
            image_query = ["image", "ls", "--quiet", "--filter", f"reference={image}"]
            if _docker(command, image_query).stdout.strip():
                _docker(command, ["image", "rm", str(image)])
            if _docker(command, image_query).stdout.strip():
                raise ValueError("Original addon recipe image remains.")
            result["recipe_image_removed"] = True
        except (OSError, ValueError, subprocess.SubprocessError):
            errors.append("recipe_image_cleanup_unconfirmed")
    result["cleanup_errors"] = errors
    if errors:
        result["status"] = "failed"


def _retire_registration(
    client: AddonRunnerClient, request: AddonRunnerRequest, runner_id: object
) -> None:
    inventory = client.read_runner_lane_inventory(repository=request.repository)
    owned = [lane for lane in inventory.lanes if lane.name == request.name]
    if len(owned) > 1 or any(
        request.label not in lane.labels or runner_id is not None and lane.github_id != runner_id
        for lane in owned
    ):
        raise ValueError("Addon runner registration identity is ambiguous.")
    for lane in owned:
        client.delete_runner(repository=request.repository, runner_id=lane.github_id)
    remaining = client.read_runner_lane_inventory(repository=request.repository)
    if any(lane.name == request.name for lane in remaining.lanes):
        raise ValueError("Addon runner retirement is not confirmed.")


def _write_recipe(context: Path, job: Mapping[str, object]) -> None:
    recipe = job.get("recipe")
    if not isinstance(recipe, dict) or set(recipe) != ADDON_RECIPE_FILES:
        raise ValueError("The exact addon runner recipe is unavailable.")
    if recipe.get("source-commit") != job.get("source_commit"):
        raise ValueError("Addon runner recipe does not match the job's source.")
    for name in ADDON_RECIPE_FILES:
        text = recipe[name]
        if not isinstance(text, str) or not text or len(text.encode()) > 65536:
            raise ValueError("Addon runner recipe file is invalid.")
        (context / name).write_text(text)


def _docker(
    command: Command,
    args: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 120,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = command(
        ["docker", *args], input=input_text, capture_output=True, text=True, timeout=timeout
    )
    if check and result.returncode:
        raise ValueError("Addon runner container operation failed.")
    return result


def cancel_ephemeral_addon_job(signum: int, _frame: object) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise InterruptedError(f"Addon runner lifecycle received signal {signum}.")


def install_cancellation_handlers() -> None:
    signal.signal(signal.SIGTERM, cancel_ephemeral_addon_job)
    signal.signal(signal.SIGINT, cancel_ephemeral_addon_job)
