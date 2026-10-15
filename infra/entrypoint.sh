#!/bin/sh
# Restore the databases if a replica exists, then run the app under Litestream so every write is replicated.
set -eu
mkdir -p /srv/data/files          # Chainlit's attachment folder (/srv/.files points here); the data volume may be a fresh, empty directory
if [ -n "${NAMI_BLOB_ACCOUNT:-}" ]; then
  for db in nami chainlit graph_checkpoints; do
    litestream restore -if-db-not-exists -if-replica-exists -config /srv/infra/litestream.yml "/srv/data/$db.db"
  done
  exec litestream replicate -config /srv/infra/litestream.yml -exec "uvicorn app.main:app --host 0.0.0.0 --port 8000"
fi
echo "NAMI_BLOB_ACCOUNT not set: running WITHOUT replication (local/dev only)" >&2
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
