import json
from pathlib import Path
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from control_plane.ci_selection import git, plan, run_selected, select_modules, native_test_modules


class AffectedTestSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = {
            "control_plane/policy.py": "def permit(): return True\n",
            "control_plane/service.py": "from control_plane.policy import permit\n",
            "tests/support/client.py": "from control_plane import service\n",
            "tests/test_service.py": "from .support.client import service\n",
            "tests/test_other.py": "import unittest\n",
        }

    def test_transitive_consumers_keep_whole_touched_safety_modules(self) -> None:
        self.assertEqual(
            select_modules(self.sources, {"control_plane/policy.py"}),
            ["tests.test_service"],
        )

    def test_dynamic_import_consumers_are_included_conservatively(self) -> None:
        self.sources["tests/test_dynamic.py"] = 'importlib.import_module("control_plane.policy")'
        self.sources["tests/test_computed.py"] = "importlib.import_module(module_name)"
        self.assertEqual(
            select_modules(self.sources, {"control_plane/policy.py"}),
            ["tests.test_computed", "tests.test_dynamic", "tests.test_service"],
        )

    def test_package_reexports_and_string_patch_targets_keep_their_consumers(self) -> None:
        self.sources["control_plane/package/implementation.py"] = "value = 1"
        self.sources["control_plane/package/__init__.py"] = "from .implementation import value"
        self.sources["tests/test_package.py"] = "from control_plane.package import value"
        self.sources["tests/test_patch.py"] = 'patch("control_plane.package.implementation.value")'
        self.assertEqual(
            select_modules(self.sources, {"control_plane/package/implementation.py"}),
            ["tests.test_package", "tests.test_patch"],
        )

    def test_broad_dependency_fanout_retains_sharded_full_ci(self) -> None:
        self.sources["tests/test_second.py"] = "from control_plane.policy import permit"
        self.assertIsNone(select_modules(self.sources, {"control_plane/policy.py"}, max_modules=1))

    def test_changed_tests_run_with_their_consumers(self) -> None:
        self.sources["tests/test_policy.py"] = "from tests.test_service import Fixture\n"
        self.assertEqual(
            select_modules(self.sources, {"tests/test_service.py"}),
            ["tests.test_policy", "tests.test_service"],
        )

    def test_unmapped_source_or_unknown_inputs_keep_full_suite(self) -> None:
        for path in (
            "control_plane/unmapped.py",
            "control_plane/__init__.py",
            "pyproject.toml",
            "frontend/src/app.tsx",
            ".github/workflows/ci.yml",
            "tests/odd name.py",
        ):
            with self.subTest(path=path):
                self.assertIsNone(select_modules(self.sources, {path}))

    def test_source_scanner_is_a_consumer_without_a_python_import(self) -> None:
        self.sources["tests/test_scan.py"] = (
            "from pathlib import Path\nfor source in Path('control_plane').rglob('*.py'): check(source)"
        )
        self.assertEqual(
            select_modules(self.sources, {"control_plane/policy.py"}),
            ["tests.test_scan", "tests.test_service"],
        )

    def test_native_or_operational_script_dependencies_retain_full_ci(self) -> None:
        self.assertIsNone(
            select_modules(
                self.sources,
                {"control_plane/policy.py"},
                native_modules=frozenset({"tests.test_service"}),
            )
        )
        self.sources["scripts/probe.py"] = "from control_plane.policy import permit"
        self.assertIsNone(select_modules(self.sources, {"control_plane/policy.py"}))

    def test_native_inventory_uses_class_prefixes_and_the_configured_rehearsal(self) -> None:
        self.sources["control_plane/cli_ci.py"] = (
            'POSTGRES_INTEGRATION_MODULES = ("tests.test_service.NativeCase",)'
        )
        configuration: dict[str, object] = {
            "qualityGate": {
                "test": {
                    "testingRouteReaderRehearsal": "uv run python -m unittest tests.test_other.Rehearsal.test_probe -v"
                }
            }
        }
        self.assertEqual(
            native_test_modules(self.sources, configuration),
            frozenset({"tests.test_service", "tests.test_other"}),
        )
        self.sources["control_plane/cli_ci.py"] = "POSTGRES_INTEGRATION_MODULES = compute()"
        with self.assertRaises(ValueError):
            native_test_modules(self.sources, configuration)

    def test_docs_do_not_select_unrelated_tests(self) -> None:
        self.assertEqual(select_modules(self.sources, {"docs/readme.md"}), [])


class PullRequestSelectionTests(unittest.TestCase):
    def test_native_inventory_resolves_from_tracked_repository_inputs(self) -> None:
        root = Path(__file__).resolve().parents[1]
        sources = {
            path: ""
            for path in git(root, "ls-tree", "-r", "--name-only", "HEAD", "tests").splitlines()
            if path.endswith(".py")
        }
        sources["control_plane/cli_ci.py"] = git(root, "show", "HEAD:control_plane/cli_ci.py")
        configuration = json.loads(git(root, "show", "HEAD:.github/github.json"))
        self.assertTrue(native_test_modules(sources, configuration))

    def test_only_proven_same_repo_default_branch_pr_uses_tracked_inputs(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--quiet", directory], check=True)
            git(root, "config", "user.name", "Fixture")
            git(root, "config", "user.email", "fixture@example.invalid")
            (root / "control_plane").mkdir()
            (root / "tests").mkdir()
            (root / "scripts").mkdir()
            (root / "scripts/helper.py").write_text("import sys\n")
            (root / "control_plane/cli_ci.py").write_text("POSTGRES_INTEGRATION_MODULES = ()\n")
            (root / "tests/test_native.py").write_text("native = True\n")
            (root / "control_plane/policy.py").write_text("def permit(): return True\n")
            (root / "tests/test_policy.py").write_text("from control_plane.policy import permit\n")
            (root / ".github").mkdir()
            (root / ".github/CODEOWNERS").write_text("/DIRECTION.md @example\n")
            (root / ".github/github.json").write_text(
                json.dumps(
                    {
                        "qualityGate": {
                            "test": {
                                "testingRouteReaderRehearsal": "python -m unittest tests.test_native"
                            }
                        }
                    }
                )
            )
            git(root, "add", ".")
            git(root, "commit", "--quiet", "-m", "base")
            base = git(root, "rev-parse", "HEAD")
            (root / "control_plane/policy.py").write_text("def permit(): return False\n")
            git(root, "add", ".")
            git(root, "commit", "--quiet", "-m", "source")
            head = git(root, "rev-parse", "HEAD")
            tree = git(root, "rev-parse", "HEAD^{tree}")
            merge = git(root, "commit-tree", tree, "-p", base, "-p", head, "-m", "synthetic")
            git(root, "checkout", "--quiet", "--detach", merge)
            (root / "tests/test_untracked.py").write_text("invalid Python !\n")
            head_repository = {"full_name": "example/control"}
            event: dict[str, object] = {
                "repository": {"full_name": "example/control", "default_branch": "main"},
                "pull_request": {
                    "head": {
                        "repo": head_repository,
                        "ref": "work/change",
                        "sha": head,
                    },
                    "base": {"ref": "main", "sha": base},
                },
            }
            self.assertEqual(plan(root, event, "pull_request"), ["tests.test_policy"])
            self.assertIsNone(plan(root, event, "push"))
            self.assertIsNone(plan(root, event, "workflow_dispatch"))
            head_repository["full_name"] = "fork/control"
            self.assertIsNone(plan(root, event, "pull_request"))
            head_repository["full_name"] = "example/control"
            for operation in ("delete", "rename"):
                if operation == "delete":
                    git(root, "rm", "tests/test_policy.py")
                else:
                    (root / "tests/test_policy.py").write_text(
                        "from control_plane.policy import permit\n"
                    )
                    git(root, "add", "tests/test_policy.py")
                    git(root, "commit", "--quiet", "-m", "restore before rename")
                    git(root, "mv", "tests/test_policy.py", "tests/test_renamed.py")
                git(root, "commit", "--quiet", "-m", operation)
                current_head = git(root, "rev-parse", "HEAD")
                current_tree = git(root, "rev-parse", "HEAD^{tree}")
                synthetic = git(
                    root,
                    "commit-tree",
                    current_tree,
                    "-p",
                    base,
                    "-p",
                    current_head,
                    "-m",
                    "synthetic",
                )
                git(root, "checkout", "--quiet", "--detach", synthetic)
                pull = event["pull_request"]
                assert isinstance(pull, dict)
                pull["head"]["sha"] = current_head
                with self.subTest(operation=operation):
                    self.assertIsNone(plan(root, event, "pull_request"))


class SelectedCommandTests(unittest.TestCase):
    def test_uses_repository_local_template_and_propagates_failure(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github").mkdir()
            program = "import pathlib,sys; pathlib.Path('selected').write_text(' '.join(sys.argv[1:])); sys.exit('tests.fail' in sys.argv)"
            command = shlex.join([sys.executable, "-c", program, "{modules}"])
            (root / ".github/github.json").write_text(
                json.dumps({"qualityGate": {"test": {"local": command}}})
            )
            run_selected(root, ["tests.pass"])
            self.assertEqual((root / "selected").read_text(), "tests.pass")
            with self.assertRaises(subprocess.CalledProcessError):
                run_selected(root, ["tests.fail"])
            run_selected(root, [])
            self.assertEqual((root / "selected").read_text(), "tests.fail")
