#!/bin/sh
# Change one NON-SECRET setting of the deployed app and restart it (about 30 s). No rebuild, no SSH. Secrets go through set-secret.sh, never here.
#   infra/azure-vm/set-env.sh NAMI_MODEL_MAIN gpt-5-mini
# It refuses to restart while a task is running (that would cut it off); FORCE=1 overrides. Settings live in /opt/nami/nami.env on the VM.
set -eu
. "$(cd "$(dirname "$0")" && pwd)/common.sh"
key="${1:?usage: set-env.sh NAMI_SOMETHING value}"; val="${2:?usage: set-env.sh NAMI_SOMETHING value}"
case "$key" in NAMI_*) ;; *) echo "only NAMI_* settings can be changed here, got: $key" >&2; exit 1;; esac
case "$key" in *KEY*|*TOKEN*|*SECRET*|*PASSWORD*) echo "$key looks like a secret: use set-secret.sh (Key Vault), not a plain file" >&2; exit 1;; esac
case "$key" in *[!A-Z0-9_]*) echo "bad setting name: $key" >&2; exit 1;; esac
case "$val" in *[!A-Za-z0-9._,@:/+-]*|"") echo "the value may only contain letters, digits and . _ , @ : / + -" >&2; exit 1;; esac
vm_run "set -e
busy=\$(docker exec nami-nami-1 python -c \"import sqlite3; db=sqlite3.connect('/srv/data/nami.db'); print(db.execute(\\\"select count(*) from tasks where status in ('running','planning','verifying')\\\").fetchone()[0])\" 2>/dev/null || echo 0)
if [ \"\$busy\" != 0 ] && [ \"${FORCE:-0}\" != 1 ]; then echo \"not restarting: \$busy task(s) running (FORCE=1 to do it anyway)\"; exit 0; fi
f=/opt/nami/nami.env
grep -v '^$key=' \$f > \$f.new; echo '$key=$val' >> \$f.new; cat \$f.new > \$f; rm -f \$f.new
systemctl restart nami; sleep 28
echo \"set: \$(grep '^$key=' \$f)\"; echo \"service: \$(systemctl is-active nami)\"" | tail -4
