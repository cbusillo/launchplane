#!/usr/bin/env bash
set -euo pipefail

: "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:?Missing GitHub Actions OIDC request token.}"
: "${ACTIONS_ID_TOKEN_REQUEST_URL:?Missing GitHub Actions OIDC request URL.}"
: "${GITHUB_REPOSITORY:?Missing GitHub repository.}"
: "${GITHUB_SHA:?Missing GitHub SHA.}"
: "${LAUNCHPLANE_SERVICE_AUDIENCE:?Missing Launchplane service audience.}"
: "${LAUNCHPLANE_SERVICE_URL:?Missing Launchplane service URL.}"

oidc_token="$({
	curl -fsSL \
		-H "Authorization: bearer ${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" \
		"${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=${LAUNCHPLANE_SERVICE_AUDIENCE}" |
		jq -r '.value'
})"

require_non_empty() {
	local value="$1"
	local field_name="$2"
	if [[ -z "${value//[[:space:]]/}" ]]; then
		echo "Configured authz grant is missing ${field_name}." >&2
		return 1
	fi
}

preflight_owner_authz_env() {
	require_non_empty "${LAUNCHPLANE_TERMINAL_AGENT_SUBJECT:-}" "LAUNCHPLANE_TERMINAL_AGENT_SUBJECT"
	require_non_empty "${LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL:-}" "LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL"
	require_non_empty "${LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT:-}" "LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT"
	require_non_empty "${LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL:-}" "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL"
	require_non_empty "${LAUNCHPLANE_LOCAL_ADMIN_SUBJECT:-}" "LAUNCHPLANE_LOCAL_ADMIN_SUBJECT"
	require_non_empty "${LAUNCHPLANE_LOCAL_ADMIN_TOKEN_LABEL:-}" "LAUNCHPLANE_LOCAL_ADMIN_TOKEN_LABEL"
}

slugify() {
	printf '%s' "$1" | tr -c '[:alnum:]_.-' '-'
}

post_payload() {
	local route_path="$1"
	local idempotency_key="$2"
	local request_payload="$3"
	local failure_message="$4"
	local response_file status_code

	response_file="$(mktemp)"
	status_code="$(curl -sS \
		-o "$response_file" \
		-w '%{http_code}' \
		-X POST \
		-H "Authorization: Bearer ${oidc_token}" \
		-H 'Content-Type: application/json' \
		-H "Idempotency-Key: ${idempotency_key}" \
		--data "$request_payload" \
		"${LAUNCHPLANE_SERVICE_URL}${route_path}")"
	if [ "$status_code" = "200" ] || [ "$status_code" = "202" ]; then
		cat "$response_file"
		return 0
	fi
	cat "$response_file" >&2
	echo "${failure_message} HTTP ${status_code}." >&2
	return 1
}

post_grant() {
	local repository="$1"
	local workflow_file="$2"
	local product_name="$3"
	local context_name="$4"
	local action_name="$5"
	local source_label="$6"
	local idempotency_suffix="$7"
	local event_name="${8:-workflow_dispatch}"
	local workflow_ref_suffix="${9:-refs/heads/main}"
	local job_workflow_ref="${10:-}"
	local request_payload

	request_payload="$({
		jq -n \
			--arg repository "$repository" \
			--arg workflow_ref "${repository}/.github/workflows/${workflow_file}@${workflow_ref_suffix}" \
			--arg event_name "$event_name" \
			--arg product_name "$product_name" \
			--arg context_name "$context_name" \
			--arg action_name "$action_name" \
			--arg source_label "$source_label" \
			--arg job_workflow_ref "$job_workflow_ref" \
			'{
        schema_version: 1,
        product: "launchplane",
        mode: "apply",
        reason: ("Deploy workflow ensuring authz grant " + $source_label),
        related_issue: "cbusillo/launchplane#83",
        grant: {
          repository: $repository,
          workflow_refs: [$workflow_ref],
          job_workflow_refs: (if $job_workflow_ref == "" then [] else [$job_workflow_ref] end),
          event_names: [$event_name],
          products: [$product_name],
          contexts: [$context_name],
          actions: [$action_name],
          source_label: $source_label
        }
      }'
	})"
	post_payload \
		/v1/authz-policies/github-actions/grants \
		"launchplane-authz-grant:${idempotency_suffix}:${GITHUB_SHA}" \
		"$request_payload" \
		"Launchplane authz grant request failed with"
}

post_terminal_agent_grant() {
	local product_name="$1"
	local context_name="$2"
	local action_name="$3"
	local source_label="$4"
	local idempotency_suffix="$5"
	local terminal_agent_subject="${LAUNCHPLANE_TERMINAL_AGENT_SUBJECT:-}"
	local terminal_agent_token_label="${LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL:-}"
	local request_payload

	require_non_empty "$terminal_agent_subject" "LAUNCHPLANE_TERMINAL_AGENT_SUBJECT"
	require_non_empty "$terminal_agent_token_label" "LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL"

	request_payload="$({
		jq -n \
			--arg product_name "$product_name" \
			--arg context_name "$context_name" \
			--arg action_name "$action_name" \
			--arg source_label "$source_label" \
			--arg terminal_agent_subject "$terminal_agent_subject" \
			--arg terminal_agent_token_label "$terminal_agent_token_label" \
			'{
        schema_version: 1,
        product: "launchplane",
        mode: "apply",
        reason: ("Deploy workflow ensuring terminal-agent authz grant " + $source_label),
        related_issue: "cbusillo/launchplane#426",
        grant: {
          subjects: [$terminal_agent_subject],
          token_labels: [$terminal_agent_token_label],
          products: [$product_name],
          contexts: [$context_name],
          actions: [$action_name],
          source_label: $source_label
        }
      }'
	})"
	post_payload \
		/v1/authz-policies/terminal-agents/grants \
		"launchplane-terminal-agent-authz-grant:${idempotency_suffix}:${GITHUB_SHA}" \
		"$request_payload" \
		"Launchplane terminal-agent authz grant request failed with"
}

post_local_owner_grant() {
	local principal="$1"
	local product_name="$2"
	local context_name="$3"
	local action_name="$4"
	local source_label="$5"
	local idempotency_suffix="$6"
	local subject_env_key token_label_env_key route_path subject token_label
	local request_payload

	if [ "$principal" = "local-admin" ]; then
		subject_env_key="LAUNCHPLANE_LOCAL_ADMIN_SUBJECT"
		token_label_env_key="LAUNCHPLANE_LOCAL_ADMIN_TOKEN_LABEL"
		route_path="/v1/authz-policies/local-admins/grants"
	elif [ "$principal" = "local-operator" ]; then
		subject_env_key="LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT"
		token_label_env_key="LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL"
		route_path="/v1/authz-policies/local-operators/grants"
	else
		echo "Unsupported local owner grant principal ${principal}." >&2
		return 1
	fi
	subject="${!subject_env_key:-}"
	token_label="${!token_label_env_key:-}"
	require_non_empty "$subject" "$subject_env_key"
	require_non_empty "$token_label" "$token_label_env_key"

	request_payload="$({
		jq -n \
			--arg product_name "$product_name" \
			--arg context_name "$context_name" \
			--arg action_name "$action_name" \
			--arg source_label "$source_label" \
			--arg subject "$subject" \
			--arg token_label "$token_label" \
			--arg principal "$principal" \
			'{
        schema_version: 1,
        product: "launchplane",
        mode: "apply",
        reason: ("Deploy workflow ensuring " + $principal + " authz grant " + $source_label),
        related_issue: "cbusillo/launchplane#929",
        grant: {
          subjects: [$subject],
          token_labels: [$token_label],
          products: [$product_name],
          contexts: [$context_name],
          actions: [$action_name],
          source_label: $source_label
        }
      }'
	})"
	post_payload \
		"$route_path" \
		"launchplane-${principal}-authz-grant:${idempotency_suffix}:${GITHUB_SHA}" \
		"$request_payload" \
		"Launchplane ${principal} authz grant request failed with"
}

local_operator_product_config_scopes_json() {
	if [ -n "${LAUNCHPLANE_LOCAL_OPERATOR_PRODUCT_CONFIG_SCOPES_JSON:-}" ]; then
		jq -c \
			'if type != "array" then error("LAUNCHPLANE_LOCAL_OPERATOR_PRODUCT_CONFIG_SCOPES_JSON must be a JSON array") else . end
       | map({product: (.product // ""), context: (.context // "")})
       | map(select((.product | length) > 0 and (.context | length) > 0))
       | unique_by(.product, .context)' \
			<<<"${LAUNCHPLANE_LOCAL_OPERATOR_PRODUCT_CONFIG_SCOPES_JSON}" ||
			return 1
		return 0
	fi

	printf '[]\n'
}

post_local_operator_product_config_grants() {
	local action_name="$1"
	local source_label_prefix="$2"
	local idempotency_prefix="$3"
	local scopes_json scope_count product_name context_name scope_suffix

	scopes_json="$(local_operator_product_config_scopes_json)"
	scope_count="$(jq 'length' <<<"$scopes_json")"
	if [ "$scope_count" = "0" ]; then
		echo "LAUNCHPLANE_LOCAL_OPERATOR_PRODUCT_CONFIG_SCOPES_JSON is unset or empty; skipping local-operator ${action_name} grant reconciliation."
		return 0
	fi

	jq -c '.[]' <<<"$scopes_json" | while IFS= read -r scope_json; do
		product_name="$(jq -r '.product' <<<"$scope_json")"
		context_name="$(jq -r '.context' <<<"$scope_json")"
		scope_suffix="$(slugify "${product_name}-${context_name}")"
		post_local_owner_grant \
			local-operator \
			"$product_name" \
			"$context_name" \
			"$action_name" \
			"${source_label_prefix}-${scope_suffix}" \
			"${idempotency_prefix}-${scope_suffix}"
	done
}

ingress_canary_route_scopes_json() {
	if [ -n "${LAUNCHPLANE_INGRESS_CANARY_ROUTE_SCOPES_JSON:-}" ]; then
		jq -c \
			'if type != "array" then error("LAUNCHPLANE_INGRESS_CANARY_ROUTE_SCOPES_JSON must be a JSON array") else . end
       | map({product: (.product // ""), context: (.context // "")})
       | map(select((.product | length) > 0 and (.context | length) > 0))
       | unique_by(.product, .context)' \
			<<<"${LAUNCHPLANE_INGRESS_CANARY_ROUTE_SCOPES_JSON}" ||
			return 1
		return 0
	fi

	jq -c -n '[{product: "launchplane", context: "launchplane"}]'
}

post_ingress_canary_route_apply_workflow_grants() {
	local scopes_json scope_count product_name context_name scope_suffix

	scopes_json="$(ingress_canary_route_scopes_json)"
	scope_count="$(jq 'length' <<<"$scopes_json")"
	if [ "$scope_count" = "0" ]; then
		echo "LAUNCHPLANE_INGRESS_CANARY_ROUTE_SCOPES_JSON resolved to no scopes; skipping ingress canary workflow grant reconciliation."
		return 0
	fi

	jq -c '.[]' <<<"$scopes_json" | while IFS= read -r scope_json; do
		product_name="$(jq -r '.product' <<<"$scope_json")"
		context_name="$(jq -r '.context' <<<"$scope_json")"
		scope_suffix="$(slugify "${product_name}-${context_name}")"
		post_grant \
			"$GITHUB_REPOSITORY" \
			ingress-route-canary-apply.yml \
			"$product_name" \
			"$context_name" \
			ingress_route.apply \
			"deploy:ingress-route-canary-apply-workflow-${scope_suffix}" \
			"ingress-route-canary-apply-workflow-${scope_suffix}"
	done
}

post_product_config_human_grant() {
	local action_name="$1"
	local source_label="$2"
	local idempotency_suffix="$3"
	local operator_logins="${LAUNCHPLANE_PRODUCT_CONFIG_OPERATOR_LOGINS:-}"
	local operator_products="${LAUNCHPLANE_PRODUCT_CONFIG_OPERATOR_PRODUCTS:-}"
	local operator_contexts="${LAUNCHPLANE_PRODUCT_CONFIG_OPERATOR_CONTEXTS:-}"
	local request_payload

	if [ -z "$operator_logins" ] || [ -z "$operator_products" ] || [ -z "$operator_contexts" ]; then
		echo "Skipping product-config human grant ${source_label}; operator login/product/context variables are not fully configured."
		return 0
	fi

	request_payload="$({
		jq -n \
			--arg logins "$operator_logins" \
			--arg products "$operator_products" \
			--arg contexts "$operator_contexts" \
			--arg action_name "$action_name" \
			--arg source_label "$source_label" \
			'def csv_list($value):
        $value
        | split(",")
        | map(gsub("^\\s+|\\s+$"; ""))
        | map(select(length > 0));
      {
        schema_version: 1,
        product: "launchplane",
        mode: "apply",
        reason: ("Deploy workflow ensuring product-config human authz grant " + $source_label),
        related_issue: "cbusillo/launchplane#521",
        grant: {
          logins: csv_list($logins),
          roles: ["admin"],
          products: csv_list($products),
          contexts: csv_list($contexts),
          actions: [$action_name],
          source_label: $source_label
        }
      }'
	})"
	post_payload \
		/v1/authz-policies/github-humans/grants \
		"launchplane-product-config-human-grant:${idempotency_suffix}:${GITHUB_SHA}" \
		"$request_payload" \
		"Launchplane product-config human authz grant request failed with"
}

configured_github_action_grants_json() {
	if [ -z "${LAUNCHPLANE_AUTHZ_GRANTS_JSON:-}" ]; then
		printf '[]\n'
		return 0
	fi

	jq -c \
		'if type != "array" then error("LAUNCHPLANE_AUTHZ_GRANTS_JSON must be a JSON array") else . end
     | map({
        repository: (.repository // ""),
        workflow_file: (.workflow_file // ""),
        product: (.product // ""),
        context: (.context // ""),
        action: (.action // ""),
        source_label: (.source_label // ""),
        idempotency_suffix: (.idempotency_suffix // ""),
        event_name: (.event_name // "workflow_dispatch"),
        workflow_ref_suffix: (.workflow_ref_suffix // "refs/heads/main"),
        job_workflow_ref: (.job_workflow_ref // "")
      })' \
		<<<"${LAUNCHPLANE_AUTHZ_GRANTS_JSON}" ||
		return 1
}

post_configured_github_action_grants() {
	local grants_json grant_count grant_json repository workflow_file product_name
	local context_name action_name source_label idempotency_suffix event_name
	local workflow_ref_suffix job_workflow_ref

	grants_json="$(configured_github_action_grants_json)"
	grant_count="$(jq 'length' <<<"$grants_json")"
	if [ "$grant_count" = "0" ]; then
		echo "LAUNCHPLANE_AUTHZ_GRANTS_JSON is unset or empty; skipping configured GitHub Actions grant reconciliation."
		return 0
	fi

	jq -c '.[]' <<<"$grants_json" | while IFS= read -r grant_json; do
		repository="$(jq -r '.repository' <<<"$grant_json")"
		workflow_file="$(jq -r '.workflow_file' <<<"$grant_json")"
		product_name="$(jq -r '.product' <<<"$grant_json")"
		context_name="$(jq -r '.context' <<<"$grant_json")"
		action_name="$(jq -r '.action' <<<"$grant_json")"
		source_label="$(jq -r '.source_label' <<<"$grant_json")"
		idempotency_suffix="$(jq -r '.idempotency_suffix' <<<"$grant_json")"
		event_name="$(jq -r '.event_name' <<<"$grant_json")"
		workflow_ref_suffix="$(jq -r '.workflow_ref_suffix' <<<"$grant_json")"
		job_workflow_ref="$(jq -r '.job_workflow_ref' <<<"$grant_json")"

		require_non_empty "$repository" repository
		require_non_empty "$workflow_file" workflow_file
		require_non_empty "$product_name" product
		require_non_empty "$context_name" context
		require_non_empty "$action_name" action
		require_non_empty "$source_label" source_label
		require_non_empty "$idempotency_suffix" idempotency_suffix
		require_non_empty "$event_name" event_name
		require_non_empty "$workflow_ref_suffix" workflow_ref_suffix

		post_grant \
			"$repository" \
			"$workflow_file" \
			"$product_name" \
			"$context_name" \
			"$action_name" \
			"$source_label" \
			"$idempotency_suffix" \
			"$event_name" \
			"$workflow_ref_suffix" \
			"$job_workflow_ref"
	done
}

post_launchplane_service_grant() {
	local workflow_file="$1"
	local action_name="$2"
	local source_label="$3"
	local idempotency_suffix="$4"
	local event_name="${5:-workflow_dispatch}"
	post_grant \
		"$GITHUB_REPOSITORY" \
		"$workflow_file" \
		launchplane \
		launchplane \
		"$action_name" \
		"$source_label" \
		"$idempotency_suffix" \
		"$event_name"
}

preflight_owner_authz_env

post_launchplane_service_grant \
	public-ingress-monitor.yml \
	public_ingress_monitor.run_once \
	deploy:public-ingress-monitor-grant \
	public-ingress-monitor \
	schedule
post_launchplane_service_grant \
	public-ingress-monitor.yml \
	public_ingress_monitor.run_once \
	deploy:public-ingress-monitor-manual-grant \
	public-ingress-monitor-manual \
	workflow_dispatch

post_grant \
	"$GITHUB_REPOSITORY" \
	runner-host-hygiene.yml \
	launchplane \
	launchplane \
	runner_host_hygiene_audit.write \
	deploy:runner-host-hygiene-audit-grant \
	runner-host-hygiene-audit
post_grant \
	"$GITHUB_REPOSITORY" \
	runner-lane-registration.yml \
	launchplane \
	launchplane \
	runner_lane_registration_audit.write \
	deploy:runner-lane-registration-audit-grant \
	runner-lane-registration-audit
post_grant \
	"$GITHUB_REPOSITORY" \
	work-graph-snapshot-validate.yml \
	launchplane \
	launchplane \
	work_graph.rank \
	deploy:work-graph-snapshot-validate-grant \
	work-graph-snapshot-validate
post_product_config_human_grant \
	work_graph.issue_inbox.reconcile \
	deploy:work-graph-issue-inbox-human-reconcile-grant \
	work-graph-issue-inbox-human-reconcile
post_grant \
	"$GITHUB_REPOSITORY" \
	merge-train-runner.yml \
	launchplane \
	launchplane \
	merge_train.policy_targets \
	deploy:merge-train-runner-policy-targets-schedule-grant \
	merge-train-runner-policy-targets-schedule \
	schedule
post_grant \
	"$GITHUB_REPOSITORY" \
	merge-train-runner.yml \
	launchplane \
	launchplane \
	merge_train.run_once \
	deploy:merge-train-runner-manual-grant \
	merge-train-runner-manual
post_grant \
	"$GITHUB_REPOSITORY" \
	merge-train-runner.yml \
	launchplane \
	launchplane \
	merge_train.run_once \
	deploy:merge-train-runner-schedule-grant \
	merge-train-runner-schedule \
	schedule
post_launchplane_service_grant \
	deploy-launchplane.yml \
	authz_policy_grant.write \
	deploy:authz-policy-grant-maintenance-dispatch \
	authz-policy-grant-maintenance-dispatch
post_launchplane_service_grant \
	deploy-launchplane.yml \
	authz_policy_grant.write \
	deploy:authz-policy-grant-maintenance-run \
	authz-policy-grant-maintenance-run \
	workflow_run
post_grant \
	"$GITHUB_REPOSITORY" \
	merge-train-policy-import.yml \
	launchplane \
	launchplane \
	merge_train.policy_import \
	deploy:merge-train-policy-import-grant \
	merge-train-policy-import
post_grant \
	"$GITHUB_REPOSITORY" \
	provider-target-operations.yml \
	launchplane \
	launchplane \
	provider_target.audit \
	deploy:provider-target-operations-audit-grant \
	provider-target-operations-audit
post_grant \
	"$GITHUB_REPOSITORY" \
	provider-target-operations.yml \
	launchplane \
	launchplane \
	provider_target.backfill \
	deploy:provider-target-operations-backfill-grant \
	provider-target-operations-backfill
post_grant \
	"$GITHUB_REPOSITORY" \
	dokploy-target-setup.yml \
	launchplane \
	launchplane \
	dokploy_target.setup \
	deploy:dokploy-target-setup-grant \
	dokploy-target-setup
post_launchplane_service_grant \
	edge-endpoint-apply.yml \
	edge_endpoint.apply \
	deploy:edge-endpoint-apply-workflow-grant \
	edge-endpoint-apply-workflow
post_launchplane_service_grant \
	edge-endpoint-apply.yml \
	edge_endpoint.read \
	deploy:edge-endpoint-read-workflow-grant \
	edge-endpoint-read-workflow
post_ingress_canary_route_apply_workflow_grants
post_terminal_agent_grant \
	launchplane \
	launchplane \
	product_profile.read \
	deploy:terminal-agent-product-profile-read-grant \
	terminal-agent-product-profile-read
post_terminal_agent_grant \
	launchplane \
	launchplane \
	product_environment.read \
	deploy:terminal-agent-agent-context-product-read-grant \
	terminal-agent-agent-context-product-read
post_terminal_agent_grant \
	launchplane \
	launchplane \
	work_graph.rank \
	deploy:terminal-agent-agent-context-work-graph-grant \
	terminal-agent-agent-context-work-graph
post_terminal_agent_grant \
	launchplane \
	launchplane \
	every_code_work_request.read \
	deploy:terminal-agent-agent-context-every-code-grant \
	terminal-agent-agent-context-every-code
post_terminal_agent_grant \
	launchplane \
	launchplane \
	every_code_preview_gate.read \
	deploy:terminal-agent-agent-context-preview-grant \
	terminal-agent-agent-context-preview
post_local_owner_grant \
	local-operator \
	launchplane \
	launchplane \
	merge_train.policy_targets \
	deploy:local-operator-merge-train-policy-targets-grant \
	local-operator-merge-train-policy-targets
post_local_owner_grant \
	local-operator \
	launchplane \
	launchplane \
	work_graph.issue_inbox.reconcile \
	deploy:local-operator-work-graph-issue-inbox-reconcile-grant \
	local-operator-work-graph-issue-inbox-reconcile
post_local_owner_grant \
	local-operator \
	launchplane \
	launchplane \
	public_ingress_monitor.run_once \
	deploy:local-operator-public-ingress-monitor-run-once-grant \
	local-operator-public-ingress-monitor-run-once
post_local_owner_grant \
	local-operator \
	launchplane \
	launchplane \
	public_ingress_notification_policy.apply \
	deploy:local-operator-public-ingress-notification-policy-grant \
	local-operator-public-ingress-notification-policy
post_local_owner_grant \
	local-operator \
	launchplane \
	launchplane \
	edge_endpoint.apply \
	deploy:local-operator-edge-endpoint-apply-grant \
	local-operator-edge-endpoint-apply
post_local_owner_grant \
	local-operator \
	launchplane \
	launchplane \
	edge_endpoint.read \
	deploy:local-operator-edge-endpoint-read-grant \
	local-operator-edge-endpoint-read
post_local_owner_grant \
	local-operator \
	launchplane \
	launchplane \
	ingress_canary_route.apply \
	deploy:local-operator-ingress-canary-route-apply-grant \
	local-operator-ingress-canary-route-apply
post_local_owner_grant \
	local-operator \
	launchplane \
	launchplane \
	ingress_canary_route.read \
	deploy:local-operator-ingress-canary-route-read-grant \
	local-operator-ingress-canary-route-read
post_local_operator_product_config_grants \
	product_config.plan \
	deploy:local-operator-product-config-plan-grant \
	local-operator-product-config-plan
post_local_operator_product_config_grants \
	product_config.apply \
	deploy:local-operator-product-config-apply-grant \
	local-operator-product-config-apply
post_local_owner_grant \
	local-admin \
	launchplane \
	launchplane \
	launchplane_service_deploy.execute \
	deploy:local-admin-self-deploy-grant \
	local-admin-self-deploy
post_product_config_human_grant \
	product_config.plan \
	deploy:product-config-human-plan-grant \
	product-config-human-plan
post_product_config_human_grant \
	product_config.apply \
	deploy:product-config-human-apply-grant \
	product-config-human-apply
post_configured_github_action_grants
