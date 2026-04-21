#!/bin/sh

set -eu

write_text_file() {
  file_path="$1"
  file_contents="$2"
  mkdir -p "$(dirname "$file_path")"
  printf '%s\n' "$file_contents" >"$file_path"
}

write_base64_file() {
  file_path="$1"
  env_name="$2"
  mkdir -p "$(dirname "$file_path")"
  python3 - "$file_path" "$env_name" <<'PY'
import base64
import os
import sys

path = sys.argv[1]
env_name = sys.argv[2]
value = os.environ.get(env_name, "")
with open(path, "wb") as handle:
    handle.write(base64.b64decode(value))
PY
}

launchplane_app_root="${LAUNCHPLANE_APP_ROOT:-/app}"
state_dir="${LAUNCHPLANE_STATE_DIR:-$launchplane_app_root/state}"
launchplane_policy_toml="${LAUNCHPLANE_POLICY_TOML:-}"
launchplane_policy_b64="${LAUNCHPLANE_POLICY_B64:-}"
launchplane_policy_file="${LAUNCHPLANE_POLICY_FILE:-}"
launchplane_target_ids_toml="${LAUNCHPLANE_DOKPLOY_TARGET_IDS_TOML:-}"
launchplane_target_ids_b64="${LAUNCHPLANE_DOKPLOY_TARGET_IDS_B64:-}"
launchplane_runtime_env_toml="${LAUNCHPLANE_RUNTIME_ENVIRONMENTS_TOML:-}"
launchplane_runtime_env_b64="${LAUNCHPLANE_RUNTIME_ENVIRONMENTS_B64:-}"
launchplane_service_host="${LAUNCHPLANE_SERVICE_HOST:-0.0.0.0}"
launchplane_service_port="${LAUNCHPLANE_SERVICE_PORT:-8080}"
launchplane_service_audience="${LAUNCHPLANE_SERVICE_AUDIENCE:-launchplane.shinycomputers.com}"
policy_file=""

mkdir -p "$state_dir"

if [ -n "$launchplane_policy_toml" ]; then
  policy_file="/tmp/launchplane-authz.toml"
  write_text_file "$policy_file" "$launchplane_policy_toml"
elif [ -n "$launchplane_policy_b64" ]; then
  policy_file="/tmp/launchplane-authz.toml"
  write_base64_file "$policy_file" "LAUNCHPLANE_POLICY_B64"
elif [ -n "$launchplane_policy_file" ]; then
  policy_file="$launchplane_policy_file"
fi

if [ -z "$policy_file" ]; then
  echo "Launchplane service requires an explicit policy input via LAUNCHPLANE_POLICY_TOML, LAUNCHPLANE_POLICY_B64, or LAUNCHPLANE_POLICY_FILE." >&2
  echo "Copy $launchplane_app_root/config/launchplane-authz.toml.example to a real policy file and point LAUNCHPLANE_POLICY_FILE at that copy." >&2
  exit 1
fi

case "$policy_file" in
  *.example)
    echo "Refusing to start Launchplane with example policy file: $policy_file" >&2
    echo "Copy the example to a non-.example path and update the placeholder repo/workflow values first." >&2
    exit 1
    ;;
esac

if [ ! -f "$policy_file" ]; then
  echo "Launchplane policy file does not exist: $policy_file" >&2
  exit 1
fi

if [ -n "$launchplane_target_ids_toml" ]; then
  target_ids_file="/tmp/dokploy-targets.toml"
  write_text_file "$target_ids_file" "$launchplane_target_ids_toml"
  export ODOO_CONTROL_PLANE_DOKPLOY_TARGET_IDS_FILE="$target_ids_file"
elif [ -n "$launchplane_target_ids_b64" ]; then
  target_ids_file="/tmp/dokploy-targets.toml"
  write_base64_file "$target_ids_file" "LAUNCHPLANE_DOKPLOY_TARGET_IDS_B64"
  export ODOO_CONTROL_PLANE_DOKPLOY_TARGET_IDS_FILE="$target_ids_file"
fi

if [ -n "$launchplane_runtime_env_toml" ]; then
  runtime_env_file="/tmp/runtime-environments.toml"
  write_text_file "$runtime_env_file" "$launchplane_runtime_env_toml"
  export ODOO_CONTROL_PLANE_RUNTIME_ENVIRONMENTS_FILE="$runtime_env_file"
elif [ -n "$launchplane_runtime_env_b64" ]; then
  runtime_env_file="/tmp/runtime-environments.toml"
  write_base64_file "$runtime_env_file" "LAUNCHPLANE_RUNTIME_ENVIRONMENTS_B64"
  export ODOO_CONTROL_PLANE_RUNTIME_ENVIRONMENTS_FILE="$runtime_env_file"
fi

exec uv run launchplane service serve \
  --host "$launchplane_service_host" \
  --port "$launchplane_service_port" \
  --state-dir "$state_dir" \
  --policy-file "$policy_file" \
  --audience "$launchplane_service_audience"
