#!/bin/sh
# Build the app image inside Azure and put it on the VM, then (unless NO_START=1) start it. Needs no Docker on your machine and no SSH.
# A temporary container registry (Basic, about $0.17/day) does the build; it is deleted afterwards (KEEP_ACR=1 keeps it: about $5/month).
#   infra/azure-vm/push-image.sh        [RG=nami-rg NAME=nami]
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/common.sh"
cd "$HERE/../.."
ACR="${NAME}acr${SUFFIX}"
echo "==> building in Azure ($ACR); a few minutes"
az acr create -g "$RG" -n "$ACR" --sku Basic -o none
az acr build -g "$RG" -r "$ACR" -t nami:latest --platform linux/amd64 . > /tmp/nami-acr-build.log 2>&1 \
  || { tail -40 /tmp/nami-acr-build.log; echo "build failed (full log: /tmp/nami-acr-build.log)" >&2; exit 1; }
tail -3 /tmp/nami-acr-build.log
echo "==> pulling it onto the VM"
TOKEN=$(az acr login -n "$ACR" --expose-token --query accessToken -o tsv)      # short-lived (hours); sent over HTTPS to Azure's management plane
vm_run "set -e; echo '$TOKEN' | docker login $ACR.azurecr.io -u 00000000-0000-0000-0000-000000000000 --password-stdin >/dev/null
docker pull -q $ACR.azurecr.io/nami:latest >/dev/null; docker tag $ACR.azurecr.io/nami:latest nami:local
docker rmi $ACR.azurecr.io/nami:latest >/dev/null; docker image prune -f >/dev/null; docker logout $ACR.azurecr.io >/dev/null; docker image ls nami:local --format 'image on the VM: {{.Repository}}:{{.Tag}} {{.Size}}'" | tail -3
if [ "${KEEP_ACR:-0}" != 1 ]; then az acr delete -g "$RG" -n "$ACR" --yes -o none && echo "==> temporary registry deleted"; fi
if [ "${NO_START:-0}" = 1 ]; then echo "Image is on the VM. Add the secrets, then run infra/azure-vm/remote-start.sh"; exit 0; fi
exec "$HERE/remote-start.sh"
