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

schema_has_alembic_version() {
	database_url="$1"
	LAUNCHPLANE_DATABASE_URL="$database_url" uv run python - <<'PY'
from control_plane.storage.postgres import _build_engine
from control_plane.storage.postgres import Base
from sqlalchemy import inspect
import os
import sys

database_url = os.environ["LAUNCHPLANE_DATABASE_URL"]
engine = _build_engine(database_url)
try:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    if "alembic_version" in existing_tables:
        raise SystemExit(0)
    if not existing_tables.intersection(Base.metadata.tables):
        raise SystemExit(0)
    raise SystemExit(1)
except SystemExit:
    raise
except Exception as error:
    print(f"Could not inspect Launchplane database schema version: {error}", file=sys.stderr)
    raise SystemExit(2)
finally:
    engine.dispose()
PY
}

legacy_schema_revision() {
	database_url="$1"
	LAUNCHPLANE_DATABASE_URL="$database_url" uv run python - <<'PY'
from control_plane.storage.postgres import _build_engine
from sqlalchemy import inspect
import os
import sys

database_url = os.environ["LAUNCHPLANE_DATABASE_URL"]
engine = _build_engine(database_url)
try:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    if "launchplane_preview_enablement" in existing_tables:
        print("b1c3d5e7f9a1")
        raise SystemExit(0)
    print("fe94a0486977")
except Exception as error:
    print(f"Could not classify legacy Launchplane database schema: {error}", file=sys.stderr)
    raise SystemExit(1)
finally:
    engine.dispose()
PY
}

launchplane_app_root="${LAUNCHPLANE_APP_ROOT:-/app}"
state_dir="${LAUNCHPLANE_STATE_DIR:-$launchplane_app_root/runtime}"
launchplane_policy_toml="${LAUNCHPLANE_POLICY_TOML:-}"
launchplane_policy_b64="${LAUNCHPLANE_POLICY_B64:-}"
launchplane_policy_file="${LAUNCHPLANE_POLICY_FILE:-}"
launchplane_service_host="${LAUNCHPLANE_SERVICE_HOST:-0.0.0.0}"
launchplane_service_port="${LAUNCHPLANE_SERVICE_PORT:-8080}"
launchplane_service_audience="${LAUNCHPLANE_SERVICE_AUDIENCE:-localhost}"
launchplane_database_url="${LAUNCHPLANE_DATABASE_URL:-}"
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
	echo "Use a minimal bootstrap policy input; live product/workflow authorization belongs in DB-backed Launchplane records." >&2
	exit 1
fi

case "$policy_file" in
*.example)
	echo "Refusing to start Launchplane with example policy file: $policy_file" >&2
	echo "Provide a real bootstrap policy through LAUNCHPLANE_POLICY_TOML, LAUNCHPLANE_POLICY_B64, or LAUNCHPLANE_POLICY_FILE." >&2
	exit 1
	;;
esac

if [ ! -f "$policy_file" ]; then
	echo "Launchplane policy file does not exist: $policy_file" >&2
	exit 1
fi

if [ -z "$launchplane_database_url" ]; then
	echo "Launchplane service refuses startup without LAUNCHPLANE_DATABASE_URL." >&2
	echo "Filesystem state is local-only; service runs must use Postgres-backed Launchplane storage." >&2
	exit 1
fi

echo "Applying Launchplane database migrations before service startup."
schema_version_status=0
schema_has_alembic_version "$launchplane_database_url" || schema_version_status="$?"
case "$schema_version_status" in
0) ;;
1)
	legacy_revision="$(legacy_schema_revision "$launchplane_database_url")"
	echo "Existing Launchplane schema is unversioned; stamping Alembic revision ${legacy_revision} before upgrade."
	LAUNCHPLANE_DATABASE_URL="$launchplane_database_url" uv run alembic stamp "$legacy_revision"
	;;
*)
	echo "Launchplane database schema verification failed before migrations." >&2
	exit 1
	;;
esac
LAUNCHPLANE_DATABASE_URL="$launchplane_database_url" uv run alembic upgrade head

exec uv run launchplane service serve \
	--host "$launchplane_service_host" \
	--port "$launchplane_service_port" \
	--state-dir "$state_dir" \
	--database-url "$launchplane_database_url" \
	--policy-file "$policy_file" \
	--audience "$launchplane_service_audience"
