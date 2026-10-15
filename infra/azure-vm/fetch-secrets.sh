#!/bin/sh
# Runs before every start. Reads the secrets from Key Vault with the VM's managed identity (no password, no key on disk) and writes
# them to /run/nami/env, which lives in memory (tmpfs) and is mode 600. A missing optional secret is skipped, a missing required one stops the start.
set -eu
KV="${KV_NAME:?}"
umask 077
mkdir -p /run/nami
TOKEN=$(curl -sf -H Metadata:true "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://vault.azure.net" | jq -r .access_token)
: > /run/nami/env.tmp
get() {  # secret-name ENV_VAR required|optional
  v=$(curl -sf -H "Authorization: Bearer $TOKEN" "https://$KV.vault.azure.net/secrets/$1?api-version=7.4" | jq -r .value 2>/dev/null || true)
  if [ -n "$v" ] && [ "$v" != "null" ]; then printf '%s=%s\n' "$2" "$v" >> /run/nami/env.tmp
  elif [ "$3" = required ]; then echo "required secret $1 is missing from Key Vault $KV" >&2; exit 1; fi
}
get openai-api-key           NAMI_OPENAI_API_KEY           optional
get anthropic-api-key        NAMI_ANTHROPIC_API_KEY        optional
get kernel-api-key           NAMI_KERNEL_API_KEY           optional
get browser-use-api-key      NAMI_BROWSER_USE_API_KEY      optional
get google-client-secret     OAUTH_GOOGLE_CLIENT_SECRET    required
get chainlit-auth-secret     CHAINLIT_AUTH_SECRET          required
get slack-bot-token          NAMI_SLACK_BOT_TOKEN          optional
get slack-app-token          NAMI_SLACK_APP_TOKEN          optional
get openrouter-api-key       NAMI_OPENROUTER_API_KEY       optional
get litestream-account-key   LITESTREAM_AZURE_ACCOUNT_KEY  optional
grep -q '^NAMI_OPENAI_API_KEY=\|^NAMI_ANTHROPIC_API_KEY=' /run/nami/env.tmp || { echo "no model key in Key Vault $KV: add openai-api-key (or anthropic-api-key)" >&2; exit 1; }
mv /run/nami/env.tmp /run/nami/env
