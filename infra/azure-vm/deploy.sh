#!/bin/sh
# Creates everything in Azure for ~$11/month. (Operations on the VM use `az vm run-command`, not SSH: see common.sh.) UNVERIFIED: written against the Azure CLI docs and the prices/SKU checks in
# docs/DEPLOY_AZURE.md; it has NOT been run. Read it first: it creates billable resources in your subscription.
#
#   GOOGLE_CLIENT_ID=... ADMIN_EMAILS=you@x.com infra/azure-vm/deploy.sh
#
# Optional: LOCATION (centralindia) RG (nami-rg) NAME (nami) SIZE (Standard_B2ats_v2) ALLOWED_DOMAINS
set -eu
: "${GOOGLE_CLIENT_ID:?}" "${ADMIN_EMAILS:?}"
LOCATION="${LOCATION:-centralindia}"; RG="${RG:-nami-rg}"; NAME="${NAME:-nami}"; SIZE="${SIZE:-Standard_B2ats_v2}"
HERE="$(cd "$(dirname "$0")" && pwd)"
KEY="${SSH_KEY:-$HOME/.ssh/nami_azure}"                                  # a key used only for this VM; your other keys are not touched
[ -f "$KEY" ] || ssh-keygen -t rsa -b 4096 -f "$KEY" -N "" -C "nami-azure" -q
ALLOWED_DOMAINS="${ALLOWED_DOMAINS:-$ADMIN_EMAILS}"                      # who may sign in: default is just the admin(s). Widen it later; empty would mean ANY Google account
SUB=$(az account show --query id -o tsv)
# Your object id, read from your own sign-in token (works for personal Microsoft accounts, where the Graph lookup `az ad signed-in-user` fails)
ME=$(az account get-access-token --query accessToken -o tsv | python3 -c "import sys,base64,json; t=sys.stdin.read().split('.')[1]; t+='='*(-len(t)%4); print(json.loads(base64.urlsafe_b64decode(t))['oid'])")
SUFFIX=$(printf '%s%s' "$SUB" "$NAME" | shasum | cut -c1-6)          # same names every run, unique across Azure
DNS="$NAME-$SUFFIX"; KV="$NAME-kv-$SUFFIX"; SA="${NAME}blob${SUFFIX}"
FQDN="$DNS.$LOCATION.cloudapp.azure.com"

echo "==> resource group, storage (database backups), Key Vault (secrets + the vault's master key)"
az group create -n "$RG" -l "$LOCATION" -o none
az storage account create -g "$RG" -n "$SA" -l "$LOCATION" --sku Standard_LRS --allow-blob-public-access false --min-tls-version TLS1_2 -o none
az storage container create --account-name "$SA" -n nami-db --auth-mode key -o none
az keyvault create -g "$RG" -n "$KV" -l "$LOCATION" --enable-rbac-authorization true --enable-purge-protection true --retention-days 7 -o none
KVID=$(az keyvault show -n "$KV" --query id -o tsv)
az role assignment create --assignee-object-id "$ME" --assignee-principal-type User --role "Key Vault Administrator" --scope "$KVID" -o none
echo "    waiting for the role to take effect..."; sleep 45
az keyvault key create --vault-name "$KV" -n nami-kek --kty RSA --size 3072 --ops wrapKey unwrapKey -o none
openssl rand -hex 32 | az keyvault secret set --vault-name "$KV" -n chainlit-auth-secret --file /dev/stdin --encoding utf-8 -o none
az storage account keys list -g "$RG" -n "$SA" --query '[0].value' -o tsv | tr -d '\n' \
  | az keyvault secret set --vault-name "$KV" -n litestream-account-key --file /dev/stdin --encoding utf-8 -o none

echo "==> network: static IP with a free DNS name ($FQDN); 80/443 open, no SSH"
az network public-ip create -g "$RG" -n "$NAME-ip" --sku Standard --allocation-method Static --dns-name "$DNS" -o none
az network nsg create -g "$RG" -n "$NAME-nsg" -o none
az network nsg rule create -g "$RG" --nsg-name "$NAME-nsg" -n web --priority 100 --protocol Tcp --destination-port-ranges 80 443 --access Allow -o none
if [ -n "${SSH_FROM:-}" ]; then   # SSH is off by default: everything goes through `az vm run-command`. Open it only to debug: SSH_FROM=your.ip
  az network nsg rule create -g "$RG" --nsg-name "$NAME-nsg" -n ssh --priority 110 --protocol Tcp --destination-port-ranges 22 --source-address-prefixes "$SSH_FROM/32" --access Allow -o none
fi

echo "==> VM $SIZE (always on: nothing here ever stops or deallocates it)"
az vm create -g "$RG" -n "$NAME-vm" -l "$LOCATION" --size "$SIZE" --image Canonical:ubuntu-24_04-lts:server:latest \
  --os-disk-size-gb 30 --storage-sku StandardSSD_LRS --admin-username azureuser --ssh-key-values "$KEY.pub" \
  --public-ip-address "$NAME-ip" --nsg "$NAME-nsg" --assign-identity '[system]' -o none
PRINCIPAL=$(az vm show -g "$RG" -n "$NAME-vm" --query identity.principalId -o tsv)
for role in "Key Vault Secrets User" "Key Vault Crypto User"; do
  az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal --role "$role" --scope "$KVID" -o none
done

GOOGLE_CLIENT_ID="$GOOGLE_CLIENT_ID" ADMIN_EMAILS="$ADMIN_EMAILS" ALLOWED_DOMAINS="$ALLOWED_DOMAINS" "$HERE/bootstrap-remote.sh"

cat <<DONE

Provisioned (nothing is running yet: it needs secrets and the image).
  Address:        https://$FQDN
  Key Vault:      $KV
  Google redirect URI to add in Google Cloud Console (APIs & Services -> Credentials -> your OAuth client):
                  https://$FQDN/chainlit/auth/oauth/google/callback
Next:
  export KV_NAME=$KV FQDN=$FQDN
  for s in openai-api-key browser-use-api-key google-client-secret slack-bot-token slack-app-token; do infra/azure-vm/set-secret.sh \$s; done
  infra/azure-vm/push-image.sh      # builds in Azure (no Docker or SSH needed) and starts it
  curl https://$FQDN/healthz
DONE
