import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
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
            history = json.dumps(
                {
                    "schema_version": 1,
                    "record_type": "unittest_module_timings",
                    "modules": {"tests.example": {"seconds": 4}},
                }
            )
            cache.write_text(history)
            _run_step(self.workflow, "test_timing_snapshot", "Freeze unittest timing snapshot", env)
            first = _artifact_path(
                self.workflow, "test_timing_snapshot", "Upload unittest timing snapshot", env
            )
            self.assertEqual((first / "history.json").read_text(), history)

            cache.unlink()
            _run_step(self.workflow, "test_timing_snapshot", "Freeze unittest timing snapshot", env)
            second = _artifact_path(
                self.workflow, "test_timing_snapshot", "Upload unittest timing snapshot", env
            )
            self.assertNotEqual(first, second)
            self.assertFalse((second / "history.json").exists())
            self.assertEqual((first / "history.json").read_text(), history)

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

    def test_imported_verified_history_balances_matching_targets_after_cache_loss(self) -> None:
        from control_plane.unittest_sharding import plan_shards, read_module_timings

        for source_sha in ("", "a" * 40):
            with self.subTest(reused=bool(source_sha)), TemporaryDirectory() as directory:
                root = Path(directory)
                env = _environment(root)
                self._mock_history_download(root, env)
                env["SOURCE_SHA"] = source_sha
                step = self.workflow.step_named(
                    "publish_test_timings", "Import verified unittest timing history"
                )
                assert step is not None
                result = subprocess.run(
                    ["bash", "-c", step.run],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertIn("available=true", (root / "output").read_text())
                self.assertIn("Published unittest timing history", result.stdout)
                _run_step(
                    self.workflow, "test_timing_snapshot", "Freeze unittest timing snapshot", env
                )
                snapshot = Path(env["UNITTEST_TIMING_SNAPSHOT_DIR"])
                costs = read_module_timings(snapshot / "history.json")
                plan = plan_shards(
                    ("tests.slow", "tests.fast", "tests.new"), shard_count=2, module_seconds=costs
                )
                self.assertEqual(plan.shard(0).modules, ("tests.slow",))
                self.assertEqual(plan.shard(0).estimate_sources, {"tests.slow": "exact"})
                self.assertEqual(
                    plan.shard(1).estimate_sources, {"tests.fast": "exact", "tests.new": "default"}
                )
                self.assertEqual(
                    {target for shard in plan.shards for target in shard.modules},
                    {"tests.slow", "tests.fast", "tests.new"},
                )
                calls = (root / "calls").read_text()
                self.assertIn("run download 456" if source_sha else "run download 123", calls)
                if source_sha:
                    self.assertIn(f"head_sha={source_sha}", calls)

    def test_missing_or_invalid_history_keeps_cold_plan_and_never_reuses_stale_cache(self) -> None:
        for failure in ("api", "download", "invalid", "missing"):
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                root = Path(directory)
                env = _environment(root)
                self._mock_history_download(root, env)
                env["SOURCE_SHA"] = "a" * 40
                env["HISTORY_FAILURE"] = failure
                Path(env["UNITTEST_TIMINGS_CACHE"]).write_text("stale")
                step = self.workflow.step_named(
                    "publish_test_timings", "Import verified unittest timing history"
                )
                assert step is not None
                result = subprocess.run(
                    ["bash", "-c", step.run],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual((root / "output").read_text(), "available=false\n")
                self.assertIn("::warning::", result.stdout)
                _run_step(
                    self.workflow, "test_timing_snapshot", "Freeze unittest timing snapshot", env
                )
                self.assertFalse(
                    (Path(env["UNITTEST_TIMING_SNAPSHOT_DIR"]) / "history.json").exists()
                )

    @staticmethod
    def _mock_history_download(root: Path, env: dict[str, str]) -> None:
        env.update(
            SOURCE_SHA="",
            GITHUB_OUTPUT=str(root / "output"),
            REPOSITORY="example/control",
            HISTORY_FAILURE="",
            HISTORY_CALLS=str(root / "calls"),
        )
        gh = root / "gh"
        gh.write_text(
            f"#!{sys.executable}\n"
            + r"""
import json
import os
from pathlib import Path
import sys
import subprocess
with Path(os.environ['HISTORY_CALLS']).open('a') as calls:
    calls.write(' '.join(sys.argv[1:]) + '\n')
failure = os.environ['HISTORY_FAILURE']
if sys.argv[1] == 'api':
    if failure == 'api':
        sys.exit(1)
    runs = [{'id': 12, 'head_sha': 'b' * 40, 'event': 'push', 'conclusion': 'success'}, {'id': 13, 'head_sha': os.environ['SOURCE_SHA'], 'event': 'pull_request', 'conclusion': 'success'}, {'id': 14, 'head_sha': os.environ['SOURCE_SHA'], 'event': 'push', 'conclusion': 'failure'}, {'id': 456, 'head_sha': os.environ['SOURCE_SHA'], 'event': 'push', 'conclusion': 'success'}]
    subprocess.run(['jq', '-r', sys.argv[sys.argv.index('--jq') + 1]], input=json.dumps({'workflow_runs': runs}), text=True, check=True)
else:
    if failure == 'download':
        sys.exit(1)
    root = Path(sys.argv[sys.argv.index('--dir') + 1])
    if failure != 'missing':
        history = {'schema_version': 1, 'record_type': 'unittest_module_timings', 'modules': {'tests.slow': {'seconds': 100}, 'tests.fast': {'seconds': 1}}}
        (root / 'history.json').write_text('invalid' if failure == 'invalid' else json.dumps(history))
"""
        )
        gh.chmod(0o755)
        env["PATH"] = f"{root}{os.pathsep}{env['PATH']}"


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
