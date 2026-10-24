import ast
from collections import defaultdict
import io
import json
import os
import re
from pathlib import Path
import subprocess
import shlex
import sys
import tarfile

from control_plane.merge_train_codeowners import (
    CODEOWNERS_PATHS,
    paths_require_individual_landing,
    parse_codeowner_patterns,
)


def git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=root, text=True).strip()


MAX_FOCUSED_MODULES = 24


def module_name(path: str) -> str:
    return path.removesuffix(".py").removesuffix("/__init__").replace("/", ".")


def select_modules(
    sources: dict[str, str], changed: set[str], *, max_modules: int = MAX_FOCUSED_MODULES
) -> list[str] | None:
    consumers: dict[str, set[str]] = defaultdict(set)
    modules = {module_name(path) for path in sources if path.endswith(".py")}
    for path, content in sources.items():
        if not path.endswith(".py"):
            continue
        module = module_name(path)
        package = module if path.endswith("/__init__.py") else module.rsplit(".", 1)[0]
        for node in ast.walk(ast.parse(content)):
            imports: list[str] = []
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ""
                if node.level:
                    parent = package.split(".")[: len(package.split(".")) - node.level + 1]
                    prefix = ".".join([*parent, *([prefix] if prefix else [])])
                imports = [prefix, *(f"{prefix}.{alias.name}" for alias in node.names)]
            elif isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name)
                and node.func.id in {"__import__", "import_module"}
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
            ):
                if (
                    node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    imports = [node.args[0].value]
                else:
                    imports = list(modules)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                imports.extend(
                    re.findall(r"(?:control_plane|tests)(?:\.[A-Za-z_]\w*)+", node.value)
                )
            for imported in imports:
                while imported and imported not in modules:
                    imported = imported.rpartition(".")[0]
                while imported:
                    if imported in modules:
                        consumers[imported].add(module)
                    imported = imported.rpartition(".")[0]
    affected: set[str] = set()
    for path in changed:
        if path.startswith("docs/") and path.endswith(".md") or path in {"README.md", "AGENTS.md"}:
            continue
        if not path.endswith(".py") or not path.startswith(("control_plane/", "tests/")):
            return None
        if not all(component.isidentifier() for component in module_name(path).split(".")):
            return None
        if path.endswith("/__init__.py"):
            return None
        pending = [module_name(path)]
        seen: set[str] = set()
        while pending:
            module = pending.pop()
            if module in seen:
                continue
            seen.add(module)
            pending.extend(consumers[module])
        tests = {name for name in seen if name.startswith("tests.test") and name in modules}
        if not tests:
            return None
        affected.update(tests)
        if len(affected) > max_modules:
            return None
    return sorted(affected)


def plan(root: Path, event: dict[str, object], event_name: str) -> list[str] | None:
    pull = event.get("pull_request")
    repository = event.get("repository")
    if (
        event_name != "pull_request"
        or not isinstance(pull, dict)
        or not isinstance(repository, dict)
    ):
        return None
    head, base = pull.get("head"), pull.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        return None
    head_repo = head.get("repo")
    if (
        not isinstance(head_repo, dict)
        or head_repo.get("full_name") != repository.get("full_name")
        or base.get("ref") != repository.get("default_branch")
        or str(head.get("ref", "")).startswith("launchplane/")
    ):
        return None
    base_sha, head_sha = str(base.get("sha", "")), str(head.get("sha", ""))
    if git(root, "rev-parse", "HEAD^2") != head_sha:
        return None
    comparison_base = git(root, "rev-parse", "HEAD^1")
    git(root, "merge-base", "--is-ancestor", base_sha, comparison_base)
    changed = set(
        git(root, "diff", "--name-only", "--no-renames", comparison_base, "HEAD").splitlines()
    )
    files = git(root, "ls-tree", "-r", "--name-only", comparison_base).splitlines()
    for path in CODEOWNERS_PATHS:
        if path not in files:
            continue
        content = git(root, "show", f"{comparison_base}:{path}")
        patterns = parse_codeowner_patterns(content)
        if paths_require_individual_landing(tuple(changed), patterns):
            return None
        break
    sources: dict[str, str] = {}
    for revision in (comparison_base, "HEAD"):
        archive = subprocess.check_output(
            ["git", "archive", revision, "control_plane", "tests"], cwd=root
        )
        with tarfile.open(fileobj=io.BytesIO(archive)) as snapshot:
            for member in snapshot:
                if member.isfile() and member.name.endswith(".py"):
                    source = snapshot.extractfile(member)
                    assert source is not None
                    sources[member.name] = source.read().decode()
    return select_modules(sources, changed)


def run_selected(root: Path, modules: list[str]) -> None:
    if not modules:
        return
    if any(not all(part.isidentifier() for part in name.split(".")) for name in modules):
        raise ValueError("Invalid test module selection")
    configuration = json.loads((root / ".github/github.json").read_text())
    command = shlex.split(configuration["qualityGate"]["test"]["local"])
    position = command.index("{modules}")
    command[position : position + 1] = modules
    subprocess.run(command, cwd=root, check=True)


def main() -> None:
    if sys.argv[1:] == ["run"]:
        run_selected(Path.cwd(), os.environ["MODULES"].split())
        return
    try:
        modules = plan(
            Path.cwd(),
            json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()),
            os.environ["EVENT_NAME"],
        )
    except (OSError, ValueError, SyntaxError, subprocess.CalledProcessError) as error:
        print(f"Full suite fallback: {type(error).__name__}")
        modules = None
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"focused={str(modules is not None).lower()}\n")
        output.write(f"modules={' '.join(modules or [])}\n")
    print(json.dumps({"focused": modules is not None, "modules": modules}))


if __name__ == "__main__":
    main()
