#!/usr/bin/env bash
#
# Drive the pipeline by hand, using the fixture alerts in tests/data.
# No Google credentials needed -- this stands in for `paper-grabber sync`
# until the app password and Drive client are set up.
#
#   ./scripts/demo.sh            # seed, enrich, then serve
#   ./scripts/demo.sh seed       # load fixture alerts into a fresh ledger
#   ./scripts/demo.sh enrich     # look up DOIs, abstracts, OA locations
#   ./scripts/demo.sh show       # print pending papers
#   ./scripts/demo.sh serve      # run the triage UI
#   ./scripts/demo.sh download   # fetch open-access PDFs into staging
#   ./scripts/demo.sh status     # counts, staging contents, addresses
#   ./scripts/demo.sh reset      # delete the demo ledger and staging
#
# The demo state lives under a directory of its own so it can be thrown away
# without touching a real ledger:
#   PG_HOME=~/some/other/dir ./scripts/demo.sh

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PG_HOME="${PG_HOME:-$HOME/.local/share/paper-grabber/demo}"
LEDGER="$PG_HOME/state.db"
STAGING="$PG_HOME/staging"
# The OpenAlex cache is deliberately kept outside PG_HOME so `reset` does not
# throw it away: lookups cost money, and re-fetching them is pure waste.
CACHE="${PG_CACHE:-$HOME/.cache/paper-grabber/openalex.db}"
MAILTO="${OPENALEX_MAILTO:-snunezcr@gmail.com}"
PORT="${PG_PORT:-8823}"

# --- plumbing ---------------------------------------------------------------

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

if [[ -x "$REPO/.venv/bin/paper-grabber" ]]; then
  PG="$REPO/.venv/bin/paper-grabber"
  PY="$REPO/.venv/bin/python"
elif command -v paper-grabber >/dev/null 2>&1; then
  PG="$(command -v paper-grabber)"
  PY="$(command -v python3)"
else
  die "paper-grabber not found. Run: uv venv && uv pip install -e '.[dev]'"
fi

mkdir -p "$PG_HOME"

lan_urls() {
  # Skip loopback and Docker's bridge; neither is reachable from the tablet.
  ip -4 -o addr show scope global 2>/dev/null \
    | awk '{split($4,a,"/"); print a[1]}' \
    | grep -v '^172\.1[7-9]\.\|^172\.2[0-9]\.\|^172\.3[01]\.' \
    || true
}

# --- steps ------------------------------------------------------------------

cmd_seed() {
  bold "Seeding ledger from tests/data/*.eml"
  [[ -n "$(echo tests/data/*.eml)" ]] || die "no fixtures in tests/data"

  LEDGER="$LEDGER" "$PY" - <<'PYEOF'
import glob, os
from paper_grabber.parse import parse_alert_email, dedupe
from paper_grabber.ledger import Ledger

paths = sorted(glob.glob("tests/data/*.eml"))
with Ledger(os.environ["LEDGER"]) as led:
    for path in paths:
        added = sum(led.record(p) for p in dedupe(parse_alert_email(open(path, "rb").read())))
        print(f"  {added:3} new from {os.path.basename(path)}")
    counts = led.counts()
print(f"  ledger: {counts}")
PYEOF
  echo "  -> $LEDGER"
}

cmd_enrich() {
  [[ -f "$LEDGER" ]] || die "no ledger yet; run: $0 seed"
  bold "Enriching against OpenAlex (cached in $CACHE)"
  "$PG" enrich-pending --ledger "$LEDGER" --cache "$CACHE" --mailto "$MAILTO"
}

cmd_show() {
  [[ -f "$LEDGER" ]] || die "no ledger yet; run: $0 seed"
  "$PG" pending --ledger "$LEDGER"
}

cmd_download() {
  [[ -f "$LEDGER" ]] || die "no ledger yet; run: $0 seed"
  bold "Downloading open-access PDFs into $STAGING"
  # Uses the fixtures directly: `download` runs its own parse+enrich pass
  # rather than reading the ledger, so it works standalone.
  "$PG" download --dest "$STAGING" --cache "$CACHE" --mailto "$MAILTO" tests/data/*.eml
}

cmd_serve() {
  [[ -f "$LEDGER" ]] || die "no ledger yet; run: $0 seed"
  bold "Triage UI on port $PORT"
  echo "  local:  http://localhost:$PORT"
  while read -r ip; do
    [[ -n "$ip" ]] && echo "  tablet: http://$ip:$PORT"
  done < <(lan_urls)
  echo
  echo "  On the tablet: Chrome menu -> Add to Home screen to install as a PWA."
  echo "  Ctrl-C to stop."
  echo
  "$PG" serve --ledger "$LEDGER" --port "$PORT"
}

cmd_status() {
  bold "Demo state"
  echo "  home:    $PG_HOME"
  echo "  ledger:  $LEDGER"
  if [[ -f "$LEDGER" ]]; then
    LEDGER="$LEDGER" "$PY" - <<'PYEOF'
import os
from paper_grabber.ledger import Ledger
with Ledger(os.environ["LEDGER"]) as led:
    counts = led.counts()
    pending = led.pending()
    enriched = sum(1 for p in pending if p.payload.get("enrichment"))
    print(f"  counts:  {counts or '{}'}")
    print(f"  enriched: {enriched}/{len(pending)} pending")
PYEOF
  else
    echo "  counts:  (no ledger; run: $0 seed)"
  fi

  echo "  staging: $STAGING"
  if [[ -d "$STAGING" ]]; then
    local n
    n="$(find "$STAGING" -maxdepth 1 -name '*.pdf' | wc -l)"
    echo "           $n PDFs, $(du -sh "$STAGING" 2>/dev/null | cut -f1)"
  else
    echo "           (empty)"
  fi

  echo "  cache:   $CACHE $( [[ -f "$CACHE" ]] && echo "(kept across resets)" || echo "(none yet)" )"
}

cmd_reset() {
  # Deliberately narrow: only ever removes the demo directory, never a real
  # ledger and never the OpenAlex cache.
  if [[ ! -d "$PG_HOME" ]]; then
    echo "nothing to reset"
    return
  fi
  warn "This deletes $PG_HOME (ledger + staged PDFs). The OpenAlex cache is kept."
  read -rp "Proceed? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || { echo "cancelled"; return; }
  rm -rf "$PG_HOME"
  echo "removed $PG_HOME"
}

cmd_all() {
  cmd_seed
  echo
  cmd_enrich
  echo
  cmd_serve
}

# --- dispatch ---------------------------------------------------------------

case "${1:-all}" in
  seed)     cmd_seed ;;
  enrich)   cmd_enrich ;;
  show)     cmd_show ;;
  download) cmd_download ;;
  serve)    cmd_serve ;;
  status)   cmd_status ;;
  reset)    cmd_reset ;;
  all)      cmd_all ;;
  -h|--help|help)
    # Print the header comment block: every comment line after the shebang,
    # stopping at the first line that is not a comment.
    awk 'NR==1 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "${BASH_SOURCE[0]}"
    ;;
  *) die "unknown command: $1  (try: $0 --help)" ;;
esac
