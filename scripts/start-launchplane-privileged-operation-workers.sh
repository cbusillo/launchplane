#!/bin/sh

set -eu

launchplane_app_root="${LAUNCHPLANE_APP_ROOT:-/app}"
state_dir="${LAUNCHPLANE_STATE_DIR:-$launchplane_app_root/runtime}"
launchplane_database_url="${LAUNCHPLANE_DATABASE_URL:-}"

if [ -z "$launchplane_database_url" ]; then
	echo "Launchplane privileged-operation workers refuse startup without LAUNCHPLANE_DATABASE_URL." >&2
	echo "Worker execution must use Postgres-backed Launchplane records and leases." >&2
	exit 1
fi

mkdir -p "$state_dir"

set -- uv run launchplane service privileged-operation-workers run \
	--state-dir "$state_dir"

if [ -n "${LAUNCHPLANE_PRIVILEGED_OPERATION_WORKER_POLL_SECONDS:-}" ]; then
	set -- "$@" --poll-seconds "$LAUNCHPLANE_PRIVILEGED_OPERATION_WORKER_POLL_SECONDS"
fi

if [ -n "${LAUNCHPLANE_PRIVILEGED_OPERATION_WORKER_LIMIT:-}" ]; then
	set -- "$@" --limit "$LAUNCHPLANE_PRIVILEGED_OPERATION_WORKER_LIMIT"
fi

if [ -n "${LAUNCHPLANE_PRIVILEGED_OPERATION_WORKER_ERROR_BACKOFF_SECONDS:-}" ]; then
	set -- "$@" --error-backoff-seconds "$LAUNCHPLANE_PRIVILEGED_OPERATION_WORKER_ERROR_BACKOFF_SECONDS"
fi

if [ -n "${LAUNCHPLANE_PRIVILEGED_OPERATION_WORKER_MAX_CONSECUTIVE_ERRORS:-}" ]; then
	set -- "$@" --max-consecutive-errors "$LAUNCHPLANE_PRIVILEGED_OPERATION_WORKER_MAX_CONSECUTIVE_ERRORS"
fi

exec "$@"
