#!/bin/sh
# Installs Docker, swap and the service on the VM (what bootstrap.sh does), sending the files through `az vm run-command`: no SSH.
#   GOOGLE_CLIENT_ID=... ADMIN_EMAILS=you@x.com [ALLOWED_DOMAINS=...] infra/azure-vm/bootstrap-remote.sh
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/common.sh"
: "${GOOGLE_CLIENT_ID:?}" "${ADMIN_EMAILS:?}"
ALLOWED_DOMAINS="${ALLOWED_DOMAINS:-$ADMIN_EMAILS}"
b64() { base64 < "$HERE/$1" | tr -d '\n'; }
SCRIPT="set -e; rm -rf /tmp/nami-setup; mkdir -p /tmp/nami-setup; cd /tmp/nami-setup
echo '$(b64 docker-compose.yml)' | base64 -d > docker-compose.yml
echo '$(b64 Caddyfile)' | base64 -d > Caddyfile
echo '$(b64 fetch-secrets.sh)' | base64 -d > fetch-secrets.sh
echo '$(b64 nami.service)' | base64 -d > nami.service
echo '$(b64 bootstrap.sh)' | base64 -d > bootstrap.sh
FQDN='$FQDN' KV_NAME='$KV' BLOB_ACCOUNT='$SA' GOOGLE_CLIENT_ID='$GOOGLE_CLIENT_ID' ADMIN_EMAILS='$ADMIN_EMAILS' ALLOWED_DOMAINS='$ALLOWED_DOMAINS' sh bootstrap.sh"
echo "==> installing Docker, swap and the service on $NAME-vm (a few minutes)"
vm_run "$SCRIPT" | tail -25
