#!/bin/bash
# Runs the tests inside the test image (a script, not a Dockerfile one-liner, so quoting stays sane). PYTEST_ARGS arrives with spaces encoded as %20
# (the registry's build runner does not keep them); empty = everything. There is no desktop here, so NAMI_SMOKE_NO_DISPLAY=1 skips the one smoke scenario that opens a visible window.
set -o pipefail
args="${PYTEST_ARGS//%20/ }"
eval "python -m pytest -q --tb=short -rfEs -p no:cacheprovider $args" 2>&1 | tee /tmp/pytest.out | tail -n 400
