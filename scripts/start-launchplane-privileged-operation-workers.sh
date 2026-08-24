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

startup_probe_timeout_seconds=90

if ! command -v timeout >/dev/null 2>&1; then
	echo "Launchplane privileged-operation workers require the timeout command." >&2
	exit 1
fi

set +e
set -- env -i \
	"PATH=$PATH" \
	"HOME=${HOME:-/tmp}" \
	"LANG=${LANG:-C.UTF-8}" \
	"LAUNCHPLANE_DATABASE_URL=$launchplane_database_url"
if [ -n "${LC_ALL:-}" ]; then set -- "$@" "LC_ALL=$LC_ALL"; fi
if [ -n "${LC_CTYPE:-}" ]; then set -- "$@" "LC_CTYPE=$LC_CTYPE"; fi
if [ -n "${LD_LIBRARY_PATH:-}" ]; then set -- "$@" "LD_LIBRARY_PATH=$LD_LIBRARY_PATH"; fi
if [ -n "${SSL_CERT_DIR:-}" ]; then set -- "$@" "SSL_CERT_DIR=$SSL_CERT_DIR"; fi
if [ -n "${SSL_CERT_FILE:-}" ]; then set -- "$@" "SSL_CERT_FILE=$SSL_CERT_FILE"; fi
if [ -n "${PGOPTIONS:-}" ]; then set -- "$@" "PGOPTIONS=$PGOPTIONS"; fi
if [ -n "${PGPASSFILE:-}" ]; then set -- "$@" "PGPASSFILE=$PGPASSFILE"; fi
if [ -n "${PGPASSWORD:-}" ]; then set -- "$@" "PGPASSWORD=$PGPASSWORD"; fi
if [ -n "${PGSERVICE:-}" ]; then set -- "$@" "PGSERVICE=$PGSERVICE"; fi
if [ -n "${PGSERVICEFILE:-}" ]; then set -- "$@" "PGSERVICEFILE=$PGSERVICEFILE"; fi
if [ -n "${PGSSLCERT:-}" ]; then set -- "$@" "PGSSLCERT=$PGSSLCERT"; fi
if [ -n "${PGSSLKEY:-}" ]; then set -- "$@" "PGSSLKEY=$PGSSLKEY"; fi
if [ -n "${PGSSLMODE:-}" ]; then set -- "$@" "PGSSLMODE=$PGSSLMODE"; fi
if [ -n "${PGSSLROOTCERT:-}" ]; then set -- "$@" "PGSSLROOTCERT=$PGSSLROOTCERT"; fi
set -- "$@" python -m control_plane.storage.privileged_operation_worker_probe
timeout --kill-after=5s "${startup_probe_timeout_seconds}s" "$@" >/dev/null 2>&1
startup_probe_status=$?
set -e
if [ "$startup_probe_status" -ne 0 ]; then
	if [ "$startup_probe_status" -eq 124 ]; then
		startup_probe_error_type="timeout"
	elif [ "$startup_probe_status" -eq 137 ]; then
		startup_probe_error_type="killed"
	else
		startup_probe_error_type="probe_failed"
	fi
	printf '{"error_type":"%s","event":"privileged_operation_worker_startup_probe_failed"}\n' \
		"$startup_probe_error_type"
	exit 1
fi

schema_probe_evidence_path="$state_dir/.privileged-operation-worker-schema-probe.$$"
umask 077
printf '%s\n' "launchplane-privileged-operation-worker-schema-probe-completed-v1" \
	>"$schema_probe_evidence_path"
exec 3<"$schema_probe_evidence_path"
rm -f "$schema_probe_evidence_path"

set -- uv run launchplane service privileged-operation-workers run \
	--state-dir "$state_dir" \
	--schema-probe-fd 3

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
