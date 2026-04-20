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

app_root="${HARBOR_APP_ROOT:-/app}"
state_dir="${HARBOR_STATE_DIR:-$app_root/state}"
policy_file="${HARBOR_POLICY_FILE:-$app_root/config/harbor-authz.toml.example}"

mkdir -p "$state_dir"

if [ -n "${HARBOR_POLICY_TOML:-}" ]; then
  policy_file="/tmp/harbor-authz.toml"
  write_text_file "$policy_file" "$HARBOR_POLICY_TOML"
elif [ -n "${HARBOR_POLICY_B64:-}" ]; then
  policy_file="/tmp/harbor-authz.toml"
  write_base64_file "$policy_file" "HARBOR_POLICY_B64"
fi

if [ -n "${HARBOR_DOKPLOY_TARGET_IDS_TOML:-}" ]; then
  target_ids_file="/tmp/dokploy-targets.toml"
  write_text_file "$target_ids_file" "$HARBOR_DOKPLOY_TARGET_IDS_TOML"
  export ODOO_CONTROL_PLANE_DOKPLOY_TARGET_IDS_FILE="$target_ids_file"
elif [ -n "${HARBOR_DOKPLOY_TARGET_IDS_B64:-}" ]; then
  target_ids_file="/tmp/dokploy-targets.toml"
  write_base64_file "$target_ids_file" "HARBOR_DOKPLOY_TARGET_IDS_B64"
  export ODOO_CONTROL_PLANE_DOKPLOY_TARGET_IDS_FILE="$target_ids_file"
fi

if [ -n "${HARBOR_RUNTIME_ENVIRONMENTS_TOML:-}" ]; then
  runtime_env_file="/tmp/runtime-environments.toml"
  write_text_file "$runtime_env_file" "$HARBOR_RUNTIME_ENVIRONMENTS_TOML"
  export ODOO_CONTROL_PLANE_RUNTIME_ENVIRONMENTS_FILE="$runtime_env_file"
elif [ -n "${HARBOR_RUNTIME_ENVIRONMENTS_B64:-}" ]; then
  runtime_env_file="/tmp/runtime-environments.toml"
  write_base64_file "$runtime_env_file" "HARBOR_RUNTIME_ENVIRONMENTS_B64"
  export ODOO_CONTROL_PLANE_RUNTIME_ENVIRONMENTS_FILE="$runtime_env_file"
fi

exec uv run harbor service serve \
  --host "${HARBOR_SERVICE_HOST:-0.0.0.0}" \
  --port "${HARBOR_SERVICE_PORT:-8080}" \
  --state-dir "$state_dir" \
  --policy-file "$policy_file" \
  --audience "${HARBOR_SERVICE_AUDIENCE:-harbor.shinycomputers.com}"
