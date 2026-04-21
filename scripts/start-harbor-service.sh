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
policy_file=""

mkdir -p "$state_dir"

if [ -n "${HARBOR_POLICY_TOML:-}" ]; then
  policy_file="/tmp/harbor-authz.toml"
  write_text_file "$policy_file" "$HARBOR_POLICY_TOML"
elif [ -n "${HARBOR_POLICY_B64:-}" ]; then
  policy_file="/tmp/harbor-authz.toml"
  write_base64_file "$policy_file" "HARBOR_POLICY_B64"
elif [ -n "${HARBOR_POLICY_FILE:-}" ]; then
  policy_file="$HARBOR_POLICY_FILE"
fi

if [ -z "$policy_file" ]; then
  echo "Harbor service requires an explicit policy input via HARBOR_POLICY_TOML, HARBOR_POLICY_B64, or HARBOR_POLICY_FILE." >&2
  echo "Copy $app_root/config/harbor-authz.toml.example to a real policy file and point HARBOR_POLICY_FILE at that copy." >&2
  exit 1
fi

case "$policy_file" in
  *.example)
    echo "Refusing to start Harbor with example policy file: $policy_file" >&2
    echo "Copy the example to a non-.example path and update the placeholder repo/workflow values first." >&2
    exit 1
    ;;
esac

if [ ! -f "$policy_file" ]; then
  echo "Harbor policy file does not exist: $policy_file" >&2
  exit 1
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
