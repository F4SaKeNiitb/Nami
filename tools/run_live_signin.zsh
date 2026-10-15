#!/bin/zsh

# Run the real-model local sign-in evaluation and keep a complete, timestamped log.
setopt pipefail

repo_root=${0:A:h:h}
cd "$repo_root" || exit 1

log_dir="data/live-signin-runs"
mkdir -p "$log_dir" || exit 1
timestamp=$(date '+%Y%m%d-%H%M%S')
log_file="$log_dir/live-signin-$timestamp.log"

print "Full output will be saved to: $log_file"
print "Starting local sign-in workflows..."

.venv/bin/python -u -m app.eval.live_signin "$@" 2>&1 | tee "$log_file"
runner_status=${pipestatus[1]}

print ""
print "Run finished with exit code $runner_status"
print "Log: $log_file"
if [[ ! -s "$log_file" ]]; then
  print "The Python process exited before producing output (often a native browser/runtime crash)." >> "$log_file"
fi
print "Run finished with exit code $runner_status" >> "$log_file"
print "$log_file" > "$log_dir/LATEST"

exit $runner_status
