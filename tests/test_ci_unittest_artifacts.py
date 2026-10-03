import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest

from tests.support.workflows import Workflow, load_workflow


class UnittestArtifactWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow(".github/workflows/ci.yml")

    def test_snapshot_cache_miss_does_not_publish_residual_history(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            env = _environment(root)
            stale = root / "unittest-timing-snapshot"
            stale.mkdir()
            (stale / "history.json").write_text("old history")

            _run_step(self.workflow, "test_timing_snapshot", "Freeze unittest timing snapshot", env)
            snapshot = _artifact_path(
                self.workflow, "test_timing_snapshot", "Upload unittest timing snapshot", env
            )

            self.assertEqual((snapshot / "snapshot-id").read_text(), "123:2\n")
            self.assertFalse((snapshot / "history.json").exists())
            self.assertEqual((stale / "history.json").read_text(), "old history")

    def test_snapshot_copies_current_history_without_reusing_previous_snapshot(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            env = _environment(root)
            cache = Path(env["UNITTEST_TIMINGS_CACHE"])
            cache.write_text("current history")
            _run_step(self.workflow, "test_timing_snapshot", "Freeze unittest timing snapshot", env)
            first = _artifact_path(
                self.workflow, "test_timing_snapshot", "Upload unittest timing snapshot", env
            )
            self.assertEqual((first / "history.json").read_text(), "current history")

            cache.unlink()
            _run_step(self.workflow, "test_timing_snapshot", "Freeze unittest timing snapshot", env)
            second = _artifact_path(
                self.workflow, "test_timing_snapshot", "Upload unittest timing snapshot", env
            )
            self.assertNotEqual(first, second)
            self.assertFalse((second / "history.json").exists())
            self.assertEqual((first / "history.json").read_text(), "current history")

    def test_downloads_and_failed_shard_upload_do_not_reuse_previous_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "artifact"
            artifact.mkdir()
            (artifact / "snapshot-id").write_text("123:2\n")
            (artifact / "plan.json").write_text("current plan")
            for job in ("test_shards", "test"):
                with self.subTest(job=job):
                    env = _environment(root)
                    _run_step(self.workflow, job, "Prepare unittest artifact directories", env)
                    previous = _artifact_path(
                        self.workflow, job, "Download unittest timing snapshot", env
                    )
                    (previous / "history.json").write_text("old history")
                    if job == "test_shards":
                        old_results = _artifact_path(
                            self.workflow, job, "Upload unittest timings", env
                        ).parent
                    else:
                        old_results = _artifact_path(
                            self.workflow, job, "Download unittest timings", env
                        )
                    (old_results / "shard-0.json").write_text("old shard")

                    _run_step(self.workflow, job, "Prepare unittest artifact directories", env)
                    fresh = _artifact_path(
                        self.workflow, job, "Download unittest timing snapshot", env
                    )
                    shutil.copytree(artifact, fresh, dirs_exist_ok=True)
                    self.assertEqual((fresh / "plan.json").read_text(), "current plan")
                    self.assertFalse((fresh / "history.json").exists())
                    self.assertEqual((previous / "history.json").read_text(), "old history")
                    if job == "test_shards":
                        # A failure before writing a result must upload no earlier result.
                        result = _artifact_path(self.workflow, job, "Upload unittest timings", env)
                        self.assertFalse(result.exists())
                    else:
                        results = _artifact_path(
                            self.workflow, job, "Download unittest timings", env
                        )
                        self.assertEqual(list(results.iterdir()), [])
                    self.assertEqual((old_results / "shard-0.json").read_text(), "old shard")

    def test_commands_use_the_artifact_directories(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            env = _environment(root)
            uv = root / "uv"
            uv.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "${ARGUMENTS_FILE}"\n')
            uv.chmod(0o755)
            env["PATH"] = f"{root}{os.pathsep}{env['PATH']}"
            env["ARGUMENTS_FILE"] = str(root / "arguments")
            env["UNITTEST_SHARD_COUNT"] = "2"
            env["UNITTEST_MAX_TESTS_PER_TARGET"] = "20"
            env["UNITTEST_MAX_SECONDS_PER_TARGET"] = "30"
            for job, step_name in (
                ("test_timing_snapshot", "Plan unittest shards"),
                ("test_shards", "Run unit test shard"),
                ("test", "Aggregate unittest timings"),
            ):
                with self.subTest(job=job):
                    if job == "test_timing_snapshot":
                        _run_step(self.workflow, job, "Freeze unittest timing snapshot", env)
                        snapshot = _artifact_path(
                            self.workflow, job, "Upload unittest timing snapshot", env
                        )
                    else:
                        _run_step(self.workflow, job, "Prepare unittest artifact directories", env)
                        snapshot = _artifact_path(
                            self.workflow, job, "Download unittest timing snapshot", env
                        )
                    _run_step(
                        self.workflow, job, step_name, env, folded=job != "test_timing_snapshot"
                    )
                    arguments = Path(env["ARGUMENTS_FILE"]).read_text().splitlines()
                    options = {
                        argument: arguments[index + 1]
                        for index, argument in enumerate(arguments)
                        if argument.startswith("--")
                    }
                    if job == "test_timing_snapshot":
                        self.assertEqual(options["--timings-file"], str(snapshot / "history.json"))
                        self.assertTrue((snapshot / "plan.json").exists())
                    else:
                        self.assertEqual(options["--plan-file"], str(snapshot / "plan.json"))
                        if job == "test_shards":
                            result = _artifact_path(
                                self.workflow, job, "Upload unittest timings", env
                            )
                            self.assertEqual(options["--timings-output"], str(result))
                        else:
                            results = _artifact_path(
                                self.workflow, job, "Download unittest timings", env
                            )
                            self.assertEqual(options["--results-dir"], str(results))


def _environment(root: Path) -> dict[str, str]:
    (root / "github-env").touch()
    return {
        **os.environ,
        "RUNNER_TEMP": str(root),
        "GITHUB_ENV": str(root / "github-env"),
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "UNITTEST_TIMINGS_CACHE": str(root / "history.json"),
    }


def _run_step(
    workflow: Workflow, job: str, name: str, env: dict[str, str], *, folded: bool = False
) -> None:
    step = workflow.step_named(job, name)
    if step is None:
        raise AssertionError(f"missing workflow step: {job}/{name}")
    program = step.run.replace("${{ matrix.shard_index }}", "0")
    if folded:
        program = " ".join(program.splitlines())
    subprocess.run(["bash", "-c", program], env=env, check=True, capture_output=True)
    for line in Path(env["GITHUB_ENV"]).read_text().splitlines():
        key, value = line.split("=", 1)
        env[key] = value


def _artifact_path(workflow: Workflow, job: str, name: str, env: dict[str, str]) -> Path:
    step = workflow.step_named(job, name)
    if step is None:
        raise AssertionError(f"missing workflow step: {job}/{name}")
    path = step.with_values["path"]
    if not isinstance(path, str):
        raise AssertionError(f"artifact path is not a string: {job}/{name}")
    path = re.sub(r"\$\{\{ env\.(\w+) \}\}", lambda match: env[match[1]], path)
    path = path.replace("${{ runner.temp }}", env["RUNNER_TEMP"])
    path = path.replace("${{ matrix.shard_index }}", "0")
    return Path(path)
