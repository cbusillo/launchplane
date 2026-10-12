"""Validate the existing sender or send a health-only deploy-failure alert."""

import argparse

from control_plane.deploy_failure_alert import send_failure_alert, validate_sender


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-sender", action="store_true")
    args = parser.parse_args()
    try:
        if args.validate_sender:
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
