#!/bin/sh
# Skip a job when its usage limit is flagged. Flag the limit when the job prints a notice.
# Run this from the repository root.
# JOB is the command to guard. The default prints a sample notice and needs no account.
set -eu
# JOB is split on spaces and never expanded as a file pattern.
set -f

if [ ! -f max_pool_governor.py ]; then
  echo "could not find max_pool_governor.py; run this from the repository root" >&2
  exit 1
fi

if [ -z "${JOB:-}" ] && [ -z "${MAX_POOL_STATE_DIR:-}" ]; then
  MAX_POOL_STATE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/max-pool-demo.XXXXXX")
  export MAX_POOL_STATE_DIR
  trap 'rm -rf "$MAX_POOL_STATE_DIR"' EXIT
  echo "demo: no JOB and no MAX_POOL_STATE_DIR, so the flag goes to a new temporary directory" >&2
fi

JOB=${JOB:-cat examples/limit-notice.txt}
POOL=${POOL:-pool:default}
SOURCE=${SOURCE:-nightly}
last_failure=0

for label in "job 1" "job 2"; do
  set +e
  python3 max_pool_governor.py check "$POOL"
  status=$?
  set -e
  if [ "$status" -eq 0 ]; then
    echo "$label: skipped: usage limit is flagged, so this job did not run"
    continue
  fi
  if [ "$status" -ne 1 ]; then
    echo "$label: could not check the usage limit flag" >&2
    exit "$status"
  fi
  echo "$label: running"
  # JOB is one command line. The default needs no quotes and no account.
  set +e
  output=$($JOB 2>&1)
  job_status=$?
  set -e
  printf '%s\n' "$output"
  if [ "$job_status" -ne 0 ]; then
    last_failure=$job_status
    echo "$label: the job exited $job_status"
  fi
  set +e
  printf '%s\n' "$output" | python3 max_pool_governor.py detect "$SOURCE" "$POOL"
  detected=$?
  set -e
  if [ "$detected" -eq 0 ]; then
    echo "$label: limit notice found, flag raised"
    continue
  fi
  if [ "$detected" -eq 1 ]; then
    echo "$label: no limit notice"
    continue
  fi
  echo "$label: could not read the job output for a limit notice" >&2
  exit "$detected"
done
exit "$last_failure"
