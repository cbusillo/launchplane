#!/usr/bin/env bash
set -euo pipefail

ALLOWED_CTID="${PROD_GATE_ALLOWED_CTID:-}"
GUEST_KIND="${PROD_GATE_GUEST_KIND:-lxc}"
ALLOWED_STORAGE="${PROD_GATE_ALLOWED_STORAGE:-}"
SNAPSHOT_PREFIX="${PROD_GATE_SNAPSHOT_PREFIX:-}"
LEGACY_SNAPSHOT_PREFIX="${PROD_GATE_LEGACY_SNAPSHOT_PREFIX:-}"
SNAPSHOT_STYLE="${PROD_GATE_SNAPSHOT_STYLE:-timestamp_entropy_optional_tag}"
ALLOW_RESTORE="${PROD_GATE_ALLOW_RESTORE:-}"

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
[[ "${ALLOWED_CTID}" =~ ^[0-9]+$ ]] || forbidden
[[ "${SNAPSHOT_PREFIX}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || forbidden
[[ -z "${ALLOWED_STORAGE}" || "${ALLOWED_STORAGE}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || forbidden
[[ "${ALLOW_RESTORE}" == "true" || "${ALLOW_RESTORE}" == "false" ]] || forbidden
case "${GUEST_KIND}" in
lxc) GUEST_COMMAND="pct" ;;
qemu) GUEST_COMMAND="qm" ;;
*) forbidden ;;
esac

raw_command="${SSH_ORIGINAL_COMMAND:-}"
[[ -n "${raw_command}" ]] || forbidden
[[ "${raw_command}" != *$'\n'* && "${raw_command}" != *$'\r'* ]] || forbidden

read -r -a args <<<"${raw_command}"

# Reveal only the exact forced-command binding, without widening shell access.
if [[ ${#args[@]} -eq 1 && "${args[0]}" == "launchplane-backup-boundary" && -n "${ALLOWED_STORAGE}" ]]; then
	printf '{"schema_version":1,"guest_kind":"%s","guest_id":"%s","storage_id":"%s","snapshot_prefix":"%s","restore_allowed":%s}\n' \
		"${GUEST_KIND}" "${ALLOWED_CTID}" "${ALLOWED_STORAGE}" "${SNAPSHOT_PREFIX}" "${ALLOW_RESTORE}"
	exit 0
fi

if [[ ${#args[@]} -eq 4 && "${args[0]}" == "pvesm" && "${args[1]}" == "status" && "${args[2]}" == "--storage" && -n "${ALLOWED_STORAGE}" && "${args[3]}" == "${ALLOWED_STORAGE}" ]]; then
	exec /usr/sbin/pvesm status --storage "${ALLOWED_STORAGE}"
fi

if [[ ${#args[@]} -eq 7 && "${args[0]}" == "pvesm" && "${args[1]}" == "list" && -n "${ALLOWED_STORAGE}" && "${args[2]}" == "${ALLOWED_STORAGE}" && "${args[3]}" == "--vmid" && "${args[4]}" == "${ALLOWED_CTID}" && "${args[5]}" == "--content" && "${args[6]}" == "backup" ]]; then
	exec /usr/sbin/pvesm list "${ALLOWED_STORAGE}" --vmid "${ALLOWED_CTID}" --content backup
fi

# pct listsnapshot <ctid>
if [[ ${#args[@]} -eq 3 && "${args[0]}" == "${GUEST_COMMAND}" && "${args[1]}" == "listsnapshot" && "${args[2]}" == "${ALLOWED_CTID}" ]]; then
	exec "/usr/sbin/${GUEST_COMMAND}" listsnapshot "${ALLOWED_CTID}"
fi

# pct snapshot <ctid> <snapshot_name>
if [[ ${#args[@]} -eq 4 && "${args[0]}" == "${GUEST_COMMAND}" && "${args[1]}" == "snapshot" && "${args[2]}" == "${ALLOWED_CTID}" ]]; then
	if snapshot_name_allowed "${args[3]}"; then
		exec "/usr/sbin/${GUEST_COMMAND}" snapshot "${ALLOWED_CTID}" "${args[3]}"
	fi
fi

# pct delsnapshot <ctid> <snapshot_name>
if [[ ${#args[@]} -eq 4 && "${args[0]}" == "${GUEST_COMMAND}" && "${args[1]}" == "delsnapshot" && "${args[2]}" == "${ALLOWED_CTID}" ]]; then
	if snapshot_name_allowed "${args[3]}"; then
		exec "/usr/sbin/${GUEST_COMMAND}" delsnapshot "${ALLOWED_CTID}" "${args[3]}"
	fi
fi

# pct rollback <ctid> <snapshot_name>
if [[ "${ALLOW_RESTORE}" == "true" && ${#args[@]} -eq 4 && "${args[0]}" == "${GUEST_COMMAND}" && "${args[1]}" == "rollback" && "${args[2]}" == "${ALLOWED_CTID}" ]]; then
	if snapshot_name_allowed "${args[3]}"; then
		exec "/usr/sbin/${GUEST_COMMAND}" rollback "${ALLOWED_CTID}" "${args[3]}"
	fi
fi

# pct start <ctid>
if [[ "${ALLOW_RESTORE}" == "true" && ${#args[@]} -eq 3 && "${args[0]}" == "${GUEST_COMMAND}" && "${args[1]}" == "start" && "${args[2]}" == "${ALLOWED_CTID}" ]]; then
	exec "/usr/sbin/${GUEST_COMMAND}" start "${ALLOWED_CTID}"
fi

# vzdump <ctid> --mode snapshot --storage <storage>
if [[ -n "${ALLOWED_STORAGE}" && ${#args[@]} -eq 6 && "${args[0]}" == "vzdump" && "${args[1]}" == "${ALLOWED_CTID}" && "${args[2]}" == "--mode" && "${args[3]}" == "snapshot" && "${args[4]}" == "--storage" && "${args[5]}" == "${ALLOWED_STORAGE}" ]]; then
	exec /usr/bin/vzdump "${ALLOWED_CTID}" --mode snapshot --storage "${ALLOWED_STORAGE}"
fi

forbidden
