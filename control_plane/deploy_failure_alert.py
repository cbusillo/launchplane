"""Health-only failure evidence for Launchplane's independent deploy reporter."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import requests

from control_plane.notifications import post_discord_webhook, public_discord_url_error
from control_plane.contracts.service_deploy_drain import OPERATION_REFERENCE_PATTERN


REPLACEMENT_TIMEOUT_SECONDS = 120
FINAL_VERIFICATION_TIMEOUT_SECONDS = 15


def validate_sender() -> str:
    sender = os.environ.get("DISCORD_LAUNCHPLANE_WEBHOOK", "").strip()
    if not sender or public_discord_url_error(sender):
        raise ValueError("The approved existing Launchplane Discord sender is unavailable.")
    return sender


def replacement_may_have_dispatched(response_file: Path) -> bool:
    try:
        response = json.loads(response_file.read_text())
    except (OSError, ValueError):
        return True  # A missing response never proves a mutation was not dispatched.
    if not isinstance(response, dict):
        return True
    result = response.get("result")
    error = response.get("error")
    if isinstance(result, dict) and result.get("deploy_state") == "draining":
        return False
    return not (
        isinstance(error, dict)
        and error.get("code")
        in {
            "self_deploy_refused",
            "self_deploy_drain_timeout",
            "self_deploy_fence_conflict",
            "invalid_request",
            "authorization_denied",
            "authentication_required",
            "request_not_sent",
        }
    )


def health_results(urls: list[str]) -> list[dict[str, str | int]]:
    results: list[dict[str, str | int]] = []
    deadline = time.monotonic() + 10
    for index, url in enumerate(urls, 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results.append({"check": index, "result": "observation_timeout"})
            break
        try:
            with requests.get(url, timeout=min(5, remaining), stream=True) as response:
                result: dict[str, str | int] = {"check": index, "http_status": response.status_code}
                result["result"] = "unexpected_http_status"
                if response.status_code == 200:
                    content = next(response.iter_content(chunk_size=4096), b"")
                    try:
                        body = json.loads(content)
                    except ValueError:
                        body = None
                    result["result"] = (
                        "ok"
                        if isinstance(body, dict) and body.get("status") == "ok"
                        else "unexpected_health_status"
                    )
                results.append(result)
        except requests.RequestException:
            results.append({"check": index, "result": "request_failed"})
    return results


def send_failure_alert() -> bool:
    response_file = Path(os.environ["SELF_DEPLOY_RESPONSE_FILE"])
    try:
        response = json.loads(response_file.read_text())
    except (OSError, ValueError):
        response = None
    error = response.get("error") if isinstance(response, dict) else None
    drain_timeout = isinstance(error, dict) and error.get("code") == "self_deploy_drain_timeout"
    operation_refs: list[str] = []
    if drain_timeout and isinstance(error, dict):
        records = response.get("records")
        raw_refs = records.get("blocking_operation_refs") if isinstance(records, dict) else None
        try:
            refs = json.loads(raw_refs) if isinstance(raw_refs, str) else None
        except ValueError:
            refs = None
        if isinstance(refs, list):
            operation_refs = list(
                dict.fromkeys(
                    item
                    for item in refs
                    if isinstance(item, str) and OPERATION_REFERENCE_PATTERN.fullmatch(item)
                )
            )
    if not drain_timeout and not replacement_may_have_dispatched(response_file):
        return False
    sender = validate_sender()
    image = os.environ["DEPLOYED_IMAGE_REFERENCE"]
    previous_image = os.environ.get("PREVIOUS_IMAGE_REFERENCE", "")
    if len(previous_image) > 500 or any(ord(char) < 32 for char in previous_image):
        previous_image = ""
    server = os.environ["GITHUB_SERVER_URL"].rstrip("/")
    repository = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    if (
        urlsplit(server).scheme != "https"
        or urlsplit(server).username is not None
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or not run_id.isdecimal()
        or not image
        or len(image) > 500
        or any(ord(char) < 32 for char in image)
    ):
        raise ValueError("Invalid deploy alert identity.")
    urls = [
        url.strip()
        for url in os.environ["LAUNCHPLANE_DEPLOY_HEALTH_URLS"].split(",")
        if url.strip()
    ]
    observations = health_results(urls)
    # Status codes are evidence; arbitrary response bodies, URLs and error text are not forwarded.
    runtime_status = os.environ.get("FAILING_RUNTIME_STATUS", "")
    runtime_result = (
        runtime_status if re.fullmatch(r"[1-5][0-9]{2}", runtime_status) else "unavailable"
    )
    run_url = f"{server}/{repository}/actions/runs/{run_id}"
    break_glass_url = f"{server}/{repository}/actions/workflows/deploy-launchplane.yml"
    failed_health_result: dict[str, str] = {}
    failure_file = os.environ.get("FAILING_HEALTH_RESULT_FILE", "")
    if failure_file:
        try:
            saved_result = json.loads(Path(failure_file).read_text())
        except (OSError, ValueError):
            saved_result = None
        if isinstance(saved_result, dict):
            http_status = str(saved_result.get("http_status", ""))
            if re.fullmatch(r"[0-5][0-9]{2}", http_status):
                failed_health_result = {"http_status": http_status, "result": "unhealthy"}
    health = json.dumps(
        {
            "runtime_http_status": runtime_result,
            "failed_health_result": failed_health_result,
            "health": observations,
        }
    )
    post_discord_webhook(
        sender,
        {
            "allowed_mentions": {"parse": []},
            "embeds": [
                {
                    "title": "Launchplane self-deploy drain timed out"
                    if drain_timeout
                    else "Launchplane replacement failed verification",
                    "description": (
                        "Held provider operations prevented replacement. No replacement was dispatched. "
                        "Match the references in runtime.release_drain "
                        "operation_details.operation_ref. Let the operations finish or use their supported "
                        "recovery, then submit a fresh normal deploy with a new key and marker."
                        if drain_timeout
                        else "Manual break-glass may be needed. Dispatch recovery, then approve the protected job."
                    ),
                    "fields": [
                        {"name": "Image", "value": image},
                        *(
                            [{"name": "Previous image", "value": previous_image}]
                            if previous_image
                            else []
                        ),
                        {"name": "Health result", "value": health[:1000]},
                        {"name": "Deploy run", "value": run_url},
                        *(
                            [
                                {
                                    "name": "Blocking operation references",
                                    "value": "\n".join(operation_refs[:15])
                                    + (
                                        f"\n(+{len(operation_refs) - 15} more)"
                                        if len(operation_refs) > 15
                                        else ""
                                    ),
                                }
                            ]
                            if operation_refs
                            else []
                        ),
                        {
                            "name": "Deploy workflow" if drain_timeout else "Manual break-glass",
                            "value": break_glass_url,
                        },
                    ],
                }
            ],
        },
    )
    return True
