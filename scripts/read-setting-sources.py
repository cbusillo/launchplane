import argparse
import json
from pathlib import Path
import runpy
import urllib.error
from urllib.parse import quote

from control_plane.runtime_setting_inventory import ProductSettingSourceInventory


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare configured lane setting names and sources."
    )
    parser.add_argument("--operator-helper", type=Path, required=True)
    parser.add_argument("--product", required=True)
    for option in ("config", "env-config", "url"):
        parser.add_argument(f"--{option}")
    args = parser.parse_args()
    helper = args.operator_helper.expanduser().resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Select the installed Launchplane operator helper.")
    transport = runpy.run_path(str(helper), run_name="setting_sources_transport")
    settings = transport["resolve_settings"](args)
    transport["validate_service_url"](settings["service_url"])
    if not settings["token"]:
        parser.error("The existing operator credential is unavailable.")
    trace = ""
    try:
        raw = transport["request_launchplane_read"](
            service_url=settings["service_url"],
            path=f"/v1/products/{quote(args.product, safe='')}/secret-bindings",
            settings=settings,
            query={"include_setting_sources": True},
            timeout=120,
        )
        trace = transport["public_trace_id"](raw.get("trace_id"))
        report = ProductSettingSourceInventory.model_validate(raw.get("setting_sources"))
        if report.product != args.product:
            raise ValueError("Product binding mismatch")
        output = {
            "status": "ok",
            "trace_id": trace,
            "result": report.model_dump(mode="json"),
        }
        transport["assert_public_safe_shape"](output)
        print(json.dumps(output))
        return 1 if report.sources_truncated or report.preview_targets_truncated else 0
    except urllib.error.HTTPError as error:
        try:
            failure = json.loads(error.read(4096))
            trace = transport["public_trace_id"](failure.get("trace_id"))
            code = failure.get("error", {}).get("code")
            if code not in {"authorization_denied", "settings_sources_unavailable", "not_found"}:
                code = "settings_sources_unavailable"
        except (ValueError, TypeError, AttributeError):
            trace, code = "", "settings_sources_unavailable"
        finally:
            error.close()
        print(
            json.dumps(
                {
                    "status": "unavailable",
                    "http_status": error.code,
                    "trace_id": trace,
                    "error_code": code,
                }
            )
        )
        return 1
    except (ValueError, TypeError, AttributeError, urllib.error.URLError, TimeoutError):
        print(
            json.dumps(
                {
                    "status": "unavailable",
                    "error_code": "settings_sources_unavailable",
                    "trace_id": trace,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
