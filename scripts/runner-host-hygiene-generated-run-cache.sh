#!/bin/bash
set -euo pipefail

export LC_ALL=C
readonly config_directory="/etc/launchplane"
readonly config_file="/etc/launchplane/runner-host-hygiene-generated-caches"
readonly state_directory="/var/lib/launchplane/runner-host-hygiene-generated-cache"
readonly maximum_reported_entries=128
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
    /usr/bin/printf '%s\n' "generated run cache operation failed" >&3
  fi
  trap - EXIT
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

[[ $# -ge 2 ]]
readonly operation="$1"
readonly binding="$2"
[[ "$operation" == "observe" || "$operation" == "prune" ]]
[[ "$binding" =~ ^[a-z0-9][a-z0-9._-]{0,63}=/[^[:space:]]+$ ]]
if [[ "$operation" == "observe" ]]; then
  [[ $# -eq 4 ]]
  readonly observation_count="$3"
  readonly observation_interval_seconds="$4"
else
  [[ $# -eq 5 ]]
  readonly observation_count="$3"
  readonly observation_interval_seconds="$4"
fi
[[ "$observation_count" =~ ^([2-9]|10)$ ]]
[[ "$observation_interval_seconds" =~ ^([0-9]|[1-5][0-9]|60)$ ]]

readonly cache_key="${binding%%=*}"
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

configured_binding=""
expected_owner=""
expected_group=""
expected_mode=""
entry_prefix=""
minimum_age_hours=""
high_water_bytes=""
low_water_bytes=""
cooldown_hours=""
while IFS='|' read -r candidate_binding candidate_owner candidate_group candidate_mode candidate_prefix candidate_minimum_age candidate_high_water candidate_low_water candidate_cooldown; do
  [[ -n "$candidate_binding" ]]
  [[ "$candidate_binding" =~ ^[a-z0-9][a-z0-9._-]{0,63}=/[^[:space:]|]+$ ]]
  [[ "$candidate_owner" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]
  [[ "$candidate_group" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]
  [[ "$candidate_mode" =~ ^[0-7]{3,4}$ ]]
  [[ "$candidate_prefix" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,95}-$ ]]
  [[ "$candidate_minimum_age" =~ ^[1-9][0-9]*$ ]]
  [[ "$candidate_high_water" =~ ^[1-9][0-9]*$ ]]
  [[ "$candidate_low_water" =~ ^[0-9]+$ ]]
  [[ "$candidate_cooldown" =~ ^[1-9][0-9]*$ ]]
  ((candidate_low_water < candidate_high_water))
  if [[ "$candidate_binding" == "$binding" ]]; then
    [[ -z "$configured_binding" ]]
    configured_binding="$candidate_binding"
    expected_owner="$candidate_owner"
    expected_group="$candidate_group"
    expected_mode="$candidate_mode"
    entry_prefix="$candidate_prefix"
    minimum_age_hours="$candidate_minimum_age"
    high_water_bytes="$candidate_high_water"
    low_water_bytes="$candidate_low_water"
    cooldown_hours="$candidate_cooldown"
  fi
done <&4
[[ "$configured_binding" == "$binding" ]]
[[ -d "$root_path" && ! -L "$root_path" ]]
[[ "$(/usr/bin/realpath -e -- "$root_path")" == "$root_path" ]]

expected_uid="$(/usr/bin/id -u "$expected_owner")"
readonly expected_uid
readonly state_file="${state_directory}/${cache_key}.state"

last_cleanup_epoch_seconds=0
last_reclaimed_bytes=0
last_post_cleanup_allocated_bytes=0
last_removed_run_ids=""
read_state() {
  if [[ ! -e "$state_file" ]]; then
    return 0
  fi
  [[ -d "$state_directory" && ! -L "$state_directory" ]]
  read -r state_directory_owner state_directory_group state_directory_mode < <(
    /usr/bin/stat -c '%U %G %a' "$state_directory"
  )
  [[ "$state_directory_owner" == "root" ]]
  [[ "$state_directory_group" == "root" ]]
  [[ "$state_directory_mode" == "700" ]]
  [[ -f "$state_file" && ! -L "$state_file" ]]
  read -r state_owner state_group state_mode < <(
    /usr/bin/stat -c '%U %G %a' "$state_file"
  )
  [[ "$state_owner" == "root" ]]
  [[ "$state_group" == "root" ]]
  [[ "$state_mode" == "600" ]]
  local key value
  while IFS='=' read -r key value; do
    case "$key" in
      last_cleanup_epoch_seconds)
        [[ "$value" =~ ^[0-9]+$ ]]
        last_cleanup_epoch_seconds="$value"
        ;;
      last_attempt_epoch_seconds)
        [[ "$value" =~ ^[0-9]+$ ]]
        ;;
      last_reclaimed_bytes)
        [[ "$value" =~ ^[0-9]+$ ]]
        last_reclaimed_bytes="$value"
        ;;
      last_post_cleanup_allocated_bytes)
        [[ "$value" =~ ^[0-9]+$ ]]
        last_post_cleanup_allocated_bytes="$value"
        ;;
      last_removed_run_ids)
        [[ -z "$value" || "$value" =~ ^[1-9][0-9]*(,[1-9][0-9]*)*$ ]]
        last_removed_run_ids="$value"
        ;;
      *) return 1 ;;
    esac
  done <"$state_file"
}
read_state

declare -A apparent_by_run=()
declare -A allocated_by_run=()
declare -A age_by_run=()
declare -A handle_observations_by_run=()
declare -A entry_names_by_run=()
declare -A entry_identity_by_name=()
declare -A entry_version_by_name=()
declare -a worker_observations=()
measurement_status="complete"
owner_valid=true
mode_valid=true
symlink_safe=true
observed_epoch_seconds=0
entry_count=0
total_apparent_bytes=0
total_allocated_bytes=0
entries_truncated=false

measure_directory() {
  local directory_path="$1"
  local measurement_mode="$2"
  local output status=0
  if [[ "$measurement_mode" == "apparent" ]]; then
    output="$(/usr/bin/du -s -b -x -- "$directory_path")" || status=$?
  else
    output="$(/usr/bin/du -s -B1 -x -- "$directory_path")" || status=$?
  fi
  if ((status != 0)); then
    measurement_status="partial"
    measured_bytes=0
    return 0
  fi
  output="${output%%$'\t'*}"
  [[ "$output" =~ ^[0-9]+$ ]]
  measured_bytes="$output"
}

count_open_handles() {
  declare -A sample_counts=()
  local fd target relative top_name run_id
  for fd in /proc/[0-9]*/fd/*; do
    target="$(/usr/bin/readlink -- "$fd" 2>/dev/null || true)"
    target="${target% (deleted)}"
    [[ "$target" == "$root_path/"* ]] || continue
    relative="${target#"$root_path/"}"
    top_name="${relative%%/*}"
    if [[ "$top_name" =~ ^${entry_prefix}([0-9]+)-[A-Za-z0-9._-]+$ ]]; then
      run_id="${BASH_REMATCH[1]}"
      sample_counts["$run_id"]=$(( ${sample_counts["$run_id"]:-0} + 1 ))
    fi
  done
  for run_id in "${!apparent_by_run[@]}"; do
    if [[ -n "${handle_observations_by_run["$run_id"]:-}" ]]; then
      handle_observations_by_run["$run_id"]+=","
    fi
    handle_observations_by_run["$run_id"]+="${sample_counts["$run_id"]:-0}"
  done
}

entry_has_mount_escape() {
  local entry_path="$1"
  local mount_output mount_target
  if ! mount_output="$(/usr/bin/findmnt -rn -R -o TARGET --target "$entry_path")"; then
    return 0
  fi
  while IFS= read -r mount_target; do
    if [[ "$mount_target" == "$entry_path" || "$mount_target" == "$entry_path/"* ]]; then
      return 0
    fi
  done <<<"$mount_output"
  return 1
}

collect_inventory() {
  local samples="$1"
  local interval="$2"
  apparent_by_run=()
  allocated_by_run=()
  age_by_run=()
  handle_observations_by_run=()
  entry_names_by_run=()
  entry_identity_by_name=()
  entry_version_by_name=()
  worker_observations=()
  measurement_status="complete"
  owner_valid=true
  mode_valid=true
  symlink_safe=true
  observed_epoch_seconds="$(/usr/bin/date +%s)"
  entry_count=0
  total_apparent_bytes=0
  total_allocated_bytes=0
  entries_truncated=false

  read -r root_owner root_group root_mode < <(
    /usr/bin/stat -c '%U %G %a' "$root_path"
  )
  [[ "$root_owner" == "$expected_owner" ]] || owner_valid=false
  [[ "$root_group" == "$expected_group" ]] || owner_valid=false
  [[ "$root_mode" == "$expected_mode" ]] || mode_valid=false
  quarantine_path="$(
    /usr/bin/find "$root_path" -xdev -mindepth 1 -maxdepth 1 -type d \
      -name '.launchplane-generated-run-cache-prune-*' -print -quit
  )"
  if [[ -n "$quarantine_path" ]]; then
    measurement_status="partial"
  fi

  local entry_path entry_name run_id apparent_bytes allocated_bytes modified_epoch age_seconds entry_identity entry_version
  while IFS= read -r -d '' entry_path; do
    entry_name="${entry_path##*/}"
    [[ "$entry_name" =~ ^${entry_prefix}([0-9]+)-[A-Za-z0-9._-]+$ ]] || continue
    run_id="${BASH_REMATCH[1]}"
    read -r entry_owner entry_group entry_mode < <(
      /usr/bin/stat -c '%U %G %a' "$entry_path"
    )
    [[ "$entry_owner" == "$expected_owner" ]] || owner_valid=false
    [[ "$entry_group" == "$expected_group" ]] || owner_valid=false
    if (( (8#$entry_mode & 0022) != 0 )); then
      mode_valid=false
    fi
    symlink_path="$(/usr/bin/find "$entry_path" -xdev -type l -print -quit)"
    if [[ -n "$symlink_path" ]] || entry_has_mount_escape "$entry_path"; then
      symlink_safe=false
    fi
    measure_directory "$entry_path" apparent
    apparent_bytes="$measured_bytes"
    measure_directory "$entry_path" allocated
    allocated_bytes="$measured_bytes"
    modified_epoch="$(/usr/bin/stat -c '%Y' "$entry_path")"
    [[ "$modified_epoch" =~ ^[0-9]+$ ]]
    age_seconds=$((observed_epoch_seconds - modified_epoch))
    if ((age_seconds < 0)); then
      age_seconds=0
    fi
    apparent_by_run["$run_id"]=$(( ${apparent_by_run["$run_id"]:-0} + apparent_bytes ))
    allocated_by_run["$run_id"]=$(( ${allocated_by_run["$run_id"]:-0} + allocated_bytes ))
    if [[ -z "${age_by_run["$run_id"]:-}" ]] || ((age_seconds < age_by_run["$run_id"])); then
      age_by_run["$run_id"]="$age_seconds"
    fi
    entry_identity="$(/usr/bin/stat -c '%d:%i' "$entry_path")"
    entry_version="$(/usr/bin/stat -c '%Z' "$entry_path")"
    [[ "$entry_identity" =~ ^[0-9]+:[0-9]+$ ]]
    [[ "$entry_version" =~ ^[0-9]+$ ]]
    entry_identity_by_name["$entry_name"]="$entry_identity"
    entry_version_by_name["$entry_name"]="$entry_version"
    if [[ -n "${entry_names_by_run["$run_id"]:-}" ]]; then
      entry_names_by_run["$run_id"]+=","
    fi
    entry_names_by_run["$run_id"]+="$entry_name"
    total_apparent_bytes=$((total_apparent_bytes + apparent_bytes))
    total_allocated_bytes=$((total_allocated_bytes + allocated_bytes))
  done < <(
    /usr/bin/find "$root_path" -xdev -mindepth 1 -maxdepth 1 -type d \
      -name "${entry_prefix}*" -print0
  )
  entry_count="${#apparent_by_run[@]}"

  local sample_index worker_count
  for ((sample_index = 0; sample_index < samples; sample_index += 1)); do
    worker_count="$(
      /usr/bin/ps -u "$expected_uid" -o args= \
        | /usr/bin/grep -c '[R]unner.Worker' || true
    )"
    [[ "$worker_count" =~ ^[0-9]+$ ]]
    worker_observations+=("$worker_count")
    count_open_handles
    if ((sample_index + 1 < samples)); then
      /usr/bin/sleep "$interval"
    fi
  done
}

json_number_array() {
  local joined="$1"
  if [[ -z "$joined" ]]; then
    /usr/bin/printf '[]'
  else
    /usr/bin/printf '[%s]' "$joined"
  fi
}

emit_inventory() {
  local sorted_file
  sorted_file="$(/usr/bin/mktemp /tmp/launchplane-generated-cache.XXXXXX)"
  temporary_files+=("$sorted_file")
  local run_id
  for run_id in "${!apparent_by_run[@]}"; do
    /usr/bin/printf '%s\t%s\t%s\t%s\n' \
      "$run_id" \
      "${apparent_by_run["$run_id"]}" \
      "${allocated_by_run["$run_id"]}" \
      "${age_by_run["$run_id"]}" >>"$sorted_file"
  done
  /usr/bin/sort -t $'\t' -k4,4nr -k1,1n -o "$sorted_file" "$sorted_file"
  if ((entry_count > maximum_reported_entries)); then
    entries_truncated=true
  fi
  local worker_joined
  worker_joined="$(IFS=,; /usr/bin/printf '%s' "${worker_observations[*]}")"
  /usr/bin/printf '{"record_type":"summary","cache_key":"%s","observed_epoch_seconds":%s,"apparent_bytes":%s,"allocated_bytes":%s,"entry_count":%s,"measurement_status":"%s","owner_valid":%s,"mode_valid":%s,"symlink_safe":%s,"owner_worker_observations":' \
    "$cache_key" \
    "$observed_epoch_seconds" \
    "$total_apparent_bytes" \
    "$total_allocated_bytes" \
    "$entry_count" \
    "$measurement_status" \
    "$owner_valid" \
    "$mode_valid" \
    "$symlink_safe"
  json_number_array "$worker_joined"
  /usr/bin/printf ',"entries_truncated":%s,"minimum_age_hours":%s,"high_water_bytes":%s,"low_water_bytes":%s,"cooldown_hours":%s,"last_cleanup_epoch_seconds":%s,"last_reclaimed_bytes":%s,"last_post_cleanup_allocated_bytes":%s,"last_removed_run_ids":' \
    "$entries_truncated" \
    "$minimum_age_hours" \
    "$high_water_bytes" \
    "$low_water_bytes" \
    "$cooldown_hours" \
    "$last_cleanup_epoch_seconds" \
    "$last_reclaimed_bytes" \
    "$last_post_cleanup_allocated_bytes"
  json_number_array "$last_removed_run_ids"
  /usr/bin/printf '}\n'
  local emitted=0 apparent_bytes allocated_bytes age_seconds observations
  while IFS=$'\t' read -r run_id apparent_bytes allocated_bytes age_seconds; do
    ((emitted < maximum_reported_entries)) || break
    observations="${handle_observations_by_run["$run_id"]:-}"
    /usr/bin/printf '{"record_type":"entry","cache_key":"%s","run_id":%s,"apparent_bytes":%s,"allocated_bytes":%s,"age_seconds":%s,"open_handle_observations":' \
      "$cache_key" \
      "$run_id" \
      "$apparent_bytes" \
      "$allocated_bytes" \
      "$age_seconds"
    json_number_array "$observations"
    /usr/bin/printf '}\n'
    emitted=$((emitted + 1))
  done <"$sorted_file"
}

write_state() {
  local cleanup_epoch="$1"
  local attempt_epoch="$2"
  local reclaimed_bytes="$3"
  local post_allocated_bytes="$4"
  local removed_run_ids="$5"
  /usr/bin/install -d -o root -g root -m 0700 "$state_directory"
  [[ -d "$state_directory" && ! -L "$state_directory" ]]
  read -r state_directory_owner state_directory_group state_directory_mode < <(
    /usr/bin/stat -c '%U %G %a' "$state_directory"
  )
  [[ "$state_directory_owner" == "root" ]]
  [[ "$state_directory_group" == "root" ]]
  [[ "$state_directory_mode" == "700" ]]
  local state_temporary
  state_temporary="$(/usr/bin/mktemp "${state_directory}/.${cache_key}.XXXXXX")"
  temporary_files+=("$state_temporary")
  /usr/bin/printf 'last_cleanup_epoch_seconds=%s\nlast_attempt_epoch_seconds=%s\nlast_reclaimed_bytes=%s\nlast_post_cleanup_allocated_bytes=%s\nlast_removed_run_ids=%s\n' \
    "$cleanup_epoch" \
    "$attempt_epoch" \
    "$reclaimed_bytes" \
    "$post_allocated_bytes" \
    "$removed_run_ids" >"$state_temporary"
  /usr/bin/chown root:root "$state_temporary"
  /usr/bin/chmod 0600 "$state_temporary"
  /usr/bin/mv -f -- "$state_temporary" "$state_file"
  /usr/bin/sync -f "$state_file"
  /usr/bin/sync -f "$state_directory"
}

if [[ "$operation" == "observe" ]]; then
  collect_inventory "$observation_count" "$observation_interval_seconds"
  emit_inventory
  exit 0
fi

readonly prune_observation_count="$3"
readonly prune_observation_interval_seconds="$4"
readonly requested_run_ids_csv="$5"
[[ "$prune_observation_count" =~ ^([2-9]|10)$ ]]
[[ "$prune_observation_interval_seconds" =~ ^([0-9]|[1-5][0-9]|60)$ ]]
[[ "$requested_run_ids_csv" =~ ^[1-9][0-9]*(,[1-9][0-9]*)*$ ]]

collect_inventory "$prune_observation_count" "$prune_observation_interval_seconds"
[[ "$measurement_status" == "complete" ]]
[[ "$owner_valid" == "true" ]]
[[ "$mode_valid" == "true" ]]
[[ "$symlink_safe" == "true" ]]
[[ "$entries_truncated" == "false" ]]
((total_allocated_bytes > high_water_bytes))
for worker_count in "${worker_observations[@]}"; do
  ((worker_count == 0))
done
if ((last_cleanup_epoch_seconds > 0)); then
  ((observed_epoch_seconds >= last_cleanup_epoch_seconds + cooldown_hours * 3600))
fi

IFS=',' read -r -a requested_run_ids <<<"$requested_run_ids_csv"
declare -A requested_seen=()
for run_id in "${requested_run_ids[@]}"; do
  [[ -z "${requested_seen["$run_id"]:-}" ]]
  requested_seen["$run_id"]=1
  [[ -n "${age_by_run["$run_id"]:-}" ]]
  ((age_by_run["$run_id"] >= minimum_age_hours * 3600))
  IFS=',' read -r -a handle_observations <<<"${handle_observations_by_run["$run_id"]}"
  ((${#handle_observations[@]} == prune_observation_count))
  for handle_count in "${handle_observations[@]}"; do
    ((handle_count == 0))
  done
done

readonly pre_prune_allocated_bytes="$total_allocated_bytes"
worker_count="$(
  /usr/bin/ps -u "$expected_uid" -o args= \
    | /usr/bin/grep -c '[R]unner.Worker' || true
)"
[[ "$worker_count" =~ ^[0-9]+$ ]]
((worker_count == 0))
count_open_handles
for run_id in "${requested_run_ids[@]}"; do
  IFS=',' read -r -a handle_observations <<<"${handle_observations_by_run["$run_id"]}"
  ((${#handle_observations[@]} == prune_observation_count + 1))
  ((${handle_observations[-1]} == 0))
done

quarantine_directory="$(
  /usr/bin/mktemp -d "${root_path}/.launchplane-generated-run-cache-prune.XXXXXX"
)"
/usr/bin/chown root:root "$quarantine_directory"
/usr/bin/chmod 0700 "$quarantine_directory"
removed_entries=0
for run_id in "${requested_run_ids[@]}"; do
  matched_entries=0
  IFS=',' read -r -a recorded_entry_names <<<"${entry_names_by_run["$run_id"]}"
  for entry_name in "${recorded_entry_names[@]}"; do
    entry_path="$root_path/$entry_name"
    [[ "$entry_name" =~ ^${entry_prefix}${run_id}-[A-Za-z0-9._-]+$ ]]
    [[ -d "$entry_path" && ! -L "$entry_path" ]]
    [[ "$(/usr/bin/realpath -e -- "$entry_path")" == "$root_path/$entry_name" ]]
    [[ "$(/usr/bin/stat -c '%d:%i:%Z' "$entry_path")" == "${entry_identity_by_name["$entry_name"]}:${entry_version_by_name["$entry_name"]}" ]]
    read -r entry_owner entry_group entry_mode < <(
      /usr/bin/stat -c '%U %G %a' "$entry_path"
    )
    [[ "$entry_owner" == "$expected_owner" ]]
    [[ "$entry_group" == "$expected_group" ]]
    (( (8#$entry_mode & 0022) == 0 ))
    symlink_path="$(/usr/bin/find "$entry_path" -xdev -type l -print -quit)"
    [[ -z "$symlink_path" ]]
    if entry_has_mount_escape "$entry_path"; then
      exit 1
    fi
    current_age_seconds=$(( $(/usr/bin/date +%s) - $(/usr/bin/stat -c '%Y' "$entry_path") ))
    ((current_age_seconds >= minimum_age_hours * 3600))
    quarantined_path="$quarantine_directory/$entry_name"
    /usr/bin/mv -T -- "$entry_path" "$quarantined_path"
    [[ "$(/usr/bin/stat -c '%d:%i' "$quarantined_path")" == "${entry_identity_by_name["$entry_name"]}" ]]
    matched_entries=$((matched_entries + 1))
    removed_entries=$((removed_entries + 1))
  done
  ((matched_entries > 0))
done
/usr/bin/rm -rf --one-file-system -- "$quarantine_directory"
[[ ! -e "$quarantine_directory" ]]

collect_inventory 1 0
post_prune_allocated_bytes="$total_allocated_bytes"
reclaimed_bytes=$((pre_prune_allocated_bytes - post_prune_allocated_bytes))
if ((reclaimed_bytes < 0)); then
  reclaimed_bytes=0
fi
cleanup_epoch_seconds="$(/usr/bin/date +%s)"
successful_cleanup_epoch_seconds="$last_cleanup_epoch_seconds"
if ((post_prune_allocated_bytes <= low_water_bytes)); then
  successful_cleanup_epoch_seconds="$cleanup_epoch_seconds"
fi
write_state \
  "$successful_cleanup_epoch_seconds" \
  "$cleanup_epoch_seconds" \
  "$reclaimed_bytes" \
  "$post_prune_allocated_bytes" \
  "$requested_run_ids_csv"
((post_prune_allocated_bytes <= low_water_bytes))
/usr/bin/printf '{"cache_key":"%s","removed_entries":%s,"reclaimed_bytes":%s,"post_allocated_bytes":%s}\n' \
  "$cache_key" \
  "$removed_entries" \
  "$reclaimed_bytes" \
  "$post_prune_allocated_bytes"
