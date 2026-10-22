#!/usr/bin/env bash
set -euo pipefail

image="${1:?Pass the locally built Launchplane image}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scratch_root="${RUNNER_TEMP:-$repo_root/state}"
mkdir -p "$scratch_root"
fixture_dir="$(mktemp -d "$scratch_root/lp-browser.XXXXXXXX")"
project="$(basename "$fixture_dir" | tr '[:upper:].' '[:lower:]-')"
compose=(docker compose -p "$project" -f "$fixture_dir/browser.json")
owns_project=0
active_pid=""
run_interruptible() {
    "$@" <&0 &
    active_pid="$!"
    wait "$active_pid"
    active_pid=""
}
cancel() {
    trap '' INT TERM
    if [[ -n "$active_pid" ]]; then
        kill -KILL "$active_pid" 2>/dev/null || true
        wait "$active_pid" 2>/dev/null || true
    fi
    exit "$1"
}
cleanup() {
    local result="$?"
    local cleanup_failed=0
    trap - EXIT
    trap '' INT TERM
    if [[ -f "$fixture_dir/probe.cid" ]]; then
        local probe
        probe="$(cat "$fixture_dir/probe.cid")"
        if docker container inspect "$probe" >/dev/null 2>&1; then
            if ! docker rm --force "$probe"; then
                if docker container inspect "$probe" >/dev/null 2>&1; then
                    printf 'Browser probe cleanup failed; retained %s.\n' "$fixture_dir" >&2
                    cleanup_failed=1
                fi
            fi
        fi
    fi
    if [[ "$owns_project" == 1 && -f "$fixture_dir/browser.json" ]]; then
        if ! "${compose[@]}" down --volumes --timeout 5; then
            printf 'Browser fixture cleanup failed; retained %s.\n' "$fixture_dir" >&2
            exit 1
        fi
    fi
    if [[ "$cleanup_failed" == 1 ]]; then
        exit 1
    fi
    rm -rf "$fixture_dir"
    exit "$result"
}
trap cleanup EXIT
trap 'cancel 130' INT
trap 'cancel 143' TERM

cp "$repo_root/docker-compose.yml" "$fixture_dir/compose.yml"
printf '%s\n' 'LAUNCHPLANE_BROWSER_CREDENTIAL_FIXTURE=control-plane-only' > "$fixture_dir/.env"
DOCKER_IMAGE_REFERENCE="$image" LAUNCHPLANE_COMPOSE_EXTERNAL_NETWORK=qualification-unused \
    docker compose -p "$project" --project-directory "$fixture_dir" -f "$fixture_dir/compose.yml" \
    config --format json > "$fixture_dir/rendered.json"
jq --arg image "$image" '
    {services: {"launchplane-browser": (.services["launchplane-browser"] |
        .image = $image | .pull_policy = "never" | .restart = "no")},
     networks: {"launchplane-browser-network": {}}}
' "$fixture_dir/rendered.json" > "$fixture_dir/browser.json"
if [[ -n "$(docker ps --all --quiet --filter "label=com.docker.compose.project=$project")" ]]; then
    printf '%s\n' 'Refusing to reuse an existing browser qualification project.' >&2
    exit 1
fi
owns_project=1
run_interruptible "${compose[@]}" up -d --wait --wait-timeout 60
container="$("${compose[@]}" ps -q launchplane-browser)"
[[ -n "$container" ]]
docker inspect "$container" > "$fixture_dir/container.json"
jq -e '
    .[0] | .Config.User != "root" and .Config.User != "" and
    .HostConfig.ReadonlyRootfs and (.HostConfig.CapDrop | index("ALL") != null) and
    (.Mounts | all(.Type == "tmpfs")) and
    (.Config.Env | all(startswith("LAUNCHPLANE_BROWSER_CREDENTIAL_FIXTURE=") | not)) and
    (.NetworkSettings.Ports | to_entries | all(.value == null))
' "$fixture_dir/container.json" >/dev/null
network="$(jq -r '.[0].NetworkSettings.Networks | keys[0]' "$fixture_dir/container.json")"
run_interruptible docker run --rm --cidfile "$fixture_dir/probe.cid" --network "$network" \
    --env LAUNCHPLANE_BROWSER_WS_ENDPOINT=ws://launchplane-browser:3000/ \
    --env LAUNCHPLANE_BROWSER_CREDENTIAL_FIXTURE=control-plane-only \
    --entrypoint /app/.venv/bin/python "$image" -c '
from playwright.sync_api import sync_playwright
from control_plane.workflows.generic_web_deploy import GenericWebPostDeployGuard
from control_plane.workflows.verireel_testing_post_deploy import (
    _TestingPageExchanges,
    launch_testing_browser,
)
from control_plane.odoo_walkthrough_browser import require_safe_browser_control
with sync_playwright() as playwright:
    browser = launch_testing_browser(playwright.chromium)
    try:
        page = browser.new_page()
        heading = "Isolated testing browser"
        guard = GenericWebPostDeployGuard()
        exchanges = _TestingPageExchanges(page, "https://testing.invalid", guard)
        page.route("https://testing.invalid/sign-up", lambda route: route.fulfill(
            status=200, body=f"<h1>{heading}</h1>"))
        response = page.goto("https://testing.invalid/sign-up", wait_until="load")
        assert response is not None
        exchanges.finish_response(response)
        assert guard.exchanges and all(guard.exchanges.values())
        assert page.locator("h1").inner_text() == heading
        page.set_content("""<input id="business" name="name">
            <input id="private-password" type="password">
            <input id="private-hidden" type="hidden">
            <input id="private-file" type="file">
            <input id="private-key" name="api_key">
            <input id="private-token" name="access_token">
            <iframe srcdoc="&lt;input id=business&gt;"></iframe>""")
        require_safe_browser_control(page.locator("#business"))
        page.locator("#business").fill("TEST browser qualification")
        assert page.locator("#business").input_value() == "TEST browser qualification"
        refused = [page.locator(f"#{name}") for name in (
            "private-password", "private-hidden", "private-file", "private-key", "private-token")]
        refused.append(page.frame_locator("iframe").locator("#business"))
        for locator in refused:
            try:
                require_safe_browser_control(locator)
            except ValueError:
                continue
            raise AssertionError("Sensitive or iframe control was accepted")
    finally:
        browser.close()
'
printf '%s\n' 'Isolated browser rendered a page and refused sensitive/iframe controls without control-plane credentials, runtime mounts or published ports.'
