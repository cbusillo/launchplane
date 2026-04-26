#!/usr/bin/env bash
set -euo pipefail

ALLOWED_CTID="${PROD_GATE_ALLOWED_CTID:-}"
ALLOWED_STORAGE="${PROD_GATE_ALLOWED_STORAGE:-}"
SNAPSHOT_PREFIX="${PROD_GATE_SNAPSHOT_PREFIX:-}"
LEGACY_SNAPSHOT_PREFIX="${PROD_GATE_LEGACY_SNAPSHOT_PREFIX:-}"
SNAPSHOT_STYLE="${PROD_GATE_SNAPSHOT_STYLE:-timestamp_entropy_optional_tag}"

forbidden() {
	echo "forbidden" >&2
	exit 126
}

escape_ere_literal() {
	printf '%s' "$1" | sed -e 's/[][\\.^$*+?(){}|]/\\&/g'
}

SNAPSHOT_PREFIX_RE="$(escape_ere_literal "${SNAPSHOT_PREFIX}")"
LEGACY_SNAPSHOT_PREFIX_RE="$(escape_ere_literal "${LEGACY_SNAPSHOT_PREFIX}")"

build_snapshot_pattern() {
	local prefix_re="$1"

	case "${SNAPSHOT_STYLE}" in
	timestamp_optional_tag)
		printf '^%s-[0-9]{8}-[0-9]{6}(-[A-Za-z0-9._-]+)?$' "${prefix_re}"
		;;
	timestamp_entropy_optional_tag)
		printf '^%s-[0-9]{8}-[0-9]{6}-[A-Za-z0-9]{2,32}(-[a-z0-9]+(-[a-z0-9]+)*)?$' "${prefix_re}"
		;;
	*)
		forbidden
		;;
	esac
}

SNAPSHOT_PATTERN="$(build_snapshot_pattern "${SNAPSHOT_PREFIX_RE}")"
LEGACY_SNAPSHOT_PATTERN=""

if [[ -n "${LEGACY_SNAPSHOT_PREFIX}" ]]; then
	LEGACY_SNAPSHOT_PATTERN="$(build_snapshot_pattern "${LEGACY_SNAPSHOT_PREFIX_RE}")"
fi

snapshot_name_allowed() {
	local snapshot_name="$1"

	if [[ "${snapshot_name}" =~ ${SNAPSHOT_PATTERN} ]]; then
		return 0
	fi

	if [[ -n "${LEGACY_SNAPSHOT_PATTERN}" && "${snapshot_name}" =~ ${LEGACY_SNAPSHOT_PATTERN} ]]; then
		return 0
	fi

	return 1
}

[[ -n "${ALLOWED_CTID}" ]] || forbidden
[[ -n "${SNAPSHOT_PREFIX}" ]] || forbidden

raw_command="${SSH_ORIGINAL_COMMAND:-}"
[[ -n "${raw_command}" ]] || forbidden

read -r -a args <<<"${raw_command}"

# pct listsnapshot <ctid>
if [[ ${#args[@]} -eq 3 && "${args[0]}" == "pct" && "${args[1]}" == "listsnapshot" && "${args[2]}" == "${ALLOWED_CTID}" ]]; then
	exec /usr/sbin/pct listsnapshot "${ALLOWED_CTID}"
fi

# pct snapshot <ctid> <snapshot_name>
if [[ ${#args[@]} -eq 4 && "${args[0]}" == "pct" && "${args[1]}" == "snapshot" && "${args[2]}" == "${ALLOWED_CTID}" ]]; then
	if snapshot_name_allowed "${args[3]}"; then
		exec /usr/sbin/pct snapshot "${ALLOWED_CTID}" "${args[3]}"
	fi
fi

# pct delsnapshot <ctid> <snapshot_name>
if [[ ${#args[@]} -eq 4 && "${args[0]}" == "pct" && "${args[1]}" == "delsnapshot" && "${args[2]}" == "${ALLOWED_CTID}" ]]; then
	if snapshot_name_allowed "${args[3]}"; then
		exec /usr/sbin/pct delsnapshot "${ALLOWED_CTID}" "${args[3]}"
	fi
fi

# pct rollback <ctid> <snapshot_name>
if [[ ${#args[@]} -eq 4 && "${args[0]}" == "pct" && "${args[1]}" == "rollback" && "${args[2]}" == "${ALLOWED_CTID}" ]]; then
	if snapshot_name_allowed "${args[3]}"; then
		exec /usr/sbin/pct rollback "${ALLOWED_CTID}" "${args[3]}"
	fi
fi

# pct start <ctid>
if [[ ${#args[@]} -eq 3 && "${args[0]}" == "pct" && "${args[1]}" == "start" && "${args[2]}" == "${ALLOWED_CTID}" ]]; then
	exec /usr/sbin/pct start "${ALLOWED_CTID}"
fi

# vzdump <ctid> --mode snapshot --storage <storage>
if [[ -n "${ALLOWED_STORAGE}" && ${#args[@]} -eq 6 && "${args[0]}" == "vzdump" && "${args[1]}" == "${ALLOWED_CTID}" && "${args[2]}" == "--mode" && "${args[3]}" == "snapshot" && "${args[4]}" == "--storage" && "${args[5]}" == "${ALLOWED_STORAGE}" ]]; then
	exec /usr/bin/vzdump "${ALLOWED_CTID}" --mode snapshot --storage "${ALLOWED_STORAGE}"
fi

forbidden
