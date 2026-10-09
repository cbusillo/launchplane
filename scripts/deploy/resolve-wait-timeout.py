"""Budget self-deploy observation for Compose drains, deployment and health."""

import argparse
import math
import os
from pathlib import Path
import re

import yaml


def duration_seconds(value: str) -> int:
    if re.fullmatch(r"(?:[0-9]+(?:\.[0-9]+)?[hms])+", value) is None:
        raise ValueError("Worker stop grace must be a duration in hours, minutes or seconds.")
    units = {"h": 3600, "m": 60, "s": 1}
    return math.ceil(
        sum(
            float(number) * units[unit]
            for number, unit in re.findall(r"([0-9]+(?:\.[0-9]+)?)([hms])", value)
        )
    )


def wait_timeout(compose_file: Path, deploy_seconds: int, health_seconds: int) -> int:
    if deploy_seconds < 1 or health_seconds < 1:
        raise ValueError("Deploy and health observation budgets must be positive.")
    compose = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    # Sibling workers drain in parallel before their API dependency is recreated.
    drain_seconds = max(
        (
            duration_seconds(str(service["stop_grace_period"]))
            for service in compose["services"].values()
            if "stop_grace_period" in service
        ),
        default=0,
    )
    return drain_seconds + deploy_seconds + health_seconds


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-file", type=Path, default=Path("docker-compose.yml"))
    args = parser.parse_args()
    print(
        wait_timeout(
            args.compose_file,
            int(os.environ.get("LAUNCHPLANE_DOKPLOY_DEPLOY_TIMEOUT_SECONDS") or "600"),
            int(os.environ.get("LAUNCHPLANE_DEPLOY_HEALTH_TIMEOUT_SECONDS") or "180"),
        )
    )


if __name__ == "__main__":
    main()
