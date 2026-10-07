"""A GitHub build history for release/provider tests, including uploaded images."""

import io
import json
from urllib.parse import parse_qs, urlsplit
import zipfile

from control_plane.build_provenance import BUILD_WORKFLOW_PATH


class LaneBuildGitHub:
    def __init__(self, repository: str, image_repository: str, digests: dict[str, str]) -> None:
        self.repository = repository
        self.image_repository = image_repository
        self.digests = digests
        self.commits = list(digests)
        self.runs = {index + 1: commit for index, commit in enumerate(self.commits)}
        self.repository_id = 1
        self.run_started_at: dict[int, str] = {}

    def _run(self, run_id: int) -> dict[str, object]:
        return {
            "id": run_id,
            "run_attempt": 1,
            "event": "push",
            "head_branch": "main",
            "head_sha": self.runs[run_id],
            "path": BUILD_WORKFLOW_PATH,
            "status": "completed",
            "conclusion": "success",
            "repository": {"id": self.repository_id},
            "head_repository": {"id": self.repository_id},
        }

    def get_json(self, path: str) -> object:
        prefix = f"/repos/{self.repository}"
        if path == prefix:
            return {"id": self.repository_id, "default_branch": "main"}
        if "/compare/" in path:
            base, head = path.split("/compare/")[1].split("...")
            status = "ahead" if self.commits.index(head) > self.commits.index(base) else "behind"
            return {
                "status": status,
                "base_commit": {"sha": base},
                "merge_base_commit": {"sha": base if status == "ahead" else head},
            }
        if "/commits?" in path:
            return [
                {"sha": sha, "parents": [{"sha": self.commits[index - 1]}] if index else []}
                for index, sha in reversed(list(enumerate(self.commits)))
            ]
        if "/actions/runs?" in path:
            query = parse_qs(urlsplit(path).query)
            return {
                "workflow_runs": [
                    self._run(run_id)
                    for run_id, commit in self.runs.items()
                    if query.get("head_sha") == [commit]
                ]
            }
        if "/artifacts?" in path:
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            return {"artifacts": [{"id": run_id, "name": "artifact-manifest-1", "expired": False}]}
        if "/actions/runs/" in path and "/attempts/" in path:
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            return {**self._run(run_id), "run_started_at": self.run_started_at[run_id]}
        raise AssertionError(f"Unexpected source read: {path}")

    def get_bytes(self, path: str) -> bytes:
        run_id = int(path.split("/actions/artifacts/")[1].split("/")[0])
        commit = self.runs[run_id]
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr(
                "artifact-manifest.json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "generic-web",
                        "source_commit": commit,
                        "image": {
                            "repository": self.image_repository,
                            "digest": self.digests[commit],
                        },
                    }
                ),
            )
        return archive.getvalue()
