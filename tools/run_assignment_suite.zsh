#!/bin/zsh

# Run deterministic repository tests followed by every real-LLM local-browser scenario.
setopt pipefail

repo_root=${0:A:h:h}
cd "$repo_root" || exit 1

log_dir="data/assignment-runs"
mkdir -p "$log_dir" || exit 1
timestamp=$(date '+%Y%m%d-%H%M%S')
log_file="$log_dir/assignment-$timestamp.log"

print "All output will be saved to: $log_file"
print "Running deterministic repository tests..."
.venv/bin/python -u -m pytest -vv -ra -o faulthandler_timeout=120 2>&1 | tee "$log_file"
unit_status=${pipestatus[1]}

if (( unit_status != 0 )); then
  print "Deterministic tests failed with exit code $unit_status; live browser scenarios were not started."
  print "Log: $log_file"
  print "Run finished with exit code $unit_status" >> "$log_file"
  print "$log_file" > "$log_dir/LATEST"
  exit $unit_status
fi

print "Deterministic tests passed. Running the real-LLM local headless-browser matrix..."
.venv/bin/python -u -m app.eval.live_signin 2>&1 | tee -a "$log_file"
live_status=${pipestatus[1]}

print "Run finished with exit code $live_status"
print "Log: $log_file"
print "Run finished with exit code $live_status" >> "$log_file"
print "$log_file" > "$log_dir/LATEST"
exit $live_status
