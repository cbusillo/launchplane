"""Verify committed action content and retirement wrapper/worker input agreement."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from collections.abc import Mapping

import yaml

from control_plane.first_party_action_pins import build_action_pin_report

WRAPPER_PATHS = (
    ".github/workflows/product-retirement.yml",
    ".github/workflows/detached-application-retirement.yml",
)


class WorkflowLoader(yaml.SafeLoader):
    """Keep the workflow 'on' key and YAML 1.2 boolean semantics."""


WorkflowLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in values if tag != "tag:yaml.org,2002:bool"]
    for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def _read_workflow(root: Path, revision: str, path: str) -> dict[str, object]:
    result = subprocess.run(
        ("git", "show", f"{revision}:{path}"),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode:
        raise ValueError(f"Cannot read workflow {path} at {revision}.")
    try:
        data = yaml.load(result.stdout, Loader=WorkflowLoader)
    except yaml.YAMLError as error:
        raise ValueError(f"Cannot parse workflow {path} at {revision}.") from error
    if not isinstance(data, dict):
        raise ValueError(f"Workflow {path} must be a mapping.")
    return data


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Workflow inputs and jobs must be mappings.")
    return value


def verify_worker_pins(root: Path, revision: str, repository: str) -> list[str]:
    violations: list[str] = []
    for path in WRAPPER_PATHS:
        wrapper = _read_workflow(root, revision, path)
        job = _mapping(_mapping(wrapper["jobs"])["retire"])
        reference = job["uses"]
        if not isinstance(reference, str):
            raise ValueError(f"Worker reference in {path} must be a string.")
        source, separator, pin = reference.partition("@")
        prefix = f"{repository}/.github/workflows/"
        if (
            not separator
            or not source.startswith(prefix)
            or len(pin) != 40
            or any(c not in "0123456789abcdef" for c in pin)
        ):
            raise ValueError(f"Invalid pinned worker reference in {path}.")
        worker_path = ".github/workflows/" + source.removeprefix(prefix)
        if "/" in source.removeprefix(prefix):
            raise ValueError(f"Invalid worker path in {path}.")
        worker = _read_workflow(root, pin, worker_path)
        expected = _mapping(_mapping(_mapping(worker["on"])["workflow_call"])["inputs"])
        actual = _mapping(_mapping(_mapping(wrapper["on"])["workflow_dispatch"])["inputs"])
        forwarded = _mapping(job["with"])
        if set(actual) != set(expected) or set(forwarded) != set(expected):
            violations.append(f"{path}: wrapper and pinned worker input names disagree")
            continue
        for name, worker_input in expected.items():
            caller_input = _mapping(actual[name])
            worker_input = _mapping(worker_input)
            if caller_input.get("required") != worker_input.get("required"):
                violations.append(f"{path}: {name} required flag disagrees")
            if path.endswith("detached-application-retirement.yml"):
                keys = ("type", "default")
            else:
                keys = () if name == "mode" else ("default",)
            for key in keys:
                if (key in worker_input or key in caller_input) and caller_input.get(
                    key
                ) != worker_input.get(key):
                    violations.append(f"{path}: {name} {key} disagrees")
    return violations


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()
    try:
        report = build_action_pin_report(
            args.repo_root,
            target_revision=args.revision,
            repository=args.repository,
        )
        violations = verify_worker_pins(args.repo_root, report.head_sha, args.repository)
    except (ValueError, OSError, subprocess.TimeoutExpired, KeyError) as error:
        parser.exit(1, f"Pin verification failed: {error}\n")
    print(
        json.dumps(
            {"action_pins": report.as_summary_dict(), "worker_violations": violations}, indent=2
        )
    )
    if report.violations or violations:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
