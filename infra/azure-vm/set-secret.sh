#!/bin/sh
# Put one secret in Key Vault without it touching your shell history or a process list.
#   KV_NAME=nami-kv-xxxxxx infra/azure-vm/set-secret.sh slack-bot-token      (it asks, hidden; Enter on an empty value skips)
set -eu
: "${KV_NAME:?}"; name="${1:?secret name}"
printf 'Value for %s (hidden, Enter to skip): ' "$name"; stty -echo; read -r v; stty echo; printf '\n'
[ -n "$v" ] || { echo "skipped $name"; exit 0; }
printf '%s' "$v" | az keyvault secret set --vault-name "$KV_NAME" --name "$name" --file /dev/stdin --encoding utf-8 --query name -o tsv
