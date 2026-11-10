"""Generate the maintenance consumer from one committed devkit source."""

import argparse
import ast
import json
import subprocess
from pathlib import Path


def generate(*, devkit_repo: Path, revision: str, output: Path, check: bool = False) -> None:
    commit = subprocess.run(
        ["git", "--no-replace-objects", "rev-parse", "--verify", f"{revision}^{{commit}}"],
        cwd=devkit_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    source = subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "show",
            f"{commit}:docker/scripts/odoo_website_settings.py",
        ],
        cwd=devkit_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    generated = render(commit=commit, source=source)
    if check:
        if output.read_text(encoding="utf-8") != generated:
            raise ValueError(
                "Generated website settings program differs from its immutable source."
            )
    else:
        output.write_text(generated, encoding="utf-8")


def render(*, commit: str, source: str) -> str:
    compile(source, "odoo_website_settings.py", "exec")
    parameter_functions = [
        ast.unparse(node)
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"read_string_parameter", "write_string_parameter"}
    ]
    if len(parameter_functions) != 2:
        raise ValueError("Canonical string parameter adapter is unavailable.")
    parameter_program = "from typing import Any\n\n" + "\n\n".join(parameter_functions) + "\n"
    indexing_parameter = next(
        ast.literal_eval(node.value)
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "INDEXING_PARAMETER"
            for target in node.targets
        )
    )
    native = (
        source
        + """
import base64
import os
payload = json.loads(base64.b64decode(os.environ["ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"])) if os.environ.get("ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64") else None
indexing = payload.get("website_indexing") if payload else None
digest = apply_website_settings(env, payload, instance=__INSTANCE__)
env.cr.commit()
if digest:
    print("website_settings_sha256=" + digest)
if indexing is not None:
    print("website_indexing_production_use=" + indexing["production_use"])
"""
    )
    wrapper = """import argparse
import os
import sys
import tempfile
sys.path.insert(0, "/volumes/scripts")
import run_odoo_startup as startup
os.environ["ODOO_DB_NAME"] = sys.argv[1]
program = __NATIVE__.replace("__INSTANCE__", repr(sys.argv[2]))
with tempfile.TemporaryDirectory(prefix="launchplane-website-settings-") as config_directory:
    settings = startup._load_settings(argparse.Namespace(config_path=os.path.join(config_directory, "odoo.conf")))
    startup._write_runtime_config(settings)
    startup._run_odoo_shell(settings, program, label="accepted website settings")
""".replace("__NATIVE__", repr(native))
    compile(wrapper, "website_settings_maintenance", "exec")
    indexing_native = (
        source
        + """
import contextlib
import io
request = json.loads(__REQUEST__)
database_uuid = read_string_parameter(env["ir.config_parameter"].sudo(), "database.uuid")
if not database_uuid or (request.get("database_uuid") and database_uuid != request["database_uuid"]):
    raise RuntimeError("Website indexing database UUID differs from the runtime binding.")
if request["indexing"].get("website_id") is None:
    websites = env["website"].sudo().search([])
    matching = [website for website in websites if str(website.domain or "").rstrip("/") == request["indexing"]["public_origin"].rstrip("/")]
    if len(matching) != 1:
        raise RuntimeError("Website indexing requires one native origin binding.")
    request["indexing"]["website_id"] = matching[0].id
payload = {"instance": "prod", "website_indexing": request["indexing"]}
if request["mode"] == "apply":
    env.cr.execute("SELECT id FROM website WHERE id = %s FOR UPDATE", [request["indexing"]["website_id"]])
    env["website"].invalidate_model(["custom_code_head"])
plan = plan_website_indexing(env, payload, instance="prod")
if not plan:
    raise RuntimeError("Website indexing returned no native plan.")
if request["mode"] == "apply":
    if plan != request["reviewed_native"]:
        raise RuntimeError("Website indexing baseline changed.")
    with contextlib.redirect_stdout(io.StringIO()):
        apply_website_settings(env, payload, instance="prod")
    readback = plan_website_indexing(env, payload, instance="prod")
    if not readback or readback["before_sha256"] != plan["after_sha256"] or readback["changed"]:
        raise RuntimeError("Website indexing readback differs from the reviewed plan.")
    env.cr.commit()
else:
    env.cr.rollback()
print("website_indexing_result=" + json.dumps({**plan, "database_uuid": database_uuid, "committed": request["mode"] == "apply"}, sort_keys=True))
"""
    )
    indexing_wrapper = wrapper.replace(repr(native), repr(indexing_native)).replace(
        '.replace("__INSTANCE__", repr(sys.argv[2]))',
        '.replace("__REQUEST__", repr(sys.argv[2]))',
    )
    indexing_wrapper = indexing_wrapper.replace(
        "import tempfile\n", "import tempfile\nimport subprocess\n"
    ).replace(
        'startup._run_odoo_shell(settings, program, label="accepted website settings")',
        "subprocess.run(startup._build_odoo_shell_command(settings), input=program.encode(), env=startup._odoo_environment(settings), check=True)",
    )
    compile(indexing_wrapper, "website_indexing_configuration", "exec")
    generated = (
        "# Generated from committed odoo-devkit source; do not edit.\n"
        f"# Source revision: {commit}\n"
        "# Regenerate with scripts/generate_odoo_website_settings_program.py.\n"
        f"WEBSITE_SETTINGS_SOURCE_REVISION = {json.dumps(commit)}\n"
        f"WEBSITE_INDEXING_PARAMETER = {json.dumps(indexing_parameter)}\n"
        f"ODOO_STRING_PARAMETERS_PROGRAM = {parameter_program!r}\n"
        f"WEBSITE_SETTINGS_PROGRAM = {wrapper!r}\n"
        f"WEBSITE_INDEXING_PROGRAM = {indexing_wrapper!r}\n"
    )
    return generated


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--devkit-repo", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "control_plane/_odoo_website_settings_program.py",
    )
    args = parser.parse_args()
    generate(
        devkit_repo=args.devkit_repo, revision=args.revision, output=args.output, check=args.check
    )
