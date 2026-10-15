"""Generate the maintenance consumer from one committed devkit source."""

import argparse
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
    compile(source, "odoo_website_settings.py", "exec")
    native = (
        source
        + """
import base64
import os
payload = json.loads(base64.b64decode(os.environ["ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"])) if os.environ.get("ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64") else None
digest = apply_website_settings(env, payload, instance=__INSTANCE__)
env.cr.commit()
if digest:
    print("website_settings_sha256=" + digest)
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
    generated = (
        "# Generated from committed odoo-devkit source; do not edit.\n"
        f"# Source revision: {commit}\n"
        "# Regenerate with scripts/generate_odoo_website_settings_program.py.\n"
        f"WEBSITE_SETTINGS_PROGRAM = {wrapper!r}\n"
    )
    if check:
        if output.read_text(encoding="utf-8") != generated:
            raise ValueError(
                "Generated website settings program differs from its immutable source."
            )
    else:
        output.write_text(generated, encoding="utf-8")


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
