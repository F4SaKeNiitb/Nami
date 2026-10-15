#!/bin/sh
# Read the deployed app's logs without SSH (everything goes through `az vm run-command`, which returns the output when the command ends:
# it cannot stream, so run it again to see newer lines).
#   infra/azure-vm/logs.sh status                    is it up, healthy, how much memory
#   infra/azure-vm/logs.sh app [lines] [text]        the app's log; [text] keeps only lines containing it. Default 80 lines.
#   infra/azure-vm/logs.sh errors [lines]            only errors and tracebacks in the last [lines] (default 600)
#   infra/azure-vm/logs.sh tasks [n]                 what the last n tasks did, step by step with times (from the task ledger; no secrets)
#   infra/azure-vm/logs.sh caddy [lines]             the web server (certificates, requests that could not reach the app)
#   infra/azure-vm/logs.sh service [lines]           systemd: secret fetch, restarts, why it would not start
# RAW=1 keeps the database-backup chatter (Litestream "wal segment" lines) that is hidden by default.
set -eu
. "$(cd "$(dirname "$0")" && pwd)/common.sh"
what="${1:-status}"; n="${2:-}"; text="${3:-}"
num() { case "$1" in ''|*[!0-9]*) echo "need a number, got: $1" >&2; exit 1;; esac; }
case "$text" in *[!A-Za-z0-9\ _.:-]*) echo "the filter text may only contain letters, digits, space and _ . : -" >&2; exit 1;; esac
C="docker compose -f /opt/nami/docker-compose.yml"
QUIET='grep -v "msg=\"\\(write \\)\\?wal segment\\|msg=\"sync\\|msg=\"compaction"'
[ "${RAW:-0}" = 1 ] && QUIET=cat
case "$what" in
  status)  vm_run "echo \"service: \$(systemctl is-active nami) (enabled: \$(systemctl is-enabled nami))\"; $C ps --format '{{.Service}}: {{.Status}}'; echo \"health: \$(curl -s -m 8 -o /dev/null -w '%{http_code}' --resolve $FQDN:443:127.0.0.1 https://$FQDN/healthz)\"; docker stats --no-stream --format '{{.Name}}: {{.MemUsage}}'; free -m | sed -n 2,3p; echo \"VM time: \$(date -u +%T)Z\"" ;;
  app)     n="${n:-80}"; num "$n"; vm_run "$C logs --no-log-prefix --tail 4000 nami 2>&1 | $QUIET | grep -F -- '$text' | tail -$n | cut -c1-220" ;;
  errors)  n="${n:-600}"; num "$n"; vm_run "$C logs --no-log-prefix --tail $n nami 2>&1 | $QUIET | grep -i 'error\\|traceback\\|exception\\|failed' | tail -40 | cut -c1-220; echo '(end)'" ;;
  caddy)   n="${n:-40}"; num "$n"; vm_run "$C logs --no-log-prefix --tail $n caddy 2>&1 | cut -c1-220" ;;
  service) n="${n:-40}"; num "$n"; vm_run "journalctl -u nami -n $n --no-pager 2>&1 | cut -c1-200" ;;
  tasks)   n="${n:-3}"; num "$n"
    vm_run "cat > /tmp/tasklog.py <<'PYEOF'
import json, sqlite3, sys
from datetime import datetime
db = sqlite3.connect('/srv/data/nami.db'); n = int(sys.argv[1])
t = lambda s: datetime.fromisoformat(str(s).replace('Z', '+00:00'))
for tid, st, req, c in db.execute('select id, status, substr(request_text,1,80), created_at from tasks order by created_at desc limit ?', (n,)).fetchall():
    print(f'\n== {req!r}  [{st}]  started {str(c)[11:19]}Z')
    prev = None
    for ty, pl, ts in db.execute('select type, payload, ts from task_events where task_id=? order by ts', (tid,)):
        try: p = json.loads(pl)
        except Exception: p = {}
        keep = {k: str(v)[:100] for k, v in p.items() if k in ('goal', 'reason', 'message', 'kind', 'text', 'passed', 'evidence', 'site', 'url', 'names')} if isinstance(p, dict) else {}
        gap = f'+{(t(ts) - prev).total_seconds():5.1f}s' if prev else '      '
        print(f'  {str(ts)[11:19]} {gap} {ty:18} {keep}'); prev = t(ts)
PYEOF
docker cp /tmp/tasklog.py nami-nami-1:/tmp/tasklog.py; docker exec nami-nami-1 python /tmp/tasklog.py $n" ;;
  *) echo "usage: logs.sh status | app [lines] [text] | errors [lines] | tasks [n] | caddy [lines] | service [lines]" >&2; exit 1 ;;
esac
