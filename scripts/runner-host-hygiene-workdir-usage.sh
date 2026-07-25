#!/bin/bash
set -euo pipefail

export LC_ALL=C
readonly config_directory="/etc/launchplane"
readonly config_file="/etc/launchplane/runner-host-hygiene-roots"
temporary_files=()

exec 3>&2
exec 2>/dev/null

cleanup() {
  local status=$?
  local temporary_file
  for temporary_file in "${temporary_files[@]}"; do
    /usr/bin/rm -f -- "$temporary_file" >/dev/null 2>&1 || true
  done
  if ((status != 0)); then
    /usr/bin/printf '%s\n' "runner workdir usage failed" >&3
  fi
  trap - EXIT
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

[[ $# -eq 1 ]]
readonly binding="$1"
[[ "$binding" =~ ^[a-z0-9][a-z0-9._-]{0,63}=/[^[:space:]]+$ ]]
readonly root_path="${binding#*=}"
[[ "$root_path" != "/" ]]
[[ "$root_path" != *"/../"* ]]
[[ "$root_path" != *"/.." ]]
[[ -d "$config_directory" && ! -L "$config_directory" ]]

read -r directory_owner directory_group directory_mode < <(
  /usr/bin/stat -c '%U %G %a' "$config_directory"
)
[[ "$directory_owner" == "root" ]]
[[ "$directory_group" == "root" ]]
[[ "$directory_mode" == "700" ]]

[[ -f "$config_file" && ! -L "$config_file" ]]
exec 4<"$config_file"
read -r config_owner config_group config_mode < <(
  /usr/bin/stat -Lc '%U %G %a' /proc/self/fd/4
)
[[ "$config_owner" == "root" ]]
[[ "$config_group" == "root" ]]
[[ "$config_mode" == "600" ]]
/usr/bin/grep -Fxq -- "$binding" <&4
[[ -d "$root_path" && ! -L "$root_path" ]]
[[ "$(/usr/bin/realpath -e -- "$root_path")" == "$root_path" ]]

workdir_file="$(/usr/bin/mktemp /tmp/launchplane-runner-workdirs.XXXXXX)"
apparent_file="$(/usr/bin/mktemp /tmp/launchplane-runner-apparent.XXXXXX)"
allocated_file="$(/usr/bin/mktemp /tmp/launchplane-runner-allocated.XXXXXX)"
temporary_files+=("$workdir_file" "$apparent_file" "$allocated_file")
/usr/bin/find "$root_path" \
  -xdev \
  -mindepth 2 \
  -maxdepth 2 \
  -type d \
  -name _work \
  -print0 >"$workdir_file"
[[ -s "$workdir_file" ]]

/usr/bin/du -s -b -x --null --files0-from="$workdir_file" >"$apparent_file"
/usr/bin/du -s -B1 -x --null --files0-from="$workdir_file" >"$allocated_file"

sum_du_output() {
  local output_file="$1"
  local record_count=0
  local size _path
  local total=0
  while IFS=$'\t' read -r -d '' size _path; do
    [[ "$size" =~ ^[0-9]+$ ]]
    total=$((total + size))
    record_count=$((record_count + 1))
  done <"$output_file"
  ((record_count > 0))
  /usr/bin/printf '%s\n' "$total"
}

apparent_bytes="$(sum_du_output "$apparent_file")"
allocated_bytes="$(sum_du_output "$allocated_file")"
[[ "$apparent_bytes" =~ ^[0-9]+$ ]]
[[ "$allocated_bytes" =~ ^[0-9]+$ ]]
/usr/bin/printf '%s\n%s\n' "$apparent_bytes" "$allocated_bytes"
