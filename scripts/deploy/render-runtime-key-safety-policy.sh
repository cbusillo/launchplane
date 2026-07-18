#!/usr/bin/env bash
set -euo pipefail

: "${GITHUB_SHA:?Missing GitHub SHA.}"

output_dir="${LAUNCHPLANE_RUNTIME_KEY_SAFETY_OUTPUT_DIR:-${RUNNER_TEMP:-.}/launchplane-runtime-key-safety}"
policy_file="${output_dir}/runtime-key-safety-policy.json"
rules_json="${LAUNCHPLANE_RUNTIME_KEY_SAFETY_RULES_JSON:-}"

mkdir -p "$output_dir"
rm -f "$policy_file"

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

if [[ -z "${rules_json//[[:space:]]/}" ]]; then
	write_output has_runtime_key_safety_policy false
	write_output runtime_key_safety_policy_file "$policy_file"
	write_output runtime_key_safety_idempotency_key ""
	echo "Runtime key-safety rules are unset; skipping policy reconciliation."
	exit 0
fi

normalized_rules="$({
	jq -c '
    if type != "array" then
      error("LAUNCHPLANE_RUNTIME_KEY_SAFETY_RULES_JSON must be a JSON array")
    else . end
    | if any(.[]; ((.binding_key // "") | length) == 0 or ((.secret_class // "") | length) == 0)
      then error("Each runtime key-safety rule must include binding_key and secret_class")
      else . end
    | if any(.[]; (.secret_class as $class | ["prod_only", "testing", "preview", "non_prod", "shared_safe"] | index($class) | not))
      then error("Runtime key-safety secret_class must be one of prod_only, testing, preview, non_prod, shared_safe")
      else . end
    | map({
        binding_key: (.binding_key // ""),
        secret_class: (.secret_class // ""),
        allowed_contexts: (.allowed_contexts // []),
        allowed_instances: (.allowed_instances // []),
        allowed_instance_patterns: (.allowed_instance_patterns // []),
        allowed_targets: (.allowed_targets // []),
        description: (.description // "")
      })
  ' <<<"$rules_json"
})"

jq -n \
	--argjson rules "$normalized_rules" \
	'{
    schema_version: 1,
    product: "launchplane",
    source_label: "deploy:runtime-key-safety-rules",
    rules: $rules
  }' >"$policy_file"

payload_sha256="$(jq -cS . "$policy_file" | sha256_stdin)"
idempotency_key="launchplane-runtime-key-safety-rules:${GITHUB_SHA}:${payload_sha256}"
write_output has_runtime_key_safety_policy true
write_output runtime_key_safety_policy_file "$policy_file"
write_output runtime_key_safety_idempotency_key "$idempotency_key"

echo "Rendered Launchplane runtime key-safety policy."
