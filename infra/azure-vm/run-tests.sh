#!/bin/sh
# Run the whole test suite in Azure (nothing runs on your machine or on the VM) and print the result.
#   infra/azure-vm/run-tests.sh                      everything (real Chromium, Node and fastembed are installed in the test image)
#   PYTEST_ARGS='tests/test_slack.py -x' infra/azure-vm/run-tests.sh      a subset (the args are passed to pytest)
# A temporary registry (Basic, about $0.17/day) runs the build and is deleted afterwards. The full log is kept in /tmp/nami-tests.log.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/common.sh"
ROOT="$HERE/../.."
CTX="$(mktemp -d)/ctx"
mkdir -p "$CTX"
# A copy of the project WITHOUT anything private: no .env, no data, no keys, no git history, no virtualenv.
rsync -a --exclude '.venv' --exclude 'data' --exclude '.env' --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' --exclude '.pytest_cache' \
      --exclude '.files' --exclude 'node_modules' --exclude 'ManishFinal.pdf' --exclude '*.pem' --exclude '*.key' --exclude '*.db' "$ROOT/" "$CTX/"
: > "$CTX/.dockerignore"            # the build context must include tests/, so it is sent unfiltered...
cp "$ROOT/.dockerignore" "$CTX/dockerignore.original"   # ...and the real .dockerignore travels under another name for the hygiene tests
cp "$HERE/Dockerfile.test" "$CTX/Dockerfile.test"
ACR="${NAME}acr${SUFFIX}"
echo "==> building the test image in Azure ($ACR); installing dependencies, then running the tests"
az acr create -g "$RG" -n "$ACR" --sku Basic -o none
set +e
az acr build -g "$RG" -r "$ACR" -t nami-test:run -f "$CTX/Dockerfile.test" --platform linux/amd64 ${PYTEST_ARGS:+--build-arg "PYTEST_ARGS=$(printf '%s' "$PYTEST_ARGS" | sed 's/ /%20/g')"} "$CTX" > /tmp/nami-tests.log 2>&1
code=$?
set -e
az acr delete -g "$RG" -n "$ACR" --yes -o none && echo "==> temporary registry deleted"
rm -rf "$(dirname "$CTX")"
echo "==> tests $( [ $code -eq 0 ] && echo PASSED || echo 'FAILED (or the build did)' ) (exit $code); full log: /tmp/nami-tests.log"
exit $code
