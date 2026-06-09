#!/usr/bin/env bash
# colabctl — Phase 0 validation spike runner.
#
# Captures the live behavior of the official `google-colab-cli` against YOUR Colab Pro
# account so the spec rests on facts. Everything is tee'd to spikes/phase0-results.txt —
# that single file is what you paste back.
#
# PREREQUISITE: complete auth FIRST (see spikes/PHASE0.md, Step 2). This script does NOT
# do interactive login; it verifies auth via `colab whoami` and bails early if missing.
#
# Usage:
#   bash spikes/run_phase0.sh                 # T4 only (cheapest; ~a few compute units)
#   SPIKE_A100=1 bash spikes/run_phase0.sh    # also try to allocate an A100 (burns CUs fast)
#   SPIKE_DRIVE=1 bash spikes/run_phase0.sh   # also test Drive mount round-trip
#
# Safe to re-run. It always tears down sessions it created at the end.

set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS="$HERE/phase0-results.txt"
PROBE="$HERE/gpu_probe.py"
T4_SESSION="spk-t4"
A100_SESSION="spk-a100"

# fresh results file
: > "$RESULTS"

log()  { printf '\n%s\n' "$*" | tee -a "$RESULTS" ; }
hdr()  { log "################################################################"; log "## $*"; log "################################################################"; }
# cap: echo the command, then run it capturing stdout+stderr to console+file
cap()  {
  printf '\n$ %s\n' "$*" | tee -a "$RESULTS"
  # shellcheck disable=SC2068
  ( "$@" ) 2>&1 | tee -a "$RESULTS"
  local rc=${PIPESTATUS[0]}
  printf '[exit=%s]\n' "$rc" | tee -a "$RESULTS"
  return "$rc"
}

hdr "colabctl Phase 0 — environment"
cap date -u
cap uname -a
cap which colab || true
cap which uv || true
cap which gcloud || true

# --- 0. tool present? --------------------------------------------------------
if ! command -v colab >/dev/null 2>&1; then
  log "!! 'colab' not on PATH. Install it first (PHASE0.md Step 1):"
  log "     uv tool install --python 3.13 'google-colab-cli==0.5.7'"
  exit 1
fi

hdr "1. CLI identity & full command surface (recorded live)"
cap colab version
cap colab --help
for c in new exec run upload download ls stop sessions status install drivemount repl log url; do
  cap colab "$c" --help || true
done

hdr "2. Auth check (verifies scopes incl. colaboratory)"
log "If this fails, do auth per PHASE0.md Step 2, then re-run."
cap colab whoami || { log "!! Not authenticated — stopping before allocation. See PHASE0.md Step 2."; exit 1; }

hdr "3. CPU smoke test (cheapest end-to-end: new -> exec -> stop)"
log "Tests the basic pipeline before spending GPU compute units."
echo 'print("colabctl cpu smoke:", 6*7)' | cap colab run - || true

hdr "4. T4 GPU session — allocate, probe, file round-trip, lifecycle"
cap colab new -s "$T4_SESSION" --gpu T4
cap colab status -s "$T4_SESSION" || true
log ">> running GPU probe on the VM (spikes/gpu_probe.py)"
cap colab exec -s "$T4_SESSION" -f "$PROBE" || true

log ">> file upload/download round-trip"
TESTFILE="$HERE/phase0_upload_test.txt"
echo "colabctl-roundtrip-$(date -u +%s)" > "$TESTFILE"
cap colab upload -s "$T4_SESSION" "$TESTFILE" "content/phase0_upload_test.txt" || true
cap colab ls -s "$T4_SESSION" content || true
cap colab download -s "$T4_SESSION" "content/phase0_upload_test.txt" "$HERE/phase0_download_test.txt" || true
log ">> diff (empty == identical round-trip):"
cap diff "$TESTFILE" "$HERE/phase0_download_test.txt" || log "(diff reported a difference or missing file — see above)"

log ">> can we retrieve the plot the probe saved to /content?"
cap colab download -s "$T4_SESSION" "content/colabctl_probe_plot.png" "$HERE/phase0_probe_plot.png" || true

log ">> session listing & history export"
cap colab sessions || true
cap colab log -s "$T4_SESSION" -n 50 || true

# --- 5. optional A100 -------------------------------------------------------
if [ "${SPIKE_A100:-0}" = "1" ]; then
  hdr "5. A100 session (OPTIONAL — burns compute units fast)"
  cap colab new -s "$A100_SESSION" --gpu A100 || log "(A100 allocation failed — record the exact message/quota outcome above)"
  cap colab exec -s "$A100_SESSION" -f "$PROBE" || true
  cap colab stop -s "$A100_SESSION" || true
else
  log ""
  log ">> A100 test skipped. Re-run with SPIKE_A100=1 to attempt it (uses more compute units)."
fi

# --- 6. optional Drive ------------------------------------------------------
if [ "${SPIKE_DRIVE:-0}" = "1" ]; then
  hdr "6. Drive mount round-trip (OPTIONAL)"
  cap colab drivemount -s "$T4_SESSION" || true
  echo 'open("/content/drive/MyDrive/colabctl_drive_test.txt","w").write("ok"); print("drive write ok")' \
    | cap colab exec -s "$T4_SESSION" - || true
fi

# --- 7. teardown (always) ---------------------------------------------------
hdr "7. Teardown (always runs — avoid leaving idle VMs / burning CUs)"
cap colab stop -s "$T4_SESSION" || true
cap colab sessions || true
log ""
hdr "DONE — paste the whole of spikes/phase0-results.txt back."
log "Also note anything the browser/CLI showed that didn't land in this file"
log "(e.g. the OAuth consent screen warnings, or which --auth mode you used)."
