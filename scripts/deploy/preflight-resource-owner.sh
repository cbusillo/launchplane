#!/usr/bin/env bash
# One code-owned label protocol for creation and current-job cleanup.
preflight_owner_label() {
  local owner="${1:?exact rehearsal owner required}"
  if [[ ! "$owner" =~ ^[a-zA-Z0-9_.:-]{1,160}$ ]]; then
    echo "Invalid rehearsal owner." >&2
    return 1
  fi
  printf 'launchplane.stored-record-preflight.owner=%s' "$owner"
}

preflight_registry_config_path() {
  preflight_owner_label "${LAUNCHPLANE_PREFLIGHT_OWNER:?exact rehearsal owner required}" >/dev/null
  printf '%s/launchplane-preflight-registry-%s' "${RUNNER_TEMP:?runner temporary directory required}" "$LAUNCHPLANE_PREFLIGHT_OWNER"
}
