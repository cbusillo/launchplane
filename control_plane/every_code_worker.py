from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord

EVERY_CODE_GITHUB_TOKEN_ENV_KEY = "LAUNCHPLANE_EVERY_CODE_GITHUB_TOKEN"


class EveryCodeWorkerApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class EveryCodeWorkerApiStore:
    service_url: str
    worker_token: str
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.service_url.strip():
            raise ValueError("Every Code API worker requires service_url")
        if not self.worker_token.strip():
            raise ValueError("Every Code API worker requires worker_token")

    def read_every_code_work_request_record(self, request_id: str) -> EveryCodeWorkRequestRecord:
        payload = self._request("GET", f"/v1/every-code/work-requests/{request_id.strip()}")
        return EveryCodeWorkRequestRecord.model_validate(payload["request"])

    def _request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        base_url = self.service_url.strip().rstrip("/")
        url = f"{base_url}{path}"
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.worker_token.strip()}",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise EveryCodeWorkerApiError(
                f"Launchplane API request failed with HTTP {error.code}: {detail}"
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise EveryCodeWorkerApiError(f"Launchplane API request failed: {error}") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EveryCodeWorkerApiError("Launchplane API response was not valid JSON") from error
        if not isinstance(payload, dict):
            raise EveryCodeWorkerApiError("Launchplane API response was not a JSON object")
        return payload


def every_code_worktree_root(
    record: EveryCodeWorkRequestRecord,
    *,
    state_dir: Path,
) -> Path:
    repository_slug = _safe_path_component(record.repository.strip().replace("/", "-"))
    request_slug = _safe_path_component(record.request_id)
    return (
        state_dir.expanduser().resolve()
        / "every-code-worker"
        / "worktrees"
        / repository_slug
        / request_slug
    )


def every_code_worktree_branch(record: EveryCodeWorkRequestRecord) -> str:
    repository_slug = _safe_branch_component(record.repository.strip().replace("/", "-"))
    request_slug = _safe_branch_component(record.request_id)
    issue_part = str(record.issue_number) if record.issue_number > 0 else "request"
    return f"every-code/{repository_slug}-{issue_part}-{request_slug}"[:180].rstrip("/.-")


def _safe_path_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-._")
    return normalized or "request"


def _safe_branch_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-._")
    return normalized or "request"
