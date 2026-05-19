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
      '{
        schema_version: 1,
        product: "launchplane",
        mode: "apply",
        reason: ("Deploy workflow ensuring authz grant " + $source_label),
        related_issue: "cbusillo/launchplane#83",
        grant: {
          repository: $repository,
          workflow_refs: [$workflow_ref],
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

apply_product_onboarding() {
  local idempotency_suffix="$1"
  local request_payload response_file status_code

  if [ -z "${DISCORD_BLUE_DOKPLOY_TARGET_ID:-}" ]; then
    echo "DISCORD_BLUE_DOKPLOY_TARGET_ID is required before Discord Blue onboarding." >&2
    return 1
  fi
  idempotency_suffix="${idempotency_suffix}:${DISCORD_BLUE_DOKPLOY_TARGET_ID}"

  request_payload="$({
    jq -n \
      --arg target_id "${DISCORD_BLUE_DOKPLOY_TARGET_ID:-}" \
      '{
        schema_version: 1,
        product: "launchplane",
        manifest: {
          product: "discord-blue",
          display_name: "Discord Blue",
          repository: "cbusillo/discord-blue",
          driver_id: "generic-web",
          image_repository: "ghcr.io/cbusillo/discord-blue",
          runtime_port: 8787,
          health_path: "/health",
          lanes: [
            {
              instance: "prod",
              context: "discord-blue"
            }
          ],
          dokploy_targets: [
            {
              context: "discord-blue",
              instance: "prod",
              target_id: $target_id,
              target_type: "application",
              target_name: "discord-blue",
              healthcheck_enabled: false,
              deploy_timeout_seconds: 600
            }
          ],
          runtime_environments: [
            {
              scope: "instance",
              context: "discord-blue",
              instance: "prod",
              env: {
                DISCORD_BLUE_STATE_DIR: "/var/lib/discord-blue"
              }
            }
          ],
          secret_bindings: [
            {
              binding_key: "DISCORD_TOKEN",
              context: "discord-blue",
              instance: "prod"
            }
          ],
          source_label: "deploy:discord-blue-onboarding"
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
    -H "Idempotency-Key: launchplane-product-onboarding:${idempotency_suffix}:${GITHUB_SHA}" \
    --data "$request_payload" \
    "${LAUNCHPLANE_SERVICE_URL}/v1/product-onboarding/apply")"
  if [ "$status_code" = "200" ] || [ "$status_code" = "202" ]; then
    cat "$response_file"
    return 0
  fi
  cat "$response_file" >&2
  echo "Launchplane product onboarding request failed with HTTP ${status_code}." >&2
  return 1
}

apply_verireel_onboarding() {
  local idempotency_suffix="verireel-preview-profile"
  local request_payload response_file status_code

  request_payload="$({
    jq -n \
      '{
        schema_version: 1,
        product: "launchplane",
        manifest: {
          product: "verireel",
          display_name: "VeriReel",
          repository: "cbusillo/verireel",
          driver_id: "verireel",
          image_repository: "ghcr.io/cbusillo/verireel-app",
          runtime_port: 3000,
          health_path: "/api/health",
          lanes: [
            {
              instance: "testing",
              context: "verireel",
              base_url: "https://ver-testing.shinycomputers.com"
            },
            {
              instance: "prod",
              context: "verireel",
              base_url: "https://ver-prod.shinycomputers.com"
            }
          ],
          preview: {
            enabled: true,
            context: "verireel-testing",
            enable_label: "preview",
            slug_template: "pr-{number}",
            app_name_prefix: "ver-preview",
            template_instance: "testing",
            preview_url_env_keys: ["VERIREEL_APP_URL", "BETTER_AUTH_URL"],
            preview_domain_env_keys: ["LAUNCHPLANE_PREVIEW_BASE_URL"],
            data_transport_mode: "driver"
          },
          runtime_environments: [
            {
              scope: "context",
              context: "verireel-testing",
              env: {
                LAUNCHPLANE_PREVIEW_BASE_URL: "https://ver-preview.shinycomputers.com"
              }
            }
          ],
          expected_config: {
            runtime_environment_keys: [
              {
                key: "LAUNCHPLANE_PREVIEW_BASE_URL",
                context: "verireel-testing"
              }
            ],
            managed_secret_bindings: [
              {
                binding_key: "BETTER_AUTH_SECRET",
                context: "verireel",
                instance: "testing"
              },
              {
                binding_key: "VERIREEL_SECRETS_MASTER_KEY",
                context: "verireel",
                instance: "testing"
              },
              {
                binding_key: "VERIREEL_CRON_SECRET",
                context: "verireel",
                instance: "testing"
              },
              {
                binding_key: "POSTGRES_PASSWORD",
                context: "verireel",
                instance: "testing"
              },
              {
                binding_key: "BETTER_AUTH_SECRET",
                context: "verireel",
                instance: "prod"
              },
              {
                binding_key: "VERIREEL_SECRETS_MASTER_KEY",
                context: "verireel",
                instance: "prod"
              },
              {
                binding_key: "VERIREEL_CRON_SECRET",
                context: "verireel",
                instance: "prod"
              },
              {
                binding_key: "POSTGRES_PASSWORD",
                context: "verireel",
                instance: "prod"
              }
            ]
          },
          source_label: "deploy:verireel-product-onboarding"
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
    -H "Idempotency-Key: launchplane-product-onboarding:${idempotency_suffix}:${GITHUB_SHA}" \
    --data "$request_payload" \
    "${LAUNCHPLANE_SERVICE_URL}/v1/product-onboarding/apply")"
  if [ "$status_code" = "200" ] || [ "$status_code" = "202" ]; then
    cat "$response_file"
    return 0
  fi
  cat "$response_file" >&2
  echo "Launchplane VeriReel product onboarding request failed with HTTP ${status_code}." >&2
  return 1
}

apply_odoo_cm_onboarding() {
  local idempotency_suffix="odoo-cm-preview-profile"
  local request_payload response_file status_code
  local target_id="${ODOO_CM_TESTING_DOKPLOY_TARGET_ID:-}"
  local prod_target_id="${ODOO_CM_PROD_DOKPLOY_TARGET_ID:-}"

  if [ -z "$target_id" ]; then
    echo "ODOO_CM_TESTING_DOKPLOY_TARGET_ID is required before Odoo CM onboarding." >&2
    return 1
  fi
  if [ -z "$prod_target_id" ]; then
    echo "ODOO_CM_PROD_DOKPLOY_TARGET_ID is required before Odoo CM onboarding." >&2
    return 1
  fi

  request_payload="$({
    jq -n \
      --arg target_id "$target_id" \
      --arg prod_target_id "$prod_target_id" \
      '{
        schema_version: 1,
        product: "launchplane",
        manifest: {
          product: "odoo-tenant-cm",
          display_name: "Odoo CM",
          repository: "cbusillo/odoo-tenant-cm",
          driver_id: "odoo",
          image_repository: "ghcr.io/cbusillo/odoo-tenant-cm",
          runtime_port: 8069,
          health_path: "/web/health",
          lanes: [
            {
              instance: "testing",
              context: "cm",
              odoo_stable_bootstrap: {
                enabled: true,
                approval_issue_url: "https://github.com/cbusillo/launchplane/issues/573",
                data_source_mode: "empty",
                confirmation: "bootstrap cm testing",
                expected_target_name: "cm-testing",
                expected_domains: ["cm-testing.shinycomputers.com"],
                require_health_verification: true,
                require_canonical_verification: true,
                require_logo_verification: true
              },
              odoo_data_policy: {
                data_authority: "resettable",
                allowed_rebuild_sources: ["empty"],
                requires_backup_before_destroy: false,
                requires_restore_proof: false,
                requires_runtime_identity: true
              }
            },
            {
              instance: "prod",
              context: "cm",
              odoo_stable_bootstrap: {
                enabled: true,
                approval_issue_url: "https://github.com/cbusillo/launchplane/issues/573",
                data_source_mode: "empty",
                confirmation: "bootstrap cm prod",
                expected_target_name: "cm-prod",
                expected_domains: ["cellmechanic.com"],
                require_health_verification: true,
                require_canonical_verification: true,
                require_logo_verification: true
              },
              odoo_data_policy: {
                data_authority: "resettable",
                allowed_rebuild_sources: ["empty"],
                requires_backup_before_destroy: false,
                requires_restore_proof: false,
                requires_runtime_identity: true
              }
            }
          ],
          preview: {
            enabled: true,
            context: "cm",
            enable_label: "preview",
            slug_template: "pr-{number}",
            app_name_prefix: "cm-odoo-preview",
            template_instance: "testing",
            override_env: {
              ODOO_INSTALL_MODULES: "cm_custom,cm_website"
            },
            preview_url_env_keys: ["WEB_BASE_URL"],
            data_transport_mode: "bootstrap"
          },
          dokploy_targets: (
            (if ($target_id | length) > 0 then
              [
                {
                  context: "cm",
                  instance: "testing",
                  target_id: $target_id,
                  target_type: "compose",
                  target_name: "cm-testing",
                  domains: ["cm-testing.shinycomputers.com"],
                  healthcheck_path: "/web/health",
                  healthcheck_enabled: true,
                  deploy_timeout_seconds: 900
                }
              ]
            else [] end) +
            (if ($prod_target_id | length) > 0 then
              [
                {
                  context: "cm",
                  instance: "prod",
                  target_id: $prod_target_id,
                  target_type: "compose",
                  target_name: "cm-prod",
                  domains: ["cm-prod.shinycomputers.com"],
                  healthcheck_path: "/web/health",
                  healthcheck_enabled: true,
                  deploy_timeout_seconds: 900
                }
              ]
            else [] end)
          ),
          source_label: "deploy:odoo-cm-product-onboarding"
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
    -H "Idempotency-Key: launchplane-product-onboarding:${idempotency_suffix}:${target_id}:${prod_target_id}:${GITHUB_SHA}" \
    --data "$request_payload" \
    "${LAUNCHPLANE_SERVICE_URL}/v1/product-onboarding/apply")"
  if [ "$status_code" = "200" ] || [ "$status_code" = "202" ]; then
    cat "$response_file"
    return 0
  fi
  cat "$response_file" >&2
  echo "Launchplane Odoo CM product onboarding request failed with HTTP ${status_code}." >&2
  return 1
}

apply_odoo_opw_onboarding() {
  local idempotency_suffix="odoo-opw-prelaunch-profile"
  local request_payload response_file status_code
  local testing_target_id="${ODOO_OPW_TESTING_DOKPLOY_TARGET_ID:-}"
  local prod_target_id="${ODOO_OPW_PROD_DOKPLOY_TARGET_ID:-}"

  request_payload="$({
    jq -n \
      --arg testing_target_id "$testing_target_id" \
      --arg prod_target_id "$prod_target_id" \
      '{
        schema_version: 1,
        product: "launchplane",
        manifest: {
          product: "odoo-tenant-opw",
          display_name: "Odoo OPW",
          repository: "cbusillo/odoo-tenant-opw",
          driver_id: "odoo",
          image_repository: "ghcr.io/cbusillo/odoo-tenant-opw",
          runtime_port: 8069,
          health_path: "/web/health",
          lanes: [
            {
              instance: "testing",
              context: "opw",
              odoo_prelaunch_rebuild: {
                enabled: true,
                approval_issue_url: "https://github.com/cbusillo/launchplane/issues/573",
                data_source_mode: "upstream_restore",
                confirmation: "restore opw upstream",
                expected_target_name: "opw-testing",
                expected_domains: ["opw-testing.shinycomputers.com"]
              },
              odoo_data_policy: {
                data_authority: "restorable",
                allowed_rebuild_sources: ["upstream_restore"],
                upstream_source: "odoo-tenant-opw/opw/testing-upstream",
                requires_backup_before_destroy: true,
                requires_restore_proof: true,
                requires_runtime_identity: true
              }
            },
            {
              instance: "prod",
              context: "opw",
              odoo_prelaunch_rebuild: {
                enabled: true,
                approval_issue_url: "https://github.com/cbusillo/launchplane/issues/573",
                data_source_mode: "upstream_restore",
                confirmation: "restore opw upstream",
                expected_target_name: "opw-prod",
                expected_domains: ["opw-prod.shinycomputers.com"]
              },
              odoo_data_policy: {
                data_authority: "restorable",
                allowed_rebuild_sources: ["upstream_restore"],
                upstream_source: "odoo-tenant-opw/opw/prod-upstream",
                requires_backup_before_destroy: true,
                requires_restore_proof: true,
                requires_runtime_identity: true
              }
            }
          ],
          preview: {
            enabled: true,
            context: "opw",
            enable_label: "preview",
            slug_template: "pr-{number}",
            app_name_prefix: "odoo-tenant-opw",
            template_instance: "testing",
            override_env: {
              ODOO_INSTALL_MODULES: "opw_custom"
            },
            preview_url_env_keys: ["WEB_BASE_URL"],
            data_transport_mode: "bootstrap"
          },
          dokploy_targets: (
            (if ($testing_target_id | length) > 0 then
              [
                {
                  context: "opw",
                  instance: "testing",
                  target_id: $testing_target_id,
                  target_type: "compose",
                  target_name: "opw-testing",
                  healthcheck_path: "/web/health",
                  healthcheck_enabled: true,
                  deploy_timeout_seconds: 900
                }
              ]
            else [] end) +
            (if ($prod_target_id | length) > 0 then
              [
                {
                  context: "opw",
                  instance: "prod",
                  target_id: $prod_target_id,
                  target_type: "compose",
                  target_name: "opw-prod",
                  healthcheck_path: "/web/health",
                  healthcheck_enabled: true,
                  deploy_timeout_seconds: 900
                }
              ]
            else [] end)
          ),
          source_label: "deploy:odoo-opw-product-onboarding"
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
    -H "Idempotency-Key: launchplane-product-onboarding:${idempotency_suffix}:${testing_target_id}:${prod_target_id}:${GITHUB_SHA}" \
    --data "$request_payload" \
    "${LAUNCHPLANE_SERVICE_URL}/v1/product-onboarding/apply")"
  if [ "$status_code" = "200" ] || [ "$status_code" = "202" ]; then
    cat "$response_file"
    return 0
  fi
  cat "$response_file" >&2
  echo "Launchplane Odoo OPW product onboarding request failed with HTTP ${status_code}." >&2
  return 1
}

apply_runtime_key_safety_policy() {
  local idempotency_suffix="$1"
  local request_payload response_file status_code

  request_payload="$({
    jq -n \
      '{
        schema_version: 1,
        product: "launchplane",
        source_label: "deploy:runtime-key-safety-policy",
        rules: [
          {
            binding_key: "DISCORD_TOKEN",
            secret_class: "prod_only",
            allowed_contexts: ["discord-blue"],
            allowed_instances: ["prod"]
          },
          {
            binding_key: "RESEND_API_KEY",
            secret_class: "shared_safe",
            allowed_contexts: ["sellyouroutboard"],
            allowed_instances: ["testing", "prod"]
          },
          {
            binding_key: "SMTP_PASSWORD",
            secret_class: "prod_only",
            allowed_contexts: ["sellyouroutboard"],
            allowed_instances: ["prod"]
          },
          {
            binding_key: "META_CONVERSIONS_API_TOKEN",
            secret_class: "shared_safe",
            allowed_contexts: ["sellyouroutboard"],
            allowed_instances: ["testing", "prod"]
          }
        ]
      }'
  })"
  response_file="$(mktemp)"
  status_code="$(curl -sS \
    -o "$response_file" \
    -w '%{http_code}' \
    -X POST \
    -H "Authorization: Bearer ${oidc_token}" \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: launchplane-runtime-key-safety-policy:${idempotency_suffix}:${GITHUB_SHA}" \
    --data "$request_payload" \
    "${LAUNCHPLANE_SERVICE_URL}/v1/runtime-key-safety/policies/apply")"
  if [ "$status_code" = "200" ] || [ "$status_code" = "202" ]; then
    cat "$response_file"
    return 0
  fi
  cat "$response_file" >&2
  echo "Launchplane runtime key-safety policy request failed with HTTP ${status_code}." >&2
  return 1
}

post_launchplane_grant() {
  local workflow_file="$1"
  local action_name="$2"
  local source_label="$3"
  local idempotency_suffix="$4"
  post_grant \
    "$GITHUB_REPOSITORY" \
    "$workflow_file" \
    sellyouroutboard \
    launchplane \
    "$action_name" \
    "$source_label" \
    "$idempotency_suffix"
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

apply_product_onboarding \
  discord-blue
apply_verireel_onboarding
apply_odoo_cm_onboarding
apply_odoo_opw_onboarding
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
  work-graph-snapshot-validate.yml \
  launchplane \
  launchplane \
  work_graph.rank \
  deploy:work-graph-snapshot-validate-grant \
  work-graph-snapshot-validate
post_grant \
  "$GITHUB_REPOSITORY" \
  merge-train-runner.yml \
  launchplane \
  launchplane \
  merge_train.run_once \
  deploy:merge-train-runner-grant \
  merge-train-runner
post_grant \
  "$GITHUB_REPOSITORY" \
  merge-train-policy-import.yml \
  launchplane \
  launchplane \
  launchplane_service_deploy.execute \
  deploy:merge-train-policy-import-grant \
  merge-train-policy-import
post_grant \
  "$GITHUB_REPOSITORY" \
  deploy-launchplane.yml \
  launchplane \
  launchplane \
  runtime_key_safety.write \
  deploy:runtime-key-safety-policy-grant \
  runtime-key-safety-policy
apply_runtime_key_safety_policy \
  launchplane-runtime-secret-classes
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
post_product_config_human_grant \
  product_config.plan \
  deploy:product-config-human-plan-grant \
  product-config-human-plan
post_product_config_human_grant \
  product_config.apply \
  deploy:product-config-human-apply-grant \
  product-config-human-apply
post_grant \
  "$GITHUB_REPOSITORY" \
  live-target-runtime.yml \
  sellyouroutboard \
  sellyouroutboard \
  live_target_runtime.plan \
  deploy:syo-live-target-runtime-plan-grant \
  syo-live-target-runtime-plan
post_grant \
  "$GITHUB_REPOSITORY" \
  live-target-runtime.yml \
  sellyouroutboard \
  sellyouroutboard \
  live_target_runtime.apply \
  deploy:syo-live-target-runtime-apply-grant \
  syo-live-target-runtime-apply
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
  odoo \
  odoo_artifact_publish_inputs.read \
  deploy:odoo-cm-preview-artifact-publish-inputs-grant \
  odoo-cm-preview-artifact-publish-inputs
post_odoo_cm_preview_grant \
  odoo \
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
  odoo_preview_apply.execute \
  deploy:odoo-cm-preview-apply-manual-grant \
  odoo-cm-preview-apply-manual \
  workflow_dispatch
post_odoo_opw_preview_grant \
  odoo \
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
  odoo_preview_apply.execute \
  deploy:odoo-opw-preview-apply-manual-grant \
  odoo-opw-preview-apply-manual \
  workflow_dispatch
