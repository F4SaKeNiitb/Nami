#!/bin/sh
# (Re)start the app on the VM and show whether it came up. No SSH.   infra/azure-vm/remote-start.sh
set -eu
. "$(cd "$(dirname "$0")" && pwd)/common.sh"
vm_run 'systemctl restart nami; sleep 30; docker image prune -f >/dev/null; docker compose -f /opt/nami/docker-compose.yml ps 2>&1 | cut -c1-160; echo "--- journal:"; journalctl -u nami -n 12 --no-pager 2>&1 | cut -c1-190' | tail -30
echo; echo "health from here:"; curl -s -m 15 "https://$FQDN/healthz" || echo "(not answering from this network yet; try again in a minute, or from your phone)"
