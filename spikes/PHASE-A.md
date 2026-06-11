# Phase A — Spikes & Validation (runbook)

The 1x→10x plan (`docs/plan.md` §6) gates several load-bearing design choices on live
behavior we have not yet verified. This is the runbook for those spikes. They are
**hand-run** against a real Colab Pro account (ADC auth, exactly as Phase 0), they are
**cheap** (the runtime probes share a single T4 allocation), and they **always tear the
runtime down**. Record outcomes in [`PHASE-A-FINDINGS.md`](./PHASE-A-FINDINGS.md).

## Prereqs

Same as Phase 0:

```bash
gcloud auth application-default login \
  --scopes=openid,https://www.googleapis.com/auth/cloud-platform,\
https://www.googleapis.com/auth/userinfo.email,\
https://www.googleapis.com/auth/colaboratory,\
https://www.googleapis.com/auth/drive.file
uv sync --extra native --extra browser
```

## The spikes

| # | Script (mode) | Validates | Gates (plan ref) |
|---|---|---|---|
| ① | `phase_a_runtime.py contents` | Jupyter **contents REST API** reachable through the runtime proxy (3 auth placements) | **Pillar 3a** — REST chunked transfer vs. the committed chunked-kernel-exec fallback |
| ② | `phase_a_runtime.py refresh` | Re-assigning with the **same `nbh`** returns the same runtime with a **fresh proxy token** | **§5.10** — non-disruptive `refresh_before_expiry` vs. disruptive re-assign |
| ③ | `phase_a_runtime.py reconnect` | Drop the kernel websocket, **re-dial the same `kernel_id`**, state intact | **§5.6** + the "connection is not the data plane" thesis |
| ④ | `phase_a_runtime.py kernels` | `GET /api/kernels` and `POST /api/kernels/{id}/interrupt` reachable through the proxy | **§5.3** interrupt |
| ⑤ | `phase_a_runtime.py a100` | A100 entitlement on this account right now (or HTTP 400) | carried Phase-0 TODO |
| ⑥ | `phase_a_keepalive.py cookie` | **SAPISIDHASH** session-cookie keep-alive RPC succeeds (header/cookie matrix) | **Track B** (owner D2) |
| ⑦ | `phase_a_keepalive.py idle --mode {activity,silent}` | How long a runtime survives, with vs. without kernel-activity pings (the unverified 90-min idle window) | keep-alive necessity; **Pillar 2** auto-resume sizing |
| ⑧ | `phase_a_sidecar.py` | The **real** colab-mcp frontend JSON-RPC protocol (capture, don't guess) | **Track A** sidecar + corrects P14's guessed shapes |
| ⑨ | `phase_a_runtime.py transfer` | **Chunked** contents-API upload + ranged download round-trip (✅ PASS 2026-06-10) | **Pillar 3a** chunked/ranged paths |
| ⑩ | `phase_a_drive.py` | **Runtime-direct Drive checkpoint**: inject token → resumable upload → ranged download → SHA-256 verify on the VM (touches your Drive) | **Pillar 3b** |
| canary | `canary.py` | Ongoing **drift + health** monitor: allocate→fingerprint raw shapes→exec→transfer→teardown + CLI version probe; exit 0/1. First run establishes `canary-baseline.json` (commit it). Also runs weekly in CI when `GOOGLE_ADC_JSON` is set. | §5.12 |

## Running

```bash
# ①–⑤ all share ONE T4 allocation (cheapest). Run all, or name a subset:
uv run --extra native python spikes/phase_a_runtime.py
uv run --extra native python spikes/phase_a_runtime.py contents kernels

# ⑥ Track B — needs an exported cookie jar (gray-area, opt-in). cookies.json may be a
#    flat {name: value} map, a browser-extension export list, or a Netscape cookies.txt.
COLABCTL_COOKIE_FILE=cookies.json uv run --extra native python spikes/phase_a_keepalive.py cookie

# ⑦ LONG (up to ~2h). Run activity and silent in separate sessions to compare.
uv run --extra native python spikes/phase_a_keepalive.py idle --mode activity --interval 300
uv run --extra native python spikes/phase_a_keepalive.py idle --mode silent   --interval 300

# ⑧ opens a Colab tab; finish login, watch the frames, optionally type a JSON-RPC line.
uv run --extra browser python spikes/phase_a_sidecar.py
```

Each runtime script prints a `PASS/FAIL/UNKNOWN` summary and a raw-JSON block to paste
into the findings doc. **Decisions to capture:**

- ① PASS → build Pillar 3a on the contents API; FAIL → ship the chunked-kernel-exec
  fallback (design already committed, so neither outcome strands us).
- ② PASS → token refresh is non-disruptive (rename `reassign_before_expiry` →
  `refresh_before_expiry`); PARTIAL/FAIL → keep re-assign and document the ceiling.
- ③ FAIL would invalidate the cheap-reconnect assumption — escalate before Pillar 2.
- ⑥/⑦ jointly size how hard keep-alive must work and how often auto-resume will fire.

## Safety notes

- The cookie path (⑥) is **gray-area** and opt-in; cookies are read once and never
  written to disk by the spike. Treat the export file as a secret and delete it after.
- All scripts unassign on exit; if one is killed mid-run, `colabctl gc` (once landed)
  or `phase_a_runtime.py` rerun + manual unassign reclaims any orphan.
