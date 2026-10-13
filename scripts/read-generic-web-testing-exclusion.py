"""No-write consumer using the installed operator helper's identity and transport."""

import argparse
import json
from pathlib import Path
import runpy
import sys
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control_plane.contracts.generic_web_testing_exclusion import (  # noqa: E402
    GENERIC_WEB_TESTING_EXCLUSION_ROUTE,
    GenericWebTestingExclusionResponse,
)


def _unavailable() -> int:
    print(json.dumps({"status": "unknown", "code": "testing_exclusion_read_unavailable"}))
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-helper", type=Path, required=True)
    parser.add_argument("--product", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if not 0 < args.timeout <= 60:
        parser.error("timeout must be between 0 and 60 seconds")
    operation = "generic-web-testing-exclusion-read"
    query = {"product": args.product, "context": args.context, "instance": "testing"}
    request = {"operation": operation, **query}
    try:
        helper = runpy.run_path(str(args.operator_helper.resolve()))
        helper["public_identifier"](args.product)
        helper["public_identifier"](args.context)
        settings = helper["prepare_operator_settings"](
            args=args, operation=operation, request=request
        )
        if settings is None:
            return 2
        payload = helper["request_launchplane_read"](
            service_url=settings["service_url"],
            path=GENERIC_WEB_TESTING_EXCLUSION_ROUTE,
            settings=settings,
            query=query,
            timeout=args.timeout,
        )
        result = GenericWebTestingExclusionResponse.model_validate(payload)
        if (result.product, result.context, result.instance) != (
            args.product,
            args.context,
            "testing",
        ):
            raise ValueError("Response lane mismatch")
        public = result.model_dump(mode="json")
        helper["assert_public_safe_shape"](public)
        print(json.dumps(public, sort_keys=True))
        return 0 if result.status == "excluded" else 1
    except urllib.error.HTTPError as error:
        try:
            helper["emit_http_error_payload"](operation=operation, request=request, exc=error)
        except Exception:
            return _unavailable()
        return 1
    except Exception:
        # Never print transport/provider text, settings or an invalid envelope.
        return _unavailable()


if __name__ == "__main__":
    raise SystemExit(main())
