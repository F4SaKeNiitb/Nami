#!/bin/sh
# Let one more Google account sign in to the deployed app, then restart it. Idempotent. No SSH.
#   infra/azure-vm/allow-email.sh someone@gmail.com        (a whole domain also works: example.com)
# The list lives in /opt/nami/nami.env on the VM as NAMI_ALLOWED_EMAIL_DOMAINS (comma separated). Remember Google's own side too: while the
# OAuth consent screen is in "Testing", the person must also be added there as a test user (see docs/DEPLOY_AZURE.md).
set -eu
. "$(cd "$(dirname "$0")" && pwd)/common.sh"
entry="$(printf '%s' "${1:?usage: allow-email.sh someone@example.com}" | tr 'A-Z' 'a-z')"
case "$entry" in *[!a-z0-9._%+@-]*|"") echo "not a plain email address or domain: $entry" >&2; exit 1;; esac    # nothing that could act as shell or sed syntax
vm_run "set -e
f=/opt/nami/nami.env
cur=\$(grep '^NAMI_ALLOWED_EMAIL_DOMAINS=' \$f | head -1 | cut -d= -f2-)
case \",\$cur,\" in *,$entry,*) echo 'already allowed';; *)
  new=\"\${cur:+\$cur,}$entry\"
  grep -v '^NAMI_ALLOWED_EMAIL_DOMAINS=' \$f > \$f.new; echo \"NAMI_ALLOWED_EMAIL_DOMAINS=\$new\" >> \$f.new; cat \$f.new > \$f; rm -f \$f.new
  echo 'added';; esac
echo \"allowed now: \$(grep '^NAMI_ALLOWED_EMAIL_DOMAINS=' \$f | cut -d= -f2-)\"
systemctl restart nami; sleep 25; echo \"service: \$(systemctl is-active nami)\"" | tail -6
