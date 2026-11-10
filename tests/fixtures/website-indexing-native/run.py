import ast
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import time
from unittest.mock import patch

import requests

import importlib
from typing import Any, cast

ROOT = Path("/workspace")
RESULTS = ROOT / "results"
RESULTS.mkdir(exist_ok=True)
importlib.import_module("odoo.cli")
odoo = importlib.import_module("odoo")
api = importlib.import_module("odoo.api")
registry_module = importlib.import_module("odoo.modules.registry")
psycopg2 = importlib.import_module("psycopg2")
html = importlib.import_module("lxml.html")
WEBSITE_INDEXING_PROGRAM = importlib.import_module("generated").WEBSITE_INDEXING_PROGRAM


DATABASE = "launchplane_indexing"
ARGS = [
    "--db_host=db",
    "--db_user=odoo",
    "--db_password=synthetic-only",
    "--database=" + DATABASE,
    "--data-dir=/workspace/data",
    "--without-demo=all",
    "--max-cron-threads=0",
    "--proxy-mode",
    "--http-interface=0.0.0.0",
    "--http-port=8069",
]
for attempt in range(60):
    try:
        psycopg2.connect(
            host="db", user="odoo", password="synthetic-only", dbname="postgres"
        ).close()
        break
    except psycopg2.OperationalError:
        time.sleep(1)
else:
    raise RuntimeError("Synthetic database did not start")
with (RESULTS / "install.log").open("w") as log:
    subprocess.run(
        ["/odoo/odoo-bin", *ARGS, "--init=website", "--stop-after-init"],
        stdout=log,
        stderr=subprocess.STDOUT,
        check=True,
        timeout=900,
    )

odoo.tools.config.parse_config(ARGS)
registry = registry_module.Registry(DATABASE)
original = '<meta name="robots" content="nofollow"><script>window.synthetic="preserved";</script>'
with registry.cursor() as cursor:
    env = api.Environment(cursor, 1, {})
    site = env["website"].create(
        {
            "name": "Indexing proof",
            "domain": "https://indexing.example.test",
            "custom_code_head": original,
            "robots_txt": "User-agent: *\nDisallow: /private-fixture\n",
        }
    )
    other = env["website"].create(
        {
            "name": "Unrelated proof",
            "domain": "https://unrelated.example.test",
            "custom_code_head": original,
        }
    )
    site_id, other_id = site.id, other.id
    parameters = env["ir.config_parameter"].sudo()
    read = getattr(parameters, "get_str", None) or parameters.get_param
    database_uuid = read("database.uuid")
    cursor.commit()
program_node = next(
    node
    for node in ast.parse(WEBSITE_INDEXING_PROGRAM).body
    if isinstance(node, ast.Assign)
    and any(isinstance(target, ast.Name) and target.id == "program" for target in node.targets)
)
assert isinstance(program_node, ast.Assign)
program_call = program_node.value
assert isinstance(program_call, ast.Call)
program_function = program_call.func
assert isinstance(program_function, ast.Attribute)
NATIVE = ast.literal_eval(program_function.value)


def execute(
    state: str, reviewed: dict[str, Any] | None = None, *, fail: bool = False
) -> dict[str, Any]:
    request = {
        "mode": "apply" if reviewed else "dry-run",
        "indexing": {
            "website_id": site_id,
            "public_origin": "https://indexing.example.test",
            "production_use": state,
        },
        "database_uuid": reviewed["database_uuid"] if reviewed else database_uuid,
    }
    if reviewed:
        request["reviewed_native"] = {
            key: value
            for key, value in reviewed.items()
            if key not in {"committed", "database_uuid"}
        }
    output = io.StringIO()
    with registry.cursor() as native_cursor:
        native_env = api.Environment(native_cursor, 1, {})

        def failed_write(_records: Any, _values: Any) -> None:
            raise RuntimeError("planted head write failure")

        from contextlib import nullcontext

        with (
            patch.object(type(native_env["website"]), "write", failed_write)
            if fail
            else nullcontext(),
            redirect_stdout(output),
        ):
            exec(NATIVE.replace("__REQUEST__", repr(json.dumps(request))), {"env": native_env})
    lines = [
        line.split("=", 1)[1]
        for line in output.getvalue().splitlines()
        if line.startswith("website_indexing_result=")
    ]
    if len(lines) != 1:
        raise RuntimeError("Missing producer receipt")
    result = cast(dict[str, Any], json.loads(lines[0]))
    with (RESULTS / "receipts.jsonl").open("a") as receipts:
        receipts.write(json.dumps(result) + "\n")
    return result


def page(host: str = "indexing.example.test", path: str = "/contactus") -> str:
    response = requests.get(
        "http://127.0.0.1:8069" + path,
        headers={"X-Forwarded-Host": host, "X-Forwarded-Proto": "https"},
        timeout=30,
    )
    response.raise_for_status()
    (RESULTS / f"page-{len(list(RESULTS.glob('page-*.html')))}.html").write_text(response.text)
    return response.text


def count(document: str) -> int:
    return sum(
        "noindex" in content.lower().replace(",", " ").split()
        for content in html.fromstring(document).xpath('//head/meta[@name="robots"]/@content')
    )


with (RESULTS / "web.log").open("w") as log:
    server = subprocess.Popen(["/odoo/odoo-bin", *ARGS], stdout=log, stderr=subprocess.STDOUT)
    try:
        for attempt in range(90):
            try:
                baseline = page()
                break
            except requests.RequestException:
                time.sleep(1)
        else:
            raise RuntimeError("Synthetic HTTP server did not start")
        counts = [count(baseline)]
        assert counts[-1] == 0
        robots = page(path="/robots.txt")
        canonical = html.fromstring(baseline).xpath('//link[@rel="canonical"]/@href')
        assert "Disallow: /\n" not in robots
        plan = execute("prelaunch")
        assert not plan["committed"] and count(page()) == 0
        protected = execute("prelaunch", plan)
        counts.append(count(page()))
        assert protected["committed"] and counts[-1] == 1
        assert page(path="/robots.txt") == robots
        assert html.fromstring(page()).xpath('//link[@rel="canonical"]/@href') == canonical
        assert "window.synthetic" in page() and count(page("unrelated.example.test")) == 0
        live = execute("live")
        try:
            execute("live", live, fail=True)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Planted write failure did not refuse")
        counts.append(count(page()))
        assert counts[-1] == 1
        try:
            execute("live", {**live, "database_uuid": "foreign-database"})
        except RuntimeError:
            pass
        else:
            raise AssertionError("Foreign native database was not refused")
        assert count(page()) == 1
        execute("live", live)
        counts.append(count(page()))
        assert counts[-1] == 0
        inverse = execute("prelaunch")
        restored = execute("prelaunch", inverse)
        counts.append(count(page()))
        assert counts[-1] == 1 and restored["after_sha256"] == protected["after_sha256"]
        stale = execute("live")
        with registry.cursor() as cursor:
            env = api.Environment(cursor, 1, {})
            website = env["website"].browse(site_id)
            website.custom_code_head += "<script>window.concurrent=true;</script>"
            cursor.commit()
            signal_changes = getattr(registry, "signal_changes", None)
            if callable(signal_changes):
                signal_changes()
        try:
            execute("live", stale)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Stale baseline did not refuse")
        assert count(page()) == 1 and "window.concurrent" in page()
        with registry.cursor() as cursor:
            env = api.Environment(cursor, 1, {})
            assert env["website"].browse(other_id).custom_code_head == original
        proof = {
            "prelaunch_failed_live_inverse_noindex": counts,
            "failed_write_preserved": True,
            "stale_baseline_refused": True,
            "foreign_database_refused": True,
            "unrelated_website_preserved": True,
            "robots_preserved": True,
            "protected_sha256": protected["after_sha256"],
        }
        (RESULTS / "proof.json").write_text(json.dumps(proof, indent=2))
        print("LP_INDEXING_NATIVE_PROOF=" + json.dumps(proof))
    finally:
        server.terminate()
        server.wait(timeout=30)
