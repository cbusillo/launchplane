"""Resolve the service's existing bootstrap endpoint without duplicating validation."""

import os
from urllib.parse import urlsplit


def main() -> None:
    health_urls = os.environ.get("LAUNCHPLANE_DEPLOY_HEALTH_URLS", "")
    candidates = [value.strip() for value in health_urls.split(",") if value.strip()]
    value = candidates[0] if candidates else os.environ.get("LAUNCHPLANE_PUBLIC_URL", "").strip()
    if any(ord(character) < 32 for character in value):
        raise ValueError("Launchplane service URL must not contain control characters.")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ValueError("Launchplane service URL must be absolute HTTP(S).")
    if parsed.username or parsed.password:
        raise ValueError("Launchplane service URL must not contain credentials.")
    service_url = f"{parsed.scheme}://{parsed.netloc}"
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"service_url={service_url}\nservice_audience={parsed.hostname}\n")


if __name__ == "__main__":
    main()
