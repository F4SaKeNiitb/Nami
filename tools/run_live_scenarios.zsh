#!/bin/zsh

# Run the opt-in real-LLM browser scenarios without first running pytest.
# With no arguments the live_signin module runs the complete scenario matrix.
# Pass scenario names to run only a subset, e.g. `none otp_expired expiry_loop`.
setopt pipefail

repo_root=${0:A:h:h}
cd "$repo_root" || exit 1

if [[ ! -x .venv/bin/python ]]; then
  print -u2 "Missing .venv/bin/python. Create the project virtual environment first."
  exit 2
fi

print "Checking model configuration..."
.venv/bin/python -c 'from app.core.config import Settings; s=Settings(); assert s.llm_api_key, "Set NAMI_OPENAI_API_KEY or NAMI_ANTHROPIC_API_KEY in .env"; print(f"Using {s.provider} ({s.model_main})")' || exit $?

log_dir="data/assignment-runs"
mkdir -p "$log_dir" || exit 1
timestamp=$(date '+%Y%m%d-%H%M%S')
log_file="$log_dir/live-signin-$timestamp.log"

print "Running real-LLM local headless-browser scenarios..."
print "All output will be saved to: $log_file"
.venv/bin/python -u -m app.eval.live_signin "$@" 2>&1 | tee "$log_file"
run_status=${pipestatus[1]}

print "Run finished with exit code $run_status"
print "Run finished with exit code $run_status" >> "$log_file"
print "$log_file" > "$log_dir/LATEST"
exit $run_status
