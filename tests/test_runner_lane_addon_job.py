import hashlib
import io
import unittest
import zipfile

from control_plane.workflows.runner_lane_ephemeral import (
    ADDON_RECIPE_ARTIFACT_PREFIX,
    AddonRunnerRequest,
    read_addon_job,
)


class AddonJobReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = AddonRunnerRequest(
            repository="example/tenant", run_id=12, run_attempt=2, job_id=34
        )
        self.commit = "a" * 40
        self.workflow_run: dict[str, object] = {
            "id": 12,
            "run_attempt": 2,
            "head_sha": self.commit,
            "event": "pull_request",
            "repository": {"id": 56, "private": True},
            "head_repository": {"id": 56},
        }
        self.job: dict[str, object] = {
            "id": 34,
            "run_id": 12,
            "status": "queued",
            "name": "addon-validation",
            "labels": ["self-hosted", self.request.label],
        }
        self.archive = self.recipe_archive()
        self.artifact: dict[str, object] = {
            "id": 78,
            "name": f"{ADDON_RECIPE_ARTIFACT_PREFIX}2",
            "expired": False,
            "digest": "sha256:" + hashlib.sha256(self.archive).hexdigest(),
            "workflow_run": {"id": 12, "head_sha": self.commit},
        }

    def recipe_archive(self, **changes: str) -> bytes:
        files = {
            "Dockerfile": "FROM fixture",
            "entrypoint.sh": "#!/bin/sh\n",
            "source-commit": self.commit,
            **changes,
        }
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for name, text in files.items():
                archive.writestr(name, text)
        return stream.getvalue()

    def get_json(self, path: str) -> object:
        if path.endswith("/runs/12"):
            return self.workflow_run
        if path.endswith("/jobs/34"):
            return self.job
        if "/artifacts?" in path:
            return {"artifacts": [self.artifact]}
        raise AssertionError(path)

    def get_bytes(self, _path: str) -> bytes:
        return self.archive

    def read(self) -> dict[str, object]:
        return read_addon_job(transport=self, request=self.request, repository_id=56)

    def test_reads_only_the_current_private_same_repository_addon_job_recipe(self) -> None:
        result = self.read()
        self.assertEqual(result["source_commit"], self.commit)
        recipe = result["recipe"]
        assert isinstance(recipe, dict)
        self.assertEqual(recipe["Dockerfile"], "FROM fixture")

    def test_failed_job_retry_reuses_only_the_same_run_and_source_recipe(self) -> None:
        self.artifact["name"] = f"{ADDON_RECIPE_ARTIFACT_PREFIX}1"
        self.assertIn("recipe", self.read())
        self.artifact["workflow_run"] = {"id": 12, "head_sha": "b" * 40}
        with self.assertRaises(ValueError):
            self.read()

    def test_rejects_forks_public_repositories_old_attempts_and_wrong_routing(
        self,
    ) -> None:
        for target, field, replacement in (
            (self.workflow_run, "head_repository", {"id": 99}),
            (self.workflow_run, "repository", {"id": 56, "private": False}),
            (self.workflow_run, "run_attempt", 1),
            (self.workflow_run, "event", "pull_request_target"),
            (self.job, "run_id", 99),
            (self.job, "labels", ["self-hosted", "unrelated"]),
        ):
            with self.subTest(field=field, replacement=replacement):
                previous = target[field]
                target[field] = replacement
                with self.assertRaises(ValueError):
                    self.read()
                target[field] = previous

    def test_queue_scan_verifies_source_without_downloading_the_recipe(self) -> None:
        self.archive = b"not an artifact download"
        self.job["name"] = "product-owned-addon-command"
        result = read_addon_job(
            transport=self, request=self.request, repository_id=56, include_recipe=False
        )
        self.assertEqual(result["status"], "queued")
        self.assertNotIn("recipe", result)

    def test_rejects_wrong_digest_source_commit_and_archive_paths(self) -> None:
        for archive in (
            self.archive + b"modified",
            self.recipe_archive(**{"source-commit": "b" * 40}),
            self.recipe_archive(**{"../escape": "wrong"}),
        ):
            with self.subTest(archive_size=len(archive)):
                self.archive = archive
                if archive[-8:] != b"modified":
                    self.artifact["digest"] = "sha256:" + hashlib.sha256(archive).hexdigest()
                with self.assertRaises(ValueError):
                    self.read()

    def test_completed_job_result_needs_no_recipe_download(self) -> None:
        self.job.update(status="completed", conclusion="failure")
        result = self.read()
        self.assertEqual(result["conclusion"], "failure")
        self.assertNotIn("recipe", result)


if __name__ == "__main__":
    unittest.main()
