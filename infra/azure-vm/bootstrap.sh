#!/bin/sh
# Runs ONCE on the VM (deploy.sh copies it over and calls it). Installs Docker, adds swap, installs the service. Idempotent.
# Needs: FQDN KV_NAME BLOB_ACCOUNT GOOGLE_CLIENT_ID ADMIN_EMAILS ALLOWED_DOMAINS in the environment.
set -eu
: "${FQDN:?}" "${KV_NAME:?}" "${GOOGLE_CLIENT_ID:?}"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq docker.io docker-compose-v2 jq curl ca-certificates unattended-upgrades

if ! swapon --show | grep -q /swapfile; then        # 1 GiB of RAM: swap turns a spike into slowness instead of a killed process
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  echo 'vm.swappiness=20' | sudo tee /etc/sysctl.d/99-nami.conf >/dev/null && sudo sysctl -p /etc/sysctl.d/99-nami.conf >/dev/null
fi

sudo mkdir -p /opt/nami /var/lib/nami
sudo chown 10001:10001 /var/lib/nami                 # the container's user
sudo cp docker-compose.yml Caddyfile fetch-secrets.sh /opt/nami/
sudo chmod 755 /opt/nami/fetch-secrets.sh
printf 'FQDN=%s\nKV_NAME=%s\n' "$FQDN" "$KV_NAME" | sudo tee /opt/nami/host.env >/dev/null     # for systemd (EnvironmentFile)
printf 'FQDN=%s\n' "$FQDN" | sudo tee /opt/nami/.env >/dev/null                                  # for docker compose
sudo tee /opt/nami/nami.env >/dev/null <<ENV
NAMI_BROWSER_PROVIDER=kernel
NAMI_LLM_PROVIDER=openai
NAMI_KERNEL_HEADLESS=false
NAMI_KMS_PROVIDER=azure
NAMI_AZURE_KEY_VAULT_URL=https://${KV_NAME}.vault.azure.net
NAMI_PUBLIC_URL=https://${FQDN}
CHAINLIT_URL=https://${FQDN}
FORWARDED_ALLOW_IPS=*
OAUTH_GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID}
NAMI_ADMIN_EMAILS=${ADMIN_EMAILS:-}
NAMI_ALLOWED_EMAIL_DOMAINS=${ALLOWED_DOMAINS:-}
NAMI_SKILLS_FREEZE=1
NAMI_BLOB_ACCOUNT=${BLOB_ACCOUNT:-}
NAMI_BLOB_CONTAINER=nami-db
ENV
sudo chmod 644 /opt/nami/nami.env                     # nothing secret in it
sudo cp nami.service /etc/systemd/system/nami.service
sudo systemctl daemon-reload
sudo systemctl enable nami >/dev/null                 # starts at every boot; it will wait for the image (push-image.sh) the first time
echo "bootstrap done"
