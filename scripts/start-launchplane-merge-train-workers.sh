#!/bin/sh

set -eu

launchplane_app_root="${LAUNCHPLANE_APP_ROOT:-/app}"
state_dir="${LAUNCHPLANE_STATE_DIR:-$launchplane_app_root/runtime}"
launchplane_database_url="${LAUNCHPLANE_DATABASE_URL:-}"

if [ -z "$launchplane_database_url" ]; then
	echo "Launchplane merge train workers refuse startup without LAUNCHPLANE_DATABASE_URL." >&2
	echo "The merge train reads its policy and records from Postgres-backed Launchplane storage." >&2
	exit 1
fi

mkdir -p "$state_dir"

set -- uv run launchplane service merge-train-workers run \
	--state-dir "$state_dir"

if [ -n "${LAUNCHPLANE_MERGE_TRAIN_SCHEDULER_INTERVAL_SECONDS:-}" ]; then
	set -- "$@" --interval-seconds "$LAUNCHPLANE_MERGE_TRAIN_SCHEDULER_INTERVAL_SECONDS"
fi

exec "$@"
