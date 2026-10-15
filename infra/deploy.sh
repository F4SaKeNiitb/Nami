#!/bin/sh
# One-command deploy to Azure Container Apps. UNVERIFIED: written against the Azure CLI docs, never run (no subscription).
# Usage: RG=nami-rg LOCATION=centralindia NAME=nami infra/deploy.sh
set -eu
: "${RG:?}" "${LOCATION:?}" "${NAME:?}" "${GOOGLE_CLIENT_ID:?}" "${ADMIN_EMAILS:?}"
ACR="${NAME}acr"; KV="${NAME}-kv"; BLOB="${NAME}blob"; ENVN="${NAME}-env"

az group create -n "$RG" -l "$LOCATION" >/dev/null
az acr create -g "$RG" -n "$ACR" --sku Basic >/dev/null
az keyvault create -g "$RG" -n "$KV" --enable-rbac-authorization true >/dev/null
az keyvault key create --vault-name "$KV" -n nami-kek --kty RSA --size 3072 --ops wrapKey unwrapKey >/dev/null
az storage account create -g "$RG" -n "$BLOB" --sku Standard_LRS --allow-blob-public-access false >/dev/null
az storage container create --account-name "$BLOB" -n nami-db --auth-mode login >/dev/null

# Build in ACR, then deploy by digest (never by a moving tag).
az acr build -g "$RG" -r "$ACR" -t nami:latest . >/dev/null
IMAGE_DIGEST=$(az acr repository show -n "$ACR" --image nami:latest --query digest -o tsv)

az containerapp env create -g "$RG" -n "$ENVN" -l "$LOCATION" >/dev/null
export KV_NAME="$KV" ACR BLOB_ACCOUNT="$BLOB" IMAGE_DIGEST GOOGLE_CLIENT_ID ADMIN_EMAILS ALLOWED_DOMAINS="${ALLOWED_DOMAINS:-}"
envsubst < infra/containerapp.yaml > /tmp/containerapp.rendered.yaml          # contains no secret values
az containerapp create -g "$RG" -n "$NAME" --environment "$ENVN" --yaml /tmp/containerapp.rendered.yaml \
  --system-assigned >/dev/null
PRINCIPAL=$(az containerapp show -g "$RG" -n "$NAME" --query identity.principalId -o tsv)
KVID=$(az keyvault show -n "$KV" --query id -o tsv)
az role assignment create --assignee "$PRINCIPAL" --role "Key Vault Secrets User" --scope "$KVID" >/dev/null
az role assignment create --assignee "$PRINCIPAL" --role "Key Vault Crypto User" --scope "$KVID" >/dev/null
az role assignment create --assignee "$PRINCIPAL" --role "Storage Blob Data Contributor" \
  --scope "$(az storage account show -g "$RG" -n "$BLOB" --query id -o tsv)" >/dev/null
echo "Deployed. Add the secrets to Key Vault ($KV): openai-api-key, kernel-api-key, google-client-secret, chainlit-auth-secret."
echo "Google redirect URI: https://$(az containerapp show -g "$RG" -n "$NAME" --query properties.configuration.ingress.fqdn -o tsv)/chainlit/auth/oauth/google/callback"
