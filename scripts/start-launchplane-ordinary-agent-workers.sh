#!/bin/sh

set -eu

launchplane_app_root="${LAUNCHPLANE_APP_ROOT:-/app}"
state_dir="${LAUNCHPLANE_STATE_DIR:-$launchplane_app_root/runtime}"
launchplane_database_url="${LAUNCHPLANE_DATABASE_URL:-}"

if [ -z "$launchplane_database_url" ]; then
	echo "Launchplane ordinary-agent workers refuse startup without LAUNCHPLANE_DATABASE_URL." >&2
	echo "Worker execution must use Postgres-backed Launchplane records and leases." >&2
	exit 1
fi

mkdir -p "$state_dir"
startup_probe_timeout_seconds=90

if ! command -v timeout >/dev/null 2>&1; then
	echo "Launchplane ordinary-agent workers require the timeout command." >&2
	exit 1
fi

printf '%s\n' '{"event":"ordinary_agent_worker_entrypoint_started"}'

set +e
timeout --kill-after=5s "${startup_probe_timeout_seconds}s" env -i \
	"PATH=$PATH" \
	"HOME=${HOME:-/tmp}" \
	"LANG=${LANG:-C.UTF-8}" \
	"LAUNCHPLANE_DATABASE_URL=$launchplane_database_url" \
	uv run python -m control_plane.storage.ordinary_agent_worker_probe >/dev/null 2>&1
startup_probe_status=$?
set -e
if [ "$startup_probe_status" -ne 0 ]; then
	if [ "$startup_probe_status" -eq 2 ]; then
		startup_probe_error_type="schema_incompatible"
	else
		startup_probe_error_type="probe_failed"
	fi
	printf '{"error_type":"%s","event":"ordinary_agent_worker_startup_probe_failed"}\n' \
		"$startup_probe_error_type"
	exit 1
fi

printf '%s\n' '{"event":"ordinary_agent_worker_entrypoint_probe_succeeded"}'

schema_probe_evidence_path="$state_dir/.ordinary-agent-worker-schema-probe.$$"
umask 077
printf '%s\n' "launchplane-ordinary-agent-worker-schema-probe-completed-v1" \
	>"$schema_probe_evidence_path"
exec 3<"$schema_probe_evidence_path"
rm -f "$schema_probe_evidence_path"

set -- uv run launchplane service ordinary-agent-workers run \
	--state-dir "$state_dir" \
	--schema-probe-fd 3

if [ -n "${LAUNCHPLANE_ORDINARY_AGENT_WORKER_POLL_SECONDS:-}" ]; then
	set -- "$@" --poll-seconds "$LAUNCHPLANE_ORDINARY_AGENT_WORKER_POLL_SECONDS"
fi

if [ -n "${LAUNCHPLANE_ORDINARY_AGENT_WORKER_LEASE_SECONDS:-}" ]; then
	set -- "$@" --lease-seconds "$LAUNCHPLANE_ORDINARY_AGENT_WORKER_LEASE_SECONDS"
fi

exec env "LAUNCHPLANE_DATABASE_URL=$launchplane_database_url" "$@"
