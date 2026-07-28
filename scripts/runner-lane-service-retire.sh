#!/bin/bash

set -euo pipefail

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077

allowed_targets_file=/etc/launchplane/runner-lane-retirement-targets

fail() {
  printf '%s\n' "runner lane service retirement failed" >&2
  exit 1
}

if [[ $# -ne 4 ]]; then
  fail
fi

repository=$1
lane_name=$2
registration_root=$3
service_user=$4
if [[ ! "$repository" =~ ^[a-z0-9][a-z0-9._-]{0,127}/[a-z0-9][a-z0-9._-]{0,127}$ ]]; then
  fail
fi
if [[ ! "$lane_name" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ ]]; then
  fail
fi
if [[ ! "$service_user" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
  fail
fi
if [[ ! "$registration_root" =~ ^/[a-zA-Z0-9._/-]+$ ]]; then
  fail
fi
if [[ "$registration_root" == *"//"* || "$registration_root" == *"/../"* || "$registration_root" == */.. ]]; then
  fail
fi
if [[ "${SUDO_USER:-}" != "$service_user" ]]; then
  fail
fi

if [[ ! -f "$allowed_targets_file" || -L "$allowed_targets_file" ]]; then
  fail
fi
if [[ $(stat -c '%u' "$allowed_targets_file" 2>/dev/null) != 0 ]]; then
  fail
fi
if [[ $(stat -c '%a' "$allowed_targets_file" 2>/dev/null) != 600 ]]; then
  fail
fi
if [[ $(realpath -e "$allowed_targets_file" 2>/dev/null) != "$allowed_targets_file" ]]; then
  fail
fi
target_record="${repository}"$'\t'"${lane_name}"$'\t'"${registration_root}"$'\t'"${service_user}"
if ! grep -Fxq -- "$target_record" "$allowed_targets_file"; then
  fail
fi
if [[ $(realpath -e "$registration_root" 2>/dev/null) != "$registration_root" ]]; then
  fail
fi

unit_name="launchplane-runner@${lane_name}.service"
if ! systemctl cat "$unit_name" >/dev/null 2>&1; then
  fail
fi
if [[ $(systemctl show "$unit_name" --property=User --value 2>/dev/null) != "$service_user" ]]; then
  fail
fi
unit_exec_start=$(systemctl show "$unit_name" --property=ExecStart --value 2>/dev/null) || fail
if [[ "$unit_exec_start" != *"${registration_root}/${lane_name}"* ]]; then
  fail
fi

systemctl stop "$unit_name"
systemctl disable "$unit_name"
if systemctl is-active --quiet "$unit_name"; then
  fail
fi
if systemctl is-enabled --quiet "$unit_name"; then
  fail
fi

printf '%s\n' "runner lane service retired"
