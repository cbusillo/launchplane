"""Validate the existing sender or send a health-only deploy-failure alert."""

import argparse
import os
from pathlib import Path

from control_plane.deploy_failure_alert import (
    replacement_may_have_dispatched,
    send_failure_alert,
    validate_sender,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--validate-sender", action="store_true")
    mode.add_argument("--check-dispatch", action="store_true")
    args = parser.parse_args()
    try:
        if args.check_dispatch:
            may_have_dispatched = replacement_may_have_dispatched(
                Path(os.environ["SELF_DEPLOY_RESPONSE_FILE"])
            )
            print("true" if may_have_dispatched else "false")
        elif args.validate_sender:
            validate_sender()
            print("Existing Launchplane Discord sender is configured.")
        else:
            sent = send_failure_alert()
            print(
                "Deploy failure alert sent."
                if sent
                else "No replacement was dispatched; alert skipped."
            )
    except Exception as error:
        # Neither HTTP exceptions nor credential-bearing URLs belong in runner logs.
        raise SystemExit(f"Deploy failure reporter failed ({type(error).__name__}).") from None


if __name__ == "__main__":
    main()
