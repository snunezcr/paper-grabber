#!/usr/bin/env bash
#
# The unattended half of the pipeline, run by research-stream-sync.timer.
#
#   sync           pull new Scholar alerts from IMAP into the ledger
#   enrich-pending look up DOIs, abstracts, and open-access locations
#   fetch          download PDFs for papers already accepted
#   upload         send staged PDFs to their chosen Drive folders
#
# Triage and filing are deliberately NOT here: those are decisions, and they
# happen in the web app whenever you get to them. This job only does the work
# that needs no judgement.
#
# Each step is allowed to fail without killing the run. A morning where
# OpenAlex is out of budget should still sync mail; a Drive outage should not
# stop tomorrow's alerts from arriving.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PG_DATA="${PG_DATA:-$HOME/.local/share/paper-grabber}"
LEDGER="${PG_LEDGER:-$PG_DATA/state.db}"
STAGING="${PG_STAGING:-$PG_DATA/staging}"
CACHE="${PG_CACHE:-$HOME/.cache/paper-grabber/openalex.db}"
MAILTO="${OPENALEX_MAILTO:-}"

if [[ -x "$REPO/.venv/bin/paper-grabber" ]]; then
  PG="$REPO/.venv/bin/paper-grabber"
elif command -v paper-grabber >/dev/null 2>&1; then
  PG="$(command -v paper-grabber)"
else
  echo "paper-grabber not found" >&2
  exit 1
fi

mkdir -p "$PG_DATA" "$STAGING" "$(dirname "$CACHE")"

mailto_arg=()
[[ -n "$MAILTO" ]] && mailto_arg=(--mailto "$MAILTO")

failed=0

step() {
  local label="$1"; shift
  echo "--- $label"
  # Capture the status directly: `if "$@"` consumes it and $? is then the
  # status of the `if`, which is always 0.
  local code=0
  "$@" || code=$?
  if (( code )); then
    echo "--- $label failed (exit $code); continuing" >&2
    failed=$((failed + 1))
  fi
  return 0
}

step "sync" \
  "$PG" sync --ledger "$LEDGER"

step "enrich" \
  "$PG" enrich-pending --ledger "$LEDGER" --cache "$CACHE" "${mailto_arg[@]}"

step "fetch" \
  "$PG" fetch --ledger "$LEDGER" --staging "$STAGING"

# Non-interactive: this runs with no terminal, so a missing Drive token must
# fail fast rather than block forever waiting for a browser that cannot open.
step "upload" \
  "$PG" upload --ledger "$LEDGER" --staging "$STAGING" --non-interactive

if (( failed )); then
  echo "$failed step(s) failed" >&2
  exit 1
fi
echo "done"
