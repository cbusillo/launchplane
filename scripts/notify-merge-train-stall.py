"""Send a value-free train alert with the existing Q194 Discord sender."""

import os
import re

from control_plane.deploy_failure_alert import validate_sender
from control_plane.notifications import post_discord_webhook


def send_stall_alert() -> None:
    repository = os.environ["STALL_REPOSITORY"]
    issue_number = os.environ["STALL_ISSUE_NUMBER"]
    state = os.environ["STALL_STATE"]
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or not issue_number.isdecimal()
        or int(issue_number) < 1
        or state not in {"open", "closed"}
        or not re.fullmatch(r"[a-f0-9]{64}", os.environ["STALL_NOTIFICATION_KEY"])
    ):
        raise ValueError("Invalid train alert identity.")
    post_discord_webhook(
        validate_sender(),
        {
            "allowed_mentions": {"parse": []},
            "embeds": [
                {
                    "title": "Merge train stalled"
                    if state == "open"
                    else "Merge train moving again",
                    "description": f"{repository}: https://github.com/{repository}/issues/{issue_number}",
                }
            ],
        },
    )


if __name__ == "__main__":
    try:
        send_stall_alert()
    except Exception as error:
        raise SystemExit(f"Train alert failed ({type(error).__name__}).") from None
