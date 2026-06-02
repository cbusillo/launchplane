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
    "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=${LAUNCHPLANE_SERVICE_AUDIENCE}" \
  | jq -r '.value'
})"

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
  local request_payload response_file status_code

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
  response_file="$(mktemp)"
  status_code="$(curl -sS \
    -o "$response_file" \
    -w '%{http_code}' \
    -X POST \
    -H "Authorization: Bearer ${oidc_token}" \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: launchplane-authz-grant:${idempotency_suffix}:${GITHUB_SHA}" \
    --data "$request_payload" \
    "${LAUNCHPLANE_SERVICE_URL}/v1/authz-policies/github-actions/grants")"
  if [ "$status_code" = "200" ] || [ "$status_code" = "202" ]; then
    cat "$response_file"
    return 0
  fi
  cat "$response_file" >&2
  echo "Launchplane authz grant request failed with HTTP ${status_code}." >&2
  return 1
}

post_terminal_agent_grant() {
  local product_name="$1"
  local context_name="$2"
  local action_name="$3"
  local source_label="$4"
  local idempotency_suffix="$5"
  local request_payload response_file status_code

  request_payload="$({
    jq -n \
      --arg product_name "$product_name" \
      --arg context_name "$context_name" \
      --arg action_name "$action_name" \
      --arg source_label "$source_label" \
      --arg terminal_agent_subject "${LAUNCHPLANE_TERMINAL_AGENT_SUBJECT:-local-owner-agent}" \
      --arg terminal_agent_token_label "${LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL:-local-owner-read}" \
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
  response_file="$(mktemp)"
  status_code="$(curl -sS \
    -o "$response_file" \
    -w '%{http_code}' \
    -X POST \
    -H "Authorization: Bearer ${oidc_token}" \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: launchplane-terminal-agent-authz-grant:${idempotency_suffix}:${GITHUB_SHA}" \
    --data "$request_payload" \
    "${LAUNCHPLANE_SERVICE_URL}/v1/authz-policies/terminal-agents/grants")"
  if [ "$status_code" = "200" ] || [ "$status_code" = "202" ]; then
    cat "$response_file"
    return 0
  fi
  cat "$response_file" >&2
  echo "Launchplane terminal-agent authz grant request failed with HTTP ${status_code}." >&2
  return 1
}

post_local_owner_grant() {
  local principal="$1"
  local product_name="$2"
  local context_name="$3"
  local action_name="$4"
  local source_label="$5"
  local idempotency_suffix="$6"
  local subject_env_key token_label_env_key default_subject default_token_label route_path
  local request_payload response_file status_code

  if [ "$principal" = "local-admin" ]; then
    subject_env_key="LAUNCHPLANE_LOCAL_ADMIN_SUBJECT"
    token_label_env_key="LAUNCHPLANE_LOCAL_ADMIN_TOKEN_LABEL"
    default_subject="local-owner-admin"
    default_token_label="local-owner-admin"
    route_path="/v1/authz-policies/local-admins/grants"
  elif [ "$principal" = "local-operator" ]; then
    subject_env_key="LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT"
    token_label_env_key="LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL"
    default_subject="local-owner-agent"
    default_token_label="local-owner-write"
    route_path="/v1/authz-policies/local-operators/grants"
  else
    echo "Unsupported local owner grant principal ${principal}." >&2
    return 1
  fi

  request_payload="$({
    jq -n \
      --arg product_name "$product_name" \
      --arg context_name "$context_name" \
      --arg action_name "$action_name" \
      --arg source_label "$source_label" \
      --arg subject "${!subject_env_key:-$default_subject}" \
      --arg token_label "${!token_label_env_key:-$default_token_label}" \
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
  response_file="$(mktemp)"
  status_code="$(curl -sS \
    -o "$response_file" \
    -w '%{http_code}' \
    -X POST \
    -H "Authorization: Bearer ${oidc_token}" \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: launchplane-${principal}-authz-grant:${idempotency_suffix}:${GITHUB_SHA}" \
    --data "$request_payload" \
    "${LAUNCHPLANE_SERVICE_URL}${route_path}")"
  if [ "$status_code" = "200" ] || [ "$status_code" = "202" ]; then
    cat "$response_file"
    return 0
  fi
  cat "$response_file" >&2
  echo "Launchplane ${principal} authz grant request failed with HTTP ${status_code}." >&2
  return 1
}

local_operator_product_config_scopes_json() {
  if [ -n "${LAUNCHPLANE_LOCAL_OPERATOR_PRODUCT_CONFIG_SCOPES_JSON:-}" ]; then
    jq -c \
      'if type != "array" then error("LAUNCHPLANE_LOCAL_OPERATOR_PRODUCT_CONFIG_SCOPES_JSON must be a JSON array") else . end
       | map({product: (.product // ""), context: (.context // "")})
       | map(select((.product | length) > 0 and (.context | length) > 0))
       | unique_by(.product, .context)' \
      <<<"${LAUNCHPLANE_LOCAL_OPERATOR_PRODUCT_CONFIG_SCOPES_JSON}" \
      || return 1
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
    scope_suffix="$(printf '%s-%s' "$product_name" "$context_name" | tr -c '[:alnum:]_.-' '-')"
    post_local_owner_grant \
      local-operator \
      "$product_name" \
      "$context_name" \
      "$action_name" \
      "${source_label_prefix}-${scope_suffix}" \
      "${idempotency_prefix}-${scope_suffix}"
  done
}

post_product_config_human_grant() {
  local action_name="$1"
  local source_label="$2"
  local idempotency_suffix="$3"
  local operator_logins="${LAUNCHPLANE_PRODUCT_CONFIG_OPERATOR_LOGINS:-}"
  local operator_products="${LAUNCHPLANE_PRODUCT_CONFIG_OPERATOR_PRODUCTS:-}"
  local operator_contexts="${LAUNCHPLANE_PRODUCT_CONFIG_OPERATOR_CONTEXTS:-}"
  local request_payload response_file status_code

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
  response_file="$(mktemp)"
  status_code="$(curl -sS \
    -o "$response_file" \
    -w '%{http_code}' \
    -X POST \
    -H "Authorization: Bearer ${oidc_token}" \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: launchplane-product-config-human-grant:${idempotency_suffix}:${GITHUB_SHA}" \
    --data "$request_payload" \
    "${LAUNCHPLANE_SERVICE_URL}/v1/authz-policies/github-humans/grants")"
  if [ "$status_code" = "200" ] || [ "$status_code" = "202" ]; then
    cat "$response_file"
    return 0
  fi
  cat "$response_file" >&2
  echo "Launchplane product-config human authz grant request failed with HTTP ${status_code}." >&2
  return 1
}

post_launchplane_grant() {
  local workflow_file="$1"
  local action_name="$2"
  local source_label="$3"
  local idempotency_suffix="$4"
  local event_name="${5:-workflow_dispatch}"
  post_grant \
    "$GITHUB_REPOSITORY" \
    "$workflow_file" \
    sellyouroutboard \
    launchplane \
    "$action_name" \
    "$source_label" \
    "$idempotency_suffix" \
    "$event_name"
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

post_launchplane_live_target_runtime_grants() {
  local product_name="$1"
  local context_name="$2"
  local suffix="$3"
  post_grant \
    "$GITHUB_REPOSITORY" \
    live-target-runtime.yml \
    "$product_name" \
    "$context_name" \
    live_target_runtime.plan \
    "deploy:${suffix}-live-target-runtime-plan-grant" \
    "${suffix}-live-target-runtime-plan"
  post_grant \
    "$GITHUB_REPOSITORY" \
    live-target-runtime.yml \
    "$product_name" \
    "$context_name" \
    live_target_runtime.apply \
    "deploy:${suffix}-live-target-runtime-apply-grant" \
    "${suffix}-live-target-runtime-apply"
}

post_syo_grant() {
  local workflow_file="$1"
  local product_name="$2"
  local context_name="$3"
  local action_name="$4"
  local source_label="$5"
  local idempotency_suffix="$6"
  local event_name="${7:-workflow_dispatch}"
  local workflow_ref_suffix="${8:-refs/heads/main}"
  post_grant \
    cbusillo/sellyouroutboard \
    "$workflow_file" \
    "$product_name" \
    "$context_name" \
    "$action_name" \
    "$source_label" \
    "$idempotency_suffix" \
    "$event_name" \
    "$workflow_ref_suffix"
}

post_syo_product_grant() {
  local action_name="$1"
  local source_label="$2"
  local idempotency_suffix="$3"
  post_syo_grant \
    promote-prod.yml \
    sellyouroutboard \
    sellyouroutboard \
    "$action_name" \
    "$source_label" \
    "$idempotency_suffix"
}

post_syo_preview_grants() {
  local context_name="$1"
  local suffix="$2"
  post_syo_grant \
    preview-control-plane.yml \
    sellyouroutboard \
    "$context_name" \
    preview_refresh.execute \
    "deploy:syo-preview-refresh-${suffix}-grant" \
    "syo-preview-refresh-${suffix}" \
    pull_request \
    '*'
  post_syo_grant \
    preview-control-plane.yml \
    sellyouroutboard \
    "$context_name" \
    preview_pr_feedback.write \
    "deploy:syo-preview-pr-feedback-${suffix}-grant" \
    "syo-preview-pr-feedback-${suffix}" \
    pull_request \
    '*'
  post_syo_grant \
    preview-control-plane.yml \
    sellyouroutboard \
    "$context_name" \
    preview_pr_feedback.write \
    "deploy:syo-preview-unsupported-feedback-${suffix}-grant" \
    "syo-preview-unsupported-feedback-${suffix}" \
    pull_request_target \
    '*'
  post_syo_grant \
    preview-cleanup.yml \
    sellyouroutboard \
    "$context_name" \
    preview_destroy.execute \
    "deploy:syo-preview-destroy-pr-${suffix}-grant" \
    "syo-preview-destroy-pr-${suffix}" \
    pull_request \
    '*'
  post_syo_grant \
    preview-cleanup.yml \
    sellyouroutboard \
    "$context_name" \
    preview_destroy.execute \
    "deploy:syo-preview-destroy-manual-${suffix}-grant" \
    "syo-preview-destroy-manual-${suffix}"
}

post_launchplane_preview_lifecycle_grant() {
  local product_name="$1"
  local context_name="$2"
  local event_name="$3"
  local suffix="$4"
  post_grant \
    "$GITHUB_REPOSITORY" \
    preview-lifecycle.yml \
    "$product_name" \
    "$context_name" \
    preview_lifecycle.plan \
    "deploy:preview-lifecycle-plan-${suffix}-grant" \
    "preview-lifecycle-plan-${suffix}" \
    "$event_name" \
    refs/heads/main
  post_grant \
    "$GITHUB_REPOSITORY" \
    preview-lifecycle.yml \
    "$product_name" \
    "$context_name" \
    preview_lifecycle.cleanup \
    "deploy:preview-lifecycle-cleanup-${suffix}-grant" \
    "preview-lifecycle-cleanup-${suffix}" \
    "$event_name" \
    refs/heads/main
}

post_launchplane_preview_lifecycle_grants() {
  local product_name="$1"
  local context_name="$2"
  local suffix="$3"
  post_launchplane_preview_lifecycle_grant \
    "$product_name" \
    "$context_name" \
    workflow_dispatch \
    "${suffix}-manual"
  post_launchplane_preview_lifecycle_grant \
    "$product_name" \
    "$context_name" \
    schedule \
    "${suffix}-schedule"
}

post_verireel_preview_grant() {
  local workflow_file="$1"
  local action_name="$2"
  local source_label="$3"
  local idempotency_suffix="$4"
  local event_name="${5:-pull_request}"
  post_grant \
    cbusillo/verireel \
    "$workflow_file" \
    verireel \
    verireel-testing \
    "$action_name" \
    "$source_label" \
    "$idempotency_suffix" \
    "$event_name" \
    '*'
}

post_odoo_cm_preview_grant() {
  local product_name="$1"
  local action_name="$2"
  local source_label="$3"
  local idempotency_suffix="$4"
  local event_name="${5:-pull_request}"
  post_grant \
    cbusillo/odoo-tenant-cm \
    odoo-preview.yml \
    "$product_name" \
    cm \
    "$action_name" \
    "$source_label" \
    "$idempotency_suffix" \
    "$event_name" \
    '*'
}

post_odoo_opw_preview_grant() {
  local product_name="$1"
  local action_name="$2"
  local source_label="$3"
  local idempotency_suffix="$4"
  local event_name="${5:-pull_request}"
  post_grant \
    cbusillo/odoo-tenant-opw \
    odoo-preview.yml \
    "$product_name" \
    opw \
    "$action_name" \
    "$source_label" \
    "$idempotency_suffix" \
    "$event_name" \
    '*'
}

post_odoo_stable_grant() {
  local repository="$1"
  local context_name="$2"
  local workflow_file="$3"
  local action_name="$4"
  local source_label="$5"
  local idempotency_suffix="$6"
  local job_workflow_file="${7:-}"
  local product_name="${8:-odoo}"
  local job_workflow_ref=""
  if [ -n "$job_workflow_file" ]; then
    job_workflow_ref="cbusillo/launchplane/.github/workflows/${job_workflow_file}@refs/heads/main"
  fi
  post_grant \
    "$repository" \
    "$workflow_file" \
    "$product_name" \
    "$context_name" \
    "$action_name" \
    "$source_label" \
    "$idempotency_suffix" \
    workflow_dispatch \
    refs/heads/main \
    "$job_workflow_ref"
}

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
  cbusillo/discord-blue \
  main.yml \
  discord-blue \
  discord-blue \
  generic_web_deploy.execute \
  deploy:discord-blue-stable-deploy-grant \
  discord-blue-stable-deploy \
  push \
  refs/heads/main

post_launchplane_grant \
  product-context-cutover-audit.yml \
  product_profile.read \
  deploy:product-context-cutover-audit-grant \
  product-context-cutover-audit
post_launchplane_grant \
  product-context-cutover.yml \
  product_profile.write \
  deploy:product-context-cutover-apply-grant \
  product-context-cutover-apply
post_launchplane_grant \
  product-legacy-context-cleanup.yml \
  product_profile.write \
  deploy:product-legacy-context-cleanup-grant \
  product-legacy-context-cleanup
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
  launchplane-seed-import.yml \
  launchplane \
  launchplane \
  product_onboarding.apply \
  deploy:launchplane-seed-import-product-onboarding-grant \
  launchplane-seed-import-product-onboarding
post_grant \
  "$GITHUB_REPOSITORY" \
  launchplane-seed-import.yml \
  launchplane \
  launchplane \
  runtime_key_safety.write \
  deploy:launchplane-seed-import-runtime-key-safety-grant \
  launchplane-seed-import-runtime-key-safety
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
post_syo_grant \
  promote-prod.yml \
  launchplane \
  sellyouroutboard \
  inventory.read \
  deploy:syo-prod-promotion-inventory-read-grant \
  syo-prod-promotion-inventory-read
post_syo_product_grant \
  generic_web_prod_promotion.execute \
  deploy:syo-prod-promotion-execute-grant \
  syo-prod-promotion-execute
post_terminal_agent_grant \
  sellyouroutboard \
  sellyouroutboard \
  product_environment.read \
  deploy:syo-terminal-agent-product-environment-read-grant \
  syo-terminal-agent-product-environment-read
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
  reon-prod \
  ingress_route.plan \
  deploy:local-operator-ingress-route-plan-grant \
  local-operator-ingress-route-plan
post_local_owner_grant \
  local-operator \
  launchplane \
  reon-prod \
  ingress_route.apply \
  deploy:local-operator-ingress-route-apply-grant \
  local-operator-ingress-route-apply
# Keep the manual OIDC dry-run workflow canary-scoped until broader route
# ownership and operator review flows are explicit.
post_grant \
  "$GITHUB_REPOSITORY" \
  ingress-route-dry-run.yml \
  launchplane \
  reon-prod \
  ingress_route.plan \
  deploy:ingress-route-dry-run-plan-grant \
  ingress-route-dry-run-plan
post_grant \
  "$GITHUB_REPOSITORY" \
  ingress-route-audit-read.yml \
  launchplane \
  reon-prod \
  ingress_route.plan \
  deploy:ingress-route-audit-read-plan-grant \
  ingress-route-audit-read-plan
post_grant \
  "$GITHUB_REPOSITORY" \
  ingress-route-canary-apply.yml \
  launchplane \
  reon-prod \
  ingress_route.apply \
  deploy:ingress-route-canary-apply-grant \
  ingress-route-canary-apply
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
post_launchplane_live_target_runtime_grants \
  sellyouroutboard \
  sellyouroutboard \
  syo
post_launchplane_live_target_runtime_grants \
  discord-blue \
  discord-blue \
  discord-blue
post_launchplane_live_target_runtime_grants \
  verireel \
  verireel \
  verireel
post_launchplane_live_target_runtime_grants \
  odoo-tenant-cm \
  cm \
  odoo-cm
post_launchplane_live_target_runtime_grants \
  odoo-tenant-opw \
  opw \
  odoo-opw
post_grant \
  "$GITHUB_REPOSITORY" \
  odoo-target-replacement-plan.yml \
  odoo-tenant-cm \
  cm \
  odoo_target_replacement_plan.read \
  deploy:odoo-cm-target-replacement-plan-grant \
  odoo-cm-target-replacement-plan
post_grant \
  "$GITHUB_REPOSITORY" \
  odoo-target-replacement-plan.yml \
  odoo-tenant-opw \
  opw \
  odoo_target_replacement_plan.read \
  deploy:odoo-opw-target-replacement-plan-grant \
  odoo-opw-target-replacement-plan
post_grant \
  "$GITHUB_REPOSITORY" \
  odoo-target-replacement-apply.yml \
  odoo-tenant-cm \
  cm \
  odoo_target_replacement_apply.execute \
  deploy:odoo-cm-target-replacement-apply-grant \
  odoo-cm-target-replacement-apply
post_grant \
  "$GITHUB_REPOSITORY" \
  odoo-target-replacement-apply.yml \
  odoo-tenant-opw \
  opw \
  odoo_target_replacement_apply.execute \
  deploy:odoo-opw-target-replacement-apply-grant \
  odoo-opw-target-replacement-apply
post_grant \
  "$GITHUB_REPOSITORY" \
  odoo-config-parameter-override.yml \
  odoo-tenant-cm \
  cm \
  odoo_config_parameter_override.write \
  deploy:odoo-cm-config-parameter-override-grant \
  odoo-cm-config-parameter-override
post_grant \
  "$GITHUB_REPOSITORY" \
  odoo-website-bootstrap-override.yml \
  odoo-tenant-cm \
  cm \
  odoo_website_bootstrap_override.write \
  deploy:odoo-cm-website-bootstrap-override-grant \
  odoo-cm-website-bootstrap-override
post_grant \
  "$GITHUB_REPOSITORY" \
  odoo-website-bootstrap-override.yml \
  odoo-tenant-opw \
  opw \
  odoo_website_bootstrap_override.write \
  deploy:odoo-opw-website-bootstrap-override-grant \
  odoo-opw-website-bootstrap-override
post_grant \
  "$GITHUB_REPOSITORY" \
  odoo-stable-bootstrap.yml \
  odoo-tenant-cm \
  cm \
  odoo_stable_bootstrap.execute \
  deploy:odoo-cm-stable-bootstrap-grant \
  odoo-cm-stable-bootstrap
post_syo_preview_grants \
  sellyouroutboard-testing \
  legacy-context
post_syo_preview_grants \
  sellyouroutboard \
  canonical-context
post_launchplane_preview_lifecycle_grants \
  sellyouroutboard \
  sellyouroutboard-testing \
  syo-legacy-context
post_launchplane_preview_lifecycle_grants \
  sellyouroutboard \
  sellyouroutboard \
  syo-canonical-context
post_launchplane_preview_lifecycle_grants \
  verireel \
  verireel-testing \
  verireel-testing
post_launchplane_preview_lifecycle_grants \
  odoo-tenant-cm \
  cm \
  odoo-cm
post_launchplane_preview_lifecycle_grants \
  odoo-tenant-opw \
  opw \
  odoo-opw
post_verireel_preview_grant \
  preview-control-plane.yml \
  preview_pr_feedback.write \
  deploy:verireel-preview-pr-feedback-grant \
  verireel-preview-pr-feedback
post_verireel_preview_grant \
  preview-fork-notice.yml \
  preview_pr_feedback.write \
  deploy:verireel-preview-unsupported-feedback-grant \
  verireel-preview-unsupported-feedback \
  pull_request_target
post_verireel_preview_grant \
  preview-cleanup.yml \
  preview_pr_feedback.write \
  deploy:verireel-preview-cleanup-feedback-grant \
  verireel-preview-cleanup-feedback
post_odoo_cm_preview_grant \
  odoo-tenant-cm \
  odoo_artifact_publish_inputs.read \
  deploy:odoo-cm-preview-artifact-publish-inputs-grant \
  odoo-cm-preview-artifact-publish-inputs
post_odoo_cm_preview_grant \
  odoo-tenant-cm \
  odoo_artifact_publish_inputs.read \
  deploy:odoo-cm-preview-artifact-publish-inputs-manual-grant \
  odoo-cm-preview-artifact-publish-inputs-manual \
  workflow_dispatch
post_odoo_cm_preview_grant \
  odoo \
  odoo_artifact_publish.write \
  deploy:odoo-cm-preview-artifact-publish-grant \
  odoo-cm-preview-artifact-publish
post_odoo_cm_preview_grant \
  odoo-tenant-cm \
  preview_refresh.execute \
  deploy:odoo-cm-preview-refresh-grant \
  odoo-cm-preview-refresh
post_odoo_cm_preview_grant \
  odoo-tenant-cm \
  preview_pr_feedback.write \
  deploy:odoo-cm-preview-pr-feedback-grant \
  odoo-cm-preview-pr-feedback
post_odoo_cm_preview_grant \
  odoo-tenant-cm \
  preview_pr_feedback.write \
  deploy:odoo-cm-preview-unsupported-feedback-grant \
  odoo-cm-preview-unsupported-feedback \
  pull_request_target
post_odoo_cm_preview_grant \
  odoo-tenant-cm \
  preview_destroy.execute \
  deploy:odoo-cm-preview-destroy-pr-grant \
  odoo-cm-preview-destroy-pr
post_odoo_cm_preview_grant \
  odoo-tenant-cm \
  preview_destroy.execute \
  deploy:odoo-cm-preview-destroy-manual-grant \
  odoo-cm-preview-destroy-manual \
  workflow_dispatch
post_odoo_cm_preview_grant \
  odoo-tenant-cm \
  odoo_preview_apply_inputs.read \
  deploy:odoo-cm-preview-apply-inputs-grant \
  odoo-cm-preview-apply-inputs
post_odoo_cm_preview_grant \
  odoo-tenant-cm \
  odoo_preview_apply_inputs.read \
  deploy:odoo-cm-preview-apply-inputs-manual-grant \
  odoo-cm-preview-apply-inputs-manual \
  workflow_dispatch
post_odoo_cm_preview_grant \
  odoo-tenant-cm \
  odoo_preview_apply.execute \
  deploy:odoo-cm-preview-apply-grant \
  odoo-cm-preview-apply
post_odoo_cm_preview_grant \
  odoo-tenant-cm \
  odoo_preview_apply.execute \
  deploy:odoo-cm-preview-apply-manual-grant \
  odoo-cm-preview-apply-manual \
  workflow_dispatch
post_odoo_stable_grant \
  cbusillo/odoo-tenant-cm \
  cm \
  odoo-artifact-publish.yml \
  odoo_artifact_publish_inputs.read \
  deploy:odoo-cm-artifact-publish-inputs-grant \
  odoo-cm-artifact-publish-inputs \
  reusable-odoo-artifact-publish.yml \
  odoo-tenant-cm
post_odoo_stable_grant \
  cbusillo/odoo-tenant-cm \
  cm \
  odoo-artifact-publish.yml \
  odoo_artifact_publish.write \
  deploy:odoo-cm-artifact-publish-grant \
  odoo-cm-artifact-publish \
  reusable-odoo-artifact-publish.yml \
  odoo-tenant-cm
post_odoo_stable_grant \
  cbusillo/odoo-tenant-cm \
  cm \
  odoo-testing-deploy.yml \
  odoo_target_replacement_apply.execute \
  deploy:odoo-cm-testing-target-replacement-grant \
  odoo-cm-testing-target-replacement \
  reusable-odoo-testing-deploy.yml \
  odoo-tenant-cm
post_odoo_stable_grant \
  cbusillo/odoo-tenant-cm \
  cm \
  odoo-post-deploy.yml \
  odoo_post_deploy.execute \
  deploy:odoo-cm-post-deploy-grant \
  odoo-cm-post-deploy \
  reusable-odoo-post-deploy.yml
post_odoo_stable_grant \
  cbusillo/odoo-tenant-cm \
  cm \
  odoo-prod-promotion.yml \
  odoo_prod_promotion_run.execute \
  deploy:odoo-cm-prod-promotion-run-grant \
  odoo-cm-prod-promotion-run \
  reusable-odoo-prod-promotion.yml
post_odoo_stable_grant \
  cbusillo/odoo-tenant-cm \
  cm \
  odoo-prod-rollback.yml \
  odoo_prod_rollback.execute \
  deploy:odoo-cm-prod-rollback-grant \
  odoo-cm-prod-rollback \
  reusable-odoo-prod-rollback.yml
post_odoo_opw_preview_grant \
  odoo-tenant-opw \
  odoo_artifact_publish_inputs.read \
  deploy:odoo-opw-preview-artifact-publish-inputs-manual-grant \
  odoo-opw-preview-artifact-publish-inputs-manual \
  workflow_dispatch
post_odoo_opw_preview_grant \
  odoo \
  odoo_artifact_publish.write \
  deploy:odoo-opw-preview-artifact-publish-grant \
  odoo-opw-preview-artifact-publish \
  workflow_dispatch
post_odoo_opw_preview_grant \
  odoo-tenant-opw \
  preview_refresh.execute \
  deploy:odoo-opw-preview-refresh-grant \
  odoo-opw-preview-refresh
post_odoo_opw_preview_grant \
  odoo-tenant-opw \
  preview_pr_feedback.write \
  deploy:odoo-opw-preview-pr-feedback-grant \
  odoo-opw-preview-pr-feedback
post_odoo_opw_preview_grant \
  odoo-tenant-opw \
  preview_pr_feedback.write \
  deploy:odoo-opw-preview-unsupported-feedback-grant \
  odoo-opw-preview-unsupported-feedback \
  pull_request_target
post_odoo_opw_preview_grant \
  odoo-tenant-opw \
  preview_destroy.execute \
  deploy:odoo-opw-preview-destroy-pr-grant \
  odoo-opw-preview-destroy-pr
post_odoo_opw_preview_grant \
  odoo-tenant-opw \
  preview_destroy.execute \
  deploy:odoo-opw-preview-destroy-manual-grant \
  odoo-opw-preview-destroy-manual \
  workflow_dispatch
post_odoo_opw_preview_grant \
  odoo-tenant-opw \
  odoo_preview_apply_inputs.read \
  deploy:odoo-opw-preview-apply-inputs-grant \
  odoo-opw-preview-apply-inputs
post_odoo_opw_preview_grant \
  odoo-tenant-opw \
  odoo_preview_apply_inputs.read \
  deploy:odoo-opw-preview-apply-inputs-manual-grant \
  odoo-opw-preview-apply-inputs-manual \
  workflow_dispatch
post_odoo_opw_preview_grant \
  odoo-tenant-opw \
  odoo_preview_apply.execute \
  deploy:odoo-opw-preview-apply-grant \
  odoo-opw-preview-apply
post_odoo_opw_preview_grant \
  odoo-tenant-opw \
  odoo_preview_apply.execute \
  deploy:odoo-opw-preview-apply-manual-grant \
  odoo-opw-preview-apply-manual \
  workflow_dispatch
post_odoo_stable_grant \
  cbusillo/odoo-tenant-opw \
  opw \
  odoo-artifact-publish.yml \
  odoo_artifact_publish_inputs.read \
  deploy:odoo-opw-artifact-publish-inputs-grant \
  odoo-opw-artifact-publish-inputs \
  reusable-odoo-artifact-publish.yml \
  odoo-tenant-opw
post_odoo_stable_grant \
  cbusillo/odoo-tenant-opw \
  opw \
  odoo-artifact-publish.yml \
  odoo_artifact_publish.write \
  deploy:odoo-opw-artifact-publish-grant \
  odoo-opw-artifact-publish \
  reusable-odoo-artifact-publish.yml \
  odoo-tenant-opw
post_odoo_stable_grant \
  cbusillo/odoo-tenant-opw \
  opw \
  odoo-testing-deploy.yml \
  odoo_target_replacement_apply.execute \
  deploy:odoo-opw-testing-target-replacement-grant \
  odoo-opw-testing-target-replacement \
  reusable-odoo-testing-deploy.yml \
  odoo-tenant-opw
post_odoo_stable_grant \
  cbusillo/odoo-tenant-opw \
  opw \
  odoo-post-deploy.yml \
  odoo_post_deploy.execute \
  deploy:odoo-opw-post-deploy-grant \
  odoo-opw-post-deploy \
  reusable-odoo-post-deploy.yml
post_odoo_stable_grant \
  cbusillo/odoo-tenant-opw \
  opw \
  odoo-prod-promotion.yml \
  odoo_prod_promotion_run.execute \
  deploy:odoo-opw-prod-promotion-run-grant \
  odoo-opw-prod-promotion-run \
  reusable-odoo-prod-promotion.yml
post_odoo_stable_grant \
  cbusillo/odoo-tenant-opw \
  opw \
  odoo-prod-rollback.yml \
  odoo_prod_rollback.execute \
  deploy:odoo-opw-prod-rollback-grant \
  odoo-opw-prod-rollback \
  reusable-odoo-prod-rollback.yml
