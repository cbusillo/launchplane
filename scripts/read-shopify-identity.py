import argparse
import json
from pathlib import Path
import runpy
import sys
import urllib.error
from urllib.parse import quote

from control_plane.http_routes.odoo_runtime import ShopifyIdentityResponse
from control_plane.odoo_shopify_identity import ROUTE


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read held testing Shopify identity and compare production authority without activating Odoo."
    )
    parser.add_argument("--operator-helper", type=Path, required=True)
    parser.add_argument("--product", required=True)
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    args = parser.parse_args()
    helper = args.operator_helper.expanduser().resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Select the installed Launchplane operator helper.")
    try:
        transport = runpy.run_path(str(helper), run_name="shopify_identity_transport")
        settings = transport["resolve_settings"](args)
        transport["validate_service_url"](settings["service_url"])
        if not settings["token"]:
            parser.error("The existing operator credential is not configured.")
        raw = transport["request_launchplane_read"](
            service_url=settings["service_url"],
            path=ROUTE.format(product=quote(args.product, safe=""), environment="testing"),
            settings=settings,
            query={},
            timeout=60,
        )
        response = ShopifyIdentityResponse.model_validate(raw)
        print(response.model_dump_json())
        return 0 if response.result.separation == "separate" else 1
    except urllib.error.HTTPError as error:
        error.close()
        print(json.dumps({"status": "unavailable", "http_status": error.code}), file=sys.stderr)
    except Exception:
        print('{"status":"unavailable"}', file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
