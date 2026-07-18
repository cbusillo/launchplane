#!/usr/bin/env bash
set -euo pipefail

: "${GITHUB_SHA:?Missing GitHub SHA.}"

authz_grant_mode="${LAUNCHPLANE_AUTHZ_GRANT_MODE:-dry_run}"
authz_policy_schema_version="${LAUNCHPLANE_AUTHZ_POLICY_SCHEMA_VERSION:-}"
authz_grant_reason="${LAUNCHPLANE_AUTHZ_GRANT_REASON:-}"
authz_grant_related_issue="${LAUNCHPLANE_AUTHZ_GRANT_RELATED_ISSUE:-}"
authz_grants_json="${LAUNCHPLANE_AUTHZ_GRANTS_JSON:-}"
authz_grants_output_dir="${LAUNCHPLANE_AUTHZ_GRANTS_OUTPUT_DIR:-${RUNNER_TEMP:-.}/launchplane-authz-grants}"
github_actions_grants_file="${authz_grants_output_dir}/github-actions-grants.json"

case "$authz_grant_mode" in
dry_run | apply) ;;
*)
	echo "LAUNCHPLANE_AUTHZ_GRANT_MODE must be dry_run or apply." >&2
	exit 1
	;;
esac
case "$authz_policy_schema_version" in
1 | 2) ;;
*)
	echo "LAUNCHPLANE_AUTHZ_POLICY_SCHEMA_VERSION must be 1 or 2." >&2
	exit 1
	;;
esac
if [ "$authz_grant_mode" = "apply" ] && [[ -z "${authz_grant_reason//[[:space:]]/}" ]]; then
	echo "Authz grant apply requires LAUNCHPLANE_AUTHZ_GRANT_REASON." >&2
	exit 1
fi
if [[ -z "${authz_grant_reason//[[:space:]]/}" ]]; then
	authz_grant_reason="Review operator-supplied authz grant"
fi
if [[ -z "${authz_grants_json//[[:space:]]/}" ]]; then
	echo "LAUNCHPLANE_AUTHZ_GRANTS_JSON must contain operator-supplied grants." >&2
	exit 1
fi

mkdir -p "$authz_grants_output_dir"

write_output() {
	local output_name="$1"
	local output_value="$2"
	if [ -n "${GITHUB_OUTPUT:-}" ]; then
		printf '%s=%s\n' "$output_name" "$output_value" >>"$GITHUB_OUTPUT"
	fi
}

sha256_stdin() {
	if command -v sha256sum >/dev/null 2>&1; then
		sha256sum | awk '{print $1}'
	elif command -v shasum >/dev/null 2>&1; then
		shasum -a 256 | awk '{print $1}'
	else
		openssl dgst -sha256 -r | awk '{print $1}'
	fi
}

canonical_grants="$({
	jq -cS --argjson schema_version "$authz_policy_schema_version" '
    def has_glob:
      contains("*") or contains("?") or contains("[");
    def trimmed:
      gsub("^[[:space:]]+|[[:space:]]+$"; "");
    def allowed_keys:
      [
        "repository",
        "repository_id",
        "repository_owner_id",
        "workflow_file",
        "product",
        "context",
        "action",
        "source_label",
        "event_name",
        "workflow_ref_suffix",
        "job_workflow_ref",
        "environment",
        "instance"
      ];
    if type != "array" or length == 0 then
      error("LAUNCHPLANE_AUTHZ_GRANTS_JSON must be a non-empty JSON array")
    else . end
    | map(
      if type != "object" then
        error("Each configured authz grant must be a JSON object")
      elif ((keys_unsorted - allowed_keys) | length) > 0 then
        error("Configured authz grants contain unsupported fields")
      else . end
      | {
        repository: (.repository // ""),
        repository_id: (.repository_id // ""),
        repository_owner_id: (.repository_owner_id // ""),
        workflow_file: (.workflow_file // ""),
        product: (.product // ""),
        context: (.context // ""),
        action: (.action // ""),
        source_label: (.source_label // ""),
        event_name: (.event_name // "workflow_dispatch"),
        workflow_ref_suffix: (.workflow_ref_suffix // "refs/heads/main"),
        job_workflow_ref: (.job_workflow_ref // ""),
        environment: (.environment // ""),
        instance: (.instance // "")
      }
    )
    | if any(.[];
        ([
          .repository,
          .repository_id,
          .repository_owner_id,
          .workflow_file,
          .product,
          .context,
          .action,
          .source_label,
          .event_name,
          .workflow_ref_suffix,
          .job_workflow_ref,
          .environment,
          .instance
        ] | any(.[]; type != "string"))
      ) then
        error("Configured authz grant fields must be strings")
      else . end
    | map(
        .repository |= trimmed
        | .repository_id |= trimmed
        | .repository_owner_id |= trimmed
        | .workflow_file |= trimmed
        | .product |= trimmed
        | .context |= trimmed
        | .action |= trimmed
        | .source_label |= trimmed
        | .event_name |= trimmed
        | .workflow_ref_suffix |= trimmed
        | .job_workflow_ref |= trimmed
        | .environment |= trimmed
        | .instance |= trimmed
      )
    | if any(.[];
        (.repository | length) == 0 or
        (.repository_id | test("^[0-9]+$") | not) or
        (.repository_owner_id | test("^[0-9]+$") | not) or
        (.workflow_file | length) == 0 or
        (.product | length) == 0 or
        (.context | length) == 0 or
        (.action | length) == 0 or
        (.source_label | length) == 0 or
        (.event_name | length) == 0 or
        (.workflow_ref_suffix | length) == 0
      ) then
        error("Each configured authz grant must include repository, numeric-string repository_id and repository_owner_id, workflow_file, product, context, action, source_label, event_name, and workflow_ref_suffix")
      elif any(.[];
        ([
          .repository,
          .repository_id,
          .repository_owner_id,
          .workflow_file,
          .product,
          .context,
          .action,
          .source_label,
          .event_name,
          .workflow_ref_suffix,
          .job_workflow_ref,
          .environment,
          .instance
        ] | any(.[]; test("[[:cntrl:]]"))) or
        (.repository | test("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$") | not) or
        (.workflow_file | test("^[A-Za-z0-9_.-]+\\.ya?ml$") | not) or
        ([
          .repository,
          .workflow_file,
          .product,
          .context,
          .action,
          .event_name,
          .workflow_ref_suffix,
          .job_workflow_ref,
          .environment,
          .instance
        ] | any(.[]; has_glob))
      ) then
        error("Configured authz grants must use printable exact repository, workflow, event, product, context, action, ref, and environment selectors without glob syntax")
      else . end
    | if $schema_version == 1 and any(.[]; .instance != "") then
        error("Schema-v1 configured authz grants cannot declare instance")
      else . end
    | sort_by([
        .repository,
        .repository_id,
        .repository_owner_id,
        .workflow_file,
        .product,
        .context,
        .action,
        .source_label,
        .event_name,
        .workflow_ref_suffix,
        .job_workflow_ref,
        .environment,
        .instance
      ])
  ' <<<"$authz_grants_json"
})"
canonical_review_set="$(
	jq -cnS \
		--argjson schema_version "$authz_policy_schema_version" \
		--argjson grants "$canonical_grants" \
		'{schema_version: $schema_version, grants: $grants}'
)"
configured_grants_sha256="$(printf '%s' "$canonical_review_set" | sha256_stdin)"

jq -c \
	--argjson schema_version "$authz_policy_schema_version" \
	--arg mode "$authz_grant_mode" \
	--arg reason "$authz_grant_reason" \
	--arg related_issue "$authz_grant_related_issue" \
	'[
    .[]
    | {
        schema_version: $schema_version,
        product: "launchplane",
        mode: $mode,
        reason: ($reason + " " + .source_label),
        related_issue: $related_issue,
        grant: {
          repository: .repository,
          repository_id: .repository_id,
          repository_owner_id: .repository_owner_id,
          workflow_refs: [(.repository + "/.github/workflows/" + .workflow_file + "@" + .workflow_ref_suffix)],
          job_workflow_refs: (if .job_workflow_ref == "" then [] else [.job_workflow_ref] end),
          event_names: [.event_name],
          environments: (if .environment == "" then [] else [.environment] end),
          products: [.product],
          contexts: [.context],
          instances: (if .instance == "" then [] else [.instance] end),
          actions: [.action],
          source_label: .source_label
        }
      }
  ]' <<<"$canonical_grants" >"$github_actions_grants_file"

grant_count="$(jq 'length' "$github_actions_grants_file")"
write_output authz_grants_output_dir "$authz_grants_output_dir"
write_output github_actions_grants_file "$github_actions_grants_file"
write_output github_actions_grant_count "$grant_count"
write_output has_github_actions_grants true
write_output configured_grants_sha256 "$configured_grants_sha256"
write_output authz_policy_schema_version "$authz_policy_schema_version"

echo "Rendered ${grant_count} operator-supplied schema-v${authz_policy_schema_version} compatibility GitHub Actions authz grants."
